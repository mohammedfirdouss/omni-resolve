# Design Document: OmniResolve

## Overview

OmniResolve is a cloud-agnostic, event-driven microservices platform for autonomous Tier-1 customer support resolution. It receives support tickets via HTTP, routes them through a pipeline of LangGraph-based agents (Triage → Knowledge → Execution), and either resolves tickets autonomously or escalates them to a human queue. All inter-service communication is asynchronous via RabbitMQ; persistent state lives in PostgreSQL; semantic policy retrieval uses Qdrant; and all LLM calls are abstracted through a LiteLLM AI Gateway.

### Key Design Goals

- **Autonomy**: Resolve the majority of Tier-1 tickets without human intervention, using a confidence threshold to gate escalations.
- **Auditability**: Every state transition, agent decision, and action invocation is persisted to an immutable audit log.
- **Portability**: No cloud-provider primitives; all infrastructure is open-source and containerized, deployable via Helm + Terraform.
- **Observability**: Full-stack metrics, logs, and AI-specific traces for production operations.

---

## Architecture

The platform is composed of eight independently deployable microservices plus shared infrastructure. They communicate asynchronously over the Event_Bus and share read/write access to the State_Store through their own service-layer connections (never direct cross-service database access).

```mermaid
graph TD
    Client["HTTP Client"] -->|POST /tickets\nPOST /policies\nGET /tickets/:id| APIGW["API Gateway\n(FastAPI)"]

    APIGW -->|writes| SS["State Store\n(PostgreSQL)"]
    APIGW -->|publishes ticket.created| EB["Event Bus\n(RabbitMQ)"]

    EB -->|ticket.created| TA["Triage Agent\n(LangGraph)"]
    TA -->|LLM calls| AIGW["AI Gateway\n(LiteLLM)"]
    AIGW -->|OpenAI / Anthropic / Ollama| LLM["LLM Providers"]
    TA -->|ticket.triaged / ticket.escalation_requested| EB
    TA -->|writes| SS

    EB -->|ticket.triaged| KA["Knowledge Agent\n(LangGraph)"]
    KA -->|semantic search| VS["Vector Store\n(Qdrant)"]
    KA -->|LLM calls (optional)| AIGW
    KA -->|ticket.context_ready / ticket.escalation_requested| EB
    KA -->|writes| SS

    EB -->|ticket.context_ready| EA["Execution Agent\n(LangGraph)"]
    EA -->|REST action calls| ExtAPI["External REST APIs"]
    EA -->|ticket.resolved / ticket.escalation_requested| EB
    EA -->|writes| SS

    EB -->|ticket.escalation_requested| ESC["Escalation Agent"]
    ESC -->|enqueue| HQ["Human Agent Queue\n(RabbitMQ queue)"]
    ESC -->|ticket.escalated| EB
    ESC -->|writes| SS

    EB -->|ticket.resolved / ticket.escalated| NS["Notification Service\n(optional)"]

    PI["Policy Ingestion\n(embedded in API Gateway)"] -->|embed + upsert| VS
    PI -->|writes| SS

    OBS["Observability Stack\n(Prometheus + Grafana + Langfuse)"]
    APIGW & TA & KA & EA & ESC & AIGW -->|metrics /metrics| OBS
    AIGW -->|traces| OBS
```

### Request Flow (Happy Path)

1. Client POSTs a ticket → API Gateway validates, persists to State_Store (`pending`), publishes `ticket.created`.
2. Triage Agent consumes `ticket.created` → calls AI Gateway → produces Resolution_Plan → if confidence ≥ 0.75 publishes `ticket.triaged`, updates status to `triaged`.
3. Knowledge Agent consumes `ticket.triaged` → queries Qdrant → attaches top-5 Policy_Documents → publishes `ticket.context_ready`.
4. Execution Agent consumes `ticket.context_ready` → invokes REST actions in order → on success publishes `ticket.resolved`, updates status to `resolved`.

### Escalation Flow

