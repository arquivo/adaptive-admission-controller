# AAC Docs — Revision Plan

| Field | Value |
|---|---|
| Version | 0.1 (working) |
| Status | In progress — reviewing `requirements.md` + `implementation_plan.md` |
| Owner | Ivo Branco |
| Created | 2026-07-27 |
| Targets | `docs/requirements.md`, `docs/implementation_plan.md` |

## Purpose & how to use this doc

This is a **living checklist** of proposed revisions to the AAC requirements and
implementation plan, produced from a review of both output docs against the two
source docs (`docs/md/`). It exists so the analysis is not lost between working
sessions — we expect to work through it over several days.

Each item is self-contained: it names the exact location(s), states the problem,
and proposes a resolution, so you can act on it without re-reading the review.

- **Part A** — design decisions that need a stakeholder call *before* editing.
  Each has options + a recommendation and a `Decision:` line to fill in.
- **Part B** — mechanical reconciliations (internal contradictions); safe to
  apply once the Part A decisions they depend on are made.
- **Part C** — new requirements to draft.
- **Part D** — open TBDs to track (consolidated).

Check items off (`- [x]`) as they land in the docs. Record outcomes in the
**Progress log** at the bottom. Severity: **High** = will bite in implementation
or is security-critical; **Medium** = real gap/inconsistency; **Low** = cleanup.

---

## Summary table

| # | Item | Severity | Part | Status |
|---|---|---|---|---|
| A1 | Trusted-proxy / `X-Forwarded-For` handling undefined | High | A | Open decision |
| A2 | Scheduler worker over-admits (ignores `acquire()`) | High | A | Open decision |
| A3 | Cost model referenced everywhere, never defined | High | A | Open decision |
| A4 | Three conflicting user-class taxonomies | High | A | Open decision |
| A5 | Redis-down fallback semantics self-defeating | Medium | A | Open decision |
| A6 | `/readyz` may unready whole proxy on one backend down | Medium | A | Open decision |
| A7 | Authenticated-user quota vs. penalty dropped | Medium | A | Open decision |
| A8 | Behavioral scoring dangles (half-present) | Medium | A | Open decision |
| B1 | IP counter TTL 60s contradicts 10s window | Medium | B | Ready |
| B2 | Two Prometheus metrics defined but not registered | Medium | B | Ready |
| B3 | Admin PUT/drain contradicts restart-only MVP | Medium | B | Needs A-input |
| B4 | pywb-framed/noframe controller label mismatch | Low | B | Ready |
| B5 | FR numbering gap (no FR-061) | Low | B | Ready |
| C1 | No per-backend upstream/dispatch timeout | Medium | C | Ready |
| C2 | No streaming / max body+header limits | Medium | C | Ready |
| C3 | Adaptive error/timeout-rate sampling unstructured | Medium | C | Ready |
| C4 | Unroutable path behavior undefined | Low | C | Ready |
| C5 | Longest-prefix routing semantics unspecified | Low | C | Ready |
| C6 | GeoIP/ASN DB refresh cadence unspecified | Low | C | Ready |
| C7 | Exempt traffic not separately labelled in metrics | Low | C | Ready |
| D  | Consolidated open TBDs | — | D | Tracking |

---

## Part A — Design decisions (need stakeholder call before editing)

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

**Decision:** _pending_

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

**Decision:** _pending_

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

**Decision:** _pending_

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

**Decision:** _pending_

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

**Decision:** _pending_

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

**Decision:** _pending_

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

**Decision:** _pending_

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

**Decision:** _pending_

---

## Part B — Mechanical reconciliations (apply once dependent A-items resolve)

### B1 — IP counter TTL contradicts the 10s window (Medium) — Ready
- **Where:** `implementation_plan.md:343` says `rl:ip ... TTL = 60s`.
- **Canonical:** `backends.yaml` `ip: {window_seconds: 10}`
  (`implementation_plan.md:104`) + penalty table `requirements.md:357` (10s) +
  FR-025 "TTL equal to the measurement window."
- **Change:** §4.3 Redis schema → `rl:ip ... TTL = 10s`.

### B2 — Two Prometheus metrics defined but never registered (Medium) — Ready
- **Where:** `requirements.md` §6.8 lists 14 metrics; `implementation_plan.md`
  §6.1 (`:473-484`) registers 12.
- **Missing:** `admission_inflight_tokens` (Gauge), `admission_requests_total`
  (Counter — the received-request denominator).
- **Change:** add both to §6.1. Also reconcile the `reason` label the plan adds
  to `rejected_total` back into the §6.8 table.

