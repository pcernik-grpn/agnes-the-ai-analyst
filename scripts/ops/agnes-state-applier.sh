#!/bin/bash
# Host-side daemon for the DB-backend state machine.
#
# Two responsibilities, both driven from /data/state:
#   1. Compose lifecycle — bring the postgres container up/down based on
#      the desired-state flag at /data/state/db-state-target.flag.
#   2. Data migration — when a job entry under /data/state/db-jobs/ is in
#      status "pending", stop the app container (releasing its DuckDB
#      file lock — the entire reason migration cannot run as an in-app
#      subprocess), run the migrator on the host with /data bind-mounted,
#      then restart the app on the new backend.
#
# Why the host runs the migrator instead of the FastAPI handler: DuckDB
# >=1.5 holds an exclusive per-process file lock on system.duckdb. Even
# explicit conn.close() + close_singleton_connections() + gc.collect()
# inside the uvicorn worker do not deterministically release that lock
# (Python keeps the file descriptor pinned until the process exits).
# Verified live on a dev instance: ``lsof`` shows the lock outlives every
# in-process release we tried. Running the migrator from the host with
# the app container fully stopped is the only path that's reliable.
#
# Runs every 30s via systemd timer. Idempotent — if there is no pending
# job and the lifecycle matches the flag, it exits without doing
# anything.
set -euo pipefail

FLAG=/data/state/db-state-target.flag
JOBS_DIR=/data/state/db-jobs
COMPOSE_DIR=/opt/agnes
LOCK_FILE=/data/state/db-state-applier.lock

# Prevent concurrent applier runs (the timer can fire while a previous
# tick is still mid-migration; flock returns immediately if held).
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

# --- Applier heartbeat (Phase 4) -----------------------------------------
# Touch a tick file so /api/admin/db/state can expose
# ``applier_last_tick_age_s`` for UI liveness. Fired on EVERY
# invocation including no-op ticks so the value stays fresh during
# idle periods. None / missing tick = applier has never run (fresh
# install, broken unit, OS reboot wiped the systemd target).
# State dir is guaranteed to exist (LOCK_FILE lives there), so the
# mkdir is just a defensive guard.
mkdir -p "$(dirname "$LOCK_FILE")"
touch /data/state/agnes-state-applier.tick || true

if [ ! -f "$FLAG" ]; then
    exit 0
fi
TARGET="$(tr -d '[:space:]' < "$FLAG")"

cd "$COMPOSE_DIR"
# Read only the one infra-controlled key this daemon needs from .env
# (AGNES_TAG, for the migrator image below), rather than bash-sourcing the
# whole file. The .env also holds free-text app config
# (AGNES_INSTANCE_CUSTOM_PREAMBLE, AGNES_INSTANCE_BRAND, …) whose values can
# contain shell metacharacters (backticks, `>`, `$`, quotes) — `. .env`
# executes those and aborts under `set -e`, which on this cutover daemon
# would wedge the state machine mid-flip. Everything else here comes from
# the job JSON (read below) or explicit `docker run -e`/CLI args; docker
# compose parses .env with its own safe parser. The VALUE is never
# shell-evaluated.
_env_get() {
  grep -m1 -E "^$1=" "$COMPOSE_DIR/.env" 2>/dev/null \
    | sed -e "s/^$1=//" -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" || true
}
# Assign separately from `export` (SC2155: avoid masking the substitution's rc).
AGNES_TAG="$(_env_get AGNES_TAG)"
export AGNES_TAG
# AGNES_IMAGE_REPO: alternate registry/repository for the app image (see
# agnes-auto-upgrade.sh). Exported for docker compose interpolation; the
# migrator IMAGE below applies the same default the compose files use.
AGNES_IMAGE_REPO="$(_env_get AGNES_IMAGE_REPO)"
export AGNES_IMAGE_REPO

# Compose chain reused for every invocation, resolved through the single
# shared resolver (scripts/ops/agnes-compose-file.sh) so this daemon can
# never disagree with agnes-auto-upgrade.sh / startup-script.sh.tpl on the
# overlay list or its order again (a hardcoded array here previously
# appended the postgres overlays AFTER docker-compose.host-mount.yml,
# the inverse of the order docker-compose.postgres-host-mount.yml's
# !override needs).
#
# TARGET (the lifecycle flag), not instance.yaml's database.backend, is
# the authoritative input HERE: the flag flips to side-car-enabled (and
# the postgres container must come up) BEFORE instance.yaml leaves the
# transient *_in_progress value — see agnes-compose-file.sh's docstring.
#
# Absence is handled rather than crashed through: under `set -e` a missing
# file here aborts the tick before the heartbeat is even interpretable, so
# the operator sees a failing timer with no statement of what is wrong. The
# resolver ships in the image (Dockerfile bakes it into
# /opt/agnes-host/scripts/ops/), so the only way to be without one is a VM
# whose $APP_DIR predates it — mid-upgrade, until the next boot or the next
# agnes-auto-upgrade.sh tick re-fetches it. Say so and exit cleanly; the
# timer is back in 30s. Every decision below depends on the resolver, so
# improvising a partial overlay list would be worse than waiting.
RESOLVER="$COMPOSE_DIR/scripts/ops/agnes-compose-file.sh"
if [ ! -f "$RESOLVER" ]; then
    logger -t agnes-state-applier "ERROR: $RESOLVER missing — skipping this tick; the stack is left exactly as it is"
    exit 0
fi
# shellcheck source=./agnes-compose-file.sh
. "$RESOLVER"
case "$TARGET" in
    side-car-enabled) _ACF_BACKEND=side_car ;;
    *) _ACF_BACKEND=duckdb ;;
