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
| 1 — Foundation ✅ | Project skeleton, interfaces, config model | Tests pass; app starts and proxies a single backend |
| 2 — Fixed Admission | Fixed capacity controller + priority queue per backend | Load test shows fixed concurrency enforced correctly |
| 3 — Scoring Engine | Request classifier + Redis-based scoring | Score decomposition visible in logs; penalties applied |
| 4 — Adaptive Controller | p95-based adaptive concurrency for Search APIs/pywb | Simulated latency curves drive correct limit changes |
| 5 — Observability ✅ | Full Prometheus metrics + structured logs + admin API | Metrics visible in test Grafana; alerts fire correctly |
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
├── scripts/
│   └── update_geoip_db.py       # Standalone GeoIP/ASN DB refresh — never invoked by the AAC itself (FR-013a)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml / requirements.txt
```

### 2.2 Core Interfaces (Python ABCs)

```python
class CapacityController(ABC):
    # acquire() BLOCKS until `cost` tokens are available — it never returns
    # False, so a caller can never forget to check a return value (the bug
    # class that originally motivated this interface). Implementations must
    # wake blocked waiters whenever current_limit() increases at runtime
    # (see AdaptiveController, §5.2).
    async def acquire(self, cost: int = 1) -> None: ...
    async def release(self, cost: int, latency_ms: float, status_code: int, timed_out: bool) -> None: ...
    def current_limit(self) -> int: ...
    def mean_latency_ms(self) -> float | None: ...  # feeds FR-033a's projected-wait estimate (§3.2); None until enough samples exist

class Scheduler(ABC):
    async def enqueue(self, request_context: RequestContext) -> asyncio.Future: ...
    async def next_request(self, backend_name: str) -> RequestContext: ...

class BackendPolicy(ABC):
    def classify(self, request: Request) -> RequestContext: ...
    def estimate_cost(self, ctx: RequestContext) -> int: ...

class PenaltyStore(ABC):
    # Isolates ScoreEngine from Redis specifically (docs/decision_log.md D3).
    # RedisPenaltyStore (§4.5) is the sole production implementation; a
    # test-only in-memory fake may exist under tests/ but is never a
    # supported deployment configuration.
    async def increment_and_get(self, key: str, ttl_seconds: int) -> int: ...
```

### 2.3 Configuration Schema (Pydantic)

```yaml
# config/backends.yaml
#
# This file is the canonical reference for default concurrency/queue values.
# Tables elsewhere in requirements.md / this plan (recommended policies,
# production initial limits) must stay consistent with these numbers —
# treat this block as the single source of truth that tests reference.
ingress:
  # Peers (REMOTE_ADDR) allowed to set X-Forwarded-For — the front-end
  # Apache/Caddy IP(s). A request from any other peer is rejected 403
  # before classification/scoring ever runs (FR-010a).
  trusted_proxies: ["127.0.0.1", "::1"]   # default: localhost (IPv4 + IPv6) for a
    # co-located front-end; installation-dependent — set to the real front-end
    # IP(s)/CIDRs when Apache/Caddy runs on a separate host
  # Client IP = the X-Forwarded-For entry this many hops from the right.
  # Default 1 (rightmost = the hop immediately before the trusted proxy).
  # Raise only if more than one trusted proxy is chained in front of the AAC.
  xff_trusted_hops: 1

geoip:
  # Two local MaxMind GeoLite2 files the AAC reads at startup for ASN/country
  # lookups (FR-013, FR-013a) — the free tier has no combined country+ASN
  # file. The AAC never downloads either file itself — see
  # scripts/update_geoip_db.py (§4.5) for the separate, explicit refresh
  # command (one edition per invocation), run independently of the AAC
  # process (typically before a rolling restart); integrating that command
  # into deployment automation is out of scope here (§8.1).
  city_db_path: /var/lib/aac/GeoLite2-City.mmdb   # placeholder — set to the real deployment path
  asn_db_path: /var/lib/aac/GeoLite2-ASN.mmdb     # placeholder — set to the real deployment path

observability:
  debug_headers:
    # Attaches X-AAC-Backend/Score/Exempt/Reject-Reason to every response
    # (FR-074). Discloses scoring internals to the client — off by default;
    # enable deliberately per environment. See docs/decision_log.md D2.
    enabled: false

scoring:
  exempt_countries: ["PT"]   # skip subnet/24, IPv6-prefix, ASN, and country penalties for these
  ipv6_prefix_length: 56     # /48 or /56 — bucket size for IPv6 abuse aggregation (FR-022); default /56

  base_scores:               # per user_class — identity-only enum, see §4.1a
    anonymous: 100
    researcher: 80
    service_account: 90
    internal: 100
    unknown: 50

  score_clamp: {min: -100, max: 100}

  # Global default penalty thresholds. Each dimension is a LIST of one or
  # more independent windows — most dimensions need only one, but `user`
  # tracks both a short burst window and a long sustained-abuse window
  # (no daily quota/hard-cutoff concept exists; see docs/decision_log.md A7).
  # Below soft_threshold: no penalty. Between soft/hard: soft_penalty applies.
  # At/above hard_threshold: hard_penalty applies. Per-window penalties for
  # the same dimension are summed (§4.2). A backend override that targets
  # a single-window dimension (ip/net24/net6/asn/country) merges as a plain
  # dict into that one window; overriding one of `user`'s two windows
  # specifically is not needed by any backend yet.
  default_penalties:
    ip:      [{window_seconds: 10,  soft_threshold: 10,  hard_threshold: 30,   soft_penalty: 10, hard_penalty: 40}]
    net24:   [{window_seconds: 60,  soft_threshold: 50,  hard_threshold: 200,  soft_penalty: 10, hard_penalty: 40}]
    net6:    [{window_seconds: 60,  soft_threshold: 50,  hard_threshold: 200,  soft_penalty: 10, hard_penalty: 40}]
    asn:     [{window_seconds: 60,  soft_threshold: 200, hard_threshold: 1000, soft_penalty: 20, hard_penalty: 70}]
    country: [{window_seconds: 300, soft_threshold: 500, hard_threshold: 2000, soft_penalty: 5,  hard_penalty: 30}]
    user:
      - {window_seconds: 60,   soft_threshold: 50,  hard_threshold: 200,  soft_penalty: 5,  hard_penalty: 40}
      - {window_seconds: 3600, soft_threshold: 500, hard_threshold: 2000, soft_penalty: 10, hard_penalty: 60}   # best-guess default — tune with production data

  # Per-backend overrides: deep-merge over default_penalties/base_scores above.
  # Only list the fields/dimensions a backend needs to diverge on; everything
  # else is inherited unmodified. Backends with no entry here use the global
  # default as-is. Example: the Search APIs are far more expensive per-request
  # than pywb replay, so they penalize abusive IPs sooner and harder.
  overrides:
    page-search-api:
      penalties:
        ip: {soft_threshold: 5, hard_threshold: 15, soft_penalty: 15, hard_penalty: 50}
    image-search-api:
      penalties:
        ip: {soft_threshold: 5, hard_threshold: 15, soft_penalty: 15, hard_penalty: 50}

