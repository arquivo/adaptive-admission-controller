# AAC — Design Decision Log

| Field | Value |
|---|---|
| Status | Historical record — all decisions below are final and already applied |
| Owner | Ivo Branco |
| Companion docs | `docs/requirements.md`, `docs/implementation_plan.md` |

## Purpose

This document records the design decisions behind the AAC requirements and
implementation plan: the problem each one addressed, the options considered,
and the final call. It exists so that anyone who later asks "why is it built
this way?" doesn't have to reverse-engineer intent from the spec text alone.

Every decision below is already reflected in `docs/requirements.md` and
`docs/implementation_plan.md` — this is the rationale trail, not a place to
track further changes. Open/unresolved items (placeholder values, undecided
conventions) live in `docs/open_tbd.md`, not here.

- **Part A** — high-stakes design decisions that needed a stakeholder call.
- **Part B** — mechanical reconciliations (internal contradictions in the docs).
- **Part C** — new requirements drafted to close gaps found during review.

---

## Part A — Design decisions

### A1 — Trusted-proxy / `X-Forwarded-For` handling is undefined (High)

**Where:** `implementation_plan.md:300` (`source_ip` from `X-Forwarded-For` or
`REMOTE_ADDR`); depends on it: FR-010, FR-022/022a, whole §6.3 scoring, PT
exemption.

**Problem:** Every abuse signal (IP, /24, IPv6 prefix, ASN, country) *and* the
`PT` exemption are computed from the client IP taken from XFF. Apache/Caddy sits
in front, so the real client IP is inside XFF — but nothing specifies how XFF is
trusted. If a client can spoof XFF, it can forge its IP/ASN/country to dodge
penalties or fraudulently claim exempt-country status. This is foundational: the
scoring model is only as trustworthy as the source IP.

**Options:**
- (a) Trusted-proxy allowlist: accept XFF only from configured front-end proxy
  IPs; else fall back to `REMOTE_ADDR`.
- (b) Fixed trusted hop count: strip N rightmost XFF entries (N = number of known
  proxies) and take the next as client IP.
- (c) Rely on the front-end proxy to overwrite XFF with a single trusted value
  (e.g. Apache `RemoteIPHeader`), and AAC reads only `REMOTE_ADDR`/a dedicated
  header.

**Recommendation:** (c) if the front-end can be configured to set an
authoritative header (simplest, least spoofable), with (a) as the AAC-side
guard. Add a **Must** FR in §6.2 and a spoofing test in §7.3. Link to A6/A8
(exemption safety depends on this).

**Decision:** Hybrid of (a) with a hard reject (no silent fallback):
- Trust `X-Forwarded-For` **only** when the direct TCP peer (`REMOTE_ADDR`) is
  in a configured trusted-proxy allowlist (known Apache/Caddy front-end IPs).
- If `REMOTE_ADDR` is **not** on the allowlist, reject the request with
  **`403 Forbidden`** — do not fall back to treating `REMOTE_ADDR` as the
  client IP. AAC must only ever be reached via a known front-end.
- When trusted, the client IP is the **rightmost** XFF entry by default (the
  hop immediately before the trusted proxy — safest against spoofing since
  only trusted infra can append entries after it). Make the trusted-hop count
  configurable (default `1`) so the web-server operator can override it if a
  proxy chain (e.g. CDN + Apache) sits in front.

---

### A2 — Phase-2 scheduler worker over-admits (High)

**Where:** `implementation_plan.md:255-261` (`run_worker`) +
`implementation_plan.md:224-233` (`FixedController.acquire` returns `bool`,
non-blocking).

**Problem:** `run_worker` does `await controller.acquire(ctx.cost)` then
unconditionally `create_task(dispatch(...))`. `acquire` returns `False` when the
limit is full but the worker ignores the result and dispatches anyway — so the
concurrency cap never actually holds. The core admission mechanism is wrong as
written.

