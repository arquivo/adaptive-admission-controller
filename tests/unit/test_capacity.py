"""Unit tests for `app/capacity.py`."""

from __future__ import annotations

import asyncio

import pytest

from app.capacity import AdaptiveController, FixedController, LatencyWindow, RateWindow
from app.config import AdaptiveBackendConfig


def _adaptive_config(
    *,
    min_concurrency: int = 2,
    initial_concurrency: int = 10,
    max_concurrency: int = 50,
    target_p95_ms: float = 200.0,
    timeout_rate_threshold: float = 0.1,
    error_rate_threshold: float = 0.1,
) -> AdaptiveBackendConfig:
    return AdaptiveBackendConfig(
        name="test-backend",
        upstream_url="http://test-backend:8080",
        match={"path_prefix": "/test"},
        connect_timeout_seconds=5,
        backend_timeout_seconds=60,
        queue_max_size=100,
        queue_timeout_seconds=300,
        controller="adaptive",
        min_concurrency=min_concurrency,
        initial_concurrency=initial_concurrency,
        max_concurrency=max_concurrency,
        target_p95_ms=target_p95_ms,
        timeout_rate_threshold=timeout_rate_threshold,
        error_rate_threshold=error_rate_threshold,
    )


async def _fill_latency(controller: AdaptiveController, latency_ms: float, count: int = 10) -> None:
    for _ in range(count):
        await controller.acquire(1)
        await controller.release(1, latency_ms=latency_ms, status_code=200, timed_out=False)


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


def test_rate_window_empty_is_zero():
    window = RateWindow()
    assert window.rate() == 0.0


def test_rate_window_computes_rate():
    window = RateWindow()
    for value in (True, True, False, False):
        window.record(value)
    assert window.rate() == pytest.approx(0.5)


def test_rate_window_respects_maxlen():
    window = RateWindow(window_size=4)
    for value in (True, True, True, True, False, False, False, False):
        window.record(value)
    assert window.rate() == pytest.approx(0.0)  # only last 4 (all False) remain


def test_adaptive_controller_current_limit_starts_at_initial():
    controller = AdaptiveController(_adaptive_config(initial_concurrency=10))
    assert controller.current_limit() == 10


async def test_adaptive_controller_adjust_noop_below_min_samples():
    controller = AdaptiveController(_adaptive_config(initial_concurrency=10))
    for _ in range(5):
        await controller.acquire(1)
        await controller.release(1, latency_ms=1000.0, status_code=200, timed_out=False)
    await controller._adjust()
    assert controller.current_limit() == 10


async def test_adaptive_controller_shrinks_on_timeout_rate():
    controller = AdaptiveController(
        _adaptive_config(initial_concurrency=10, timeout_rate_threshold=0.05)
    )
    await _fill_latency(controller, latency_ms=50.0, count=10)
    await controller.acquire(1)
    await controller.release(1, latency_ms=50.0, status_code=200, timed_out=True)
    await controller._adjust()
    assert controller.current_limit() == 6  # max(min, int(10 * 0.60))
    assert controller._cooldown_until > 0


async def test_adaptive_controller_shrinks_on_error_rate():
    controller = AdaptiveController(
        _adaptive_config(initial_concurrency=10, error_rate_threshold=0.05)
    )
    await _fill_latency(controller, latency_ms=50.0, count=10)
    await controller.acquire(1)
    await controller.release(1, latency_ms=50.0, status_code=500, timed_out=False)
    await controller._adjust()
    assert controller.current_limit() == 7  # max(min, int(10 * 0.75))


async def test_adaptive_controller_shrinks_hard_when_p95_over_double_target():
    controller = AdaptiveController(_adaptive_config(initial_concurrency=10, target_p95_ms=100.0))
    await _fill_latency(controller, latency_ms=300.0)
    await controller._adjust()
    assert controller.current_limit() == 7  # max(min, int(10 * 0.70))


