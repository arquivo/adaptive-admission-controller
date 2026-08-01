Adaptive Admission Controller
Requirements and Architecture Specification
For SolrCloud, pywb Replay, Patching, SavePageNow and archival infrastructure backends
Version
1.0
Status
Complete draft
Owner
Ivo Branco
Language
English
Date
19 July 2026

1. Executive Summary
This document defines the requirements for a new Adaptive Admission Controller. The system will sit in front of multiple high-value backend services and decide which requests may enter, which should wait, and how much concurrent load each backend should receive. The initial backend families are SolrCloud clusters, pywb replay services, framed and no-frame replay variants, patching workflows, SavePageNow capture workflows, and future archival/search services.
The main design conclusion is that the system is not merely a rate limiter and not merely a traffic scheduler. It is an admission control layer composed of three cooperating parts: a request classifier, a traffic scheduler, and a capacity controller. Some capacity controllers will use fixed concurrency limits; others will adapt dynamically using observed backend latency, timeouts, and errors.
Core recommendation
Build a pluggable Adaptive Admission Controller. Each backend should have its own policy and its own capacity controller: fixed, adaptive, or a future custom algorithm. The scheduler decides who receives capacity first; the capacity controller decides how much capacity exists.

2. Terminology
Term
Meaning
Example
Admission Controller
The service that decides whether a request is accepted immediately, queued, rejected, or routed to a backend.
The complete system described in this document.
Traffic Scheduler
The component that chooses which queued request should run next.
Authenticated requests may be selected before anonymous requests.
Capacity Controller
The component that determines how many requests may be in flight for a backend.
Fixed limit of 20 or adaptive limit that changes over time.
Adaptive Concurrency
A strategy that controls in-flight requests based on observed latency and failures rather than static requests per second.
Increase capacity when p95 is healthy; reduce when p95 degrades.
Weighted Cost
A model where expensive request types consume more than one token.
SavePageNow = 10 tokens; simple Solr query = 1 token.
Backend Policy
A backend-specific set of scheduling, capacity, timeout, priority and rejection rules.
Solr AdaptivePolicy, Patching FixedPolicy.
3. Problem Statement
The infrastructure must protect several heterogeneous backend services. A simple global requests-per-second limit is insufficient, because backend pressure is caused by the number of concurrent in-flight operations, the cost of each operation, and the current health of each target service. Different services also have different user impact and different overload characteristics.
The system must address the following challenges:
    • SolrCloud clusters may be sensitive to concurrent distributed queries, especially when every user query touches all shards.
    • pywb replay traffic may have different cost profiles depending on framed replay, no-frame replay, rewriting and replay complexity.
    • Patching operations may be expensive and should often be protected with strict fixed limits.
    • SavePageNow capture workflows can be very expensive and may require strict upper bounds, priority rules and queueing.
    • Authenticated, premium, internal or geographically important traffic may require higher priority than anonymous traffic.
    • The same system should support both fixed concurrency limits and adaptive concurrency control per backend.
4. Goals and Non-Goals
4.1 Goals
    • Protect SolrCloud, pywb and archival backends from overload.
    • Maximize useful throughput while keeping latency within acceptable bounds.
    • Use concurrency limits rather than relying exclusively on requests per second.
    • Support backend-specific policies and algorithms.
    • Queue requests when appropriate instead of immediately rejecting them.
    • Prioritize traffic using authentication, geography, request type, tenant, service tier or custom rules.
    • Expose clear operational metrics for visibility and tuning.
    • Provide a clean foundation for future adaptive algorithms inspired by Netflix Concurrency Limits and Envoy Adaptive Concurrency concepts.
4.2 Non-Goals
    • The system is not intended to replace SolrCloud, pywb or backend-specific internal queues.
    • The first version does not need to implement every adaptive algorithm known in the literature.
    • The first version does not need distributed consensus for the concurrency budget unless deployed as multiple active replicas.
    • The first version should not attempt machine-learning-based autoscaling before simple deterministic controllers are validated.
5. Proposed Architecture
The system should be implemented as a dedicated asynchronous HTTP service in front of the target backends.
                       +-----------------------------+
Incoming HTTP Traffic  |  Adaptive Admission Layer   |
---------------------->|-----------------------------|
                       |  Request Classifier         |
                       |  Global Scheduler           |
                       |  Backend Policy Registry    |
                       |  Capacity Controllers       |
                       +--------------+--------------+
                                      |
              +-----------------------+------------------------+
              |                       |                        |
              v                       v                        v
        SolrCloud clusters       pywb replay              Capture / patching
        adaptive or fixed        adaptive or fixed         mostly fixed or bounded

