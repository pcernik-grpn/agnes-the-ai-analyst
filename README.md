# Agnes — AI Harness

Agnes is a source-available, self-hosted AI harness for organizations: one governed home for the data, agents, skills, memory, and apps your organization's AI works with. Every surface — web chat, Slack, Telegram, MCP, CLI, and a public agent API — runs through the same RBAC, audit, and credential-brokering spine.

- **Data** — extracts data from configured sources into DuckDB, serves it via a FastAPI backend, and distributes RBAC-filtered Parquet files to analysts who query them locally using Claude Code and DuckDB. A semantic layer (canonical metrics, glossary) keeps agents computing business numbers the same way humans do.
- **Agents** — named, scoped agent profiles with their own tokens, pinned models, monthly token budgets, and private memory — callable one-shot or as streaming multi-turn sessions over a public REST/SSE API, with webhooks, artifacts, and structured JSON output. Agent scope is enforced at request time; an agent can never exceed its owner's grants.
- **Skills & plugins** — aggregates curated Claude Code marketplaces into one RBAC-filtered feed, with a store for publishing skills (static + LLM security review) and an in-product Studio for authoring them.
- **Corporate memory & knowledge** — governed organizational knowledge: memory domains, session mining with consent, document collections with hybrid search, and maintained digests — all searchable from one box and shipped offline to analyst laptops.
- **Data apps** — hosts user-authored web applications next to the data: push-to-deploy git repos, RBAC-gated ingress, auto-sleep/wake — deployable end-to-end by the chat agent itself.
- **Agent surfaces** — web chat on sandboxed microVMs (credentials brokered server-side, never inside the sandbox), Slack and Telegram bots, an OAuth 2.1 remote MCP connector, and a headless CLI, all behind the same access control.

The data engine is the platform's core. Each data source produces a self-describing `extract.duckdb` file. The `SyncOrchestrator` attaches all extract databases into a master `analytics.duckdb`, making every table available through a unified view layer without copying data unnecessarily.

Agnes runs as a single container for small teams, or splits into api/gateway/worker roles with Postgres app-state, Redis coordination, and a durable job queue for horizontal scale-out.

## Architecture: extract.duckdb Contract

Every connector produces the same output structure:

