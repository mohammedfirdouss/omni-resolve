"""Unit tests for the API Gateway (task 2.7)."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apigw_helpers import FakePolicyStore, make_env

TICKET = {"customer_id": "c-1", "category": "refund", "description": "please refund"}
POLICY = {"title": "Refund policy", "content": "30-day returns", "category": "billing"}


async def test_state_store_unavailable_returns_503_and_no_event():
    # Engine pointing at a port nothing listens on -> connect error on use.
    dead_engine = create_async_engine("postgresql+asyncpg://x:x@127.0.0.1:1/none")
    env = await make_env(sessionmaker=async_sessionmaker(dead_engine, expire_on_commit=False))
    async with env.client() as client:
        response = await client.post("/tickets", json=TICKET)
    assert response.status_code == 503
    assert env.bus.published == []  # Requirement 1.5: no ticket.created


async def test_unknown_ticket_id_returns_404():
    env = await make_env()
    async with env.client() as client:
        response = await client.get(f"/tickets/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_unknown_policy_id_returns_404():
    env = await make_env()
    async with env.client() as client:
        response = await client.get(f"/policies/{uuid.uuid4()}")
    assert response.status_code == 404


async def test_ticket_ids_are_unique_uuid4():
    env = await make_env()
    ids = []
    async with env.client() as client:
        for _ in range(50):
            response = await client.post("/tickets", json=TICKET)
            assert response.status_code == 201
            ids.append(response.json()["ticket_id"])
    assert len(set(ids)) == 50
    for ticket_id in ids:
        assert uuid.UUID(ticket_id).version == 4


async def test_ticket_created_event_published_with_canonical_schema():
    env = await make_env()
    async with env.client() as client:
        response = await client.post("/tickets", json=TICKET)
    events = env.bus.events_of_type("ticket.created")
    assert len(events) == 1
    event = events[0]
    assert event.ticket_id == response.json()["ticket_id"]
    assert event.payload == TICKET


async def test_policy_upsert_replaces_existing_metadata_and_vector():
    env = await make_env()
    async with env.client() as client:
        first = await client.post("/policies", json=POLICY)
        policy_id = first.json()["policy_id"]

        updated = dict(POLICY, title="Refund policy v2", policy_id=policy_id)
        second = await client.post("/policies", json=updated)
        assert second.status_code == 201
        assert second.json()["policy_id"] == policy_id

        fetched = await client.get(f"/policies/{policy_id}")
    assert fetched.json()["title"] == "Refund policy v2"
    assert env.store.upserts[policy_id]["title"] == "Refund policy v2"
    assert len(env.store.upserts) == 1


async def test_vector_store_failure_returns_502_and_rolls_back_metadata():
    env = await make_env(store=FakePolicyStore(fail=True))
    async with env.client() as client:
        response = await client.post("/policies", json=POLICY)
        assert response.status_code == 502
        # metadata write rolled back -> any GET must 404
        from sqlalchemy import select

        from shared.db import PolicyDocument

        async with env.sessionmaker() as session:
            rows = (await session.execute(select(PolicyDocument))).scalars().all()
        assert rows == []


async def test_health_and_metrics_endpoints():
    env = await make_env()
    async with env.client() as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert b"omni_requests_total" in metrics.content
