"""Phase 1 multi-instance load-balancing integration tests.

Verifies `LeastLoadedLoadBalancer` actually spreads real, over-the-wire
traffic across multiple physical upstream instances configured under one
backend name (see conftest.py's module docstring on why these use real
sockets rather than an ASGI-mounted double).
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from tests.integration.conftest import _running_mock_backend, make_multi_backend_config, running_app

pytestmark = pytest.mark.asyncio


async def test_concurrent_requests_spread_across_both_instances():
    counter_a = {"current": 0, "max": 0}
    counter_b = {"current": 0, "max": 0}
    async with _running_mock_backend(counter_a) as url_a, _running_mock_backend(counter_b) as url_b:
        config = make_multi_backend_config(
            [
                {
                    "name": "multi-backend",
                    "upstream_urls": [url_a, url_b],
                    "path_prefix": "/proxytest",
                    "concurrency_limit": 100,
                    # These concurrent requests all share one client IP —
                    # sticky sessions would pin them all to a single
                    # instance, defeating this test's purpose of proving
                    # raw least-loaded selection spreads load. That's
                    # covered separately below.
                    "sticky_sessions": False,
                }
            ]
        )
        async with running_app(config) as (_app, client):
            await asyncio.gather(
                *(client.get("/proxytest/slow?delay=0.2") for _ in range(8))
            )

    # Both real upstream sockets must have actually received traffic —
    # proves selection, not just config parsing, spread requests across
    # instances rather than piling every request onto one.
    assert counter_a["max"] > 0
    assert counter_b["max"] > 0


async def test_sticky_session_pins_client_to_same_instance_across_requests():
    counter_a = {"current": 0, "max": 0}
    counter_b = {"current": 0, "max": 0}
    async with _running_mock_backend(counter_a) as url_a, _running_mock_backend(counter_b) as url_b:
        config = make_multi_backend_config(
            [
                {
                    "name": "multi-backend",
                    "upstream_urls": [url_a, url_b],
                    "path_prefix": "/proxytest",
                    "concurrency_limit": 100,
                }
            ]
        )
        async with running_app(config) as (_app, client):
            # Sequential (not concurrent) requests from one client — sticky
            # sessions default on, and this backend is nowhere near
            # saturated, so every request must land on whichever instance
            # served the first one.
            for _ in range(6):
                response = await client.get("/proxytest/echo")
                assert response.status_code == 200

    calls = sorted([counter_a.get("calls", 0), counter_b.get("calls", 0)])
    assert calls == [0, 6]


async def test_dead_instance_marked_down_then_traffic_shifts_to_live_instance():
    # Bind then immediately release a port so nothing listens on it —
    # guarantees ECONNREFUSED rather than a real service.
    dead_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead_sock.bind(("127.0.0.1", 0))
    dead_port = dead_sock.getsockname()[1]
    dead_sock.close()
    dead_url = f"http://127.0.0.1:{dead_port}"

    async with _running_mock_backend({"current": 0, "max": 0}) as live_url:
        config = make_multi_backend_config(
            [
                {
                    "name": "multi-backend",
                    "upstream_urls": [dead_url, live_url],
                    "path_prefix": "/proxytest",
                    "concurrency_limit": 100,
                    "connect_timeout_seconds": 0.2,
                }
            ]
        )
        async with running_app(config) as (_app, client):
            # Least-loaded selection ties on in-flight count 0 for both
            # instances at startup and breaks ties by insertion order, so the
            # first request deterministically hits the dead instance first.
            first = await client.get("/proxytest/echo")
            assert first.status_code == 502

            # The dead instance is now marked down — every subsequent
            # request must land on the live instance and succeed.
            for _ in range(5):
                response = await client.get("/proxytest/echo")
                assert response.status_code == 200


async def test_all_primaries_down_traffic_fails_over_to_backup_instance():
    dead_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dead_sock.bind(("127.0.0.1", 0))
    dead_port = dead_sock.getsockname()[1]
    dead_sock.close()
    dead_url = f"http://127.0.0.1:{dead_port}"

    async with _running_mock_backend({"current": 0, "max": 0}) as backup_url:
        config = make_multi_backend_config(
            [
                {
                    "name": "multi-backend",
                    "upstream_url": dead_url,
                    "backup_upstream_urls": [backup_url],
                    "path_prefix": "/proxytest",
                    "concurrency_limit": 100,
                    "connect_timeout_seconds": 0.2,
                }
            ]
        )
        async with running_app(config) as (_app, client):
            # The only primary is dead — the first request hits it, fails,
            # and marks it down.
            first = await client.get("/proxytest/echo")
            assert first.status_code == 502

            # With no healthy primary left, traffic now fails over to the
            # backup instance instead of continuing to 502.
            second = await client.get("/proxytest/echo")
            assert second.status_code == 200

