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

The likely mechanism: `app/capacity.py`'s `FixedController`/`AdaptiveController.acquire()`/
`release()` are built on a single shared `asyncio.Condition` per backend, and `release()` calls
`notify_all()` on every completed request — waking *every* currently-blocked waiter so each can
re-acquire the condition's lock and re-check its predicate, even though only one (at most a few) can
actually proceed. As offered concurrency grows past the configured `concurrency_limit`, the number
of waiters woken per release grows with it, so the overhead of *rejecting* wake-ups scales with
however many clients are queued — a wake-storm, not useful work. This wasn't something the original
items 1-4 (FD limits, Redis pool sizing, load-test tooling) were scoped to touch; it surfaced only
because item 4 asked to actually run a real load test and look at what breaks first.

Two production backends (`page-search-api`, `image-search-api`) already configure `max_concurrency:
300` in the table above — per this measurement, either one approaching that ceiling in real traffic
would hit the same wall this test found at a single backend's ~150 concurrent in-flight requests,
well before reaching its own configured limit.

Feeding this back into `docs/scaling_remediation_plan.md`'s item 5 section: **this is CPU-bound
contention, but not the kind item 5 (multi-worker/multi-process scale-out) was written to address.**
It's a single-process algorithmic inefficiency (an `O(waiters)` wake-storm on every release), not
inherent single-core saturation from legitimate request-handling work — the actual proxied requests
in these runs were fast (the stub backend answered in single-digit milliseconds). A synchronization
primitive that wakes only the waiters that can actually proceed (e.g. an `asyncio.Semaphore`, whose
`release()` wakes exactly one waiter in FIFO order, rather than a `Condition` + `notify_all()`) is a
much smaller, cheaper fix than multi-process scale-out, and would need to land and be re-measured
before item 5's bigger, product-sign-off-gated question (sticky-session state needing to move to
Redis, per-worker lifespan duplication) is worth revisiting at all.
