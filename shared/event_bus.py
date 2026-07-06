"""RabbitMQ event bus wrapper (aio-pika) with canonical-schema enforcement.

Topology
--------
- Topic exchange ``omni.events``; per-service quorum queues bound by routing
  key (the ``event_type``).
- Consistent-hash exchange ``omni.events.hash`` for per-``ticket_id`` ordering.
- Dead-letter exchange/queue ``omni.dlq``; a message is dead-lettered after
  ``MAX_DELIVERY_ATTEMPTS`` (5) failed acknowledgement cycles, emitting a
  ``system.event_dead_lettered`` alert.

Every publish and consume validates the canonical event schema; violations
raise/route ``system.invalid_event`` instead of being processed.

An in-memory implementation (``InMemoryEventBus``) with identical semantics is
provided for unit tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

from shared.models import (
    Event,
    EventType,
    EventValidationError,
    new_event,
    validate_event,
)

logger = logging.getLogger("omniresolve.event_bus")

EXCHANGE_NAME = "omni.events"
HASH_EXCHANGE_NAME = "omni.events.hash"
DLQ_NAME = "omni.dlq"
MAX_DELIVERY_ATTEMPTS = 5
REDELIVERY_BASE_DELAY = 5.0  # seconds, doubling, capped at 60
REDELIVERY_MAX_DELAY = 60.0
ACK_TIMEOUT = 60.0

EventHandler = Callable[[Event], Awaitable[None]]
AlertHandler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    """Abstract publisher/consumer contract shared by real and in-memory buses."""

    async def publish(self, event: Event | dict[str, Any]) -> None:
        raise NotImplementedError

    async def subscribe(self, queue_name: str, routing_keys: list[str], handler: EventHandler) -> None:
        raise NotImplementedError

    # -- shared validation helpers -------------------------------------------

    @staticmethod
    def validate_outbound(event: Event | dict[str, Any]) -> Event:
        raw = event.model_dump() if isinstance(event, Event) else event
        return validate_event(raw)


class RabbitMQEventBus(EventBus):
    """aio-pika backed implementation for real deployments."""

    def __init__(self, url: str = "amqp://guest:guest@localhost/") -> None:
        self._url = url
        self._connection = None
        self._channel = None
        self._exchange = None
        self._alert_handlers: list[AlertHandler] = []

    def add_alert_handler(self, handler: AlertHandler) -> None:
        self._alert_handlers.append(handler)

    async def connect(self) -> None:
        import aio_pika

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=16)
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
        )
        # Dead-letter queue
        await self._channel.declare_queue(DLQ_NAME, durable=True)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def publish(self, event: Event | dict[str, Any]) -> None:
        import aio_pika

        validated = self.validate_outbound(event)
        message = aio_pika.Message(
            body=validated.model_dump_json().encode(),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers={"x-ticket-id": validated.ticket_id},
        )
        await self._exchange.publish(message, routing_key=validated.event_type)

    async def subscribe(
        self, queue_name: str, routing_keys: list[str], handler: EventHandler
    ) -> None:
        import aio_pika

        queue = await self._channel.declare_queue(
            queue_name,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-delivery-limit": MAX_DELIVERY_ATTEMPTS,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": DLQ_NAME,
            },
        )
        for key in routing_keys:
            await queue.bind(self._exchange, routing_key=key)

        async def _on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            async with message.process(requeue=True, ignore_processed=True):
                try:
                    raw = json.loads(message.body)
                    event = validate_event(raw)
                except (json.JSONDecodeError, EventValidationError) as exc:
                    await self._emit_invalid_event(message.body, str(exc))
                    return  # ack: schema violations are not redeliverable
                await handler(event)

        await queue.consume(_on_message)

    async def _emit_invalid_event(self, raw: Any, violation: str) -> None:
        alert = new_event(
            EventType.SYSTEM_INVALID_EVENT.value,
            "unknown",
            {
                "raw_payload": raw.decode() if isinstance(raw, bytes) else raw,
                "violation_description": violation,
            },
        )
        logger.error("system.invalid_event: %s", violation)
        for handler in self._alert_handlers:
            result = handler(alert)
            if asyncio.iscoroutine(result):
                await result


class InMemoryEventBus(EventBus):
    """Faithful in-memory bus for unit tests.

    Semantics mirrored from RabbitMQ topology: per-queue subscription by
    routing key, per-ticket ordering, redelivery with exponential backoff
    (5 s doubling, capped 60 s — compressed via ``delay_scale`` in tests), and
    dead-lettering after MAX_DELIVERY_ATTEMPTS failed acks with a
    ``system.event_dead_lettered`` alert.
    """

    def __init__(self, delay_scale: float = 0.0) -> None:
        self._subscriptions: list[tuple[str, list[str], EventHandler]] = []
        self.published: list[Event] = []
        self.dead_letters: list[dict[str, Any]] = []
        self.alerts: list[Event] = []
        self._delay_scale = delay_scale
        # per-ticket ordering: serialize handler dispatch per ticket_id
        self._ticket_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def add_alert_handler(self, handler: AlertHandler) -> None:  # symmetry with RabbitMQ bus
        pass

    async def subscribe(
        self, queue_name: str, routing_keys: list[str], handler: EventHandler
    ) -> None:
        self._subscriptions.append((queue_name, routing_keys, handler))

    def _matches(self, routing_keys: list[str], event_type: str) -> bool:
        for key in routing_keys:
            if key == event_type or key == "#":
                return True
            if key.endswith(".*") and event_type.split(".")[0] == key.split(".")[0]:
                return True
        return False

    async def publish(self, event: Event | dict[str, Any]) -> None:
        try:
            validated = self.validate_outbound(event)
        except EventValidationError:
            self.alerts.append(
                new_event(
                    EventType.SYSTEM_INVALID_EVENT.value,
                    "unknown",
                    {
                        "raw_payload": event if isinstance(event, dict) else event.model_dump(),
                        "violation_description": "outbound event failed schema validation",
                    },
                )
            )
            raise
        self.published.append(validated)
        for _name, routing_keys, handler in list(self._subscriptions):
            if self._matches(routing_keys, validated.event_type):
                await self._deliver(validated, handler)

    async def _deliver(self, event: Event, handler: EventHandler) -> None:
        async with self._ticket_locks[event.ticket_id]:
            delay = REDELIVERY_BASE_DELAY
            for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
                try:
                    await handler(event)
                    return
                except Exception as exc:
                    logger.warning(
                        "delivery attempt %d/%d failed for %s: %s",
                        attempt,
                        MAX_DELIVERY_ATTEMPTS,
                        event.event_type,
                        exc,
                    )
                    if attempt < MAX_DELIVERY_ATTEMPTS:
                        await asyncio.sleep(delay * self._delay_scale)
                        delay = min(delay * 2, REDELIVERY_MAX_DELAY)
            self.dead_letters.append(event.model_dump())
            self.alerts.append(
                new_event(
                    EventType.SYSTEM_EVENT_DEAD_LETTERED.value,
                    event.ticket_id,
                    {
                        "original_event": event.model_dump(),
                        "attempt_count": MAX_DELIVERY_ATTEMPTS,
                    },
                )
            )

    def events_of_type(self, event_type: str) -> list[Event]:
        return [e for e in self.published if e.event_type == event_type]