At any stage, a `ticket.escalation_requested` event is emitted (low confidence, retrieval failure, action failure, etc.). The Escalation Agent consumes this event, enqueues the ticket into the human agent queue, and publishes `ticket.escalated`.

---

## Components and Interfaces

### 1. API Gateway (`api-gateway`)

- **Runtime**: Python 3.12 / FastAPI + Uvicorn
- **Endpoints**:
  - `POST /tickets` — ingest ticket
  - `GET /tickets/{ticket_id}` — retrieve full ticket history
  - `POST /policies` — ingest policy document
  - `GET /policies/{policy_id}` — retrieve policy metadata
  - `GET /metrics` — Prometheus exposition
  - `GET /health` — liveness/readiness probe
- **Responsibilities**: Input validation (Pydantic v2 models), UUID v4 generation, State_Store writes, Event_Bus publish, error response shaping.
- **SLA**: ≤ 500 ms from receipt to `ticket.created` published; 100 req/s sustained throughput.

### 2. Triage Agent (`triage-agent`)

- **Runtime**: Python 3.12 / LangGraph + LangChain
- **Trigger**: Consumes `ticket.created` from Event_Bus.
- **Responsibilities**: Construct LLM prompt from ticket fields, call AI_Gateway, parse Resolution_Plan, compute Confidence_Score, route to `ticket.triaged` or `ticket.escalation_requested`, persist audit records.
- **Retry**: Up to 3 retries on AI_Gateway errors with exponential backoff (1 s, 2 s, 4 s).
- **SLA**: ≤ 10 s per ticket.

### 3. Knowledge Agent (`knowledge-agent`)

- **Runtime**: Python 3.12 / LangGraph
- **Trigger**: Consumes `ticket.triaged` from Event_Bus.
- **Responsibilities**: Embed ticket description, query Qdrant for top-5 nearest policy vectors, attach results to event payload, escalate on timeout/empty/unavailable.
- **SLA**: ≤ 2 s for Vector_Store query.

### 4. Execution Agent (`execution-agent`)

- **Runtime**: Python 3.12 / LangGraph
- **Trigger**: Consumes `ticket.context_ready` from Event_Bus.
- **Responsibilities**: Sequentially invoke REST actions (`process_refund`, `track_order`, `adjust_billing`, `send_notification`), record each HTTP request/response, update status to `resolved` or `execution_failed`.
- **Per-action timeout**: 10 s.
- **Total timeout**: 30 s.

### 5. Escalation Agent (`escalation-agent`)

- **Runtime**: Python 3.12
- **Trigger**: Consumes `ticket.escalation_requested` from Event_Bus.
- **Responsibilities**: Idempotent enqueue to human agent queue (keyed on `ticket_id`), local retry buffer (≤ 10,000 entries, 30 s interval), emit `system.escalation_buffer_full` alert when buffer full.
- **SLA**: Status update ≤ 2 s.

### 6. AI Gateway (`ai-gateway`)

- **Runtime**: Python 3.12 / LiteLLM Proxy
- **Responsibilities**: Single endpoint for all LLM inference, config-based provider selection (OpenAI / Anthropic / Ollama), rate-limit retry (up to 60 s), metrics emission, `/health` provider probe.
- **Configuration**: `LITELLM_MODEL`, `LITELLM_PROVIDER`, `OPENAI_API_KEY`, etc. — no restart required for model swap.

### 7. Policy Ingestion (embedded in API Gateway)

- **Responsibilities**: Receive POST /policies, call embedding model via AI_Gateway, atomically upsert vector + metadata in Qdrant, persist record in State_Store.
- **SLA**: ≤ 10 s from submission to persisted.

### 8. Event Bus (`rabbitmq`)

- **Runtime**: RabbitMQ 3.12 with quorum queues enabled.
- **Exchange design**: One topic exchange (`omni.events`) with per-service queues bound by routing keys.
- **Dead-letter queue**: `omni.dlq` — events moved here after 5 failed acknowledgement cycles.
- **Ordering**: Per-`ticket_id` ordering enforced via consistent hash exchange plugin routing by `ticket_id`.

