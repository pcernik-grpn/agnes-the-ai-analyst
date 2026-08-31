# Usage data package, per-turn token granularity, and realtime usage ingest

**Date:** 2026-08-31
**Status:** Approved design, pre-implementation (revised after code verification)
**Related:** `2026-05-12` internal tables (#278), `#333` stack unification (which
removed the "Agnes Internal" catalog card), the stale-hint cleanup PR that
precedes this work.

## Problem

Session and token-consumption data is collected but poorly governed,
inconsistently surfaced, and in one case simply broken:

1. **Governance hole.** Since #333 the internal tables (`agnes_sessions`,
   `agnes_telemetry`, `agnes_audit`) are excluded from every packaging and
   grant surface — package picker (`app/web/router.py:7330`), unpackaged tray
   (`:7158`), grant tree (`app/resource_types.py:250`), dashboard signals
   (`app/services/admin_dashboard.py:293`) — while being *implicitly* visible
   to every authenticated user (own rows only). Admins cannot control who may
   query usage data, and the tables cannot be presented as a package at all.
2. **`/me/activity` returns zero tokens for every user on every instance —
   a live bug.** Since v60 the pipeline writes the **full email** into
   `usage_session_summary.username` (`services/session_pipeline/runner.py:344`,
   `canonical_username = resolved_email or dir_name`, with the historical
   backfill at `src/db.py:_v59_to_v60`). But `_username_for_stats()`
   (`app/api/me_stats.py:50-66`) returns `user["id"]` — a UUID — and its
   docstring still asserts the column "carries the user_id value". Every
   token panel filters `WHERE username = <uuid>` against rows holding
   `alice@example.com` (`src/repositories/usage.py:760, 782, 813, 848, 879`;
   PG mirror `usage_pg.py:925`), so the match set is always empty. The
   `user_id` column the fix needs is **already written**
   (`runner.py:352`) — no backfill is required for post-v45 rows.
3. **Coarse token data.** Tokens are persisted per *session*
   (`usage_session_summary`, four token columns) and per chat *message*
   (`chat_messages.tokens_in/tokens_out` only). There is no uniform per-turn
   record across surfaces.
4. **Cache tokens are lost for chat before any export happens.**
   `app/chat/runner.py:1736-1737, 1752-1753` never reads
   `cache_read_input_tokens` / `cache_creation_input_tokens` off the SDK usage
   object, and `chat_messages` has no columns to hold them; the narrower
   export in `app/chat/session_export.py:173-177` is a symptom, not the cause.
   Fixing the export alone would be a no-op.
5. **Latency.** Uploads wait up to one 10-minute processor tick
   (`services/scheduler/__main__.py:166`) before appearing anywhere.
   (The 10-minute *collector* at `:155` is the legacy `/home/*/user/sessions/`
   scan and is not in the upload path — `POST /api/upload/sessions` writes
   straight to `/data/user_sessions/<user_id>/`.)
6. **Inconsistent read surfaces.** `/admin/telemetry`, `/admin/adoption`,
   `/me/activity` and the internal tables each resolve "which rows belong to
   this user" differently, so the same question can produce different numbers
   per surface.

## Agreed semantics (product decisions)

- **Only admins see everything**, including transcript content
  (`/admin/sessions` stays admin-only, unchanged).
- **Admins decide who may see their own usage rows**: access to the internal
  tables is granted via a data package. A user with the package sees the
  tables filtered to *their own* rows; a user without it does not see the
  tables at all. There is no "grantee sees everyone" tier.
- **No cost persistence.** Prices change; stored costs go stale. Tokens are
  stored at the finest available granularity and cost is *computed at read
  time* from a configurable price map.
- **Realtime ingest**, including the built-in chat.
- **One set of numbers.** All four read surfaces must agree.

## Design

### 1. Seeded "Agnes Usage" data package

- On app boot (next to the internal-table registration in
  `connectors/internal/registry.py`), seed a package with stable slug
  `agnes-usage` (`publisher_kind='organization'`, `created_by='system_seed'`)
  containing the internal tables. Idempotent: create only when no row with the
  slug exists; **do not resurrect a soft-deleted package** (an admin's delete
  is a decision; `POST /restore` exists).
- Remove the `source_type == 'internal'` exclusions from the four surfaces
  listed in Problem §1, so internal tables are packageable and grantable.
- **Manifest surface must be handled:** `_build_data_packages_section`
  (`app/api/sync.py:1865-1890`) iterates package member rows *directly*,
  neither filtered by `get_accessible_tables` nor by `sync_state`. Once
  internal tables are packageable they appear in
  `manifest.data_packages[].tables[]` with empty hash/size, which promotes the
  `is_internal_table` guard at `app/api/sync.py:1727` from defense-in-depth to
  load-bearing (`tests/test_pull_sync.py:861-881` shows `agnes pull` would
  otherwise try to materialize a packaged `agnes_audit`). Filter internal
  members out of the manifest section explicitly and keep the guard.

