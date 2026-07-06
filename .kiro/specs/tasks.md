# Implementation Plan: OmniResolve

## Overview

Build the OmniResolve agentic customer support platform as a set of eight independently deployable Python 3.12 microservices communicating asynchronously over RabbitMQ, with PostgreSQL for state persistence, Qdrant for semantic vector retrieval, and LiteLLM as the AI Gateway. Tasks are ordered so each step builds on the previous, ending with full end-to-end wiring and observability instrumentation.

---

## Tasks

- [ ] 1. Project scaffold and shared foundations
  - Create the top-level monorepo layout: `services/api-gateway`, `services/triage-agent`, `services/knowledge-agent`, `services/execution-agent`, `services/escalation-agent`, `services/ai-gateway`, `shared/`, `infra/`, `tests/integration/`
  - Add `shared/models.py` — Pydantic v2 models for all canonical event payloads, API request/response bodies, Resolution_Plan, and the 422-error body
  - Add `shared/event_bus.py` — thin RabbitMQ publisher/consumer wrapper (aio-pika) with schema validation on publish and consume
  - Add `shared/db.py` — async SQLAlchemy 2.x connection factory, retry decorator (3 attempts, 5 s exponential backoff), and `system.state_write_failed` alert hook
  - Add `shared/circuit_breaker.py` — `tenacity`-based circuit breaker: 5 consecutive failures / 30 s opens for 60 s
  - Add `shared/observability.py` — `prometheus_client` registry helpers and a `record_latency_anomaly` hook wired to Langfuse
  - Write `alembic/` migration creating all five PostgreSQL tables (`tickets`, `ticket_state_transitions`, `agent_decisions`, `execution_actions`, `policy_documents`, `retrieval_records`)
  - Add `docker-compose.yml` (dev) and `docker-compose.test.yml` (integration tests) with PostgreSQL 16, RabbitMQ 3.12, Qdrant 1.9, and placeholder service slots
  - _Requirements: 1.1–1.7, 7.4, 8.1–8.5, 10.1_

- [ ] 2. API Gateway — ticket and policy endpoints
  - [ ] 2.1 Implement `POST /tickets`, `GET /tickets/{ticket_id}`, `POST /policies`, `GET /policies/{policy_id}`, `GET /health`, `GET /metrics` using FastAPI + Uvicorn
    - Validate all inbound payloads with Pydantic v2 models; return 422 with per-field errors on failure
    - Persist ticket to State_Store with `status = 'pending'`; return 201 with UUID v4 `ticket_id`
    - Persist policy metadata to State_Store; embed content via AI_Gateway; upsert vector to Qdrant; return 201 with `policy_id`
    - Return HTTP 503 when State_Store is unavailable on ticket ingestion (no event published)
    - Return HTTP 502 and persist nothing when embedding model errors during policy ingestion
    - `GET /tickets/{ticket_id}` returns full state history (transitions + agent decisions + execution actions) or 404
    - `GET /policies/{policy_id}` returns title, category, ingested_at or 404
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.7, 8.4, 12.1, 12.2, 12.4, 12.5, 12.6, 12.7_

  - [ ]* 2.2 Write property test for ticket ingestion round-trip (Property 1)
    - **Property 1: Ticket ingestion round-trip preserves all fields**
    - **Validates: Requirements 1.1, 1.2, 8.4**
    - Generate random valid `(customer_id, category, description)` strings within length bounds; POST, then GET; assert all three fields are byte-for-byte identical
    - Tag: `# Feature: omni-resolve, Property 1`

  - [ ]* 2.3 Write property test for invalid ticket rejection (Property 2)
    - **Property 2: Invalid ticket payloads are always rejected with 422**
    - **Validates: Requirements 1.4, 1.7**
    - Generate payloads violating each constraint (empty fields, over-length, wrong type); assert HTTP 422 and every offending field named in response
    - Tag: `# Feature: omni-resolve, Property 2`

  - [ ]* 2.4 Write property test for policy round-trip (Property 6)
    - **Property 6: Policy document round-trip preserves metadata**
    - **Validates: Requirements 12.1, 12.2, 12.5**
    - Generate random valid `(title, content, category)` strings; POST, then GET; assert title, category identical and `ingested_at` is well-formed ISO-8601 UTC
    - Tag: `# Feature: omni-resolve, Property 6`

  - [ ]* 2.5 Write property test for invalid policy rejection (Property 7)
    - **Property 7: Invalid policy payloads are always rejected with 422**
    - **Validates: Requirements 12.6**
    - Generate payloads violating each constraint; assert HTTP 422 and nothing persisted to State_Store or Vector_Store
    - Tag: `# Feature: omni-resolve, Property 7`

  - [ ]* 2.6 Write property test for policy embedding failure atomicity (Property 16)
    - **Property 16: Policy embedding failure leaves no partial state**
    - **Validates: Requirements 12.4**
    - Mock embedding model to return error; assert HTTP 502 and no record in State_Store or Vector_Store
    - Tag: `# Feature: omni-resolve, Property 16`

  - [ ]* 2.7 Write unit tests for API Gateway
    - Test State_Store unavailability returns 503 (no event published)
    - Test 404 for unknown ticket_id and policy_id
    - Test UUID v4 generation uniqueness
    - _Requirements: 1.5, 8.4, 12.7_