async def test_adaptive_controller_shrinks_softly_when_p95_over_target():
    controller = AdaptiveController(_adaptive_config(initial_concurrency=10, target_p95_ms=100.0))
    await _fill_latency(controller, latency_ms=150.0)
    await controller._adjust()
    assert controller.current_limit() == 8  # max(min, int(10 * 0.85))


async def test_adaptive_controller_grows_when_p95_well_under_target():
    controller = AdaptiveController(_adaptive_config(initial_concurrency=20, target_p95_ms=100.0))
    await _fill_latency(controller, latency_ms=10.0)
    await controller._adjust()
    assert controller.current_limit() == 21  # min(max, int(20 * 1.05))


async def test_adaptive_controller_clamps_at_min_on_repeated_shrink():
    controller = AdaptiveController(
        _adaptive_config(initial_concurrency=10, min_concurrency=5, timeout_rate_threshold=0.05)
    )
    for _ in range(5):
        await _fill_latency(controller, latency_ms=50.0, count=10)
        await controller.acquire(1)
        await controller.release(1, latency_ms=50.0, status_code=200, timed_out=True)
        await controller._adjust()
    assert controller.current_limit() == 5


async def test_adaptive_controller_clamps_at_max_on_repeated_growth():
    controller = AdaptiveController(
        _adaptive_config(initial_concurrency=20, max_concurrency=22, target_p95_ms=1000.0)
    )
    for _ in range(5):
        await _fill_latency(controller, latency_ms=10.0)
        await controller._adjust()
    assert controller.current_limit() == 22


async def test_adaptive_controller_limit_increase_unblocks_waiting_acquire():
    controller = AdaptiveController(
        _adaptive_config(initial_concurrency=20, max_concurrency=100, target_p95_ms=1000.0)
    )
    for _ in range(20):
        await controller.acquire(1)

    waiter = asyncio.ensure_future(controller.acquire(1))
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(waiter), timeout=0.05)

    for _ in range(10):
        controller._latency.record(10.0)
    await controller._adjust()
    assert controller.current_limit() == 21  # int(20 * 1.05)

    await asyncio.wait_for(waiter, timeout=1.0)
    assert waiter.done()


async def test_adaptive_controller_adjust_loop_applies_one_step_then_cools_down():
    controller = AdaptiveController(
        _adaptive_config(initial_concurrency=10, target_p95_ms=100.0, timeout_rate_threshold=0.05)
    )
    await _fill_latency(controller, latency_ms=50.0, count=10)
    await controller.acquire(1)
    await controller.release(1, latency_ms=50.0, status_code=200, timed_out=True)

    loop_task = asyncio.ensure_future(controller.adjust_loop(interval=0))
    await asyncio.sleep(0.05)
    loop_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop_task

    assert controller.current_limit() == 6


async def test_adaptive_controller_simulated_latency_trajectory():
    """Simulation test (§5.3): feeds a synthetic latency curve — sustained
    overload, easing to moderate overload, then full recovery — through a
    real `AdaptiveController` and checks the limit trajectory matches the
    adjustment table at each stage. Each phase fills the full 100-sample
    window so it isn't polluted by the previous phase's readings."""
    controller = AdaptiveController(
        _adaptive_config(
            initial_concurrency=40, min_concurrency=10, max_concurrency=60, target_p95_ms=100.0
        )
    )

    # Severe overload (p95 > 2x target) -> hard shrink.
    await _fill_latency(controller, latency_ms=500.0, count=100)
    await controller._adjust()
    assert controller.current_limit() == 28  # int(40 * 0.70)

    # Easing, but still over target -> soft shrink.
    await _fill_latency(controller, latency_ms=150.0, count=100)
    await controller._adjust()
    assert controller.current_limit() == 23  # int(28 * 0.85)

    # Full recovery (p95 well under target) -> gradual multi-step growth.
    await _fill_latency(controller, latency_ms=10.0, count=100)
    expected = [24, 25, 26, 27, 28]  # int(limit * 1.05) each step
    for expected_limit in expected:
        await controller._adjust()
        assert controller.current_limit() == expected_limit
