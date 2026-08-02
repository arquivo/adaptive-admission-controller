"""Real-socket mock upstream backend for integration tests.

`BackendDispatcher` always speaks real HTTP (via `httpx.AsyncClient`) to
`upstream_url`, regardless of how the AAC app itself is driven in tests — so
the mock backend must be an actual running server, not an ASGI-mounted
double. Anything less would paper over a regression to full response
buffering (FR-054) instead of proving genuine socket-level streaming.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import socket
from contextlib import asynccontextmanager
from unittest.mock import patch

import fakeredis
import httpx
import pytest
import pytest_asyncio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.config import AACConfig
from app.main import create_app

STREAM_CHUNK = b"0123456789abcdef" * 1024  # 16 KiB
STREAM_CHUNK_COUNT = 640  # ~10 MiB total, enough to span many TCP segments


async def _echo(request: Request, counter: dict[str, int] | None = None) -> JSONResponse:
    if counter is not None:
        counter["calls"] = counter.get("calls", 0) + 1
    body = await request.body()
    return JSONResponse(
        {
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "headers": dict(request.headers),
            "body_length": len(body),
        }
    )


async def _stream_down(request: Request) -> StreamingResponse:
    async def gen():
        for _ in range(STREAM_CHUNK_COUNT):
            yield STREAM_CHUNK

    return StreamingResponse(gen(), media_type="application/octet-stream")


async def _stream_up(request: Request) -> JSONResponse:
    hasher = hashlib.sha256()
    total = 0
    async for chunk in request.stream():
        hasher.update(chunk)
        total += len(chunk)
    return JSONResponse({"sha256": hasher.hexdigest(), "byte_count": total})


def _make_slow_handler(concurrency: dict[str, int]):
    """`concurrency` is a shared mutable counter (`{"current": ..., "max":
    ...}`) so tests can assert on peak in-flight overlap at the *upstream*,
    independently of what the AAC observed — the only way to prove the AAC's
    capacity gate genuinely serializes dispatch rather than just delaying it.
    """

    async def _slow(request: Request) -> JSONResponse:
        delay_seconds = float(request.query_params.get("delay", "0.2"))
        concurrency["current"] += 1
        concurrency["max"] = max(concurrency["max"], concurrency["current"])
        try:
            await asyncio.sleep(delay_seconds)
        finally:
            concurrency["current"] -= 1
        return JSONResponse({"ok": True})

    return _slow


async def _fail(request: Request) -> JSONResponse:
    return JSONResponse({"error": "simulated backend failure"}, status_code=500)


async def _multi_cookie(request: Request) -> JSONResponse:
    """Returns two distinct `Set-Cookie` headers — a headers `dict` would
    silently collapse them, so this proves `BackendDispatcher`'s
    `raw_headers`-based response construction (`app/dispatcher.py:105-108`)
    genuinely preserves repeated headers end to end, not just in a unit
    test of the dispatcher itself."""
    response = JSONResponse({"ok": True})
    response.raw_headers = [
        *response.raw_headers,
        (b"set-cookie", b"a=1"),
        (b"set-cookie", b"b=2"),
    ]
    return response


async def _dispatch_by_suffix(
    request: Request, slow_handler, counter: dict[str, int] | None = None
):
    """The AAC forwards the original (unstripped) request path upstream, so
    the mock backend routes by *suffix* rather than by a fixed path — it
    doesn't know what prefix the AAC test config used."""
    path = request.url.path
    if path.rstrip("/").endswith("/stream"):
        return await _stream_down(request)
    if path.rstrip("/").endswith("/upload"):
        return await _stream_up(request)
    if path.rstrip("/").endswith("/slow"):
        return await slow_handler(request)
    if path.rstrip("/").endswith("/fail"):
        return await _fail(request)
    if path.rstrip("/").endswith("/multi-cookie"):
        return await _multi_cookie(request)
    return await _echo(request, counter)