esac
export COMPOSE_FILE
COMPOSE_FILE=$(agnes_resolve_compose_file "$COMPOSE_DIR" /data/state "$_ACF_BACKEND")
dc() { docker compose "$@"; }

# --- Pending-job detection ------------------------------------------------
# A job file with status=pending is the signal that the API endpoint
# wants us to actually MIGRATE data, not just shift lifecycle. We pick
# the oldest pending — there should usually only be one because the
# API holds the MigrationLock until it has written the job.
# Sort by mtime so we maintain FIFO ordering if two jobs queued up
# (applier missed a tick, operator submitted back-to-back requests).
#
# H8 — Pending-job expiry. A pending job whose ``queued_at`` is older
# than PENDING_JOB_MAX_AGE_SEC (default 3600s = 1h) is marked failed
# without being processed: the operator may have masked the timer,
# queued a migration, manually fixed state via the CLI, then unmasked
# weeks later — running an old intent against now-incompatible current
# state would be worse than dropping the request. Expiry runs BEFORE
# the candidate scan so an expired pending is excluded from selection.
PENDING_JOB_MAX_AGE_SEC=${PENDING_JOB_MAX_AGE_SEC:-3600}
PENDING_JOB=""
if [ -d "$JOBS_DIR" ]; then
    PENDING_JOB=$(python3 - "$JOBS_DIR" "$PENDING_JOB_MAX_AGE_SEC" <<'PY' 2>/dev/null
import json, os, sys, time
from datetime import datetime, timezone
d = sys.argv[1]
max_age = int(sys.argv[2])
now = datetime.now(timezone.utc)
candidates = []
for f in os.listdir(d):
    if not f.endswith(".json"):
        continue
    p = os.path.join(d, f)
    try:
        data = json.load(open(p))
    except Exception:
        continue
    if data.get("status") != "pending":
        continue
    queued_at = data.get("queued_at")
    age = None
    if queued_at:
        try:
            age = (now - datetime.fromisoformat(queued_at)).total_seconds()
        except Exception:
            age = None
    # No queued_at (pre-H8 jobs) or unparseable timestamp — fall back to
    # filesystem mtime so the expiry guard still bites on legacy files.
    if age is None:
        age = now.timestamp() - os.path.getmtime(p)
    if age > max_age:
        # Atomic-rewrite as failed/expired so the next tick (or the
        # API status endpoint) sees the terminal state.
        data["status"] = "failed"
        data.setdefault("error", {})
        data["error"]["step"] = "queued"
        data["error"]["class"] = "PendingJobExpired"
        data["error"]["message"] = (
            f"pending job expired (queued {int(age)}s ago, threshold {max_age}s); "
            "applier refuses to run stale intent against potentially-divergent state"
        )
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        # H2-NEW: the tmp inherits umask 0644, so it needs 0600 — applied
        # HERE rather than to `p` after the rename, which would leave the
        # job file (its error messages can quote a database url) observable
        # at the umask default for the window between the two calls.
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        continue
    candidates.append((os.path.getmtime(p), p))
candidates.sort()
print(candidates[0][1] if candidates else "")
PY
    ) || PENDING_JOB=""
fi

# --- Helpers --------------------------------------------------------------
update_job() {
    # Set status + optional error.message on a job file. Atomic via
    # tmp+rename so the API endpoint never reads half-written JSON.
    #
    # A 4th argument of "append" keeps whatever message is already on the
    # job and adds this one after it. The default REPLACES, which is right
    # for the first terminal write but destroys evidence on a second: a
    # migration that failed and whose rollback then also failed would end up
    # recording only the rollback, and the operator would have to go to
    # journalctl to find out why the migration failed at all.
    local file=$1 status=$2 error=${3:-} mode=${4:-replace}
    python3 - <<PY "$file" "$status" "$error" "$mode"
import json, os, sys
p, status, err, mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(p) as fh:
    data = json.load(fh)
data["status"] = status
if err:
    data.setdefault("error", {})
    prev = data["error"].get("message") or ""
    if mode == "append" and prev and err not in prev:
        data["error"]["message"] = f"{prev} | {err}"
    else:
        data["error"]["message"] = err
    data["error"].setdefault("step", data.get("current_step", "unknown"))
tmp = p + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2)
# H2-NEW: the tmp inherits umask 0644, so it needs 0600 — applied
# HERE rather than to `p` after the rename, which would leave the
# job file (its error messages can quote a database url) observable
# at the umask default for the window between the two calls.
os.chmod(tmp, 0o600)
os.replace(tmp, p)
PY
}

