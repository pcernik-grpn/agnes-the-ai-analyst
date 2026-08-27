#!/usr/bin/env bash
# Integration test for the role-split (m-tier) sequential /readyz-gated
# rolling recreate added to scripts/ops/agnes-auto-upgrade.sh, plus the
# data-refresh-job sync-defer probe.
#
# Stubs `docker`, `curl`, `logger`, and `flock` with fakes on PATH that
# record every invocation to a transcript file, then drives the script
# through six scenarios:
#
#   A. Single-container topology (no worker/gateway/apiN services in the
#      resolved compose config) — the ORIGINAL one-shot `docker compose
#      up -d` recreate must run byte-for-byte unchanged; no `--no-deps`
#      anywhere.
#   B. Role-split topology, healthy — worker+gateway recreate together
#      first, then api1 (ready immediately) then api2 (503-then-200,
#      polled 3x) recreate ONE AT A TIME, each fully readyz-gated before
#      the next is even touched. Overall exit 0, no alert.
#   C. Role-split topology, 3 replicas, api2 persistently unready — api1
#      recreates+readies, api2 recreates but never reports ready within
#      the bounded timeout: the rollout ABORTS (non-zero exit + webhook
#      alert) and api3 is NEVER recreated (stays on the previous image).
#   D. Sync-defer (existing behavior preserved): /api/sync/status reports
#      locked=true — the script defers the recreate entirely.
#   E. Sync-defer (new behavior): /api/sync/status reports locked=false
#      but GET /api/jobs?kind=data-refresh&status=running returns a
#      running job (worker-side sync under role-split) — the script must
#      still defer, and must authenticate the jobs call with
#      `Authorization: Bearer $SCHEDULER_API_TOKEN`.
#   F. Fail-open: SCHEDULER_API_TOKEN is unset — the jobs probe must never
#      even be attempted, and (with no other busy signal) the recreate
#      proceeds normally.
#   G. Role-split topology, worker+gateway recreate itself hard-fails
#      (`docker compose up -d --no-deps worker gateway` exits non-zero) —
#      the rollout must ABORT (non-zero exit + webhook alert) WITHOUT ever
#      recreating any api replica.
#   I. Host-artifact refresh from the pinned image: config files + the
#      script itself come out of the image's /opt/agnes-host/ via
#      docker create/cp — no network fetch of the source repo — and an
#      extracted config change alone (equal image ids) triggers the
#      recreate and the self-update.
#   J. Extract container cannot be created (image not pullable, not in
#      the local store) — the tick degrades to WARN-and-keep-existing:
#      no file touched, no recreate, exit 0, still no network fetch.
#   K. APPS_SUBDOMAIN_BASE is set (data-app subdomains configured). The
#      Caddyfile.apps-subdomain vhost fragment must ALSO come out of the
#      pinned image, and the whole tick must survive `set -u`: an earlier
#      revision left a `$RAW_BASE` reference behind in this block after
#      the variable's definition was deleted, so on exactly these VMs the
#      tick aborted before the recreate AND before the self-update —
#      auto-upgrade stopped dead, unrecoverably (the self-update is what
#      would have shipped the fix).
#   L. APPS_SUBDOMAIN_BASE is set but no extract container is available —
#      the fragment refresh must degrade like every other artifact
#      (WARN, keep the existing copy, still wire the Caddyfile from it),
#      never abort the tick and never fall back to a network fetch.
#
# Run with: bash tests/test_auto_upgrade_role_split.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
script=$repo_root/scripts/ops/agnes-auto-upgrade.sh

fail() {
    echo "FAIL: $*"
    echo "--- transcript ---"
    cat "$transcript" 2>/dev/null || true
    exit 1
}

line_num() {
    # First transcript line number containing the fixed-string pattern, or
    # empty if absent.
    grep -n -F -- "$1" "$transcript" 2>/dev/null | head -1 | cut -d: -f1
}

