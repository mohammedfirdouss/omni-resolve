"""FastAPI HTTP surface over the AIGateway core."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai_gateway.gateway import (
    SERVICE_NAME,
    AIGateway,
    GatewayError,
    ProviderCallError,
    RateLimitExhausted,
)


class CompletionRequest(BaseModel):
    messages: list[dict[str, Any]] = Field(min_length=1)
    ticket_id: str = ""
    max_tokens: int | None = None
    temperature: float | None = None


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    ticket_id: str = ""


def create_app(gateway: AIGateway | None = None) -> FastAPI:
    app = FastAPI(title="OmniResolve AI Gateway")
    gateway = gateway or AIGateway()
    app.state.gateway = gateway

    @app.exception_handler(RateLimitExhausted)
    async def _handle_rate_limit(request: Request, exc: RateLimitExhausted) -> JSONResponse:
        # Requirement 6.3: provider name, failure reason, total elapsed duration.
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exhausted",
                "provider": exc.provider,
                "failure_reason": exc.reason,
                "elapsed_seconds": round(exc.elapsed_seconds, 3),
            },
        )

    @app.exception_handler(ProviderCallError)
    async def _handle_provider_error(request: Request, exc: ProviderCallError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={
                "error": "provider_error",
                "provider": exc.provider,
                "failure_reason": exc.reason,
            },
        )

    @app.middleware("http")
    async def _observe(request: Request, call_next):
        started = time.monotonic()
        response: Response = await call_next(request)
        gateway.metrics.observe_request(
            request.url.path, str(response.status_code), time.monotonic() - started
        )
        return response

    @app.post("/v1/completions")
    @app.post("/v1/chat/completions")
    async def completions(body: CompletionRequest) -> dict[str, Any]:
        return await gateway.complete(
            body.messages,
            ticket_id=body.ticket_id,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
        )

    @app.post("/v1/embeddings")
    @app.post("/embeddings")
    async def embeddings(body: EmbeddingRequest) -> dict[str, Any]:
        texts = [body.input] if isinstance(body.input, str) else body.input
        return await gateway.embed(texts, ticket_id=body.ticket_id)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        providers = await gateway.health()
        return {"service": SERVICE_NAME, "providers": providers}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        body, content_type = gateway.metrics.exposition()
        return Response(content=body, media_type=content_type)

    return app