The Admission Controller should be internally decomposed into the following components:
    • HTTP ingress layer: accepts client requests and applies request deadlines.
    • Request classifier: extracts backend, request type, user class, authentication status, geography and cost estimate.
    • Policy registry: maps backend and request type to the correct scheduling and capacity policy.
    • Traffic scheduler: chooses the next eligible request from one or more queues.
    • Capacity controller: grants or denies capacity tokens for the selected backend.
    • Backend dispatcher: forwards admitted requests to Solr, pywb or other services.
    • Metrics collector: records latency, queueing, failures, drops and controller state.
6. Functional Requirements
ID
Requirement
Priority
Notes
FR-001
The system shall accept HTTP requests and route them to a configured backend.
Must
Reverse-proxy behaviour is required.
FR-002
The system shall classify each request before admission.
Must
Classification may use path, headers, auth state, country, backend and request type.
FR-003
The system shall support multiple backends with independent policies.
Must
Solr, pywb, patching and SavePageNow must not share one fixed policy.
FR-004
The system shall support fixed concurrency limits per backend.
Must
Needed for expensive and predictable workflows.
FR-005
The system shall support adaptive concurrency limits per backend.
Must
Needed for Search and replay workloads where latency is a useful signal.
FR-006
The system shall maintain at least one queue per backend or backend class.
Must
Prevents one backend from blocking unrelated traffic.
FR-007
The system shall support priority scheduling.
Must
Authenticated or higher-value traffic can be preferred.
FR-008
The system shall support weighted fair scheduling.
Should
Prevents starvation of lower-priority classes.
FR-009
The system shall support request cost units.
Should
Expensive requests can consume multiple tokens.
FR-010
The system shall support queue timeout and maximum queue length controls.
Must
Prevents unbounded memory growth and poor user experience.
FR-011
The system shall return controlled rejection responses when capacity or queue limits are exceeded.
Must
Prefer explicit 429/503 behaviour over backend collapse.
FR-012
The system shall export Prometheus-compatible metrics.
Must
Required for operations and adaptive tuning.
FR-013
The system shall log admission decisions and policy outcomes.
Should
Useful for debugging and auditability.
FR-014
The system shall allow runtime configuration of backend policies.
Should
Avoid rebuilds for simple policy changes.
FR-015
The system shall support dry-run or observe-only mode for new policies.
Could
Useful before enforcing adaptive changes.
7. Non-Functional Requirements
Category
Requirement
Design Implication
Performance
The controller must add low overhead relative to backend latency.
Use async I/O, connection pooling and efficient queues.
Reliability
The controller must fail predictably.
Use timeouts, circuit-breaker behaviour and bounded queues.
Scalability
The system should support horizontal scaling.
Use shared budget coordination only if multiple replicas enforce the same backend limit.
Operability
The system must expose clear metrics and logs.
Prometheus metrics should be per backend and per class.
Maintainability
Backend policy logic must be pluggable.
Use controller interfaces for fixed, adaptive and custom policies.
Security
Authentication state and priority metadata must be trusted.
Only use verified auth headers or perform authentication upstream.
Fairness
Lower-priority traffic should not be starved indefinitely.
Use weighted fair scheduling or ageing.
8. Backend Policy Model
Each backend must be configured with a policy object. A policy defines how traffic is classified, queued, admitted, timed out and measured.
BackendPolicy:
  name: solr-main
  backend_type: solrcloud
  scheduler: weighted_fair
  capacity_controller: adaptive
  min_concurrency: 20
  initial_concurrency: 100
  max_concurrency: 500
  target_p95_ms: 100
  queue_max_size: 5000
  queue_timeout_ms: 3000
  request_cost_model: solr_query_cost

8.1 Recommended Initial Policies
Backend
Controller
Initial posture
Why
Example control
SolrCloud search
Adaptive
Start conservative, then learn capacity
Latency is a good indicator and all requests touch all shards.
Target p95 with min/max bounds.
pywb replay
Adaptive or fixed
Start fixed, move to adaptive after metrics are understood
Replay may vary by page complexity and rewrite behaviour.
Target p95 plus strict error handling.
pywb framed replay
Adaptive with lower max
Framed replay may be more expensive than no-frame replay
Protect expensive replay path.
Higher token cost or lower max concurrency.
pywb no-frame replay
Adaptive
Usually less complex than framed replay
Can share replay controller or have separate policy.
Independent p95 target.
Patching
Fixed
Use strict caps first
Heavy work is often not latency-homogeneous.
Small fixed concurrency.
SavePageNow
Fixed or bounded adaptive
Start fixed with very strict upper bound
Capture workflows can be expensive and externally variable.
Low fixed limit or adaptive with hard cap.
9. Traffic Scheduling Requirements
The scheduler answers one question: which request should run next when backend capacity becomes available. It should be independent from the capacity algorithm.
9.1 Classification Inputs
    • Authentication state: authenticated, anonymous, service account, internal user.
    • User tier: premium, standard, anonymous or future tenant classes.
    • Geography: source country or region, if trustworthy and useful.
    • Backend: SolrCloud cluster, pywb replay, SavePageNow, patching or other service.
    • Request type: search, replay, framed replay, no-frame replay, patching, capture.
    • Estimated cost: number of tokens consumed by the request.
    • Deadline: maximum time the request may wait before rejection.
