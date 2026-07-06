"""Unit tests for the Triage Agent (task 6.4)."""

from __future__ import annotations

from sqlalchemy import select

from shared.circuit_breaker import CircuitBreaker
from shared.db import AgentDecision, Ticket

from triage_agent.parser import parse_resolution_plan

from triage_helpers import created_event, llm_transport, make_env, plan_json, seed_ticket


async def test_retry_exhaustion_escalates_with_status_escalated():
    counter: dict = {}
    env = await make_env(llm_transport(None, fail=True, counter=counter))
    ticket_id = await seed_ticket(env)

    await env.agent.handle_ticket_created(created_event(ticket_id))

    assert counter["n"] == 4  # initial attempt + 3 retries
    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert len(escalations) == 1
    assert escalations[0].payload["reason"] == "triage_llm_failure"
    async with env.sessionmaker() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.status == "escalated"


async def test_retry_uses_exponential_backoff_delays():
    slept: list[float] = []

    async def spy_sleep(seconds: float) -> None:
        slept.append(seconds)

    env = await make_env(llm_transport(None, fail=True), sleep=spy_sleep)
    ticket_id = await seed_ticket(env)
    await env.agent.handle_ticket_created(created_event(ticket_id))
    assert slept == [1.0, 2.0, 4.0]


async def test_agent_decision_recorded_for_every_ticket():
    env = await make_env(llm_transport(plan_json(0.9)))
    ids = [await seed_ticket(env) for _ in range(3)]
    for ticket_id in ids:
        await env.agent.handle_ticket_created(created_event(ticket_id))

    async with env.sessionmaker() as session:
        decisions = (await session.execute(select(AgentDecision))).scalars().all()
    assert {d.ticket_id for d in decisions} == set(ids)
    for decision in decisions:
        assert decision.agent == "triage-agent"
        assert decision.input_summary["category"] == "refund"
        assert float(decision.confidence_score) == 0.9


async def test_low_confidence_does_not_change_status():
    env = await make_env(llm_transport(plan_json(0.2)))
    ticket_id = await seed_ticket(env)
    await env.agent.handle_ticket_created(created_event(ticket_id))
    async with env.sessionmaker() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.status == "pending"  # Escalation Agent owns the transition


async def test_circuit_breaker_opens_after_five_consecutive_failures():
    fake_now = {"t": 0.0}
    breaker = CircuitBreaker(name="test", clock=lambda: fake_now["t"])
    counter: dict = {}
    env = await make_env(
        llm_transport(None, fail=True, counter=counter), circuit_breaker=breaker
    )

    # First ticket: 4 attempts (initial + 3 retries) = 4 consecutive failures.
    t1 = await seed_ticket(env)
    await env.agent.handle_ticket_created(created_event(t1))
    assert breaker.state == "closed"

    # Second ticket: 5th failure opens the circuit; remaining attempts fail fast.
    t2 = await seed_ticket(env)
    await env.agent.handle_ticket_created(created_event(t2))
    assert breaker.state == "open"
    assert counter["n"] == 5  # no HTTP call after the circuit opened

    # Both tickets escalated regardless.
    assert len(env.bus.events_of_type("ticket.escalation_requested")) == 2


async def test_parser_extracts_actions_in_order():
    plan = parse_resolution_plan(
        "t-1",
        plan_json(
            0.9,
            actions=[
                {"action_type": "track_order", "parameters": {"order": 1}},
                {"action_type": "send_notification", "parameters": {}},
                {"action_type": "bogus_action", "parameters": {}},  # dropped
            ],
        ),
    )
    assert [a.action_type for a in plan.actions] == ["track_order", "send_notification"]


def test_main_builds_app_with_expected_routes():
    from triage_agent.main import app

    paths = {route.path for route in app.routes}
    assert {"/health", "/metrics"} <= paths
