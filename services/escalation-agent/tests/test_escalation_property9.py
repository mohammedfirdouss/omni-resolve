"""Property test for escalation idempotency (Property 9)."""

from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings, strategies as st

from escalation_helpers import escalation_event, make_env, seed_pending_ticket

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)


# Feature: omni-resolve, Property 9: Escalation idempotency
@SETTINGS
@given(n=st.integers(min_value=1, max_value=20))
def test_property_9_n_deliveries_one_queue_entry(n):
    async def case():
        env = await make_env()
        ticket_id = await seed_pending_ticket(env)
        event = escalation_event(ticket_id)

        for _ in range(n):
            await env.agent.handle_event(event)

        # exactly one entry in the human agent queue
        assert len(env.queue) == 1
        assert len(env.queue.entries_for(ticket_id)) == 1
        # exactly one ticket.escalated published and one state transition
        assert len(env.bus.events_of_type("ticket.escalated")) == 1
        from sqlalchemy import func, select

        from shared.db import TicketStateTransition

        async with env.sessionmaker() as session:
            escalated_transitions = (
                await session.execute(
                    select(func.count(TicketStateTransition.id)).where(
                        TicketStateTransition.ticket_id == ticket_id,
                        TicketStateTransition.new_state == "escalated",
                    )
                )
            ).scalar()
        assert escalated_transitions == 1
        await env.dispose()

    asyncio.run(case())