### 2. RBAC: internal tables become stack-gated (**BREAKING**)

- Drop the implicit-access carve-outs (`src/rbac.py:97-100`, `:246-252`,
  `:306-311`; `app/auth/access.py:280-284`). Internal tables resolve like any
  other table: visible iff a package containing them is in the caller's stack.
- The **row-level filter is unchanged** (`connectors/internal/access.py:145-166`):
  non-admins get their own rows, admins get the unscoped view. Package
  membership decides *visibility of the tables*, never row scope.
- **`/api/query` must be gated explicitly — it does not inherit the change.**
  `find_internal_refs(request.sql)` (`app/api/query.py:1708-1738`) is a
  text-scan short-circuit that runs **before** `get_accessible_tables` /
  `can_access_table` is consulted, and `_run_internal_query` (`:855-895`)
  performs no table-access check at all — only an `is_user_admin` call for the
  row filter. MCP (`app/api/mcp/foundation_tools.py:1098`) and the CLI proxy
  through it. Without an added check inside the `internal_refs` branch,
  dropping the carve-outs would hide the tables from `/api/v2/catalog` and
  `/api/v2/sample` (which *do* gate — `app/api/v2_sample.py:296`) while leaving
  `SELECT * FROM agnes_sessions` working for everyone: the worst of both
  states. The access check belongs in that branch, and is the single most
  important correctness point in this section.
- **Principal callers need an explicit decision, not an edit.**
  `src/rbac.py:244-252` unconditionally appends `INTERNAL_TABLES` for
  `SessionPrincipal`/`AgentPrincipal`, which route through `can_access_session()`
  (scope intersection, no `StackResolver`) — "in the caller's stack" is
  undefined for them. **Decision: principals keep today's behavior** (internal
  tables reachable, own rows only), because an agent's authority is already
  bounded by its owner's grants ∩ its scope, and removing the tables would
  break delegation without a governance gain. Document it as a deliberate
  carve-out and keep the three tests that pin it green:
  `tests/test_agent_scope_seams.py:184-193`, `tests/test_copresence_datapath.py:40-42`,
  `tests/test_query_internal_session_principal.py`.
- **Upgrade impact:** after deploying, non-admin users lose access until an
  admin grants `agnes-usage` to their group. `**BREAKING**` CHANGELOG bullet;
  `docs/RBAC.md` and `docs/observability.md` updated with the operator step.

### 3. Per-turn token records (PG-only, A3-compliant)

- New table `usage_turns` via **Alembic revision only** (no DuckDB ladder
  step): `id`, `session_file`, `session_id`, `user_id`, `surface`
  (`claude_code` | `chat` | `slack` | `telegram` | …), `turn_uuid`,
  `parent_uuid`, `model`, `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_creation_tokens`, `occurred_at`,
  `processor_version`, `extracted_at`. Unique on `(session_file, turn_uuid)`
  so writers are idempotent.
- Follow the full A3 recipe (`docs/migrations.md:187-232`), including the
  steps the first draft omitted: SQLAlchemy model in `src/models/<cluster>.py`
  + `src/models/__init__.py` import (step 1) and PG-side tests (step 8). New
  repo `src/repositories/usage_turns_pg.py`, registered PG-only in
  `_REGISTRY`; resolution on a DuckDB app-state instance raises
  `RequiresPostgresBackend` → typed `501` (`src/repositories/__init__.py:227-247`,
  handler `app/main.py:3322-3332`), with parity-sweep exemptions covered by
  `assert_pg_only_exemptions_fail_clean`.
- **Writers:**
  1. The usage session processor emits one row per assistant turn while it
     already walks `message.usage` (Claude Code jsonl uploads).
  2. **Chat: fix the capture at the source first.** Add
     `cache_read_tokens` / `cache_creation_tokens` to `chat_messages`
     (both backends — `usage`/`chat` are frozen pre-A3 pairs) and read them in
     `app/chat/runner.py:1736-1737, 1752-1753`; then write the `usage_turns`
     row synchronously at message persist for the built-in chat and the
     Slack/Telegram/Teams surfaces. Only after that does widening
     `session_export.py:173-177` mean anything.
  3. The agent-API broker keeps writing `llm_usage` (already per-call, all four
     token kinds, sole writer `app/api/broker_agent_policy.py:387`); no
     duplication into `usage_turns`.
