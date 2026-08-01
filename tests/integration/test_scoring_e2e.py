"""Integration test proving the scoring engine is wired end to end through
the real request lifecycle (`docs/implementation_plan.md` §4): repeated
requests from the same client IP must accumulate against the same Redis
counter `RedisPenaltyStore` maintains, via `ScoreEngine.score()` called in
`proxy_handler` before `scheduler.enqueue()`.

No diagnostic response header exists yet to observe `ctx.score` directly
over the wire (`X-AAC-Score` is FR-074/D2, Phase 5) — so this asserts on the
`fakeredis` counter state (`app.state.redis`, the same client
`RedisPenaltyStore` incremented) reachable after `running_app`'s lifespan
has started.
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import make_config, running_app

pytestmark = pytest.mark.asyncio


async def test_repeated_requests_from_same_ip_increment_redis_penalty_counter(mock_backend):
    config = make_config(mock_backend)

    async with running_app(config) as (app, client):
        for _ in range(5):
            response = await client.get("/proxytest/echo")
            assert response.status_code == 200

        # window_seconds=60 for every dimension in conftest's _MINIMAL_SCORING.
        count = await app.state.redis.get("rl:ip:127.0.0.1:mock-backend:60")

    assert count is not None
    assert int(count) == 5


async def test_distinct_client_ips_get_independent_counters(mock_backend):
    # Both peer IPs must be trusted proxies, or the untrusted one gets a 403
    # before classification/scoring ever runs (app/ingress.py).
    config = make_config(mock_backend, trusted_proxies=["127.0.0.1", "127.0.0.2"])

    async with running_app(config, client_peer=("127.0.0.1", 1)) as (app, client_a):
        await client_a.get("/proxytest/echo")
        await client_a.get("/proxytest/echo")

    async with running_app(config, client_peer=("127.0.0.2", 1)) as (app, client_b):
        await client_b.get("/proxytest/echo")

        count_a = await app.state.redis.get("rl:ip:127.0.0.1:mock-backend:60")
        count_b = await app.state.redis.get("rl:ip:127.0.0.2:mock-backend:60")

    # Each `running_app` gets its own fresh fakeredis instance, so
    # 127.0.0.1's counter from the first block is gone here — this proves
    # the *key* is IP-scoped (127.0.0.2's own count is exactly 1, unaffected
    # by whatever 127.0.0.1 accumulated), not that state persists across app
    # instances.
    assert count_a is None
    assert int(count_b) == 1
