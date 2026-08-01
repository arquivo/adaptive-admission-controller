"""Phase 5 observability integration tests: diagnostic headers, admin API,
and metrics increments, driven through the real `create_app()` lifespan."""

from __future__ import annotations

import asyncio

import pytest

from app import metrics
from tests.integration.conftest import _free_port, make_config, running_app

pytestmark = pytest.mark.asyncio


def _counter_value(counter, *labels):
    return counter.labels(*labels)._value.get()


async def test_diagnostic_headers_absent_by_default(mock_backend):
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get("/proxytest/echo")

    assert response.status_code == 200
    assert "X-AAC-Backend" not in response.headers


async def test_diagnostic_headers_present_on_admitted_response_when_enabled(mock_backend):
    config = make_config(mock_backend, debug_headers_enabled=True)
    async with running_app(config) as (_app, client):
        response = await client.get("/proxytest/echo")

    assert response.status_code == 200
    assert response.headers["X-AAC-Backend"] == "mock-backend"
    assert "X-AAC-Score" in response.headers
    assert response.headers["X-AAC-Exempt"] in {"true", "false"}
    assert "X-AAC-Reject-Reason" not in response.headers


async def test_diagnostic_headers_include_reject_reason_on_queue_full(
    mock_backend_with_concurrency_counter,
):
    upstream_url, _counter = mock_backend_with_concurrency_counter
    config = make_config(
        upstream_url,
        concurrency_limit=1,
        queue_max_size=1,
        queue_timeout_seconds=30,
        debug_headers_enabled=True,
    )
    async with running_app(config) as (_app, client):
        responses = await asyncio.gather(
            *(client.get("/proxytest/slow?delay=0.3") for _ in range(10))
        )

    rejected = [r for r in responses if r.status_code == 429]
    assert rejected
    for r in rejected:
        assert r.headers["X-AAC-Reject-Reason"] == "queue_full"


async def test_unmatched_path_has_no_diagnostic_headers(mock_backend):
    config = make_config(mock_backend, debug_headers_enabled=True)
    async with running_app(config) as (_app, client):
        response = await client.get("/does-not-exist")

    assert response.status_code == 404
    assert "X-AAC-Backend" not in response.headers


async def test_admitted_request_increments_requests_and_admitted_counters(mock_backend):
    config = make_config(mock_backend)
    before_requests = _counter_value(metrics.requests_total, "mock-backend", "anonymous", "false")
    before_admitted = _counter_value(metrics.admitted_total, "mock-backend", "anonymous", "false")

    async with running_app(config) as (_app, client):
        response = await client.get("/proxytest/echo")

    assert response.status_code == 200
    after_requests = _counter_value(metrics.requests_total, "mock-backend", "anonymous", "false")
    after_admitted = _counter_value(metrics.admitted_total, "mock-backend", "anonymous", "false")
    assert after_requests == before_requests + 1
    assert after_admitted == before_admitted + 1


async def test_queue_full_rejection_increments_rejected_counter(
    mock_backend_with_concurrency_counter,
):
    upstream_url, _counter = mock_backend_with_concurrency_counter
    config = make_config(
        upstream_url, concurrency_limit=1, queue_max_size=1, queue_timeout_seconds=30
    )
    before = _counter_value(
        metrics.rejected_total, "mock-backend", "anonymous", "queue_full", "false"
    )

    async with running_app(config) as (_app, client):
        responses = await asyncio.gather(
            *(client.get("/proxytest/slow?delay=0.3") for _ in range(10))
        )

    rejected = [r for r in responses if r.status_code == 429]
    assert rejected
    after = _counter_value(
        metrics.rejected_total, "mock-backend", "anonymous", "queue_full", "false"
    )
    assert after == before + len(rejected)


async def test_backend_error_increments_backend_errors_total_not_rejected():
    unreachable_url = f"http://127.0.0.1:{_free_port()}"
    config = make_config(unreachable_url)
    before_errors = _counter_value(metrics.backend_errors_total, "mock-backend")
    before_admitted = _counter_value(metrics.admitted_total, "mock-backend", "anonymous", "false")
    before_rejected_any = sum(
        s.value
        for s in metrics.rejected_total.collect()[0].samples
        if s.labels.get("backend") == "mock-backend"
    )

    async with running_app(config) as (_app, client):
        response = await client.get("/proxytest/echo")

    assert response.status_code == 502
    after_errors = _counter_value(metrics.backend_errors_total, "mock-backend")
    after_admitted = _counter_value(metrics.admitted_total, "mock-backend", "anonymous", "false")
    after_rejected_any = sum(
        s.value
        for s in metrics.rejected_total.collect()[0].samples
        if s.labels.get("backend") == "mock-backend"
    )
    assert after_errors == before_errors + 1
    assert after_admitted == before_admitted + 1  # backend failure still counts as admitted
    assert after_rejected_any == before_rejected_any  # never an admission-control reject


async def test_admin_routes_fail_closed_without_token_configured(mock_backend, monkeypatch):
    monkeypatch.delenv("AAC_ADMIN_API_TOKEN", raising=False)
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get(
            "/admin/backends", headers={"Authorization": "Bearer whatever"}
        )

    assert response.status_code == 403


async def test_admin_routes_reject_missing_or_wrong_token(mock_backend, monkeypatch):
    monkeypatch.setenv("AAC_ADMIN_API_TOKEN", "correct-token")
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        no_auth = await client.get("/admin/backends")
        wrong_auth = await client.get(
            "/admin/backends", headers={"Authorization": "Bearer wrong-token"}
        )

    assert no_auth.status_code == 401
    assert wrong_auth.status_code == 401


async def test_admin_backends_list_returns_summary_with_correct_token(mock_backend, monkeypatch):
    monkeypatch.setenv("AAC_ADMIN_API_TOKEN", "correct-token")
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get(
            "/admin/backends", headers={"Authorization": "Bearer correct-token"}
        )

    assert response.status_code == 200
    backends = response.json()["backends"]
    assert len(backends) == 1
    entry = backends[0]
    assert entry["name"] == "mock-backend"
    assert entry["path_prefix"] == "/proxytest"
    assert entry["controller_type"] == "fixed"
    assert entry["current_limit"] == 100
    assert entry["queue_size"] == 0


async def test_admin_backend_policy_returns_config_and_scoring(mock_backend, monkeypatch):
    monkeypatch.setenv("AAC_ADMIN_API_TOKEN", "correct-token")
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get(
            "/admin/backends/mock-backend/policy",
            headers={"Authorization": "Bearer correct-token"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["config"]["name"] == "mock-backend"
    assert "resolved_scoring" in body


async def test_admin_backend_policy_unknown_backend_returns_404(mock_backend, monkeypatch):
    monkeypatch.setenv("AAC_ADMIN_API_TOKEN", "correct-token")
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get(
            "/admin/backends/does-not-exist/policy",
            headers={"Authorization": "Bearer correct-token"},
        )

    assert response.status_code == 404


async def test_admin_backend_limit_returns_current_limit(mock_backend, monkeypatch):
    monkeypatch.setenv("AAC_ADMIN_API_TOKEN", "correct-token")
    config = make_config(mock_backend, concurrency_limit=42)
    async with running_app(config) as (_app, client):
        response = await client.get(
            "/admin/backends/mock-backend/limit",
            headers={"Authorization": "Bearer correct-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"backend": "mock-backend", "current_limit": 42}