- [ ] 3. Checkpoint — API Gateway
  - Ensure all API Gateway tests pass. Ask the user if any questions arise before continuing.

- [ ] 4. Event Bus — publish and validate `ticket.created`
  - [ ] 4.1 Extend `shared/event_bus.py` to publish `ticket.created` from the API Gateway after successful State_Store write
    - Enforce canonical event schema on every publish; reject and emit `system.invalid_event` for non-conforming shapes
    - Configure topic exchange `omni.events`, per-service quorum queues, dead-letter queue `omni.dlq` (after 5 failed acks), and consistent-hash exchange for per-`ticket_id` ordering
    - _Requirements: 1.3, 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 4.2 Write property test for event schema conformance (Property 8)
    - **Property 8: Event schema conformance is total**
    - **Validates: Requirements 7.4**
    - Generate arbitrary dicts; assert only events with all four required fields and correct types are accepted; all others rejected
    - Tag: `# Feature: omni-resolve, Property 8`

  - [ ]* 4.3 Write unit tests for Event Bus
    - Test DLQ routing after 5 failed acks (`system.event_dead_lettered` alert emitted)
    - Test per-`ticket_id` ordering guarantee
    - Test redelivery with exponential backoff
    - _Requirements: 7.3, 7.5_

- [ ] 5. AI Gateway service
  - [ ] 5.1 Implement `services/ai-gateway` using LiteLLM Proxy
    - Config-based provider selection (`LITELLM_MODEL`, `LITELLM_PROVIDER`, `OPENAI_API_KEY`, etc.) with no-restart model swap
    - Auto-retry on HTTP 429 with exponential backoff (1 s, doubling) for up to 60 s; return structured error with provider name, failure reason, elapsed time on exhaustion
    - Expose `/health` endpoint probing each configured provider (5 s timeout, returns `"available"` / `"unavailable"`)
    - Expose `/metrics` with Prometheus metrics; emit latency to Observability_Stack (provider name, model name, prompt tokens, completion tokens, wall-clock latency) for every call including failures
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 5.2 Write property test for AI Gateway latency metadata (Property 11)
    - **Property 11: AI Gateway latency metadata is always recorded**
    - **Validates: Requirements 6.4**
    - Mock provider responses (success and failure); assert every call records provider name, model name, prompt tokens, completion tokens, and wall-clock latency
    - Tag: `# Feature: omni-resolve, Property 11`

  - [ ]* 5.3 Write unit tests for AI Gateway
    - Test 429 retry logic exhaustion returns structured error
    - Test config swap without restart
    - Test `/health` probe timeout returns `"unavailable"`
    - _Requirements: 6.2, 6.3, 6.5_

- [ ] 6. Triage Agent
  - [ ] 6.1 Implement `services/triage-agent` as a LangGraph agent consuming `ticket.created`
    - Build LLM prompt from ticket fields; call AI_Gateway; parse Resolution_Plan (ticket_id, ordered actions, confidence_score)
    - Clamp/validate Confidence_Score to [0.00, 1.00] two decimal places
    - If confidence ≥ 0.75: update status to `triaged`, publish `ticket.triaged` with full Resolution_Plan
    - If confidence < 0.75: publish `ticket.escalation_requested` with ticket_id and confidence_score
    - Retry AI_Gateway up to 3 times (exponential backoff: 1 s, 2 s, 4 s); on exhaustion update status to `escalated` and publish `ticket.escalation_requested`
    - Record agent decision to State_Store (inputs, recommended actions, confidence_score) and persist state transition
    - Apply circuit breaker from `shared/circuit_breaker.py` over AI_Gateway calls
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [ ]* 6.2 Write property test for confidence score range (Property 3)
    - **Property 3: Confidence score is always in range [0.00, 1.00]**
    - **Validates: Requirements 2.2**
    - Generate arbitrary LLM response strings; assert parsed confidence_score ∈ [0.0, 1.0] with ≤ 2 decimal places
    - Tag: `# Feature: omni-resolve, Property 3`

  - [ ]* 6.3 Write property test for confidence-based routing (Property 4)
    - **Property 4: Routing is determined entirely by confidence score**
    - **Validates: Requirements 2.3, 2.4, 2.5**
    - Generate random confidence float in [0.0, 1.0]; assert ≥ 0.75 → exactly `ticket.triaged` + status `triaged`; < 0.75 → exactly `ticket.escalation_requested`
    - Tag: `# Feature: omni-resolve, Property 4`

  - [ ]* 6.4 Write unit tests for Triage Agent
    - Test AI_Gateway retry exhaustion triggers escalation
    - Test agent decision record persisted for every ticket
    - Test circuit breaker opens after 5 consecutive failures
    - _Requirements: 2.6, 2.7_

