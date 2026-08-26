#!/bin/sh
# Single source of truth for the docker-compose overlay list
# (the `COMPOSE_FILE` value / `-f` chain every `docker compose` invocation
# on a customer-instance VM needs).
#
# Before this file existed, three independent pieces of code assembled
# this list and disagreed:
#   - infra/modules/customer-instance/startup-script.sh.tpl (VM create/
#     reboot) derived it from instance.yaml's database.backend.
#   - scripts/ops/agnes-auto-upgrade.sh (every 5 min via cron) trusted
#     whatever COMPOSE_FILE happened to already be in /opt/agnes/.env,
#     falling back to a hardcoded list that OMITTED the postgres overlays
#     when .env didn't have one.
#   - scripts/ops/agnes-state-applier.sh (the DB-backend state machine,
#     every 30s) built its own hardcoded `-f` array, appending the
#     postgres overlays AFTER docker-compose.host-mount.yml instead of
#     before docker-compose.postgres-host-mount.yml (wrong order — see
#     agnes_resolve_compose_file's docstring below).
#
# The concrete failure this caused: agnes-state-applier.sh flips
# /data/state/instance.yaml's database.backend to side_car after a
# successful DuckDB->Postgres migration, but it never touched
# /opt/agnes/.env. The very next agnes-auto-upgrade.sh tick then read
# .env's stale COMPOSE_FILE (or hit its own no-postgres fallback) and
# recreated the stack WITHOUT the postgres container, while the app was
# still configured to connect to postgres:5432. The migration only became
# durable after a full VM reboot, where the startup script recomputed
# COMPOSE_FILE from scratch. This was hit live and worked around by
# hand-editing .env.
#
# Every call site now sources this file and calls agnes_resolve_compose_file
# instead of keeping its own copy of the assembly logic.
#
# POSIX sh — sourced by bash callers (agnes-auto-upgrade.sh,
# agnes-state-applier.sh) and by the Terraform-templated startup script,
# so it must not rely on bash-only syntax (arrays, [[ ]]).
#
# Delivery: shipped from the app image at
# /opt/agnes-host/scripts/ops/agnes-compose-file.sh (Dockerfile) and
# extracted to $APP_DIR/scripts/ops/agnes-compose-file.sh by
# startup-script.sh.tpl's existing recursive
# `docker cp .../opt/agnes-host/. $APP_DIR/` (no separate extraction step
# needed). agnes-auto-upgrade.sh also re-fetches it on every tick (it is
# in CONFIG_FILES) so a running VM picks up a fix within 5 minutes, not
# only at the next reboot.

# agnes_tls_active <compose_dir> <state_dir>
#
# True (exit 0) when Caddy should front the app: both cert files are
# non-empty AND the Caddyfile is present and non-empty. Mirrors the check
# agnes-auto-upgrade.sh already applied before this file existed;
# agnes-state-applier.sh's weaker directory-only check
# (`[ -d "$state_dir/certs" ]`, which does not verify the certs actually
# have content) is folded into this single, stricter test.
agnes_tls_active() {
    _acf_cdir=$1
    _acf_sdir=$2
    [ -s "$_acf_sdir/certs/fullchain.pem" ] \
        && [ -s "$_acf_sdir/certs/privkey.pem" ] \
        && [ -s "$_acf_cdir/Caddyfile" ]
}

# agnes_gcp_logging_active <compose_dir>
#
# True (exit 0) when the GCP Cloud Logging overlay
# (docker-compose.gcp-logging.yml) should be engaged: the overlay file is
# present AND the driver probe has left its marker
# (<compose_dir>/.gcp-logging-ok). File presence alone is NOT enough:
# Docker refuses to START a container whose log driver cannot initialize,
# so arming the overlay on a host where gcplogs cannot authenticate (VM
# service account missing roles/logging.logWriter, or no GCE metadata
# server at all) turns the next routine container recreate into a full
# outage — exactly what took a production VM down for 9 minutes (#1557).
# The marker is written only by agnes_gcp_logging_probe below, and every
# builder of a COMPOSE_FILE list (the boot startup script's inline append,
# agnes-auto-upgrade.sh, agnes-state-applier.sh — all via this file) uses
# this one gate, so boot and the recurring ticks can never disagree about
# the overlay (#1558).
agnes_gcp_logging_active() {
    _acf_cdir=$1
    [ -f "$_acf_cdir/docker-compose.gcp-logging.yml" ] \
        && [ -f "$_acf_cdir/.gcp-logging-ok" ]
}

