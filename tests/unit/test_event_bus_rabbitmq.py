"""RabbitMQEventBus unit tests with faked aio-pika objects (no broker)."""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

from shared.event_bus import DLQ_NAME, EXCHANGE_NAME, MAX_DELIVERY_ATTEMPTS, RabbitMQEventBus
from shared.models import new_event


class FakeExchange:
    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def publish(self, message, routing_key: str) -> None:
        self.published.append((message, routing_key))


class FakeQueue:
    def __init__(self) -> None:
        self.bindings: list[tuple[object, str]] = []
        self.consumer = None

    async def bind(self, exchange, routing_key: str) -> None:
        self.bindings.append((exchange, routing_key))

    async def consume(self, callback) -> None:
        self.consumer = callback


class FakeChannel:
    def __init__(self) -> None:
        self.queues: dict[str, FakeQueue] = {}
        self.declared: list[tuple[str, dict]] = []

    async def set_qos(self, prefetch_count: int) -> None:
        pass

    async def declare_exchange(self, name, type_, durable=False, **kw):
        return FakeExchange()

    async def declare_queue(self, name, durable=False, arguments=None, **kw):
        self.declared.append((name, arguments or {}))
        queue = FakeQueue()
        self.queues[name] = queue
        return queue


class FakeMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body

    @contextlib.asynccontextmanager
    async def process(self, requeue=True, ignore_processed=True):
        yield


async def test_publish_validates_and_routes_by_event_type():
    bus = RabbitMQEventBus("amqp://unused")
    bus._exchange = FakeExchange()

    event = new_event(
        "ticket.created", "t-1",
        {"customer_id": "c", "category": "x", "description": "d"},
    )
    await bus.publish(event)

    message, routing_key = bus._exchange.published[0]
    assert routing_key == "ticket.created"
    assert json.loads(message.body)["ticket_id"] == "t-1"
    assert message.headers["x-ticket-id"] == "t-1"


async def test_publish_rejects_non_conforming_event():
    import pytest

    from shared.models import EventValidationError

    bus = RabbitMQEventBus("amqp://unused")
    bus._exchange = FakeExchange()
    with pytest.raises(EventValidationError):
        await bus.publish({"event_type": "ticket.created"})
    assert bus._exchange.published == []


async def test_subscribe_declares_quorum_queue_with_dlq_and_delivery_limit():
    bus = RabbitMQEventBus("amqp://unused")
    bus._channel = FakeChannel()
    bus._exchange = FakeExchange()

    handled: list = []

    async def handler(event):
        handled.append(event)

    await bus.subscribe("my-queue", ["ticket.created", "ticket.triaged"], handler)

    name, arguments = bus._channel.declared[0]
    assert name == "my-queue"
    assert arguments["x-queue-type"] == "quorum"
    assert arguments["x-delivery-limit"] == MAX_DELIVERY_ATTEMPTS
    assert arguments["x-dead-letter-routing-key"] == DLQ_NAME

    queue = bus._channel.queues["my-queue"]
    assert [key for _, key in queue.bindings] == ["ticket.created", "ticket.triaged"]

    # valid message -> handler invoked with a validated Event
    valid = new_event(
        "ticket.created", "t-9",
        {"customer_id": "c", "category": "x", "description": "d"},
    )
    await queue.consumer(FakeMessage(valid.model_dump_json().encode()))
    assert handled[0].ticket_id == "t-9"


async def test_consumer_rejects_invalid_payload_and_emits_alert():
    bus = RabbitMQEventBus("amqp://unused")
    bus._channel = FakeChannel()
    bus._exchange = FakeExchange()

    alerts: list = []
    bus.add_alert_handler(alerts.append)

    async def handler(event):
        raise AssertionError("handler must not run for invalid events")

    await bus.subscribe("q", ["ticket.created"], handler)
    queue = bus._channel.queues["q"]

    await queue.consumer(FakeMessage(b"not json at all"))
    await queue.consumer(FakeMessage(json.dumps({"event_type": "ticket.created"}).encode()))

    assert len(alerts) == 2
    assert all(a.event_type == "system.invalid_event" for a in alerts)
    assert alerts[0].payload["violation_description"]


async def test_async_alert_handlers_are_awaited():
    bus = RabbitMQEventBus("amqp://unused")
    received: list = []

    async def async_handler(alert):
        received.append(alert)

    bus.add_alert_handler(async_handler)
    await bus._emit_invalid_event(b"raw-bytes", "broken")
    assert len(received) == 1
    assert received[0].payload["raw_payload"] == "raw-bytes"


async def test_connect_builds_topology(monkeypatch):
    import shared.event_bus as eb

    declared = {}

    class FakeConnection:
        async def channel(self):
            return FakeChannel()

        async def close(self):
            declared["closed"] = True

    async def fake_connect_robust(url):
        declared["url"] = url
        return FakeConnection()

    fake_aio_pika = SimpleNamespace(
        connect_robust=fake_connect_robust,
        ExchangeType=SimpleNamespace(TOPIC="topic"),
    )
    monkeypatch.setitem(__import__("sys").modules, "aio_pika", fake_aio_pika)

    bus = eb.RabbitMQEventBus("amqp://broker:5672/")
    await bus.connect()
    assert declared["url"] == "amqp://broker:5672/"
    assert bus._exchange is not None
    await bus.close()
    assert declared.get("closed")