# --- Shared fake-bin builder --------------------------------------------
# Env read by the fakes (set per-scenario before invoking the script):
#   FAKE_TAG_ID / FAKE_RUNNING_IMAGE_ID   image-drift inputs (differ by default)
#   FAKE_TOPOLOGY=role_split|single       `docker compose config --services` shape
#   FAKE_API_REPLICA_LIST                 newline-separated apiN service names
#   FAKE_SYNC_LOCKED=1                    /api/sync/status -> locked:true
#   FAKE_DATA_REFRESH_RUNNING=1           /api/jobs -> one running data-refresh job
#   FAKE_READYZ_FAIL_COUNT_<svc>=N        that replica's /readyz fails N times, then ready
#   FAKE_READYZ_ALWAYS_FAIL_<svc>=1       that replica's /readyz never reports ready
build_fake_bin() {
    local dir=$1
    mkdir -p "$dir"

    cat > "$dir/docker" <<'FAKE'
#!/usr/bin/env bash
echo "docker $*" >> "$TRANSCRIPT"

if [ "${1:-}" = "compose" ]; then
    shift
    while [ "${1:-}" = "--profile" ]; do
        shift 2
    done
    sub="${1:-}"
    shift || true
    case "$sub" in
        pull)
            exit 0
            ;;
        config)
            if [ "${FAKE_TOPOLOGY:-single}" = "role_split" ]; then
                printf 'worker\ngateway\nredis\ncaddy-mtier\n'
                printf '%s\n' "$FAKE_API_REPLICA_LIST"
            else
                printf 'app\nscheduler\n'
            fi
            exit 0
            ;;
        ps)
            echo "${FAKE_RUNNING_CID:-runningcid123}"
            exit 0
            ;;
        exec)
            if [ "${1:-}" = "-T" ]; then shift; fi
            svc="${1:-}"
            counter_file="$READYZ_STATE_DIR/readyz_calls_$svc"
            count=0
            [ -f "$counter_file" ] && count=$(cat "$counter_file")
            count=$((count + 1))
            echo "$count" > "$counter_file"

            fail_count_var="FAKE_READYZ_FAIL_COUNT_$svc"
            always_fail_var="FAKE_READYZ_ALWAYS_FAIL_$svc"
            fail_count=${!fail_count_var:-0}
            always_fail=${!always_fail_var:-0}

            if [ "$always_fail" = "1" ]; then
                exit 22
            fi
            if [ "$count" -le "$fail_count" ]; then
                exit 22
            fi
            printf '{"status": "ready"}\n'
            exit 0
            ;;
        up)
            exit "${FAKE_COMPOSE_UP_RC:-0}"
            ;;
        *)
            exit 0
            ;;
    esac
else
    case "${1:-}" in
        images)
            echo "${FAKE_TAG_ID:-sha256:newimage000}"
            exit 0
            ;;
        inspect)
            echo "${FAKE_RUNNING_IMAGE_ID:-sha256:oldimage000}"
            exit 0
            ;;
        image)
            if [ "${2:-}" = "inspect" ]; then
                echo ""
            fi
            exit 0
            ;;
        create)
            # Extract container for the /opt/agnes-host artifact refresh.
            # Without FAKE_EXTRACT_CID the create fails — simulating "image
            # not pullable and not in the local store", the analogue of the
            # old network-failure simulation for the raw-GitHub fetch.
            if [ -n "${FAKE_EXTRACT_CID:-}" ]; then
                echo "$FAKE_EXTRACT_CID"
                exit 0
            fi
            exit 1
            ;;
        cp)
            # docker cp <cid>:<path-under-/opt/agnes-host/> <dest> — serve
            # the file from the FAKE_HOST_ARTIFACTS_DIR fixture tree. Parse
            # after "agnes-host/" (not a fixed prefix) because the sandbox
            # sed rewrites /opt/agnes* paths wholesale.
            src="${2:-}"
            dest="${3:-}"
            path_in_image="${src#*agnes-host/}"
            if [ -n "${FAKE_HOST_ARTIFACTS_DIR:-}" ] \
               && [ -f "$FAKE_HOST_ARTIFACTS_DIR/$path_in_image" ]; then
                cp "$FAKE_HOST_ARTIFACTS_DIR/$path_in_image" "$dest"
                exit 0
            fi
            exit 1
            ;;
        rm|pull)
            exit 0
            ;;
        *)
            exit 0
            ;;
    esac
fi
FAKE
    chmod +x "$dir/docker"

    cat > "$dir/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl $*" >> "$TRANSCRIPT"

url=""
for a in "$@"; do
    case "$a" in
        http*) url="$a" ;;
    esac
done

if [ -n "${WEBHOOK_URL:-}" ] && [ "$url" = "$WEBHOOK_URL" ]; then
    echo "curl-called" >> "$CURL_CALLED"
    exit 0
