# AAC — Open TBDs & Placeholder Values

| Field | Value |
|---|---|
| Status | Living tracker |
| Owner | Ivo Branco |
| Companion docs | `docs/requirements.md`, `docs/implementation_plan.md` |

## Purpose

Consolidated list of values and open conventions in `docs/requirements.md` and
`docs/implementation_plan.md` that are either placeholders (not yet validated
against production data) or genuinely undecided (blocked on a stakeholder
call or real infrastructure detail). Both docs point here inline wherever
such a value appears.

When an item below gets resolved, update the value in the referencing doc
*and* remove (or check off) the item here in the same change — don't let
this list drift from what the docs actually say.

## Resolved items

- [x] **Solr routing convention** — resolved 2026-07-29: `page-search-api` and
  `image-search-api` (renamed from `solr-page-search`/`solr-image-search` — AAC
  fronts the Search API services, not SolrCloud directly) are routed by path
  prefix like every other backend, since all six backends share a single host
  per environment: `/textsearch` → `page-search-api`, `/imagesearch` →
  `image-search-api`. Applied to FR-011a/FR-011b (merged into FR-011a) in
  `requirements.md` and `match.path_prefix` in `implementation_plan.md` §2.3.
- [x] **IPv6 prefix length** — resolved 2026-07-29: configurable, default `/56`
  (`/48` also supported). Applied as `scoring.ipv6_prefix_length: 56` in
  `implementation_plan.md` §2.3 and FR-022 in `requirements.md`.
- [x] **User long-window (3600s) penalty thresholds** and **adaptive
  timeout-rate/error-rate thresholds** — resolved 2026-07-29: Ivo confirmed the
  existing draft numbers (500/2000 req soft/hard, -10/-60 penalty for the user
  3600s window; 0.05/0.10 for timeout/error rate) as best-guess defaults —
  no longer flagged as blocking placeholders, but still subject to the same
  production-data tuning as every other number in `implementation_plan.md`
  §4.4/§2.3 (per its "Initial Values — tune with production data" framing).
- [x] **`service_account`/`internal` base scores** — resolved 2026-07-29: Ivo
  confirmed 90/100 as final, no change needed. No longer flagged as
  placeholders in `requirements.md` §6.3 or `implementation_plan.md` §2.3.
- [x] **`ingress.trusted_proxies`** — resolved 2026-07-29: Ivo confirmed the
  value is installation-dependent by nature (it must name whatever host(s)
  actually run Apache/Caddy in front of the AAC), with a sensible default of
  localhost — `["127.0.0.1", "::1"]` (IPv4 + IPv6) for a co-located front-end.
  Applied in `implementation_plan.md` §2.3; still must be overridden to the
  real front-end IP(s)/CIDRs when Apache/Caddy runs on a separate host
  (FR-010a) — that override is ordinary per-environment deployment config,
  not an open stakeholder question.
- [x] **GeoIP/ASN refresh mechanism** — resolved 2026-08-01: MaxMind's free
  GeoLite2 tier ships country data (`GeoLite2-City`) and ASN data
  (`GeoLite2-ASN`) as two separate files, not one — `geoip.db_path` is now
  `geoip.city_db_path` + `geoip.asn_db_path` (`config/backends.yaml`,
  `implementation_plan.md` §2.3). `scripts/update_geoip_db.py` is now a real
  implementation: one edition per invocation (`--edition
  {GeoLite2-City,GeoLite2-ASN} --dest-path PATH`), downloading via MaxMind's
  direct HTTP API with HTTP Basic Auth from `MAXMIND_ACCOUNT_ID`/
  `MAXMIND_LICENSE_KEY` environment variables (no `AAC_` prefix — these are
  MaxMind's own credentials, not AAC settings). The real deployment *paths*
  for `city_db_path`/`asn_db_path` remain installation-dependent and are
  still open below — only the refresh *mechanism* was the question here, and
  it's now closed.

## Open items

- [ ] **Real production concurrency numbers** for all 6 backends — current values
  are first-draft placeholders. Ivo confirmed (2026-07-29): these are
  installation-dependent (depend on the server/cluster they run on) and not a
  further stakeholder decision — keep as-is for now; a dedicated load-testing
  effort is planned as future work to establish real numbers per install (see
  `implementation_plan.md` Phase 6, §7).
- [ ] **Real backend upstream hosts/ports** — pywb ports (8080–8083) and the
  Search API upstream hosts (`page-search-api`, `image-search-api` in
  `config/backends.yaml` §2.3) are all illustrative placeholders. Ivo confirmed
  (2026-07-29): these are real arquivo.pt services, but the actual host/port
  is installation-dependent (depends on which server/cluster the AAC is
  deployed against) and not a further stakeholder decision — values must be
  set per environment rather than resolved once here.
- [ ] **Cost-model token values** — moot for MVP per `docs/decision_log.md` A3
  (uniform cost = 1); only needed if weighted cost is picked up post-MVP.
- [ ] **Per-backend dispatch timeout values** — concrete placeholder defaults
  now set (5s/60s connect/response for all backends, 10s/120s for
  ArchivePageNow; see `docs/decision_log.md` C1, `requirements.md` §6.6).
  Ivo confirmed (2026-07-29): also installation-dependent, tuned per
  deployment rather than validated once centrally.
- [ ] **GeoIP/ASN `city_db_path`/`asn_db_path`** — placeholder paths
  `/var/lib/aac/GeoLite2-City.mmdb` / `/var/lib/aac/GeoLite2-ASN.mmdb` (see
  `docs/decision_log.md` C6, `implementation_plan.md` §2.3); the real
  deployment paths aren't decided yet — installation-dependent, same as the
  other placeholder paths above. The refresh *mechanism* itself is resolved
  (see "Resolved items" above).
