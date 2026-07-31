"""/healthz and /readyz endpoints (FR-073).

/readyz bypasses PenaltyStore/ScoreEngine entirely (those are Phase 3) —
it only checks that config loaded successfully at startup and that Redis
is reachable via a raw ping (FR-083, FR-083a). Per-backend reachability is
explicitly excluded; a single dead backend must not pull the whole AAC out
of orchestrator rotation.
"""

from __future__ import annotations

import asyncio

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

REDIS_PING_TIMEOUT_SECONDS = 1.0


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "alive"})


async def readyz(request: Request) -> JSONResponse:
    if not getattr(request.app.state, "ready", False):
        return JSONResponse({"status": "not_ready", "reason": "config_invalid"}, status_code=503)

    try:
        await asyncio.wait_for(
            request.app.state.redis.ping(), timeout=REDIS_PING_TIMEOUT_SECONDS
        )
    except Exception:
        return JSONResponse(
            {"status": "not_ready", "reason": "redis_unreachable"}, status_code=503
        )

    return JSONResponse({"status": "ready"})


routes = [
    Route("/healthz", healthz, methods=["GET"]),
    Route("/readyz", readyz, methods=["GET"]),
]
