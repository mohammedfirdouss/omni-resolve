"""Unit tests for the AI Gateway (task 5.3)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ai_gateway.app import create_app
from ai_gateway.config import load_config, qualify_model, configured_providers
from ai_gateway.gateway import AIGateway, RateLimitExhausted


class FakeRateLimit(Exception):
    status_code = 429


def make_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def ok_completion(**kwargs):
    return {
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
        "choices": [{"message": {"content": "hello"}}],
        "model": kwargs.get("model"),
    }


async def test_429_retry_exhaustion_returns_structured_error():
    calls = {"n": 0}
    fake_now = {"t": 0.0}
    slept: list[float] = []

    async def always_429(**kwargs):
        calls["n"] += 1
        raise FakeRateLimit("rate limited")

    def clock() -> float:
        return fake_now["t"]

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        fake_now["t"] += seconds

    gateway = AIGateway(
        completion_fn=always_429, embedding_fn=always_429,
        env={}, clock=clock, sleep=fake_sleep,
    )
    app = create_app(gateway)
    async with make_client(app) as client:
        response = await client.post(
            "/v1/completions", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == 429
    body = response.json()
    assert body["provider"] == "openai"
    assert body["failure_reason"]
    assert body["elapsed_seconds"] >= 0
    # exponential backoff: 1, 2, 4, 8, 16, then 32 would exceed 60 s budget
    assert slept == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert calls["n"] == len(slept) + 1


async def test_config_swap_without_restart(monkeypatch):
    seen_models: list[str] = []

    async def spy_completion(**kwargs):
        seen_models.append(kwargs["model"])
        return await ok_completion(**kwargs)

    gateway = AIGateway(completion_fn=spy_completion, embedding_fn=spy_completion)
    app = create_app(gateway)

    monkeypatch.setenv("LITELLM_PROVIDER", "openai")
    monkeypatch.setenv("LITELLM_MODEL", "gpt-4o-mini")
    async with make_client(app) as client:
        await client.post("/v1/completions", json={"messages": [{"role": "user", "content": "a"}]})
        # swap provider+model between requests — same process, no restart
        monkeypatch.setenv("LITELLM_PROVIDER", "anthropic")
        monkeypatch.setenv("LITELLM_MODEL", "claude-3-5-haiku-20241022")
        await client.post("/v1/completions", json={"messages": [{"role": "user", "content": "b"}]})

    assert seen_models == ["openai/gpt-4o-mini", "anthropic/claude-3-5-haiku-20241022"]


async def test_health_probe_timeout_returns_unavailable():
    async def hanging_probe(**kwargs):
        await asyncio.sleep(30)

    gateway = AIGateway(
        completion_fn=hanging_probe, embedding_fn=hanging_probe,
        env={}, health_probe_timeout=0.01,
    )
    app = create_app(gateway)
    async with make_client(app) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    providers = response.json()["providers"]
    assert providers == [{"name": "openai", "status": "unavailable"}]


async def test_health_reports_all_configured_providers_available():
    gateway = AIGateway(
        completion_fn=ok_completion, embedding_fn=ok_completion,
        env={"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"},
    )
    app = create_app(gateway)
    async with make_client(app) as client:
        response = await client.get("/health")
    providers = {p["name"]: p["status"] for p in response.json()["providers"]}
    assert providers == {"openai": "available", "anthropic": "available"}


async def test_embeddings_endpoint_both_paths():
    gateway = AIGateway(
        completion_fn=ok_completion,
        embedding_fn=lambda **kw: ok_completion(**kw),
        env={},
    )
    app = create_app(gateway)
    async with make_client(app) as client:
        for path in ("/v1/embeddings", "/embeddings"):
            response = await client.post(path, json={"input": "hello"})
            assert response.status_code == 200


async def test_provider_error_returns_502():
    async def broken(**kwargs):
        raise RuntimeError("connection reset")

    gateway = AIGateway(completion_fn=broken, embedding_fn=broken, env={})
    app = create_app(gateway)
    async with make_client(app) as client:
        response = await client.post(
            "/v1/completions", json={"messages": [{"role": "user", "content": "hi"}]}
        )
    assert response.status_code == 502
    assert response.json()["provider"] == "openai"


async def test_metrics_endpoint_exposes_llm_metrics():
    gateway = AIGateway(completion_fn=ok_completion, embedding_fn=ok_completion, env={})
    app = create_app(gateway)
    async with make_client(app) as client:
        await client.post("/v1/completions", json={"messages": [{"role": "user", "content": "x"}]})
        response = await client.get("/metrics")
    assert b"omni_llm_calls_total" in response.content


def test_config_precedence_and_qualify(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    cfg.write_text('{"provider": "ollama", "model": "llama3"}')
    env = {"AI_GATEWAY_CONFIG_FILE": str(cfg)}
    config = load_config(env)
    assert (config.provider, config.model) == ("ollama", "llama3")
    # env beats file
    config = load_config({**env, "LITELLM_PROVIDER": "anthropic"})
    assert config.provider == "anthropic"
    assert qualify_model("openai", "gpt-4o-mini") == "openai/gpt-4o-mini"
    assert qualify_model("openai", "azure/foo") == "azure/foo"
    assert configured_providers({"OLLAMA_BASE_URL": "http://o"}) == ["openai", "ollama"]


async def test_latency_anomaly_recorded_for_slow_calls():
    fake_now = {"t": 0.0}

    async def slow_completion(**kwargs):
        fake_now["t"] += 6.0  # simulated 6 s call
        return await ok_completion(**kwargs)

    gateway = AIGateway(
        completion_fn=slow_completion, embedding_fn=slow_completion,
        env={}, clock=lambda: fake_now["t"],
    )
    await gateway.complete([{"role": "user", "content": "hi"}])
    kinds = [t.kind for t in gateway.trace_store.traces]
    assert "latency_anomaly" in kinds
    anomaly = next(t for t in gateway.trace_store.traces if t.kind == "latency_anomaly")
    parent = next(t for t in gateway.trace_store.traces if t.kind == "llm_call")
    assert anomaly.parent_trace_id == parent.trace_id


def test_main_builds_app_with_expected_routes():
    from ai_gateway.main import app as production_app

    paths = {route.path for route in production_app.routes}
    assert {"/v1/completions", "/v1/embeddings", "/embeddings", "/health", "/metrics"} <= paths
