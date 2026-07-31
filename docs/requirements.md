# Adaptive Admission Controller — System Requirements

| Field | Value |
|---|---|
| Version | 1.0 |
| Status | Draft — Pending Stakeholder Validation |
| Owner | Ivo Branco |
| Date | 2026-07-21 |
| Supersedes | `Adaptative_admisson_controller.md`, `Requisitos_Traffic_Scheduler_pywb_Solr_v2.md` |

---

## 1. Executive Summary

The Adaptive Admission Controller (AAC) is a dedicated asynchronous reverse-proxy service that sits between the front-end HTTP layer (Apache httpd / future Caddy) and six independent backend processes of arquivo.pt: two Search API services (page search, image search — each backed by its own SolrCloud cluster) and four pywb instances (framed replay, no-frame replay, patching, ArchivePageNow/SavePageNow), each running as its own process on its own port. See §3.2 for the full backend inventory and §6.2 for path-to-backend routing.

Its purpose is threefold:

1. **Protect backends from overload** — enforce per-backend concurrency limits that adapt to observed latency and error signals.
2. **Prioritize legitimate traffic** — score every request across multiple dimensions (authentication, network origin, request type) and admit higher-value requests first.
3. **Mitigate distributed abuse** — penalize traffic bursts aggregated at IP, subnet (/24, IPv6 prefix), ASN/ISP, and country level without requiring hard bans.
4. **Avoid penalizing the archive's home country** — a configurable set of countries (default: Portugal) is exempt from subnet, ASN/ISP, and country-level penalties, since arquivo.pt's own national traffic is naturally large and diverse and should not be treated as distributed abuse.

The system is not a rate limiter. It is a traffic scheduler combined with an adaptive capacity controller.

---

## 2. Glossary

| Term | Meaning |
|---|---|
| **Admission Controller** | The full service: decides whether a request is admitted, queued, or rejected. |
| **Traffic Scheduler** | The component that selects which queued request runs next. |
| **Capacity Controller** | The component that determines how many requests may be in-flight per backend. |
| **Adaptive Concurrency** | A strategy that adjusts in-flight limits based on observed latency and failure signals rather than static RPS. |
| **Request Score** | A numeric priority assigned to each request based on user class and network-origin abuse signals (IP, subnet, ASN, country, authenticated-user identity). |
| **Weighted Cost** | **Post-MVP.** A model where expensive request types would consume multiple capacity tokens (e.g., a hypothetical ArchivePageNow = 10 tokens). MVP uses a uniform cost of 1 for every request; backend protection comes from fixed/adaptive concurrency limits alone (see FR-046). |
| **Backend Policy** | The complete set of scheduling, capacity, timeout, cost, and rejection rules for one backend. |
| **ASN** | Autonomous System Number — identifies the ISP or network operator. |
| **Exempt Country** | A country (default: Portugal, `PT`) configured to bypass subnet, ASN/ISP, and country-level abuse penalties. Per-IP and per-user penalties still apply. |

---

## 3. Problem Statement

arquivo.pt infrastructure faces two related but distinct problems:

### 3.1 Distributed Abuse
Scrapers and bots use large numbers of IPs — often within the same ISP or subnet — to stay below per-IP rate limits while collectively exhausting backend concurrency. IP-only rate limiting is insufficient. Geographic blocking is coarse and generates false positives. The solution must aggregate signals at the IP, /24 subnet, ASN, and country level.

Because arquivo.pt is Portugal's national web archive, legitimate Portuguese traffic is itself large-volume and spread across many residential ISPs, subnets, and CGNAT ranges. Applying subnet/ASN/country-level penalties uniformly would misclassify this legitimate national traffic as distributed abuse. The system must therefore support an exempt country list (default: Portugal) that bypasses these aggregate-level penalties while still applying per-IP and per-user penalties.

### 3.2 Backend Heterogeneity
The platform hosts six independent backend processes with fundamentally different cost and latency profiles. Each runs on its own hardware/process and port, and each must be configured with its own AAC backend policy — there is no shared pool between them.

| # | Backend name | Process | Cost/latency profile |
|---|---|---|---|
| 1 | `page-search-api` | Page Search API service (dedicated hardware; talks to its own SolrCloud cluster internally) | Every query fans out to all shards; latency is a useful overload signal. |
| 2 | `image-search-api` | Image Search API service (separate dedicated hardware; talks to its own SolrCloud cluster, separate collections) | Independent capacity and latency profile from page search; must not share a policy or concurrency budget with it. |
| 3 | `pywb-framed` | pywb uwsgi process, own port | Framed wayback replay. |
| 4 | `pywb-noframe` | pywb uwsgi process, own port | No-frame wayback replay. |
| 5 | `pywb-patching` | pywb uwsgi process, own port | Patching — completes a page with missing archived resources. |
| 6 | `pywb-archivepagenow` | pywb uwsgi process, own port | ArchivePageNow/SavePageNow — proxies and captures a page on demand. |

