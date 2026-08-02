# Extending the AAC

This is a guide for adding new functionality to the AAC — a new backend, a new scoring
dimension, a new capacity-controller type, or a new pluggable component entirely. Read
[Architecture](architecture.md) first if you haven't; this guide assumes familiarity with the
request lifecycle and module map described there.

## Project layout

```
app/
  main.py           app factory, lifespan wiring, proxy_handler
  config.py         Settings + AACConfig (YAML schema) + resolve_scoring_config
  interfaces.py     abstract interfaces + RequestContext + UserClass
  ingress.py        TrustedProxyMiddleware
  registry.py       BackendPolicyRegistry (path -> backend matching)
  classifier.py     RequestContext construction
  scoring.py        ScoreEngine
  penalty_store.py  RedisPenaltyStore
  capacity.py       FixedController, AdaptiveController
  scheduler.py      PriorityScheduler
  dispatcher.py     BackendDispatcher
  load_balancer.py  LeastLoadedLoadBalancer
  geoip.py          GeoIPLookup
  auth.py           JWTVerifier
  observability.py  JSON logging
  metrics.py        Prometheus metrics + /metrics
  health.py         /healthz, /readyz
  admin.py          /admin/*
  errors.py         QueueFullError, QueueWaitExceededError
config/
  backends.yaml     example/canonical policy config
scripts/
  update_geoip_db.py  standalone MaxMind download command
  load_test.py        standalone load-test tool
tests/
  unit/           one test module per app/ module, fakes.py for shared test doubles
  integration/    real-socket mock-backend end-to-end tests via httpx.ASGITransport
```

## Adding a new backend

No code change is required — this is purely a `config/backends.yaml` edit. Add an entry to the
`backends` list:

```yaml
  - name: my-new-backend
    upstreams:
      - url: http://my-new-backend:8080
      # add more entries here for a horizontally-scaled backend — see
      # Configuration Reference — Multi-instance load balancing
    # backup_upstreams:               # optional — see Configuration
    #   - url: http://my-new-backend-standby:8080   # Reference — Backup instances
    match:
      path_prefix: /my-prefix
    controller: fixed          # or adaptive
    concurrency_limit: 50      # fixed-only; see adaptive fields in configuration.md
    connect_timeout_seconds: 5
    backend_timeout_seconds: 60
    queue_max_size: 1000
    queue_timeout_seconds: 300
```

