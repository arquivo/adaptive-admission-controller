"""Trusted-proxy ingress middleware (FR-010a).

Rejects any request whose directly-connecting peer is not in
`ingress.trusted_proxies` with `403`, without ever falling back to
`REMOTE_ADDR`-as-client-IP for such a peer. For a trusted peer, resolves the
real client IP from `X-Forwarded-For` at `xff_trusted_hops` from the right.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Operational endpoints are exempt from the trusted-proxy check: orchestrator
# liveness/readiness probes and scrapers typically hit the AAC process
# directly rather than through the front-end Apache/Caddy, and none of these
# paths need a resolved client IP. Not specified by FR-010a explicitly —
# a deliberate operational default rather than a hard requirement.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


@lru_cache(maxsize=32)
def _trusted_networks(proxies: tuple[str, ...]) -> tuple:
    return tuple(ipaddress.ip_network(p, strict=False) for p in proxies)


def _ip_in_allowlist(peer_ip: str, proxies: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks(tuple(proxies)))


def resolve_client_ip(xff: str | None, fallback: str, hops: int) -> str:
    """The X-Forwarded-For entry `hops` from the right. Falls back to the
    (already-trusted) peer IP when XFF is absent or has fewer entries than
    `hops` — an under-crediting default, not a security hole, since this is
    only reached after the peer has already passed the trusted-proxy check.
    """
    if not xff:
        return fallback
    parts = [p.strip() for p in xff.split(",") if p.strip()]
    if len(parts) < hops:
        return fallback
    return parts[-hops]


class TrustedProxyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        ingress = request.app.state.config.ingress
        peer_ip = request.client.host if request.client else None

        if peer_ip is None or not _ip_in_allowlist(peer_ip, ingress.trusted_proxies):
            return JSONResponse({"detail": "forbidden"}, status_code=403)

        request.state.client_ip = resolve_client_ip(
            request.headers.get("x-forwarded-for"), peer_ip, ingress.xff_trusted_hops
        )
        return await call_next(request)