### 9. State Store (`postgresql`)

- **Runtime**: PostgreSQL 16.
- **Schema**: See Data Models section.
- **Retry on write failure**: 3 attempts with 5 s exponential backoff.

### 10. Vector Store (`qdrant`)

- **Runtime**: Qdrant 1.9.
- **Collection**: `policies` — vectors of the embedding model's output dimension, distance metric: cosine.

---

## Data Models

### PostgreSQL Schema

#### `tickets`

| Column | Type | Constraints |
|---|---|---|
| `ticket_id` | UUID | PK |
| `customer_id` | VARCHAR(128) | NOT NULL |
| `category` | VARCHAR(64) | NOT NULL |
| `description` | TEXT | NOT NULL, max 4000 chars enforced at app layer |
| `status` | VARCHAR(32) | NOT NULL, default `'pending'` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default NOW() |
| `resolved_at` | TIMESTAMPTZ | nullable |
| `total_elapsed_seconds` | NUMERIC(10,3) | nullable |

Status state machine: `pending` → `triaged` → `resolved` | `escalated`; also `triaged` → `execution_failed` → `escalated`.

#### `ticket_state_transitions`

| Column | Type | Constraints |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `ticket_id` | UUID | FK → tickets |
| `previous_state` | VARCHAR(32) | nullable (NULL for initial `pending`) |
| `new_state` | VARCHAR(32) | NOT NULL |
| `triggered_by` | VARCHAR(64) | NOT NULL (agent identifier) |
| `transitioned_at` | TIMESTAMPTZ | NOT NULL, default NOW() |

#### `agent_decisions`

| Column | Type | Constraints |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `ticket_id` | UUID | FK → tickets |
| `agent` | VARCHAR(64) | NOT NULL |
| `decision_type` | VARCHAR(64) | NOT NULL |
| `input_summary` | JSONB | NOT NULL |
| `output_summary` | JSONB | NOT NULL |
| `confidence_score` | NUMERIC(4,2) | nullable |
| `recorded_at` | TIMESTAMPTZ | NOT NULL, default NOW() |

#### `execution_actions`

| Column | Type | Constraints |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `ticket_id` | UUID | FK → tickets |
| `action_type` | VARCHAR(64) | NOT NULL |
| `request_body` | JSONB | NOT NULL |
| `response_status` | INTEGER | NOT NULL |
| `response_body` | JSONB | NOT NULL |
| `invoked_at` | TIMESTAMPTZ | NOT NULL |

#### `policy_documents`

| Column | Type | Constraints |
|---|---|---|
| `policy_id` | UUID | PK |
| `title` | VARCHAR(200) | NOT NULL |
| `category` | VARCHAR(100) | NOT NULL |
| `ingested_at` | TIMESTAMPTZ | NOT NULL, default NOW() |

*(Content and vector are stored in Qdrant; only metadata lives in PostgreSQL.)*

#### `retrieval_records`

| Column | Type | Constraints |
|---|---|---|
| `id` | BIGSERIAL | PK |
| `ticket_id` | UUID | FK → tickets |
| `policy_id` | UUID | FK → policy_documents |
| `similarity_score` | NUMERIC(5,4) | NOT NULL |
| `retrieved_at` | TIMESTAMPTZ | NOT NULL, default NOW() |

### Canonical Event Schema

All events published to the Event_Bus conform to:

```json
{
  "event_type": "string",
  "ticket_id":  "string (UUID v4)",
  "timestamp":  "string (ISO-8601 UTC)",
  "payload":    "object"
}
```

**Event types and their payloads**:

| Event | Payload fields |
|---|---|
| `ticket.created` | `customer_id`, `category`, `description` |
| `ticket.triaged` | `resolution_plan` (full object) |
| `ticket.context_ready` | `resolution_plan`, `policy_document_ids[]` |
| `ticket.resolved` | `actions_completed[]` |
| `ticket.escalation_requested` | `reason`, `confidence_score?`, `resolution_plan?` |
| `ticket.escalated` | `queue_entry_id` |
| `system.invalid_event` | `raw_payload`, `violation_description` |
| `system.event_dead_lettered` | `original_event`, `attempt_count` |
| `system.escalation_buffer_full` | `buffer_size` |
| `system.state_write_failed` | `ticket_id`, `attempted_state` |