fi

case "$url" in
    */api/sync/status)
        if [ "${FAKE_SYNC_LOCKED:-0}" = "1" ]; then
            printf '{"locked": true}\n'
        else
            printf '{"locked": false}\n'
        fi
        exit 0
        ;;
    */api/jobs*)
        if [ "${FAKE_DATA_REFRESH_RUNNING:-0}" = "1" ]; then
            printf '{"jobs": [{"id": "job-1", "kind": "data-refresh", "status": "running"}]}\n'
        else
            printf '{"jobs": []}\n'
        fi
        exit 0
        ;;
    *)
        # No other URL is expected: config-file refresh + self-update come
        # from the pinned image via `docker create`/`docker cp`, never from
        # a network fetch (scenarios I/J assert this). Fail loudly so an
        # unexpected fetch shows up as a scenario failure.
        exit 7
        ;;
esac
FAKE
    chmod +x "$dir/curl"

    cat > "$dir/logger" <<'FAKE'
#!/usr/bin/env bash
shift  # -t
shift  # tag
echo "logger: $*" >> "$TRANSCRIPT"
FAKE
    chmod +x "$dir/logger"

    # flock is Linux-only (util-linux); stub it to a no-op so this runs on
    # macOS dev laptops too (same rationale as tests/test_state_applier_host_script.sh).
    cat > "$dir/flock" <<'FAKE'
#!/usr/bin/env bash
exit 0
FAKE
    chmod +x "$dir/flock"
}

# --- Sandbox builder ------------------------------------------------------
# Args: tmp_dir
# Patches the absolute host paths the script hardcodes (`/opt/agnes`, the
# flock lockfile, the shared webhook-config file) onto sandbox-local paths,
# same sed-a-copy technique as tests/test_state_applier_host_script.sh /
# tests/test_db_backup_pg_canary.sh.
make_sandboxed_script() {
    local tmp=$1
    mkdir -p "$tmp/opt/agnes"

    # The script sources the shared compose-overlay resolver from
    # /opt/agnes/scripts/ops (delivered by the same artifact refresh) and
    # skips the tick when it is absent, so the sandbox has to provide it or
    # every scenario below exits early without exercising anything.
    mkdir -p "$tmp/opt/agnes/scripts/ops"
    cp "$(dirname "$script")/agnes-compose-file.sh" "$tmp/opt/agnes/scripts/ops/agnes-compose-file.sh"

    # Self-update target dir (the sed below relocates /usr/local/bin here);
    # seed it with the current script so `cmp -s` has a real comparand.
    mkdir -p "$tmp/usr-local-bin"
    cp "$script" "$tmp/usr-local-bin/agnes-auto-upgrade.sh"

    local sandboxed=$tmp/agnes-auto-upgrade.sh
    sed \
        -e "s|/opt/agnes|$tmp/opt/agnes|g" \
        -e "s|/usr/local/bin|$tmp/usr-local-bin|g" \
        -e "s|/var/lock/agnes-auto-upgrade.lock|$tmp/agnes-auto-upgrade.lock|g" \
        -e "s|/etc/agnes-watchdog.env|$tmp/nonexistent-agnes-watchdog.env|g" \
        "$script" > "$sandboxed"
    chmod +x "$sandboxed"
    echo "$sandboxed"
}

write_env() {
    # Args: opt_agnes_dir, scheduler_token ("" to omit the key entirely),
    #       apps_subdomain_base ("" / omitted to omit the key entirely)
    local dir=$1 token=$2 apps_base=${3:-}
    {
        echo "AGNES_TAG=test-tag"
        echo "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml"
        if [ -n "$token" ]; then
            echo "SCHEDULER_API_TOKEN=$token"
        fi
        if [ -n "$apps_base" ]; then
            echo "APPS_SUBDOMAIN_BASE=$apps_base"
        fi
    } > "$dir/.env"
}

run_scenario() {
    local name=$1
    tmp=$(mktemp -d)
    transcript=$tmp/transcript.log
    curl_called_file=$tmp/curl_called
    readyz_state_dir=$tmp/readyz_state
    mkdir -p "$readyz_state_dir"
    : > "$transcript"
    : > "$curl_called_file"

    sandboxed=$(make_sandboxed_script "$tmp")
    fake_bin=$tmp/bin
    build_fake_bin "$fake_bin"
    write_env "$tmp/opt/agnes" "test-scheduler-token"

    echo "--- scenario $name ---"
}