A single global policy cannot address all these backends correctly.

**Out of scope: branch topology.** arquivo.pt runs a blue/green branch setup (Branch A / Branch B) where, for high availability, both branches run 2 clusters/replicas of the same data at all times. The AAC does not need to manage branch switching or cross-branch coordination — this is handled upstream of the AAC. The AAC's backend registry describes only the six backends of whichever branch it is deployed against.

---

## 4. Goals and Non-Goals

### 4.1 Goals

- Protect the Search APIs (page/image), pywb, patching, and ArchivePageNow from overload.
- Maximize useful throughput while keeping latency within acceptable bounds.
- Use concurrency-based admission, not RPS-only rate limiting.
- Support per-backend policies with pluggable capacity controller algorithms.
- Queue requests when appropriate; return controlled rejection responses when capacity or queue limits are exceeded.
- Prioritize traffic by authentication status, user class, network origin, and request type.
- Penalize distributed abuse at subnet and ASN level without permanent hard blocks.
- Exempt a configurable set of countries (default: Portugal) from subnet, ASN/ISP, and country-level penalties.
- Expose Prometheus metrics and structured logs for operations and tuning.
- Support future deployment as multiple replicas with shared state coordination.

### 4.2 Non-Goals

- Does not replace internal queues inside the Search APIs' SolrCloud clusters, pywb, or other backends.
- Does not implement machine-learning-based autoscaling in the initial version.
- Does not require distributed consensus for capacity budgets in single-instance deployment.
- Does not implement every adaptive concurrency algorithm in the literature for the MVP.
- Does not solve caching; caching is handled by other layers.
- Does not permanently block traffic by country or IP alone (except via explicit operational overrides outside this system).
- Does not manage blue/green branch switching, cross-branch replication, or cross-branch coordination (see §3.2); the AAC operates against the six backends of a single active branch.
- Does not enforce maximum request body or header size. The front-end web server (Apache httpd today, Caddy in the future) already enforces these limits; duplicating them in the AAC is out of scope (see FR-054 for the streaming requirement, which is a distinct concern from a size limit).

---

## 5. Architecture Overview

```
[Client]
   │
[Apache httpd / future Caddy]
   │
[Adaptive Admission Controller — FastAPI ASGI]
   │  ┌──────────────────────────────────────────┐
   │  │  HTTP Ingress Layer                       │
   │  │  Request Classifier                       │
   │  │  Score Engine  ◄── Redis (global state)   │
   │  │  Backend Policy Registry                  │
   │  │  Traffic Scheduler (Priority Queues)      │
   │  │  Capacity Controllers                     │
   │  │  Backend Dispatcher (httpx async)         │
   │  │  Metrics Collector (Prometheus)           │
   │  └──────────────────────────────────────────┘
   │
   ├──► Page Search API (adaptive or fixed)
   ├──► Image Search API (adaptive or fixed)
   ├──► pywb framed replay (adaptive or fixed)
   ├──► pywb no-frame replay (adaptive or fixed)
   ├──► pywb patching (fixed)
   └──► pywb ArchivePageNow/SavePageNow (fixed or bounded adaptive)
```

The critical business logic lives entirely in the AAC. Apache httpd and Caddy act only as front-end proxies. pywb is never modified.

---

## 6. Functional Requirements

### 6.1 HTTP Ingress

| ID | Requirement | Priority |
|---|---|---|
| FR-001 | The system shall accept HTTP requests and forward them to a configured backend (reverse-proxy). | Must |
| FR-002 | The system shall apply per-request deadlines and maximum queue wait times before forwarding. | Must |
| FR-003 | The system shall return controlled HTTP 429 (queue full / rate exceeded) or 503 (backend unavailable) responses when admission is denied. | Must |

### 6.2 Request Classification