_instance_yaml_target_mode() {
    # Echo the mode (octal, no leading zero) this host may give the overlay.
    #
    # 0600 has a precondition, and it is the SAME one the provisioning script
    # gates its own `chmod 600` on (#1217,
    # infra/modules/customer-instance/startup-script.sh.tpl): the file must
    # come to rest owned by the app container's uid, because 0600 grants
    # nothing to anyone else. `os.replace`/`mv` re-owns the file to whoever
    # writes it — this process — or to agnes-applier, when we run as root and
    # the chown after the rename can still move it. So the mode has to follow
    # the account the file lands on, not the process doing the writing.
    #
    # Tightening unconditionally is what turned #1217's documented,
    # availability-safe degradation into an outage: on a host where
    # agnes-applier did not get the app's uid, the first rewrite left the
    # overlay 0600 under an uid the app is not, and the app's fail-closed
    # strict read (app/main.py) then refuses to start the instance.
    # PRESERVING the existing mode — this fix's first shape — fails the same
    # way one rewrite later: the app's own writers
    # (src/db_state_machine.py::write_backend_state, the admin config
    # editors) run as uid 999 INSIDE the container, where owner-only is
    # exactly right, and chmod their temp 0600 unconditionally — so on a
    # mismatched host the overlay is routinely 0600-owned-by-999 before this
    # daemon touches it, and carrying that mode across a rename that re-owns
    # the file strands it all over again. And on a fresh overlay "preserve"
    # bottomed out in a stat-fallback of 644 that CREATED the file
    # world-readable with the database url inline. Both halves found by
    # Devin Review on #1298.
    #
    # So on a mismatched host the mode is never owner-only and never carried
    # over. The app's read is granted through its GROUP instead: 0640 with
    # gid 999 — the app container's primary group, same number as the uid
    # (Dockerfile `useradd --system --uid 999 … agnes` + `USER agnes`;
    # provisioning's `chown 999:999` leans on the same pair) — wherever this
    # process can actually hand the file to that group. That is probed on a
    # scratch file, because the group re-assert after the rename is
    # best-effort and a 0640 whose grant never landed is the same lockout
    # 0600 was. Where the grant is not possible (the common shape: a
    # non-root applier on an allocated uid with no gid-999 membership) the
    # mode falls back to 0644 — the same availability-first degradation
    # provisioning documents for this host state
    # (docs/postgres-cutover-runbook.md), now a conscious, logged decision
    # instead of a stat default.
    local path=$1
    # The app container's uid (and primary gid — see above). Kept next to
    # its consumers; the provisioning side has its own AGNES_APPLIER_UID for
    # the same number.
    local app_uid=999
    local me applier_uid final_uid probe
    me=$(id -u)
    applier_uid=$(id -u agnes-applier 2>/dev/null || echo "")
    if [ "$me" = "0" ] && [ -n "$applier_uid" ]; then
        final_uid=$applier_uid
    else
        final_uid=$me
    fi
    if [ "$final_uid" = "$app_uid" ]; then
        echo 600
        return 0
    fi
    probe=$(mktemp "${path}.gidprobe.XXXXXX" 2>/dev/null || echo "")
    if [ -n "$probe" ] && chown ":${app_uid}" "$probe" 2>/dev/null; then
        rm -f "$probe" 2>/dev/null || true
        logger -t agnes-state-applier -p user.warning \
            "agnes-applier resolves to uid ${final_uid}, not ${app_uid} (the app container's) — writing ${path} at mode 0640 with group ${app_uid} instead of owner-only 0600, so the app can still read its own config through its gid. The database url and any connector credentials in that file stay readable beyond their owner until the two uids agree; see docs/postgres-cutover-runbook.md for the remediation." 2>/dev/null || true
        echo 640
        return 0
    fi
    rm -f "$probe" 2>/dev/null || true
    logger -t agnes-state-applier -p user.warning \
        "agnes-applier resolves to uid ${final_uid}, not ${app_uid} (the app container's), and this process cannot hand ${path} to group ${app_uid} — writing it at mode 0644 so the app can still read its own config. The database url and any connector credentials in that file are world-readable until the two uids agree; see docs/postgres-cutover-runbook.md for the remediation." 2>/dev/null || true
    echo 644
}

_instance_yaml_reassert_owner() {
    # The post-rename ownership re-assert, one implementation for both
    # writer routes below so they cannot drift. Normally a no-op — the
    # atomic rename already carries the temp file's ownership (this
    # process's own uid, since the script runs as User=agnes-applier) onto
    # the overlay. By NAME rather than a hardcoded uid on purpose — it
    # self-corrects to whatever agnes-applier resolves to on this host
    # instead of baking in a number that could drift from the provisioning
    # pin (#1217, see infra/modules/customer-instance/startup-script.sh.tpl).
    #
    # At mode 0640 the app's read grant IS the group, so the re-assert must
    # land on the app's gid (999 — the uid/gid pair _instance_yaml_target_mode
    # documents): resetting the group to agnes-applier here would re-lock
    # the app out one line after the mode unlocked it. Owner+group first
    # (covers root, and agnes-applier itself when it holds gid-999
    # membership); group-only as the fallback for a custom-infra process
    # that cannot change the owner. The grant was probed in
    # _instance_yaml_target_mode, so on the 640 path one of these lands.
    # Doing the group change after the rename is safe where the H2
    # destination-chmod was not: the interim group is the writer's own,
    # which only ever grants LESS than the final state — nothing becomes
    # observable during the window.
    local path=$1 mode=$2
    if [ "$mode" = "640" ]; then
        chown "agnes-applier:999" "$path" 2>/dev/null \
            || chown ":999" "$path" 2>/dev/null || true
    else
        chown agnes-applier:agnes-applier "$path" 2>/dev/null || true
    fi
}

