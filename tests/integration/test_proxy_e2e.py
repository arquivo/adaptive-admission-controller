"""Phase 1 proxy round-trip integration tests.

Drives the real `create_app()` FastAPI app (full lifespan: dispatchers,
registry, redis client) via `httpx.ASGITransport`, against a real-socket
mock upstream backend (see `conftest.py`).
"""

from __future__ import annotations

import hashlib

import fakeredis
import pytest

from tests.integration.conftest import _free_port, make_config, running_app

pytestmark = pytest.mark.asyncio


async def test_matched_prefix_round_trip_preserves_path_query_and_headers(mock_backend):
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get(
            "/proxytest/echo?foo=bar&baz=qux",
            headers={
                "X-Test-Echo": "hello",
                # A sentinel value distinct from httpx's own default
                # ("keep-alive") for its outbound hop — proves this specific
                # value from the client->AAC hop was stripped rather than
                # merely coincidentally overwritten by httpx.
                "Connection": "this-value-must-not-survive",
                "TE": "trailers",  # hop-by-hop, must not reach upstream at all
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "GET"
    assert body["path"] == "/proxytest/echo"
    assert body["query"] == "foo=bar&baz=qux"
    assert body["headers"].get("x-test-echo") == "hello"
    # httpx itself sets a `Connection` header for its own outbound hop to the
    # upstream (correct RFC 7230 §6.1 behavior — each hop owns this header),
    # so its mere presence isn't a leak; only the *value* proves the client's
    # original header wasn't forwarded verbatim.
    assert body["headers"].get("connection") != "this-value-must-not-survive"
    assert "te" not in body["headers"]
    # Host must be recomputed for the new hop, not forwarded from the
    # original request (which had host "testserver").
    assert body["headers"]["host"] != "testserver"


async def test_unprefixed_subpath_forwards_unchanged(mock_backend):
    """Confirms the documented assumption: the AAC forwards the original
    path unchanged (no prefix stripping) — matches a drop-in Apache
    `ProxyPass` replacement."""
    config = make_config(mock_backend, path_prefix="/proxytest")
    async with running_app(config) as (_app, client):
        response = await client.get("/proxytest/echo/nested/thing")

    assert response.status_code == 200
    assert response.json()["path"] == "/proxytest/echo/nested/thing"


async def test_streamed_large_response_round_trips_correctly(
    mock_backend, stream_chunk, stream_chunk_count
):
    config = make_config(mock_backend)
    expected_total = len(stream_chunk) * stream_chunk_count

    async with running_app(config) as (_app, client):
        async with client.stream("GET", "/proxytest/stream") as response:
            assert response.status_code == 200
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
        assert total == expected_total


async def test_streamed_post_upload_hash_matches(mock_backend):
    config = make_config(mock_backend)
    payload_chunk = b"upload-payload-chunk-" * 512  # ~11 KiB
    chunk_count = 200  # ~2.2 MiB total

    expected_hasher = hashlib.sha256()
    for _ in range(chunk_count):
        expected_hasher.update(payload_chunk)

    async def body_stream():
        for _ in range(chunk_count):
            yield payload_chunk

    async with running_app(config) as (_app, client):
        response = await client.post("/proxytest/upload", content=body_stream())

    assert response.status_code == 200
    result = response.json()
    assert result["sha256"] == expected_hasher.hexdigest()
    assert result["byte_count"] == len(payload_chunk) * chunk_count


async def test_unmatched_path_returns_404(mock_backend):
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get("/does-not-exist")

    assert response.status_code == 404


async def test_unreachable_backend_returns_502():
    unreachable_url = f"http://127.0.0.1:{_free_port()}"
    config = make_config(unreachable_url)
    async with running_app(config) as (_app, client):
        response = await client.get("/proxytest/echo")

    assert response.status_code == 502



async def test_untrusted_peer_rejected_403(mock_backend):
    config = make_config(mock_backend, trusted_proxies=["127.0.0.1"])
    async with running_app(config, client_peer=("10.0.0.9", 1)) as (_app, client):
        response = await client.get("/proxytest/echo")

    assert response.status_code == 403


async def test_health_and_ready_endpoints_exempt_from_trusted_proxy_check(
    mock_backend, monkeypatch
):
    monkeypatch.setattr(
        "app.main.redis_asyncio.from_url", lambda *_a, **_k: fakeredis.FakeAsyncRedis()
    )
    config = make_config(mock_backend, trusted_proxies=["127.0.0.1"])
    async with running_app(config, client_peer=("10.0.0.9", 1)) as (_app, client):
        healthz_response = await client.get("/healthz")
        readyz_response = await client.get("/readyz")

    assert healthz_response.status_code == 200
    assert readyz_response.status_code == 200


async def test_readyz_ready_when_redis_reachable(mock_backend, monkeypatch):
    monkeypatch.setattr(
        "app.main.redis_asyncio.from_url", lambda *_a, **_k: fakeredis.FakeAsyncRedis()
    )
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_readyz_not_ready_when_redis_unreachable(mock_backend, monkeypatch):
    class _UnreachableRedis:
        async def ping(self):
            raise ConnectionError("simulated redis outage")

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "app.main.redis_asyncio.from_url", lambda *_a, **_k: _UnreachableRedis()
    )
    config = make_config(mock_backend)
    async with running_app(config) as (_app, client):
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["reason"] == "redis_unreachable"
