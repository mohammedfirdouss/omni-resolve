"""Human agent queue abstraction.

Two implementations of the same ``HumanQueue`` contract:

- ``RabbitMQHumanQueue`` — real deployment; publishes persistent messages to
  the durable queue ``omni.human_queue`` via aio-pika.
- ``InMemoryHumanQueue`` — unit tests; stores entries in a dict keyed on
  ``ticket_id``.

Both provide *idempotent* enqueue keyed on the ticket identifier
(Requirement 5.4): enqueuing the same ``ticket_id`` again returns the same
``queue_entry_id`` without creating a duplicate entry.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

HUMAN_QUEUE_NAME = "omni.human_queue"


class HumanQueueUnavailable(RuntimeError):
    """The human agent queue cannot accept entries right now."""


class HumanQueue:
    """Abstract contract for the human agent queue."""

    async def enqueue(self, ticket_id: str, entry: dict[str, Any]) -> str:
        """Idempotently enqueue ``entry`` keyed on ``ticket_id``.

        Returns a stable ``queue_entry_id``. Raises ``HumanQueueUnavailable``
        when the queue cannot be reached.
        """
        raise NotImplementedError


def deterministic_queue_entry_id(ticket_id: str) -> str:
    """Stable queue-entry id derived from the ticket id (idempotency key)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HUMAN_QUEUE_NAME}/{ticket_id}"))


class InMemoryHumanQueue(HumanQueue):
    """Test double with identical idempotency semantics.

    Set ``available = False`` to simulate an outage (raises
    ``HumanQueueUnavailable`` on enqueue).
    """

    def __init__(self) -> None:
        self.available: bool = True
        self.entries: dict[str, dict[str, Any]] = {}  # ticket_id -> entry
        self._entry_ids: dict[str, str] = {}  # ticket_id -> queue_entry_id
        self.enqueue_calls: int = 0

    async def enqueue(self, ticket_id: str, entry: dict[str, Any]) -> str:
        self.enqueue_calls += 1
        if not self.available:
            raise HumanQueueUnavailable("in-memory human queue marked unavailable")
        existing = self._entry_ids.get(ticket_id)
        if existing is not None:
            return existing
        queue_entry_id = deterministic_queue_entry_id(ticket_id)
        self._entry_ids[ticket_id] = queue_entry_id
        self.entries[ticket_id] = entry
        return queue_entry_id

    def entries_for(self, ticket_id: str) -> list[dict[str, Any]]:
        return [self.entries[ticket_id]] if ticket_id in self.entries else []

    def __len__(self) -> int:
        return len(self.entries)


class RabbitMQHumanQueue(HumanQueue):
    """aio-pika backed human agent queue (durable queue ``omni.human_queue``).

    The ``queue_entry_id`` is a deterministic UUIDv5 of the ticket id and is
    stamped as the AMQP ``message_id`` so downstream consumers (and the broker,
    when a deduplication plugin is enabled) can discard duplicates. Primary
    idempotency is enforced by the Escalation Agent itself before enqueue.
    """

    def __init__(self, url: str = "amqp://guest:guest@localhost/") -> None:
        self._url = url
        self._connection = None
        self._channel = None

    async def connect(self) -> None:
        import aio_pika

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        await self._channel.declare_queue(HUMAN_QUEUE_NAME, durable=True)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
            self._channel = None

    async def enqueue(self, ticket_id: str, entry: dict[str, Any]) -> str:
        import aio_pika

        if self._channel is None:
            raise HumanQueueUnavailable("human queue channel is not connected")

        queue_entry_id = deterministic_queue_entry_id(ticket_id)
        body = json.dumps(
            {"queue_entry_id": queue_entry_id, "ticket_id": ticket_id, "entry": entry}
        ).encode()
        message = aio_pika.Message(
            body=body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=queue_entry_id,
            headers={"x-ticket-id": ticket_id},
        )
        try:
            await self._channel.default_exchange.publish(
                message, routing_key=HUMAN_QUEUE_NAME
            )
        except Exception as exc:  # connection/channel failures -> unavailable
            raise HumanQueueUnavailable(str(exc)) from exc
        return queue_entry_id
