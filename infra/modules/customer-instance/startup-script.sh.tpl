#!/bin/bash
# Agnes VM startup script — templated by Terraform.
# Idempotent — runs on every boot.
set -euo pipefail
exec > /var/log/agnes-startup.log 2>&1
chmod 640 /var/log/agnes-startup.log  # defense in depth — not readable by non-root

CUSTOMER_NAME="${customer_name}"
IMAGE_REPO="${image_repo}"
IMAGE_TAG="${image_tag}"
UPGRADE_MODE="${upgrade_mode}"
UPGRADE_SCHEDULE="${upgrade_schedule}"
TLS_MODE="${tls_mode}"
DOMAIN="${domain}"
APPS_SUBDOMAIN_BASE="${data_apps_subdomain_base}"
DOMAIN_ALIAS="${domain_alias}"
ACME_EMAIL="${acme_email}"
DATA_SOURCE="${data_source}"
KEBOOLA_STACK_URL="${keboola_stack_url}"
SEED_ADMIN_EMAIL="${seed_admin_email}"
SEED_ADMIN_PASSWORD="${seed_admin_password}"
ROLE="${role}"

# uid the agnes-applier system user must be pinned to. MUST equal the app
# container's uid (Dockerfile `useradd --system --uid 999 ... agnes`, `USER
# agnes`) — instance.yaml is written 0600, and agnes-applier is what ends up
# owning it (see the recursive /data/state chown + the applier's own
# rewrites below), so the app container can read its own config only while
# the two numbers agree. Declared once here so the two useradd attempts and
# the readback check below can't drift apart from each other (#1217).
AGNES_APPLIER_UID=999

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup at $(date) ==="

# --- 1. Docker (install if missing) ---
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version &>/dev/null; then
    apt-get update && apt-get install -y docker-compose-plugin
fi
# H4-NEW: required by agnes-state-applier.sh's write_instance_yaml PyYAML path.
# The applier has a pure-bash fallback so a missing package does not wedge the
# state machine, but installing it here ensures all freshly-provisioned VMs
# take the full-fidelity PyYAML path (which preserves non-database YAML keys).
apt-get install -y --no-install-recommends python3-yaml

# --- 1c. Kernel memory tuning: Transparent Huge Pages = madvise ---
# THP=always (the distro default on many images) backs every large anonymous
# allocation with 2 MiB huge pages. Combined with glibc arena retention, the
# DuckDB/BigQuery query churn ratchets RSS up to its largest working set and
# never releases it, tripping the cgroup OOM killer on data-source-heavy
# instances. 'madvise' (the setting Postgres and other DB workloads recommend)
# uses huge pages only where explicitly requested, removing the amplifier.
# Re-applied on every boot, so it survives reboots without a grub/sysctl change.
# The companion app-side mitigation (MALLOC_ARENA_MAX / MALLOC_TRIM_THRESHOLD_)
# is baked into the container image's Dockerfile.
if [ -w /sys/kernel/mm/transparent_hugepage/enabled ]; then
    echo madvise > /sys/kernel/mm/transparent_hugepage/enabled || true
    echo "THP set to: $(cat /sys/kernel/mm/transparent_hugepage/enabled)"
fi

# --- 1d. Docker log rotation (default json-file path) ---
# Without an explicit log-driver config, Docker's json-file driver never
# rotates — a routinely-recreated container (agnes-auto-upgrade ticks every
# 5 min) can accumulate unbounded log files on the boot disk, on top of the
# recreate itself already destroying the previous container's log history.
# This is the fallback for VMs that don't run docker-compose.gcp-logging.yml
# (enable_gcp_logging=false, or any non-GCE deployment of this module).
# Placed here — BEFORE the data disk mount and well before any `docker
# compose up` — because a daemon restart is safe with no containers running
# and unsafe once they are. Written ONLY when the file is absent: an
# operator-authored daemon.json (custom log driver, registry mirror, other
# daemon setting) must never be clobbered by this script re-running on every
# boot.
DAEMON_JSON=/etc/docker/daemon.json
# --- docker-log-rotation begin (extracted + executed by tests/test_daemon_json_rotation.py) ---
DOCKER_LOG_ROTATION_WRITTEN=0
if [ -f "$DAEMON_JSON" ]; then
    echo "INFO: $DAEMON_JSON already exists — leaving Docker log-rotation config to the operator" >&2
else
    mkdir -p "$(dirname "$DAEMON_JSON")"
    cat > "$DAEMON_JSON" <<'DAEMONEOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
DAEMONEOF
    DOCKER_LOG_ROTATION_WRITTEN=1
fi
# --- docker-log-rotation end ---
if [ "$DOCKER_LOG_ROTATION_WRITTEN" = "1" ]; then
    systemctl restart docker
fi

# --- 2. Persistent data disk mount ---
DATA_DEV="/dev/disk/by-id/google-data"
DATA_MNT="/data"
if [ -b "$DATA_DEV" ]; then
    if ! blkid "$DATA_DEV" | grep -q ext4; then
        mkfs.ext4 -F "$DATA_DEV"
    fi
    mkdir -p "$DATA_MNT"
    mountpoint -q "$DATA_MNT" || mount -o discard,defaults "$DATA_DEV" "$DATA_MNT"
    grep -qF "$DATA_DEV" /etc/fstab || echo "$DATA_DEV $DATA_MNT ext4 discard,defaults,nofail 0 2" >> /etc/fstab
    mkdir -p "$DATA_MNT/state" "$DATA_MNT/analytics" "$DATA_MNT/extracts" "$DATA_MNT/uploads"
    # Match Dockerfile USER agnes (uid:gid 999:999). A freshly-attached PD is
    # root-owned by default; without this chown the non-root container cannot
    # write to /data/state/system.duckdb and every authed request 500s after
    # the first upgrade that flips USER from root to agnes (regression hit
    # agnes-development on 2026-04-29). Idempotent — safe on reboot.
    #
    # /data/uploads holds admin-uploaded marketplace cover images
    # (v50/0.55.0+). app/main.py eagerly mkdirs it at boot for the
    # StaticFiles mount; on host-bind setups where /data root is
    # root-owned, that mkdir 403s and the container crashloops — so
    # pre-create the dir here under the SAME chown -R that already
    # covers state/analytics/extracts. Cheap insurance.
    #
    # NEVER recurse into the postgres data dirs: they must stay uid 70 (the
    # postgres image's user), and this script runs on EVERY boot — a blanket
    # `chown -R 999 /data` bricked a live side-car DB during a customer
    # VM recreation (2026-07-21: "could not open file global/pg_filenode.map: Permission
    # denied" once host-mounted /data/postgres existed). Recursively chowning
    # everything EXCEPT those dirs, then re-asserting uid 70 on them below,
    # keeps both app and DB ownership correct across reboots and also
    # self-heals disks damaged by the old blanket chown.
    find "$DATA_MNT" -mindepth 1 -maxdepth 1 \
        ! -name postgres ! -name dispatcher-postgres ! -name kai-agent-postgres \
        -exec chown -R 999:999 {} +
    chown 999:999 "$DATA_MNT"
fi

# Initial instance.yaml::database = {backend: "side_car"} so a fresh
# instance boots straight onto the bundled Postgres side-car app-state (A1).
# The compose-file resolver (scripts/ops/agnes-compose-file.sh) reads this
# file at boot and, for "side_car", assembles the postgres overlays; the
# POSTGRES_PASSWORD mint + DATABASE_URL write further below are already
# unconditional, so no other change is needed for a new VM to come up on PG.
# The DB-backend state machine (see scripts/ops/agnes-state-applier.sh +
# app/api/admin_db_migrate.py) can still migrate a "side_car" install to any
# other persisted backend later — this only seeds day-zero state.
# Idempotent: never clobber an existing file — this seed is for brand-new
# instances only; an existing instance keeps its persisted backend
# (including "duckdb", which remains a fully supported legacy state) exactly
# as-is across every reboot and redeploy.
INSTANCE_YAML="$DATA_MNT/state/instance.yaml"
if [ ! -f "$INSTANCE_YAML" ]; then
    mkdir -p "$DATA_MNT/state"
    cat > "$INSTANCE_YAML" <<'YAML'
database:
  backend: side_car
YAML
    # Vendor-neutral per-instance branding (logo_svg / brand / subtitle /
    # copyright / favicon / theme colours / custom_scripts) from the Terraform variables,
    # pre-rendered to a base64'd top-level `instance:` + `theme:` YAML fragment.
    # Appended ONLY here, inside the "file absent" branch, so it seeds a fresh
    # instance without ever clobbering an operator's later edits or a migrated
    # database.backend. The app reads these keys back from this same file (see
    # app/instance_config.py; theme colours recolor the design-system --ds-*
    # tokens). Empty when the caller set no branding -> the block is skipped
    # entirely and instance.yaml is byte-for-byte the database-only file above.
    # base64 carries the multi-line SVG / script HTML across the metadata
    # boundary without any heredoc-delimiter or shell-expansion hazard.
%{ if instance_branding_b64 != "" ~}
    echo "${instance_branding_b64}" | base64 -d >> "$INSTANCE_YAML"
%{ endif ~}
    chown 999:999 "$INSTANCE_YAML"
fi
# The 0600 tightening lives further down, right after the state-applier user
# exists — the mode is only safe once the ownership it depends on is
# established, and that user is what finally owns this file.

