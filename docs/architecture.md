# Architecture — Detailed Reference

Comprehensive architectural overview of the Agnes platform (v2). For the picture
first, see [`../ARCHITECTURE.md`](../ARCHITECTURE.md#visual-overview) — the layered
platform diagram, the analyst loop, and query routing, in
[`diagrams/`](diagrams/).

## Top-Level Module Map

```
agnes-the-ai-analyst/
├── src/                  Core engine (db, orchestrator, rbac, profiler, repositories)
├── connectors/           Pluggable data connectors (keboola, bigquery, jira, llm)
├── app/                  FastAPI application (API + web UI)
│   ├── api/              REST API routers
│   ├── auth/             Auth providers (JWT, Google OAuth, email magic link, password)
│   └── web/              HTML dashboard routes
├── services/             Standalone background services (scheduler, telegram_bot, …)
├── cli/                  CLI tool (agnes pull, agnes query, agnes admin)
├── scripts/              Utility and migration scripts
├── config/               Instance configuration templates
├── tests/                Test suite
└── docs/                 User-facing documentation
```

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  EXTERNAL DATA SOURCES                                          │
│  Keboola Storage  │  BigQuery  │  Jira Cloud  │  CSV/files     │
└──────────┬────────┴─────┬──────┴──────┬────────┴────────────────┘
           │              │             │
           ▼              ▼             ▼
┌─────────────────────────────────────────────────────────────────┐
│  CONNECTORS  (connectors/)                                      │
│  extractor.py per source → extract.duckdb contract             │
└──────────────────────────┬──────────────────────────────────────┘
                           │  /data/extracts/{source}/extract.duckdb
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  SYNC ORCHESTRATOR  (src/orchestrator.py)                       │
│  Scans extracts/, ATTACHes each extract.duckdb,                │
│  creates master views in analytics.duckdb (atomic swap)        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
     ┌──────────────┐  ┌────────┐  ┌──────────────┐
     │  FastAPI app  │  │  CLI   │  │  Scheduler   │
     │  port 8000    │  │  `da`  │  │  sidecar     │
     └──────────────┘  └────────┘  └──────────────┘
              │
    ┌─────────┴──────────┐
    ▼                    ▼
system.duckdb       analytics.duckdb
(state/registry)    (master views)
```

**Deployment:** Docker Compose. The `app` service runs Uvicorn. The `scheduler` sidecar triggers
sync jobs and the LLM pipeline (corporate-memory, verification-detector, session-collector) via
the app's REST API on offset cadences. Optional `full` profile adds telegram-bot.

```bash
docker compose up               # app + scheduler
docker compose --profile full up  # all services
docker compose --profile extract run extract  # one-shot extraction
```

---

## extract.duckdb Contract

Every connector writes to the same directory layout:

```
/data/extracts/{source_name}/
├── extract.duckdb          ← _meta table + views over parquet files
└── data/                   ← parquet files (local connectors only)
    ├── table_a.parquet
    └── table_b.parquet
```

### _meta table

Required in every `extract.duckdb`:

```sql
CREATE TABLE _meta (
    table_name   VARCHAR NOT NULL,
    description  TEXT,
    rows         BIGINT,
    size_bytes   BIGINT,
    extracted_at TIMESTAMP,
    query_mode   VARCHAR    -- 'local' or 'remote'
);
```

The orchestrator reads `_meta` to know which tables exist and creates a corresponding
view in `analytics.duckdb` for each row.

### _remote_attach table (optional)

Connectors whose views reference an external DuckDB extension (e.g. Keboola, BigQuery)
must include this table so the orchestrator can re-ATTACH the external source at rebuild time:

```sql
CREATE TABLE _remote_attach (
    alias     VARCHAR,  -- DuckDB alias for the attached source, e.g. 'kbc'
    extension VARCHAR,  -- Extension name, e.g. 'keboola'
    url       VARCHAR,  -- Connection URL
    token_env VARCHAR   -- Name of the env var holding the auth token
);
```

The orchestrator installs/loads the extension, reads the token from the environment, and
ATTACHes the external source so remote views resolve correctly. This mechanism is generic —
any connector can use it. Auth credentials are never stored in `extract.duckdb`.

---

## SyncOrchestrator

`src/orchestrator.py` — thread-safe via `_rebuild_lock` (and, cross-process, the
Postgres advisory lock `rebuild_lease()` — both taken together by the
`rebuild_mutex()` context manager). `rebuild()` and `rebuild_source(source_name)`
dispatch on the configured [analytics backend](#analytics-data-plane-legacy-vs-ducklake)
— `legacy` (below) or `ducklake` — before doing anything else.

### rebuild() — legacy backend

1. Open a **temporary** DuckDB file (`analytics.duckdb.tmp`).
2. Scan `/data/extracts/*/extract.duckdb` (sorted, skips non-directories and missing files).
3. Validate each directory name as a safe SQL identifier (`^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`).
4. For each source: `ATTACH '{db_file}' AS {source_name} (READ_ONLY)`.
5. Handle `_remote_attach` — install extension, read token from env, ATTACH external source.
6. Read `_meta`, validate each `table_name` identifier, create `CREATE OR REPLACE VIEW`.
7. Update `sync_state` in `system.duckdb` (mtime-based hash, no full file read).
8. `CHECKPOINT` and close the temp connection.
9. **Atomic swap**: `shutil.move(tmp_path, target_path)` — replaces `analytics.duckdb` in-place.

### rebuild_source(source_name) — legacy backend

Convenience wrapper that calls `rebuild()` in full (partial rebuild is not possible because
`analytics.duckdb` is written fresh from scratch each time). Used after Jira webhooks.

On the `ducklake` backend the equivalent call is genuinely incremental instead —
see below.

### Identifier validation

Both `source_name` and `table_name` are checked against `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`
before being interpolated into SQL. Invalid names are skipped with a warning.

---

## Analytics Data Plane: legacy vs DuckLake

`analytics.backend` (`instance.yaml` / `AGNES_ANALYTICS_BACKEND` env) — `legacy`
(default, zero-config) or `ducklake` (opt-in, wave-2G/WS E). Resolved once per
process and cached for its lifetime (`src/analytics_backend.py`) — config is
read at boot, not hot-reloaded, so flipping it requires a restart of every role
process.

**The invariant that never changes between backends:** the `extract.duckdb`
contract above — `/data/extracts/{source}/{extract.duckdb, data/*.parquet}` —
is the distribution artifact and the rollback truth for BOTH backends. Neither
backend re-extracts from the source system; both are populated purely by
reading what the connectors already wrote to that tree. Migrating between
backends is therefore always a rebuild-from-extracts, never a re-sync (see
`agnes admin analytics migrate` below).

- **`legacy`** — the rebuild-and-swap `{DATA_DIR}/analytics/server.duckdb`
  described above: a full temp-file rebuild, atomically swapped in.
- **`ducklake`** (`src/ducklake_session.py`, `src/orchestrator.py`'s
  `_do_rebuild_ducklake`) — a DuckLake catalog (Postgres-backed for
  multi-process deployments, a DuckDB-file catalog for single-process `all`
  mode) with data files DuckLake itself owns under `ducklake.data_path`.
  Three properties define this plane:
  - **Copy-ingest writer.** The worker is the only writer: after each sync,
    `CREATE OR REPLACE TABLE lake."<source>"."<table>" AS SELECT * FROM
    read_parquet(...)` copies each source's parquet into its own catalog
    schema, then master views (`lake.main."<view>"`) are (re)created —
    porting the same view-ownership claim/collision semantics the legacy
    rebuild uses. `rebuild_source(name)` only re-ingests that one source's
    schema — every other source's DuckLake tables/snapshots are untouched,
    fixing the legacy "any webhook forces a full rebuild" cost by
    construction.
  - **Read-only reader plane.** `api`/`gateway` roles hold one long-lived
    DuckLake attach per process (`get_ducklake_read()`), reused across
    requests via `.cursor()` — never `CREATE VIEW`/lake DDL, never a
    catalog-write commit, per request. This matters because a DuckLake
    catalog commits a new snapshot on every DDL statement even when nothing
    changed; a reader that wrote on every request would be unbounded write
    amplification onto the shared Postgres catalog. `query_mode='remote'`
    (BigQuery) wrapper views are therefore created by the writer once per
    rebuild, not by the reader per request.
  - **Snapshot-isolated concurrent reads.** A reader mid-request sees a
    consistent snapshot even while the writer commits a new one concurrently
    — DuckLake's MVCC, not anything Agnes builds itself.
  - **Maintenance.** A daily `ducklake-maintenance` job (see
    [Background Jobs](#background-jobs)) runs `merge_adjacent_files` →
    `ducklake_expire_snapshots` → `ducklake_cleanup_old_files` → a catalog
    `VACUUM` (Postgres-catalog only), mutually exclusive with a rebuild via
    the same `rebuild_mutex()` lock pair.

**Migrating an existing instance** (`agnes admin analytics migrate --to
ducklake|legacy`, `POST /api/admin/analytics/migrate`) never flips config
itself — config is operator-owned and boot-time-cached, as above. It (1)
validates prerequisites (`ducklake` extension loadable; for a Postgres
catalog, a real reachability probe, auto-repairing a missing catalog database
on an existing Postgres volume), (2) enqueues a full rebuild into the named
target backend from the on-disk extracts tree (`SyncOrchestrator
.migrate_to_backend`, bypassing whichever backend is currently configured),
and (3) tells the operator to flip `analytics.backend` and restart once that
job completes. Rollback (`--to legacy`) is the same shape in reverse — the
extracts tree never stopped being legacy's source of truth either.
Materialized-SQL tables are not re-materialized by either direction; they
follow their own scheduler cadence. Full operator recipe:
[`DEPLOYMENT.md`](DEPLOYMENT.md#ducklake-analytics-backend).

---

## Data Sources

### Keboola — Batch Pull

`connectors/keboola/extractor.py`

- Uses the DuckDB Keboola community extension to download tables directly to parquet.
- Fallback path: `connectors/keboola/client.py` (Keboola Storage API wrapper).
- Sync strategies: `full_refresh`, `incremental`, `partitioned`.
- Writes `extract.duckdb` + `data/*.parquet` under `/data/extracts/keboola/`.
- For tables with `query_mode='remote'`, populates `_remote_attach` so views proxy queries
  to Keboola rather than downloading data locally.

Sync trigger flow:

```
POST /api/sync/trigger (admin)
  → BackgroundTask: _run_sync()
    → Read table_registry from system.duckdb (main process)
    → Serialize configs as JSON, spawn subprocess (no DuckDB lock conflict)
    → Subprocess: connectors/keboola/extractor.run()  →  extract.duckdb
    → SyncOrchestrator().rebuild()  →  analytics.duckdb
    → Profiler: profile each synced parquet  →  table_profiles
```

### BigQuery — Remote Attach

`connectors/bigquery/extractor.py`

- Uses the DuckDB BigQuery community extension via the `BqAccess` facade in `connectors/bigquery/access.py`.
- No data download — views proxy all queries directly to BigQuery.
- Auth via `GOOGLE_APPLICATION_CREDENTIALS` (service account JSON) or ADC.
- Populates `_remote_attach` with `extension='bigquery'` and no `token_env` (env-based auth).

### BigQuery — Materialized SQL

`connectors/bigquery/extractor.py::materialize_query` (added in v0.25.0)

- Runs admin-registered SQL through the DuckDB BigQuery extension via `BqAccess.duckdb_session()` and writes the result to `/data/extracts/bigquery/data/<id>.parquet` atomically (`<id>.parquet.tmp` → `os.replace`).
- Triggered by `_run_materialized_pass` in `app/api/sync.py` between custom-connectors and orchestrator rebuild on every `/api/sync/trigger`. Per-table `sync_schedule` honored via `is_table_due()`.
- Cost guardrail: BQ dry-run via `app.api.v2_scan._bq_dry_run_bytes` (single source of truth for cost-estimate logic). `data_source.bigquery.max_bytes_per_materialize` (default 10 GiB; `0` disables). Fail-open when dry-run errors (DuckDB three-part syntax the native BQ client can't parse) — log warning + proceed.
- Distribution: result parquet rides the same manifest + `agnes pull` flow as Keboola tables. Per-user RBAC unchanged (`resource_grants(group, ResourceType.TABLE, table_id)`). That flow optionally gains a bucket-mirror + presigned-URL hop — see [Distribution: signed-URL object store mirror](#distribution-signed-url-object-store-mirror) below.

### Jira — Real-Time Push

`connectors/jira/webhook.py` → `incremental_transform.py` → `extract_init.py`

```
Jira Cloud webhook (issue created/updated/deleted)
  → POST /api/jira/webhook  (HMAC-SHA256 verification)
  → connectors/jira/webhook.py  (validate, persist raw JSON)
  → connectors/jira/incremental_transform.py  (update monthly parquet shards)
  → extract_init.py  (update _meta)
  → SyncOrchestrator().rebuild_source('jira')
```

Output tables (7): `issues`, `comments`, `attachments`, `changelog`, `issuelinks`,
`remote_links` (all month-partitioned), plus `organizations` — a current-state
dimension, unpartitioned, refreshed from the organization API rather than derived
from issue JSON.

Background supplements:
- `jira-sla-poll` — refreshes SLA fields for open tickets every 45 min.
- `jira-consistency` — detects and backfills missing issues every 30 min.

Files NOT to modify: `connectors/jira/file_lock.py`, `connectors/jira/transform.py`.

---

## Distribution: signed-URL object store mirror

Optional (off by default), wave-2H addition on top of the manifest +
`agnes pull` flow every connector above already rides. When an
S3-compatible object store is configured (`distribution.object_store.*` /
`distribution.signed_urls`, see
[`DEPLOYMENT.md`](DEPLOYMENT.md#signed-url-distribution-object-store)), three
extra hops layer on top of the existing distribution path — none of them
change what's authoritative:

1. **Bucket mirror** (`app/worker/kinds.py::_run_distribution_mirror`, a
   `distribution-mirror` LIGHT job chained off every successful
   `data-refresh`) reads the same `extracts/**/data/<table_id>.parquet`
   files `agnes pull` has always served, and uploads any whose content
   changed (md5-compared against the object's stamped metadata) to
   `{prefix}/<table_id>.parquet` in the configured bucket via
   `src.object_store.ObjectStore` (one S3-compatible implementation,
   `S3ObjectStore`, built on `boto3`). It never moves, deletes, or
   rewrites the extracts tree — that tree remains the sole source of
   truth and the rollback path regardless of whether the mirror is
   configured, current, or has ever run.
2. **Manifest v2 signed URLs** (`app/api/sync.py::_build_manifest_for_user`
   / `_apply_signed_url`) add `signed_url` + `signed_url_expires_at`
   (15-minute TTL) to a table's `tables[<id>]` manifest entry — but only
   when the mirror's marker index (`src/distribution.py`) shows that
   table's object is present *and* current. An unmirrored or stale table
   gets no `signed_url`; older CLIs that don't recognize the field ignore
   it. RBAC is unaffected — a `signed_url` only ever appears on an entry
   the caller could already reach via `get_accessible_tables`.
3. **`agnes pull` prefers the signed URL** (`cli/lib/pull.py::_download_one`
   / `_fetch_signed_url`) when present, fetching directly from the bucket
   (SSRF-guarded, no redirects). On ANY failure — network error, expired/
   403, md5 mismatch — it falls back to the existing app-served
   `/api/data/{id}/download` route, and md5-verifies again via the same
   `_verify_and_promote` step either path uses. A misconfigured or expired
   signed URL therefore degrades to "slightly slower pull," never to a
   corrupted or missing local parquet.

**Separate concern from DuckLake's `data_path`.** `ducklake.data_path`
(see [Analytics Data Plane](#analytics-data-plane-legacy-vs-ducklake) above)
is where DuckLake stores its *own* managed data files for the analytics
query engine — a different filesystem-coupling problem with its own
migration path. This mirror only ever reads the `extracts/` tree the
orchestrator already scans; the two do not share config, code, or a
migration story, and moving one to object storage says nothing about the
other.

---

## DuckDB Schema

### system.duckdb — `{DATA_DIR}/state/system.duckdb`

Current schema version: **123** (auto-migrated from any earlier version on startup — see `src/db.py`; the authoritative constant is `SCHEMA_VERSION` there).

| Table | Purpose |
|-------|---------|
| `schema_version` | Tracks applied migration version |
| `users` | Registered users: id, email, name, password_hash, setup/reset tokens, active flag, `must_change_password` (forces rotation of seeded/admin-set passwords) |
| `user_groups` | Named groups (`Admin`, `Everyone` seeded as `is_system=TRUE`; admin-managed and Google-synced groups) |
| `user_group_members` | `(user_id, group_id, source)` — `source ∈ {admin, google_sync, system_seed}` |
| `resource_grants` | Generic per-`(group, resource_type, resource_id)` grants (replaces `dataset_permissions` + `plugin_access`) |
| `sync_state` | Per-table sync status: last_sync, rows, file_size_bytes, hash, status |
| `sync_history` | Historical sync runs with duration and error |
| `user_sync_settings` | Per-user dataset enable/disable preferences |
| `table_registry` | Registered tables: source_type, bucket, source_table, query_mode, sync_schedule |
| `table_profiles` | JSON data profiles (stats, nulls, cardinality) per table |
| `chat_sessions` | Cloud-chat sessions (v68): per-user transcript sessions (surface, slack ids, title, message_count); dual-backend (DuckDB + Postgres via `src/models/chat.py`) |
| `chat_messages` | Cloud-chat messages (v68): role, content, tool_calls, token/model accounting, FK → `chat_sessions` |
| `user_workdirs` | Cloud-chat per-user workspace markers (v68): last init, marketplace/template SHA, agnes version at init |
| `knowledge_items` | Corporate memory knowledge entries (`confidence`, `entities`, `source_type`, `source_ref`, `valid_from`/`valid_until`, `supersedes`, `sensitivity`, `is_personal`, `is_required` — v49 dropped the scalar `domain` column; relations live in `knowledge_item_domains`) |
| `knowledge_votes` | Up/down votes on knowledge items |
| `knowledge_contradictions` | Pairs of items the LLM judge flagged as contradictory; carries `severity` and `suggested_resolution` (JSON-encoded structured action — see ADR Decision 4) |
| `verification_evidence` | One row per detected verification — persists `user_quote`, `detection_type`, and `source_ref` so future Bayesian re-calibration has raw signal (ADR Decision 3) |
| `session_extraction_state` | Tracks which `/data/user_sessions/*.jsonl` files have been processed by the verification detector |
| `audit_log` | API action log: user, action, resource, duration |
| `telegram_links` | Telegram chat_id linked to user_id |
| `pending_codes` | Telegram link confirmation codes |
| `script_registry` | Deployed Python notification scripts |
| `data_packages` | Admin-curated bundles of tables (id, slug, name, icon, color). Distributed as a single "stack" unit; tables M:N via `data_package_tables`. |
| `data_package_tables` | M:N junction between `data_packages` and `table_registry`. |
| `memory_domains` | First-class memory domain rows (id, slug, name, icon, color). Replaces the scalar `knowledge_items.domain` column dropped in v49. |
| `knowledge_item_domains` | M:N junction between `knowledge_items` and `memory_domains`. One item can live in multiple domains. |
| `user_stack_subscriptions` | Per-user LOCAL DOWNLOAD opt-in for available-tier Data Packages + Memory Domains (`(user_id, resource_type, resource_id)`) — auto-membership means both tiers are already in the stack/authorized without a row here; a row only additionally flags "keep a local copy" (`agnes pull` fetches it). Marketplace plugins keep their legacy `user_plugin_optouts` table. |

Connections: `get_system_db()` returns a cursor on a **single shared connection** per
`DATA_DIR` (protected by `threading.Lock`). Callers `close()` the cursor, not the
underlying connection. This avoids DuckDB write-lock conflicts in the multi-threaded
FastAPI process.

### analytics.duckdb — `{DATA_DIR}/analytics/server.duckdb`

Read-only views over all ATTACHed `extract.duckdb` sources. Rebuilt atomically by
`SyncOrchestrator.rebuild()`. Query endpoints open this file via `get_analytics_db_readonly()`
which ATTACHes all `extract.duckdb` files in read-only mode so remote views resolve correctly.

---

## Authentication

All auth flows issue a **JWT** (`app/auth/jwt.py`) stored as a cookie (`access_token`) or
passed as a `Bearer` token in the `Authorization` header. The `get_current_user` dependency
validates the JWT and loads the user from `users` in `system.duckdb`.

### Providers (`app/auth/providers/`)

| Provider | Available when | Flow |
|----------|---------------|------|
| `google.py` | `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` set | Google OAuth 2.0 / OIDC (Authlib). Domain restriction via `allowed_domains` in `instance.yaml`. Callback issues JWT cookie. |
| `microsoft.py` | `MICROSOFT_TENANT_ID` + `MICROSOFT_CLIENT_ID` + `MICROSOFT_CLIENT_SECRET` set, **and** the tenant is a directory GUID / verified domain | Microsoft Entra ID OIDC (Authlib), single-tenant only — `common`/`organizations`/`consumers` are refused and leave the provider unavailable. Authentication only (no Graph group sync); users land in `Everyone`. Setup + trust model: [`auth-microsoft-oauth.md`](auth-microsoft-oauth.md). |
| `email.py` | `SMTP_HOST` set | Magic link: `POST /auth/email/send-link` generates a token stored in `users.setup_token`; `POST /auth/email/verify` exchanges it for a JWT. SMTP relay is the only mail transport (SendGrid & co. via their relay host). |
| `password.py` | Always registered | Email + password with hashed credentials. |

### RBAC

Two layers, no role hierarchy (see `docs/RBAC.md` for the full reference):

- **App-level access**: membership in the `Admin` system group. The
  `require_admin` FastAPI dependency in `app.auth.access` gates admin
  endpoints (admin UI, user management, settings, …).
- **Resource-level access**: per-(group, resource_type, resource_id)
  grants in `resource_grants`. The `require_resource_access(rt,
  path_template)` dependency factory gates entity-scoped endpoints.

Table access (`src/rbac.py:can_access_table`) is a thin wrapper over
`app.auth.access.can_access(user_id, "table", table_id, conn)`. Admin
group members short-circuit; everyone else needs an explicit
`resource_grants(group, "table", table_id)` row via any group they
belong to. There is no `is_public` shortcut and no implicit "Everyone
can read" fallback — the legacy `dataset_permissions` + `is_public`
mechanism was dropped in v19.

---

## API Layer

All routes are FastAPI `APIRouter` instances registered in `app/main.py`.

### REST API (`app/api/`)

| Router | Prefix | Key endpoints |
|--------|--------|---------------|
| `sync` | `/api/sync` | `GET /manifest` (hash manifest, per-user filtered), `POST /trigger` (admin), `GET/POST /settings`, `GET/POST /table-subscriptions` |
| `data` | `/api/data` | Download parquet files for synced tables |
| `query` | `/api/query` | `POST /` — execute a SELECT against `analytics.duckdb` (sandbox enforced) |
| `admin` | `/api/admin` | `GET /discover-tables`, `GET /registry`, `POST /register-table`, `PUT /registry/{id}`, `DELETE /registry/{id}` |
| `catalog` | `/api/catalog` | Data catalog: table list, profiles, metric definitions |
| `users` | `/api/users` | User CRUD (admin), self-service profile |
| `permissions` | `/api/permissions` | Dataset permission grants (admin) |
| `access_requests` | `/api/access-requests` | Request + review workflow |
| `scripts` | `/api/scripts` | Deploy, list, run, delete Python notification scripts |
| `settings` | `/api/settings` | Instance and user settings |
| `memory` | `/api/memory` | Corporate memory CRUD and voting |
| `upload` | `/api/upload` | File upload (CSV, parquet) |
| `telegram` | `/api/telegram` | Telegram account link/unlink |
| `jira_webhooks` | `/api/jira` | Jira webhook receiver (HMAC-SHA256 verified) |
| `health` | `/api/health` | Service health, sync status, disk |

### Auth routes (`app/auth/`)

`POST /auth/token`, `GET /auth/me`, `POST /auth/logout`,
`GET /auth/google/login`, `GET /auth/google/callback`,
`POST /auth/email/send-link`, `POST /auth/email/verify`,
`POST /auth/password/login`

### Web UI (`app/web/`)

HTML dashboard routes served by Jinja2 templates (`app/web/templates/`), registered last
in `app/main.py` (catch-all). Route handlers live in `app/web/router.py` and return
`templates.TemplateResponse(request, "<name>.html", ctx)`.

**Base-template hierarchy (the design-system page shell — #367 / #482).** Every page
`{% extends %}` one of three bases:

- **`base_ds.html` — the canonical base.** Loads the stylesheets in the required
  order (`style-custom` → `design-tokens` → `components` → `stack_card` → the
  attribute-scoped `rail`/`paper-skin` sheets), sets
  `<html data-theme="{{ instance_theme | default('blue') }}" data-ui-layout="rail">`
  + the favicon, renders the production nav (`_app_rail.html` — the only chrome;
  the topnav chrome (`_app_header.html`) was retired in Wave 0, 2026-08 —
  `get_ui_layout()` always returns `"rail"`, and a configured
  `instance.ui_layout` / `AGNES_UI_LAYOUT` is ignored with a startup warning),
  the canonical `.container` shell, the operator
  `custom_scripts` placements (`head_start` / `head_end` / `body_end`), and the global JS
  (`_app_scripts.html` — undo toast, modal-Esc, command palette, admin shortcuts). It
  **auto-imports `_components.html` as `ds`**, so pages call `{{ ds.button(…) }}` without
  importing. Body extension point: `{% block content %}`. Blocks: `title`, `head_extra`,
  `body_attrs`, `container_modifier`, `hero`, `flash`, `content`, `extra_scripts`,
  `scripts` (plus `layout` to opt out of the `.container` wrap entirely, e.g. full-width
  pages like `dashboard`).
- **`base_page.html` — content-page shell (extends `base_ds`).** Adds the standard chrome:
  an automatic gradient hero (`_page_hero.html`, rendered from top-level
  `page_hero_eyebrow` / `page_hero_title` / `page_hero_subtitle` vars) + `{% block toolbar %}`
  + `{% block page %}`. Use it for pages that have a hero (and optional toolbar).
- **`base.html` — legacy.** Only bespoke/unmigrated pages still extend it: the
  catalog/marketplace `*_detail` card-hero pages and the `_message` partial are
  intentionally kept; any others are migration-tail follow-ups. **Do not build new pages
  on `base.html`.**

Shared partials: `_page_hero.html` (canonical hero), `_components.html` (the `ds.*` macro
library — buttons, panels, tabs, …), `_app_rail.html` (nav), `_app_scripts.html` (global
JS). Component CSS lives in `app/web/static/` (`style-custom.css` + `css/*.css`); all colours
come from `--ds-*` tokens in `css/design-tokens.css`.

Anti-drift is enforced by `tests/test_design_system_contract.py`: leaf templates may **not**
reintroduce a `.container:has()` width opt-out or a bare `:root {}` token block, must use the
canonical primitives (not deprecated class aliases), and must reference `var(--ds-primary)`
rather than `var(--primary)`. The build recipe is under **Extending the Platform → New Web
Page**.

---

## Services

Each service is a self-contained Python package (`services/<name>/__main__.py`) run as a
Docker Compose service.

| Service | Profile | Schedule / Mode | Description |
|---------|---------|-----------------|-------------|
| `scheduler` | default | Always-on; polls every N seconds | Lightweight sidecar that triggers jobs via the app's REST API: `POST /api/sync/trigger` every 15 min, `GET /api/health` every 5 min, `POST /api/admin/run-session-collector` every 10 min, `POST /api/admin/run-verification-detector` every 15 min, `POST /api/admin/run-corporate-memory` every 17 min, `POST /api/marketplaces/sync-all` daily 03:00. Auth via `SCHEDULER_API_TOKEN` or auto-fetch from `/auth/token`. |
| `telegram_bot` | `full` | Always-on (long-poll) | Telegram bot: polling + HTTP dispatch, `/status` command, notification script execution. |
| `corporate_memory` | (driven by scheduler) | Every 17 min | Scans `CLAUDE.local.md` files in **both** layouts — `<HOME_BASE>/<user>/` (bare VM, analysts working on the server; root overridable via `CORPORATE_MEMORY_HOME_BASE`) and `${DATA_DIR}/user_local_md/` (laptop analysts, uploaded by `agnes push`) — extracts knowledge via LLM (Claude Haiku), writes to `knowledge_items` in system.duckdb. Inline contradiction detection runs after each new item: one batched Haiku structured-output call returns judgments + structured resolution suggestions for every same-domain candidate (no SQL keyword pre-filter — see [ADR Decision 4](ADR-corporate-memory-v1.md)). Driven by scheduler-v2 since #176. |
| `verification_detector` | (driven by scheduler) | Every 15 min | Scans unprocessed analyst session JSONLs, extracts corrections / confirmations / unprompted definitions via Haiku structured outputs. Confidence is computed in code from `(source_type, detection_type)` — never trusted from the LLM. Each verification persists a `verification_evidence` row carrying `user_quote` + `detection_type` ([ADR Decision 3](ADR-corporate-memory-v1.md)). Driven by scheduler-v2 since #176. |
| `session_collector` | (driven by scheduler) | Every 10 min | Copies Claude Code `.jsonl` session transcripts to central storage. Driven by scheduler-v2 since #176. |

**Desktop/browser notifications** (formerly the standalone `ws_gateway` service, TCP 8765 + a Unix-socket HTTP dispatch): absorbed into the main app (wave-2F task 6). The WS endpoint (`/api/notifications/ws`, same JWT auth, per-user connection limit, heartbeat ping/pong) lives in `app/api/notifications_ws.py` and serves only on `Role.GATEWAY` processes; producers (e.g. the Telegram bot) call `app/notifications.py::publish_notification(user, payload)`, which publishes on the coordination pub/sub channel `notify:{user}` — replacing the old in-memory `connections` dict + Unix socket, and making delivery work across replicas when `coordination.backend=redis`.

**Multi-replica chat HA** (wave-2F): `ChatManager` state — previously a
single process's in-memory dicts — is now coordination-backed so chat can
run on any number of `Role.GATEWAY` replicas once `coordination.backend:
redis`. A **routing lease** (`app/chat/routing.py`, key `chat:{chat_id}`)
names the one gateway currently hosting a session's live sandbox, claimed
on spawn and renewed on the idle-reaper heartbeat. Every outbound frame
carries a monotonic `seq` (`app/chat/frame_seq.py`) and is appended to a
bounded **replay stream** (`app/chat/replay.py`, `chat-out:{chat_id}`), so
a WS reconnect with `?last_seq=` gets exactly the missed frames, or a
`full_refresh` control frame if the gap can't be closed. Commands that
land on a non-owning gateway (a webhook behind a load balancer, an
`/agnes` slash command) are forwarded over an **inbound stream**
(`app/chat/inbound.py`, `chat-in:{chat_id}`) to the owning gateway instead
of racing a second runner. A reconnect that lands on a gateway which
doesn't hold the lease **claims (steals) it and takes over**
(`ChatManager._takeover_foreign_session`): destroy the old sandbox, spawn
a fresh runner, replay recent turns for continuity — not a live handoff,
so any turn in flight on the old gateway at that moment is lost (accepted
v1 trade-off). Desktop/browser notifications (above) ride the same
coordination fabric. Full operator-facing detail — the gate-lift
condition, and why N single-worker replicas beat `UVICORN_WORKERS > 1` on
a gateway — lives in [`DEPLOYMENT.md`](DEPLOYMENT.md) → *Multi-process →
Multi-replica chat HA*.

### Corporate-memory privacy boundary

`is_personal` on `knowledge_items` is enforced as an authorization rule at every read site, not a UI hint:

- `GET /api/memory` and `GET /api/memory?search=…` silently coerce `exclude_personal=True` for any caller whose role is below `km_admin`.
- `GET /api/memory/{id}/provenance` and `POST /api/memory/{id}/vote` use the shared `_can_view_item(user, item)` helper (`not is_personal OR contributor OR km_admin/admin`) and return **404** (not 403) on denial to avoid existence-leak.
- Contributors reach their own personal items via `/api/memory/my-contributions`.

See [ADR Decision 1](ADR-corporate-memory-v1.md) for the full reasoning.

---

## Hosted Data Apps

Off by default (`data_apps.enabled`). Lets analysts host their own web
applications — Flask/Dash dashboards, static SPAs — next to the data, using
the same upstream `data-app-python-js` runtime image and `keboola-config/`
app-repo contract as Keboola's existing Data Apps product. Full design:
[`docs/superpowers/specs/2026-07-21-data-apps-design.md`](superpowers/specs/2026-07-21-data-apps-design.md).

```
┌──────────────┐   deploy    ┌──────────────┐   docker    ┌──────────────┐
│   registry   │ ──────────▶ │ apps-runner  │ ──────────▶ │  runtime     │
│ (data_apps   │  spec +     │  (sidecar,   │  run/stop/  │  container   │
│  table +     │  config.json│  holds the   │  status     │ agnes-       │
│  git repo)   │             │  Docker sock)│             │ dataapp-<slug>│
└──────────────┘             └──────────────┘             └──────┬───────┘
       ▲                                                          │
       │ RBAC-gated ingress + wake-on-request                     │ :8888
       └──────────────────── ingress proxy ◀───────────────────────┘
                          `/apps/<slug>/…` (or subdomain)
```

- **Registry** (`src/data_apps/`, `src/repositories/data_apps.py`): the
  `data_apps` table is the source of truth (owner, repo mode/URL, resource
  limits, sleep state). `src/data_apps/spec.py` builds the runtime's
  `config.json` (git remote + secrets) and the Docker container spec from a
  registry row; `src/data_apps/git_repos.py` hosts internal (`repo_mode:
  internal`) app repos as bare git repos served over HTTP with
  push-to-deploy (`POST /api/data-apps/{slug}/deploy` fast-forwards the
  `agnes-live` branch and calls the runner).
- **apps-runner** (`services/apps_runner/`): the only process holding the
  Docker socket — deliberately dumb, no registry access or RBAC of its own,
  just `up`/`stop`/`resume`/`status`/`logs` translated to Docker Engine API
  calls over a token-gated internal HTTP API (`src/data_apps/runner_client.py`
  is the app-side client). Image name is allowlisted, mounts are fixed
  per-slug paths — no arbitrary image or volume ever reaches `docker run`.
  The same sidecar carries the chat-sandbox API when `chat.provider: docker`
  (`services/apps_runner/sandbox_api.py`, client
  `app/chat/sandbox_runner_client.py`): a separate route prefix with its own
  image allowlist, its own container-name confinement, and per-mount
  validation, so the chat gateway also stays off the socket.
- **Ingress proxy** (`app/api/data_apps_proxy.py`): reverse-proxies
  `/apps/<slug>/…` (and, when `data_apps.subdomain_base` is configured,
  `<slug>.<subdomain_base>`) to the running container, RBAC-gated the same
  way as every other resource type. A sleeping app gets woken on first
  request (holding page while it starts) rather than 404ing; an idle-reaper
  scheduler job puts unused apps back to sleep after
  `data_apps.default_idle_timeout_s`.

Apps reach Agnes data the same way the CLI/MCP surfaces do — as an API
client, via an owner-scoped token injected as `AGNES_TOKEN` — never through a
mounted parquet. See spec §8 for the full rationale and the owner-inherited
access model this implies for sharing.

That token's `data-app:<slug>` scope is enforced fail-closed in
`app/auth/pat_resolver.py`: it is admitted only on the app data surface —
`/api/query`, `/api/data`, the `/api/catalog` read routes, `/api/metrics`,
`/api/glossary`, the read-only `/api/semantic-models` members, and the
`/api/v2` catalog/schema/sample/scan routes the `agnes` CLI calls from inside
a container — and refused everywhere else, notably `/api/admin/*` and the
credential-minting routes. The allowlist is exact paths plus narrow subtrees,
not one entry per router, so a route added later under an allowed path is not
admitted by accident; a test walks the real route table to keep that honest.

The scope narrows which **endpoints** the app may call; it does not narrow
**which rows** it sees — inside that surface the app still reads with the
owner's grants, evaluated live per request.

**Container hardening** (spec §10): every data-app container runs
`cap_drop: ALL`, `no-new-privileges` and a `pids_limit`
(`data_apps.container_pids_limit`, default 512) — never applied to the
chat-sandbox path, which needs broader write access for agent-authored code.
A read-only root filesystem (`data_apps.container_read_only`) is available but
**off by default**: it needs a tmpfs allowlist verified against the shipped
runtime image, whose nginx + supervisord write outside the `/tmp` + `/app`
tmpfs the spec builder currently supplies, so turning it on unverified would
crash-loop every hosted app.

**Origin isolation** (`data_apps.allow_same_origin`, default **off**): a hosted
app runs user-authored JS, so *where* it is served decides what that JS can
reach. Served on the **main origin** (`/apps/<slug>/…`) it is same-origin with
the Agnes `/api`, so its JS can `fetch('/api/…', {credentials:'include'})` with
the viewer's own session cookie and read the response — mint a PAT, read admin
config — because same-origin reads need no CORS and the `CsrfOriginMiddleware`
only refuses *cross*-origin state changes. No response header closes this (a
same-origin `window.open`/`<iframe>` to `/api` is DOM-readable and ungoverned by
CSP). The only fix is origin isolation: configure `data_apps.subdomain_base` so
apps are served from their own origin (`<slug>.<subdomain_base>`), where the
existing CORS policy blocks the credentialed read and `CsrfOriginMiddleware`
blocks the write. The ingress proxy therefore **refuses** to serve an app on the
main origin unless one of: the request carries a per-app `data-app-preview:<slug>`
token (the in-chat preview — served same-origin only to a caller who already
holds a short-TTL token for *that* app, so a plain drive-by navigation to
`/apps/<other-slug>/` stays refused), or the operator opts into
`data_apps.allow_same_origin` / `AGNES_DATA_APPS_ALLOW_SAME_ORIGIN` (serve ALL
apps same-origin — trusted authors only). Requests that arrive on a data-app
subdomain are always served. A startup log flags an enabled-but-unservable
posture (no isolated origin, no opt-in).

The isolated-origin signal is the request's `Host` (rewritten by
`app/data_apps_subdomain.py`), so the TLS-terminating reverse proxy in front of
Agnes **must route by Host** — a `<slug>.<subdomain_base>` request should reach
Agnes only when the client genuinely connected to that subdomain, and a
client-supplied `Host` that doesn't match the terminating certificate should be
rejected or normalized. A browser cannot forge `Host` for a cross-origin request,
so this is a deployment-correctness requirement rather than a browser-reachable
bypass; when `subdomain_base` is unset the marker is never set regardless of
`Host`.

**Network egress** (GCP metadata): a data-app container sits on the `agnes-apps`
bridge with normal egress (its runtime image installs dependencies from PyPI/npm
at boot). On a cloud VM that reach includes the instance **metadata server**
(`169.254.169.254`), from which a compromised app could read the VM's
service-account token and pivot to the whole cloud project. The
`customer-instance` Terraform module blocks this at the host firewall — an
idempotent `DOCKER-USER` iptables DROP from the `agnes-apps` source subnet to
`169.254.169.254/32`, installed at boot (see the `container-metadata-hardening`
block in `infra/modules/customer-instance/startup-script.sh.tpl`). It is
source-scoped, not a blanket block, so the Agnes app container's own metadata
use (e.g. BigQuery GCE-metadata auth) is unaffected — the `app` service pins
`default` as its gateway network (`docker-compose.yml` sets BOTH
`networks.default.gw_priority` — the field that actually selects the gateway
on Docker Engine ≥ 28, where `priority` deliberately does not — and the older
`priority`, which pre-28 engines derive the default route from via connection
order) so its egress routes via `default`, keeping its source IP out of the
`agnes-apps` subnet the rule matches. `gw_priority` requires Compose CLI ≥
v2.33.1 — older Compose rejects the key at validation. The rule resolves the
subnet at boot; re-run the block (or reboot) after any manual `agnes-apps`
network recreation. For a non-Terraform host
(plain docker-compose), install the equivalent rule yourself:
`iptables -I DOCKER-USER -s <agnes-apps-subnet> -d 169.254.169.254/32 -j DROP`
(resolve the subnet with `docker network inspect agnes-apps`). Least-privilege
the VM service account regardless — the firewall rule is defense-in-depth, not a
substitute for a narrowly-scoped SA.

---

## Background Jobs

A durable job queue (the `jobs` table, present on both backends —
`src/repositories/jobs.py` / `jobs_pg.py`, routed through the
`jobs_repo()` factory) backs a worker runtime for the heaviest,
most contention-prone recurring work; the rest of the Services table
above stays on direct synchronous HTTP calls from the scheduler.

**Schema** (migration v94 / Alembic `0041_jobs_v94`): `id`, `kind`,
`payload_json`, `status` (`queued`/`running`/`done`/`failed`), `priority`,
`run_after`, `attempts`/`max_attempts`, `lease_expires_at`/`leased_by`/
`lease_token`, `idempotency_key`, `error`, plus `created_at`/`started_at`/
`finished_at`. `idempotency_key` dedup is enforced in
`JobsRepository.enqueue()` via an in-process lock on DuckDB (which has no
partial-index support) and via a partial unique index + `ON CONFLICT` on
Postgres — same dedup behavior, different mechanism per backend.

**Worker loop** (`app/worker/runtime.py`, started from `app/main.py`'s
lifespan when the process's `AGNES_ROLE` includes the worker plane): up to
three lanes share one asyncio loop — heavy (concurrency 1), light
(concurrency 2), and extraction (concurrency **configurable**, default 1 —
`extraction.concurrency` / `AGNES_EXTRACTION_CONCURRENCY`, clamped to
`[1, 8]`, resolved once at worker start; spec §7.5 — see below and
`docs/DEPLOYMENT.md#multi-process`). Extraction's slot count is the only
one of the three that isn't hardcoded — parallelism ACROSS connections
(the per-connection idempotency key still caps one in-flight job per
connection), not sharding within one.
Each lane slot repeats `claim_next()` → runs the kind's
handler in a thread while a heartbeat extends the lease →
`complete()`/`fail()`. A fresh-per-claim `lease_token` (not just
`worker_id`) guards every call so a stale slot can't clobber a fresh
claim of the same job by another slot in the same process. A separate
sweep task reclaims exhausted/expired leases (`reap_exhausted()`), and
shutdown drains in-flight jobs within a bound instead of hard-killing
them.

**Which lanes a process spawns** is controlled by `AGNES_WORKER_LANES`
(`app/worker/runtime.py::selected_lanes()`) — comma-separated lane names,
unset defaulting to heavy+light exactly as before this env var existed
(the extraction lane is opt-in; see below). Mirrors `AGNES_ROLE`'s
env-var convention, including failing loudly on an unknown token.

**Kinds registry** (`app/worker/kinds.py::register_all_kinds()`,
`app/worker/registry.py`): `data-refresh` and
`jira-refresh` (heavy lane), `marketplaces-sync`, `session-collector`,
and `corporate-memory` (light lane), among others. Each handler is a thin adapter over
the function already backing the equivalent HTTP endpoint — no logic is
duplicated between the queued and HTTP-triggered call sites.

**Extraction lane** (spec §7.5 "Extraction inside Agnes (later)" / §16
step 7 of `docs/superpowers/specs/2026-08-27-fact-graph-over-collections-
design.md`): a lane of its own, not sharing heavy, because a corpus
re-extraction sitting in heavy's concurrency-1 slot would block every
table sync for its whole duration. Its one kind, `corpus-extraction`, is
the producer-invocation seam — it does not crawl/convert/anonymize/
extract itself; it resolves a `sharepoint` connection's credentials the
same way the admin UI does (`connectors.sharepoint.settings
.resolve_sharepoint_settings`, vault-first then the server's
`SHAREPOINT_CERT_PRIVATE_KEY` env var) and shells out to the
operator-configured `extraction.producer.command`/`.module`
(`instance.yaml`, off by default via `extraction.enabled` — a registered
switch, `AGNES_EXTRACTION_ENABLED`) under a bounded timeout, with the
resolved credentials reaching the subprocess only via its child
environment — never argv, never logged. That child env is a curated
non-secret allowlist (`PATH`, locale/timezone/tempdir/TLS/proxy vars) plus
any operator-opted-in `extraction.producer.env_passthrough`, plus the
three named SharePoint credentials and the corpus id — never the full
parent environment, so no other instance secret (vault key, LLM API key,
DB DSN, ...) reaches an external, admin-configurable binary. The producer
itself (the operator's own producer, adopted per spec §7.1) is not
vendored into this repo. Off by default and additive: an instance that
never sets `extraction.enabled`/`AGNES_WORKER_LANES` is unaffected.

Deployment: the `worker` Dockerfile build target (an extension point,
`EXTRACTION_PRODUCER_INSTALL` build-arg, for bundling a producer's runtime
deps — never built by default; `docker build .` with no `--target` still
produces the ordinary `app` image) and the `extraction-worker` compose
service (`docker-compose.yml`, profile-gated, `AGNES_WORKER_LANES
=extraction`) let a deployment run extraction in its own process/container
instead of adding it to the main `app` service's lanes. That service sets
`AGNES_ROLE=worker`, which is a role split — running it makes the
deployment multi-process, requiring Postgres app-state, explicit
`JWT_SECRET_KEY`/`SESSION_SECRET`, and `coordination.backend: redis` (same
as any other role-split process; see
[`DEPLOYMENT.md#multi-process`](DEPLOYMENT.md#multi-process)). Flipping
`extraction.enabled` and starting the profile is not sufficient on its
own — `app/startup_guards.py::validate_deployment` refuses to boot
otherwise.

**Cross-process rebuild lease**: `SyncOrchestrator`'s `_rebuild_lock` is
an in-process `threading.Lock` — invisible across processes. In a
role-split topology (a dedicated `api` process handling
`/api/sync/trigger` + the Jira webhook, and a separate `worker` process
running enqueued jobs) both can independently reach `rebuild()`/
`rebuild_source()`'s critical section and race on `analytics.duckdb`.
`src/db_pg.py::rebuild_lease()` wraps that section in a Postgres
advisory lock (no-op on the DuckDB backend, already single-process by
the startup guard) so the two processes serialize instead of racing.

**REST/CLI/MCP**: `app/api/jobs.py` (`POST /api/jobs`,
`GET /api/jobs/{id}`, `GET /api/jobs`), `agnes admin jobs
enqueue|show|list`, and the `admin_job_enqueue`/`admin_job_get`/
`admin_jobs_list` MCP tools — all gated by `require_admin` (the
scheduler's shared-secret bearer resolves to a synthetic admin user, so
no special-casing is needed).

Which scheduler rows moved to the queue vs. stayed synchronous HTTP, and
why: [`jobs-classification.md`](jobs-classification.md).

---

## Coordination Backend

`app/coordination/` provides a `CoordinationBackend` abstraction (`base.py`)
with two implementations — `MemoryCoordinationBackend` (in-process dict +
lock, the default) and `RedisCoordinationBackend` (redis-py) — selected by
`app.coordination.factory.coordination()`, a process-wide singleton keyed
off `coordination.backend` (`instance.yaml`) / `AGNES_COORDINATION_BACKEND`
(env) and `redis.url` / `AGNES_REDIS_URL`. The interface is four primitive
groups: TTL key/value (`kv_set`/`kv_get`/`kv_delete`, the latter an atomic
get-and-delete for single-use tokens), counters (`incr`, TTL applied only
on first increment — window semantics are the caller's), leases
(`lease_acquire`/`lease_renew`/`lease_release`, exclusive per name/holder),
and pub/sub (`publish`/`subscribe`). Both implementations are exercised
against the same contract suite (`tests/test_coordination_contract.py`) so
a consumer written against the ABC behaves identically regardless of
backend. `app/coordination/leases.py::run_with_lease` layers an
acquire-or-wait-then-renew-then-release loop on top of the raw lease
primitives for long-running singleton consumers (Slack Socket Mode,
Telegram long-poll, the paused-sandbox sweep).

`coordination.backend=redis` is itself one of the three conditions
`app/startup_guards.py::is_multi_process()` checks — configuring it
declares multi-process intent even ahead of an actual role split, and the
same guard module then requires it (alongside `DATABASE_URL` and explicit
secrets) for any topology that is role-split or runs
`UVICORN_WORKERS > 1`.

Consumers riding this abstraction: chat WS auth tickets, leader leases,
shared rate limits/chat quotas, cache-invalidation pub/sub, operational
auth codes, and `.env_overlay` token reload — enumerated with their key
prefixes and TTLs in [`DEPLOYMENT.md`](DEPLOYMENT.md) → *Multi-process →
Coordination backend*, which also states the disposability invariant this
design is built around: a single non-HA Redis is the supported shape, and
every consumer recovers cleanly from a `FLUSHALL`.

---

## Security

### Query Sandbox (`app/api/query.py`)

The `/api/query` endpoint enforces a strict SQL allowlist:

- Only `SELECT` and `WITH` queries accepted.
- Blocklist of ~30 keywords/functions: `DROP`, `DELETE`, `INSERT`, `UPDATE`, `ALTER`,
  `CREATE`, `ATTACH`, `DETACH`, `LOAD`, `INSTALL`, `COPY`, `PRAGMA`, file functions
  (`read_parquet`, `read_csv`, `glob`, etc.), URL schemes (`s3://`, `gcs://`, `http://`),
  and multi-statement separator (`;`).
- Table-level RBAC: forbidden views are detected by word-boundary regex match against
  the SQL text. Query is rejected if user lacks access to any referenced table.
- Analytics DB opened in `read_only=True` mode per request.

### Script Sandbox (`app/api/scripts.py`)

Deployed and ad-hoc Python scripts are checked against a pattern blocklist before execution:

- Blocked: `subprocess`, `shutil`, `ctypes`, `importlib`, `socket`, `requests`, `httpx`,
  `urllib`, `os`, `sys`, `signal`, `open(`, `pathlib`, `exec(`, `eval(`, `compile(`,
  `__import__`, and others.
- Scripts run in a subprocess with a configurable timeout (`SCRIPT_TIMEOUT`, default 300 s)
  and capped output (`SCRIPT_MAX_OUTPUT`, default 64 KB).

### Identifier Validation (`src/orchestrator.py`, `src/db.py`)

All dynamic SQL identifiers (source names, table names, extension aliases) are validated
against `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` before interpolation. Invalid identifiers are
skipped with a log warning, never executed.

### Authentication Layers

| Layer | Mechanism |
|-------|-----------|
| Web UI / API | JWT Bearer token or `access_token` cookie |
| Google OAuth | Authlib OIDC + domain allowlist |
| Email magic link | `secrets.token_urlsafe(32)` stored in `users.setup_token`, 1-hour expiry |
| Jira webhook | HMAC-SHA256 signature verification |
| Inter-service (scheduler) | `SCHEDULER_API_TOKEN` env var or auto-fetched JWT |

---

## Configuration

```
config/instance.yaml             (instance-specific, not committed)
    │ loaded by config/loader.py
    │ ${ENV_VAR} references resolved from .env
    ▼
app/instance_config.py           (exposes get_data_source_type(), get_allowed_domains(), get_value())
    ▼
FastAPI dependency injection     (passed to API routers as needed)
```

Table configuration lives in `table_registry` inside `system.duckdb`, not in static files.
Use `POST /api/admin/register-table` or the web UI admin panel to register tables.

Required env vars: `DATA_DIR`, `JWT_SECRET_KEY`. Source-specific vars (`KEBOOLA_STORAGE_TOKEN`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `SMTP_HOST`, etc.) are
optional and gate the relevant connectors/providers.

---

## Data Filesystem Layout

```
/data/
├── state/
│   └── system.duckdb          user registry, sync state, table_registry, audit log
├── analytics/
│   └── server.duckdb          master analytics DB (views over all extracts)
└── extracts/
    ├── keboola/
    │   ├── extract.duckdb     _meta + views
    │   └── data/*.parquet
    ├── bigquery/
    │   └── extract.duckdb     _meta + _remote_attach + remote views
    └── jira/
        ├── extract.duckdb     _meta + views
        └── data/*.parquet
```

---

## Extending the Platform

### New Data Source

1. Create `connectors/<name>/extractor.py`.
2. Write `extract.duckdb` with `_meta` table and views/tables.
3. Add `data/*.parquet` for local sources.
4. Add `_remote_attach` row if views reference an external DuckDB extension.
5. `SyncOrchestrator` picks it up automatically on next `rebuild()`.

### New Auth Provider

1. Add `app/auth/providers/<name>.py` exporting a FastAPI `APIRouter`.
2. Register the router in `app/main.py`.
3. All providers must issue a JWT and set the `access_token` cookie on success.

### New Web Page

Dashboard pages use the design-system page shell (see **Web UI** above). To add one:

1. Add a route in `app/web/router.py` returning
   `templates.TemplateResponse(request, "<name>.html", ctx)`.
2. Create `app/web/templates/<name>.html`:
   - **Extend `base_page.html`** if the page has a gradient hero (and optional toolbar),
     otherwise **`base_ds.html`**. **Never `base.html`** — it is legacy.
   - On `base_page`: declare the hero with top-level
     `{% set page_hero_eyebrow / page_hero_title / page_hero_subtitle %}` (it renders
     automatically — do **not** `{% include "_page_hero.html" %}` yourself), put action
     buttons in `{% block toolbar %}`, and the page body in `{% block page %}`.
   - On `base_ds`: put the body in `{% block content %}`.
   - Page-specific CSS goes in `{% block head_extra %}` (a `<style>` block) — never an
     inline `<style>` in the body. Prefer the `--ds-*` tokens + existing component classes;
     raw `#rrggbb` literals and `var(--primary)` are rejected by the contract test.
   - Call `{{ ds.button(…) }}` / `{% call ds.panel(…) %}…{% endcall %}` directly — `ds` is
     auto-imported by `base_ds`; **do not** add `{% import "_components.html" as ds %}`.
   - Wider shell? `{% block container_modifier %}container--wide{% endblock %}`. Do **not**
     add a `.container:has(.x){max-width}` opt-out or a bare `:root {}` block — both fail
     `tests/test_design_system_contract.py`.
   - Nav, theme (`data-theme`), favicon, operator `custom_scripts`, and the global JS are
     already provided by the base — don't reimplement them.
3. Verify: `pytest tests/test_design_system_contract.py tests/test_web_ui.py -q`, then
   render-check the route (status 200; the hero renders exactly once; the canonical
   `.container` is present; no `.container:has(` and no `class="page-shell"` in the output).

Reference adopters: `profile.html` (on `base_page`) and `corporate_memory.html` (on
`base_ds`). Note: the legacy `.X-page { max-width }` inner-width wrappers some migrated
pages still carry are intentional *content* widths inside the 1280px `.container`, not a
pattern to copy for new pages.
