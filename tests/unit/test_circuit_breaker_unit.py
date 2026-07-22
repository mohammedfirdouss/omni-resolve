"""Unit tests for the shared circuit breaker (shared/circuit_breaker.py).

Policy under test (design.md, Error Handling): after `failure_threshold`
consecutive failures within `failure_window` seconds the circuit opens for
`open_duration` seconds, failing fast (CircuitOpenError) without invoking the
wrapped callable; after the open duration elapses the circuit allows a single
probe call through (half-open) before fully resetting on success.
"""

from __future__ import annotations

import pytest

from shared.circuit_breaker import CircuitBreaker, CircuitOpenError


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _boom(*_args, **_kwargs):
    raise ValueError("upstream failure")


async def _ok(*_args, **_kwargs):
    return "ok"


@pytest.mark.asyncio
async def test_starts_closed_and_calls_pass_through():
    breaker = CircuitBreaker(name="t1", clock=FakeClock())
    assert breaker.state == "closed"
    result = await breaker.call(_ok)
    assert result == "ok"
    assert breaker.state == "closed"


@pytest.mark.asyncio
async def test_opens_after_failure_threshold_within_window():
    clock = FakeClock()
    breaker = CircuitBreaker(name="t2", failure_threshold=3, failure_window=30.0, clock=clock)

    for _ in range(2):
        with pytest.raises(ValueError):
            await breaker.call(_boom)
    assert breaker.state == "closed"

    with pytest.raises(ValueError):
        await breaker.call(_boom)
    assert breaker.state == "open"


@pytest.mark.asyncio
async def test_open_circuit_fails_fast_without_invoking_wrapped_call():
    clock = FakeClock()
    breaker = CircuitBreaker(name="t3", failure_threshold=2, failure_window=30.0, clock=clock)
    calls = {"n": 0}

    async def counting_boom(*_a, **_k):
        calls["n"] += 1
        raise ValueError("fail")

    for _ in range(2):
        with pytest.raises(ValueError):
            await breaker.call(counting_boom)
    assert breaker.state == "open"
    assert calls["n"] == 2

    with pytest.raises(CircuitOpenError):
        await breaker.call(counting_boom)
    # the wrapped callable must NOT be invoked while open
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_failures_outside_window_do_not_accumulate():
    clock = FakeClock()
    breaker = CircuitBreaker(name="t4", failure_threshold=3, failure_window=10.0, clock=clock)

    with pytest.raises(ValueError):
        await breaker.call(_boom)
    clock.advance(11.0)  # outside the failure window; old failure should expire
    with pytest.raises(ValueError):
        await breaker.call(_boom)
    assert breaker.state == "closed"


@pytest.mark.asyncio
async def test_half_open_probe_allowed_after_open_duration_elapses():
    clock = FakeClock()
    breaker = CircuitBreaker(
        name="t5", failure_threshold=2, failure_window=30.0, open_duration=60.0, clock=clock
    )

    for _ in range(2):
        with pytest.raises(ValueError):
            await breaker.call(_boom)
    assert breaker.state == "open"

    with pytest.raises(CircuitOpenError):
        await breaker.call(_boom)

    clock.advance(60.0)
    assert breaker.state == "half_open"

    # the probe call is allowed through during half-open
    result = await breaker.call(_ok)
    assert result == "ok"
    assert breaker.state == "closed"


@pytest.mark.asyncio
async def test_success_resets_failure_count():
    clock = FakeClock()
    breaker = CircuitBreaker(name="t6", failure_threshold=3, failure_window=30.0, clock=clock)

    with pytest.raises(ValueError):
        await breaker.call(_boom)
    with pytest.raises(ValueError):
        await breaker.call(_boom)
    assert breaker.state == "closed"

    await breaker.call(_ok)  # success clears the failure history
    assert breaker._failure_times == []

    with pytest.raises(ValueError):
        await breaker.call(_boom)
    with pytest.raises(ValueError):
        await breaker.call(_boom)
    # only 2 consecutive failures since the reset; threshold is 3
    assert breaker.state == "closed"


def test_decorator_usage_wraps_async_callable():
    clock = FakeClock()
    breaker = CircuitBreaker(name="t7", clock=clock)

    @breaker
    async def wrapped():
        return 42

    assert wrapped.__name__ == "wrapped"
