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
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from starlette.requests import Request


class UserClass(StrEnum):
    """Identity-only classification (`docs/implementation_plan.md` §4.1a,
    `docs/decision_log.md` A4) — reflects *who is asking*, never *how they
    are behaving*. There is deliberately no `suspicious`/`bot` member: an
    abusive client is still one of these five, and is driven toward the back
    of the queue by the per-IP/subnet/ASN/country/user penalties in
    `app.scoring`, not by a separate behavior guess that would double-count
    the same signal.
    """

    ANONYMOUS = "anonymous"
    RESEARCHER = "researcher"
    SERVICE_ACCOUNT = "service_account"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


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
    subnet_6: str | None = None
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


@dataclass(frozen=True)
class UpstreamInstance:
    """One physical upstream server behind a logical backend name."""

    url: str


@dataclass(frozen=True)
class InstanceStatus:
    """Read-only snapshot of one instance's selection-relevant state,
    exposed via `LoadBalancer.snapshot()` (used by the admin API)."""

    url: str
    healthy: bool
    in_flight: int
    sticky_count: int = 0


class LoadBalancer(ABC):
    """Picks which physical instance of a backend's `upstreams` serves an
    already-admitted request (`docs/development.md` "Adding a new pluggable
    component in general"). Sits below `CapacityController`/`Scheduler` —
    those still gate/queue by backend *name*; `LoadBalancer` only decides
    *which instance* once a request has already been admitted.

    LeastLoadedLoadBalancer is the sole production implementation.
    """

    @abstractmethod
    async def select(self, ctx: RequestContext) -> UpstreamInstance:
        """Selects an instance and reserves capacity on it atomically —
        callers must not assume `select()` is side-effect-free. Always
        returns an instance (fails open if every instance is unhealthy)."""
        ...

    @abstractmethod
    async def release(self, instance: UpstreamInstance, *, connect_failed: bool) -> None:
        """Releases capacity reserved by a prior `select()`. `connect_failed`
        signals a connection-level failure (port unreachable/refused) —
        implementations may use this to mark the instance down; a slow-but-
        connected response must pass `connect_failed=False`."""
        ...

    @abstractmethod
    def snapshot(self) -> list[InstanceStatus]:
        """Read-only view of every instance's current state, for the admin
        API and metrics scraping."""
        ...