- [ ] 7. Checkpoint — Triage Agent
  - Ensure all Triage Agent tests pass. Ask the user if any questions arise before continuing.

- [ ] 8. Knowledge Agent
  - [ ] 8.1 Implement `services/knowledge-agent` as a LangGraph agent consuming `ticket.triaged`
    - Embed ticket description via AI_Gateway; query Qdrant for top-5 nearest policy vectors with cosine distance
    - Enforce 2-second hard timeout on Qdrant query
    - On timeout → escalate with reason `retrieval_timeout`; on zero results → escalate with reason `no_policy_found`; on unavailability → escalate with reason `vector_store_unavailable`
    - On success: attach Policy_Document IDs + Resolution_Plan to event; publish `ticket.context_ready`
    - Persist retrieval_records (policy_ids + similarity scores) to State_Store
    - Apply circuit breaker over Qdrant calls
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [ ]* 8.2 Write property test for similarity score range (Property 5)
    - **Property 5: Policy retrieval similarity scores are in range [0.0, 1.0]**
    - **Validates: Requirements 3.7**
    - Generate random mocked Qdrant responses; assert every stored similarity score ∈ [0.0, 1.0]
    - Tag: `# Feature: omni-resolve, Property 5`

  - [ ]* 8.3 Write unit tests for Knowledge Agent
    - Test 2 s timeout escalation path with reason `retrieval_timeout`
    - Test zero-result escalation path with reason `no_policy_found`
    - Test unavailability escalation with reason `vector_store_unavailable`
    - _Requirements: 3.3, 3.5, 3.6_

- [ ] 9. Execution Agent
  - [ ] 9.1 Implement `services/execution-agent` as a LangGraph agent consuming `ticket.context_ready`
    - Invoke REST actions (`process_refund`, `track_order`, `adjust_billing`, `send_notification`) in exactly the order defined by the Resolution_Plan
    - Enforce per-action timeout of 10 s and total timeout of 30 s
    - On all actions success: update status to `resolved`, publish `ticket.resolved` with completed actions list
    - On any action HTTP 4xx/5xx or per-action timeout: halt remaining actions, update status to `execution_failed`, publish `ticket.escalation_requested`
    - On total timeout exceeded: halt pending actions, update status to `execution_failed`, publish `ticket.escalation_requested`
    - Record HTTP request body, response status, response body, invocation timestamp to State_Store for every action invoked
    - Apply circuit breaker over external REST calls
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [ ]* 9.2 Write property test for execution action ordering (Property 12)
    - **Property 12: Execution actions are invoked in plan order**
    - **Validates: Requirements 4.1**
    - Generate random Resolution_Plan action lists; assert invocation order matches plan order exactly
    - Tag: `# Feature: omni-resolve, Property 12`

  - [ ]* 9.3 Write property test for execution audit record completeness (Property 13)
    - **Property 13: Execution action audit record is always complete**
    - **Validates: Requirements 4.6**
    - Generate any action invocation; assert State_Store record contains HTTP request body, response status, response body, and invocation timestamp — no field null or absent
    - Tag: `# Feature: omni-resolve, Property 13`

  - [ ]* 9.4 Write property test for execution terminal state correctness (Property 14)
    - **Property 14: Execution terminal state is correct for all outcomes**
    - **Validates: Requirements 4.4, 4.5**
    - Generate random plans with random fail positions (4xx/5xx/timeout); assert all-success → `resolved` + `ticket.resolved`; any failure → `execution_failed` + `ticket.escalation_requested`; no actions invoked after first failure
    - Tag: `# Feature: omni-resolve, Property 14`

  - [ ]* 9.5 Write unit tests for Execution Agent
    - Test per-action 10 s timeout halts remaining actions
    - Test total 30 s timeout halts pending actions and escalates
    - _Requirements: 4.2, 4.3_

