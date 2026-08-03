# Scaling Remediation Plan

Follow-up work for the "Single-process deployment: no OS-level resource limits configured" entry
in [`known_limitations.md`](known_limitations.md#single-process-deployment-no-os-level-resource-limits-configured).
Ordered by dependency, not necessarily priority — items later in the list assume earlier ones are
done.

## 1. Raise file-descriptor limits in `docker-compose.yml`

Add an explicit `ulimits: nofile: {soft: 65536, hard: 65536}` block to the `aac` service. This is
the cheapest, lowest-risk item on this list — a pure config change, no code touched — and removes
the FD ceiling as a variable before any of the load testing below.

**Files:** `docker-compose.yml`.

## 2. Make Redis's connection pool size explicit and configurable

Add `redis_max_connections: int = 100` to `Settings` (env `AAC_REDIS_MAX_CONNECTIONS`), and pass it
to `redis_asyncio.from_url(settings.redis_url, max_connections=settings.redis_max_connections)` in
`app/main.py`'s lifespan. Today the value is an implicit redis-py library default
(`app/main.py:65`) — nothing in this codebase decides it, so it can't be tuned without reading
redis-py's source. Making it explicit doesn't change behavior by itself, but is the prerequisite
for raising it once load testing (item 4) shows it's the bottleneck.

**Files:** `app/config.py` (`Settings`), `app/main.py:65`.
**Tests:** a unit test asserting `redis_asyncio.from_url` is called with the configured value
(mock/spy on `redis_asyncio.from_url`, matching the existing test style in `tests/unit/`).

## 3. Enhance `scripts/load_test.py` to actually answer "how many connections can this handle"

Current gaps, per its own docstring (`scripts/load_test.py:9-14`) and the known-limitations entry:
single URL only, no ramp-up, no sustained-connection/idle-hold mode, single-process generator.

Proposed additions, each independent and separately testable:
- **Multi-endpoint mode**: accept a list of `--url` (repeatable flag) or a small YAML/JSON target
  file, splitting `--concurrency`/`--requests` across them, so one run can exercise multiple
  backends concurrently (matching how the real AAC serves multiple backends from one process).
- **Ramp-up mode**: `--ramp-seconds N` — linearly increase concurrency from 1 to `--concurrency`
  over `N` seconds instead of starting at full concurrency immediately, to find the actual point of
  degradation rather than just pass/fail at one fixed level.
- **Sustained/idle-hold mode**: `--hold-open-seconds N` — after each response, keep that
  connection's slot occupied (e.g. a slow drip read, or just delay before releasing the semaphore)
  for `N` seconds before allowing a new request into that slot, to specifically probe keep-alive
  pool exhaustion rather than pure throughput.
- **Multi-process generator**: either document "run N copies of this script from N separate
  processes/hosts and merge the reports" as the supported pattern, or add a `--worker-processes`
  flag using `multiprocessing` to fan out `_fire()` across cores locally. Prefer documenting the
  multi-host pattern first — closer to how a real ~500-concurrent-client staging run (the §7.2
  requirement this script explicitly doesn't replace) would actually be driven.

**Files:** `scripts/load_test.py`.
**Tests:** extend `tests/unit/test_load_test.py` if one exists, else add one — cover ramp-up
scheduling and multi-endpoint request distribution as pure-function unit tests (no real network),
following the existing `_percentile`/`_report` unit-testable-helper pattern already in the file.

## 4. Run a real load test and record actual ceilings

Using items 1-3, run `scripts/load_test.py` (or the real ~500-concurrent-client staging run from
§7.2 if staging infra is available) against a docker-compose stack with the raised FD limit, and
record: at what concurrency does Redis pool exhaustion appear (watch for connection-wait latency
spikes correlating with `redis_max_connections`), at what concurrency do FDs approach the new
65536 ceiling, and what per-backend `concurrency_limit`/`queue_max_size` values are actually
sustainable given those ceilings. This determines whether items 1-2's defaults need adjusting
before shipping, and produces the first real numbers for the placeholder values already flagged in
`known_limitations.md`'s "Installation-dependent placeholder values" table.

**Output:** a short results write-up (where — `docs/deployment.md` alongside the existing
"Tentative initial production limits" section is the natural place) — not new code.

**Done 2026-08-03** — see `docs/deployment.md`'s new "Local load-test results (single-box Docker
Compose)" subsection. Result: CPU-bound contention was found, but *not* the single-core-saturation
kind item 5 below was written to address — see item 5's update.

## 5. Multi-worker/multi-process scale-out (larger, needs product sign-off first)

Only pursue this if item 4's results show single-core CPU-bound work (JWT verification, GeoIP
lookups, JSON log serialization) is the actual ceiling, not I/O/connection limits — items 1-4 may
be sufficient on their own.

