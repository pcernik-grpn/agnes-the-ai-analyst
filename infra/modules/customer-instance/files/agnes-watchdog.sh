#!/bin/bash
# Agnes host-side watchdog — checks the container fleet every 5 minutes for
# the failure signatures of known production incidents:
#
#   - DuckDB FatalException crash loops ("terminate called" — the process
#     aborts from a worker thread, so no Python traceback exists)
#   - the invalidated-database "zombie" state ("database has been
#     invalidated" — the app still answers /api/health 200 while every
#     write returns 500, invisible to uptime checks)
#   - WAL salvage events ("WAL replay failed" + new *.wal.discarded.* files
#     — each one is a window of lost writes)
#   - ART index desync symptoms ("Failed to delete all rows from index",
#     "Failed to append to PRIMARY_*")
#   - container restart bursts, cgroup OOM kills, scheduler HTTP-500
#     streaks, /data disk pressure, dead health endpoint
#   - a redis-backed coordination backend going unreachable ("Coordination
#     Unavailable" repeated in a role container's logs — see below)
#
# Role-container aware: rather than a single hardcoded app container, this
# scans every "role" container the host is actually running — just `app`
# on a standalone/S-tier deployment, or the full `worker`/`gateway`/
# `api1`/`api2`/... fleet under the role-split (m-tier) overlay
# (docker-compose.mtier.yml). All of these run the identical Agnes uvicorn
# image (see that file — they only differ by an AGNES_ROLE env var), so the
# same incident signatures apply to each verbatim; every alert names the
# container it came from. `docker compose ps` resolves whatever
# COMPOSE_FILE/COMPOSE_PROFILES this host runs (same env agnes-auto-
# upgrade.sh reads), so a topology change (e.g. adding an api3 replica)
# needs no change here.
#
# Besides incident alerts it also reports two informational deployment-
# timeline events (prefixed ' i ' in the message body): an app image
# change (auto-upgrade recreated the container) and a DB schema-version
# change (startup self-migration or a manual alembic run), both tracked
# as run-to-run deltas in the state dir, off a single reference container
# (the first role container found — a one-shot fleet-wide event doesn't
# need reporting once per replica).
#
# Alerts go to journald (`logger -t agnes-watchdog`) and
# /var/log/agnes-watchdog.log, plus an optional webhook (Slack / Google
# Chat compatible: POST {"text": "..."}). Configure in
# /etc/agnes-watchdog.env:
#
#   WEBHOOK_URL="https://..."   # empty = log-only
#   ENV_STAGE="prod"            # written by provisioning; drives the label
#   ENV_LABEL="..."             # optional full override of the label
#
# Runs as root via agnes-watchdog.timer. Deliberately independent of the
# app: its job is to report states in which the app can no longer report
# on itself.
set -u

STATE=/var/lib/agnes-watchdog
mkdir -p "$STATE"
[ -f /etc/agnes-watchdog.env ] && . /etc/agnes-watchdog.env
WEBHOOK_URL="${WEBHOOK_URL:-}"
HOST=$(hostname)

# Compose project directory — `docker compose ps`/`config` below must
# resolve the same COMPOSE_FILE/COMPOSE_PROFILES agnes-auto-upgrade.sh
# reads from /opt/agnes/.env, so `cd` there first (mirrors that script).
# Overridable only for the bash-harness unit test
# (tests/test_watchdog_role_containers.sh); production always uses
# /opt/agnes.
COMPOSE_DIR="${AGNES_WATCHDOG_COMPOSE_DIR:-/opt/agnes}"
cd "$COMPOSE_DIR" 2>/dev/null || true

# Environment label precedence: explicit ENV_LABEL > ENV_STAGE (written by
# provisioning, e.g. the Terraform module's per-VM role) >
# POSTHOG_ENVIRONMENT from /opt/agnes/.env (deployments that set it) >
# hostname only.
STAGE="${ENV_STAGE:-}"
if [ -z "$STAGE" ]; then
    STAGE=$(grep -E '^POSTHOG_ENVIRONMENT=' /opt/agnes/.env 2>/dev/null | cut -d= -f2 | tr -d '"')
fi
case "$STAGE" in
  prod*) EMOJI=$(printf '\xf0\x9f\x94\xb4');;
  dev*)  EMOJI=$(printf '\xf0\x9f\x9f\xa1');;
  *)     EMOJI=$(printf '\xe2\x9a\xaa'); STAGE="${STAGE:-unknown}";;