9.2 Scheduling Algorithms
Algorithm
Use case
Limitations
FIFO
Simple low-risk queues where all requests are similar.
No prioritization or fairness between classes.
Priority queue
Authenticated, premium or internal traffic should be preferred.
Can starve low-priority traffic if no ageing is used.
Weighted fair queueing
Traffic classes should receive stable shares.
More complex implementation.
Deficit round-robin
Different request costs should be handled fairly.
Requires cost accounting.
Recommended scheduler for MVP
Use priority classes plus weighted fair scheduling. This gives immediate control over authenticated and anonymous traffic while avoiding permanent starvation of lower-priority classes.


10. Capacity Controllers
The capacity controller answers one question: how many requests may be running against a backend at the same time. The system must support both fixed and adaptive controllers.
10.1 Fixed Concurrency Controller
The fixed controller is deterministic and should be used for expensive or poorly predictable workflows. It enforces a hard number of in-flight tokens.
FixedController(limit=5)

on acquire(cost):
    if inflight_tokens + cost <= limit:
        admit
    else:
        queue or reject

on release(cost):
    inflight_tokens -= cost

10.2 Adaptive Concurrency Controller
The adaptive controller changes the concurrency limit based on observed backend health. The principal signal should be latency, especially p95. Error rate and timeout rate should be used as safety signals.
AdaptiveController:
  min_limit: 20
  initial_limit: 100
  max_limit: 500
  target_p95_ms: 100
  update_interval: 30s

Every update interval:
  if timeout_rate > 0:
      reduce limit aggressively
  elif p95 > 2 * target_p95:
      reduce limit strongly
  elif p95 > target_p95:
      reduce limit moderately
  elif p95 < 0.5 * target_p95 and error_rate is healthy:
      increase limit slowly
  else:
      keep current limit

The goal is not to maximize concurrency blindly. The goal is to maximize useful throughput while keeping latency and failures under control.
11. Initial Adaptive Algorithm Specification
The first implementation should be simple, observable and safe. More advanced algorithms can be added later behind the same CapacityController interface.
Condition
Action
Reason
Safeguard
p95 < 50% of target and error rate healthy
Increase limit by 5%
Backend appears under-utilized.
Do not exceed configured max.
p95 between 50% and 100% of target
Keep limit
Backend is within useful operating range.
Continue sampling.
p95 above target
Decrease limit by 10-15%
Latency indicates congestion or backend pressure.
Do not go below min.
p95 above 2x target
Decrease limit by 25-30%
Strong overload signal.
Consider temporary rejection over queueing.
Timeouts observed
Decrease limit aggressively
Timeouts are a stronger safety signal than latency.
May trigger cooldown.
Backend 5xx rate exceeds threshold
Decrease limit moderately or aggressively
Backend is failing or overloaded.
Keep separate from client-side errors.
11.1 Algorithm Boundaries
    • Always configure min_limit and max_limit per backend.
    • Use slow increases and faster decreases.
    • Use cooldown periods after severe decreases to avoid oscillation.
    • Record all limit changes as metrics and structured logs.
    • Keep adaptive logic behind an interface so a Netflix/Vegas/Gradient-style controller can replace the initial algorithm later.
12. Request Lifecycle
    1. Receive HTTP request at the Admission Controller.
    2. Apply maximum request body and header limits if required.
    3. Classify backend and request type.
    4. Determine user class, geography, authentication state and estimated cost.
    5. Select the backend policy.
    6. Place the request into the correct queue.
    7. Scheduler selects the next eligible request when capacity is available.
    8. Capacity controller grants tokens or keeps the request queued.
    9. Dispatcher forwards the request to the backend using connection pooling.
    10. Response latency, status code, timeout status and token cost are recorded.
    11. Capacity tokens are released.
    12. Adaptive controller updates its limit periodically using recent samples.