backends:
  # --- Search APIs (2 independent services, separate hardware/collections;
  # each talks to its own SolrCloud cluster internally) ---
  - name: page-search-api
    upstream_url: http://page-search-api:8080   # environment-specific — see docs/open_tbd.md
    match:
      path_prefix: /textsearch
    controller: adaptive
    min_concurrency: 20
    initial_concurrency: 100
    max_concurrency: 500
    target_p95_ms: 100
    timeout_rate_threshold: 0.05   # best-guess default — tune with production data
    error_rate_threshold: 0.10     # best-guess default — tune with production data
    connect_timeout_seconds: 5
    backend_timeout_seconds: 60
    queue_max_size: 5000
    queue_timeout_seconds: 300

  - name: image-search-api
    upstream_url: http://image-search-api:8080   # environment-specific — see docs/open_tbd.md
    match:
      path_prefix: /imagesearch
    controller: adaptive
    min_concurrency: 20
    initial_concurrency: 100
    max_concurrency: 500
    target_p95_ms: 100
    timeout_rate_threshold: 0.05   # best-guess default, see page-search-api above
    error_rate_threshold: 0.10     # best-guess default, see page-search-api above
    connect_timeout_seconds: 5
    backend_timeout_seconds: 60
    queue_max_size: 5000
    queue_timeout_seconds: 300

  # --- pywb (4 independent uwsgi processes, own port each) ---
  - name: pywb-framed
    upstream_url: http://pywb-framed:8080
    match:
      path_prefix: /wayback
    controller: fixed
    concurrency_limit: 100
    connect_timeout_seconds: 5
    backend_timeout_seconds: 60
    queue_max_size: 2000
    queue_timeout_seconds: 300

  - name: pywb-noframe
    upstream_url: http://pywb-noframe:8081
    match:
      path_prefix: /noFrame/replay
    controller: fixed
    concurrency_limit: 100
    connect_timeout_seconds: 5
    backend_timeout_seconds: 60
    queue_max_size: 2000
    queue_timeout_seconds: 300

  - name: pywb-patching
    upstream_url: http://pywb-patching:8082
    match:
      path_prefix: /noFrame/patching
    controller: fixed
    concurrency_limit: 10
    connect_timeout_seconds: 5
    backend_timeout_seconds: 60
    queue_max_size: 100
    queue_timeout_seconds: 300

  - name: pywb-archivepagenow
    upstream_url: http://pywb-archivepagenow:8083
    match:
      path_prefix: /save
    controller: fixed
    concurrency_limit: 5
    # Longer than the other five backends: ArchivePageNow performs a live
    # capture of the target page rather than serving from the existing
    # archive, so it is expected to take substantially longer per request.
    connect_timeout_seconds: 10
    backend_timeout_seconds: 120
    queue_max_size: 50
    queue_timeout_seconds: 300
