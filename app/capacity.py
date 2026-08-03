"""Fixed and adaptive concurrency capacity controllers
(`docs/implementation_plan.md` §3.1, §5.1-§5.2).

`LatencyWindow` lives here rather than in a separate "adaptive" module
because FR-033a's predictive queue-wait rejection needs a mean service-time
estimate for every backend — fixed or adaptive — not just adaptive ones
(FR-047). `AdaptiveController` (§5.2) reuses it unchanged for its `p95()`
reads, alongside `RateWindow` for timeout/5xx-rate tracking (§5.1).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from app import metrics
from app.config import AdaptiveBackendConfig
from app.interfaces import CapacityController

logger = logging.getLogger(__name__)

_MIN_SAMPLES = 10


class LatencyWindow:
    def __init__(self, window_size: int = 100):
        self._samples: deque[float] = deque(maxlen=window_size)

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def mean(self) -> float | None:
        if len(self._samples) < _MIN_SAMPLES:
            return None
        return sum(self._samples) / len(self._samples)

    def p95(self) -> float | None:
        if len(self._samples) < _MIN_SAMPLES:
            return None
        sorted_samples = sorted(self._samples)
        return sorted_samples[int(0.95 * len(sorted_samples))]


class FixedController(CapacityController):
    """`acquire()` blocks until a slot is available — it never returns a
    boolean, so a caller can never forget to check a result before
    dispatching (the historical over-admission bug this guards against)."""

    def __init__(self, limit: int):
        self._limit = limit
        self._in_flight = 0
        self._condition = asyncio.Condition()
        self._latency = LatencyWindow()

    async def acquire(self, cost: int = 1) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight + cost <= self._limit)
            self._in_flight += cost

    async def release(
        self, cost: int, latency_ms: float, status_code: int, timed_out: bool
    ) -> None:
        async with self._condition:
            self._in_flight -= cost
            if not timed_out and status_code < 500:
                self._latency.record(latency_ms)
            # Bounded to `cost`, not notify_all(): at most `cost` waiters (each
            # needing cost=1, decision log A3 — see app/registry.py's
            # estimate_cost()) can newly fit after this release. notify_all()
            # here was an O(waiters) wake-storm on every completed request —
            # the measured cause of the CPU-bound throughput inversion in
            # docs/deployment.md's load-test results. Revisit if `cost` ever
            # becomes non-uniform (a smaller-cost waiter behind a larger one
            # could then be starved by under-waking).
            self._condition.notify(cost)

    def current_limit(self) -> int:
        return self._limit

    def mean_latency_ms(self) -> float | None:
        return self._latency.mean()


class RateWindow:
    """Rolling boolean outcome rate — same rolling-window shape as
    `LatencyWindow`, but over a matched/not-matched outcome rather than a
    continuous sample (§5.1). Used by `AdaptiveController` for timeout-rate
    and 5xx-rate tracking; unlike `LatencyWindow`, `rate()` has no minimum
    sample count — an empty window reads as `0.0` (no evidence of a problem
    yet), matching `_adjust()`'s fail-open posture pre-p95.
    """

    def __init__(self, window_size: int = 100):
        self._outcomes: deque[bool] = deque(maxlen=window_size)

    def record(self, matched: bool) -> None:
        self._outcomes.append(matched)

    def rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)


class AdaptiveController(CapacityController):
    """p95-based adaptive concurrency controller (§5.2). Same blocking
    `acquire()`/`release()` contract as `FixedController`, but
    `current_limit()` moves over time: a periodic background `adjust_loop()`
    task (started/cancelled by `app.main`'s lifespan alongside the backend's
    worker task) grows or shrinks the limit from timeout rate, 5xx rate, and
    p95 latency relative to `target_p95_ms` — never from the request path
    itself, which only ever calls `acquire()`/`release()`.
    """

    def __init__(self, config: AdaptiveBackendConfig):
        self._name = config.name
        self._limit = config.initial_concurrency
        self._min = config.min_concurrency
        self._max = config.max_concurrency
        self._target_p95 = config.target_p95_ms
        self._timeout_rate_threshold = config.timeout_rate_threshold
        self._error_rate_threshold = config.error_rate_threshold
        self._latency = LatencyWindow()
        self._timeouts = RateWindow()
        self._errors = RateWindow()
        self._cooldown_until: float = 0.0
        self._in_flight = 0
        self._condition = asyncio.Condition()

    async def acquire(self, cost: int = 1) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight + cost <= self._limit)
            self._in_flight += cost

    async def release(
        self, cost: int, latency_ms: float, status_code: int, timed_out: bool
    ) -> None:
        async with self._condition:
            self._in_flight -= cost
            self._timeouts.record(timed_out)
            self._errors.record(status_code >= 500 and not timed_out)
            if not timed_out and status_code < 500:
                self._latency.record(latency_ms)
            # Bounded to `cost`, not notify_all(): at most `cost` waiters (each
            # needing cost=1, decision log A3 — see app/registry.py's
            # estimate_cost()) can newly fit after this release. notify_all()
            # here was an O(waiters) wake-storm on every completed request —
            # the measured cause of the CPU-bound throughput inversion in
            # docs/deployment.md's load-test results. Revisit if `cost` ever
            # becomes non-uniform (a smaller-cost waiter behind a larger one
            # could then be starved by under-waking).
            self._condition.notify(cost)

    def current_limit(self) -> int:
        return self._limit

    def mean_latency_ms(self) -> float | None:
        return self._latency.mean()

    async def adjust_loop(self, interval: float = 30.0) -> None:
        """Runs forever — one task per adaptive backend, cancelled alongside
        the backend's worker task on shutdown. Never awaited from the
        request path."""
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() < self._cooldown_until:
                continue
            await self._adjust()

    async def _adjust(self) -> None:
        p95 = self._latency.p95()
        if p95 is None:
            return  # not enough samples yet — leave the limit unchanged, never reject

        old_limit = self._limit
        target = self._target_p95
        timeout_rate = self._timeouts.rate()
        error_rate = self._errors.rate()

        if timeout_rate > self._timeout_rate_threshold:
            self._limit = max(self._min, int(self._limit * 0.60))
            self._cooldown_until = time.monotonic() + 60
        elif error_rate > self._error_rate_threshold:
            self._limit = max(self._min, int(self._limit * 0.75))
            self._cooldown_until = time.monotonic() + 30
        elif p95 > 2 * target:
            self._limit = max(self._min, int(self._limit * 0.70))
            self._cooldown_until = time.monotonic() + 30
        elif p95 > target:
            self._limit = max(self._min, int(self._limit * 0.85))
        elif p95 < 0.5 * target:
            self._limit = min(self._max, int(self._limit * 1.05))

        if self._limit == old_limit:
            return

        logger.info(
            "adaptive_limit_change",
            extra={
                "backend": self._name,
                "old_limit": old_limit,
                "new_limit": self._limit,
                "p95_ms": p95,
                "timeout_rate": timeout_rate,
                "error_rate": error_rate,
            },
        )
        metrics.adaptive_limit_changes_total.labels(self._name).inc()
        metrics.concurrency_limit.labels(self._name).set(self._limit)
        if self._limit > old_limit:
            # Raising the limit must immediately unblock any `acquire()`
            # already waiting — otherwise a queued request only benefits
            # from the new limit on its *next* arrival, not the ones already
            # blocked (§5.3).
            async with self._condition:
                self._condition.notify_all()
