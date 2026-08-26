# Server Operations

Operational guide for the Agnes Docker deployment.

## Basic Information

| Parameter | Value |
|-----------|-------|
| GCP Project | your-gcp-project |
| Zone | europe-north1-a |
| Machine type | e2-medium |
| OS | Debian 12 (bookworm) |
| External IP | YOUR_SERVER_IP |

## Docker Compose

### Starting and stopping

```bash
# Start all services (app + scheduler)
docker compose up -d

# Include optional services (Telegram bot, etc.)
docker compose --profile full up -d

# Stop all services
docker compose down

# Restart a single service
docker compose restart app

# Pull latest images and redeploy
docker compose pull && docker compose up -d
```

### Status

```bash
# List running containers and their state
docker compose ps

# Resource usage
docker stats
```

## Log Viewing

```bash
# All services, follow
docker compose logs -f

# Single service
docker compose logs -f app
docker compose logs -f scheduler

# Last N lines
docker compose logs --tail=100 app

# Since a timestamp
docker compose logs --since=1h app
```

Application logs are written to stdout/stderr and captured by Docker.

## Health Check

```bash
# Quick check
curl https://your-instance.example.com/health

# With response body
curl -s https://your-instance.example.com/health | python3 -m json.tool
```

Expected response:
```json
{"status": "ok"}
```

The `/health` endpoint also checks DuckDB connectivity and returns `503` if
the database is unavailable.

## Data Sync

### Trigger a manual sync

```bash
# Via API
curl -X POST http://localhost:8000/api/sync/trigger

# Sync a single table
curl -X POST "http://localhost:8000/api/sync/trigger?table=table_name"
```

### Check sync status

```bash
curl -s http://localhost:8000/api/sync/status | python3 -m json.tool
```

## Data Structure

```
/data/                          # Persistent volume (GCP pd-balanced, snapshotted)
├── state/
│   └── system.duckdb           # Table registry, users, sync state, audit log
├── analytics/
│   └── server.duckdb           # Master analytics DB (rebuilt on startup)
└── extracts/
    └── {source_name}/
        ├── extract.duckdb      # Per-source extract DB with views
        └── data/               # Parquet files (local sources: Keboola, Jira)
            └── *.parquet
```

`system.duckdb` is the source of truth for configuration. Back it up before
any destructive operation.

## Admin CLI

```bash
# List registered tables
docker compose exec app agnes admin list-tables

# Register a new table
docker compose exec app agnes admin register-table

# User management
docker compose exec app agnes admin list-users

# Query data directly
docker compose exec app agnes query "SELECT * FROM my_table LIMIT 10"
```

## Application Deployment

Application is deployed via Docker image. The recommended workflow:

1. Push changes to the `main` branch
2. CI builds and pushes a new image
3. On the server, pull and restart:
   ```bash
   cd <install-dir>
   docker compose pull
   docker compose up -d
   ```

To pin a specific image version, set the tag in `docker-compose.yml` before deploying.

### Environment configuration

```bash
# Edit .env (never commit this file)
nano <install-dir>/.env

# Restart app to apply changes
docker compose restart app
```

See `config/.env.template` for the full variable reference and
`config/instance.yaml.example` for instance configuration.

## Monitoring

### GCP Cloud Monitoring

The VM reports metrics via the Google Cloud Ops Agent:

```bash
# Check agent status
sudo systemctl status google-cloud-ops-agent
```

Key metrics in GCP Console > Monitoring > Metrics Explorer:
- `agent.googleapis.com/disk/percent_used` — watch `/data` partition
- `agent.googleapis.com/memory/percent_used`
- `agent.googleapis.com/cpu/utilization`

A disk space alert fires when `/data` exceeds 85% for 5 minutes.

### Container logs (GCP Cloud Logging)

On GCE deployments the container stdout/stderr (app INFO + uncaught-exception
tracebacks, scheduler, etc.) ships to **GCP Cloud Logging** via Docker's
`gcplogs` driver, engaged by the `docker-compose.gcp-logging.yml` overlay.
Activation is **placement + probe driven**: the overlay ships baked into the
image, the Terraform startup script extracts it into `/opt/agnes/` (and removes
it again when `enable_gcp_logging=false`), and every `COMPOSE_FILE` builder
(the boot script, `agnes-auto-upgrade.sh`, `agnes-state-applier.sh` — all via
`scripts/ops/agnes-compose-file.sh::agnes_gcp_logging_active`) appends it only
when the file is present on disk **and** the driver probe has armed
`/opt/agnes/.gcp-logging-ok`. The probe (`agnes_gcp_logging_probe`) starts a
no-op container with `--log-driver=gcplogs` at boot — and on any auto-upgrade
tick where the overlay sits marker-less — because Docker refuses to *start* a
container whose log driver cannot initialize: an armed overlay without
`roles/logging.logWriter` on the VM service account (the module grants it with
`enable_gcp_logging`) would otherwise take the instance down on the next
routine recreate. A failed probe logs a warning and leaves the stack on
`json-file`. Non-GCP hosts never get the file and keep the default `json-file`
driver. To enable it on a GCE host manually, drop the overlay into
`/opt/agnes/docker-compose.gcp-logging.yml` and let the next auto-upgrade tick
probe + recreate. Entries land under
resource `gce_instance` (next to the VM/system logs), logName
`gcplogs-docker-driver`, tagged with `jsonPayload.instance.name` /
`jsonPayload.container.name`. The app's own JSON log line (with its `lvl`,
`logger`, `request_id`, …) is preserved verbatim as a string in
`jsonPayload.message`.