| ID | Requirement | Priority |
|---|---|---|
| FR-010 | The system shall classify each request before admission using path, headers, authentication state, source IP, and request type. | Must |
| FR-010a | The system shall resolve the client source IP from `X-Forwarded-For` only when the directly-connecting peer (`REMOTE_ADDR`) is present in a configured trusted-proxy allowlist; the client IP is the rightmost `X-Forwarded-For` entry by default, with the number of trusted hops configurable. A request whose peer is not on the allowlist shall be rejected with `403 Forbidden` rather than falling back to `REMOTE_ADDR`. | Must |
| FR-011 | The system shall determine the target backend for each request from: `page-search-api`, `image-search-api`, `pywb-framed`, `pywb-noframe`, `pywb-patching`, `pywb-archivepagenow`. | Must |
| FR-011a | The system shall determine the target backend from the request path prefix: `/textsearch` → `page-search-api`; `/imagesearch` → `image-search-api`; `/wayback` → `pywb-framed`; `/noFrame/replay` → `pywb-noframe`; `/noFrame/patching` → `pywb-patching`; `/save` → `pywb-archivepagenow`. All six backends are reachable under a single host per environment, so path prefix alone is sufficient to route every request. Matching is **longest-prefix-wins**: among all configured backends, the request path resolves to the backend whose `path_prefix` is the longest match, so a more specific prefix (e.g. `/noFrame/patching`) takes precedence over a shorter one that also matches (e.g. a hypothetical bare `/noFrame`). | Must |
| FR-011c | The system shall return HTTP `404 Not Found` for any request whose path does not match any configured backend's route, without enqueueing or scoring it. | Must |
| FR-012 | The system shall determine the user class for each request from a fixed identity set: anonymous, authenticated researcher, service account, internal, or unknown (verification failed or ambiguous). | Must |
| FR-013 | The system shall extract or resolve the source ASN/ISP for each request. Local TTL cache acceptable; global accuracy required for abuse signals. | Should |
| FR-013a | The system shall resolve ASN/country from a local GeoIP/ASN database file (path configurable). It shall never fetch this database from a remote service at AAC startup or during normal operation. Refreshing the database is an explicit, separate operational action (a standalone command that downloads a new database to the configured path) run independently of the AAC process; the AAC picks up a refreshed database only on its next restart. | Must |
| FR-014 | The system shall extract the source country for each request as an auxiliary classification signal. | Should |

### 6.3 Request Scoring

| ID | Requirement | Priority |
|---|---|---|
| FR-020 | The system shall assign a numeric priority score to every request before enqueuing. | Must |
| FR-021 | The score shall derive from a base score per user class and a set of subtracted penalties. | Must |
| FR-022 | Penalties shall be calculated per dimension: individual IP, IPv4 /24 subnet, IPv6 prefix (configurable `/48` or `/56`; default `/56`), ASN/ISP, country, and authenticated user identity. | Must |
| FR-022a | The system shall support a configurable exempt-country list (default: `["PT"]`). Requests originating from an exempt country shall not receive subnet (/24, IPv6 prefix), ASN/ISP, or country-level penalties. Per-IP and per-user penalties shall still apply. | Must |
| FR-023 | Penalties shall be proportional to the request volume from that dimension within one or more configurable time windows. A dimension may track multiple independent windows simultaneously (e.g. the authenticated-user dimension tracks both a 60s burst window and a 3600s sustained window); each window's soft/hard step function contributes independently to the total penalty for that dimension. | Must |
| FR-024 | Penalty counters shall be stored in a shared Redis instance visible to all AAC nodes. | Must |
| FR-025 | Penalty counters shall have TTL equal to the measurement window so they expire automatically. | Must |
| FR-026 | The system shall log the full score decomposition (base, penalty_ip, penalty_net24, penalty_asn, penalty_country, penalty_user, final_score, country_exempt flag) per request. | Must |
| FR-027 | The score shall be clamped within a configured min/max range (e.g., -100 to 100). | Must |
| FR-028 | Base scores per user class and penalty thresholds/weights per abuse dimension shall be configurable in a single global default block, rather than hardcoded. | Must |
| FR-029 | The system shall allow individual backends to override any subset of the global default scoring configuration (e.g., a backend with a higher per-request cost may apply stricter thresholds). Fields not explicitly overridden shall inherit the global default; the override is a deep-merge, not a full replacement. | Should |

Concrete default values and the override mechanism are defined in `config/backends.yaml` (see implementation_plan.md §2.3 and §4.4) — that file is canonical if it ever disagrees with the tables below.

`UserClass` is an identity-only classification — it reflects *who is asking*, not *how they are behaving*. There is no separate "suspicious" or "bot" base class and no upfront bot-detection step: a client that behaves abusively is still one of the five classes below, and is driven toward the back of the queue by the per-IP/subnet/ASN/country/user penalties in FR-022 as its behavior accumulates. This avoids double-counting the same abuse signal as both an identity guess and a penalty.

**Suggested initial base scores by user class:**

