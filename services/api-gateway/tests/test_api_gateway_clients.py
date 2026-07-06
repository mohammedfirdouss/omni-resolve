"""Unit tests for the embedding client, vector store wrapper, and app wiring."""

from __future__ import annotations

import httpx
import pytest

from api_gateway.embedding import EmbeddingClient, EmbeddingError
from api_gateway.vector_store import QdrantPolicyStore, VectorStoreError


def mock_transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_embedding_client_returns_vector():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/embeddings")
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    client = EmbeddingClient("http://ai-gw", transport=mock_transport(handler))
    assert await client.embed("hello") == [0.1, 0.2, 0.3]


async def test_embedding_client_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = EmbeddingClient("http://ai-gw", transport=mock_transport(handler))
    with pytest.raises(EmbeddingError):
        await client.embed("hello")


async def test_embedding_client_malformed_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": []}]})

    client = EmbeddingClient("http://ai-gw", transport=mock_transport(handler))
    with pytest.raises(EmbeddingError):
        await client.embed("hello")


async def test_embedding_client_transport_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = EmbeddingClient("http://ai-gw", transport=mock_transport(handler))
    with pytest.raises(EmbeddingError):
        await client.embed("hello")


class FakeQdrant:
    def __init__(self, *, exists: bool = False, fail: str | None = None) -> None:
        self.exists = exists
        self.fail = fail
        self.created = []
        self.upserted = []

    async def collection_exists(self, name: str) -> bool:
        if self.fail == "exists":
            raise ConnectionError("qdrant down")
        return self.exists

    async def create_collection(self, collection_name: str, vectors_config) -> None:
        self.created.append((collection_name, vectors_config))

    async def upsert(self, collection_name: str, points, wait: bool) -> None:
        if self.fail == "upsert":
            raise ConnectionError("qdrant down")
        self.upserted.append((collection_name, points))


async def test_policy_store_creates_collection_and_upserts():
    fake = FakeQdrant(exists=False)
    store = QdrantPolicyStore(fake)
    await store.upsert_policy(
        policy_id="p-1", vector=[0.1, 0.2], content="c", title="t", category="cat"
    )
    assert len(fake.created) == 1
    assert len(fake.upserted) == 1
    _, points = fake.upserted[0]
    assert points[0].payload["policy_id"] == "p-1"


async def test_policy_store_skips_create_when_collection_exists():
    fake = FakeQdrant(exists=True)
    store = QdrantPolicyStore(fake)
    await store.upsert_policy(
        policy_id="p-1", vector=[0.1], content="c", title="t", category="cat"
    )
    assert fake.created == []


async def test_policy_store_wraps_errors():
    store = QdrantPolicyStore(FakeQdrant(fail="exists"))
    with pytest.raises(VectorStoreError):
        await store.upsert_policy(
            policy_id="p", vector=[0.1], content="c", title="t", category="x"
        )
    store2 = QdrantPolicyStore(FakeQdrant(exists=True, fail="upsert"))
    with pytest.raises(VectorStoreError):
        await store2.upsert_policy(
            policy_id="p", vector=[0.1], content="c", title="t", category="x"
        )


def test_main_builds_app_with_expected_routes():
    from api_gateway.main import app

    paths = {route.path for route in app.routes}
    assert {"/tickets", "/tickets/{ticket_id}", "/policies",
            "/policies/{policy_id}", "/health", "/metrics"} <= paths
