# ORM-Migration Inventory — `cli/`, `connectors/`, `services/`, `scripts/`

Scope: 197 `.py` files across the four directories. Endgame: SQLAlchemy ORM
models replace the dual-repo pattern (DuckDB + Postgres). DuckDB stays for
**analytics only** (parquet views in `analytics.duckdb`, plus
connector-produced `extract.duckdb` files).

Convention used below: "state tables" = anything in `system.duckdb` / PG
(users, table_registry, sync_state, audit_log, marketplace_*, store_*,
usage_*, etc.). "Analytics" = parquet views and the connector
`extract.duckdb` contract (`_meta`, `_remote_attach`).

---

## `cli/`

### `cli/` root + helpers

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `cli/__init__.py` | empty | infra | — | — | keep-as-is |
| `cli/main.py` | Typer root, registers all subcommands | cli-glue | none | — | keep-as-is |
| `cli/client.py` | HTTP client wrapper (`get_client`) — auth/retries/streaming | cli-glue | none | — (consumes API only) | keep-as-is |
| `cli/config.py` | local token / server URL / **client-side** `sync_state.json` | infra | none | — (JSON file `sync_state.json`, NOT the server `sync_state` table) | keep-as-is |
| `cli/error_render.py` | Pretty-print HTTP errors | infra | none | — | keep-as-is |
| `cli/snapshot_meta.py` | Snapshot sidecar JSON metadata + file lock | infra | none | — | keep-as-is |
| `cli/update_check.py` | Auto-check newer CLI version on server | infra | none | — | keep-as-is |
| `cli/v2_client.py` | `/api/v2/*` HTTP helpers | cli-glue | none | — | keep-as-is |

### `cli/lib/` (helpers used by commands)

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `cli/lib/__init__.py` | docstring only | infra | — | — | keep-as-is |
| `cli/lib/claude_sessions.py` | locate `~/.claude/projects/*` transcripts | infra | none | — | keep-as-is |
| `cli/lib/commands.py` | install Claude Code workspace slash-commands | infra | none | — | keep-as-is |
| `cli/lib/hooks.py` | install SessionStart/SessionEnd hooks | infra | none | — | keep-as-is |
| `cli/lib/initial_workspace.py` | re-export of `src.initial_workspace` helpers | infra | none | — | keep-as-is |
| `cli/lib/loopback.py` | localhost OAuth callback listener | infra | none | — | keep-as-is |
| `cli/lib/marketplace.py` | constants for Claude Code marketplace clone | infra | none | — | keep-as-is |
| `cli/lib/override.py` | "is this an override workspace?" | infra | none | — | keep-as-is |
| `cli/lib/private_list.py` | local "do not upload" list | infra | none | — | keep-as-is |
| `cli/lib/pull.py` | **`run_pull`** — refresh workspace parquets + rebuild local DuckDB views | cli-glue | **L699-731**: `conn.execute("SELECT table_name FROM information_schema.tables …")`, `DROP VIEW IF EXISTS "…"`, `CREATE VIEW … AS SELECT * FROM read_parquet(…)` — analytics views over downloaded parquets | none (client-side `sync_state.json` only) | keep-as-is (analyst-local analytics DuckDB; no state tables) |
| `cli/lib/pull_sync.py` | per-type sync engine for unified stack | infra | none (JSON state on disk) | — | keep-as-is |
| `cli/lib/push_lock.py` | single-instance lock for `agnes push` | infra | none | — | keep-as-is |
| `cli/lib/session_health.py` | detect broken `capture-session` hook | infra | none | — | keep-as-is |
| `cli/lib/session_queue.py` | session jsonl upload queue mgmt | infra | none | — | keep-as-is |

### `cli/mcp/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `cli/mcp/__init__.py` | docstring | infra | — | — | keep-as-is |
| `cli/mcp/_dynamic_passthrough.py` | dynamic registration of passthrough MCP tools | infra | none | — | keep-as-is |
| `cli/mcp/server.py` | Agnes MCP stdio server (catalog/schema/describe/query) | cli-glue | **L203-204**: wraps user SQL in `SELECT * FROM (<sql>) AS _q LIMIT N` and `conn.execute(wrapped)` over local analytics DuckDB | — (analytics views only) | keep-as-is |

