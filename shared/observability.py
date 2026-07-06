"""Prometheus registry helpers + Langfuse-compatible tracing hooks.

Every microservice mounts ``/metrics`` via ``make_metrics_app`` and records
request rate, error rate, and latency through ``ServiceMetrics``. AI-specific
traces (LLM prompts, retrieved policy IDs, actions, outcomes) flow through
``record_trace``; calls slower than LATENCY_ANOMALY_THRESHOLD_SECONDS are
additionally recorded as latency anomalies linked to their parent trace.

Langfuse export is optional: if the ``langfuse`` package/config is absent,
traces are retained in-process (ring buffer) and logged, which also serves
unit tests.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logger = logging.getLogger("omniresolve.observability")

LATENCY_ANOMALY_THRESHOLD_SECONDS = 5.0
ERROR_RATE_ALERT_THRESHOLD = 0.05
ERROR_RATE_WINDOW_SECONDS = 300.0


class ServiceMetrics:
    """Per-service Prometheus metrics: request rate, error rate, p95 latency."""

    def __init__(self, service_name: str, registry: CollectorRegistry | None = None) -> None:
        self.service_name = service_name
        self.registry = registry or CollectorRegistry()
        self.requests_total = Counter(
            "omni_requests_total",
            "Total requests handled",
            ["service", "operation", "status"],
            registry=self.registry,
        )
        self.request_latency = Histogram(
            "omni_request_latency_seconds",
            "Request latency in seconds",
            ["service", "operation"],
            registry=self.registry,
        )
        self.llm_calls_total = Counter(
            "omni_llm_calls_total",
            "LLM inference calls",
            ["service", "provider", "model", "outcome"],
            registry=self.registry,
        )
        self.llm_latency = Histogram(
            "omni_llm_latency_seconds",
            "LLM call wall-clock latency",
            ["service", "provider", "model"],
            registry=self.registry,
        )
        self._events: deque[tuple[float, bool]] = deque(maxlen=100_000)
        self._alert_channel = None  # set via configure_alert_channel

    def configure_alert_channel(self, channel: Any) -> None:
        """channel: callable(dict) -> None; None means fall back to ERROR log."""
        self._alert_channel = channel

    def observe_request(self, operation: str, status: str, latency_seconds: float) -> None:
        self.requests_total.labels(self.service_name, operation, status).inc()
        self.request_latency.labels(self.service_name, operation).observe(latency_seconds)
        is_error = status.startswith("5") or status == "error"
        self._events.append((time.monotonic(), is_error))
        self._maybe_alert_error_rate()

    def error_rate(self, window_seconds: float = ERROR_RATE_WINDOW_SECONDS) -> float:
        cutoff = time.monotonic() - window_seconds
        recent = [e for t, e in self._events if t >= cutoff]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)

    def _maybe_alert_error_rate(self) -> None:
        rate = self.error_rate()
        if rate > ERROR_RATE_ALERT_THRESHOLD:
            alert = {
                "alert": "error_rate_exceeded",
                "service": self.service_name,
                "error_rate": round(rate, 4),
                "threshold": ERROR_RATE_ALERT_THRESHOLD,
                "window_seconds": ERROR_RATE_WINDOW_SECONDS,
            }
            if self._alert_channel is not None:
                self._alert_channel(alert)
            else:
                logger.error("ALERT (no channel configured): %s", alert)

    def exposition(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


# ---------------------------------------------------------------------------
# AI-specific tracing (Langfuse-compatible shape)
# ---------------------------------------------------------------------------


@dataclass
class Trace:
    trace_id: str
    agent: str
    ticket_id: str
    kind: str  # llm_call | retrieval | action | reasoning_step | latency_anomaly
    data: dict[str, Any] = field(default_factory=dict)
    parent_trace_id: str | None = None
    recorded_at: float = field(default_factory=time.time)


class TraceStore:
    """In-process trace sink; mirrors what gets exported to Langfuse."""

    def __init__(self, maxlen: int = 50_000) -> None:
        self.traces: deque[Trace] = deque(maxlen=maxlen)
        self._langfuse = None  # optional exporter

    def record_trace(
        self,
        *,
        trace_id: str,
        agent: str,
        ticket_id: str,
        kind: str,
        data: dict[str, Any],
        parent_trace_id: str | None = None,
        latency_seconds: float | None = None,
    ) -> list[Trace]:
        """Record a trace; auto-record a linked latency anomaly when slow."""
        recorded = [
            Trace(
                trace_id=trace_id,
                agent=agent,
                ticket_id=ticket_id,
                kind=kind,
                data=data,
                parent_trace_id=parent_trace_id,
            )
        ]
        if latency_seconds is not None and latency_seconds > LATENCY_ANOMALY_THRESHOLD_SECONDS:
            recorded.append(
                Trace(
                    trace_id=f"{trace_id}:anomaly",
                    agent=agent,
                    ticket_id=ticket_id,
                    kind="latency_anomaly",
                    data={"latency_seconds": latency_seconds, **data},
                    parent_trace_id=trace_id,
                )
            )
        for t in recorded:
            self.traces.append(t)
            logger.debug("trace %s/%s recorded", t.agent, t.kind)
        return recorded


GLOBAL_TRACE_STORE = TraceStore()


def record_latency_anomaly(
    *, trace_id: str, agent: str, ticket_id: str, latency_seconds: float, data: dict[str, Any]
) -> Trace:
    trace = Trace(
        trace_id=f"{trace_id}:anomaly",
        agent=agent,
        ticket_id=ticket_id,
        kind="latency_anomaly",
        data={"latency_seconds": latency_seconds, **data},
        parent_trace_id=trace_id,
    )
    GLOBAL_TRACE_STORE.traces.append(trace)
    return trace


def make_metrics_app(metrics: ServiceMetrics):
    """ASGI app serving the Prometheus exposition format (mount at /metrics)."""

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body, content_type = metrics.exposition()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", content_type.encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app
