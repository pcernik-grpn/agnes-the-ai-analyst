# Agnes `src/` ORM-Migration Inventory

Branch: `vr/orm-migration-plan` (synced from `origin/main`).
Scope: every `*.py` under `src/`. 149 files, ~40 K lines.

Legend:
- DD = DuckDB-backed (repo or analytics)
- PG = `_pg.py` SQLAlchemy mirror
- Raw SQL count = grep of `conn.execute|cur.execute` in the file (one-line approximation; comments not excluded)
- DuckDB-specific dependency = uses extensions, ATTACH, parquet IO, FTS/BM25, or DuckDB-only SQL flavor
- Migration verdict per file

---

## Top-level `src/` (non-repo, non-models)

### Infrastructure / shared utilities

| Path | Purpose | Category | DuckDB dep | Raw SQL | Verdict |
|---|---|---|---:|---:|---|
| `src/__init__.py` | package marker, `__version__='0.1.0'` | infra | no | 0 | keep-as-is |
| `src/audit_helpers.py` | classify caller as web/cli/scheduler from auth state | infra | no | 0 | keep-as-is |
| `src/category_icons.py` | inline SVG dict for marketplace category pills | infra | no | 0 | keep-as-is |
| `src/identifier_validation.py` | regex allowlist for DuckDB identifier interpolation safety | infra | regex name = `is_safe_identifier`, contextually DuckDB; reusable for PG | 0 | keep-as-is |
| `src/sql_safe.py` | duplicate-ish identifier validator (`^[a-zA-Z_][...]{0,63}$`) for BQ extension paths | infra | comment cites DuckDB BQ ext but logic is generic | 0 | keep-as-is |
| `src/sanitize_news.py` | nh3-allowlist HTML sanitizer for admin news entity | infra | no | 0 | keep-as-is |
| `src/store_categories.py` | constant list of 9 Store categories | infra | no | 0 | keep-as-is |
| `src/store_naming.py` | `-by-<user>` suffix + archive-name helpers + entity-version hash | infra | no | 0 | keep-as-is |
| `src/connectors_manifest.py` | parse SKILL.md frontmatter from seed workspace, return install-prompt tiles | infra | no | 0 | keep-as-is |
| `src/marketplace_listing.py` | walk a plugin dir, enumerate skills/agents/commands | infra | no | 0 | keep-as-is |
| `src/marketplace_urls.py` | URL builders for served curated-marketplace assets | infra | no | 0 | keep-as-is |
| `src/marketplace_asset_validation.py` | doc/image MIME + extension allowlists shared by mirror + Store | infra | no | 0 | keep-as-is |
| `src/scheduler.py` | parse `sync_schedule` strings, decide if a table is due | infra | no | 0 | keep-as-is |
| `src/usage_ask.py` | text-to-SQL prompt + SELECT-only validator for telemetry questions | infra (LLM glue) | SQL targets DuckDB v41 `usage_*` tables — schema doc string is DuckDB-flavor | 0 | medium — when usage_pg lands, prompt schema doc string needs an engine-aware variant |

### Memory-isolation subprocess shims

| Path | Purpose | Category | DuckDB dep | Verdict |
|---|---|---|---|---|
| `src/_subprocess_runner.py` | generic subprocess job runner (JSON stdin → JSON stdout) for memory-isolated jobs | infra | indirect: motivated by DuckDB anon-arena retention | keep-as-is |
| `src/_profiler_worker.py` | entry-point for `python -m src._profiler_worker`, calls `profile_table` in fresh PID | service-glue | indirect (calls `profile_table`) | keep-as-is |

### DuckDB / Postgres engine layer

| Path | Purpose | Category | DuckDB dep | Raw SQL | Verdict |
|---|---|---|---:|---:|---|
| `src/db.py` (**5565 LOC**) | system+analytics DuckDB engines, all 70+ schema migrations (`_v1_to_v2`…`_vN_to_vN+1`), seed admin user, FTS install, BQ extension bootstrap | state-other | yes — every CREATE TABLE in this file is DuckDB SQL. ATTACHes analytics DB. Inlines `INSTALL fts` / `INSTALL bigquery` | hundreds of `conn.execute(CREATE TABLE …)` | **hard** — primary target of the migration. The DDL ladder must translate to SQLAlchemy models / Alembic; analytics-side ATTACH path stays |
| `src/db_pg.py` | SQLAlchemy `Base`, engine singleton, `get_session()`, URL resolution (`DATABASE_URL` > `AGNES_DB_URL`) | state-other | no | 0 | keep-as-is — already the destination |
| `src/db_state_machine.py` | StrEnum + transition validator for `DUCKDB → SIDE_CAR → CLOUD`, with `DUCKDB_QUACK` placeholder | state-other | no | 0 | keep-as-is |
| `src/duckdb_conn.py` | thin `_open_duckdb()` wrapper that pins session timezone to UTC | analytics | yes — `duckdb.connect(...)` | 0 | keep-as-is |

