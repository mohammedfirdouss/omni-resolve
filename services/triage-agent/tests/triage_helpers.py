"""Test environment factory for Triage Agent tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import Base, create_ticket
from shared.event_bus import InMemoryEventBus
from shared.models import EventType, new_event

from triage_agent.agent import TriageAgent


def llm_transport(content: str | None = None, *, status: int = 200, fail: bool = False,
                  counter: dict | None = None) -> httpx.MockTransport:
    """MockTransport for the AI Gateway completions endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
        if fail:
            raise httpx.ConnectError("ai gateway down")
        if status != 200:
            return httpx.Response(status, json={"detail": "error"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": content or ""}}]}
        )

    return httpx.MockTransport(handler)


def plan_json(confidence: float, actions: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "actions": actions
            if actions is not None
            else [{"action_type": "process_refund", "parameters": {"amount": 10}}],
            "confidence_score": confidence,
        }
    )


@dataclass
class Env:
    agent: TriageAgent
    bus: InMemoryEventBus
    sessionmaker: Any


async def make_env(transport: httpx.MockTransport, **agent_kwargs) -> Env:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def no_sleep(_):
        return None

    bus = InMemoryEventBus()
    agent = TriageAgent(
        sessionmaker=sessionmaker,
        event_bus=bus,
        transport=transport,
        sleep=agent_kwargs.pop("sleep", no_sleep),
        db_retry_base_delay=0.0,
        **agent_kwargs,
    )
    return Env(agent=agent, bus=bus, sessionmaker=sessionmaker)


async def seed_ticket(env: Env, *, category: str = "refund",
                      description: str = "please refund") -> str:
    async with env.sessionmaker() as session:
        ticket = await create_ticket(
            session, customer_id="c-1", category=category, description=description
        )
        await session.commit()
        return ticket.ticket_id


def created_event(ticket_id: str, *, category: str = "refund",
                  description: str = "please refund"):
    return new_event(
        EventType.TICKET_CREATED.value,
        ticket_id,
        {"customer_id": "c-1", "category": category, "description": description},
    )
