"""Prometheus metrics setup (Phase 1 stub).

Only wires up the `/metrics` endpoint against the default registry. The
full metric set from requirements.md §6.8 (admission_inflight_requests,
admission_rejected_total, etc.) is registered in Phase 5 — nothing on the
Phase 1 request path increments a counter yet.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route


async def metrics_endpoint(request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


routes = [Route("/metrics", metrics_endpoint, methods=["GET"])]