write_instance_yaml() {
    # Preserve all non-database top-level keys (logging, auth_providers,
    # feature_flags, etc. the operator may have set via the admin UI).
    # The previous bash heredoc approach rewrote the file from scratch and
    # silently destroyed them (B6 — review finding).
    #
    # H4-NEW: graceful fallback when PyYAML is unavailable on the host.
    # Provisioning installs python3-yaml so this fallback is defensive-only,
    # but old or stripped VMs should not wedge the state machine on a
    # missing dependency.
    local backend=$1 url=${2:-}
    local path="/data/state/instance.yaml"
    # Computed once, in bash, and handed to BOTH writers below — the PyYAML
    # route and the pure-bash fallback have to agree on this, and a second
    # implementation inside the heredoc is a drift waiting to happen.
    local mode
    mode=$(_instance_yaml_target_mode "$path")
    # Try PyYAML route first — preserves any non-database top-level keys
    # the operator set (logging, auth providers, feature flags).
    if python3 -c 'import yaml' 2>/dev/null; then
        python3 - "$path" "$backend" "$url" "$mode" <<'PY'
import os, sys, yaml
path, backend, url, mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
existing = {}
if os.path.exists(path):
    # A read that fails must NOT fall through to `existing = {}`: the whole
    # point of this branch is to carry the operator's non-database sections
    # (logging, auth providers, feature flags) across a backend switch, and
    # an empty `existing` rewrites the file with only `database:` — the exact
    # destruction the heredoc approach was replaced for. A malformed file is
    # still recoverable that way, so YAMLError keeps the old behaviour; a
    # file we are not allowed to READ is not, and became possible only once
    # instance.yaml went 0600, so it aborts and leaves the file alone.
    try:
        existing = yaml.safe_load(open(path).read()) or {}
    except OSError as exc:
        sys.exit(
            f"refusing to rewrite {path}: cannot read it ({exc}). "
            "The file is 0600; check that this process runs as its owner. "
            "Rewriting from an empty base would drop every operator-set "
            "section."
        )
    except (yaml.YAMLError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, so it reaches neither the OSError
        # arm above nor this one unless it is named — and it is the shape a
        # partial write actually produces. Left to propagate it exits non-zero,
        # which the caller now reads as a refused rollback and answers by
        # leaving app+scheduler down with nothing to bring them back. Garbled
        # bytes are a malformed file, same as unparseable YAML: recoverable by
        # rewriting, so they take the lenient path. Mirrors the split in
        # app/instance_config.py.
        existing = {}
db = dict(existing.get("database") or {})
db["backend"] = backend
if url:
    db["url"] = url
else:
    db.pop("url", None)
existing["database"] = db
tmp = path + ".tmp"
with open(tmp, "w") as f:
    yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=True)
# The mode goes on the TEMP file, before the rename — os.replace is atomic
# and carries the temp's mode, so the real path is never observable at the
# umask default. Chmodding the destination afterwards leaves a window on
# a file that holds the database url with its password inline.
#
# `mode` is 0600 only where the file will come to rest on the app
# container's uid; otherwise 0640 (the app reads via group 999, which the
# caller re-asserts after the rename) or 0644 — never an owner-only mode
# under an uid the app is not, so this rewrite cannot make the overlay
# unreadable to the app. Decided by _instance_yaml_target_mode in the
# calling script — see its comment.
os.chmod(tmp, int(mode, 8))
os.replace(tmp, path)
PY
        # Propagate the writer's exit status instead of `return` (which would
        # discard it). The abort above only protects the file if the caller
        # can tell the write did not happen — otherwise the state machine logs
        # a backend flip that never reached disk. Callers that already tolerate
        # a failed write keep their `|| true`.
        local rc=$?
        if [ "$rc" -ne 0 ]; then
            logger -t agnes-state-applier "write_instance_yaml: refused to rewrite $path (rc=$rc) — see stderr"
            return "$rc"
        fi
        _instance_yaml_reassert_owner "$path" "$mode"
        return 0
    fi
    # Pure-bash fallback. H4-NEW — preserves the database section only;
    # any non-database top-level keys are LOST. Provisioning should
    # install python3-yaml so this path is rarely hit; we keep it alive
    # so a missing dependency never wedges the state machine.
    echo "WARN: write_instance_yaml using bash fallback (PyYAML not installed); non-database top-level keys will be dropped" >&2
    local tmp="${path}.tmp"
    {
        echo "database:"
        echo "  backend: ${backend}"
        if [ -n "$url" ]; then
            # B2-NEW: emit the URL as a YAML double-quoted scalar so
            # values containing :, #, [, ], {, }, ?, &, etc. parse as
            # a single string. Escape \ and " (the only chars that need
            # escaping inside a YAML double-quoted scalar). Escape \ first
            # to avoid double-escaping the backslash just inserted for ".
            # Pre-fix the bare interpolation produced malformed YAML that
            # read_backend_state silently swallowed (B2-NEW).
            local url_escaped
            url_escaped=$(printf '%s' "$url" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')
            printf '  url: "%s"\n' "$url_escaped"
        fi
    } > "$tmp"
    # Same gate as the PyYAML route above, from the same computed value:
    # 0600 only where the app container will own the result, 0640/0644
    # otherwise.
    chmod "$mode" "$tmp"
    mv -f "$tmp" "$path"
    _instance_yaml_reassert_owner "$path" "$mode"
}

# --- Stuck-running recovery (B5 + H5-NEW) ------------------------------------
# Extracted into a function so it can be unit-tested and called cleanly.
_recover_stuck_jobs() {
    # H5-NEW + B5: jobs whose heartbeat is older than 120s are marked
    # failed AND the overlay's database.backend is restored to
    # source_backend. Without the restore, the next migration retry reads
    # ``*_in_progress`` as the current backend and the migrator rejects
    # ``source_backend='side_car_in_progress'`` → state machine wedged
    # until an operator manually edits instance.yaml. Recovery now
    # symmetrically calls write_instance_yaml(source_backend, source_url),
    # mirroring the cancel path.
    local jobs_dir="${JOBS_DIR:-/data/state/db-jobs}"
    [ -d "$jobs_dir" ] || return 0
    local now
    now=$(date +%s)
    local job_path alive_path age source_backend source_url
    local recovered_any=0
    for job_path in "$jobs_dir"/*.json; do
        [ -f "$job_path" ] || continue
        local st
        st=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" "$job_path" 2>/dev/null || echo "")
        [ "$st" = "running" ] || continue
        alive_path="${job_path%.json}.alive"
        if [ -f "$alive_path" ]; then
            age=$(( now - $(stat -c '%Y' "$alive_path" 2>/dev/null || stat -f '%m' "$alive_path") ))
        else
            age=999
        fi
        [ "$age" -gt 120 ] || continue
        # Read source_backend + source_url BEFORE we rewrite the job so
        # the values are captured from the original running record.
        source_backend=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('source_backend','') or '')" "$job_path" 2>/dev/null || echo "")
        source_url=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('source_url','') or '')" "$job_path" 2>/dev/null || echo "")
        logger -t agnes-state-applier "Stale running job $job_path: alive=${age}s, marking failed"
        update_job "$job_path" "failed" "stuck running (no heartbeat for ${age}s, host reboot / OOM / docker crash suspected)"
        # H5-NEW: restore instance.yaml from the *_in_progress placeholder
        # back to source_backend. Symmetric with the cancel path.
        # Empty source_url is correct for duckdb sources — write_instance_yaml
        # handles that by dropping the url key.
        if [ -n "$source_backend" ]; then
            # The `|| true` is kept so one unrecoverable job cannot abort the
            # sweep over the others — but the failure is no longer discarded.
            # instance.yaml still names the transient `*_in_progress` here, and
            # `use_pg()` counts both transients as Postgres, so restarting a
            # duckdb-source instance on it points the app at the database the
            # crashed migration never finished filling. An empty database that
            # accepts writes is indistinguishable from a healthy one, so the
            # restart below is withheld instead.
            if write_instance_yaml "$source_backend" "$source_url"; then
                :
            else
                # Its OWN flag, not the one step 4 reads. Shell variables have
                # no function scope here, so setting the shared one leaked this
                # decision into the rest of the tick: a pending job could then
                # migrate successfully — the migrator writes the target backend
                # itself, from its own container, before `mark_success` — and
                # step 4 would still refuse to restart on a stale marker,
                # leaving a perfectly completed migration with the app down.
                # The two gates answer different questions about different
                # writes and must not share state.
                RECOVERY_SKIP_RESTART=1
                update_job "$job_path" "failed" "instance.yaml rollback was ALSO refused — backend left at the in-progress value; app+scheduler deliberately NOT restarted" append
                logger -t agnes-state-applier "Stuck-job recovery for $job_path could not rewrite instance.yaml — backend still reads *_in_progress; app+scheduler left DOWN rather than started against a database the migration never filled. Repair instance.yaml (set database.backend to $source_backend) and start them by hand."
            fi
        fi
        rm -f "$alive_path"
        recovered_any=1
    done

    if [ "$recovered_any" -eq 1 ] && [ "${RECOVERY_SKIP_RESTART:-0}" != "1" ]; then
        # B3-NEW: After reverting instance.yaml to source state, restart
        # app + scheduler. The applier had stopped them before running
        # the migrator (line ~413 of this script). A SIGKILL/OOM/host-
        # reboot kills the migrator without re-starting them; the next
        # tick's recovery marks the job failed but exits at the
        # no-pending-job path (line ~327), leaving services DOWN
        # indefinitely. A single restart after all stuck jobs are handled
        # (not per-job) mirrors the success path at line ~546.
        logger -t agnes-state-applier "Recovery restart: bringing app + scheduler back up after stuck-job recovery"
        set +e
        dc up -d --no-deps --force-recreate app scheduler 2>&1 \
            | logger -t agnes-state-applier || true
        set -e
    elif [ "$recovered_any" -eq 1 ]; then
        logger -t agnes-state-applier "Recovery restart WITHHELD — instance.yaml still names an in-progress backend; see the failure logged above"
    fi
}

_recover_stuck_jobs

# --- Lifecycle: ensure postgres container matches the flag ----------------
case "$TARGET" in
    side-car-enabled)
        # B4-NEW tightening: bootstrap unit (root) is responsible for
        # `mkdir -p /data/postgres && chown 70:70 /data/postgres`.
        # startup-script.sh.tpl also pre-creates it via `install -d` at
        # provision time. The applier (non-root, agnes-applier) must NOT
        # attempt chown here — it would fail under set -e and abort the
        # tick on any fresh VM where the directory is still root-owned.
        # We only STAT and warn; actual chown belongs to the bootstrap unit.
        if [ ! -d /data/postgres ]; then
            echo "ERR: /data/postgres missing — bootstrap unit failed?" >&2
            exit 1
        fi
        if [ "$(stat -c '%u:%g' /data/postgres 2>/dev/null || echo '')" != "70:70" ]; then
            echo "WARN: /data/postgres ownership not 70:70; bootstrap unit may have failed; postgres container may refuse to start" >&2
        fi
        if ! docker ps --format '{{.Names}}' | grep -q '^agnes-postgres-1$'; then
            dc up -d postgres
            # Wait for postgres to accept connections — the migrator
            # we'll launch in a moment opens a TCP connection on
            # postgres:5432 and we'd rather fail fast here than have
            # the migrator timeout on its first ALEMBIC operation.
            PG_READY=0
            for _ in $(seq 1 30); do
                if docker exec agnes-postgres-1 pg_isready -U agnes >/dev/null 2>&1; then
                    PG_READY=1
                    break
                fi
                sleep 2
            done
            if [ "$PG_READY" -ne 1 ]; then
                logger -t agnes-state-applier "postgres did not become ready within 60s — aborting"
                exit 1
            fi
        fi
        ;;
    duckdb|cloud-only)
        # Tear down side-car PG if it's running — but only when there's
        # no pending job, otherwise we'd kill the migrator's source DB
        # before it can read from it.
        if [ -z "$PENDING_JOB" ] && docker ps --format '{{.Names}}' | grep -q '^agnes-postgres-1$'; then
            docker stop agnes-postgres-1 >/dev/null 2>&1 || true
            docker rm   agnes-postgres-1 >/dev/null 2>&1 || true
        fi
        ;;
esac

# --- Run migrator if there's a pending job --------------------------------
if [ -z "$PENDING_JOB" ]; then
    exit 0
fi

logger -t agnes-state-applier "Picked up pending migration job: $PENDING_JOB"

# E.5 — structured rollback on unexpected abort.
# When a python heredoc inside this script raises mid-execution,
# ``set -e`` aborts before we can run the post-migrator update_job /
# write_instance_yaml block. Pre-fix the pending job stayed at
# ``pending`` forever and the next applier tick re-picked it (or
# the H8 expiry caught it 1h later). Post-fix the ERR trap
# idempotently marks the pending job failed and reverts
# instance.yaml::backend to the source state so /api/admin/db/state
# never reports a *_in_progress that won't progress.
__rollback() {
    local rc=$?
    local trapped_at=${BASH_LINENO[0]:-?}
    [ -n "${PENDING_JOB:-}" ] || return $rc
    # Best-effort: any failure inside the trap is swallowed so we
    # don't recurse. Status update first; instance.yaml revert
    # second (only if we know the source backend at this point).
    update_job "$PENDING_JOB" "failed" \
        "applier aborted at line ${trapped_at} (rc=${rc}); recovering via ERR trap" \
        || true
    if [ -n "${SOURCE_BACKEND:-}" ]; then
        # H8-NEW: cloud-source rollback used to drop the url because we
        # only passed SOURCE_BACKEND. write_instance_yaml interprets a
        # missing 2nd arg as "drop the key" → the next app boot then
        # tried to start with backend=cloud and no DATABASE_URL,
        # re-introducing the B4-class outage on the failure path.
        # For duckdb source, SOURCE_URL is empty — write_instance_yaml
        # already handles empty URL by dropping the key (correct).
        write_instance_yaml "$SOURCE_BACKEND" "${SOURCE_URL:-}" || true
        case "$SOURCE_BACKEND" in
            duckdb|cloud) rm -f "$FLAG" 2>/dev/null || true ;;
        esac
    fi
    logger -t agnes-state-applier \
        "Applier aborted (rc=${rc}) — rolled back job ${PENDING_JOB##*/} via ERR trap"
    return $rc
}
trap '__rollback' ERR

