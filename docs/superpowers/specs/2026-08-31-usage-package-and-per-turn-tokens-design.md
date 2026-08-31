# Usage data package, per-turn token granularity, and realtime usage ingest

**Date:** 2026-08-31
**Status:** Approved design, pre-implementation
**Related:** `2026-05-12` internal tables (#278), `#333` stack unification (which
removed the "Agnes Internal" catalog card), the stale-hint cleanup PR that
precedes this work.

## Problem

Session and token-consumption data is collected but poorly governed and
inconsistently surfaced:

1. **Governance hole.** Since #333 the internal tables (`agnes_sessions`,
   `agnes_telemetry`, `agnes_audit`) are excluded from every packaging and
   grant surface (package picker, grant tree, unpackaged tray) while being
   *implicitly* visible to every authenticated user (own rows only). Admins
   cannot control who may query their own usage data, and the tables cannot
   be presented as a data package at all.
2. **Coarse token data.** Tokens are persisted per *session*
   (`usage_session_summary`) and per chat *message* (`chat_messages.tokens_in/
   tokens_out` — cache tokens are dropped by the chat→jsonl export), plus a
   per-call ledger only for agent-bound calls (`llm_usage`). There is no
   uniform per-turn record across surfaces.
3. **Latency.** The usage pipeline is polling-based (session collector and
   usage processor every 10 minutes), so dashboards lag uploads by up to
   ~20 minutes.
4. **Inconsistent read surfaces.** `/admin/telemetry`, `/admin/adoption`,
   `/me/activity` and the internal tables each resolve "which rows belong to
   this user" differently. The `username` column historically carries three
   different key spaces (user id, email local-part, OS username), so the same
   question can produce different numbers per surface — including a user's
   own activity page showing nothing.

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
- **Realtime ingest**, including the built-in chat: fresh data should appear
  in seconds, not on a 10-minute tick.
- **One set of numbers.** All four read surfaces must agree.

## Design

### 1. Seeded "Agnes Usage" data package

