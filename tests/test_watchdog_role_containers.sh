#!/usr/bin/env bash
# Integration test for the role-container fleet awareness added to
# infra/modules/customer-instance/files/agnes-watchdog.sh (wave 2E task 4):
# the watchdog used to hardcode a single `agnes-app-1` container; it now
# enumerates every "role" container the host is actually running (via
# `docker compose ps`) and applies the existing incident signatures to
# EACH, naming the container in every alert, plus a new low-noise
# coordination-backend-unreachable signature.
#
# Stubs `docker` with a fake on PATH that records every invocation to a
# transcript file and serves canned `docker compose ps` / `docker logs` /
# `docker inspect` / `docker exec ... curl .../api/health` output, then
# drives the script through five scenarios:
#
#   A. Single-container topology — `docker compose ps` reports only `app`.
#      The one incident-bearing container is scanned and named in its
#      alert exactly like before the refactor.
#   B. Role-split (m-tier) fleet — worker/gateway/api1/api2 are ALL
#      scanned and named individually (including containers with no
#      incidents, proving the loop doesn't stop at the first hit), while
#      sidecars reported by the same `docker compose ps` call (redis,
#      postgres) are filtered out entirely — never even `docker logs`'d.
#   C. Coordination-backend-unreachable, redis configured — a container
#      logging 5x `CoordinationUnavailable` alerts (named); a sibling
#      container logging only 1x (below the low-noise threshold) does not.
#   D. Coordination-backend-unreachable, redis NOT configured — the same
#      5x `CoordinationUnavailable` log lines never alert at all (the
#      zero-config `memory` backend never raises this, so the signature
#      must stay gated on redis actually being configured).
#   E. Fallback path — `docker compose ps` reports nothing (e.g. cwd isn't
#      a resolvable compose project), but `agnes-app-1` is inspectable
#      directly: the legacy hardcoded name still gets scanned so an
#      unusual host never regresses to "nothing monitored".
#
# Run with: bash tests/test_watchdog_role_containers.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
script=$repo_root/infra/modules/customer-instance/files/agnes-watchdog.sh

fail() {
    echo "FAIL: $*"
    echo "--- transcript ---"
    cat "$transcript" 2>/dev/null || true
    echo "--- log file ---"
    cat "$tmp/var/log/agnes-watchdog.log" 2>/dev/null || true
    exit 1
}

# --- Shared fake `docker` builder ---------------------------------------
# Env read by the fake (set per-scenario before invoking the script):
#   FAKE_TOPOLOGY=single|role_split|empty   shape of `docker compose ps`
#   FAKE_API_LINES                          extra "<service> <name>" lines
#                                            appended for role_split
#   FAKE_EXISTING_CONTAINERS                space-separated names that a
#                                            no-args `docker inspect <n>`
#                                            existence probe succeeds for
#                                            (fallback-path scenario E)
#   FAKE_LOGS_DIR                           dir of "<container>.log" files
#                                            fed back verbatim by `docker
#                                            logs <container> --since ...`
#                                            (missing file = empty logs)
build_fake_docker() {
    local dir=$1
    mkdir -p "$dir"
    cat > "$dir/docker" <<'FAKE'
#!/usr/bin/env bash
echo "docker $*" >> "$TRANSCRIPT"

if [ "${1:-}" = "compose" ]; then
    shift
    sub="${1:-}"
    shift || true
    case "$sub" in
        ps)
            case "${FAKE_TOPOLOGY:-single}" in
                role_split)
                    printf 'worker agnes-worker-1\n'
                    printf 'gateway agnes-gateway-1\n'
                    printf '%s\n' "${FAKE_API_LINES:-}"
                    printf 'redis agnes-redis-1\n'
                    printf 'postgres agnes-postgres-1\n'
                    ;;
                empty)
                    :
                    ;;
                extraction)
                    printf 'app agnes-app-1\n'
                    printf 'scheduler agnes-scheduler-1\n'
                    printf 'extraction-worker agnes-extraction-worker-1\n'
                    printf 'redis agnes-redis-1\n'
                    ;;
                *)
                    printf 'app agnes-app-1\n'
                    printf 'scheduler agnes-scheduler-1\n'
                    ;;
            esac
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi

