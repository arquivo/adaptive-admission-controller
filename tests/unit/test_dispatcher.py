"""Unit tests for app.dispatcher.BackendDispatcher — direct coverage of
dispatch_queued's exception handling. The critical regression here is
exception-ordering: httpx.ConnectTimeout is a subclass of *both*
httpx.TimeoutException and httpx.ConnectError, so the connect-failure branch
must be tried first or it silently never fires."""

from __future__ import annotations

import asyncio

import httpx
from starlette.requests import Request

from app.capacity import FixedController
from app.config import FixedBackendConfig
from app.dispatcher import BackendDispatcher
from app.interfaces import RequestContext
from app.load_balancer import LeastLoadedLoadBalancer


def _config() -> FixedBackendConfig:
    return FixedBackendConfig(
        name="test-backend",
        upstreams=[{"url": "http://test-backend:8080"}],
        match={"path_prefix": "/test"},
        connect_timeout_seconds=5,
        backend_timeout_seconds=60,
        queue_max_size=100,
        queue_timeout_seconds=300,
        controller="fixed",
        concurrency_limit=10,
    )


def _request() -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/test/x",
        "query_string": b"",
        "headers": [],
    }
    return Request(scope)


def _ctx() -> RequestContext:
    return RequestContext(backend="test-backend", path="/test/x", method="GET", arrival_time=0.0)


async def _dispatch_with_send_error(monkeypatch, exc: Exception):
    config = _config()
    load_balancer = LeastLoadedLoadBalancer([str(config.upstreams[0].url)])
    dispatcher = BackendDispatcher(config, load_balancer)

    async def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(dispatcher._client, "send", _raise)

    controller = FixedController(limit=10)
    await controller.acquire(1)
    future = asyncio.get_event_loop().create_future()
    await dispatcher.dispatch_queued(_request(), _ctx(), future, controller)
    await dispatcher.aclose()
    return load_balancer, future.result()


async def test_connect_timeout_marks_instance_down_and_returns_502(monkeypatch):
    load_balancer, response = await _dispatch_with_send_error(
        monkeypatch, httpx.ConnectTimeout("connect timed out")
    )
    assert response.status_code == 502
    assert load_balancer.snapshot()[0].healthy is False


async def test_connect_error_marks_instance_down_and_returns_502(monkeypatch):
    load_balancer, response = await _dispatch_with_send_error(
        monkeypatch, httpx.ConnectError("connection refused")
    )
    assert response.status_code == 502
    assert load_balancer.snapshot()[0].healthy is False


async def test_read_timeout_does_not_mark_instance_down_and_returns_503(monkeypatch):
    load_balancer, response = await _dispatch_with_send_error(
        monkeypatch, httpx.ReadTimeout("read timed out")
    )
    assert response.status_code == 503
    assert load_balancer.snapshot()[0].healthy is True
