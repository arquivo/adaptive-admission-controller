"""Administrative read-only API (`docs/implementation_plan.md` §6.3, FR-084).

GET-only for MVP — no runtime hot-reload (`docs/decision_log.md` B3). Every
route requires a bearer token matching `Settings().admin_api_token`
(`AAC_ADMIN_API_TOKEN`, FR-081/FR-084): if no token is configured, admin
routes fail closed (403) rather than defaulting to open access, since this
surface exposes internal backend policy detail.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


def _check_auth(request: Request) -> JSONResponse | None:
    token = request.app.state.admin_api_token
    if not token:
        return JSONResponse({"detail": "admin API disabled"}, status_code=403)

    header = request.headers.get("authorization", "")
    scheme, _, credential = header.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(credential, token):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return None


def _require_admin_auth(
    handler: Callable[[Request], Awaitable[Response]],
) -> Callable[[Request], Awaitable[Response]]:
    async def wrapped(request: Request) -> Response:
        denied = _check_auth(request)
        if denied is not None:
            return denied
        return await handler(request)

    return wrapped


def _backend_summary(name: str, request: Request) -> dict:
    policy = request.app.state.registry.all_policies()[name]
    controller = request.app.state.controllers[name]
    scheduler = request.app.state.schedulers[name]
    upstreams = request.app.state.load_balancers[name].snapshot()
    return {
        "name": name,
        "path_prefix": policy.config.match.path_prefix,
        "controller_type": policy.config.controller,
        "current_limit": controller.current_limit(),
        "mean_latency_ms": controller.mean_latency_ms(),
        "queue_size": scheduler.queue_size(),
        "upstream_count": len(upstreams),
        "healthy_upstream_count": sum(1 for u in upstreams if u.healthy),
    }


@_require_admin_auth
async def list_backends(request: Request) -> Response:
    policies = request.app.state.registry.all_policies()
    return JSONResponse(
        {"backends": [_backend_summary(name, request) for name in policies]}
    )


@_require_admin_auth
async def backend_policy(request: Request) -> Response:
    name = request.path_params["name"]
    policies = request.app.state.registry.all_policies()
    if name not in policies:
        return JSONResponse({"detail": "unknown backend"}, status_code=404)
    policy = policies[name]
    return JSONResponse(
        {
            "config": policy.config.model_dump(mode="json"),
            "resolved_scoring": policy.resolved_scoring.model_dump(),
        }
    )


@_require_admin_auth
async def backend_limit(request: Request) -> Response:
    name = request.path_params["name"]
    controllers = request.app.state.controllers
    if name not in controllers:
        return JSONResponse({"detail": "unknown backend"}, status_code=404)
    return JSONResponse({"backend": name, "current_limit": controllers[name].current_limit()})


@_require_admin_auth
async def backend_upstreams(request: Request) -> Response:
    name = request.path_params["name"]
    policies = request.app.state.registry.all_policies()
    if name not in policies:
        return JSONResponse({"detail": "unknown backend"}, status_code=404)
    load_balancer = request.app.state.load_balancers[name]
    return JSONResponse(
        {
            "backend": name,
            "sticky_sessions": policies[name].config.sticky_sessions,
            "upstreams": [
                {
                    "url": status.url,
                    "healthy": status.healthy,
                    "in_flight": status.in_flight,
                    "sticky_count": status.sticky_count,
                    "is_backup": status.is_backup,
                }
                for status in load_balancer.snapshot()
            ],
        }
    )


routes = [
    Route("/admin/backends", list_backends, methods=["GET"]),
    Route("/admin/backends/{name}/policy", backend_policy, methods=["GET"]),
    Route("/admin/backends/{name}/limit", backend_limit, methods=["GET"]),
    Route("/admin/backends/{name}/upstreams", backend_upstreams, methods=["GET"]),
]