```bash
# Tail app logs from the laptop (last 10 min)
gcloud logging read \
  'logName:"gcplogs-docker-driver" AND jsonPayload.container.name="/agnes-app-1"' \
  --project=<gcp-project> --limit=50 --freshness=10m \
  --format='value(timestamp, jsonPayload.message)'

# Only error-level app lines (the level lives inside jsonPayload.message)
gcloud logging read \
  'logName:"gcplogs-docker-driver" AND jsonPayload.container.name="/agnes-app-1" AND jsonPayload.message:"\"lvl\": \"ERROR\""' \
  --project=<gcp-project> --limit=50 --freshness=1h

# Confirm a container is actually on the gcplogs driver
docker inspect agnes-app-1 --format '{{.HostConfig.LogConfig.Type}}'   # → gcplogs
```

`docker logs` / `docker compose logs` keep working locally via Docker's
dual-logging cache (Docker ≥ 20.10). Note: `gcplogs` does not parse the app's
JSON line, so Cloud Logging assigns no native `severity` — filter on the `lvl`
field inside `jsonPayload.message` as shown above. Non-GCP deployments omit the
overlay and keep the default `json-file` driver.

### Local checks

```bash
# Disk usage
df -h /data

# Data directory breakdown
du -sh /data/*

# Container resource usage
docker stats --no-stream
```

## Backup and Disaster Recovery

The `/data` persistent disk has daily GCP snapshot schedules with 14-day retention.

```bash
# List existing snapshots
gcloud compute snapshots list --project=your-gcp-project \
  --filter="sourceDisk:data-disk" --sort-by=~creationTimestamp

# Create a manual snapshot before risky operations
gcloud compute disks snapshot data-disk \
  --project=your-gcp-project \
  --zone=europe-north1-a \
  --snapshot-names=data-disk-$(date +%Y%m%d)-manual
```

See `disaster-recovery.md` for full recovery procedures.

## Web Application

The FastAPI app is available at `https://your-instance.example.com`.

- **Google OAuth**: restricted to `allowed_domain` set in `config/instance.yaml`
- **Email magic link**: available out of the box (no external service required)
- **Admin API**: `POST /api/admin/register-table` (register), `PUT /api/admin/registry/{id}` (update), `GET /api/admin/registry` (list) — manage tables
- **Sync API**: `POST /api/sync/trigger` — trigger data extraction

### Google OAuth setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
2. Create OAuth 2.0 Client ID (Web application)
3. Authorized JavaScript origins: `https://your-instance.example.com`
4. Authorized redirect URIs: `https://your-instance.example.com/auth/google/callback`
5. Add `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` to `.env`

## Jira Webhook Integration

Receives webhooks from Atlassian Jira for real-time issue sync.

### Configuration

Add to `.env`:
```bash
JIRA_WEBHOOK_SECRET=<generate with: python -c "import secrets; print(secrets.token_hex(32))">
JIRA_API_TOKEN=<API token from https://id.atlassian.com/manage-profile/security/api-tokens>
```

Add to `config/instance.yaml`:
```yaml
jira:
  domain: "your-org.atlassian.net"
  email: "integration-user@your-domain.com"
  webhook_secret: "${JIRA_WEBHOOK_SECRET}"
  api_token: "${JIRA_API_TOKEN}"
```

### Jira webhook setup

1. Go to Jira Admin > System > WebHooks
2. Create new webhook:
   - **URL**: `https://your-instance.example.com/webhooks/jira`
   - **Secret**: same value as `JIRA_WEBHOOK_SECRET`
   - **Events**: Issue created/updated/deleted, Comment created/updated, Attachment created

### Monitoring

```bash
# Health check
curl https://your-instance.example.com/webhooks/jira/health

# Webhook processing logs
docker compose logs -f app | grep -i jira
```

## Troubleshooting

### Container won't start

```bash
docker compose logs app | tail -50
# Look for configuration or DuckDB errors at startup
```

### DuckDB locked

If the app crashes mid-write, DuckDB may hold a write lock:

```bash
docker compose down
# Wait a few seconds, then:
docker compose up -d
```

DuckDB releases locks when the process exits cleanly. A forced restart resolves
most lock issues.

### Sync failing

```bash
# Check sync logs
docker compose logs app | grep -i "sync\|error\|exception"

# Verify data source credentials in .env
docker compose exec app agnes admin list-tables
```

### Out of disk space

```bash
df -h /data
du -sh /data/extracts/*

# Remove old parquet partitions if needed (check with orchestrator first)
# Trigger a fresh snapshot before any manual cleanup
```
