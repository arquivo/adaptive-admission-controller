# Known Limitations

## No dry-run / observe-only mode

The AAC has no built-in "shadow" or "observe-only" mode — a mode that would classify, score, and
log its admission decision without actually enforcing it. This was considered during the original
design (`Could`-priority requirement) but never implemented. As a result, the [production rollout
runbook](deployment.md#production-rollout)'s "shadow mode" step has to be done by *not* routing
real traffic through the AAC yet (an Apache-level mirroring/canary decision), rather than by
routing all traffic through it in a non-enforcing mode. If this is picked up later, the natural
place for it is a config flag that makes `scheduler.enqueue()` always admit immediately while
still running scoring/classification and emitting the same metrics and logs — so the shape of the
work is understood even though it's unbuilt.

## Uniform request cost

Every request costs `1` unit of concurrency (`DefaultBackendPolicy.estimate_cost()` in
`app/registry.py` always returns `1`) — `admission_inflight_tokens` tracks this separately from
`admission_inflight_requests` for exactly this reason, but the two gauges move identically today.
A weighted-cost model (e.g. large video WARC replay costing more than a small text snippet) was
explicitly deferred as a post-MVP feature, not designed in detail. Picking it up requires: a real
cost table per request shape, and re-validating every backend's concurrency limits against the new
maximum per-request cost (a limit tuned assuming cost-1 requests may be wrong once some requests
cost more).

## Installation-dependent placeholder values

`config/backends.yaml` ships illustrative defaults, not production numbers. These require
per-environment tuning before a real rollout, independent of any further design decision:

| Value | Current placeholder | Why it can't be resolved centrally |
|---|---|---|
| Per-backend concurrency limits (`concurrency_limit`, `initial_concurrency`/`min_concurrency`/`max_concurrency`) | See [Deployment — Tentative initial production limits](deployment.md#tentative-initial-production-limits) | Depends on the actual server/cluster capacity behind each backend in a given deployment; needs real load-test data (`scripts/load_test.py`) per install. |
| Backend `upstreams` hosts/ports | Illustrative hostnames (`page-search-api:8080`, etc.) | Real arquivo.pt services, but the actual host/port depends on which server/cluster a given deployment targets. |
| `connect_timeout_seconds` / `backend_timeout_seconds` | 5s/60s for most backends, 10s/120s for `pywb-archivepagenow` (it fetches a live page, not an archived one) | Reasonable starting defaults, but tuned per deployment against real backend latency characteristics. |
| `geoip.city_db_path` / `geoip.asn_db_path` | `/var/lib/aac/GeoLite2-City.mmdb` / `/var/lib/aac/GeoLite2-ASN.mmdb` | Real filesystem paths depend on where a given deployment stores the MaxMind databases; the *refresh mechanism* itself (`scripts/update_geoip_db.py`) is fully implemented — see [Deployment — GeoIP/ASN database refresh](deployment.md#geoip-asn-database-refresh). |
| `ingress.trusted_proxies` | `["127.0.0.1", "::1"]` (co-located front-end default) | Must name whatever host(s) actually run Apache/Caddy in front of a given AAC instance — not resolvable without knowing the topology. |
| `auth.issuer` / `auth.jwks_url` | Example Keycloak URLs, `auth.enabled: false` | Real Keycloak realm details are per-deployment; auth is off until configured. |

None of these are open design questions — the *shape* of every value above is decided; only the
*number* is installation-specific.

## Cost-model token values

Not applicable while request cost is uniformly `1` (see above) — this only becomes relevant if
weighted cost is implemented.

## Multi-instance load balancing scope limits

Three deliberate scope cuts in `LeastLoadedLoadBalancer` (`app/load_balancer.py`, see
[Architecture — Instance selection](architecture.md#instance-selection)), each stated explicitly
rather than silently assumed:

- **Sticky-session state is in-memory, per-process only.** The client-IP → pinned-instance map
  lives in one `LeastLoadedLoadBalancer` object per backend, inside one process. This is fine
  today — there is no evidence the AAC itself runs as more than one process — but a client's pin
  would not be shared if that ever changed; a multi-process deployment would need Redis-backed
  sticky state instead.
- **A backend's overall admission limit doesn't shrink when some instances are down.**
  `CapacityController.current_limit()` (the shared per-backend concurrency cap enforced *above*
  instance selection) is entirely unaware of how many of that backend's `upstreams` are currently
  healthy — a partial outage (e.g. one of three instances down) funnels the full configured
  concurrency onto the survivors rather than reducing it proportionally. Fixing this would mean
  wiring `LoadBalancer` health state into `AdaptiveController`'s existing shrink/grow logic, which
  needs product sign-off on the intended behavior (e.g. should `fixed`-controller backends also
  shrink?) rather than being a purely mechanical change.
- **Connect/read timeouts are shared per-backend, not per-instance.** `connect_timeout_seconds`/
  `backend_timeout_seconds` apply identically to every entry in a backend's `upstreams` — there is
  no way to give one instance a longer timeout than its siblings. This is consistent with the
  no-per-instance-weight design decision (instances are assumed to be equal-capacity clones of the
  same service); a genuinely heterogeneous fleet behind one backend name isn't supported.

## Everything else is resolved

The original design's open-TBD list (`docs/old/open_tbd.md`) additionally tracked the Solr/Search
API routing convention, IPv6 prefix length, user long-window penalty thresholds, adaptive
timeout/error-rate thresholds, `service_account`/`internal` base scores, and the GeoIP/ASN refresh
*mechanism* — all of these were resolved during implementation and are reflected as-is in
[Configuration Reference](configuration.md) and [Architecture](architecture.md). See
[`docs/old/open_tbd.md`](old/open_tbd.md) and [`docs/old/decision_log.md`](old/decision_log.md) for
the full historical rationale behind each.
