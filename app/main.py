"""FastAPI app factory + lifespan.

Phase 1's request lifecycle, precisely: TrustedProxyMiddleware (403, or set
`request.state.client_ip`) -> registry.match() (404 on miss, FR-011c) ->
dispatcher.dispatch() awaited directly inline in the handler coroutine. No
queueing, no capacity gating, no scoring — those are Phase 2/3.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import AACConfig, Settings, load_config
from app.dispatcher import BackendDispatcher
from app.health import routes as health_routes
from app.ingress import TrustedProxyMiddleware
from app.metrics import routes as metrics_routes
from app.registry import BackendPolicyRegistry

logger = logging.getLogger(__name__)


def _build_lifespan(preloaded_config: AACConfig | None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = Settings()

        if preloaded_config is not None:
            config = preloaded_config
        else:
            try:
                config = load_config(settings.config_path)
            except Exception:
                # FR-083: fail to start (non-zero exit) on invalid/incomplete
                # config, rather than relying on uvicorn's default behavior
                # for an exception raised inside lifespan.
                logger.exception("config_load_failed")
                os._exit(1)

        app.state.config = config
        app.state.registry = BackendPolicyRegistry(config)
        app.state.dispatchers = {
            backend.name: BackendDispatcher(backend) for backend in config.backends
        }
        app.state.redis = redis_asyncio.from_url(settings.redis_url)
        app.state.ready = True

        try:
            yield
        finally:
            await app.state.redis.aclose()
            for dispatcher in app.state.dispatchers.values():
                await dispatcher.aclose()

    return lifespan


async def proxy_handler(request: Request):
    policy = request.app.state.registry.match(request.url.path)
    if policy is None:
        return JSONResponse({"detail": "not found"}, status_code=404)  # FR-011c

    dispatcher = request.app.state.dispatchers[policy.config.name]
    return await dispatcher.dispatch(request)


def create_app(config: AACConfig | None = None) -> FastAPI:
    """`config`: an already-parsed `AACConfig`, for tests that want to skip
    writing a temp YAML file. Production/`uvicorn app.main:create_app
    --factory` always passes None, loading from `Settings().config_path`.
    """
    app = FastAPI(lifespan=_build_lifespan(config))
    app.add_middleware(TrustedProxyMiddleware)

    # Health/metrics routes registered before the catch-all proxy route so
    # they're matched first.
    for route in health_routes:
        app.router.routes.append(route)
    for route in metrics_routes:
        app.router.routes.append(route)

    app.add_api_route(
        "/{path:path}",
        proxy_handler,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    return app