- [ ] 10. Checkpoint — Execution and Knowledge Agents
  - Ensure all Execution Agent and Knowledge Agent tests pass. Ask the user if any questions arise before continuing.

- [ ] 11. Escalation Agent
  - [ ] 11.1 Implement `services/escalation-agent` consuming `ticket.escalation_requested`
    - Idempotent enqueue to human agent queue keyed on `ticket_id` (no duplicates on redelivery)
    - Update Ticket status to `escalated` within 2 s; publish `ticket.escalated`
    - Local retry buffer: max 10,000 entries, retry interval 30 s; emit `system.escalation_buffer_full` when buffer full
    - On buffer full: persist overflow to State_Store with `status = 'escalation_pending'`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 11.2 Write property test for escalation idempotency (Property 9)
    - **Property 9: Escalation idempotency**
    - **Validates: Requirements 5.4**
    - Replay N identical `ticket.escalation_requested` events for same `ticket_id` (N ≥ 1); assert exactly one entry in human agent queue
    - Tag: `# Feature: omni-resolve, Property 9`

  - [ ]* 11.3 Write unit tests for Escalation Agent
    - Test `system.escalation_buffer_full` alert emitted at 10,000 entries
    - Test overflow persisted to State_Store with `escalation_pending`
    - Test `ticket.escalated` published after successful enqueue
    - _Requirements: 5.3, 5.5_

- [ ] 12. State Store audit trail
  - [ ] 12.1 Implement state transition recording in `shared/db.py` — append `ticket_state_transitions` row on every status change
    - Validate transitions follow state machine: `pending` → `triaged` → `resolved` | `escalated`; `triaged` → `execution_failed` → `escalated`
    - Record `total_elapsed_seconds` on `resolved` or `escalated` terminal state entry
    - Enforce 3-retry / 5 s backoff on write failure; emit `system.state_write_failed` on exhaustion; nack event for redelivery
    - Retain records ≥ 90 days (enforced via PostgreSQL table partitioning or Alembic migration adding retention policy)
    - _Requirements: 8.1, 8.2, 8.3, 8.5_

  - [ ]* 12.2 Write property test for state transition log completeness (Property 10)
    - **Property 10: State transition log is complete and append-only**
    - **Validates: Requirements 8.1, 8.3**
    - Generate random valid ticket workflows ending in terminal state; assert `ticket_state_transitions` forms contiguous valid state-machine path from `pending` with no gaps and no invalid transitions
    - Tag: `# Feature: omni-resolve, Property 10`

  - [ ]* 12.3 Write property test for GET ticket history completeness (Property 15)
    - **Property 15: GET ticket returns complete state history**
    - **Validates: Requirements 8.4**
    - Generate random ticket workflows with N transitions; assert GET returns all N; generate random UUIDs and assert 404
    - Tag: `# Feature: omni-resolve, Property 15`

  - [ ]* 12.4 Write unit tests for State Store audit trail
    - Test `system.state_write_failed` emitted after 3 retries exhausted
    - Test `total_elapsed_seconds` recorded on `resolved` and `escalated`
    - _Requirements: 8.3, 8.5_

- [ ] 13. Observability instrumentation
  - [ ] 13.1 Instrument all microservices with Prometheus metrics (request rate, error rate, p95 latency) and expose `/metrics` endpoint
    - Wire Langfuse tracing for every agent reasoning step (LLM prompt, policy IDs retrieved, actions invoked, outcomes)
    - Record latency anomaly (> 5 s LLM call) to Langfuse linked to parent agent trace
    - Alert on error rate > 5% over 5-minute window; fall back to ERROR log if no alert channel configured
    - Add Grafana dashboard JSON (ticket throughput, resolution rate, escalation rate, agent latency, DLQ depth) with ≤ 30 s refresh
    - Configure Grafana / Prometheus retention ≥ 30 days; Langfuse trace retention ≥ 30 days
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [ ]* 13.2 Write unit tests for observability instrumentation
    - Test Prometheus metric endpoint scrape returns all required metric names with correct label cardinality
    - Test latency anomaly recorded and linked to correct parent trace when LLM call > 5 s
    - Test alert emitted when error rate threshold breached; fallback to ERROR log when no channel configured
    - Test Grafana dashboard JSON passes schema validation
    - _Requirements: 9.1, 9.4, 9.5_