# Read all required job fields in a single python invocation. The
# fields land into shell vars via `read`; missing optional fields are
# emitted as empty strings. Newline-separated output + `read -r` is
# more shell-safe than a single space-separated line — URLs contain
# special chars that don't survive whitespace tokenization cleanly.
#
# B1-NEW: also read target_url_pinned_ip and source_url_pinned_ip
# (schema_version=2 fields). v1 jobs written before this fix was
# deployed will emit empty strings for the pinned fields; the
# EFFECTIVE variables below fall back to the hostname URLs in that case.
{ read -r JOB_ID; read -r TARGET_URL; read -r TARGET_URL_PINNED_IP; read -r TARGET_BACKEND; read -r SOURCE_BACKEND; read -r SOURCE_URL; read -r SOURCE_URL_PINNED_IP; } < <(
    python3 - "$PENDING_JOB" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d["job_id"])
print(d.get("target_url", ""))
print(d.get("target_url_pinned_ip", "") or "")   # B1-NEW: pinned IP if present
print(d.get("target_backend", ""))
print(d.get("source_backend", ""))
print(d.get("source_url", "") or "")
print(d.get("source_url_pinned_ip", "") or "")   # B1-NEW: pinned IP if present
PY
)

# B1-NEW: prefer the pinned-IP URL when it's present in the job JSON.
# A v1 (legacy) job written before this fix lands will have empty
# strings for the pinned fields; in that case we fall back to the
# hostname URL — preserves upgrade-compatibility for jobs already
# queued but not yet applied when this fix is deployed.
#
# The alias guard below uses the original hostname URLs (TARGET_URL /
# SOURCE_URL) because _urls_alias already performs its own DNS
# resolution internally — passing the already-resolved IP would
# work too, but using the canonical hostname URLs keeps the guard
# logic consistent with the API-side check.
TARGET_URL_EFFECTIVE="${TARGET_URL_PINNED_IP:-$TARGET_URL}"
SOURCE_URL_EFFECTIVE="${SOURCE_URL_PINNED_IP:-$SOURCE_URL}"