```

`upstream_url` hosts/ports above are illustrative placeholders — these are real arquivo.pt services, but the actual host/port for each depends on the server/cluster the AAC is deployed against, so they must be set per environment rather than hardcoded once (see `docs/open_tbd.md`). `match.path_prefix` is resolved for all six backends (all reachable under a single host per environment; Search APIs route by path like pywb — `/textsearch`, `/imagesearch`). Per-backend `connect_timeout_seconds`/`backend_timeout_seconds`, concurrency limits, and the adaptive-only `timeout_rate_threshold`/`error_rate_threshold` are initial best-guess defaults — all require production validation before enforcement (`requirements.md` FR-053, §6.6; see `docs/open_tbd.md`).

**Scoring config resolution:** at startup, each backend's effective scoring config is computed once as `deep_merge(scoring.default_penalties, scoring.overrides.get(backend_name, {}).penalties)` (same for `base_scores` if a backend ever needs to override those too) and cached on the backend's `BackendPolicy` object. Requests never merge configs on the hot path — `calculate_score` (§4.2) always receives an already-resolved, backend-specific config. Every `default_penalties.<dimension>` value, and its resolved counterpart, is a **list** of `PenaltyConfig` windows — the deep-merge for a single-window dimension merges dict-into-dict at index 0; `user`'s two windows are carried through unchanged since no backend overrides them today.

### 2.4 Tasks

**Status: complete (2026-07-31).** Implemented with `uv` + `pyproject.toml` per the approved plan (`/home/ibranco/.claude/plans/soft-mixing-moonbeam.md`). 54 unit + integration tests passing; full `docker compose up` smoke test verified `/healthz`/`/readyz` (incl. Redis down), all-6-prefix routing, 404/403, and non-zero exit on broken YAML.

- [x] Initialise Python project (`pyproject.toml`, virtual environment, linting).
- [x] Define all ABCs in `interfaces.py`, including `PenaltyStore` (§2.2) — `ScoreEngine` (Phase 3, §4.5) must depend only on this interface, never on a Redis client directly.
- [x] Implement `config.py` with Pydantic models for backend policies.
- [x] Implement per-backend scoring config resolution (deep-merge `scoring.default_penalties`/`base_scores` with `scoring.overrides.<backend>`, once at startup — see §2.3 "Scoring config resolution").
- [x] Implement `dispatcher.py` using `httpx.AsyncClient` with connection pooling and per-backend `httpx.Timeout(connect=connect_timeout_seconds, read=backend_timeout_seconds)` (§2.3, FR-053).
- [x] Stream request/response bodies between client and backend via `httpx.AsyncClient` streaming (no full in-memory buffering) — required for large archived resources such as video WARC records (FR-054).
- [x] Implement pass-through `main.py` that routes requests to the correct upstream using longest-prefix-wins matching on `match.path_prefix` (§4.1, FR-011a); return `404` for a request matching no configured backend (FR-011c).
- [x] Add `/healthz` and `/readyz` endpoints.
- [x] Implement trusted-proxy ingress middleware (`ingress` config, §2.3; FR-010a): reject `403` any request whose `REMOTE_ADDR` is not in `trusted_proxies`; otherwise resolve the client IP from `X-Forwarded-For` per `xff_trusted_hops`.
- [x] Write unit tests for config parsing.
- [x] Write unit tests for trusted-proxy IP resolution: an allowlisted peer with a valid XFF resolves the correct client IP; a non-allowlisted peer is rejected 403 regardless of XFF content.
- [x] Write unit tests for scoring config merge: a backend override touching only one dimension/field leaves all other dimensions and unlisted backends unchanged.
- [x] Validate that the app starts and proxies a real or mock backend.
- [x] (not in original plan — added after the `docker compose` smoke test surfaced a bare unhandled 500) `dispatcher.py` returns `502 {"detail": "bad gateway"}` when the backend connection fails, instead of propagating the raw `httpx` error.
- [x] (not in original plan) `Dockerfile` + `docker-compose.yml` (uv multi-stage build; AAC + Redis only — real backend containers are Phase 6).

---

## 3. Phase 2 — Fixed Admission Controller

**Goal:** Enforce fixed concurrency limits with a priority queue per backend. All admission control logic is wired end-to-end.

### 3.1 Fixed Capacity Controller

`LatencyWindow` is defined here, not in Phase 4, because FR-033a's predictive
queue-wait rejection (§3.2) needs a mean service-time estimate for **every**
backend, fixed or adaptive — not just the adaptive ones. Phase 4 (§5.1)
reuses this same class unchanged and adds `RateWindow` alongside it for
timeout/error-rate tracking.

```python
class LatencyWindow:
    def __init__(self, window_size: int = 100):
        self._samples: deque[float] = deque(maxlen=window_size)

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def mean(self) -> float | None:
        if len(self._samples) < 10:
            return None
        return sum(self._samples) / len(self._samples)

    def p95(self) -> float | None:
        if len(self._samples) < 10:
            return None
        sorted_samples = sorted(self._samples)
        return sorted_samples[int(0.95 * len(sorted_samples))]


class FixedController(CapacityController):
    def __init__(self, limit: int):
        self._limit = limit
        self._in_flight = 0
        self._condition = asyncio.Condition()
        self._latency = LatencyWindow()

    async def acquire(self, cost: int = 1) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight + cost <= self._limit)
            self._in_flight += cost

    async def release(self, cost: int, latency_ms: float, status_code: int, timed_out: bool) -> None:
        async with self._condition:
            self._in_flight -= cost
            if not timed_out and status_code < 500:
                self._latency.record(latency_ms)
            self._condition.notify_all()  # a slot just freed up; wake any waiter that now fits

    def current_limit(self) -> int:
        return self._limit

    def mean_latency_ms(self) -> float | None:
        return self._latency.mean()
```

### 3.2 Priority Scheduler

```python
# PriorityQueue entry: (-score, timestamp, request_context, future)
# Negative score so that highest score is returned first by heapq.

def estimate_wait_seconds(queue_depth: int, concurrency_limit: int, mean_latency_ms: float | None) -> float:
    # Little's-law-style approximation: `concurrency_limit` requests served
    # in parallel, each taking ~mean_latency_ms on average, drain
    # `queue_depth` queued items ahead of a new arrival at an effective rate
    # of concurrency_limit / mean_latency_ms per ms (FR-033a).
    if mean_latency_ms is None or concurrency_limit <= 0:
        return 0.0  # not enough samples yet (cold start) — fail open, don't reject
    return (queue_depth * mean_latency_ms) / concurrency_limit / 1000.0


class PriorityScheduler(Scheduler):
    def __init__(self, queue_max_size: int, queue_timeout: float, controller: CapacityController):
        self._queue = asyncio.PriorityQueue(maxsize=queue_max_size)
        self._timeout = queue_timeout
        self._controller = controller  # source of current_limit()/mean_latency_ms() for FR-033a

    async def enqueue(self, ctx: RequestContext) -> asyncio.Future:
        projected_wait = estimate_wait_seconds(
            queue_depth=self._queue.qsize(),
            concurrency_limit=self._controller.current_limit(),
            mean_latency_ms=self._controller.mean_latency_ms(),
        )
        if projected_wait > self._timeout:
            raise QueueWaitExceededError()  # FR-033a — 429, reason=queue_wait_exceeded

        future = asyncio.get_running_loop().create_future()
        entry = (-ctx.score, ctx.arrival_time, ctx, future)
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            raise QueueFullError()  # FR-033 — 429, reason=queue_full
        return future

    async def run_worker(self, controller: CapacityController, dispatcher):
        while True:
            # Wait for a slot BEFORE popping, so the item we take is whatever
            # is currently highest-score — not whatever happened to be
            # highest-score back when we started waiting. This is only safe
            # to do generically because MVP cost is always 1 (A3): the worker
            # doesn't need to know *which* request it's about to serve before
            # it knows the request's cost.
            await controller.acquire(1)
            _, _, ctx, future = await self._queue.get()
            if future.cancelled():
                await controller.release(1, latency_ms=0, status_code=0, timed_out=False)
                continue
            asyncio.create_task(dispatcher.dispatch(ctx, future, controller))
