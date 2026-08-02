"""Integration tests against a *real* Redis instance (not `fakeredis`).

Every other integration test uses `fakeredis` by default (see
`tests/integration/conftest.py`'s `running_app`), which is fast and
dependency-free but re-implements Redis's semantics rather than running the
genuine article — a subtle divergence (e.g. exact `EXPIRE`/`INCR` interaction
timing) could pass against `fakeredis` and misbehave against production
Redis. These tests connect to a real Redis (db 15, kept separate from
whatever a developer's db 0 holds) and skip cleanly if none is reachable, so
the suite still runs on a machine without Redis installed.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import pytest_asyncio
import redis.asyncio as redis_asyncio

from app.config import AACConfig
from tests.integration.conftest import make_config, running_app

pytestmark = pytest.mark.asyncio

_TEST_REDIS_URL = os.environ.get("AAC_TEST_REDIS_URL", "redis://localhost:6379/15")
# `running_app` patches `app.main.redis_asyncio.from_url` — the same module
# object as this file's `redis_asyncio` import — for the duration of the
# lifespan. Capture the real `from_url` now so `redis_factory` below calls
# the genuine function instead of recursing into its own patched self.
_real_from_url = redis_asyncio.from_url


@pytest_asyncio.fixture
async def real_redis():
    client = redis_asyncio.from_url(_TEST_REDIS_URL)
    try:
        await client.ping()
    except redis_asyncio.RedisError:
        pytest.skip(f"no real Redis reachable at {_TEST_REDIS_URL}")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


async def test_repeated_requests_increment_real_redis_counter(mock_backend, real_redis):
    """Same assertion as `test_scoring_e2e.py`'s fakeredis version, but
    proving `RedisPenaltyStore`'s real `INCR` behaves as assumed against
    genuine Redis, not just its `fakeredis` stand-in."""
    config = make_config(mock_backend)

    async with running_app(
        config, redis_factory=lambda: _real_from_url(_TEST_REDIS_URL)
    ) as (_app, client):
        for _ in range(5):
            response = await client.get("/proxytest/echo")
            assert response.status_code == 200

    count = await real_redis.get("rl:ip:127.0.0.1:mock-backend:60")
    assert count is not None
    assert int(count) == 5


async def test_penalty_counter_resets_after_real_ttl_expires(mock_backend, real_redis):
    """`RedisPenaltyStore.increment_and_get()` only sets `EXPIRE` on a
    key's first hit in a window (`app/penalty_store.py`) — real `EXPIRE`
    timing, not `fakeredis`'s approximation of it, is exactly the kind of
    thing worth confirming against genuine Redis."""
    # `make_config` hardcodes a 60s window via `_MINIMAL_SCORING`; this test
    # needs a short one, so it patches the built config's dump before
    # re-validating rather than hand-duplicating the whole scoring block.
    raw = make_config(mock_backend).model_dump(mode="json")
    for dim in raw["scoring"]["default_penalties"]:
        raw["scoring"]["default_penalties"][dim][0]["window_seconds"] = 1
    config = AACConfig.model_validate(raw)

    async with running_app(
        config, redis_factory=lambda: _real_from_url(_TEST_REDIS_URL)
    ) as (_app, client):
        await client.get("/proxytest/echo")
        count_before = await real_redis.get("rl:ip:127.0.0.1:mock-backend:1")
        assert int(count_before) == 1

        await asyncio.sleep(1.5)

        count_after = await real_redis.get("rl:ip:127.0.0.1:mock-backend:1")
        assert count_after is None

        await client.get("/proxytest/echo")
        count_new_window = await real_redis.get("rl:ip:127.0.0.1:mock-backend:1")
        assert int(count_new_window) == 1
