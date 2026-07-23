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

The Adaptive Admission Controller (AAC) is a dedicated asynchronous reverse-proxy service that sits between the front-end HTTP layer (Apache httpd / future Caddy) and the backend services of arquivo.pt — primarily SolrCloud search clusters, pywb replay, patching workflows, and ArchivePageNow capture.

Its purpose is threefold:

1. **Protect backends from overload** — enforce per-backend concurrency limits that adapt to observed latency and error signals.
2. **Prioritize legitimate traffic** — score every request across multiple dimensions (authentication, network origin, behavioral patterns, request type) and admit higher-value requests first.
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
| **Request Score** | A numeric priority assigned to each request based on user class, network origin, and behavioral signals. |
| **Weighted Cost** | A model where expensive request types consume multiple capacity tokens (e.g., ArchivePageNow = 10 tokens). |
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
The platform hosts backends with fundamentally different cost and latency profiles:

- **SolrCloud** — every query fans out to all shards; latency is a useful overload signal.
- **Framed replay** — pywb with framed wayback.
- **No framed replay** — pywb with no-frame wayback.
- **Patching** - pywb with patching that completes the page with missing archived resources.
- **ArchivePageNow** — pywb that proxies an user.

A single global policy cannot address all these backends correctly.

---

## 4. Goals and Non-Goals

### 4.1 Goals

- Protect SolrCloud, pywb, patching, and ArchivePageNow from overload.
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

- Does not replace internal queues inside SolrCloud, pywb, or other backends.
- Does not implement machine-learning-based autoscaling in the initial version.
- Does not require distributed consensus for capacity budgets in single-instance deployment.
- Does not implement every adaptive concurrency algorithm in the literature for the MVP.
- Does not solve caching; caching is handled by other layers.
- Does not permanently block traffic by country or IP alone (except via explicit operational overrides outside this system).

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
   ├──► SolrCloud clusters (adaptive or fixed)
   ├──► pywb replay — framed / no-frame (adaptive or fixed)
   ├──► Patching (adaptive or fixed)
   └──► ArchivePageNow / capture (fixed or bounded adaptive)
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
| FR-011 | The system shall determine the target backend (SolrCloud, pywb-framed, pywb-noframe, patching, ArchivePageNow) for each request. | Must |
| FR-012 | The system shall determine the user class for each request: anonymous, authenticated researcher, service account, internal. | Must |
| FR-013 | The system shall extract or resolve the source ASN/ISP for each request. Local TTL cache acceptable; global accuracy required for abuse signals. | Should |
| FR-014 | The system shall extract the source country for each request as an auxiliary classification signal. | Should |

### 6.3 Request Scoring

| ID | Requirement | Priority |
|---|---|---|
| FR-020 | The system shall assign a numeric priority score to every request before enqueuing. | Must |
| FR-021 | The score shall derive from a base score per user class and a set of subtracted penalties. | Must |
| FR-022 | Penalties shall be calculated per dimension: individual IP, IPv4 /24 subnet, IPv6 /48 or /56 prefix, ASN/ISP, country, and authenticated user identity. | Must |
| FR-022a | The system shall support a configurable exempt-country list (default: `["PT"]`). Requests originating from an exempt country shall not receive subnet (/24, IPv6 prefix), ASN/ISP, or country-level penalties. Per-IP and per-user penalties shall still apply. | Must |
| FR-023 | Penalties shall be proportional to the request volume from that dimension within configurable time windows (10s, 60s, 300s). | Must |
| FR-024 | Penalty counters shall be stored in a shared Redis instance visible to all AAC nodes. | Must |
| FR-025 | Penalty counters shall have TTL equal to the measurement window so they expire automatically. | Must |
| FR-026 | The system shall log the full score decomposition (base, penalty_ip, penalty_net24, penalty_asn, penalty_country, penalty_user, final_score, country_exempt flag) per request. | Must |
| FR-027 | The score shall be clamped within a configured min/max range (e.g., -100 to 100). | Must |

**Suggested initial base scores by user class:**

| User Class | Base Score | Rationale |
|---|---|---|
| Anonymous occasional user | 100 | Protect the casual public experience. |
| Authenticated researcher | 80 | Legitimate but intensive; above bots, below casuals. |
| Unknown / unclassified | 40–60 | No strong signal; mid-range priority. |
| Suspicious (elevated penalties) | 10–30 | Can be served but degraded. |
| Identified bot | 0 or negative | Goes to the back of the queue and will time out under load. |

