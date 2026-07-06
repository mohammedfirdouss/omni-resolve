"""Unit tests for the Knowledge Agent (task 8.3)."""

from __future__ import annotations

import httpx
from sqlalchemy import select

from shared.db import AgentDecision, RetrievalRecord

from knowledge_helpers import (
    FakeQdrant,
    make_env,
    make_point,
    seed_triaged_ticket,
    triaged_event,
)


async def get_escalation_decision(env, ticket_id: str):
    async with env.sessionmaker() as session:
        decisions = (
            (await session.execute(
                select(AgentDecision).where(
                    AgentDecision.ticket_id == ticket_id,
                    AgentDecision.decision_type == "escalation",
                )
            )).scalars().all()
        )
    assert len(decisions) == 1
    return decisions[0]


async def test_timeout_escalates_with_reason_retrieval_timeout():
    env = await make_env(
        FakeQdrant([make_point("p-1", 0.9)], delay=1.0), qdrant_timeout_seconds=0.01
    )
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_event(triaged_event(ticket_id))

    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert len(escalations) == 1
    assert escalations[0].payload["reason"] == "retrieval_timeout"
    decision = await get_escalation_decision(env, ticket_id)
    assert decision.output_summary["reason"] == "retrieval_timeout"
    assert env.bus.events_of_type("ticket.context_ready") == []


async def test_zero_results_escalates_with_reason_no_policy_found():
    env = await make_env(FakeQdrant([]))
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_event(triaged_event(ticket_id))

    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert escalations[0].payload["reason"] == "no_policy_found"
    decision = await get_escalation_decision(env, ticket_id)
    assert decision.output_summary["reason"] == "no_policy_found"


async def test_unavailability_escalates_with_reason_vector_store_unavailable():
    env = await make_env(FakeQdrant(error=ConnectionError("qdrant down")))
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_event(triaged_event(ticket_id))

    escalations = env.bus.events_of_type("ticket.escalation_requested")
    assert escalations[0].payload["reason"] == "vector_store_unavailable"
    decision = await get_escalation_decision(env, ticket_id)
    assert decision.output_summary["reason"] == "vector_store_unavailable"


async def test_happy_path_publishes_context_ready_with_top5_ids():
    env = await make_env()  # default: 5 points p-0..p-4
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_event(triaged_event(ticket_id, confidence=0.88))

    ready = env.bus.events_of_type("ticket.context_ready")
    assert len(ready) == 1
    payload = ready[0].payload
    assert payload["policy_document_ids"] == [f"p-{i}" for i in range(5)]
    assert payload["resolution_plan"]["confidence_score"] == 0.88

    async with env.sessionmaker() as session:
        records = (
            (await session.execute(
                select(RetrievalRecord).where(RetrievalRecord.ticket_id == ticket_id)
            )).scalars().all()
        )
    assert len(records) == 5
    assert env.bus.events_of_type("ticket.escalation_requested") == []


async def test_circuit_open_maps_to_vector_store_unavailable():
    from shared.circuit_breaker import CircuitBreaker

    fake_now = {"t": 0.0}
    breaker = CircuitBreaker(name="qdrant", clock=lambda: fake_now["t"])
    qdrant = FakeQdrant(error=ConnectionError("down"))
    env = await make_env(qdrant, circuit_breaker=breaker)

    for _ in range(5):  # open the circuit
        ticket_id = await seed_triaged_ticket(env)
        await env.agent.handle_event(triaged_event(ticket_id))
    assert breaker.state == "open"

    calls_before = qdrant.calls
    ticket_id = await seed_triaged_ticket(env)
    await env.agent.handle_event(triaged_event(ticket_id))
    assert qdrant.calls == calls_before  # failed fast, no call
    assert (
        env.bus.events_of_type("ticket.escalation_requested")[-1].payload["reason"]
        == "vector_store_unavailable"
    )


async def test_main_health_and_metrics_endpoints():
    from knowledge_agent.main import create_app

    env = await make_env()
    app = create_app(agent=env.agent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["service"] == "knowledge-agent"
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert b"omni_requests_total" in metrics.content


async def test_build_runtime_constructs_agent_from_env(monkeypatch):
    from shared.observability import ServiceMetrics

    from knowledge_agent.main import build_runtime

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:5432/x")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost/")
    monkeypatch.setenv("QDRANT_URL", "http://localhost:6333")
    # everything constructed lazily -- no network I/O
    agent, bus = build_runtime(ServiceMetrics("knowledge-agent-test"))
    assert agent is not None and bus is not None


def test_lifespan_starts_injected_agent():
    from fastapi.testclient import TestClient

    from knowledge_agent.main import create_app

    class FakeAgent:
        started = False

        async def start(self):
            self.started = True

    fake = FakeAgent()
    app = create_app(agent=fake)
    with TestClient(app) as client:  # context manager runs lifespan
        assert client.get("/health").status_code == 200
    assert fake.started
