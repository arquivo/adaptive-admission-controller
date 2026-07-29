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

## Open items

- [ ] **Solr routing convention** — how `solr-page-search` vs `solr-image-search`
  is distinguished (host / path prefix / query param). Blocks FR-011b (Must) and
  the two `match.path_prefix: null` entries. *(pre-existing)*
- [ ] **Real production concurrency numbers** for all 6 backends — current values
  are first-draft placeholders. *(pre-existing)*
- [ ] **Real pywb upstream ports** — 8080–8083 are placeholders. *(pre-existing)*
- [ ] **IPv6 prefix length** — /48 vs /56 (FR-022 leaves it open;
  `requirements.md` §6.3). Materially affects abuse aggregation.
- [ ] **Cost-model token values** — moot for MVP per `docs/decision_log.md` A3
  (uniform cost = 1); only needed if weighted cost is picked up post-MVP.
- [ ] **Per-backend dispatch timeout values** — concrete placeholder defaults
  now set (5s/60s connect/response for all backends, 10s/120s for
  ArchivePageNow; see `docs/decision_log.md` C1, `requirements.md` §6.6) but
  not yet validated against production data.
- [ ] **User long-window (3600s) penalty thresholds** — soft/hard
  threshold + penalty values for the new sustained-abuse window added under
  `docs/decision_log.md` A7.
- [ ] **Adaptive timeout-rate/error-rate thresholds** —
  `timeout_rate_threshold`/`error_rate_threshold` placeholder defaults
  (0.05/0.10) on the two Solr backend blocks (see `docs/decision_log.md` C3),
  not yet validated against production data.
- [ ] **GeoIP/ASN `db_path`** — placeholder path
  `/var/lib/aac/GeoLite2-City.mmdb` (see `docs/decision_log.md` C6,
  `implementation_plan.md` §2.3); set to the real deployment path before
  production.
- [ ] **`service_account`/`internal` base scores** — placeholder values (90/100,
  see `docs/decision_log.md` A4, `requirements.md` §6.3) not yet validated.