**Update 2026-08-03, post item 4:** the real load test found CPU-bound contention (single process
pinned at 110-165% CPU, throughput *dropping* as offered concurrency rose past ~100-150 with 0%
rejections throughout) — but traced it to `app/capacity.py`'s `FixedController`/
`AdaptiveController` using a shared `asyncio.Condition` per backend whose `release()` calls
`notify_all()`, waking every blocked waiter on every completed request regardless of how many can
actually proceed (a wake-storm that scales with queue depth, not with real work). That's a
single-process algorithmic inefficiency, not inherent single-core saturation from legitimate
request-handling work (the stub backend itself answered in single-digit milliseconds throughout).
Full details and numbers: `docs/deployment.md`'s new subsection.

Practical implication: **item 5 (multi-process scale-out) is not yet justified by this data** —
multiplying a process that's wasting CPU on wake-storms wouldn't fix the underlying inefficiency,
just add more processes each hitting the same per-backend wall around ~100-150 concurrent in-flight
requests. The cheaper, smaller candidate fix worth considering first: replace the `Condition`-based
acquire/release in `app/capacity.py` with a synchronization primitive that wakes only as many
waiters as can proceed (e.g. `asyncio.Semaphore`, whose `release()` wakes exactly one FIFO waiter)
rather than all of them. That's a targeted change to `app/capacity.py` (and its tests in
`tests/unit/test_capacity.py`), not the sticky-session-to-Redis migration or per-worker lifespan
questions below — but it's still outside the code changes items 1-4 were scoped to make, so it
wasn't implemented as part of this remediation plan without separately confirming that scope
expansion. If pursued, it should be re-measured with the same `scripts/load_test.py` methodology
before item 5's bigger question (multi-process, which still needs the sticky-session-state
migration and per-worker lifespan sign-off below) is revisited.

Prerequisite: move `LeastLoadedLoadBalancer`'s sticky-session state (client-IP → pinned-instance
map, currently in-process memory per `known_limitations.md`'s "Multi-instance load balancing scope
limits" section) to Redis, since multiple `uvicorn --workers N` processes each get their own copy
of in-memory state today. This also needs product sign-off on behavior during the migration (does
a client's pin survive a mid-rollout restart?) — not a purely mechanical change, per the existing
known-limitations note on `CapacityController` health-awareness having the same
sign-off-needed shape.

**Files (once scoped):** `app/load_balancer.py` (sticky state backend), `app/main.py` (per-worker
lifespan implications — each `uvicorn` worker runs its own lifespan, so the `AdaptiveController`
adjust loops and worker tasks per backend would multiply per-process; needs a decision on whether
that's fine or needs consolidating), `Dockerfile`/`docker-compose.yml` (`--workers N` flag).

## Verification (each item)

1. `.venv/bin/ruff check .` and `.venv/bin/pytest -q` after any code change (items 2, 3, 5).
2. Item 1: `docker compose up` then `docker exec <container> sh -c 'ulimit -n'` to confirm the
   raised limit took effect.
3. Item 4's staging run is itself the verification for items 1-3's config choices.
