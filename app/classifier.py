"""Request classification (`docs/implementation_plan.md` §4.1).

Builds the `RequestContext` a `BackendPolicy` attaches to a request:
`source_ip`/`subnet_24`/`subnet_6` from the already-resolved client IP,
`country`/`asn` from an injected `GeoIPLookup`, `user_class`/`user_id` from an
injected `JWTVerifier`. `geoip`/`auth` are passed in explicitly (mirroring how
`FixedController`/`PriorityScheduler` are constructed and passed in rather
than looked up ad hoc from `request.app.state`) so this stays unit-testable
with fakes.
"""

from __future__ import annotations

import ipaddress
import time
from typing import TYPE_CHECKING

from app.interfaces import RequestContext

if TYPE_CHECKING:
    from starlette.requests import Request

    from app.auth import JWTVerifier
    from app.geoip import GeoIPLookup
    from app.registry import DefaultBackendPolicy


def classify(
    request: Request,
    policy: DefaultBackendPolicy,
    geoip: GeoIPLookup,
    auth: JWTVerifier,
) -> RequestContext:
    source_ip = getattr(request.state, "client_ip", None)
    subnet_24, subnet_6 = _subnets(source_ip, policy.resolved_scoring.ipv6_prefix_length)
    country, asn = geoip.lookup(source_ip) if source_ip is not None else (None, None)
    user_class, user_id = auth.verify(request.headers.get("authorization"))

    return RequestContext(
        backend=policy.config.name,
        path=request.url.path,
        method=request.method,
        arrival_time=time.monotonic(),
        source_ip=source_ip,
        user_class=user_class.value,
        subnet_24=subnet_24,
        subnet_6=subnet_6,
        asn=asn,
        country=country,
        user_id=user_id,
    )


def _subnets(ip: str | None, ipv6_prefix_length: int) -> tuple[str | None, str | None]:
    """A request's source IP is v4 or v6, never both — whichever dimension
    doesn't apply stays `None`, and its penalty function naturally
    contributes 0 (approved plan gap #2: §4.2's pseudocode never calls a
    `net6_penalty` despite the config/Redis schema requiring one)."""
    if ip is None:
        return None, None
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None, None
    if addr.version == 4:
        return str(ipaddress.ip_network(f"{ip}/24", strict=False)), None
    return None, str(ipaddress.ip_network(f"{ip}/{ipv6_prefix_length}", strict=False))
