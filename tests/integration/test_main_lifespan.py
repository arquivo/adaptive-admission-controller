"""Integration test for `app.main`'s lifespan wiring itself — drives the real
`create_app()` lifespan directly (not through `conftest.running_app`, which
patches `redis_asyncio.from_url` with a lambda that discards its arguments,
making it unsuitable for asserting what `app.main` actually passed).
"""

from __future__ import annotations

from unittest.mock import patch

import fakeredis
import pytest

from app.main import create_app
from tests.integration.conftest import make_config

pytestmark = pytest.mark.asyncio


async def test_lifespan_passes_configured_redis_max_connections(monkeypatch):
    """`app.main`'s lifespan must forward `Settings.redis_max_connections`
    to `redis_asyncio.from_url`, not silently rely on redis-py's own default
    (`app/config.py`'s `Settings.redis_max_connections`, `app/main.py`'s
    `redis_asyncio.from_url` call)."""
    monkeypatch.setenv("AAC_REDIS_MAX_CONNECTIONS", "7")
    config = make_config("http://localhost:1")  # never actually connected to
    app = create_app(config=config)

    captured: dict = {}

    def fake_from_url(*args, **kwargs):
        captured.update(kwargs)
        return fakeredis.FakeAsyncRedis()

    with patch("app.main.redis_asyncio.from_url", fake_from_url):
        async with app.router.lifespan_context(app):
            pass

    assert captured.get("max_connections") == 7
