"""Test environment factory for Escalation Agent tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import Base, create_ticket
from shared.event_bus import InMemoryEventBus
from shared.models import EventType, new_event

from escalation_agent.agent import EscalationAgent
from escalation_agent.human_queue import InMemoryHumanQueue


@dataclass
class Env:
    agent: EscalationAgent
    bus: InMemoryEventBus
    queue: InMemoryHumanQueue
    sessionmaker: Any
    engine: Any

    async def dispose(self) -> None:
        await self.engine.dispose()


async def make_env(**agent_kwargs) -> Env:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    bus = InMemoryEventBus()
    queue = InMemoryHumanQueue()
    agent = EscalationAgent(
        bus=bus,
        human_queue=queue,
        sessionmaker=sessionmaker,
        write_retry_base_delay=0.0,
        **agent_kwargs,
    )
    return Env(agent=agent, bus=bus, queue=queue, sessionmaker=sessionmaker, engine=engine)


async def seed_pending_ticket(env: Env) -> str:
    async with env.sessionmaker() as session:
        ticket = await create_ticket(
            session, customer_id="c-1", category="refund", description="d"
        )
        await session.commit()
        return ticket.ticket_id


def escalation_event(ticket_id: str, *, reason: str = "low_confidence",
                     confidence: float | None = 0.4):
    payload: dict[str, Any] = {"reason": reason}
    if confidence is not None:
        payload["confidence_score"] = confidence
        payload["resolution_plan"] = {
            "ticket_id": ticket_id,
            "actions": [],
            "confidence_score": confidence,
        }
    return new_event(EventType.TICKET_ESCALATION_REQUESTED.value, ticket_id, payload)
