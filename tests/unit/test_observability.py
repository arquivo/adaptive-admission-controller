"""Unit tests for `app/observability.py`."""

from __future__ import annotations

import json
import logging

from app.interfaces import RequestContext
from app.observability import JsonFormatter, log_admission_event
from app.scoring import ScoreBreakdown


def _ctx(**overrides) -> RequestContext:
    defaults = dict(
        backend="mock-backend",
        path="/proxytest/echo",
        method="GET",
        arrival_time=0.0,
        source_ip="203.0.113.5",
        user_class="anonymous",
        asn="AS64500",
        country="PT",
        score=90,
    )
    defaults.update(overrides)
    return RequestContext(**defaults)


def test_json_formatter_emits_valid_json_with_expected_fields():
    record = logging.LogRecord(
        name="app.observability",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="admitted",
        args=(),
        exc_info=None,
    )
    record.backend = "mock-backend"
    record.status_code = 200

    payload = json.loads(JsonFormatter().format(record))

    assert payload["event"] == "admitted"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.observability"
    assert payload["backend"] == "mock-backend"
    assert payload["status_code"] == 200
    assert "timestamp" in payload


def test_json_formatter_includes_exception_info():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="app.observability",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failure",
            args=(),
            exc_info=__import__("sys").exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc_info"]


def test_log_admission_event_without_score_breakdown_uses_final_score_only(caplog):
    ctx = _ctx(score_breakdown=None)
    with caplog.at_level(logging.INFO, logger="app.observability"):
        log_admission_event("admitted", ctx, status_code=200)

    record = caplog.records[0]
    assert record.score == {"final": 90}
    assert record.backend == "mock-backend"
    assert record.status_code == 200
    assert "reason" not in record.__dict__


def test_log_admission_event_with_score_breakdown_reports_full_decomposition(caplog):
    breakdown = ScoreBreakdown(
        base=100,
        penalty_ip=10,
        penalty_net24=0,
        penalty_net6=0,
        penalty_asn=0,
        penalty_country=0,
        penalty_user=0,
        is_exempt=True,
        final=90,
    )
    ctx = _ctx(score_breakdown=breakdown)
    with caplog.at_level(logging.INFO, logger="app.observability"):
        log_admission_event(
            "rejected", ctx, reason="queue_full", queue_wait_ms=12.5, status_code=429
        )

    record = caplog.records[0]
    assert record.score["penalty_ip"] == 10
    assert record.score["final"] == 90
    assert record.country_exempt is True
    assert record.reason == "queue_full"
    assert record.queue_wait_ms == 12.5
    assert record.status_code == 429


def test_log_admission_event_omits_optional_fields_when_not_provided(caplog):
    ctx = _ctx()
    with caplog.at_level(logging.INFO, logger="app.observability"):
        log_admission_event("admitted", ctx)

    record = caplog.records[0]
    assert "reason" not in record.__dict__
    assert "queue_wait_ms" not in record.__dict__
    assert "backend_latency_ms" not in record.__dict__
    assert "status_code" not in record.__dict__
