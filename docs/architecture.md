# Architecture

This is the developer-facing reference for how the AAC is put together: module responsibilities,
the request lifecycle in detail, and the fail-open contract every optional dependency follows.
For *what to configure*, see the [Configuration Reference](configuration.md). For *how to extend
it*, see [Extending the AAC](development.md).

## Module map

| Module | Responsibility |
|---|---|
| `app/main.py` | FastAPI app factory + lifespan (startup/shutdown wiring) + `proxy_handler`, the single entry point every proxied request goes through. |
| `app/config.py` | `Settings` (env-driven process config) and `AACConfig` (the parsed `config/backends.yaml` schema), plus `resolve_scoring_config()`. |
| `app/interfaces.py` | The core abstract interfaces (`CapacityController`, `Scheduler`, `BackendPolicy`, `PenaltyStore`), `UserClass`, and the `RequestContext` dataclass threaded through the whole pipeline. |
| `app/ingress.py` | `TrustedProxyMiddleware` — resolves the real client IP from `X-Forwarded-For`, or rejects the request. |
| `app/registry.py` | `BackendPolicyRegistry` — longest-prefix-wins path→backend matching; owns one `DefaultBackendPolicy` per configured backend. |
| `app/classifier.py` | Builds a `RequestContext` from a request: source IP/subnets, GeoIP country/ASN, JWT-derived user class/id. |
| `app/scoring.py` | `ScoreEngine` — turns a classified `RequestContext` into a final clamped score using Redis-backed rolling penalty counters. |
| `app/penalty_store.py` | `RedisPenaltyStore` — the sole production `PenaltyStore` implementation (`INCR` + conditional `EXPIRE`). |
| `app/capacity.py` | `FixedController` and `AdaptiveController` — the two `CapacityController` implementations, plus the `LatencyWindow`/`RateWindow` rolling-sample helpers they share. |
| `app/scheduler.py` | `PriorityScheduler` — the per-backend priority queue, predictive queue-wait rejection, and the worker loop that pairs a capacity slot with the highest-score queued request. |
| `app/dispatcher.py` | `BackendDispatcher` — streams a request to its backend and streams the response back, without full in-memory buffering. |
| `app/load_balancer.py` | `LeastLoadedLoadBalancer` — the sole `LoadBalancer` implementation; picks which physical instance of a backend serves an already-admitted request, tracks per-instance health, and owns sticky-session state. |
| `app/geoip.py` | `GeoIPLookup` — reads two local MaxMind `.mmdb` files (country, ASN) at startup; per-file fail-open. |
| `app/auth.py` | `JWTVerifier` — verifies a bearer token against a Keycloak JWKS endpoint to derive `user_class`/`user_id`; fails open to "no verifiable token." |
| `app/observability.py` | JSON log formatter (`configure_logging`) and `log_admission_event()`, the one-line-per-decision structured log. |
| `app/metrics.py` | All Prometheus metric definitions and the `/metrics` route. |
| `app/health.py` | `/healthz` and `/readyz`. |
| `app/admin.py` | GET-only, bearer-token-authenticated `/admin/*` introspection API. |
| `app/errors.py` | `QueueFullError`, `QueueWaitExceededError` — the two admission-rejection exception types. |

## Request lifecycle, in detail

This is the exact sequence `app/main.py` implements, reproduced from its module docstring:

```
TrustedProxyMiddleware (403, or set request.state.client_ip)
  -> registry.match()            (404 on miss, no policy configured for this path)
  -> classify()                  (build RequestContext: IP, subnets, GeoIP, JWT)
  -> score_engine.score()        (sets ctx.score — MUST run before enqueue)
  -> scheduler.enqueue()         (429 on QueueFullError / QueueWaitExceededError)
  -> await the resulting Future, bounded by queue_timeout_seconds
                                 (503 on asyncio.TimeoutError)
  -> the Response a background worker resolved it with
```

Walking through each stage:

1. **`TrustedProxyMiddleware`** (`app/ingress.py`) runs on every request except `/healthz`,
   `/readyz`, and `/metrics`. It rejects the request with `403` unless the directly-connecting
   TCP peer is in `ingress.trusted_proxies` — there is no fallback to trusting `REMOTE_ADDR`
   directly. For a trusted peer, it resolves the real client IP from `X-Forwarded-For`, taking
   the entry `ingress.xff_trusted_hops` positions from the right, and stores it on
   `request.state.client_ip`.

