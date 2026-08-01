"""LeastLoadedLoadBalancer — selects which physical instance of a backend's
`upstreams` serves an already-admitted request (see `app/interfaces.py`'s
`LoadBalancer` ABC for the contract).

Phase 1: pure least-in-flight selection. Phase 2 adds passive health
tracking — a connection-level failure marks an instance down immediately,
excluding it from selection — plus `health_check_loop()`, a periodic active
TCP probe that brings a down instance back once it's reachable again.
Phase 3 (this revision) adds sticky sessions: a client IP is pinned to the
instance it last used, unless that instance is unhealthy or is at/above its
fair share of capacity while a less-loaded healthy instance exists.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from app.interfaces import InstanceStatus, LoadBalancer, RequestContext, UpstreamInstance

logger = logging.getLogger(__name__)


class LeastLoadedLoadBalancer(LoadBalancer):
    """One instance per backend, constructed once in `app/main.py`'s
    lifespan. `select()`/`release()` share a single `asyncio.Lock` so that
    selection and its in-flight increment happen atomically — without this,
    concurrent bursts would all observe the same "least loaded" instance and
    pile onto it before any of their increments land.
    """

    def __init__(
        self,
        urls: list[str],
        *,
        connect_timeout_seconds: float = 5.0,
        health_check_interval_seconds: float = 10.0,
        sticky_enabled: bool = True,
        sticky_ttl_seconds: float = 300.0,
        capacity_hint: Callable[[], int] | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self._in_flight: dict[str, int] = dict.fromkeys(urls, 0)
        self._healthy: dict[str, bool] = dict.fromkeys(urls, True)
        self._marked_down_since: dict[str, float] = {}
        self._hostports: dict[str, tuple[str, int]] = {
            url: self._parse_hostport(url) for url in urls
        }
        self._connect_timeout_seconds = connect_timeout_seconds
        self._health_check_interval_seconds = health_check_interval_seconds
        self._sticky_enabled = sticky_enabled
        self._sticky_ttl_seconds = sticky_ttl_seconds
        self._capacity_hint = capacity_hint
        self._now = now
        # client IP -> (instance_url, last_used_monotonic)
        self._sticky: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _parse_hostport(url: str) -> tuple[str, int]:
        parts = urlsplit(url)
        default_port = 443 if parts.scheme == "https" else 80
        return parts.hostname, parts.port or default_port

    async def select(self, ctx: RequestContext) -> UpstreamInstance:
        async with self._lock:
            candidates = {url: n for url, n in self._in_flight.items() if self._healthy[url]}
            if not candidates:
                # Fail open: every instance is down, but never invent a new
                # rejection path here — a true full outage still surfaces
                # via the existing 502/503 dispatch-error handling.
                candidates = self._in_flight

            sticky_key = ctx.source_ip if self._sticky_enabled else None
            if sticky_key is not None:
                pinned = self._pinned_instance(sticky_key, candidates)
                if pinned is not None:
                    now = self._now()
                    self._in_flight[pinned] += 1
                    self._sticky[sticky_key] = (pinned, now)
                    return UpstreamInstance(url=pinned)

            url = min(candidates, key=candidates.get)
            self._in_flight[url] += 1
            if sticky_key is not None:
                self._sticky[sticky_key] = (url, self._now())
            return UpstreamInstance(url=url)

    def _pinned_instance(self, sticky_key: str, candidates: dict[str, int]) -> str | None:
        """Returns the client's pinned instance URL if it should still be
        used, or None if there's no live pin (absent, expired, unhealthy, or
        evicted for being at/above its fair share of capacity while a
        healthier alternative exists). Caller must hold `self._lock`."""
        entry = self._sticky.get(sticky_key)
        if entry is None:
            return None
        pinned_url, last_used = entry
        if self._now() - last_used > self._sticky_ttl_seconds:
            return None
        if pinned_url not in candidates:
            return None
        if self._should_evict_pin(pinned_url, candidates):
            return None
        return pinned_url

    def _should_evict_pin(self, pinned_url: str, candidates: dict[str, int]) -> bool:
        if self._capacity_hint is None:
            return False
        fair_share = math.ceil(self._capacity_hint() / max(len(candidates), 1))
        if candidates[pinned_url] < fair_share:
            return False
        return any(load < fair_share for url, load in candidates.items() if url != pinned_url)

    async def release(self, instance: UpstreamInstance, *, connect_failed: bool) -> None:
        async with self._lock:
            current = self._in_flight.get(instance.url, 0)
            if current <= 0:
                logger.warning("load_balancer_double_release", extra={"url": instance.url})
            else:
                self._in_flight[instance.url] = current - 1

            if connect_failed and self._healthy.get(instance.url, False):
                self._healthy[instance.url] = False
                self._marked_down_since[instance.url] = time.monotonic()
                logger.warning("upstream_instance_marked_down", extra={"url": instance.url})

    def snapshot(self) -> list[InstanceStatus]:
        sticky_counts: dict[str, int] = dict.fromkeys(self._in_flight, 0)
        for pinned_url, _ in self._sticky.values():
            if pinned_url in sticky_counts:
                sticky_counts[pinned_url] += 1
        return [
            InstanceStatus(
                url=url,
                healthy=self._healthy[url],
                in_flight=in_flight,
                sticky_count=sticky_counts[url],
            )
            for url, in_flight in self._in_flight.items()
        ]

    async def health_check_loop(self, interval: float | None = None) -> None:
        """Runs forever (cancelled on shutdown alongside every other
        `worker_tasks` entry, see `app/main.py`). Each tick, raw-TCP-connects
        (not an HTTP request — no backend here is confirmed to expose a
        health endpoint) to every currently-down instance; a successful
        connect brings it back into rotation immediately. Also sweeps
        expired sticky entries on the same cadence, rather than adding a
        second dedicated timer."""
        sleep_seconds = interval if interval is not None else self._health_check_interval_seconds
        while True:
            await asyncio.sleep(sleep_seconds)
            await self._check_down_instances()
            await self._sweep_expired_sticky_entries(self._now())

    async def _sweep_expired_sticky_entries(self, now: float) -> None:
        async with self._lock:
            expired = [
                key
                for key, (_, last_used) in self._sticky.items()
                if now - last_used > self._sticky_ttl_seconds
            ]
            for key in expired:
                del self._sticky[key]

    async def _check_down_instances(self) -> None:
        async with self._lock:
            down_urls = [url for url, healthy in self._healthy.items() if not healthy]
        for url in down_urls:
            if await self._probe(url):
                async with self._lock:
                    if not self._healthy.get(url, True):
                        self._healthy[url] = True
                        self._marked_down_since.pop(url, None)
                        logger.info("upstream_instance_recovered", extra={"url": url})

    async def _probe(self, url: str) -> bool:
        host, port = self._hostports[url]
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=self._connect_timeout_seconds
            )
        except (OSError, TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True