IMAGE="${AGNES_IMAGE_REPO:-ghcr.io/keboola/agnes-the-ai-analyst}:${AGNES_TAG:-stable}"
SOURCE_URL_ARGS=()
if [ -n "$SOURCE_URL_EFFECTIVE" ]; then
    SOURCE_URL_ARGS=( --source-url "$SOURCE_URL_EFFECTIVE" )
fi

# B2-NEW — applier-side alias guard.
# The API endpoint checks _urls_alias before writing the job; this
# guard catches jobs written before B2-NEW was deployed (old pending
# jobs in the queue) where ``postgres`` (compose service name) resolved
# to the same IP as an explicit ``172.18.0.x`` cloud_url, bypassing
# the string-only guard.  Call the same Python implementation so the
# logic stays centralised.
# Uses the original hostname URLs (TARGET_URL / SOURCE_URL) — _urls_alias
# resolves hostnames internally, so passing pinned IPs would also work,
# but using canonical hostnames keeps the guard semantics identical to
# the API-side check.
if [ -n "$SOURCE_URL" ] && [ -n "$TARGET_URL" ]; then
    ALIAS_RESULT=$(python3 - "$SOURCE_URL" "$TARGET_URL" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "/app")
try:
    from app.api.db_state import _urls_alias
    print("ALIAS" if _urls_alias(sys.argv[1], sys.argv[2]) else "DISTINCT")