### `cli/commands/` — root + admin

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `cli/commands/__init__.py` | empty | infra | — | — | keep-as-is |
| `cli/commands/admin.py` (1199 lines) | huge admin Typer group — register-table, tables, sources, users, groups, grants, etc. | cli-glue | **none** — all routes through `src.repositories` + `get_system_db` context | reads all state via repos (table_registry, users, user_groups, resource_grants, marketplace_*, sync_state, sync_history, etc.) | easy (already on repos; ORM swap is mechanical) |
| `cli/commands/admin_activity.py` | terminal access to Activity Center | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_ask.py` | NL telemetry query | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_autodoc.py` | LLM-generate table descriptions | cli-glue | none — uses `profile_repo`, `table_registry_repo` | table_registry, profile_results | easy |
| `cli/commands/admin_data_package.py` | CRUD over Data Packages (v49) | cli-glue | none (HTTP) | — (via API) | keep-as-is |
| ~~`cli/commands/admin_data_semantics.py`~~ | scaffold workspace data-semantics pack | — | — | — | **deleted** (#1707 Block 6: pre-Ossie scaffolder, nothing read its output) |
| `cli/commands/admin_mcp.py` | Universal MCP source + tool admin | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_memory_domain.py` | CRUD over Memory Domains | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_metrics.py` | import/export/validate metric YAML ↔ DB | cli-glue | none — uses `src.repositories` | metric_definitions | easy |
| `cli/commands/admin_news.py` | news show/draft/edit/publish/etc. | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_sessions.py` | terminal access to global Sessions browser | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_store.py` | operator-flavored bulk Store ops | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/admin_usage.py` | telemetry export | cli-glue | none (HTTP) | — | keep-as-is |

### `cli/commands/` — analyst commands

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `cli/commands/auth.py` | login/logout/whoami/import-token | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/capture_session.py` | SessionStart hook helper | cli-glue | none | — | keep-as-is |
| `cli/commands/catalog.py` | `agnes catalog` — list tables + metrics | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/db.py` | `agnes admin db {state,migrate,job,cancel}` | cli-glue | none (HTTP to `/api/admin/db/*`) | — | keep-as-is |
| `cli/commands/describe.py` | `agnes describe <table>` — schema + sample rows | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/diagnose.py` | workspace diagnostics | cli-glue | none | — (reads client `sync_state.json`) | keep-as-is |
| `cli/commands/disk_info.py` | snapshot dir disk usage | cli-glue | none | — | keep-as-is |
| `cli/commands/explore.py` | `agnes explore <table>` — count/desc/sample | cli-glue | **L37-59**: `conn.execute("SELECT table_name FROM information_schema.tables …")`, `DESCRIBE "<t>"`, `SELECT * FROM "<t>" LIMIT 5` against local **analytics** DuckDB | — (analytics views only) | keep-as-is |
| `cli/commands/init.py` | bootstrap analyst workspace | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/mark_private.py` | mark current session private | cli-glue | none | — | keep-as-is |
| `cli/commands/marketplace.py` | search/detail/add/remove marketplace items | cli-glue | none (HTTP); uses `src.marketplace_metadata` for client-side validation | — | keep-as-is |
| `cli/commands/mcp.py` | start Agnes MCP stdio server | cli-glue | none (delegates to `cli/mcp/server.py`) | — | keep-as-is |
| `cli/commands/memory_admin.py` | admin commands for corporate memory | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/my_stack.py` | view current user marketplace stack | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/onboarded.py` | self-scoped onboarded-flag toggle | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/pull.py` | Typer wrapper around `cli/lib/pull.py:run_pull` | cli-glue | none | — | keep-as-is |
| `cli/commands/push.py` | upload session jsonls + CLAUDE.local.md | cli-glue | none | — | keep-as-is |
| `cli/commands/query.py` | `agnes query` against local analytics DuckDB | cli-glue | **L90**: `conn.execute(sql).fetchmany(limit)` (user SQL passed verbatim, intentional) | — (analytics views only) | keep-as-is |
| `cli/commands/refresh_marketplace.py` | reconcile workspace plugins with user's stack | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/sample.py` | shorthand for `agnes describe -n 5` | cli-glue | none | — | keep-as-is |
| `cli/commands/schema.py` | `agnes schema <table>` — columns + BQ hints | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/self_upgrade.py` | self-upgrade flow | cli-glue | none | — | keep-as-is |
| `cli/commands/server.py` | server deploy/rollback/logs/status/backup | cli-glue | none | — | keep-as-is |
| `cli/commands/setup.py` | setup init/bootstrap/test-connection/first-sync | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/skills.py` | knowledge base for AI agents | cli-glue | none | — | keep-as-is |
| `cli/commands/snapshot.py` | snapshot list/create/refresh/drop/prune | cli-glue | **L85**: `conn.execute(f'DROP VIEW IF EXISTS "{name}"')`; **L337-338**: `CREATE OR REPLACE VIEW "{name}" AS SELECT * FROM read_parquet(…)` over local analytics DuckDB | — (analytics views only) | keep-as-is |
| `cli/commands/stack.py` | user-facing stack management | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/status.py` | workspace status | cli-glue | none | — | keep-as-is |
| `cli/commands/statusline.py` | Claude Code statusLine helper | cli-glue | none | — | keep-as-is |
| `cli/commands/store.py` | Flea Market creator-side ops | cli-glue | none (HTTP) | — | keep-as-is |
| `cli/commands/tokens.py` | `agnes auth token` — PAT mgmt | cli-glue | none (HTTP) | — | keep-as-is |

**`cli/` summary:** Every direct SQL call inside `cli/` targets the analyst's
**local analytics DuckDB** (parquet views) — never state tables. The admin
CLI files that touch state (`admin.py`, `admin_autodoc.py`,
`admin_data_semantics.py`, `admin_metrics.py`) already route through
`src.repositories`. ORM migration here = re-link those four files to the
new repos; the analyst commands need no change.

---

## `connectors/`

### `connectors/bigquery/`

| Path | Purpose | Category | SQL hotspot | State tables | Connector boundary | Verdict |
|---|---|---|---|---|---|---|
| `connectors/bigquery/__init__.py` | docstring | connector | — | — | — | keep-as-is |
| `connectors/bigquery/access.py` | per-request BQ access (DuckDB extension session, predicate pushdown, jobs API fallback) | connector | **L313-674**: `INSTALL bigquery; LOAD bigquery;`, `CREATE SECRET …`, `ATTACH … TYPE bigquery`, `SET bq_query_timeout_ms`, plus user-SQL pass-through — all session-scoped, no state writes | — | analytics (BQ session only) | keep-as-is |
| `connectors/bigquery/auth.py` | ephemeral access-token helper | connector | none | — | — | keep-as-is |
| `connectors/bigquery/extractor.py` | produces extract.duckdb with remote views | connector | **L397-768**: `CREATE TABLE _meta`, `CREATE TABLE _remote_attach`, `INSERT INTO _meta/_remote_attach`, `ATTACH bq`, `CREATE VIEW … AS SELECT * FROM bq.<dataset>.<tbl>`, plus L933-947 inside extract.duckdb. **Also imports `src.db.get_system_db` + `TableRegistryRepository` (L1081-1082)** to read partition_by / clustered_by from registry. | reads `table_registry` (via repo) | analytics (writes extract.duckdb); reads registry through repo | keep-as-is (boundary case noted) |
| `connectors/bigquery/metadata.py` | populate `TableMetadata` for BQ-backed registry | connector | none | — | — | keep-as-is |

### `connectors/internal/` — **boundary case** (serves state tables back as analytics)

| Path | Purpose | Category | SQL hotspot | State tables | Connector boundary | Verdict |
|---|---|---|---|---|---|---|
| `connectors/internal/__init__.py` | docstring | connector | — | — | — | keep-as-is |
| `connectors/internal/access.py` | per-request-scoped query engine — wraps `usage_session_summary`, `usage_events`, `audit_log` as `agnes_*` CTE views with RBAC scoping | connector | **L256**: `SELECT table_name FROM information_schema.tables`; **L336-337**: `CREATE TABLE "<src>" AS SELECT * FROM _pg_src_df` (PG-materialise into `:memory:` DuckDB); **L447, L483**: `cursor.execute(wrapped)` over CTE-wrapped user SQL | **reads `users`, `usage_events`, `usage_session_summary`, `audit_log`, `personal_access_tokens`** (via SELECT or repo) | **state → CTE-projected to analyst**; intentionally bridges state tables into the analyst query plane | hard — the CTE-wrapper / PG-materialise dance is the *one* place state is intentionally re-exposed as a query view. ORM redesign needs a parallel "scope these models per-user, project to a dataframe" pattern |
| `connectors/internal/registry.py` | seed internal-source rows in `table_registry` at startup | connector | **L43-46**: `DELETE FROM table_registry WHERE source_type='internal' AND id NOT IN (…)` (stale-row eviction); then `TableRegistryRepository.register()` for each | **writes `table_registry`** | state writer (boot seeding) | easy (one raw DELETE → repo method `delete_stale_internal_rows`) |

### `connectors/jira/`

| Path | Purpose | Category | SQL hotspot | State tables | Connector boundary | Verdict |
|---|---|---|---|---|---|---|
| `connectors/jira/__init__.py` | docstring | connector | — | — | — | keep-as-is |
| `connectors/jira/extract_init.py` | init Jira extract.duckdb with `_meta` + views | connector | **L36-67**: `DROP TABLE IF EXISTS _meta`, `CREATE TABLE _meta`, `INSERT INTO _meta`, `SELECT count(*) FROM "<t>"`, `CREATE OR REPLACE VIEW` | — | analytics (extract.duckdb only) | keep-as-is |
| `connectors/jira/file_lock.py` | advisory file locking | infra | none | — | — | keep-as-is |
| `connectors/jira/incremental_transform.py` | incremental update of issue in parquet | connector | none | — | analytics | keep-as-is |
| `connectors/jira/service.py` | Jira API service for issue data | connector | none | — | — | keep-as-is |
| `connectors/jira/transform.py` | raw Jira JSON → parquet | connector | none | — | analytics | keep-as-is |
| `connectors/jira/validation.py` | input validation | connector | none | — | — | keep-as-is |
| `connectors/jira/scripts/__init__.py` | empty | infra | — | — | — | keep-as-is |
| `connectors/jira/scripts/backfill.py` | backfill Jira data | script | none | — | analytics | keep-as-is |
| `connectors/jira/scripts/backfill_remote_links.py` | backfill remote links | script | none | — | analytics | keep-as-is |
| `connectors/jira/scripts/backfill_sla.py` | backfill SLA | script | none | — | analytics | keep-as-is |
| `connectors/jira/scripts/consistency_check.py` | parquet consistency check | script | minor (DuckDB introspection over parquet) | — | analytics | keep-as-is |
| `connectors/jira/scripts/poll_sla.py` | SLA polling | script | none | — | analytics | keep-as-is |
| `connectors/jira/tests/__init__.py` | empty | infra | — | — | — | keep-as-is |
| `connectors/jira/tests/test_file_lock.py` | unit test | infra | none | — | — | keep-as-is |
| `connectors/jira/tests/test_parquet_lock.py` | unit test | infra | none | — | — | keep-as-is |
| `connectors/jira/tests/test_sla_poll.py` | unit test | infra | none | — | — | keep-as-is |

### `connectors/keboola/`

| Path | Purpose | Category | SQL hotspot | State tables | Connector boundary | Verdict |
|---|---|---|---|---|---|---|
| `connectors/keboola/__init__.py` | docstring | connector | — | — | — | keep-as-is |
| `connectors/keboola/access.py` | DuckDB Keboola extension session facade | connector | **L40-43**: `INSTALL keboola FROM community; LOAD keboola; ATTACH '…' AS kbc (TYPE keboola, …)` | — | analytics | keep-as-is |
| `connectors/keboola/client.py` | legacy Storage API wrapper (fallback) | connector | none | — | — | keep-as-is |
| `connectors/keboola/extractor.py` | produces extract.duckdb + data/*.parquet | connector | **L63-748**: `SET memory_limit`, `SET threads`, `INSTALL keboola`, `LOAD keboola`, `ATTACH … AS kbc TYPE keboola`, `DROP TABLE IF EXISTS _meta`, `CREATE TABLE _meta`, `INSERT INTO _meta`, `INSERT INTO _remote_attach`, `COPY (SELECT * FROM kbc.…) TO …parquet`, `DETACH kbc`, `CHECKPOINT`. **Also imports `src.db.get_system_db` + `SyncStateRepository` + `TableRegistryRepository`** at L369-370, L974-975 for sync state + registry reads | reads/writes `sync_state`, reads `table_registry` (via repos) | analytics writer; uses repos for state | keep-as-is (boundary case noted) |
| `connectors/keboola/incremental.py` | incremental watermark + merge | connector | none direct (uses extract conn) | — | analytics | keep-as-is |
| `connectors/keboola/metadata.py` | populate `TableMetadata` for Keboola row | connector | none | — | — | keep-as-is |
| `connectors/keboola/parquet_io.py` | parquet I/O helpers for legacy SDK path | connector | none | — | analytics | keep-as-is |
| `connectors/keboola/partitioned.py` | partitioned sync — per-partition parquets | connector | none direct | — | analytics | keep-as-is |
| `connectors/keboola/storage_api.py` | lightweight Storage API client | connector | none | — | — | keep-as-is |
| `connectors/keboola/where_filters.py` | whereFilters parse/validate | connector | none | — | — | keep-as-is |
| `connectors/keboola/tests/__init__.py` | empty | infra | — | — | — | keep-as-is |

### `connectors/llm/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `connectors/llm/__init__.py` | docstring | connector | — | — | keep-as-is |
| `connectors/llm/anthropic_provider.py` | Anthropic structured extraction | connector | none | — | keep-as-is |
| `connectors/llm/base.py` | StructuredExtractor protocol | connector | none | — | keep-as-is |
| `connectors/llm/exceptions.py` | exception hierarchy | connector | none | — | keep-as-is |
| `connectors/llm/factory.py` | extractor factory from instance config | connector | none | — | keep-as-is |
| `connectors/llm/openai_compat.py` | OpenAI-compat provider | connector | none | — | keep-as-is |

### `connectors/mcp/`

| Path | Purpose | Category | SQL hotspot | State tables | Connector boundary | Verdict |
|---|---|---|---|---|---|---|
| `connectors/mcp/__init__.py` | docstring | connector | — | — | — | keep-as-is |
| `connectors/mcp/classifier.py` | heuristic registration-mode classifier | connector | none | — | — | keep-as-is |
| `connectors/mcp/client.py` | MCP client wrapper for inbound connector | connector | none — imports `per_user_secrets_repo, shared_secrets_repo` | reads per_user_secrets, shared_secrets via repos | repo-only state read | easy (uses repos) |
| `connectors/mcp/extractor.py` | produces extract.duckdb for materialize-mode tools | connector | **L77-112**: `DROP TABLE IF EXISTS _meta`, `CREATE TABLE _meta`, `INSERT INTO _meta`, `CREATE OR REPLACE VIEW … AS SELECT * FROM read_parquet(…)`. Imports `MCPSourceRepository`, `ToolRegistryRepository` | reads mcp_sources, tool_registry via repos | analytics writer; uses repos for state | keep-as-is |

### `connectors/openmetadata/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `connectors/openmetadata/__init__.py` | docstring | connector | — | — | keep-as-is |
| `connectors/openmetadata/client.py` | OpenMetadata REST client | connector | none | — | keep-as-is |
| `connectors/openmetadata/enricher.py` | catalog enricher | connector | none | — | keep-as-is |
| `connectors/openmetadata/transformer.py` | OM data transformer | connector | none | — | keep-as-is |

**`connectors/` summary:** Almost every SQL call is the `extract.duckdb`
contract (writing `_meta` + `_remote_attach`, attaching the source DuckDB
extension, COPYing parquet). These stay on DuckDB — they're the analytics
side. Two real boundary cases:

1. **`connectors/internal/access.py` + `registry.py`** — connectors/internal
   intentionally bridges state tables (`usage_*`, `audit_log`, `users`) into
   the analyst query plane via per-request CTE wrappers. This is the most
   architecturally interesting file for the ORM migration: today it issues
   raw SELECTs against state tables in DuckDB; under PG it materialises a
   dataframe and CREATE TABLEs it into `:memory:` DuckDB. The ORM model
   layer needs a "scope-and-project" pattern to replace the raw selects.

2. **Connector extractors that read registry/sync_state via repos**
   (`bigquery/extractor.py`, `keboola/extractor.py`, `mcp/client.py`,
   `mcp/extractor.py`) — already on `src.repositories`. Just relink.

---

## `services/`

### `services/scheduler/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/scheduler/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/scheduler/__main__.py` | replaces systemd timers — schedules sync jobs | service | **none** — uses `src.scheduler.is_table_due` (which uses repos) | sync_state, sync_history, table_registry (via repos) | easy (already on repos) |

### `services/session_pipeline/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/session_pipeline/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/session_pipeline/contract.py` | ProcessorResult contract | infra | none (imports duckdb for typing) | — | keep-as-is |
| `services/session_pipeline/lib.py` | pure parse_jsonl helpers | infra | none | — | keep-as-is |
| `services/session_pipeline/runner.py` | per-processor runner — drives a SessionProcessor across sessions | service | **L58-69**: `conn.execute("SELECT id, email FROM users WHERE id = ?", …)` and the LIKE-email fallback `SELECT id, email FROM users WHERE email LIKE ? || '@%' ESCAPE '\\\\'` | **users** (raw SELECT) | medium — replace with `UsersRepository.find_by_id`/`find_by_email_localpart`; small surface |

### `services/session_processors/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/session_processors/__init__.py` | package docstring | infra | — | — | keep-as-is |
| `services/session_processors/usage.py` | UsageProcessor entry point | service | none — delegates to `usage_lib` + `src.repositories` | usage_events, usage_session_summary, marketplace_plugins, store_entities | easy |
| `services/session_processors/usage_lib.py` | event-extraction + rollup logic | service | **HEAVY** — direct raw SQL: L281 `SELECT DISTINCT name FROM marketplace_plugins`; L292/659 `SELECT synthetic_name, type FROM store_entities WHERE visibility_status='approved'`; L597 `SELECT processed_at FROM session_processor_state …`; L618-620 `INSERT INTO session_processor_state … ON CONFLICT DO UPDATE`; L673 `DELETE FROM usage_tool_daily WHERE day >= ?`; L674-705 `INSERT INTO usage_tool_daily … SELECT … FROM usage_events`; L714 `DELETE FROM usage_marketplace_item_daily WHERE day >= ?`; L718 `INSERT INTO usage_marketplace_item_daily`; L758-784 `INSERT INTO usage_marketplace_item_window … FROM usage_events`, plus `BEGIN/COMMIT/ROLLBACK` blocks | **marketplace_plugins, store_entities, session_processor_state, usage_tool_daily, usage_marketplace_item_daily, usage_marketplace_item_window, usage_events** | **hard** — this is the single biggest raw-SQL hotspot in `services/`. Daily/window rollups use INSERT…SELECT aggregations from `usage_events`; clean ORM translation needs either (a) raw `Session.execute(text(…))` retained for the rollups, or (b) Core query construction. INSERTs/DELETEs are easy via repo. Rollup SELECT-aggregations are the genuinely hard part. |
| `services/session_processors/verification.py` | VerificationProcessor entry point | service | none — uses `src.repositories` + LLM connector | memory_items, verification_candidates, etc. (via repos) | easy |

### `services/session_collector/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/session_collector/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/session_collector/__main__.py` | entry point | service | none | — | keep-as-is |
| `services/session_collector/collector.py` | collect Claude Code session transcripts from user homes | service | none — pure file walk + logging | — (writes parquets, not state) | keep-as-is |

### `services/verification_detector/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/verification_detector/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/verification_detector/__main__.py` | CLI for ad-hoc local runs of verification processor | service | **L58-61**: `conn.execute("DELETE FROM session_processor_state WHERE processor_name = ?", [name])` | **session_processor_state** | easy — one DELETE → repo method |
| `services/verification_detector/detector.py` | LLM-side helpers for verification | service | none | — | keep-as-is |
| `services/verification_detector/duplicates.py` | duplicate-candidate detection hook | service | none (called from processor, gets conn injected) | duplicate_candidates (likely via repo) | easy |
| `services/verification_detector/prompts.py` | LLM prompt templates | service | none | — | keep-as-is |
| `services/verification_detector/schemas.py` | JSON schema for LLM structured output | service | none | — | keep-as-is |

### `services/corporate_memory/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/corporate_memory/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/corporate_memory/__main__.py` | entry point | service | none | — | keep-as-is |
| `services/corporate_memory/collector.py` | knowledge collector for corporate memory | service | none — LLM extract + repo writes (delegated) | memory_items, memory_domains (via repos) | easy |
| `services/corporate_memory/confidence.py` | confidence scoring | service | none | — | keep-as-is |
| `services/corporate_memory/contradiction.py` | contradiction detection | service | none | — | keep-as-is |
| `services/corporate_memory/entities.py` | entity resolution v1 | service | none | — | keep-as-is |
| `services/corporate_memory/prompts.py` | extraction prompts | service | none | — | keep-as-is |
| `services/corporate_memory/tagger.py` | auto topic tagging | service | none | — | keep-as-is |

### `services/slack_bot/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/slack_bot/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/slack_bot/binding.py` | Slack user ↔ Agnes user binding via 6-digit code | service | **HEAVY** — L41 (issue-log insert), L50 (audit), L62 (code insert), L83 `SELECT count(*) FROM slack_binding_issue_log WHERE slack_user_id=? AND issued_at > ?`, L91 `DELETE FROM slack_binding_codes WHERE slack_user_id=?`, L99-105 `INSERT INTO slack_binding_codes`, `INSERT INTO slack_binding_issue_log`, L140 `SELECT count(*) FROM slack_binding_redeem_log WHERE user_email=? AND attempted_at > ?`, L150 `INSERT INTO slack_binding_redeem_log`, L156 `SELECT slack_user_id, issued_at FROM slack_binding_codes WHERE code=?`, L171/181 `DELETE FROM slack_binding_codes WHERE code=?`, L184 `DELETE FROM slack_binding_redeem_log WHERE user_email=?`, L208-211 `SELECT … FROM resource_grants rg …` (RBAC join). Also imports `users_repo`, `audit_repo` | **slack_binding_codes, slack_binding_issue_log, slack_binding_redeem_log, resource_grants, users** | **hard** — purest "needs an ORM model" file in services: an entire mini-table-set (3 slack_binding_* tables) is hand-rolled with raw SQL. New `SlackBindingRepository` + ORM models. |
| `services/slack_bot/blocks.py` | pure Block Kit builders | service | none | — | keep-as-is |
| `services/slack_bot/commands.py` | slash-command dispatcher | service | none — uses `UserRepository` | users (via repo) | easy |
| `services/slack_bot/events.py` | event dispatcher | service | **L220-222**: `conn.execute("SELECT slack_user_id FROM users WHERE email = ?", …)` (otherwise uses `UserRepository`) | users | easy — one SELECT → existing `UserRepository` method |
| `services/slack_bot/identity.py` | resolve bot's own Slack user id at startup | service | none | — | keep-as-is |
| `services/slack_bot/interactivity.py` | block-kit button click routing | service | none | — | keep-as-is |
| `services/slack_bot/secrets.py` | resolve slack bot secrets — env/vault/none | service | none — uses `system_secrets_repo` | system_secrets (via repo) | easy |
| `services/slack_bot/sender.py` | outbound chat.postMessage | service | none | — | keep-as-is |
| `services/slack_bot/sigverify.py` | HMAC signing-secret verification | service | none | — | keep-as-is |
| `services/slack_bot/sink.py` | ChatManager ↔ Slack pump bridge | service | none | — | keep-as-is |
| `services/slack_bot/socket_mode_client.py` | Socket Mode inbound transport | service | none | — | keep-as-is |

### `services/telegram_bot/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/telegram_bot/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/telegram_bot/__main__.py` | entry point | service | none | — | keep-as-is |
| `services/telegram_bot/bot.py` | main entry — webhook loop | service | none (JSON-file storage) | — | keep-as-is |
| `services/telegram_bot/config.py` | env config | service | none | — | keep-as-is |
| `services/telegram_bot/dispatch.py` | shared notification dispatch to WS gateway | service | none | — | keep-as-is |
| `services/telegram_bot/runner.py` | execute user notification scripts on demand | service | none | — | keep-as-is |
| `services/telegram_bot/sender.py` | Telegram Bot API sender | service | none | — | keep-as-is |
| `services/telegram_bot/status.py` | `/status` command reporting | service | none | — | keep-as-is |
| `services/telegram_bot/storage.py` | JSON file storage (mappings + pending codes) | service | none — file-based, not DB | — | keep-as-is (note: separate from state migration) |
| `services/telegram_bot/test_report.py` | sample report chart | service | none | — | keep-as-is |

### `services/ws_gateway/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `services/ws_gateway/__init__.py` | empty | infra | — | — | keep-as-is |
| `services/ws_gateway/__main__.py` | asyncio entry | service | none | — | keep-as-is |
| `services/ws_gateway/auth.py` | JWT auth | service | none | — | keep-as-is |
| `services/ws_gateway/config.py` | config | service | none | — | keep-as-is |
| `services/ws_gateway/gateway.py` | TCP WS + Unix socket HTTP dispatch | service | none — pure messaging | — | keep-as-is |

**`services/` summary:** Most services route through `src.repositories`.
The three real raw-SQL hotspots are:

- `services/session_processors/usage_lib.py` — **hard** (daily/window
  rollup aggregations on `usage_events`)
- `services/slack_bot/binding.py` — **hard** (3 dedicated slack_binding_*
  tables, no repo today)
- `services/session_pipeline/runner.py` — **medium** (users SELECT)
- `services/verification_detector/__main__.py` — **easy** (one DELETE)
- `services/slack_bot/events.py` — **easy** (one SELECT, repo exists)

---

## `scripts/`

| Path | Purpose | Category | SQL hotspot | State tables | Verdict |
|---|---|---|---|---|---|
| `scripts/__init__.py` | empty | infra | — | — | keep-as-is |
| `scripts/backfill_marketplace_rollup.py` | one-shot backfill v45→v46 marketplace rollup | script | **L37-48**: `SELECT MIN(CAST(occurred_at AS DATE)) FROM usage_events`, `SELECT COUNT(*) FROM usage_marketplace_item_daily`, `SELECT period_label, COUNT(*) FROM usage_marketplace_item_window` | usage_events, usage_marketplace_item_daily, usage_marketplace_item_window | medium → likely **delete** (post-v46 ship) |
| `scripts/build_demo_extract.py` | generate demo extract.duckdb for image | script | **L28-76**: `SET GLOBAL TimeZone`, `CREATE TABLE orders_demo AS …`, `CREATE TABLE _meta`, `INSERT INTO _meta`, `COPY … TO '…' (FORMAT parquet)`, `CREATE TABLE "<t>" AS SELECT * FROM read_parquet(…)` | — (synthesises analytics extract.duckdb) | analytics seed | keep-as-is |
| `scripts/db_state_migrator.py` | migration subprocess orchestrator (DB backend state machine) — DuckDB↔PG copy | script | **L398-890**: many — `SELECT COUNT(*) FROM audit_log`, `SELECT id, params, params_before FROM audit_log`, `UPDATE audit_log SET … WHERE id=?`, `sa.text(insert_sql)`, `sa.text(f"SELECT COUNT(*) FROM "<t>"")` — invokes ORM-via-SA layer + DuckDB | **audit_log + every state table** (orchestrates full backend swap) | audit_log + all state | hard, but **keep-as-is** until ORM migration retires backend-swap (it'll re-tool against the new ORM models anyway) |
| `scripts/debug/probe_google_groups.py` | probe Google Cloud Identity / Admin Directory APIs | script | none | — | keep-as-is |
| `scripts/dev/mock_crm_mcp_server.py` | mock CRM MCP server for POC/dev | script | none | — | keep-as-is |
| `scripts/dev/poc_mcp_e2e.py` | E2E POC for Universal MCP | script | **L55**: `SELECT version FROM schema_version`; L125-130: `SELECT table_name, rows FROM _meta`, `SELECT id, name, country FROM listaccounts …` (the latter is dev-data, not state) | schema_version (read) | dev | keep-as-is |
| `scripts/duckdb_manager.py` | initialize/manage DuckDB views from parquet | script | **L347-441**: `CREATE OR REPLACE VIEW <t> AS SELECT * FROM read_parquet(…)`, `SHOW TABLES`, `SELECT COUNT(*) FROM <t>` | — (analytics views) | analytics | keep-as-is (legacy; may be **delete** if superseded by SyncOrchestrator) |
| `scripts/fix_description_escapes.py` | one-shot cleanup for corrupted `table_registry.description` rows | script | **L97-121**: `SELECT id, name, description FROM table_registry …`, `UPDATE table_registry SET description=? WHERE id=?` | **table_registry** | one-shot cleanup | delete (after run) |
| `scripts/generate_openapi.py` | generate OpenAPI snapshot from FastAPI app | script | none | — | keep-as-is |
| `scripts/generate_sample_data.py` | sample data generator for demo/testing | script | **L1120-1126**: `COPY (SELECT * FROM read_csv_auto(...)) …`, `SELECT count(*) FROM '<parquet>'` | — (synthesises analytics parquets) | analytics seed | keep-as-is |
| `scripts/migrate_duckdb_to_pg/__init__.py` | one-shot DuckDB → PG data migration entry | script | none directly (orchestrates `tasks.py`) | every state table | medium — retains value during ORM cutover; **delete** after PG-only world |
| `scripts/migrate_duckdb_to_pg/__main__.py` | CLI entry `python -m scripts.migrate_duckdb_to_pg` | script | none | — | medium → delete with above |
| `scripts/migrate_duckdb_to_pg/tasks.py` | per-table copy tasks (generic + JSONB-aware) | script | **L81-412**: `PRAGMA table_info('<t>')`, `SELECT column_name FROM information_schema.columns`, `sa.text(insert_sql)`, `sa.text(f"SELECT {pk} FROM {tgt}")` — generic copy + JSONB cast + SHA-256 validate | every state table (generic) | medium — **delete** after cutover; until then, lives next to ORM models |
| `scripts/migrate_json_to_duckdb.py` | one-time JSON → DuckDB | script | none directly (uses repos) | table_registry | delete (already done) |
| `scripts/migrate_metrics_to_duckdb.py` | migrate metric YAML → DuckDB metric_definitions | script | none directly (uses repos) | metric_definitions | delete (already done) |
| `scripts/migrate_parquets_to_extracts.py` | one-time move parquets → extract.duckdb layout | script | **L81-108**: `DROP TABLE IF EXISTS _meta`, `CREATE TABLE _meta`, `CREATE OR REPLACE VIEW "<t>" AS SELECT * FROM read_parquet('<p>')`, `SELECT count(*) FROM read_parquet('<p>')`, `INSERT INTO _meta VALUES (…)` | — (analytics layout migration) | analytics one-shot | delete (already done) |
| `scripts/migrate_registry_to_duckdb.py` | migrate table registry from md/JSON → DuckDB | script | none direct (uses repos) | table_registry | delete (already done) |
| `scripts/seed_corporate_memory.py` | seed synthetic corporate-memory data | script | none directly (uses repos / fixtures) | memory_items, memory_domains | easy (repo-based) |
| `scripts/seed_dummy_tables.py` | seed `table_registry` with dummy entries | script | none direct (uses repos) | table_registry | easy (repo-based) |
| `scripts/seed_e2e_user.py` | idempotent seed for e2e smoke user | script | **L54-106**: `SELECT id FROM user_groups WHERE name=?`, `UPDATE users SET password_hash=?, updated_at=? WHERE id=?` | **users, user_groups** | easy — repo methods exist; just relink |

**`scripts/` summary:** Most one-shot migration scripts (`migrate_*.py`,
`fix_description_escapes.py`) should be **deleted** once their PR ships
(or kept as historical curiosities, not migrated). `db_state_migrator.py`
and `migrate_duckdb_to_pg/` are the active backend-swap pipeline and
**stay** until the ORM-only world; they'll be re-tooled against the new
models anyway. Analytics scripts (`build_demo_extract.py`,
`generate_sample_data.py`, `duckdb_manager.py`) are extract.duckdb
producers — keep on DuckDB.

---

## 1. Category counts

| Category | Count |
|---|---|
| cli-glue (CLI command files; HTTP or repo callers) | 49 |
| connector (data source connectors) | 35 |
| service (long-running services) | 35 |
| script (one-shot / migration / utility) | 20 |
| infra (helpers, locks, hooks, empty `__init__.py`) | 58 |
| dead | 0 |
| **Total** | **197** |

(Numbers reflect the file's *primary* role; many CLI command files have
substantial admin behavior but are still client-side glue.)

---

## 2. Files outside `src/repositories/` with raw SQL hitting STATE tables

These are the ground-truth migration targets — every direct SQL call
against a state table (users, table_registry, sync_state, audit_log,
marketplace_*, store_*, usage_*, session_processor_state, slack_binding_*,
resource_grants, user_groups, etc.) outside the repository layer:

| File | State tables touched (raw SQL) |
|---|---|
| `connectors/internal/access.py` | usage_events, usage_session_summary, audit_log, users, personal_access_tokens (intentional state→analytics bridge) |
| `connectors/internal/registry.py` | table_registry (one DELETE for stale-row eviction) |
| `services/session_pipeline/runner.py` | users (SELECT) |
| `services/session_processors/usage_lib.py` | marketplace_plugins, store_entities, session_processor_state, usage_tool_daily, usage_marketplace_item_daily, usage_marketplace_item_window, usage_events |
| `services/slack_bot/binding.py` | slack_binding_codes, slack_binding_issue_log, slack_binding_redeem_log, resource_grants, users |
| `services/slack_bot/events.py` | users (one SELECT) |
| `services/verification_detector/__main__.py` | session_processor_state (one DELETE) |
| `scripts/backfill_marketplace_rollup.py` | usage_events, usage_marketplace_item_daily, usage_marketplace_item_window |
| `scripts/db_state_migrator.py` | audit_log (UPDATE for param backfill), every state table (via sa.text COUNT validations) |
| `scripts/fix_description_escapes.py` | table_registry (SELECT/UPDATE) |
| `scripts/migrate_duckdb_to_pg/tasks.py` | every state table (generic INSERT…ON CONFLICT + COUNT validate) |
| `scripts/seed_e2e_user.py` | users, user_groups |

Files **without** direct state-table SQL but reading/writing state via
`src.repositories` (already on the right track, ORM swap is mechanical):

- `cli/commands/admin.py`, `admin_autodoc.py`, `admin_data_semantics.py`, `admin_metrics.py`
- `connectors/bigquery/extractor.py`, `connectors/keboola/extractor.py`, `connectors/mcp/client.py`, `connectors/mcp/extractor.py`
- `services/scheduler/__main__.py`, `services/session_processors/usage.py`, `services/session_processors/verification.py`, `services/slack_bot/commands.py`, `services/slack_bot/secrets.py`, `services/corporate_memory/collector.py`
- `scripts/seed_corporate_memory.py`, `scripts/seed_dummy_tables.py`, `scripts/migrate_metrics_to_duckdb.py`, `scripts/migrate_registry_to_duckdb.py`, `scripts/migrate_json_to_duckdb.py`

---

## 3. Connectors that DO touch state tables (boundary cases)

The connector contract is "produce extract.duckdb for analytics, don't
touch app state". Three files break that contract on purpose:

1. **`connectors/internal/access.py`** — by design. The "internal" data
   source IS the state tables, projected per-user as analytics views.
   This file holds the most architecturally interesting state SQL in
   the connectors tree: raw `SELECT` against `usage_events` /
   `usage_session_summary` / `audit_log` / `users` /
   `personal_access_tokens`, plus a PG→DuckDB materialise step
   (`CREATE TABLE … AS SELECT * FROM _pg_src_df` at L336-337). Under
   ORM, this needs a *scope-and-project* pattern: build a SQLAlchemy
   query scoped to the user, materialise to a DataFrame, register in
   a `:memory:` DuckDB for the CTE wrapper. **Hard.**

2. **`connectors/internal/registry.py`** — boot-time seeding. Writes
   to `table_registry` (one raw DELETE for stale-row eviction + N
   repo-driven `register()` calls). The DELETE is trivially turned
   into `TableRegistryRepository.delete_stale_internal_rows()`.
   **Easy.**

3. **`connectors/bigquery/extractor.py`** and **`connectors/keboola/extractor.py`**
   — *not* a contract violation; they read `table_registry` /
   `sync_state` via repositories (not raw SQL) to know what to extract.
   Their direct SQL is all against the extract.duckdb they're
   producing. Cleanly stays on DuckDB-analytics; only the repo
   imports need a relink to the new ORM repos. **Keep-as-is.**

4. **`connectors/mcp/client.py`** and **`connectors/mcp/extractor.py`**
   — same shape as the BQ/Keboola extractors: state reads via
   `per_user_secrets_repo`, `shared_secrets_repo`, `MCPSourceRepository`,
   `ToolRegistryRepository`. **Keep-as-is.**

---

## Migration-plan callouts

The four hardest files (in order of effort):

1. **`services/session_processors/usage_lib.py`** — the rollup aggregations
   (INSERT … SELECT … FROM usage_events GROUP BY …) are the biggest single
   block of raw SQL in the four directories. Two options: keep them as
   `Session.execute(text(…))` even after ORM lands (legitimate use of raw
   SQL for analytical INSERT-SELECT), or rewrite as SA Core query
   construction. The DELETE/INSERT row-level statements migrate trivially
   to a `UsageRollupRepository`.

2. **`services/slack_bot/binding.py`** — needs three new ORM models
   (`SlackBindingCode`, `SlackBindingIssueLog`, `SlackBindingRedeemLog`)
   and a `SlackBindingRepository`. No analytical complications, just a
   table-count and CRUD volume that's currently inlined.

3. **`connectors/internal/access.py`** — the CTE-wrapper + PG-materialise
   pattern needs design work, not just a code translation. Today it
   discriminates between DuckDB (raw SELECT) and PG (`pd.DataFrame` round-trip
   into `:memory:` DuckDB); under ORM it must scope-and-project per-user
   through model queries while preserving the "no residual state on the
   shared connection" invariant.

4. **`scripts/db_state_migrator.py` + `scripts/migrate_duckdb_to_pg/tasks.py`** —
   the backend-swap pipeline itself. It will need to be re-tooled against the
   new ORM models (its current logic is "for every Base.metadata.sorted_tables
   table, copy"). Stays *operational* during the transition; gets simpler
   after.

Everything else is repo-call-site mechanical: relink `from src.repositories
import X` → `from src.repositories.orm import X` (or whatever the new module
path is) and adjust signatures.
