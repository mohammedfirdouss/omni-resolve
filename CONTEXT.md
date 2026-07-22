# OmniResolve

Autonomous Tier-1 customer support platform: eight microservices coordinating over an event bus to triage, investigate, and resolve or escalate support tickets.

## Language

**Outcome Router**:
The `shared/routing.py` functions (`route_triage_outcome`, `route_execution_outcome`, `route_retrieval_outcome`) are the single place each agent's pipeline result is translated into the canonical `Event` it publishes. Agents build an outcome value describing what happened; the router decides which `EventType` that becomes and what payload it carries.

**State Store Repository**:
The sole module (`shared/db.py`) permitted to hold a `Session` or reference an ORM class. Agents call `record_decision`, `record_action`, `record_retrieval`, `record_overflow`, and `transition_state` with plain domain values; the repository owns session lifecycle, write retry/backoff, and the `system.state_write_failed` alert hook internally. No agent constructs an ORM row or opens a `Session` directly.
_Avoid_: direct ORM instantiation in agent code.

**TestEnv**:
The shared SQLite/in-memory-bus test substrate (`tests/support/env.py`) every service's own `Env` test fixture wraps. Test-only; never imported by production code.

**Instrumented Call**:
The async context-manager seam in `shared/observability.py` that pairs one `ServiceMetrics.observe_request` call and one `TraceStore.record_trace` call under a single timer and `trace_id`. Agents supply `status`/`kind`/`data` per outcome branch via `call.record(...)`; the wrapper owns only the mechanical timing/recording, never the business semantics of what's being recorded.