case "${1:-}" in
    logs)
        shift
        ctr="$1"
        if [ -f "$FAKE_LOGS_DIR/$ctr.log" ]; then
            cat "$FAKE_LOGS_DIR/$ctr.log"
        fi
        exit 0
        ;;
    inspect)
        shift
        ctr="$1"; shift || true
        if [ "${1:-}" = "--format" ]; then
            fmt="$2"
            case "$fmt" in
                *RestartCount*) echo "0" ;;
                *Image*) echo "sha256:img-$ctr" ;;
                *Id*) echo "cid-$ctr" ;;
                *) echo "" ;;
            esac
            exit 0
        fi
        # No --format: existence probe used only by the fallback path.
        case " ${FAKE_EXISTING_CONTAINERS:-} " in
            *" $ctr "*) exit 0 ;;
            *) exit 1 ;;
        esac
        ;;
    exec)
        # docker exec <ctr> curl -sf -m 10 http://localhost:8000/api/health
        echo '{"status": "ok", "schema": {"current": 42}}'
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
FAKE
    chmod +x "$dir/docker"

    # logger: no-op, just record.
    cat > "$dir/logger" <<'FAKE'
#!/usr/bin/env bash
shift  # -t
shift  # tag
echo "logger: $*" >> "$TRANSCRIPT"
FAKE
    chmod +x "$dir/logger"
}

# --- Sandbox builder ------------------------------------------------------
# Same sed-a-copy technique as tests/test_db_backup_pg_canary.sh /
# tests/test_state_applier_host_script.sh: patch the absolute host paths
# the script hardcodes onto sandbox-local ones. COMPOSE_DIR and STATE are
# both overridable/sandboxed so no part of this test touches the real
# /opt/agnes, /var/lib, or /var/log.
make_sandboxed_script() {
    local tmp=$1
    local sandboxed=$tmp/agnes-watchdog.sh
    sed \
        -e "s|STATE=/var/lib/agnes-watchdog|STATE=$tmp/var/lib/agnes-watchdog|" \
        -e "s|/etc/agnes-watchdog.env|$tmp/etc/agnes-watchdog.env|g" \
        -e "s|>> /var/log/agnes-watchdog.log|>> $tmp/var/log/agnes-watchdog.log|" \
        "$script" > "$sandboxed"
    chmod +x "$sandboxed"
    echo "$sandboxed"
}

# Args: tmp_dir, env_stage
write_watchdog_env() {
    local dir=$1 stage=$2
    mkdir -p "$(dirname "$dir/etc/agnes-watchdog.env")"
    mkdir -p "$dir/etc"
    {
        echo "WEBHOOK_URL="
        echo "ENV_STAGE=$stage"
    } > "$dir/etc/agnes-watchdog.env"
}

run_scenario() {
    local name=$1
    tmp=$(mktemp -d)
    transcript=$tmp/transcript.log
    fake_logs_dir=$tmp/fake_logs
    mkdir -p "$fake_logs_dir" "$tmp/var/lib/agnes-watchdog" "$tmp/var/log"
    : > "$transcript"

    sandboxed=$(make_sandboxed_script "$tmp")
    fake_bin=$tmp/bin
    build_fake_docker "$fake_bin"
    write_watchdog_env "$tmp" "dev"

    # Pre-seed last_run so $SINCE comes from the state file, never from
    # `date -u -d '-5 min' ...` — that GNU-date flag isn't available on a
    # macOS dev laptop's BSD `date`, and production always has this file
    # populated after the very first tick anyway.
    echo "2026-01-01T00:00:00Z" > "$tmp/var/lib/agnes-watchdog/last_run"

    echo "--- scenario $name ---"
}

