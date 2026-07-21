# Adaptive Admission Controller — Implementation Plan

| Field | Value |
|---|---|
| Version | 1.0 |
| Status | Draft |
| Owner | Ivo Branco |
| Date | 2026-07-21 |
| Requirements | `docs/requirements.md` |

---

## 0. Principles

- **Correctness and safety before advanced features.** A fixed-limit controller that works reliably is better than an adaptive one that oscillates.
- **Every component behind an interface.** Capacity controllers and schedulers are pluggable from day one.
- **Observe first.** Metrics and structured logs are part of the MVP, not an afterthought.
- **Incremental validation.** Each phase ends with a testable, deployable artifact.

---

## 1. Phases Overview

| Phase | Deliverable | Validation Gate |
|---|---|---|
| 1 — Foundation | Project skeleton, interfaces, config model | Tests pass; app starts and proxies a single backend |
| 2 — Fixed Admission | Fixed capacity controller + priority queue per backend | Load test shows fixed concurrency enforced correctly |
| 3 — Scoring Engine | Request classifier + Redis-based scoring | Score decomposition visible in logs; penalties applied |
| 4 — Adaptive Controller | p95-based adaptive concurrency for Solr/pywb | Simulated latency curves drive correct limit changes |
| 5 — Observability | Full Prometheus metrics + structured logs + admin API | Metrics visible in test Grafana; alerts fire correctly |
| 6 — Integration & Hardening | Real backends in staging; failure-mode tests | Passes load test and failure injection suite |
| 7 — Production Deploy | Single-instance production rollout | Monitored rollout; baseline metrics captured |

---

## 2. Phase 1 — Foundation

**Goal:** Establish the project skeleton with all interfaces defined and a working single-backend pass-through proxy.

### 2.1 Project Structure

```
adaptive-admission-controller/
├── app/
│   ├── main.py                  # FastAPI app factory
│   ├── config.py                # Pydantic settings and backend policy schema
│   ├── interfaces.py            # CapacityController, Scheduler, BackendPolicy ABCs
│   ├── classifier.py            # Request classification (stub)
│   ├── dispatcher.py            # httpx async backend proxy
│   ├── registry.py              # BackendPolicyRegistry
│   └── metrics.py               # prometheus_client setup
├── tests/
│   ├── unit/
│   └── integration/
├── config/
│   └── backends.yaml            # Example backend policy configuration
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml / requirements.txt
```

### 2.2 Core Interfaces (Python ABCs)

```python
class CapacityController(ABC):
    async def acquire(self, cost: int = 1) -> bool: ...
    def release(self, cost: int, latency_ms: float, status_code: int, timed_out: bool) -> None: ...
    def current_limit(self) -> int: ...

class Scheduler(ABC):
    async def enqueue(self, request_context: RequestContext) -> asyncio.Future: ...
    async def next_request(self, backend_name: str) -> RequestContext: ...

class BackendPolicy(ABC):
    def classify(self, request: Request) -> RequestContext: ...
    def estimate_cost(self, ctx: RequestContext) -> int: ...
```

### 2.3 Configuration Schema (Pydantic)

```yaml
# config/backends.yaml
backends:
  - name: solr-main
    upstream_url: http://solr:8983
    controller: adaptive
    min_concurrency: 20
    initial_concurrency: 100
    max_concurrency: 500
    target_p95_ms: 100
    queue_max_size: 5000
    queue_timeout_seconds: 300
    request_cost_model: default

  - name: pywb-framed
    upstream_url: http://pywb:8080
    controller: fixed
    concurrency_limit: 100
    queue_max_size: 2000
    queue_timeout_seconds: 300

  - name: patching
    upstream_url: http://patching:9000
    controller: fixed
    concurrency_limit: 10
    queue_max_size: 100
    queue_timeout_seconds: 300

  - name: savepage
    upstream_url: http://savepage:9100
    controller: fixed
    concurrency_limit: 5
    queue_max_size: 50
    queue_timeout_seconds: 300
```

### 2.4 Tasks

