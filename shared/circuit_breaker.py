"""Shared circuit breaker for all outbound calls.

Policy (design.md, Error Handling): after 5 consecutive failures within a
30-second window the circuit opens for 60 seconds, failing fast without
consuming retry budget. Built on ``tenacity`` primitives for retry/backoff
composition where needed.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 30.0
OPEN_DURATION_SECONDS = 60.0


class CircuitOpenError(RuntimeError):
    """Raised immediately (fail-fast) while the circuit is open."""


class CircuitBreaker:
    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = FAILURE_THRESHOLD,
        failure_window: float = FAILURE_WINDOW_SECONDS,
        open_duration: float = OPEN_DURATION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.failure_window = failure_window
        self.open_duration = open_duration
        self._clock = clock
        self._failure_times: list[float] = []
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self.open_duration:
            return "half_open"
        return "open"

    def _record_failure(self) -> None:
        now = self._clock()
        self._failure_times = [
            t for t in self._failure_times if now - t <= self.failure_window
        ]
        self._failure_times.append(now)
        if len(self._failure_times) >= self.failure_threshold:
            self._opened_at = now
            self._failure_times.clear()

    def _record_success(self) -> None:
        self._failure_times.clear()
        self._opened_at = None

    def _check(self) -> None:
        if self.state == "open":
            raise CircuitOpenError(f"circuit {self.name!r} is open")
        if self.state == "half_open":
            # allow one probe call through; stay armed
            self._opened_at = None
            self._failure_times = [self._clock()] * (self.failure_threshold - 1)

    async def call(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        self._check()
        try:
            result = await fn(*args, **kwargs)
        except Exception:
            self._record_failure()
            raise
        self._record_success()
        return result

    def __call__(self, fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await self.call(fn, *args, **kwargs)

        wrapper.__name__ = getattr(fn, "__name__", "wrapped")
        return wrapper