13. Backend-Specific Requirements
13.1 SolrCloud
    • Use an adaptive concurrency controller by default for search traffic.
    • Treat each request as structurally expensive because every request touches all shards.
    • Use p95 latency as the primary signal for adaptive control.
    • Track Solr response codes, timeouts and upstream connection failures separately.
    • Optionally maintain different policies per Solr cluster if clusters have different hardware or datasets.
13.2 pywb Replay
    • Support separate policies for framed and no-frame replay if their costs differ.
    • Start with fixed limits if replay behaviour is not yet measured.
    • Move to adaptive limits when stable p95, timeout and error metrics are available.
    • Allow higher token cost for more complex replay modes.
13.3 Patching
    • Use fixed concurrency by default.
    • Prefer small, explicit caps because patching may be heavy and less latency-predictable.
    • Queue patching work separately from user-facing search and replay traffic.
    • Consider lower priority unless the operation is user-triggered and time-sensitive.
13.4 SavePageNow / Capture
    • Start with strict fixed limits or bounded adaptive limits.
    • Use a high request cost relative to simple search or replay.
    • Use queue timeout and queue length limits to avoid unbounded capture backlog.
    • Keep capture traffic isolated from Solr and replay capacity budgets.
14. Metrics and Observability
Metrics must be emitted per backend and, where useful, per traffic class.
Metric
Type
Description
admission_inflight_requests
Gauge
Current admitted in-flight requests.
admission_inflight_tokens
Gauge
Current weighted cost in flight.
admission_concurrency_limit
Gauge
Current capacity limit for the backend.
admission_queue_size
Gauge
Current queue length.
admission_requests_total
Counter
Total received requests by backend and class.
admission_admitted_total
Counter
Requests admitted to backend.
admission_rejected_total
Counter
Requests rejected by policy or capacity.
admission_queue_timeout_total
Counter
Requests that waited too long in queue.
backend_request_duration_seconds
Histogram
Backend latency distribution.
backend_errors_total
Counter
Backend 5xx or upstream failures.
backend_timeouts_total
Counter
Backend or dispatcher timeout count.
adaptive_limit_changes_total
Counter
Number of adaptive limit changes.
15. Suggested Administrative API
The first version can expose a small operational API. Authentication and authorization should be enforced for all administrative endpoints.
Endpoint
Method
Purpose
/healthz
GET
Basic process health.
/readyz
GET
Readiness including configuration and backend reachability checks.
/metrics
GET
Prometheus metrics.
/admin/backends
GET
List configured backends and current policy state.
/admin/backends/{name}/policy
GET
View active backend policy.
/admin/backends/{name}/policy
PUT
Update backend policy, if runtime config is enabled.
/admin/backends/{name}/limit
GET
Inspect current fixed or adaptive capacity limit.
/admin/backends/{name}/drain
POST
Optionally stop admitting new traffic to a backend.
16. Implementation Recommendation
The recommended first implementation is Python with an asynchronous HTTP framework such as FastAPI or another ASGI-compatible stack. The system is primarily network-bound and policy-driven; the expensive execution remains inside SolrCloud, pywb and capture services.
16.1 Suggested Python Components
    • ASGI web server for HTTP ingress.
    • asyncio queues or custom priority queues for request scheduling.
    • httpx or aiohttp for upstream HTTP dispatch with connection pooling.
    • prometheus-client for metrics exposure.
    • Pydantic or equivalent configuration schema validation.
    • Structured logging in JSON for policy decisions and backend outcomes.
16.2 Core Interfaces
class CapacityController:
    async def acquire(self, cost: int = 1) -> bool:
        ...

    def release(self, cost: int, latency_ms: float, status_code: int, timed_out: bool) -> None:
        ...

    def current_limit(self) -> int:
        ...

class Scheduler:
    async def enqueue(self, request_context):
        ...

    async def next_request(self, backend_name: str):
        ...

class BackendPolicy:
    def classify(self, request):
        ...

    def estimate_cost(self, request_context) -> int:
        ...

Implementation note
Keep scheduling and capacity control separate. This allows the traffic scheduler to remain stable while capacity algorithms evolve from fixed limits to simple adaptive limits and later to Vegas/Gradient-inspired algorithms.

17. Deployment and Scaling Considerations
    • Single-instance deployment is simplest and provides exact capacity enforcement.
    • Multiple active replicas require coordinated limits or partitioned capacity budgets.
    • If multiple replicas are deployed without shared state, each replica should receive a fraction of the backend limit.
    • A future distributed implementation may use Redis or another shared coordination layer for token accounting.
    • The controller should support graceful shutdown: stop accepting new requests, drain queues where possible, and release all in-flight accounting.
