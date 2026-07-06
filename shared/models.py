"""Canonical Pydantic v2 models shared by every OmniResolve microservice.

Covers: API request/response bodies, the canonical event envelope and every
event payload, the Resolution_Plan object, and the shared 422 error body.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

# ---------------------------------------------------------------------------
# Ticket status state machine
# ---------------------------------------------------------------------------


class TicketStatus(str, Enum):
    PENDING = "pending"
    TRIAGED = "triaged"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    EXECUTION_FAILED = "execution_failed"
    ESCALATION_PENDING = "escalation_pending"


# Valid transitions: pending -> triaged -> resolved | escalated
#                    triaged -> execution_failed -> escalated
#                    pending -> escalated (triage failure / low confidence)
VALID_TRANSITIONS: dict[str | None, set[str]] = {
    None: {TicketStatus.PENDING.value},
    TicketStatus.PENDING.value: {
        TicketStatus.TRIAGED.value,
        TicketStatus.ESCALATED.value,
    },
    TicketStatus.TRIAGED.value: {
        TicketStatus.RESOLVED.value,
        TicketStatus.ESCALATED.value,
        TicketStatus.EXECUTION_FAILED.value,
    },
    TicketStatus.EXECUTION_FAILED.value: {TicketStatus.ESCALATED.value},
}

TERMINAL_STATES = {TicketStatus.RESOLVED.value, TicketStatus.ESCALATED.value}


def is_valid_transition(previous: str | None, new: str) -> bool:
    return new in VALID_TRANSITIONS.get(previous, set())


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------


class TicketCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    customer_id: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=4000)


class TicketCreateResponse(BaseModel):
    ticket_id: str


class PolicyCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=50_000)
    category: str = Field(min_length=1, max_length=100)
    policy_id: str | None = Field(default=None, description="Optional existing id for upsert")

    @field_validator("policy_id")
    @classmethod
    def _validate_policy_id(cls, v: str | None) -> str | None:
        if v is not None:
            uuid.UUID(v)  # raises ValueError -> 422
        return v


class PolicyCreateResponse(BaseModel):
    policy_id: str


class PolicyGetResponse(BaseModel):
    policy_id: str
    title: str
    category: str
    ingested_at: str  # ISO-8601 UTC


class FieldError(BaseModel):
    field: str
    violation: str


class ValidationErrorResponse(BaseModel):
    errors: list[FieldError]


# ---------------------------------------------------------------------------
# Resolution plan
# ---------------------------------------------------------------------------

ActionType = Literal["process_refund", "track_order", "adjust_billing", "send_notification"]

ACTION_TYPES: tuple[str, ...] = (
    "process_refund",
    "track_order",
    "adjust_billing",
    "send_notification",
)


class PlannedAction(BaseModel):
    action_type: ActionType
    parameters: dict[str, Any] = Field(default_factory=dict)


class ResolutionPlan(BaseModel):
    ticket_id: str
    actions: list[PlannedAction]
    confidence_score: float = Field(ge=0.0, le=1.0)

    @field_validator("confidence_score")
    @classmethod
    def _round_two_places(cls, v: float) -> float:
        return float(round(Decimal(str(v)), 2))


CONFIDENCE_THRESHOLD = 0.75


# ---------------------------------------------------------------------------
# Canonical event schema
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    TICKET_CREATED = "ticket.created"
    TICKET_TRIAGED = "ticket.triaged"
    TICKET_CONTEXT_READY = "ticket.context_ready"
    TICKET_RESOLVED = "ticket.resolved"
    TICKET_ESCALATION_REQUESTED = "ticket.escalation_requested"
    TICKET_ESCALATED = "ticket.escalated"
    SYSTEM_INVALID_EVENT = "system.invalid_event"
    SYSTEM_EVENT_DEAD_LETTERED = "system.event_dead_lettered"
    SYSTEM_ESCALATION_BUFFER_FULL = "system.escalation_buffer_full"
    SYSTEM_STATE_WRITE_FAILED = "system.state_write_failed"


class Event(BaseModel):
    """Canonical envelope: every event on the bus must conform to this."""

    model_config = ConfigDict(extra="ignore", strict=True)

    event_type: str
    ticket_id: str
    timestamp: str
    payload: dict[str, Any]

    @field_validator("timestamp")
    @classmethod
    def _validate_iso8601_utc(cls, v: str) -> str:
        parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timestamp must carry a UTC offset")
        return v


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event(event_type: str, ticket_id: str, payload: dict[str, Any]) -> Event:
    return Event(
        event_type=event_type,
        ticket_id=ticket_id,
        timestamp=utcnow_iso(),
        payload=payload,
    )


# --- Typed event payloads ---------------------------------------------------


class TicketCreatedPayload(BaseModel):
    customer_id: str
    category: str
    description: str


class TicketTriagedPayload(BaseModel):
    resolution_plan: ResolutionPlan


class TicketContextReadyPayload(BaseModel):
    resolution_plan: ResolutionPlan
    policy_document_ids: list[str]


class TicketResolvedPayload(BaseModel):
    actions_completed: list[str]


class EscalationRequestedPayload(BaseModel):
    reason: str
    confidence_score: float | None = None
    resolution_plan: ResolutionPlan | None = None


class TicketEscalatedPayload(BaseModel):
    queue_entry_id: str


class InvalidEventPayload(BaseModel):
    raw_payload: Any
    violation_description: str


class EventDeadLetteredPayload(BaseModel):
    original_event: dict[str, Any]
    attempt_count: int


class EscalationBufferFullPayload(BaseModel):
    buffer_size: int


class StateWriteFailedPayload(BaseModel):
    ticket_id: str
    attempted_state: str


EVENT_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    EventType.TICKET_CREATED.value: TicketCreatedPayload,
    EventType.TICKET_TRIAGED.value: TicketTriagedPayload,
    EventType.TICKET_CONTEXT_READY.value: TicketContextReadyPayload,
    EventType.TICKET_RESOLVED.value: TicketResolvedPayload,
    EventType.TICKET_ESCALATION_REQUESTED.value: EscalationRequestedPayload,
    EventType.TICKET_ESCALATED.value: TicketEscalatedPayload,
    EventType.SYSTEM_INVALID_EVENT.value: InvalidEventPayload,
    EventType.SYSTEM_EVENT_DEAD_LETTERED.value: EventDeadLetteredPayload,
    EventType.SYSTEM_ESCALATION_BUFFER_FULL.value: EscalationBufferFullPayload,
    EventType.SYSTEM_STATE_WRITE_FAILED.value: StateWriteFailedPayload,
}


class EventValidationError(ValueError):
    """Raised when an inbound or outbound event violates the canonical schema."""

    def __init__(self, violation: str, raw: Any = None) -> None:
        super().__init__(violation)
        self.violation = violation
        self.raw = raw


def validate_event(raw: Any) -> Event:
    """Validate an arbitrary object against the canonical event schema.

    Raises EventValidationError with a human-readable violation description.
    """
    if not isinstance(raw, dict):
        raise EventValidationError("event must be a JSON object", raw)
    try:
        event = Event.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise EventValidationError(str(exc), raw) from exc

    payload_model = EVENT_PAYLOAD_MODELS.get(event.event_type)
    if payload_model is not None:
        try:
            payload_model.model_validate(event.payload)
        except Exception as exc:
            raise EventValidationError(
                f"payload does not conform to schema for {event.event_type}: {exc}", raw
            ) from exc
    return event
