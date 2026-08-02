#!/usr/bin/env bash
# Automates the failover/failback verification that has previously been done
# by hand (most recently for the backup-instances feature): stand up the AAC
# via Docker Compose against two stub backend containers (primary + backup),
# kill the primary, confirm traffic fails over to the backup, bring the
# primary back, and confirm traffic fails back — all as one repeatable,
# CI-usable script instead of a throwaway override file typed by hand each
# time `app/load_balancer.py`/`app/dispatcher.py` changes.
#
# Usage: scripts/smoke_test_failover.sh
# Requires: docker compose (with the plugin, not docker-compose v1).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

TMPDIR="$(mktemp -d)"
OVERRIDE_FILE="$TMPDIR/docker-compose.override.smoke.yml"
COMPOSE=(docker compose -f docker-compose.yml -f "$OVERRIDE_FILE")

cleanup() {
    "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$TMPDIR"
}
trap cleanup EXIT

log() { echo "[smoke] $*" >&2; }

fail() {
    log "FAIL: $*"
    exit 1
}

# A minimal config with one backend (`smoke-backend`, path_prefix "/") behind
# a primary + backup pair. `health_check_interval_seconds: 2` keeps this
# script's failback wait short; a real deployment tunes this far higher (see
# config/backends.yaml's `pywb-framed` example, 10s).
mkdir -p "$TMPDIR/config"
cat >"$TMPDIR/config/backends.yaml" <<'YAML'
ingress:
  trusted_proxies: ["127.0.0.1"]
  xff_trusted_hops: 1
geoip:
  city_db_path: /nonexistent/GeoLite2-City.mmdb
  asn_db_path: /nonexistent/GeoLite2-ASN.mmdb
observability:
  debug_headers:
    enabled: false
scoring:
  exempt_countries: []
  base_scores: { anonymous: 100 }
  score_clamp: { min: -100, max: 100 }
  default_penalties:
    ip: [{ window_seconds: 60, soft_threshold: 100000, hard_threshold: 200000, soft_penalty: 0, hard_penalty: 0 }]
    net24: [{ window_seconds: 60, soft_threshold: 100000, hard_threshold: 200000, soft_penalty: 0, hard_penalty: 0 }]
    net6: [{ window_seconds: 60, soft_threshold: 100000, hard_threshold: 200000, soft_penalty: 0, hard_penalty: 0 }]
    asn: [{ window_seconds: 60, soft_threshold: 100000, hard_threshold: 200000, soft_penalty: 0, hard_penalty: 0 }]
    country: [{ window_seconds: 60, soft_threshold: 100000, hard_threshold: 200000, soft_penalty: 0, hard_penalty: 0 }]
    user: [{ window_seconds: 60, soft_threshold: 100000, hard_threshold: 200000, soft_penalty: 0, hard_penalty: 0 }]
backends:
  - name: smoke-backend
    upstreams: [{ url: http://smoke-primary:8080 }]
    backup_upstreams: [{ url: http://smoke-backup:8080 }]
    match: { path_prefix: / }
    controller: fixed
    concurrency_limit: 100
    connect_timeout_seconds: 2
    backend_timeout_seconds: 10
    queue_max_size: 100
    queue_timeout_seconds: 30
    health_check_interval_seconds: 2
    sticky_sessions: true
YAML

# Two stub backends: plain `python -m http.server` serving a distinct static
# body each, so a response's content proves which instance actually served
# it (not just that *some* 200 came back).
cat >"$OVERRIDE_FILE" <<YAML
services:
  aac:
    environment:
      AAC_CONFIG_PATH: /app/smoke-config/backends.yaml
    volumes:
      - $TMPDIR/config:/app/smoke-config:ro
    depends_on:
      smoke-primary:
        condition: service_started
      smoke-backup:
        condition: service_started

  smoke-primary:
    image: python:3.12-slim
    command: ["sh", "-c", "mkdir -p /www && echo primary-ok > /www/index.html && exec python -m http.server 8080 --directory /www"]

  smoke-backup:
    image: python:3.12-slim
    command: ["sh", "-c", "mkdir -p /www && echo backup-ok > /www/index.html && exec python -m http.server 8080 --directory /www"]
YAML

# Requests run *inside* the aac container (via `exec`) rather than against
# the host-published port: this keeps the request's source IP 127.0.0.1,
# matching `ingress.trusted_proxies` above, and avoids needing `curl` in the
# runtime image (see docs/development.md's testing conventions).
aac_get() {
    "${COMPOSE[@]}" exec -T aac python -c "
import httpx
try:
    r = httpx.get('http://localhost:8000/', timeout=5)
    print(r.status_code)
    print(r.text.strip())
except httpx.HTTPError as exc:
    print('REQUEST_ERROR')
    print(exc)
"
}

log "Building and starting the stack..."
"${COMPOSE[@]}" up --build -d

log "Waiting for the AAC to become healthy..."
for _ in $(seq 1 30); do
    if "${COMPOSE[@]}" exec -T aac python -c "
import httpx, sys
sys.exit(0 if httpx.get('http://localhost:8000/healthz', timeout=2).status_code == 200 else 1)
" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

log "Baseline request must be served by the primary..."
result="$(aac_get)"
status="$(echo "$result" | sed -n 1p)"
body="$(echo "$result" | sed -n 2p)"
[ "$status" = "200" ] || fail "expected 200 from primary, got: $result"
[ "$body" = "primary-ok" ] || fail "expected primary-ok body, got: $result"
log "OK: baseline served by primary"

log "Stopping the primary to force failover..."
"${COMPOSE[@]}" stop smoke-primary >/dev/null

result="$(aac_get)"
status="$(echo "$result" | sed -n 1p)"
[ "$status" = "502" ] || fail "expected 502 on first request to dead primary, got: $result"
log "OK: first request after primary death got 502 (marks it down)"

result="$(aac_get)"
status="$(echo "$result" | sed -n 1p)"
body="$(echo "$result" | sed -n 2p)"
[ "$status" = "200" ] || fail "expected 200 from backup, got: $result"
[ "$body" = "backup-ok" ] || fail "expected backup-ok body, got: $result"
log "OK: failover to backup succeeded"

log "Restarting the primary to verify failback..."
"${COMPOSE[@]}" start smoke-primary >/dev/null
# health_check_interval_seconds: 2 above; give it a few ticks.
sleep 6

result="$(aac_get)"
status="$(echo "$result" | sed -n 1p)"
body="$(echo "$result" | sed -n 2p)"
[ "$status" = "200" ] || fail "expected 200 from recovered primary, got: $result"
[ "$body" = "primary-ok" ] || fail "expected traffic to fail back to primary-ok, got: $result"
log "OK: failback to recovered primary succeeded"

log "All checks passed."