17.1 Failure Modes
Failure
Expected behaviour
Requirement
Backend timeout
Record timeout, release tokens, reduce adaptive limit if applicable.
Must
Backend 5xx burst
Record errors and reduce adaptive limit if configured.
Must
Queue full
Reject with controlled status and clear response.
Must
Controller overload
Apply admission rejection before internal collapse.
Must
Configuration error
Fail readiness checks and avoid accepting unsafe traffic.
Must
Metrics failure
Continue serving but log degraded observability.
Should
18. MVP Scope
The first production-ready version should optimize for correctness, safety and observability rather than advanced adaptive algorithms.
18.1 MVP Must-Haves
    • Backend registry with Solr, pywb, patching and SavePageNow entries.
    • Request classifier based on path, backend, request type and authentication metadata.
    • Fixed concurrency controller.
    • Simple adaptive concurrency controller for selected backends.
    • Priority and weighted fair scheduling.
    • Bounded queues and queue timeout handling.
    • Prometheus metrics per backend.
    • Structured logs for admission, rejection and adaptive limit changes.
18.2 Post-MVP Enhancements
    • Advanced Vegas or Gradient-style controllers.
    • Distributed token budget coordination across multiple controller replicas.
    • Policy dry-run mode.
    • Configuration UI or admin dashboard.
    • Per-tenant budgets and quotas.
    • Automated policy recommendation from production metrics.
    • Separate overload-shedding strategies for anonymous and authenticated traffic.
19. Key Risks and Mitigations
Risk
Impact
Mitigation
Adaptive controller oscillates
Unstable latency and poor throughput
Use bounded changes, cooldowns and slow increase / fast decrease.
Queue grows without bound
Memory pressure and bad user experience
Use maximum queue sizes and queue deadlines.
Priority causes starvation
Anonymous or low-priority traffic never runs
Use weighted fair scheduling or ageing.
Backend latency varies for external reasons
Adaptive controller reduces capacity unnecessarily
Use strict min limits, smoothing and separate timeout/error handling.
Multiple controller replicas over-admit traffic
Backend receives more load than intended
Partition budgets or use shared token coordination.
Incorrect classification
Wrong priority or backend policy applied
Use explicit route rules, tests and structured logs.
20. Testing Strategy
    • Unit-test request classification and cost estimation rules.
    • Unit-test fixed and adaptive capacity controller behaviour.
    • Simulate latency curves to verify adaptive limit growth and reduction.
    • Load-test Solr and pywb separately to establish initial safe limits.
    • Test queue timeout, backend timeout, cancellation and client disconnect behaviour.
    • Test fairness between authenticated and anonymous traffic.
    • Test failure behaviour when one backend is degraded while others remain healthy.
    • Validate Prometheus metrics and logs during controlled load tests.
21. External References and Design Inspiration
The system design is inspired by established concurrency-control and backend-protection patterns. These references should be used as conceptual guidance rather than as strict implementation requirements.
Reference
Relevant idea
URL
Netflix Concurrency Limits
Java library implementing concepts from TCP congestion control to auto-detect concurrency limits for services.
https://github.com/Netflix/concurrency-limits
Envoy Adaptive Concurrency Filter
Envoy HTTP filter that dynamically adjusts outstanding request concurrency based on latency sampling.
https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/adaptive_concurrency_filter
Apache Solr Request Rate Limiters
Solr documentation for request rate limiting and allowed concurrent requests at JVM level.
https://solr.apache.org/guide/solr/latest/deployment-guide/rate-limiters.html
Apache Solr Cluster Types
SolrCloud concepts including shards, replicas and distributed query behaviour.
https://solr.apache.org/guide/solr/latest/deployment-guide/cluster-types.html
22. Final Conclusion
The proposed system should be described and developed as an Adaptive Admission Controller. This terminology is more accurate than traffic scheduler because the system does more than order requests. It decides whether requests may enter, when they may enter, how many requests each backend may process, and how capacity should adapt to backend health.
The architecture should deliberately separate the scheduler from the capacity controller. The scheduler controls fairness and priority. The capacity controller protects each backend. This separation makes it possible to use fixed limits for expensive workflows, adaptive limits for latency-sensitive services, and future custom controllers without redesigning the whole system.
Final architectural decision
Build a Python-based asynchronous Adaptive Admission Controller with pluggable backend policies. Start with fixed controllers and a simple p95-based adaptive controller. Use metrics and production observations to evolve toward more advanced algorithms only where they provide measurable value.