See [Configuration Reference — `backends`](configuration.md#backends) for the full field list and
the discriminated `fixed`/`adaptive` shape. Startup validation will reject a duplicate `name` or
`match.path_prefix` automatically.

If the backend needs its own scoring thresholds, add a matching entry under
`scoring.overrides.<name>` — see [Configuration Reference — `scoring`](configuration.md#scoring).

## Adding a new scoring dimension

Today's six dimensions (`ip`, `net24`, `net6`, `asn`, `country`, `user`) all follow the same
shape in `app/scoring.py`, which makes adding a seventh mechanical:

1. **`RequestContext`** (`app/interfaces.py`) — add the new field the dimension keys off (mirror
   how `asn`/`country`/`user_id` are plain optional strings).
2. **`classify()`** (`app/classifier.py`) — populate that field from the request (or leave it
   `None` if it doesn't apply to a given request, the way `subnet_24`/`subnet_6` are mutually
   exclusive).
3. **`app/config.py`** — add the dimension name to `_PENALTY_DIMENSIONS`, add a field to
   `DefaultPenalties`, and add it to `BackendOverride`'s dimension validation if overrides should
   be allowed.
4. **`app/scoring.py`** — add a `<dimension>_penalty()` function following the existing pattern
   (a one-line wrapper around `_dimension_penalty()`), call it from `calculate_score()`, and add
   it to `penalty` (respecting the `is_exempt` skip-list if the new dimension should be
   exemption-sensitive like `net24`/`net6`/`asn`/`country`, or always-applied like `ip`/`user`).
   Add the corresponding field to `ScoreBreakdown` and to `_fail_open_score()`'s zero-penalty
   construction.
5. **`config/backends.yaml`** — add a `default_penalties.<dimension>` entry (at least one window).
6. **Tests** — extend `tests/unit/test_scoring.py` following its existing per-dimension test
   shape; extend `tests/unit/fakes.py`'s `FakePenaltyStore` if it needs new key-tracking behavior.

The Redis key schema is generic across dimensions already
(`rl:{dimension}:{value}:{backend}:{window_seconds}`), so a new dimension needs no schema design
of its own.

## Adding a new capacity-controller type

`CapacityController` (`app/interfaces.py`) is the abstract interface both `FixedController` and
`AdaptiveController` implement. A third implementation (e.g. a token-bucket or externally-driven
controller) needs to:

1. Implement all four abstract methods: `acquire(cost)` (must **block** until capacity is
   available — never return a boolean the caller might ignore), `release(cost, latency_ms,
   status_code, timed_out)`, `current_limit()`, and `mean_latency_ms()` (feeds the scheduler's
   predictive queue-wait rejection — return `None` until there's a trustworthy estimate, matching
   `LatencyWindow`'s existing 10-sample minimum).
2. Add a new `controller` literal value to the discriminated union in `app/config.py`
   (`_BackendCommon` subclass, following `FixedBackendConfig`/`AdaptiveBackendConfig`), with
   whatever config fields the new controller needs.
3. Wire construction into `app/main.py`'s lifespan, alongside the existing `if backend.controller
   == "fixed" / else` branch. If the controller needs a background task (like
   `AdaptiveController.adjust_loop()`), start it there and make sure it's included in the
   `worker_tasks` list that gets cancelled on shutdown.
4. If the new controller's admission decisions should be introspectable, extend
   `app/admin.py`'s `_backend_summary()`.

Nothing else in the request path needs to change — `PriorityScheduler.run_worker()` only calls
the four interface methods, never a concrete class.

## Adding a new pluggable component in general

`BackendPolicy`, `PenaltyStore`, and `LoadBalancer` are the other `ABC`s in `app/interfaces.py`. All
currently have exactly one production implementation (`DefaultBackendPolicy` in `app/registry.py`,
`RedisPenaltyStore` in `app/penalty_store.py`, `LeastLoadedLoadBalancer` in `app/load_balancer.py`)
— this is deliberate (see [the decision log entry on `PenaltyStore`](old/decision_log.md), Part D,
D3): the abstraction exists for testability (swap in a fake for unit tests), not because multiple
production backends are planned. If you do add a second real implementation, follow the same
shape: implement every abstract method, wire construction into `app/main.py`'s lifespan, and add
config fields to select between implementations if that becomes necessary.

## Testing philosophy

The existing test suite follows a few consistent conventions — matching them keeps new tests
consistent with the rest of the codebase:

- **No new test-only dependencies without a strong reason.** `fakeredis` is the one exception
  already in `pyproject.toml`'s dev dependencies; monkeypatching exact I/O boundaries (an HTTP
  call, a `maxminddb.open_database()` call) is otherwise preferred over adding a new mocking
  library.
- **Monkeypatch at the I/O boundary, not the business logic.** E.g. `tests/unit/test_auth.py`
  monkeypatches the HTTP call inside `JWTVerifier._safe_refresh()`, not `verify()` itself.
- **Local fake/helper classes live in the test files that need them**, or in `tests/unit/fakes.py`
  if shared across modules (see `FakePenaltyStore`).
- **Before/after delta assertions for Prometheus counters** — read a metric's value before the
  action under test, then assert the delta, rather than asserting an absolute value that could be
  polluted by other tests sharing the same process-wide registry.
- **Integration tests use a real-socket mock upstream backend** (`tests/integration/conftest.py`),
  driven through the real app via `httpx.ASGITransport` — this exercises the actual streaming
  dispatch path (`app/dispatcher.py`) rather than mocking `httpx` itself.

Run the full suite with:

```bash
uv run pytest        # unit + integration tests
uv run ruff check .  # lint (line-length 100, target py312)
```

## Design constraints worth knowing before you change something

These are established, deliberate decisions — see [`docs/old/decision_log.md`](old/decision_log.md)
for the full rationale behind each:

- **Never hard-block a request.** Every rejection path (`429`/`503`) is a provable resource limit
  (queue full, projected wait exceeded, queue timeout) — there is no separate ban/deny mechanism,
  and no plan to add one to this component. Suspicious behavior is expressed entirely through
  score penalties, not through blocking.
- **Uniform request cost (`cost = 1`) for MVP.** `DefaultBackendPolicy.estimate_cost()` always
  returns `1`; backend protection comes entirely from concurrency limits, not per-request weight.
  A weighted-cost model was explicitly deferred, not designed — if picked up, it needs its own
  token table and a re-check of every fixed limit against the new max request cost.
- **`/admin/*` is GET-only, restart-only config.** No hot-reload, no runtime policy mutation —
  `docs/old/decision_log.md` B3. If you want live-tunable config, that's a new feature to design
  deliberately, not an oversight to "fix."
- **`PenaltyStore` has exactly one production backend (Redis).** Don't add a second real store
  without deciding that's actually wanted — the interface exists for testability, not multi-backend
  support (D3 above).