# =====================================================================
# Scenario A: single-container topology — one-shot recreate unchanged.
# =====================================================================
run_scenario A
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "A: script must exit 0 on a healthy single-container recreate (got $rc)"

grep -qF "docker compose ps -q app" "$transcript" \
    || fail "A: drift reference must still be the 'app' service for single-container"
grep -qF "docker compose up -d" "$transcript" \
    || fail "A: expected the one-shot recreate"
grep -qF -- "--no-deps" "$transcript" \
    && fail "A: single-container topology must never use the role-split --no-deps path"
grep -q "curl-called" "$curl_called_file" \
    && fail "A: no alert expected on a healthy run"
echo "OK: A — single-container topology keeps the exact one-shot recreate"
rm -rf "$tmp"

# =====================================================================
# Scenario B: role-split, healthy — worker+gateway first, then api1, api2
# ONE AT A TIME, each readyz-gated. api2 answers 503 twice then 200 (waits
# then proceeds).
# =====================================================================
run_scenario B
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_REPLICA_LIST=$'api1\napi2' \
    FAKE_READYZ_FAIL_COUNT_api2=2 \
    AGNES_AUTO_UPGRADE_READYZ_INTERVAL=1 \
    AGNES_AUTO_UPGRADE_READYZ_TIMEOUT=10 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "B: script must exit 0 once every replica reports ready (got $rc)"

grep -qF "docker compose ps -q worker" "$transcript" \
    || fail "B: drift reference must be 'worker' under role-split"
l_wg=$(line_num "docker compose up -d --no-deps worker gateway")
l_api1_up=$(line_num "docker compose up -d --no-deps api1")
l_api2_up=$(line_num "docker compose up -d --no-deps api2")
[ -n "$l_wg" ] || fail "B: worker+gateway recreate line missing"
[ -n "$l_api1_up" ] || fail "B: api1 recreate line missing"
[ -n "$l_api2_up" ] || fail "B: api2 recreate line missing"
[ "$l_wg" -lt "$l_api1_up" ] || fail "B: worker+gateway must recreate BEFORE api1"
[ "$l_api1_up" -lt "$l_api2_up" ] || fail "B: api1 must finish (incl. its readyz wait) BEFORE api2 is even touched"
api2_exec_calls=$(grep -cF "docker compose exec -T api2 curl" "$transcript" || true)
[ "$api2_exec_calls" -ge 3 ] || fail "B: api2 should have been polled at least 3x (503, 503, 200) — saw $api2_exec_calls"
grep -q "curl-called" "$curl_called_file" \
    && fail "B: no alert expected when the whole rollout succeeds"
echo "OK: B — role-split rolling recreate: worker/gateway then api1/api2 sequentially, waits then proceeds"
rm -rf "$tmp"

# =====================================================================
# Scenario C: role-split, 3 replicas — api2 never reports ready. The
# rollout must ABORT (non-zero exit + alert) WITHOUT ever touching api3.
# =====================================================================
run_scenario C
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_REPLICA_LIST=$'api1\napi2\napi3' \
    FAKE_READYZ_ALWAYS_FAIL_api2=1 \
    AGNES_AUTO_UPGRADE_READYZ_INTERVAL=1 \
    AGNES_AUTO_UPGRADE_READYZ_TIMEOUT=2 \
    WEBHOOK_URL="https://example.invalid/webhook" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" && fail "C: script must exit non-zero when a replica never becomes ready"

grep -qF "docker compose up -d --no-deps api1" "$transcript" \
    || fail "C: api1 must have been recreated"
grep -qF "docker compose up -d --no-deps api2" "$transcript" \
    || fail "C: api2 must have been recreated (and then found unready)"
grep -qF "docker compose up -d --no-deps api3" "$transcript" \
    && fail "C: api3 must NEVER be recreated once api2 aborts the rollout — it must keep serving the previous image"
grep -q "curl-called" "$curl_called_file" \
    || fail "C: a persistent readyz failure must fire the webhook alert"
grep -qF "https://example.invalid/webhook" "$transcript" \
    || fail "C: the alert POST must target WEBHOOK_URL"
grep -q "ABORTED role-split rolling recreate" "$transcript" \
    || fail "C: the abort must be logged"
echo "OK: C — persistent readyz failure aborts the rollout, alerts, and leaves remaining replicas untouched"
rm -rf "$tmp"

