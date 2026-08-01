# Adaptive Admission Controller (AAC)

An async reverse proxy for [arquivo.pt](https://arquivo.pt) that sits between the front-end
(Apache httpd/Caddy) and its backend services. It protects those backends from overload with
per-backend concurrency control (fixed or adaptive), and keeps things fair under contention with a
Redis-backed reputation score that prioritizes legitimate traffic — without ever hard-blocking a
client outright. A backend can also be horizontally scaled across multiple upstream instances,
load-balanced by available capacity with sticky sessions and automatic health-based failover.

See [docs/overview.md](docs/overview.md) for the full picture, or jump straight to what you need:

- **[Overview](docs/overview.md)** — what problem this solves, design principles, request lifecycle
- **[Architecture](docs/architecture.md)** — module map, internals, fail-open behavior
- **[Configuration Reference](docs/configuration.md)** — every YAML field and environment variable
- **[Docker & Deployment Guide](docs/deployment.md)** — Docker Compose, GeoIP refresh, load testing, production rollout
- **[Extending the AAC](docs/development.md)** — how to add a backend, a scoring dimension, or a new pluggable component
- **[API Reference](docs/api_reference.md)** — every endpoint, metric, and error response
- **[Known Limitations](docs/known_limitations.md)** — what's a placeholder or not yet implemented

## The six backends

Routed purely by longest-prefix-wins path matching — no host-based routing:

| Path prefix | Backend | Controller |
|---|---|---|
| `/textsearch` | `page-search-api` | Adaptive |
| `/imagesearch` | `image-search-api` | Adaptive |
| `/wayback` | `pywb-framed` | Fixed |
| `/noFrame/replay` | `pywb-noframe` | Fixed |
| `/noFrame/patching` | `pywb-patching` | Fixed |
| `/save` | `pywb-archivepagenow` | Fixed |

## Quick start

```bash
uv sync
uv run uvicorn app.main:create_app --factory --reload
```

Requires Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and a reachable Redis instance
(`AAC_REDIS_URL`, default `redis://localhost:6379/0`).

Or with Docker Compose (starts the AAC + Redis):

```bash
docker compose up --build
```

See [Docker & Deployment Guide](docs/deployment.md) for building the image directly, GeoIP/ASN
database refresh, load testing, and the production rollout runbook.

## Configuration

Configured via a YAML policy file (default `config/backends.yaml`, overridable with
`AAC_CONFIG_PATH`) plus a handful of `AAC_`-prefixed environment variables. Invalid or incomplete
configuration fails process startup rather than serving traffic. See the
[Configuration Reference](docs/configuration.md) for every field, default, and validation rule.

## Testing

```bash
uv run pytest        # unit + integration tests
uv run ruff check .  # lint
```

Integration tests drive the full app against a real-socket mock upstream backend via
`httpx.ASGITransport`. See [Extending the AAC — Testing philosophy](docs/development.md#testing-philosophy).

## History

This project's original requirements specification, design-decision log, and phased implementation
plan are preserved under [`docs/old/`](docs/old/) for historical reference.
