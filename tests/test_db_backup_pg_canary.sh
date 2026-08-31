#!/usr/bin/env bash
# Integration test for the Postgres pg_dump + restore-canary path added to
# infra/modules/customer-instance/files/agnes-db-backup.sh.
#
# Stubs `docker`, `logger`, and `curl` with fakes on PATH that record every
# invocation to a transcript file, then drives the backup script through
# four scenarios:
#
#   A. DuckDB backend (instance.yaml::database.backend = duckdb) — pg_dump
#      must be skipped entirely; no PG_STATUS file; MSG has no "pg=".
#   B. Side-car backend + container up + everything succeeds — pg_dump,
#      createdb, pg_restore, the sanity psql query, and a final dropdb all
#      run against the expected container with the expected args;
#      PG_STATUS=OK; overall exit 0.
#   C. Side-car backend + container up but the restore-canary's sanity
#      query returns garbage — PG_STATUS=FAILED, webhook fires, overall
#      script exit non-zero even though the DuckDB verify itself succeeds.
#   D. Side-car backend PERSISTED in instance.yaml but the container is NOT
#      actually running (mid-migration / stale state) — must behave exactly
#      like scenario A (container-presence check gates the whole path).
#
# Run with: bash tests/test_db_backup_pg_canary.sh
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
script=$repo_root/infra/modules/customer-instance/files/agnes-db-backup.sh

fail() {
    echo "FAIL: $*"
    echo "--- transcript ---"
    cat "$transcript" 2>/dev/null || true
    exit 1
}

# --- Shared fake-bin builder --------------------------------------------
# Args: fake_bin_dir
# Env read by the fakes (set per-scenario before invoking the script):
#   FAKE_PG_CONTAINER_UP=1        docker ps lists agnes-postgres-1
#   FAKE_PG_DUMP_FAIL=1           pg_dump exits non-zero
#   FAKE_CREATEDB_RC=<n>          createdb exit code (default 0)
#   FAKE_PG_RESTORE_RC=<n>        pg_restore exit code (default 0)
#   FAKE_USER_COUNT=<n>           psql sanity-query stdout (default 3)
#   FAKE_PSQL_BAD_OUTPUT=1        psql prints non-numeric garbage instead
#   FAKE_DUCKDB_VERIFY_RC=<n>     agnes-app-1 python verify exit (default 0)
build_fake_bin() {
    local dir=$1
    mkdir -p "$dir"

    cat > "$dir/docker" <<'FAKE'
#!/usr/bin/env bash
echo "docker $*" >> "$TRANSCRIPT"
cmd="$1"; shift || true
case "$cmd" in
    ps)
        if [ "${FAKE_PG_CONTAINER_UP:-0}" = "1" ]; then
            echo "agnes-postgres-1"
        fi
        exit 0
        ;;
    exec)
        # Optional `-i` before the container name (pg_restore stdin feed).
        if [ "${1:-}" = "-i" ]; then shift; fi
        container="$1"; shift || true
        sub="$1"; shift || true
        case "$sub" in
            pg_dump)
                if [ "${FAKE_PG_DUMP_FAIL:-0}" = "1" ]; then
                    echo "pg_dump: simulated failure" >&2
                    exit 1
                fi
                printf 'FAKE-CUSTOM-FORMAT-DUMP\n'
                exit 0
                ;;
            createdb)
                exit "${FAKE_CREATEDB_RC:-0}"
                ;;
            pg_restore)
                cat >/dev/null   # drain stdin so the redirect never blocks
                exit "${FAKE_PG_RESTORE_RC:-0}"
                ;;
            dropdb)
                exit 0
                ;;
            psql)
                if [ "${FAKE_PSQL_BAD_OUTPUT:-0}" = "1" ]; then
                    echo "not-a-number"
                else
                    echo "${FAKE_USER_COUNT:-3}"
                fi
                exit 0
                ;;
            python)
                exit "${FAKE_DUCKDB_VERIFY_RC:-0}"
                ;;
            *)
                exit 0
                ;;
        esac
        ;;
    *)
        exit 0
        ;;
