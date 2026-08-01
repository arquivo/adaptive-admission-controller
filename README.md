# Adaptive Admission Controller (AAC)

An async reverse-proxy for [arquivo.pt](https://arquivo.pt) that sits between the front-end
(Apache httpd/Caddy) and the backend services, protecting them from overload via per-backend
concurrency limits and prioritizing legitimate traffic using Redis-backed scoring.

## Status: Phase 4 ("Adaptive Concurrency Controller") complete

The project is being built in phases (see `docs/implementation_plan.md`). Phase 1 delivered a
config-driven pass-through proxy: trusted-proxy ingress, longest-prefix backend routing, and a
streaming dispatcher. Phase 2 added admission control on top of that: every request now flows
through a per-backend priority queue and a blocking concurrency gate before being dispatched —
a backend can no longer be overwhelmed by unbounded concurrent traffic. Requests are rejected
with `429` if the queue is full or a projected wait already exceeds the configured timeout, and
with `503` if a request times out waiting in the queue or the backend itself times out.
Phase 3 wires in real request classification and Redis-backed scoring: every request is
classified by source IP/subnet, GeoIP country/ASN, and (optionally) a verified Keycloak JWT, then
scored against per-backend penalty thresholds so the priority queue actually reorders requests
meaningfully instead of only ever seeing ties. A verified JWT only ever raises a request's
priority — the AAC never gates access on authentication. Phase 4 replaces the fixed-limit
stand-in for `controller: adaptive` backends with a real p95-based adaptive controller: a
background loop periodically shrinks or grows each backend's concurrency limit from its rolling
p95 latency, timeout rate, and 5xx rate, relative to a configured target and thresholds — an
overloaded backend gets throttled automatically, and a healthy one is allowed to grow back
towards its configured maximum. Full observability (metrics, admin API, diagnostic headers) lands
in Phase 5.

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
- Redis (for `/readyz` and Redis-backed penalty counters)

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

## GeoIP/ASN database refresh

The AAC reads two local MaxMind GeoLite2 files at startup — `geoip.city_db_path` and
`geoip.asn_db_path` (`config/backends.yaml`) — and never downloads either itself; a missing or
corrupt file fails open (that half of country/ASN lookups just returns `None`). Refreshing a file
is a separate, explicit action via `scripts/update_geoip_db.py`, run manually or by deployment
automation, never by the AAC process. It downloads one MaxMind edition per invocation from
MaxMind's direct download API, authenticating via HTTP Basic Auth using account credentials from
the `MAXMIND_ACCOUNT_ID`/`MAXMIND_LICENSE_KEY` environment variables (a free MaxMind account is
required — see <https://dev.maxmind.com/geoip/updating-databases>). The AAC only picks up a
refreshed file on its next restart.

```bash
export MAXMIND_ACCOUNT_ID=... MAXMIND_LICENSE_KEY=...
uv run python scripts/update_geoip_db.py --edition GeoLite2-City --dest-path /var/lib/aac/GeoLite2-City.mmdb
uv run python scripts/update_geoip_db.py --edition GeoLite2-ASN --dest-path /var/lib/aac/GeoLite2-ASN.mmdb
```

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
