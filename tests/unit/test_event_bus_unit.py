"""Event Bus unit tests (task 4.3): DLQ, per-ticket ordering, redelivery backoff."""

from __future__ import annotations

import asyncio

import pytest

from shared.event_bus import InMemoryEventBus, MAX_DELIVERY_ATTEMPTS
from shared.models import EventValidationError, new_event


def ticket_event(ticket_id: str = "t-1", n: int = 0):
    return new_event(
        "ticket.created",
        ticket_id,
        {"customer_id": f"c-{n}", "category": "refund", "description": f"d-{n}"},
    )


async def test_dead_letter_after_five_failed_acks_with_alert():
    bus = InMemoryEventBus()
    attempts = {"n": 0}

    async def failing_handler(event):
        attempts["n"] += 1
        raise RuntimeError("handler cannot process")

    await bus.subscribe("q", ["ticket.created"], failing_handler)
    event = ticket_event()
    await bus.publish(event)

    assert attempts["n"] == MAX_DELIVERY_ATTEMPTS == 5
    assert len(bus.dead_letters) == 1
    assert bus.dead_letters[0]["ticket_id"] == "t-1"

    alerts = [a for a in bus.alerts if a.event_type == "system.event_dead_lettered"]
    assert len(alerts) == 1
    assert alerts[0].payload["attempt_count"] == 5
    assert alerts[0].payload["original_event"]["ticket_id"] == "t-1"


async def test_redelivery_uses_exponential_backoff_capped_at_60s():
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def spy_sleep(seconds: float):
        delays.append(seconds)
        await real_sleep(0)

    bus = InMemoryEventBus(delay_scale=1.0)

    async def failing_handler(event):
        raise RuntimeError("nope")

    await bus.subscribe("q", ["ticket.created"], failing_handler)

    import shared.event_bus as eb
    original = eb.asyncio.sleep
    eb.asyncio.sleep = spy_sleep
    try:
        await bus.publish(ticket_event())
    finally:
        eb.asyncio.sleep = original

    # 4 sleeps between 5 attempts: 5, 10, 20, 40 (doubling, cap 60 not hit yet)
    assert delays == [5.0, 10.0, 20.0, 40.0]


async def test_per_ticket_ordering_is_preserved():
    bus = InMemoryEventBus()
    seen: list[tuple[str, int]] = []

    async def slow_handler(event):
        # Yield to the loop mid-handling; without per-ticket locking this
        # would interleave deliveries for the same ticket.
        seen.append(("start", event.payload["seq"]))
        await asyncio.sleep(0)
        seen.append(("end", event.payload["seq"]))

    await bus.subscribe("q", ["system.escalation_buffer_full"], slow_handler)

    async def publish(n: int):
        await bus.publish(
            new_event("system.escalation_buffer_full", "t-1", {"buffer_size": n, "seq": n})
        )

    await asyncio.gather(publish(0), publish(1), publish(2))

    # no interleaving: every start is immediately followed by its own end
    for i in range(0, len(seen), 2):
        assert seen[i][0] == "start" and seen[i + 1][0] == "end"
        assert seen[i][1] == seen[i + 1][1]


async def test_publish_rejects_non_conforming_event_and_emits_invalid_event():
    bus = InMemoryEventBus()
    with pytest.raises(EventValidationError):
        await bus.publish({"event_type": "ticket.created"})  # missing fields
    assert bus.published == []
    assert [a.event_type for a in bus.alerts] == ["system.invalid_event"]


async def test_routing_only_matching_subscribers_receive():
    bus = InMemoryEventBus()
    received: dict[str, list] = {"a": [], "b": []}

    async def handler_a(event):
        received["a"].append(event)

    async def handler_b(event):
        received["b"].append(event)

    await bus.subscribe("qa", ["ticket.created"], handler_a)
    await bus.subscribe("qb", ["ticket.resolved"], handler_b)
    await bus.publish(ticket_event())

    assert len(received["a"]) == 1
    assert received["b"] == []
