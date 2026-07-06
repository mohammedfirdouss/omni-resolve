"""Property 15 — GET ticket returns complete state history (task 12.3).

Exercised at the data layer: ``shared.db.get_ticket_history`` is exactly what
the API Gateway serves for ``GET /tickets/{id}`` (None -> HTTP 404); the HTTP
mapping itself is covered by the API Gateway's own test suite.
"""

from __future__ import annotations

import asyncio
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import Base, create_ticket, get_ticket_history, record_state_transition

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

WORKFLOW_PREFIXES = [
    [],
    ["triaged"],
    ["triaged", "resolved"],
    ["triaged", "execution_failed"],
    ["triaged", "execution_failed", "escalated"],
    ["escalated"],
]


async def make_db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# Feature: omni-resolve, Property 15: GET ticket returns complete state history
@SETTINGS
@given(workflow=st.sampled_from(WORKFLOW_PREFIXES))
def test_property_15_history_returns_all_n_transitions(workflow):
    async def case():
        engine, sessionmaker = await make_db()
        async with sessionmaker() as session:
            ticket = await create_ticket(session, customer_id="c", category="x", description="d")
            await session.flush()
            for state in workflow:
                await record_state_transition(
                    session, ticket_id=ticket.ticket_id, new_state=state, triggered_by="t"
                )
            await session.commit()

        async with sessionmaker() as session:
            history = await get_ticket_history(session, ticket.ticket_id)

        expected_n = 1 + len(workflow)  # initial pending row + N transitions
        assert history is not None
        assert len(history["state_transitions"]) == expected_n
        assert [t["new_state"] for t in history["state_transitions"]] == ["pending", *workflow]
        await engine.dispose()

    asyncio.run(case())


# Feature: omni-resolve, Property 15: GET ticket returns complete state history
@SETTINGS
@given(random_uuid=st.uuids(version=4))
def test_property_15_unknown_uuid_returns_none_maps_to_404(random_uuid):
    async def case():
        engine, sessionmaker = await make_db()
        async with sessionmaker() as session:
            history = await get_ticket_history(session, str(random_uuid))
        assert history is None  # API Gateway maps this to HTTP 404
        await engine.dispose()

    asyncio.run(case())