log_file() { cat "$tmp/var/log/agnes-watchdog.log" 2>/dev/null || true; }

# =====================================================================
# Scenario A: single-container topology — the one container is scanned
# and named in its alert, exactly like before the refactor.
# =====================================================================
run_scenario A
printf "terminate called\nterminate called\n" > "$fake_logs_dir/agnes-app-1.log"

TRANSCRIPT="$transcript" FAKE_LOGS_DIR="$fake_logs_dir" \
    FAKE_TOPOLOGY=single \
    AGNES_WATCHDOG_COMPOSE_DIR="$tmp" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

grep -qF "docker logs agnes-app-1 --since" "$transcript" \
    || fail "A: the single app container must be scanned"
log_file | grep -qF "CRASH[agnes-app-1]: 2x 'terminate called'" \
    || fail "A: crash-loop alert must name the container"
echo "OK: A — single-container topology still scans and names the one app container"
rm -rf "$tmp"

# =====================================================================
# Scenario B: role-split fleet — every role container is scanned and
# named individually (including containers with no incidents); sidecars
# (redis, postgres) reported by the same `docker compose ps` call are
# filtered out and never even `docker logs`'d.
# =====================================================================
run_scenario B
printf "terminate called\nterminate called\n" > "$fake_logs_dir/agnes-worker-1.log"
printf "database has been invalidated\n" > "$fake_logs_dir/agnes-api2-1.log"

TRANSCRIPT="$transcript" FAKE_LOGS_DIR="$fake_logs_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_LINES=$'api1 agnes-api1-1\napi2 agnes-api2-1' \
    AGNES_WATCHDOG_COMPOSE_DIR="$tmp" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

for ctr in agnes-worker-1 agnes-gateway-1 agnes-api1-1 agnes-api2-1; do
    grep -qF "docker logs $ctr --since" "$transcript" \
        || fail "B: $ctr must be scanned (role container)"
done
grep -qF "docker logs agnes-redis-1" "$transcript" \
    && fail "B: redis is not a role container and must never be docker-logs'd"
grep -qF "docker logs agnes-postgres-1" "$transcript" \
    && fail "B: postgres is not a role container and must never be docker-logs'd"

log_file | grep -qF "CRASH[agnes-worker-1]: 2x 'terminate called'" \
    || fail "B: worker's crash-loop alert must be named"
log_file | grep -qF "ZOMBIE[agnes-api2-1]: 1x 'database has been invalidated'" \
    || fail "B: api2's zombie-db alert must be named"
log_file | grep -qF "CRASH[agnes-gateway-1]" \
    && fail "B: gateway had no incident — must not alert"
log_file | grep -qF "CRASH[agnes-api1-1]" \
    && fail "B: api1 had no incident — must not alert"
echo "OK: B — every role container scanned+named individually; sidecars filtered out"
rm -rf "$tmp"

# =====================================================================
# Scenario C: coordination-backend-unreachable, redis configured — a
# container logging 5x CoordinationUnavailable alerts (named); a sibling
# logging only 1x (below the low-noise threshold) does not.
# =====================================================================
run_scenario C
mkdir -p "$tmp/config"
printf 'coordination:\n  backend: redis\n' > "$tmp/config/instance.mtier.yaml"
printf 'CoordinationUnavailable\n%.0s' {1..5} > "$fake_logs_dir/agnes-api1-1.log"
printf "CoordinationUnavailable\n" > "$fake_logs_dir/agnes-gateway-1.log"

TRANSCRIPT="$transcript" FAKE_LOGS_DIR="$fake_logs_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_LINES=$'api1 agnes-api1-1\napi2 agnes-api2-1' \
    AGNES_WATCHDOG_COMPOSE_DIR="$tmp" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

log_file | grep -qF "COORDINATION[agnes-api1-1]: 5x 'CoordinationUnavailable'" \
    || fail "C: 5x CoordinationUnavailable with redis configured must alert, named"
log_file | grep -qF "COORDINATION[agnes-gateway-1]" \
    && fail "C: a single CoordinationUnavailable hit is below the low-noise threshold — must not alert"