```

### 3.3 Request Lifecycle Wiring

```
HTTP Request
  → identify backend (longest-prefix match on path_prefix, §4.1, FR-011a;
    no match → 404, FR-011c)
  → build RequestContext (score=100 default in phase 2)
  → estimate projected queue wait (§3.2, FR-033a); exceeds queue_timeout → 429
  → enqueue in backend PriorityScheduler; queue full → 429 (FR-033)
  → await Future (with queue_timeout)
  → return response or 503/429
```

### 3.4 Tasks

**Status: complete (2026-08-01).** Implemented per the approved plan (`/home/ibranco/.claude/plans/soft-mixing-moonbeam.md`). 69 unit + integration tests passing; full `docker compose up` smoke test verified all 6 backend paths still route correctly (502 on unreachable real upstreams, 404/403 unchanged from Phase 1), plus — against a temporary slow mock upstream substituted for one backend — genuine dispatch serialization under `concurrency_limit: 1` and `429 reason=queue_full` rejection under `queue_max_size: 1`.

Two deliberate deviations from this section's illustrative pseudocode (full rationale in the plan file's Context section):
- `Scheduler.next_request(backend_name)` (dead code, never called by the plan's own worker pseudocode) was dropped from the ABC and replaced with `run_worker(controller, dispatcher)`, matching what's actually driven at runtime.
- `enqueue()` takes the real Starlette `Request` alongside `RequestContext` (`enqueue(request, request_context)`), and threads it through the queue entry to the dispatcher — `RequestContext` alone can't carry the live body stream needed for FR-054. Correspondingly, the dispatcher method is `dispatch_queued(request, ctx, future, controller)`, not `dispatch(ctx, future, controller)`.

- [x] Implement `FixedController` with blocking `acquire()` (`asyncio.Condition`, §3.1) — `acquire()` must never return without holding the slot; no boolean result for the caller to (mis)check.
- [x] Implement `LatencyWindow` (§3.1) and feed it from `FixedController.release()` — needed for FR-033a's projected-wait estimate on fixed backends, not just adaptive ones.
- [x] Implement `PriorityScheduler` with worker coroutine that waits for a capacity slot *before* popping the next request (§3.2), so a fresher higher-score request can't be stuck behind a stale lower-score one that already claimed a slot and is blocked on it.
- [x] Implement `estimate_wait_seconds()` and the FR-033a check in `PriorityScheduler.enqueue()` (§3.2): reject with `429`/`queue_wait_exceeded` when the projection already exceeds `queue_timeout_seconds`, without touching the queue; fails open (never rejects) when fewer than 10 latency samples exist yet.
- [x] Wire request lifecycle in `main.py` middleware.
- [x] Implement queue timeout (asyncio.wait_for) and 503 response on timeout.
- [x] Implement 429 response on `QueueFullError` (FR-033, reason `queue_full`) and on `QueueWaitExceededError` (FR-033a, reason `queue_wait_exceeded`) — distinct reasons, same status code.
- [x] Unit tests: fixed controller admit/reject at boundary, queue timeout.
- [x] Unit tests: a request popped after already being cancelled releases its acquired slot back (§3.2) instead of leaking capacity.
- [x] Unit tests: `estimate_wait_seconds()` — below the 10-sample minimum returns `0.0` (never rejects); at/above it, a queue depth × mean latency that exceeds `queue_timeout_seconds` raises `QueueWaitExceededError` before the queue is anywhere near `queue_max_size`.
- [x] Load test: verify fixed concurrency limit is enforced under sustained traffic, including under burst load — regression test for the original over-admission bug where a false/ignored `acquire()` result let dispatch proceed anyway. (`tests/integration/test_proxy_e2e.py::test_concurrency_limit_of_one_genuinely_serializes_dispatch` and `::test_burst_beyond_queue_capacity_yields_queue_full_429`.)

---

## 4. Phase 3 — Request Classification and Scoring Engine

**Goal:** Assign meaningful priority scores to every request using Redis-backed counters for IP, subnet, ASN, and country signals, with a configurable exempt-country list (default: Portugal) that skips subnet/ASN/country penalties.

### 4.1 Request Classifier

Inputs extracted per request:

| Field | Source |
|---|---|
| backend | Matched against each backend's `match.path_prefix` — see §2.3. Matching is longest-prefix-wins (FR-011a); a path matching no configured backend resolves to a `404` response (FR-011c) rather than a backend value. Resolves to one of: `page-search-api`, `image-search-api`, `pywb-framed`, `pywb-noframe`, `pywb-patching`, `pywb-archivepagenow`. |
| request_type | Derived from backend: search-page, search-image, replay-framed, replay-noframe, patching, archivepagenow |
| user_class | Authorization header / session token verification |
| source_ip | Resolved by the trusted-proxy ingress check (`ingress` config, §2.3; FR-010a): the `X-Forwarded-For` entry `xff_trusted_hops` from the right, taken only when `REMOTE_ADDR` is in `trusted_proxies`. A request from an untrusted peer is rejected `403` before it ever reaches the classifier. |
| subnet_24 | Computed from source_ip |
| asn | GeoIP/ASN database lookup (local cache with TTL) |
| country | GeoIP lookup (local cache with TTL) |
| user_id | From verified auth token |
| estimated_cost | From request type and backend cost model |

### 4.1a UserClass Enum

```python
class UserClass(str, Enum):
    ANONYMOUS = "anonymous"
    RESEARCHER = "researcher"           # authenticated
    SERVICE_ACCOUNT = "service_account"
    INTERNAL = "internal"
    UNKNOWN = "unknown"                 # verification failed or ambiguous
