"""End-to-end integration tests (task 17.1).

Wires the real agent implementations together over real PostgreSQL, RabbitMQ,
and Qdrant containers. LLM and external action APIs are the only mocked
boundaries (deterministic httpx.MockTransport), because the spec's SLAs
assume provider behaviour we can't reproduce hermetically.

Covers the happy path (created -> triaged -> context_ready -> resolved) and
every escalation branch (low confidence, retrieval timeout, zero results,
action failure).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db import Base, Ticket, get_ticket_history
from shared.event_bus import RabbitMQEventBus
from shared.models import EventType, new_event


def _infra_reachable() -> bool:
    if os.environ.get("OMNI_IT_DATABASE_URL"):
        return True
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _infra_reachable(),
        reason="integration tests need a Docker daemon or an OMNI_IT_* stack",
    ),
]

POLL_INTERVAL = 0.2
WAIT_TIMEOUT = 30.0


def plan_response(confidence: float) -> str:
    return json.dumps(
        {
            "actions": [
                {"action_type": "process_refund", "parameters": {"amount": 25}},
                {"action_type": "send_notification", "parameters": {}},
            ],
            "confidence_score": confidence,
        }
    )


def llm_transport(confidence: float) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": plan_response(confidence)}}]}
        )

    return httpx.MockTransport(handler)


def action_transport(fail_action: str | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.path.rsplit("/", 1)[-1]
        if action == fail_action:
            return httpx.Response(500, json={"error": "downstream failure"})
        return httpx.Response(200, json={"ok": True})

    return httpx.MockTransport(handler)


def embeddings_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.5, 0.5, 0.5]}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ai-gw")


async def wait_for_status(sessionmaker, ticket_id: str, statuses: set[str]) -> str:
    deadline = asyncio.get_event_loop().time() + WAIT_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        async with sessionmaker() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is not None and ticket.status in statuses:
                return ticket.status
        await asyncio.sleep(POLL_INTERVAL)
    raise AssertionError(f"ticket {ticket_id} never reached {statuses}")


class Stack:
    """All agents wired to the real infra for one test."""

    def __init__(self, infra_urls, *, confidence: float, fail_action: str | None,
                 seed_policies: bool, knowledge_timeout: float = 2.0):
        self.db_url, self.rabbit_url, self.qdrant_url = infra_urls
        self.confidence = confidence
        self.fail_action = fail_action
        self.seed_policies = seed_policies
        self.knowledge_timeout = knowledge_timeout

    async def __aenter__(self):
        from qdrant_client import AsyncQdrantClient, models as qmodels

        from escalation_agent.agent import EscalationAgent
        from escalation_agent.human_queue import InMemoryHumanQueue
        from execution_agent.agent import ExecutionAgent
        from knowledge_agent.agent import KnowledgeAgent
        from triage_agent.agent import TriageAgent

        self.engine = create_async_engine(self.db_url)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessionmaker = async_sessionmaker(self.engine, expire_on_commit=False)

        self.bus = RabbitMQEventBus(self.rabbit_url)
        await self.bus.connect()

        self.qdrant = AsyncQdrantClient(url=self.qdrant_url)
        collection = f"policies_{uuid.uuid4().hex[:8]}"
        await self.qdrant.create_collection(
            collection_name=collection,
            vectors_config=qmodels.VectorParams(size=4, distance=qmodels.Distance.COSINE),
        )
        if self.seed_policies:
            from shared.db import PolicyDocument

            points, policy_ids = [], []
            for i in range(5):
                policy_id = str(uuid.uuid4())
                policy_ids.append(policy_id)
                points.append(
                    qmodels.PointStruct(
                        id=policy_id, vector=[0.5, 0.5, 0.5, 0.5],
                        payload={"policy_id": policy_id},
                    )
                )
            await self.qdrant.upsert(collection_name=collection, points=points, wait=True)
            async with self.sessionmaker() as session:
                for policy_id in policy_ids:
                    session.add(PolicyDocument(policy_id=policy_id, title="t", category="c"))
                await session.commit()

        self.human_queue = InMemoryHumanQueue()
        self.triage = TriageAgent(
            sessionmaker=self.sessionmaker, event_bus=self.bus,
            transport=llm_transport(self.confidence),
            sleep=lambda _: asyncio.sleep(0), db_retry_base_delay=0.0,
        )
        self.knowledge = KnowledgeAgent(
            event_bus=self.bus, sessionmaker=self.sessionmaker,
            embeddings_client=embeddings_client(), qdrant_client=self.qdrant,
            collection_name=collection, write_retry_base_delay=0.0,
            qdrant_timeout_seconds=self.knowledge_timeout,
        )
        self.execution = ExecutionAgent(
            sessionmaker=self.sessionmaker, event_bus=self.bus,
            transport=action_transport(self.fail_action), db_retry_base_delay=0.0,
        )
        self.escalation = EscalationAgent(
            bus=self.bus, human_queue=self.human_queue,
            sessionmaker=self.sessionmaker, write_retry_base_delay=0.0,
        )
        for agent_start in (self.triage.start, self.knowledge.start,
                            self.execution.start, self.escalation.start):
            await agent_start()
        return self

    async def __aexit__(self, *exc):
        await self.bus.close()
        await self.qdrant.close()
        await self.engine.dispose()

    async def submit_ticket(self) -> str:
        from shared.db import create_ticket

        async with self.sessionmaker() as session:
            ticket = await create_ticket(
                session, customer_id="c-1", category="refund",
                description="please refund order 42",
            )
            await session.commit()
            ticket_id = ticket.ticket_id
        await self.bus.publish(
            new_event(
                EventType.TICKET_CREATED.value, ticket_id,
                {"customer_id": "c-1", "category": "refund",
                 "description": "please refund order 42"},
            )
        )
        return ticket_id


async def test_happy_path_created_to_resolved(infra_urls):
    async with Stack(infra_urls, confidence=0.92, fail_action=None,
                     seed_policies=True) as stack:
        ticket_id = await stack.submit_ticket()
        status = await wait_for_status(stack.sessionmaker, ticket_id, {"resolved"})
        assert status == "resolved"

        async with stack.sessionmaker() as session:
            history = await get_ticket_history(session, ticket_id)
        states = [t["new_state"] for t in history["state_transitions"]]
        assert states == ["pending", "triaged", "resolved"]
        assert len(history["execution_actions"]) == 2
        assert history["agent_decisions"]  # triage + knowledge decisions
        assert history["total_elapsed_seconds"] is not None


async def test_low_confidence_escalates(infra_urls):
    async with Stack(infra_urls, confidence=0.30, fail_action=None,
                     seed_policies=True) as stack:
        ticket_id = await stack.submit_ticket()
        status = await wait_for_status(stack.sessionmaker, ticket_id, {"escalated"})
        assert status == "escalated"
        assert len(stack.human_queue.entries_for(ticket_id)) == 1
        assert stack.human_queue.entries[ticket_id]["reason"] == "low_confidence"


async def test_zero_policy_results_escalates(infra_urls):
    async with Stack(infra_urls, confidence=0.92, fail_action=None,
                     seed_policies=False) as stack:  # empty collection
        ticket_id = await stack.submit_ticket()
        status = await wait_for_status(stack.sessionmaker, ticket_id, {"escalated"})
        assert status == "escalated"
        assert stack.human_queue.entries[ticket_id]["reason"] == "no_policy_found"


async def test_retrieval_timeout_escalates(infra_urls):
    async with Stack(infra_urls, confidence=0.92, fail_action=None,
                     seed_policies=True, knowledge_timeout=0.000001) as stack:
        ticket_id = await stack.submit_ticket()
        status = await wait_for_status(stack.sessionmaker, ticket_id, {"escalated"})
        assert status == "escalated"
        assert stack.human_queue.entries[ticket_id]["reason"] == "retrieval_timeout"


async def test_action_failure_escalates_via_execution_failed(infra_urls):
    async with Stack(infra_urls, confidence=0.92, fail_action="send_notification",
                     seed_policies=True) as stack:
        ticket_id = await stack.submit_ticket()
        status = await wait_for_status(stack.sessionmaker, ticket_id, {"escalated"})
        assert status == "escalated"

        async with stack.sessionmaker() as session:
            history = await get_ticket_history(session, ticket_id)
        states = [t["new_state"] for t in history["state_transitions"]]
        assert states == ["pending", "triaged", "execution_failed", "escalated"]
        assert stack.human_queue.entries[ticket_id]["reason"] == "action_failed"
