"""BackendDispatcher — streams a request through to a single backend's
upstream and streams the response back, without full in-memory buffering
(FR-054, needed for large archived resources such as video WARC records).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from app import metrics, observability
from app.config import BackendConfig

if TYPE_CHECKING:
    import asyncio

    from starlette.requests import Request

    from app.interfaces import CapacityController, RequestContext

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

    async def _send_and_stream(self, request: Request) -> tuple[httpx.Response, Response]:
        """Sends `request` upstream and returns both the raw `httpx.Response`
        (for status/latency bookkeeping) and the `StreamingResponse` wrapping
        its body. Raises `httpx.HTTPError` (including `httpx.TimeoutException`)
        on connect/send failure — callers decide the resulting status code."""
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
        upstream_response = await self._client.send(upstream_request, stream=True)

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
        return upstream_response, response

    async def dispatch_queued(
        self,
        request: Request,
        ctx: RequestContext,
        future: asyncio.Future,
        controller: CapacityController,
        queue_wait_ms: float = 0.0,
    ) -> None:
        """Run by `PriorityScheduler.run_worker()` from a detached task once
        a capacity slot has been acquired for this request. Always releases
        the slot (FR-052: immediately on response or timeout) and resolves
        `future` with the final `Response` — unless `future` was already
        cancelled (the original request gave up first), in which case any
        already-open upstream response is closed directly instead.

        Every outcome here counts as "admitted" (`observability.
        log_admission_event("admitted", ...)`) regardless of the backend's
        own status code — a backend-originated 502/503 is a backend problem,
        tracked separately via `backend_errors_total`/`backend_timeouts_total`,
        not an admission-control rejection."""
        name = self._config.name
        class_label = ctx.user_class or "unknown"
        start = time.monotonic()
        try:
            upstream_response, response = await self._send_and_stream(request)
        except httpx.TimeoutException:
            latency_ms = (time.monotonic() - start) * 1000
            await controller.release(1, latency_ms=latency_ms, status_code=0, timed_out=True)
            metrics.backend_timeouts_total.labels(name).inc()
            metrics.backend_request_duration_seconds.labels(name, class_label).observe(
                latency_ms / 1000
            )
            observability.log_admission_event(
                "admitted",
                ctx,
                queue_wait_ms=queue_wait_ms,
                backend_latency_ms=latency_ms,
                status_code=503,
            )
            if not future.cancelled():
                future.set_result(
                    JSONResponse(
                        {"detail": "gateway timeout", "reason": "backend_timeout"},
                        status_code=503,
                    )
                )
            return
        except httpx.HTTPError:
            # Connection refused/reset/etc. — a single unreachable backend
            # must surface as this path's own 502, not an unhandled 500
            # (FR-083a: a dead backend affects only its own path).
            latency_ms = (time.monotonic() - start) * 1000
            await controller.release(1, latency_ms=latency_ms, status_code=0, timed_out=False)
            metrics.backend_errors_total.labels(name).inc()
            metrics.backend_request_duration_seconds.labels(name, class_label).observe(
                latency_ms / 1000
            )
            observability.log_admission_event(
                "admitted",
                ctx,
                queue_wait_ms=queue_wait_ms,
                backend_latency_ms=latency_ms,
                status_code=502,
            )
            if not future.cancelled():
                future.set_result(JSONResponse({"detail": "bad gateway"}, status_code=502))
            return

        latency_ms = (time.monotonic() - start) * 1000
        status_code = upstream_response.status_code
        await controller.release(1, latency_ms=latency_ms, status_code=status_code, timed_out=False)
        if status_code >= 500:
            metrics.backend_errors_total.labels(name).inc()
        metrics.backend_request_duration_seconds.labels(name, class_label).observe(
            latency_ms / 1000
        )
        observability.log_admission_event(
            "admitted",
            ctx,
            queue_wait_ms=queue_wait_ms,
            backend_latency_ms=latency_ms,
            status_code=status_code,
        )

        if future.cancelled():
            # Nobody will ever consume `response`'s body stream.
            await upstream_response.aclose()
            return
        future.set_result(response)