### Analytics path (stays on DuckDB)

| Path | Purpose | Category | DuckDB dep | Verdict |
|---|---|---|---|---|
| `src/orchestrator.py` (813 LOC) | scans `/data/extracts/*/extract.duckdb`, ATTACHes each, reads `_remote_attach`, INSTALL/LOAD extensions, creates master views in `analytics.duckdb`, atomic-swap rebuild | analytics | yes — `ATTACH`, `INSTALL`, `LOAD`, `CREATE OR REPLACE VIEW`, BQ extension secrets | keep-as-is |
| `src/orchestrator_security.py` | allowlist for which extensions / token env vars the orchestrator will load (M14/M15 hardening) | analytics | yes — extension allowlist | keep-as-is |
| `src/profiler.py` (1457 LOC) | YData-style profiling via DuckDB SQL (NULL %, distinct, histograms, sample rows) over the analytics DB; writes profiles.json | analytics | yes — DuckDB SQL flavor (HISTOGRAM, APPROX_COUNT_DISTINCT, sample rows) | keep-as-is |
| `src/remote_query.py` (437 LOC) | `RemoteQueryEngine`: validate SQL, BQ COUNT pre-check, fetch arrow, register as DuckDB view, execute join | analytics | yes — Arrow → `conn.register(view, table)` | keep-as-is |
| `src/fts.py` | `ensure_fts_loaded()` + `ensure_knowledge_fts_index()`: `INSTALL fts`, `LOAD fts`, `PRAGMA create_fts_index('knowledge_items', 'id', ['title','body'], strip_accents=1)` | analytics (state-adjacent) | yes — FTS extension, BM25 ranking | **hard** — knowledge search depends on this. Migration needs either Postgres `tsvector` + GIN, or a separate DuckDB FTS satellite |
| `src/rbac.py` | shim re-exporting `can_access_table` / `get_accessible_tables`; delegates to `app.auth.access` | infra | takes `duckdb.DuckDBPyConnection` arg, but body just calls `app.auth.access` | medium — change signature to accept either backend, or drop the `duckdb` import |
| `src/grant_intersection.py` | set-intersection of co-session participants' grants per ResourceType | infra | uses repository factory (`use_pg`, `users_repo`); takes a `duckdb` conn but routes through factory | medium — already factory-routed, just drop the `duckdb` typed parameter |

### Marketplace server

| Path | Purpose | Category | DuckDB dep | Verdict |
|---|---|---|---|---|
| `src/marketplace.py` (495 LOC) | nightly sync: clone or fetch+reset each `marketplace_registry` row into `${DATA_DIR}/marketplaces/<slug>`, validate, redact tokens | marketplace-server | no — git + filesystem; reads registry via repository factory | keep-as-is |
| `src/marketplace_filter.py` | resolver: user → allowed plugins across all marketplaces via `resource_grants` join | marketplace-server | uses `duckdb.DuckDBPyConnection` arg but delegates via repository factory | medium — already PG-aware; drop the DuckDB type hint |
| `src/marketplace_metadata.py` | parse `.claude-plugin/marketplace-metadata.json` (cover photos, doc links, category overrides) | marketplace-server | no | keep-as-is |
| `src/marketplace_metadata_scaffold.py` | scaffold a starter `marketplace-metadata.json` from canonical sources (GEN/HUMAN/CONFLICT merge) | marketplace-server | no | keep-as-is |
| `src/marketplace_asset_mirror.py` (750 LOC) | mirror external cover/doc URLs into `${DATA_DIR}/marketplace-cache/<slug>/` w/ SSRF guards | marketplace-server | no | keep-as-is |
| `src/initial_workspace.py` (605 LOC) | per-instance Initial Workspace Template — clone/fetch/zip; singleton | marketplace-server | no | keep-as-is |

### Templates / LLM glue