### 6.4 Traffic Scheduling

| ID | Requirement | Priority |
|---|---|---|
| FR-030 | The system shall maintain an independent priority queue per backend. | Must |
| FR-031 | Within each queue, requests shall be ordered by score descending, then by arrival timestamp ascending (FIFO within same score). | Must |
| FR-032 | The system shall support weighted fair scheduling to prevent indefinite starvation of lower-score traffic. | Should |
| FR-033 | The system shall enforce a configurable maximum queue length per backend. Requests that cannot be enqueued shall receive immediate rejection. | Must |
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
| FR-046 | The system shall support configurable request cost units so that expensive request types consume multiple tokens. | Should |
| FR-047 | All capacity controller types shall implement a common interface (acquire, release, current_limit). | Must |

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

### 6.7 Backend Policies

| ID | Requirement | Priority |
|---|---|---|
| FR-060 | Each backend shall have an independently configured policy specifying: controller type, concurrency limits, queue limits, queue timeout, cost model, and scheduling algorithm. | Must |
| FR-062 | The system shall support a dry-run (observe-only) mode that classifies and scores requests but does not enforce capacity or queue limits. | Could |

**Recommended initial backend policies:**

| Backend | Controller | Initial posture | Notes |
|---|---|---|---|
| SolrCloud search | Adaptive | Conservative start; learn capacity | p95 target; all requests touch all shards. |
| pywb framed replay | Adaptive | Start fixed; move to adaptive when p95 data available | Higher token cost. |
| pywb no-frame replay | Adaptive | Usually cheaper than framed | Independent p95 target. |
| Patching | Fixed | Small explicit cap | Heavy, not latency-predictable. |
| ArchivePageNow | Fixed or bounded adaptive | Very strict upper bound | High request cost; isolated capacity. |

### 6.8 Observability

| ID | Requirement | Priority |
|---|---|---|
| FR-070 | The system shall export Prometheus metrics per backend and per traffic class. | Must |
| FR-071 | The system shall emit structured JSON logs for admission, rejection, score decomposition, and adaptive limit changes. | Must |
| FR-072 | The system shall expose a `/metrics` endpoint in Prometheus text format. | Must |
| FR-073 | The system shall expose `/healthz` and `/readyz` endpoints. | Must |

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

### 6.9 Administrative API

| Endpoint | Method | Purpose |
|---|---|---|
| `/healthz` | GET | Basic process health. |
| `/readyz` | GET | Readiness including config and backend reachability. |
| `/metrics` | GET | Prometheus metrics. |
| `/admin/backends` | GET | List backends and current policy state. |
| `/admin/backends/{name}/policy` | GET | View active backend policy. |
| `/admin/backends/{name}/limit` | GET | Inspect current capacity limit. |

Authentication and authorization must be enforced on all `/admin/*` endpoints.

---

## 7. Non-Functional Requirements

| Category | Requirement | Design Implication |
|---|---|---|
| Performance | Controller must add low overhead relative to backend latency. | Use async I/O, connection pooling, and efficient priority queues. |
| Reliability | Controller must fail predictably. | Bounded queues, timeouts, and controlled rejection before collapse. |
| Scalability | System should support horizontal scaling. | Redis for global state; each replica receives a fraction of backend budget without shared state, or uses Redis token coordination. |
| Operability | Clear metrics and logs required for operations. | Per-backend, per-class Prometheus metrics. Structured JSON logs. |
| Maintainability | Backend policy logic must be pluggable. | Common interfaces for capacity controllers and schedulers. |
| Security | Auth state and priority metadata must come from verified sources only. | Use verified auth headers or upstream authentication; never trust client-supplied priority claims. |
| Fairness | Lower-priority traffic must not be starved indefinitely. | Configure queue timeouts; optionally add weighted fair scheduling. |

---

## 8. Redis State Requirements

