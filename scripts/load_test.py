#!/usr/bin/env python3
"""Standalone load-test tool for a running AAC instance
(`docs/implementation_plan.md` §7.2).

Fires `--requests` total GET requests (round-robin across one or more
`--url`s), capped at `--concurrency` in flight at once, then reports the
response-time p50/p95/p99 and the status-code (or connection-error)
distribution. Two opt-in modes probe beyond raw throughput:
`--ramp-seconds` grows concurrency gradually instead of slamming
`--concurrency` immediately, and `--hold-open-seconds` keeps each slot
occupied after its response completes, to specifically pressure
keep-alive/connection-pool ceilings rather than just request rate.

This is a runnable diagnostic tool, not a substitute for the full §7.2
requirement — a real staging run with ~500 concurrent clients against the
real `page-search-api`/`image-search-api` backends. That needs real staging
infrastructure this script can't provide on its own; it's meant to be
pointed at one when available, or at a local/dev AAC + mock backend in the
meantime. It's also single-process: driving genuinely multi-process load
(the load generator itself becoming a bottleneck before the AAC does) isn't
supported directly — run multiple copies of this script from separate
hosts/terminals and sum/compare their reports instead.

Example:
    uv run python scripts/load_test.py --url http://localhost:8000/textsearch/ \\
        --concurrency 50 --requests 500

    # Ramp up over 30s, hitting two backends, holding each connection open
    # for 2s after its response to pressure keep-alive pools:
    uv run python scripts/load_test.py \\
        --url http://localhost:8000/textsearch/ --url http://localhost:8000/imagesearch/ \\
        --concurrency 200 --requests 2000 --ramp-seconds 30 --hold-open-seconds 2
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter

import httpx


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, int(pct * len(sorted_values)))
    return sorted_values[index]


def _distribute_urls(urls: list[str], total_requests: int) -> list[str]:
    """Round-robin: request `i` goes to `urls[i % len(urls)]`. A single URL
    reproduces today's behavior of hitting one endpoint every time."""
    return [urls[i % len(urls)] for i in range(total_requests)]


def _ramp_capacity(concurrency: int, ramp_seconds: float, elapsed_seconds: float) -> int:
    """How many concurrent slots should be open `elapsed_seconds` into a
    `ramp_seconds`-long ramp-up, growing linearly from 1 to `concurrency`.
    `ramp_seconds <= 0` means no ramp — full concurrency immediately."""
    if ramp_seconds <= 0:
        return concurrency
    fraction = min(1.0, elapsed_seconds / ramp_seconds)
    return max(1, int(concurrency * fraction))


async def _fire(
    client: httpx.AsyncClient, url: str, latencies: list[float], statuses: Counter
) -> None:
    start = time.monotonic()
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        statuses[f"error:{type(exc).__name__}"] += 1
        return
    finally:
        latencies.append((time.monotonic() - start) * 1000)
    statuses[str(response.status_code)] += 1


async def _ramp_up(semaphore: asyncio.Semaphore, concurrency: int, ramp_seconds: float) -> None:
    """Gradually releases extra permits on `semaphore` (created with an
    initial value of 1) until it holds `concurrency` — a plain `Semaphore`
    (not `BoundedSemaphore`) allows growing past its initial value this way.
    No-ops immediately if there's no ramp to run."""
    if ramp_seconds <= 0:
        return
    start = time.monotonic()
    granted = 1
    while granted < concurrency:
        await asyncio.sleep(0.1)
        target = _ramp_capacity(concurrency, ramp_seconds, time.monotonic() - start)
        while granted < target:
            semaphore.release()
            granted += 1


async def run_load_test(
    urls: list[str],
    concurrency: int,
    total_requests: int,
    request_timeout: float,
    ramp_seconds: float = 0.0,
    hold_open_seconds: float = 0.0,
) -> tuple[list[float], Counter]:
    latencies: list[float] = []
    statuses: Counter = Counter()
    request_urls = _distribute_urls(urls, total_requests)
    semaphore = asyncio.Semaphore(1 if ramp_seconds > 0 else concurrency)

    async with httpx.AsyncClient(timeout=request_timeout) as client:

        async def _bounded(url: str) -> None:
            async with semaphore:
                await _fire(client, url, latencies, statuses)
                if hold_open_seconds > 0:
                    await asyncio.sleep(hold_open_seconds)

        await asyncio.gather(
            _ramp_up(semaphore, concurrency, ramp_seconds),
            *(_bounded(url) for url in request_urls),
        )

    return latencies, statuses


def _report(
    latencies: list[float],
    statuses: Counter,
    elapsed_seconds: float,
    *,
    urls: list[str],
    ramp_seconds: float = 0.0,
    hold_open_seconds: float = 0.0,
) -> None:
    sorted_latencies = sorted(latencies)
    total = len(latencies)
    print(f"requests: {total} in {elapsed_seconds:.2f}s ({total / elapsed_seconds:.1f} req/s)")
    if len(urls) > 1:
        print(f"endpoints: {len(urls)} (round-robin)")
    if ramp_seconds > 0:
        print(f"ramp-up: {ramp_seconds:.1f}s to full concurrency")
    if hold_open_seconds > 0:
        print(f"hold-open: {hold_open_seconds:.1f}s per request after response")
    if sorted_latencies:
        print(
            "latency ms: "
            f"p50={_percentile(sorted_latencies, 0.50):.1f} "
            f"p95={_percentile(sorted_latencies, 0.95):.1f} "
            f"p99={_percentile(sorted_latencies, 0.99):.1f} "
            f"max={sorted_latencies[-1]:.1f}"
        )
    else:
        print("latency ms: n/a")
    print("status distribution:")
    for status, count in sorted(statuses.items(), key=lambda kv: kv[0]):
        print(f"  {status}: {count} ({100 * count / total:.1f}%)")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        dest="urls",
        action="append",
        required=True,
        help="target URL to hammer with GET requests (repeatable to round-robin across backends)",
    )
    parser.add_argument("--concurrency", type=int, default=20, help="max in-flight requests")
    parser.add_argument("--requests", type=int, default=200, help="total requests to send")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout, seconds")
    parser.add_argument(
        "--ramp-seconds",
        type=float,
        default=0.0,
        help="linearly grow to --concurrency over this many seconds (default: full immediately)",
    )
    parser.add_argument(
        "--hold-open-seconds",
        type=float,
        default=0.0,
        help="keep each slot occupied this long after its response, to pressure keep-alive pools",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    start = time.monotonic()
    latencies, statuses = asyncio.run(
        run_load_test(
            args.urls,
            args.concurrency,
            args.requests,
            args.timeout,
            ramp_seconds=args.ramp_seconds,
            hold_open_seconds=args.hold_open_seconds,
        )
    )
    _report(
        latencies,
        statuses,
        time.monotonic() - start,
        urls=args.urls,
        ramp_seconds=args.ramp_seconds,
        hold_open_seconds=args.hold_open_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