- On app boot (next to the existing internal-table registration in
  `connectors/internal/registry.py`), seed a data package with stable slug
  `agnes-usage` (`publisher_kind='organization'`, `created_by='system_seed'`)
  containing the internal tables. Seeding is idempotent: create only when no
  row with the slug exists; **do not resurrect a soft-deleted package** (an
  admin's delete is a decision; `POST /restore` exists).
- Remove the `source_type == 'internal'` exclusions from the package table
  picker, the "unpackaged tables" tray, the grant tree
  (`app/resource_types.py`) and the admin dashboard signals, so internal
  tables are packageable and grantable like any other registered table.

### 2. RBAC: internal tables become stack-gated (**BREAKING**)

- Drop the implicit-access carve-outs in `src/rbac.py` and
  `app/auth/access.py`. Internal tables flow through the standard resolution:
  visible iff a package containing them is in the caller's stack
  (grant → group → stack), same as every other table.
- The **row-level filter is unchanged**: non-admin callers get their own rows
  (`user_id`), admins get the unscoped view. Package membership only decides
  *whether the tables are visible at all*, never widens rows.
- Catalog (`/api/v2/catalog`), MCP foundation tools and the CLI inherit the
  change automatically because they resolve through the same helpers; the
  implementation plan must verify each surface.
- **Upgrade impact:** after deploying, non-admin users lose access to the
  internal tables until an admin grants `agnes-usage` to their group.
  CHANGELOG gets a `**BREAKING**` bullet; `docs/RBAC.md` and
  `docs/observability.md` document the new flow and the operator step.

### 3. Per-turn token records (PG-only, A3-compliant)

- New table `usage_turns` via **Alembic revision only** (no DuckDB ladder
  step): `id`, `session_file`, `session_id`, `user_id`, `surface`
  (`claude_code` | `chat` | `slack` | `telegram` | …), `turn_uuid`,
  `parent_uuid`, `model`, `input_tokens`, `output_tokens`,
  `cache_read_tokens`, `cache_creation_tokens`, `occurred_at`,
  `processor_version`, `extracted_at`. Uniqueness on
  `(session_file, turn_uuid)` makes writers idempotent.
- New Postgres-only repository `src/repositories/usage_turns_pg.py`,
  registered PG-only in `_REGISTRY`; resolving it on a DuckDB app-state
  instance raises `RequiresPostgresBackend` → typed `501` (per the A3
  recipe in `docs/migrations.md`). Parity-sweep exemptions listed with
  `assert_pg_only_exemptions_fail_clean` coverage.
- **Writers:**
  1. The usage session processor emits one row per assistant turn while it
     already walks `message.usage` (Claude Code jsonl uploads).
  2. `ChatManager` writes the turn row *synchronously at message persist*
     for the built-in chat and the Slack/Telegram/Teams surfaces — capturing
     cache tokens from the SDK usage object, which are currently discarded.
  3. The agent-API broker keeps writing `llm_usage` (already per-call, all
     four token kinds); no duplication into `usage_turns`.
- Fix `app/chat/session_export.py` to carry cache tokens in the exported
  usage block so processor-derived summaries match chat-written turns.
- New internal table `agnes_turns` → `usage_turns` (filter `user_id`),
  member of the seeded package. It is registered only when the Postgres
  backend is active; on DuckDB app-state instances the id is absent (not a
  broken row).

### 4. Cost computed at read time

- A per-model price map (`pricing.models.<model>: {input, output,
  cache_read, cache_write}` in USD per MTok) in `config/instance.yaml`, with
  in-code defaults for current provider list prices. One helper module owns
  the lookup; the chat daily-guardrail's hardcoded price constants migrate
  onto it. Dashboards (`/admin/telemetry`, `/admin/adoption`, `/me/activity`)
  render cost columns computed from stored tokens; nothing persists cost.

### 5. Realtime ingest (event-driven + sweep)

- `POST /api/upload/sessions` enqueues a single-file usage-processing job
  immediately after a successful write; the processor gains a
  `process one file` entry point. The existing 10-minute sweep stays as
  catch-up (collector-ingested files, missed events), and the
  `session_processor_state` hash ledger already makes reprocessing a no-op.
- Built-in chat is realtime by construction (turns written at message
  persist); chat session *summaries* continue to come from the export +
  processor at session end and via the sweep. "Now" views read turns.
- Rollups (7d/30d marketplace windows) keep their existing cadence; live
  views read the raw tables, so no rollup change is needed for freshness.

### 6. Identity unification and the consistency contract

- **Canonical key: `users.id`.** A one-shot backfill resolves
  `usage_session_summary` / `usage_events` rows with `user_id IS NULL`:
  `username` values that are already a user id pass through; email
  local-parts resolve via the users table; unresolvable legacy rows remain
  admin-visible but drop out of per-user views.
- The session collector and pipeline write `user_id` on every new row; the
  upload path already keys directories by user id.
- **Shared read model:** one module (e.g. `app/services/usage_stats.py`)
  owns the canonical queries (sessions, per-window token totals, adoption
  aggregates, cost). `/api/me/stats/*`, `/api/admin/telemetry/*`,
  `/api/admin/adoption/*` and the internal-table projections all call it.
- **Contract test:** fixture sessions (Claude Code jsonl + chat) →
  assert `/admin/telemetry`, `/admin/adoption`, `/me/activity` and
  `SELECT` over the internal tables report identical token totals for the
  same user and window. This test is the regression net for "the numbers
  must agree".
- Diagnose the reported "my activity page is empty" case against a live
  instance after the identity work lands; the two candidate causes (key
  drift; sessions being pushed to a different instance than the one being
  inspected) are covered by the backfill and by an explicit empty-state hint
  in `/me/activity` showing which server the CLI last pushed to.

## Non-goals

- No change to transcript access: content stays admin-only.
- No persisted cost values, no billing-grade metering (the existing
  "best-effort guardrail, not a billing ledger" stance stands).
- No retention-policy changes.
- No new DuckDB app-state schema or repos (A3 ratchet respected).

## Testing

- TDD throughout; contract tests parametrize both backends where a surface
  exists on both, with fail-clean `501` assertions for PG-only pieces.
- RBAC tests must use non-admin callers (admin god-mode makes visibility
  tests vacuous).
- E2E: grant flow (admin grants package → user sees own rows; revoke →
  tables disappear), realtime latency (upload → row visible within
  seconds), chat cache-token capture.

## Rollout

- Single PR train onto the integration branch, after the stale-hint PR.
- `**BREAKING**` CHANGELOG bullet for the RBAC change (Added/Changed/Fixed
  bullets for the package seeding, `usage_turns`, pricing map, realtime
  ingest and the identity backfill).
- Operator note in the release: grant `agnes-usage` to the intended groups
  right after upgrade.