echo "OK: C — coordination-backend alert fires past the low-noise threshold, named; a single blip does not"
rm -rf "$tmp"

# =====================================================================
# Scenario D: coordination-backend-unreachable, redis NOT configured —
# the same 5x CoordinationUnavailable never alerts (signature must stay
# gated on redis actually being configured; the zero-config `memory`
# backend never raises this in the first place).
# =====================================================================
run_scenario D
printf 'CoordinationUnavailable\n%.0s' {1..5} > "$fake_logs_dir/agnes-app-1.log"

TRANSCRIPT="$transcript" FAKE_LOGS_DIR="$fake_logs_dir" \
    FAKE_TOPOLOGY=single \
    AGNES_WATCHDOG_COMPOSE_DIR="$tmp" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

log_file | grep -qF "COORDINATION[" \
    && fail "D: without redis configured, CoordinationUnavailable must never alert"
echo "OK: D — coordination-backend signature stays gated on redis actually being configured"
rm -rf "$tmp"

# =====================================================================
# Scenario E: fallback path — `docker compose ps` reports nothing, but
# `agnes-app-1` is directly inspectable: the legacy hardcoded name still
# gets scanned so an unusual host never regresses to "nothing monitored".
# =====================================================================
run_scenario E
printf "terminate called\n" > "$fake_logs_dir/agnes-app-1.log"

TRANSCRIPT="$transcript" FAKE_LOGS_DIR="$fake_logs_dir" \
    FAKE_TOPOLOGY=empty \
    FAKE_EXISTING_CONTAINERS="agnes-app-1" \
    AGNES_WATCHDOG_COMPOSE_DIR="$tmp" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

grep -qF "docker inspect agnes-app-1" "$transcript" \
    || fail "E: the fallback existence probe must be attempted"
grep -qF "docker logs agnes-app-1 --since" "$transcript" \
    || fail "E: the fallback-discovered container must still be scanned"
log_file | grep -qF "CRASH[agnes-app-1]: 1x 'terminate called'" \
    || fail "E: the fallback-discovered container's alert must be named"
echo "OK: E — fallback path still finds and scans agnes-app-1 when compose ps yields nothing"
rm -rf "$tmp"

# =====================================================================
# Scenario F: extraction-lane topology (customer-instance module opt-in) —
# `extraction-worker` is a role container (it runs the app image with
# AGNES_ROLE=worker) so it is scanned and named; redis coordination is
# declared via the .env AGNES_COORDINATION_BACKEND override rather than any
# instance*.yaml (the module writes the env form so it never touches the
# applier-owned instance.yaml), and the coordination signature must fire
# off that declaration alone. The redis sidecar itself stays filtered out.
# =====================================================================
run_scenario F
printf 'AGNES_COORDINATION_BACKEND=redis\n' > "$tmp/.env"
printf 'CoordinationUnavailable\n%.0s' {1..5} > "$fake_logs_dir/agnes-extraction-worker-1.log"

TRANSCRIPT="$transcript" FAKE_LOGS_DIR="$fake_logs_dir" \
    FAKE_TOPOLOGY=extraction \
    AGNES_WATCHDOG_COMPOSE_DIR="$tmp" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed"

grep -qF "docker logs agnes-extraction-worker-1 --since" "$transcript" \
    || fail "F: extraction-worker must be scanned (role container)"
grep -qF "docker logs agnes-redis-1" "$transcript" \
    && fail "F: redis is not a role container and must never be docker-logs'd"
log_file | grep -qF "COORDINATION[agnes-extraction-worker-1]: 5x 'CoordinationUnavailable'" \
    || fail "F: env-declared redis (AGNES_COORDINATION_BACKEND in .env) must gate the coordination signature ON"
echo "OK: F — extraction-worker scanned+named; .env AGNES_COORDINATION_BACKEND=redis arms the coordination signature"
rm -rf "$tmp"

echo "OK"