- [ ] Initialise Python project (`pyproject.toml`, virtual environment, linting).
- [ ] Define all ABCs in `interfaces.py`.
- [ ] Implement `config.py` with Pydantic models for backend policies.
- [ ] Implement `dispatcher.py` using `httpx.AsyncClient` with connection pooling.
- [ ] Implement pass-through `main.py` that routes requests to the correct upstream.
- [ ] Add `/healthz` and `/readyz` endpoints.
- [ ] Write unit tests for config parsing.
- [ ] Validate that the app starts and proxies a real or mock backend.

---

## 3. Phase 2 — Fixed Admission Controller

**Goal:** Enforce fixed concurrency limits with a priority queue per backend. All admission control logic is wired end-to-end.

### 3.1 Fixed Capacity Controller

```python
class FixedController(CapacityController):
    def __init__(self, limit: int):
        self._limit = limit
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def acquire(self, cost: int = 1) -> bool:
        async with self._lock:
            if self._in_flight + cost <= self._limit:
                self._in_flight += cost
                return True
            return False

    def release(self, cost: int, **kwargs) -> None:
        self._in_flight -= cost
```

### 3.2 Priority Scheduler

```python
# PriorityQueue entry: (-score, timestamp, request_context, future)
# Negative score so that highest score is returned first by heapq.

class PriorityScheduler(Scheduler):
    def __init__(self, queue_max_size: int, queue_timeout: float):
        self._queue = asyncio.PriorityQueue(maxsize=queue_max_size)
        self._timeout = queue_timeout

    async def enqueue(self, ctx: RequestContext) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        entry = (-ctx.score, ctx.arrival_time, ctx, future)
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            raise QueueFullError()
        return future

    async def run_worker(self, controller: CapacityController, dispatcher):
        while True:
            _, _, ctx, future = await self._queue.get()
            if future.cancelled():
                continue
            await controller.acquire(ctx.cost)
            asyncio.create_task(dispatcher.dispatch(ctx, future, controller))
```

### 3.3 Request Lifecycle Wiring

```
HTTP Request
  → identify backend
  → build RequestContext (score=100 default in phase 2)
  → enqueue in backend PriorityScheduler
  → await Future (with queue_timeout)
  → return response or 503/429
```

### 3.4 Tasks

- [ ] Implement `FixedController`.
- [ ] Implement `PriorityScheduler` with worker coroutine.
- [ ] Wire request lifecycle in `main.py` middleware.
- [ ] Implement queue timeout (asyncio.wait_for) and 503 response on timeout.
- [ ] Implement 429 response on QueueFullError.
- [ ] Unit tests: fixed controller admit/reject at boundary, queue timeout.
- [ ] Load test: verify fixed concurrency limit is enforced under sustained traffic.

---

## 4. Phase 3 — Request Classification and Scoring Engine

**Goal:** Assign meaningful priority scores to every request using Redis-backed counters for IP, subnet, ASN, and country signals.

### 4.1 Request Classifier

Inputs extracted per request:

| Field | Source |
|---|---|
| backend | URL path prefix or Host header |
| request_type | Path pattern (search, replay-framed, replay-noframe, patching, savepage) |
| user_class | Authorization header / session token verification |
| source_ip | `X-Forwarded-For` or `REMOTE_ADDR` (trust only verified proxy headers) |
| subnet_24 | Computed from source_ip |
| asn | GeoIP/ASN database lookup (local cache with TTL) |
| country | GeoIP lookup (local cache with TTL) |
| user_id | From verified auth token |
| estimated_cost | From request type and backend cost model |

### 4.2 Scoring Formula

```python
BASE_SCORES = {
    UserClass.ANONYMOUS:    100,
    UserClass.RESEARCHER:    80,
    UserClass.UNKNOWN:       50,
    UserClass.SUSPICIOUS:    20,
    UserClass.BOT:            0,
}

async def calculate_score(ctx: RequestContext, redis: Redis) -> int:
    base = BASE_SCORES[ctx.user_class]

    penalty = 0
    penalty += await ip_penalty(ctx.source_ip, ctx.backend, redis)
    penalty += await net24_penalty(ctx.subnet_24, ctx.backend, redis)
    penalty += await asn_penalty(ctx.asn, ctx.backend, redis)
    penalty += await country_penalty(ctx.country, ctx.backend, redis)
    penalty += await user_penalty(ctx.user_id, ctx.backend, redis)

    final = clamp(base - penalty, min_score=-100, max_score=100)
    ctx.score_breakdown = ScoreBreakdown(base, penalty_ip, ...)
    return final
```

