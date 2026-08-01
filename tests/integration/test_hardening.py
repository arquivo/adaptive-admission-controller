"""Phase 6 (`docs/implementation_plan.md` §7) integration hardening tests:
multi-backend isolation, adaptive-controller reaction to a real error burst,
Redis-outage fail-open admission, and client-supplied-header immunity.

Real staging backends and a genuine 500-concurrent-client load test are out
of scope here — they require infrastructure this sandbox doesn't have (see
`docs/implementation_plan.md` §7 and the project memory note on Phase 6
scope). `scripts/load_test.py` is the runnable tool for that; this file
covers everything that can be proven against the existing mock-backend
harness.
"""

from __future__ import annotations

import asyncio

from tests.integration.conftest import make_config, make_multi_backend_config, running_app


async def test_saturated_backend_does_not_block_sibling_backend(
    mock_backend_with_concurrency_counter,
):
    """Two backends, one concurrency-limit-of-1 and blocked mid-request on
    `/slow`, the other free — the second backend's own request must not wait
    on the first's queue/capacity gate at all."""
    url, _concurrency = mock_backend_with_concurrency_counter
    config = make_multi_backend_config(
        [
            {
                "name": "slow-backend",
                "upstream_url": url,
                "path_prefix": "/slow-svc",
                "concurrency_limit": 1,
                "queue_max_size": 5,
                "queue_timeout_seconds": 5,
            },
            {
                "name": "fast-backend",
                "upstream_url": url,
                "path_prefix": "/fast-svc",
                "concurrency_limit": 10,
                "queue_max_size": 5,
                "queue_timeout_seconds": 5,
            },
        ]
    )

    async with running_app(config) as (_app, client):
        slow_task = asyncio.create_task(client.get("/slow-svc/slow?delay=0.3"))
        await asyncio.sleep(0.05)  # let it actually acquire slow-backend's one slot

        fast_response = await client.get("/fast-svc/echo")
        assert fast_response.status_code == 200

        slow_response = await slow_task
        assert slow_response.status_code == 200


async def test_adaptive_controller_shrinks_limit_on_real_error_burst(mock_backend):
    """Drives real requests against the mock backend's `/fail` (500) and
    `/echo` (200) suffixes, then calls `_adjust()` directly (rather than
    waiting on the real 30s `adjust_loop()` interval) to prove the shrink
    actually fires from genuine dispatched-request outcomes, not just
    synthetic `LatencyWindow`/`RateWindow` state as in `test_capacity.py`."""
    config = make_multi_backend_config(
        [
            {
                "name": "adaptive-backend",
                "upstream_url": mock_backend,
                "path_prefix": "/adaptive-svc",
                "controller": "adaptive",
                "min_concurrency": 1,
                "initial_concurrency": 20,
                "max_concurrency": 20,
                "target_p95_ms": 50,
                "timeout_rate_threshold": 0.5,
                "error_rate_threshold": 0.2,
                "queue_max_size": 100,
                "queue_timeout_seconds": 10,
            }
        ]
    )

    async with running_app(config) as (app, client):
        controller = app.state.controllers["adaptive-backend"]

        # Seed >=10 successful-latency samples so `_adjust()` has a p95 to act on.
        for _ in range(12):
            response = await client.get("/adaptive-svc/echo")
            assert response.status_code == 200

        # Push the rolling 5xx rate (18 samples total) above the 0.2 threshold.
        for _ in range(6):
            response = await client.get("/adaptive-svc/fail")
            assert response.status_code == 500

        initial_limit = controller.current_limit()
        await controller._adjust()

        assert controller.current_limit() < initial_limit


async def test_admission_continues_when_redis_is_unreachable(mock_backend):
    """`docs/decision_log.md` A5: `/readyz` must flip to not-ready, but an
    ordinary proxied request must still be admitted and dispatched — proving
    `app/scoring.py`'s fail-open fix works at the wire level, not just in the
    `ScoreEngine` unit test."""

    class _UnreachableRedis:
        async def ping(self):
            raise ConnectionError("simulated redis outage")

        async def incr(self, *_args, **_kwargs):
            raise ConnectionError("simulated redis outage")

        async def expire(self, *_args, **_kwargs):
            raise ConnectionError("simulated redis outage")

        async def aclose(self):
            pass

    config = make_config(mock_backend)
    async with running_app(config, redis_factory=_UnreachableRedis) as (_app, client):
        readyz = await client.get("/readyz")
        assert readyz.status_code == 503

        response = await client.get("/proxytest/echo")
        assert response.status_code == 200


async def test_client_supplied_priority_header_has_no_effect_on_score(mock_backend):
    """Phase 6 §7.3: the only client-supplied header ever read for scoring
    purposes is `Authorization` (`app/classifier.py`) — a spoofed
    priority/score-looking header must be silently ignored."""
    config = make_config(mock_backend, debug_headers_enabled=True)

    async with running_app(config) as (_app, client):
        baseline = await client.get("/proxytest/echo")
        spoofed = await client.get(
            "/proxytest/echo",
            headers={"X-AAC-Score": "-100", "X-Priority": "999"},
        )

    assert baseline.status_code == 200
    assert spoofed.status_code == 200
    assert baseline.headers["X-AAC-Score"] == spoofed.headers["X-AAC-Score"]


async def test_malformed_forwarded_for_header_does_not_crash_request(mock_backend):
    """A garbage `X-Forwarded-For` value must degrade gracefully (unresolved
    client IP -> no subnet/geoip signal) rather than raise inside classify()
    /ingress and turn into a 500."""
    config = make_config(mock_backend)

    async with running_app(config) as (_app, client):
        response = await client.get(
            "/proxytest/echo", headers={"X-Forwarded-For": "not-an-ip, also garbage, ,,,"}
        )

    assert response.status_code == 200


async def test_request_stuck_in_queue_past_timeout_yields_503_queue_timeout(
    mock_backend_with_concurrency_counter,
):
    """§7.1: distinct from the immediate `429 queue_wait_exceeded`/`queue_full`
    rejections (already covered at the scheduler-unit level in
    `test_scheduler.py`) — this is a request that *is* admitted into the
    queue, but the backend ahead of it never frees the one concurrency slot
    before `queue_timeout_seconds` elapses, so it times out for real while
    waiting on its `Future`."""
    url, _concurrency = mock_backend_with_concurrency_counter
    config = make_config(
        url,
        concurrency_limit=1,
        queue_max_size=5,
        queue_timeout_seconds=0.2,
    )

    async with running_app(config) as (_app, client):
        holder_task = asyncio.create_task(client.get("/proxytest/slow?delay=2"))
        await asyncio.sleep(0.05)  # let it acquire the one concurrency slot

        queued_response = await client.get("/proxytest/echo")

        assert queued_response.status_code == 503
        assert queued_response.json()["reason"] == "queue_timeout"

        holder_task.cancel()
        try:
            await holder_task
        except asyncio.CancelledError:
            pass
