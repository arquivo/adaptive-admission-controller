"""Unit tests for `app/scheduler.py`."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.capacity import FixedController
from app.errors import QueueFullError, QueueWaitExceededError
from app.interfaces import RequestContext
from app.scheduler import PriorityScheduler, estimate_wait_seconds


def _ctx(score: int = 100) -> RequestContext:
    return RequestContext(
        backend="mock-backend",
        path="/proxytest/echo",
        method="GET",
        arrival_time=time.monotonic(),
        score=score,
    )


class _FakeRequest:
    """Stands in for a `starlette.requests.Request` — the scheduler never
    inspects it, only threads it through to the dispatcher."""


def test_estimate_wait_seconds_cold_start_fails_open():
    result = estimate_wait_seconds(queue_depth=1000, concurrency_limit=10, mean_latency_ms=None)
    assert result == 0.0


def test_estimate_wait_seconds_zero_or_negative_limit_fails_open():
    assert estimate_wait_seconds(queue_depth=1000, concurrency_limit=0, mean_latency_ms=50.0) == 0.0


def test_estimate_wait_seconds_computes_littles_law_estimate():
    # 20 queued items, limit 10, mean latency 500ms -> (20*500)/10/1000 = 1.0s
    assert estimate_wait_seconds(queue_depth=20, concurrency_limit=10, mean_latency_ms=500.0) == 1.0


async def test_enqueue_raises_queue_full_before_touching_wait_projection():
    controller = FixedController(limit=100)  # generous, so projected wait is 0 either way
    scheduler = PriorityScheduler(
        backend_name="mock-backend", queue_max_size=1, queue_timeout=300, controller=controller
    )

    await scheduler.enqueue(_FakeRequest(), _ctx())
    with pytest.raises(QueueFullError):
        await scheduler.enqueue(_FakeRequest(), _ctx())


async def test_enqueue_raises_queue_wait_exceeded_before_queue_is_full():
    controller = FixedController(limit=1)
    for _ in range(10):
        await controller.release(1, latency_ms=1000.0, status_code=200, timed_out=False)
    # mean_latency_ms() now == 1000ms. The first enqueue lands on an empty
    # queue (projected wait 0 regardless of timeout) and always succeeds; the
    # second sees queue_depth=1, projecting a 1s wait against a 0.5s timeout
    # -- well under queue_max_size=1000, so this is FR-033a firing, not FR-033.
    scheduler = PriorityScheduler(
        backend_name="mock-backend", queue_max_size=1000, queue_timeout=0.5, controller=controller
    )
    await scheduler.enqueue(_FakeRequest(), _ctx())

    with pytest.raises(QueueWaitExceededError):
        await scheduler.enqueue(_FakeRequest(), _ctx())


async def test_run_worker_releases_capacity_for_cancelled_future_without_dispatching():
    controller = FixedController(limit=1)
    scheduler = PriorityScheduler(
        backend_name="mock-backend", queue_max_size=10, queue_timeout=300, controller=controller
    )
    dispatched = []

    class _RecordingDispatcher:
        async def dispatch_queued(self, request, ctx, future, controller, queue_wait_ms=0.0):
            dispatched.append(request)
            future.set_result("ok")

    # A single worker is the sole caller of `controller.acquire()` in
    # production, so the right way to prove a cancelled entry's slot was
    # released is to enqueue a second, real entry behind it and confirm the
    # worker reaches it -- not to `acquire()` from the test directly, which
    # would race the worker's own next acquire/pop cycle.
    cancelled_future = await scheduler.enqueue("cancelled-request", _ctx())
    cancelled_future.cancel()
    live_future = await scheduler.enqueue("live-request", _ctx())

    worker_task = asyncio.ensure_future(scheduler.run_worker(controller, _RecordingDispatcher()))
    try:
        result = await asyncio.wait_for(live_future, timeout=1.0)
    finally:
        worker_task.cancel()

    assert result == "ok"
    assert dispatched == ["live-request"]
