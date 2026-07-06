"""Property 10 — state transition log is complete and append-only (task 12.2)."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import (
    Base,
    InvalidStateTransition,
    Ticket,
    TicketStateTransition,
    create_ticket,
    record_state_transition,
)
from shared.models import TERMINAL_STATES, is_valid_transition

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

# All valid paths through the status state machine to a terminal state.
VALID_WORKFLOWS = [
    ["triaged", "resolved"],
    ["triaged", "escalated"],
    ["escalated"],
    ["triaged", "execution_failed", "escalated"],
]

ALL_STATES = ["pending", "triaged", "resolved", "escalated", "execution_failed"]


async def make_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# Feature: omni-resolve, Property 10: State transition log is complete and append-only
@SETTINGS
@given(workflow=st.sampled_from(VALID_WORKFLOWS), agent=st.sampled_from(
    ["triage-agent", "execution-agent", "escalation-agent"]))
def test_property_10_transition_log_is_contiguous_valid_path(workflow, agent):
    async def case():
        engine, sessionmaker = await make_db()
        async with sessionmaker() as session:
            ticket = await create_ticket(
                session, customer_id="c", category="x", description="d"
            )
            await session.flush()
            for state in workflow:
                await record_state_transition(
                    session, ticket_id=ticket.ticket_id, new_state=state, triggered_by=agent
                )
            await session.commit()

            rows = (
                (await session.execute(
                    select(TicketStateTransition)
                    .where(TicketStateTransition.ticket_id == ticket.ticket_id)
                    .order_by(TicketStateTransition.id)
                )).scalars().all()
            )

        # Contiguous path from pending: first row NULL -> pending, then each
        # row's previous_state chains to the prior row's new_state, and every
        # step is legal in the state machine.
        assert rows[0].previous_state is None
        assert rows[0].new_state == "pending"
        for prev_row, row in zip(rows, rows[1:]):
            assert row.previous_state == prev_row.new_state
            assert is_valid_transition(row.previous_state, row.new_state)
        assert [r.new_state for r in rows] == ["pending", *workflow]
        assert rows[-1].new_state in TERMINAL_STATES
        await engine.dispose()

    asyncio.run(case())


# Feature: omni-resolve, Property 10: State transition log is complete and append-only
@SETTINGS
@given(
    workflow=st.sampled_from(VALID_WORKFLOWS),
    illegal_target=st.sampled_from(ALL_STATES),
)
def test_property_10_illegal_transitions_rejected_and_append_nothing(workflow, illegal_target):
    async def case():
        engine, sessionmaker = await make_db()
        async with sessionmaker() as session:
            ticket = await create_ticket(
                session, customer_id="c", category="x", description="d"
            )
            await session.flush()
            for state in workflow:
                await record_state_transition(
                    session, ticket_id=ticket.ticket_id, new_state=state, triggered_by="t"
                )
            await session.commit()

        current = workflow[-1]
        if is_valid_transition(current, illegal_target):
            await engine.dispose()
            return  # only exercising illegal moves here

        async with sessionmaker() as session:
            count_before = len(
                (await session.execute(
                    select(TicketStateTransition.id).where(
                        TicketStateTransition.ticket_id == ticket.ticket_id
                    )
                )).all()
            )
            with pytest.raises(InvalidStateTransition):
                await record_state_transition(
                    session, ticket_id=ticket.ticket_id,
                    new_state=illegal_target, triggered_by="t",
                )
            await session.rollback()
            count_after = len(
                (await session.execute(
                    select(TicketStateTransition.id).where(
                        TicketStateTransition.ticket_id == ticket.ticket_id
                    )
                )).all()
            )
        assert count_after == count_before  # nothing appended
        await engine.dispose()

    asyncio.run(case())
