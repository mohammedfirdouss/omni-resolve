"""HTTP client for content embeddings served by the AI_Gateway.

The API Gateway never talks to LLM providers directly (Requirement 6.1); all
embedding calls go through the AI_Gateway's ``/embeddings`` endpoint.
"""

from __future__ import annotations

import os

import httpx

DEFAULT_AI_GATEWAY_URL = "http://ai-gateway:8000"
DEFAULT_EMBED_TIMEOUT_SECONDS = 10.0  # Requirement 12.2: ingestion SLA <= 10 s


class EmbeddingError(RuntimeError):
    """The embedding model returned an error or an unusable response."""


class EmbeddingClient:
    """Thin httpx client for ``POST {AI_GATEWAY_URL}/embeddings``.

    ``transport`` is injectable so tests can use ``httpx.MockTransport``
    without any network I/O; ``timeout`` is injectable per the shared brief.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_EMBED_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (
            base_url or os.environ.get("AI_GATEWAY_URL", DEFAULT_AI_GATEWAY_URL)
        ).rstrip("/")
        self._timeout = timeout
        self._transport = transport

    async def embed(self, text: str) -> list[float]:
        """Return the embedding vector for ``text``.

        Raises EmbeddingError on any transport error, non-2xx response, or a
        response that does not contain a non-empty numeric vector.
        """
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=self._timeout
            ) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings", json={"input": text}
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc

        try:
            vector = data["data"][0]["embedding"]
            if not isinstance(vector, list) or not vector:
                raise ValueError("embedding must be a non-empty list")
            return [float(component) for component in vector]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EmbeddingError(f"malformed embedding response: {exc}") from exc
