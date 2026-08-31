#!/bin/bash
# Daily system.duckdb backup + canary restore-verification.
#
# Copies the live DB file + WAL to /data/backups/system-duckdb/<YYYYMMDD>/
# and then PROVES the copy is restorable: agnes-db-verify.py opens a scratch
# copy (replaying the WAL), checks row counts, and exercises the statement
# classes that failed during the 2026-06 index-corruption incident. A backup
# that exists but cannot be opened is not a backup — and a failing verify is
# also the earliest available signal of silent on-disk corruption.
#
# Complements (does not replace) disk-level PD snapshots: a snapshot
# preserves a corrupted file faithfully; this catches the corruption.
#
# Keeps 7 daily backups. FAILED verify -> webhook alert (shared config in
# /etc/agnes-watchdog.env). Runs as root via agnes-db-backup.timer.
#
# Postgres coverage: when the deployment has migrated to the on-VM Postgres
# side-car (instance.yaml::database.backend == side_car — same persisted
# field startup-script.sh.tpl reads to pick the compose overlay, plus a
# container-presence check so a stale/mid-migration instance.yaml can't
# trigger a dump against a container that isn't actually up), ALSO pg_dump
# the "agnes" control-plane database into the same dated backup dir and
# restore-canary it: restore the dump into a disposable scratch database
# inside the SAME container, run a trivial `SELECT count(*) FROM users`,
# then drop the scratch database. No password is needed for any of this —
# docker exec talks to the container over its local unix socket, and the
# postgres:16-alpine entrypoint always writes a "local ... trust" pg_hba.conf
# line regardless of POSTGRES_HOST_AUTH_METHOD (the same reason
# agnes-state-applier.sh's `pg_isready -U agnes` needs no password). Same
# 7-day retention and webhook as the DuckDB path; a FAILED pg canary alerts
# the same way a FAILED DuckDB verify does.
#
# Forward reference: once the DuckLake catalog lands (a later wave), its
# metadata lives in this same Postgres instance, so this pg_dump already
# covers it — no separate backup path will be needed then.
set -u
[ -f /etc/agnes-watchdog.env ] && . /etc/agnes-watchdog.env
WEBHOOK_URL="${WEBHOOK_URL:-}"
# Overridable only for the bash test harness (tests/test_db_backup_pg_canary.sh)
# — production always uses the defaults below.
INSTANCE_YAML="${AGNES_DB_BACKUP_INSTANCE_YAML:-/data/state/instance.yaml}"
POSTGRES_CONTAINER="${AGNES_DB_BACKUP_POSTGRES_CONTAINER:-agnes-postgres-1}"
HOST=$(hostname)
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

# --- Postgres side-car detection + pg_dump/restore-canary helpers ---------
#
# Mirrors startup-script.sh.tpl's PERSISTED_BACKEND check: read
# instance.yaml::database.backend with the same sed pattern, then confirm
# the side-car container is actually up (a backend value can persist across
# a reboot or mid-migration state even when the container isn't running).
pg_backend_active() {
    local backend
    backend=$(sed -n 's/^[[:space:]]*backend:[[:space:]]*//p' "$INSTANCE_YAML" 2>/dev/null | tr -d '"' | head -1)
    [ "$backend" = "side_car" ] || return 1
    docker ps --format '{{.Names}}' 2>/dev/null | grep -Fxq "$POSTGRES_CONTAINER"
}

# pg_dump the control-plane DB in custom format (-F c) — pg_restore-able and
# compressed. Runs via `docker exec` against the side-car's own pg_dump
# binary; no host-side postgres client is required. See the header comment
# for why no password is passed.
pg_dump_backup() {
    local dest="$1"
    docker exec "$POSTGRES_CONTAINER" pg_dump -U agnes -F c agnes \
        > "$dest/agnes-postgres.dump" 2> "$dest/pg-backup.log"
}

# Restore-canary: prove the dump just taken is actually restorable — the
# same philosophy as agnes-db-verify.py for the DuckDB file. Restores into a
# disposable database inside the SAME container (the live "agnes" DB is
# never touched), runs one trivial sanity query, then drops the scratch DB
# regardless of outcome.
pg_restore_canary() {
    local dump_file="$1"
    local canary_db="agnes_backup_canary"
    local rc=0

    # Belt-and-braces: drop any scratch DB left behind by a crashed
    # previous run before (re-)creating it.
    docker exec "$POSTGRES_CONTAINER" dropdb -U agnes --if-exists "$canary_db" >/dev/null 2>&1 || true

    if ! docker exec "$POSTGRES_CONTAINER" createdb -U agnes "$canary_db"; then
        return 1
    fi

    if ! docker exec -i "$POSTGRES_CONTAINER" pg_restore -U agnes -d "$canary_db" --no-owner < "$dump_file"; then
        docker exec "$POSTGRES_CONTAINER" dropdb -U agnes --if-exists "$canary_db" >/dev/null 2>&1 || true
        return 1
    fi

    local count
    count=$(docker exec "$POSTGRES_CONTAINER" psql -U agnes -d "$canary_db" -tAc 'SELECT count(*) FROM users;' 2>/dev/null | tr -d '[:space:]')
    if ! [[ "$count" =~ ^[0-9]+$ ]]; then
        rc=1
    fi

    docker exec "$POSTGRES_CONTAINER" dropdb -U agnes --if-exists "$canary_db" >/dev/null 2>&1 || true
    return "$rc"
}