| User Class | Base Score | Rationale |
|---|---|---|
| Anonymous | 100 | Protect the casual public experience. |
| Authenticated researcher | 80 | Legitimate but intensive; above the unknown/unverified tier. |
| Service account | 90 | Trusted, pre-vetted automation (e.g. internal harvesting/monitoring clients). |
| Internal | 100 | arquivo.pt's own infrastructure; fully trusted. |
| Unknown | 40–60 | Verification failed or ambiguous; no strong signal, mid-range priority. |

A client accumulating enough penalty ends up clamped near `score_clamp.min` (FR-027; `config/backends.yaml`, implementation_plan.md §2.3) regardless of its identity class — that clamped floor, not a separate class, is what sends it to the back of the queue to time out under sustained load.

### 6.4 Traffic Scheduling

| ID | Requirement | Priority |
|---|---|---|
| FR-030 | The system shall maintain an independent priority queue per backend. | Must |
| FR-031 | Within each queue, requests shall be ordered by score descending, then by arrival timestamp ascending (FIFO within same score). | Must |
| FR-032 | The system shall support weighted fair scheduling to prevent indefinite starvation of lower-score traffic. | Should |
| FR-033 | The system shall enforce a configurable maximum queue length per backend. Requests that cannot be enqueued shall receive immediate rejection. | Must |
| FR-033a | Before enqueuing a request, the system shall estimate the projected wait time for that request based on the backend's current queue depth and current effective service rate (derived from the current concurrency limit and observed average — not p95 — service latency). If the estimated wait already exceeds the backend's configured queue timeout (FR-034), the system shall immediately reject the request with `429`, without enqueueing it. This is a distinct, earlier-triggering check from FR-033: a queue can be well under its configured maximum length and still be projected to take longer than the queue timeout to drain, given the backend's current throughput. | Must |
| FR-034 | The system shall enforce a configurable queue timeout per backend (default: 300 seconds). Requests that exceed this wait shall receive a controlled error response. | Must |
| FR-035 | The system shall NOT use request ageing as a primary anti-starvation mechanism. A low-score request should not gain the same priority as a high-score request merely by waiting. | Must |

### 6.5 Capacity Control

| ID | Requirement | Priority |
|---|---|---|
| FR-040 | The system shall support a fixed concurrency controller that enforces a hard in-flight token limit per backend. | Must |
| FR-041 | The system shall support an adaptive concurrency controller that adjusts limits based on observed p95 latency, error rate, and timeout rate. | Must |
| FR-042 | Each backend shall be independently assigned either a fixed or adaptive controller. | Must |
| FR-043 | The adaptive controller shall use slow increases (e.g., +5%) and faster decreases (e.g., -10% to -30%) to minimize oscillation. | Must |
| FR-044 | The adaptive controller shall enforce minimum and maximum concurrency bounds per backend. | Must |
| FR-045 | The adaptive controller shall enter a cooldown period after large decreases to prevent rapid oscillation. | Must |
| FR-046 | **Post-MVP.** The system shall support configurable request cost units so that expensive request types consume multiple tokens. MVP uses a uniform cost of 1 for every request type (see Weighted Cost, §2 Glossary). | Could |
| FR-047 | All capacity controller types shall implement a common interface (acquire, release, current_limit, mean_latency_ms). `mean_latency_ms` feeds FR-033a's projected queue-wait estimate and is required from fixed controllers, not only adaptive ones. | Must |

**Adaptive controller adjustment table:**

| Condition | Action |
|---|---|
| p95 < 50% of target and error rate healthy | Increase limit by ~5% |
| p95 between 50% and 100% of target | Hold current limit |
| p95 > target | Decrease by 10–15% |
| p95 > 2× target | Decrease by 25–30% |
| Timeouts observed | Decrease aggressively; may trigger cooldown |
| Backend 5xx rate exceeds threshold | Decrease moderately or aggressively |

### 6.6 Backend Dispatch

| ID | Requirement | Priority |
|---|---|---|
| FR-050 | The system shall forward admitted requests to the target backend using async HTTP with connection pooling. | Must |
| FR-051 | The system shall record response latency, status code, and timeout status for every dispatched request and report them to the capacity controller. | Must |
| FR-052 | The system shall release capacity tokens immediately after receiving the backend response or detecting a timeout. | Must |
| FR-053 | The system shall enforce a configurable per-backend upstream dispatch timeout — a connect timeout and a response timeout — bounding how long it waits for the backend's HTTP response. This is distinct from the queue-wait timeout (FR-034), which only bounds time spent waiting in the priority queue before dispatch begins. A request that exceeds the dispatch timeout is treated as a backend timeout (FR-051, FR-052) and receives a controlled `503` response. | Must |
| FR-054 | The system shall stream request and response bodies between client and backend rather than buffering them fully in memory, to avoid excessive memory use when serving large archived resources (e.g. video WARC records). | Must |