# agnes_gcp_logging_probe <compose_dir> <image>
#
# Verifies the gcplogs docker log driver can actually initialize on this
# host by starting a no-op container with `--log-driver=gcplogs` (the
# driver authenticates against the GCE metadata server at container start,
# which is exactly where an unauthorized production container failed).
# Arms or clears the shared marker (<compose_dir>/.gcp-logging-ok) that
# agnes_gcp_logging_active requires, and returns the probe's verdict.
# With the overlay file absent there is nothing to arm: the marker is
# cleared without running docker at all, so a deliberately removed overlay
# (enable_gcp_logging=false) also disarms the gate. The probe is cheap
# (`/bin/true` in the already-pulled app image, which is Debian-based) but
# not free — callers run it at boot and on ticks where the marker is
# missing, not on every resolve.
agnes_gcp_logging_probe() {
    _acf_cdir=$1
    _acf_image=$2
    if [ ! -f "$_acf_cdir/docker-compose.gcp-logging.yml" ]; then
        rm -f "$_acf_cdir/.gcp-logging-ok"
        return 1
    fi
    # Cap the probe so an unreachable metadata endpoint hangs a boot/tick
    # for at most a minute instead of indefinitely. `timeout` ships with
    # coreutils on every supported VM image; fall back to an uncapped run
    # where it is missing (dev laptops) rather than failing the probe.
    _acf_probe="docker run --rm --log-driver=gcplogs --entrypoint /bin/true"
    if command -v timeout >/dev/null 2>&1; then
        _acf_probe="timeout 60 $_acf_probe"
    fi
    if $_acf_probe "$_acf_image" >/dev/null 2>&1; then
        touch "$_acf_cdir/.gcp-logging-ok"
        return 0
    fi
    rm -f "$_acf_cdir/.gcp-logging-ok"
    return 1
}

# agnes_resolve_compose_file <compose_dir> <state_dir> [backend_override]
#
# Prints the colon-separated COMPOSE_FILE value to stdout.
#
#   compose_dir       - directory holding the docker-compose*.yml files
#                        and Caddyfile (/opt/agnes on a provisioned VM).
#   state_dir          - directory holding instance.yaml + certs/
#                        (/data/state on a provisioned VM).
#   backend_override   - optional. When given, used INSTEAD of reading
#                         state_dir/instance.yaml. agnes-state-applier.sh
#                         needs this: its db-state-target.flag flips to
#                         "side-car-enabled" (and the postgres container
#                         must come up) BEFORE instance.yaml's
#                         database.backend leaves the transient
#                         *_in_progress value, so the flag — not the
#                         persisted backend — is authoritative for that
#                         script's own lifecycle decisions.
#
# Order is significant: docker-compose.postgres-host-mount.yml is a
# bridge overlay (see its own header comment) that restates the
# data-migrate service's mount — it must load AFTER BOTH
# docker-compose.postgres.yml (which defines that service) and
# docker-compose.host-mount.yml (which deliberately does not reference
# data-migrate at all, precisely so it stays loadable without the
# postgres overlay) — so it is always last.
agnes_resolve_compose_file() {
    _acf_compose_dir=$1
    _acf_state_dir=$2
    _acf_backend=${3:-}

    if [ -z "$_acf_backend" ] && [ -f "$_acf_state_dir/instance.yaml" ]; then
        # Exact match on "side_car" only — deliberately excludes the
        # transient "side_car_in_progress": engaging the postgres overlay
        # for an in-progress migration could start (or restart) the app
        # against an empty side-car before the migrator has filled it,
        # which is indistinguishable from a healthy instance.
        _acf_backend=$(sed -n 's/^[[:space:]]*backend:[[:space:]]*//p' \
            "$_acf_state_dir/instance.yaml" 2>/dev/null | tr -d '"' | head -1)
    fi

    _acf_list="docker-compose.yml:docker-compose.prod.yml"
    if [ "$_acf_backend" = "side_car" ]; then
        _acf_list="$_acf_list:docker-compose.postgres.yml"
    fi
    _acf_list="$_acf_list:docker-compose.host-mount.yml"
    if [ "$_acf_backend" = "side_car" ]; then
        _acf_list="$_acf_list:docker-compose.postgres-host-mount.yml"
    fi

    if agnes_tls_active "$_acf_compose_dir" "$_acf_state_dir"; then
        _acf_list="$_acf_list:docker-compose.tls.yml"
    fi

    # File presence AND probe marker — see agnes_gcp_logging_active above.
    if agnes_gcp_logging_active "$_acf_compose_dir"; then
        _acf_list="$_acf_list:docker-compose.gcp-logging.yml"
    fi

    printf '%s' "$_acf_list"
}