| Path | Purpose | Category | DuckDB dep | Verdict |
|---|---|---|---|---|
| `src/claude_md.py` | render analyst CLAUDE.md from admin-editable template + RBAC-filtered tables/metrics | service-glue | takes a `duckdb` conn, calls `src.rbac.get_accessible_tables` | medium — engine-agnostic via repo factory |
| `src/welcome_template.py` | render `/setup` page agent-prompt; admin-editable Jinja2 template | service-glue | takes a `duckdb` conn → `WelcomeTemplateRepository` | medium |
| `src/catalog_export.py` (464 LOC) | OpenMetadata → YAML metrics/tables; uses `TableRegistryRepository` | service-glue | takes a DuckDB conn but only via the repo factory | medium |
| `src/table_autodoc.py` | LLM auto-doc table descriptions; pure prompt builder (no DB) | infra | no | keep-as-is |
| `src/data_semantics_scaffold.py` (594 LOC) | emit starter `_brief.md` + tables/metrics YAML from `metric_definitions` + `table_registry` + `data_packages` | service-glue | no direct DB; reads through repos | keep-as-is |

---

## `src/models/` — SQLAlchemy declarative models (PG side)

Already-ORM. These DEFINE the target schema for the migration.

| Path | Tables | Migration verdict |
|---|---|---|
| `src/models/__init__.py` | aggregator: imports every model to register on `Base.metadata` for Alembic autogenerate | keep-as-is |
| `src/models/audit.py` | `AuditLog` | keep-as-is |
| `src/models/chat.py` | `ChatSession`, `ChatMessage`, `ChatSessionParticipant`, `UserWorkdir` | keep-as-is |
| `src/models/config.py` | `InstanceTemplate`, `MetricDefinition`, `PersonalAccessToken` | keep-as-is |
| `src/models/data_packages.py` | `DataPackage`, `DataPackageTable`, `DataPackageTool` | keep-as-is |
| `src/models/knowledge.py` | knowledge items + memory domains + suggestions (302 LOC, largest model file) | keep-as-is |
| `src/models/lookup.py` | template + small lookup tables (welcome, news, claude_md) | keep-as-is |
| `src/models/mcp.py` | MCP sources + tool registry + secrets vault | keep-as-is |
| `src/models/misc.py` | grab bag (setup tokens, sync settings, observability views, view_ownership, BQ metadata cache, etc.) | keep-as-is |
| `src/models/ops.py` | `TableRegistry`, `SyncState`, `SyncHistory` | keep-as-is |
| `src/models/rbac.py` | `UserGroup`, `UserGroupMember`, `ResourceGrant` (209 LOC) | keep-as-is |
| `src/models/recipes.py` | `Recipe` | keep-as-is |
| `src/models/store.py` | `StoreEntity`, `StoreSubmission`, user installs, curated subscriptions, stack subscriptions (239 LOC) | keep-as-is |
| `src/models/telemetry.py` | `usage_*` event tables (216 LOC) | keep-as-is |
| `src/models/vault.py` | `SystemSecret` | keep-as-is |

---

## `src/observability/` — instrumentation helpers

| Path | Purpose | Category | Verdict |
|---|---|---|---|
| `src/observability/__init__.py` | re-export `trace_generation` | infra | keep-as-is |
| `src/observability/llm_tracing.py` | `trace_generation` context manager → one structured `llm_generation` log record | infra | keep-as-is |

---

## `src/store_guardrails/` — Flea-market upload pipeline

| Path | Purpose | Category | DB touch | Verdict |
|---|---|---|---|---|
| `src/store_guardrails/__init__.py` | re-exports | infra | no | keep-as-is |
| `src/store_guardrails/_frontmatter.py` | YAML frontmatter parsing | infra | no | keep-as-is |
| `src/store_guardrails/bundle_meta.py` | extract bundle metadata from baked zip | infra | no | keep-as-is |
| `src/store_guardrails/content_check.py` (592 LOC) | static text scan for licensed / inappropriate content | infra | no | keep-as-is |
| `src/store_guardrails/llm_review.py` | call LLM reviewer, parse structured verdict | infra | no | keep-as-is |
| `src/store_guardrails/manifest_check.py` | validate plugin manifest shape | infra | no | keep-as-is |
| `src/store_guardrails/prompts.py` | static prompt templates | infra | no | keep-as-is |
| `src/store_guardrails/purge.py` | sweep stale Store-related rows | service-glue | 1 `conn.execute` — via repo | medium |
| `src/store_guardrails/quality_check.py` | template / structural lint | infra | no | keep-as-is |
| `src/store_guardrails/reaper.py` | reap orphan submissions | service-glue | 2 `conn.execute` — via repo | medium |
| `src/store_guardrails/runner.py` (435 LOC) | orchestrate inline + async LLM review; calls `StoreEntitiesRepository`, `StoreSubmissionsRepository`, `AuditRepository` | service-glue | repo calls only; takes a `duckdb` conn | medium |
| `src/store_guardrails/static_scan.py` | bytecode/secret regex scan | infra | no | keep-as-is |