**Recommended per-backend dispatch timeout defaults (placeholder — validate with production data; see `docs/open_tbd.md`):**

| Backend | Connect timeout | Response timeout |
|---|---|---|
| `page-search-api` | 5s | 60s |
| `image-search-api` | 5s | 60s |
| `pywb-framed` | 5s | 60s |
| `pywb-noframe` | 5s | 60s |
| `pywb-patching` | 5s | 60s |
| `pywb-archivepagenow` | 10s | 120s |

`pywb-archivepagenow` gets a longer timeout because ArchivePageNow performs a live capture of the target page rather than serving from the existing archive — it is expected to take substantially longer than a Search API query or archived-page replay.

### 6.7 Backend Policies

| ID | Requirement | Priority |
|---|---|---|
| FR-060 | Each backend shall have an independently configured policy specifying: controller type, concurrency limits, queue limits, queue timeout, cost model, and scheduling algorithm. | Must |
| FR-061 | The system shall support a dry-run (observe-only) mode that classifies and scores requests but does not enforce capacity or queue limits. | Could |

**Recommended initial backend policies:**

| Backend | Path/route | Controller | Initial posture | Notes |
|---|---|---|---|---|
| `page-search-api` | `/textsearch` | Adaptive | Conservative start; learn capacity | p95 target; all requests touch all shards. |
| `image-search-api` | `/imagesearch` | Adaptive | Conservative start; learn capacity | Separate cluster/hardware from page search; independent p95 target and limits. |
| `pywb-framed` | `/wayback` | Fixed → Adaptive | Start fixed; move to adaptive when p95 data available | Higher token cost. |
| `pywb-noframe` | `/noFrame/replay` | Fixed → Adaptive | Usually cheaper than framed | Independent p95 target. |
| `pywb-patching` | `/noFrame/patching` | Fixed | Small explicit cap | Heavy, not latency-predictable. |
| `pywb-archivepagenow` | `/save` | Fixed or bounded adaptive | Very strict upper bound | High request cost; isolated capacity. |

### 6.8 Observability

| ID | Requirement | Priority |
|---|---|---|
| FR-070 | The system shall export Prometheus metrics per backend and per traffic class. | Must |
| FR-071 | The system shall emit structured JSON logs for admission, rejection, score decomposition, and adaptive limit changes. | Must |
| FR-072 | The system shall expose a `/metrics` endpoint in Prometheus text format. | Must |
| FR-073 | The system shall expose `/healthz` and `/readyz` endpoints. | Must |
| FR-074 | When enabled via configuration (`observability.debug_headers.enabled`, default `false`), the system shall attach diagnostic response headers to every request: `X-AAC-Backend` (matched backend name), `X-AAC-Score` (final clamped score), `X-AAC-Exempt` (`true`/`false`), and `X-AAC-Reject-Reason` (present only on a rejected request, mirroring the `reason` label on `admission_rejected_total`). This is a lightweight diagnostic aid, not a replacement for the full score decomposition already required in structured logs (FR-026) — headers stay to these four fields rather than exposing the full per-dimension penalty breakdown. Because this discloses scoring signals directly to the client, it defaults to disabled and should be enabled deliberately. | Should |

**Required Prometheus metrics:**

| Metric | Type | Description |
|---|---|---|
| `admission_inflight_requests` | Gauge | In-flight requests per backend. |
| `admission_inflight_tokens` | Gauge | Weighted tokens in flight per backend. |
| `admission_concurrency_limit` | Gauge | Current capacity limit per backend. |
| `admission_queue_size` | Gauge | Current queue depth per backend. |
| `admission_requests_total` | Counter | Total received requests by backend and class. |
| `admission_admitted_total` | Counter | Requests admitted per backend. |
| `admission_rejected_total` | Counter | Requests rejected by policy or capacity. |
| `admission_queue_timeout_total` | Counter | Requests expired in queue. |
| `backend_request_duration_seconds` | Histogram | Backend latency per backend and class. |
| `backend_errors_total` | Counter | Backend 5xx or upstream failures. |
| `backend_timeouts_total` | Counter | Backend or dispatcher timeouts. |
| `adaptive_limit_changes_total` | Counter | Adaptive limit adjustment events. |
| `score_distribution` | Histogram | Distribution of request scores by backend. |
| `queue_wait_duration_seconds` | Histogram | Time requests spend waiting in queue. |

`admission_rejected_total` additionally carries a `reason` label (e.g. `queue_full`, `queue_wait_exceeded`, `capacity_full`, `backend_unavailable`) distinguishing why a request was rejected. `queue_wait_exceeded` corresponds to FR-033a's predictive rejection, distinct from `queue_full` (FR-033's hard length cap).

