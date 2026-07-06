"""Escalation Agent core logic (Requirement 5, Property 9).

Flow per ``ticket.escalation_requested`` event:

1. Idempotency gate (Property 9): skip if the ticket was already processed —
   checked against the in-process seen-set / retry buffer AND durably against
   the State_Store (ticket status already ``escalated``, or an existing
   ``EscalationOverflow`` queue-entry marker), so redelivery after a restart
   is still deduped.
2. Update ticket status to ``escalated`` via
   ``shared.db.record_state_transition`` (write-retried; 3 attempts,
   exponential backoff). Already-``escalated`` tickets are an idempotent
   no-op.
3. Enqueue into the human agent queue with the resolution plan / confidence
   score (when available) and the escalation reason.
4. Publish ``ticket.escalated`` with the ``queue_entry_id``.

If the human queue is unavailable, entries land in a local retry buffer
(capacity 10,000, retried every 30 s). When the buffer is full, a
``system.escalation_buffer_full`` alert is emitted (event bus +
observability) and the overflow entry is persisted to the State_Store
``escalation_overflow`` table with status ``escalation_pending``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db import (
    EscalationOverflow,
    InvalidStateTransition,
    Ticket,
    record_state_transition,
    with_write_retry,
)
from shared.event_bus import EventBus
from shared.models import Event, EventType, TicketStatus, new_event
from shared.observability import GLOBAL_TRACE_STORE, ServiceMetrics, TraceStore

from escalation_agent.human_queue import HumanQueue

logger = logging.getLogger("omniresolve.escalation_agent")

SERVICE_NAME = "escalation-agent"
CONSUMER_QUEUE_NAME = "escalation-agent.escalation_requested"

DEFAULT_BUFFER_CAPACITY = 10_000
DEFAULT_RETRY_INTERVAL_SECONDS = 30.0

AlertChannel = Callable[[dict[str, Any]], Awaitable[None] | None]
Sleep = Callable[[float], Awaitable[None]]


class EscalationAgent:
    """Consumes ``ticket.escalation_requested`` and hands tickets to humans."""

    def __init__(
        self,
        *,
        bus: EventBus,
        human_queue: HumanQueue,
        sessionmaker: async_sessionmaker[AsyncSession],
        metrics: ServiceMetrics | None = None,
        trace_store: TraceStore | None = None,
        alert_channel: AlertChannel | None = None,
        buffer_capacity: int = DEFAULT_BUFFER_CAPACITY,
        retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
        write_retry_base_delay: float = 5.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._bus = bus
        self._human_queue = human_queue
        self._sessionmaker = sessionmaker
        self.metrics = metrics or ServiceMetrics(SERVICE_NAME)
        self._trace_store = trace_store or GLOBAL_TRACE_STORE
        self._alert_channel = alert_channel
        self._buffer_capacity = buffer_capacity
        self._retry_interval = retry_interval_seconds
        self._sleep = sleep

        # In-process idempotency: tickets fully processed this lifetime.
        self._seen: set[str] = set()
        # Local retry buffer: ticket_id -> queue entry awaiting the human queue.
        self.retry_buffer: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._retry_task: asyncio.Task[None] | None = None

        # State_Store write with shared retry policy (3 attempts, exp backoff);
        # emits system.state_write_failed and raises StateWriteFailed on
        # exhaustion so the in-flight event is nacked for redelivery.
        self._write_escalated = with_write_retry(base_delay=write_retry_base_delay)(
            self._transition_to_escalated
        )

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the event bus."""
        await self._bus.subscribe(
            CONSUMER_QUEUE_NAME,
            [EventType.TICKET_ESCALATION_REQUESTED.value],
            self.handle_event,
        )

    def start_retry_loop(self) -> asyncio.Task[None]:
        """Launch the background 30 s retry loop for buffered entries."""
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = asyncio.create_task(self.run_retry_loop())
        return self._retry_task

    async def stop(self) -> None:
        if self._retry_task is not None:
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            self._retry_task = None

    # -- event handling --------------------------------------------------------

    async def handle_event(self, event: Event) -> None:
        started = time.monotonic()
        ticket_id = event.ticket_id

        # Idempotency (Property 9), in-process: already processed or buffered.
        if ticket_id in self._seen or ticket_id in self.retry_buffer:
            self._observe("escalation_requested", "duplicate", started)
            return

        # Idempotency, durable: State_Store already shows this ticket as
        # escalated (e.g. redelivery after restart, or triage escalated it) or
        # an overflow queue-entry marker exists.
        async with self._sessionmaker() as session:
            if await self._already_processed(session, ticket_id):
                self._seen.add(ticket_id)
                self._observe("escalation_requested", "duplicate", started)
                return

        # Requirement 5.1: status -> escalated, promptly (2 s SLA).
        await self._mark_escalated(ticket_id)

        entry = self._build_entry(event)
        await self._enqueue_or_buffer(ticket_id, entry)
        self._observe("escalation_requested", "ok", started)
        self._trace_store.record_trace(
            trace_id=str(uuid.uuid4()),
            agent=SERVICE_NAME,
            ticket_id=ticket_id,
            kind="reasoning_step",
            data={"step": "escalation_processed", "reason": entry.get("reason")},
            latency_seconds=time.monotonic() - started,
        )

    async def _already_processed(self, session: AsyncSession, ticket_id: str) -> bool:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is not None and ticket.status == TicketStatus.ESCALATED.value:
            return True
        marker = await session.execute(
            select(EscalationOverflow.id)
            .where(EscalationOverflow.ticket_id == ticket_id)
            .limit(1)
        )
        return marker.first() is not None

    async def _mark_escalated(self, ticket_id: str) -> None:
        try:
            await self._write_escalated(
                ticket_id=ticket_id, new_state=TicketStatus.ESCALATED.value
            )
        except InvalidStateTransition as exc:
            # Already escalated (race / triage set it): idempotent no-op.
            # Unknown ticket or otherwise-illegal transition: log and still
            # enqueue — a human must see the ticket regardless.
            logger.warning(
                "state transition to escalated skipped for %s: %s", ticket_id, exc
            )

    async def _transition_to_escalated(self, *, ticket_id: str, new_state: str) -> None:
        async with self._sessionmaker() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is not None and ticket.status == TicketStatus.ESCALATED.value:
                return  # idempotent no-op
            await record_state_transition(
                session,
                ticket_id=ticket_id,
                new_state=new_state,
                triggered_by=SERVICE_NAME,
            )
            await session.commit()

    @staticmethod
    def _build_entry(event: Event) -> dict[str, Any]:
        payload = event.payload
        return {
            "ticket_id": event.ticket_id,
            "reason": payload.get("reason", "unspecified"),
            "confidence_score": payload.get("confidence_score"),
            "resolution_plan": payload.get("resolution_plan"),
            "requested_at": event.timestamp,
        }

    # -- enqueue / retry buffer -------------------------------------------------

    async def _enqueue_or_buffer(self, ticket_id: str, entry: dict[str, Any]) -> None:
        try:
            queue_entry_id = await self._human_queue.enqueue(ticket_id, entry)
        except Exception as exc:
            logger.warning("human queue unavailable for %s: %s", ticket_id, exc)
            await self._buffer_entry(ticket_id, entry)
            return
        self._seen.add(ticket_id)
        await self._publish_escalated(ticket_id, queue_entry_id)

    async def _buffer_entry(self, ticket_id: str, entry: dict[str, Any]) -> None:
        if len(self.retry_buffer) < self._buffer_capacity:
            self.retry_buffer[ticket_id] = entry
            if len(self.retry_buffer) >= self._buffer_capacity:
                await self._emit_buffer_full(ticket_id)
        else:
            # Buffer full: alert + persist overflow to the State_Store so the
            # escalation is not lost (design: "Escalation Buffer Full").
            await self._emit_buffer_full(ticket_id)
            await self._persist_overflow(ticket_id, entry)
            self._seen.add(ticket_id)

    async def _publish_escalated(self, ticket_id: str, queue_entry_id: str) -> None:
        await self._bus.publish(
            new_event(
                EventType.TICKET_ESCALATED.value,
                ticket_id,
                {"queue_entry_id": queue_entry_id},
            )
        )

    async def _emit_buffer_full(self, ticket_id: str) -> None:
        buffer_size = len(self.retry_buffer)
        alert_event = new_event(
            EventType.SYSTEM_ESCALATION_BUFFER_FULL.value,
            ticket_id,
            {"buffer_size": buffer_size},
        )
        await self._bus.publish(alert_event)
        alert = {
            "alert": EventType.SYSTEM_ESCALATION_BUFFER_FULL.value,
            "service": SERVICE_NAME,
            "buffer_size": buffer_size,
            "capacity": self._buffer_capacity,
        }
        logger.error("ALERT %s", alert)
        self._trace_store.record_trace(
            trace_id=str(uuid.uuid4()),
            agent=SERVICE_NAME,
            ticket_id=ticket_id,
            kind="alert",
            data=alert,
        )
        if self._alert_channel is not None:
            result = self._alert_channel(alert)
            if asyncio.iscoroutine(result):
                await result

    async def _persist_overflow(self, ticket_id: str, entry: dict[str, Any]) -> None:
        async with self._sessionmaker() as session:
            session.add(
                EscalationOverflow(
                    ticket_id=ticket_id,
                    status=TicketStatus.ESCALATION_PENDING.value,
                    payload=entry,
                )
            )
            await session.commit()

    # -- retry loop --------------------------------------------------------------

    async def run_retry_loop(self) -> None:
        """Reattempt buffered enqueues every ``retry_interval_seconds``."""
        while True:
            await self._sleep(self._retry_interval)
            try:
                await self.retry_pending()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the loop alive on unexpected errors
                logger.exception("escalation retry pass failed")

    async def retry_pending(self) -> int:
        """One retry pass: drain the buffer, then State_Store overflow rows.

        Returns the number of entries successfully enqueued.
        """
        drained = 0
        for ticket_id in list(self.retry_buffer.keys()):
            entry = self.retry_buffer[ticket_id]
            try:
                queue_entry_id = await self._human_queue.enqueue(ticket_id, entry)
            except Exception as exc:
                logger.info("human queue still unavailable: %s", exc)
                return drained
            del self.retry_buffer[ticket_id]
            self._seen.add(ticket_id)
            await self._publish_escalated(ticket_id, queue_entry_id)
            drained += 1
        drained += await self._drain_overflow()
        return drained

    async def _drain_overflow(self) -> int:
        """Enqueue persisted overflow rows once the queue is reachable again."""
        drained = 0
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(EscalationOverflow)
                        .where(
                            EscalationOverflow.status
                            == TicketStatus.ESCALATION_PENDING.value
                        )
                        .order_by(EscalationOverflow.id)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                try:
                    queue_entry_id = await self._human_queue.enqueue(
                        row.ticket_id, row.payload
                    )
                except Exception:
                    break
                await session.execute(
                    delete(EscalationOverflow).where(EscalationOverflow.id == row.id)
                )
                self._seen.add(row.ticket_id)
                await self._publish_escalated(row.ticket_id, queue_entry_id)
                drained += 1
            await session.commit()
        return drained

    # -- observability -------------------------------------------------------------

    def _observe(self, operation: str, status: str, started: float) -> None:
        self.metrics.observe_request(operation, status, time.monotonic() - started)
