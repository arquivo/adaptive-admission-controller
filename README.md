# Adaptive Admission Controller (AAC)

An async reverse-proxy for [arquivo.pt](https://arquivo.pt) that sits between the front-end
(Apache httpd/Caddy) and the backend services, protecting them from overload via per-backend
concurrency limits and prioritizing legitimate traffic using Redis-backed scoring.

## Status: Phase 2 ("Fixed Admission Controller") complete

The project is being built in phases (see `docs/implementation_plan.md`). Phase 1 delivered a
config-driven pass-through proxy: trusted-proxy ingress, longest-prefix backend routing, and a
streaming dispatcher. Phase 2 adds admission control on top of that: every request now flows
through a per-backend priority queue and a blocking concurrency gate before being dispatched —
a backend can no longer be overwhelmed by unbounded concurrent traffic. Requests are rejected
with `429` if the queue is full or a projected wait already exceeds the configured timeout, and
with `503` if a request times out waiting in the queue or the backend itself times out.
Request scoring/prioritization (beyond a fixed default score) and adaptive concurrency land in
later phases.

## Backends

All requests are routed purely by path prefix (longest-prefix-wins) to one of six backends,
which all live under a single host per environment:

| Path prefix | Backend |
|---|---|
| `/textsearch` | `page-search-api` |
| `/imagesearch` | `image-search-api` |
| `/wayback` | `pywb-framed` |
| `/noFrame/replay` | `pywb-noframe` |
| `/noFrame/patching` | `pywb-patching` |
| `/save` | `pywb-archivepagenow` |

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Redis (for `/readyz`; scoring/penalties aren't wired in until Phase 3)

## Setup

```bash
uv sync
```

## Configuration

The AAC is configured via a YAML file (default `config/backends.yaml`, overridable with the
`AAC_CONFIG_PATH` environment variable) plus a small set of `AAC_`-prefixed environment
variables (`AAC_REDIS_URL`, `AAC_LOG_LEVEL`) — see `app/config.py`. Invalid or incomplete config
fails process startup with a non-zero exit rather than serving traffic.

`config/backends.yaml` is an example config with illustrative `upstream_url` values — real
hosts/ports, concurrency limits, and thresholds are environment-specific and tracked as open
TBDs in `docs/open_tbd.md`.

## Running locally

```bash
uv run uvicorn app.main:create_app --factory --reload
```

## Running with Docker

```bash
docker compose up --build
```

Starts the AAC and a Redis instance. Real backend containers aren't part of this compose file
yet (Phase 6) — point `config/backends.yaml` at reachable backends, or expect `502`s.

## Testing

```bash
uv run pytest        # unit + integration tests
uv run ruff check .  # lint
```

Integration tests spin up a real-socket mock upstream backend and drive the full app through
`httpx.ASGITransport`.

## Endpoints

- `/healthz` — liveness.
- `/readyz` — readiness: config loaded at startup and Redis reachable. Per-backend reachability
  is intentionally excluded, so one dead backend doesn't pull the whole AAC out of rotation.
- `/metrics` — Prometheus metrics (stub; the full metric set lands in Phase 5).
- Everything else is proxied to the matching backend (subject to that backend's queue/concurrency
  limits), or `404` if no configured prefix matches.

## Documentation

- `docs/requirements.md` — consolidated requirements.
- `docs/implementation_plan.md` — phased implementation plan and task tracking.
- `docs/decision_log.md` — rationale for key design decisions.
- `docs/open_tbd.md` — placeholder values and open questions still pending real-environment data.
