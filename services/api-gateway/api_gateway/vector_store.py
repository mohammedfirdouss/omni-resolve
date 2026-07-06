"""Qdrant policy vector store used by policy ingestion.

Policy content + embedding live in Qdrant collection ``policies`` (cosine
distance); only metadata lives in the State_Store. A Qdrant point upsert is
atomic per point id, which gives Requirement 12.3's atomic replace semantics.
"""

from __future__ import annotations

from typing import Any

POLICIES_COLLECTION = "policies"


class VectorStoreError(RuntimeError):
    """The Vector_Store rejected or failed the operation."""


class QdrantPolicyStore:
    """Wrapper over an (injectable) ``qdrant_client.AsyncQdrantClient``."""

    def __init__(self, client: Any, collection_name: str = POLICIES_COLLECTION) -> None:
        self._client = client
        self._collection_name = collection_name

    async def ensure_collection(self, vector_size: int) -> None:
        from qdrant_client import models as qmodels

        try:
            if not await self._client.collection_exists(self._collection_name):
                await self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=qmodels.VectorParams(
                        size=vector_size, distance=qmodels.Distance.COSINE
                    ),
                )
        except Exception as exc:
            raise VectorStoreError(f"failed to ensure collection: {exc}") from exc

    async def upsert_policy(
        self,
        *,
        policy_id: str,
        vector: list[float],
        content: str,
        title: str,
        category: str,
    ) -> None:
        """Atomically upsert (replace) the policy's vector and content."""
        from qdrant_client import models as qmodels

        await self.ensure_collection(len(vector))
        try:
            await self._client.upsert(
                collection_name=self._collection_name,
                points=[
                    qmodels.PointStruct(
                        id=policy_id,
                        vector=vector,
                        payload={
                            "policy_id": policy_id,
                            "content": content,
                            "title": title,
                            "category": category,
                        },
                    )
                ],
                wait=True,
            )
        except Exception as exc:
            raise VectorStoreError(f"failed to upsert policy vector: {exc}") from exc