```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

The orchestrator scans `/data/extracts/*/extract.duckdb`, attaches each into `analytics.duckdb`, and creates master views.

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│   Keboola    │  │   BigQuery   │  │   Jira       │
│  extractor   │  │  extractor   │  │  webhooks    │
│ (DuckDB ext) │  │ (remote BQ)  │  │ (incremental)│
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       ▼                 ▼                 ▼
   extract.duckdb    extract.duckdb    extract.duckdb
   + data/*.parquet  (views → BQ)      + data/*.parquet
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
              SyncOrchestrator.rebuild()
              ATTACH → master views in analytics.duckdb
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
          FastAPI                  CLI
          (serve)               (agnes pull)
```

## Supported Data Sources

| Mode | Distribution | Sources | Use when |
|------|--------------|---------|----------|
| **Batch pull** (`local`) | Parquet on disk, scheduled | Keboola | Source has a native bulk-export and the table fits on disk |
| **Materialized SQL** (`materialized`) | Parquet on disk, scheduled query | BigQuery, Keboola | Source table is too large to mirror as-is; you want a curated subset / aggregate on disk |
| **Remote attach** (`remote`) | View only, no download | BigQuery | Table is too large to materialize; latency cost of remote query is acceptable |
| **Real-time push** | Incremental parquet | Jira | Source is event-driven and you need sub-minute freshness |

The first three modes are what `agnes pull` distributes to analysts. The fourth is server-side only — analysts query Jira data through the same `agnes pull`-distributed parquets.

Admins manage per-source registrations through the `/admin/tables` UI (per-connector tabs for BigQuery / Keboola / Jira) or the `agnes admin register-table` CLI; per-row "Manage access" deep-links to `/admin/access` for granting tables to user groups via `resource_grants(group, ResourceType.TABLE, table_id)`.

Analysts get a closed loop with Claude Code: `agnes init` writes `<workspace>/.claude/settings.json` with a SessionStart hook (a detached `agnes update --quiet` that converges the CLI, workspace, plugins and pulls fresh RBAC-filtered parquets) and a SessionEnd hook (`agnes push --quiet`) so every session starts current and ends with the session log uploaded back.

Adding a new source means creating `connectors/<name>/extractor.py` that produces `extract.duckdb` with a `_meta` table (`table_name`, `description`, `rows`, `size_bytes`, `extracted_at`, `query_mode`). The orchestrator attaches it automatically.

## Quick Start with Docker

```bash
# Clone the repository
git clone https://github.com/keboola/agnes-the-ai-analyst.git
cd agnes-the-ai-analyst

# Copy and edit configuration
cp config/instance.yaml.example config/instance.yaml
cp config/.env.template .env
# Edit both files for your environment

# Start the app and scheduler
docker compose up

# Start with all optional services (Telegram bot, etc.)
docker compose --profile full up

# Start with TLS (Caddy on :443 with corporate-CA certs from /data/state/certs)
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.tls.yml \
    --profile tls up -d
```

Once running, the FastAPI app is available at `http://localhost:8000` (or `https://$DOMAIN` in TLS mode). See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for cert provisioning + auto-rotation via `scripts/ops/agnes-tls-rotate.sh`. Trigger a manual sync:

```bash
curl -X POST http://localhost:8000/api/sync/trigger
```

## Local sync & auto-update

Analysts run Claude Code against a local DuckDB built from RBAC-filtered parquets pulled from the server. `agnes pull` is the distribution path:

```bash
agnes pull             # delta-pull: manifest → MD5 compare → download changed → rebuild views
agnes pull --quiet     # same, no progress output (for hooks/cron)
agnes push  # push session jsonl + CLAUDE.local.md back to the server
```

`agnes init` writes Claude Code lifecycle hooks into `<workspace>/.claude/settings.json`:

- `SessionStart` → one detached `agnes update --quiet` — converges the CLI, workspace template, Agnes-owned hooks/statusLine/commands, marketplace plugins, and pulls fresh data; runs in the background so it never blocks session start
- `SessionEnd` → `agnes push --quiet` — uploads notes and session log

Hooks live at workspace level so they only fire in this analyst workspace, not in unrelated Claude Code sessions on the same machine.

### Admin: which tables auto-sync to whom

The auto-sync set per analyst is the intersection of:

1. Tables with `query_mode IN ('local', 'materialized')` — these have parquets on disk and end up in the manifest
2. Tables granted to one of the analyst's groups via `resource_grants(group, ResourceType.TABLE, table_id)` (see [`docs/RBAC.md`](docs/RBAC.md))

To enroll a new table for auto-sync, register it (or update its `query_mode`) and grant it to the relevant groups in `/admin/access`. New analysts get the same set on their next `agnes pull`.

For BigQuery, register a `query_mode='materialized'` table with a SQL body:

```bash
agnes admin register-table orders_90d \
    --source-type bigquery \
    --query-mode materialized \
    --query @docs/queries/orders_90d.sql \
    --schedule "every 6h"
```

The scheduler runs the query through the DuckDB BigQuery extension on each tick that's due, writes the result as a parquet, and the analyst picks it up on the next `agnes pull`. Cost guardrail: `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB) — operations exceeding the BQ dry-run estimate are skipped.

## Development Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
uv pip install ".[dev,server]"

# Run FastAPI locally with hot reload
uvicorn app.main:app --reload

# Run the test suite
pytest tests/ connectors/ -v
```

## Project Structure

```
├── src/                    # Core engine
│   ├── db.py               # DuckDB schema (system.duckdb, analytics.duckdb)
│   ├── orchestrator.py     # SyncOrchestrator — ATTACHes extract.duckdb files
│   ├── repositories/       # DuckDB-backed CRUD (sync_state, table_registry, users, etc.)
│   ├── profiler.py         # Data profiling
│   ├── catalog_export.py   # OpenMetadata catalog export
│   └── data_apps/          # Hosted data-apps registry: config.json + container spec builders
├── app/                    # FastAPI application
│   ├── main.py             # App setup, router registration
│   ├── api/                # REST API (sync, data, catalog, admin, auth)
│   ├── auth/               # Auth providers (Google OAuth, email magic link, desktop JWT)
│   └── web/                # HTML dashboard routes
├── connectors/             # Data source connectors (extract.duckdb contract)
│   ├── keboola/            # Keboola: extractor.py (DuckDB extension) + client.py (fallback)
│   ├── bigquery/           # BigQuery: extractor.py (remote-only via DuckDB BQ extension)
│   └── jira/               # Jira: webhook + incremental parquet → extract.duckdb
├── cli/                    # CLI tool (`agnes pull`, `agnes query`, `agnes admin`)
├── services/               # Standalone services (scheduler, telegram_bot, apps_runner sidecar, etc.)
├── scripts/                # Utility + migration scripts
├── config/                 # Configuration templates (instance.yaml.example)
├── docs/                   # Documentation + metric YAML definitions
└── tests/                  # Test suite
```

## Configuration

| File | Purpose |
|------|---------|
| `config/instance.yaml` | Instance-specific settings: branding, data source type, auth provider, Google domain |
| `.env` | Secrets and environment variables — never committed |
| `system.duckdb` `table_registry` table | Table definitions managed via `POST /api/admin/register-table` (or `PUT /api/admin/registry/{id}` to update) or the web UI |

Copy the example to get started:

```bash
cp config/instance.yaml.example config/instance.yaml
```

See `config/instance.yaml.example` for all available options.

## Documentation

**Full index: [docs/README.md](docs/README.md)** — every doc, organized by audience (analyst / operator / developer).

Key entry points:

- [Quickstart](docs/QUICKSTART.md) — local development setup
- [Onboarding Guide](docs/ONBOARDING.md) — end-to-end Terraform deployment into a GCP project (recommended for production)
- [Deployment Guide](docs/DEPLOYMENT.md) — chooses between Terraform and Docker Compose; covers OSS self-host
- [Configuration Reference](docs/CONFIGURATION.md) — `instance.yaml`, env vars, per-instance options
- [Architecture](ARCHITECTURE.md) — orchestrator, extractors, DB layout
- [Security](SECURITY.md) — threat model, trust boundaries, known limitations, operator responsibilities

## Contributing

1. Fork the repository and create a feature branch.
2. Run `pytest tests/ connectors/ -v` to verify all tests pass before opening a pull request.
3. Keep commits focused and messages concise.
4. Open a pull request against `main` with a clear description of the change.

For bugs and feature requests, open a GitHub issue. For **security vulnerabilities**, do not open an issue — follow [SECURITY.md](SECURITY.md).

## License

This project is licensed under the [PolyForm Small Business License 1.0.0](LICENSE).
