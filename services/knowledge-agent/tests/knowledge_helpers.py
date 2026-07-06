"""Test environment factory for Knowledge Agent tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import Base, PolicyDocument, create_ticket, record_state_transition
from shared.event_bus import InMemoryEventBus
from shared.models import EventType, new_event

from knowledge_agent.agent import KnowledgeAgent


def make_point(policy_id: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(id=policy_id, score=score, payload={"policy_id": policy_id})


class FakeQdrant:
    """Injectable stand-in for AsyncQdrantClient.search."""

    def __init__(self, points: list | None = None, *, error: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.points = points or []
        self.error = error
        self.delay = delay
        self.calls = 0

    async def search(self, collection_name: str, query_vector: list[float], limit: int):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.points[:limit]


def embeddings_client(dim: int = 4) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [0.5] * dim}]})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ai-gw"
    )


@dataclass
class Env:
    agent: KnowledgeAgent
    bus: InMemoryEventBus
    sessionmaker: Any
    qdrant: FakeQdrant
    engine: Any

    async def dispose(self) -> None:
        await self.engine.dispose()


async def make_env(qdrant: FakeQdrant | None = None, **agent_kwargs) -> Env:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    bus = InMemoryEventBus()
    qdrant = qdrant or FakeQdrant([make_point(f"p-{i}", 0.9 - i * 0.1) for i in range(5)])
    agent = KnowledgeAgent(
        event_bus=bus,
        sessionmaker=sessionmaker,
        embeddings_client=agent_kwargs.pop("embeddings_client", embeddings_client()),
        qdrant_client=qdrant,
        write_retry_base_delay=0.0,
        **agent_kwargs,
    )
    return Env(agent=agent, bus=bus, sessionmaker=sessionmaker, qdrant=qdrant, engine=engine)


async def seed_triaged_ticket(env: Env, *, description: str = "please refund") -> str:
    """Create a ticket in 'triaged' state and seed the referenced policies."""
    async with env.sessionmaker() as session:
        ticket = await create_ticket(
            session, customer_id="c-1", category="refund", description=description
        )
        await session.flush()
        await record_state_transition(
            session, ticket_id=ticket.ticket_id, new_state="triaged", triggered_by="test"
        )
        for policy_id, _score in [(p.id, p.score) for p in env.qdrant.points]:
            session.add(PolicyDocument(policy_id=policy_id, title="t", category="c"))
        await session.commit()
        return ticket.ticket_id


def triaged_event(ticket_id: str, *, confidence: float = 0.9):
    return new_event(
        EventType.TICKET_TRIAGED.value,
        ticket_id,
        {
            "resolution_plan": {
                "ticket_id": ticket_id,
                "actions": [{"action_type": "process_refund", "parameters": {}}],
                "confidence_score": confidence,
            }
        },
    )
