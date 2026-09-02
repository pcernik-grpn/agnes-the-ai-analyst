# ORM Migration Inventory: `app/` Python Files

**Scope**: every `.py` under `app/` in the agnes-the-ai-analyst repo (branch
`vr/orm-migration-plan`, just-synced from `origin/main`).

**Goal**: classify each file for the SQLAlchemy ORM migration that replaces the
dual-repo pattern (`src/repositories/x.py` DuckDB + `x_pg.py` PG). DuckDB stays
for analytics-only (`analytics.duckdb`, `extract.duckdb`, BQ extension,
parquet views, FTS).

**Raw SQL census**: 169 hits in 30 files. Full grep output:
`/tmp/agnes-sql-hits.txt`. Inline SQL hits are listed under "Raw SQL hotspot".

Verdicts:

- `easy`     — file calls repo factories only; ORM models drop in
- `medium`   — some inline state SQL to lift into a repo / ORM call
- `hard`     — heavy raw state SQL, often joined / window'd / fanned-out
- `keep-as-is` — analytics-only path (DuckDB analytics is staying)
- `infra`     — no state SQL, no migration touch needed for this PR
- `dead`     — appears unused (none found)

---

## app/ (top level)

### `app/__init__.py`
- **Purpose**: package marker
- **Category**: `infra`
- **Raw SQL**: none
- **Tables**: none
- **Verdict**: `infra` (no-op)

### `app/main.py`
- **Purpose**: FastAPI app factory — middleware wiring, router registration,
  lifespan hooks (orchestrator rebuild on boot, scheduler bootstrap), OpenAPI
  patching, error handlers
- **Category**: `infra`
- **Raw SQL**: none
- **Tables**: none (imports repo factories indirectly via routers)
- **Verdict**: `infra` (will need to wire ORM session factory into the lifespan,
  but no SQL to lift here)

### `app/instance_config.py`
- **Purpose**: loads `config/instance.yaml`, exposes typed getters
  (`get_data_source_type`, `get_value`, BigQuery project config, etc.)
- **Category**: `infra`
- **Raw SQL**: none
- **Tables**: none
- **Verdict**: `infra`

### `app/utils.py`
- **Purpose**: data-dir helpers, parquet resolver
  (`resolve_local_parquet(table_id, source_type)`), marketplace/store dir
  helpers
- **Category**: `infra`
- **Raw SQL**: none (parquet path resolution only)
- **Tables**: none — reads filesystem only
- **Verdict**: `infra`

### `app/version.py`
- **Purpose**: `APP_VERSION`, `MIN_COMPAT_CLI_VERSION`
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/logging_config.py`
- **Purpose**: structured logging setup (JSON formatter, request-id filter)
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/markdown_render.py`
- **Purpose**: server-side markdown→HTML (welcome banners, store entity
  descriptions)
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/serialization.py`
- **Purpose**: `AgnesJSONResponse` — labels naive datetimes as UTC in API JSON
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/secrets.py`
- **Purpose**: JWT-secret / session-secret bootstrapping (generate-or-load
  to/from `${DATA_DIR}/state/*.key`)
- **Category**: `infra`
- **Raw SQL**: none
- **Tables**: none (filesystem-backed)
- **Verdict**: `infra`

### `app/secrets_vault.py`  — **RAW SQL**
- **Purpose**: Fernet-encrypted secret storage. Three repository classes
  (`SharedSecretsRepository`, `SystemSecretsRepository`,
  `PerUserSecretsRepository`) backing the `mcp_secrets`, `system_secrets`,
  `mcp_user_secrets` tables.
- **Category**: `infra` (these ARE the repositories — but they live outside
  `src/repositories/` and write state)
- **Raw SQL hotspot**:
  - L154: `INSERT INTO mcp_secrets (source_id, encrypted_value, ...) VALUES ...`
  - L170: `SELECT encrypted_value FROM mcp_secrets WHERE source_id = ?`
  - L190: `DELETE FROM mcp_secrets WHERE source_id = ?`
  - L193: `SELECT 1 FROM mcp_secrets WHERE source_id = ?`
  - L214: `INSERT INTO system_secrets (name, encrypted_value, ...)`
  - L228: `SELECT encrypted_value FROM system_secrets WHERE name = ?`
  - L248: `DELETE FROM system_secrets WHERE name = ?`
  - L251: `SELECT 1 FROM system_secrets WHERE name = ?`
  - L276: `INSERT INTO mcp_user_secrets (source_id, user_id, encrypted_value, ...)`
  - L289: `SELECT encrypted_value FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?`
  - L309: `DELETE FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?`
  - L315: `SELECT 1 FROM mcp_user_secrets WHERE source_id = ? AND user_id = ?`
  - L327: `SELECT user_id FROM mcp_user_secrets WHERE source_id = ?`
- **Tables**: `mcp_secrets`, `system_secrets`, `mcp_user_secrets`
- **Verdict**: `medium` — these are de-facto repositories in the wrong
  directory. Move into `src/repositories/secrets_*.py` (and `_pg.py`
  sibling) + ORM model. Mechanical refactor; isolated surface.

### `app/resource_types.py`  — **RAW SQL**
- **Purpose**: ResourceType registry — central enum + `list_blocks` projection
  delegates for every resource kind (TABLE, MARKETPLACE_PLUGIN, MEMORY_DOMAIN,
  DATA_PACKAGE, MCP_TOOL, RECIPE, STORE_ENTITY). Used by `/admin/access`.
- **Category**: `infra` (used by every auth-gated endpoint)
- **Raw SQL hotspot**:
  - L104: `SELECT mr.id, mr.name, ..., mp.name, mp.version, mp.category, ... FROM marketplace_registry mr LEFT JOIN marketplace_plugins mp ...`
  - L161: `SELECT id, name, ... FROM table_registry WHERE COALESCE(source_type, '') != 'internal' ORDER BY ...`
  - L199: `SELECT id, name, slug, description FROM memory_domains WHERE COALESCE(status, 'active') = 'active' ORDER BY name`
  - L240: `SELECT id, slug, name FROM data_packages WHERE COALESCE(status, 'active') = 'active' ORDER BY name`
  - L282: `SELECT id, name, description, source_id FROM mcp_tools ORDER BY name`
  - L322: `SELECT id, slug, title FROM recipes WHERE ... ORDER BY title`
  - L394: `SELECT id, name, type, ... FROM store_entities WHERE visibility_status = 'approved' ORDER BY ...`
- **Tables**: `marketplace_registry`, `marketplace_plugins`, `table_registry`,
  `memory_domains`, `data_packages`, `mcp_tools`, `recipes`, `store_entities`
- **Verdict**: `medium` — every `list_blocks` projection is a single, simple,
  read-only join. Replace each with the corresponding repo's `list_for_access_ui()`
  method (or new ORM-backed projection on the model). Hot path for /admin/access
  but mechanical.

---

## app/auth/

### `app/auth/__init__.py`
- **Purpose**: package marker
- **Category**: `infra`
- **Verdict**: `infra`

### `app/auth/_common.py`
- **Purpose**: shared auth utilities (audit hook, error helpers)
- **Category**: `auth`
- **Raw SQL**: none
- **Verdict**: `easy`

### `app/auth/access.py`  — comment refers to (now-deleted) raw SQL
- **Purpose**: authorization core — `is_user_admin`, `can_access`,
  `require_admin`, `require_resource_access`, JWT minting for sessions and
  co-sessions. Uses `UserGroupsRepository`,
  `UserGroupMembersRepository`, `ResourceGrantsRepository`, `UserRepository`
  via the repo factory.
- **Category**: `auth`
- **Raw SQL**: L201 — a docstring/comment line mentions the old `conn.execute`
  path that was already replaced by repo calls. No live SQL.
- **Tables**: `user_groups`, `user_group_members`, `resource_grants`, `users`
  (all via repos)
- **Verdict**: `easy` — pure repo consumer

### `app/auth/dependencies.py`
- **Purpose**: FastAPI dependencies — `get_current_user`, `get_optional_user`,
  `_get_db`, local-dev-mode user injection, PAT resolver delegation,
  session-principal stash. Calls `users_repo()` + `pat_resolver`.
- **Category**: `auth`
- **Raw SQL**: none
- **Tables**: `users` (via repo)
- **Verdict**: `easy`

### `app/auth/group_sync.py`
- **Purpose**: Google Workspace group fetcher (Admin SDK). Compares fetched
  list with stored memberships and writes diffs via the repos.
- **Category**: `auth`
- **Raw SQL**: none (uses `user_groups_repo()`, `user_group_members_repo()`)
- **Tables**: `user_groups`, `user_group_members` (via repos)
- **Verdict**: `easy`

### `app/auth/jwt.py`
- **Purpose**: JWT create/verify wrappers — pulls signing key via
  `secrets.get_jwt_secret()`. Pure crypto.
