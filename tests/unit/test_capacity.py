"""Unit tests for `app/capacity.py`."""

from __future__ import annotations

import asyncio

import pytest

from app.capacity import FixedController, LatencyWindow


def test_latency_window_none_below_min_samples():
    window = LatencyWindow()
    for _ in range(9):
        window.record(10.0)
    assert window.mean() is None
    assert window.p95() is None


def test_latency_window_mean_and_p95_once_enough_samples():
    window = LatencyWindow()
    for value in range(1, 11):  # 1..10 ms
        window.record(float(value))
    assert window.mean() == pytest.approx(5.5)
    assert window.p95() == pytest.approx(sorted(range(1, 11))[int(0.95 * 10)])


def test_latency_window_respects_maxlen():
    window = LatencyWindow(window_size=10)
    for value in range(1, 21):  # 1..20, only last 10 should remain
        window.record(float(value))
    assert window.mean() == pytest.approx(sum(range(11, 21)) / 10)


async def test_fixed_controller_admits_up_to_limit_then_blocks():
    controller = FixedController(limit=2)
    await controller.acquire(1)
    await controller.acquire(1)

    blocked = asyncio.ensure_future(controller.acquire(1))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(blocked), timeout=0.05)
    assert not blocked.done()

    await controller.release(1, latency_ms=5.0, status_code=200, timed_out=False)
    await asyncio.wait_for(blocked, timeout=1.0)
    assert blocked.done()


async def test_fixed_controller_release_wakes_only_when_slot_fits():
    controller = FixedController(limit=1)
    await controller.acquire(1)

    waiter = asyncio.ensure_future(controller.acquire(2))
    await controller.release(1, latency_ms=1.0, status_code=200, timed_out=False)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=0.05)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


async def test_fixed_controller_records_latency_only_on_success():
    controller = FixedController(limit=5)
    for _ in range(10):
        await controller.acquire(1)
        await controller.release(1, latency_ms=100.0, status_code=200, timed_out=False)
    assert controller.mean_latency_ms() == pytest.approx(100.0)

    await controller.acquire(1)
    await controller.release(1, latency_ms=99999.0, status_code=500, timed_out=False)
    assert controller.mean_latency_ms() == pytest.approx(100.0)  # unchanged: 500 not recorded

    await controller.acquire(1)
    await controller.release(1, latency_ms=99999.0, status_code=200, timed_out=True)
    assert controller.mean_latency_ms() == pytest.approx(100.0)  # unchanged: timed_out not recorded


def test_fixed_controller_current_limit():
    controller = FixedController(limit=7)
    assert controller.current_limit() == 7