# =====================================================================
# Scenario D: sync-defer (existing behavior) — /api/sync/status locked.
# =====================================================================
run_scenario D
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_SYNC_LOCKED=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "D: a deferred tick must still exit 0 (got $rc)"

grep -qF "docker compose up -d" "$transcript" \
    && fail "D: no recreate at all should happen while sync/status reports locked"
grep -q "deferred recreate: sync/refresh in flight (sync/status locked)" "$transcript" \
    || fail "D: the defer reason must name 'sync/status locked'"
echo "OK: D — /api/sync/status locked still defers the recreate entirely"
rm -rf "$tmp"

# =====================================================================
# Scenario E: sync-defer (new behavior) — a running data-refresh job,
# authenticated with SCHEDULER_API_TOKEN.
# =====================================================================
run_scenario E
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_SYNC_LOCKED=0 \
    FAKE_DATA_REFRESH_RUNNING=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "E: a deferred tick must still exit 0 (got $rc)"

grep -qF "docker compose up -d" "$transcript" \
    && fail "E: no recreate should happen while a data-refresh job is running"
grep -qF "/api/jobs?kind=data-refresh&status=running" "$transcript" \
    || fail "E: the jobs endpoint must be queried with kind=data-refresh&status=running"
grep -qF "Authorization: Bearer test-scheduler-token" "$transcript" \
    || fail "E: the jobs query must authenticate with the SCHEDULER_API_TOKEN bearer"
grep -q "deferred recreate: sync/refresh in flight (data-refresh job running)" "$transcript" \
    || fail "E: the defer reason must name 'data-refresh job running'"
echo "OK: E — a running data-refresh job defers the recreate, authenticated via SCHEDULER_API_TOKEN"
rm -rf "$tmp"

# =====================================================================
# Scenario F: fail-open — no SCHEDULER_API_TOKEN configured. The jobs
# probe must never even be attempted, and (with no other busy signal)
# the recreate proceeds normally.
# =====================================================================
tmp=$(mktemp -d)
transcript=$tmp/transcript.log
curl_called_file=$tmp/curl_called
readyz_state_dir=$tmp/readyz_state
mkdir -p "$readyz_state_dir"
: > "$transcript"
: > "$curl_called_file"
sandboxed=$(make_sandboxed_script "$tmp")
fake_bin=$tmp/bin
build_fake_bin "$fake_bin"
write_env "$tmp/opt/agnes" ""   # no SCHEDULER_API_TOKEN key at all
echo "--- scenario F ---"

rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_SYNC_LOCKED=0 \
    FAKE_DATA_REFRESH_RUNNING=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "F: recreate should proceed when there is no other busy signal (got $rc)"

grep -qF "/api/jobs" "$transcript" \
    && fail "F: the jobs probe must never be attempted without a SCHEDULER_API_TOKEN"
grep -qF "docker compose up -d" "$transcript" \
    || fail "F: the recreate must proceed when the token is absent and no other signal is busy"
echo "OK: F — missing SCHEDULER_API_TOKEN skips the jobs probe (fails open) and the recreate proceeds"
rm -rf "$tmp"

# =====================================================================
# Scenario G: role-split, worker+gateway recreate itself hard-fails. The
# rollout must ABORT (non-zero exit + alert) WITHOUT ever recreating any
# api replica — a failed worker/gateway recreate must not be papered over
# by rolling the api replicas forward anyway.
# =====================================================================
run_scenario G
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=role_split \
    FAKE_API_REPLICA_LIST=$'api1\napi2' \
    FAKE_COMPOSE_UP_RC=1 \
    AGNES_AUTO_UPGRADE_READYZ_INTERVAL=1 \
    AGNES_AUTO_UPGRADE_READYZ_TIMEOUT=2 \
    WEBHOOK_URL="https://example.invalid/webhook" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" && fail "G: script must exit non-zero when the worker/gateway recreate itself fails"

grep -qF "docker compose up -d --no-deps worker gateway" "$transcript" \
    || fail "G: the worker/gateway recreate must have been attempted"
grep -qF "docker compose up -d --no-deps api1" "$transcript" \
    && fail "G: api1 must NEVER be recreated when worker/gateway recreate hard-fails"
grep -qF "docker compose up -d --no-deps api2" "$transcript" \
    && fail "G: api2 must NEVER be recreated when worker/gateway recreate hard-fails"
