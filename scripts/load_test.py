#!/usr/bin/env python3
"""Standalone load-test tool for a running AAC instance
(`docs/implementation_plan.md` §7.2).

Fires `--requests` total GET requests at `--url`, capped at `--concurrency`
in flight at once, then reports the response-time p50/p95/p99 and the
status-code (or connection-error) distribution.

This is a runnable diagnostic tool, not a substitute for the full §7.2
requirement — a real staging run with ~500 concurrent clients against the
real `page-search-api`/`image-search-api` backends. That needs real staging
infrastructure this script can't provide on its own; it's meant to be
pointed at one when available, or at a local/dev AAC + mock backend in the
meantime.

Example:
    uv run python scripts/load_test.py --url http://localhost:8000/textsearch/ \\
        --concurrency 50 --requests 500
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


async def run_load_test(
    url: str, concurrency: int, total_requests: int, request_timeout: float
) -> tuple[list[float], Counter]:
    latencies: list[float] = []
    statuses: Counter = Counter()
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=request_timeout) as client:

        async def _bounded() -> None:
            async with semaphore:
                await _fire(client, url, latencies, statuses)

        await asyncio.gather(*(_bounded() for _ in range(total_requests)))

    return latencies, statuses


def _report(latencies: list[float], statuses: Counter, elapsed_seconds: float) -> None:
    sorted_latencies = sorted(latencies)
    total = len(latencies)
    print(f"requests: {total} in {elapsed_seconds:.2f}s ({total / elapsed_seconds:.1f} req/s)")
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
    parser.add_argument("--url", required=True, help="target URL to hammer with GET requests")
    parser.add_argument("--concurrency", type=int, default=20, help="max in-flight requests")
    parser.add_argument("--requests", type=int, default=200, help="total requests to send")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout, seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    start = time.monotonic()
    latencies, statuses = asyncio.run(
        run_load_test(args.url, args.concurrency, args.requests, args.timeout)
    )
    _report(latencies, statuses, time.monotonic() - start)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