`admission_requests_total`, `admission_admitted_total`, `admission_rejected_total`, and `score_distribution` additionally carry an `exempt` label (`true`/`false`) reflecting whether the request's source country is in the configured exempt-country list (FR-022a). Exempt-country traffic is still counted in these metrics' existing totals — the label adds a breakdown dimension, it does not exclude or redirect the count — which is what makes the §14 "metered separately... for anomaly review" mitigation real.

### 6.9 Administrative API

| Endpoint | Method | Purpose |
|---|---|---|
| `/healthz` | GET | Basic process health. |
| `/readyz` | GET | Readiness: config valid + Redis reachable. Per-backend reachability is reported separately (`/admin/backends`, metrics) and does not gate this endpoint — see FR-083a. |
| `/metrics` | GET | Prometheus metrics. |
| `/admin/backends` | GET | List backends and current policy state. |
| `/admin/backends/{name}/policy` | GET | View active backend policy. |
| `/admin/backends/{name}/limit` | GET | Inspect current capacity limit. |

Authentication and authorization must be enforced on all `/admin/*` endpoints.

### 6.10 Configuration Management

The AAC has two distinct categories of configuration, sourced differently:

| Category | Contains | Source | Precedence |
|---|---|---|---|
| **Policy configuration** | Backend registry, path routing, concurrency limits/bounds, queue limits, scoring weights, penalty thresholds, exempt-country list, diagnostic-header toggle (FR-074) | A single YAML file (`config/backends.yaml`), version-controlled | Compiled-in Pydantic defaults < YAML file. No environment-variable overrides of individual policy fields — this avoids two disagreeing sources of truth for the same value. |
| **Secrets & deployment wiring** | Redis connection URL/credentials, admin API auth token, log level, HTTP listen port, path to the policy YAML file itself | Environment variables only | Never written to the YAML file or committed to version control. |

| ID | Requirement | Priority |
|---|---|---|
| FR-080 | The system shall load all backend/scoring policy configuration from a single YAML file at a configurable path (default: `config/backends.yaml`, overridable via the `AAC_CONFIG_PATH` environment variable). | Must |
| FR-081 | The system shall source all secrets (Redis connection string/credentials, admin API auth token) exclusively from environment variables. Secrets shall never be read from or written to the policy YAML file. | Must |
| FR-082 | The system shall validate the full configuration (schema types, cross-field constraints such as `min_concurrency <= initial_concurrency <= max_concurrency`, and required secrets present) at startup. | Must |
| FR-083 | The system shall fail to start (non-zero exit) on invalid or incomplete configuration, and `/readyz` shall report not-ready if configuration failed to load. | Must |
| FR-083a | `/readyz` shall also report not-ready if the configured Redis instance is unreachable. `/readyz` shall NOT factor individual backend reachability into its status — a down backend affects only its own path (503) and is reported separately via `/admin/backends` and metrics, so one dead backend does not pull the whole AAC out of orchestrator rotation. | Must |
| FR-084 | For the MVP, configuration changes shall require a process restart to take effect. Runtime hot-reload (via `/admin/*` PUT or signal-triggered reload) is a post-MVP enhancement. | Must |
| FR-085 | The system shall be deployable as a container image. The policy YAML file shall be injectable via a mounted volume/ConfigMap; secrets shall be injectable via standard container-orchestrator secret mechanisms (e.g., Kubernetes Secrets, Docker secrets) exposed as environment variables. | Must |

**Illustrative environment variables (names to be finalized during implementation):**

| Variable | Purpose | Example |
|---|---|---|
| `AAC_CONFIG_PATH` | Path to the policy YAML file | `/etc/aac/backends.yaml` |
| `AAC_REDIS_URL` | Redis connection string | `redis://:password@redis-global:6379/0` |
| `AAC_ADMIN_API_TOKEN` | Bearer token/secret for `/admin/*` auth | (secret) |
| `AAC_LOG_LEVEL` | Structured log verbosity | `INFO` |
| `AAC_LISTEN_PORT` | HTTP listen port | `8000` |

---

## 7. Non-Functional Requirements

