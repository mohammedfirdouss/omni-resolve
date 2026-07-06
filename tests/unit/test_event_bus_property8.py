"""Property 8 — event schema conformance is total (task 4.2, Requirement 7.4).

Generates a mix of conforming and non-conforming raw objects (missing fields,
wrong types, non-ISO / naive timestamps, non-dict payloads, non-dict roots)
and asserts ``shared.models.validate_event`` accepts an object *iff* all four
required envelope fields are present with the correct types and an ISO-8601
timestamp carrying a UTC offset — everything else raises EventValidationError.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shared.models import (
    EVENT_PAYLOAD_MODELS,
    EventValidationError,
    new_event,
    validate_event,
)

# ---------------------------------------------------------------------------
# Reference oracle: independent re-statement of the canonical-envelope rules.
# ---------------------------------------------------------------------------


def _timestamp_ok(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None  # naive timestamps are non-conforming


def conforms(raw: object) -> bool:
    """True iff `raw` satisfies the canonical event schema (incl. typed payloads)."""
    if not isinstance(raw, dict):
        return False
    if not isinstance(raw.get("event_type"), str) or isinstance(raw.get("event_type"), bool):
        return False
    if not isinstance(raw.get("ticket_id"), str) or isinstance(raw.get("ticket_id"), bool):
        return False
    if not _timestamp_ok(raw.get("timestamp")):
        return False
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not all(isinstance(k, str) for k in payload):
        return False
    payload_model = EVENT_PAYLOAD_MODELS.get(raw["event_type"])
    if payload_model is not None:
        try:
            payload_model.model_validate(payload)
        except Exception:
            return False
    return True


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False),
    st.text(max_size=20),
)

STR_KEY_DICTS = st.dictionaries(st.text(max_size=10), SCALARS, max_size=4)

_DATETIMES = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2035, 12, 31)
)

UTC_TIMESTAMPS = st.one_of(
    _DATETIMES.map(lambda d: d.replace(tzinfo=timezone.utc).isoformat()),
    _DATETIMES.map(lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")),
)

TIMESTAMP_CANDIDATES = st.one_of(
    UTC_TIMESTAMPS,
    _DATETIMES.map(lambda d: d.isoformat()),  # naive → non-conforming
    st.text(max_size=25),  # almost always non-ISO garbage
    SCALARS,  # wrong types
)

EVENT_TYPE_CANDIDATES = st.one_of(
    st.text(max_size=24),
    st.sampled_from(sorted(EVENT_PAYLOAD_MODELS)),  # known types → payload must conform too
    SCALARS,
)

PAYLOAD_CANDIDATES = st.one_of(
    STR_KEY_DICTS,
    SCALARS,  # non-dict payloads → non-conforming
    st.lists(SCALARS, max_size=3),
)

# Arbitrary dicts where every field may be missing, well-typed, or mistyped.
ARBITRARY_EVENT_DICTS = st.fixed_dictionaries(
    {},
    optional={
        "event_type": EVENT_TYPE_CANDIDATES,
        "ticket_id": st.one_of(st.text(max_size=36), SCALARS),
        "timestamp": TIMESTAMP_CANDIDATES,
        "payload": PAYLOAD_CANDIDATES,
        "extra_field": SCALARS,  # extras are ignored by the envelope
    },
)


@st.composite
def conforming_events(draw) -> dict:
    """Events guaranteed to satisfy the canonical schema."""
    event_type = draw(
        st.one_of(
            st.sampled_from(["ticket.created", "system.escalation_buffer_full"]),
            st.text(min_size=1, max_size=20).filter(lambda s: s not in EVENT_PAYLOAD_MODELS),
        )
    )
    if event_type == "ticket.created":
        payload = {
            "customer_id": draw(st.text(min_size=1, max_size=12)),
            "category": draw(st.text(min_size=1, max_size=12)),
            "description": draw(st.text(min_size=1, max_size=30)),
        }
    elif event_type == "system.escalation_buffer_full":
        payload = {"buffer_size": draw(st.integers(min_value=0, max_value=100_000))}
    else:
        payload = draw(STR_KEY_DICTS)
    return {
        "event_type": event_type,
        "ticket_id": draw(st.text(min_size=1, max_size=36)),
        "timestamp": draw(UTC_TIMESTAMPS),
        "payload": payload,
    }


NON_DICT_ROOTS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.text(max_size=30),
    st.lists(SCALARS, max_size=3),
)

RAW_CANDIDATES = st.one_of(conforming_events(), ARBITRARY_EVENT_DICTS, NON_DICT_ROOTS)


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


# Feature: omni-resolve, Property 8: Event schema conformance is total
@settings(max_examples=100)
@given(raw=RAW_CANDIDATES)
def test_event_schema_conformance_is_total(raw):
    if conforms(raw):
        event = validate_event(raw)
        assert event.event_type == raw["event_type"]
        assert event.ticket_id == raw["ticket_id"]
        assert event.timestamp == raw["timestamp"]
        assert event.payload == raw["payload"]
    else:
        with pytest.raises(EventValidationError):
            validate_event(raw)


def test_new_event_helper_always_produces_conforming_events():
    event = new_event("ticket.resolved", "t-1", {"actions_completed": ["process_refund"]})
    assert validate_event(event.model_dump()).event_type == "ticket.resolved"
