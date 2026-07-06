"""Knowledge Agent service entrypoint: consumer loop + /health + /metrics.

``create_app`` accepts a pre-built :class:`KnowledgeAgent` (tests inject fakes);
when omitted, real RabbitMQ / PostgreSQL / Qdrant / AI Gateway clients are
constructed from environment variables.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from shared.observability import ServiceMetrics, make_metrics_app

from knowledge_agent.agent import AGENT_NAME, KnowledgeAgent


def build_runtime(metrics: ServiceMetrics) -> tuple[KnowledgeAgent, "object"]:
    """Construct the production agent from environment configuration."""
    from qdrant_client import AsyncQdrantClient

    from shared.db import create_engine_and_sessionmaker
    from shared.event_bus import RabbitMQEventBus

    _, sessionmaker = create_engine_and_sessionmaker(os.environ.get("DATABASE_URL"))
    event_bus = RabbitMQEventBus(os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq/"))
    agent = KnowledgeAgent(
        event_bus=event_bus,
        sessionmaker=sessionmaker,
        embeddings_client=httpx.AsyncClient(
            base_url=os.environ.get("AI_GATEWAY_URL", "http://ai-gateway:8000"),
            timeout=10.0,
        ),
        qdrant_client=AsyncQdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333")),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        metrics=metrics,
    )
    return agent, event_bus


def create_app(
    agent: KnowledgeAgent | None = None,
    metrics: ServiceMetrics | None = None,
) -> FastAPI:
    metrics = metrics or ServiceMetrics(AGENT_NAME)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal agent
        bus = None
        if agent is None:  # production path
            agent, bus = build_runtime(metrics)
            await bus.connect()
        await agent.start()
        yield
        if bus is not None:
            await bus.close()

    app = FastAPI(title="OmniResolve Knowledge Agent", lifespan=lifespan)
    app.mount("/metrics", make_metrics_app(metrics))

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": AGENT_NAME}

    return app


app = create_app()