### 4.3 Redis Key Schema

```
rl:ip:{ip}:{backend}           TTL = 60s
rl:net24:{prefix24}:{backend}  TTL = 60s
rl:net6:{prefix6}:{backend}    TTL = 60s
rl:asn:{asn}:{backend}         TTL = 60s
rl:country:{cc}:{backend}      TTL = 300s
rl:user:{uid}:{backend}        TTL = 3600s (daily quota)
```

### 4.4 Penalty Thresholds (Initial Values — tune with production data)

| Dimension | Window | Soft threshold | Hard threshold | Soft penalty | Hard penalty |
|---|---|---|---|---|---|
| IP | 10s | 10 req | 30 req | -10 | -40 |
| IPv4 /24 | 60s | 50 req | 200 req | -10 | -40 |
| ASN | 60s | 200 req | 1000 req | -20 | -70 |
| Country | 300s | 500 req | 2000 req | -5 | -30 |
| Authenticated user | 60s | 50 req | 200 req | -5 | -40 |

### 4.5 Tasks

- [ ] Implement `classifier.py` (path → backend, path → request_type, auth → user_class).
- [ ] Integrate GeoIP/ASN lookup library with local TTL cache.
- [ ] Implement `ScoreEngine` with Redis async counter increments.
- [ ] Implement penalty functions per dimension.
- [ ] Log full score decomposition as structured JSON per request.
- [ ] Unit tests: classification rules, penalty calculation, score clamping.
- [ ] Integration tests: verify score reflects correct Redis counter state.

---

## 5. Phase 4 — Adaptive Concurrency Controller

**Goal:** Implement a p95-based adaptive concurrency controller for SolrCloud and pywb backends.

### 5.1 Latency Sampling

Every `release()` call feeds a rolling latency window:

```python
class LatencyWindow:
    def __init__(self, window_size: int = 100):
        self._samples: deque[float] = deque(maxlen=window_size)

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def p95(self) -> float | None:
        if len(self._samples) < 10:
            return None
        sorted_samples = sorted(self._samples)
        return sorted_samples[int(0.95 * len(sorted_samples))]
```

### 5.2 Adaptive Controller

```python
class AdaptiveController(CapacityController):
    def __init__(self, config: AdaptiveConfig):
        self._limit = config.initial_concurrency
        self._min = config.min_concurrency
        self._max = config.max_concurrency
        self._target_p95 = config.target_p95_ms
        self._latency = LatencyWindow()
        self._cooldown_until: float = 0

    def release(self, cost, latency_ms, status_code, timed_out):
        self._in_flight -= cost
        if not timed_out and status_code < 500:
            self._latency.record(latency_ms)

    async def _adjust_loop(self, interval: float = 30.0):
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() < self._cooldown_until:
                continue
            self._adjust()

    def _adjust(self):
        p95 = self._latency.p95()
        if p95 is None:
            return

        old_limit = self._limit
        target = self._target_p95

        if self._timeout_rate > 0:
            self._limit = max(self._min, int(self._limit * 0.60))
            self._cooldown_until = time.monotonic() + 60
        elif p95 > 2 * target:
            self._limit = max(self._min, int(self._limit * 0.70))
            self._cooldown_until = time.monotonic() + 30
        elif p95 > target:
            self._limit = max(self._min, int(self._limit * 0.85))
        elif p95 < 0.5 * target:
            self._limit = min(self._max, int(self._limit * 1.05))

        if self._limit != old_limit:
            log_limit_change(old_limit, self._limit, p95)
            metrics.adaptive_limit_changes_total.inc()
```

### 5.3 Tasks

- [ ] Implement `LatencyWindow` with configurable sample size.
- [ ] Implement `AdaptiveController` with adjustment loop.
- [ ] Implement cooldown period logic.
- [ ] Record timeout rate and 5xx rate as separate signals.
- [ ] Log all limit change events with old/new limits and triggering p95.
- [ ] Unit tests: adjustment table, cooldown, min/max bounds.
- [ ] Simulation tests: feed synthetic latency curves; verify expected limit trajectory.

