"""Triage Agent: LangGraph pipeline consuming ``ticket.created``.

Graph: build_prompt -> call_llm (circuit-broken, 3 retries @ 1/2/4 s) ->
parse_plan -> route. Routing is decided solely by the confidence score
(Property 4): >= 0.75 publishes ``ticket.triaged`` + status ``triaged``;
< 0.75 publishes ``ticket.escalation_requested``. LLM retry exhaustion sets
status ``escalated`` and publishes ``ticket.escalation_requested``
(Requirement 2.6).
"""

from __future__ import annotations

import time
import uuid
from typing import Any, TypedDict

import httpx
from langgraph.graph import END, StateGraph

from shared.circuit_breaker import CircuitBreaker, CircuitOpenError
from shared.db import AgentDecision, record_state_transition, with_write_retry
from shared.models import (
    CONFIDENCE_THRESHOLD,
    EventType,
    Event,
    ResolutionPlan,
    TicketStatus,
    new_event,
)
from shared.observability import ServiceMetrics, TraceStore

from triage_agent.parser import parse_resolution_plan

SERVICE_NAME = "triage-agent"
RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)

PROMPT_TEMPLATE = """You are a Tier-1 customer support triage agent.
Analyze the ticket and produce a JSON resolution plan.

Ticket ID: {ticket_id}
Category: {category}
Description: {description}

Respond with ONLY a JSON object of the shape:
{{"actions": [{{"action_type": "process_refund|track_order|adjust_billing|send_notification", "parameters": {{}}}}],
  "confidence_score": 0.0}}
confidence_score reflects your certainty that these actions fully resolve the ticket."""


class TriageState(TypedDict, total=False):
    ticket_id: str
    category: str
    description: str
    prompt: str
    llm_response: str
    llm_failed: bool
    plan: ResolutionPlan


