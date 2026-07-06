"""Per-request provider/model configuration for the AI Gateway.

Configuration is resolved from the environment (and an optional JSON config
file) on EVERY request, so operators can swap providers or models by changing
``LITELLM_PROVIDER`` / ``LITELLM_MODEL`` (or the config file) without
restarting the gateway process (Requirement 6.2).

Recognised sources, in precedence order:
1. Environment variables: ``LITELLM_PROVIDER``, ``LITELLM_MODEL``,
   ``LITELLM_EMBEDDING_MODEL``, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``,
   ``OLLAMA_BASE_URL``.
2. Optional JSON config file pointed to by ``AI_GATEWAY_CONFIG_FILE`` with
   lowercase keys (``provider``, ``model``, ``embedding_model``).
3. Built-in defaults.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_PROVIDER = "openai"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

#: Minimal models used by the /health probe for providers that are configured
#: (via credentials) but are not the currently active provider.
DEFAULT_PROBE_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-20241022",
    "ollama": "llama3",
}

#: Env var that marks each provider as "configured" for /health probing.
PROVIDER_CREDENTIAL_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "ollama": "OLLAMA_BASE_URL",
}


@dataclass(frozen=True)
class GatewayConfig:
    """The active provider/model selection resolved for one request."""

    provider: str
    model: str
    embedding_model: str


def _read_config_file(env: Mapping[str, str]) -> dict[str, Any]:
    path = env.get("AI_GATEWAY_CONFIG_FILE")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_config(env: Mapping[str, str] | None = None) -> GatewayConfig:
    """Resolve the active configuration. Called once per request."""
    env = os.environ if env is None else env
    file_cfg = _read_config_file(env)

    def pick(env_name: str, file_key: str, default: str) -> str:
        return env.get(env_name) or str(file_cfg.get(file_key) or "") or default

    provider = pick("LITELLM_PROVIDER", "provider", DEFAULT_PROVIDER).lower()
    model = pick("LITELLM_MODEL", "model", DEFAULT_MODEL)
    embedding_model = pick(
        "LITELLM_EMBEDDING_MODEL", "embedding_model", DEFAULT_EMBEDDING_MODEL
    )
    return GatewayConfig(provider=provider, model=model, embedding_model=embedding_model)


def qualify_model(provider: str, model: str) -> str:
    """Return the litellm model string, e.g. ``anthropic/claude-3-5-haiku``.

    Models that already contain a provider prefix are passed through as-is.
    """
    if "/" in model:
        return model
    return f"{provider}/{model}"


def provider_call_kwargs(provider: str, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Credentials / endpoint kwargs to pass to litellm for ``provider``."""
    env = os.environ if env is None else env
    kwargs: dict[str, Any] = {}
    if provider == "openai" and env.get("OPENAI_API_KEY"):
        kwargs["api_key"] = env["OPENAI_API_KEY"]
    elif provider == "anthropic" and env.get("ANTHROPIC_API_KEY"):
        kwargs["api_key"] = env["ANTHROPIC_API_KEY"]
    elif provider == "ollama" and env.get("OLLAMA_BASE_URL"):
        kwargs["api_base"] = env["OLLAMA_BASE_URL"]
    return kwargs


def configured_providers(env: Mapping[str, str] | None = None) -> list[str]:
    """Providers the /health endpoint must probe.

    Always includes the currently active provider, plus every provider that
    has credentials present in the environment.
    """
    env = os.environ if env is None else env
    active = load_config(env).provider
    providers = [active]
    for provider, credential_env in PROVIDER_CREDENTIAL_ENV.items():
        if env.get(credential_env) and provider not in providers:
            providers.append(provider)
    return providers