**Options:**
- (a) Blocking acquire: worker awaits until a token frees (condition variable /
  custom gate). Handles adaptive limit changes cleanly.
- (b) `asyncio.Semaphore(limit)` the worker awaits before dequeuing — simple, but
  awkward for the adaptive controller whose limit changes at runtime (semaphore
  size is fixed).
- (c) Check the bool and re-queue on `False` — busy-loops; not recommended.

**Recommendation:** (a) — a `CapacityController` that exposes an awaitable
"wait for slot" primitive; the worker waits for a slot, *then* pops the
highest-score request, then dispatches. Update the §2.2 interface, §3.1
`FixedController`, and §3.2 worker to match. Note the adaptive controller must
release waiters when it raises its limit.

**Decision:** (a) — blocking acquire. `CapacityController` exposes an
awaitable "wait for slot" primitive; the worker blocks on it, then pops the
highest-score request from the queue, then dispatches. The adaptive
controller must wake waiters when it raises its limit at runtime.

---

### A3 — Cost model is referenced everywhere but never defined (High)

**Where:** glossary "ArchivePageNow = 10 tokens" (`requirements.md:37`);
`estimate_cost`/`ctx.cost` in interfaces + controllers; `request_cost_model:
default` on Solr backends only (`implementation_plan.md:132`, pywb entries omit
it); `pywb-archivepagenow` protected by `concurrency_limit: 5`; FR-046 "Should"
(`requirements.md:200`).

**Problem:** No table maps request types → token costs, and ArchivePageNow is
actually protected by a low fixed limit (5), not by a cost of 10. A `cost=10`
request can never fit a `limit=5` backend, so cost and limit must be designed
together. "default" cost model is undefined.

**Options:**
- (a) MVP uses **uniform cost = 1**; backend protection comes from fixed/adaptive
  limits only. Mark the "10 tokens" example as illustrative/future; make FR-046
  explicitly post-MVP.
- (b) MVP uses **weighted cost**: define the token table (which request types
  cost what), define where cost models live in the schema, and re-check every
  fixed limit against max request cost.

**Recommendation:** (a) for MVP (simpler, and low fixed limits already isolate
the expensive backends), with the weighted model kept as a documented post-MVP
lever. Either way, resolve the glossary vs. config mismatch and the
Solr-only `request_cost_model` field (see B — tie-in).

**Decision:** (a) — uniform cost = 1 for MVP. Backend protection comes
entirely from fixed/adaptive concurrency limits, not per-request cost. The
"ArchivePageNow = 10 tokens" glossary example becomes illustrative/future
only; FR-046 is explicitly post-MVP. Drop the Solr-only `request_cost_model`
field for MVP (uniform cost makes it a no-op) — see B6.

---

### A4 — Three conflicting user-class taxonomies (High)

**Where:** FR-012 (`requirements.md:147`): *anonymous, authenticated researcher,
service account, internal*. Base-score table (`requirements.md:171-177`):
*anonymous occasional, authenticated researcher, unknown, suspicious, bot*.
`backends.yaml base_scores` (`implementation_plan.md:92-96`): *anonymous,
researcher, unknown, suspicious, bot*.

**Problem:** `service_account` and `internal` have **no base score** anywhere;
`unknown`/`suspicious`/`bot` have base scores but aren't classification outputs
in FR-012. Worse, "suspicious"/"bot" conflate an *identity class* with a
*penalty outcome* — giving a bot both a low base score **and** penalties
double-counts. The plan's comment "must match §4.2 UserClass enum" points at a
§4.2 that never defines the enum.

**Options:**
- (a) Base score from a fixed **identity** set only (anonymous, researcher,
  service_account, internal, unknown); "suspicious"/"bot" are *derived from
  penalties*, not base-score classes. Add base scores for service_account +
  internal; remove suspicious/bot from `base_scores`.
