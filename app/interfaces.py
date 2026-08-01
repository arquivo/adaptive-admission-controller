"""Core abstract interfaces for the Adaptive Admission Controller.

CapacityController, Scheduler, and BackendPolicy are pluggable from day one
(docs/implementation_plan.md principle #2). Phase 1 defines these interfaces
only; concrete implementations (FixedController, PriorityScheduler, ...)
arrive in later phases.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import Request


@dataclass
class RequestContext:
    """Mutable per-request state threaded through classification, scoring,
    scheduling, and dispatch. Not a wire schema — deliberately a plain
    dataclass rather than a Pydantic model.
    """

    backend: str
    path: str
    method: str
    arrival_time: float
    source_ip: str | None = None
    user_class: str | None = None
    subnet_24: str | None = None
    asn: str | None = None
    country: str | None = None
    user_id: str | None = None
    score: int = 100
    cost: int = 1
    score_breakdown: Any | None = None


class CapacityController(ABC):
    """Admission gate for a single backend's concurrency.

    acquire() BLOCKS until `cost` tokens are available — it never returns
    False, so a caller can never forget to check a return value. Implementations
    must wake blocked waiters whenever current_limit() increases at runtime.
    """

    @abstractmethod
    async def acquire(self, cost: int = 1) -> None: ...

    @abstractmethod
    async def release(
        self, cost: int, latency_ms: float, status_code: int, timed_out: bool
    ) -> None: ...

    @abstractmethod
    def current_limit(self) -> int: ...

    @abstractmethod
    def mean_latency_ms(self) -> float | None:
        """Feeds FR-033a's projected-wait estimate; None until enough samples exist."""
        ...


class Scheduler(ABC):
    @abstractmethod
    async def enqueue(self, request: Request, request_context: RequestContext) -> asyncio.Future:
        """Enqueues `request`/`request_context` together, so the eventual
        worker can dispatch the real request (streaming body included)
        rather than just the scheduling metadata in `RequestContext`."""
        ...

    @abstractmethod
    async def run_worker(self, controller: CapacityController, dispatcher: Any) -> None:
        """Runs forever: acquire a capacity slot, pop the next queued
        request, dispatch it. `dispatcher` is untyped here (`Any`) to avoid
        a circular import with `app.dispatcher`."""
        ...


class BackendPolicy(ABC):
    @abstractmethod
    def classify(self, request: Request) -> RequestContext: ...

    @abstractmethod
    def estimate_cost(self, ctx: RequestContext) -> int: ...


class PenaltyStore(ABC):
    """Isolates ScoreEngine (Phase 3) from Redis specifically.

    RedisPenaltyStore is the sole production implementation; a test-only
    in-memory fake may exist under tests/ but is never a supported
    deployment configuration.
    """

    @abstractmethod
    async def increment_and_get(self, key: str, ttl_seconds: int) -> int: ...
