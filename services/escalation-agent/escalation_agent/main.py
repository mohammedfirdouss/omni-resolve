"""Escalation Agent entrypoint: consumer loop + retry loop + /health + /metrics."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response

from escalation_agent.agent import SERVICE_NAME, EscalationAgent


def build_runtime() -> tuple[EscalationAgent, object, object]:
    from shared.db import create_engine_and_sessionmaker
    from shared.event_bus import RabbitMQEventBus

    from escalation_agent.human_queue import RabbitMQHumanQueue

    _, sessionmaker = create_engine_and_sessionmaker(os.environ.get("DATABASE_URL"))
    rabbit_url = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq/")
    bus = RabbitMQEventBus(rabbit_url)
    human_queue = RabbitMQHumanQueue(rabbit_url)
    agent = EscalationAgent(bus=bus, human_queue=human_queue, sessionmaker=sessionmaker)
    return agent, bus, human_queue


def create_app(agent: EscalationAgent | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal agent
        bus = human_queue = None
        if agent is None:  # production path
            agent, bus, human_queue = build_runtime()
            await bus.connect()
            await human_queue.connect()
        await agent.start()
        agent.start_retry_loop()
        yield
        await agent.stop()
        if human_queue is not None:
            await human_queue.close()
        if bus is not None:
            await bus.close()

    app = FastAPI(title="OmniResolve Escalation Agent", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        body, content_type = agent.metrics.exposition()
        return Response(content=body, media_type=content_type)

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8004")))