```

Identity-only: this reflects *who is asking*, resolved once during classification (`requirements.md` FR-012). It is **not** used to encode behavior — there is no `suspicious`/`bot` member. A client that behaves abusively is still one of the five classes above; its effective score drops through the per-IP/subnet/ASN/country/user penalties in §4.2, not through a separate identity guess that would double-count the same abuse signal (see `docs/decision_log.md` A4).

### 4.2 Scoring Formula

`config` below is the backend's already-resolved `ResolvedScoringConfig` (see §2.3 "Scoring config resolution") — `base_scores`, `exempt_countries`, and per-dimension penalty thresholds have already had any `scoring.overrides.<backend>` entries merged in before this function is ever called. `store` is a `PenaltyStore` (§2.2) — `calculate_score` and the `*_penalty` helpers below depend only on that interface, never on a Redis client directly; `RedisPenaltyStore` (§4.5) is the only production implementation (`docs/decision_log.md` D3).

```python
async def calculate_score(ctx: RequestContext, store: PenaltyStore, config: ResolvedScoringConfig) -> int:
    base = config.base_scores[ctx.user_class]
    is_exempt = ctx.country in config.exempt_countries

    penalty = 0
    penalty_ip = await ip_penalty(ctx.source_ip, ctx.backend, store, config.penalties.ip)
    penalty_net24 = await net24_penalty(ctx.subnet_24, ctx.backend, store, config.penalties.net24)
    penalty_asn = await asn_penalty(ctx.asn, ctx.backend, store, config.penalties.asn)
    penalty_country = await country_penalty(ctx.country, ctx.backend, store, config.penalties.country)
    penalty_user = await user_penalty(ctx.user_id, ctx.backend, store, config.penalties.user)

    penalty += penalty_ip
    penalty += penalty_user
    if not is_exempt:
        # Counters above are still incremented (for observability) even when
        # exempt; only their contribution to the score is skipped here.
        penalty += penalty_net24
        penalty += penalty_asn
        penalty += penalty_country

    final = clamp(base - penalty, min_score=config.score_clamp.min, max_score=config.score_clamp.max)
    ctx.score_breakdown = ScoreBreakdown(base, penalty_ip, penalty_net24, penalty_asn,
                                          penalty_country, penalty_user, is_exempt, final)
    return final
```

Each `*_penalty` function applies the soft/hard step function from its `PenaltyConfig` (window_seconds, soft/hard threshold, soft/hard penalty — see §4.4): below `soft_threshold` → 0; at/above `soft_threshold` and below `hard_threshold` → `soft_penalty`; at/above `hard_threshold` → `hard_penalty`.

`config.penalties.<dimension>` is a **list** of `PenaltyConfig` windows (§2.3) — every dimension has at least one; `user` has two (a 60s burst window and a 3600s sustained window). Each `*_penalty` function evaluates every window in its list independently against its own Redis key (§4.3) and **sums** the resulting per-window penalties into the single value it returns to `calculate_score` above — consistent with how `calculate_score` itself sums penalties across dimensions. There is no quota or hard-cutoff path anywhere in this formula: every outcome, at any window, in any dimension, is a score adjustment, never a block.

### 4.3 Redis Key Schema

```
rl:ip:{ip}:{backend}                  TTL = 10s
rl:net24:{prefix24}:{backend}         TTL = 60s
rl:net6:{prefix6}:{backend}           TTL = 60s
rl:asn:{asn}:{backend}                TTL = 60s
rl:country:{cc}:{backend}             TTL = 300s
rl:user:{uid}:{backend}:60            TTL = 60s    (burst window)
rl:user:{uid}:{backend}:3600          TTL = 3600s  (sustained window, §4.4)
```

### 4.4 Penalty Thresholds (Initial Values — tune with production data)

These are the `scoring.default_penalties` values from `config/backends.yaml` (§2.3) — that file is canonical if the two ever disagree. `page-search-api` and `image-search-api` override the `ip` row (see `scoring.overrides` in §2.3) since the Search APIs' per-request cost is much higher than pywb's.

| Dimension | Window | Soft threshold | Hard threshold | Soft penalty | Hard penalty |
|---|---|---|---|---|---|
| IP | 10s | 10 req | 30 req | -10 | -40 |
| IPv4 /24 | 60s | 50 req | 200 req | -10 | -40 |
| IPv6 prefix (`/56` default, `/48` configurable) | 60s | 50 req | 200 req | -10 | -40 |
| ASN | 60s | 200 req | 1000 req | -20 | -70 |
| Country | 300s | 500 req | 2000 req | -5 | -30 |
| Authenticated user — burst | 60s | 50 req | 200 req | -5 | -40 |
| Authenticated user — sustained | 3600s | 500 req | 2000 req | -10 | -60 |

`user` is the only dimension with more than one window (§4.2, A7 in `docs/decision_log.md`) — the burst and sustained rows above are both applied and summed, never chosen between.

### 4.5 Tasks

- [x] Implement `classifier.py` (path → backend, path → request_type, auth → user_class).
- [x] Integrate GeoIP/ASN lookup library (`maxminddb`) reading the two local database files at `geoip.city_db_path`/`geoip.asn_db_path` (§2.3, FR-013a) with a local TTL cache, each failing open independently; the running AAC process never fetches either file itself.
- [x] Implement `scripts/update_geoip_db.py` — a standalone CLI command (run manually or by deployment automation, never invoked by the AAC process) that downloads one MaxMind GeoLite2 edition per invocation (`--edition {GeoLite2-City,GeoLite2-ASN} --dest-path PATH`) from MaxMind's direct HTTP download API (HTTP Basic Auth via `MAXMIND_ACCOUNT_ID`/`MAXMIND_LICENSE_KEY`), validates it by checking `metadata().database_type` matches the requested edition, and writes it atomically; the AAC only picks up a refreshed database on its next restart (FR-013a).
- [x] Implement `ScoreEngine` against the `PenaltyStore` interface (§2.2) — never a direct Redis client — backed by `RedisPenaltyStore` (async `INCR`+`EXPIRE`), the sole production implementation (`docs/decision_log.md` D3).
- [x] (tests only) Implement an in-memory `FakePenaltyStore` for unit tests — not a supported deployment configuration; never referenced from `config/backends.yaml` or environment-variable wiring.
- [x] Implement penalty functions per dimension, each taking its resolved list of `PenaltyConfig` windows (one or more per dimension — `user` has two, §2.3/§4.2) and summing the per-window soft/hard results, rather than hardcoded constants.
- [x] Implement exempt-country logic: skip net24/net6/asn/country penalty contribution when `ctx.country` is in `config.exempt_countries`, while still incrementing the underlying Redis counters and logging a `country_exempt` flag.
- [x] Log full score decomposition as structured JSON per request.
- [x] Unit tests: classification rules, penalty calculation, score clamping.
- [x] Unit tests: exempt-country requests skip net24/asn/country penalties but still receive ip/user penalties.
- [x] Unit tests: `page-search-api`/`image-search-api` use their overridden `ip` penalty thresholds; other backends use the global default unchanged.
- [x] Unit tests: the `user` dimension sums penalties across its 60s and 3600s windows independently (e.g. hard on the burst window + soft on the sustained window ⇒ both penalties applied).
- [x] Integration tests: verify score reflects correct Redis counter state.

---

## 5. Phase 4 — Adaptive Concurrency Controller

**Goal:** Implement a p95-based adaptive concurrency controller for the Search API and pywb backends.

### 5.1 Latency Sampling

`LatencyWindow` (mean + p95) was already introduced in Phase 2 §3.1 — moved
there because FR-033a's projected-wait estimate needs it for fixed backends
too, not only adaptive ones. `AdaptiveController` below reuses it unchanged
for its `p95()` reads.

Timeout rate and 5xx rate use the same rolling-window shape, over a boolean outcome instead of a continuous sample:

```python
class RateWindow:
    def __init__(self, window_size: int = 100):
        self._outcomes: deque[bool] = deque(maxlen=window_size)

    def record(self, matched: bool) -> None:
        self._outcomes.append(matched)

    def rate(self) -> float:
        if not self._outcomes:
            return 0.0
        return sum(self._outcomes) / len(self._outcomes)