# --- 3. App directory + extract host artifacts from the pinned image ---
APP_DIR="/opt/agnes"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Pull the pinned image first so we can extract host-side artifacts from it.
# Everything we need on the host (compose files, Caddyfile, agnes-auto-upgrade.sh)
# ships baked into the image at /opt/agnes-host/, released atomically with
# the app. AGNES_TAG is the single version pin for both — no split-brain
# with main-branch curl.
#
# Why image-extract beats curling raw.githubusercontent.com:
#   - Version pin: customer pins AGNES_TAG → extracted artifacts match the
#     same tag. main-branch curls would break that pin silently.
#   - Egress: image is already pulled from the private registry; the public
#     internet is no longer required for boot.
#   - Rollback: revert is one tag bump. Curl-from-main has no per-customer
#     rollback path.
docker pull "$${IMAGE_REPO}:$${IMAGE_TAG}"
EXTRACT_CONTAINER=$(docker create "$${IMAGE_REPO}:$${IMAGE_TAG}")
trap "docker rm '$EXTRACT_CONTAINER' >/dev/null 2>&1 || true" EXIT
docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/." "$APP_DIR/"
docker cp "$EXTRACT_CONTAINER:/opt/agnes-host/agnes-auto-upgrade.sh" /usr/local/bin/agnes-auto-upgrade.sh
chmod +x /usr/local/bin/agnes-auto-upgrade.sh

# docker-compose.gcp-logging.yml (see its own header comment) ships baked
# into the image and was just extracted into $APP_DIR unconditionally by the
# recursive docker cp above. Its mere PRESENCE is what activates it — the
# COMPOSE_FILE resolver (scripts/ops/agnes-compose-file.sh) appends it to
# every recurring `docker compose` invocation whenever the file exists on
# disk, and section 4 below inlines the same presence check into
# COMPOSE_FILE_VALUE so this script's own first `up -d` engages it too. On a
# non-GCE / non-GCP deployment (or an operator who wants the default
# json-file driver instead), remove it right back out so that presence check
# stays false. Runs on every boot, so it also self-heals a VM whose
# enable_gcp_logging flipped since the last provisioning.
%{ if !enable_gcp_logging ~}
rm -f "$APP_DIR/docker-compose.gcp-logging.yml"
%{ endif ~}

# Install agnes-state-applier (DB backend state machine — applies compose
# lifecycle changes when /data/state/db-state-target.flag changes). The
# script + its systemd units are baked into /opt/agnes-host/ via Dockerfile
# (same image-extract contract as agnes-auto-upgrade.sh above), already
# pulled into $APP_DIR by the recursive docker cp two lines up.
# Create dedicated non-root user for the DB-state applier — limits
# blast radius from full root to "docker group" (still effectively
# root via /var/run/docker.sock, but no other system surface).
# Idempotent on re-runs.
#
# The uid is PINNED to $AGNES_APPLIER_UID, not allocated. `chown -R
# agnes-applier /data/state` below is what finally owns instance.yaml, and
# the applier re-creates that file under its own uid on every rewrite — so
# at 0600 the app container (Dockerfile `USER agnes`, same pinned uid) can
# read its own config only while these two uids are the same number.
# `useradd --system` picks the top free id in the system range, which lands
# there on today's image by allocation rather than by intent — so pin it,
# and let the chmod below check the pin took.
#
# This `if` only guards user CREATION, not the uid check — an
# agnes-applier that already exists (e.g. a VM provisioned before this pin
# existed, or one where uid $AGNES_APPLIER_UID was taken by something else at
# the time) is deliberately left alone here rather than remediated
# automatically: `usermod -u` on a live system user can leave files it
# already owns pointing at the old uid, which is a worse surprise than a
# loud warning. The readback below fires for BOTH the freshly-created and
# the pre-existing case, since it re-reads whatever uid the name resolves to
# right now instead of trusting this block succeeded.
if ! id -u agnes-applier >/dev/null 2>&1; then
    # Only the UID is pinned. `--gid` and `--user-group` are mutually
    # exclusive, so a form passing both always fails — and the gid does not
    # matter here anyway: instance.yaml is 0600, so the group bits grant
    # nothing and only the owner's uid decides who can read it.
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --uid "$AGNES_APPLIER_UID" --user-group agnes-applier 2>/dev/null \
    || useradd --system --no-create-home --shell /usr/sbin/nologin \
            --user-group agnes-applier
fi
usermod -aG docker agnes-applier
mkdir -p /data/state /data/postgres
chown -R agnes-applier:agnes-applier /data/state

# Tighten instance.yaml to 0600 — but ONLY when the invariant that makes 0600
# safe actually holds. The file carries the Postgres URL with its password
# inline plus any operator-set connector credentials, and root's umask would
# otherwise leave a freshly created one world-readable on a data volume
# several non-root containers mount. Outside the create branch above so a boot
# also repairs a file written before this existed.
#
# Conditional because the failure it would otherwise cause is worse than the
# leak it closes: if uid $AGNES_APPLIER_UID was already taken at provisioning
# time, the pin above fell through to an allocated id, the app cannot read a
# 0600 file it does not own, and — with the fail-closed read this change
# also introduces — the instance refuses to start. A full outage in place
# of a silent degradation. Where the pin did not take, the mode stays as it
# was and the reason is on the console; the hardening applies exactly where
# its precondition is met.
#
# Covers both non-happy paths from #1217: (a) the uid was taken at THIS
# boot's provisioning (the `if` above fell through to the unpinned form) and
# (b) agnes-applier already existed from before the pin with some other uid
# (an in-place upgrade) — the `if` above skipped creation entirely, so this
# readback is the only place either shape is caught.
APPLIER_UID=$(id -u agnes-applier 2>/dev/null || echo "")
if [ "$APPLIER_UID" = "$AGNES_APPLIER_UID" ]; then
    chmod 600 "$INSTANCE_YAML" 2>/dev/null || true
else
    echo "WARN: agnes-applier is uid $APPLIER_UID, not $AGNES_APPLIER_UID — leaving $INSTANCE_YAML at its current mode. 0600 would make it unreadable by the app container (uid $AGNES_APPLIER_UID), which owns neither the file nor this user. Remediation: free uid $AGNES_APPLIER_UID (check what holds it with 'getent passwd $AGNES_APPLIER_UID') and either 'userdel'+re-run this script to recreate agnes-applier pinned, or 'usermod -u $AGNES_APPLIER_UID agnes-applier' followed by 'chown -R agnes-applier:agnes-applier /data/state /opt/agnes/.env' to re-home its existing files onto the new uid. Until then this VM runs in the pre-#1217 degraded mode: instance.yaml stays at its current, looser permissions." >&2
fi
# /data/postgres must stay 70:70 (postgres image uid) — applier just
# runs docker exec against the container, doesn't touch the volume.
# Recursive: also repairs a data dir damaged by the pre-2026-07-21 blanket
# `chown -R 999 /data` (idempotent and cheap on a healthy dir).
chown -R 70:70 /data/postgres
chmod 700 /data/postgres

install -m 0755 "$APP_DIR/agnes-state-applier.sh" /usr/local/bin/agnes-state-applier.sh
install -m 0644 "$APP_DIR/agnes-state-applier.service" /etc/systemd/system/agnes-state-applier.service
install -m 0644 "$APP_DIR/agnes-state-applier.timer" /etc/systemd/system/agnes-state-applier.timer
# Bootstrap unit (Phase 8.1 follow-up #2). Runs as root, creates the
# agnes-applier user + chowns /data/state on first boot. The main
# applier unit ``Requires=`` it so by the time systemd resolves
# ``User=agnes-applier`` for the applier, the user definitely exists.
# The eager useradd block above (lines ~108-117) is now belt-and-
# braces; customer infras that don't ship matching provisioning logic
# get the bootstrap for free via this unit.
install -m 0644 "$APP_DIR/agnes-state-applier-bootstrap.service" /etc/systemd/system/agnes-state-applier-bootstrap.service
systemctl daemon-reload
systemctl enable --now agnes-state-applier-bootstrap.service
systemctl enable --now agnes-state-applier.timer

# docker-compose.tls.yml + Caddyfile land regardless of TLS_MODE. agnes-auto-upgrade.sh
# detects TLS at runtime via cert files on disk; certs can appear after boot via
# agnes-tls-rotate.sh or manual provisioning. The caddy service bind-mounts
# ./Caddyfile, so it must exist on disk before any `docker compose up` even when
# the tls overlay is currently inactive. Cheap to keep them on disk either way.

# --- 4. Fetch secrets from Secret Manager — fail loudly if missing ---
KEBOOLA_TOKEN=""
if [ "$DATA_SOURCE" = "keboola" ]; then
    # No `|| echo ""` fallback — if the token secret is missing, boot should fail
    # loudly rather than silently start an app that will fail sync cryptically later.
    KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token)
fi
JWT_KEY=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-jwt-secret)
# SESSION_SECRET — signs session cookies (app/secrets.py::get_session_secret).
# Single-node deployments would otherwise fall back to a per-node generated-and-
# persisted file, which desyncs across processes in a role-split deployment and
# trips the multi-process startup guard (app/startup_guards.py). Fetched the
# exact same way as JWT_KEY above: a dedicated Secret Manager secret, no on-VM
# fallback generation.
SESSION_KEY=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-session-secret)

# ── Postgres password from Secret Manager + side-car data dir prep ──
# Task 2A.1 provisioned agnes-<customer>-postgres-password with VM SA bound to
# secretAccessor. Pull every boot (idempotent — the secret value is stable
# across reboots; we re-fetch rather than reading from .env because .env may
# have been wiped to force a reset).
POSTGRES_PASSWORD=$(gcloud secrets versions access latest --secret=agnes-$${CUSTOMER_NAME}-postgres-password)

# postgres:16-alpine runs as uid 70 (the Alpine ``postgres`` user). When a
# future overlay bind-mounts /data/postgres into the side-car (mirroring the
# /data:/data pattern in docker-compose.host-mount.yml), the dir must be
# pre-created and owned by uid 70 so initdb / runtime writes succeed.
# Cheap to do unconditionally — the dir is unused when the side-car runs on
# the default named volume.
mkdir -p "$DATA_MNT/postgres"
chown -R 70:70 "$DATA_MNT/postgres"
chmod 700 "$DATA_MNT/postgres"

