"""Async SQLAlchemy 2.x state store for OmniResolve.

Provides the ORM table definitions, an engine/session factory, a shared
write-retry decorator (3 attempts, 5 s exponential backoff), state-machine
validated transition recording, and the ``system.state_write_failed`` alert
hook.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    select,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import DateTime, JSON, TypeDecorator

from shared.models import (
    TERMINAL_STATES,
    EventType,
    TicketStatus,
    is_valid_transition,
    new_event,
)

logger = logging.getLogger("omniresolve.db")

DEFAULT_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://omni:omni@localhost:5432/omni"
)

WRITE_RETRY_ATTEMPTS = 3
WRITE_RETRY_BASE_DELAY = 5.0  # seconds, exponential: 5, 10, 20


class InvalidStateTransition(ValueError):
    pass


class StateWriteFailed(RuntimeError):
    """All write retries exhausted; the in-flight event must be nacked."""


# ---------------------------------------------------------------------------
# ORM definitions
# ---------------------------------------------------------------------------


class UTCDateTime(TypeDecorator):
    """Timezone-aware DateTime that also works on SQLite in tests."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


class Base(DeclarativeBase):
    type_annotation_map = {
        dict[str, Any]: JSON().with_variant(sqlite.JSON(), "sqlite"),
        datetime: UTCDateTime(),
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TicketStatus.PENDING.value
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    total_elapsed_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)


class TicketStateTransition(Base):
    __tablename__ = "ticket_state_transitions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.ticket_id"), nullable=False, index=True
    )
    previous_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_state: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_by: Mapped[str] = mapped_column(String(64), nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=_utcnow
    )


class AgentDecision(Base):
    __tablename__ = "agent_decisions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.ticket_id"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(64), nullable=False)
    input_summary: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    output_summary: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    confidence_score: Mapped[float | None] = mapped_column(Numeric(4, 2), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class ExecutionAction(Base):
    __tablename__ = "execution_actions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.ticket_id"), nullable=False, index=True
    )
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_body: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    invoked_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class PolicyDocument(Base):
    __tablename__ = "policy_documents"

    policy_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class RetrievalRecord(Base):
    __tablename__ = "retrieval_records"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ticket_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tickets.ticket_id"), nullable=False, index=True
    )
    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("policy_documents.policy_id"), nullable=False
    )
    similarity_score: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


class EscalationOverflow(Base):
    """Secondary overflow table used when the escalation retry buffer is full."""

    __tablename__ = "escalation_overflow"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    ticket_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=TicketStatus.ESCALATION_PENDING.value
    )
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=_utcnow)


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------


