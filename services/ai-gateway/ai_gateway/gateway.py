"""Core AI Gateway logic: LiteLLM dispatch, 429 retry, instrumentation.

Every inference call — completions, embeddings, and health probes, whether it
succeeds or fails — records provider name, model name, prompt token count,
completion token count, and wall-clock latency to the Observability_Stack
(Requirement 6.4 / Property 11) via ``shared.observability.ServiceMetrics``
and a ``TraceStore`` record. Latency > 5 s auto-records a linked anomaly
trace inside ``TraceStore.record_trace``.

All timing knobs (retry delay, retry budget, health probe timeout) and all
side-effecting collaborators (completion fn, embedding fn, probe fn, clock,
sleep) are constructor-injectable so tests never touch the network or sleep
real seconds. Spec defaults are preserved as default values.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Awaitable, Callable, Mapping

from shared.observability import ServiceMetrics, TraceStore

from ai_gateway.config import (
    DEFAULT_PROBE_MODELS,
    configured_providers,
    load_config,
    provider_call_kwargs,
    qualify_model,
)

SERVICE_NAME = "ai-gateway"

# Spec defaults (Requirements 6.3, 6.5).
RETRY_INITIAL_DELAY_SECONDS = 1.0
RETRY_MAX_ELAPSED_SECONDS = 60.0
HEALTH_PROBE_TIMEOUT_SECONDS = 5.0

AsyncCallable = Callable[..., Awaitable[Any]]


class GatewayError(Exception):
    """Base for structured gateway errors carrying the provider name."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(reason)
        self.provider = provider
        self.reason = reason


class RateLimitExhausted(GatewayError):
    """Raised when 429 retries exhaust the retry budget (Requirement 6.3)."""

    def __init__(self, provider: str, reason: str, elapsed_seconds: float) -> None:
        super().__init__(provider, reason)
        self.elapsed_seconds = elapsed_seconds


class ProviderCallError(GatewayError):
    """Raised for non-rate-limit provider failures."""


