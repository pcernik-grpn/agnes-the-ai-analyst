# Postgres cut-over runbook

Operator playbook for the release that introduces the Postgres side-car as
the default customer-instance deploy shape. Read this before bumping your
`infra-vX.Y.Z` module pin past the cut-over release.

This document is vendor-agnostic — substitute `<customer>` and `<your-host>`
with your own values. Commands assume you SSH into the customer-instance VM
and that the app lives at `/opt/agnes` (the default install path).

---

## Migrating an existing VM to the non-root applier (one-time, Phase 8.1)

Customer-instance VMs provisioned BEFORE this commit ran the
agnes-state-applier as root. New VMs get the non-root setup
automatically via the startup-script. To migrate an existing VM:

```bash
# As root on the customer VM:
useradd --system --no-create-home --shell /usr/sbin/nologin \
        --uid 999 --user-group agnes-applier
usermod -aG docker agnes-applier
chown -R agnes-applier:agnes-applier /data/state
systemctl daemon-reload
systemctl restart agnes-state-applier.timer
```

`--uid 999` pins agnes-applier to the same uid as the app container
(Dockerfile `USER agnes`). `/data/state/instance.yaml` is written `0600`,
and agnes-applier is what ends up owning it — so the app can only read its
own config while the two uids agree (#1217). Confirm the pin took:

```bash
id -u agnes-applier   # must print 999
```

If `useradd` above fails with "UID '999' is not unique", uid 999 already
belongs to a different account on this VM (`getent passwd 999` shows which
one). Either free it and re-run the command above, or accept the degraded
mode policy for such hosts: provisioning's own readback logs an `ERROR`
naming the uid holder and then applies the same fallback the applier uses at
every rewrite — `0640` with the app's gid (999) as the group wherever it can
actually grant that (running as root, or agnes-applier holding gid-999
membership), falling back to `0644` otherwise. The applier's own
`write_instance_yaml` reasserts that same policy on every subsequent rewrite.
The applier and the app keep working; what the mismatch costs is the
owner-only tightening, until the uids agree. Preserving the file's existing
mode is deliberately NOT what happens: the app's own writers
(`write_backend_state` and the admin config editors) run as uid 999 inside
the container — where owner-only is exactly right — and chmod the file
`0600` unconditionally, so carrying that mode across a re-own to a
mismatched agnes-applier (provisioning's own recursive `chown -R
/data/state`, or the applier's rewrite rename) would strand it owner-only
under a uid the app is not, and the fail-closed boot read would take the
instance down. Provisioning, an ordinary backend flip, cancel and stuck-job
recovery therefore can never leave the overlay unreadable to the app
container.

**Treat this as a state to leave, not to live in.** `instance.yaml` holds
the database url with its password inline plus any connector credentials an
operator set, on a data volume several non-root containers mount — the
degradation costs you exactly that hardening. The applier logs a warning
naming the uid it found at every rewrite (`journalctl -t
agnes-state-applier`), and the bootstrap unit logs one at every boot
(`journalctl -t agnes-state-applier-bootstrap`).

Do **not** `usermod -u 999` an *existing* agnes-applier account without also
re-chowning the files it already owns (`/data/state`, `/opt/agnes/.env`) —
a bare uid change leaves them pointing at the old number.

Then confirm the next tick succeeds:

```bash
journalctl -u agnes-state-applier.service -f
```

(If the timer was active and a migration was running mid-tick when
you ran the commands above, the in-flight tick continues as root —
the next tick fires as agnes-applier.)

---

## Cancel semantics

`POST /api/admin/db/cancel/<id>` (or `agnes admin db cancel <id>`) writes a
`<job-id>.cancel` sentinel file that the host-side applier checks at every
processing boundary. When the applier observes the sentinel, it stops the
migration, removes the sentinel, resets `current_state` and `source_backend`
back to what they were before the job started, and marks the job `cancelled`.
The source `DATABASE_URL` is preserved intact — no `.env` change is written.

## Pre-PNR vs post-PNR cancel

There is a point-of-no-return (PNR) in the migration: the step where the
applier rewrites `/opt/agnes/.env` and flips `current_state` to the target
backend (`current_step >= flip_backend`). Once that step is reached, `cancel`
returns **409 Conflict** — the migration completed and the app is already
running on the new backend. Do NOT attempt to recover by hand-editing `.env`;
the state machine is already consistent. If you want to go back to the
previous backend, use the appropriate reverse migration (e.g. DuckDB → side
car → DuckDB via rollback — see [Rollback to DuckDB](#rollback-to-duckdb)).

## Stuck-running recovery

The applier writes a `<job-id>.alive` heartbeat file every tick while a job
is running. If `status=running` with no JSON update for more than 120 seconds
(i.e. the heartbeat has gone stale), the applier auto-marks the job `failed`
on its next tick, allowing a fresh migration to be triggered. Operators
normally do not need to intervene. The only situation that requires manual
action is if the applier timer itself has stopped — in that case follow the
"Migrating an existing VM" steps at the top of this runbook to re-initialise
the unit.

## URL alias detection (migrate-onto-self guard)

The `POST /api/admin/db/migrate` endpoint rejects requests where the target
URL resolves to the same physical database as the current backend.
"Same database" detection is server-side: `host/db` and `host:5432/db` are
normalised before comparison, as are credential-only differences and
driver-prefix variants (`postgresql://` vs `postgresql+psycopg://`). Operators
do not need to format URLs identically — they just need to ensure the target
URL actually points to a different database instance. The error is a
**400 Bad Request** with code `url_alias_same_db`.

## URL redaction

`GET /api/admin/db/job/<id>` (and the matching CLI `agnes admin db job <id>`)
returns connection URLs with the password replaced by `****`. The admin UI
never shows the plaintext password. If you need the real URL — for example to
run a manual `psql` check — SSH into the VM and read the JSON job file
directly under `/data/state/jobs/<id>.json`. The file is written `0600` and
readable only by `root` and the `agnes-applier` service account.

See the [Migrating an existing VM to the non-root applier](#migrating-an-existing-vm-to-the-non-root-applier-one-time-phase-81) section above for the service account setup if `agnes-applier` is not yet present on your VM.

---

## What changed

- **Postgres is now a side-car container on the customer-instance VM.** The
  upstream `customer-instance` Terraform module provisions a per-instance
  password in Secret Manager (`agnes-<customer>-postgres-password`) and the
  VM startup-script writes `POSTGRES_PASSWORD`, `DATABASE_URL` and
  `COMPOSE_FILE` into `/opt/agnes/.env` before `docker compose up`.
- **Persistence lives on the existing data disk.** The `postgres_data` named
  volume is bound to `/data/postgres`, which is already covered by your daily
  snapshot policy. The startup-script `chown`s the directory to uid `70`
  (the `postgres:16-alpine` container user) before the side-car boots.
- **App-state writes route to Postgres** via the factory in
  `src/repositories/__init__.py` when `DATABASE_URL` is set. The DuckDB
  analytics layer — `analytics.duckdb`, parquet views, the BigQuery extension
  — is **unchanged**. Only the system tables (users, audit_log, RBAC,
  table_registry, sync_state, recipes, memory domains, …) move to PG.
- **Two one-shot services run on every `docker compose up`.** `migrate`
  runs `alembic upgrade head` against the new database; `data-migrate` runs
  `python -m scripts.migrate_duckdb_to_pg` to copy any remaining rows from
  the DuckDB `system.duckdb` snapshot into PG. Both are idempotent — first
  deploy backfills, subsequent deploys are no-ops. `app` and `scheduler`
  both `depends_on: { data-migrate: { condition: service_completed_successfully } }`
  so no request hits a partially-migrated database.
- **Fresh deployments (no prior DuckDB) boot too.** On a brand-new data
  volume there is no `system.duckdb`; the compose `data-migrate` command
  passes `--missing-source-ok`, which turns the missing source into a
  logged no-op (exit 0) so the boot proceeds straight to an
  alembic-seeded PG. Operator-driven runs of
  `python -m scripts.migrate_duckdb_to_pg` WITHOUT the flag still exit 2
  on a missing source — during a real cutover a missing `system.duckdb`
  means a mis-mounted volume, not a fresh install.

---

## Admin UI / CLI (recommended)

As of the state-machine release, app-state DB transitions are admin-driven
through `/admin/server-config` (UI) or the `agnes admin db` CLI. The flows
below replace ad-hoc `.env` editing for the common DuckDB → side-car
and DuckDB → managed (cloud) PG transitions.

### Web UI

1. Navigate to `/admin/server-config`.
2. Scroll to the **Database backend** section.
3. The card shows the current backend (`duckdb` / `side_car` / `cloud`)
   plus the allowed next transitions.
4. Click the desired transition (e.g. "Enable side-car Postgres") and
   confirm in the modal. A progress card appears with live job status.
5. Wait for the "Migration complete" banner (typically 2–3 minutes on a
   fresh instance; longer if `audit_log` is large).
6. Reload `/admin/server-config` and verify the card shows the new
   backend.

### CLI

```bash
# Show current state — backend, redacted URL, allowed next transitions
agnes admin db state

# Migrate to side-car PG (password generated by the host applier)
agnes admin db migrate side_car

# Migrate to managed cloud PG (interactive: prompts for connection string)
agnes admin db migrate cloud
# Or non-interactive:
agnes admin db migrate cloud \
  --cloud-url 'postgresql+psycopg://agnes:PASS@host:5432/agnes'

# Poll a running job
agnes admin db job <job_id>

# Cancel a running job (only valid pre-flip — once the .env is rewritten
# and the stack has restarted, cancel becomes a no-op)
agnes admin db cancel <job_id>
```

Behind the scenes the API records a `db_state_machine.target_state`
intent + a `db_migration_job`; the host-side `agnes-state-applier.timer`
runs `scripts.migrate_duckdb_to_pg` (or the cloud equivalent) and then
rewrites `/opt/agnes/.env` + `docker compose up -d` once the data copy
verifies. The DuckDB system snapshot is gzipped under
`/data/state/backups/duckdb-pre-<target>-<ts>.duckdb.gz` before the flip
— see [Rollback to DuckDB](#rollback-to-duckdb) for the restore path.

During a DuckDB→PG transition the applier first runs a best-effort DuckDB
`CHECKPOINT` — folding any `.wal` sidecar into the main file so the
pre-flip gzip backup is self-contained even if the app was hard-killed —
and then copies into Postgres. On a **retry** after a partial failure it
truncates the target PG state tables and rebuilds them from the current
DuckDB source, so a nonzero `target_rows_reset` in the job record is
deliberate (it stops the copy's bare `ON CONFLICT DO NOTHING` from keeping
stale rows out of a prior attempt), **not** data loss. The docker-compose
`data-migrate` one-shot is a separate path and is never reset this way.

### Manual smoke (dev instance)

After deploying this release to a dev instance, run the following on the VM
to confirm the flow end-to-end:

1. Baseline:
   ```bash
   curl -fsS http://localhost:8000/api/admin/db/state \
     -H "Authorization: Bearer $PAT"
   # Expect: {"backend":"duckdb", ...}
   ```
2. Trigger migration from UI or CLI:
   ```bash
   agnes admin db migrate side_car
   ```
3. Watch the host-side actions:
   ```bash
   docker ps                         # postgres container appears within ~1 min
   docker logs agnes-postgres-1 -f   # "ready to accept connections"
   ```
4. Verify the flip:
   ```bash
   agnes admin db state
   # Expect: {"backend":"side_car", ...}
   curl -fsS http://localhost:8000/api/health
   # Expect: HTTP 200
   ```
5. Inspect the pre-flip backup:
   ```bash
   ls -la /data/state/backups/
   # Expect: duckdb-pre-side_car-YYYYMMDD-HHMMSS.duckdb.gz
   ```

---

## Standard deploy

The `.env`-editing flow below remains supported for operators who prefer
to drive the cut-over directly (or who are bringing up a brand-new VM
where the state machine hasn't yet been initialised). For an existing
running instance, prefer the [Admin UI / CLI](#admin-ui--cli-recommended)
flow above.

1. **Bump the module pin** in your customer infra repo:

   ```hcl
   module "agnes" {
     source = "git::https://github.com/keboola/agnes-the-ai-analyst.git//infra/modules/customer-instance?ref=infra-vX.Y.Z"
     # ...
   }
   ```

2. **`terraform apply`.** This creates the new Secret Manager triple
   (`google_secret_manager_secret` + `_secret_version` + IAM binding for the
   VM service account) and grants `roles/secretmanager.secretAccessor` to the
   VM. No VM re-create is required — the secret is read at boot by the
   startup-script.

3. **Roll the VM forward.** SSH in and run the auto-upgrade script (or wait
   for the next cron tick — default cadence is 5 min):

   ```bash
   sudo /usr/local/bin/agnes-auto-upgrade.sh
   ```

   This re-runs the startup-script flow: pulls the new password from Secret
   Manager, ensures `/data/postgres` exists with uid `70` ownership, writes
   the three env vars into `/opt/agnes/.env`, and runs
   `docker compose pull && docker compose up -d`.

4. **Verify the app is healthy:**

   ```bash
   curl -fsS http://localhost:8000/api/health
   ```

   Expect HTTP 200 and a JSON body.

5. **Confirm new writes land in PG:**

   ```bash
   cd /opt/agnes
   docker compose exec postgres psql -U agnes -d agnes -c "SELECT count(*) FROM audit_log;"
   ```

   Drive a write (e.g. log in via the UI) and re-run the count — it should
   increment.

---

## Verify after deploy

Spot checks to run once the auto-upgrade finishes:

**All services healthy and migrate/data-migrate exited 0:**

```bash
cd /opt/agnes
docker compose ps
```

Expect `app`, `scheduler`, `postgres` in `running` (healthy where a healthcheck
is defined); `migrate` and `data-migrate` in `exited (0)`.

**Schema head matches the latest revision:**

```bash
docker compose exec postgres psql -U agnes -d agnes \
  -c "SELECT version_num FROM alembic_version;"
```

Compare against the latest revision in `alembic/versions/` shipped with the
release (the highest `NNNN_` prefix).

**Row-count sanity for one migrated table** — compare PG to the DuckDB
snapshot that lives on disk untouched by this cut-over:

```bash
docker compose exec postgres psql -U agnes -d agnes \
  -c "SELECT count(*) FROM users;"

docker compose exec app python -c "
import duckdb
c = duckdb.connect('/data/state/system.duckdb', read_only=True)
print(c.execute('SELECT count(*) FROM users').fetchone())
"
```

The two counts should match. Repeat for `audit_log`, `table_registry`,
`sync_state`, and any other table the data-migrate script copies (see
`scripts/migrate_duckdb_to_pg.py` for the full list).

---

## Provisioning a Cloud SQL Postgres via Terraform

GCP operators can provision the managed Postgres alongside the VM
using the bundled `cloud-pg` module. Minimal root configuration:

```hcl
# Pre-existing Secret Manager secret holding the app-user password.
# Provision out-of-band so the plaintext never enters TF code:
#   gcloud secrets create agnes-cloud-pg-app-password --replication-policy=automatic
#   gcloud secrets versions add agnes-cloud-pg-app-password --data-file=- <<< '…'
data "google_secret_manager_secret" "agnes_cloud_pg_password" {
  project   = var.project
  secret_id = "agnes-cloud-pg-app-password"
}

module "agnes_cloud_pg" {
  source = "git::https://github.com/keboola/agnes-the-ai-analyst.git//infra/modules/cloud-pg?ref=infra-vX.Y.Z"

  name              = "agnes-prod-pg"
  project           = var.project
  region            = "europe-west1"          # match the VM region
  tier              = "db-custom-2-7680"      # 2 vCPU / 7.5 GB
  postgres_version  = "POSTGRES_16"
  storage_size_gb   = 50
  backup_enabled    = true
  high_availability = true                    # prod; set false for dev

  authorized_cidrs = [
    { name = "agnes-vm", value = "${module.agnes_vm.external_ip}/32" },
  ]

  password_secret_id = data.google_secret_manager_secret.agnes_cloud_pg_password.id
}

output "agnes_cloud_pg_url_template" {
  value = module.agnes_cloud_pg.url_template
}
```

Module docs: [`infra/modules/cloud-pg/README.md`](../infra/modules/cloud-pg/README.md).

After `terraform apply`:

1. Note the `url_template` output — `postgresql+psycopg://agnes:<PASSWORD>@<ip>:5432/agnes`.
2. Substitute `<PASSWORD>` with the value from
   `gcloud secrets versions access latest --secret=agnes-cloud-pg-app-password`.
3. Open `/admin/database` → click **Migrate to managed Postgres** →
   paste the URL. The state machine drives the rest.

Equivalents for other clouds: fork the module and swap
`google_sql_database_instance` for `aws_db_instance` (RDS) or
`azurerm_postgresql_flexible_server` (Azure). The `customer-instance`
module's interface — variable names, Secret Manager pattern,
authorized-network plumbing — is the contract; adapters per cloud
are otherwise mechanical.

---

## Operator override — managed Postgres (manual, no Terraform)

If you don't want the side-car container — for example to share one Cloud
SQL / RDS / Supabase / Neon instance across several Agnes VMs, or to keep
backups in your existing managed-DB tier — point the app at a managed PG and
disable the overlay:

1. **SSH into the VM** and edit `/opt/agnes/.env`:

   ```bash
   sudo -e /opt/agnes/.env
   ```

   - Replace the side-car URL
     `DATABASE_URL=postgresql+psycopg://agnes:...@postgres:5432/agnes`
     with your managed-PG URL, e.g.
     `DATABASE_URL=postgresql+psycopg://agnes:...@<your-managed-host>:5432/agnes`.
   - Remove the line
     `COMPOSE_FILE=docker-compose.yml:docker-compose.postgres.yml:docker-compose.host-mount.yml`
     (or shorten it to the baseline
     `COMPOSE_FILE=docker-compose.yml:docker-compose.host-mount.yml`) so the
     postgres overlay is not included.

2. **Restart the stack:**

   ```bash
   sudo systemctl restart agnes
   # or, equivalently:
   cd /opt/agnes && docker compose up -d
   ```

3. **Run `alembic upgrade head` against the managed PG** as part of your
   deploy pipeline (one-shot, no long-running service required):

   ```bash
   docker run --rm \
     -e DATABASE_URL="postgresql+psycopg://agnes:...@<your-managed-host>:5432/agnes" \
     ghcr.io/keboola/agnes-the-ai-analyst:stable \
     alembic upgrade head
   ```

   If this is the first cut-over from DuckDB, also run the data-migrate
   step once against the managed URL (it's idempotent — safe to re-run):

   ```bash
   docker run --rm \
     -v /data/state:/data/state:ro \
     -e DATABASE_URL="postgresql+psycopg://agnes:...@<your-managed-host>:5432/agnes" \
     ghcr.io/keboola/agnes-the-ai-analyst:stable \
     python -m scripts.migrate_duckdb_to_pg
   ```

   The Terraform module still provisions the side-car password secret in
   this layout — it's unused but harmless. If you want to drop it, override
   the `enable_postgres_secret` variable (or whatever your module pin
   exposes) in your TF root.

---

## Rollback to DuckDB

The DuckDB system database at `/data/state/system.duckdb` is **never deleted
by this stack**. The cut-over copies rows into PG; it does not remove the
source file. Rolling back is therefore just turning the overlay off again:

1. **SSH in, edit `/opt/agnes/.env`:**

   ```bash
   sudo -e /opt/agnes/.env
   ```

   - Remove the `DATABASE_URL=...` line.
   - Remove or shorten `COMPOSE_FILE` to omit the postgres overlay.

2. **Roll the VM forward:**

   ```bash
   sudo /usr/local/bin/agnes-auto-upgrade.sh
   # or, equivalently:
   cd /opt/agnes && docker compose up -d
   ```

3. **Verify:**

   ```bash
   curl -fsS http://localhost:8000/api/health
   ```

   Drive a write (log in, register a table) and confirm it lands in DuckDB:

   ```bash
   docker compose exec app python -c "
   import duckdb
   c = duckdb.connect('/data/state/system.duckdb', read_only=True)
   print(c.execute('SELECT count(*) FROM audit_log').fetchone())
   "
   ```

The PG side-car keeps its data on `/data/postgres` for any future
re-cutover; **nothing is destroyed by rollback**. If you want to reclaim
the disk space, `sudo rm -rf /data/postgres/*` after confirming the
rollback is healthy.

---

## `POSTGRES_PASSWORD` rotation

Rotate the side-car password when a credential leaks or on a scheduled
cadence. The Secret Manager triple is the source of truth; the VM
startup-script reads from it on every boot but does **not** re-read mid-life,
so an `.env` edit + `docker compose restart` is required after writing a new
secret version:

```bash
# 1. Generate a new password (URL-safe — no /, +, =).
NEW=$(openssl rand -base64 32 | tr -d /=+ | head -c 32)

# 2. Add a new version to the Secret Manager secret.
echo -n "$NEW" | gcloud secrets versions add \
  agnes-<customer>-postgres-password --data-file=-

# 3. On the VM, edit /opt/agnes/.env and replace POSTGRES_PASSWORD
#    (and the password embedded in DATABASE_URL) with the new value.
sudo -e /opt/agnes/.env

# 4. Restart only the affected services.
cd /opt/agnes
docker compose restart postgres app scheduler
```

After step 4, also re-run the password update inside PG itself if the
existing role's password needs to change at the database level (the
side-car only sets `POSTGRES_PASSWORD` at first-init):

```bash
docker compose exec postgres psql -U agnes -d agnes \
  -c "ALTER ROLE agnes WITH PASSWORD '<new-password>';"
```

Then disable any older secret versions in Secret Manager once the new
password is confirmed in use.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `migrate` exits non-zero, `app` refuses to start | Failed `alembic upgrade head` — constraint violation, missing extension, or a data-shape conflict against an existing PG | `cd /opt/agnes && docker compose logs migrate`; address the offending revision (often a hand-applied schema change on a managed PG); re-run `docker compose up -d`. |
| `data-migrate` hangs > 5 min | Very large `audit_log`, a stuck PG lock, or DuckDB file held open by another process | `docker compose logs -f data-migrate`; if a single table is the culprit, run the script manually with `--only <table>` to triage: `docker compose run --rm data-migrate python -m scripts.migrate_duckdb_to_pg --only audit_log`. |
| `/data/postgres` permission denied at container boot | Startup-script didn't run, or ran without the `chown -R 70:70 /data/postgres` step (older infra pin) | SSH in: `sudo chown -R 70:70 /data/postgres && cd /opt/agnes && docker compose restart postgres`. |
| Disk full on `/data` | PG data + DuckDB snapshot + parquet uploads exceed the data-disk size | Resize the `data` disk in your customer infra TF (the `data_disk_size_gb` variable on the `customer-instance` module); `terraform apply`; reboot the VM so the kernel picks up the larger device, then `sudo resize2fs /dev/disk/by-id/google-data-1` (or your distro's equivalent) to grow the filesystem. |
| `psql: FATAL: password authentication failed for user "agnes"` after a rotation | `/opt/agnes/.env` updated but the PG role's password wasn't changed inside the database | `docker compose exec postgres psql -U agnes -d agnes -c "ALTER ROLE agnes WITH PASSWORD '<new>';"` — see the rotation section above. |
| App logs show `sqlalchemy.exc.OperationalError: connection refused` | `postgres` service is unhealthy or still starting | `docker compose ps` — if postgres is restarting, `docker compose logs postgres`; if it's healthy but the app can't reach it, confirm `DATABASE_URL` host is `postgres` (the compose service name), not `localhost`. |
| `COMPOSE_FILE` env not honored — overlay services missing | Older `agnes-auto-upgrade.sh` (pre-Task 2B.1) that didn't `export COMPOSE_FILE` from `/opt/agnes/.env` | Pull the latest `agnes-auto-upgrade.sh` from the upstream `scripts/ops/` directory and replace `/usr/local/bin/agnes-auto-upgrade.sh`; or invoke `docker compose -f docker-compose.yml -f docker-compose.postgres.yml -f docker-compose.host-mount.yml up -d` explicitly once to recover. |
| Users report failed `agnes auth login` during migration window | In-flight `cli_auth_codes` row was minted in DuckDB just before the flip; after the flip the app reads from PG where the row doesn't exist (the migrator deliberately skips this table — codes are ~2-min TTL session-local data, see `DUCKDB_ONLY_TABLES` in `tests/db_pg/test_schema_parity.py`). | One-shot transient — affected users just retry `agnes auth login` and the new code lands directly in PG. Worth mentioning in user-facing maintenance announcements if you expect login traffic during the cutover. |

---

## Related docs

- [`docs/migrations.md`](migrations.md) — Alembic conventions, repository
  cross-engine contract, the DuckDB → PG data-migrate script.
- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — full deploy guide (compose
  topology, TLS, image channels).
- [`CHANGELOG.md`](../CHANGELOG.md) — release notes; look for the entry that
  bumps the `customer-instance` module to the cut-over version.