esac
FAKE
    chmod +x "$dir/docker"

    cat > "$dir/logger" <<'FAKE'
#!/usr/bin/env bash
shift  # -t
shift  # tag
echo "logger: $*" >> "$TRANSCRIPT"
FAKE
    chmod +x "$dir/logger"

    cat > "$dir/curl" <<'FAKE'
#!/usr/bin/env bash
echo "curl $*" >> "$TRANSCRIPT"
echo "curl-called" >> "$CURL_CALLED"
exit 0
FAKE
    chmod +x "$dir/curl"

    cat > "$dir/chown" <<'FAKE'
#!/usr/bin/env bash
# no-op in the sandbox — can't chown to uid 999 without root.
exit 0
FAKE
    chmod +x "$dir/chown"
}

# --- Sandbox builder ------------------------------------------------------
# Args: tmp_dir
# Seeds a fake DuckDB source file + a fake instance.yaml at the paths the
# harness will point the script at via AGNES_DB_BACKUP_* overrides, and the
# ROOT/system.duckdb paths via sed-patched copy of the script (same
# technique as tests/test_state_applier_host_script.sh).
make_sandboxed_script() {
    local tmp=$1
    mkdir -p "$tmp/data/state" "$tmp/data/backups"
    printf 'fake duckdb bytes\n' > "$tmp/data/state/system.duckdb"

    local sandboxed=$tmp/agnes-db-backup.sh
    sed \
        -e "s|/data/state/system.duckdb|$tmp/data/state/system.duckdb|g" \
        -e "s|/data/backups/system-duckdb|$tmp/data/backups/system-duckdb|g" \
        -e "s|/etc/agnes-watchdog.env|$tmp/nonexistent-agnes-watchdog.env|g" \
        -e "s|/opt/agnes/.env|$tmp/nonexistent-opt-agnes.env|g" \
        -e "s|/var/log/agnes-watchdog.log|$tmp/agnes-watchdog.log|g" \
        "$script" > "$sandboxed"
    chmod +x "$sandboxed"
    echo "$sandboxed"
}

run_scenario() {
    local name=$1
    tmp=$(mktemp -d)
    transcript=$tmp/transcript.log
    curl_called_file=$tmp/curl_called
    : > "$transcript"
    : > "$curl_called_file"

    sandboxed=$(make_sandboxed_script "$tmp")
    fake_bin=$tmp/bin
    build_fake_bin "$fake_bin"

    instance_yaml=$tmp/instance.yaml

    echo "--- scenario $name ---"
}

# =====================================================================
# Scenario A: DuckDB backend — pg_dump must be skipped entirely.
# =====================================================================
run_scenario A
cat > "$instance_yaml" <<'YAML'
database:
  backend: duckdb
YAML

TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    AGNES_DB_BACKUP_INSTANCE_YAML="$instance_yaml" \
    AGNES_DB_BACKUP_POSTGRES_CONTAINER="agnes-postgres-1" \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || fail "A: script exited non-zero on a healthy DuckDB-only run"

grep -q "pg_dump" "$transcript" && fail "A: pg_dump must not run for DuckDB backend"
[ -f "$tmp/data/backups/system-duckdb/$(date -u +%Y%m%d)/PG_STATUS" ] \
    && fail "A: PG_STATUS file must not exist for DuckDB backend"
echo "OK: A — DuckDB backend skips pg_dump entirely"
rm -rf "$tmp"

# =====================================================================
# Scenario B: side-car backend, container up, everything succeeds.
# =====================================================================
run_scenario B
cat > "$instance_yaml" <<'YAML'
database:
  backend: side_car
YAML

TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    AGNES_DB_BACKUP_INSTANCE_YAML="$instance_yaml" \
    AGNES_DB_BACKUP_POSTGRES_CONTAINER="agnes-postgres-1" \
    FAKE_PG_CONTAINER_UP=1 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || fail "B: script exited non-zero on a fully healthy side-car run"

grep -qF "docker exec agnes-postgres-1 pg_dump -U agnes -F c agnes" "$transcript" \
    || fail "B: expected pg_dump invocation with -U agnes -F c agnes against agnes-postgres-1"
