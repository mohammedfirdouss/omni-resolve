"""Test environment factory for Execution Agent tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from shared.db import Base, create_ticket, record_state_transition
from shared.event_bus import InMemoryEventBus
from shared.models import EventType, new_event

from execution_agent.agent import ExecutionAgent


@dataclass
class ActionServer:
    """Scripted fake for the external action API.

    ``script`` maps an invocation index (0-based, in call order) to a
    behaviour: an int HTTP status, "timeout", or "error". Unlisted
    invocations return 200.
    """

    script: dict[int, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        async def handler(request: httpx.Request) -> httpx.Response:
            index = len(self.calls)
            action_type = request.url.path.rsplit("/", 1)[-1]
            self.calls.append(action_type)
            behaviour = self.script.get(index, 200)
            if behaviour == "timeout":
                raise httpx.ReadTimeout("simulated timeout")
            if behaviour == "error":
                raise httpx.ConnectError("simulated connection error")
            return httpx.Response(behaviour, json={"ok": behaviour < 400, "action": action_type})

        return httpx.MockTransport(handler)


@dataclass
class Env:
    agent: ExecutionAgent
    bus: InMemoryEventBus
    sessionmaker: Any
    server: ActionServer
    engine: Any

    async def dispose(self) -> None:
        await self.engine.dispose()


async def make_env(server: ActionServer | None = None, **agent_kwargs) -> Env:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    bus = InMemoryEventBus()
    server = server or ActionServer()
    agent = ExecutionAgent(
        sessionmaker=sessionmaker,
        event_bus=bus,
        transport=server.transport(),
        db_retry_base_delay=0.0,
        **agent_kwargs,
    )
    return Env(agent=agent, bus=bus, sessionmaker=sessionmaker, server=server, engine=engine)


async def seed_triaged_ticket(env: Env) -> str:
    async with env.sessionmaker() as session:
        ticket = await create_ticket(
            session, customer_id="c-1", category="refund", description="d"
        )
        await session.flush()
        await record_state_transition(
            session, ticket_id=ticket.ticket_id, new_state="triaged", triggered_by="test"
        )
        await session.commit()
        return ticket.ticket_id


def context_ready_event(ticket_id: str, action_types: list[str]):
    return new_event(
        EventType.TICKET_CONTEXT_READY.value,
        ticket_id,
        {
            "resolution_plan": {
                "ticket_id": ticket_id,
                "actions": [
                    {"action_type": a, "parameters": {"seq": i}}
                    for i, a in enumerate(action_types)
                ],
                "confidence_score": 0.9,
            },
            "policy_document_ids": ["p-1"],
        },
    )