| Category | Requirement | Design Implication |
|---|---|---|
| Performance | Controller must add low overhead relative to backend latency. | Use async I/O, connection pooling, and efficient priority queues. |
| Reliability | Controller must fail predictably. | Bounded queues, timeouts, and controlled rejection before collapse. |
| Scalability | System should support horizontal scaling. | Redis for global state; each replica receives a fraction of backend budget without shared state, or uses Redis token coordination. |
| Operability | Clear metrics and logs required for operations. | Per-backend, per-class Prometheus metrics. Structured JSON logs. |
| Maintainability | Backend policy logic must be pluggable. | Common interfaces for capacity controllers and schedulers. Penalty counters are accessed through an internal store interface, not a direct Redis dependency scattered through the codebase; Redis remains the sole supported production implementation (no multi-backend store support planned — see `docs/decision_log.md` D3). |
| Security | Auth state and priority metadata must come from verified sources only. | Use verified auth headers or upstream authentication; never trust client-supplied priority claims. |
| Fairness | Lower-priority traffic must not be starved indefinitely. | Configure queue timeouts; optionally add weighted fair scheduling. |

---

## 8. Redis State Requirements

| Key Pattern | Purpose | TTL |
|---|---|---|
| `rl:ip:{ip}:{backend}` | Request count per IP per backend | Window duration (10s) |
| `rl:net24:{prefix}:{backend}` | Request count per IPv4 /24 per backend | Window duration |
| `rl:net6:{prefix}:{backend}` | Request count per IPv6 prefix per backend | Window duration |
| `rl:asn:{asn}:{backend}` | Request count per ASN per backend | Window duration |
| `rl:country:{cc}:{backend}` | Request count per country per backend | Window duration (60s / 300s) |
| `rl:user:{id}:{backend}:{window_seconds}` | Request count per authenticated user per backend, tracked across multiple independent windows (e.g. 60s burst + 3600s sustained) | = that window's `window_seconds` |

- Redis must be a shared/global instance accessible from all AAC nodes.
- Redis local to a single server is insufficient for distributed abuse mitigation.
- Redis access must be asynchronous (`redis.asyncio`).
- Counters for `rl:net24`, `rl:net6`, `rl:asn`, and `rl:country` shall still be incremented for requests from exempt countries (for observability and tuning), but their values shall not be subtracted from the request score.
- The scoring engine shall access these counters through an internal store abstraction rather than calling Redis directly; Redis is the only supported production implementation of that abstraction (see `docs/decision_log.md` D3).

---

## 9. Request Lifecycle

1. HTTP request received; deadline applied.
2. Backend identified from path/headers.
3. User class, auth status, IP, subnet, ASN, and country extracted.
4. Request cost estimated from request type.
5. Penalty counters read from Redis; score calculated and clamped.
6. Estimated queue wait computed from current queue depth and current effective service rate; if it already exceeds the queue timeout, the request is rejected immediately with `429` (FR-033a) without being enqueued.
7. Request placed in the backend's priority queue.
8. Scheduler selects the highest-score request when capacity is available.
9. Capacity controller grants tokens or keeps request queued.
10. Dispatcher forwards request to backend over pooled async HTTP connection.
11. Response latency, status code, and timeout status recorded.
12. Capacity tokens released; adaptive controller receives the sample.
13. Adaptive controller updates limit on its next scheduled interval.

---

## 10. Failure Modes

| Failure | Expected Behaviour | Priority |
|---|---|---|
| Backend timeout | Record timeout; release tokens; reduce adaptive limit if applicable. | Must |
| Backend 5xx burst | Record errors; reduce adaptive limit if configured. | Must |
| Queue full | Reject immediately with `429` (FR-033), without enqueueing. | Must |
| Queue backlog projected to exceed timeout | Reject immediately with `429` (FR-033a), without enqueueing, even though the queue is under its configured maximum length. | Must |
| Controller overload | Apply admission rejection before internal process collapse. | Must |
| Configuration error | Fail `/readyz`; avoid accepting traffic under invalid configuration. | Must |
| Redis unavailable | Scoring fails open (base score only, zero penalties); fixed/adaptive concurrency limits keep protecting backends unchanged; `/readyz` reports not-ready (FR-083a); alert immediately. | Must |
| Metrics failure | Continue serving; log degraded observability. | Should |

---

## 11. Deployment Considerations

- **Single-instance**: simplest deployment; exact capacity enforcement; recommended for initial production.
- **Multiple active replicas**: each replica must receive a proportional fraction of the backend limit, or use Redis-based token coordination.
- **Graceful shutdown**: stop accepting new requests, drain queues where feasible, release all in-flight accounting.
- **Front-end compatibility**: the AAC must be deployable behind Apache httpd today and Caddy in the future without changes to its internal logic.
- **Containerized deployment**: the AAC ships as a container image; the policy YAML is delivered via mounted volume/ConfigMap, secrets via orchestrator-managed environment variables (see §6.10).
- **GeoIP/ASN database refresh**: the AAC only ever reads the local GeoIP/ASN database file at startup (FR-013a); it never downloads it. Refreshing the file is a separate, explicit operational action, typically run before a rolling restart; integrating that action into the broader deployment/release process is outside the scope of this document.