except Exception:
    print("DISTINCT")
PY
) || ALIAS_RESULT="DISTINCT"
    if [ "$ALIAS_RESULT" = "ALIAS" ]; then
        update_job "$PENDING_JOB" "failed" \
            "applier alias guard (B2-NEW): source and target URL alias the same Postgres database — refusing to migrate onto self"
        logger -t agnes-state-applier \
            "Migration job $JOB_ID aborted: source and target alias the same DB (B2-NEW guard)"
        exit 0
    fi
fi

# 1. Stop the app + scheduler so DuckDB releases the file lock.
docker stop agnes-app-1 agnes-scheduler-1 >/dev/null 2>&1 || true

# 2. Run the migrator on the host with /data bind-mounted. --network
#    agnes_default is needed so 'postgres' resolves for the side_car
#    target_url; safe to pass even when the postgres container is
#    absent (the migrator's verify step uses the target URL, so for
#    cloud the URL is reachable from outside the compose network).
NETWORK_ARGS=()
if docker network ls --format '{{.Name}}' | grep -q '^agnes_default$'; then
    NETWORK_ARGS=( --network agnes_default )
fi

# C.2 — bound the migrator subprocess wall-clock. Engine-side
# statement_timeout (set in _bounded_engine) only caps SQL queries; a
# hung migrator that doesn't reach a step boundary (e.g. wedged DuckDB
# connection holding the GIL) would otherwise sit forever. coreutils
# `timeout(1)` is universally available on customer-instance VMs.
# Exit codes from `timeout`:
#   124 — TERM fired (limit exceeded)
#   137 — KILL fired (TERM ignored, --kill-after kicked in)
# Both indicate the watchdog triggered.
MIGRATOR_TIMEOUT_SEC=${MIGRATOR_TIMEOUT_SEC:-1800}
set +e
timeout --signal=TERM --kill-after=30 "$MIGRATOR_TIMEOUT_SEC" \
    docker run --rm \
        ${NETWORK_ARGS[@]+"${NETWORK_ARGS[@]}"} \
        -v /data:/data \
        -e DATA_DIR=/data \
        "$IMAGE" \
        python -m scripts.db_state_migrator \
            --job-id   "$JOB_ID" \
            --to       "$TARGET_BACKEND" \
            --source-backend "$SOURCE_BACKEND" \
            --target-url "$TARGET_URL_EFFECTIVE" \
            ${SOURCE_URL_ARGS[@]+"${SOURCE_URL_ARGS[@]}"} \
            --duckdb-path /data/state/system.duckdb \
            --jobs-dir   "$JOBS_DIR" \
            --backups-dir /data/state/backups
MIG_RC=$?
set -e
if [ "$MIG_RC" -eq 124 ] || [ "$MIG_RC" -eq 137 ]; then
    update_job "$PENDING_JOB" "failed" \
        "migrator subprocess exceeded ${MIGRATOR_TIMEOUT_SEC}s timeout (rc=${MIG_RC} — watchdog fired)"
    logger -t agnes-state-applier \
        "Migration job $JOB_ID — migrator subprocess timed out after ${MIGRATOR_TIMEOUT_SEC}s"
fi

# 3. Decide post-migration lifecycle based on whether the migrator updated
#    its job file to success. (The migrator owns the JSON during its
#    invocation; if it crashed without writing we set a generic failure.)
FINAL_STATUS=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("status",""))' "$PENDING_JOB")
if [ "$FINAL_STATUS" = "pending" ] || [ -z "$FINAL_STATUS" ]; then
    update_job "$PENDING_JOB" "failed" "migrator process exited with rc=$MIG_RC without writing a terminal status"
    FINAL_STATUS="failed"
fi

if [ "$FINAL_STATUS" = "success" ]; then
    # Only claim the flip if it reached disk. write_instance_yaml now refuses
    # to rewrite a file it cannot read rather than rebuilding it from an empty
    # base, so a failure here means the backend on disk is still the source —
    # logging "flipped" would send an operator looking in the wrong place.
    if write_instance_yaml "$TARGET_BACKEND" "$TARGET_URL"; then
        logger -t agnes-state-applier "Migration job $JOB_ID succeeded — flipped instance.yaml backend to $TARGET_BACKEND"
    else
        # NOT marked failed, and the job status is deliberately left alone.
        # This write is not the flip: the migrator itself calls
        # `write_backend_state(target_state, url=target_url)` immediately
        # before `mark_success` (scripts/db_state_migrator.py), so by the time
        # FINAL_STATUS reads "success" instance.yaml ALREADY names the target
        # — and it must, since that write goes to the same directory, so a
        # migrator that could not write it would not have succeeded. What runs
        # here is a normalization of `database.url` from the migrator's
        # pinned-IP form back to the canonical hostname. Losing it leaves the
        # instance on the right backend with a less friendly url, which is a
        # working state; stamping the job "failed" over the migrator's own
        # terminal status would report a completed migration as a failure and
        # send the operator looking for data that moved perfectly well.
        #
        # $FLAG likewise stays at the TARGET lifecycle — the data is there and
        # the side-car must keep running.
        logger -t agnes-state-applier "Migration job $JOB_ID succeeded; instance.yaml url normalization FAILED — backend is $TARGET_BACKEND as the migrator left it, but database.url still carries the migrator's form rather than the canonical one. Job status left as success; repair the url by hand if it matters."
    fi