class TriageAgent:
    def __init__(
        self,
        *,
        sessionmaker: Any,
        event_bus: Any,
        ai_gateway_url: str = "http://ai-gateway:8100",
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
        sleep: Any = None,
        circuit_breaker: CircuitBreaker | None = None,
        metrics: ServiceMetrics | None = None,
        trace_store: TraceStore | None = None,
        db_retry_base_delay: float = 5.0,
    ) -> None:
        import asyncio

        self._sessionmaker = sessionmaker
        self._bus = event_bus
        self._ai_gateway_url = ai_gateway_url.rstrip("/")
        self._transport = transport
        self._retry_delays = retry_delays
        self._sleep = sleep or asyncio.sleep
        self._breaker = circuit_breaker or CircuitBreaker(name="ai-gateway")
        self.metrics = metrics or ServiceMetrics(SERVICE_NAME)
        self.trace_store = trace_store or TraceStore()
        self._db_retry_base_delay = db_retry_base_delay
        self._graph = self._build_graph()

    # ------------------------------------------------------------ graph nodes

    def _build_graph(self):
        graph = StateGraph(TriageState)
        graph.add_node("build_prompt", self._node_build_prompt)
        graph.add_node("call_llm", self._node_call_llm)
        graph.add_node("parse_plan", self._node_parse_plan)
        graph.set_entry_point("build_prompt")
        graph.add_edge("build_prompt", "call_llm")
        graph.add_edge("call_llm", "parse_plan")
        graph.add_edge("parse_plan", END)
        return graph.compile()

    async def _node_build_prompt(self, state: TriageState) -> TriageState:
        return {
            "prompt": PROMPT_TEMPLATE.format(
                ticket_id=state["ticket_id"],
                category=state["category"],
                description=state["description"],
            )
        }

    async def _node_call_llm(self, state: TriageState) -> TriageState:
        last_error: Exception | None = None
        for attempt, delay in enumerate((*self._retry_delays, None)):
            try:
                response = await self._breaker.call(self._post_completion, state["prompt"])
                return {"llm_response": response, "llm_failed": False}
            except CircuitOpenError as exc:
                last_error = exc
                break  # fail fast: do not consume retry budget
            except Exception as exc:
                last_error = exc
                if delay is None:
                    break
                await self._sleep(delay)
        return {"llm_response": "", "llm_failed": True}

    async def _node_parse_plan(self, state: TriageState) -> TriageState:
        return {"plan": parse_resolution_plan(state["ticket_id"], state["llm_response"])}

    async def _post_completion(self, prompt: str) -> str:
        async with httpx.AsyncClient(transport=self._transport, timeout=10.0) as client:
            response = await client.post(
                f"{self._ai_gateway_url}/v1/completions",
                json={"messages": [{"role": "user", "content": prompt}]},
            )
            response.raise_for_status()
            data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return ""

    # ------------------------------------------------------------ event entry

    async def handle_ticket_created(self, event: Event) -> None:
        started = time.monotonic()
        ticket_id = event.ticket_id
        payload = event.payload
        state: TriageState = await self._graph.ainvoke(
            {
                "ticket_id": ticket_id,
                "category": str(payload.get("category", "")),
                "description": str(payload.get("description", "")),
            }
        )
        plan = state["plan"]
        llm_failed = bool(state.get("llm_failed"))

        if llm_failed:
            # Requirement 2.6: retries exhausted -> status escalated + event.
            await self._record_outcome(
                ticket_id=ticket_id,
                payload=payload,
                plan=plan,
                decision_type="triage_llm_failure",
                new_state=TicketStatus.ESCALATED.value,
            )
            await self._bus.publish(
                new_event(
                    EventType.TICKET_ESCALATION_REQUESTED.value,
                    ticket_id,
                    {"reason": "triage_llm_failure"},
                )
            )
        elif plan.confidence_score >= CONFIDENCE_THRESHOLD:
            await self._record_outcome(
                ticket_id=ticket_id,
                payload=payload,
                plan=plan,
                decision_type="triage_plan",
                new_state=TicketStatus.TRIAGED.value,
            )
            await self._bus.publish(
                new_event(
                    EventType.TICKET_TRIAGED.value,
                    ticket_id,
                    {"resolution_plan": plan.model_dump()},
                )
            )
        else:
            await self._record_outcome(
                ticket_id=ticket_id,
                payload=payload,
                plan=plan,
                decision_type="triage_low_confidence",
                new_state=None,  # Escalation Agent owns the status change
            )
            await self._bus.publish(
                new_event(
                    EventType.TICKET_ESCALATION_REQUESTED.value,
                    ticket_id,
                    {
                        "reason": "low_confidence",
                        "confidence_score": plan.confidence_score,
                        "resolution_plan": plan.model_dump(),
                    },
                )
            )

        latency = time.monotonic() - started
        self.metrics.observe_request("triage", "error" if llm_failed else "200", latency)
        self.trace_store.record_trace(
            trace_id=uuid.uuid4().hex,
            agent=SERVICE_NAME,
            ticket_id=ticket_id,
            kind="reasoning_step",
            data={
                "prompt": state.get("prompt", ""),
                "confidence_score": plan.confidence_score,
                "actions": [a.action_type for a in plan.actions],
                "llm_failed": llm_failed,
            },
            latency_seconds=latency,
        )

    async def _record_outcome(
        self,
        *,
        ticket_id: str,
        payload: dict[str, Any],
        plan: ResolutionPlan,
        decision_type: str,
        new_state: str | None,
    ) -> None:
        @with_write_retry(base_delay=self._db_retry_base_delay)
        async def write(*, ticket_id: str, new_state: str | None) -> None:
            async with self._sessionmaker() as session:
                session.add(
                    AgentDecision(
                        ticket_id=ticket_id,
                        agent=SERVICE_NAME,
                        decision_type=decision_type,
                        input_summary={
                            "ticket_id": ticket_id,
                            "category": payload.get("category"),
                            "description": payload.get("description"),
                        },
                        output_summary={
                            "actions": [a.model_dump() for a in plan.actions],
                        },
                        confidence_score=plan.confidence_score,
                    )
                )
                if new_state is not None:
                    await record_state_transition(
                        session,
                        ticket_id=ticket_id,
                        new_state=new_state,
                        triggered_by=SERVICE_NAME,
                    )
                await session.commit()

        await write(ticket_id=ticket_id, new_state=new_state)

    async def start(self) -> None:
        await self._bus.subscribe(
            "triage-agent", [EventType.TICKET_CREATED.value], self.handle_ticket_created
        )
