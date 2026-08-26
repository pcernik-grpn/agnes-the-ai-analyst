#!/usr/bin/env bash
# M-tier smoke: boots the role-split profile, asserts probes + kill-one-api
# continuity + redis coordination plumbing + FLUSHALL chaos resilience +
# chat/gateway-role WS-ticket + restart continuity (infra path — see §4) +
# DuckLake catalog attach/readyz/live-query (wave-2G Task 5 — see §1.5/1.6).
# Local/dev harness — needs docker; not run in unit CI.
set -euo pipefail
cd "$(dirname "$0")/../.."

export JWT_SECRET_KEY="${JWT_SECRET_KEY:-$(openssl rand -hex 32)}"
export SESSION_SECRET="${SESSION_SECRET:-$(openssl rand -hex 32)}"
# Required by docker-compose.postgres.yml's postgres side-car — no literal
# default there by design (LOW-2), so the harness must supply one.
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 20)}"
# The base `app` service (docker-compose.yml) declares `env_file: .env`,
# which Compose treats as a hard error when the file is absent. A fresh
# checkout (CI, a throwaway load-test VM) has no `.env` — every secret this
# harness needs is exported above and reaches the containers via `${...}`
# interpolation, so an empty file is enough to satisfy the directive. Only
# create it when missing; never clobber a developer's real `.env`.
[ -f .env ] || : > .env
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.mtier.yml --profile mtier)

