"""Unit tests for `app/metrics.py`."""

from __future__ import annotations

import pytest
from prometheus_client import REGISTRY
from starlette.applications import Starlette

from app import metrics

# prometheus_client's REGISTRY.collect() reports Counter family names with
# their `_total` suffix stripped (it's re-added on the actual sample name),
# so these must match the collected *family* name, not the Python identifier.
_EXPECTED_METRIC_NAMES = {
    "admission_inflight_requests",
    "admission_inflight_tokens",
    "admission_concurrency_limit",
    "admission_queue_size",
    "admission_requests",
    "admission_admitted",
    "admission_rejected",
    "admission_queue_timeout",
    "backend_errors",
    "backend_timeouts",
    "adaptive_limit_changes",
    "backend_instance_errors",
    "backend_instance_connect_failures",
    "admission_instance_inflight_requests",
    "admission_instance_healthy",
    "backend_request_duration_seconds",
    "queue_wait_duration_seconds",
    "score_distribution",
}


def _collected_names() -> set[str]:
    names = set()
    for family in REGISTRY.collect():
        names.add(family.name)
    return names


def test_all_expected_metrics_are_registered():
    collected = _collected_names()
    for name in _EXPECTED_METRIC_NAMES:
        assert name in collected, f"{name} not registered against the default REGISTRY"


def test_score_distribution_has_the_documented_bucket_range():
    buckets = metrics.score_distribution.labels("mock-backend", "false")._buckets
    # range(-100, 110, 10) => 21 upper bounds, plus prometheus_client's own +Inf.
    assert len(buckets) == 22


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_text_exposition():
    metrics.requests_total.labels("mock-backend", "anonymous", "false").inc()
    app = Starlette(routes=metrics.routes)
    app.state.load_balancers = {}
    async with app.router.lifespan_context(app):
        pass

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "admission_requests_total" in response.text


@pytest.mark.asyncio
async def test_metrics_endpoint_populates_instance_gauges_from_load_balancer_snapshot():
    from app.interfaces import InstanceStatus

    class _FakeLoadBalancer:
        def snapshot(self):
            return [
                InstanceStatus(url="http://a:8080", healthy=True, in_flight=3, sticky_count=1),
                InstanceStatus(url="http://b:8080", healthy=False, in_flight=0, sticky_count=0),
            ]

    app = Starlette(routes=metrics.routes)
    app.state.load_balancers = {"mock-backend": _FakeLoadBalancer()}
    async with app.router.lifespan_context(app):
        pass

    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/metrics")

    body = response.text
    assert (
        'admission_instance_inflight_requests{backend="mock-backend",instance="http://a:8080"} 3.0'
        in body
    )
    assert 'admission_instance_healthy{backend="mock-backend",instance="http://a:8080"} 1.0' in body
    assert 'admission_instance_healthy{backend="mock-backend",instance="http://b:8080"} 0.0' in body
