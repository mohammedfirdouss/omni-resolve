"""Observability instrumentation tests (task 13.2)."""

from __future__ import annotations

import json
import logging
import pathlib

from shared.observability import (
    LATENCY_ANOMALY_THRESHOLD_SECONDS,
    ServiceMetrics,
    TraceStore,
    record_latency_anomaly,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DASHBOARD_PATH = REPO_ROOT / "infra" / "grafana" / "dashboards" / "omniresolve.json"

REQUIRED_METRICS = {
    "omni_requests_total": {"service", "operation", "status"},
    "omni_request_latency_seconds": {"service", "operation"},
    "omni_llm_calls_total": {"service", "provider", "model", "outcome"},
    "omni_llm_latency_seconds": {"service", "provider", "model"},
}


def test_metrics_exposition_has_required_names_and_labels():
    metrics = ServiceMetrics("test-svc")
    metrics.observe_request("op", "200", 0.05)
    metrics.llm_calls_total.labels("test-svc", "openai", "gpt-4o-mini", "success").inc()
    metrics.llm_latency.labels("test-svc", "openai", "gpt-4o-mini").observe(0.5)

    body, content_type = metrics.exposition()
    text = body.decode()
    assert "text/plain" in content_type
    for name in REQUIRED_METRICS:
        assert name in text, f"metric {name} missing from exposition"

    # label cardinality: exactly the expected label names per metric family
    for family in metrics.registry.collect():
        expected = REQUIRED_METRICS.get(family.name)
        if expected is None:
            continue
        for sample in family.samples:
            assert set(sample.labels) - {"le"} == expected


def test_latency_anomaly_recorded_and_linked_to_parent_trace():
    store = TraceStore()
    recorded = store.record_trace(
        trace_id="parent-1",
        agent="triage-agent",
        ticket_id="t-1",
        kind="llm_call",
        data={"provider": "openai"},
        latency_seconds=LATENCY_ANOMALY_THRESHOLD_SECONDS + 0.5,
    )
    assert len(recorded) == 2
    parent, anomaly = recorded
    assert anomaly.kind == "latency_anomaly"
    assert anomaly.parent_trace_id == parent.trace_id
    assert anomaly.data["latency_seconds"] > LATENCY_ANOMALY_THRESHOLD_SECONDS


def test_fast_call_records_no_anomaly():
    store = TraceStore()
    recorded = store.record_trace(
        trace_id="parent-2",
        agent="triage-agent",
        ticket_id="t-2",
        kind="llm_call",
        data={},
        latency_seconds=1.0,
    )
    assert [t.kind for t in recorded] == ["llm_call"]


def test_record_latency_anomaly_helper_links_parent():
    trace = record_latency_anomaly(
        trace_id="parent-3", agent="a", ticket_id="t", latency_seconds=9.0, data={}
    )
    assert trace.parent_trace_id == "parent-3"
    assert trace.kind == "latency_anomaly"


def test_error_rate_alert_fires_via_configured_channel():
    metrics = ServiceMetrics("alerting-svc")
    alerts: list[dict] = []
    metrics.configure_alert_channel(alerts.append)

    for _ in range(9):
        metrics.observe_request("op", "200", 0.01)
    metrics.observe_request("op", "500", 0.01)  # 10% > 5% threshold

    assert alerts, "expected an error-rate alert"
    assert alerts[-1]["alert"] == "error_rate_exceeded"
    assert alerts[-1]["service"] == "alerting-svc"
    assert alerts[-1]["error_rate"] > 0.05


def test_error_rate_alert_falls_back_to_error_log(caplog):
    metrics = ServiceMetrics("no-channel-svc")  # no channel configured
    with caplog.at_level(logging.ERROR, logger="omniresolve.observability"):
        for _ in range(9):
            metrics.observe_request("op", "200", 0.01)
        metrics.observe_request("op", "500", 0.01)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected ERROR-severity fallback log"
    assert "error_rate_exceeded" in errors[-1].getMessage()


def test_no_alert_when_error_rate_below_threshold():
    metrics = ServiceMetrics("quiet-svc")
    alerts: list[dict] = []
    metrics.configure_alert_channel(alerts.append)
    for _ in range(100):
        metrics.observe_request("op", "200", 0.01)
    metrics.observe_request("op", "500", 0.01)  # ~1% < 5%
    assert alerts == []


# ---------------------------------------------------------------------------
# Grafana dashboard JSON validation (Requirement 9.2)
# ---------------------------------------------------------------------------


def test_grafana_dashboard_json_is_valid_and_complete():
    dashboard = json.loads(DASHBOARD_PATH.read_text())

    # refresh interval no greater than 30 s
    assert dashboard["refresh"].endswith("s")
    assert int(dashboard["refresh"].rstrip("s")) <= 30

    panels = dashboard["panels"]
    assert panels, "dashboard must define panels"
    titles = " ".join(p["title"].lower() for p in panels)
    for required in ("throughput", "resolution rate", "escalation rate", "latency", "dlq"):
        assert required in titles, f"missing panel: {required}"

    for panel in panels:
        assert panel["type"]
        assert panel["gridPos"]
        assert panel["targets"], f"panel {panel['title']!r} has no queries"
        for target in panel["targets"]:
            assert target["expr"].strip()
