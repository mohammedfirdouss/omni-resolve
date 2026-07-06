"""Unit tests for the Execution Agent (task 9.5)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from shared.db import ExecutionAction, Ticket

from execution_helpers import ActionServer, context_ready_event, make_env, seed_triaged_ticket

FOUR_ACTIONS = ["process_refund", "track_order", "adjust_billing", "send_notification"]


async def test_per_action_timeout_halts_remaining_actions():
    env = await make_env(ActionServer(script={1: "timeout"}))
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_context_ready(context_ready_event(ticket_id, FOUR_ACTIONS))

    assert env.server.calls == ["process_refund", "track_order"]  # halted after timeout
    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert escalations[0].payload["reason"] == "execution_timeout"
    assert escalations[0].payload["failed_action"] == "track_order"

    async with env.sessionmaker() as session:
        records = (
            (await session.execute(
                select(ExecutionAction).where(ExecutionAction.ticket_id == ticket_id)
            )).scalars().all()
        )
    timeout_record = next(r for r in records if r.action_type == "track_order")
    assert timeout_record.response_status == 504
    assert timeout_record.response_body == {"error": "timeout"}


async def test_total_timeout_halts_pending_actions_and_escalates():
    # Fake clock: each action "takes" 20 s, so the 30 s budget is blown
    # after the second action starts.
    fake_now = {"t": 0.0}

    def clock() -> float:
        fake_now["t"] += 10.0  # advances on every budget/latency check
        return fake_now["t"]

    env = await make_env(clock=clock)
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_context_ready(context_ready_event(ticket_id, FOUR_ACTIONS))

    assert len(env.server.calls) < len(FOUR_ACTIONS)  # pending actions halted
    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert len(escalations) == 1
    assert escalations[0].payload["reason"] == "execution_timeout"
    async with env.sessionmaker() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.status == "execution_failed"


async def test_4xx_halts_and_previously_completed_actions_unchanged():
    env = await make_env(ActionServer(script={2: 404}))
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_context_ready(context_ready_event(ticket_id, FOUR_ACTIONS))

    assert env.server.calls == FOUR_ACTIONS[:3]
    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert escalations[0].payload["reason"] == "action_failed"
    # completed actions are reported, not rolled back
    assert escalations[0].payload["actions_completed"] == FOUR_ACTIONS[:2]


async def test_all_four_builtin_action_types_invoked_independently():
    for action in FOUR_ACTIONS:
        env = await make_env()
        ticket_id = await seed_triaged_ticket(env)
        await env.agent.handle_context_ready(context_ready_event(ticket_id, [action]))
        assert env.server.calls == [action]
        assert len(env.bus.events_of_type("ticket.resolved")) == 1


async def test_circuit_breaker_open_fails_fast():
    from shared.circuit_breaker import CircuitBreaker

    fake_now = {"t": 0.0}
    breaker = CircuitBreaker(name="actions", clock=lambda: fake_now["t"])
    server = ActionServer(script={i: "error" for i in range(10)})
    env = await make_env(server, circuit_breaker=breaker)

    for _ in range(5):  # five consecutive failures open the circuit
        ticket_id = await seed_triaged_ticket(env)
        await env.agent.handle_context_ready(context_ready_event(ticket_id, ["track_order"]))
    assert breaker.state == "open"

    calls_before = len(env.server.calls)
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_context_ready(context_ready_event(ticket_id, ["track_order"]))
    assert len(env.server.calls) == calls_before  # no HTTP call while open
    async with env.sessionmaker() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.status == "execution_failed"


async def test_main_health_and_metrics_endpoints():
    from execution_agent.main import create_app

    env = await make_env()
    app = create_app(agent=env.agent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "execution-agent"
        metrics = await client.get("/metrics")
        assert b"omni_requests_total" in metrics.content


def test_lifespan_starts_injected_agent():
    from fastapi.testclient import TestClient

    from execution_agent.main import create_app

    class FakeAgent:
        started = False

        class metrics:
            @staticmethod
            def exposition():
                return b"", "text/plain"

        async def start(self):
            FakeAgent.started = True

    app = create_app(agent=FakeAgent())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert FakeAgent.started


async def test_build_runtime_constructs_agent_from_env(monkeypatch):
    from execution_agent.main import build_runtime

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:5432/x")
    agent, bus = build_runtime()
    assert agent is not None and bus is not None