# Re-align a persisted side-car database.url with the CURRENT postgres
# password. The password survives recreates (Secret Manager), but a VM
# recreate replaces the side-car's container while /data/state/instance.yaml
# persists — an instance.yaml written against an older side-car (e.g. one
# initialized on a named volume with different credentials, as observed
# during a customer VM recreation pre-2026-07-21) leaves the app crash-looping on FATAL password auth. The
# rewrite is a no-op when the url already carries the current password, and
# never touches non-side_car backends (cloud urls point at managed instances
# with their own credentials).
#
# sed -i replaces the file via temp-file + rename. GNU sed running as root
# preserves ownership and mode, but restore them explicitly so the re-align
# can never change who may read the state file regardless of sed flavor —
# both fresh-created (999, 0644, above) and applier-rewritten
# (agnes-applier, 0600, scripts/ops/agnes-state-applier.sh) shapes exist in
# the field.
# Matches side_car AND the transient side_car_in_progress deliberately (both
# anchored-exact, not a prefix accident): in both states database.url targets
# the local side-car, so a reboot mid-migration needs the same credential
# re-align. The overlay selection below stays exact-match on side_car — an
# in-progress migration must not engage the side-car overlay set early.
if [ -f "$INSTANCE_YAML" ] && grep -qE '^[[:space:]]*backend:[[:space:]]*"?side_car(_in_progress)?"?[[:space:]]*$' "$INSTANCE_YAML"; then
    IY_OWNER=$(stat -c '%u:%g' "$INSTANCE_YAML")
    IY_MODE=$(stat -c '%a' "$INSTANCE_YAML")
    sed -i "s|postgresql+psycopg://agnes:[^@]*@postgres:5432/agnes|postgresql+psycopg://agnes:$POSTGRES_PASSWORD@postgres:5432/agnes|" "$INSTANCE_YAML"
    chown "$IY_OWNER" "$INSTANCE_YAML"
    chmod "$IY_MODE" "$INSTANCE_YAML"
fi