def create_engine_and_sessionmaker(
    url: str | None = None,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(url or DEFAULT_DATABASE_URL, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Write retry decorator + alert hook
# ---------------------------------------------------------------------------

AlertHook = Callable[[dict[str, Any]], Awaitable[None] | None]

_state_write_failed_hooks: list[AlertHook] = []


def register_state_write_failed_hook(hook: AlertHook) -> None:
    _state_write_failed_hooks.append(hook)


async def _emit_state_write_failed(ticket_id: str, attempted_state: str) -> None:
    event = new_event(
        EventType.SYSTEM_STATE_WRITE_FAILED.value,
        ticket_id,
        {"ticket_id": ticket_id, "attempted_state": attempted_state},
    ).model_dump()
    logger.error("system.state_write_failed: %s -> %s", ticket_id, attempted_state)
    for hook in _state_write_failed_hooks:
        result = hook(event)
        if asyncio.iscoroutine(result):
            await result


def with_write_retry(
    attempts: int = WRITE_RETRY_ATTEMPTS, base_delay: float = WRITE_RETRY_BASE_DELAY
):
    """Retry a State_Store write up to ``attempts`` times with exponential backoff.

    On exhaustion emits ``system.state_write_failed`` (when ticket_id /
    attempted_state kwargs are present) and raises StateWriteFailed so the
    caller can nack the in-flight event for redelivery.
    """

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = base_delay
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except (InvalidStateTransition, StateWriteFailed):
                    raise
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "state store write failed (attempt %d/%d): %s", attempt, attempts, exc
                    )
                    if attempt < attempts:
                        await asyncio.sleep(delay)
                        delay *= 2
            ticket_id = kwargs.get("ticket_id", "unknown")
            attempted_state = kwargs.get("new_state", kwargs.get("attempted_state", "unknown"))
            await _emit_state_write_failed(str(ticket_id), str(attempted_state))
            raise StateWriteFailed(str(last_exc)) from last_exc

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# State transition recording (Requirement 8)
# ---------------------------------------------------------------------------


async def record_state_transition(
    session: AsyncSession,
    *,
    ticket_id: str,
    new_state: str,
    triggered_by: str,
) -> TicketStateTransition:
    """Append a validated state transition and update the ticket row.

    Enforces the status state machine, stamps terminal states with
    ``total_elapsed_seconds``, and appends to ``ticket_state_transitions``.
    Callers own the transaction (commit/rollback).
    """
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise InvalidStateTransition(f"unknown ticket {ticket_id}")

    previous = ticket.status
    if not is_valid_transition(previous, new_state):
        raise InvalidStateTransition(f"illegal transition {previous!r} -> {new_state!r}")

    now = _utcnow()
    ticket.status = new_state
    if new_state in TERMINAL_STATES:
        ticket.resolved_at = now
        created = ticket.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        ticket.total_elapsed_seconds = round((now - created).total_seconds(), 3)

    transition = TicketStateTransition(
        ticket_id=ticket_id,
        previous_state=previous,
        new_state=new_state,
        triggered_by=triggered_by,
        transitioned_at=now,
    )
    session.add(transition)
    return transition


async def create_ticket(
    session: AsyncSession,
    *,
    customer_id: str,
    category: str,
    description: str,
    triggered_by: str = "api-gateway",
) -> Ticket:
    """Insert a new pending ticket plus its initial state transition row."""
    ticket = Ticket(
        ticket_id=str(uuid.uuid4()),
        customer_id=customer_id,
        category=category,
        description=description,
        status=TicketStatus.PENDING.value,
    )
    session.add(ticket)
    session.add(
        TicketStateTransition(
            ticket_id=ticket.ticket_id,
            previous_state=None,
            new_state=TicketStatus.PENDING.value,
            triggered_by=triggered_by,
        )
    )
    return ticket


async def get_ticket_history(session: AsyncSession, ticket_id: str) -> dict[str, Any] | None:
    """Full ticket state history: transitions + agent decisions + actions."""
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        return None

    transitions = (
        (
            await session.execute(
                select(TicketStateTransition)
                .where(TicketStateTransition.ticket_id == ticket_id)
                .order_by(TicketStateTransition.id)
            )
        )
        .scalars()
        .all()
    )
    decisions = (
        (
            await session.execute(
                select(AgentDecision)
                .where(AgentDecision.ticket_id == ticket_id)
                .order_by(AgentDecision.id)
            )
        )
        .scalars()
        .all()
    )
    actions = (
        (
            await session.execute(
                select(ExecutionAction)
                .where(ExecutionAction.ticket_id == ticket_id)
                .order_by(ExecutionAction.id)
            )
        )
        .scalars()
        .all()
    )

    return {
        "ticket_id": ticket.ticket_id,
        "customer_id": ticket.customer_id,
        "category": ticket.category,
        "description": ticket.description,
        "status": ticket.status,
        "created_at": ticket.created_at.isoformat(),
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "total_elapsed_seconds": (
            float(ticket.total_elapsed_seconds)
            if ticket.total_elapsed_seconds is not None
            else None
        ),
        "state_transitions": [
            {
                "previous_state": t.previous_state,
                "new_state": t.new_state,
                "triggered_by": t.triggered_by,
                "transitioned_at": t.transitioned_at.isoformat(),
            }
            for t in transitions
        ],
        "agent_decisions": [
            {
                "agent": d.agent,
                "decision_type": d.decision_type,
                "input_summary": d.input_summary,
                "output_summary": d.output_summary,
                "confidence_score": (
                    float(d.confidence_score) if d.confidence_score is not None else None
                ),
                "recorded_at": d.recorded_at.isoformat(),
            }
            for d in decisions
        ],
        "execution_actions": [
            {
                "action_type": a.action_type,
                "request_body": a.request_body,
                "response_status": a.response_status,
                "response_body": a.response_body,
                "invoked_at": a.invoked_at.isoformat(),
            }
            for a in actions
        ],
    }