- New internal table `agnes_turns` → `usage_turns` (filter `user_id`), member
  of the seeded package. It exists only on the Postgres backend. **Note the
  cost:** `INTERNAL_TABLES` is a module-level frozen tuple whose derived
  constants are built at import time (`connectors/internal/access.py:208-210`
  `_TABLE_REF_RE`, `:264` `_INTERNAL_ALIAS_NAMES`), so backend-conditional
  membership means making those lazy and updating every iterator
  (`src/rbac.py`, `registry.py:42`, `table_registry.prune_internal_except`).
  If that proves invasive, ship `agnes_turns` unconditionally and let the
  PG-only repo's typed `501` handle DuckDB instances.

### 4. Cost computed at read time

- A per-model price map (`pricing.models.<model>: {input, output, cache_read,
  cache_write}`, USD per MTok) in `config/instance.yaml`, with in-code defaults
  for current provider list prices. One helper module owns the lookup; the chat
  guardrail's hardcoded constants (`app/chat/manager.py:86-87`, used at `:4505`)
  migrate onto it. Dashboards render cost computed from stored tokens; nothing
  persists cost.

### 5. Realtime ingest (event-driven + sweep)

- `POST /api/upload/sessions` enqueues a single-file usage-processing job right
  after a successful write; the processor gains a "process one file" entry
  point. The 10-minute sweep stays as catch-up (legacy-collector files, missed
  events); the `session_processor_state` hash ledger makes reprocessing a
  no-op. Honest baseline: this cuts a **~10-minute** worst case to seconds.
- Built-in chat becomes realtime by construction (turns written at message
  persist). Chat session *summaries* still come from the export + processor at
  session end and via the sweep; "now" views read turns.
- Rollups keep their existing cadence; live views read raw tables.

### 6. Fix `/me/activity`, then unify identity

- **Fix the bug first, in its own commit, with a failing test:** change
  `_username_for_stats()` to stop returning `user["id"]`, and switch the four
  `usage_repo` session/token reads to filter on the **`user_id`** column
  (already populated by `runner.py:352`) rather than the display-oriented
  `username`. Also correct the stale claim at `me_stats.py:146` that the runner
  writes `session_file = f"{username}/…"` — it writes `dir_name`
  (`runner.py:302`).
- **Canonical key: `users.id`** everywhere a query means "this person's rows";
  `username` stays a display/grouping field holding the email. Legacy rows with
  `user_id IS NULL` (pre-v45, or orphaned uploads for deleted users) resolve
  via email where possible and otherwise remain admin-only.
- **Shared read model:** one module (e.g. `app/services/usage_stats.py`) owns
  the canonical queries (sessions, per-window token totals, adoption
  aggregates, cost). `/api/me/stats/*`, `/api/admin/telemetry/*`,
  `/api/admin/adoption/*` and the internal-table projections call it. `usage`
  is a **frozen pre-A3 pair**, so any new method lands in both `usage.py` and
  `usage_pg.py` with a contract test (`docs/migrations.md:243-256`).
- **Contract test:** fixture sessions (Claude Code jsonl + chat) → assert
  `/admin/telemetry`, `/admin/adoption`, `/me/activity` and a `SELECT` over the
  internal tables report identical token totals for the same user and window.
  This is the regression net for "the numbers must agree".
- Add an empty-state hint to `/me/activity` naming the server the CLI last
  pushed to, so "pushed to a different instance" stops looking like a bug.

## Non-goals

- No change to transcript access: content stays admin-only.
- No persisted cost values, no billing-grade metering (the existing
  "best-effort guardrail, not a billing ledger" stance stands).
- No retention-policy changes.
- No new DuckDB app-state repos or schema ladder steps (A3 respected); the
  `chat_messages` column addition is a frozen-pair maintenance change, which
  A3 explicitly still allows.

## Testing

- TDD throughout; contract tests parametrize both backends where a surface
  exists on both, with fail-clean `501` assertions for PG-only pieces.
- RBAC tests must use **non-admin** callers — admin god-mode short-circuits
  every check and makes visibility tests vacuous.
- Explicit test that a non-admin without the package gets denied on
  `/api/query`, `/api/v2/catalog`, `/api/v2/sample` **and** the MCP path, so
  the §2 short-circuit cannot regress silently.
- E2E: grant/revoke flow, upload→visible latency, chat cache-token capture.

## Rollout

- Sequenced so the user-visible fix is not gated on the breaking change:
  1. `/me/activity` fix (§6 first bullet) — small, independently shippable.
  2. Cache-token capture + `usage_turns` + realtime ingest.
  3. Package seeding + RBAC gating (**BREAKING**) + shared read model.
- `**BREAKING**` CHANGELOG bullet for the RBAC change; Added/Changed/Fixed
  bullets for the rest.
- Operator note in the release: grant `agnes-usage` to the intended groups
  right after upgrade.