- **Category**: `auth`
- **Raw SQL**: none
- **Verdict**: `infra` (no state — but auth machinery)

### `app/auth/pat_resolver.py`
- **Purpose**: resolve `Authorization: Bearer <pat>` → user dict via
  `PersonalAccessTokenRepository` + `UserRepository`.
- **Category**: `auth`
- **Raw SQL**: none
- **Tables**: `personal_access_tokens`, `users` (via repos)
- **Verdict**: `easy`

### `app/auth/rate_limit.py`
- **Purpose**: in-memory token bucket (per-IP + per-endpoint), used by
  password/email auth providers.
- **Category**: `middleware`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/auth/router.py`
- **Purpose**: `/api/auth/*` endpoints — `POST /token` (PAT exchange),
  `POST /bootstrap` (admin seed), `POST /refresh-groups`. Calls repos.
- **Category**: `auth` + `route-state`
- **Raw SQL**: none
- **Tables**: `users`, `personal_access_tokens`, `user_group_members` (via repos)
- **Verdict**: `easy`

### `app/auth/scheduler_token.py`
- **Purpose**: HS256 scheduler-job token mint/verify — used to authenticate
  internal scheduler → API calls.
- **Category**: `auth`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/auth/session_principal.py`
- **Purpose**: `SessionPrincipal` dataclass — co-session caller representation
  (intersection of granted resources). No DB access.
- **Category**: `auth`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/auth/providers/__init__.py`
- **Purpose**: package marker
- **Category**: `auth`
- **Verdict**: `infra`

### `app/auth/providers/email.py`  — **RAW SQL**
- **Purpose**: magic-link auth — generate, send, consume.
- **Category**: `auth`
- **Raw SQL hotspot**:
  - L146: `UPDATE users SET reset_token = ?, reset_token_created = NULL WHERE email = ? AND reset_token = ? AND reset_token_created IS NOT NULL AND reset_token_created >= ?` — atomic compare-and-swap for token consumption
  - L161: `SELECT reset_token FROM users WHERE email = ?` — verify CAS winner
  - L173: `UPDATE users SET reset_token = NULL WHERE email = ? AND reset_token = ?` — clear marker
- **Tables**: `users` (raw); also `users_repo()` for the get_by_email at L184
- **Verdict**: `medium` — the CAS is intentional (`UPDATE ... WHERE token=? AND
  created>=?` is a race-free token consumption pattern). ORM can express this
  via `Query.update()` on a model with a where clause, or a session-level
  `bulk_update_mappings` + return-count check. Repo-method
  `UserRepository.consume_magic_link(email, token, cutoff, consume_id) -> bool`
  is the natural target.

### `app/auth/providers/google.py`
- **Purpose**: Google OAuth login + callback — uses `users_repo()` to upsert,
  `group_sync.fetch_user_groups()` to materialize Workspace memberships.
- **Category**: `auth`
- **Raw SQL**: none
- **Tables**: `users`, `user_groups`, `user_group_members` (via repos)
- **Verdict**: `easy`

### `app/auth/providers/password.py`  — **RAW SQL**
- **Purpose**: password-auth provider — login, setup, reset (CAS-protected),
  email transports. Mirrors `email.py` for the reset flow.
- **Category**: `auth`
- **Raw SQL hotspot**:
  - L392: `UPDATE users SET reset_token = ?, reset_token_created = NULL WHERE email = ? AND reset_token = ? AND reset_token_created IS NOT NULL AND reset_token_created >= ? AND active = TRUE` — same CAS pattern as email.py
  - L406: `SELECT reset_token FROM users WHERE email = ?` — verify CAS winner
- **Tables**: `users` (raw); also `users_repo()` elsewhere
- **Verdict**: `medium` — same fix as email.py — a repo-level
  `consume_reset_token(email, token, cutoff, consume_id) -> bool` covers both
  call sites.

---

## app/chat/

### `app/chat/__init__.py`
- **Purpose**: package marker
- **Category**: `infra`
- **Verdict**: `infra`

### `app/chat/audit.py`  — **RAW SQL**
- **Purpose**: writes chat events into `audit_log`.
- **Category**: `infra` (chat-side audit hook)
- **Raw SQL hotspot**:
  - L30: `INSERT INTO audit_log (id, timestamp, user_id, action, params) VALUES (?, ?, ?, ?, ?)`
- **Tables**: `audit_log`
- **Verdict**: `easy` — one INSERT, swap for `AuditRepository(conn).log(...)`
  (already exists in `src/repositories/audit.py`). Likely 5-line patch.

### `app/chat/auto_title.py`
- **Purpose**: generates short titles for chat sessions via the chat LLM.
- **Category**: `infra` (no state writes; only reads + LLM call)
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/config.py`
- **Purpose**: chat feature config — env-var driven (model id, max tokens,
  E2B template, etc.).
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/copresence_summary.py`  — **RAW SQL**
- **Purpose**: builds the SR-8 intersection seed text shown to a new
  co-session participant.
- **Category**: `route-state` (chat persistence-adjacent)
- **Raw SQL hotspot**:
  - L14: `SELECT title FROM chat_sessions WHERE id = ?`
- **Tables**: `chat_sessions`
- **Verdict**: `easy` — replace with `ChatRepository.get_session(id).title`.

### `app/chat/e2b_provider.py`
- **Purpose**: E2B sandbox provisioning / lifecycle wrapper.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/e2b_workspace_sync.py`
- **Purpose**: pulls/pushes per-user workspace files into the E2B sandbox.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/manager.py`
- **Purpose**: lifecycle manager — start/stop/list active chat tasks, owns
  the per-session `Runner` futures. Reads/writes via `ChatRepository`.
- **Category**: `route-state`
- **Raw SQL**: none (delegates to ChatRepository)
- **Tables**: `chat_sessions`, `chat_messages` (via repo)
- **Verdict**: `easy`

### `app/chat/persistence.py`  — **RAW SQL (heaviest hotspot)**
- **Purpose**: `ChatRepository` — the dual-backend chat repository. DuckDB
  path (raw SQL on `self._conn`) + Postgres path (delegates to
  `src/repositories/*_pg.py`). This file IS the dual-repo pattern in its
  ugliest form — every method has two implementations side-by-side.
- **Category**: `route-state` (it's logically a repo but lives in `app/`)
- **Raw SQL hotspot** — 31 occurrences:
  - L137: `INSERT INTO chat_sessions (id, user_email, surface, ...) VALUES ...`
  - L151: `_SESSION_SELECT + " WHERE s.id = ?"` (SELECT from chat_sessions LEFT JOIN chat_messages)
  - L169: same select template, list_sessions
  - L176: same select template, get_slack_dm_session
  - L192: same select template, get_slack_thread_session
  - L205: `UPDATE chat_sessions SET ... WHERE id = ?` (rename)
  - L222: `UPDATE chat_sessions SET archived = TRUE WHERE id = ?`
  - L235: `SELECT title FROM chat_sessions WHERE id = ?`
  - L297-302: archive cascade (`SELECT COUNT ... before/after`)
  - L311: `SELECT id FROM chat_sessions WHERE user_email = ? AND archived = TRUE` (purge all)
  - L316: `DELETE FROM chat_messages WHERE session_id = ?`
  - L324: `DELETE FROM chat_sessions WHERE id = ?`
  - L329: `DELETE FROM chat_sessions WHERE user_email = ?`
  - L369: `INSERT INTO chat_messages (id, session_id, role, content, ...) VALUES ...`
  - L391: `SELECT ... FROM chat_messages WHERE id = ?`
  - L409: `SELECT ... FROM chat_messages WHERE session_id = ?` + ordering
  - L432: `UPDATE chat_messages SET archived = TRUE WHERE ...` (ephemeral cleanup)
  - L448: `SELECT ... FROM chat_messages WHERE session_id = ? AND archived = FALSE`
  - L468: `INSERT INTO user_workdirs (user_email, workdir_path, ...) VALUES`
  - L478: same as above, workdir upsert
  - L490: `SELECT * FROM user_workdirs WHERE user_email = ?`
  - L530: `INSERT INTO chat_session_participants (session_id, user_email, role, ...) VALUES`
  - L572: `UPDATE chat_session_participants ... WHERE session_id = ? AND user_email = ?`
  - L597: `SELECT ... FROM chat_session_participants WHERE session_id = ?`
  - L626: `INSERT INTO user_workdirs ...` (CWD persistence)
  - L637: `DELETE FROM user_workdirs WHERE user_email = ?`
  - L650: `SELECT workdir_path FROM user_workdirs WHERE user_email = ?`
  - L661: `SELECT ... FROM chat_session_participants WHERE ...`
- **Tables**: `chat_sessions`, `chat_messages`, `user_workdirs`,
  `chat_session_participants`
- **Verdict**: `hard` — biggest single migration target. Today PG is already
  shimmed via `*_pg.py` siblings; consolidating into single ORM models
  collapses ~600 LOC of dual paths. This file alone is what the migration is
  designed to eliminate.

### `app/chat/provider.py`
- **Purpose**: chat LLM provider abstraction (Anthropic / OpenAI / mock).
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/readiness.py`
- **Purpose**: pre-flight check for chat surface (secrets, sandbox template,
  model config).
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/runner.py`
- **Purpose**: per-session execution loop (LLM call → tool dispatch → message
  persistence via ChatRepository).
- **Category**: `infra` / `route-state` (writes via repo)
- **Raw SQL**: none
- **Tables**: chat_messages, chat_sessions (via ChatRepository)
- **Verdict**: `easy`

### `app/chat/session_principal_guard.py`
- **Purpose**: dependency that 403s a session-principal caller on endpoints
  not allowed under intersection-principal semantics.
- **Category**: `auth`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/chat/types.py`
- **Purpose**: dataclasses (`ChatSession`, `ChatMessage`, `Surface` enum,
  participant role enum).
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra` (will become SQLAlchemy mapped classes)

### `app/chat/workdir.py`
- **Purpose**: per-user CWD resolver — calls `ChatRepository.get_workdir`,
  validates the path is inside `${DATA_DIR}/workspaces/`.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `easy`

---

## app/debug/

### `app/debug/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/debug/duckdb_panel.py`
- **Purpose**: debug-toolbar panel showing DuckDB connection stats; gated by
  AGNES_DEBUG_AUTH.
- **Category**: `infra`
- **Raw SQL**: none (introspects the connection object, no queries)
- **Verdict**: `infra`

---

## app/middleware/

### `app/middleware/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/middleware/request_id.py`
- **Purpose**: assigns `x-request-id` per request; stashes on
  `request.state` for the logging filter.
- **Category**: `middleware`
- **Raw SQL**: none
- **Verdict**: `infra`

---

## app/services/

### `app/services/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/services/stack_resolver.py`
- **Purpose**: `StackResolver` — composes `is_required` + `browse` + `stack`
  projections (required/available/subscribed) for any ResourceType. Single
  source of truth for the My-Stack page. Calls repos
  (`resource_grants_repo`, `user_stack_subscriptions_repo`, etc.).
- **Category**: `infra` (used by routes)
- **Raw SQL**: none — pure repo composition
- **Tables**: `resource_grants`, `user_stack_subscriptions`, `user_group_members`
  (all via repos)
- **Verdict**: `easy`

---

## app/web/

### `app/web/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/web/setup_instructions.py`
- **Purpose**: static Czech setup-wizard copy + a couple of helpers that
  format `instance.yaml` examples. No DB.
- **Category**: `templates-only`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/web/router.py`  — **RAW SQL (3,260 LOC, 12 hits)**
- **Purpose**: all HTML page routes. Pulls data via repos for the most part;
  drops to raw SQL for a handful of small COUNT / SELECT 1 / lookup helpers.
- **Category**: `route-state` (state-heavy) + analytics for table-detail
  schema introspection (which calls `app/api/v2_schema.py`)
- **Raw SQL hotspot** — 12 occurrences:
  - L688: `SELECT COUNT(*) FROM table_registry WHERE COALESCE(source_type, '') != 'internal'` — dashboard headline counter
  - L1114: same COUNT — catalog empty-state hint
  - L1185: `SELECT 1 FROM user_stack_subscriptions WHERE user_id = ? AND resource_type = 'data_package' AND resource_id = ?` — in-stack check on package page
  - L1316: `SELECT profile FROM table_profiles WHERE table_id = ?` — table detail page
  - L1518: `SELECT COUNT(*) FROM knowledge_items WHERE id IN (...) AND is_required = TRUE` — per-domain required count
  - L1580: `SELECT COUNT(*) FROM knowledge_items WHERE status = 'pending'` — pending banner
  - L1637: `SELECT 1 FROM user_stack_subscriptions WHERE ... resource_type = 'memory_domain' ...`
  - L1732: `SELECT COUNT(*) FROM knowledge_item_relations WHERE relation_type = 'likely_duplicate' AND resolved = FALSE` — duplicate-candidate badge
  - L2765: `SELECT id, status, version, created_at, reviewed_by_model FROM store_submissions WHERE entity_id = ? ORDER BY created_at DESC` — submission sibling list
  - L2955: `SELECT g.id, g.name, ..., m.source, m.added_at FROM user_group_members m JOIN user_groups g ON ... WHERE m.user_id = ? ORDER BY ...` — profile page memberships
  - L3033: `SELECT 1 FROM information_schema.columns WHERE table_name = 'user_groups' AND column_name = 'external_id'` — schema-introspection (DEBUG_AUTH)
  - L3038: `SELECT g.name, {external_id} FROM user_group_members m JOIN user_groups g ON ... WHERE m.user_id = ? AND m.source = 'google_sync' ORDER BY g.name` — refetch-groups dry-run
- **Tables**: `table_registry`, `user_stack_subscriptions`, `table_profiles`,
  `knowledge_items`, `knowledge_item_relations`, `store_submissions`,
  `user_group_members`, `user_groups`, `information_schema.columns`
- **Verdict**: `medium` — every hit is a small COUNT / SELECT 1 / single-row
  lookup that has an obvious repo target (most repos already expose `count(...)`
  / `exists(...)`). The `information_schema.columns` check at L3033 is a
  schema-version sniff that should become a schema-version method on
  `UserGroupsRepository`. Mechanical PR.

---

## app/marketplace_server/

### `app/marketplace_server/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/marketplace_server/git_backend.py`
- **Purpose**: dulwich git backend for `/marketplace.git/*` — composes the
  synthetic git repo (RBAC-filtered Claude Code marketplace) per caller PAT.
- **Category**: `marketplace-server`
- **Raw SQL**: none — reads marketplace metadata via repos and the curated
  files on disk
- **Tables**: `marketplace_registry`, `marketplace_plugins` (via repos)
- **Verdict**: `easy`

### `app/marketplace_server/git_router.py`
- **Purpose**: WSGI bridge for git smart-HTTP protocol on top of `git_backend`.
- **Category**: `marketplace-server`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/marketplace_server/packager.py`
- **Purpose**: builds the `marketplace.zip` payload (RBAC-filtered) plus
  per-plugin tarballs.
- **Category**: `marketplace-server`
- **Raw SQL**: none — uses `marketplace_filter` + repos
- **Tables**: `marketplace_registry`, `marketplace_plugins`, `resource_grants`
  (via repos)
- **Verdict**: `easy`

### `app/marketplace_server/router.py`
- **Purpose**: `/marketplace/info` (RBAC-filtered marketplace summary) +
  `/marketplace.zip` (served zip).
- **Category**: `marketplace-server`
- **Raw SQL**: none
- **Verdict**: `easy`

---

## app/api/   (state-only routes)

### `app/api/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/api/_metadata_models.py`
- **Purpose**: Pydantic models for table metadata payloads (column docs,
  partition hints, etc.) shared by metadata.py + admin.py.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/api/access.py`  — **RAW SQL**
- **Purpose**: `/api/access/*` — groups, members, grants CRUD. Calls repos
  (`UserGroupsRepository`, `UserGroupMembersRepository`,
  `ResourceGrantsRepository`).
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L476: `DELETE FROM user_group_members WHERE group_id = ?` — cascade on group delete
  - L479: `DELETE FROM resource_grants WHERE group_id = ?` — cascade on group delete
  - L778/795/797: `BEGIN/COMMIT/ROLLBACK` — transaction wrapper around grant-requirement update + subscription fanout
  - L785: `INSERT INTO user_stack_subscriptions (user_id, resource_type, resource_id) SELECT m.user_id, ?, ? FROM user_group_members m WHERE m.group_id = ? ON CONFLICT DO NOTHING` — required→available downgrade fanout
- **Tables**: `user_group_members`, `resource_grants`, `user_stack_subscriptions`
- **Verdict**: `medium` — three patterns to lift:
  - cascade delete: `user_group_members_repo().delete_for_group(id)`,
    `resource_grants_repo().delete_for_group(id)` — repos already exist
  - transaction-wrapped fanout: SQLAlchemy session.begin() + a repo method
    `user_stack_subscriptions_repo().fanout_grant_to_group_members(...)` with
    ON CONFLICT DO NOTHING (PG: ON CONFLICT, DuckDB: same syntax — same SQL works)

### `app/api/activity.py`  — **RAW SQL**
- **Purpose**: `/api/activity/*` — audit-log timeline, sync activity, health
  pulse card. Used by /admin/activity.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L120: `SELECT id, email, name FROM users WHERE id IN ({placeholders})` — batch-enrich timeline rows with user labels
  - L194: `SELECT MAX(timestamp) FROM audit_log WHERE action LIKE 'run_%' OR action='marketplace.sync_all'` — scheduler freshness
  - L214: `SELECT status, COUNT(*) FROM sync_history WHERE synced_at >= ? GROUP BY status` — 24h sync ok/fail
  - L233: `SELECT COUNT(DISTINCT user_id) FROM audit_log WHERE timestamp >= ? AND user_id IS NOT NULL` — active users today
  - L239: `SELECT MAX(processed_at), SUM(items_extracted) FROM session_processor_state WHERE processor_name='verification' AND processed_at >= ?` — memory pipeline freshness
- **Tables**: `users`, `audit_log`, `sync_history`, `session_processor_state`
- **Verdict**: `medium` — every hit is a small aggregate. Five new repo
  methods: `UserRepository.list_by_ids(ids)`,
  `AuditRepository.latest_timestamp(action_in=...)`,
  `SyncHistoryRepository.status_counts_since(ts)`,
  `AuditRepository.distinct_users_since(ts)`,
  `SessionProcessorStateRepository.last_run_for_processor(name, since)`.

### `app/api/admin.py`  — **RAW SQL (4,688 LOC, 4 hits)**
- **Purpose**: monster admin router — table registry CRUD, server-config,
  scheduler runners (session collector / processor / corporate-memory /
  Jira-SLA / Jira-consistency / blocked-purge / reap-stuck-reviews), store
  submission moderation, BigQuery/Keboola test endpoints. The bulk of
  endpoints already call repo factories.
- **Category**: `route-state`
- **Raw SQL hotspot** — only 4 hits in 4,688 LOC:
  - L2316: `SELECT table_id, CASE WHEN status='error' AND error IS NOT NULL ... ELSE NULL END AS err, last_sync FROM sync_state` — batched sync_state read on /registry
  - L3244: `DELETE FROM sync_state WHERE table_id = ?` — cascade on table unregister
  - L3245: `DELETE FROM sync_history WHERE table_id = ?` — cascade on table unregister
  - L4534: `DELETE FROM store_submissions WHERE id = ?` — admin delete store submission
- **Tables**: `sync_state`, `sync_history`, `store_submissions`
- **Verdict**: `medium` — four small lifts:
  - `SyncStateRepository.list_with_errors_and_timestamps()` — for L2316
  - `SyncStateRepository.delete_for_table(name)` — for L3244
  - `SyncHistoryRepository.delete_for_table(name)` — for L3245
  - `StoreSubmissionsRepository.delete(id)` — already exists; just swap the call

### `app/api/admin_bigquery_test.py`
- **Purpose**: BQ connection test endpoint — uses BqAccess; no state writes.
- **Category**: `route-analytics`
- **Raw SQL**: none
- **Verdict**: `keep-as-is`

### `app/api/admin_chat.py`
- **Purpose**: admin chat ops — list active tasks, kill, secrets test,
  debug.
- **Category**: `route-state`
- **Raw SQL**: none — uses ChatRepository + secrets_vault
- **Verdict**: `easy`

### `app/api/admin_keboola_test.py`
- **Purpose**: Keboola connector connectivity test.
- **Category**: `infra` (test endpoint, no app-state)
- **Raw SQL**: none
- **Verdict**: `keep-as-is`

### `app/api/admin_mcp.py`
- **Purpose**: MCP source / tool CRUD (`/api/admin/mcp-sources/*`,
  `/api/admin/mcp-tools/*`) — uses `mcp_sources_repo()`,
  `mcp_tools_repo()` extensively (50 references).
- **Category**: `route-state`
- **Raw SQL**: none
- **Tables**: `mcp_sources`, `mcp_tools` (via repos)
- **Verdict**: `easy`

### `app/api/admin_sessions.py`
- **Purpose**: admin-facing session listing (per-user, per-host).
- **Category**: `route-state`
- **Raw SQL**: none — uses chat repo
- **Verdict**: `easy`

### `app/api/admin_slack_secrets.py`
- **Purpose**: admin endpoints for storing/testing Slack bot secrets.
- **Category**: `route-state`
- **Raw SQL**: none — uses `SystemSecretsRepository`
- **Verdict**: `easy`

### `app/api/admin_usage.py`  — **RAW SQL (8 hits)**
- **Purpose**: usage-events export (CSV/NDJSON/parquet streaming), LLM
  text-to-SQL `/api/admin/usage/ask`, reprocess, prune. Internal `usage_*`
  tables live in the same DuckDB system DB as state but are conceptually a
  small analytics warehouse.
- **Category**: `route-state` (boundary case — `usage_events` is state-shaped
  but read like an analytics table)
- **Raw SQL hotspot**:
  - L96: `SELECT COUNT(*) FROM usage_events WHERE ...` — pre-export row count
  - L127, L150, L177: dynamic `SELECT * FROM usage_events WHERE ...` (CSV / NDJSON / parquet COPY streams)
  - L286: text-to-SQL execution — validated_sql is LLM output, gated by where_validator
  - L368-388: BEGIN/DELETE-RETURNING/COMMIT block for `/reprocess`
    - DELETE FROM session_processor_state WHERE processor_name IN ('usage','marketplace_rollup_30d')
    - DELETE FROM usage_events RETURNING 1
    - DELETE FROM usage_session_summary RETURNING 1
    - DELETE FROM usage_tool_daily RETURNING 1
    - DELETE FROM usage_marketplace_item_daily RETURNING 1
    - DELETE FROM usage_marketplace_item_window RETURNING 1
  - L429-434: `SELECT COUNT ... before/after` + `DELETE FROM usage_events WHERE occurred_at < CURRENT_DATE - INTERVAL (?) DAY` (prune)
- **Tables**: `usage_events`, `usage_session_summary`, `usage_tool_daily`,
  `usage_marketplace_item_daily`, `usage_marketplace_item_window`,
  `session_processor_state`
- **Verdict**: `hard` — dynamic SQL builder + COPY-to-parquet + LLM-generated
  SQL execution. The streaming export and parquet COPY rely on DuckDB-specific
  features (`COPY (...) TO ... (FORMAT PARQUET)`). Migration plan: keep the
  text-to-SQL surface on DuckDB (read-only, analytical), lift the DELETE
  ladders into repos (`UsageEventsRepository.reprocess()`,
  `UsageEventsRepository.prune(retention_days)`). Mixed verdict — the export
  paths are arguably analytics, the reprocess/prune are state ops.

### `app/api/admin_usage_summary.py`
- **Purpose**: usage summary KPIs / facets / query — used by /admin/telemetry.
  Builds payloads via repo helpers (`usage_repo()`).
- **Category**: `route-state` (reads usage_* tables via repo)
- **Raw SQL**: none — fully repo-mediated
- **Verdict**: `easy`

### `app/api/admin_user_sessions.py`  — **RAW SQL**
- **Purpose**: admin view of per-user sessions (combines parquet jsonl
  presence + processed-session rows from `usage_session_summary`).
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L105: `SELECT session_file, session_id, started_at, ..., primary_model FROM usage_session_summary WHERE user_id = ? OR username = ? ORDER BY started_at DESC NULLS LAST`
  - L360: `SELECT id, email FROM users WHERE id = ?` — lookup target user
  - L382: `SELECT COUNT(*) FROM audit_log WHERE user_id = ?` — admin audit count
- **Tables**: `usage_session_summary`, `users`, `audit_log`
- **Verdict**: `medium` — three repo methods exist or are easy to add:
  `UsageRepository.list_user_sessions(user_id, username)`,
  `UserRepository.get_by_id(id)`,
  `AuditRepository.count_for_user(user_id)`.

### `app/api/bq_metadata_refresh.py`
- **Purpose**: refresh BigQuery remote-table metadata caches; scheduler job.
- **Category**: `route-analytics`
- **Raw SQL**: none — talks to BQ + writes `bq_metadata_cache` via repo
- **Verdict**: `keep-as-is`

### `app/api/cache_warmup.py`
- **Purpose**: startup warm of catalog / schema caches.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/api/catalog.py`
- **Purpose**: legacy catalog endpoints (`/api/catalog/profile/*`,
  `/api/catalog/tables`, `/api/catalog/metrics/*` — deprecated).
- **Category**: `route-analytics` (reads `table_registry` + parquet profiles)
- **Raw SQL**: none — uses repos
- **Tables**: `table_registry`, `table_profiles` (via repos)
- **Verdict**: `keep-as-is` (analytics catalog) — though `table_registry` is
  state; the routes are read-only and call repos so they survive untouched.

### `app/api/chat.py`
- **Purpose**: `/api/chat/*` — start, list, get, send-message, archive,
  rename, delete. All via `ChatRepository` + `ChatManager`.
- **Category**: `route-state`
- **Raw SQL**: none
- **Verdict**: `easy`

### `app/api/chat_copresence.py`  — **RAW SQL**
- **Purpose**: `/api/chat/copresence/*` — invite participant, accept/decline.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L109: `SELECT id FROM users WHERE email = ?` — resolve invitee
- **Tables**: `users`
- **Verdict**: `easy` — single lookup → `users_repo().get_by_email(...)`.

### `app/api/claude_md.py`
- **Purpose**: serve / update analyst-side `CLAUDE.local.md` payloads.
- **Category**: `route-state`
- **Raw SQL**: none — uses `user_claude_md_repo()`
- **Verdict**: `easy`

### `app/api/cli_artifacts.py`
- **Purpose**: file-upload bridge for `agnes push` (artifacts under the
  user's `${DATA_DIR}/user_artifacts/`).
- **Category**: `route-state`
- **Raw SQL**: none — filesystem + audit
- **Verdict**: `easy`

### `app/api/cli_auth.py`
- **Purpose**: CLI login flow (browser handoff → PAT).
- **Category**: `auth`
- **Raw SQL**: none — uses PAT + users repos
- **Verdict**: `easy`

### `app/api/connectors.py`
- **Purpose**: `/api/connectors/*` — list and probe registered connector
  modules.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/api/cowork_bundle.py`
- **Purpose**: builds the cowork-mode bundle ZIP (claude.md + skills + mcp +
  setup script) for a participant. 1,515 LOC of mostly string templating.
- **Category**: `route-state`
- **Raw SQL**: none — uses repos for setup-token tracking, marketplace
  content via `marketplace_filter`
- **Tables**: `setup_tokens`, `audit_log` (via repos)
- **Verdict**: `easy`

### `app/api/data.py`
- **Purpose**: `/api/data/{table_id}/download` — streams the parquet file
  from `/data/extracts/.../data/{table_id}.parquet`.
- **Category**: `route-analytics`
- **Raw SQL**: none — filesystem path resolution + audit
- **Verdict**: `keep-as-is`

### `app/api/data_packages.py`
- **Purpose**: `/api/data-packages/*` — data-package CRUD (admin) + browse /
  detail (analyst).
- **Category**: `route-state`
- **Raw SQL**: none — uses `data_packages_repo()`
- **Tables**: `data_packages`, `data_package_tables` (via repos)
- **Verdict**: `easy`

### `app/api/db_state.py`
- **Purpose**: `/api/admin/db-state/*` — Postgres migration job orchestration
  for the dual-backend state machine (idle / migrating_to_pg / dual_write /
  pg_only). Validates remote PG URLs, blocks unsafe IPs, manages async jobs.
- **Category**: `route-state` (controls the state machine the migration
  itself depends on)
- **Raw SQL**: none — uses `db_state_machine` module and filesystem job files
- **Verdict**: `easy` (but central to the ORM migration plan — keep in mind
  for the migration story)

### `app/api/health.py`  — **RAW SQL**
- **Purpose**: `/api/health` + `/api/health/detailed` + `/api/version` +
  `/api/debug/throw`. Detailed health probes the database directly.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L180: `SELECT MAX(processed_at) FROM session_processor_state WHERE processor_name = ?` — verification freshness
  - L238: `SELECT session_file FROM session_processor_state WHERE processor_name = ?` — FIFO stuck-file check
  - L319: `SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1` — schema-version probe
  - L365: `SELECT 1` — DuckDB liveness ping
  - L410: `SELECT COUNT(*) FROM users` — user count for health card
- **Tables**: `session_processor_state`, `schema_version`, `users`
- **Verdict**: `medium` — small lifts. `SessionProcessorStateRepository`
  needs a `latest_for_processor(name)` + `list_files_for_processor(name)`;
  `SchemaVersionRepository.current()` (probably exists); `UserRepository.count()`
  (likely exists). The bare `SELECT 1` ping stays — that's a connection
  liveness check, not a domain query.

### `app/api/initial_workspace.py`
- **Purpose**: `/api/admin/initial-workspace/*` + analyst-facing
  `/api/initial-workspace.zip` — manages the per-instance initial-workspace
  template materialized into `${DATA_DIR}/initial_workspace_rendered/`.
- **Category**: `route-state`
- **Raw SQL**: none — reads/writes `instance_config` rows via the repo +
  filesystem
- **Verdict**: `easy`

### `app/api/jira_webhooks.py`
- **Purpose**: Jira webhook receiver — incremental row updates for the Jira
  extract.duckdb + parquet files.
- **Category**: `route-analytics`
- **Raw SQL**: none — delegates to `connectors/jira/incremental_transform.py`
- **Verdict**: `keep-as-is`

### `app/api/marketplace.py`  — **RAW SQL (2,900 LOC, 12 hits)**
- **Purpose**: `/api/marketplace/*` — items listing (curated + flea fused
  cards), categories, detail pages, asset/doc/mirrored fetches. Analytics
  rolls on top of `usage_marketplace_item_*` tables.
- **Category**: `route-state` (heavy reads of state + telemetry rollups)
- **Raw SQL hotspot**:
  - L412: `SELECT period_label, name, invocations, distinct_users FROM usage_marketplace_item_window WHERE period_label IN ('last_30d','last_7d') AND source = ? AND {type_filter}` — 30d+7d window stats
  - L424: `SELECT name, SUM(CASE WHEN day >= CURRENT_DATE - INTERVAL 7 DAY THEN count ELSE 0 END), SUM(... 14..7 ...) FROM usage_marketplace_item_daily WHERE ...` — trend calc
  - L484: per-plugin daily-series 30-day fill
  - L533: per-curated-plugin window stat row
  - L549: per-curated-plugin trend
  - L569: per-curated-plugin daily series
  - L614: inner-item stats by plugin
  - L624: inner-item trends
  - L922: `SELECT marketplace_id, plugin_name, COUNT(DISTINCT user_id) FROM user_plugin_optouts GROUP BY ...` — subscriber counts
  - L1274: dynamic `SELECT COALESCE(NULLIF(TRIM(category),''),'Other') AS cat, COUNT(*) FROM store_entities ... GROUP BY cat` — flea category counts
  - L1536: `SELECT name, email FROM users WHERE id = ?` — owner display
  - L1559: `SELECT id, name, email FROM users WHERE id IN (...)` — batch owner display
- **Tables**: `usage_marketplace_item_window`, `usage_marketplace_item_daily`,
  `user_plugin_optouts`, `store_entities`, `users`
- **Verdict**: `hard` — the usage_marketplace_* aggregates are analytical
  (rolled up by the scheduler from `usage_events`). The cleanest separation is:
  - Lift owner-display queries (L1536, L1559) → `UserRepository.batch_display(ids)` → `easy`
  - Lift subscriber count (L922) → `UserPluginOptoutsRepository.subscriber_counts()` → `easy`
  - Lift flea category count (L1274) → `StoreEntitiesRepository.count_by_category(...)` → `easy`
  - Keep window/trend/daily-series SQL on DuckDB as analytics queries (these
    are derived rollup tables, semi-analytics). New
    `UsageMarketplaceRollupsRepository` that wraps them OR — and this is the
    interesting call — promote `usage_marketplace_item_*` to "analytics-only"
    and read directly via DuckDB. Decide during migration design.

### `app/api/marketplaces.py`  — **RAW SQL**
- **Purpose**: `/api/admin/marketplaces/*` — marketplace registry CRUD,
  sync, mark/unmark plugin as system.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L437: `DELETE FROM marketplace_plugins WHERE marketplace_id = ?` — cascade on marketplace delete
  - L441: `DELETE FROM resource_grants WHERE resource_type = ? AND starts_with(resource_id, ? || '/')` — RBAC cascade
  - L578: `SELECT 1 FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?` — existence check before mark-system
  - L589: `UPDATE marketplace_plugins SET is_system = TRUE WHERE marketplace_id = ? AND name = ?`
  - L605: `SELECT id FROM user_groups` — fanout target list
  - L657: same existence check for unmark
  - L664: `UPDATE marketplace_plugins SET is_system = FALSE WHERE ...`
- **Tables**: `marketplace_plugins`, `resource_grants`, `user_groups`
- **Verdict**: `medium` — repo gaps:
  - `MarketplacePluginsRepository.delete_for_marketplace(id)`
  - `MarketplacePluginsRepository.exists(marketplace_id, name)`
  - `MarketplacePluginsRepository.set_system(marketplace_id, name, value)`
  - `ResourceGrantsRepository.delete_by_resource_prefix(rtype, prefix)` — note the `starts_with(...||'/')` is to avoid LIKE wildcard collisions on slugs containing `_`
  - `UserGroupsRepository.list_ids()` (likely exists)

### `app/api/mcp/__init__.py`
- **Purpose**: package marker
- **Verdict**: `infra`

### `app/api/mcp/tools_generator.py`
- **Purpose**: synthesizes MCP tool manifests from registered tables (one
  per-table query tool).
- **Category**: `route-state`
- **Raw SQL**: none — uses `TableRegistryRepository`
- **Verdict**: `easy`

### `app/api/mcp_http.py`
- **Purpose**: HTTP transport for the Agnes MCP server.
- **Category**: `route-state`
- **Raw SQL**: none
- **Verdict**: `easy`

### `app/api/mcp_passthrough.py`
- **Purpose**: server-side proxy for upstream MCP servers (HTTP + SSE).
- **Category**: `route-state`
- **Raw SQL**: none — uses `mcp_sources_repo()`
- **Verdict**: `easy`

### `app/api/mcp_per_table.py`  — **RAW SQL (analytics)**
- **Purpose**: `/api/mcp/table/{table_id}` — constrained filter+limit query
  against a registered table. RBAC + table-view query via `analytics_conn`.
- **Category**: `route-analytics` (queries `analytics.duckdb` views; the
  registry read is via `TableRegistryRepository`)
- **Raw SQL hotspot**:
  - L69: `DESCRIBE "{table_view_name}"` — column lookup against analytics view
  - L134: dynamic `SELECT … FROM "{view}" WHERE … LIMIT ?` against analytics_conn — the actual query path
- **Tables**: analytics views (not state)
- **Verdict**: `keep-as-is` — this is analytics by design.

### `app/api/mcp_policy.py`
- **Purpose**: per-tool RBAC policy resolution (uses MCP_TOOL ResourceType).
- **Category**: `auth`
- **Raw SQL**: none
- **Verdict**: `easy`

### `app/api/mcp_user_secrets.py`
- **Purpose**: per-user MCP secret CRUD via `PerUserSecretsRepository`.
- **Category**: `route-state`
- **Raw SQL**: none here (the repo holds the SQL — see `app/secrets_vault.py`)
- **Verdict**: `easy`

### `app/api/me.py`  — **RAW SQL**
- **Purpose**: `/api/me/onboarded`, `/api/me/home-stats`. Home-stats joins
  multiple usage-rollup tables.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L139: a CTE over `usage_session_summary`, `usage_events`, `users` joining sessions+prompts+token counts+last_pull_at in one shot
- **Tables**: `usage_session_summary`, `usage_events`, `users`
- **Verdict**: `medium` — the CTE is too compound to drop into the existing
  per-table repos cleanly. Target: `UsageRepository.home_stats(uid, username,
  window)` that returns a structured dict. The repo would carry the SQL with
  bound params identical to today.

### `app/api/me_debug.py`  — **RAW SQL**
- **Purpose**: `/api/me/debug` — decoded JWT, group-sync snapshot, etc.
  Debug-gated.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L95: `SELECT … FROM users WHERE id = ?` (read full user row for debug)
- **Tables**: `users`
- **Verdict**: `easy` — `UserRepository.get_by_id(id)` already exists.

### `app/api/me_stats.py`  — **RAW SQL (7 hits)**
- **Purpose**: `/api/me/sessions`, `/api/me/tokens`, `/api/me/queries`,
  `/api/me/sync` — per-user usage panels.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L109: `SELECT session_file, session_id, started_at, … FROM usage_session_summary WHERE username = ? ORDER BY started_at DESC NULLS LAST`
  - L229: same shape, /api/me/queries panel (`primary_model`, `tool_calls`, etc.)
  - L279: `SELECT day, input, output, cache_read, cache_creation FROM ... GROUP BY day` — daily tokens series
  - L309: `SELECT primary_model, SUM(...) ... GROUP BY primary_model` — by-model token rollup
  - L340: `SELECT session_file, ... FROM usage_session_summary ORDER BY ... LIMIT` — top sessions
  - L369: `SELECT SUM(...) ... FROM usage_session_summary WHERE username = ?` — period totals
  - L465: `SELECT last_pull_at FROM users WHERE id = ?` — sync card
- **Tables**: `usage_session_summary`, `usage_events`, `users`
- **Verdict**: `medium` — same prescription as `me.py`. Centralize behind
  `UsageRepository.user_sessions/user_tokens_daily/user_top_sessions/user_totals`
  and `UserRepository.get_last_pull_at(uid)`.

### `app/api/memory.py`  — **RAW SQL (8 hits, 1,736 LOC)**
- **Purpose**: corporate-memory item CRUD + admin governance + per-user votes
  + audit. Already routes 90% of writes through `KnowledgeRepository` and
  `AuditRepository`, but has a few read holdouts.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L111: `SELECT g.name FROM user_group_members m JOIN user_groups g ON m.group_id = g.id WHERE m.user_id = ?` — caller's groups (audience)
  - L148: `SELECT DISTINCT rg.resource_id FROM resource_grants rg JOIN user_group_members m ON m.group_id = rg.group_id WHERE m.user_id = ? AND rg.resource_type = 'memory_domain'` — granted domains
  - L357: `SELECT item_id FROM knowledge_votes WHERE user_id = ? AND vote > 0` — upvoted-by-user filter
  - L444: comment-only (states the conversion to repo was made for some calls)
  - L567: `SELECT item_id, vote FROM knowledge_votes WHERE user_id = ?` — all of user's votes
  - L956: `SELECT * FROM audit_log WHERE action IN (?, ?) ORDER BY timestamp DESC LIMIT ? OFFSET ?` — KM-audit filter
  - L963: `SELECT * FROM audit_log WHERE action LIKE 'corporate_memory.%' OR action LIKE 'km_%' ORDER BY timestamp DESC LIMIT ? OFFSET ?` — KM-audit unfiltered
  - L1256: `SELECT id, slug FROM memory_domains WHERE id IN ({placeholders})` — domain id→slug resolution
- **Tables**: `user_group_members`, `user_groups`, `resource_grants`,
  `knowledge_votes`, `audit_log`, `memory_domains`
- **Verdict**: `medium` — every hit has an obvious repo target. Most
  notable: `KnowledgeVotesRepository.upvoted_item_ids(user_id)` and
  `MemoryDomainsRepository.id_slug_map(ids)`. KM-audit filter:
  `AuditRepository.list_for_actions(actions, page, per_page)` with prefix
  match.

### `app/api/memory_domain_suggestions.py`
- **Purpose**: end-user "suggest a domain" + admin approve/reject.
- **Category**: `route-state`
- **Raw SQL**: none — uses `memory_domain_suggestions_repo()`
- **Verdict**: `easy`

### `app/api/memory_domains.py`
- **Purpose**: admin CRUD for memory domains.
- **Category**: `route-state`
- **Raw SQL**: none — uses `memory_domains_repo()`
- **Verdict**: `easy`

### `app/api/metadata.py`
- **Purpose**: `/api/admin/metadata/{table_id}` — column docs CRUD + push
  to source (Keboola/BQ).
- **Category**: `route-state`
- **Raw SQL**: none — uses table-registry repo
- **Verdict**: `easy`

### `app/api/metrics.py`
- **Purpose**: `/api/metrics`, `/api/metrics/{id}` — metric_definitions
  read-only.
- **Category**: `route-state`
- **Raw SQL**: none — uses `metric_definitions_repo()`
- **Verdict**: `easy`

### `app/api/my_stack.py`  — **RAW SQL**
- **Purpose**: `/api/my-stack` — composes the My Stack response (curated +
  store + system pinned).
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L129: `SELECT marketplace_id, name FROM marketplace_plugins WHERE is_system = TRUE` — system-plugin set for the entire response
  - L230: `SELECT is_system FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?` — system check before unsubscribe
- **Tables**: `marketplace_plugins`
- **Verdict**: `easy` — both reads → `MarketplacePluginsRepository.list_system()`,
  `MarketplacePluginsRepository.is_system(marketplace_id, name)`.

### `app/api/news.py`
- **Purpose**: `/api/news/*` — news-card CRUD.
- **Category**: `route-state`
- **Raw SQL**: none — uses `news_repo()`
- **Verdict**: `easy`

### `app/api/observability.py`  — **RAW SQL (8 hits)**
- **Purpose**: `/api/observability/*` — audit-log facets, KPIs, saved views
  CRUD. Powers /admin/activity.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L70: `SELECT a.user_id AS id, COALESCE(u.email, a.user_id) AS label, COUNT(*) FROM audit_log a LEFT JOIN users u ON u.id = a.user_id WHERE a.timestamp >= ? AND a.user_id IS NOT NULL GROUP BY ... LIMIT 50` — users facet
  - L83: `SELECT action AS label, COUNT(*) FROM audit_log WHERE timestamp >= ? AND action IS NOT NULL GROUP BY action ORDER BY n DESC LIMIT 50` — actions facet
  - L92: same shape for `result` facet
  - L101: same shape for `resource` facet
  - L112: dynamic `CASE WHEN client_kind IS NOT NULL AND client_kind != '' THEN client_kind WHEN action IN (...) THEN 'scheduler' WHEN user_id IS NULL THEN 'system' ELSE 'other' END AS src ... GROUP BY src` — sources facet
  - L151: `SELECT COUNT(*) FROM audit_log WHERE timestamp >= ?` — events total
  - L154: `SELECT COUNT(DISTINCT user_id) FROM audit_log WHERE timestamp >= ? AND user_id IS NOT NULL` — active users
  - L159: error count
  - L165: `SELECT CAST(approx_quantile(duration_ms, 0.95) AS INTEGER) FROM audit_log ...` — p95 latency
  - L224: `SELECT COUNT(*) FROM user_observability_views WHERE user_id = ?` — view-count cap
  - L228: `SELECT 1 FROM user_observability_views WHERE user_id = ? AND name = ?` — overwrite-detect
- **Tables**: `audit_log`, `users`, `user_observability_views`
- **Verdict**: `medium` — facet builder is repetitive enough that a
  generic `AuditRepository.facet(column, since, limit, label_join=...)` works
  for L70/L83/L92/L101. The sources CASE is bespoke and worth its own method
  `AuditRepository.source_facet(since, scheduler_actions)`. The `approx_quantile`
  is a DuckDB-native function — on PG it would be `percentile_cont(0.95) WITHIN
  GROUP (ORDER BY duration_ms)`. Worth noting: cross-engine equivalence needs
  a SQLAlchemy DDL-flavor switch or repo-method polymorphism.

### `app/api/query.py`
- **Purpose**: `POST /api/query` — analyst SQL execution against analytics +
  remote BQ; rewrites BQ table refs, applies quotas, validates predicates.
- **Category**: `route-analytics`
- **Raw SQL**: none — composes analyst SQL but doesn't write state-style SQL
  itself
- **Verdict**: `keep-as-is`

### `app/api/query_hybrid.py`
- **Purpose**: server-side hybrid BQ + local DuckDB query.
- **Category**: `route-analytics`
- **Raw SQL**: none here (composes analyst SQL only)
- **Verdict**: `keep-as-is`

### `app/api/recipes.py`
- **Purpose**: recipe CRUD + browse.
- **Category**: `route-state`
- **Raw SQL**: none — uses `recipes_repo()`
- **Verdict**: `easy`

### `app/api/scripts.py`
- **Purpose**: scheduler-script CRUD (`scheduled_scripts` table) + manual
  trigger.
- **Category**: `route-state`
- **Raw SQL**: none — uses `scheduled_scripts_repo()`
- **Verdict**: `easy`

### `app/api/settings.py`
- **Purpose**: `/api/admin/server-config/*` — KV settings stored in
  `server_config` table.
- **Category**: `route-state`
- **Raw SQL**: none — uses `server_config_repo()`
- **Verdict**: `easy`

### `app/api/slack.py`
- **Purpose**: Slack-bot webhook routes + slash command dispatch.
- **Category**: `route-state`
- **Raw SQL**: none — uses repos
- **Verdict**: `easy`

### `app/api/stack.py`
- **Purpose**: `/api/stack/subscribe`, `/api/stack/unsubscribe` — generic
  resource-stack subscription.
- **Category**: `route-state`
- **Raw SQL**: none — uses `user_stack_subscriptions_repo()`
- **Verdict**: `easy`

### `app/api/stack_views.py`
- **Purpose**: `/api/stack/views` — saved stack views (filters / sorts).
- **Category**: `route-state`
- **Raw SQL**: none
- **Verdict**: `easy`

### `app/api/store.py`  — **RAW SQL (3,196 LOC, 4 hits)**
- **Purpose**: store-flea CRUD — submission preview / create / update /
  install / bundle / archive. Almost entirely repo-mediated.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L139: dynamic `SELECT id FROM store_entities WHERE synthetic_name = ? [AND id != ?] [AND visibility_status != 'archived']` — synthetic-name collision check
  - L2545: `UPDATE store_entities SET visibility_status = 'approved', name = ?, archived_at = NULL, archived_by = NULL, updated_at = ? WHERE id = ?` — revert archive after on-disk rename failure
  - L2694: `SELECT id, email FROM users WHERE id IN ({placeholders})` — bulk owner-email map
- **Tables**: `store_entities`, `users`
- **Verdict**: `easy` — three repo additions:
  - `StoreEntitiesRepository.exists_by_synthetic_name(name, exclude_id, exclude_archived)`
  - `StoreEntitiesRepository.revert_archive(id, original_name)`
  - `UserRepository.batch_email_map(ids)` (probably already exists)

### `app/api/sync.py`  — **RAW SQL (1,408 LOC, 1 hit)**
- **Purpose**: `/api/sync/manifest`, `/api/sync/trigger`, `/api/sync/status`,
  table-subscription settings.
- **Category**: `route-state`
- **Raw SQL hotspot**:
  - L1075: `UPDATE users SET last_pull_at = current_timestamp WHERE id = ?` — stamp on manifest fetch
- **Tables**: `users`
- **Verdict**: `easy` — `UserRepository.stamp_last_pull_at(uid)` (one line).

### `app/api/telegram.py`
- **Purpose**: Telegram-bot webhook routes.
- **Category**: `route-state`
- **Raw SQL**: none
- **Verdict**: `easy`

### `app/api/tokens.py`
- **Purpose**: `/api/tokens/*` — PAT CRUD for the caller + admin tokens.
- **Category**: `route-state`
- **Raw SQL**: none — uses `personal_access_tokens_repo()`
- **Verdict**: `easy`

### `app/api/upload.py`
- **Purpose**: legacy `/api/upload/*` (sessions, artifacts, local-md).
- **Category**: `route-state`
- **Raw SQL**: none — filesystem + audit
- **Verdict**: `easy`

### `app/api/uploads.py`
- **Purpose**: cover-image upload (store flea).
- **Category**: `route-state`
- **Raw SQL**: none — filesystem only
- **Verdict**: `easy`

### `app/api/users.py`
- **Purpose**: `/api/users/*` — admin user CRUD + delete cascade.
- **Category**: `route-state`
- **Raw SQL**: none — uses `users_repo()` + `ChatRepository` for cascade
- **Verdict**: `easy`

### `app/api/v2_arrow.py`
- **Purpose**: Arrow IPC encode/decode helpers used by `/api/v2/scan`.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/api/v2_cache.py`
- **Purpose**: tiny LRU+TTL cache class for v2 catalog payloads.
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/api/v2_catalog.py`
- **Purpose**: `/api/v2/catalog` — analyst-side rich catalog.
- **Category**: `route-state` (reads table registry, RBAC) + analytics
  metadata
- **Raw SQL**: none — uses repos
- **Verdict**: `easy`

### `app/api/v2_marketplace.py`
- **Purpose**: `/api/v2/skills` — per-user resolved skill list (curated +
  flea + system) for CLI clients.
- **Category**: `route-state`
- **Raw SQL**: none — uses marketplace repos
- **Verdict**: `easy`

### `app/api/v2_quota.py`
- **Purpose**: in-memory per-user quota tracker for `/api/v2/scan` (daily
  bytes + concurrent scans).
- **Category**: `infra`
- **Raw SQL**: none
- **Verdict**: `infra`

### `app/api/v2_sample.py`  — **RAW SQL (analytics)**
- **Purpose**: `/api/v2/sample/{table_id}` — N-row sample from a registered
  table (local parquet or remote BQ via DuckDB BQ extension).
- **Category**: `route-analytics`
- **Raw SQL hotspot**:
  - L79: `SELECT * FROM bigquery_query(?, ?)` — DuckDB BQ extension call. Pure analytics.
- **Verdict**: `keep-as-is`

### `app/api/v2_scan.py`  — **RAW SQL (analytics)**
- **Purpose**: `/api/v2/scan` — full table scan with predicate pushdown,
  Arrow IPC streaming.
- **Category**: `route-analytics`
- **Raw SQL hotspot**:
  - L330: `SELECT * FROM bigquery_query(?, ?)` — same as v2_sample
- **Verdict**: `keep-as-is`

### `app/api/v2_schema.py`  — **RAW SQL (analytics)**
- **Purpose**: `/api/v2/schema/{table_id}` — column list for a registered
  table (BQ INFORMATION_SCHEMA for remote, DESCRIBE parquet for local).
- **Category**: `route-analytics`
- **Raw SQL hotspot**:
  - L199: `DESCRIBE SELECT * FROM read_parquet(?)` — parquet column probe in a fresh in-memory DuckDB
- **Verdict**: `keep-as-is`

### `app/api/welcome.py`
- **Purpose**: `/api/admin/welcome-template` CRUD + preview.
- **Category**: `route-state`
- **Raw SQL**: none — uses `welcome_template_repo()`
- **Verdict**: `easy`

### `app/api/where_validator.py`
- **Purpose**: SQL `WHERE` predicate AST validator (`sqlglot` based) used by
  `/api/v2/scan` and `/admin/usage/ask`.
- **Category**: `infra`
- **Raw SQL**: none — AST inspector only
- **Verdict**: `infra`

---

## app/initial_workspace_default/

### `app/initial_workspace_default/.claude/hooks/pre_tool_use.py`
- **Purpose**: bundled hook template shipped to analyst workspaces (NOT
  loaded by the server). It's data, not code, for /api/initial-workspace.zip.
- **Category**: `infra` (data file shipped to clients)
- **Raw SQL**: none
- **Verdict**: `infra` (do not touch in ORM migration)

---

# Summary

## 1. File count per category

| Category | Count |
|---|---|
| `route-state` (writes/reads state via routes) | 54 |
| `route-analytics` (parquet / BQ / analytics views) | 10 |
| `auth` (auth providers, dependencies, group sync) | 12 |
| `marketplace-server` (`/marketplace.git`, `/marketplace.zip`) | 4 |
| `middleware` | 3 |
| `templates-only` | 1 |
| `infra` (main.py, utils.py, config, models, helpers, __init__) | 41 |
| `dead` | 0 |

(Total: 125 .py files under `app/`. Some files double-count — e.g. `auth/router.py` is both `auth` and `route-state`; counted under the primary category.)

## 2. Files with raw SQL outside `src/repositories/` (the fact-check)

30 files, 169 SQL hits total. Sorted by hit count:

| File | Hits | Verdict |
|---|---|---|
| `app/chat/persistence.py` | 31 | hard — biggest target; dual-backend repo in wrong location |
| `app/secrets_vault.py` | 13 | medium — three repo classes living in `app/` |
| `app/web/router.py` | 12 | medium — small COUNT/SELECT 1 helpers |
| `app/api/marketplace.py` | 12 | hard — owner display + rollup analytics mix |
| `app/api/observability.py` | 11 | medium — facet builder; `approx_quantile` cross-engine concern |
| `app/api/admin_usage.py` | 8 | hard — export streaming + reprocess + text-to-SQL |
| `app/api/memory.py` | 8 | medium — read holdouts |
| `app/api/access.py` | 7 | medium — cascade deletes + transactional fanout |
| `app/api/resource_types.py` | 7 | medium — projection delegates for /admin/access |
| `app/api/marketplaces.py` | 7 | medium — system-flag fanout |
| `app/api/me_stats.py` | 7 | medium — per-user usage panels |
| `app/api/activity.py` | 5 | medium — health pulse aggregates |
| `app/api/health.py` | 5 | medium — schema-version + session-processor reads |
| `app/api/store.py` | 4 | easy — synthetic-name collision + revert + email map |
| `app/api/admin.py` | 4 | medium — sync_state + sync_history + submission delete |
| `app/api/observability.py` (counted above) | — | — |
| `app/api/admin_user_sessions.py` | 3 | medium — user-session list + audit count |
| `app/auth/providers/email.py` | 3 | medium — CAS token consumption |
| `app/auth/providers/password.py` | 2 | medium — same CAS pattern |
| `app/api/me.py` | 1 | medium — home_stats CTE |
| `app/api/chat_copresence.py` | 1 | easy — single user lookup |
| `app/api/my_stack.py` | 2 | easy — system-plugin reads |
| `app/api/sync.py` | 1 | easy — last_pull_at stamp |
| `app/api/v2_sample.py` | 1 | keep-as-is — BQ extension |
| `app/api/v2_scan.py` | 1 | keep-as-is — BQ extension |
| `app/api/v2_schema.py` | 1 | keep-as-is — DESCRIBE parquet |
| `app/api/mcp_per_table.py` | 2 | keep-as-is — analytics views |
| `app/api/me_debug.py` | 1 | easy — debug `users.get_by_id` |
| `app/chat/audit.py` | 1 | easy — `AuditRepository.log` swap |
| `app/chat/copresence_summary.py` | 1 | easy — single title read |
| `app/auth/access.py` | 0 (line 201 is a comment) | easy |

## 3. Files that touch BOTH analytics and state (boundary cases)

These files mix `analytics.duckdb` / `extract.duckdb` / parquet / BQ reads
with state-table reads. Each needs a verdict per concern at migration time.

- **`app/api/admin_usage.py`** — `usage_events` is logically state-shaped
  (transactional inserts from session processor) but read as a small
  analytical warehouse. Export uses DuckDB `COPY TO ... (FORMAT PARQUET)`
  which has no native PG analog. **Recommendation**: keep `usage_*` tables
  on DuckDB even after the ORM migration, or migrate to PG with
  `COPY usage_events TO STDOUT (FORMAT csv/binary)` + Python parquet write.

- **`app/api/marketplace.py`** — owner-display + subscriber counts are
  state (cleanly liftable); `usage_marketplace_item_window` and
  `usage_marketplace_item_daily` are rollup tables derived from
  `usage_events` by the scheduler. **Recommendation**: keep rollup tables
  in DuckDB analytics; lift only the owner / subscriber / category COUNT
  queries.

- **`app/api/me.py` / `app/api/me_stats.py`** — read both
  `usage_session_summary` (rollup) and `users` (state). The CTE at me.py:139
  joins them. **Recommendation**: either keep the rollup-state join on
  DuckDB and have `UsageRepository` accept a `user_id` + return a structured
  dict, or split the join so the rollup read happens on DuckDB and the
  `last_pull_at` comes from the PG users row.

- **`app/api/health.py`** — `session_processor_state` is state; the FIFO
  stuck-file check reads filesystem and then state.

- **`app/web/router.py`** (table-detail page) — pulls
  `table_profiles` (state) but the analytics-side schema fallback calls
  `app/api/v2_schema.py` (analytics). Already cleanly separated by function
  call.

- **`app/api/v2_schema.py`** — uses `_open_duckdb(":memory:")` for the
  parquet DESCRIBE; this is analytics-only, no boundary concern despite
  the `conn.execute` hit.

- **`app/resource_types.py`** — every projection reads STATE tables. No
  analytics boundary; safe to lift fully into repos.

---

# Migration order recommendation (out of scope but visible from this inventory)

1. **Quick wins (`easy`)** — ~30 files where one or two repo calls replace
   the only SQL hit. Land first; clears the trivial noise.
   Examples: `app/chat/audit.py`, `app/chat/copresence_summary.py`,
   `app/api/sync.py`, `app/api/chat_copresence.py`, `app/api/me_debug.py`,
   `app/api/my_stack.py`, `app/api/store.py`.

2. **CAS + transactional patterns (`medium`)** — `app/auth/providers/{email,password}.py`
   (consume_token), `app/api/access.py` (grant downgrade fanout),
   `app/api/marketplaces.py` (mark-system fanout). Need careful repo method
   design for cross-engine semantics.

3. **Facet + aggregate routes (`medium`)** —
   `app/api/observability.py`, `app/api/activity.py`,
   `app/api/admin_user_sessions.py`, `app/api/me.py`, `app/api/me_stats.py`,
   `app/api/health.py`, `app/api/admin.py`, `app/resource_types.py`,
   `app/api/memory.py`, `app/web/router.py`. New repo aggregate methods
   per cluster.

4. **The big two (`hard`)** —
   - `app/chat/persistence.py` — collapse the dual-backend repo into a
     single ORM-mapped one. Already has a PG sibling, so the contract is
     well-understood.
   - `app/api/admin_usage.py` + `app/api/marketplace.py` — decide
     state-vs-analytics line for `usage_*` tables. If they stay on DuckDB:
     no migration. If they move to PG: design the `COPY ... TO parquet`
     replacement.

5. **Leave alone (`keep-as-is`)** —
   `app/api/data.py`, `app/api/query.py`, `app/api/query_hybrid.py`,
   `app/api/v2_arrow.py`, `app/api/v2_cache.py`, `app/api/v2_sample.py`,
   `app/api/v2_scan.py`, `app/api/v2_schema.py`, `app/api/mcp_per_table.py`,
   `app/api/catalog.py`, `app/api/jira_webhooks.py`,
   `app/api/bq_metadata_refresh.py`, `app/api/admin_bigquery_test.py`,
   `app/api/admin_keboola_test.py` — all touch analytics.duckdb /
   extract.duckdb / BQ extension. DuckDB stays here.

---

**Generated**: 2026-06-04 against `vr/install-prompt-seed-unif` worktree
(branch `vr/orm-migration-plan` per task spec).
**Branch state**: clean. No files modified during this inventory.