| Key Pattern | Purpose | TTL |
|---|---|---|
| `rl:ip:{ip}:{backend}` | Request count per IP per backend | Window duration (10s / 60s) |
| `rl:net24:{prefix}:{backend}` | Request count per IPv4 /24 per backend | Window duration |
| `rl:net6:{prefix}:{backend}` | Request count per IPv6 prefix per backend | Window duration |
| `rl:asn:{asn}:{backend}` | Request count per ASN per backend | Window duration |
| `rl:country:{cc}:{backend}` | Request count per country per backend | Window duration (60s / 300s) |
| `rl:user:{id}:{backend}` | Request count per authenticated user per backend | Window duration / daily |
| `rl:behavior:{fingerprint}:{backend}` | Behavioral pattern signals | Configurable |

- Redis must be a shared/global instance accessible from all AAC nodes.
- Redis local to a single server is insufficient for distributed abuse mitigation.
- Redis access must be asynchronous (`redis.asyncio`).
- Counters for `rl:net24`, `rl:net6`, `rl:asn`, and `rl:country` shall still be incremented for requests from exempt countries (for observability and tuning), but their values shall not be subtracted from the request score.

---

## 9. Request Lifecycle

1. HTTP request received; deadline applied.
2. Backend identified from path/headers.
3. User class, auth status, IP, subnet, ASN, and country extracted.
4. Request cost estimated from request type.
5. Penalty counters read from Redis; score calculated and clamped.
6. Request placed in the backend's priority queue.
7. Scheduler selects the highest-score request when capacity is available.
8. Capacity controller grants tokens or keeps request queued.
9. Dispatcher forwards request to backend over pooled async HTTP connection.
10. Response latency, status code, and timeout status recorded.
11. Capacity tokens released; adaptive controller receives the sample.
12. Adaptive controller updates limit on its next scheduled interval.

---

## 10. Failure Modes

| Failure | Expected Behaviour | Priority |
|---|---|---|
| Backend timeout | Record timeout; release tokens; reduce adaptive limit if applicable. | Must |
| Backend 5xx burst | Record errors; reduce adaptive limit if configured. | Must |
| Queue full | Reject with controlled 503 response. | Must |
| Controller overload | Apply admission rejection before internal process collapse. | Must |
| Configuration error | Fail `/readyz`; avoid accepting traffic under invalid configuration. | Must |
| Redis unavailable | Fall back to local counters with degraded distributed visibility; log alert. | Should |
| Metrics failure | Continue serving; log degraded observability. | Should |

---

## 11. Deployment Considerations

- **Single-instance**: simplest deployment; exact capacity enforcement; recommended for initial production.
- **Multiple active replicas**: each replica must receive a proportional fraction of the backend limit, or use Redis-based token coordination.
- **Graceful shutdown**: stop accepting new requests, drain queues where feasible, release all in-flight accounting.
- **Front-end compatibility**: the AAC must be deployable behind Apache httpd today and Caddy in the future without changes to its internal logic.

---

## 12. MVP Scope

### 12.1 MVP Must-Haves

- Backend registry with Solr, pywb (framed + no-frame), patching, and ArchivePageNow entries.
- Request classifier based on path, backend, request type, and authentication metadata.
- Scoring engine: base score by user class + IP/subnet/ASN/country penalties from Redis.
- Fixed concurrency controller.
- Simple adaptive concurrency controller (p95-based) for SolrCloud and pywb.
- Priority queue per backend with score ordering and FIFO tie-breaking.
- Bounded queues and queue timeout (default 300s).
- Prometheus metrics per backend and per class.
- Structured JSON logs: admission, rejection, score decomposition, adaptive limit changes.
- `/healthz`, `/readyz`, `/metrics` endpoints.

### 12.2 Post-MVP Enhancements

- Advanced Vegas/Gradient-style adaptive controllers.
- Distributed token budget coordination (Redis-based, multi-replica).
- Policy dry-run mode.
- Admin dashboard / configuration UI.
- Per-tenant budgets and quotas.
- Weighted fair scheduling with anti-starvation guarantees.
- Automated policy recommendation from production metrics.

---

## 13. Testing Requirements

- Unit tests for request classification and cost estimation rules.
- Unit tests for fixed and adaptive capacity controller state transitions.
- Simulated latency curves to verify adaptive limit growth and reduction behaviour.
- Load tests against Solr and pywb separately to establish safe initial limits.
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
| Redis unavailable | Loss of distributed abuse signals | Local fallback counters; alert immediately; Redis HA recommended. |
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