---

## 6. Phase 5 — Full Observability

**Goal:** Complete Prometheus metrics, structured JSON logs, and the administrative API.

### 6.1 Prometheus Metrics

Register all metrics defined in `requirements.md §6.8` using `prometheus_client`:

```python
from prometheus_client import Counter, Gauge, Histogram

inflight_requests   = Gauge("admission_inflight_requests", "...", ["backend"])
concurrency_limit   = Gauge("admission_concurrency_limit", "...", ["backend"])
queue_size          = Gauge("admission_queue_size", "...", ["backend"])
admitted_total      = Counter("admission_admitted_total", "...", ["backend", "class"])
rejected_total      = Counter("admission_rejected_total", "...", ["backend", "class", "reason"])
queue_timeout_total = Counter("admission_queue_timeout_total", "...", ["backend"])
backend_latency     = Histogram("backend_request_duration_seconds", "...", ["backend", "class"])
backend_errors      = Counter("backend_errors_total", "...", ["backend"])
backend_timeouts    = Counter("backend_timeouts_total", "...", ["backend"])
limit_changes       = Counter("adaptive_limit_changes_total", "...", ["backend"])
score_distribution  = Histogram("score_distribution", "...", ["backend"], buckets=range(-100, 110, 10))
queue_wait          = Histogram("queue_wait_duration_seconds", "...", ["backend", "class"])
```

### 6.2 Structured Logging

Every admission decision emits one JSON log line:

```json
{
  "event": "admitted",
  "backend": "solr-main",
  "user_class": "anonymous",
  "source_ip": "1.2.3.4",
  "asn": "AS12345",
  "country": "PT",
  "score": {
    "base": 100,
    "penalty_ip": -10,
    "penalty_net24": 0,
    "penalty_asn": -20,
    "penalty_country": 0,
    "penalty_user": 0,
    "final": 70
  },
  "cost": 1,
  "queue_wait_ms": 42,
  "backend_latency_ms": 85,
  "status_code": 200
}
```

### 6.3 Administrative API Tasks

- [ ] Implement `GET /admin/backends` — list backends with current policy and live metrics snapshot.
- [ ] Implement `GET/PUT /admin/backends/{name}/policy` — view/update backend policy at runtime.
- [ ] Implement `GET /admin/backends/{name}/limit` — current limit (fixed or adaptive).
- [ ] Implement `POST /admin/backends/{name}/drain` — stop accepting new requests for a backend.
- [ ] Enforce authentication on all `/admin/*` endpoints.

### 6.4 Tasks

- [ ] Register all required Prometheus metrics.
- [ ] Emit structured JSON log per request (admission, rejection, timeout, limit change).
- [ ] Expose `/metrics` endpoint.
- [ ] Implement admin API endpoints.
- [ ] Write integration test: verify expected metrics are emitted under load.

---

## 7. Phase 6 — Integration and Hardening

**Goal:** Validate the full system against real or realistic backends in a staging environment. Exercise all defined failure modes.

### 7.1 Integration Tests

- [ ] Route real Solr queries through AAC; verify limit is enforced and metrics match.
- [ ] Route real pywb requests; verify separate queues for framed and no-frame.
- [ ] Simulate latency injection on Solr; verify adaptive controller reduces limit.
- [ ] Simulate backend 5xx burst; verify limit reduction and error metrics.
- [ ] Simulate queue saturation; verify 503 responses and `queue_timeout_total` counter.
- [ ] Simulate client disconnect during queue wait; verify Future cancellation.
- [ ] Verify that Solr saturation does not affect pywb queue.
- [ ] Verify that Redis disconnection triggers fallback and alerts.

### 7.2 Load Testing

Tools: `locust` or `k6`.

Scenarios:

1. **Baseline** — Ramp from 10 to 500 concurrent clients against Solr. Record p95 latency and concurrency limit evolution.
2. **Priority** — Mix 80% low-score bots and 20% high-score users. Verify high-score users are served faster.
3. **Distributed abuse** — Many IPs from same ASN. Verify ASN penalty reduces their priority without blocking legitimate traffic.
4. **Fixed backend** — Saturate patching backend; verify fixed limit holds.
5. **Recovery** — Remove load after saturation; verify adaptive limit recovers.