---

## 12. MVP Scope

### 12.1 MVP Must-Haves

- Backend registry with all six backends: `page-search-api`, `image-search-api`, `pywb-framed`, `pywb-noframe`, `pywb-patching`, `pywb-archivepagenow`.
- Request classifier based on path, backend, request type, and authentication metadata.
- Scoring engine: base score by user class + IP/subnet/ASN/country/user penalties from Redis, with multi-window penalty tracking per dimension (FR-023) and uniform request cost = 1 (FR-046 weighted cost is post-MVP).
- Fixed concurrency controller.
- Simple adaptive concurrency controller (p95-based) for the Search APIs and pywb.
- Priority queue per backend with score ordering and FIFO tie-breaking.
- Bounded queues and queue timeout (default 300s).
- Predictive queue-wait rejection (FR-033a): reject immediately with `429` when the projected wait to drain the current backlog already exceeds the queue timeout, even if the queue is under its configured maximum length.
- Prometheus metrics per backend and per class.
- Structured JSON logs: admission, rejection, score decomposition, adaptive limit changes.
- `/healthz`, `/readyz`, `/metrics` endpoints.
- Single YAML policy file + environment-variable secrets, validated at startup with fail-fast behavior (§6.10). Restart-only config changes; no runtime hot-reload.

### 12.2 Post-MVP Enhancements

- Advanced Vegas/Gradient-style adaptive controllers.
- Distributed token budget coordination (Redis-based, multi-replica).
- Policy dry-run mode.
- Runtime hot-reload of policy configuration (`/admin/*` PUT or signal-triggered reload) — MVP requires a restart.
- Admin dashboard / configuration UI.
- Per-tenant budgets and quotas.
- Weighted fair scheduling with anti-starvation guarantees.
- Automated policy recommendation from production metrics.
- Weighted request cost model (per-request-type token costs, FR-046) — MVP uses a uniform cost of 1 for every request (see §2 Glossary, Weighted Cost).

---

## 13. Testing Requirements

- Unit tests for request classification and cost estimation rules.
- Unit tests for fixed and adaptive capacity controller state transitions.
- Simulated latency curves to verify adaptive limit growth and reduction behaviour.
- Load tests against each of the six backends independently (`page-search-api`, `image-search-api`, `pywb-framed`, `pywb-noframe`, `pywb-patching`, `pywb-archivepagenow`) to establish safe initial limits.
- Tests for queue timeout, backend timeout, cancellation, and client disconnect.
- Fairness tests verifying that high-score traffic is served before low-score traffic.
- Tests verifying that degraded backends do not affect unrelated backend queues.
- Prometheus metrics and structured log validation during controlled load tests.

---

## 14. Key Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Adaptive controller oscillates | Unstable latency and degraded throughput | Bounded changes, cooldown periods, slow increase / fast decrease. |
| Queue grows without bound | Memory pressure and poor UX | Max queue sizes and queue deadlines enforced strictly. |
| Priority causes starvation | Low-score traffic never served | Queue timeouts expire stale requests; optional weighted fair scheduling. |
| Backend latency spikes externally | Adaptive controller reduces capacity unnecessarily | Strict min limits, smoothing windows, separate timeout/error signals. |
| Multiple replicas over-admit | Backend receives more load than intended | Partition budgets per replica or use Redis token coordination. |
| Incorrect classification | Wrong priority or backend policy applied | Explicit route rules, integration tests, structured logs for all decisions. |
| Redis unavailable | Loss of distributed abuse signals | Scoring fails open (base score only); capacity limiters (Redis-independent) keep protecting backends; `/readyz` flips not-ready (FR-083a) to signal upstream HA/alerting; Redis HA recommended. |
| Exempt-country status used to launder distributed abuse | Bots inside the exempt country evade subnet/ASN/country penalties | Per-IP and per-user penalties remain in force for exempt countries; exempt-country traffic is metered separately in metrics/logs for anomaly review; exemption list is admin-configurable, not hardcoded. |

---

## 15. External References

| Reference | Relevant Concept |
|---|---|
| Netflix Concurrency Limits | TCP congestion-control-inspired concurrency limit auto-detection. |
| Envoy Adaptive Concurrency Filter | Latency-sampling-based dynamic concurrency adjustment. |
| Apache Solr Rate Limiters | Solr-level concurrency controls (JVM layer). |
| Apache Solr Cluster Types | SolrCloud shard/replica distributed query behaviour. |

---

*This document supersedes the source files in `docs/md/`. All further requirement changes should be tracked here.*