### B3 — Admin PUT/drain contradicts restart-only MVP (Medium) — Needs A-input
- **Where:** plan §6.3 (`:518-522`) implements `PUT /admin/.../policy` + `POST
  /admin/.../drain`; requirements §6.9 (`:271-277`) is GET-only; FR-084
  (`:296`) = restart-only for MVP; §12.2 defers hot-reload; §8.2 (`:590`)
  hedges "without restart if runtime reload is enabled."
- **Change:** move `PUT policy` to post-MVP; **decide** whether `drain` stays
  (genuinely useful for the Phase-7 rollout — if kept, add it to §6.9 and note
  it's operational, not config hot-reload). Remove the §8.2 hedge or align it
  with FR-084.

### B4 — pywb-framed/noframe controller label mismatch (Low) — Ready
- **Where:** §6.7 labels them "Adaptive" (`requirements.md:235-236`); config has
  `controller: fixed` (`implementation_plan.md:164-171`); §8.3 says "Fixed →
  Adaptive" (`:598-599`).
- **Change:** §6.7 "Controller" column → "Fixed → Adaptive" to match canonical
  config.

### B5 — FR numbering gap (Low) — Ready
- **Where:** §6.7 jumps FR-060 → FR-062 (no FR-061).
- **Change:** renumber or intentionally note the gap.

### B6 — `request_cost_model` field consistency (Low) — depends on A3
- **Where:** Solr backends carry `request_cost_model: default`; pywb entries
  omit it.
- **Change:** per A3 outcome — either drop the field entirely (uniform cost) or
  add it uniformly with a defined meaning.

---

## Part C — New requirements to draft

### C1 — Per-backend upstream/dispatch timeout (Medium)
Schema has `queue_timeout_seconds` but no *backend* timeout, yet FR-052 detects
timeouts and the adaptive controller keys off timeout rate. Add
`backend_timeout_seconds` (+ connect timeout) per backend in §2.3. ArchivePageNow
(live capture) needs a much longer upstream timeout than Solr — this is also why
its real value is a TBD (see D).

### C2 — Streaming + max body/header limits (Medium)
A replay proxy serves large archived resources; buffering full responses is a
memory risk. The EN source called for max body/header limits. Add an FR:
stream request/response bodies; enforce max header size and max request body
size (configurable). Tech stack already picks `httpx` streaming.

### C3 — Adaptive error/timeout-rate sampling structure (Medium)
Only latency has a `LatencyWindow` (`implementation_plan.md:388-399`);
`self._timeout_rate` / 5xx rate in `_adjust` (`:434`) are referenced but never
computed. Sketch their rolling-window computation (counts over the adjust
interval) in Phase 4.

### C4 — Unroutable path behavior (Low)
Requests matching no backend have no defined behavior. Specify default-deny /
404 (relevant since Apache may forward only known paths, but should be explicit).

### C5 — Longest-prefix routing semantics (Low)
`/noFrame/replay` and `/noFrame/patching` share a prefix. Specify longest-prefix
(not first-segment) matching so they resolve to distinct backends.

### C6 — GeoIP/ASN DB refresh cadence (Low)
Tech stack picks MaxMind GeoLite2; FR-013 requires global accuracy. A stale DB
means wrong ASN/country. Add an operational note/FR for periodic DB refresh.

### C7 — Exempt traffic metric label (Low)
§14 risk mitigation claims exempt traffic is "metered separately" for anomaly
review, but only the log carries `country_exempt`; metrics don't. Add an
`exempt` label (or a dedicated counter) so the anomaly-review claim is real.

---

## Part D — Consolidated open TBDs to track

Carried from the docs + surfaced by this review. Keep this list authoritative.

- [ ] **Solr routing convention** — how `solr-page-search` vs `solr-image-search`
  is distinguished (host / path prefix / query param). Blocks FR-011b (Must) and
  the two `match.path_prefix: null` entries. *(pre-existing)*
- [ ] **Real production concurrency numbers** for all 6 backends — current values
  are first-draft placeholders. *(pre-existing)*
- [ ] **Real pywb upstream ports** — 8080–8083 are placeholders. *(pre-existing)*
- [ ] **IPv6 prefix length** — /48 vs /56 (FR-022 leaves it open;
  `requirements.md:157`). Materially affects abuse aggregation.
- [ ] **Cost-model token values** — only needed if A3 chooses weighted cost.
- [ ] **Per-backend backend/upstream timeout values** — especially ArchivePageNow
  (see C1).

---

## Progress log

- **2026-07-27** — Revision plan created from review of `requirements.md` +
  `implementation_plan.md` against the two source docs. 8 design decisions (A),
  5 mechanical reconciliations (B), 7 new-requirement drafts (C), 6 tracked TBDs
  (D). Nothing applied to the docs yet — all items Open.