---

## `src/repositories/` — the DD ↔ PG dual-repo cluster

37 DuckDB repos · 41 Postgres repos · 1 protocol · 1 mixins module · 1 factory.

### Factory + shared

| Path | Purpose | Category | Verdict |
|---|---|---|---|
| `src/repositories/__init__.py` | `use_pg()` switch, `_build("<key>")` factory, `<entity>_repo()` callables for every repo | infra (factory) | **keep-as-is** — survives the migration; eventually only the PG branch matters |
| `src/repositories/_orchestration_mixins.py` | filesystem (YAML / JSON) import/export bodies shared by both DD and PG repos | infra | keep-as-is |
| `src/repositories/audit_protocol.py` | `Protocol` contract; ensures DD and PG repos match | infra | keep-as-is — pattern to replicate per cluster |

### DuckDB repos (`state-repo`) with PG mirror

All take `duckdb.DuckDBPyConnection`. None use `read_parquet` / `ATTACH` / parquet IO except `knowledge.py` (FTS). Most are pure CRUD.

| Path | LOC | Raw SQL | PG mirror | DuckDB dep | Verdict |
|---|---:|---:|---|---|---|
| `access_tokens.py` | 98 | 8 | `access_tokens_pg.py` (108) — sync | none | easy |
| `audit.py` | 185 | 4 | `audit_pg.py` (232) — sync (PG version notes future tsvector upgrade) | none | easy |
| `bq_metadata_cache.py` | 156 | 5 | `bq_metadata_cache_pg.py` (117) | none | easy |
| `claude_md_template.py` | 72 | 4 | `claude_md_template_pg.py` (75) — sync | none | easy |
| `cli_auth_codes.py` | 77 | 3 | **MISSING PG MIRROR** | none | medium — write the PG sibling first; pure CRUD |
| `column_metadata.py` | 76 | 5 | `column_metadata_pg.py` (84) | none | easy |
| `data_packages.py` | 416 | 21 | `data_packages_pg.py` (470) | none — multi-table joins | medium |
| `knowledge.py` | **1335** | **49** | `knowledge_pg.py` (1027) — drift (PG lacks FTS path) | **yes** — extensive FTS/BM25 (`fts_main_knowledge_items.match_bm25(id, ?)`, `INSTALL fts`, `strip_accents=1`); 21 hits on FTS/BM25/json_extract markers | **hard** — biggest single file. FTS path has no PG equivalent yet. Either Postgres `tsvector`+GIN or a DuckDB FTS satellite for search-only |
| `marketplace_plugins.py` | 383 | 15 | `marketplace_plugins_pg.py` (319) | none | medium |
| `marketplace_registry.py` | 117 | 5 | `marketplace_registry_pg.py` (105) — sync | none | easy |
| `mcp_sources.py` | 96 | 5 | `mcp_sources_pg.py` (117) | none | easy |
| `memory_domain_suggestions.py` | 114 | 6 | `memory_domain_suggestions_pg.py` (154) | none | easy |
| `memory_domains.py` | 315 | 24 | `memory_domains_pg.py` (400) — drift, PG larger | none — joins + COUNT(*) | medium |
| `metrics.py` | 185 | 10 | `metrics_pg.py` (211) | none | easy |
| `news_template.py` | 324 | 16 | `news_template_pg.py` (266) | none | medium |
| `notifications.py` | 142 | 14 | `notifications_pg.py` (163) | none | easy |
| `observability_views.py` | 89 | 5 | `observability_views_pg.py` (86) | none | easy |
| `profiles.py` | 44 | 3 | `profiles_pg.py` (59) | none | easy |
| `recipes.py` | 161 | 8 | `recipes_pg.py` (256) | none | easy |
| `resource_grants.py` | 294 | 16 | `resource_grants_pg.py` (277) | none — set ops, JOIN on user_group_members | medium |
| `session_processor_state.py` | 136 | 3 | `session_processor_state_pg.py` (112) | none | easy |
| `setup_tokens.py` | 88 | 7 | `setup_tokens_pg.py` (104) | none | easy |
| `store_entities.py` | **703** | 20 | `store_entities_pg.py` (568) | none | medium |
| `store_submissions.py` | 498 | 15 | `store_submissions_pg.py` (367) | none | medium |
| `sync_settings.py` | 49 | 4 | `sync_settings_pg.py` (60) — sync | none | easy |
| `sync_state.py` | 142 | 9 | `sync_state_pg.py` (152) — sync | none | easy |
| `table_registry.py` | 365 | 10 | `table_registry_pg.py` (272) | none | medium |
| `tool_registry.py` | 192 | 16 | `tool_registry_pg.py` (215) | none | easy |
| `usage.py` | 568 | 27 | `usage_pg.py` (584) — sync | none — but heavy aggregate queries the LLM text-to-SQL prompts depend on (see `src/usage_ask.py`) | medium — engine-aware schema doc string in `usage_ask.py` |
| `user_curated_subscriptions.py` | 193 | 15 | `user_curated_subscriptions_pg.py` (155) | none | easy |
| `user_group_members.py` | 272 | 16 | `user_group_members_pg.py` (188) | none | medium |
| `user_groups.py` | 206 | 8 | `user_groups_pg.py` (180) | none | easy |
| `user_stack_subscriptions.py` | 97 | 7 | `user_stack_subscriptions_pg.py` (132) | none | easy |
| `user_store_installs.py` | 114 | 9 | `user_store_installs_pg.py` (92) | none | easy |
| `users.py` | 146 | 12 | `users_pg.py` (133) — sync | none | easy |
| `view_ownership.py` | 123 | 11 | `view_ownership_pg.py` (88) | none | easy |
| `welcome_template.py` | 73 | 4 | `welcome_template_pg.py` (75) — sync | none | easy |