### 7.3 Security Hardening

- [ ] Verify that client-supplied priority headers are ignored.
- [ ] Verify that auth state is only derived from verified upstream headers.
- [ ] Verify admin API requires authentication; returns 401 without credentials.
- [ ] Verify that large request bodies and malformed headers are handled without crash.

---

## 8. Phase 7 — Production Deployment

**Goal:** Roll out single-instance AAC to production with monitoring and rollback plan.

### 8.1 Deployment Steps

1. Deploy AAC in shadow mode behind Apache httpd (receive traffic, forward directly, observe metrics without enforcing limits).
2. Enable dry-run mode (classify and score but do not enforce admission).
3. Validate that classification, scoring, and metrics are correct against real traffic.
4. Enable fixed controllers for patching and SavePageNow (low-risk, predictable).
5. Enable fixed controllers for pywb with conservative limits.
6. Enable adaptive controller for SolrCloud with conservative initial and min limits.
7. Monitor p95 latency, limit evolution, queue depth, and rejection rate daily for 2 weeks.
8. Tune thresholds and scoring penalties based on observed production data.

### 8.2 Rollback Plan

- Keep original direct Apache → backend routing as a fast fallback.
- AAC can be bypassed by updating Apache httpd `ProxyPass` rules; no backend changes required.
- All threshold and penalty configuration is in `config/backends.yaml`; changes take effect without restart if runtime reload is enabled.

### 8.3 Initial Production Limits (Tentative — validate with load tests)

| Backend | Controller | Initial limit | Min | Max |
|---|---|---|---|---|
| SolrCloud | Adaptive | 50 | 10 | 300 |
| pywb framed | Fixed → Adaptive | 50 | — | — |
| pywb no-frame | Fixed → Adaptive | 100 | — | — |
| Patching | Fixed | 10 | — | — |
| SavePageNow | Fixed | 5 | — | — |

*These numbers must be validated with production load tests before enforcement.*

---

## 9. Technology Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.12+ | Team expertise; async ecosystem; matches existing arquivo.pt stack. |
| HTTP Framework | FastAPI | Native async, clean middleware, Pydantic integration. |
| ASGI Server | Uvicorn | Low overhead; async I/O native. |
| Backend HTTP Client | `httpx` async | Async, connection pooling, streaming support. |
| Global State | Redis (shared, async) | Required for distributed abuse signal aggregation. |
| Local Cache | TTL dict or `cachetools` | ASN/GeoIP lookups; non-critical. |
| Metrics | `prometheus_client` | Standard; required for Grafana/alerting integration. |
| Logging | `structlog` or stdlib JSON formatter | Structured JSON logs per request. |
| Config Validation | Pydantic v2 | Type-safe backend policy schema. |
| GeoIP/ASN | `maxminddb` + MaxMind GeoLite2 | Free ASN and country data; local lookup. |
| Testing | `pytest` + `pytest-asyncio` + `httpx` test client | Async-native test suite. |
| Load Testing | `locust` or `k6` | Scenario-based load generation. |

---

## 10. Milestones

| Milestone | Target | Success Criterion |
|---|---|---|
| M1: Foundation complete | Phase 1 done | App starts; proxies test backend; all tests green. |
| M2: Fixed admission working | Phase 2 done | Concurrency limit enforced; queue timeout tested. |
| M3: Scoring live | Phase 3 done | Score decomposition visible in logs; Redis penalties applied. |
| M4: Adaptive controller working | Phase 4 done | Simulation tests validate limit adjustment behaviour. |
| M5: Full observability | Phase 5 done | All metrics emitted; admin API functional. |
| M6: Staging validated | Phase 6 done | Load and failure-mode tests pass in staging. |
| M7: Production — phase 1 | Phase 7 steps 1–3 | Shadow mode metrics match expectations. |
| M8: Production — phase 2 | Phase 7 steps 4–8 | All backends under AAC; limits tuned to production data. |

---

*Implementation details should be validated against `docs/requirements.md` before each phase begins. Changes to requirements must be reflected in this plan.*