- (b) Keep suspicious/bot as base classes but define a strict rule that prevents
  penalty double-counting (e.g. penalties don't apply once classified bot).

**Recommendation:** (a) — cleaner and avoids double-counting. Pin the canonical
`UserClass` enum in a new §4.1a/§4.2 of the plan and reference it from FR-012,
the §6.3 table, and `base_scores`. This has a mechanical follow-through in Part B
once the set is chosen.

**Decision:** (a), with "suspicious"/"bot" kept as a **derived** concept, not
a base-score input. `base_scores` becomes an identity-only enum: `anonymous`,
`researcher`, `service_account`, `internal`, `unknown` (add base scores for
`service_account` + `internal`, currently missing). There is no separate
upfront "is this a bot" classification step. Instead, "bot"/"suspicious" is
what the *existing* per-/24 subnet, per-ASN, and per-country penalties
naturally produce: a client hammering from one subnet/ASN accumulates enough
penalty that its effective score drops and it lands at the back of the queue
— that demotion **is** the bot treatment. Pin the canonical `UserClass` enum
(these 5 identity values) in a new §4.1a/§4.2, referenced from FR-012, the
§6.3 table, and `base_scores`.

---

### A5 — Redis-down fallback semantics are self-defeating as written (Medium)

**Where:** `requirements.md:370` + risk table §14 ("fall back to local counters
with degraded distributed visibility").

**Problem:** "Local counters" after a Redis outage reset all aggregate abuse
accounting to zero across fragmented per-replica state — an ongoing distributed
attack would see all penalties vanish exactly when the shared signal is lost.
Local counting is near-useless for distributed signals. The saving grace: the
capacity controllers are in-process and keep protecting backends.

**Recommendation:** State the real posture explicitly: **on Redis loss, scoring
fails open (every request gets its base score, no penalties) while fixed/adaptive
concurrency limits continue to protect backends; alert immediately.** Drop or
reframe "local counters." Update FR + §10 failure table + §14 risk mitigation.

**Decision:** On Redis loss the AAC keeps serving — no "local counters"
fallback (dropped entirely), no blocking, no crash. Scoring fails open: every
request gets its base score, zero subnet/ASN/country/IP/user penalty, while
the in-process fixed/adaptive concurrency limiters (Redis-independent)
continue to protect backends exactly as before. The only externally visible
change is `/readyz`, which flips to not-ready (per A6: config valid + Redis
reachable) so whatever consumes it — the upstream blue/green branch HA switch,
monitoring/alerting — can react; this is the alerting mechanism, not a
request-blocking one. Update FR + §10 failure table + §14 risk mitigation to
state this precisely; delete "local counters" wording.

---

### A6 — `/readyz` may pull the whole proxy on one backend down (Medium)

**Where:** §6.9 ("Readiness including config and backend reachability",
`requirements.md:273`), FR-083 (`requirements.md:295`).

**Problem:** If readiness aggregates *backend* reachability and one pywb is down,
an orchestrator (k8s) pulls the entire AAC out of rotation — killing traffic to
the five healthy backends too.

**Recommendation:** Define readiness as **config valid + Redis reachable** (the
dependencies the AAC itself needs to function). Report per-backend health
separately (e.g. in `/admin/backends` and metrics) but do **not** gate process
readiness on it; a down backend returns 503 for its own path only. Adjust FR-083
+ §6.9 wording + add a failure-mode row.

**Decision:** Agreed as recommended — readiness = config valid + Redis
reachable only; per-backend reachability is reported separately
(`/admin/backends` + metrics) and never gates `/readyz`. Ties directly into
A5: Redis reachability is the one dependency that *does* gate readiness, and
losing it is meant to be visible externally via `/readyz` while the AAC keeps
serving internally.

---

### A7 — Authenticated-user quota vs. penalty is dropped (Medium)

**Where:** `requirements.md:334` (user row, TTL "Window duration / daily"),
`implementation_plan.md:348` (`rl:user ... TTL = 3600s (daily quota)`),
`backends.yaml` user penalty `window_seconds: 60`. Both source docs separate
*priority* from *quota* (PT §9, §13.2: "60s / diário").

**Problem:** One key `rl:user:{id}:{backend}` with one TTL cannot be both a 60s
parallelism-penalty window and a daily quota. The quota concept (present in both
sources) is effectively absent from the output requirements.

**Options:**
- (a) Add a second key (`rl:userquota:{id}` daily) + a quota FR; keep the 60s key
  for the parallelism penalty.
- (b) Explicitly defer daily quotas to post-MVP and keep only the 60s penalty.

**Recommendation:** (b) for MVP scope, but say so explicitly (quota ≠ priority is
a stated design principle worth preserving as a post-MVP item). Fix the §4.3 TTL
so it matches whichever window survives.

**Decision:** Neither (a) nor (b) — there is no quota/hard-cutoff concept at
all, for `user` or anything else. Instead, generalize to **multiple
independent penalty windows per dimension**: a client can be penalised on a
short window (60s, bursty abuse) *and* a long window (3600s, sustained
low-and-slow abuse) at the same time.
- Reshape `PenaltyConfig` so every dimension's `default_penalties.<dim>` entry
  is a **list** of window configs (uniform shape everywhere, not a `user`-only
  special case). `ip`/`net24`/`net6`/`asn`/`country` keep a single-element
  list (unchanged behaviour); `user` gets two: the existing 60s entry plus a
  new 3600s entry (thresholds/penalties TBD — tracked in `docs/open_tbd.md`).
- Each window is tracked independently in Redis:
  `rl:user:{id}:{backend}:{window_seconds}`, TTL = that window's
  `window_seconds` (fixes the B1-style TTL/window mismatch for this key too).
- `user_penalty()` evaluates every window's soft/hard step function
  independently and **sums** them into one `penalty_user`, consistent with how
  `calculate_score` already sums penalties across dimensions (§4.2) — no new
  combination rule needed.
- Net effect: crossing a threshold on any window never blocks the request —
  it lowers effective score, so those requests sit further back in the
  priority queue and are served only when spare backend capacity exists. This
  queue-and-serve-if-capacity behaviour *is* the enforcement; delete "daily
  quota" wording from `requirements.md:334` and
  `implementation_plan.md:348` entirely.

---

### A8 — Behavioral scoring dangles (half-present) (Medium)

**Where:** exec summary lists "behavioral patterns" as a scoring dimension
(`requirements.md:20`); Redis schema reserves `rl:behavior:{fingerprint}`
(`requirements.md:335`); but no FR drives it and the scoring formula omits
`penalty_behavior` (`implementation_plan.md:311-336`) — even though the PT
source's formula included it. MVP §12.1 correctly lists only IP/subnet/ASN/
country.

**Recommendation:** Explicitly **defer behavioral scoring to post-MVP**: remove
"behavioral patterns" from the exec-summary scoring-dimensions sentence (or mark
it future), annotate the `rl:behavior` Redis row as reserved/post-MVP, and add a
line to §12.2. Alternatively, promote it to a real FR — but that widens MVP
scope, so deferring is recommended.

**Decision:** Stronger than the recommendation — **remove entirely**, not
defer. Delete "behavioral patterns" from the exec-summary scoring-dimensions
sentence, delete the reserved `rl:behavior:{fingerprint}` row from the §4.3
Redis schema, and delete any other mention in either doc. No reserved
placeholder is carried forward; if behavioral scoring is wanted later it
comes back as a fresh FR designed against the real system, not a half-defined
carryover.

---

## Part B — Mechanical reconciliations

### B1 — IP counter TTL contradicts the 10s window (Medium)
- **Where:** `implementation_plan.md:343` says `rl:ip ... TTL = 60s`.
- **Canonical:** `backends.yaml` `ip: {window_seconds: 10}`
  (`implementation_plan.md:104`) + penalty table `requirements.md:357` (10s) +
  FR-025 "TTL equal to the measurement window."
- **Change:** §4.3 Redis schema → `rl:ip ... TTL = 10s`.

### B2 — Two Prometheus metrics defined but never registered (Medium)
- **Where:** `requirements.md` §6.8 lists 14 metrics; `implementation_plan.md`
  §6.1 (`:473-484`) registers 12.
- **Missing:** `admission_inflight_tokens` (Gauge), `admission_requests_total`
  (Counter — the received-request denominator).
- **Change:** add both to §6.1. Also reconcile the `reason` label the plan adds
  to `rejected_total` back into the §6.8 table.

### B3 — Admin PUT/drain contradicts restart-only MVP (Medium)
- **Where:** plan §6.3 (`:518-522`) implements `PUT /admin/.../policy` + `POST
  /admin/.../drain`; requirements §6.9 (`:271-277`) is GET-only; FR-084
  (`:296`) = restart-only for MVP; §12.2 defers hot-reload; §8.2 (`:590`)
  hedges "without restart if runtime reload is enabled."
- **Decision:** `/admin` is **GET-only for MVP** — no hot-reload, no drain.
  Remove `PUT /admin/.../policy` and `POST /admin/.../drain` from §6.3
  entirely (both moved out of scope, not just post-MVP-flagged). Remove the
  §8.2 "without restart if runtime reload is enabled" hedge so it aligns
  cleanly with FR-084 (restart-only). If draining is wanted later, it comes
  back as a fresh FR.

### B4 — pywb-framed/noframe controller label mismatch (Low)
- **Where:** §6.7 labels them "Adaptive" (`requirements.md:235-236`); config has
  `controller: fixed` (`implementation_plan.md:164-171`); §8.3 says "Fixed →
  Adaptive" (`:598-599`).
- **Change:** §6.7 "Controller" column → "Fixed → Adaptive" to match canonical
  config.

### B5 — FR numbering gap (Low)
- **Where:** §6.7 jumps FR-060 → FR-062 (no FR-061).
- **Change:** renumber or intentionally note the gap.

### B6 — `request_cost_model` field consistency (Low)
- **Where:** Solr backends carry `request_cost_model: default`; pywb entries
  omit it.
- **Change:** per A3 (uniform cost = 1 for MVP), drop the `request_cost_model`
  field entirely from `backends.yaml` for MVP — it's a no-op under uniform
  cost. Revisit if/when weighted cost is picked up post-MVP.

---

## Part C — New requirements rationale

### C1 — Per-backend upstream/dispatch timeout (Medium)
Schema has `queue_timeout_seconds` but no *backend* timeout, yet FR-052 detects
timeouts and the adaptive controller keys off timeout rate. Add
`backend_timeout_seconds` (+ connect timeout) per backend in §2.3. ArchivePageNow
(live capture) needs a much longer upstream timeout than Solr — this is also why
its real value is a TBD (see `docs/open_tbd.md`).

**Decision:** Add per-backend `connect_timeout_seconds`/`backend_timeout_seconds`
(new FR-053) to `config/backends.yaml` §2.3, distinct from `queue_timeout_seconds`
(which only bounds queue wait, not backend response wait). Placeholder defaults:
5s connect / 60s response for all backends except `pywb-archivepagenow` at 10s
connect / 120s response, since it performs a live capture rather than serving
from the existing archive. Values are explicitly flagged not-yet-validated
(see `docs/open_tbd.md`).

### C2 — Streaming + max body/header limits (Medium)
A replay proxy serves large archived resources; buffering full responses is a
memory risk. The EN source called for max body/header limits. Add an FR:
stream request/response bodies; enforce max header size and max request body
size (configurable). Tech stack already picks `httpx` streaming.

**Decision:** Streaming only — Ivo explicitly rejected adding a max body/header
size limit inside the AAC; that enforcement is delegated entirely to the
front-end web server (Apache httpd today, Caddy in the future) and is out of
scope here (new `requirements.md` §4.2 Non-Goals bullet). Kept as its own new
FR-054: stream request/response bodies via `httpx.AsyncClient` (no full
in-memory buffering) — a distinct concern from a size *limit*, needed for large
archived resources such as video WARC records.

### C3 — Adaptive error/timeout-rate sampling structure (Medium)
Only latency has a `LatencyWindow` (`implementation_plan.md:388-399`);
`self._timeout_rate` / 5xx rate in `_adjust` (`:434`) are referenced but never
computed. Sketch their rolling-window computation (counts over the adjust
interval) in Phase 4.

**Decision:** Mechanical fix, no design ambiguity. Add a `RateWindow` class
(deque of bools + `.rate()`) mirroring `LatencyWindow`, fixing a genuine latent
bug: `AdaptiveController._adjust()` referenced `self._timeout_rate`, which was
never initialized or updated anywhere. Also adds the previously-missing 5xx-rate
(`error_rate`) cooldown branch to `_adjust()` — `requirements.md`'s adjustment
table (§6.5) already listed a "Backend 5xx rate exceeds threshold" row that was
never implemented in code. New `timeout_rate_threshold`/`error_rate_threshold`
config fields (placeholder 0.05/0.10) added to the two adaptive (Solr) backend
blocks only — meaningless for `FixedController`. No new FR needed; already
covered by existing FR-041.

### C4 — Unroutable path behavior (Low)
Requests matching no backend have no defined behavior. Specify default-deny /
404 (relevant since Apache may forward only known paths, but should be explicit).

**Decision:** Mechanical — return HTTP `404 Not Found` for any request path
matching no configured backend, without enqueueing or scoring it (new
FR-011c).

### C5 — Longest-prefix routing semantics (Low)
`/noFrame/replay` and `/noFrame/patching` share a prefix. Specify longest-prefix
(not first-segment) matching so they resolve to distinct backends.

**Decision:** Mechanical — matching is longest-prefix-wins: among all
configured backends, the path resolves to whichever backend's `path_prefix` is
the longest match, so a more specific prefix takes precedence over a shorter
one that also matches (FR-011a, amended in place — no new FR needed).

### C6 — GeoIP/ASN DB refresh cadence (Low)
Tech stack picks MaxMind GeoLite2; FR-013 requires global accuracy. A stale DB
means wrong ASN/country. Add an operational note/FR for periodic DB refresh.

**Decision:** Ivo's exact design, preserved precisely (new FR-013a): the AAC
reads a **local** GeoIP/ASN database file only; it **never** downloads this
file itself, neither at startup nor at any restart — that would create a
runtime dependency on an external service's availability. A separate, explicit
standalone command (`scripts/update_geoip_db.py`) exists purely to
download/refresh the local `.mmdb` file. The AAC only picks up a refreshed
database on its **next restart** (a rolling restart is how the new DB gets
picked up). The actual cadence/deployment-process integration of running the
download command is explicitly out of scope for these docs (a
deployment-process detail — noted in `requirements.md` §11 and
`implementation_plan.md` §8.1).

### C7 — Exempt traffic metric label (Low)
§14 risk mitigation claims exempt traffic is "metered separately" for anomaly
review, but only the log carries `country_exempt`; metrics don't. Add an
`exempt` label (or a dedicated counter) so the anomaly-review claim is real.

**Decision:** Add an `exempt` label (`true`/`false`) to 4 existing Prometheus
metrics (`admission_requests_total`, `admission_admitted_total`,
`admission_rejected_total`, `score_distribution`), reflecting FR-022a's
exempt-country list. Per Ivo's explicit clarification: exempt-country traffic
must still be **counted within the existing aggregate totals** — the label is
an additional breakdown dimension on top of the totals, not a redirect into a
separate metric. This is what makes the §14 "metered separately... for
anomaly review" mitigation real. No new FR needed — references existing
FR-022a.