```

### 5.2 Adaptive Controller

```python
class AdaptiveController(CapacityController):
    def __init__(self, config: AdaptiveConfig):
        self._limit = config.initial_concurrency
        self._min = config.min_concurrency
        self._max = config.max_concurrency
        self._target_p95 = config.target_p95_ms
        self._timeout_rate_threshold = config.timeout_rate_threshold
        self._error_rate_threshold = config.error_rate_threshold
        self._latency = LatencyWindow()
        self._timeouts = RateWindow()
        self._errors = RateWindow()
        self._cooldown_until: float = 0
        self._in_flight = 0
        self._condition = asyncio.Condition()

    async def acquire(self, cost: int = 1) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight + cost <= self._limit)
            self._in_flight += cost

    async def release(self, cost, latency_ms, status_code, timed_out):
        async with self._condition:
            self._in_flight -= cost
            self._timeouts.record(timed_out)
            self._errors.record(status_code >= 500 and not timed_out)
            if not timed_out and status_code < 500:
                self._latency.record(latency_ms)
            self._condition.notify_all()  # in_flight dropped; a waiter may now fit

    def current_limit(self) -> int:
        return self._limit

    def mean_latency_ms(self) -> float | None:
        return self._latency.mean()  # feeds FR-033a's projected-wait estimate, same as FixedController

    async def _adjust_loop(self, interval: float = 30.0):
        while True:
            await asyncio.sleep(interval)
            if time.monotonic() < self._cooldown_until:
                continue
            await self._adjust()

    async def _adjust(self):
        p95 = self._latency.p95()
        if p95 is None:
            return

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

        if self._limit != old_limit:
            log_limit_change(old_limit, self._limit, p95)
            metrics.adaptive_limit_changes_total.inc()
            if self._limit > old_limit:
                # Raising the limit must immediately unblock any acquire()
                # already waiting — otherwise a queued request only benefits
                # from the new limit on the next arrival, not the ones
                # already blocked (see §5.3 tasks).
                async with self._condition:
                    self._condition.notify_all()
```

### 5.3 Tasks

- [x] Implement `AdaptiveController` with adjustment loop and blocking `acquire()` (`asyncio.Condition`, §5.2 — same pattern as `FixedController`, §3.1); reuses the `LatencyWindow` already implemented in Phase 2 §3.1, no new implementation needed.
- [x] Wake blocked `acquire()` waiters whenever `_adjust()` raises `_limit` (§5.2) — an adaptive increase must be able to admit already-queued requests immediately, not just future ones.
- [x] Implement cooldown period logic.
- [x] Implement `RateWindow` for timeout-rate and 5xx-rate tracking (§5.1); wire into `AdaptiveController.release()`/`_adjust()` (§5.2) — replaces the placeholder `self._timeout_rate` reference with an actual rolling-window computation, and adds the previously-unimplemented 5xx-rate cooldown branch from the adjustment table (`requirements.md` §6.5).
- [x] Log all limit change events with old/new limits and triggering p95.
- [x] Unit tests: adjustment table, cooldown, min/max bounds.
- [x] Unit tests: raising `_limit` at runtime unblocks an already-waiting `acquire()` immediately.
- [x] Unit tests: `_adjust()` triggers the timeout-rate and error-rate cooldown branches at their configured thresholds, independent of p95.
- [x] Simulation tests: feed synthetic latency curves; verify expected limit trajectory.

---

## 6. Phase 5 — Full Observability ✅

**Goal:** Complete Prometheus metrics, structured JSON logs, and the administrative API.

### 6.1 Prometheus Metrics

Register all metrics defined in `requirements.md §6.8` using `prometheus_client`:

```python
from prometheus_client import Counter, Gauge, Histogram

