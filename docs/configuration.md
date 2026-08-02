# Configuration Reference

The AAC is configured through two layers, kept deliberately separate:

- **Process settings** — a small number of `AAC_`-prefixed environment variables: where to find
  things (the config file, Redis).
- **Policy configuration** — a YAML file (default `config/backends.yaml`) describing ingress
  trust, GeoIP database paths, authentication, observability toggles, scoring, and every backend.
  It is re-parsed and re-validated fresh every time it's loaded (once, at process startup).

Invalid or incomplete configuration fails process startup with a non-zero exit — the AAC never
starts up in a partially-configured state and starts serving traffic anyway.

## Process settings (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `AAC_CONFIG_PATH` | `config/backends.yaml` | Path to the policy YAML file. |
| `AAC_REDIS_URL` | `redis://localhost:6379/0` | Redis connection used for scoring counters and the `/readyz` ping. |
| `AAC_LOG_LEVEL` | `INFO` | Root logger level (structured JSON logs — see [Architecture](architecture.md)). |
| `AAC_ADMIN_API_TOKEN` | *(unset)* | Bearer token required by `/admin/*`. If unset or empty, all admin routes fail closed with `403` rather than defaulting to open access. |

MaxMind's own download credentials — `MAXMIND_ACCOUNT_ID` / `MAXMIND_LICENSE_KEY` — are a
separate pair of environment variables read only by `scripts/update_geoip_db.py`, never by the
AAC process itself. See [Deployment — GeoIP database refresh](deployment.md#geoip-asn-database-refresh).

## Policy file (`config/backends.yaml`)

`config/backends.yaml` in the repository is a fully worked, annotated example — it's the
canonical reference for defaults and is what the test suite validates against. Every section
below is a top-level key in that file.

### `ingress`

Controls which peer is trusted to set `X-Forwarded-For`, and how the real client IP is resolved
from it (`app/ingress.py`).

```yaml
ingress:
  trusted_proxies: ["127.0.0.1", "::1"]
  xff_trusted_hops: 1
```

| Field | Type | Required | Description |
|---|---|---|---|
| `trusted_proxies` | list of IP/CIDR strings | yes | Peers allowed to set `X-Forwarded-For`. A request from any other directly-connecting peer is rejected `403` before classification or scoring ever runs. Default in the example config is localhost-only (co-located front-end); override to your real front-end's IP(s)/CIDRs when Apache/Caddy runs on a separate host. |
| `xff_trusted_hops` | int, ≥ 1 | no (default `1`) | The client IP is the `X-Forwarded-For` entry this many hops from the *right*. Default `1` means "the hop immediately before the trusted proxy" — the safest choice against spoofing, since only trusted infrastructure can append entries after it. Increase this if a proxy chain (e.g. CDN + Apache) sits in front. |

If `X-Forwarded-For` is absent, or has fewer entries than `xff_trusted_hops`, the client IP falls
back to the trusted peer's own address — an under-crediting default, not a security hole, since
this is only reached after the peer already passed the trusted-proxy check.

### `geoip`

Two local MaxMind GeoLite2 files, read once at startup (`app/geoip.py`). MaxMind's free tier ships
country data and ASN data as separate files — there is no free combined edition.

```yaml
geoip:
  city_db_path: /var/lib/aac/GeoLite2-City.mmdb
  asn_db_path: /var/lib/aac/GeoLite2-ASN.mmdb
```

| Field | Type | Required | Description |
|---|---|---|---|
| `city_db_path` | path | yes | `GeoLite2-City` database — used for `country`. |
| `asn_db_path` | path | yes | `GeoLite2-ASN` database — used for `asn`. |

Both paths must be present in the config (the field is required), but a missing or corrupt file at
that path fails open at runtime — that dimension's lookups simply return `None` rather than
crashing startup. See [Deployment — GeoIP database refresh](deployment.md#geoip-asn-database-refresh)
for how to populate these files.

### `auth`

Keycloak JWT verification (`app/auth.py`) — purely a scoring/priority input. A verified token
raises a request's base score (see `scoring.base_scores` below); the AAC has no login flow of its
own and never gates access on this. **Disabled by default**: every request classifies as
`anonymous` until a real realm is configured.

```yaml
auth:
  enabled: false
  issuer: https://keycloak.example.org/realms/arquivo
  jwks_url: https://keycloak.example.org/realms/arquivo/protocol/openid-connect/certs
  audience: aac
  jwks_refresh_seconds: 300
```

| Field | Type | Required | Description |
|---|---|---|---|
| `enabled` | bool | no (default `false`) | Turns JWT verification on. |
| `issuer` | string | required if `enabled: true` | Expected `iss` claim. |
| `jwks_url` | URL | required if `enabled: true` | Keycloak JWKS endpoint, polled every `jwks_refresh_seconds`. |
| `audience` | string | no | Expected `aud` claim; if set, audience is enforced. |
| `jwks_refresh_seconds` | float, > 0 | no (default `300`) | JWKS refresh interval. |

Role mapping from a verified token's `realm_access.roles`: `internal` → `UserClass.INTERNAL`,
else `service_account` → `UserClass.SERVICE_ACCOUNT`, else any other successfully verified token
→ `UserClass.RESEARCHER`. No `Authorization` header at all → `UserClass.ANONYMOUS`. A header
present but unverifiable (unknown `kid`, bad signature, expired, wrong issuer/audience) →
`UserClass.UNKNOWN`. If the JWKS endpoint is unreachable (startup or periodic refresh),
verification fails open to "no verifiable token" until a refresh succeeds — this never blocks
requests or gates `/readyz`.

### `observability`

```yaml
observability:
  debug_headers:
    enabled: false
```

| Field | Type | Required | Description |
|---|---|---|---|
| `debug_headers.enabled` | bool | no (default `false`) | Attaches `X-AAC-Backend`/`X-AAC-Score`/`X-AAC-Exempt`/`X-AAC-Reject-Reason` to every response. Off by default since it discloses scoring internals to the requester — see [API Reference](api_reference.md#diagnostic-response-headers). |

### `scoring`

Global defaults plus per-backend overrides for the reputation-scoring formula (`app/scoring.py`).

```yaml
scoring:
  exempt_countries: ["PT"]
  ipv6_prefix_length: 56

  base_scores:
    anonymous: 100
    researcher: 80
    service_account: 90
    internal: 100
    unknown: 50

  score_clamp: { min: -100, max: 100 }

  default_penalties:
    ip: [{ window_seconds: 10, soft_threshold: 10, hard_threshold: 30, soft_penalty: 10, hard_penalty: 40 }]
    net24: [{ window_seconds: 60, soft_threshold: 50, hard_threshold: 200, soft_penalty: 10, hard_penalty: 40 }]
    net6: [{ window_seconds: 60, soft_threshold: 50, hard_threshold: 200, soft_penalty: 10, hard_penalty: 40 }]
    asn: [{ window_seconds: 60, soft_threshold: 200, hard_threshold: 1000, soft_penalty: 20, hard_penalty: 70 }]
    country: [{ window_seconds: 300, soft_threshold: 500, hard_threshold: 2000, soft_penalty: 5, hard_penalty: 30 }]
    user:
      - { window_seconds: 60, soft_threshold: 50, hard_threshold: 200, soft_penalty: 5, hard_penalty: 40 }
      - { window_seconds: 3600, soft_threshold: 500, hard_threshold: 2000, soft_penalty: 10, hard_penalty: 60 }

  overrides:
    page-search-api:
      penalties:
        ip: { soft_threshold: 5, hard_threshold: 15, soft_penalty: 15, hard_penalty: 50 }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `exempt_countries` | list of ISO country codes | no (default empty) | Countries whose traffic skips the `net24`/`net6`/`asn`/`country` penalty *contribution* to the score. The underlying Redis counters still increment for observability; `ip` and `user` penalties always apply regardless. |
| `ipv6_prefix_length` | `48` or `56` | no (default `56`) | IPv6 aggregation bucket size for the `net6` dimension — mirrors what `net24` does for IPv4. |
| `base_scores` | map of `UserClass` value → int | yes | Starting score per identity class, before any penalty is subtracted. Keys must be `anonymous`, `researcher`, `service_account`, `internal`, `unknown`. |
| `score_clamp.min` / `score_clamp.max` | int | yes | Final score is clamped to this range after penalties are subtracted. |
| `default_penalties.<dimension>` | list of penalty windows | yes, one non-empty list per dimension | Dimensions: `ip`, `net24`, `net6`, `asn`, `country`, `user`. Each dimension can carry more than one independent window (e.g. `user` has both a 60s burst window and a 3600s sustained-abuse window) — every window's step function is evaluated and their penalties are summed. |
| `overrides.<backend-name>` | `penalties` + `base_scores` maps | no | Per-backend deep-merge over the global defaults above — a backend touching only one field/dimension leaves everything else (other dimensions, other backends) unchanged. `overrides` keys must name a backend actually listed in `backends` below, or config validation fails. |

Each penalty window (`PenaltyConfig`) has five fields, all required: `window_seconds` (> 0),
`soft_threshold` (≥ 0), `hard_threshold` (≥ 0, must be ≥ `soft_threshold`), `soft_penalty` (≥ 0),
`hard_penalty` (≥ 0, must be ≥ `soft_penalty`). Below `soft_threshold`: no penalty. Between soft
and hard: `soft_penalty`. At or above hard: `hard_penalty`.

### `backends`

A non-empty list of backend definitions. Each backend uses a **discriminated union** on the
`controller` field — `fixed` or `adaptive` — so only the fields relevant to that controller type
need to be set.

Fields common to every backend (`_BackendCommon` in `app/config.py`):

| Field | Type | Description |
|---|---|---|
| `name` | string, unique | Identifies the backend everywhere — metrics labels, Redis penalty keys, `/admin/backends/{name}/...`. |
| `upstreams` | non-empty list of `{url: http(s) URL}` | One or more physical instances of the same logical service. Only the scheme/host/port of each URL is used; the original request path and query string are forwarded unchanged (a drop-in replacement for an Apache `ProxyPass`). Duplicate URLs within one backend's list are rejected at load time. See [Multi-instance load balancing](#multi-instance-load-balancing) below for how requests are spread across more than one entry. |
| `backup_upstreams` | list of `{url: http(s) URL}` | No effect while any `upstreams` entry is healthy — a pure emergency-standby pool used only once every primary instance is down. Empty by default. See [Backup instances](#backup-instances) below. |
| `match.path_prefix` | string, unique | The path prefix this backend owns. Matching is longest-prefix-wins and path-segment-boundary aware, so `/noFrame/replay` and `/noFrame/patching` correctly resolve to distinct backends despite sharing a prefix. |
| `connect_timeout_seconds` | float, > 0 | Upstream TCP connect timeout — shared across every instance in `upstreams` (instances are assumed equal-capacity clones of the same service, so there's no per-instance override). |
| `backend_timeout_seconds` | float, > 0 | Upstream read/write timeout — a distinct concern from `queue_timeout_seconds` below, which only bounds *queue wait*, not backend response time. |
| `queue_max_size` | int, > 0 | Hard cap on this backend's priority queue depth. Exceeding it → `429`, `reason=queue_full`. |
| `queue_timeout_seconds` | float, > 0 | Both the budget a queued request is allowed to wait before a `503 reason=queue_timeout`, *and* the threshold the predictive rejection compares its projected wait against (`429 reason=queue_wait_exceeded`). |
| `health_check_interval_seconds` | float, > 0 | No effect for a single-instance backend. For a multi-instance backend, how often instances currently marked down are re-probed for recovery. Default `10`. |
| `sticky_sessions` | bool | No effect for a single-instance backend. For a multi-instance backend, whether repeat requests from the same client IP are pinned to the same instance. Default `true`. |
| `sticky_session_ttl_seconds` | float, > 0 | No effect when `sticky_sessions` is `false` or there's only one instance. How long a client's pin is kept after its last use before being dropped. Default `300`. |

**`controller: fixed`** additionally requires:

| Field | Type | Description |
|---|---|---|
| `concurrency_limit` | int, > 0 | Static cap on in-flight requests to this backend. |

**`controller: adaptive`** additionally requires:

| Field | Type | Description |
|---|---|---|
| `min_concurrency` / `initial_concurrency` / `max_concurrency` | int, > 0 | The limit starts at `initial_concurrency` and is adjusted within `[min_concurrency, max_concurrency]` over time. |
| `target_p95_ms` | float, > 0 | Target p95 latency the adjustment loop steers toward. |
| `timeout_rate_threshold` | float, (0, 1] | Rolling timeout-rate threshold that triggers the most aggressive shrink (60% of current limit). |
| `error_rate_threshold` | float, (0, 1] | Rolling 5xx-rate threshold that triggers a moderate shrink (75%). |

See [Architecture — Concurrency control](architecture.md#concurrency-control) for exactly how
these interact.

### Multi-instance load balancing

A backend's `upstreams` list can name more than one physical instance of the same service (e.g.
several identical replicas behind a load balancer VIP, or several uwsgi worker processes on
different ports). The admission/queueing layer above (`concurrency_limit`/adaptive controller,
priority queue) is entirely unaware of this — it still governs one shared pool of in-flight
requests per backend *name*. A separate selection layer picks which instance serves each
already-admitted request:

- **Selection**: among currently-healthy instances, the one with the fewest in-flight requests
  ("most capacity available") is chosen. There is no static weight to configure — this is
  deliberate, since instances are assumed to be equal-capacity clones of the same service.
- **Sticky sessions** (`sticky_sessions`, on by default): repeat requests from the same client IP
  are pinned to whichever instance served them first, for up to `sticky_session_ttl_seconds` since
  their last request. The pin is dropped — and a new one chosen — if that instance goes unhealthy,
  or if it's carrying more than its fair share of load (`ceil(current_limit / healthy_instance_
  count)`) while another healthy instance has real headroom below that share. If every instance is
  equally at its fair share, the pin is kept rather than evicted for no reason.
- **Health checking**: an instance the AAC can't connect to (connection refused, or no response
  within `connect_timeout_seconds`) is marked down immediately and excluded from selection. A
  background loop, every `health_check_interval_seconds`, re-probes only the currently-down
  instances with a raw TCP connect and brings one back into rotation as soon as it accepts a
  connection — no operator action needed. If every instance of a backend is down, selection fails
  open (returns one anyway) rather than blocking or rejecting every request outright; the resulting
  connection failure surfaces as the existing `502`/`connect_failed` bookkeeping.
- **Observability**: `GET /metrics` exposes per-instance `admission_instance_inflight_requests` and
  `admission_instance_healthy` gauges, and `GET /admin/backends/{name}/upstreams` returns a live
  per-instance snapshot (url, healthy, in-flight count, sticky-pin count) — see
  [API Reference](api_reference.md).

See [Known Limitations](known_limitations.md) for the scope this deliberately doesn't cover (e.g.
sticky-session state is in-memory per-process, and a partial outage doesn't shrink the backend's
overall admission limit).

### Backup instances

A backend can optionally configure `backup_upstreams` — a second tier of instances that receive
traffic only once **every** entry in `upstreams` is unhealthy:

```yaml
  - name: pywb-noframe
    upstreams: [{ url: http://pywb-noframe:8081 }]
    backup_upstreams:
      - url: http://pywb-noframe-standby:8081
    match:
      path_prefix: /noFrame/replay
    ...
```

- **Trigger is health-only, never capacity.** Backups stay untouched while any primary instance is
  healthy, even if every primary is fully saturated — primaries only ever redistribute load among
  themselves in that case, exactly as without backups configured. Only a full primary outage (every
  `upstreams` entry unhealthy) activates the backup pool.
- **Selection, health checking, and sticky sessions are shared machinery**, not a separate code
  path — once active, backups are selected by the same least-in-flight rule, participate in the
  same passive/active health checking, and can be sticky-pinned the same way a primary can.
- **Automatic failback.** Once a primary recovers, it's immediately eligible for selection again;
  any client sticky-pinned to a backup is treated like a stale pin (its pinned instance is no
  longer in the active candidate set) and is transparently rerouted to a primary on its very next
  request — no separate eviction logic, no manual intervention.
- If every instance — primary and backup alike — is unhealthy, selection fails open across the full
  combined set, exactly as it does for primaries alone today.
- `GET /admin/backends/{name}/upstreams` reports `is_backup` per instance so operators can tell
  which tier each URL belongs to (see [API Reference](api_reference.md)).

### Config-level validation

`AACConfig` (`app/config.py`) additionally enforces, at load time:

- No two backends share a `name`.
- No two backends share a `match.path_prefix`.
- No backend's combined `upstreams` + `backup_upstreams` contains a duplicate URL (compared after
  normalizing trailing slashes) — including the same URL listed as both a primary and a backup.
- Every key under `scoring.overrides` must name a backend that actually exists in `backends`.
- `ingress.trusted_proxies` entries must each parse as a valid IP network.
- `auth.enabled: true` requires both `auth.issuer` and `auth.jwks_url` to be set.

Any violation raises a Pydantic validation error at startup, which the AAC treats as fatal
(non-zero exit) rather than starting up with a partially-valid configuration.

## Installation-specific values

The values shipped in `config/backends.yaml` — concurrency limits, upstream hosts/ports, timeout
values, GeoIP file paths, Keycloak issuer/JWKS URL — are illustrative defaults, not production
numbers. They must be tuned per environment. See [Known Limitations](known_limitations.md) for the
specific fields that are still installation-dependent placeholders.
