"""Production entrypoint: consume ticket.created and serve /health + /metrics."""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, Response

from shared.db import create_engine_and_sessionmaker
from shared.event_bus import RabbitMQEventBus

from triage_agent.agent import SERVICE_NAME, TriageAgent


def build() -> tuple[FastAPI, TriageAgent, RabbitMQEventBus]:
    _, sessionmaker = create_engine_and_sessionmaker(os.environ.get("DATABASE_URL"))
    bus = RabbitMQEventBus(os.environ.get("RABBITMQ_URL", "amqp://guest:guest@localhost/"))
    agent = TriageAgent(
        sessionmaker=sessionmaker,
        event_bus=bus,
        ai_gateway_url=os.environ.get("AI_GATEWAY_URL", "http://ai-gateway:8100"),
    )

    app = FastAPI(title="OmniResolve Triage Agent")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/metrics")
    async def metrics() -> Response:
        body, content_type = agent.metrics.exposition()
        return Response(content=body, media_type=content_type)

    @app.on_event("startup")
    async def startup() -> None:
        await bus.connect()
        asyncio.create_task(agent.start())

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await bus.close()

    return app, agent, bus


app, _agent, _bus = build()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8001")))