### Resolution_Plan Object

```json
{
  "ticket_id":        "string (UUID v4)",
  "actions": [
    {
      "action_type":  "process_refund | track_order | adjust_billing | send_notification",
      "parameters":   "object"
    }
  ],
  "confidence_score": "number (0.00–1.00)"
}
```

### API Request/Response Models

#### `POST /tickets` — Request

```json
{
  "customer_id":  "string (1–128 chars)",
  "category":     "string (1–64 chars)",
  "description":  "string (1–4000 chars)"
}
```

#### `POST /tickets` — 201 Response

```json
{
  "ticket_id": "string (UUID v4)"
}
```

#### `POST /policies` — Request

```json
{
  "title":    "string (1–200 chars)",
  "content":  "string (1–50 000 chars)",
  "category": "string (1–100 chars)"
}
```

#### `POST /policies` — 201 Response

```json
{
  "policy_id": "string (UUID v4)"
}
```

#### `422` Error Response (shared)

```json
{
  "errors": [
    { "field": "string", "violation": "string" }
  ]
}
```

---

## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: Ticket ingestion round-trip preserves all fields

*For any* valid ticket payload `(customer_id, category, description)`, submitting it via `POST /tickets` and then retrieving it via `GET /tickets/{ticket_id}` must return a ticket record whose `customer_id`, `category`, and `description` fields are byte-for-byte identical to the submitted values.

**Validates: Requirements 1.1, 1.2, 8.4**

---

### Property 2: Invalid ticket payloads are always rejected with 422

*For any* ticket payload where at least one field is absent, empty, or exceeds its defined length constraint, the API Gateway must return HTTP 422 and the response body must name every offending field.

**Validates: Requirements 1.4, 1.7**

---

### Property 3: Confidence score is always in range [0.00, 1.00]

*For any* ticket processed by the Triage Agent, the Confidence_Score recorded in the State_Store and included in the Resolution_Plan must be a value in the closed interval [0.0, 1.0] rounded to at most two decimal places.

**Validates: Requirements 2.2**

---

### Property 4: Routing is determined entirely by confidence score

*For any* Resolution_Plan produced by the Triage Agent, if the Confidence_Score is ≥ 0.75 the agent must emit exactly a `ticket.triaged` event and set status to `triaged`; if the Confidence_Score is < 0.75 the agent must emit exactly a `ticket.escalation_requested` event. No other routing outcome is possible.

**Validates: Requirements 2.3, 2.4, 2.5**

---

### Property 5: Policy retrieval similarity scores are in range [0.0, 1.0]

*For any* Knowledge Agent retrieval operation, every similarity score persisted to the State_Store must be in the closed interval [0.0, 1.0].

**Validates: Requirements 3.7**

---

### Property 6: Policy document round-trip preserves metadata

*For any* valid policy document `(title, content, category)`, submitting it via `POST /policies` and then retrieving it via `GET /policies/{policy_id}` must return the exact same `title`, `category`, and a well-formed ISO-8601 UTC `ingested_at` timestamp.

**Validates: Requirements 12.1, 12.2, 12.5**

---

### Property 7: Invalid policy payloads are always rejected with 422

*For any* policy payload where at least one field is absent, empty, or exceeds its defined length constraint, the API Gateway must return HTTP 422, identify every offending field, and persist nothing to the State_Store or Vector_Store.

**Validates: Requirements 12.6**

---

### Property 8: Event schema conformance is total

*For any* event published to the Event_Bus, the event must contain `event_type` (string), `ticket_id` (string), `timestamp` (ISO-8601 UTC string), and `payload` (object). Any event missing or mis-typing any of these fields must be rejected by the receiving service without processing.