def _is_rate_limit(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 429


def _usage_field(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def extract_usage(response: Any) -> tuple[int, int]:
    """(prompt_tokens, completion_tokens) from a provider response; 0 if absent."""
    usage = response.get("usage") if isinstance(response, dict) else getattr(response, "usage", None)
    return _usage_field(usage, "prompt_tokens"), _usage_field(usage, "completion_tokens")


def response_payload(response: Any) -> dict[str, Any]:
    """Serialise a provider response object to a JSON-safe dict."""
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    if hasattr(response, "dict"):
        return response.dict()
    return {"result": str(response)}


class AIGateway:
    """LiteLLM-backed gateway with per-request config and full instrumentation."""

    def __init__(
        self,
        *,
        completion_fn: AsyncCallable | None = None,
        embedding_fn: AsyncCallable | None = None,
        probe_fn: AsyncCallable | None = None,
        metrics: ServiceMetrics | None = None,
        trace_store: TraceStore | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        retry_initial_delay: float = RETRY_INITIAL_DELAY_SECONDS,
        retry_max_elapsed: float = RETRY_MAX_ELAPSED_SECONDS,
        health_probe_timeout: float = HEALTH_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        if completion_fn is None or embedding_fn is None:
            import litellm  # deferred: heavy import, unused when fns injected

            completion_fn = completion_fn or litellm.acompletion
            embedding_fn = embedding_fn or litellm.aembedding
        self._completion_fn = completion_fn
        self._embedding_fn = embedding_fn
        self._probe_fn = probe_fn or completion_fn
        self.metrics = metrics or ServiceMetrics(SERVICE_NAME)
        self.trace_store = trace_store or TraceStore()
        self._env = env  # None -> live os.environ lookup on every request
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self.retry_initial_delay = retry_initial_delay
        self.retry_max_elapsed = retry_max_elapsed
        self.health_probe_timeout = health_probe_timeout

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        ticket_id: str = "",
        **params: Any,
    ) -> dict[str, Any]:
        """Chat/text completion via the currently configured provider/model."""
        config = load_config(self._env)
        model = qualify_model(config.provider, config.model)
        call_kwargs = {
            "model": model,
            "messages": messages,
            **provider_call_kwargs(config.provider, self._env),
            **{k: v for k, v in params.items() if v is not None},
        }
        response = await self._instrumented(
            operation="completion",
            fn=self._completion_fn,
            call_kwargs=call_kwargs,
            provider=config.provider,
            model=model,
            ticket_id=ticket_id,
        )
        return response_payload(response)

    async def embed(
        self,
        input_texts: list[str],
        *,
        ticket_id: str = "",
        **params: Any,
    ) -> dict[str, Any]:
        """Embeddings via the currently configured provider/embedding model."""
        config = load_config(self._env)
        model = qualify_model(config.provider, config.embedding_model)
        call_kwargs = {
            "model": model,
            "input": input_texts,
            **provider_call_kwargs(config.provider, self._env),
            **{k: v for k, v in params.items() if v is not None},
        }
        response = await self._instrumented(
            operation="embedding",
            fn=self._embedding_fn,
            call_kwargs=call_kwargs,
            provider=config.provider,
            model=model,
            ticket_id=ticket_id,
        )
        return response_payload(response)

    async def health(self) -> list[dict[str, str]]:
        """Probe each configured provider with a minimal request (Req 6.5)."""
        config = load_config(self._env)
        results: list[dict[str, str]] = []
        for provider in configured_providers(self._env):
            if provider == config.provider:
                model_name = config.model
            else:
                model_name = DEFAULT_PROBE_MODELS.get(provider, config.model)
            model = qualify_model(provider, model_name)
            call_kwargs = {
                "model": model,
                "messages": [{"role": "user", "content": "healthcheck"}],
                "max_tokens": 1,
                **provider_call_kwargs(provider, self._env),
            }
            start = self._clock()
            prompt_tokens = completion_tokens = 0
            try:
                response = await asyncio.wait_for(
                    self._probe_fn(**call_kwargs), timeout=self.health_probe_timeout
                )
                prompt_tokens, completion_tokens = extract_usage(response)
                status, outcome, error = "available", "success", None
            except Exception as exc:  # noqa: BLE001 - any failure => unavailable
                status, outcome = "unavailable", "error"
                error = str(exc) or type(exc).__name__
            self._record_call(
                operation="health_probe",
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_seconds=self._clock() - start,
                outcome=outcome,
                ticket_id="",
                error=error,
            )
            results.append({"name": provider, "status": status})
        return results

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _instrumented(
        self,
        *,
        operation: str,
        fn: AsyncCallable,
        call_kwargs: dict[str, Any],
        provider: str,
        model: str,
        ticket_id: str,
    ) -> Any:
        """Dispatch with 429 retry; record all five metadata fields always."""
        start = self._clock()
        try:
            response = await self._call_with_retry(fn, call_kwargs, provider, start)
        except RateLimitExhausted as exc:
            self._record_call(
                operation=operation,
                provider=provider,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                latency_seconds=self._clock() - start,
                outcome="rate_limit_exhausted",
                ticket_id=ticket_id,
                error=exc.reason,
            )
            raise
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            self._record_call(
                operation=operation,
                provider=provider,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                latency_seconds=self._clock() - start,
                outcome="error",
                ticket_id=ticket_id,
                error=reason,
            )
            raise ProviderCallError(provider, reason) from exc
        prompt_tokens, completion_tokens = extract_usage(response)
        self._record_call(
            operation=operation,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_seconds=self._clock() - start,
            outcome="success",
            ticket_id=ticket_id,
            error=None,
        )
        return response

    async def _call_with_retry(
        self,
        fn: AsyncCallable,
        call_kwargs: dict[str, Any],
        provider: str,
        start: float,
    ) -> Any:
        """Retry HTTP 429 with exponential backoff (1 s, doubling) up to 60 s.

        The next backoff sleep is only taken if it fits within the total
        retry budget; otherwise a ``RateLimitExhausted`` carrying provider,
        reason, and total elapsed seconds is raised (Requirement 6.3).
        """
        delay = self.retry_initial_delay
        while True:
            try:
                return await fn(**call_kwargs)
            except Exception as exc:
                if not _is_rate_limit(exc):
                    raise
                elapsed = self._clock() - start
                if elapsed + delay > self.retry_max_elapsed:
                    reason = str(exc) or "rate limited (HTTP 429)"
                    raise RateLimitExhausted(provider, reason, elapsed) from exc
                await self._sleep(delay)
                delay *= 2

    def _record_call(
        self,
        *,
        operation: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_seconds: float,
        outcome: str,
        ticket_id: str,
        error: str | None,
    ) -> None:
        """Requirement 6.4 / Property 11: record the five metadata fields."""
        self.metrics.llm_calls_total.labels(SERVICE_NAME, provider, model, outcome).inc()
        self.metrics.llm_latency.labels(SERVICE_NAME, provider, model).observe(latency_seconds)
        data: dict[str, Any] = {
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "latency_seconds": latency_seconds,
            "operation": operation,
            "outcome": outcome,
        }
        if error is not None:
            data["error"] = error
        self.trace_store.record_trace(
            trace_id=uuid.uuid4().hex,
            agent=SERVICE_NAME,
            ticket_id=ticket_id,
            kind="llm_call",
            data=data,
            latency_seconds=latency_seconds,
        )