### PG-only repos (`state-repo-pg`) with no DuckDB sibling

These broke the dual-backend contract (likely temporarily, or because the DuckDB persistence sits elsewhere, e.g. `app/chat/persistence.py` and `app/secrets_vault.py`).

| Path | LOC | Why no DD sibling |
|---|---:|---|
| `chat_messages_pg.py` | 180 | DD path lives in `app/chat/persistence.py::ChatRepository` |
| `chat_session_participants_pg.py` | 255 | same |
| `chat_sessions_pg.py` | 186 | same |
| `secrets_vault_pg.py` | 252 | DD path lives in `app/secrets_vault.py` |
| `user_workdirs_pg.py` | 78 | DD path lives in `app/chat/persistence.py::ChatRepository` |

Verdict for all five: **keep-as-is on PG side**, but the migration plan needs to fold their corresponding DD code from `app/chat/persistence.py` + `app/secrets_vault.py` into the same factory model so the orchestration symmetry of `_orchestration_mixins.py` / `audit_protocol.py` extends here too. Rated **medium**.

---

## Sync/drift signals

- 1 DuckDB repo missing a PG mirror: `cli_auth_codes.py`.
- 5 PG repos without a sibling in `src/repositories/*.py` — DD code is elsewhere (chat persistence, secrets vault).
- Largest LOC-delta drift pairs (likely places parity is stale):
  - `data_packages.py` (416) vs `_pg` (470) — PG has more methods
  - `memory_domains.py` (315) vs `_pg` (400) — PG has more methods
  - `recipes.py` (161) vs `_pg` (256) — PG has more methods
  - `knowledge.py` (1335) vs `_pg` (1027) — DD has FTS that PG doesn't mirror
  - `store_entities.py` (703) vs `_pg` (568) — DD ahead, PG playing catch-up
  - `news_template.py` (324) vs `_pg` (266) — DD ahead
- Several DD repos take a typed `duckdb.DuckDBPyConnection` but their body is pure repo-factory delegation (`src/rbac.py`, `src/grant_intersection.py`, `src/marketplace_filter.py`, `src/claude_md.py`, `src/welcome_template.py`, `src/catalog_export.py`). These are easy wins for the migration — just drop the type annotation and let the factory return either backend.

---

## Hard-stop migration blockers (the "escape hatch" list)