esac
LABEL="${ENV_LABEL:-$EMOJI ${STAGE^^} ($HOST)}"

NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SINCE=$(cat "$STATE/last_run" 2>/dev/null || date -u -d '-5 min' +%Y-%m-%dT%H:%M:%SZ)
echo "$NOW" > "$STATE/last_run"

ALERTS=()
add() { ALERTS+=("$1"); }
# Informational deployment-timeline events (image upgrade, DB schema bump).
# Separate channel from ALERTS: they ride along with alerts or go out on
# their own, but never trip the hourly alert-type anti-spam (each is
# one-shot by construction — the underlying value changed).
INFOS=()
info() { INFOS+=("$1"); }

# --- Role-container enumeration -----------------------------------------
# ROLE_CONTAINER_RE matches the compose *services* that run the Agnes
# uvicorn app image and can therefore emit its DuckDB/WAL/health/
# coordination signatures: `app` (standalone/S-tier), or
# `worker`/`gateway`/`api<N>` under the role-split (m-tier) overlay. It
# deliberately excludes sidecars (postgres, redis, caddy*, prometheus,
# cadvisor) and one-shot jobs (migrate, data-migrate, duckdb-seed) — none
# of those run app code, so the incident signatures below would never
# match their logs anyway, and scanning them would just add noise to the
# transcript for nothing.
ROLE_CONTAINER_RE='^(app|worker|gateway|api[0-9]+|extraction-worker)$'
list_role_containers() {
    local out
    out=$(docker compose ps --format '{{.Service}} {{.Name}}' 2>/dev/null)
    if [ -n "$out" ]; then
        awk -v re="$ROLE_CONTAINER_RE" '$1 ~ re {print $2}' <<<"$out"
        return 0
    fi
    # Fallback: docker compose unresolvable from this cwd (e.g. a bare,
    # non-compose install, or /opt/agnes missing) — fall back to the
    # legacy hardcoded container name so an unusual host never regresses
    # to "nothing monitored". Single-container topology's normal path is
    # the `docker compose ps` branch above; this only fires when that
    # itself can't run.
    if docker inspect agnes-app-1 >/dev/null 2>&1; then
        echo agnes-app-1
    fi
}

CONTAINERS=()
while IFS= read -r ctr; do
    [ -n "$ctr" ] && CONTAINERS+=("$ctr")
done < <(list_role_containers)

if [ "${#CONTAINERS[@]}" -eq 0 ]; then
    add "CONTAINER: no agnes role containers found (docker compose ps returned none)"
fi

# --- Coordination backend configured? -----------------------------------
# Redis coordination is declared via instance.yaml::coordination.backend
# (config/instance.mtier.yaml ships `backend: redis` for the m-tier
# profile — see app/coordination/factory.py) OR via the
# AGNES_COORDINATION_BACKEND env override in .env (env wins over yaml in
# that same factory; the customer-instance module's extraction-lane opt-in
# writes the env form so it never touches the applier-owned instance.yaml).
# Grepped host-side across every mounted instance.yaml variant plus .env,
# same host-read-not-container-shell style as agnes-db-backup.sh's
# PERSISTED_BACKEND check.
REDIS_CONFIGURED=0
if grep -rlq 'backend:[[:space:]]*redis' config/instance*.yaml 2>/dev/null; then
    REDIS_CONFIGURED=1
elif grep -q '^AGNES_COORDINATION_BACKEND=redis$' .env 2>/dev/null; then
    REDIS_CONFIGURED=1
fi

