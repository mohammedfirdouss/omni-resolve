"""Execution Agent: invokes Resolution_Plan actions strictly in plan order.

Consumes ``ticket.context_ready``. Enforces per-action (10 s) and total (30 s)
timeouts. Every invocation — success, HTTP error, or timeout — is recorded to
the State_Store with request body, response status, response body, and
invocation timestamp (Property 13). All-success -> ``resolved`` +
``ticket.resolved``; any failure -> halt, ``execution_failed`` +
``ticket.escalation_requested`` (Property 14). Nothing after the first
failure is invoked (Property 12 ordering / 14 halt).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from shared.circuit_breaker import CircuitBreaker, CircuitOpenError
from shared.db import ExecutionAction, record_state_transition, with_write_retry
from shared.models import Event, EventType, TicketStatus, new_event
from shared.observability import ServiceMetrics, TraceStore

SERVICE_NAME = "execution-agent"
QUEUE_NAME = "execution-agent.context-ready"

PER_ACTION_TIMEOUT_SECONDS = 10.0  # Requirement 4.5
TOTAL_TIMEOUT_SECONDS = 30.0  # Requirements 4.2/4.3

REASON_ACTION_FAILED = "action_failed"
REASON_EXECUTION_TIMEOUT = "execution_timeout"

# Synthetic status codes for audit rows when no HTTP response exists.
STATUS_TIMEOUT = 504
STATUS_UNREACHABLE = 503


class ExecutionAgent:
    def __init__(
        self,
        *,
        sessionmaker: Any,
        event_bus: Any,
        action_base_url: str = "http://actions:9000",
        transport: httpx.AsyncBaseTransport | None = None,
        per_action_timeout: float = PER_ACTION_TIMEOUT_SECONDS,
        total_timeout: float = TOTAL_TIMEOUT_SECONDS,
        circuit_breaker: CircuitBreaker | None = None,
        metrics: ServiceMetrics | None = None,
        trace_store: TraceStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        db_retry_base_delay: float = 5.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._bus = event_bus
        self._base_url = action_base_url.rstrip("/")
        self._transport = transport
        self._per_action_timeout = per_action_timeout
        self._total_timeout = total_timeout
        self._breaker = circuit_breaker or CircuitBreaker(name="external-actions")
        self.metrics = metrics or ServiceMetrics(SERVICE_NAME)
        self.trace_store = trace_store or TraceStore()
        self._clock = clock
        self._record_action = with_write_retry(base_delay=db_retry_base_delay)(
            self._record_action_impl
        )
        self._transition = with_write_retry(base_delay=db_retry_base_delay)(
            self._transition_impl
        )

    # -------------------------------------------------------------- action I/O

    async def _invoke_action(self, action_type: str, parameters: dict[str, Any]) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=self._transport, timeout=self._per_action_timeout
        ) as client:
            return await client.post(
                f"{self._base_url}/actions/{action_type}", json=parameters
            )

    async def _record_action_impl(
        self,
        *,
        ticket_id: str,
        action_type: str,
        request_body: dict[str, Any],
        response_status: int,
        response_body: dict[str, Any],
    ) -> None:
        async with self._sessionmaker() as session:
            session.add(
                ExecutionAction(
                    ticket_id=ticket_id,
                    action_type=action_type,
                    request_body=request_body,
                    response_status=response_status,
                    response_body=response_body,
                    invoked_at=datetime.now(timezone.utc),
                )
            )
            await session.commit()

    async def _transition_impl(self, *, ticket_id: str, new_state: str) -> None:
        async with self._sessionmaker() as session:
            await record_state_transition(
                session, ticket_id=ticket_id, new_state=new_state, triggered_by=SERVICE_NAME
            )
            await session.commit()

    # ------------------------------------------------------------- entry point

    async def handle_context_ready(self, event: Event) -> None:
        started = self._clock()
        ticket_id = event.ticket_id
        plan = event.payload.get("resolution_plan", {})
        actions: list[dict[str, Any]] = list(plan.get("actions", []))

        completed: list[str] = []
        failure_reason: str | None = None
        failed_action: str | None = None

        for action in actions:
            action_type = str(action.get("action_type", ""))
            parameters = action.get("parameters") or {}

            # Total budget check BEFORE starting the next action (Req 4.3).
            if self._clock() - started >= self._total_timeout:
                failure_reason = REASON_EXECUTION_TIMEOUT
                break

            status: int
            body: dict[str, Any]
            try:
                response = await self._breaker.call(
                    self._invoke_action, action_type, parameters
                )
                status = response.status_code
                try:
                    body = response.json()
                    if not isinstance(body, dict):
                        body = {"result": body}
                except ValueError:
                    body = {"raw": response.text}
            except (httpx.TimeoutException, TimeoutError):
                status, body = STATUS_TIMEOUT, {"error": "timeout"}
            except CircuitOpenError:
                status, body = STATUS_UNREACHABLE, {"error": "circuit_open"}
            except Exception as exc:
                status, body = STATUS_UNREACHABLE, {"error": str(exc) or type(exc).__name__}

            # Property 13: every invocation gets a complete audit record.
            await self._record_action(
                ticket_id=ticket_id,
                action_type=action_type,
                request_body=parameters,
                response_status=status,
                response_body=body,
            )

            if 200 <= status < 300:
                completed.append(action_type)
            else:
                failure_reason = (
                    REASON_EXECUTION_TIMEOUT if status == STATUS_TIMEOUT else REASON_ACTION_FAILED
                )
                failed_action = action_type
                break  # halt: nothing after the first failure is invoked

        if failure_reason is None:
            await self._transition(
                ticket_id=ticket_id, new_state=TicketStatus.RESOLVED.value
            )
            await self._bus.publish(
                new_event(
                    EventType.TICKET_RESOLVED.value,
                    ticket_id,
                    {"actions_completed": completed},
                )
            )
            outcome = "resolved"
        else:
            await self._transition(
                ticket_id=ticket_id, new_state=TicketStatus.EXECUTION_FAILED.value
            )
            await self._bus.publish(
                new_event(
                    EventType.TICKET_ESCALATION_REQUESTED.value,
                    ticket_id,
                    {
                        "reason": failure_reason,
                        "resolution_plan": plan,
                        "actions_completed": completed,
                        "failed_action": failed_action,
                    },
                )
            )
            outcome = failure_reason

        latency = max(self._clock() - started, 0.0)
        self.metrics.observe_request(
            "execute_plan", "200" if outcome == "resolved" else "error", latency
        )
        self.trace_store.record_trace(
            trace_id=uuid.uuid4().hex,
            agent=SERVICE_NAME,
            ticket_id=ticket_id,
            kind="action",
            data={
                "actions_completed": completed,
                "failed_action": failed_action,
                "outcome": outcome,
            },
            latency_seconds=latency,
        )

    async def start(self) -> None:
        await self._bus.subscribe(
            QUEUE_NAME, [EventType.TICKET_CONTEXT_READY.value], self.handle_context_ready
        )
