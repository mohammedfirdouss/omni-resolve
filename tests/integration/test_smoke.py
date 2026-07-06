"""Post-deploy smoke tests (task 17.2).

Run against a deployed stack: set OMNI_SMOKE_BASE_URL (API Gateway),
OMNI_SMOKE_AI_GATEWAY_URL, and OMNI_SMOKE_RABBITMQ_MGMT_URL
(+ OMNI_SMOKE_RABBITMQ_USER/PASSWORD). Skipped when unset.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

BASE_URL = os.environ.get("OMNI_SMOKE_BASE_URL")
AI_GATEWAY_URL = os.environ.get("OMNI_SMOKE_AI_GATEWAY_URL")
RABBITMQ_MGMT_URL = os.environ.get("OMNI_SMOKE_RABBITMQ_MGMT_URL")

SERVICE_HEALTH_URLS = [
    url for url in os.environ.get("OMNI_SMOKE_HEALTH_URLS", "").split(",") if url
]

RESOLUTION_SLA_SECONDS = 60.0
EXPECTED_QUEUES = {
    "omni.dlq",
    "omni.human_queue",
    "triage-agent",
    "knowledge-agent.ticket-triaged",
    "execution-agent.context-ready",
    "escalation-agent.escalation_requested",
}

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="smoke tests need OMNI_SMOKE_BASE_URL (deployed stack)"
)


def test_every_service_health_returns_200():
    urls = SERVICE_HEALTH_URLS or [f"{BASE_URL}/health"]
    for url in urls:
        response = httpx.get(url, timeout=10)
        assert response.status_code == 200, f"{url} -> {response.status_code}"


@pytest.mark.skipif(not AI_GATEWAY_URL, reason="needs OMNI_SMOKE_AI_GATEWAY_URL")
def test_ai_gateway_health_reports_all_providers():
    response = httpx.get(f"{AI_GATEWAY_URL}/health", timeout=30)
    assert response.status_code == 200
    providers = response.json()["providers"]
    assert providers, "AI Gateway reported no providers"
    for provider in providers:
        assert provider["status"] in {"available", "unavailable"}


@pytest.mark.skipif(not RABBITMQ_MGMT_URL, reason="needs OMNI_SMOKE_RABBITMQ_MGMT_URL")
def test_rabbitmq_management_confirms_queues_exist():
    auth = (
        os.environ.get("OMNI_SMOKE_RABBITMQ_USER", "omni"),
        os.environ.get("OMNI_SMOKE_RABBITMQ_PASSWORD", "omni"),
    )
    response = httpx.get(f"{RABBITMQ_MGMT_URL}/api/queues", auth=auth, timeout=10)
    assert response.status_code == 200
    existing = {q["name"] for q in response.json()}
    missing = EXPECTED_QUEUES - existing
    assert not missing, f"missing queues: {missing}"


def test_single_ticket_resolves_within_sla():
    created = httpx.post(
        f"{BASE_URL}/tickets",
        json={
            "customer_id": "smoke-test",
            "category": "refund",
            "description": "smoke test: refund order 1",
        },
        timeout=10,
    )
    assert created.status_code == 201
    ticket_id = created.json()["ticket_id"]

    deadline = time.monotonic() + RESOLUTION_SLA_SECONDS
    last_status = None
    while time.monotonic() < deadline:
        ticket = httpx.get(f"{BASE_URL}/tickets/{ticket_id}", timeout=10).json()
        last_status = ticket["status"]
        if last_status in {"resolved", "escalated"}:
            return  # reached a terminal state within SLA
        time.sleep(2)
    pytest.fail(f"ticket {ticket_id} stuck in {last_status!r} after {RESOLUTION_SLA_SECONDS}s")