- [ ] 14. Checkpoint — Observability and State Store
  - Ensure all State Store and Observability tests pass. Ask the user if any questions arise before continuing.

- [ ] 15. Infrastructure: Docker, Helm, and Terraform
  - [ ] 15.1 Write `Dockerfile` for each microservice (Python 3.12, multi-stage, non-root user)
    - Pin all Python dependency versions in `requirements.txt` per service
    - _Requirements: 10.1_

  - [ ] 15.2 Write Helm charts for all services and infrastructure components
    - Include liveness/readiness probes (`/health`), resource limits, configurable replica counts, and values overrides for each environment
    - Target Kubernetes ≥ 1.27
    - _Requirements: 10.2, 10.6_

  - [ ] 15.3 Write Terraform modules for AWS, GCP, and bare-metal targets
    - Single root module invocation; environment-specific inputs via `.tfvars`; no changes to module source required per environment
    - Use only open-source infra dependencies (PostgreSQL, RabbitMQ, Qdrant)
    - _Requirements: 10.3, 10.4, 10.5_

- [ ] 16. CI/CD pipeline
  - [ ] 16.1 Write GitHub Actions workflow for pull-request unit tests
    - Run unit tests only for services with changed source files; fail if any test fails; fail if line coverage < 80% for any module
    - _Requirements: 11.1, 11.3, 11.4_

  - [ ] 16.2 Write GitHub Actions workflow for container build and push on main branch merge
    - Build and push versioned images within 15 minutes of merge; tag with full commit SHA and semantic version / build number
    - Emit failure notification to configured alert channel within 5 minutes of any pipeline failure
    - _Requirements: 11.2, 11.5, 11.6_

- [ ] 17. Integration tests and end-to-end wiring
  - [ ] 17.1 Write integration test suite in `tests/integration/` using `testcontainers-python`
    - Spin up real PostgreSQL 16, RabbitMQ 3.12, Qdrant 1.9 containers; run against `docker-compose.test.yml`
    - Cover happy-path end-to-end flow (ticket created → triaged → context ready → resolved)
    - Cover each escalation branch (low confidence, retrieval timeout, zero results, action failure)
    - _Requirements: 1.1–1.7, 2.1–2.7, 3.1–3.7, 4.1–4.7, 5.1–5.5_

  - [ ]* 17.2 Write smoke tests
    - POST a ticket and assert it resolves within SLA; GET `/health` on each service returns 200; AI Gateway `/health` reports all providers; RabbitMQ management API confirms all queues exist
    - _Requirements: 1.2, 6.5_

- [ ] 18. Final checkpoint — all tests pass
  - Ensure all unit, property, integration, and smoke tests pass. Run `pytest --cov` across all services and confirm ≥ 80% line coverage per module. Ask the user if any questions arise.

---

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP
- Each task references specific requirements for traceability
- Checkpoints (tasks 3, 7, 10, 14, 18) provide incremental validation gates
- Property tests use the `hypothesis` framework with `@settings(max_examples=100)` and must include the tag comment `# Feature: omni-resolve, Property N`
- Unit tests use `pytest` + `pytest-asyncio`; HTTP mocking via `respx`; RabbitMQ via in-memory mock
- Integration tests use `testcontainers-python` and are run nightly, not on every PR
- All outbound calls (AI Gateway, Qdrant, external REST, RabbitMQ) are wrapped by the shared circuit breaker
- State_Store writes across all agents use the shared retry decorator in `shared/db.py`

---

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["2.1", "4.1", "5.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "4.2", "4.3", "5.2", "5.3"] },
    { "id": 3, "tasks": ["6.1"] },
    { "id": 4, "tasks": ["6.2", "6.3", "6.4", "8.1"] },
    { "id": 5, "tasks": ["8.2", "8.3", "9.1"] },
    { "id": 6, "tasks": ["9.2", "9.3", "9.4", "9.5", "11.1", "12.1"] },
    { "id": 7, "tasks": ["11.2", "11.3", "12.2", "12.3", "12.4"] },
    { "id": 8, "tasks": ["13.1"] },
    { "id": 9, "tasks": ["13.2", "15.1", "15.2", "15.3"] },
    { "id": 10, "tasks": ["16.1", "16.2"] },
    { "id": 11, "tasks": ["17.1"] },
    { "id": 12, "tasks": ["17.2"] }
  ]
}
```
