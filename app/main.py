"""FastAPI app factory + lifespan.

Request lifecycle: TrustedProxyMiddleware (403, or set
`request.state.client_ip`) -> registry.match() (404 on miss, FR-011c) ->
classify() -> score_engine.score() (sets `ctx.score`, must run before
enqueue — `PriorityScheduler.enqueue()` reads it at enqueue time) ->
scheduler.enqueue() (429 on QueueFullError/QueueWaitExceededError) -> await
the resulting Future, bounded by queue_timeout_seconds (503 on timeout) ->
the Response a background worker resolved it with.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.auth import JWTVerifier
from app.capacity import FixedController
from app.config import AACConfig, Settings, load_config, resolve_scoring_config
from app.dispatcher import BackendDispatcher
from app.errors import QueueFullError, QueueWaitExceededError
from app.geoip import GeoIPLookup
from app.health import routes as health_routes
from app.ingress import TrustedProxyMiddleware
from app.metrics import routes as metrics_routes
from app.penalty_store import RedisPenaltyStore
from app.registry import BackendPolicyRegistry
from app.scheduler import PriorityScheduler
from app.scoring import ScoreEngine

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
        app.state.redis = redis_asyncio.from_url(settings.redis_url)
        app.state.geoip = GeoIPLookup(config.geoip.city_db_path, config.geoip.asn_db_path)
        app.state.auth = JWTVerifier(config.auth)
        await app.state.auth.initial_fetch()
        app.state.penalty_store = RedisPenaltyStore(app.state.redis)
        app.state.score_engine = ScoreEngine(
            app.state.penalty_store,
            {
                backend.name: resolve_scoring_config(config.scoring, backend.name)
                for backend in config.backends
            },
        )
        app.state.registry = BackendPolicyRegistry(config, app.state.geoip, app.state.auth)
        app.state.dispatchers = {
            backend.name: BackendDispatcher(backend) for backend in config.backends
        }
        app.state.controllers = {}
        app.state.schedulers = {}
        worker_tasks = [asyncio.create_task(app.state.auth.refresh_loop())]
        for backend in config.backends:
            # Phase 2 only implements the fixed controller. `adaptive`
            # backends get a temporary non-adapting FixedController sized at
            # initial_concurrency, so the queue/capacity pipeline is uniform
            # across all backends (FR-047) — Phase 4 replaces this stand-in
            # with real adaptive concurrency adjustment.
            limit = (
                backend.concurrency_limit
                if backend.controller == "fixed"
                else backend.initial_concurrency
            )
            controller = FixedController(limit)
            scheduler = PriorityScheduler(
                backend.queue_max_size, backend.queue_timeout_seconds, controller
            )
            app.state.controllers[backend.name] = controller
            app.state.schedulers[backend.name] = scheduler
            worker_tasks.append(
                asyncio.create_task(
                    scheduler.run_worker(controller, app.state.dispatchers[backend.name])
                )
            )
        app.state.ready = True

        try:
            yield
        finally:
            for task in worker_tasks:
                task.cancel()
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            app.state.geoip.close()
            await app.state.redis.aclose()
            for dispatcher in app.state.dispatchers.values():
                await dispatcher.aclose()

    return lifespan


async def proxy_handler(request: Request):
    policy = request.app.state.registry.match(request.url.path)
    if policy is None:
        return JSONResponse({"detail": "not found"}, status_code=404)  # FR-011c

    backend_name = policy.config.name
    ctx = policy.classify(request)
    ctx.score = await request.app.state.score_engine.score(ctx)
    scheduler = request.app.state.schedulers[backend_name]

    try:
        future = await scheduler.enqueue(request, ctx)
    except QueueWaitExceededError:
        return JSONResponse(
            {"detail": "too many requests", "reason": "queue_wait_exceeded"}, status_code=429
        )
    except QueueFullError:
        return JSONResponse(
            {"detail": "too many requests", "reason": "queue_full"}, status_code=429
        )

    try:
        return await asyncio.wait_for(future, timeout=policy.config.queue_timeout_seconds)
    except TimeoutError:
        return JSONResponse(
            {"detail": "service unavailable", "reason": "queue_timeout"}, status_code=503
        )


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
