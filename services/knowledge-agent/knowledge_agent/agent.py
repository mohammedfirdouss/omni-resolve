"""Knowledge Agent core — LangGraph pipeline over ticket.triaged events.

Pipeline (Requirement 3):
    load ticket -> embed description via AI_Gateway -> query Qdrant top-5
    (cosine) -> on success publish ``ticket.context_ready`` with the
    Resolution_Plan + Policy_Document ids; on timeout / zero results /
    unavailability publish ``ticket.escalation_requested`` with a structured
    reason code recorded to the State_Store.

Every dependency (event bus, DB sessionmaker, AI Gateway HTTP client, Qdrant
client, timeout durations, circuit breaker, clock) is injectable for tests.
The Knowledge Agent never changes ticket status: escalation status transitions
are owned by the Escalation Agent downstream.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Callable, TypedDict

import httpx
from langgraph.graph import END, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.circuit_breaker import CircuitBreaker, CircuitOpenError
from shared.db import AgentDecision, RetrievalRecord, Ticket, with_write_retry
from shared.event_bus import EventBus
from shared.models import Event, EventType, new_event
from shared.observability import ServiceMetrics, TraceStore

from knowledge_agent.scores import normalize_similarity_score

AGENT_NAME = "knowledge-agent"
QUEUE_NAME = "knowledge-agent.ticket-triaged"
COLLECTION_NAME = "policies"
TOP_K = 5
QDRANT_TIMEOUT_SECONDS = 2.0  # Requirement 3.2/3.3 — hard timeout

REASON_RETRIEVAL_TIMEOUT = "retrieval_timeout"
REASON_NO_POLICY_FOUND = "no_policy_found"
REASON_VECTOR_STORE_UNAVAILABLE = "vector_store_unavailable"
OUTCOME_SUCCESS = "success"


class RetrievalState(TypedDict, total=False):
    """LangGraph state threaded through the retrieval pipeline."""

    ticket_id: str
    resolution_plan: dict[str, Any]
    description: str
    query_vector: list[float]
    hits: list[tuple[str, float]]  # (policy_id, normalized similarity score)
    outcome: str


class KnowledgeAgent:
    def __init__(
        self,
        *,
        event_bus: EventBus,
        sessionmaker: async_sessionmaker[AsyncSession],
        embeddings_client: httpx.AsyncClient,
        qdrant_client: Any,
        collection_name: str = COLLECTION_NAME,
        top_k: int = TOP_K,
        embedding_model: str = "text-embedding-3-small",
        qdrant_timeout_seconds: float = QDRANT_TIMEOUT_SECONDS,
        circuit_breaker: CircuitBreaker | None = None,
        trace_store: TraceStore | None = None,
        metrics: ServiceMetrics | None = None,
        write_retry_base_delay: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._event_bus = event_bus
        self._sessionmaker = sessionmaker
        self._embeddings = embeddings_client
        self._qdrant = qdrant_client
        self._collection = collection_name
        self._top_k = top_k
        self._embedding_model = embedding_model
        self._qdrant_timeout = qdrant_timeout_seconds
        self._breaker = circuit_breaker or CircuitBreaker(name="qdrant")
        self._traces = trace_store or TraceStore()
        self._metrics = metrics or ServiceMetrics(AGENT_NAME)
        self._clock = clock
        self._persist_success = with_write_retry(base_delay=write_retry_base_delay)(
            self._persist_success_impl
        )
        self._persist_escalation = with_write_retry(base_delay=write_retry_base_delay)(
            self._persist_escalation_impl
        )
        self._graph = self._build_graph()

    # ------------------------------------------------------------------ graph

    def _build_graph(self):
        graph = StateGraph(RetrievalState)
        graph.add_node("load_ticket", self._node_load_ticket)
        graph.add_node("embed", self._node_embed)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("finalize_success", self._node_finalize_success)
        graph.add_node("finalize_escalation", self._node_finalize_escalation)
        graph.set_entry_point("load_ticket")
        graph.add_edge("load_ticket", "embed")
        graph.add_edge("embed", "retrieve")
        graph.add_conditional_edges(
            "retrieve",
            self._route_outcome,
            {"success": "finalize_success", "escalate": "finalize_escalation"},
        )
        graph.add_edge("finalize_success", END)
        graph.add_edge("finalize_escalation", END)
        return graph.compile()

    @staticmethod
    def _route_outcome(state: RetrievalState) -> str:
        return "success" if state["outcome"] == OUTCOME_SUCCESS else "escalate"

    # ------------------------------------------------------------------ nodes

    async def _node_load_ticket(self, state: RetrievalState) -> dict[str, Any]:
        async with self._sessionmaker() as session:
            ticket = await session.get(Ticket, state["ticket_id"])
        if ticket is None:
            raise LookupError(f"unknown ticket {state['ticket_id']!r}")
        return {"description": ticket.description}

    async def _node_embed(self, state: RetrievalState) -> dict[str, Any]:
        response = await self._embeddings.post(
            "/v1/embeddings",
            json={"model": self._embedding_model, "input": state["description"]},
        )
        response.raise_for_status()
        vector = response.json()["data"][0]["embedding"]
        return {"query_vector": [float(v) for v in vector]}

    async def _node_retrieve(self, state: RetrievalState) -> dict[str, Any]:
        try:
            points = await asyncio.wait_for(
                self._breaker.call(self._search, state["query_vector"]),
                timeout=self._qdrant_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):  # Requirement 3.3
            return {"outcome": REASON_RETRIEVAL_TIMEOUT, "hits": []}
        except CircuitOpenError:  # fail-fast while the vector store is dark
            return {"outcome": REASON_VECTOR_STORE_UNAVAILABLE, "hits": []}
        except Exception:  # connection refused / qdrant errors — Requirement 3.6
            return {"outcome": REASON_VECTOR_STORE_UNAVAILABLE, "hits": []}

        hits = [
            (self._point_policy_id(point), normalize_similarity_score(point.score))
            for point in list(points)[: self._top_k]
        ]
        if not hits:  # Requirement 3.5
            return {"outcome": REASON_NO_POLICY_FOUND, "hits": []}
        return {"outcome": OUTCOME_SUCCESS, "hits": hits}

    async def _search(self, query_vector: list[float]) -> Any:
        return await self._qdrant.search(
            collection_name=self._collection,
            query_vector=query_vector,
            limit=self._top_k,
        )

    @staticmethod
    def _point_policy_id(point: Any) -> str:
        payload = getattr(point, "payload", None) or {}
        return str(payload.get("policy_id", point.id))

    async def _node_finalize_success(self, state: RetrievalState) -> dict[str, Any]:
        ticket_id = state["ticket_id"]
        policy_ids = [policy_id for policy_id, _ in state["hits"]]
        await self._persist_success(
            ticket_id=ticket_id,
            description=state["description"],
            hits=state["hits"],
        )
        await self._event_bus.publish(
            new_event(
                EventType.TICKET_CONTEXT_READY.value,
                ticket_id,
                {
                    "resolution_plan": state["resolution_plan"],
                    "policy_document_ids": policy_ids,
                },
            )
        )
        return {}

    async def _node_finalize_escalation(self, state: RetrievalState) -> dict[str, Any]:
        ticket_id = state["ticket_id"]
        reason = state["outcome"]
        await self._persist_escalation(
            ticket_id=ticket_id,
            description=state["description"],
            reason=reason,
        )
        plan = state["resolution_plan"]
        await self._event_bus.publish(
            new_event(
                EventType.TICKET_ESCALATION_REQUESTED.value,
                ticket_id,
                {
                    "reason": reason,
                    "confidence_score": plan.get("confidence_score"),
                    "resolution_plan": plan,
                },
            )
        )
        return {}

    # ------------------------------------------------------------ persistence

    async def _persist_success_impl(
        self, *, ticket_id: str, description: str, hits: list[tuple[str, float]]
    ) -> None:
        async with self._sessionmaker() as session:
            for policy_id, score in hits:
                session.add(
                    RetrievalRecord(
                        ticket_id=ticket_id,
                        policy_id=policy_id,
                        similarity_score=score,
                    )
                )
            session.add(
                AgentDecision(
                    ticket_id=ticket_id,
                    agent=AGENT_NAME,
                    decision_type="retrieval",
                    input_summary={"query": description},
                    output_summary={
                        "policy_document_ids": [policy_id for policy_id, _ in hits],
                        "similarity_scores": [score for _, score in hits],
                    },
                )
            )
            await session.commit()

    async def _persist_escalation_impl(
        self, *, ticket_id: str, description: str, reason: str
    ) -> None:
        async with self._sessionmaker() as session:
            session.add(
                AgentDecision(
                    ticket_id=ticket_id,
                    agent=AGENT_NAME,
                    decision_type="escalation",
                    input_summary={"query": description},
                    output_summary={"reason": reason},
                )
            )
            await session.commit()

    # ------------------------------------------------------------- entrypoint

    async def handle_event(self, event: Event) -> None:
        """Handler for a validated ``ticket.triaged`` event."""
        started = self._clock()
        state: RetrievalState = {
            "ticket_id": event.ticket_id,
            "resolution_plan": dict(event.payload["resolution_plan"]),
        }
        final_state: RetrievalState = await self._graph.ainvoke(state)
        latency = max(self._clock() - started, 0.0)

        outcome = final_state["outcome"]
        policy_ids = [policy_id for policy_id, _ in final_state.get("hits", [])]
        self._traces.record_trace(
            trace_id=f"{AGENT_NAME}:{event.ticket_id}:{uuid.uuid4()}",
            agent=AGENT_NAME,
            ticket_id=event.ticket_id,
            kind="retrieval",
            data={
                "query": final_state.get("description", ""),
                "policy_ids": policy_ids,
                "outcome": outcome,
            },
            latency_seconds=latency,
        )
        self._metrics.observe_request(
            "policy_retrieval",
            "ok" if outcome == OUTCOME_SUCCESS else outcome,
            latency,
        )

    async def start(self) -> None:
        """Subscribe to ticket.triaged on the event bus."""
        await self._event_bus.subscribe(
            QUEUE_NAME, [EventType.TICKET_TRIAGED.value], self.handle_event
        )