inflight_requests   = Gauge("admission_inflight_requests", "...", ["backend"])
inflight_tokens     = Gauge("admission_inflight_tokens", "...", ["backend"])
concurrency_limit   = Gauge("admission_concurrency_limit", "...", ["backend"])
queue_size          = Gauge("admission_queue_size", "...", ["backend"])
requests_total      = Counter("admission_requests_total", "...", ["backend", "class", "exempt"])
admitted_total      = Counter("admission_admitted_total", "...", ["backend", "class", "exempt"])
rejected_total      = Counter("admission_rejected_total", "...", ["backend", "class", "reason", "exempt"])
queue_timeout_total = Counter("admission_queue_timeout_total", "...", ["backend"])
backend_latency     = Histogram("backend_request_duration_seconds", "...", ["backend", "class"])
backend_errors      = Counter("backend_errors_total", "...", ["backend"])
backend_timeouts    = Counter("backend_timeouts_total", "...", ["backend"])
limit_changes       = Counter("adaptive_limit_changes_total", "...", ["backend"])
score_distribution  = Histogram("score_distribution", "...", ["backend", "exempt"], buckets=range(-100, 110, 10))
queue_wait          = Histogram("queue_wait_duration_seconds", "...", ["backend", "class"])
```

`exempt` is `"true"`/`"false"`, reflecting `requirements.md` FR-022a's exempt-country list. Exempt-country traffic is still counted in these metrics' base totals — the label only adds a breakdown dimension for the §14 anomaly-review mitigation; it never excludes or redirects the count.

### 6.2 Structured Logging

Every admission decision emits one JSON log line:

```json
{
  "event": "admitted",
  "backend": "page-search-api",
  "user_class": "anonymous",
  "source_ip": "1.2.3.4",
  "asn": "AS12345",
  "country": "PT",
  "country_exempt": true,
  "score": {
    "base": 100,
    "penalty_ip": -10,
    "penalty_net24": 0,
    "penalty_asn": -20,
    "penalty_country": 0,
    "penalty_user": 0,
    "final": 90
  },
  "cost": 1,
  "queue_wait_ms": 42,
  "backend_latency_ms": 85,
  "status_code": 200
}
```

### 6.2a Diagnostic Response Headers (opt-in, FR-074)

When `observability.debug_headers.enabled` is `true` (default `false`, §2.3), the response middleware attaches four headers to every response:

```
X-AAC-Backend: page-search-api
X-AAC-Score: 90
X-AAC-Exempt: true
X-AAC-Reject-Reason: queue_wait_exceeded   # present only when the request was rejected
```

These mirror a subset of the fields already logged per request (§6.2) — a lightweight pointer for interactive debugging (`curl -i`, browser devtools) against a live environment, not a replacement for the full per-dimension score breakdown, which stays log-only (`docs/decision_log.md` D2).

### 6.3 Administrative API Tasks

- [x] Implement `GET /admin/backends` — list backends with current policy and live metrics snapshot.
- [x] Implement `GET /admin/backends/{name}/policy` — view active backend policy (GET-only for MVP; no runtime hot-reload — see FR-084, and `docs/decision_log.md` B3).
- [x] Implement `GET /admin/backends/{name}/limit` — current limit (fixed or adaptive).
- [x] Enforce authentication on all `/admin/*` endpoints.

### 6.4 Tasks

- [x] Register all required Prometheus metrics.
- [x] Emit structured JSON log per request (admission, rejection, timeout, limit change).
- [x] Expose `/metrics` endpoint.
- [x] Implement admin API endpoints.
- [x] Implement opt-in diagnostic response headers (`X-AAC-Backend`, `X-AAC-Score`, `X-AAC-Exempt`, `X-AAC-Reject-Reason`) gated by `observability.debug_headers.enabled` (default `false`, §6.2a, FR-074).
- [x] Unit tests: headers absent by default; present with correct values when enabled, including `X-AAC-Reject-Reason` on a rejected request and its absence on an admitted one.
- [x] Write integration test: verify expected metrics are emitted under load.

---

## 7. Phase 6 — Integration and Hardening

**Goal:** Validate the full system against real or realistic backends in a staging environment. Exercise all defined failure modes.

### 7.1 Integration Tests

- [ ] Route real queries through `page-search-api` and `image-search-api` independently; verify each limit is enforced and metrics are reported per-backend.
- [ ] Route real pywb requests through all four paths (`/wayback`, `/noFrame/replay`, `/noFrame/patching`, `/save`); verify each resolves to its own backend queue.
- [ ] Verify longest-prefix-wins routing: a request path matching two configured prefixes (e.g. `/noFrame/patching` vs. a hypothetical bare `/noFrame`) resolves to the backend with the longer, more specific prefix (FR-011a).
- [ ] Verify a request path matching no configured backend returns `404` without being enqueued or scored (FR-011c).
- [ ] Simulate latency injection on `page-search-api`; verify its adaptive controller reduces its limit without affecting `image-search-api`.
- [ ] Simulate backend 5xx burst; verify limit reduction and error metrics.
- [ ] Simulate queue saturation; verify 503 responses and `queue_timeout_total` counter.
- [ ] Simulate a backlog large enough that the projected wait already exceeds `queue_timeout_seconds` while the queue is still under `queue_max_size`; verify immediate `429` with reason `queue_wait_exceeded` (FR-033a), distinct from the `queue_full` case above `queue_max_size`.
- [ ] Simulate client disconnect during queue wait; verify Future cancellation.
- [ ] Verify that saturating any one of the six backends does not affect the other five queues.
- [ ] Verify that Redis disconnection makes `/readyz` report not-ready (FR-083a) while admission continues to work normally — scoring fails open (base score only, zero penalties) and the fixed/adaptive capacity limiters keep protecting backends unaffected — and that an alert fires.

### 7.2 Load Testing

Tools: `locust` or `k6`.

Scenarios:

1. **Baseline** — Ramp from 10 to 500 concurrent clients against `page-search-api`, then repeat against `image-search-api`. Record p95 latency and concurrency limit evolution for each independently.
2. **Priority** — Mix 80% low-score bots and 20% high-score users. Verify high-score users are served faster.
3. **Distributed abuse** — Many IPs from same ASN. Verify ASN penalty reduces their priority without blocking legitimate traffic.
4. **Exempt country** — Many IPs from the same ASN/subnet, all geolocated to an exempt country (e.g., PT). Verify subnet/ASN/country penalties are skipped while per-IP and per-user penalties still apply.
5. **Fixed backend** — Saturate patching backend; verify fixed limit holds.
6. **Recovery** — Remove load after saturation; verify adaptive limit recovers.

### 7.3 Security Hardening

- [ ] Verify that client-supplied priority headers are ignored.
- [ ] Verify that auth state is only derived from verified upstream headers.
- [ ] Verify that a spoofed `X-Forwarded-For` from a peer not in `trusted_proxies` is rejected `403`, never trusted as the client IP (FR-010a).
- [ ] Verify admin API requires authentication; returns 401 without credentials.
- [ ] Verify diagnostic response headers (§6.2a, FR-074) are absent when `observability.debug_headers.enabled` is left at its default (`false`); enabling it exposes exactly the four documented headers, nothing more.
- [ ] Verify that large request bodies (e.g. a multi-GB WARC-backed resource) are streamed through without full in-memory buffering (FR-054) and that malformed headers are handled without crash. Maximum body/header size is enforced upstream by Apache httpd, not by the AAC — out of scope here (§4.2 Non-Goals, `requirements.md`).

---

## 8. Phase 7 — Production Deployment

**Goal:** Roll out single-instance AAC to production with monitoring and rollback plan.

### 8.1 Deployment Steps

1. Deploy AAC in shadow mode behind Apache httpd (receive traffic, forward directly, observe metrics without enforcing limits).
2. Enable dry-run mode (classify and score but do not enforce admission).
3. Validate that classification, scoring, and metrics are correct against real traffic.
4. Enable fixed controllers for `pywb-patching` and `pywb-archivepagenow` (low-risk, predictable).
5. Enable fixed controllers for `pywb-framed` and `pywb-noframe` with conservative limits.
6. Enable adaptive controller for `page-search-api` and `image-search-api` independently, each with conservative initial and min limits.
7. Monitor p95 latency, limit evolution, queue depth, and rejection rate daily for 2 weeks.
8. Tune thresholds and scoring penalties based on observed production data.
9. Run `scripts/update_geoip_db.py` to refresh the local GeoIP/ASN database, then perform a rolling restart to pick it up (FR-013a) — establish this as a recurring operational step; the exact cadence/automation is outside the scope of this document.

### 8.2 Rollback Plan

- Keep original direct Apache → backend routing as a fast fallback.
- AAC can be bypassed by updating Apache httpd `ProxyPass` rules; no backend changes required.
- All threshold and penalty configuration is in `config/backends.yaml`; changes require a process restart to take effect (FR-084 — no runtime hot-reload for MVP).

### 8.3 Initial Production Limits (Tentative — validate with load tests)

| Backend | Controller | Initial limit | Min | Max |
|---|---|---|---|---|
| `page-search-api` | Adaptive | 50 | 10 | 300 |
| `image-search-api` | Adaptive | 50 | 10 | 300 |
| `pywb-framed` | Fixed → Adaptive | 50 | — | — |
| `pywb-noframe` | Fixed → Adaptive | 100 | — | — |
| `pywb-patching` | Fixed | 10 | — | — |
| `pywb-archivepagenow` | Fixed | 5 | — | — |

*These numbers must be validated with production load tests before enforcement. `image-search-api` currently mirrors `page-search-api` as a placeholder — no separate baseline exists yet; treat as the least-validated row in this table.*

---

## 9. Technology Stack

| Component | Choice | Reason |
|---|---|---|
| Language | Python 3.12+ | Team expertise; async ecosystem; matches existing arquivo.pt stack. |
| HTTP Framework | FastAPI | Native async, clean middleware, Pydantic integration. |
| ASGI Server | Uvicorn | Low overhead; async I/O native. |
| Backend HTTP Client | `httpx` async | Async, connection pooling, streaming support. |
| Global State | Redis (shared, async), behind a `PenaltyStore` interface | Required for distributed abuse signal aggregation. The interface exists for testability (a fake store in unit tests, §4.5); Redis is the only supported production implementation — no other backing store is planned (`docs/decision_log.md` D3). |
| Local Cache | TTL dict or `cachetools` | ASN/GeoIP lookups; non-critical. |
| Metrics | `prometheus_client` | Standard; required for Grafana/alerting integration. |
| Logging | `structlog` or stdlib JSON formatter | Structured JSON logs per request. |
| Config Validation | Pydantic v2 | Type-safe backend policy schema. |
| GeoIP/ASN | `maxminddb` + MaxMind GeoLite2 | Free ASN and country data; local-file-only lookup at `geoip.city_db_path`/`geoip.asn_db_path` (§2.3, two files — the free tier has no combined edition) — the AAC never fetches either itself. `scripts/update_geoip_db.py` is the separate, standalone command that refreshes one edition per invocation (FR-013a). |
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
