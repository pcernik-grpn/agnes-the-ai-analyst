---
name: agnes-orchestrator
description: Rules for the SyncOrchestrator, the extract.duckdb ATTACH flow, query_mode semantics (local / remote / materialized), and when to call rebuild() vs rebuild_source(). Use when editing src/orchestrator.py, src/db.py, or anything that produces extract.duckdb in connectors/.
---

# Agnes orchestrator

Source of truth for orchestrator invariants. See `CLAUDE.md § Architecture`
and `docs/architecture.md` for the canonical description.

## ATTACH flow

`SyncOrchestrator.rebuild()` scans `/data/extracts/*/extract.duckdb`,
ATTACHes each into the master `analytics.duckdb`, creates views like
`<source>."<bucket>"."<table>"`, and updates `sync_state`.

Per-source rebuild is `rebuild_source(name)` — used after Jira webhooks where
only one source changed. Full `rebuild()` is the fallback when scope is
unclear.

## Thread safety

All write paths take `self._rebuild_lock` (a `threading.Lock`). New write
paths — anything that DETACHes / re-ATTACHes / updates `sync_state` — MUST
hold the lock. Read paths must not hold it.

## query_mode

Every table has a `query_mode` in its `_meta` row:

- `local` — batch-pulled to parquet, queried locally. Parquets live under
  `/data/extracts/<source>/data/`. Synced via `agnes pull`.
- `remote` — queried against the upstream (e.g., BigQuery) at query time.
  No parquet on disk. Requires a `_remote_attach` row in `extract.duckdb`.
- `materialized` — admin-registered SQL run by the scheduler. Result lands as
  a parquet under `/data/extracts/<source>/data/`. Distributed like `local`.

## `_remote_attach` mechanism

For `query_mode='remote'` tables, the extractor writes a `_remote_attach`
table in `extract.duckdb` with columns:

| column | meaning |
|---|---|
| `alias` | name used in the ATTACH statement |
| `extension` | DuckDB extension to install + load |
| `url` | upstream connection URL |
| `token_env` | env var holding the auth token (`''` if extension-specific auth, e.g., BigQuery's GCE metadata server) |

At query time the orchestrator installs/loads the extension, resolves the
token, creates a session-scoped SECRET when required, and ATTACHes the
source so views like `kbc."bucket"."table"` resolve.

## Master DB locations

- System DB: `${DATA_DIR}/state/system.duckdb` (sync_state, table_registry, users, RBAC).
- Analytics DB: `${DATA_DIR}/analytics/server.duckdb` (master views).

## Schema migrations

`src/db.py` auto-migrates from `v1 → vN` on startup. Per-version notes live
in `CHANGELOG.md`. Adding a schema version means:

1. Bumping the version constant in `src/db.py`.
2. Adding the `vN-1 → vN` migration step.
3. Adding a CHANGELOG bullet that names the version.
4. Updating documentation that references the schema version (search for
   "schema v" in `docs/` + `CLAUDE.md`).

## Files NOT to modify

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