# --- Per-container incident-signature scan ------------------------------
# scan_container: applies every log-grep + RestartCount/oom_kill/health
# signature to one container, naming it in every alert. These are the
# exact signatures the watchdog has always checked — just parameterized
# over $ctr instead of hardcoded to a single `agnes-app-1`.
scan_container() {
    local ctr=$1
    local logs c rc prev_rc cid ook prev_ook health_body

    logs=$(docker logs "$ctr" --since "$SINCE" 2>&1)

    c=$(grep -c "terminate called" <<<"$logs")
    [ "$c" -gt 0 ] && add "CRASH[$ctr]: ${c}x 'terminate called' (DuckDB FatalException) since $SINCE"
    c=$(grep -c "database has been invalidated" <<<"$logs")
    [ "$c" -gt 0 ] && add "ZOMBIE[$ctr]: ${c}x 'database has been invalidated' — writes failing while app looks healthy"
    c=$(grep -c "WAL replay failed" <<<"$logs")
    [ "$c" -gt 0 ] && add "WAL-SALVAGE[$ctr]: ${c}x 'WAL replay failed' — possible data-loss window"
    c=$(grep -c "Failed to delete all rows from index" <<<"$logs")
    [ "$c" -gt 0 ] && add "INDEX-DESYNC[$ctr]: ${c}x 'Failed to delete all rows from index'"
    c=$(grep -c "Failed to append to PRIMARY_" <<<"$logs")
    [ "$c" -gt 0 ] && add "INDEX-APPEND-FATAL[$ctr]: ${c}x 'Failed to append to PRIMARY_*'"

    # Coordination-backend unreachable (new — wave 2E task 4). Only
    # meaningful when redis coordination is actually configured; the
    # zero-config `memory` default backend never raises this. A single
    # blip is tolerated by the lease loop itself for up to one lease
    # ttl_s (app/coordination/leases.py) and recovers on its own, so —
    # same "don't fire on one transient occurrence" idiom as the
    # SCHEDULER HTTP-500 streak check further down, not the single-
    # occurrence DuckDB signatures above — require a handful of hits in
    # one 5-minute scan window before alerting.
    if [ "$REDIS_CONFIGURED" -eq 1 ]; then
        c=$(grep -c "CoordinationUnavailable" <<<"$logs")
        [ "$c" -ge 3 ] && add "COORDINATION[$ctr]: ${c}x 'CoordinationUnavailable' since $SINCE — redis coordination backend unreachable"
    fi

    rc=$(docker inspect "$ctr" --format '{{.RestartCount}}' 2>/dev/null || echo "")
    if [ -n "$rc" ]; then
        prev_rc=$(cat "$STATE/rc.$ctr" 2>/dev/null || echo "$rc")
        echo "$rc" > "$STATE/rc.$ctr"
        [ "$rc" -gt "$prev_rc" ] && add "RESTARTS[$ctr]: container RestartCount $prev_rc -> $rc"
    else
        add "CONTAINER: $ctr not inspectable (down?)"
        return 0
    fi

    cid=$(docker inspect "$ctr" --format '{{.Id}}' 2>/dev/null || echo "")
    if [ -n "$cid" ] && [ -f "/sys/fs/cgroup/system.slice/docker-$cid.scope/memory.events" ]; then
        ook=$(awk '/^oom_kill /{print $2}' "/sys/fs/cgroup/system.slice/docker-$cid.scope/memory.events")
        prev_ook=$(cat "$STATE/oomk.$ctr" 2>/dev/null || echo "$ook")
        echo "$ook" > "$STATE/oomk.$ctr"
        [ "$ook" -gt "$prev_ook" ] && add "OOM[$ctr]: oom_kill counter $prev_ook -> $ook"
    fi

    health_body=$(docker exec "$ctr" curl -sf -m 10 http://localhost:8000/api/health 2>/dev/null || echo "")
    [ -z "$health_body" ] && add "HEALTH[$ctr]: /api/health not returning 200"
}

for ctr in "${CONTAINERS[@]}"; do
    scan_container "$ctr"
done

newdisc=$(find /data/state -maxdepth 1 -name "*.wal.discarded.*" -newermt "$SINCE" 2>/dev/null)
[ -n "$newdisc" ] && add "NEW DISCARDED WAL: $newdisc"

# Deployment timeline: report an app image change (auto-upgrade recreated
# the container with a new build) and a DB schema-version change, tracked
# off a single reference container — the first role container found (a
# fleet-wide upgrade/migration is one event, not one per replica). First
# run seeds state silently. Skipped entirely when no role container is up.
REF_CTR="${CONTAINERS[0]:-}"
if [ -n "$REF_CTR" ]; then
    img=$(docker inspect "$REF_CTR" --format '{{.Image}}' 2>/dev/null || echo "")
    if [ -n "$img" ]; then
        prev_img=$(cat "$STATE/image" 2>/dev/null || echo "")
        echo "$img" > "$STATE/image"
        if [ -n "$prev_img" ] && [ "$img" != "$prev_img" ]; then
            # Best-effort version context from the boot banner in the log window.
            ver=$(docker logs "$REF_CTR" --since "$SINCE" 2>&1 \
                | grep -oE 'Agnes [0-9][0-9.]* \| channel: [a-z-]* \| schema v[0-9]*' | tail -1)
            info "UPGRADE: $REF_CTR image ${prev_img#sha256:} -> ${img#sha256:}${ver:+ ($ver)}"
        fi
    fi

    health_body=$(docker exec "$REF_CTR" curl -sf -m 10 http://localhost:8000/api/health 2>/dev/null || echo "")
    if [ -n "$health_body" ]; then
        # Deployment timeline: report a DB schema-version change (startup
        # self-migration / manual alembic run) from the health body the
        # liveness probe already fetched — no extra DB access. First run
        # seeds state silently.
        schema=$(grep -o '"current":[0-9]*' <<<"$health_body" | head -1 | tr -dc 0-9)
        if [ -n "$schema" ]; then
            prev_schema=$(cat "$STATE/schema" 2>/dev/null || echo "")
            echo "$schema" > "$STATE/schema"
            if [ -n "$prev_schema" ] && [ "$schema" != "$prev_schema" ]; then
                info "DB: schema v$prev_schema -> v$schema"
            fi
        fi
    fi
fi

s500=$(docker logs agnes-scheduler-1 --since "$SINCE" 2>&1 | grep -c "HTTP 500")
[ "$s500" -ge 3 ] && add "SCHEDULER: ${s500} job calls returned HTTP 500 since $SINCE"

duse=$(df --output=pcent /data 2>/dev/null | tail -1 | tr -dc 0-9)
[ -n "$duse" ] && [ "$duse" -ge 85 ] && add "DISK: /data at ${duse}%"

[ "${#ALERTS[@]}" -eq 0 ] && [ "${#INFOS[@]}" -eq 0 ] && exit 0

BODY=""
[ "${#ALERTS[@]}" -gt 0 ] && BODY="$(printf ' - %s\n' "${ALERTS[@]}")"
if [ "${#INFOS[@]}" -gt 0 ]; then
    [ -n "$BODY" ] && BODY="$BODY
"
    BODY="$BODY$(printf ' i %s\n' "${INFOS[@]}")"
fi
MSG="[agnes-watchdog] $LABEL | $NOW
$BODY"
logger -t agnes-watchdog "$MSG"
echo "$MSG" >> /var/log/agnes-watchdog.log

# Anti-spam: the same set of alert TYPES repeats on the webhook at most
# hourly (the journal + log file still record every occurrence). Hash only
# the type prefix of each alert (text before the first colon), one per
# line — the message bodies embed per-run counts and the $SINCE timestamp,
# which would make every hash unique and the suppression a no-op; and
# joining the set into one line would truncate it to the first prefix and
# over-suppress distinct alert sets. The type prefix now embeds the
# container name (e.g. "CRASH[agnes-worker-1]"), so a fleet's alerts
# suppress independently per container instead of one container's repeat
# silencing another's fresh incident.
# Info lines bypass the suppression entirely: a run carrying any info is
# always sent (the info is one-shot), and an info-only run leaves the
# alert hash/time untouched so it cannot reset an active suppression.
if [ "${#ALERTS[@]}" -gt 0 ]; then
    H=$(printf '%s\n' "${ALERTS[@]}" | sed 's/:.*//' | md5sum | cut -d' ' -f1)
    LAST_H=$(cat "$STATE/alert_hash" 2>/dev/null || echo "")
    LAST_T=$(cat "$STATE/alert_time" 2>/dev/null || echo 0)
    NOW_E=$(date +%s)
    if [ "$H" = "$LAST_H" ] && [ $((NOW_E - LAST_T)) -lt 3600 ] && [ "${#INFOS[@]}" -eq 0 ]; then
        exit 0
    fi
    echo "$H" > "$STATE/alert_hash"; echo "$NOW_E" > "$STATE/alert_time"
fi

if [ -n "$WEBHOOK_URL" ]; then
    esc=$(printf '%s' "$MSG" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')
    curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"text\": \"$esc\"}" "$WEBHOOK_URL" >/dev/null 2>&1 \
        || logger -t agnes-watchdog "webhook send failed"
fi
exit 0
