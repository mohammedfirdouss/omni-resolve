"""Unit tests for the Escalation Agent (task 11.3)."""

from __future__ import annotations

from sqlalchemy import select

from shared.db import EscalationOverflow, Ticket

from escalation_helpers import escalation_event, make_env, seed_pending_ticket


async def test_ticket_escalated_published_after_successful_enqueue():
    env = await make_env()
    ticket_id = await seed_pending_ticket(env)
    await env.agent.handle_event(escalation_event(ticket_id, reason="no_policy_found"))

    escalated = env.bus.events_of_type("ticket.escalated")
    assert len(escalated) == 1
    assert escalated[0].payload["queue_entry_id"]
    entry = env.queue.entries_for(ticket_id)[0]
    assert entry["reason"] == "no_policy_found"
    assert entry["confidence_score"] == 0.4
    async with env.sessionmaker() as session:
        ticket = await session.get(Ticket, ticket_id)
    assert ticket.status == "escalated"


async def test_buffer_full_alert_emitted_at_capacity():
    env = await make_env(buffer_capacity=3)
    env.queue.available = False  # human queue down -> everything buffers

    for _ in range(3):
        ticket_id = await seed_pending_ticket(env)
        await env.agent.handle_event(escalation_event(ticket_id))

    alerts = env.bus.events_of_type("system.escalation_buffer_full")
    assert len(alerts) == 1
    assert alerts[0].payload["buffer_size"] == 3
    assert len(env.agent.retry_buffer) == 3
    assert len(env.queue) == 0  # nothing reached the queue


async def test_overflow_persisted_with_escalation_pending_status():
    env = await make_env(buffer_capacity=2)
    env.queue.available = False

    ids = []
    for _ in range(4):  # 2 fill the buffer, 2 overflow to the State_Store
        ticket_id = await seed_pending_ticket(env)
        ids.append(ticket_id)
        await env.agent.handle_event(escalation_event(ticket_id))

    async with env.sessionmaker() as session:
        rows = (await session.execute(select(EscalationOverflow))).scalars().all()
    assert len(rows) == 2
    assert {r.ticket_id for r in rows} == set(ids[2:])
    for row in rows:
        assert row.status == "escalation_pending"
        assert row.payload["ticket_id"] == row.ticket_id


async def test_retry_pending_drains_buffer_and_overflow_when_queue_recovers():
    env = await make_env(buffer_capacity=2)
    env.queue.available = False
    ids = []
    for _ in range(4):
        ticket_id = await seed_pending_ticket(env)
        ids.append(ticket_id)
        await env.agent.handle_event(escalation_event(ticket_id))

    env.queue.available = True
    drained = await env.agent.retry_pending()

    assert drained == 4
    assert len(env.queue) == 4
    assert len(env.agent.retry_buffer) == 0
    async with env.sessionmaker() as session:
        rows = (await session.execute(select(EscalationOverflow))).scalars().all()
    assert rows == []
    assert len(env.bus.events_of_type("ticket.escalated")) == 4


async def test_already_escalated_ticket_is_idempotent_noop():
    from shared.db import record_state_transition

    env = await make_env()
    ticket_id = await seed_pending_ticket(env)
    # Triage already escalated it (Requirement 2.6 path).
    async with env.sessionmaker() as session:
        await record_state_transition(
            session, ticket_id=ticket_id, new_state="escalated", triggered_by="triage-agent"
        )
        await session.commit()

    await env.agent.handle_event(escalation_event(ticket_id))
    # durable dedupe: no queue entry, no crash, no extra transition
    assert len(env.queue) == 0
    assert env.bus.events_of_type("ticket.escalated") == []


async def test_unknown_ticket_still_enqueued_for_human_review():
    env = await make_env()
    await env.agent.handle_event(escalation_event("00000000-0000-4000-8000-000000000000"))
    assert len(env.queue) == 1  # a human must see it even without a DB row


async def test_retry_loop_respects_injected_interval():
    intervals: list[float] = []
    stop_after = 3

    async def spy_sleep(seconds: float) -> None:
        intervals.append(seconds)
        if len(intervals) >= stop_after:
            raise KeyboardInterrupt  # break the loop for the test

    env = await make_env(retry_interval_seconds=30.0, sleep=spy_sleep)
    try:
        await env.agent.run_retry_loop()
    except KeyboardInterrupt:
        pass
    assert intervals == [30.0, 30.0, 30.0]


async def test_main_health_and_metrics_endpoints():
    import httpx

    from escalation_agent.main import create_app

    env = await make_env()
    app = create_app(agent=env.agent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        metrics = await client.get("/metrics")
        assert b"omni_requests_total" in metrics.content


def test_lifespan_starts_and_stops_injected_agent():
    from fastapi.testclient import TestClient

    from escalation_agent.main import create_app

    import asyncio as aio

    class FakeAgent:
        started = stopped = loop_started = False

        class metrics:
            @staticmethod
            def exposition():
                return b"", "text/plain"

        async def start(self):
            FakeAgent.started = True

        def start_retry_loop(self):
            FakeAgent.loop_started = True
            return aio.get_event_loop().create_task(aio.sleep(0))

        async def stop(self):
            FakeAgent.stopped = True

    app = create_app(agent=FakeAgent())
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert FakeAgent.started and FakeAgent.loop_started and FakeAgent.stopped


async def test_build_runtime_constructs_agent_from_env(monkeypatch):
    from escalation_agent.main import build_runtime

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:x@localhost:5432/x")
    agent, bus, human_queue = build_runtime()
    assert agent is not None and bus is not None and human_queue is not None


async def test_human_queue_deterministic_entry_ids():
    from escalation_agent.human_queue import (
        InMemoryHumanQueue,
        deterministic_queue_entry_id,
    )

    queue = InMemoryHumanQueue()
    first = await queue.enqueue("t-1", {"a": 1})
    second = await queue.enqueue("t-1", {"a": 2})  # duplicate: same id, no overwrite
    assert first == second == deterministic_queue_entry_id("t-1")
    assert queue.entries["t-1"] == {"a": 1}


async def test_rabbitmq_human_queue_enqueue_and_errors():
    from escalation_agent.human_queue import (
        HumanQueueUnavailable,
        RabbitMQHumanQueue,
        deterministic_queue_entry_id,
    )

    import pytest

    queue = RabbitMQHumanQueue("amqp://x")

    # not connected -> unavailable
    with pytest.raises(HumanQueueUnavailable):
        await queue.enqueue("t-1", {"a": 1})

    published = []

    class FakeExchange:
        async def publish(self, message, routing_key):
            published.append((message, routing_key))

    class FakeChannel:
        default_exchange = FakeExchange()

    queue._channel = FakeChannel()
    entry_id = await queue.enqueue("t-1", {"a": 1})
    assert entry_id == deterministic_queue_entry_id("t-1")
    message, routing_key = published[0]
    assert routing_key == "omni.human_queue"
    assert message.message_id == entry_id

    class BrokenExchange:
        async def publish(self, message, routing_key):
            raise ConnectionError("broker gone")

    class BrokenChannel:
        default_exchange = BrokenExchange()

    queue._channel = BrokenChannel()
    with pytest.raises(HumanQueueUnavailable):
        await queue.enqueue("t-2", {"a": 2})