grep -qF "docker compose exec -T api1 curl" "$transcript" \
    && fail "G: api1 readyz must never be polled — it was never touched"
grep -q "curl-called" "$curl_called_file" \
    || fail "G: a hard-failed worker/gateway recreate must fire the webhook alert"
grep -qF "https://example.invalid/webhook" "$transcript" \
    || fail "G: the alert POST must target WEBHOOK_URL"
grep -q "ABORTED role-split rolling recreate — worker/gateway recreate failed" "$transcript" \
    || fail "G: the abort must be logged with the worker/gateway-specific reason"
echo "OK: G — worker/gateway recreate failure aborts the rollout, alerts, and never touches api replicas"
rm -rf "$tmp"

# =====================================================================
# Scenario H: docker GC on a NO-DRIFT tick. The prune used to be the last
# statement inside the drift block, so a VM only ever reclaimed disk on a
# tick that happened to recreate containers — and never at all on a box
# sitting on the current image. Both prunes must now run on every tick,
# BEFORE the pull (so a nearly-full boot disk has room for the new image),
# without recreating anything.
# =====================================================================
run_scenario H
rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_TAG_ID=sha256:sameimage000 \
    FAKE_RUNNING_IMAGE_ID=sha256:sameimage000 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "H: a no-drift tick must exit 0 (got $rc)"

grep -qF "docker compose up -d" "$transcript" \
    && fail "H: nothing may be recreated when the running image already matches the tag"
grep -qF "docker image prune -f" "$transcript" \
    || fail "H: dangling images must be pruned even when no upgrade happened"
grep -qF "docker builder prune" "$transcript" \
    || fail "H: the BuildKit cache must be pruned even when no upgrade happened"
l_img_prune=$(line_num "docker image prune -f")
l_bld_prune=$(line_num "docker builder prune")
l_pull=$(line_num "docker compose pull")
[ -n "$l_pull" ] || fail "H: the pull line is missing from the transcript"
[ "$l_img_prune" -lt "$l_pull" ] \
    || fail "H: image prune must run BEFORE the pull, so a full boot disk gets room first"
[ "$l_bld_prune" -lt "$l_pull" ] \
    || fail "H: builder prune must run BEFORE the pull, so a full boot disk gets room first"
grep -qF -- "--filter until=" "$transcript" \
    || fail "H: builder prune must keep a retention window rather than dropping the whole cache"
grep -qE "docker image prune( -f)? -a| --all" "$transcript" \
    && fail "H: image prune must stay dangling-only — -a would drop tagged images a data app still needs"
echo "OK: H — both prunes run on a no-drift tick, before the pull, dangling-only"
rm -rf "$tmp"

# =====================================================================
# Scenario I: host-artifact refresh from the pinned image. The config
# files and the script's self-update must come from /opt/agnes-host/ in
# the image (docker create + docker cp), never from a network fetch of
# the source repo — and an extracted config change ALONE (image ids
# equal) must count as drift and trigger the recreate.
# =====================================================================
run_scenario I

# Fixture "image": /opt/agnes-host/ artifact tree the fake docker cp
# serves from.
host_artifacts=$tmp/host-artifacts
mkdir -p "$host_artifacts/scripts/ops" "$host_artifacts/static"
printf '# compose fresh-from-image\n' > "$host_artifacts/docker-compose.yml"
printf '# caddyfile fresh-from-image\n' > "$host_artifacts/Caddyfile"
cp "$(dirname "$script")/agnes-compose-file.sh" "$host_artifacts/scripts/ops/agnes-compose-file.sh"
printf '#!/bin/bash\n# self-update fresh-from-image\n' > "$host_artifacts/agnes-auto-upgrade.sh"

# Stale on-disk state the extraction must overwrite, plus a config marker
# that disagrees with whatever hash the refreshed tree produces — so the
# recreate below rides on CONFIG drift alone (image ids are forced equal).
printf '# compose stale-on-disk\n' > "$tmp/opt/agnes/docker-compose.yml"
printf 'bogus-marker-hash\n' > "$tmp/opt/agnes/.agnes-config-applied"

rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_TAG_ID=sha256:sameimage000 \
    FAKE_RUNNING_IMAGE_ID=sha256:sameimage000 \
    FAKE_EXTRACT_CID=extractcid123 \
    FAKE_HOST_ARTIFACTS_DIR="$host_artifacts" \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "I: a healthy artifact-refresh tick must exit 0 (got $rc)"

