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
rejections throughout). The initial hypothesis was `app/capacity.py`'s `FixedController`/
`AdaptiveController` using a shared `asyncio.Condition` per backend whose `release()` calls
`notify_all()`, waking every blocked waiter on every completed request regardless of how many can
actually proceed (a wake-storm scaling with queue depth) — **this has since been ruled out, see the
next update.** Full details and numbers: `docs/deployment.md`'s "Local load-test results" subsection.

**Update 2026-08-03, after implementing and measuring the wake-storm fix:** the candidate fix
(`release()` calling `notify(cost)` instead of `notify_all()`) was implemented and tested
(`app/capacity.py`, `tests/unit/test_capacity.py`), then re-measured with the same load-test
methodology. Result: **no measurable improvement** at any concurrency level. Root cause: `app/main.py`
starts exactly one `run_worker()` task per backend, so at most one waiter can ever be blocked on a
given backend's condition at a time — `notify_all()` and `notify(cost)` are functionally equivalent
here regardless of queue depth. The wake-storm theory assumed multiple concurrent waiters per
backend, which this codebase's architecture doesn't allow. The `notify(cost)` change is kept as a
harmless correctness improvement (more precise, and it guards against a future change that adds more
worker tasks per backend), but it isn't the fix for the measured inversion.

**Update 2026-08-03, real profiling with `py-spy`:** since `cProfile` gives unreliable results for
asyncio code (it misattributes idle/I/O-wait time and inflates call counts around every `await`),
profiling was redone with `py-spy`, a sampling profiler, attached cross-container via a shared PID
namespace (neither `pip` nor `py-spy` are available in the slim runtime image). Findings: the process
runs on a **single OS thread**, and CPU-active time during a degraded run is spread thin across
dozens of call sites (anyio bookkeeping, httpcore, JSON structured logging, starlette middleware),
none above ~2% — **no single hot function to optimize away.** Full details:
`docs/deployment.md`'s "Local load-test results" subsection.

Practical implication: this now points to genuine single-core saturation from cumulative per-request
overhead across the whole async stack, not an algorithmic bug — **item 5 (multi-process scale-out) is
now justified by this data**, matching this section's original framing before the wake-storm detour.
There's no cheaper `app/capacity.py`-sized fix left to try; the next step is scoping item 5 itself
(sticky-session-to-Redis migration, per-worker lifespan questions, product sign-off — see below),
which is a larger piece of work than this remediation plan's items 1-4 covered.

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
