"""Property test for similarity score range (Property 5)."""

from __future__ import annotations

import asyncio

from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import select

from shared.db import RetrievalRecord

from knowledge_agent.scores import normalize_similarity_score

from knowledge_helpers import FakeQdrant, make_env, make_point, seed_triaged_ticket, triaged_event

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

raw_scores = st.floats(allow_nan=True, allow_infinity=True, width=32)


# Feature: omni-resolve, Property 5: Policy retrieval similarity scores are in range [0.0, 1.0]
@SETTINGS
@given(score=raw_scores)
def test_property_5_normalizer_is_total(score):
    normalized = normalize_similarity_score(score)
    assert 0.0 <= normalized <= 1.0


# Feature: omni-resolve, Property 5: Policy retrieval similarity scores are in range [0.0, 1.0]
@SETTINGS
@given(scores=st.lists(raw_scores, min_size=1, max_size=5))
def test_property_5_persisted_scores_in_range(scores):
    async def case():
        points = [make_point(f"p-{i}", s) for i, s in enumerate(scores)]
        env = await make_env(FakeQdrant(points))
        ticket_id = await seed_triaged_ticket(env)

        await env.agent.handle_event(triaged_event(ticket_id))

        async with env.sessionmaker() as session:
            records = (
                (await session.execute(
                    select(RetrievalRecord).where(RetrievalRecord.ticket_id == ticket_id)
                )).scalars().all()
            )
        assert len(records) == len(scores)
        for record in records:
            assert 0.0 <= float(record.similarity_score) <= 1.0
        await env.dispose()

    asyncio.run(case())