grep -qF "docker create" "$transcript" \
    || fail "I: the refresh must create an extract container from the pinned image"
grep -q "raw.githubusercontent.com" "$transcript" \
    && fail "I: no network fetch of the source repo may remain — artifacts come from the image"
grep -qF "curl -fsSL" "$transcript" \
    && fail "I: the old curl-based artifact/self-update fetch must be gone entirely"
grep -qF "# compose fresh-from-image" "$tmp/opt/agnes/docker-compose.yml" \
    || fail "I: docker-compose.yml must be refreshed from the image's /opt/agnes-host/"
grep -qF "# caddyfile fresh-from-image" "$tmp/opt/agnes/Caddyfile" \
    || fail "I: Caddyfile must be extracted from the image too"
grep -qF "docker compose up -d" "$transcript" \
    || fail "I: an extracted config change alone (equal image ids) must trigger the recreate"
grep -qF "bogus-marker-hash" "$tmp/opt/agnes/.agnes-config-applied" \
    && fail "I: the config marker must be rewritten after the recreate"
grep -qF "# self-update fresh-from-image" "$tmp/usr-local-bin/agnes-auto-upgrade.sh" \
    || fail "I: the self-update must install the script shipped in the image"
grep -q "self-update: replaced" "$transcript" \
    || fail "I: the self-update replacement must be logged"
echo "OK: I — config files + self-update extract from the pinned image, config drift alone recreates"
rm -rf "$tmp"

# =====================================================================
# Scenario J: the extract container cannot be created at all (image not
# pullable and absent from the local store). The tick must degrade to
# WARN-and-keep-existing: no config file touched, no recreate (image ids
# equal, marker lazily initialized), exit 0 — and still no network fetch.
# =====================================================================
run_scenario J
printf '# compose pre-existing\n' > "$tmp/opt/agnes/docker-compose.yml"

rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_TAG_ID=sha256:sameimage000 \
    FAKE_RUNNING_IMAGE_ID=sha256:sameimage000 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "J: a tick without an extract container must still exit 0 (got $rc)"

grep -qF "# compose pre-existing" "$tmp/opt/agnes/docker-compose.yml" \
    || fail "J: existing config files must be kept untouched when extraction is unavailable"
grep -q "cannot create an extract container" "$transcript" \
    || fail "J: the unavailable extract container must be WARN-logged"
grep -qF "docker compose up -d" "$transcript" \
    && fail "J: nothing may be recreated on a no-drift tick without an extract container"
grep -q "raw.githubusercontent.com" "$transcript" \
    && fail "J: extraction failure must NOT fall back to a network fetch of the source repo"
echo "OK: J — no extract container degrades to WARN-and-keep-existing, no recreate, no network fetch"
rm -rf "$tmp"

# =====================================================================
# Scenario K: APPS_SUBDOMAIN_BASE configured. Two things must hold, and
# the first is why this scenario exists at all:
#
#   1. The tick must COMPLETE. Every scenario above leaves
#      APPS_SUBDOMAIN_BASE unset, so the data-app-subdomain block is
#      skipped entirely and a bug inside it is invisible. A revision of
#      this script deleted the RAW_BASE definition but left `$RAW_BASE`
#      referenced in that block; under `set -u` the expansion aborted the
#      tick — on every VM with a subdomain base — before the recreate and
#      before the self-update, which is the one mechanism that could have
#      delivered a fix. Asserting exit 0 with the base set is the guard.
#   2. The vhost fragment must come from the pinned image's
#      /opt/agnes-host/, exactly like every other host artifact — not
#      from an unauthenticated fetch of the source repo's main branch,
#      which is the pin this whole change exists to restore.
# =====================================================================
run_scenario K
write_env "$tmp/opt/agnes" "test-scheduler-token" "apps.example.com"

host_artifacts=$tmp/host-artifacts
mkdir -p "$host_artifacts/scripts/ops" "$host_artifacts/static"
printf '# compose fresh-from-image\n' > "$host_artifacts/docker-compose.yml"
printf '# caddyfile fresh-from-image\n' > "$host_artifacts/Caddyfile"
printf '# apps-subdomain fragment fresh-from-image\n' > "$host_artifacts/Caddyfile.apps-subdomain"
cp "$(dirname "$script")/agnes-compose-file.sh" "$host_artifacts/scripts/ops/agnes-compose-file.sh"
printf '#!/bin/bash\n# self-update fresh-from-image\n' > "$host_artifacts/agnes-auto-upgrade.sh"

