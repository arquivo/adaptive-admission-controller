# Docker & Deployment Guide

## Running locally with Docker Compose

The fastest way to get the AAC running with its one hard dependency (Redis):

```bash
docker compose up --build
```

This starts two services (`docker-compose.yml`):

- `redis` — `redis:7-alpine`, with a `redis-cli ping` healthcheck.
- `aac` — built from the repository `Dockerfile`, listening on `8000:8000`, with
  `AAC_CONFIG_PATH=/app/config/backends.yaml` and `AAC_REDIS_URL=redis://redis:6379/0` set, the
  `./config` directory mounted read-only at `/app/config`, and `depends_on: redis` gated on
  Redis's healthcheck passing.

Real backend containers are **not** part of this compose file — `config/backends.yaml`'s
`upstream_url` values point at illustrative hostnames (`page-search-api:8080`, etc.). Either point
the config at backends you can actually reach, or expect every proxied request to receive a `502`
while `/healthz`/`/readyz` still report healthy (a `502` is a per-backend failure, not an AAC
failure — see [Architecture — Fail-open behavior](architecture.md#fail-open-behavior)).

## Building and running the image directly

```bash
docker build -t aac .
docker run --rm -p 8000:8000 \
  -e AAC_REDIS_URL=redis://<your-redis-host>:6379/0 \
  -v "$(pwd)/config:/app/config:ro" \
  aac
```

### How the image is built

The `Dockerfile` is a multi-stage build:

1. **Builder stage** (`python:3.12-slim`) — copies the [`uv`](https://docs.astral.sh/uv/) binary
   from `ghcr.io/astral-sh/uv:latest`, then syncs dependencies (`uv sync --frozen
   --no-install-project --no-dev`) *before* copying application code, so dependency layers stay
   cached across code-only changes. Only after that does it copy `app/` and `config/` and run a
   final `uv sync --frozen --no-dev` to install the project itself. `UV_COMPILE_BYTECODE=1` and
   `UV_LINK_MODE=copy` are set for faster cold starts and to avoid hardlink issues across layers.
2. **Runtime stage** — copies `/app` from the builder and runs:
   ```
   uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
   ```

## Running without Docker

```bash
uv sync
uv run uvicorn app.main:create_app --factory --reload
```

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a reachable Redis instance
(`AAC_REDIS_URL`, default `redis://localhost:6379/0`).

## GeoIP/ASN database refresh

The AAC reads two local MaxMind GeoLite2 files at startup (`geoip.city_db_path` /
`geoip.asn_db_path`) and never downloads either itself — refreshing them is a separate, explicit
step, run manually or by deployment automation, never by the AAC process. A missing or corrupt
file fails open (that half of country/ASN lookups just returns `None` — see
[Architecture — Fail-open behavior](architecture.md#fail-open-behavior)); the AAC only picks up a
refreshed file on its **next restart**.

```bash
export MAXMIND_ACCOUNT_ID=... MAXMIND_LICENSE_KEY=...
uv run python scripts/update_geoip_db.py --edition GeoLite2-City --dest-path /var/lib/aac/GeoLite2-City.mmdb
uv run python scripts/update_geoip_db.py --edition GeoLite2-ASN --dest-path /var/lib/aac/GeoLite2-ASN.mmdb
```

A free MaxMind account (for `MAXMIND_ACCOUNT_ID`/`MAXMIND_LICENSE_KEY`) is required — see
<https://dev.maxmind.com/geoip/updating-databases>. The script downloads one edition per
invocation from MaxMind's direct download API over HTTP Basic Auth, validates the result by
opening it with `maxminddb.open_database()` and checking its `database_type` matches the
requested `--edition`, then writes it to `--dest-path` atomically (same-directory temp file +
`os.replace()`, so a failed or interrupted run never leaves a partially-written database in
place). Exit codes distinguish missing credentials (`2`), auth failure (`3`), download failure
(`4`), a malformed archive (`5`), and a validation failure (`6`) — see the script's own `--help`
and docstring for details.

There is no built-in scheduling for this — set up a periodic job (cron, systemd timer, or your
orchestrator's equivalent) that runs both invocations above followed by a rolling restart of the
AAC process(es) to pick up the refreshed files. The exact cadence is an operational decision for
your deployment, not something the AAC prescribes.

## Load testing

`scripts/load_test.py` is a standalone async load-test tool for a *running* AAC instance: it
fires a configurable number of concurrent `GET` requests and reports latency p50/p95/p99 plus the
response status-code distribution.

```bash
uv run python scripts/load_test.py --url http://localhost:8000/textsearch/ \
    --concurrency 50 --requests 500
```

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Target URL. |
| `--concurrency` | `20` | Max in-flight requests. |
| `--requests` | `200` | Total requests to send. |
| `--timeout` | `30.0` | Per-request timeout, seconds. |

This is a diagnostic tool for a quick concurrency/backpressure smoke test — not a substitute for a
real staging run with realistic concurrent-client counts against the real `page-search-api`/
`image-search-api` backends, which needs real staging infrastructure to be meaningful.

## Failover smoke test

`scripts/smoke_test_failover.sh` automates the failover/failback verification that used to be done
by hand (most recently for the backup-instances feature, by hand-authoring a throwaway
`docker-compose.override.yml`): it stands up the AAC via Docker Compose against two stub backend
containers (a primary and a backup, each a bare `python:3.12-slim` + `python -m http.server`
serving a distinct static body), then asserts, over the wire:

1. a baseline request is served by the primary,
2. stopping the primary produces a `502` on the next request (marking it down) and then a `200`
   from the backup on the request after that,
3. restarting the primary and waiting past `health_check_interval_seconds` produces a `200` from
   the primary again (failback).

```bash
scripts/smoke_test_failover.sh
```

It builds its own temporary config and `docker-compose.override.yml` (via `mktemp -d`), tears
everything down on exit (success or failure) via a trap, and exits non-zero on any assertion
failure — safe to run locally after touching `app/load_balancer.py` or `app/dispatcher.py`, and
this is exactly what `.github/workflows/smoke.yml` runs on a weekly schedule and via manual
`workflow_dispatch` (see [Development — Continuous integration](development.md#continuous-integration)).

## Production rollout

This section is an operational runbook, not something the AAC automates. Every step below is an
infrastructure/Apache-config action taken by whoever operates the deployment.

1. **Shadow mode.** Deploy the AAC behind Apache httpd receiving a mirrored or canary subset of
   traffic, forwarding directly, so its metrics/logs can be observed without it enforcing any
   limits yet. This is purely an Apache/infra routing decision (e.g. `mod_proxy` mirroring) — no
   AAC feature is required for this step, and no true "observe-only" mode exists inside the AAC
   itself (see [Known Limitations](known_limitations.md#no-dry-run--observe-only-mode)). Because
   of that gap, either implement a dry-run mode first, or skip straight from this step to enabling
   the lowest-risk fixed-controller backends below and lean on shadow-mode metrics plus
   conservative initial limits instead of a true dry-run pass.
2. **Validate.** Confirm classification, scoring, and metrics look correct against real traffic
   before enforcing anything.
3. **Enable the lowest-risk backends first.** `pywb-patching` and `pywb-archivepagenow` — low
   traffic volume, predictable load.
4. **Enable the higher-traffic fixed backends.** `pywb-framed` and `pywb-noframe`, with
   conservative concurrency limits.
5. **Enable the adaptive backends.** `page-search-api` and `image-search-api` independently, each
   with a conservative `initial_concurrency`/`min_concurrency`.
6. **Monitor daily for ~2 weeks.** p95 latency, limit evolution (`adaptive_limit_changes_total`),
   queue depth, and rejection rate.
7. **Tune.** Adjust `config/backends.yaml` thresholds and scoring penalties based on observed
   production data, then restart to apply (no runtime hot-reload — see below).
8. **Establish a recurring GeoIP/ASN refresh.** Run `scripts/update_geoip_db.py` for both editions
   on a schedule, followed by a rolling restart.

### Rollback

- Keep the original direct Apache → backend routing available as a fast fallback — bypass the AAC
  entirely by reverting Apache's `ProxyPass` rules. No backend-side changes are required.
- All threshold/penalty configuration lives in `config/backends.yaml`; changes require a process
  restart to take effect — there is no runtime hot-reload for `/admin/*` or the config file (GET-only
  admin API by design, see [API Reference](api_reference.md#admin-api)).

### Tentative initial production limits

These are starting points to validate with real load tests, not final numbers — see
[Known Limitations](known_limitations.md) for which config values are still installation-dependent
placeholders.

| Backend | Controller | Initial limit | Min | Max |
|---|---|---|---|---|
| `page-search-api` | Adaptive | 50 | 10 | 300 |
| `image-search-api` | Adaptive | 50 | 10 | 300 |
| `pywb-framed` | Fixed → Adaptive | 50 | — | — |
| `pywb-noframe` | Fixed → Adaptive | 100 | — | — |
| `pywb-patching` | Fixed | 10 | — | — |
| `pywb-archivepagenow` | Fixed | 5 | — | — |

`image-search-api` currently mirrors `page-search-api` as a placeholder — no separate baseline
exists yet; treat it as the least-validated row in this table.

### Local load-test results (single-box Docker Compose)

Run 2026-08-03 as part of `docs/scaling_remediation_plan.md` item 4, using the enhanced
`scripts/load_test.py` from inside the `aac` container (so the client's source IP is `127.0.0.1`,
matching `ingress.trusted_proxies`) against `docker compose up`, one backend
(`concurrency_limit: 300`, `queue_max_size: 2000`), and a threaded stub upstream with a large
listen backlog (plain `python -m http.server`'s default backlog of 5 saturates well before the AAC
does and produces misleading 502s — see caveat below). This is a single developer laptop, not
representative production hardware; treat the absolute numbers as directional, not a promise.

| Offered concurrency | Throughput | p50 | p95 | p99 | Rejected (429/503) |
|---|---|---|---|---|---|
| 50 | 327 req/s | 145 ms | 193 ms | 257 ms | 0% |
| 100 | 313 req/s | 303 ms | 439 ms | 552 ms | 0% |
| 150 | 185 req/s | 737 ms | 1139 ms | 1205 ms | 0% |
| 200 | 100 req/s | 2010 ms | 2423 ms | 2519 ms | 0% |
| 250 | 80 req/s | 2978 ms | 4285 ms | 4591 ms | 0% |

**Finding: throughput *drops* as offered concurrency rises past ~100-150, with zero rejections at
any level tested.** That inversion — more concurrent clients producing *fewer* completed
requests/sec, not just higher latency — rules out the two most obvious explanations: it isn't
`queue_max_size` rejecting (0% 429s throughout, `admission_queue_size` was back to 0 between runs),
and it isn't FD exhaustion (`/proc/net/sockstat` showed ~100 TCP sockets in use against the raised
65536 ulimit). `docker stats` showed the `aac` container's single process pinned at 110-165% CPU
during the degraded runs, confirming this is CPU-bound contention inside the process itself, not a
resource ceiling from items 1-2.

**Initial hypothesis (since revised — see the updates below):** `app/capacity.py`'s
`FixedController`/`AdaptiveController.acquire()`/`release()` are built on a single shared
`asyncio.Condition` per backend, and `release()` called `notify_all()` on every completed request —
waking *every* currently-blocked waiter even though only one (at most a few) could actually proceed.
This looked like an `O(waiters)` wake-storm that would scale with queue depth. This wasn't something
the original items 1-4 (FD limits, Redis pool sizing, load-test tooling) were scoped to touch; it
surfaced only because item 4 asked to actually run a real load test and look at what breaks first.

Two production backends (`page-search-api`, `image-search-api`) already configure `max_concurrency:
300` in the table above — per this measurement, either one approaching that ceiling in real traffic
would hit the same wall this test found at a single backend's ~150 concurrent in-flight requests,
well before reaching its own configured limit.

**Update 2026-08-03, after implementing and measuring the wake-storm fix:** `release()` was changed
to `notify(cost)` — bounded to exactly the capacity that just freed up, instead of waking every
waiter (see `app/capacity.py` and its regression tests in `tests/unit/test_capacity.py`) — and
re-measured with the same A/B methodology (toggling the change via `git stash`, rebuilding the image,
identical load sweeps, `docker stats` sampled *concurrently* with the load rather than after, and
`concurrency_limit` set deliberately below the offered range to force real contention). Result:
**no measurable difference** in throughput or CPU behavior at any concurrency level tested.

Re-reading `app/main.py`/`app/scheduler.py` explains why: exactly one `run_worker()` task exists per
backend, so at most one waiter can ever be blocked on a given backend's `asyncio.Condition` at a
time — `notify_all()` and `notify(cost)` wake the same number of waiters (at most one) in this
codebase's actual architecture. The wake-storm theory assumed multiple concurrent waiters per
backend; that assumption doesn't hold here. The `notify(cost)` change has been kept anyway as a
harmless correctness improvement — it's still the more precise call, and it guards against a future
change that adds more worker tasks per backend — but it does not explain or fix the measured
throughput inversion.

**Update 2026-08-03, real profiling with `py-spy`:** `cProfile` was tried first and produced
misleading data — it fundamentally miscounts asyncio code, attributing idle/blocked-on-I/O time to
whatever frame happened to be active when the event loop suspended, and inflating call counts because
every `await` suspend/resume registers as a fresh call/return event to a frame-based profiler.
Switching to `py-spy` (a sampling profiler that isn't fooled by cooperative yielding — attached
cross-container via a shared PID namespace, since neither `pip` nor `py-spy` are available in the
slim runtime image) gave a trustworthy picture instead:

- The process runs on a **single OS thread** throughout — `py-spy dump` never showed more than one.
- During a degraded 200-concurrency run, only ~20% of wall-clock samples were CPU-active (the rest
  genuinely idle on I/O), and that active time was spread thin across dozens of call sites — anyio
  task/timeout bookkeeping, httpcore connection handling, JSON structured logging, starlette's
  middleware chain — none individually above ~2% of samples. **There is no single hot function to
  optimize away.**

That points to genuine **single-core saturation from cumulative per-request overhead** across the
full async stack (middleware, scoring, structured logging, metrics), not an algorithmic bug — one OS
thread can only do so much Python-level work per second, and at 100-250 concurrent in-flight requests
the aggregate work needed for all of them competes for that same thread. This matches
`docs/scaling_remediation_plan.md`'s item 5 as originally framed (before the wake-storm detour):
multi-process scale-out, not a targeted `app/capacity.py` fix, is the change that would actually raise
this ceiling — see that item for the updated status and its prerequisites (sticky-session state
migration, per-worker lifespan questions, product sign-off).