**Validates: Requirements 7.4**

---

### Property 9: Escalation idempotency

*For any* `ticket_id`, delivering the `ticket.escalation_requested` event N times (N ≥ 1) must result in exactly one entry in the human agent queue for that ticket — repeated deliveries must not create duplicates.

**Validates: Requirements 5.4**

---

### Property 10: State transition log is complete and append-only

*For any* ticket that reaches a terminal state (`resolved` or `escalated`), the sequence of entries in `ticket_state_transitions` must form a contiguous, valid path through the status state machine starting at `pending`, with no gaps and no entries for states not reachable from the previous state.

**Validates: Requirements 8.1, 8.3**

---

### Property 11: AI Gateway latency metadata is always recorded

*For any* LLM inference call dispatched by the AI Gateway (including failed calls), the Observability_Stack record must include provider name, model name, prompt token count, completion token count, and wall-clock latency.

**Validates: Requirements 6.4**

---

### Property 12: Execution actions are invoked in plan order

*For any* Resolution_Plan containing an ordered list of actions, the Execution Agent must invoke those actions in exactly the order they appear in the plan — no action at position N may be invoked before all actions at positions 0 through N-1 have completed or failed.

**Validates: Requirements 4.1**

---

### Property 13: Execution action audit record is always complete

*For any* action invoked by the Execution Agent, the State_Store record must include the HTTP request body, HTTP response status code, HTTP response body, and invocation timestamp. No field may be null or absent.

**Validates: Requirements 4.6**

---

### Property 14: Execution terminal state is correct for all outcomes

*For any* Resolution_Plan where all actions succeed, the ticket status must become `resolved` and a `ticket.resolved` event must be published. *For any* plan where at least one action returns an HTTP 4xx/5xx or times out, the ticket status must become `execution_failed`, a `ticket.escalation_requested` event must be published, and no actions after the failing one must be invoked.

**Validates: Requirements 4.4, 4.5**

---

### Property 15: GET ticket returns complete state history

*For any* ticket that has undergone N state transitions, a `GET /tickets/{ticket_id}` request must return all N transition records. *For any* UUID that has never been ingested as a ticket, the same endpoint must return HTTP 404.

**Validates: Requirements 8.4**

---

### Property 16: Policy embedding failure leaves no partial state

*For any* policy submission where the embedding model returns an error, the API Gateway must return HTTP 502 and neither the State_Store nor the Vector_Store must contain any record or vector related to that submission.

**Validates: Requirements 12.4**

---

## Error Handling

### Validation Errors (API Layer)

All inbound request validation is performed by Pydantic v2 models before any I/O is performed. Validation failures immediately return 422 with a structured error body — no partial writes occur.

### State Store Write Failures

If a PostgreSQL write fails, the writing agent retries up to 3 times with 5-second exponential backoff. If all retries are exhausted, a `system.state_write_failed` alert is emitted and the operation is treated as uncommitted (the in-flight event is nacked, triggering Event_Bus redelivery).

### AI Gateway Errors

LLM rate-limit errors (HTTP 429) trigger automatic retry with exponential backoff for up to 60 seconds inside the AI Gateway. The Triage Agent independently retries the AI Gateway call up to 3 times (1 s, 2 s, 4 s). After all retries exhaust, the ticket is escalated.

### Vector Store Timeout / Unavailability

Knowledge Agent enforces a 2-second hard timeout on Qdrant queries. On timeout, unavailability, or zero results, the ticket is immediately escalated with a structured reason code.

### Execution Action Failures

Execution Agent enforces per-action (10 s) and total (30 s) timeouts. On any HTTP 4xx/5xx or timeout, the agent halts, logs completed and failed actions, and escalates. Already-completed actions are not rolled back (eventual consistency with human review for partial cases).

### Escalation Buffer Full

When the local retry buffer reaches 10,000 entries, a `system.escalation_buffer_full` alert is emitted. New escalations arriving while the buffer is full are persisted in a secondary overflow table in the State_Store with `status = 'escalation_pending'` to avoid data loss.

