# AAC Overview

The Adaptive Admission Controller (AAC) is an async reverse proxy that sits between
[arquivo.pt](https://arquivo.pt)'s front-end web server (Apache httpd or Caddy) and its backend
services. It protects those backends from overload and makes sure legitimate traffic is served
first when a backend is under contention — without ever blocking a client outright.

## Problem it solves

arquivo.pt's backends aren't uniform: two Search API services (backed by SolrCloud) and four pywb
processes (replay, patching, and live-capture) each have very different cost and latency
profiles. A burst of traffic — whether organic or a distributed scraping run spread across many
IPs/ASNs — can overwhelm any one of them. The AAC addresses this with three cooperating
mechanisms:

1. **Concurrency control** — each backend has its own capacity limit (fixed or adaptive) that
   caps how many requests can be in flight to it at once.
2. **Priority scheduling** — requests waiting for a capacity slot sit in a per-backend priority
   queue, ordered by a computed score, so contention degrades gracefully (favoring legitimate
   traffic) instead of failing indiscriminately.
3. **Reputation-based scoring** — every request is scored from its source IP, subnet, ASN,
   country, and (optionally) authenticated identity, using Redis-backed rolling counters. Abusive
   traffic patterns lower a request's score and push it toward the back of the queue; they are
   never blocked outright.

Home-country traffic (Portugal, `PT`) is explicitly exempted from the subnet/ASN/country
dimensions of scoring, since arquivo.pt's primary audience is Portuguese researchers and the
system must not penalize its own core users for looking, in aggregate, like a lot of traffic from
one country.

## Design principles

- **Never hard-block.** The AAC's job is to *prioritize*, not deny. Under contention, low-score
  requests wait longer or are rejected with `429`/`503` once a queue is provably full or a
  projected wait already exceeds its budget — but there is no separate "ban" mechanism.
- **Fail open.** Every optional dependency (Redis for scoring, GeoIP databases, Keycloak for
  identity) degrades gracefully on failure rather than blocking admission. See
  [Architecture — Fail-open behavior](architecture.md#fail-open-behavior) for the specifics per
  subsystem.
- **Pluggable from day one.** Capacity control, scheduling, and backend classification are each
  behind an abstract interface (`app/interfaces.py`), even though only one production
  implementation of each exists today. See [Extending the AAC](development.md) for how to add a
  new one.
- **Config-driven, not code-driven.** Per-backend behavior (routing, concurrency limits, scoring
  overrides, timeouts) lives entirely in `config/backends.yaml`. Adding or retuning a backend
  should never require a code change. See [Configuration Reference](configuration.md).

## Request lifecycle

Every request flows through the same pipeline, regardless of which backend it targets:

```
TrustedProxyMiddleware  →  registry.match()  →  classify()  →  score_engine.score()
        (403 or                (404 on               |                |
     resolve client IP)          miss)          RequestContext    sets ctx.score
                                                                        |
                                                                        v
                                              scheduler.enqueue()  (429 on queue full /
                                                     |               projected wait exceeded)
                                                     v
                                        await Future, bounded by queue_timeout_seconds
                                                     |               (503 on timeout)
                                                     v
                                     Response from a background worker via BackendDispatcher
```

See [Architecture](architecture.md) for what each stage actually does and which module owns it.

## The six backends

All backends live behind a single AAC instance and are routed purely by longest-prefix-wins path
matching — there is no host-based routing.

| Path prefix | Backend | Controller | Notes |
|---|---|---|---|
| `/textsearch` | `page-search-api` | Adaptive | Fronts arquivo.pt's page-search SolrCloud cluster. |
| `/imagesearch` | `image-search-api` | Adaptive | Fronts arquivo.pt's image-search SolrCloud cluster. |
| `/wayback` | `pywb-framed` | Fixed | Framed replay. |
| `/noFrame/replay` | `pywb-noframe` | Fixed | Frameless replay. |
| `/noFrame/patching` | `pywb-patching` | Fixed | Patched replay (low concurrency — expensive per request). |
| `/save` | `pywb-archivepagenow` | Fixed | Live capture ("Save Page Now") — much longer timeouts, since it fetches the live page rather than serving from the existing archive. |

## Where to go next

- **Deploying it** → [Docker & Deployment Guide](deployment.md)
- **Configuring it for your environment** → [Configuration Reference](configuration.md)
- **Understanding how it works internally** → [Architecture](architecture.md)
- **Adding a backend, scoring dimension, or controller type** → [Extending the AAC](development.md)
- **Every HTTP endpoint it exposes** → [API Reference](api_reference.md)
- **What's still a placeholder / not implemented** → [Known Limitations](known_limitations.md)

## History

The AAC was built from a formal requirements/design specification, refined through a documented
design-decision process. That original specification, decision rationale, and phase-by-phase
implementation plan are preserved under [`docs/old/`](old/) for historical reference — they are
no longer maintained and may describe intermediate states superseded by the documentation above.
