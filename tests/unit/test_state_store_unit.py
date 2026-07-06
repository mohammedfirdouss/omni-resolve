"""State Store audit-trail unit tests (task 12.4)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import shared.db as shared_db
from shared.db import (
    Base,
    StateWriteFailed,
    create_ticket,
    record_state_transition,
    register_state_write_failed_hook,
    with_write_retry,
)


async def make_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_state_write_failed_emitted_after_three_retries(monkeypatch):
    sleeps: list[float] = []

    async def spy_sleep(seconds: float):
        sleeps.append(seconds)

    monkeypatch.setattr(shared_db.asyncio, "sleep", spy_sleep)

    received: list[dict] = []
    register_state_write_failed_hook(lambda event: received.append(event))
    try:
        attempts = {"n": 0}

        @with_write_retry()
        async def failing_write(*, ticket_id: str, new_state: str):
            attempts["n"] += 1
            raise ConnectionError("postgres down")

        with pytest.raises(StateWriteFailed):
            await failing_write(ticket_id="t-1", new_state="escalated")

        assert attempts["n"] == 3
        assert sleeps == [5.0, 10.0]  # exponential backoff between attempts
        assert len(received) == 1
        alert = received[0]
        assert alert["event_type"] == "system.state_write_failed"
        assert alert["payload"] == {"ticket_id": "t-1", "attempted_state": "escalated"}
    finally:
        shared_db._state_write_failed_hooks.clear()


async def test_async_hooks_are_awaited(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(shared_db.asyncio, "sleep", instant)

    received: list[dict] = []

    async def async_hook(event):
        received.append(event)

    register_state_write_failed_hook(async_hook)
    try:

        @with_write_retry()
        async def failing_write(*, ticket_id: str, new_state: str):
            raise ConnectionError("down")

        with pytest.raises(StateWriteFailed):
            await failing_write(ticket_id="t-2", new_state="resolved")
        assert len(received) == 1
    finally:
        shared_db._state_write_failed_hooks.clear()


@pytest.mark.parametrize("terminal_path", [["triaged", "resolved"], ["escalated"]])
async def test_total_elapsed_seconds_recorded_on_terminal_states(terminal_path):
    engine, sessionmaker = await make_db()
    async with sessionmaker() as session:
        ticket = await create_ticket(session, customer_id="c", category="x", description="d")
        await session.flush()
        for state in terminal_path:
            await record_state_transition(
                session, ticket_id=ticket.ticket_id, new_state=state, triggered_by="t"
            )
        await session.commit()

    async with sessionmaker() as session:
        from shared.db import Ticket

        refreshed = await session.get(Ticket, ticket.ticket_id)
    assert refreshed.status == terminal_path[-1]
    assert refreshed.resolved_at is not None
    assert refreshed.total_elapsed_seconds is not None
    assert float(refreshed.total_elapsed_seconds) >= 0.0
    await engine.dispose()


async def test_non_terminal_transition_does_not_stamp_elapsed():
    engine, sessionmaker = await make_db()
    async with sessionmaker() as session:
        ticket = await create_ticket(session, customer_id="c", category="x", description="d")
        await session.flush()
        await record_state_transition(
            session, ticket_id=ticket.ticket_id, new_state="triaged", triggered_by="t"
        )
        await session.commit()

    async with sessionmaker() as session:
        from shared.db import Ticket

        refreshed = await session.get(Ticket, ticket.ticket_id)
    assert refreshed.total_elapsed_seconds is None
    assert refreshed.resolved_at is None
    await engine.dispose()


async def test_successful_write_after_transient_failure_no_alert(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(shared_db.asyncio, "sleep", instant)
    received: list[dict] = []
    register_state_write_failed_hook(lambda e: received.append(e))
    try:
        attempts = {"n": 0}

        @with_write_retry()
        async def flaky_write(*, ticket_id: str, new_state: str):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("transient")
            return "ok"

        assert await flaky_write(ticket_id="t-3", new_state="triaged") == "ok"
        assert received == []  # no alert on eventual success
    finally:
        shared_db._state_write_failed_hooks.clear()
