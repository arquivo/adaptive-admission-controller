"""Fixed concurrency capacity controller (`docs/implementation_plan.md` §3.1).

`LatencyWindow` lives here rather than in a Phase 4 "adaptive" module because
FR-033a's predictive queue-wait rejection needs a mean service-time estimate
for every backend — fixed or adaptive — not just adaptive ones (FR-047).
Phase 4 reuses this same class unchanged.
"""

from __future__ import annotations

import asyncio
from collections import deque

from app.interfaces import CapacityController

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
            self._condition.notify_all()  # a slot just freed — wake any waiter that now fits

    def current_limit(self) -> int:
        return self._limit

    def mean_latency_ms(self) -> float | None:
        return self._latency.mean()