2. **`registry.match(path)`** (`app/registry.py`) finds the configured backend whose
   `match.path_prefix` is the longest prefix of the request path (path-segment-boundary aware, so
   `/textsearch` doesn't accidentally match `/textsearchEVIL`). No match → `404` before any
   classification or scoring work happens.

3. **`policy.classify(request)`** (`app/classifier.py`) builds the `RequestContext`: `/24` subnet
   for IPv4 or a `/48`-or-`/56` prefix for IPv6 (whichever the source IP is), a GeoIP
   country/ASN lookup, and a JWT-derived `user_class`/`user_id`. Exactly one of `subnet_24`/
   `subnet_6` is populated, never both.

4. **`score_engine.score(ctx)`** (`app/scoring.py`) computes the final score and writes it to
   `ctx.score`. This must happen *before* `scheduler.enqueue()`, since the scheduler reads
   `ctx.score` at enqueue time to place the request in the priority queue — scoring is never done
   lazily inside the scheduler or worker.

5. **`scheduler.enqueue(request, ctx)`** (`app/scheduler.py`) first checks a *predictive*
   rejection: given the current queue depth, the backend's current concurrency limit, and its
   observed mean latency, would a new arrival's projected wait already exceed
   `queue_timeout_seconds`? If so, raise `QueueWaitExceededError` (`429`,
   `reason=queue_wait_exceeded`) without ever enqueueing. Otherwise, try to place `(-score,
   arrival_time, ctx, request, future)` onto a bounded `asyncio.PriorityQueue`; if it's already at
   `queue_max_size`, raise `QueueFullError` (`429`, `reason=queue_full`). On success, return the
   `Future` the caller will await.

6. The handler awaits that `Future`, bounded by `asyncio.wait_for(..., timeout=policy.config.
   queue_timeout_seconds)`. A timeout here means the request sat in the queue too long without
   being popped — `503`, `reason=queue_timeout`.

7. Meanwhile, one `PriorityScheduler.run_worker()` task per backend runs forever: it calls
   `controller.acquire(1)` (blocks until a capacity slot is free), pops the highest-score item
   currently in the queue, and hands it to `BackendDispatcher.dispatch_queued()` as a detached
   task. That dispatch first asks the backend's `LoadBalancer.select(ctx)` which physical instance
   to use (see [Instance selection](#instance-selection) below), then streams the request
   upstream, streams the response back, and finally resolves the `Future` with the resulting
   `Response` — always releasing both the capacity slot (`controller.release(...)`) and the
   instance's in-flight count (`load_balancer.release(...)`) regardless of outcome (success,
   backend timeout, backend connection error, or the future having already been cancelled because
   the original request gave up first).

Every outcome that reaches step 7's dispatch — including a backend-originated `502`/`503` — counts
as **admitted**, not rejected: `admission_rejected_total` only increments for admission-control
decisions (queue full, projected wait exceeded, queue timeout), never for the backend's own
response status. Backend-side failures are tracked separately via `backend_errors_total`/
`backend_timeouts_total`.

## Concurrency control

Two `CapacityController` implementations exist (`app/capacity.py`), both with the same blocking
contract: `acquire()` never returns a boolean — it blocks until a slot is available — so a caller
can never forget to check a result before dispatching (this guards against a real over-admission
bug from an earlier design iteration).

- **`FixedController`** — a static `concurrency_limit`, enforced via an `asyncio.Condition`.
- **`AdaptiveController`** — starts at `initial_concurrency` and is periodically adjusted by a
  background `adjust_loop()` task (default interval 30s, one task per adaptive backend, started
  and cancelled alongside the app lifespan) based on:
  - **Timeout rate** over threshold → shrink to 60% of current limit, 60s cooldown.
  - **5xx error rate** over threshold → shrink to 75%, 30s cooldown.
  - **p95 latency > 2×** target → shrink to 70%, 30s cooldown.
  - **p95 latency > 1×** target → shrink to 85%, no extra cooldown.
  - **p95 latency < 0.5×** target → grow by 5%, capped at `max_concurrency`.
  - Otherwise unchanged.

  All limit changes are clamped to `[min_concurrency, max_concurrency]`. Below 10 latency
  samples, `_adjust()` makes no change at all — it never guesses. Raising the limit immediately
  wakes any `acquire()` calls already blocked, so a newly available slot isn't wasted waiting for
  the next request's arrival to notice it.

  `LatencyWindow` (mean + p95 over the last 100 samples) is shared by both controllers — even
  `FixedController` tracks it, since the scheduler's predictive queue-wait rejection needs a mean
  service-time estimate for every backend, not just adaptive ones.

## Instance selection

`LeastLoadedLoadBalancer` (`app/load_balancer.py`) sits inside `BackendDispatcher.dispatch_queued`,
strictly *below* the admission/backpressure layer described above — it never affects whether a
request is admitted, only which of a backend's `upstreams` entries an already-admitted request is
sent to. One instance is constructed per backend in `main.py`'s lifespan and handed to that
backend's `BackendDispatcher`; a single-instance backend degenerates to a no-op.

- **Selection** picks the healthy instance with the fewest in-flight requests, incrementing that
  count in the same `asyncio.Lock`-held critical section as the pick itself — this atomicity is
  what stops a concurrent burst from all observing the same "least loaded" instance and piling
  onto it. `release()` (called from every one of `dispatch_queued`'s exit paths, mirroring
  `controller.release(...)`) decrements it again.
- **Health** is two-sided. Passively, `httpx.ConnectError`/`httpx.ConnectTimeout` (connection
  refused, or no response within `connect_timeout_seconds`) marks an instance down immediately via
  `release(instance, connect_failed=True)` — this exception pair must be caught *before* the
  existing `except httpx.TimeoutException:` branch, since `ConnectTimeout` is a subclass of both;
  swapping the order makes the down-marking branch dead code. Actively, `health_check_loop()`
  (started alongside the app lifespan, one per backend, same cadence as
  `health_check_interval_seconds`) raw-TCP-connects to only the currently-down instances and marks
  one healthy again as soon as it accepts a connection. If every instance is down, `select()` fails
  open — it still returns one rather than blocking or inventing a new rejection path; the resulting
  connection failure is just today's `502` from the unchanged exception handling.
- **Sticky sessions** key on `ctx.source_ip` (already populated by `classify()` — no new plumbing).
  A `dict[str, tuple[str, float]]` maps client IP → `(pinned instance, last-used monotonic time)`.
  A pin is honored unless it's expired (`sticky_session_ttl_seconds` since last use), its instance
  is no longer healthy, or the *fair-share eviction rule* fires: `fair_share = ceil(capacity_hint()
  / healthy_instance_count)`, where `capacity_hint` is a bound `controller.current_limit` reference
  (not a live object — keeps `LoadBalancer` decoupled from `CapacityController` as a type). A pin
  is evicted only when its instance is at or above `fair_share` *and* a healthy alternative is
  strictly below it; if every instance is equally saturated, the pin is kept. Expired entries are
  swept on the same tick as `health_check_loop`, rather than adding a second background task.
- **Observability**: `snapshot()` returns a per-instance `(url, healthy, in_flight, sticky_count)`
  list, read by `GET /metrics` (gauges, at scrape time) and `GET /admin/backends/{name}/upstreams`
  (see [API Reference](api_reference.md)).

See [Known Limitations](known_limitations.md) for what this deliberately leaves out of scope.

## Priority scheduling

`PriorityScheduler` (`app/scheduler.py`) is a per-backend `asyncio.PriorityQueue` bounded by
`queue_max_size`, ordered by `(-score, arrival_time)` — highest score first, ties broken by
arrival order (FIFO). `estimate_wait_seconds()` implements the predictive rejection described in
the lifecycle above: a Little's-law-style approximation,
`(queue_depth * mean_latency_ms) / concurrency_limit / 1000`. With fewer than 10 latency samples,
or a concurrency limit of zero, it returns `0.0` — fail-open, never reject on insufficient data.

## Scoring

`ScoreEngine.score(ctx)` (`app/scoring.py`) computes:

```
final = clamp(base_scores[ctx.user_class] - total_penalty, score_clamp.min, score_clamp.max)
```

`total_penalty` sums six independent dimensions — `ip`, `net24`, `net6`, `asn`, `country`, `user`
— each backed by one or more Redis-counted rolling windows (`app/penalty_store.py`:
`INCR` + conditional `EXPIRE`, keyed `rl:{dimension}:{value}:{backend}:{window_seconds}`). Each
window independently applies a step function: below `soft_threshold` → 0, between soft and
`hard_threshold` → `soft_penalty`, at or above hard → `hard_penalty`. Multiple windows on the same
dimension (e.g. a 60s burst window and a 3600s sustained-abuse window on `user`) are summed.

If the request's `country` is in `scoring.exempt_countries` (arquivo.pt's default: `PT`), the
`net24`/`net6`/`asn`/`country` penalties are skipped — but their Redis counters still increment
for observability, and the `ip`/`user` penalties always apply regardless of exemption. This is
what keeps Portuguese researcher traffic, which naturally looks like heavy traffic from one
country/ASN in aggregate, from being penalized for it — while still catching genuinely abusive
individual IPs or authenticated users.

Every score computation logs a full `score_breakdown` JSON line (base, each per-dimension penalty,
exemption flag, final) and attaches the same structure to `ctx.score_breakdown` for the
diagnostic response headers (see [API Reference](api_reference.md)).

## Fail-open behavior

Every optional dependency degrades gracefully rather than blocking or crashing admission:

| Dependency | Failure mode | Behavior |
|---|---|---|
| **Redis** (scoring) | Unreachable/erroring during `ScoreEngine.score()` | Falls back to base score only (zero penalties) for that request. In-process concurrency limiters are unaffected — backends stay protected. `/readyz` flips to not-ready so this is externally visible. |
| **GeoIP database** (`city_db_path` or `asn_db_path`) | Missing/unreadable file at startup | That file's lookups return `None` (`country` or `asn`); the other file, if readable, still works. Logged once at startup, never retried. |
| **Keycloak JWKS** (`auth.jwks_url`) | Unreachable at startup or periodic refresh | `JWTVerifier.verify()` reports "no verifiable token" (`UserClass.UNKNOWN` for a present-but-unverifiable header, `ANONYMOUS` for no header at all) until a refresh succeeds. Does not gate `/readyz` or block requests. |
| **A single backend** | Connection refused, reset, or unreachable | Surfaces as a `502` for that backend's path only (`backend_errors_total` increments); every other backend keeps working normally. Backend reachability is deliberately excluded from `/readyz` for exactly this reason — see below. |

`/readyz` reports **config-valid + Redis-reachable only** — per-backend reachability is
intentionally excluded, so one dead backend never pulls the whole AAC out of an orchestrator's
rotation. Per-backend health is instead exposed via `/admin/backends` and Prometheus metrics.

## Identity vs. behavior

`UserClass` (`app/interfaces.py`) is deliberately an **identity-only** enum: `ANONYMOUS`,
`RESEARCHER`, `SERVICE_ACCOUNT`, `INTERNAL`, `UNKNOWN`. There is no `SUSPICIOUS`/`BOT` member —
behavior-based demotion is entirely the job of the per-IP/subnet/ASN/country/user penalty
dimensions in `app/scoring.py`, not a separate upfront classification step. A client hammering a
backend from one subnet accumulates enough penalty that its effective score drops and it lands at
the back of the queue — that demotion *is* the bot treatment, without double-counting the same
signal through two different mechanisms.

A verified JWT only ever *raises* a request's priority (`RESEARCHER`/`SERVICE_ACCOUNT`/`INTERNAL`
base scores are all ≥ `ANONYMOUS`'s in the example config) — the AAC has no login flow of its own
and never gates access on authentication.

## Streaming

`BackendDispatcher` (`app/dispatcher.py`) never buffers a full request or response body in
memory: `httpx.AsyncClient.build_request(..., content=request.stream())` streams the incoming
body upstream, and the response is wrapped in a Starlette `StreamingResponse` that streams
`upstream_response.aiter_raw()` chunks back, closing the upstream response in a `finally` block
(guaranteed even on client disconnect or task cancellation — a plain `BackgroundTask` isn't
reliable there). This matters for arquivo.pt specifically: replayed archive resources can be
large (video WARC records), and a proxy that buffers them fully would be a memory risk.

Hop-by-hop headers (`Connection`, `Transfer-Encoding`, `Host`, etc., per RFC 7230 §6.1) are
stripped in both directions; everything else is forwarded verbatim, including repeated headers
like multiple `Set-Cookie` values (set via `response.raw_headers` directly rather than a headers
dict, which would silently collapse repeats).
