"""Test doubles + app factory helpers for API Gateway tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.event_bus import InMemoryEventBus

from api_gateway.app import create_app
from api_gateway.embedding import EmbeddingClient, EmbeddingError


class FakeEmbeddingClient(EmbeddingClient):
    def __init__(self, *, fail: bool = False, dim: int = 8) -> None:
        self.fail = fail
        self.dim = dim
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        if self.fail:
            raise EmbeddingError("embedding model exploded (test)")
        return [0.1] * self.dim


@dataclass
class FakePolicyStore:
    upserts: dict[str, dict[str, Any]] = field(default_factory=dict)
    fail: bool = False

    async def upsert_policy(
        self, *, policy_id: str, vector: list[float], content: str, title: str, category: str
    ) -> None:
        from api_gateway.vector_store import VectorStoreError

        if self.fail:
            raise VectorStoreError("qdrant down (test)")
        self.upserts[policy_id] = {
            "vector": vector,
            "content": content,
            "title": title,
            "category": category,
        }


@dataclass
class Env:
    app: Any
    bus: InMemoryEventBus
    embedding: FakeEmbeddingClient
    store: FakePolicyStore
    sessionmaker: Any
    engine: Any

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )


async def make_env(
    *, embedding: FakeEmbeddingClient | None = None, store: FakePolicyStore | None = None,
    sessionmaker=None,
) -> Env:
    engine = None
    if sessionmaker is None:
        engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    bus = InMemoryEventBus()
    embedding = embedding or FakeEmbeddingClient()
    store = store or FakePolicyStore()
    app = create_app(
        sessionmaker=sessionmaker,
        event_bus=bus,
        embedding_client=embedding,
        policy_store=store,
    )
    return Env(app=app, bus=bus, embedding=embedding, store=store,
               sessionmaker=sessionmaker, engine=engine)