# A stale fragment on disk — the extraction must overwrite it.
printf '# apps-subdomain fragment stale-on-disk\n' > "$tmp/opt/agnes/Caddyfile.apps-subdomain"

rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_TAG_ID=sha256:sameimage000 \
    FAKE_RUNNING_IMAGE_ID=sha256:sameimage000 \
    FAKE_EXTRACT_CID=extractcid123 \
    FAKE_HOST_ARTIFACTS_DIR="$host_artifacts" \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "K: a tick with APPS_SUBDOMAIN_BASE set must complete (got $rc) — an unbound variable under 'set -u' aborts before the recreate AND the self-update"

grep -qF "agnes-host/Caddyfile.apps-subdomain" "$transcript" \
    || fail "K: the vhost fragment must be extracted from the image's /opt/agnes-host/"
grep -qF "# apps-subdomain fragment fresh-from-image" "$tmp/opt/agnes/Caddyfile.apps-subdomain" \
    || fail "K: the on-disk fragment must be replaced by the one shipped in the pinned image"
grep -qF "curl -fsSL" "$transcript" \
    && fail "K: the fragment must not be fetched over the network — no raw-main fetch may survive"
grep -q "raw.githubusercontent.com" "$transcript" \
    && fail "K: no fetch of the source repo's main branch may remain"
grep -qF "on_demand_tls" "$tmp/opt/agnes/Caddyfile" \
    || fail "K: the on-demand-TLS global options block must be prepended to the Caddyfile"
grep -qF "# apps-subdomain fragment fresh-from-image" "$tmp/opt/agnes/Caddyfile" \
    || fail "K: the image's fragment must be the one appended to the Caddyfile"
grep -qF "# self-update fresh-from-image" "$tmp/usr-local-bin/agnes-auto-upgrade.sh" \
    || fail "K: the self-update must still run on a VM with a data-app subdomain base"
echo "OK: K — APPS_SUBDOMAIN_BASE set: tick completes, fragment comes from the image, Caddyfile wired, self-update runs"
rm -rf "$tmp"

# =====================================================================
# Scenario L: APPS_SUBDOMAIN_BASE set, but no extract container (the
# scenario-J failure mode). The fragment refresh must degrade exactly
# like every other host artifact — WARN, keep the existing copy, still
# wire the Caddyfile from it — rather than aborting the tick or reaching
# for the network.
# =====================================================================
run_scenario L
write_env "$tmp/opt/agnes" "test-scheduler-token" "apps.example.com"
printf '# caddyfile pre-existing\n' > "$tmp/opt/agnes/Caddyfile"
printf '# apps-subdomain fragment pre-existing\n' > "$tmp/opt/agnes/Caddyfile.apps-subdomain"

rc=0
TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    READYZ_STATE_DIR="$readyz_state_dir" \
    FAKE_TOPOLOGY=single \
    FAKE_TAG_ID=sha256:sameimage000 \
    FAKE_RUNNING_IMAGE_ID=sha256:sameimage000 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || rc=$?
[ "$rc" -eq 0 ] || fail "L: a tick with APPS_SUBDOMAIN_BASE set and no extract container must still exit 0 (got $rc)"

grep -qF "# apps-subdomain fragment pre-existing" "$tmp/opt/agnes/Caddyfile.apps-subdomain" \
    || fail "L: the existing fragment must be kept untouched when extraction is unavailable"
grep -qF "curl -fsSL" "$transcript" \
    && fail "L: an unavailable extract container must NOT fall back to a network fetch of the fragment"
grep -q "raw.githubusercontent.com" "$transcript" \
    && fail "L: no fetch of the source repo's main branch may remain"
grep -qF "on_demand_tls" "$tmp/opt/agnes/Caddyfile" \
    || fail "L: the Caddyfile must still be wired from the fragment already on disk"
grep -qF "# apps-subdomain fragment pre-existing" "$tmp/opt/agnes/Caddyfile" \
    || fail "L: the on-disk fragment must be the one appended to the Caddyfile"
echo "OK: L — no extract container with a subdomain base: WARN-and-keep-existing, tick completes, no network fetch"
rm -rf "$tmp"

echo "OK"
