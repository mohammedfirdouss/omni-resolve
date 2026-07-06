"""FastAPI application factory for the OmniResolve API Gateway.

All I/O dependencies (State_Store sessionmaker, Event_Bus, embedding client,
policy vector store) are injected so tests can run against in-memory doubles.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db import (
    PolicyDocument,
    create_ticket,
    get_ticket_history,
)
from shared.event_bus import EventBus
from shared.models import (
    EventType,
    PolicyCreateRequest,
    TicketCreateRequest,
    new_event,
)
from shared.observability import ServiceMetrics, make_metrics_app

from api_gateway.embedding import EmbeddingClient, EmbeddingError
from api_gateway.vector_store import QdrantPolicyStore, VectorStoreError

SERVICE_NAME = "api-gateway"


def _validation_error_body(exc: RequestValidationError) -> dict[str, Any]:
    """Map Pydantic v2 validation errors to the shared 422 error shape."""
    errors = []
    for err in exc.errors():
        loc = [str(part) for part in err["loc"] if part != "body"]
        errors.append({"field": ".".join(loc) or "body", "violation": err["msg"]})
    return {"errors": errors}


def create_app(
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    event_bus: EventBus,
    embedding_client: EmbeddingClient,
    policy_store: QdrantPolicyStore,
    metrics: ServiceMetrics | None = None,
) -> FastAPI:
    app = FastAPI(title="OmniResolve API Gateway")
    metrics = metrics or ServiceMetrics(SERVICE_NAME)
    app.state.metrics = metrics

    @app.exception_handler(RequestValidationError)
    async def _handle_422(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_validation_error_body(exc))

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        started = time.monotonic()
        response: Response = await call_next(request)
        metrics.observe_request(
            request.url.path, str(response.status_code), time.monotonic() - started
        )
        return response

    # ------------------------------------------------------------------ tickets

    @app.post("/tickets", status_code=201)
    async def post_ticket(body: TicketCreateRequest) -> JSONResponse:
        try:
            async with sessionmaker() as session:
                ticket = await create_ticket(
                    session,
                    customer_id=body.customer_id,
                    category=body.category,
                    description=body.description,
                )
                await session.commit()
                ticket_id = ticket.ticket_id
        except Exception:
            # Requirement 1.5: State_Store unavailable -> 503, no event published.
            return JSONResponse(
                status_code=503, content={"detail": "state store unavailable"}
            )

        await event_bus.publish(
            new_event(
                EventType.TICKET_CREATED.value,
                ticket_id,
                {
                    "customer_id": body.customer_id,
                    "category": body.category,
                    "description": body.description,
                },
            )
        )
        return JSONResponse(status_code=201, content={"ticket_id": ticket_id})

    @app.get("/tickets/{ticket_id}")
    async def get_ticket(ticket_id: str) -> JSONResponse:
        async with sessionmaker() as session:
            history = await get_ticket_history(session, ticket_id)
        if history is None:
            return JSONResponse(status_code=404, content={"detail": "ticket not found"})
        return JSONResponse(status_code=200, content=history)

    # ----------------------------------------------------------------- policies

    @app.post("/policies", status_code=201)
    async def post_policy(body: PolicyCreateRequest) -> JSONResponse:
        # Embed first: an embedding failure must leave no partial state
        # (Requirement 12.4 / Property 16).
        try:
            vector = await embedding_client.embed(body.content)
        except EmbeddingError:
            return JSONResponse(
                status_code=502, content={"detail": "embedding model error"}
            )

        policy_id = body.policy_id or str(uuid.uuid4())
        try:
            async with sessionmaker() as session:
                existing = await session.get(PolicyDocument, policy_id)
                if existing is None:
                    session.add(
                        PolicyDocument(
                            policy_id=policy_id, title=body.title, category=body.category
                        )
                    )
                else:
                    existing.title = body.title
                    existing.category = body.category
                # Vector upsert happens inside the DB transaction: if it fails
                # we roll back so neither store keeps partial state (12.3).
                await policy_store.upsert_policy(
                    policy_id=policy_id,
                    vector=vector,
                    content=body.content,
                    title=body.title,
                    category=body.category,
                )
                await session.commit()
        except VectorStoreError:
            return JSONResponse(
                status_code=502, content={"detail": "vector store error"}
            )
        except Exception:
            return JSONResponse(
                status_code=503, content={"detail": "state store unavailable"}
            )

        return JSONResponse(status_code=201, content={"policy_id": policy_id})

    @app.get("/policies/{policy_id}")
    async def get_policy(policy_id: str) -> JSONResponse:
        async with sessionmaker() as session:
            policy = await session.get(PolicyDocument, policy_id)
        if policy is None:
            return JSONResponse(status_code=404, content={"detail": "policy not found"})
        return JSONResponse(
            status_code=200,
            content={
                "policy_id": policy.policy_id,
                "title": policy.title,
                "category": policy.category,
                "ingested_at": policy.ingested_at.isoformat(),
            },
        )

    # ------------------------------------------------------------------- probes

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    app.mount("/metrics", make_metrics_app(metrics))

    return app
