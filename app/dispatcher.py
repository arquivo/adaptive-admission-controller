"""BackendDispatcher — streams a request through to a single backend's
upstream and streams the response back, without full in-memory buffering
(FR-054, needed for large archived resources such as video WARC records).
"""

from __future__ import annotations

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from app.config import BackendConfig

# Headers that are connection-specific and must never be forwarded verbatim
# in either direction (RFC 7230 §6.1 plus Host, which must be recomputed by
# whichever HTTP client sends the request).
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
    }
)


def _strip_hop_by_hop(headers: httpx.Headers) -> list[tuple[str, str]]:
    return [(k, v) for k, v in headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS]


class BackendDispatcher:
    """Owns one `httpx.AsyncClient` for a single backend — required because
    each backend has its own connect/read timeout (httpx binds `Timeout`/
    `Limits` at client construction, not per-request).
    """

    def __init__(self, config: BackendConfig):
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=config.connect_timeout_seconds,
                read=config.backend_timeout_seconds,
                write=config.backend_timeout_seconds,
                pool=config.connect_timeout_seconds,
            ),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def dispatch(self, request: Request) -> Response:
        # `match.path_prefix` is used only for backend *selection* (see
        # app.registry) — the original path is forwarded unchanged, matching
        # a drop-in replacement for Apache's ProxyPass.
        url = httpx.URL(str(self._config.upstream_url)).copy_with(
            path=request.url.path,
            query=request.url.query.encode() if request.url.query else None,
        )
        upstream_request = self._client.build_request(
            method=request.method,
            url=url,
            headers=_strip_hop_by_hop(request.headers),
            content=request.stream(),
        )
        try:
            upstream_response = await self._client.send(upstream_request, stream=True)
        except httpx.HTTPError:
            # Connection refused/timed out/etc. — a single unreachable
            # backend must surface as this path's own 502, not an unhandled
            # 500 (FR-083a: a dead backend affects only its own path).
            return JSONResponse({"detail": "bad gateway"}, status_code=502)

        async def body():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            finally:
                # Guaranteed even on client disconnect/task cancellation —
                # a bare BackgroundTask is not reliably run in that case.
                await upstream_response.aclose()

        response = StreamingResponse(body(), status_code=upstream_response.status_code)
        # Set raw_headers directly rather than passing a `headers` dict to
        # the constructor: a dict would silently collapse repeated headers
        # (e.g. multiple Set-Cookie) down to one.
        response.raw_headers = [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in _strip_hop_by_hop(upstream_response.headers)
        ]
        return response
