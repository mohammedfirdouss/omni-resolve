"""Property tests for the Triage Agent (Properties 3 and 4)."""

from __future__ import annotations

import asyncio
import decimal
import json
import uuid

from hypothesis import HealthCheck, given, settings, strategies as st

from triage_agent.parser import clamp_confidence, parse_resolution_plan

from triage_helpers import created_event, llm_transport, make_env, plan_json, seed_ticket

SETTINGS = settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow]
)

# LLM responses: pure garbage, JSON with arbitrary confidence values, or
# JSON-ish text with a plan embedded in prose.
llm_responses = st.one_of(
    st.text(max_size=200),
    st.builds(
        lambda conf: json.dumps({"actions": [], "confidence_score": conf}),
        st.one_of(
            st.floats(allow_nan=True, allow_infinity=True),
            st.integers(min_value=-10_000, max_value=10_000),
            st.text(max_size=10),
            st.none(),
        ),
    ),
    st.builds(
        lambda conf: f"Here is my plan:\n{plan_json(conf)}\nHope that helps!",
        st.floats(min_value=-5, max_value=5, allow_nan=False),
    ),
)


# Feature: omni-resolve, Property 3: Confidence score is always in range [0.00, 1.00]
@SETTINGS
@given(response=llm_responses)
def test_property_3_confidence_always_in_range(response):
    plan = parse_resolution_plan(str(uuid.uuid4()), response)
    score = plan.confidence_score
    assert 0.0 <= score <= 1.0
    # at most two decimal places
    assert decimal.Decimal(str(score)) == decimal.Decimal(str(score)).quantize(
        decimal.Decimal("0.01")
    )


# Feature: omni-resolve, Property 3: Confidence score is always in range [0.00, 1.00]
@SETTINGS
@given(
    value=st.one_of(
        st.floats(allow_nan=True, allow_infinity=True),
        st.integers(),
        st.text(max_size=10),
        st.none(),
    )
)
def test_property_3_clamp_confidence_total(value):
    score = clamp_confidence(value)
    assert 0.0 <= score <= 1.0


# Feature: omni-resolve, Property 4: Routing is determined entirely by confidence score
@SETTINGS
@given(confidence=st.floats(min_value=0.0, max_value=1.0, allow_nan=False))
def test_property_4_routing_by_confidence(confidence):
    async def case():
        env = await make_env(llm_transport(plan_json(confidence)))
        ticket_id = await seed_ticket(env)
        await env.agent.handle_ticket_created(created_event(ticket_id))

        triaged = env.bus.events_of_type("ticket.triaged")
        escalations = env.bus.events_of_type("ticket.escalation_requested")
        effective = round(confidence, 2)  # parser rounds to 2 dp before routing

        if effective >= 0.75:
            assert len(triaged) == 1 and len(escalations) == 0
            from shared.db import Ticket

            async with env.sessionmaker() as session:
                ticket = await session.get(Ticket, ticket_id)
            assert ticket.status == "triaged"
            assert triaged[0].payload["resolution_plan"]["confidence_score"] == effective
        else:
            assert len(escalations) == 1 and len(triaged) == 0
            assert escalations[0].payload["reason"] == "low_confidence"
            assert escalations[0].payload["confidence_score"] == effective

    asyncio.run(case())