def _mock_backend_app(concurrency: dict[str, int]) -> Starlette:
    slow_handler = _make_slow_handler(concurrency)

    async def _route(request: Request):
        return await _dispatch_by_suffix(request, slow_handler, concurrency)

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                _route,
                methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
            ),
        ]
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def _running_mock_backend(concurrency: dict[str, int], *, port: int | None = None):
    """`port` lets a test rebind a mock backend onto a port that previously
    had nothing listening on it — used to simulate a dead instance coming
    back up, as opposed to a *new* instance appearing at a *new* address."""
    port = port if port is not None else _free_port()
    config = uvicorn.Config(
        _mock_backend_app(concurrency), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        # uvicorn.Server exposes no event/future for "sockets bound"; `started`
        # is a plain bool flipped inside `serve()`, so polling is unavoidable.
        while not server.started:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest_asyncio.fixture
async def mock_backend():
    async with _running_mock_backend({"current": 0, "max": 0}) as url:
        yield url


@pytest_asyncio.fixture
async def mock_backend_with_concurrency_counter():
    """Like `mock_backend`, but also exposes the `/slow` route's shared
    concurrency counter dict, for tests that assert on peak upstream overlap
    (`counter["max"]`)."""
    concurrency = {"current": 0, "max": 0}
    async with _running_mock_backend(concurrency) as url:
        yield url, concurrency


@pytest.fixture
def stream_chunk():
    return STREAM_CHUNK


@pytest.fixture
def stream_chunk_count():
    return STREAM_CHUNK_COUNT


_MINIMAL_SCORING = {
    "exempt_countries": [],
    "base_scores": {"anonymous": 100},
    "score_clamp": {"min": -100, "max": 100},
    "default_penalties": {
        dim: [
            {
                "window_seconds": 60,
                "soft_threshold": 50,
                "hard_threshold": 200,
                "soft_penalty": 10,
                "hard_penalty": 40,
            }
        ]
        for dim in ("ip", "net24", "net6", "asn", "country", "user")
    },
}


def make_config(
    upstream_url: str,
    *,
    path_prefix: str = "/proxytest",
    trusted_proxies: list[str] | None = None,
    concurrency_limit: int = 100,
    queue_max_size: int = 100,
    queue_timeout_seconds: float = 30,
    debug_headers_enabled: bool = False,
) -> AACConfig:
    """A minimal single-backend `AACConfig` pointed at a real (mock) upstream,
    for driving the proxy round trip end to end."""
    return AACConfig.model_validate(
        {
            "ingress": {
                "trusted_proxies": trusted_proxies or ["127.0.0.1"],
                "xff_trusted_hops": 1,
            },
            "geoip": {
                "city_db_path": "/nonexistent/GeoLite2-City.mmdb",
                "asn_db_path": "/nonexistent/GeoLite2-ASN.mmdb",
            },
            "observability": {"debug_headers": {"enabled": debug_headers_enabled}},
            "scoring": copy.deepcopy(_MINIMAL_SCORING),
            "backends": [
                {
                    "name": "mock-backend",
                    "upstreams": [{"url": upstream_url}],
                    "match": {"path_prefix": path_prefix},
                    "controller": "fixed",
                    "concurrency_limit": concurrency_limit,
                    "connect_timeout_seconds": 5,
                    "backend_timeout_seconds": 30,
                    "queue_max_size": queue_max_size,
                    "queue_timeout_seconds": queue_timeout_seconds,
                }
            ],
        }
    )


def make_multi_backend_config(
    backends: list[dict],
    *,
    trusted_proxies: list[str] | None = None,
    debug_headers_enabled: bool = False,
) -> AACConfig:
    """Like `make_config`, but builds an arbitrary number of backends from a
    list of plain dicts, needed for isolation tests that must prove one
    backend's saturation/adjustment doesn't affect a sibling backend.

    Each dict requires `name`/`path_prefix` plus either `upstream_url` (single
    instance) or `upstream_urls` (multi-instance list) — exactly one of the
    two — and either `concurrency_limit` (fixed controller, the default) or
    the full adaptive field set (`controller: "adaptive"` +
    `min_concurrency`/`initial_concurrency`/`max_concurrency`/
    `target_p95_ms`/`timeout_rate_threshold`/`error_rate_threshold`). An
    optional `backup_upstream_urls` list configures the backend's failover
    pool. An optional `health_check_interval_seconds` overrides the default
    10s active-health-check cadence — useful for tests that need to observe
    a recovered instance without a real 10s sleep. Queue/timeout fields fall
    back to the same defaults as `make_config`.
    """
    backend_configs = []
    for b in backends:
        if ("upstream_url" in b) == ("upstream_urls" in b):
            raise ValueError("exactly one of `upstream_url`/`upstream_urls` is required")
        urls = b["upstream_urls"] if "upstream_urls" in b else [b["upstream_url"]]
        entry = {
            "name": b["name"],
            "upstreams": [{"url": url} for url in urls],
            "match": {"path_prefix": b["path_prefix"]},
            "connect_timeout_seconds": b.get("connect_timeout_seconds", 5),
            "backend_timeout_seconds": b.get("backend_timeout_seconds", 30),
            "queue_max_size": b.get("queue_max_size", 100),
            "queue_timeout_seconds": b.get("queue_timeout_seconds", 30),
            "controller": b.get("controller", "fixed"),
        }
        if "backup_upstream_urls" in b:
            entry["backup_upstreams"] = [{"url": url} for url in b["backup_upstream_urls"]]
        if "health_check_interval_seconds" in b:
            entry["health_check_interval_seconds"] = b["health_check_interval_seconds"]
        if "sticky_sessions" in b:
            entry["sticky_sessions"] = b["sticky_sessions"]
        if "sticky_session_ttl_seconds" in b:
            entry["sticky_session_ttl_seconds"] = b["sticky_session_ttl_seconds"]
        if entry["controller"] == "fixed":
            entry["concurrency_limit"] = b.get("concurrency_limit", 100)
        else:
            for field in (
                "min_concurrency",
                "initial_concurrency",
                "max_concurrency",
                "target_p95_ms",
                "timeout_rate_threshold",
                "error_rate_threshold",
            ):
                entry[field] = b[field]
        backend_configs.append(entry)

    return AACConfig.model_validate(
        {
            "ingress": {
                "trusted_proxies": trusted_proxies or ["127.0.0.1"],
                "xff_trusted_hops": 1,
            },
            "geoip": {
                "city_db_path": "/nonexistent/GeoLite2-City.mmdb",
                "asn_db_path": "/nonexistent/GeoLite2-ASN.mmdb",
            },
            "observability": {"debug_headers": {"enabled": debug_headers_enabled}},
            "scoring": copy.deepcopy(_MINIMAL_SCORING),
            "backends": backend_configs,
        }
    )


@asynccontextmanager
async def running_app(
    config: AACConfig,
    *,
    client_peer: tuple[str, int] = ("127.0.0.1", 12345),
    redis_factory=fakeredis.FakeAsyncRedis,
):
    """Runs `create_app(config)` through its real lifespan (dispatchers,
    redis client) and yields an `httpx.AsyncClient` talking to it over
    `httpx.ASGITransport` — no real socket needed for the AAC side itself,
    only for the upstream mock backend (see module docstring).

    Defaults `app.state.redis` to an in-memory `fakeredis` client — every
    request now scores via `RedisPenaltyStore` (Phase 3), so a real Redis
    would otherwise be required for every test in this module. Pass
    `redis_factory` to substitute a different double (e.g. one that
    simulates an unreachable Redis for `/readyz` tests).
    """
    app = create_app(config=config)
    with patch("app.main.redis_asyncio.from_url", lambda *_a, **_k: redis_factory()):
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=client_peer)
            base_url = "http://testserver"
            async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
                yield app, client
