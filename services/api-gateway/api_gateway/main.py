"""Production entrypoint: wires real PostgreSQL, RabbitMQ, Qdrant, AI Gateway."""

from __future__ import annotations

import os

from shared.db import create_engine_and_sessionmaker
from shared.event_bus import RabbitMQEventBus

from api_gateway.app import create_app
from api_gateway.embedding import EmbeddingClient
from api_gateway.vector_store import QdrantPolicyStore


def build_app():
    from qdrant_client import AsyncQdrantClient

    _, sessionmaker = create_engine_and_sessionmaker(os.environ.get("DATABASE_URL"))
    bus = RabbitMQEventBus(os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost/"))
    qdrant = AsyncQdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"))

    app = create_app(
        sessionmaker=sessionmaker,
        event_bus=bus,
        embedding_client=EmbeddingClient(),
        policy_store=QdrantPolicyStore(qdrant),
    )

    @app.on_event("startup")
    async def _connect_bus() -> None:
        await bus.connect()

    @app.on_event("shutdown")
    async def _close_bus() -> None:
        await bus.close()

    return app


app = build_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