### Dead-Letter Queue

Events that fail acknowledgement after 5 attempts are moved to `omni.dlq`. An operator-facing dashboard in Grafana shows DLQ depth; alerts fire when depth exceeds 100.

### Circuit Breakers

Each agent applies a circuit breaker (via the `tenacity` library) over outbound calls. After 5 consecutive failures within 30 seconds the circuit opens for 60 seconds, immediately failing-fast and escalating without consuming retry budget.

---

## Testing Strategy

### Unit Tests

- **Framework**: `pytest` with `pytest-asyncio`
- **Coverage target**: ≥ 80% line coverage per microservice module (CI enforced)
- **Scope**: Validation logic, state-machine routing, confidence-score thresholding, event schema construction, error-handling paths, retry logic (with mocked dependencies)
- **Mocking**: `unittest.mock` / `respx` for HTTP, `fakeredis`/in-memory RabbitMQ mock for Event_Bus, SQLite in-memory for State_Store in simple cases

### Property-Based Tests

- **Framework**: `hypothesis` (Python)
- **Minimum iterations**: 100 per property (configured via `@settings(max_examples=100)`)
- **Tag format in test code**: `# Feature: omni-resolve, Property N: <property_text>`

| Property | Test target | Generator strategy |
|---|---|---|
| 1 — Ticket round-trip | API Gateway + State_Store | Random valid `customer_id`, `category`, `description` strings within length bounds |
| 2 — Invalid tickets rejected | Pydantic validation layer | Strings violating each constraint (empty, over-length, wrong type) |
| 3 — Confidence score range | Triage Agent score parser | Arbitrary LLM response strings; verify parsed score ∈ [0.0, 1.0] |
| 4 — Confidence routing | Triage Agent router | Random confidence float; verify event type matches threshold rule |
| 5 — Similarity score range | Knowledge Agent retrieval | Random Qdrant response mocks; verify stored scores ∈ [0.0, 1.0] |
| 6 — Policy round-trip | API Gateway + Vector_Store | Random valid `title`, `content`, `category` strings |
| 7 — Invalid policies rejected | Pydantic validation layer | Strings violating each constraint |
| 8 — Event schema conformance | Event schema validator | Arbitrary dicts; verify rejection of non-conforming shapes |
| 9 — Escalation idempotency | Escalation Agent | Replay N identical `ticket.escalation_requested` events; verify queue has 1 entry |
| 10 — State transition log | State_Store writer | Random valid ticket workflows; verify transition table forms valid path |
| 11 — AI Gateway latency metadata | AI Gateway instrumentation | Mocked provider responses (success + failure); verify all fields recorded |
| 12 — Execution action ordering | Execution Agent | Random Resolution_Plan action lists; verify invocation order matches plan order |
| 13 — Execution audit record completeness | Execution Agent + State_Store | Any action invocation; verify all four audit fields present |
| 14 — Execution terminal state correctness | Execution Agent | Random plans with random fail positions; verify correct terminal state and event |
| 15 — GET ticket history completeness | API Gateway + State_Store | Random ticket workflows with N transitions; verify all N returned; random UUIDs → 404 |
| 16 — Policy embedding failure atomicity | API Gateway + Vector_Store | Any policy submission with mocked embedding failure; verify nothing persisted |

### Integration Tests

- Spin up real PostgreSQL, RabbitMQ, and Qdrant in Docker via `testcontainers-python`
- Cover end-to-end happy path and each escalation branch
- Run against `docker-compose.test.yml`; not part of the standard PR unit-test gate but run nightly

### Smoke Tests

- Post-deploy checks that verify:
  - `/health` on each service returns 200
  - AI Gateway `/health` reports all configured providers
  - RabbitMQ management API confirms all queues exist
  - A single end-to-end ticket submission resolves within SLA

### Observability Tests

- Prometheus metric endpoint scrape tests (verify metric names and label cardinality)
- Grafana dashboard JSON schema validation
- Langfuse trace shape validation (example-based, not property-based)