# SCHEDULER_API_TOKEN — shared secret between the app and scheduler containers.
# Both source the same /opt/agnes/.env via Docker Compose env_file:, so the
# scheduler's outbound bearer token always matches the app's expected value.
# See app/auth/scheduler_token.py for the auth path it unlocks.
#
# Preserve across reboots: the token is plumbed into a long-lived synthetic
# user, and rotating it forces a restart of both containers. Read back from
# an existing .env when present; mint fresh only on the first boot.
SCHEDULER_API_TOKEN=""
if [ -f "$APP_DIR/.env" ]; then
    SCHEDULER_API_TOKEN=$(grep -E '^SCHEDULER_API_TOKEN=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$SCHEDULER_API_TOKEN" ]; then
    # 64 hex chars = 256 bits of /dev/urandom entropy. Floor enforced in
    # app/auth/scheduler_token.SCHEDULER_TOKEN_MIN_LENGTH is 32; 64 leaves
    # headroom for a future tightening without re-provisioning every VM.
    SCHEDULER_API_TOKEN=$(openssl rand -hex 32)
fi

# AGNES_VAULT_KEY — Fernet key for the admin secrets vault (app/secrets_vault.py:
# datasource / Slack / MCP secrets stored encrypted in the state DB). Without it
# the vault refuses writes (PUT → 409 vault_key_not_configured); losing it makes
# every previously-stored vault row undecryptable.
#
# Durability: /opt/agnes/.env lives on the BOOT disk, which a VM recreate wipes,
# so the SCHEDULER_API_TOKEN read-back-from-.env pattern alone is not enough here
# (that token can be re-minted freely; this key cannot). The durable home is a
# keyfile on the persistent DATA disk — the same disk that holds the encrypted
# rows, so key and ciphertext survive (or die) together. Precedence:
#   existing keyfile > key hand-added to .env (adopted into the keyfile so it
#   survives the NEXT recreate) > mint fresh.
# Accepted trade-off: key and ciphertext share a disk, so a stolen data-disk
# snapshot exposes vault rows; the vault's threat model is DB dumps / backups
# leaving the machine, not disk theft.
# --- vault-key begin (extracted + executed by tests/test_startup_vault_key.py) ---
VAULT_KEY_FILE="$DATA_MNT/state/agnes-vault.key"
AGNES_VAULT_KEY=""
if [ -f "$VAULT_KEY_FILE" ]; then
    AGNES_VAULT_KEY=$(tr -d '[:space:]' < "$VAULT_KEY_FILE" || true)
fi
if [ -z "$AGNES_VAULT_KEY" ] && [ -f "$APP_DIR/.env" ]; then
    AGNES_VAULT_KEY=$(grep -E '^AGNES_VAULT_KEY=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$AGNES_VAULT_KEY" ]; then
    # Fernet key = 32 /dev/urandom bytes, URL-safe base64 (44 chars incl. '='
    # padding) — same format cryptography.fernet.Fernet.generate_key() emits.
    AGNES_VAULT_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
fi
mkdir -p "$DATA_MNT/state"
(umask 077; printf '%s\n' "$AGNES_VAULT_KEY" > "$VAULT_KEY_FILE")
chmod 600 "$VAULT_KEY_FILE"
# --- vault-key end ---

# Optional Google OAuth credentials. The per-VM secret names are derived by
# the module from `var.oauth_secret_name_template` (substituting {kind}/{role}/
# {name} placeholders) and substituted into this script as
# `${oauth_client_id_secret_name}` / `${oauth_client_secret_secret_name}`.
#
# Empty template -> empty substitution -> we fall back to the legacy shared
# `google-oauth-client-{id,secret}` names (v1.x default — same OAuth client
# across every VM in the module call). Setting the template gives each VM its
# own OAuth client (different redirect URIs + separate blast radius from
# Google's end).
#
# The named secrets must already exist in Secret Manager AND the VM SA must
# have secretAccessor on them: module auto-grants for the derived per-VM names
# via `google_secret_manager_secret_iam_member.vm_oauth`; legacy ones still
# come via runtime_secrets. Missing / 403 / empty -> silent fallback to "" so
# password + email auth keep working.
OAUTH_ID_SECRET_NAME="${oauth_client_id_secret_name}"
OAUTH_SECRET_NAME="${oauth_client_secret_secret_name}"
if [ -z "$${OAUTH_ID_SECRET_NAME}" ]; then OAUTH_ID_SECRET_NAME="google-oauth-client-id"; fi
if [ -z "$${OAUTH_SECRET_NAME}" ]; then OAUTH_SECRET_NAME="google-oauth-client-secret"; fi
GOOGLE_CLIENT_ID=$(gcloud secrets versions access latest --secret="$${OAUTH_ID_SECRET_NAME}" 2>/dev/null || echo "")
GOOGLE_CLIENT_SECRET=$(gcloud secrets versions access latest --secret="$${OAUTH_SECRET_NAME}" 2>/dev/null || echo "")

# Optional app-level secrets injected via the caller's `runtime_secret_env` map
# (e.g. ANTHROPIC_API_KEY, SLACK_BOT_TOKEN). Module auto-grants
# secretAccessor for each map key. Missing / 403 / empty -> silent fallback to ""
# so the operator can wire a secret name before the value exists; the app
# surfaces its own missing-key error at startup (e.g. _chat_anthropic_key_ok).
%{ for secret_name, env_name in runtime_secret_env ~}
${env_name}=$(gcloud secrets versions access latest --secret=${secret_name} 2>/dev/null || echo "")
%{ endfor ~}

# AGNES_VERSION, RELEASE_CHANNEL, AGNES_COMMIT_SHA are baked into the image
# itself as ENV (see Dockerfile ARG/ENV + release.yml build-args). We do NOT
# set them here — doing so would override the image-level values with the
# floating tag name ("stable"/"dev"), hiding the real CalVer / git SHA.
# The app picks them up from the image's runtime environment.

# CADDY_TLS controls Caddyfile cert provisioning (see Caddyfile inline docs).
# - tls_mode=caddy + ACME_EMAIL set → Let's Encrypt auto-issue (public domain)
# - tls_mode=caddy + no ACME_EMAIL  → Caddy-managed self-signed (lab use)
# - any other tls_mode             → leave CADDY_TLS unset, Caddyfile default
#                                     (cert-file mode for corporate PKI) applies.
# Operators wanting cert-file mode shouldn't set tls_mode at all on the dev
# instance — leave it "none" and let the corp-PKI rotate scripts handle certs.
CADDY_TLS_LINE=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    # Value MUST be quoted in the .env file: agnes-auto-upgrade.sh sources
    # /opt/agnes/.env via `set -a; . .env; set +a`, and bash interprets an
    # unquoted `KEY=value with spaces` as `KEY=value` followed by trying to
    # exec `with`/`spaces` as commands → boot succeeds but every cron tick
    # logs "<email>: command not found".
    if [ -n "$ACME_EMAIL" ]; then
        CADDY_TLS_LINE="CADDY_TLS=\"tls $ACME_EMAIL\""
    else
        CADDY_TLS_LINE="CADDY_TLS=\"tls internal\""
    fi
fi

# --- apps-subdomain-caddy begin (extracted + executed by tests/test_caddyfile_apps_subdomain_docker.py) ---
# Data-app subdomains: Caddy vhost + per-app certificates
# Hosted apps are refused on the main origin (they run user-authored JS that
# would otherwise be same-origin with /api), so they are only reachable once
# Caddy terminates TLS for *.$APPS_SUBDOMAIN_BASE.
#
# Certificates are issued PER HOSTNAME on first request (on-demand, HTTP-01),
# not as one wildcard: a wildcard can only be validated over DNS-01, which
# would put a DNS-zone write credential on this very host — the host that runs
# user-authored app code. Issuance is gated by the `ask` endpoint below, so a
# stranger cannot drive ACME by requesting made-up names.
#
# Only when a base is configured: `*.` with an empty value is a site address
# Caddy refuses to parse, taking the PRIMARY site down with it — the same way
# an empty DOMAIN_ALIAS once did.
#
# The global options block must be FIRST in a Caddyfile, so it is prepended,
# not appended. Guarded on its own marker because this script runs on EVERY
# boot and a second copy is a file Caddy cannot parse. (The boot-time image
# extract normally restores a pristine Caddyfile first; the guard covers the
# paths that do not.)
if [ -n "$APPS_SUBDOMAIN_BASE" ] && [ -f "$APP_DIR/Caddyfile" ]; then
    if [ ! -f "$APP_DIR/Caddyfile.apps-subdomain" ]; then
        echo "WARN: Caddyfile.apps-subdomain missing from the image — data apps will not be reachable on *.$APPS_SUBDOMAIN_BASE" >&2
    elif grep -q on_demand_tls "$APP_DIR/Caddyfile"; then
        :  # already wired this boot — idempotent
    else
        {
            printf '{\n\ton_demand_tls {\n\t\task http://app:8000/api/data-apps-tls-check\n\t}\n}\n\n'
            cat "$APP_DIR/Caddyfile"
            printf '\n'
            cat "$APP_DIR/Caddyfile.apps-subdomain"
        } > "$APP_DIR/.Caddyfile.new" && mv "$APP_DIR/.Caddyfile.new" "$APP_DIR/Caddyfile"
        echo "INFO: data-app subdomains wired for *.$APPS_SUBDOMAIN_BASE (per-app certs via on-demand TLS)"
    fi
fi
# --- apps-subdomain-caddy end ---

# DOMAIN_ALIAS — a legacy hostname Caddy serves alongside DOMAIN and 308s onto
# it, so a domain cutover doesn't break old bookmarks / CLI configs / MCP
# connector URLs (Caddyfile's second site block). Written ONLY when non-empty:
# Caddy's `{$DOMAIN_ALIAS:localhost:8081}` fallback applies when the variable is
# UNSET, but an empty-but-set value substitutes an EMPTY site address and Caddy
# refuses to parse the config — which would take the primary site down too.
# Equal to DOMAIN is the other way to break the same thing: two site blocks
# with the same address, which Caddy also refuses to parse — taking the primary
# site down on the next recreate/reload. Terraform rejects it at plan time; this
# guard covers a value that reached the instance some other way (Devin Review
# on #1182).
DOMAIN_ALIAS_LINE=""
if [ -n "$DOMAIN_ALIAS" ] && [ "$DOMAIN_ALIAS" != "$DOMAIN" ]; then
    DOMAIN_ALIAS_LINE="DOMAIN_ALIAS=$DOMAIN_ALIAS"
elif [ -n "$DOMAIN_ALIAS" ]; then
    echo "WARN: domain_alias equals domain ($DOMAIN) — ignoring it; two site blocks with one address would stop Caddy from starting" >&2
fi

# Preserve operator overrides on AGNES_TAG. Rationale: this script
# runs on every boot (and the `metadata_startup_script` is in
# `lifecycle.ignore_changes` so a TF apply that changed the
# `image_tag` variable does NOT propagate to a long-lived VM until
# someone explicitly recreates it). Operators commonly hand-edit
# `/opt/agnes/.env` to pin a custom image tag (e.g. for a dev branch
# build, or a staged rollout) — overwriting that on every reboot
# clobbers their decision. Read the existing AGNES_TAG and let it
# win when it disagrees with $IMAGE_TAG; ditto for AGNES_TEMP_DIR
# (a deployment-specific path tweak operators sometimes set to
# steer tempdirs onto a larger volume).
EXISTING_AGNES_TAG=""
EXISTING_AGNES_TEMP_DIR=""
if [ -f "$APP_DIR/.env" ]; then
    EXISTING_AGNES_TAG=$(grep -E '^AGNES_TAG=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
    EXISTING_AGNES_TEMP_DIR=$(grep -E '^AGNES_TEMP_DIR=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
EFFECTIVE_AGNES_TAG="$${EXISTING_AGNES_TAG:-$IMAGE_TAG}"
if [ -n "$EXISTING_AGNES_TAG" ] && [ "$EXISTING_AGNES_TAG" != "$IMAGE_TAG" ]; then
    echo "INFO: preserving operator-edited AGNES_TAG=$EXISTING_AGNES_TAG (TF variable said $IMAGE_TAG; rm /opt/agnes/.env to reset)"
fi
AGNES_TEMP_DIR_LINE=""
if [ -n "$EXISTING_AGNES_TEMP_DIR" ]; then
    AGNES_TEMP_DIR_LINE="AGNES_TEMP_DIR=\"$EXISTING_AGNES_TEMP_DIR\""
fi

# SERVER_URL — the deployment's public URL. app.chat.manager::agnes_server_url
# resolves SERVER_URL (then AGNES_INTERNAL_URL, then loopback) and feeds it to
# every cloud-chat sandbox as AGNES_SERVER; without it the sandbox is told to
# reach Agnes at http://127.0.0.1:8000, which inside the sandbox is nothing —
# the in-sandbox CLI dies with ECONNRESET on its first call and chat is dead
# on arrival on any module-provisioned VM (previously masked by hand-edited
# .env files that VM recreates wipe). Precedence: operator-edited value in the
# existing .env wins (same pattern as AGNES_TAG above), else https on the
# configured domain, else plain HTTP on the VM's external IP (the same :8000
# the firewall exposes for tls_mode=none deployments).
#
# Setting SERVER_URL reaches beyond chat: it pins the public_base_url origin
# (OAuth redirects, magic links, MCP issuer — previously request-derived).
# On domain VMs that origin becomes https://<domain>, the canonical address
# for both supported TLS shapes; a VM reached under several hostnames (or a
# public origin that differs from the configured domain) should hand-set
# SERVER_URL, which this block preserves. On domain-less VMs it becomes the
# pinned http://<ip>:8000 and, being plain-HTTP non-localhost, deliberately
# trips app/main.py's RFC 8414 issuer check so the streamable MCP connector
# degrades gracefully — both intended for a direct-access plain-HTTP
# deployment; a fronted deployment should set a domain or hand-set
# SERVER_URL. AGNES_INTERNAL_URL is not a module-level knob: the .env
# heredoc below rewrites the file wholesale each boot and has never carried
# that variable, so no module-provisioned VM can rely on it and auto-setting
# SERVER_URL shadows nothing here (split-horizon support would be a new
# module variable, not an .env edit).
EXISTING_SERVER_URL=""
if [ -f "$APP_DIR/.env" ]; then
    EXISTING_SERVER_URL=$(grep -E '^SERVER_URL=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
# Two classes of persisted value are NOT meaningful operator overrides and
# must be re-derived instead of preserved:
#   1. Loopback — can only be a previously-persisted failed derivation;
#      preserving it locks the failure in forever (the exact bug this block
#      fixes).
#   2. http://<IPv4>:8000 — the shape this block itself derives. Preserving
#      it would freeze a stale IP on deployments without a reserved address
#      (this module reserves static IPs, but forks may not); re-deriving is
#      idempotent on a static IP and self-healing on an ephemeral one. An
#      operator who genuinely wants a raw-IP override should use a hostname
#      or a different port, which stays sticky.
# Host extraction handles bracketed IPv6 ([::1]:8000) and port/path suffixes;
# exact-match only — https://localhost.internal.example.com must survive.
EXISTING_SERVER_HOST="$${EXISTING_SERVER_URL#*://}"
case "$EXISTING_SERVER_HOST" in
    \[*)
        EXISTING_SERVER_HOST="$${EXISTING_SERVER_HOST#\[}"
        EXISTING_SERVER_HOST="$${EXISTING_SERVER_HOST%%\]*}"
        ;;
    *)
        EXISTING_SERVER_HOST="$${EXISTING_SERVER_HOST%%[:/]*}"
        ;;
esac
case "$EXISTING_SERVER_HOST" in
    127.0.0.1|localhost|::1) EXISTING_SERVER_URL="" ;;
esac
case "$EXISTING_SERVER_URL" in
    http://[0-9]*.[0-9]*.[0-9]*.[0-9]*:8000) EXISTING_SERVER_URL="" ;;
esac
SERVER_URL=""
if [ -n "$EXISTING_SERVER_URL" ]; then
    SERVER_URL="$EXISTING_SERVER_URL"
elif [ -n "$DOMAIN" ]; then
    # A configured domain implies TLS termination on this VM — ACME
    # (tls_mode=caddy) or corp-PKI cert-file mode (tls_mode=none + certs on
    # disk; agnes-auto-upgrade engages the tls overlay when it sees them) —
    # so https is the right scheme for both supported shapes. The unsupported
    # plain-HTTP-on-a-domain shape needs a hand-set SERVER_URL, which the
    # override above preserves.
    SERVER_URL="https://$DOMAIN"
else
    EXTERNAL_IP=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" \
        || true)
    if [ -n "$EXTERNAL_IP" ]; then
        SERVER_URL="http://$EXTERNAL_IP:8000"
    fi
    # Metadata read failed: write NO SERVER_URL line rather than persisting a
    # wrong value — the app keeps its request-derived/loopback behavior
    # (status quo ante) and the next boot re-derives.
fi
SERVER_URL_LINE=""
if [ -n "$SERVER_URL" ]; then
    SERVER_URL_LINE="SERVER_URL=$SERVER_URL"
fi

# Select the docker-compose overlay set from the persisted backend state.
# instance.yaml is written above only when absent, so a VM an operator migrated
# to side_car / cloud keeps that backend across reboots. The on-VM Postgres
# side-car overlay is engaged ONLY for backend=side_car; duckdb and cloud run
# the baseline (cloud reaches managed Postgres via instance.yaml::database.url,
# not the side-car). Mirrors agnes-state-applier.sh's overlay selection.
# Without this, a reboot of a cloud/duckdb instance re-engaged the side-car and
# ran the one-shot `migrate` against it, which fails and blocks app startup via
# depends_on.
PERSISTED_BACKEND=$(sed -n 's/^[[:space:]]*backend:[[:space:]]*//p' "$INSTANCE_YAML" 2>/dev/null | tr -d '"' | head -1)
if [ "$PERSISTED_BACKEND" = "side_car" ]; then
    COMPOSE_FILE_VALUE="docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml:docker-compose.postgres-host-mount.yml"
else
    COMPOSE_FILE_VALUE="docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml"
fi

# GCP Cloud Logging overlay — same presence gate as the canonical resolver
# (scripts/ops/agnes-compose-file.sh::agnes_resolve_compose_file), inlined
# because only the recurring drivers (agnes-auto-upgrade.sh,
# agnes-state-applier.sh) source that file; this script's first
# `docker compose up -d` builds COMPOSE_FILE_VALUE itself. Without this
# append the very first boot ran the stack on the json-file driver, and the
# first auto-upgrade tick lazily initializes its config marker to the status
# quo (no drift detected) — so logs didn't reach Cloud Logging until some
# unrelated recreate. Section 2 above removed the extracted file when
# enable_gcp_logging=false, so presence is the single switch, exactly as the
# resolver sees it. Appended before the deploy-layer overlays
# (dispatcher/kai-agent) to match the resolver's managed-first ordering and
# keep kai-agent last for the strict-boot strip below.
if [ -f "$APP_DIR/docker-compose.gcp-logging.yml" ]; then
    COMPOSE_FILE_VALUE="$COMPOSE_FILE_VALUE:docker-compose.gcp-logging.yml"
fi

%{ if dispatcher_enabled ~}
# --- 4b. Opt-in LLM dispatcher (token-arbitrage PoC) ---
# Runs as extra compose services (docker-compose.dispatcher.yml below —
# module-owned, NOT extracted from the Agnes image) in the same compose
# project: the app reaches it as http://dispatcher:8600 over compose DNS
# and nothing is exposed beyond the host loopback. The lifecycle rides the
# existing scripts for free: agnes-auto-upgrade honors COMPOSE_FILE from
# .env (pull + up include the overlay), and agnes-state-applier only ever
# targets named services with --no-deps, so it neither touches nor removes
# the dispatcher (neither script runs `compose down`/--remove-orphans).
DISP_DIR="$APP_DIR/dispatcher"
mkdir -p "$DISP_DIR"

# Both fetches fail LOUDLY (no ||-fallback, same posture as the Keboola
# token): an enabled dispatcher without its key or Vertex credentials would
# 401/500 every chat request — better a visible boot failure than a
# silently broken PoC.
DISPATCHER_KEY=$(gcloud secrets versions access latest --secret=${dispatcher_key_secret})
gcloud secrets versions access latest --secret=${dispatcher_vertex_sa_secret} > "$DISP_DIR/vertex-sa.json"

echo "${dispatcher_policies_b64}" | base64 -d > "$DISP_DIR/policies.yaml"
cat > "$DISP_DIR/keys.yaml" <<KEYSEOF
keys:
  "$DISPATCHER_KEY": agnes
KEYSEOF

# The dispatcher image runs as uid 10001 (non-root): key material readable
# only by that uid, the policy file by anyone.
chown 10001 "$DISP_DIR/keys.yaml" "$DISP_DIR/vertex-sa.json"
chmod 0400 "$DISP_DIR/keys.yaml" "$DISP_DIR/vertex-sa.json"
chmod 0444 "$DISP_DIR/policies.yaml"

# Ledger postgres password. Same durability concern as AGNES_VAULT_KEY above:
# /opt/agnes/.env lives on the BOOT disk, which a VM recreate wipes, but the
# ledger Postgres data dir lives on the persistent DATA disk and only honors
# POSTGRES_PASSWORD on first initdb — a recreate that re-mints the password
# would desync it from the surviving database, locking the dispatcher out of
# its own ledger. Precedence mirrors the vault key: existing keyfile on the
# data disk > key already in .env (adopted into the keyfile so it survives
# the NEXT recreate) > mint fresh.
# --- dispatcher-pg-password begin (extracted + executed by tests/test_startup_dispatcher_pg_password.py) ---
# The keyfile must NOT live inside $DATA_MNT/dispatcher-postgres — that
# directory is bind-mounted as the container's PGDATA, and postgres:16-alpine's
# initdb aborts on first boot if PGDATA contains anything but "lost+found".
DISPATCHER_PG_PASSWORD_FILE="$DATA_MNT/state/dispatcher-pg-password"
DISPATCHER_PG_PASSWORD=""
if [ -f "$DISPATCHER_PG_PASSWORD_FILE" ]; then
    DISPATCHER_PG_PASSWORD=$(tr -d '[:space:]' < "$DISPATCHER_PG_PASSWORD_FILE" || true)
fi
if [ -z "$DISPATCHER_PG_PASSWORD" ] && [ -f "$APP_DIR/.env" ]; then
    DISPATCHER_PG_PASSWORD=$(grep -E '^DISPATCHER_PG_PASSWORD=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$DISPATCHER_PG_PASSWORD" ]; then
    DISPATCHER_PG_PASSWORD=$(openssl rand -hex 24)
fi

mkdir -p "$DATA_MNT/state"
(umask 077; printf '%s\n' "$DISPATCHER_PG_PASSWORD" > "$DISPATCHER_PG_PASSWORD_FILE")
chmod 600 "$DISPATCHER_PG_PASSWORD_FILE"

# Ledger data on the persistent disk, kept separate from the keyfile above.
# postgres:16-alpine's entrypoint runs as root and chowns its data dir to
# uid 70 on first init itself; the recursive re-assert repairs dirs damaged
# by the pre-2026-07-21 blanket `chown -R 999 /data` (same fix as
# /data/postgres in section 2 — this dir is excluded from that chown now).
mkdir -p "$DATA_MNT/dispatcher-postgres"
# --- dispatcher-pg-password end ---
# Outside the extracted test block: chown needs root, which the block's
# test harness doesn't have. Recursive to repair dirs damaged by the
# pre-2026-07-21 blanket `chown -R 999 /data` (this dir is excluded from
# that chown now — same fix as /data/postgres in section 2).
chown -R 70:70 "$DATA_MNT/dispatcher-postgres"

# Quoted heredoc: the $${...} below are resolved by docker compose from
# /opt/agnes/.env at `compose up` time, not by this shell.
cat > "$APP_DIR/docker-compose.dispatcher.yml" <<'DISPYAML'
services:
  dispatcher:
    image: $${DISPATCHER_IMAGE}
    restart: always
    environment:
      DATABASE_URL: postgresql://dispatcher:$${DISPATCHER_PG_PASSWORD}@dispatcher-pg:5432/dispatcher
      GOOGLE_APPLICATION_CREDENTIALS: /config/vertex-sa.json
    volumes:
      - /opt/agnes/dispatcher:/config:ro
    ports:
      - "127.0.0.1:8600:8600" # host-side debugging; the app uses compose DNS
    depends_on:
      dispatcher-pg:
        condition: service_healthy
  dispatcher-pg:
    image: postgres:16-alpine
    restart: always
    environment:
      POSTGRES_USER: dispatcher
      POSTGRES_PASSWORD: $${DISPATCHER_PG_PASSWORD}
      POSTGRES_DB: dispatcher
    volumes:
      - /data/dispatcher-postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U dispatcher"]
      interval: 5s
      timeout: 3s
      retries: 12
DISPYAML

COMPOSE_FILE_VALUE="$COMPOSE_FILE_VALUE:docker-compose.dispatcher.yml"
%{ endif ~}
%{ if data_apps_enabled ~}
# --- Data apps (apps-runner sidecar) ---
# APPS_RUNNER_TOKEN — shared secret between the app and the apps-runner sidecar
# (both source the same .env). Preserve across reboots like SCHEDULER_API_TOKEN:
# read back from an existing .env, mint fresh only on first boot.
APPS_RUNNER_TOKEN=""
if [ -f "$APP_DIR/.env" ]; then
    APPS_RUNNER_TOKEN=$(grep -E '^APPS_RUNNER_TOKEN=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$APPS_RUNNER_TOKEN" ]; then
    APPS_RUNNER_TOKEN=$(openssl rand -hex 32)
fi
# DOCKER_GID — the apps-runner runs as the image's non-root uid 999 but must
# reach the root:docker-owned socket; docker-compose.yml adds $${DOCKER_GID} as
# a supplementary group. Resolve the host socket's gid so uid 999 can talk to
# the daemon (else every up()/stop() 502s with PermissionError(13)).
DOCKER_GID=$(stat -c '%g' /var/run/docker.sock 2>/dev/null || echo 999)
# The runner only pulls images under this prefix (everything before the last
# ':' of the full runtime image); the app reads the full image via the env var.
DATA_APPS_RUNTIME_IMAGE="${data_apps_runtime_image}"
APPS_RUNNER_IMAGE_PREFIX="$${DATA_APPS_RUNTIME_IMAGE%:*}"
%{ endif ~}
%{ if kai_agent_enabled ~}
# --- 4c. Opt-in embedded kai-agent turn engine ---
# Runs the kai-agent turn engine (an external Claude-Agent-SDK engine that
# embeds in Agnes through its jwt host adapter; the Agnes half of the contract
# lives in app/api/kai.py) as extra compose services: the engine, its own
# Postgres, and a one-shot schema migrate between them. The overlay rides the
# existing lifecycle for free, exactly like the dispatcher's: agnes-auto-
# upgrade honors COMPOSE_FILE from .env (pull + up include the overlay), and
# agnes-state-applier only targets named services with --no-deps.
KAI_DIR="$APP_DIR/kai-agent"
mkdir -p "$KAI_DIR"

# Both fetches fail LOUDLY (same posture as the dispatcher's): an enabled
# engine without the shared JWT secret cannot authenticate a single session,
# and without the E2B key it cannot spawn a sandbox.
KAI_HOST_JWT_SECRET=$(gcloud secrets versions access latest --secret=${kai_agent_jwt_secret})
KAI_E2B_API_KEY=$(gcloud secrets versions access latest --secret=${kai_agent_e2b_key_secret})

# Engine Postgres password — same durability dance as the dispatcher ledger's:
# the data dir on the persistent DATA disk honors POSTGRES_PASSWORD on first
# initdb only, while .env lives on the wipeable BOOT disk. Precedence:
# existing keyfile on the data disk > value already in .env (adopted into the
# keyfile so it survives the NEXT recreate) > mint fresh.
# --- kai-agent-pg-password begin (extracted + executed by tests/test_startup_kai_agent_toggle.py) ---
# The keyfile must NOT live inside $DATA_MNT/kai-agent-postgres — that
# directory is bind-mounted as the container's PGDATA, and postgres:16-alpine's
# initdb aborts on first boot if PGDATA contains anything but "lost+found".
KAI_AGENT_PG_PASSWORD_FILE="$DATA_MNT/state/kai-agent-pg-password"
KAI_AGENT_PG_PASSWORD=""
if [ -f "$KAI_AGENT_PG_PASSWORD_FILE" ]; then
    KAI_AGENT_PG_PASSWORD=$(tr -d '[:space:]' < "$KAI_AGENT_PG_PASSWORD_FILE" || true)
fi
if [ -z "$KAI_AGENT_PG_PASSWORD" ] && [ -f "$APP_DIR/.env" ]; then
    KAI_AGENT_PG_PASSWORD=$(grep -E '^KAI_AGENT_PG_PASSWORD=' "$APP_DIR/.env" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
if [ -z "$KAI_AGENT_PG_PASSWORD" ]; then
    KAI_AGENT_PG_PASSWORD=$(openssl rand -hex 24)
fi

mkdir -p "$DATA_MNT/state"
(umask 077; printf '%s\n' "$KAI_AGENT_PG_PASSWORD" > "$KAI_AGENT_PG_PASSWORD_FILE")
chmod 600 "$KAI_AGENT_PG_PASSWORD_FILE"

mkdir -p "$DATA_MNT/kai-agent-postgres"
# --- kai-agent-pg-password end ---
# Outside the extracted test block: chown needs root, which the block's test
# harness doesn't have (same note as the dispatcher's; this dir is excluded
# from the blanket data-disk chown in section 2).
chown -R 70:70 "$DATA_MNT/kai-agent-postgres"

# Artifact Registry images authenticate through the VM SA via gcloud's docker
# credential helper — configured for the image's own registry host only, and
# persisted in root's docker config so the agnes-auto-upgrade pulls keep
# working. Best-effort (`|| echo WARN`): the engine must never gate the
# machine, and a genuine credential problem still surfaces as a failed
# engine pull in the tolerant block in section 5. Any other private registry
# needs pre-authenticated pull access on the VM (not provided here).
KAI_AGENT_IMAGE_HOST="${kai_agent_image}"
KAI_AGENT_IMAGE_HOST="$${KAI_AGENT_IMAGE_HOST%%/*}"
case "$KAI_AGENT_IMAGE_HOST" in
    *-docker.pkg.dev) gcloud auth configure-docker "$KAI_AGENT_IMAGE_HOST" --quiet \
        || echo "WARN: gcloud auth configure-docker $KAI_AGENT_IMAGE_HOST failed — the engine image pull will likely fail below" >&2 ;;
esac

# An engine without a public origin cannot serve a single turn: its E2B
# sandbox must reach the LLM broker from the public internet. SERVER_URL is
# legitimately empty when the VM has no domain AND the metadata read for the
# external IP failed (that read is deliberately tolerant) — pasting that
# into the URL below would configure the broker as "/api/broker/anthropic"
# and every conversation would fail with nothing in the boot log saying why.
# SKIP the engine materialization this boot rather than abort: the metadata
# blip is transient, and the engine must never turn a degraded add-on into
# an unprovisioned machine — the base stack, TLS, cron and watchdog proceed
# untouched; a reboot or VM recreate retries with a fresh derivation.
KAI_AGENT_MATERIALIZE=1
if [ -z "$SERVER_URL" ] && [ -z "$DOMAIN" ]; then
    # The transient case the skip below exists for: the earlier tolerant
    # metadata read may have raced the network coming up. Retry it briefly
    # here, so a mere blip recovers in-place instead of costing the engine
    # until the next reboot (a skipped boot tears the engine down and drops
    # the overlay from COMPOSE_FILE, so the tick cannot re-materialize it).
    # A persistent failure still skips — deliberately louder than broken.
    # Scoped to the engine: the app's own SERVER_URL line for this boot is
    # already decided, an accepted pre-existing degradation on such a boot.
    for _kai_ip_try in 1 2 3; do
        sleep 5
        KAI_EXTERNAL_IP=$(curl -sf -H "Metadata-Flavor: Google" \
            "http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip" \
            2>/dev/null || true)
        if [ -n "$KAI_EXTERNAL_IP" ]; then
            SERVER_URL="http://$KAI_EXTERNAL_IP:8000"
            break
        fi
    done
fi
if [ -z "$SERVER_URL" ]; then
    echo "WARN: kai-agent engine SKIPPED this boot — no resolvable public origin (set a domain on the instance, or the GCE metadata read failed); reboot or recreate to retry" >&2
    KAI_AGENT_MATERIALIZE=0
elif [ "$TLS_MODE" = "caddy" ] && [ -z "$DOMAIN" ]; then
    # A caddy VM without a domain derives http://<external-ip>:8000, but the
    # web_raw firewall rule only tags tls_mode != "caddy" VMs — so :8000 is
    # CLOSED and the sandbox could never reach the broker. As loud as the
    # empty case, and for the same reason: a URL that resolves but is
    # unreachable fails every conversation with nothing saying why.
    case "$SERVER_URL" in
        http://*:8000*)
            echo "WARN: kai-agent engine SKIPPED this boot — SERVER_URL ($SERVER_URL) is the raw :8000 shape on a tls_mode=caddy VM, whose firewall does not expose that port; set a domain (or hand-set a reachable SERVER_URL in /opt/agnes/.env) and reboot" >&2
            KAI_AGENT_MATERIALIZE=0
            ;;
    esac
fi
if [ "$KAI_AGENT_MATERIALIZE" = "0" ] && [ -f "$APP_DIR/docker-compose.kai-agent.yml" ]; then
    # A PREVIOUS boot materialized the engine. Left alone, its containers
    # would keep running under restart:always with that boot's env (stale
    # broker URL; a stale HOST_JWT_SECRET if the secret rotated) while the
    # .env rewrite below drops the overlay from COMPOSE_FILE — live but
    # outside compose management, and the tick's retry would not see them.
    # Converge a skipped boot to a clean "engine off" state instead. The old
    # .env is still on disk here (the rewrite happens after this block), so
    # compose can interpolate the overlay's variables for the teardown.
    # `rm -sf` (stop + remove the file's services), NOT `down`: down would
    # also try to remove the project's default network, which the base
    # stack's containers still hold — that removal fails with "active
    # endpoints" and a non-zero exit, firing the WARN on a teardown that
    # actually succeeded.
    (cd "$APP_DIR" && docker compose -f docker-compose.kai-agent.yml rm -sf 2>/dev/null) \
        || echo "WARN: could not tear down the previous boot's kai-agent containers — check 'docker ps' by hand" >&2
    rm -f "$APP_DIR/docker-compose.kai-agent.yml"
fi
if [ "$KAI_AGENT_MATERIALIZE" = "1" ]; then

# The engine's env. Derived URLs split by who calls them: the E2B sandbox
# egresses to the LLM broker from the public internet, so that URL must be
# the deployment's public origin (SERVER_URL — a domain-less plain-HTTP VM
# only works if its :8000 is reachable from the sandbox, so give the instance
# a domain); the ticket + workspace fetches are made by the engine's own
# server and ride compose DNS to the app, avoiding a TLS hairpin.
# HOST_JWT_ISSUER/AUDIENCE mirror the app-side defaults in app/api/kai.py.
# The caller's kai_agent_env is appended AFTER these lines: env_file gives
# later duplicate keys precedence, so the map can override any derived value.
cat > "$KAI_DIR/.env" <<KAIENVEOF
HOST_MODULE=jwt
HOST_JWT_SECRET=$KAI_HOST_JWT_SECRET
HOST_JWT_ISSUER=agnes
HOST_JWT_AUDIENCE=kai-agent
HOST_BROKER_LLM_URL=$SERVER_URL/api/broker/anthropic
HOST_BROKER_TICKET_URL=http://app:8000/api/kai/tickets
HOST_WORKSPACE_URL=http://app:8000/api/kai/workspace
%{ if kai_agent_broker_mcp_enabled ~}
HOST_BROKER_MCP_URL=$SERVER_URL/api/kai/mcp
%{ endif ~}
POSTGRES_URL=postgresql://kai:$KAI_AGENT_PG_PASSWORD@kai-agent-pg:5432/kai_agent
E2B_API_KEY=$KAI_E2B_API_KEY
KAIENVEOF
echo "${kai_agent_env_b64}" | base64 -d >> "$KAI_DIR/.env"
chmod 600 "$KAI_DIR/.env"

# Quoted heredoc: the $${...} below are resolved by docker compose from
# /opt/agnes/.env at `compose up` time, not by this shell. The one-shot
# migrate runs from the engine's app dir inside the image because its
# migrator resolves the ./drizzle folder relative to the cwd; the engine
# waits on it via service_completed_successfully (the migrator is
# idempotent, so the auto-upgrade tick re-running it is harmless).
cat > "$APP_DIR/docker-compose.kai-agent.yml" <<'KAIYAML'
services:
  kai-agent:
    image: $${KAI_AGENT_IMAGE}
    restart: always
    env_file: /opt/agnes/kai-agent/.env
    # Bounded like the app/scheduler containers (heavy work happens in the
    # remote E2B sandbox, not here), and hardened like the data-app
    # containers: caps are ceilings, not reservations. The values come from
    # the per-VM kai_agent_mem_limit / kai_agent_cpus / kai_agent_pg_mem_limit
    # TF fields via /opt/agnes/.env — NOT from hand-edits of that file, which
    # every boot rewrites from scratch (the same reason app_mem_limit is a TF
    # field). The :-defaults below are a belt-and-braces fallback only.
    mem_limit: $${KAI_AGENT_MEM_LIMIT:-2g}
    cpus: $${KAI_AGENT_CPUS:-1.0}
    pids_limit: 512
    security_opt:
      - no-new-privileges:true
    ports:
      - "127.0.0.1:3001:3000" # host-side testing via SSH tunnel; the app uses compose DNS
    depends_on:
      kai-agent-pg:
        condition: service_healthy
      kai-agent-migrate:
        condition: service_completed_successfully
  kai-agent-migrate:
    image: $${KAI_AGENT_IMAGE}
    restart: "no"
    working_dir: /app/apps/kai-agent
    command: ["node", "dist/db/migrate.js"]
    env_file: /opt/agnes/kai-agent/.env
    mem_limit: $${KAI_AGENT_MEM_LIMIT:-2g}
    security_opt:
      - no-new-privileges:true
    depends_on:
      kai-agent-pg:
        condition: service_healthy
  kai-agent-pg:
    image: postgres:16-alpine
    restart: always
    environment:
      POSTGRES_USER: kai
      POSTGRES_PASSWORD: $${KAI_AGENT_PG_PASSWORD}
      POSTGRES_DB: kai_agent
    # no-new-privileges is safe here: the entrypoint drops root -> postgres
    # via setuid()/su-exec syscalls, which no_new_privs does not restrict
    # (it blocks GAINING privilege across execve, not shedding it).
    mem_limit: $${KAI_AGENT_PG_MEM_LIMIT:-1g}
    security_opt:
      - no-new-privileges:true
    volumes:
      - /data/kai-agent-postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U kai"]
      interval: 5s
      timeout: 3s
      retries: 12
KAIYAML

COMPOSE_FILE_VALUE="$COMPOSE_FILE_VALUE:docker-compose.kai-agent.yml"

# Closes the KAI_AGENT_MATERIALIZE guard: everything above (engine env,
# overlay, COMPOSE_FILE append) is skipped on a boot with no public origin.
fi
%{ endif ~}
cat > "$APP_DIR/.env" <<ENVEOF
JWT_SECRET_KEY=$JWT_KEY
SESSION_SECRET=$SESSION_KEY
$SERVER_URL_LINE
DATA_DIR=$DATA_MNT
DATA_SOURCE=$DATA_SOURCE
KEBOOLA_STORAGE_TOKEN=$KEBOOLA_TOKEN
KEBOOLA_STACK_URL=$KEBOOLA_STACK_URL
SEED_ADMIN_EMAIL=$SEED_ADMIN_EMAIL
SEED_ADMIN_PASSWORD=$SEED_ADMIN_PASSWORD
SCHEDULER_API_TOKEN=$SCHEDULER_API_TOKEN
AGNES_VAULT_KEY=$AGNES_VAULT_KEY
LOG_LEVEL=info
DOMAIN=$DOMAIN
AGNES_TAG=$EFFECTIVE_AGNES_TAG
AGNES_APP_MEM_LIMIT=${app_mem_limit}
AGNES_SCHEDULER_MEM_LIMIT=${scheduler_mem_limit}
AGNES_APP_CPUS=${app_cpus}
AGNES_SCHEDULER_CPUS=${scheduler_cpus}
%{ if home_route != "" ~}
AGNES_HOME_ROUTE=${home_route}
%{ endif ~}
%{ if !studio_enabled ~}
AGNES_STUDIO_ENABLED=false
%{ endif ~}
%{ if experience != "" ~}
AGNES_INSTANCE_EXPERIENCE=${experience}
%{ endif ~}
%{ if chat_provider != "" ~}
AGNES_CHAT_PROVIDER=${chat_provider}
%{ endif ~}
%{ if theme != "" ~}
AGNES_INSTANCE_THEME=${theme}
%{ endif ~}
ACME_EMAIL=$ACME_EMAIL
GOOGLE_CLIENT_ID=$GOOGLE_CLIENT_ID
GOOGLE_CLIENT_SECRET=$GOOGLE_CLIENT_SECRET
%{ for secret_name, env_name in runtime_secret_env ~}
${env_name}=$${${env_name}}
%{ endfor ~}
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+psycopg://agnes:$POSTGRES_PASSWORD@postgres:5432/agnes
%{ if dispatcher_enabled ~}
DISPATCHER_IMAGE=${dispatcher_image}
DISPATCHER_PG_PASSWORD=$DISPATCHER_PG_PASSWORD
LLM_DISPATCHER_URL=http://dispatcher:8600
LLM_DISPATCHER_API_KEY=$DISPATCHER_KEY
%{ endif ~}
%{ if kai_agent_enabled ~}
KAI_HOST_JWT_SECRET=$KAI_HOST_JWT_SECRET
KAI_AGENT_IMAGE=${kai_agent_image}
KAI_AGENT_PG_PASSWORD=$KAI_AGENT_PG_PASSWORD
KAI_AGENT_MEM_LIMIT=${kai_agent_mem_limit}
KAI_AGENT_CPUS=${kai_agent_cpus}
KAI_AGENT_PG_MEM_LIMIT=${kai_agent_pg_mem_limit}
%{ if kai_agent_broker_mcp_enabled ~}
KAI_BROKER_MCP_ENABLED=true
%{ endif ~}
%{ endif ~}
COMPOSE_FILE=$COMPOSE_FILE_VALUE
%{ if data_apps_enabled ~}
AGNES_DATA_APPS_ENABLED=true
AGNES_DATA_APPS_RUNTIME_IMAGE=${data_apps_runtime_image}
%{ if data_apps_subdomain_base != "" ~}
AGNES_DATA_APPS_SUBDOMAIN_BASE=${data_apps_subdomain_base}
# Same value, second consumer: the app reads AGNES_DATA_APPS_SUBDOMAIN_BASE,
# Caddy substitutes {$APPS_SUBDOMAIN_BASE} into the vhost address. The name is
# baked into the shipped Caddyfile.apps-subdomain, hence two vars, one source.
APPS_SUBDOMAIN_BASE=${data_apps_subdomain_base}
%{ endif ~}
APPS_RUNNER_TOKEN=$APPS_RUNNER_TOKEN
APPS_RUNNER_IMAGE_PREFIX=$APPS_RUNNER_IMAGE_PREFIX
DOCKER_GID=$DOCKER_GID
%{ endif ~}
$CADDY_TLS_LINE
$DOMAIN_ALIAS_LINE
$AGNES_TEMP_DIR_LINE
ENVEOF
chmod 600 "$APP_DIR/.env"
# B3-NEW: chown .env to agnes-applier IMMEDIATELY so the non-root
# applier's very first run (before the bootstrap unit fires) can
# already source the file. The bootstrap unit's ExecStart re-asserts
# this every boot in case an operator (or agnes-auto-upgrade) rewrites
# .env later.
# In the normal boot order this `if` is always false — section 3 above
# already created agnes-applier, pinned to $AGNES_APPLIER_UID. Kept pinned
# here too (same fallback shape) so a reordering of the two blocks can't
# quietly reintroduce an unpinned user via this path — #1217 was exactly
# this kind of duplicate that only one of two copies got fixed.
if ! id -u agnes-applier >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin \
            --uid "$AGNES_APPLIER_UID" --user-group agnes-applier 2>/dev/null \
    || useradd --system --no-create-home --shell /usr/sbin/nologin \
            --user-group agnes-applier
fi
chown agnes-applier:agnes-applier /opt/agnes/.env
chmod 0600 /opt/agnes/.env

# --- 5. Start Agnes ---
COMPOSE_PROFILES_ARG=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    COMPOSE_PROFILES_ARG="--profile tls"
fi
%{ if data_apps_enabled ~}
# The `apps` profile MUST be a command-line --profile flag, not COMPOSE_PROFILES
# in .env: docker compose does not merge the two — the moment ANY --profile flag
# is passed (e.g. `--profile tls` on the default caddy/TLS instance) the
# COMPOSE_PROFILES env var is ignored entirely, so an apps profile carried
# through .env would be silently dropped and the apps-runner sidecar never start
# (data-app deploys then 502). Multiple --profile flags DO union, so appending
# here activates apps alongside tls. agnes-auto-upgrade.sh mirrors this so the
# recurring upgrade tick keeps the sidecar running.
COMPOSE_PROFILES_ARG="$COMPOSE_PROFILES_ARG --profile apps"
%{ endif ~}

# Honor COMPOSE_FILE from /opt/agnes/.env. The .env write above sets the
# full list ``docker-compose.yml:docker-compose.prod.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml``
# so the prod + postgres + host-mount overlays engage by default. Order
# matters: host-mount loads LAST so its ``volumes: !override`` on
# data-migrate (in docker-compose.host-mount.yml) can see the service
# defined by docker-compose.postgres.yml. Fall back to the historical
# prod + host-mount baseline when .env doesn't set COMPOSE_FILE — keeps
# existing customer instances behaving identically if an operator removes
# the line. The colon-separated COMPOSE_FILE form is the documented
# alternative to explicit ``-f`` args (docker.com/compose/reference/envvars
# /compose_file); docker compose reads it from the working-directory .env
# automatically. Export so the value is visible to the docker compose
# subprocess regardless of whether docker's own dotenv loader fires first.
COMPOSE_FILE_DEFAULT="docker-compose.yml:docker-compose.prod.yml:docker-compose.host-mount.yml"
# shellcheck disable=SC1091
set -a; . "$APP_DIR/.env"; set +a
export COMPOSE_FILE="$${COMPOSE_FILE:-$COMPOSE_FILE_DEFAULT}"
%{ if kai_agent_enabled ~}
# The engine overlay is OPTIONAL and must never gate the machine: its
# one-shot migrate is a hard start condition for the engine SERVICE, so an
# unpullable engine image or a failed migration would fail the strict
# pull/up below and abort this script BEFORE the auto-upgrade cron and the
# watchdog are installed — an engine-enabled VM would then sit half
# provisioned while the app looks healthy. Pull + start the BASE stack
# strictly first (overlay stripped; it was appended last in section 4c),
# then bring the engine up tolerantly after the strict block.
KAI_FULL_COMPOSE_FILE="$COMPOSE_FILE"
export COMPOSE_FILE="$${COMPOSE_FILE%:docker-compose.kai-agent.yml}"
%{ endif ~}

docker compose $COMPOSE_PROFILES_ARG pull
%{ if data_apps_enabled ~}
# Pre-pull the data-app RUNTIME image. It is not a compose service, so the
# `docker compose pull` above never touches it — which left the very first
# deploy on a fresh VM fetching ~1.3 GB synchronously inside the runner's
# `containers.run`. That pull outlived docker-py's HTTP timeout, the daemon
# tore it down, and the retried `create` raised ImageNotFound: the deploy
# failed reporting a missing image that was merely still downloading.
# Best-effort (`|| true`) — a registry hiccup must not fail the whole boot;
# the deploy path still works, just slowly, on the cold-pull backstop.
docker pull "$AGNES_DATA_APPS_RUNTIME_IMAGE" || echo "WARN: could not pre-pull data-app runtime image $AGNES_DATA_APPS_RUNTIME_IMAGE"
%{ endif ~}
# Retry `up`: on a first boot the app can exceed its healthcheck start window
# (fresh image, DuckDB->PG data migration, keboola table attach), which makes
# `up -d` exit non-zero on the dependency gate — and under `set -e` that used
# to kill this script BEFORE the caddy/cron/watchdog sections, leaving a VM
# with a healthy app but no TLS and no auto-upgrade (hit on a customer dev
# VM, 2026-07-21).
# Three attempts with a pause give slow first boots time to converge; if the
# app is genuinely broken the third failure still fails the boot loudly.
COMPOSE_UP_OK=0
for attempt in 1 2 3; do
    if docker compose $COMPOSE_PROFILES_ARG up -d; then
        COMPOSE_UP_OK=1
        break
    fi
    if [ "$attempt" != 3 ]; then
        echo "WARN: docker compose up attempt $attempt failed; retrying in 60s"
        sleep 60
    fi
done
if [ "$COMPOSE_UP_OK" != "1" ]; then
    echo "ERROR: docker compose up failed after 3 attempts"
    exit 1
fi
%{ if data_apps_enabled ~}
# --- container-metadata-hardening begin (extracted + executed by tests/test_startup_container_metadata_hardening.py) ---
# Block hosted data-app containers from reaching the cloud metadata server
# (169.254.169.254). A data app runs user-authored code — RCE inside its own
# container is by design — and on a plain bridge network it can otherwise read
# the VM's service-account token from the metadata server and pivot to the
# entire cloud project. Docker's process hardening (cap_drop, no-new-privileges)
# does nothing about network reach, so this is enforced at the host firewall
# where a compromised container cannot undo it.
#
# Scoped to the agnes-apps bridge SOURCE subnet, NOT a blanket block: the Agnes
# app container itself legitimately reaches the metadata server (e.g. BigQuery
# GCE-metadata auth). The app is multi-homed but `default` is pinned as its
# highest-priority network (docker-compose.yml `networks.default.priority`), so
# its egress — metadata included — routes via `default`, its source IP is in the
# `default` subnet, and this agnes-apps-scoped rule never matches it. DOCKER-USER
# is the Docker-provided FORWARD hook, evaluated before SNAT/MASQUERADE, so a
# container's real source IP still matches here. Idempotent (check before insert).
#
# Resolved at boot from the live network. If agnes-apps is later recreated with a
# different subnet (e.g. `docker network prune` + redeploy without a reboot), the
# rule is stale until the next boot re-runs this block — re-run it (or reboot)
# after any manual agnes-apps network recreation.
METADATA_IP="169.254.169.254"
APPS_NETWORK="agnes-apps"
# Compose created agnes-apps on `up`; ensure it exists so its subnet resolves
# even if a future topology defers creation. Harmless when it already exists.
docker network create "$APPS_NETWORK" >/dev/null 2>&1 || true
APPS_SUBNETS="$(docker network inspect "$APPS_NETWORK" 2>/dev/null \
    | grep -oE '"Subnet":[[:space:]]*"[0-9./]+"' \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+' || true)"
if ! command -v iptables >/dev/null 2>&1; then
    echo "WARN: iptables not found; data-app containers can reach $METADATA_IP (cloud metadata) — block it manually, see docs/architecture.md" >&2
elif [ -z "$APPS_SUBNETS" ]; then
    echo "WARN: could not resolve $APPS_NETWORK subnet; data-app metadata egress NOT blocked this boot" >&2
else
    for subnet in $APPS_SUBNETS; do
        if iptables -C DOCKER-USER -s "$subnet" -d "$METADATA_IP/32" -j DROP 2>/dev/null; then
            :  # already present — idempotent
        elif iptables -I DOCKER-USER -s "$subnet" -d "$METADATA_IP/32" -j DROP 2>/dev/null; then
            echo "INFO: blocked $subnet -> $METADATA_IP (data-app metadata egress)" >&2
        else
            echo "WARN: failed to install DOCKER-USER metadata DROP for $subnet" >&2
        fi
    done
fi
# --- container-metadata-hardening end ---
%{ endif ~}
%{ if kai_agent_enabled ~}
# Now the engine, tolerantly: the base stack (incl. Caddy/TLS) is up, and the
# sections below (auto-upgrade cron, watchdog) must install regardless of the
# engine's fate. TARGETED at the engine services — a full-list pull/up here
# would fetch and start the whole stack a second time on every boot, and an
# unrelated base-stack failure would be reported as an engine problem.
# Gated on materialization: a skipped boot has no overlay to bring up. On
# failure the .env keeps the FULL list, so the next auto-upgrade tick (and
# any operator `docker compose up -d`) retries the engine with no state to
# repair.
if [ "$KAI_AGENT_MATERIALIZE" = "1" ]; then
    export COMPOSE_FILE="$KAI_FULL_COMPOSE_FILE"
    if ! docker compose $COMPOSE_PROFILES_ARG pull kai-agent kai-agent-migrate kai-agent-pg \
        || ! docker compose $COMPOSE_PROFILES_ARG up -d kai-agent; then
        echo "WARN: kai-agent engine sidecar failed to pull or start; base stack is up — fix the engine image/migration and re-run docker compose up -d (or wait for the auto-upgrade tick)" >&2
    fi
fi
%{ endif ~}

# --- 6. Auto-upgrade via cron (pulls new image digest on $UPGRADE_SCHEDULE) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    # agnes-auto-upgrade.sh was already extracted to /usr/local/bin/ in
    # section 3 alongside the compose files — the host artifacts ship
    # together from the pinned image. Nothing more to fetch here.
    :

    # Install cron entry idempotently: remove any prior agnes-auto-upgrade line, then append ours.
    CRON_LINE="$UPGRADE_SCHEDULE /usr/local/bin/agnes-auto-upgrade.sh >> /var/log/agnes-auto-upgrade.log 2>&1"
    (crontab -l 2>/dev/null | grep -v agnes-auto-upgrade || true; echo "$CRON_LINE") | crontab -
fi

# --- 7. Host-side watchdog + daily DB backup with restore-verification ---
# Independent of the app on purpose: the watchdog's job is to report states
# in which the app can no longer report on itself (process crash loops, the
# invalidated-database "zombie" state where /api/health stays 200 while
# every write 500s). Files ship as module artifacts via base64 (fileset()
# in main.tf), so they arrive with the infra tag regardless of which app
# image_tag the operator pinned.
%{ if enable_watchdog ~}
WD_STAGE=/opt/agnes-watchdog
mkdir -p "$WD_STAGE"
%{ for fname, content_b64 in watchdog_files_b64 ~}
echo "${content_b64}" | base64 -d > "$WD_STAGE/${fname}"
%{ endfor ~}
install -m 0755 "$WD_STAGE/agnes-watchdog.sh" /usr/local/bin/agnes-watchdog.sh
install -m 0755 "$WD_STAGE/agnes-db-backup.sh" /usr/local/bin/agnes-db-backup.sh
install -m 0644 "$WD_STAGE/agnes-watchdog.service" /etc/systemd/system/agnes-watchdog.service
install -m 0644 "$WD_STAGE/agnes-watchdog.timer" /etc/systemd/system/agnes-watchdog.timer
install -m 0644 "$WD_STAGE/agnes-db-backup.service" /etc/systemd/system/agnes-db-backup.service
install -m 0644 "$WD_STAGE/agnes-db-backup.timer" /etc/systemd/system/agnes-db-backup.timer
mkdir -p "$DATA_MNT/backups"
install -m 0644 "$WD_STAGE/agnes-db-verify.py" "$DATA_MNT/backups/agnes-db-verify.py"
chown 999:999 "$DATA_MNT/backups" "$DATA_MNT/backups/agnes-db-verify.py"

# Alert config. A non-empty Terraform alert_webhook_url wins; when it is
# empty, preserve an operator-hand-edited webhook across reboots (same
# precedence pattern as AGNES_TAG above).
TF_WEBHOOK_URL="${alert_webhook_url}"
EXISTING_WEBHOOK=""
if [ -f /etc/agnes-watchdog.env ]; then
    EXISTING_WEBHOOK=$(grep -E '^WEBHOOK_URL=' /etc/agnes-watchdog.env | head -1 | cut -d= -f2- | tr -d '"' || true)
fi
EFFECTIVE_WEBHOOK="$${TF_WEBHOOK_URL:-$EXISTING_WEBHOOK}"
cat > /etc/agnes-watchdog.env <<WDEOF
# agnes-watchdog + agnes-db-backup alert config (see agnes-watchdog.sh).
WEBHOOK_URL="$EFFECTIVE_WEBHOOK"
ENV_STAGE="$ROLE"
WDEOF
chmod 600 /etc/agnes-watchdog.env

systemctl daemon-reload
systemctl enable --now agnes-watchdog.timer agnes-db-backup.timer
%{ endif ~}

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup complete at $(date) ==="
docker compose ps
