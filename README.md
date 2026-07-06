# OmniResolve

Cloud-agnostic, event-driven microservices platform for autonomous Tier-1 customer
support resolution. Tickets arrive over HTTP, flow through a pipeline of
LangGraph agents (Triage → Knowledge → Execution), and are either resolved
autonomously or escalated to a human queue. Built from the specs in
[`.kiro/specs/`](.kiro/specs/).

## Architecture

| Component | Directory | Tech |
|---|---|---|
| API Gateway | `services/api-gateway` | FastAPI + Uvicorn (ticket + policy ingestion) |
| Triage Agent | `services/triage-agent` | LangGraph, consumes `ticket.created` |
| Knowledge Agent | `services/knowledge-agent` | LangGraph + Qdrant RAG, consumes `ticket.triaged` |
| Execution Agent | `services/execution-agent` | LangGraph, consumes `ticket.context_ready` |
| Escalation Agent | `services/escalation-agent` | Idempotent human-queue enqueue |
| AI Gateway | `services/ai-gateway` | LiteLLM (OpenAI / Anthropic / Ollama, hot-swappable) |
| Event Bus | RabbitMQ 3.12 | topic exchange `omni.events`, quorum queues, DLQ `omni.dlq` |
| State Store | PostgreSQL 16 | tickets, transitions, decisions, actions, policies (Alembic) |
| Vector Store | Qdrant 1.9 | `policies` collection, cosine |
| Observability | Prometheus + Grafana + Langfuse-style tracing | `/metrics` on every service |

Shared foundations live in [`shared/`](shared/): canonical event schema +
validation, async SQLAlchemy state store with state-machine-enforced audit
trail and write retries, RabbitMQ wrapper (+ in-memory test double), circuit
breaker (5 failures/30 s → open 60 s), Prometheus/tracing helpers.

## Quick start (dev)

```bash
docker compose up --build        # full stack: services + postgres + rabbitmq + qdrant + prometheus + grafana
# migrations
DATABASE_URL_SYNC=postgresql+psycopg://omni:omni@localhost:5432/omni .venv/bin/alembic upgrade head

# submit a ticket
curl -s -X POST localhost:8000/tickets -H 'content-type: application/json' \
  -d '{"customer_id":"c-1","category":"refund","description":"Please refund order #42"}'
```

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest                # unit + property tests (all services)
.venv/bin/python -m pytest --cov=shared --cov=services   # with coverage (CI enforces >= 80%)
docker compose -f docker-compose.test.yml up -d && .venv/bin/python -m pytest tests/integration  # nightly
```

Property-based tests (hypothesis, 100 examples each) implement the 16
correctness properties from `design.md`, tagged
`# Feature: omni-resolve, Property N`.

## Deploy

- **Helm**: `infra/helm/omniresolve` (Kubernetes ≥ 1.27, liveness/readiness on `/health`)
- **Terraform**: `infra/terraform` — single root module, `-var-file` per target (AWS / GCP / bare-metal)
- **CI/CD**: `.github/workflows` — PR unit tests w/ 80% coverage gate; main-branch image build/push tagged with commit SHA + build number
