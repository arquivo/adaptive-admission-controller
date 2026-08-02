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
import time
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app import metrics, observability
from app.admin import routes as admin_routes
from app.auth import JWTVerifier
from app.capacity import AdaptiveController, FixedController
from app.config import AACConfig, Settings, load_config, resolve_scoring_config
from app.dispatcher import BackendDispatcher
from app.errors import QueueFullError, QueueWaitExceededError
from app.geoip import GeoIPLookup
from app.health import routes as health_routes
from app.ingress import TrustedProxyMiddleware
from app.load_balancer import LeastLoadedLoadBalancer
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
        observability.configure_logging(settings.log_level)

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
        app.state.admin_api_token = settings.admin_api_token
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

        # Controllers are built before load balancers so each backend's
        # `LeastLoadedLoadBalancer` can be given a `capacity_hint` bound to
        # its own controller's `current_limit` (used for sticky-session
        # fair-share eviction) without a circular construction order.
        app.state.controllers = {}
        for backend in config.backends:
            if backend.controller == "fixed":
                controller = FixedController(backend.concurrency_limit)
                metrics.concurrency_limit.labels(backend.name).set(backend.concurrency_limit)
            else:
                controller = AdaptiveController(backend)
                metrics.concurrency_limit.labels(backend.name).set(backend.initial_concurrency)
            app.state.controllers[backend.name] = controller

        app.state.load_balancers = {
            backend.name: LeastLoadedLoadBalancer(
                [str(u.url) for u in backend.upstreams],
                backup_urls=[str(u.url) for u in backend.backup_upstreams],
                connect_timeout_seconds=backend.connect_timeout_seconds,
                health_check_interval_seconds=backend.health_check_interval_seconds,
                sticky_enabled=backend.sticky_sessions,
                sticky_ttl_seconds=backend.sticky_session_ttl_seconds,
                capacity_hint=app.state.controllers[backend.name].current_limit,
            )
            for backend in config.backends
        }
        app.state.dispatchers = {
            backend.name: BackendDispatcher(backend, app.state.load_balancers[backend.name])
            for backend in config.backends
        }
        app.state.schedulers = {}
        worker_tasks = [asyncio.create_task(app.state.auth.refresh_loop())]
        for load_balancer in app.state.load_balancers.values():
            worker_tasks.append(asyncio.create_task(load_balancer.health_check_loop()))
        for backend in config.backends:
            controller = app.state.controllers[backend.name]
            if backend.controller == "adaptive":
                worker_tasks.append(asyncio.create_task(controller.adjust_loop()))
            scheduler = PriorityScheduler(
                backend.name, backend.queue_max_size, backend.queue_timeout_seconds, controller
            )
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
    exempt_label = "true" if ctx.score_breakdown and ctx.score_breakdown.is_exempt else "false"
    class_label = ctx.user_class or "unknown"
    debug_enabled = request.app.state.config.observability.debug_headers.enabled

    def _finalize(response: Response, *, reject_reason: str | None = None) -> Response:
        if debug_enabled:
            response.headers["X-AAC-Backend"] = backend_name
            response.headers["X-AAC-Score"] = str(ctx.score)
            response.headers["X-AAC-Exempt"] = exempt_label
            if reject_reason is not None:
                response.headers["X-AAC-Reject-Reason"] = reject_reason
        return response

    metrics.requests_total.labels(backend_name, class_label, exempt_label).inc()
    metrics.score_distribution.labels(backend_name, exempt_label).observe(ctx.score)

    scheduler = request.app.state.schedulers[backend_name]
    try:
        future = await scheduler.enqueue(request, ctx)
    except QueueWaitExceededError:
        metrics.rejected_total.labels(
            backend_name, class_label, "queue_wait_exceeded", exempt_label
        ).inc()
        observability.log_admission_event(
            "rejected", ctx, reason="queue_wait_exceeded", status_code=429
        )
        return _finalize(
            JSONResponse(
                {"detail": "too many requests", "reason": "queue_wait_exceeded"}, status_code=429
            ),
            reject_reason="queue_wait_exceeded",
        )
    except QueueFullError:
        metrics.rejected_total.labels(backend_name, class_label, "queue_full", exempt_label).inc()
        observability.log_admission_event("rejected", ctx, reason="queue_full", status_code=429)
        return _finalize(
            JSONResponse({"detail": "too many requests", "reason": "queue_full"}, status_code=429),
            reject_reason="queue_full",
        )

    metrics.inflight_requests.labels(backend_name).inc()
    metrics.inflight_tokens.labels(backend_name).inc(ctx.cost)
    try:
        response = await asyncio.wait_for(future, timeout=policy.config.queue_timeout_seconds)
    except TimeoutError:
        metrics.queue_timeout_total.labels(backend_name).inc()
        metrics.rejected_total.labels(
            backend_name, class_label, "queue_timeout", exempt_label
        ).inc()
        observability.log_admission_event(
            "rejected",
            ctx,
            reason="queue_timeout",
            queue_wait_ms=(time.monotonic() - ctx.arrival_time) * 1000,
            status_code=503,
        )
        return _finalize(
            JSONResponse(
                {"detail": "service unavailable", "reason": "queue_timeout"}, status_code=503
            ),
            reject_reason="queue_timeout",
        )
    else:
        metrics.admitted_total.labels(backend_name, class_label, exempt_label).inc()
        return _finalize(response)
    finally:
        metrics.inflight_requests.labels(backend_name).dec()
        metrics.inflight_tokens.labels(backend_name).dec(ctx.cost)


def create_app(config: AACConfig | None = None) -> FastAPI:
    """`config`: an already-parsed `AACConfig`, for tests that want to skip
    writing a temp YAML file. Production/`uvicorn app.main:create_app
    --factory` always passes None, loading from `Settings().config_path`.
    """
    app = FastAPI(lifespan=_build_lifespan(config))
    app.add_middleware(TrustedProxyMiddleware)

    # Health/metrics/admin routes registered before the catch-all proxy
    # route so they're matched first.
    for route in health_routes:
        app.router.routes.append(route)
    for route in metrics_routes:
        app.router.routes.append(route)
    for route in admin_routes:
        app.router.routes.append(route)

    app.add_api_route(
        "/{path:path}",
        proxy_handler,
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    return app

