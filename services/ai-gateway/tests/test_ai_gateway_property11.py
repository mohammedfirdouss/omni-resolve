"""Property test for AI Gateway latency metadata (Property 11)."""

from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings, strategies as st

from ai_gateway.gateway import AIGateway, GatewayError

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

REQUIRED_FIELDS = ("provider", "model", "prompt_tokens", "completion_tokens", "latency_seconds")


class FakeRateLimit(Exception):
    status_code = 429


outcomes = st.one_of(
    # success with arbitrary usage counts
    st.fixed_dictionaries(
        {
            "kind": st.just("success"),
            "prompt_tokens": st.integers(min_value=0, max_value=1_000_000),
            "completion_tokens": st.integers(min_value=0, max_value=1_000_000),
        }
    ),
    # provider failure
    st.fixed_dictionaries(
        {"kind": st.just("error"), "message": st.text(max_size=50)}
    ),
    # rate limit that never clears
    st.fixed_dictionaries({"kind": st.just("rate_limit")}),
)


# Feature: omni-resolve, Property 11: AI Gateway latency metadata is always recorded
@SETTINGS
@given(outcome=outcomes, operation=st.sampled_from(["completion", "embedding"]))
def test_property_11_latency_metadata_always_recorded(outcome, operation):
    async def case():
        async def fake_call(**kwargs):
            if outcome["kind"] == "error":
                raise RuntimeError(outcome["message"])
            if outcome["kind"] == "rate_limit":
                raise FakeRateLimit("429")
            return {
                "usage": {
                    "prompt_tokens": outcome["prompt_tokens"],
                    "completion_tokens": outcome["completion_tokens"],
                },
                "choices": [{"message": {"content": "ok"}}],
            }

        async def no_sleep(_):
            return None

        gateway = AIGateway(
            completion_fn=fake_call,
            embedding_fn=fake_call,
            env={},
            sleep=no_sleep,
            retry_max_elapsed=0.0,  # rate limits exhaust immediately
        )

        try:
            if operation == "completion":
                await gateway.complete([{"role": "user", "content": "hi"}])
            else:
                await gateway.embed(["hi"])
        except GatewayError:
            pass  # failures must still be recorded

        assert len(gateway.trace_store.traces) >= 1
        trace = gateway.trace_store.traces[0]
        for field in REQUIRED_FIELDS:
            assert field in trace.data, f"missing {field}"
        assert isinstance(trace.data["latency_seconds"], float)
        if outcome["kind"] == "success":
            assert trace.data["prompt_tokens"] == outcome["prompt_tokens"]
            assert trace.data["completion_tokens"] == outcome["completion_tokens"]

    asyncio.run(case())