cleanup() { "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

AUTH_TOKEN_PAYLOAD='{"email":"smoke-nobody@example.com","password":"wrong"}'

# Fire one throwaway POST /auth/token (bad creds -> 401, but slowapi's
# @limiter.limit decorator increments the rate-limit bucket *before* the
# handler runs, so the request still counts). Ignored on purpose — used
# only to make the auth rate limiter touch redis.
hit_auth_token() {
  curl -fsS -m 2 -o /dev/null -X POST localhost:8080/auth/token \
    -H 'Content-Type: application/json' -d "$AUTH_TOKEN_PAYLOAD" >/dev/null 2>&1 || true
}

readyz_is_ready() {
  curl -fsS -m 2 localhost:8080/readyz 2>/dev/null | grep -Eq '"status":[[:space:]]*"ready"'
}

# --- Chat/gateway WS-ticket helpers (§4 below) ---------------------------
# app/api/chat.py's `_issue_ticket`/`_consume_ticket` are just
# `coordination().kv_set`/`kv_delete` — plain redis SET/GETDEL on key
# `ws-ticket:{token}` with a 60s TTL (see app/coordination/redis_backend.py
# `kv_set`/`kv_delete`). Minting one directly via redis-cli stands in for a
# real `POST /api/chat/sessions` call (which needs an authenticated,
# chat-RBAC-granted user this harness doesn't seed — see §4's comment for
# why the rest of a live session is out of reach here).
CHAT_SMOKE_ID="smoke-chat-$$"

mint_ws_ticket() {
  # $1 = token
  local payload
  payload=$(printf '{"chat_id":"%s","user_email":"smoke-nobody@example.com"}' "$CHAT_SMOKE_ID")
  "${COMPOSE[@]}" exec -T redis redis-cli SET "ws-ticket:${1}" "$payload" EX 60 >/dev/null
}

ticket_still_present() {
  # $1 = token; true (exit 0) iff the key still exists in redis.
  "${COMPOSE[@]}" exec -T redis redis-cli EXISTS "ws-ticket:${1}" 2>/dev/null | tr -d '\r' | grep -q '^1$'
}

ws_handshake_101() {
  # $1 = token. Prints 1 if the WS route accepted the upgrade (HTTP 101),
  # else 0. Exec'd from api1 (a DIFFERENT role container than gateway) so
  # the ticket consume is genuinely cross-container over the shared redis
  # backend — the app image already has curl (see the prometheus check
  # above / Dockerfile). `ws.accept()` (app/api/chat.py::ws_stream) runs
  # BEFORE `ChatManager.attach()`, so 101 is observable even though
  # chat.enabled=false here makes app.state.chat_manager None on every
  # role — attach() then raises right after (mgr is None) and the
  # connection drops, which is expected in this reduced harness, not a
  # failure of the ticket/coordination path under test. `-D -` dumps raw
  # response headers to stdout so the 101 status line is captured even if
  # curl's own state machine gets confused reading bytes after a protocol
  # upgrade (a known curl quirk on 101 responses); `--max-time` bounds the
  # hang since curl never sees a graceful HTTP close on an upgraded
  # connection.
  local key hdrs
  key=$(openssl rand -base64 16)
  hdrs=$("${COMPOSE[@]}" exec -T api1 curl -sS -D - -o /dev/null --http1.1 --max-time 3 \
    -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
    -H 'Sec-WebSocket-Version: 13' -H "Sec-WebSocket-Key: ${key}" \
    "http://gateway:8000/api/chat/sessions/${CHAT_SMOKE_ID}/stream?ticket=${1}&last_seq=42" 2>/dev/null || true)
  if printf '%s' "$hdrs" | grep -q "101 Switching Protocols"; then
    echo 1
  else
    echo 0
  fi
}

# --- 1. Config plumbing: redis URL must reach every role container ------
# coordination.backend=redis (config/instance.mtier.yaml) but
# resolve_redis_url() (app/coordination/factory.py) reads AGNES_REDIS_URL
# (env, checked first) or redis.url (instance.yaml), falling back to
# redis://localhost:6379/0 if neither is set. Inside a container
# "localhost" is the container itself, not the compose `redis` service —
# a missing override here would silently break every redis-backed
# coordination call (leases, and app/auth/rate_limit.py's rate-limiter
# storage) without failing fast anywhere. Static check on the compose
# overlay, before anything boots.
echo "checking redis URL plumbing in docker-compose.mtier.yml..."
for role in api1 api2 gateway worker; do
  awk -v role="  ${role}:" '
    $0 == role { in_block=1; next }
    in_block && /^  [a-zA-Z0-9_-]+:$/ { in_block=0 }
    in_block { print }
  ' docker-compose.mtier.yml | grep -q 'AGNES_REDIS_URL: redis://redis:' \
    || { echo "FAIL: ${role} stanza missing AGNES_REDIS_URL pointing at the compose redis service"; exit 1; }
done
echo "redis URL plumbing OK (statically, in the compose overlay)"

# --- 1.5 Config plumbing: DuckLake catalog DSN must reach every role -----
# Wave-2G Task 5: this harness flips analytics.backend=ducklake
# (config/instance.mtier.yaml) with an explicit Postgres catalog DSN
# (app.startup_guards.validate_deployment requires one in any multi-process
# topology) supplied per-role via AGNES_DUCKLAKE_CATALOG_DSN, pointed at
# the dedicated `agnes_ducklake` database (see
# deploy/postgres/init-ducklake-db.sql) rather than the `agnes` app-state
# database. Same static-then-live confirmation shape as the redis check
# above.
echo "checking ducklake catalog DSN plumbing in docker-compose.mtier.yml..."
for role in api1 api2 gateway worker; do
  awk -v role="  ${role}:" '
    $0 == role { in_block=1; next }
    in_block && /^  [a-zA-Z0-9_-]+:$/ { in_block=0 }
    in_block { print }
  ' docker-compose.mtier.yml | grep -q 'AGNES_DUCKLAKE_CATALOG_DSN: postgresql' \
    || { echo "FAIL: ${role} stanza missing AGNES_DUCKLAKE_CATALOG_DSN pointing at a postgres DSN"; exit 1; }
done
echo "ducklake catalog DSN plumbing OK (statically, in the compose overlay)"

"${COMPOSE[@]}" up -d --build
for i in $(seq 1 60); do
  curl -fsS localhost:8080/readyz >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS localhost:8080/healthz | grep -q alive || { echo "FAIL healthz"; exit 1; }
curl -fsS localhost:8080/readyz | grep -q ready || { echo "FAIL readyz"; exit 1; }

# Dynamic confirmation that the env actually resolved inside a live
# container — `extends:`/`profiles:` merging can silently drop keys (see
# the api1/api2 anchor-vs-explicit-stanza comment in
# docker-compose.mtier.yml), so the static grep above isn't sufficient on
# its own.
for role in api1 api2 gateway worker; do
  "${COMPOSE[@]}" exec -T "$role" printenv AGNES_REDIS_URL 2>/dev/null | grep -q '^redis://redis:' \
    || { echo "FAIL: ${role} container does not see AGNES_REDIS_URL pointing at the compose redis service"; exit 1; }
done
echo "redis URL plumbing OK (confirmed live inside api1/api2/gateway/worker)"

for role in api1 api2 gateway worker; do
  "${COMPOSE[@]}" exec -T "$role" printenv AGNES_DUCKLAKE_CATALOG_DSN 2>/dev/null | grep -q '^postgresql' \
    || { echo "FAIL: ${role} container does not see AGNES_DUCKLAKE_CATALOG_DSN pointing at a postgres DSN"; exit 1; }
done
echo "ducklake catalog DSN plumbing OK (confirmed live inside api1/api2/gateway/worker)"

# --- 1.6 DuckLake readiness + live query ---------------------------------
# app/main.py's lifespan registers a "ducklake" /readyz check
# (app.api.health_probes.register_readiness_check) whenever
# analytics.backend=ducklake — confirm it's neither missing nor failing
# (the earlier `readyz | grep -q ready` already implies this, since a
# failed extra check flips /readyz to 503, but checking the body directly
# names the exact check under test rather than relying on the aggregate).
echo "checking ducklake readyz check is present and passing..."
readyz_body=$(curl -fsS localhost:8080/readyz)
printf '%s' "$readyz_body" | grep -q '"failed_checks":\[\]' \
  || { echo "FAIL: /readyz reports failed_checks: $readyz_body"; exit 1; }
echo "ducklake readyz check OK (failed_checks empty: $readyz_body)"

# Dynamic, container-local proof that the DuckLake catalog ATTACH + a real
# query round-trips end to end for this exact container — not just that
# the aggregate /readyz probe passed. Runs a trivial `SELECT 1` through
# src.ducklake_session.get_ducklake_read() (the same singleton the
# app/api/query.py / app/api/query_hybrid.py endpoints dispatch to via
# src.db.get_analytics_db_readonly() when analytics.backend=ducklake) —
# the closest equivalent this harness has to "agnes query" without an
# authenticated user + a registered table + a real sync (out of scope for
# this smoke per the task-5 brief: a full sync needs real source
# credentials this harness doesn't configure).
echo "checking a live DuckLake query through api1..."
ducklake_query_out=$("${COMPOSE[@]}" exec -T api1 python -c "
from src.ducklake_session import get_ducklake_read
cur = get_ducklake_read()
try:
    print(cur.execute('SELECT 1').fetchone())
finally:
    cur.close()
" 2>&1)
printf '%s' "$ducklake_query_out" | grep -q '(1,)' \
  || { echo "FAIL: ducklake SELECT 1 did not return (1,) from api1: $ducklake_query_out"; exit 1; }
echo "ducklake live query OK (api1: $ducklake_query_out)"

# --- 2. Coordination-live assertion --------------------------------------
echo "checking redis reachability..."
"${COMPOSE[@]}" exec -T redis redis-cli PING | grep -q PONG || { echo "FAIL: redis PING"; exit 1; }

# Real lease/ws-ticket keys (app/coordination/leases.py's
# `paused-sandbox-sweep`; app/api/chat.py's `ws-ticket:<id>`) only ever
# appear once chat is enabled (chat.enabled=true plus ANTHROPIC_API_KEY
# and a provider whose backing is up — the kai-agent engine sidecar +
# KAI_HOST_JWT_SECRET, or the apps-runner sidecar with a built sandbox
# image; see app/main.py's `_chat_*_ok` startup guards), none of which
# this harness configures (config/instance.mtier.yaml has no `chat:`
# section at all). Forcing that on would pull in an engine/sidecar/
# Anthropic dependency this smoke doesn't otherwise need just to get a
# key to scan for. Same reason: the
# gateway-kill→sweep-lease-reappears continuity check is intentionally
# omitted (sweep lease only fires when chat.enabled=true).
#
# Instead: app/auth/rate_limit.py points slowapi's Limiter storage at the
# exact same resolve_redis_url() the coordination backend itself uses,
# and it is always active (AGNES_AUTH_RATELIMIT_ENABLED defaults on,
# unset here) regardless of chat config. POST /auth/token a few times
# (bad creds -> 401, but the bucket still increments) and confirm a
# `limits`-library key lands in redis — its storage backend prefixes
# every key with "LIMITS:" (see limits.storage.redis.RedisStorage.PREFIX
# in the vendored dependency). This is the most robust real observable of
# the redis coordination wiring working end-to-end through the running
# app, without depending on optional chat/Slack/Telegram features.
hit_auth_token
hit_auth_token
hit_auth_token
coord_ok=0
for i in $(seq 1 45); do
  count=$("${COMPOSE[@]}" exec -T redis redis-cli --scan --pattern 'LIMITS:*' 2>/dev/null | wc -l | tr -d ' ' || true)
  if [ "${count:-0}" -gt 0 ]; then coord_ok=1; break; fi
  sleep 2
done
[ "$coord_ok" -eq 1 ] || { echo "FAIL: no LIMITS:* keys appeared in redis within 90s of boot (coordination redis path not wired)"; exit 1; }
echo "coordination-live OK (redis carries rate-limiter state; LIMITS:* keys=$count)"

# --- 2.5 Prometheus scrape confirmation ----------------------------------
# Wave 2D (observability): deploy/prometheus/prometheus.yml polls every
# role container's `/metrics` (app/observability/metrics.py) every 15s by
# Compose DNS name (api1/api2/gateway/worker:8000, plus cadvisor:8080).
# Confirm via `up{job=~"agnes.*"}` on the Prometheus HTTP API, exec'd from
# inside the compose network (the app image already has curl — see
# Dockerfile) against the `prometheus` service's own DNS name, rather than
# the host-published :9090 port — this also exercises the same Compose
# DNS resolution the scrape targets themselves rely on, not just host
# port forwarding.
echo "checking prometheus scraped at least one agnes role target..."
prom_ok=0
for i in $(seq 1 30); do
  resp=$("${COMPOSE[@]}" exec -T gateway curl -fsS -m 3 -G 'http://prometheus:9090/api/v1/query' \
    --data-urlencode 'query=up{job=~"agnes.*"}' 2>/dev/null || true)
  if printf '%s' "$resp" | grep -q '"value":\[[0-9.]*,"1"\]'; then
    prom_ok=1
    break
  fi
  sleep 2
done
[ "$prom_ok" -eq 1 ] || { echo "FAIL: no agnes-* prometheus target reported up==1 within 60s of boot"; exit 1; }
echo "prometheus scrape OK (an agnes-* target reports up==1)"

echo "killing api1 under traffic..."
"${COMPOSE[@]}" kill api1
fails=0
for i in $(seq 1 20); do
  curl -fsS -m 2 localhost:8080/healthz >/dev/null 2>&1 || fails=$((fails+1))
  sleep 0.5
done
[ "$fails" -le 2 ] || { echo "FAIL: $fails/20 requests failed after killing api1"; exit 1; }
echo "api1 kill-continuity OK (failures: $fails/20)"

echo "restarting api1 before FLUSHALL chaos test..."
# --no-deps mirrors how production actually recreates a role container
# (scripts/ops/agnes-auto-upgrade.sh's `up -d --no-deps worker gateway`;
# scripts/ops/agnes-state-applier.sh's `up -d --no-deps --force-recreate
# app scheduler`). Without it, Compose re-runs api1's `depends_on` chain —
# including the `data-migrate` one-shot (docker-compose.postgres.yml), a
# ONE-TIME DuckDB->PG cutover that is deliberately not idempotent: on this
# second run it reads an already-migrated Postgres and exits 1, aborting
# the smoke. Production never re-runs it precisely because those recreate
# paths pass --no-deps; the harness must match.
"${COMPOSE[@]}" up -d --no-deps api1
for i in $(seq 1 60); do
  curl -fsS localhost:8080/readyz >/dev/null 2>&1 && break
  sleep 2
done

# --- 3. FLUSHALL chaos ----------------------------------------------------
# app/coordination/leases.py's module docstring calls this out explicitly
# ("FLUSHALL story"): losing all coordination state must never crash a
# replica or wedge the LB — at worst a lease/rate-limit bucket resets
# early, and the next acquire/increment starts clean. Drive real traffic
# (mixing a plain liveness probe with the redis-backed /auth/token path)
# straight through the wipe and verify that story holds.
echo "FLUSHALL chaos: wiping redis under traffic..."
flushall_ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
loop_fails=0
for i in $(seq 1 20); do
  curl -fsS -m 2 localhost:8080/healthz >/dev/null 2>&1 || loop_fails=$((loop_fails+1))
  hit_auth_token
  if [ "$i" -eq 5 ]; then
    "${COMPOSE[@]}" exec -T redis redis-cli FLUSHALL >/dev/null
  fi
  sleep 0.5
done
[ "$loop_fails" -le 2 ] || { echo "FAIL: $loop_fails/20 healthz requests failed across the FLUSHALL"; exit 1; }
echo "FLUSHALL traffic-continuity OK (failures: $loop_fails/20)"

echo "waiting for healthz/readyz to be green post-FLUSHALL..."
recovered=0
for i in $(seq 1 15); do
  if curl -fsS -m 2 localhost:8080/healthz 2>/dev/null | grep -q alive && readyz_is_ready; then
    recovered=1
    break
  fi
  sleep 2
done
[ "$recovered" -eq 1 ] || { echo "FAIL: healthz/readyz did not recover green within 30s of FLUSHALL"; exit 1; }
echo "post-FLUSHALL healthz/readyz OK"

echo "checking api1/api2/gateway/worker logs for new tracebacks since FLUSHALL..."
tb_count=$("${COMPOSE[@]}" logs --since "$flushall_ts" api1 api2 gateway worker 2>/dev/null \
  | grep -c "Traceback (most recent call last)" || true)
if [ "${tb_count:-0}" -ne 0 ]; then
  echo "FAIL: $tb_count traceback(s) in api1/api2/gateway/worker logs since the FLUSHALL"
  "${COMPOSE[@]}" logs --since "$flushall_ts" api1 api2 gateway worker 2>/dev/null | grep -A20 "Traceback (most recent call last)" | head -100
  exit 1
fi
echo "no new tracebacks after FLUSHALL OK"

echo "verifying a fresh coordination write lands in redis after FLUSHALL..."
hit_auth_token
hit_auth_token
fresh_ok=0
for i in $(seq 1 15); do
  fresh_count=$("${COMPOSE[@]}" exec -T redis redis-cli --scan --pattern 'LIMITS:*' 2>/dev/null | wc -l | tr -d ' ' || true)
  if [ "${fresh_count:-0}" -gt 0 ]; then fresh_ok=1; break; fi
  sleep 1
done
[ "$fresh_ok" -eq 1 ] || { echo "FAIL: no fresh LIMITS:* key reappeared in redis after FLUSHALL"; exit 1; }
echo "post-FLUSHALL coordination write OK (redis repopulated within ${i}s; keys=$fresh_count)"

# --- 4. Chat/gateway-role WS-ticket + restart continuity (infra path) ----
# Full chat.enabled=true live-session coverage (spawn a runner, exchange
# turns, observe the reconnect replay/full_refresh control frame — wave-2F
# tasks 1-5, see docs/architecture.md and docs/DEPLOYMENT.md's chat HA
# sections) needs a real ANTHROPIC_API_KEY plus a provider whose backing
# is up (the kai-agent engine sidecar + KAI_HOST_JWT_SECRET, or the
# apps-runner sidecar + a built agnes-chat-sandbox image), plus an
# authenticated user with a chat-RBAC grant — none of which this harness
# configures (config/instance.mtier.yaml has no `chat:` section at all,
# same reason section 2's comment above gives for omitting the sweep-lease
# check). There is deliberately no mock chat provider to substitute
# (app/main.py refuses unknown providers outright; the closest stand-in,
# the kai-agent stub engine of docs/kai-agent-local-dev.md, is another
# sidecar this harness doesn't run — and the docker provider is
# single-gateway, unsupported on this multi-gateway topology anyway). So
# rather than a live session, this section drives the real running route handler
# (app/api/chat.py::ws_stream) to prove the cross-replica INFRA a live
# session rides on:
#
#   1. Ticket mint+consume: mint a `ws-ticket:*` key directly in redis
#      (see the helpers above) and confirm a WS handshake against
#      `gateway`, issued from a DIFFERENT container (api1), reaches
#      `ws.accept()` (HTTP 101) — a genuinely cross-replica ticket consume
#      over the shared redis coordination backend.
#   2. Single-use semantics: the same (now-deleted) ticket must NOT reach
#      101 a second time.
#   3. Gateway-role kill/restart continuity: kill the `gateway` container,
#      restart it, and confirm a freshly-minted ticket's WS handshake
#      succeeds against it again — the container-level recovery half of
#      the claim-then-respawn takeover story.
#
# Deliberately NOT covered here (needs a live spawned sandbox): the
# reconnect-with-last_seq replay / full_refresh control frame itself, and
# the claim-then-respawn takeover logic in ChatManager. Also note: Caddy's
# example config for this harness (deploy/caddy/Caddyfile.mtier) now
# implements the reference LB rule — a matcher routes /api/chat/*,
# /api/notifications/ws, and /api/slack/* to the `gateway` upstream ahead
# of the api1/api2 catch-all (see docs/DEPLOYMENT.md's chat HA section,
# "Load-balancer routing rule"). This section still exercises reachability
# via the compose-internal network from a peer container (api1->gateway),
# not literally through :8080 — the WS-ticket handshake needs a
# container-issued ticket either way, and the cross-container hop is the
# property under test.
echo "checking chat WS ticket mint/consume + gateway-role restart continuity..."
token1=$(openssl rand -hex 20)
mint_ws_ticket "$token1"
[ "$(ws_handshake_101 "$token1")" = "1" ] || { echo "FAIL: WS handshake did not reach 101 with a freshly-minted ticket"; exit 1; }
echo "chat WS ticket-consume + gateway accept OK (101 observed, cross-container via api1->gateway)"

if ticket_still_present "$token1"; then
  echo "FAIL: ws-ticket:${token1} still present in redis after a WS connect attempt (not consumed)"
  exit 1
fi
echo "ticket single-use OK (consumed from redis on first use)"

if [ "$(ws_handshake_101 "$token1")" = "1" ]; then
  echo "FAIL: a consumed ticket was accepted a second time"
  exit 1
fi
echo "ticket replay-rejection OK (second use did not reach 101)"

echo "killing gateway to check role-restart continuity..."
"${COMPOSE[@]}" kill gateway
# --no-deps: same reason as the api1 restart above — a role-container
# recreate must not re-trigger the one-time data-migrate cutover.
"${COMPOSE[@]}" up -d --no-deps gateway
gw_ready=0
for i in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T gateway curl -fsS -m 2 localhost:8000/readyz 2>/dev/null | grep -Eq '"status":[[:space:]]*"ready"' \
    && { gw_ready=1; break; }
  sleep 2
done
[ "$gw_ready" -eq 1 ] || { echo "FAIL: gateway did not become ready again after restart"; exit 1; }

token2=$(openssl rand -hex 20)
mint_ws_ticket "$token2"
[ "$(ws_handshake_101 "$token2")" = "1" ] || { echo "FAIL: WS handshake did not reach 101 against the restarted gateway"; exit 1; }
echo "chat WS reconnect-after-gateway-restart OK (101 observed against the restarted gateway)"

echo "MTIER SMOKE OK (kill failures: $fails/20, flushall failures: $loop_fails/20, post-flushall tracebacks: ${tb_count:-0}, chat WS infra: ok, ducklake: ok)"