1. **`src/db.py` schema-version ladder** (5565 LOC). The migration needs Alembic to reach the same endpoint as `_v1_to_v74` (or whatever current top version is). Already started — `src/models/` is the destination. Per CLAUDE.md, both ladders must reach the same schema endpoint; the contract gate is `tests/test_db_schema_version.py`.
2. **`src/repositories/knowledge.py` FTS path** — DuckDB `INSTALL fts` + `fts_main_knowledge_items.match_bm25(id, ?)` has no straight Postgres equivalent. Options: (a) Postgres `tsvector` + GIN with `unaccent` (matches the `strip_accents=1` semantics for Czech-diacritics test), (b) keep a DuckDB FTS satellite indexed off the PG source of truth.
3. **`src/fts.py`** — same problem, shared helper. Either retire or rework as backend-aware shim.
4. **Analytics path** (`src/orchestrator.py`, `src/profiler.py`, `src/remote_query.py`, `src/duckdb_conn.py`, `src/db.py::get_analytics_db()`) — DuckDB stays here. This is the platform's analytics engine, not state. The migration should explicitly preserve it.
5. **`src/usage_ask.py`** — text-to-SQL prompt embeds DuckDB schema doc strings. When `usage_pg` rows are the source of truth, the prompt either keeps a DuckDB satellite for analytical queries on telemetry, or grows an engine-aware variant.

---

## Summary table — category counts

| Category | Count |
|---|---:|
| state-repo (DuckDB CRUD in `src/repositories/*.py`) | 37 |
| state-repo-pg (Postgres mirror) | 41 |
| state-other (schema authoring, engine, state machine) | 4 (`db.py`, `db_pg.py`, `db_state_machine.py`, plus repo `__init__.py` factory if counted here; `__init__.py` listed under infra) |
| analytics (DuckDB-pinned: orchestrator, profiler, FTS, remote query, conn helper, BQ ext bootstrap pieces of db.py) | 5 (`orchestrator.py`, `orchestrator_security.py`, `profiler.py`, `remote_query.py`, `duckdb_conn.py`; `fts.py` borderline state-adjacent) |
| marketplace-server | 6 (`marketplace.py`, `marketplace_filter.py`, `marketplace_metadata.py`, `marketplace_metadata_scaffold.py`, `marketplace_asset_mirror.py`, `initial_workspace.py`) |
| infra (DB-agnostic helpers, validators, sanitizers, constants) | 22 |
| cli-glue / service-glue (orchestrate other modules) | ~10 (`claude_md.py`, `welcome_template.py`, `catalog_export.py`, `table_autodoc.py`, `data_semantics_scaffold.py`, `_subprocess_runner.py`, `_profiler_worker.py`, `usage_ask.py`, `rbac.py`, `grant_intersection.py`, plus the four `store_guardrails/{purge,reaper,runner}.py` and the audit_protocol/mixins under repositories) |
| models (SQLAlchemy declarative, the migration target) | 15 (`src/models/*.py`) |
| observability | 3 (`src/observability/*.py`) |
| store-guardrails | 12 (`src/store_guardrails/*.py`) |
| dead | 0 |
| **Total .py files** | **149** |

### Verdict counts (state-side files, not analytics/infra)

| Verdict | Count | Notes |
|---|---:|---|
| easy | ~25 | pure CRUD repos, raw SQL count low, no DuckDB-only features |
| medium | ~15 | multi-table joins, drift between DD and PG, or thin shims that take a typed `duckdb` conn but route through the factory |
| hard | 3 | `src/db.py` (the schema ladder), `src/repositories/knowledge.py` (FTS/BM25), `src/fts.py` (FTS extension helper) |
| keep-as-is | ~40 | analytics, marketplace-server, infra, observability, store_guardrails statics, all `src/models/*.py` |
| delete | 0 | nothing dead under `src/` |

---

## Migration-plan-relevant observations

- The repository factory (`src/repositories/__init__.py::_build`) is already the right shape: `use_pg()` toggles between DD and PG branches per repo key. The ORM endgame is to delete the DD branch (and `<name>.py` files) and inline what's now in `<name>_pg.py` as the only implementation.
- The PG mirrors already use SQLAlchemy Core + the `src/models/*.py` declarative classes. The migration is therefore a "consolidate PG branch, retire DD branch" exercise, **not** a green-field ORM build.
- The 5 PG-only repos (chat + secrets) imply the next consolidation step is moving `app/chat/persistence.py` and `app/secrets_vault.py` DuckDB code into `src/repositories/chat_*.py` / `src/repositories/secrets_vault.py` so the factory pattern stays symmetric.
- Per CLAUDE.md dual-backend discipline, the cross-engine contract tests (`tests/db_pg/test_*_contract.py`) are the safety net: every repo method must pass on both backends until the DD branch is removed. The plan should keep those tests green at every step rather than collapsing to PG-only mid-migration.
- The analytics path (DuckDB extensions, `extract.duckdb` ATTACH, parquet views, BQ ext, FTS) is **not** part of the ORM migration and the plan should explicitly fence it off.
