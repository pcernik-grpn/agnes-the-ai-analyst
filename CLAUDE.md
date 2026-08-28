# Agnes — AI Harness

Source-available, self-hosted AI harness for organizations — governed data access, agent profiles with a public agent API, skills marketplace, corporate memory, hosted data apps, and agent surfaces (web chat, Slack, Telegram, MCP, CLI). The data engine extracts data from sources into DuckDB, serves via FastAPI, and distributes parquets to analysts who use Claude Code for local analysis; app-state runs on DuckDB or Postgres, single-process or role-split (api/gateway/worker).

Full documentation index: [`docs/README.md`](docs/README.md).

## First-Time Setup

When a user opens this project for the first time, guide them through interactive setup. Ask for:

1. Company domain (e.g. `acme.com`) — used for Google OAuth
2. Data source type — `keboola` / `bigquery` / `local` (`csv` is an accepted
   alias for `local`; there is no CSV *connector*, so it means "no external
   source" — see [`docs/DATA_SOURCES.md`](docs/DATA_SOURCES.md))
3. Instance name (e.g. `Acme Data Analyst`)

Then: copy `config/instance.yaml.example` → `config/instance.yaml` and fill it in, copy `config/.env.template` → `.env` and add data-source credentials, and register tables via the admin API (`POST /api/admin/register-table`) or the web UI at `/admin/tables`.

Full step-by-step (local dev, Docker, TLS) lives in [`docs/QUICKSTART.md`](docs/QUICKSTART.md) and [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). New-instance GCP deployment is [`docs/ONBOARDING.md`](docs/ONBOARDING.md).

## Project Structure

```
├── src/                    # Core engine
│   ├── db.py               # DuckDB schema (system.duckdb, analytics.duckdb)
│   ├── orchestrator.py     # SyncOrchestrator — ATTACHes extract.duckdb files
│   ├── repositories/       # DuckDB-backed CRUD (sync_state, table_registry, users, etc.)
│   ├── profiler.py         # Data profiling
│   └── data_apps/          # Hosted data-apps registry: config.json + container spec builders
├── app/                    # FastAPI application
│   ├── main.py             # App setup, router registration
│   ├── api/                # REST API (sync, data, catalog, admin, auth)
│   └── web/                # HTML dashboard routes
├── connectors/             # Data source connectors (extract.duckdb contract)
│   ├── keboola/            # Keboola: extractor.py (DuckDB extension) + client.py (fallback)
│   ├── bigquery/           # BigQuery: extractor.py (remote-only via DuckDB BQ extension)
│   ├── jira/               # Jira: webhook + incremental parquet → extract.duckdb
│   └── databricks/         # Databricks: SQL-warehouse materialization, per-query remote execution, UC metric-view sync
├── cli/                    # CLI tool (`agnes pull`, `agnes query`, `agnes admin`)
├── app/auth/               # Authentication (FastAPI-based providers)
├── services/               # Standalone services (scheduler, telegram_bot, apps_runner sidecar, etc.)
├── infra/                  # Terraform: reusable `modules/customer-instance` (VM, startup script, systemd units)
├── scripts/                # Utility + migration scripts (`ops/` = VM-side host scripts)
├── config/                 # Configuration templates (instance.yaml.example)
├── docs/                   # Documentation + metric YAML definitions
└── tests/                  # Test suite
```

## Architecture: extract.duckdb Contract

Every data source produces the same output:
```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views
└── data/                   ← parquet files (local sources only)
```

The SyncOrchestrator scans `/data/extracts/*/extract.duckdb`, ATTACHes each into master `analytics.duckdb`, and creates views.

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

Source modes (per-table `query_mode`):
- **Batch pull** (Keboola, `local`): DuckDB extension downloads to parquet, scheduled.
- **Remote attach** (BigQuery, `remote`): DuckDB BQ extension, no download, queries go to BQ.
- **Materialized SQL** (`materialized`): scheduler runs admin-registered SQL through DuckDB and writes the result to a parquet under `/data/extracts/<source>/data/`. Distributed via the same manifest + `agnes pull` flow as local tables. BigQuery cost guardrail: `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; `0` disables). Databricks rows (`source_type=databricks`) run on the SQL warehouse via the Statement Execution API — incl. `MEASURE()` queries over Unity Catalog metric views — result-size-capped by `data_source.databricks.max_bytes_per_materialize`.
- **Real-time push** (Jira): webhooks update parquets incrementally.

Which engine runs a `query_mode='remote'` statement is decided in one place
(`src/remote_engines.py`): it detects the registered remote rows a statement
touches per engine, refuses a statement straddling two (`remote_cross_engine_
unsupported`), and hands off to that engine's own guard + rewrite + transport.
BigQuery's live in `app/api/query.py` (dry-run cost cap, `bigquery_query()`
push-down); Databricks's in `connectors/databricks/remote.py` (no dry-run
exists, so `max_bytes_per_remote_query` caps *returned* bytes via the API's
`byte_limit` and a capped result is refused, never returned short). A
Databricks remote statement cannot join server-side-only data unless the
experimental Unity Catalog ATTACH (`data_source.databricks.attach_enabled`,
default off) gives DuckDB a local view.

### Remote table support (`_remote_attach`)

Extractors with `query_mode='remote'` tables include a `_remote_attach` table in `extract.duckdb` (`alias`, `extension`, `url`, `token_env`) so the orchestrator can re-ATTACH the external DuckDB extension at query time — installing/loading the extension, fetching the token (via `token_env` lookup, or an extension-specific auth path when `token_env=''`, e.g. BigQuery's GCE metadata server), creating a session-scoped SECRET when required, and ATTACHing the source so views like `kbc."bucket"."table"` resolve. The mechanism is generic — any connector can plug in.

Deeper architecture notes: [`docs/architecture.md`](docs/architecture.md).

### Semantic layer (Apache Ossie documents)

A semantic model — datasets, per-column fields, relationships, metrics and
`ai_context` — is stored whole as an [Apache Ossie](https://ossie.apache.org/)
document in `semantic_models`, validated against a vendored, pinned JSON
Schema. `metric_definitions`, `glossary_terms` and `column_metadata` are
**projections** of that document and regenerable from it; the document is the
owner. Sources (`semantic_sources`) feed it over three transports — git clone,
upload, or an existing connection — each through an adapter whose entire
contract is `extract(config) -> list[str]` returning documents as text. Adapters
never write to the database, which is what makes a new source format additive.
Provenance is `(source, source_ref)` and a sync prunes only within its own; a
model owned by a source is read-only through the API (`409 source_owned`) so a
scheduled sync cannot silently revert a downstream edit. Reference:
[`docs/semantic-layer.md`](docs/semantic-layer.md). Design:
[`docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md`](docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md).

Note the neighbour: `src/semantic_validation.py` is a **query** validator (does
a SQL statement obey a document's constraints and dialects);
`src/semantic/document_validation.py` is a **document** validator (does a
document conform to the schema). Different concerns, adjacent names.

### Agent profiles & agent-as-API

Named, scoped agents layered over a user's own stack — CRUD/scope/PAT issuance
at `/api/v1/agents*` (`agnes agent …` CLI, `/agents` builder page) and a
one-shot runtime call at `POST /api/v1/agents/{slug}/responses`. A
`'selected'`-scoped agent's effective authority (owner grants ∩ agent scope)
is enforced live at every brokered request via a restricted `AgentPrincipal`
— never merely computed/audit-recorded — so an agent (or its PAT) can never
reach beyond its declared scope or inherit its owner's admin authority. The secret
broker enforces each agent's pinned model and `token_budget_monthly`
(`429 budget_exhausted`) before forwarding to the LLM. Each agent also keeps
a private memory notebook (`memory_write_mode`: `off`/`propose`/`auto`)
materialized into its sandbox pre-spawn; owners inspect/approve/archive/
delete via `/api/v1/agents/{id}/memories`, `agnes agent memory …`, or the
`/agents` builder panel. `agnes chat <slug>` is a streaming terminal client
over the multi-turn session API (AG-UI SSE) — a pure `/api/v1` caller, no
privileged backchannel. A live, user-driven agent turn can also mid-turn
**@delegate** one sub-request to another agent the caller may run (depth-1,
one delegation per turn) — a `delegate_to_agent` in-sandbox tool reaches
`POST /api/v1/agents/{slug}/delegate`, which spawns the delegate as a fresh
child session under the ORIGINAL CALLER's identity
(`ChatManager.handle_delegation`), never A's or B's owner, so the delegate's
row-level access policies bind to the caller, never a wider identity —
the exact `AgentPrincipal` mechanism above, reused rather than reinvented.
Design:
[`docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md`](docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md).

## Configuration

Instance-specific config: `config/instance.yaml` (see example).
Environment variables: `.env` (never committed).
Table definitions: DuckDB `table_registry` table in `system.duckdb`.

## Development

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
uv pip install ".[dev,server]"

# Run FastAPI locally
uvicorn app.main:app --reload

# Run tests (connectors/ too — every connector keeps its tests beside the
# code, and CI runs both; `tests/` alone silently skips them)
.venv/bin/pytest tests/ connectors/ --tb=short -n auto -q

# Locally `-n auto` is capped at 6 workers (each is a ~430 MB process, and the
# suite is I/O-bound past that). Raise or lower it for a one-off run:
AGNES_TEST_MAX_WORKERS=12 .venv/bin/pytest tests/ -n auto -q

# Trigger sync manually
curl -X POST http://localhost:8000/api/sync/trigger

# Docker
docker compose up
```

### Writing tests: never build the app per test

A function-scoped fixture must NOT call `create_app()` — it measures ~430 ms,
which for most API tests dwarfs the test itself. Request the session-shared
**`shared_app`** fixture instead, or **`seeded_app`** when you also want the
four seeded role users and their tokens:

```python
@pytest.fixture
def my_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))   # isolation still per-test
    return TestClient(shared_app)
```

Per-test isolation is unaffected: repositories read `DATA_DIR` at call time, so
redirecting it in the fixture body still gives each test its own state. Use
`seeded_app_fresh` only when the test runs the ASGI lifespan itself, GETs
`/uploads/...`, or asserts on app construction. `tests/test_shared_app_contract.py`
enforces this and names the fix when it fails.

### Parallel Claude Code worktrees

Use `scripts/dev/worktree-spawn.sh` when starting a second Claude Code session for
parallel work. It creates an isolated Git worktree under `.worktrees/<branch-slug>`
while symlinking shared local state (`user/`, `.venv/`, `.env`, `data/`) back to
the main checkout.

```bash
scripts/dev/worktree-spawn.sh <branch-name> [base-branch]

# Example: create a feature branch from latest main
scripts/dev/worktree-spawn.sh fix/auth-redirect origin/main
cd .worktrees/fix-auth-redirect
```

Keep only one writer active for DuckDB-backed state at a time. Do not run
`agnes pull`, `agnes push`, migrations, or other DuckDB-writing commands
concurrently across worktrees. For parallel Docker Compose stacks, set a
unique project name first:

```bash
export COMPOSE_PROJECT_NAME=agnes-<branch-slug>
```

When the side work is done, remove the worktree and delete the branch after it
has been merged:

```bash
git worktree remove .worktrees/<branch-slug>
git branch -d <branch-name>
```

### Local sync & Claude Code hooks

`agnes pull` is the canonical analyst-side distribution path: pulls the RBAC-filtered manifest from the server, downloads parquets whose MD5 changed (skipping `query_mode='remote'` rows), rebuilds local DuckDB views over them. `agnes push` mirrors it for the upload direction (sessions, CLAUDE.local.md).

`agnes init` writes Claude Code hooks into `<workspace>/.claude/settings.json`:

- `SessionStart` → one detached `agnes update --quiet` — the unified convergence: self-upgrade the CLI, apply the workspace template, re-assert the Agnes-owned hooks/statusLine/commands, refresh marketplace plugins, `agnes push` whatever the last SessionEnd hook missed, and `agnes pull` fresh parquets. Run in the background (`( nohup … & )`) so it never blocks session start; a freshly-installed CLI binary activates next session.
- `SessionEnd`   → `agnes push --quiet` — uploads session jsonl + `CLAUDE.local.md` to the server

Both trail with `; true` and run detached (`( nohup … & )`) so a server outage or slow sync never blocks a session. Workspace-level (not user-home) so the hooks fire only when Claude Code opens this analyst workspace. `agnes update` is also the recommended manual command to repair a broken install or pick up a new release; it holds a single-instance lock so only one runs at a time. Engineers who work across many repos can enable the user-scope layer — skills + data everywhere — with `agnes global enable`; see [docs/global-distribution.md](docs/global-distribution.md).

Admin RBAC for auto-sync flows through data packages (per-table `resource_grants` no longer surface tables in analyst manifests): the admin adds `query_mode IN ('local', 'materialized')` tables to a data package and grants the package to one of the analyst's groups; the package then appears in the analyst's stack — automatically when the grant is marked required, otherwise once the analyst subscribes (`agnes stack browse` / `agnes stack add data_package <id>`) — and its tables land in their manifest for `agnes pull` to download. No per-user sync config; the admin layer plus the user's stack are the source of truth.

## Business Metrics

Standardized metric definitions live in DuckDB (`metric_definitions` table). Import the starter pack with `agnes admin metrics import docs/metrics/`.

That import is upsert-only, so the registry grows but never shrinks. When a directory is the source of truth (a generated export rather than hand-authored files), `--prune` reconciles instead: it deletes the metrics this importer previously wrote that the directory no longer contains. The scope is keyed on the writer (`source='yaml_import'`, narrowed by `--source-ref <label>` when several exports share one instance), so a metric authored in the UI or created by a connector is out of reach by construction. Run `--dry-run` first — a rename is indistinguishable from delete + create at the id level.

**For AI agents analyzing data:** before computing any business metric, look up the canonical definition — `agnes catalog --metrics` to find it, `agnes catalog --metrics --show revenue/mrr` to read the SQL and business rules. Use that SQL, adapted to the question. Never invent metric calculations.

## Querying Agnes data — agent rails

When asked about ANY data in Agnes, follow this protocol.

### Discovery first

Before writing ANY query against a table, run:

    agnes catalog --json | jq <filter>     # know what's available
    agnes schema <table>                   # learn columns + types
    agnes describe <table> -n 5            # see real values for shape

NEVER write `SELECT * FROM <table>` blindly. For local-mode tables it's
wasteful; for remote-mode tables it can blow up at 225M rows.

### Choose the right tool

`agnes query` defaults to `--scope auto`: it runs locally when the table is
synced and transparently falls back to server-side execution when there is no
local data or the table is `remote`/`server_only` (a `[scope]` stderr note
says where it ran). `--remote` and `--local` are shorthands for
`--scope server` / `--scope local`. The rails below are the cost-aware manual
choices for LARGE remote tables — auto-fallback runs the query server-side
as-is, so for big BigQuery tables still prefer a filtered snapshot or an
explicit aggregate.

Tables in `agnes catalog` have a `query_mode`:

- **`local`**: data is on the laptop as parquet (synced via `agnes pull`).
  Query directly with `agnes query "SELECT … FROM <table>"`.

- **`remote`** (typically BigQuery): the parquet does NOT exist on the laptop.
  You MUST either:
  1. **`agnes snapshot create`** a filtered subset → query the local snapshot, OR
  2. **`agnes query --remote`** for one-shot server-side execution. Works on
     all `query_mode='remote'` rows regardless of upstream BQ entity type
     (BASE TABLE → Storage Read API with predicate pushdown; VIEW /
     MATERIALIZED_VIEW → BQ jobs API, no pushdown). Cost-guarded by a
     5 GiB scan cap (configurable in /admin/server-config). Direct
     `bq."<dataset>"."<table>"` paths are registry-gated — unregistered
     paths return 403 `bq_path_not_registered`.

- **`server_only=true`** (independent of `query_mode`): materialized/local on
  the server but NOT synced to the laptop (`agnes pull` skips it), so there is
  no local view. Plain `agnes query` still answers — the default `--scope auto`
  finds no local view and re-runs server-side with a `[scope]` note, exactly as
  described above. Only `--local` / `--scope local` fails with "table does not
  exist"; `agnes query --remote` is the explicit form. The catalog surfaces
  this as the `server_only` field and in `fetch_via`.

### `agnes snapshot create` workflow (preferred for remote tables)

    # 1. estimate first
    agnes snapshot create web_sessions_example \
        --select event_date,country_code,session_id \
        --where "event_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY) 
                 AND country_code = 'CZ'" \
        --estimate
    # → "estimated_scan_bytes: 4.2 GB, result: ~250k rows, 12 MB locally"

    # 2. if reasonable, fetch
    agnes snapshot create web_sessions_example ... --as cz_recent

    # 3. query the local snapshot
    agnes query "SELECT event_date, COUNT(*) FROM cz_recent GROUP BY 1 ORDER BY 1"

### Heuristics for `agnes snapshot create`

- ALWAYS list specific columns in `--select`. Avoid implicit SELECT *.
- ALWAYS include a `--where` for remote tables; otherwise add `--limit`.
- ALWAYS run `--estimate` first when:
  - You're not sure of the data shape
  - The table has `partition_by` or `clustered_by` set (per `agnes schema`)
  - The fetch could plausibly exceed 1 GB local bytes
- Reuse `agnes snapshot list` before fetching — if a snapshot covers your
  query already, skip the fetch.

### BigQuery SQL flavor for `--where`

For `source_type=bigquery` (per `agnes catalog`):

- Date literal: `DATE '2026-01-01'` (NOT `'2026-01-01'::date`)
- Timestamp literal: `TIMESTAMP '2026-01-01 00:00:00 UTC'`
- Now: `CURRENT_DATE()`, `CURRENT_TIMESTAMP()`
- Date arithmetic: `DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)`
- Regex: `REGEXP_CONTAINS(col, r'pattern')` (raw string!)
- NULL: `col IS NOT NULL` (standard)
- Cast: `CAST(x AS INT64)` (NOT `INT`)

For `source_type=keboola` / `source_type=jira` (local), use DuckDB SQL flavor
in your `agnes query` calls — there's no `--where` on local since fetch is implicit.

### Snapshot hygiene

- Reuse snapshots across questions in the same conversation.
- Use descriptive names: `cz_recent`, `orders_q1_us`, `sessions_today`.
- Drop with `agnes snapshot drop <name>` when done with a topic.
- `agnes disk-info` to see total cache size.

### When NOT to use `agnes snapshot create`

- Single aggregate on remote BASE TABLE (`SELECT COUNT(*) FROM remote`):
  use `agnes query --remote "SELECT COUNT(*) FROM web_sessions_example"`.
  Storage Read API pushes the COUNT into BQ — cheap, no materialization.
- Single aggregate on remote VIEW/MATERIALIZED_VIEW: same syntax works
  (#160), but the BQ jobs API can't push WHERE/COUNT into the view body.
  Cost guardrail (default 5 GiB) catches expensive scans → 400
  `remote_scan_too_large` with `agnes snapshot create` suggestion. Pivot to
  `agnes snapshot create <id> --where '<predicate>'` if the cap is hit.
- Throwaway exploration: `agnes query --remote "SELECT … FROM <registered_id>"`.
  Direct `bq."<dataset>"."<table>"` paths are now registry-gated — register
  first or use the catalog id.
- Cross-table JOIN with both tables remote: combine `agnes snapshot create` for one
  side + `agnes query --remote` for the other; full cross-remote JOIN
  requires more thought (see #101 for design space).

## Hybrid Queries (BigQuery + Local)

Server-side only. Admins can POST `{sql, register_bq: {alias: bq_sql}}` to `/api/query/hybrid` (see `app/api/query_hybrid.py`), which runs the BQ sub-queries server-side (where BQ credentials live) and joins the result against the server's local parquet views in a single DuckDB session.

There is no analyst-facing CLI flag for this — analysts who need to combine a local table with a remote one should `agnes snapshot create` a filtered subset of the remote table and `agnes query` the join locally, or run the join server-side via `agnes query --remote`.

## Marketplace

Agnes ingests admin-registered Claude Code marketplaces (git repos cloned nightly to `${DATA_DIR}/marketplaces/<slug>/`) and re-serves a single aggregated, RBAC-filtered marketplace back to user instances over two PAT-gated channels: `GET /marketplace.zip` and `GET /marketplace.git/*`. Content is filtered per caller by joining `resource_grants ↔ marketplace_plugins` against the caller's groups.

Full reference — ingestion, the served endpoint, RBAC filtering, user registration inside Claude Code: [`docs/marketplace.md`](docs/marketplace.md). Content-authoring side (`marketplace-metadata.json`): [`docs/curated-marketplace-format.md`](docs/curated-marketplace-format.md).

## Access control

Two layers, no role hierarchy:

- `user_groups` — named groups. `Admin` (god-mode short-circuit on every authorization check) and `Everyone` (auto-membership) are seeded as `is_system=TRUE`.
- `user_group_members` — `(user_id, group_id, source)`; `source` segregates writers so Google's nightly sync doesn't clobber admin-added members.
- `resource_grants` — generic `(group, resource_type, resource_id)` triples for any entity-scoped grant.

Gate endpoints with `Depends(require_admin)` (app-level mutations) or `Depends(require_resource_access(ResourceType.X, "{path}"))` (entity-scoped), both from `app.auth.access`. Add a resource type by extending the `ResourceType` `StrEnum` and registering a `ResourceTypeSpec` (with a `list_blocks` projection delegate) in `app/resource_types.py` — no DB migration.

Admin UI: `/admin/access`. CLI: `agnes admin group …` and `agnes admin grant …`. Full reference: [`docs/RBAC.md`](docs/RBAC.md).

A third, optional layer sits above these two and answers a narrower question: not "can this group reach the table", but "what does a caller who can reach it actually see". An admin may attach **one SQL policy** to a registered table — only one that never leaves the server, i.e. `query_mode='remote'` or `server_only=true` — that Agnes substitutes for the table on every server-side read, filtering rows and masking columns by the caller's `$user_email` / `$user_id` / `$user_groups`. Off by default behind `access_policies.enabled`. Full reference: [`docs/table-access-policies.md`](docs/table-access-policies.md).

## Extensibility

### Data Sources (extract.duckdb contract)
New connector = `connectors/<name>/extractor.py` producing `extract.duckdb + data/`. Must create a `_meta` table with columns: `table_name`, `description`, `rows`, `size_bytes`, `extracted_at`, `query_mode`. The orchestrator ATTACHes it automatically.

### Authentication
Auth providers in `app/auth/` (FastAPI-based):
- **Google**: OAuth via Google (Workspace group memberships pulled at sign-in — see [`docs/auth-groups.md`](docs/auth-groups.md) for the GCP setup checklist + the `security` label gotcha)
- **Microsoft**: OAuth via Microsoft Entra ID, single-tenant *enforced* — the reserved `common`/`organizations`/`consumers` endpoints are refused and leave the provider unavailable. Authentication only, no Graph group sync yet; a tenant is not an identity boundary on its own, so pin `auth.allowed_domain` (see [`docs/auth-microsoft-oauth.md`](docs/auth-microsoft-oauth.md))
- **Email**: magic link (itsdangerous token)
- **Keboola**: OAuth via the Keboola stack (project-bound; optional `X-StorageApi-Token` header auth for existing users, switch-gated)
- **Desktop**: JWT for API

Per-instance offering is narrowed by `auth.providers` (see `config/instance.yaml.example`).

### Web pages
HTML dashboard pages use the design-system **page shell** (#367/#482): `{% extends "base_page.html" %}` (gradient hero + `{% block toolbar %}` + `{% block page %}`) or `{% extends "base_ds.html" %}` (everything else; body in `{% block content %}`). **Never `base.html`** — it is legacy. The base auto-imports the `ds.*` macros (no `{% import "_components.html" %}`), sets theme/favicon/nav/global-JS, and provides the canonical `.container`; page CSS goes in `{% block head_extra %}`, never inline in the body. Contract guards in `tests/test_design_system_contract.py` reject `.container:has()` opt-outs, bare `:root{}`, raw `#hex`, and `var(--primary)` (use `var(--ds-primary)`). Full step-by-step recipe: [`docs/architecture.md`](docs/architecture.md) → *Extending the Platform → New Web Page*.

**Visual standard (binding for ALL UI work):** `.claude/skills/agnes-conventions/references/design-system.md` — `--ds-*` tokens only, theme switch via `data-theme` (`paper` default since Wave 0, 2026-08 — the issue-#896 prototype look; `blue`/`navy`/`dark`/`auto` remain fully supported and an explicit theme choice always wins), chrome via `data-ui-layout` (hard-wired to `"rail"` — the topnav chrome was retired in the same wave; a configured `instance.ui_layout`/`AGNES_UI_LAYOUT` is tolerated but inert, ignored with a one-time startup warning). A NEW theme value still ships as its own opt-in scoped block, never by mutating an existing theme's block (guarded by `tests/test_ui_layout_theme.py`).

### Hosted Data Apps
`src/data_apps/` (registry + spec builders) + `services/apps_runner/` (the sidecar that alone holds the Docker socket) host user web apps next to the data — off by default (`data_apps.enabled`), compose profile `apps`. See [`docs/architecture.md`](docs/architecture.md#hosted-data-apps) and [`docs/superpowers/specs/2026-07-21-data-apps-design.md`](docs/superpowers/specs/2026-07-21-data-apps-design.md).

## Key Implementation Details

### DuckDB Schema (src/db.py)
- Auto-migrating schema (`v1 → vN`). The current version and migration ladder live in `src/db.py`; per-version schema change notes are in `CHANGELOG.md` — do not maintain a duplicate history here.
- `table_registry`: id, name, source_type, bucket, source_table, query_mode, sync_schedule, etc.
- `sync_state`, `sync_history`: track extraction progress.
- `users`, `audit_log`: account state + audit trail. RBAC lives in `user_groups` + `user_group_members` + `resource_grants`.
- System DB at `{DATA_DIR}/state/system.duckdb`, analytics DB at `{DATA_DIR}/analytics/server.duckdb`.

### SyncOrchestrator (src/orchestrator.py)
- `rebuild()`: scans extracts dir, ATTACHes all, creates master views, updates sync_state.
- `rebuild_source(name)`: single source (used after Jira webhooks).
- Thread-safe via `_rebuild_lock`.

### Connector Pattern
- **Keboola**: `connectors/keboola/extractor.py` uses the DuckDB Keboola extension, falls back to `client.py` (legacy Storage API wrapper).
- **BigQuery**: `connectors/bigquery/extractor.py` uses the DuckDB BQ extension (remote-only, no download).
- **Jira**: `connectors/jira/webhook.py` → `incremental_transform.py` → `extract_init.py` updates `_meta`.
- **Databricks**: `connectors/databricks/extractor.py` materializes registered SQL on a SQL warehouse (Statement Execution API → Arrow → parquet, no SDK); `remote.py` ships an analyst's statement to the warehouse per query for `query_mode='remote'` rows; `attach.py` + `extract_init.py` optionally ATTACH Unity Catalog into DuckDB (`uc_catalog`/`delta`, opt-in, experimental) so remote rows can be JOINed locally; `semantic_layer.py` mirrors Unity Catalog metric views into `metric_definitions` (`source='databricks_semantic_layer'`, scoped prune per workspace).

### Config Loading
1. `config/loader.py` loads `instance.yaml`.
2. `app/instance_config.py` exposes `get_data_source_type()`, `get_value()`.
3. Table config lives in DuckDB `table_registry` (not markdown files).

### Files NOT to modify (stable infrastructure)
- `connectors/jira/file_lock.py` — advisory file locking

(`services/ws_gateway/` was previously listed here but the standalone
service no longer exists: wave-2F task 6 absorbed its WS + auth + heartbeat
logic into `app/api/notifications_ws.py` (gated to `Role.GATEWAY` processes)
and its dispatch path into `app/notifications.py::publish_notification`,
which rides the coordination pub/sub channel `notify:{user}` instead of the
old in-memory `connections` dict + Unix-socket HTTP dispatch.

`connectors/jira/transform.py` was previously listed here but has been
removed: the `_remote_links` hardening in 0.54.19 required modifying
`transform_remote_links` and `transform_all` to honor a new "overlay
absent → preserve existing rows" contract. The transform module remains
sensitive — touch it only when you understand the JSON-overlay /
parquet-rewrite pipeline end-to-end — but it is no longer off-limits.)

## Release process

Full recipe, deploy workflows, manual rollback runbook, weekly tag-housekeeping, and CI quirks: [`docs/RELEASING.md`](docs/RELEASING.md). The non-negotiable rules:

- **Changelog discipline.** Every PR that changes user-visible behavior MUST add a bullet under `## [Unreleased]` in `CHANGELOG.md`, in the same PR — grouped Added/Changed/Fixed/Removed/Internal, `**BREAKING**` prefix for breaking changes. No follow-ups.
- **Release-cut is a dedicated cut PR, never a feature PR.** A feature/fix PR only ever adds an `[Unreleased]` bullet — it never bumps `pyproject.toml`/`server.json` or renames `[Unreleased]`. `.github/workflows/daily-cut.yml` cuts once a day (minor bump; `patch`/`major` on manual dispatch for a hotfix/milestone) into a PR labeled `release-cut` that a human reviews and merges — this is what killed the old CHANGELOG-rename race between competing feature PRs. After merge: `gh workflow run tag-release.yml -f tag=vX.Y.Z` (the cut PR's body carries the exact command) tags the merge commit and creates the GitHub Release.
- **Run the full test suite before every push** — `.venv/bin/pytest tests/ connectors/ --tb=short -n auto -q` (this is what CI runs). `connectors/` is not optional: every connector keeps its tests beside the code, so `tests/` alone skips them and a connector regression passes a "full" local run and fails in CI. Failures in code you touched: fix before pushing. Failures unrelated to your diff: confirm with `git stash` they reproduce on a clean branch, note them in the PR body, don't block on them.
- **Watch the post-merge `release.yml` run.** On `main` pushes a `smoke-test` job pulls the just-built `:stable` image and runs a docker-compose stack; if it fails, the `rollback-on-smoke-fail` job calls the reusable `rollback.yml` workflow which re-points `:stable` to the previous known-good build and opens a tracking issue labeled `bug`. Success signal after merge = `smoke-test` green + `rollback-on-smoke-fail` skipped. If the rollback fires, the merge shipped a broken image to GHCR — investigate the tracking issue before any further push (the issue body has the failing image, commit SHA, deprecated tag, and rollback target). Manual rollback / forced target / weekly tag-pruning operator commands are in [`docs/RELEASING.md`](docs/RELEASING.md).

## Specialized agents, skills & commands

Agnes ships a Claude Code dev-agent kit under `.claude/` (auto-discovered). Pick
the right tool:

| Need | Use | How |
|---|---|---|
| Chart a large, foggy effort — destination known, too many decisions open to write a plan | `agnes-wayfinder` | a map + numbered decision tickets as markdown under `docs/superpowers/maps/<effort>/`; resolve one per session (`research` excepted) until the route is clear, then hand off to `superpowers:writing-plans` → `/agnes-build`. Explicit invocation only; if you can already state the steps, skip it. |
| Verify a change before claiming it's done | `verify-agnes-change` | cheapest-first loop: `scripts/verify_syncmap.py` (instant, the sync-map rows no test guards) → the guards your diff touches → full suite → `/agnes-review`. Fix and re-run each gate until it passes. |
| Review a change before merge | `/agnes-review` | scope-gated review **team** (rules always fires; adversarial is opt-in via `--adversarial`; architecture / rbac / parity fire only in-scope) + `agnes-review-consolidator` → one advisory report (`file:line` + severity, ≤15 findings). Read-only working tree; optional comment-only PR post. |
| Implement a whole plan in parallel | `/agnes-build` | decomposes a plan into independent tasks (sync-map coupling), builds each in its own git worktree via `agnes-builder`, integrates (migration serialized last), then runs `/agnes-review`. |
| Implement a feature (connector / endpoint / web page / repo method / migration) | `agnes-builder` | disciplined implementer (TDD-first, DuckDB↔PG parity in the same change, migration-ladder sync, CHANGELOG, vendor-agnostic, scope discipline). Routes to the `agnes-conventions` playbooks. |
| Cut a release / tag | `agnes-releaser` | per the release process. |
| Deep knowledge while editing a subsystem | `agnes-*` knowledge skills | auto-loaded by description. |

**Agents** (`.claude/agents/`): `agnes-reviewer-rules`, `agnes-reviewer-adversarial`,
`agnes-reviewer-architecture`, `agnes-reviewer-rbac`, `agnes-reviewer-parity`
+ `agnes-review-consolidator` (the review team), `agnes-builder` (implementer),
`agnes-decomposer` + `agnes-integrator` (the build team),
`agnes-releaser` (release).

**Commands** (`.claude/commands/`): `/agnes-review`, `/agnes-build`.

**Skills** (`.claude/skills/`): knowledge — `agnes-orchestrator`, `agnes-rbac`,
`agnes-connectors`, `agnes-release-process`; implementation playbooks —
`agnes-conventions` (`SKILL.md` + `references/{connector,repo-parity,migration,endpoint-rbac,web-page}.md`);
verification — `verify-agnes-change` (the pre-merge loop); planning —
`agnes-wayfinder` (the pre-plan map, for efforts too foggy to plan yet).
Read the relevant one before editing that part of the codebase.

**Invariants & guards:** the change-safety **sync-map** lives in `CONTRIBUTING.md`
(walked by the review team — surfaces that must change together, incl. DuckDB↔PG
parity and REST×CLI×MCP coverage). A PostToolUse **quality hook**
(`scripts/post-edit-quality.sh`, wired in `.claude/settings.json`) runs ruff
fix/format + mypy on every edited Python file.

Design rationale: `docs/superpowers/specs/2026-05-15-agnes-agents-design.md`,
`docs/superpowers/specs/2026-06-05-agnes-dev-agent-kit-design.md`.

## Project conventions

### Security invariants (untrusted input)
Any code touching untrusted input — HTTP requests, uploaded files, a connector's
`extract.duckdb`, a curated-marketplace repo, Slack/Telegram messages, or URLs —
must follow the security playbook: `.claude/skills/agnes-conventions/references/security.md`.
Read it before writing or reviewing such code. The non-negotiables, each a real
prior finding: escape SQL identifiers (`quote_ident`, never bare `f'"{name}"'`);
no file paths in `FROM`/`JOIN` on `/api/query`; sanitize before `innerHTML`
(`renderMarkdownSafe`, never raw `marked.parse`); render authored templates with
Jinja2 `SandboxedEnvironment`; keep regexes over untrusted text linear-time;
validate **and** realpath-contain filesystem paths built from untrusted names;
never put secrets on argv or in URL query strings (git credential helper +
hidden prompts + `Authorization: Bearer`); gate credential egress with a host
allowlist (`is_attach_host_allowed`); derive client IP from trusted proxy hops
(`app.auth.client_ip.trusted_client_ip`), never leftmost XFF; require a CSRF
token on state-changing web POSTs and never mutate on GET; scope infra exposure
per-instance, never fleet-wide. `agnes-reviewer-rules` runs the reviewer
quick-scan from that playbook on every PR.

### Vendor-agnostic public repo — no customer-specific content
This repo is the public source-available distribution. **Nothing customer-specific belongs in code, config defaults, comments, docs, commit messages, or PR titles/bodies** — no specific deployments or brands, cloud project IDs, internal hostnames, runbook paths, internal SA emails, or cross-references to private repos. Frame motivations abstractly ("behind a TLS-terminating reverse proxy"); use placeholders in examples (`example.com`, `<your-host>`, `<install-dir>`). Customer-specific automation lives in the private infra repos that *consume* this repo. Before opening a PR, scan the diff and PR body for customer-specific tokens.

### Command & search UX standard
Every user-facing read/find surface (CLI command, MCP tool, web search box)
follows one model: **default scope = auto/everywhere, result origin always
labeled, `--scope auto|local|server` as the only override** (legacy `--remote`/
`--local` are frozen aliases — never add a new boolean scope flag or flip an
existing default). Flag vocabulary for new commands: positional search term,
`--limit`, `--json`. "Not found" errors must hint the next step (shared helper:
`cli/query_hints.py`). Server-side MCP foundation tools are defined once in
`app/api/mcp/foundation_tools.py` (guarded by `tests/test_mcp_tool_parity.py`).
Full playbook + review checklist: `.claude/skills/agnes-conventions/references/command-ux.md`
(sync-map rows in `CONTRIBUTING.md` make this a blocking review finding).

### Issue economy — fix or close, don't spawn
The default reaction to "I noticed something while doing X" is **fix it now**, **close it as moot after audit**, or **leave a `TODO` in the touching diff** — not "file an issue". Before filing any follow-up issue: verify the claim is still true on current `main` (issues routinely cite moved line numbers and deleted call sites — if the premise is gone, close the parent), and check whether it's a ≤30-min, ≤1-file fix you could just do in the current PR. Filing is acceptable only for multi-file refactors with open design questions, production changes needing operator coordination, unclear cross-team ownership, or bugs whose fix would balloon the current PR ≥3×. When investigating an existing issue, reproduce the symptom on current `main` first; if it doesn't fire, close with a comment documenting the audit. When in doubt: fix it, or close it.

### Dual-backend discipline — PG-first ratchet (A3)

Postgres is the canonical, only-growing app-state backend. **The DuckDB
app-state backend is frozen (A3, remediation-program Track A):** the ~65
existing DuckDB↔PG repo pairs and the `src/db.py` migration ladder stay
maintained (bugfixes, contract tests, method parity) until they are deleted
outright by a later cleanup pass, but **no new DuckDB app-state surface may
be added** — no new `src/repositories/<name>.py` DuckDB repo module, no new
`_REGISTRY` entry with a DuckDB backend, no new `src/db.py` `_vN_to_v(N+1)`
schema step. (This freeze is scoped to *app-state* only — analytics DuckDB,
the `extract.duckdb` contract, `analytics.duckdb`, and DuckDB extensions
like BQ/FTS are untouched and stay DuckDB-only by design.)

**New app-state work is Postgres-only:**

- **New repository = `src/repositories/<name>_pg.py` only**, no DuckDB
  sibling. Register it in `src/repositories/__init__.py` `_REGISTRY` with
  only the `PG` backend. Reach it through the `*_repo()` factory, never
  instantiate directly — same rule as always.
- **New schema change = an Alembic revision only** (`migrations/versions/`),
  no matching `_vN_to_v(N+1)` step in `src/db.py`. `src/db_pg.py`
  (`Base.metadata`) still needs the SQLAlchemy model, as always.
- **A PG-only feature must fail clean, never with an unhandled 500, on an
  instance still running the frozen DuckDB app-state backend.** Resolving a
  PG-only repo key while the active backend is DuckDB raises the typed
  `src.repositories.RequiresPostgresBackend` (naming the feature); the
  app-wide handler in `app/main.py` translates it to a `501`. See
  `docs/migrations.md` → "Adding a PG-only feature" for the full recipe.
- The static ratchet is `tests/test_repository_registry.py::test_registry_backends_are_symmetric`
  (a `_REGISTRY` entry may carry every backend — a frozen pre-A3 pair — or
  Postgres-only, never DuckDB alone), plus the frozen-key/frozen-module
  pins in `tests/test_repository_registry_pg_first_ratchet.py` and
  `tests/db_pg/test_repo_module_pg_first_ratchet.py` (no *new* full pair or
  DuckDB-only module either, even a well-formed one). The DuckDB ladder's
  ceiling is `src/db.py::FROZEN_DUCKDB_SCHEMA_VERSION`, gated by
  `tests/test_db_schema_version_frozen.py`.

**Existing DuckDB↔PG pairs stay under the pre-A3 rule until deleted:**

- **Touching a method on an existing `src/repositories/X.py` that still has
  a `_pg.py` sibling? Update `X_pg.py` in the same PR.** No exceptions for
  "I'll do PG later" — a frozen pair is "maintained", not "abandoned".
- **Cross-engine contract tests for existing pairs must stay green.**
  `tests/db_pg/test_<cluster>_contract.py` parametrizes both backends
  through the same assertion set. If you add a method to an existing pair,
  extend the contract test in the same PR.
- **Reach repos through the factory, never instantiate them directly** —
  unaffected by the ratchet, applies to every repo regardless of backend
  count. Two guards enforce this: `tests/test_backend_split_guard.py` is a
  **static** ratchet that scans for `get_system_db()` callers + direct repo
  instantiation, and the **dynamic** status-parity sweeps
  (`tests/db_pg/_parity_sweep_util.py`) drive both backends through a
  `TestClient` and diff the HTTP status of every parameter-free route. A
  route backed by a PG-only repo is legitimately expected to diverge; list
  it (route → one-line reason) in the sweep's `_PG_ONLY_ROUTE_EXEMPTIONS`
  (`dict[str, str]`) and the mechanism still requires
  `assert_pg_only_exemptions_fail_clean` to prove the DuckDB side answers a
  TYPED `501` (`body["error"] == "requires_postgres_backend"`) — never a raw
  500, and never an unrelated 4xx accepted just because it's also an error
  status.
- **No PG-only optimizations without a DuckDB fallback path** on an existing
  frozen pair — unchanged by the ratchet. If a query has a PG-native window
  function, the DuckDB sibling either uses the same syntax (DuckDB ⊇ PG in
  most window-function support) or implements an equivalent in DuckDB's
  flavor.

DuckDB-Quack (DuckDB 2.0, ~fall 2026) and any other future backend join
through the same factory layer regardless of how this freeze resolves; the
state machine in `src/db_state_machine.py` already reserves the enum value.

### Git commits & pull requests
- Keep commit messages clean and concise.
- Do not include AI attribution in commits or PRs.