else
    logger -t agnes-state-applier "Migration job $JOB_ID failed — leaving backend on $SOURCE_BACKEND"
    # Roll the state machine back so the next /api/admin/db/state read
    # shows the (non-transient) source backend, not *_in_progress.
    # H8-NEW: also pass SOURCE_URL on the failed-migration path
    # so cloud-source rollbacks don't wipe the url. Same B4-class
    # outage class as the __rollback site.
    # Guarded, not bare. This runs under `set -euo pipefail` with
    # `trap '__rollback' ERR`, and write_instance_yaml can now exit
    # non-zero, so a bare call would abort the script right here — before
    # the flag handling below and before step 4 brings app+scheduler back
    # up. A failed migration would then take the instance offline and stay
    # there: `_recover_stuck_jobs` only repairs jobs still in status
    # `running`, and this one is already `failed`. That is the exact
    # outage class the comment above names, so the rollback path must be
    # able to fail without stopping the recovery it exists to perform.
    # One failure class must NOT be rolled back: the migrator raises
    # BackendFlipNotVerified only AFTER the rows are on the target, and it
    # deliberately leaves instance.yaml alone for exactly that reason. Writing
    # the source back here would undo that decision one layer up and point the
    # app at the old store while every row lives in the new one — recent data
    # appearing to vanish, new writes landing in the stale database. Read the
    # class the migrator recorded and honour it.
    FAIL_CLASS=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print((d.get('error') or {}).get('class','') or '')" "$PENDING_JOB" 2>/dev/null || echo "")
    if [ "$FAIL_CLASS" = "BackendFlipNotVerified" ]; then
        SKIP_APP_RESTART=1
        logger -t agnes-state-applier "Migration job $JOB_ID failed as BackendFlipNotVerified — the data IS on $TARGET_BACKEND and the migrator deliberately did not revert instance.yaml. Not reverting it here either, and not restarting app+scheduler: repair the overlay by hand, then start them."
    elif write_instance_yaml "$SOURCE_BACKEND" "${SOURCE_URL:-}"; then
        # Clear the lifecycle flag if the rollback lands on a non-PG state —
        # otherwise the next applier tick would re-trigger the postgres
        # lifecycle ("side-car-enabled" / "cloud-only") and leave an orphan
        # agnes-postgres-1 container running with no data.
        #
        # B.3 — Both duckdb and cloud sources lack a side-car lifecycle
        # need, so both must clear the flag on rollback. The asymmetric
        # original (duckdb only) silently broke cloud→side_car DR rollback:
        # instance.yaml said "cloud" but the flag still said
        # "side-car-enabled", and the next tick re-enabled the postgres
        # container. For source=side_car we keep the flag as-is because
        # "side-car-enabled" is still the correct lifecycle.
        case "$SOURCE_BACKEND" in
            duckdb|cloud)
                rm -f "$FLAG"
                ;;
        esac
    else
        # The rollback did not reach disk, so instance.yaml still names the
        # transient `*_in_progress` backend. Leave the flag alone: clearing
        # it would assert a lifecycle the file does not agree with, and the
        # pair being consistent is what the next tick reads.
        #
        # And step 4 is SKIPPED here, which corrects an earlier reading of
        # this branch. `use_pg()` counts SIDE_CAR_IN_PROGRESS and
        # CLOUD_IN_PROGRESS as Postgres (src/repositories/__init__.py), so an
        # overlay still naming the transient does not mean "the backend it
        # was already using" — with a duckdb source it points the app at a
        # Postgres that the failed migration never filled. Restarting into
        # that is worse than staying down: an empty database that accepts
        # writes is not a state anyone can tell apart from a healthy one.
        # Everything the earlier fix was actually for still happens — the
        # script does not abort mid-run, the flag handling ran, the job
        # records both failures and the log says what to do. Only the restart
        # is withheld, deliberately and out loud.
        SKIP_APP_RESTART=1
        update_job "$PENDING_JOB" "failed" "instance.yaml rollback was ALSO refused — backend left at the in-progress value; app+scheduler deliberately NOT restarted" append
        logger -t agnes-state-applier "Migration job $JOB_ID failed and the instance.yaml rollback FAILED — backend still reads *_in_progress, which resolves to Postgres; app+scheduler left DOWN rather than started against the wrong store. Repair instance.yaml (set database.backend to $SOURCE_BACKEND) and start them by hand."
    fi
fi

# 4. Bring the app back up. After-state app reads instance.yaml and
#    opens the chosen backend on startup.
#
# `--no-deps` is critical: docker-compose.postgres.yml declares the
# `migrate` and `data-migrate` services with `build: .`. On the
# customer-instance VM the source tree isn't present, so any compose
# command that follows the depends_on chain (migrate → app → scheduler)
# attempts a build, fails with `failed to read dockerfile`, and either
# leaves app+scheduler down or up with a stale config. Our state
# machine already ran the migration on the HOST via `docker run` —
# the in-compose migrate/data-migrate services are vestigial here and
# must not be touched on each cycle.
if [ "${SKIP_APP_RESTART:-0}" = "1" ]; then
    logger -t agnes-state-applier "Skipping the app+scheduler restart — see the failure logged above; instance.yaml must be repaired first"
    exit 1
fi
set +e
RESTART_LOG=$(dc up -d --no-deps --force-recreate app scheduler 2>&1)
RESTART_RC=$?
set -e
if [ "$RESTART_RC" -ne 0 ]; then
    # Don't fail the applier hard — the restart is best-effort recovery.
    # Surface the failure to journalctl so operators see it.
    logger -t agnes-state-applier "WARNING app+scheduler restart exited $RESTART_RC: $RESTART_LOG"
fi