TS=$(date -u +%Y%m%d)
ROOT=/data/backups/system-duckdb
DEST=$ROOT/$TS
mkdir -p "$DEST"
# A Postgres-backend instance may have no system.duckdb at all — born on
# Postgres, or the frozen post-migration snapshot was retired. Nothing to
# back up is not a failed backup: skip the copy (and the verify below picks
# SKIPPED); the pg_dump + restore-canary half still runs.
if [ -f /data/state/system.duckdb ]; then
    cp /data/state/system.duckdb "$DEST/system.duckdb"
    [ -f /data/state/system.duckdb.wal ] && cp /data/state/system.duckdb.wal "$DEST/system.duckdb.wal"
fi
# Postgres pg_dump + restore-canary — only when the side-car backend is
# active; DuckDB-backend and cloud-backend deployments skip this entirely
# and behave exactly as before. Written into the same $DEST as
# system.duckdb so the retention sweep below covers it for free.
PG_STATUS=""
if pg_backend_active; then
    if pg_dump_backup "$DEST" && pg_restore_canary "$DEST/agnes-postgres.dump" > "$DEST/pg-verify.log" 2>&1; then
        PG_STATUS=OK
    else
        PG_STATUS=FAILED
    fi
    echo "$PG_STATUS $(date -u +%FT%TZ)" > "$DEST/PG_STATUS"
fi

# uid:gid 999:999 = the container's non-root user; the verify step runs
# inside the app container (where a known-good duckdb is installed).
chown -R 999:999 "$ROOT"

if [ ! -f "$DEST/system.duckdb" ]; then
    # No source file was copied above. Whether that is legitimate depends on
    # the BACKEND, not on the file's absence alone:
    #
    #  - Postgres side-car active -> nothing to back up on the DuckDB side
    #    (born on Postgres, or the frozen post-migration snapshot retired).
    #    A genuine skip; recorded honestly instead of verifying a file that
    #    does not exist, which would read FAILED and page every run.
    #
    #  - No side-car (DuckDB or cloud backend) -> system.duckdb IS the app
    #    state, so its absence is the catastrophe this unit exists to catch
    #    (a deleted file, a broken mount, an unmounted disk). Calling that
    #    SKIPPED would exit 0 and never fire the webhook, turning the daily
    #    backup alarm off exactly when it matters.
    if pg_backend_active; then
        STATUS=SKIPPED
    else
        STATUS=FAILED
        echo "system.duckdb missing at /data/state/system.duckdb and no Postgres side-car is active" \
            > "$DEST/verify.log"
    fi
elif docker exec agnes-app-1 python /data/backups/agnes-db-verify.py \
        "$DEST/system.duckdb" > "$DEST/verify.log" 2>&1; then
    STATUS=OK
else
    STATUS=FAILED
fi
echo "$STATUS $(date -u +%FT%TZ)" > "$DEST/STATUS"

find "$ROOT" -maxdepth 1 -type d -name '20*' -mtime +7 -exec rm -rf {} \;

if [ -n "$PG_STATUS" ]; then
    MSG="[agnes-db-backup] $LABEL | $TS verify=$STATUS pg=$PG_STATUS size=$(du -sh "$DEST" | cut -f1)"
else
    MSG="[agnes-db-backup] $LABEL | $TS verify=$STATUS size=$(du -sh "$DEST" | cut -f1)"
fi
logger -t agnes-db-backup "$MSG"
echo "$MSG" >> /var/log/agnes-watchdog.log
if { [ "$STATUS" = "FAILED" ] || [ "$PG_STATUS" = "FAILED" ]; } && [ -n "$WEBHOOK_URL" ]; then
    # Same JSON escaping as agnes-watchdog.sh — $MSG embeds the
    # operator-configurable ENV_LABEL, and this is the one alert that must
    # not silently break on a quote or backslash.
    esc=$(printf '%s' "$MSG — see $DEST/verify.log" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk '{printf "%s\\n", $0}')
    curl -sf -m 10 -X POST -H 'Content-Type: application/json' \
        -d "{\"text\": \"$esc\"}" "$WEBHOOK_URL" >/dev/null 2>&1 \
        || logger -t agnes-db-backup "webhook send failed"
fi
# SKIPPED counts as success for the exit code: it is the designed outcome
# for an instance with no DuckDB state, and the systemd unit's failure
# state should mean "a backup that should exist could not be produced".
if [ -n "$PG_STATUS" ]; then
    { [ "$STATUS" = "OK" ] || [ "$STATUS" = "SKIPPED" ]; } && [ "$PG_STATUS" = "OK" ]
else
    # No side-car: STATUS can only be OK or FAILED here — the branch above
    # never records SKIPPED without an active Postgres backend.
    [ "$STATUS" = "OK" ]
fi
