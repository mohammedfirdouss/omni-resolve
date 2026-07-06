"""Execution Agent entrypoint: consumer loop + /health + /metrics."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from execution_agent.agent import SERVICE_NAME, ExecutionAgent


def build_runtime() -> tuple[ExecutionAgent, object]:
    from shared.db import create_engine_and_sessionmaker
    from shared.event_bus import RabbitMQEventBus

    _, sessionmaker = create_engine_and_sessionmaker(os.environ.get("DATABASE_URL"))
    bus = RabbitMQEventBus(os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq/"))
    agent = ExecutionAgent(
        sessionmaker=sessionmaker,
        event_bus=bus,
        action_base_url=os.environ.get("ACTION_API_URL", "http://actions:9000"),
    )
    return agent, bus


def create_app(agent: ExecutionAgent | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal agent
        bus = None
        if agent is None:  # production path
            agent, bus = build_runtime()
            await bus.connect()
        await agent.start()
        yield
        if bus is not None:
            await bus.close()

    app = FastAPI(title="OmniResolve Execution Agent", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # agent may be swapped in during lifespan; late import of its metrics
        target = app.state.agent if hasattr(app.state, "agent") else agent
        body, content_type = target.metrics.exposition()
        return Response(content=body, media_type=content_type)

    if agent is not None:
        app.state.agent = agent
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8003")))
