# API Reference

Every proxied request goes through `proxy_handler` (`app/main.py`), matched last, after the
routes below — so `/healthz`, `/readyz`, `/metrics`, and `/admin/*` always resolve to the AAC's
own endpoints even if a backend's `path_prefix` happens to overlap.

## Operational endpoints

### `GET /healthz`

Liveness only — always `200 {"status": "alive"}` once the process is up. Does not check config,
Redis, or backends.

### `GET /readyz`

Readiness — deliberately narrow, per [Architecture — Fail-open behavior](architecture.md#fail-open-behavior):

| Condition | Response |
|---|---|
| Config failed to load at startup | *(process exits non-zero at startup instead — this state is never actually observable)* |
| Redis unreachable (1s ping timeout) | `503 {"status": "not_ready", "reason": "redis_unreachable"}` |
| Otherwise | `200 {"status": "ready"}` |

Per-backend reachability is **excluded on purpose** — one dead backend must not pull the whole AAC
out of an orchestrator's rotation. Check individual backend health via `/admin/backends` or the
`backend_errors_total`/`backend_timeouts_total` metrics instead.

### `GET /metrics`

Standard Prometheus text exposition (`prometheus_client.generate_latest()`). All metrics are
module-level singletons defined in `app/metrics.py`:

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `admission_inflight_requests` | Gauge | `backend` | Requests currently dispatched to a backend. |
| `admission_inflight_tokens` | Gauge | `backend` | Same, weighted by `ctx.cost` (uniformly `1` today — see [Known Limitations](known_limitations.md)). |
| `admission_concurrency_limit` | Gauge | `backend` | Current capacity-controller limit (static for `fixed`, adjusted for `adaptive`). |
| `admission_queue_size` | Gauge | `backend` | Current priority-queue depth. |
| `admission_requests_total` | Counter | `backend`, `class`, `exempt` | Every request that reached scoring, before admission control runs. |
| `admission_admitted_total` | Counter | `backend`, `class`, `exempt` | Requests that got a backend response — including backend-side `5xx`/`502` (see below). |
| `admission_rejected_total` | Counter | `backend`, `class`, `reason`, `exempt` | Admission-control rejections only. `reason` ∈ `queue_full`, `queue_wait_exceeded`, `queue_timeout`. |
| `admission_queue_timeout_total` | Counter | `backend` | Subset of rejections specifically due to queue timeout. |
| `backend_errors_total` | Counter | `backend` | Backend 5xx or unreachable — tracked separately from admission rejections. |
| `backend_timeouts_total` | Counter | `backend` | Backend dispatch (connect/read) timeouts. |
| `adaptive_limit_changes_total` | Counter | `backend` | Every time `AdaptiveController._adjust()` changes the limit. |
| `backend_request_duration_seconds` | Histogram | `backend`, `class` | Backend latency. |
| `queue_wait_duration_seconds` | Histogram | `backend`, `class` | Time spent queued before dispatch. |
| `score_distribution` | Histogram | `backend`, `exempt` | Score histogram, 10-wide buckets from -100 to 100. |

`admission_admitted_total` counts a request as admitted the moment a backend response (of any
status code) resolves its `Future` — a backend returning `502`/`503` itself is **not** an admission
rejection; that distinction is what `backend_errors_total` is for. See
[Architecture — Request lifecycle](architecture.md#request-lifecycle-in-detail), step 7.

`class` is a Python keyword — all four label call sites in the codebase pass label values
positionally, never as `.labels(class=...)`.

## Admin API

All three routes require `Authorization: Bearer <token>`, matched against `AAC_ADMIN_API_TOKEN`
via `secrets.compare_digest`. **Fail-closed by design**: if `AAC_ADMIN_API_TOKEN` is unset, every
route returns `403 {"detail": "admin API disabled"}` regardless of any header sent — there is no
"admin API open by default" state. A present-but-wrong token returns `401 {"detail":
"unauthorized"}`. This is GET-only and read-only — there is no way to change policy through this
API; changing `config/backends.yaml` and restarting is the only way (see
[Configuration Reference — Config-level validation](configuration.md#config-level-validation)).

### `GET /admin/backends`

Summary of every configured backend:

```json
{
  "backends": [
    {
      "name": "page-search-api",
      "path_prefix": "/textsearch",
      "controller_type": "adaptive",
      "current_limit": 50,
      "mean_latency_ms": 128.4,
      "queue_size": 0
    }
  ]
}
```

`mean_latency_ms` is `null` until at least 10 latency samples have been observed
(`LatencyWindow`'s minimum-sample floor — see
[Architecture — Concurrency control](architecture.md#concurrency-control)).

### `GET /admin/backends/{name}/policy`

Full resolved configuration for one backend — its raw `BackendConfig` plus the scoring config
*after* `resolve_scoring_config()`'s override deep-merge has been applied (i.e., what actually
governs its penalties right now, not just the override fragment in YAML):

```json
{
  "config": { "...": "full BackendConfig, as parsed from YAML" },
  "resolved_scoring": { "...": "merged base_scores + penalties for this backend" }
}
```

`404 {"detail": "unknown backend"}` for a name that isn't configured.

### `GET /admin/backends/{name}/limit`

```json
{ "backend": "page-search-api", "current_limit": 50 }
```

Same `404` behavior as above for an unknown name. This is a narrower, cheaper version of the
`policy` route's `current_limit`, useful for a quick monitoring poll.

## Diagnostic response headers

Off by default (`observability.debug_headers.enabled: false` — see
[Configuration Reference — `observability`](configuration.md#observability)), since they disclose
scoring internals to the requester. When enabled, every proxied response (success or rejection)
carries:

| Header | Meaning |
|---|---|
| `X-AAC-Backend` | The matched backend name. |
| `X-AAC-Score` | The request's final computed score. |
| `X-AAC-Exempt` | `"true"`/`"false"` — whether `scoring.exempt_countries` suppressed subnet/ASN/country penalties for this request. |
| `X-AAC-Reject-Reason` | Present only on a rejection response — one of `queue_wait_exceeded`, `queue_full`, `queue_timeout`. |

## Proxy error responses

Every response from the catch-all proxy route (`/{path:path}`, matched only after the routes
above) that isn't a backend's own response:

| Status | Body `detail` / `reason` | Cause |
|---|---|---|
| `403` | *(TrustedProxyMiddleware, plain text)* | Directly-connecting peer isn't in `ingress.trusted_proxies`. |
| `404` | `"not found"` | No configured backend's `match.path_prefix` matches the request path. |
| `429` | `reason: "queue_wait_exceeded"` | Predictive check: this request's projected queue wait already exceeds `queue_timeout_seconds` — rejected before ever being enqueued. |
| `429` | `reason: "queue_full"` | The backend's priority queue is already at `queue_max_size`. |
| `503` | `reason: "queue_timeout"` | Request was enqueued but waited past `queue_timeout_seconds` before a worker dispatched it. |
| `502` / backend-native status | *(from the backend itself, streamed through unchanged)* | Backend connection error, backend timeout, or the backend's own response — not an AAC admission decision; see `backend_errors_total`/`backend_timeouts_total`. |

Every admission decision (admitted or rejected) is also logged as a single structured JSON line
via `log_admission_event()` (`app/observability.py`), including the full `score_breakdown` for
rejections and admissions alike.