grep -qF "docker exec agnes-postgres-1 createdb -U agnes agnes_backup_canary" "$transcript" \
    || fail "B: expected createdb of the scratch canary DB"
grep -qF "docker exec -i agnes-postgres-1 pg_restore -U agnes -d agnes_backup_canary --no-owner" "$transcript" \
    || fail "B: expected pg_restore into the scratch canary DB via stdin"
grep -qF "docker exec agnes-postgres-1 psql -U agnes -d agnes_backup_canary -tAc SELECT count(*) FROM users;" "$transcript" \
    || fail "B: expected the trivial sanity query against the canary DB"
grep -qF "docker exec agnes-postgres-1 dropdb -U agnes --if-exists agnes_backup_canary" "$transcript" \
    || fail "B: expected the scratch canary DB to be dropped"

dated_dest="$tmp/data/backups/system-duckdb/$(date -u +%Y%m%d)"
[ -f "$dated_dest/PG_STATUS" ] || fail "B: PG_STATUS file missing"
grep -q '^OK ' "$dated_dest/PG_STATUS" || fail "B: PG_STATUS must read OK (got: $(cat "$dated_dest/PG_STATUS"))"
[ -f "$dated_dest/agnes-postgres.dump" ] || fail "B: agnes-postgres.dump artifact missing"
grep -q "curl-called" "$curl_called_file" && fail "B: webhook must not fire on a healthy run"
echo "OK: B — side-car backend runs pg_dump + restore-canary end to end, PG_STATUS=OK"
rm -rf "$tmp"

# =====================================================================
# Scenario C: side-car backend, restore succeeds but the sanity query
# returns garbage — canary must fail and alert, even though the DuckDB
# verify itself is healthy.
# =====================================================================
run_scenario C
cat > "$instance_yaml" <<'YAML'
database:
  backend: side_car
YAML

TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    AGNES_DB_BACKUP_INSTANCE_YAML="$instance_yaml" \
    AGNES_DB_BACKUP_POSTGRES_CONTAINER="agnes-postgres-1" \
    FAKE_PG_CONTAINER_UP=1 \
    FAKE_PSQL_BAD_OUTPUT=1 \
    FAKE_DUCKDB_VERIFY_RC=0 \
    WEBHOOK_URL="https://example.invalid/webhook" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" && fail "C: script must exit non-zero when the pg canary fails"

dated_dest="$tmp/data/backups/system-duckdb/$(date -u +%Y%m%d)"
grep -q '^FAILED ' "$dated_dest/PG_STATUS" || fail "C: PG_STATUS must read FAILED (got: $(cat "$dated_dest/PG_STATUS" 2>/dev/null)"
grep -q '^OK ' "$dated_dest/STATUS" || fail "C: DuckDB STATUS should still be OK — the canary is independent"
grep -q "curl-called" "$curl_called_file" || fail "C: webhook must fire when the pg canary fails"
grep -q "pg=FAILED" "$tmp/agnes-watchdog.log" || fail "C: log line must include pg=FAILED"
echo "OK: C — failed restore-canary sanity query fails the run and alerts"
rm -rf "$tmp"

# =====================================================================
# Scenario D: instance.yaml says side_car but the container isn't
# actually running — must behave exactly like the DuckDB-only case.
# =====================================================================
run_scenario D
cat > "$instance_yaml" <<'YAML'
database:
  backend: side_car
YAML

TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    AGNES_DB_BACKUP_INSTANCE_YAML="$instance_yaml" \
    AGNES_DB_BACKUP_POSTGRES_CONTAINER="agnes-postgres-1" \
    FAKE_PG_CONTAINER_UP=0 \
    WEBHOOK_URL="" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || fail "D: script exited non-zero when the side-car container isn't up"

grep -q "pg_dump" "$transcript" && fail "D: pg_dump must not run when the postgres container isn't up"
[ -f "$tmp/data/backups/system-duckdb/$(date -u +%Y%m%d)/PG_STATUS" ] \
    && fail "D: PG_STATUS file must not exist when the container presence check fails"
