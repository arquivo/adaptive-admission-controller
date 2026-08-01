"""Priority scheduler with predictive queue-wait rejection
(`docs/implementation_plan.md` §3.2).

Deviates from the plan's pseudocode in two ways, both documented in the
approved Phase 2 plan (`/home/ibranco/.claude/plans/soft-mixing-moonbeam.md`):
`enqueue()` also takes the real `Request` (not just `RequestContext`) so the
eventual dispatch can stream the actual body (FR-054); `run_worker()` replaces
the `Scheduler.next_request()` ABC method, which nothing ever called.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from app import metrics
from app.errors import QueueFullError, QueueWaitExceededError
from app.interfaces import CapacityController, RequestContext, Scheduler

if TYPE_CHECKING:
    from starlette.requests import Request


def estimate_wait_seconds(
    queue_depth: int, concurrency_limit: int, mean_latency_ms: float | None
) -> float:
    """Little's-law-style approximation (FR-033a): `concurrency_limit`
    requests served in parallel, each taking ~`mean_latency_ms` on average,
    drain `queue_depth` queued items ahead of a new arrival at an effective
    rate of `concurrency_limit / mean_latency_ms` per ms."""
    if mean_latency_ms is None or concurrency_limit <= 0:
        return 0.0  # cold start / not enough samples yet — fail open, never reject
    return (queue_depth * mean_latency_ms) / concurrency_limit / 1000.0


class PriorityScheduler(Scheduler):
    def __init__(
        self,
        backend_name: str,
        queue_max_size: int,
        queue_timeout: float,
        controller: CapacityController,
    ):
        self._backend_name = backend_name
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=queue_max_size)
        self._timeout = queue_timeout
        self._controller = controller  # current_limit()/mean_latency_ms() for FR-033a

    def queue_size(self) -> int:
        return self._queue.qsize()

    async def enqueue(self, request: Request, request_context: RequestContext) -> asyncio.Future:
        projected_wait = estimate_wait_seconds(
            queue_depth=self._queue.qsize(),
            concurrency_limit=self._controller.current_limit(),
            mean_latency_ms=self._controller.mean_latency_ms(),
        )
        if projected_wait > self._timeout:
            raise QueueWaitExceededError()  # FR-033a — 429, reason=queue_wait_exceeded

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (
            -request_context.score,
            request_context.arrival_time,
            request_context,
            request,
            future,
        )
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            raise QueueFullError() from None  # FR-033 — 429, reason=queue_full
        metrics.queue_size.labels(self._backend_name).set(self._queue.qsize())
        return future

    async def run_worker(self, controller: CapacityController, dispatcher: Any) -> None:
        while True:
            # Wait for a slot BEFORE popping, so the item taken is whatever
            # is currently highest-score — not whatever happened to be
            # highest-score back when we started waiting. Only safe to do
            # generically because MVP cost is always 1 (decision log A3):
            # the worker doesn't need to know a request's cost before it
            # knows it's about to serve it.
            await controller.acquire(1)
            _, _, ctx, request, future = await self._queue.get()
            metrics.queue_size.labels(self._backend_name).set(self._queue.qsize())
            if future.cancelled():
                await controller.release(1, latency_ms=0, status_code=0, timed_out=False)
                continue
            # `ctx.arrival_time` is stamped at classify() time, just before
            # scoring+enqueue — so this also folds in scoring latency, not
            # pure queue-sit time. Close enough for FR-033a's own projection,
            # and there's no other timestamp available without adding one.
            queue_wait_ms = (time.monotonic() - ctx.arrival_time) * 1000
            metrics.queue_wait_duration_seconds.labels(
                self._backend_name, ctx.user_class or "unknown"
            ).observe(queue_wait_ms / 1000)
            asyncio.create_task(
                dispatcher.dispatch_queued(request, ctx, future, controller, queue_wait_ms)
            )
