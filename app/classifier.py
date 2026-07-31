"""Request classification (Phase 1 stub).

Builds the `RequestContext` a `BackendPolicy` attaches to a request. Real
classification — user_class from auth, subnet/ASN/country from GeoIP — lands
in Phase 3 (docs/implementation_plan.md §4.1). Phase 1's request path does
NOT call this at all; it exists as forward-compatible scaffolding.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.interfaces import RequestContext

if TYPE_CHECKING:
    from starlette.requests import Request

    from app.registry import DefaultBackendPolicy


def classify(request: Request, policy: DefaultBackendPolicy) -> RequestContext:
    return RequestContext(
        backend=policy.config.name,
        path=request.url.path,
        method=request.method,
        arrival_time=time.monotonic(),
        source_ip=getattr(request.state, "client_ip", None),
        user_class=None,  # Phase 3: resolved from auth
        score=100,  # fixed default until Phase 3's ScoreEngine
    )