echo "OK: D — persisted side_car backend with no running container skips pg_dump (container-presence gate)"
rm -rf "$tmp"

# =====================================================================
# Scenario E: side-car backend with NO system.duckdb at all — a PG-native
# instance (born on Postgres) or one whose frozen post-migration snapshot
# was retired. The DuckDB half must SKIP (not FAIL): STATUS=SKIPPED, no
# webhook, exit 0 — while the pg_dump + restore-canary half still runs.
# =====================================================================
run_scenario E
cat > "$instance_yaml" <<'YAML'
database:
  backend: side_car
YAML
rm "$tmp/data/state/system.duckdb"

TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    AGNES_DB_BACKUP_INSTANCE_YAML="$instance_yaml" \
    AGNES_DB_BACKUP_POSTGRES_CONTAINER="agnes-postgres-1" \
    FAKE_PG_CONTAINER_UP=1 \
    WEBHOOK_URL="https://example.invalid/webhook" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" || fail "E: script must exit 0 when there is no system.duckdb to back up"

dated_dest="$tmp/data/backups/system-duckdb/$(date -u +%Y%m%d)"
grep -q '^SKIPPED ' "$dated_dest/STATUS" || fail "E: STATUS must read SKIPPED (got: $(cat "$dated_dest/STATUS" 2>/dev/null))"
grep -q '^OK ' "$dated_dest/PG_STATUS" || fail "E: pg half must still run and pass (got: $(cat "$dated_dest/PG_STATUS" 2>/dev/null))"
grep -qF "docker exec agnes-postgres-1 pg_dump -U agnes -F c agnes" "$transcript" \
    || fail "E: pg_dump must still run without a DuckDB file"
grep -q "docker exec agnes-app-1 python" "$transcript" \
    && fail "E: the DuckDB verify must not run against a file that does not exist"
grep -q "curl-called" "$curl_called_file" && fail "E: a skipped DuckDB half must not alert"
grep -q "verify=SKIPPED" "$tmp/agnes-watchdog.log" || fail "E: log line must say verify=SKIPPED"
echo "OK: E — missing system.duckdb skips the DuckDB half cleanly while the pg canary still runs"
rm -rf "$tmp"

# =====================================================================
# Scenario F: NO system.duckdb and NO Postgres side-car — a DuckDB-backend
# instance whose state file has vanished (deleted, broken mount, disk not
# mounted). This is the catastrophe the unit exists to catch, so it must
# NOT be reported as a skip: STATUS=FAILED, the webhook fires, exit
# non-zero. Scenario E's skip is legitimate only BECAUSE Postgres holds
# the state instead; without a side-car there is no such second copy.
# =====================================================================
run_scenario F
cat > "$instance_yaml" <<'YAML'
database:
  backend: duckdb
YAML
rm "$tmp/data/state/system.duckdb"

TRANSCRIPT="$transcript" CURL_CALLED="$curl_called_file" \
    AGNES_DB_BACKUP_INSTANCE_YAML="$instance_yaml" \
    AGNES_DB_BACKUP_POSTGRES_CONTAINER="agnes-postgres-1" \
    WEBHOOK_URL="https://example.invalid/webhook" \
    PATH="$fake_bin:$PATH" \
    bash "$sandboxed" && fail "F: a DuckDB-backend instance with no system.duckdb must exit non-zero"

dated_dest="$tmp/data/backups/system-duckdb/$(date -u +%Y%m%d)"
grep -q '^FAILED ' "$dated_dest/STATUS" \
    || fail "F: STATUS must read FAILED, not SKIPPED (got: $(cat "$dated_dest/STATUS" 2>/dev/null))"
[ -f "$dated_dest/PG_STATUS" ] && fail "F: no side-car, so there must be no PG_STATUS"
grep -q "curl-called" "$curl_called_file" \
    || fail "F: losing the only copy of the app state must alert"
echo "OK: F — a vanished system.duckdb on a DuckDB-backend instance still FAILS and alerts"
rm -rf "$tmp"

echo "OK"