# The closed set of overlays agnes_resolve_compose_file decides on by
# itself. Anything else in a candidate list came from the deploy layer or
# an operator (docker-compose.dispatcher.yml, written by
# startup-script.sh.tpl; docker-compose.mtier.yml, the role-split
# topology), and this file has no opinion about it — see
# agnes_compose_file_reconcile.
AGNES_MANAGED_OVERLAYS="docker-compose.yml docker-compose.prod.yml \
docker-compose.postgres.yml docker-compose.host-mount.yml \
docker-compose.postgres-host-mount.yml docker-compose.tls.yml \
docker-compose.gcp-logging.yml"

# agnes_compose_file_reconcile <resolved> <candidate>
#
# Prints the COMPOSE_FILE value a caller should actually use: <resolved>
# (authoritative, correctly ordered) plus every entry of <candidate> this
# file does not manage, appended in candidate order.
#
# Reconcile, never substitute. Both halves are load-bearing:
#
#   - Taking <resolved> wholesale would DROP the overlays only the deploy
#     layer knows about. startup-script.sh.tpl writes .env's COMPOSE_FILE
#     without docker-compose.tls.yml (TLS is engaged with `--profile tls`)
#     and without docker-compose.gcp-logging.yml, while the resolver adds
#     both from file presence — so on every TLS or GCE-logging VM the
#     candidate is "missing" something on EVERY tick, and a substitution
#     there would discard docker-compose.dispatcher.yml and any m-tier
#     docker-compose.mtier.yml each time. On an m-tier VM that also breaks
#     role detection: `docker compose config --services` stops reporting
#     worker/gateway, so the upgrade job brings up the single-container
#     `app` service alongside the running api replicas.
#
#   - Keeping <candidate> wholesale when nothing is missing would make a
#     backend flip non-durable in the other direction. After a side_car ->
#     duckdb migration, agnes-state-applier.sh rewrites instance.yaml and
#     stops the side-car but never touches .env, which still lists the
#     postgres overlays from the last boot. Those overlays are MANAGED, so
#     dropping them here is what stops the next tick from recreating
#     `postgres` + `data-migrate` and re-injecting DATABASE_URL into the
#     app — the mirror image of the bug this file was written for.
agnes_compose_file_reconcile() {
    _acf_resolved=$1
    _acf_candidate=$2
    _acf_out=$_acf_resolved
    for _acf_f in $(printf '%s' "$_acf_candidate" | tr ':' ' '); do
        # Already required by the authoritative state.
        case ":$_acf_resolved:" in
            *":$_acf_f:"*) continue ;;
        esac
        # A managed overlay the authoritative state no longer wants.
        case " $AGNES_MANAGED_OVERLAYS " in
            *" $_acf_f "*) continue ;;
        esac
        _acf_out="$_acf_out:$_acf_f"
    done
    printf '%s' "$_acf_out"
}

# agnes_compose_file_missing <resolved> <candidate>
#
# Prints (space-separated) any overlay present in <resolved> but absent
# from <candidate>. Diagnostic only — it names what a stale or
# operator-set /opt/agnes/.env COMPOSE_FILE lacks, for the log line that
# accompanies a reconcile. It is deliberately one-sided, so it must never
# be the sole gate for a decision: use agnes_compose_file_reconcile, which
# also handles the overlays a candidate carries in excess.
agnes_compose_file_missing() {
    _acf_resolved=$1
    _acf_candidate=$2
    _acf_missing=""
    for _acf_f in $(printf '%s' "$_acf_resolved" | tr ':' ' '); do
        case ":$_acf_candidate:" in
            *":$_acf_f:"*) : ;;
            *) _acf_missing="$_acf_missing $_acf_f" ;;
        esac
    done
    printf '%s' "$_acf_missing" | sed 's/^ //'
}
