"""Property tests for the Execution Agent (Properties 12, 13, 14)."""

from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import select

from shared.db import ExecutionAction, Ticket
from shared.models import ACTION_TYPES

from execution_helpers import ActionServer, context_ready_event, make_env, seed_triaged_ticket

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

action_lists = st.lists(st.sampled_from(ACTION_TYPES), min_size=1, max_size=6)
failure_kinds = st.sampled_from([400, 404, 500, 503, "timeout", "error"])


# Feature: omni-resolve, Property 12: Execution actions are invoked in plan order
@SETTINGS
@given(actions=action_lists)
def test_property_12_actions_invoked_in_plan_order(actions):
    async def case():
        env = await make_env()
        ticket_id = await seed_triaged_ticket(env)
        await env.agent.handle_context_ready(context_ready_event(ticket_id, actions))
        assert env.server.calls == actions  # exact order, nothing skipped
        await env.dispose()

    asyncio.run(case())


# Feature: omni-resolve, Property 13: Execution action audit record is always complete
@SETTINGS
@given(
    actions=action_lists,
    fail_at=st.integers(min_value=0, max_value=5),
    kind=failure_kinds,
)
def test_property_13_audit_record_always_complete(actions, fail_at, kind):
    async def case():
        script = {fail_at: kind} if fail_at < len(actions) else {}
        env = await make_env(ActionServer(script=script))
        ticket_id = await seed_triaged_ticket(env)
        await env.agent.handle_context_ready(context_ready_event(ticket_id, actions))

        async with env.sessionmaker() as session:
            records = (
                (await session.execute(
                    select(ExecutionAction).where(ExecutionAction.ticket_id == ticket_id)
                )).scalars().all()
            )
        assert len(records) >= 1  # at least the first invocation is audited
        for record in records:
            assert record.action_type
            assert record.request_body is not None
            assert isinstance(record.response_status, int)
            assert record.response_body is not None
            assert record.invoked_at is not None
        await env.dispose()

    asyncio.run(case())


# Feature: omni-resolve, Property 14: Execution terminal state is correct for all outcomes
@SETTINGS
@given(
    actions=action_lists,
    fail_at=st.integers(min_value=0, max_value=7),
    kind=failure_kinds,
)
def test_property_14_terminal_state_correct(actions, fail_at, kind):
    async def case():
        fails = fail_at < len(actions)
        script = {fail_at: kind} if fails else {}
        env = await make_env(ActionServer(script=script))
        ticket_id = await seed_triaged_ticket(env)
        await env.agent.handle_context_ready(context_ready_event(ticket_id, actions))

        resolved = env.bus.events_of_type("ticket.resolved")
        escalations = env.bus.events_of_type("ticket.escalation_requested")
        async with env.sessionmaker() as session:
            ticket = await session.get(Ticket, ticket_id)

        if not fails:
            assert ticket.status == "resolved"
            assert len(resolved) == 1 and len(escalations) == 0
            assert resolved[0].payload["actions_completed"] == actions
        else:
            assert ticket.status == "execution_failed"
            assert len(escalations) == 1 and len(resolved) == 0
            # nothing after the first failure was invoked
            assert env.server.calls == actions[: fail_at + 1]
            assert escalations[0].payload["actions_completed"] == actions[:fail_at]
        await env.dispose()

    asyncio.run(case())
