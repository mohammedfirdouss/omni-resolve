"""Property-based tests for the API Gateway (Properties 1, 2, 6, 7, 16)."""

from __future__ import annotations

import asyncio
import datetime

from hypothesis import HealthCheck, given, settings, strategies as st

from apigw_helpers import FakeEmbeddingClient, FakePolicyStore, make_env

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

# Printable text without surrogates; min 1 char per spec bounds.
def field_text(max_size: int):
    return st.text(
        alphabet=st.characters(codec="utf-8", exclude_categories=("Cs",)),
        min_size=1,
        max_size=max_size,
    )


valid_tickets = st.fixed_dictionaries(
    {
        "customer_id": field_text(128),
        "category": field_text(64),
        "description": field_text(4000),
    }
)

valid_policies = st.fixed_dictionaries(
    {
        "title": field_text(200),
        "content": field_text(2000),  # bounded for test speed; spec max 50k
        "category": field_text(100),
    }
)


# Feature: omni-resolve, Property 1: Ticket ingestion round-trip preserves all fields
@SETTINGS
@given(payload=valid_tickets)
def test_property_1_ticket_round_trip(payload):
    async def case():
        env = await make_env()
        async with env.client() as client:
            created = await client.post("/tickets", json=payload)
            assert created.status_code == 201
            ticket_id = created.json()["ticket_id"]

            fetched = await client.get(f"/tickets/{ticket_id}")
            assert fetched.status_code == 200
            body = fetched.json()
            assert body["customer_id"] == payload["customer_id"]
            assert body["category"] == payload["category"]
            assert body["description"] == payload["description"]

    asyncio.run(case())


VIOLATIONS = {
    "missing": lambda maxlen: None,  # sentinel: remove field
    "empty": lambda maxlen: "",
    "over_length": lambda maxlen: "x" * (maxlen + 1),
    "wrong_type": lambda maxlen: 12345,
}

TICKET_LIMITS = {"customer_id": 128, "category": 64, "description": 4000}
POLICY_LIMITS = {"title": 200, "content": 50_000, "category": 100}


def corrupt(valid: dict, limits: dict, choices: dict[str, str]) -> dict:
    payload = dict(valid)
    for fname, violation in choices.items():
        if violation == "missing":
            payload.pop(fname, None)
        else:
            payload[fname] = VIOLATIONS[violation](limits[fname])
    return payload


def bad_field_choices(fields):
    return st.dictionaries(
        keys=st.sampled_from(sorted(fields)),
        values=st.sampled_from(sorted(VIOLATIONS)),
        min_size=1,
    )


# Feature: omni-resolve, Property 2: Invalid ticket payloads are always rejected with 422
@SETTINGS
@given(valid=valid_tickets, choices=bad_field_choices(TICKET_LIMITS))
def test_property_2_invalid_tickets_rejected(valid, choices):
    payload = corrupt(valid, TICKET_LIMITS, choices)

    async def case():
        env = await make_env()
        async with env.client() as client:
            response = await client.post("/tickets", json=payload)
            assert response.status_code == 422
            named = {e["field"] for e in response.json()["errors"]}
            for offending in choices:
                assert offending in named
        assert env.bus.published == []  # nothing may reach the bus

    asyncio.run(case())


# Feature: omni-resolve, Property 6: Policy document round-trip preserves metadata
@SETTINGS
@given(payload=valid_policies)
def test_property_6_policy_round_trip(payload):
    async def case():
        env = await make_env()
        async with env.client() as client:
            created = await client.post("/policies", json=payload)
            assert created.status_code == 201
            policy_id = created.json()["policy_id"]

            fetched = await client.get(f"/policies/{policy_id}")
            assert fetched.status_code == 200
            body = fetched.json()
            assert body["title"] == payload["title"]
            assert body["category"] == payload["category"]
            # well-formed ISO-8601 UTC timestamp
            parsed = datetime.datetime.fromisoformat(body["ingested_at"])
            assert parsed.utcoffset() == datetime.timedelta(0)

    asyncio.run(case())


# Feature: omni-resolve, Property 7: Invalid policy payloads are always rejected with 422
@SETTINGS
@given(valid=valid_policies, choices=bad_field_choices(POLICY_LIMITS))
def test_property_7_invalid_policies_rejected(valid, choices):
    payload = corrupt(valid, POLICY_LIMITS, choices)

    async def case():
        env = await make_env()
        async with env.client() as client:
            response = await client.post("/policies", json=payload)
            assert response.status_code == 422
            named = {e["field"] for e in response.json()["errors"]}
            for offending in choices:
                assert offending in named
        # nothing persisted to either store
        assert env.store.upserts == {}
        assert env.embedding.calls == []
        from sqlalchemy import func, select

        from shared.db import PolicyDocument

        async with env.sessionmaker() as session:
            count = (await session.execute(select(func.count(PolicyDocument.policy_id)))).scalar()
        assert count == 0

    asyncio.run(case())


# Feature: omni-resolve, Property 16: Policy embedding failure leaves no partial state
@SETTINGS
@given(payload=valid_policies)
def test_property_16_embedding_failure_atomicity(payload):
    async def case():
        env = await make_env(embedding=FakeEmbeddingClient(fail=True))
        async with env.client() as client:
            response = await client.post("/policies", json=payload)
            assert response.status_code == 502
        assert env.store.upserts == {}
        from sqlalchemy import func, select

        from shared.db import PolicyDocument

        async with env.sessionmaker() as session:
            count = (await session.execute(select(func.count(PolicyDocument.policy_id)))).scalar()
        assert count == 0

    asyncio.run(case())
