"""Prometheus metrics (`docs/implementation_plan.md` §6.1, `requirements.md`
§6.8).

All metrics are module-level singletons registered against the default
registry at import time — `/metrics` just serializes whatever the rest of
the app incremented on them.

`class` is a Python keyword, so `.labels(...)` calls throughout the codebase
must pass label values *positionally*, in the exact order declared below,
rather than as keyword arguments (`.labels(class="x")` is a syntax error).
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

# Gauges: ["backend"]
inflight_requests = Gauge(
    "admission_inflight_requests", "In-flight requests per backend.", ["backend"]
)
inflight_tokens = Gauge(
    "admission_inflight_tokens", "Weighted tokens in flight per backend.", ["backend"]
)
concurrency_limit = Gauge(
    "admission_concurrency_limit", "Current concurrency limit per backend.", ["backend"]
)
queue_size = Gauge("admission_queue_size", "Current queue depth per backend.", ["backend"])

# Counters: ["backend", "class", "exempt"]
requests_total = Counter(
    "admission_requests_total",
    "Total received requests by backend and class.",
    ["backend", "class", "exempt"],
)
admitted_total = Counter(
    "admission_admitted_total", "Requests admitted per backend.", ["backend", "class", "exempt"]
)
# Counter: ["backend", "class", "reason", "exempt"]
rejected_total = Counter(
    "admission_rejected_total",
    "Requests rejected by policy or capacity.",
    ["backend", "class", "reason", "exempt"],
)

# Counters: ["backend"]
queue_timeout_total = Counter(
    "admission_queue_timeout_total", "Requests that timed out waiting in queue.", ["backend"]
)
backend_errors_total = Counter(
    "backend_errors_total", "Backend errors (5xx or unreachable) per backend.", ["backend"]
)
backend_timeouts_total = Counter(
    "backend_timeouts_total", "Backend dispatch timeouts per backend.", ["backend"]
)
adaptive_limit_changes_total = Counter(
    "adaptive_limit_changes_total", "Adaptive controller limit changes per backend.", ["backend"]
)

# Histograms
backend_request_duration_seconds = Histogram(
    "backend_request_duration_seconds",
    "Backend latency per backend and class.",
    ["backend", "class"],
)
queue_wait_duration_seconds = Histogram(
    "queue_wait_duration_seconds",
    "Time requests spend waiting in queue.",
    ["backend", "class"],
)
score_distribution = Histogram(
    "score_distribution",
    "Distribution of request scores by backend.",
    ["backend", "exempt"],
    buckets=range(-100, 110, 10),
)


async def metrics_endpoint(request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


routes = [Route("/metrics", metrics_endpoint, methods=["GET"])]
