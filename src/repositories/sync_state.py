"""Repository for sync state and history."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, List, Dict

import duckdb


def _deserialize_parts(d: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize the ``parts`` field to ``list[dict] | None``.

    DuckDB returns a JSON column as a *string*; PG JSONB returns an already
    parsed object. Both repos route reads through this so the cross-engine
    contract yields identical Python values.
    """
    if d is None:
        return None
    v = d.get("parts")
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("utf-8")
    if isinstance(v, str):
        d["parts"] = json.loads(v) if v else None
    return d


class SyncStateRepository:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def _row_to_dict(self, row) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        columns = [desc[0] for desc in self.conn.description]
        return _deserialize_parts(dict(zip(columns, row)))

    def _rows_to_dicts(self, rows) -> List[Dict[str, Any]]:
        if not rows:
            return []
        columns = [desc[0] for desc in self.conn.description]
        return [_deserialize_parts(dict(zip(columns, row))) for row in rows]

    def get_table_state(self, table_id: str) -> Optional[Dict[str, Any]]:
        result = self.conn.execute("SELECT * FROM sync_state WHERE table_id = ?", [table_id]).fetchone()
        return self._row_to_dict(result)

    def get_last_sync(self, table_id: str) -> Optional[datetime]:
        result = self.conn.execute("SELECT last_sync FROM sync_state WHERE table_id = ?", [table_id]).fetchone()
        return result[0] if result else None

    def get_all_states(self) -> List[Dict[str, Any]]:
        results = self.conn.execute("SELECT * FROM sync_state ORDER BY table_id").fetchall()
        return self._rows_to_dicts(results)

    def update_sync(
        self,
        table_id: str,
        rows: int,
        file_size_bytes: int,
        hash: str,
        uncompressed_size_bytes: int = 0,
        columns: int = 0,
        status: str = "ok",
        error: Optional[str] = None,
        duration_ms: Optional[int] = None,
        bump_last_sync: bool = True,
        parts: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Upsert the row's sync bookkeeping.

        ``bump_last_sync=False`` records fresh rows/hash/status without
        touching ``last_sync`` (NULL on a first-ever write) — used by the
        filesystem-fallback publish for materialized rows, whose schedule
        gate reads ``last_sync`` and must stay open for same-day retries.

        ``parts`` is the per-partition manifest for a partitioned table — a
        list of ``{path, hash, size_bytes}``. ``None`` (the default) means a
        single-file table and writes SQL NULL, so the manifest/pull keep
        treating it as single-file (backward compatible).
        """
        now = datetime.now(timezone.utc) if bump_last_sync else None
        parts_json = json.dumps(parts) if parts is not None else None
        self.conn.execute(
            """INSERT INTO sync_state (table_id, last_sync, rows, file_size_bytes,
                uncompressed_size_bytes, columns, hash, parts, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (table_id) DO UPDATE SET
                last_sync = COALESCE(excluded.last_sync, sync_state.last_sync),
                rows = excluded.rows,
                file_size_bytes = excluded.file_size_bytes,
                uncompressed_size_bytes = excluded.uncompressed_size_bytes,
                columns = excluded.columns,
                hash = excluded.hash,
                parts = excluded.parts,
                status = excluded.status,
                error = excluded.error""",
            [table_id, now, rows, file_size_bytes, uncompressed_size_bytes, columns, hash, parts_json, status, error],
        )
        # History rows always carry a real timestamp — `now` is None when the
        # caller preserves last_sync, but the history event still happened.
        self.conn.execute(
            """INSERT INTO sync_history (id, table_id, synced_at, rows, duration_ms, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [str(uuid.uuid4()), table_id, now or datetime.now(timezone.utc), rows, duration_ms, status, error],
        )

    def get_sync_history(self, table_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        results = self.conn.execute(
            "SELECT * FROM sync_history WHERE table_id = ? ORDER BY synced_at DESC LIMIT ?",
            [table_id, limit],
        ).fetchall()
        return self._rows_to_dicts(results)

    def list_recent(
        self,
        *,
        since: datetime,
        limit: int = 100,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return cross-table sync events newer than `since`, newest first.

        Used by Activity Center's Sync tab to render a unified feed across
        all registered tables. Per-table history stays available via
        `get_sync_history(table_id, limit)`.
        """
        sql = "SELECT * FROM sync_history WHERE synced_at >= ?"
        params: List[Any] = [since]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY synced_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return self._rows_to_dicts(rows)

    def status_counts_since(self, since: datetime) -> Dict[str, int]:
        """``{status: count}`` over ``sync_history`` rows synced at/after
        *since*. Backs the Activity Center health pulse's "sync_24h" field
        (``app/api/activity.py`` ``_compute_health``)."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM sync_history WHERE synced_at >= ? GROUP BY status",
            [since],
        ).fetchall()
        return {r[0]: int(r[1]) for r in rows}

    def set_error(self, table_id: str, error_message: str) -> None:
        """Record a per-table sync failure on the existing `error` /`status`
        columns so admin endpoints can surface it (`GET /api/admin/registry`
        joins this column into each row's `last_sync_error`).

        Upserts a sync_state row when one doesn't exist yet (a row that
        errored on its first ever materialize had no prior `update_sync`
        write). `last_sync` is left NULL on first-ever-error so the manifest
        doesn't claim a sync happened. Existing rows keep their last
        successful `last_sync` / `rows` / `hash` fields — only `status` and
        `error` flip — so analysts who already pulled the prior good
        parquet via `agnes pull` keep serving from it while the operator fixes
        the source.
        """
        self.conn.execute(
            """INSERT INTO sync_state (table_id, status, error)
            VALUES (?, 'error', ?)
            ON CONFLICT (table_id) DO UPDATE SET
                status = 'error',
                error = excluded.error""",
            [table_id, error_message],
        )

    def set_skipped(self, table_id: str, reason: str) -> None:
        """Record a per-table sync SKIP on the existing `error`/`status`
        columns (#754) — same shape as `set_error`, distinct status value
        so `GET /api/admin/registry` and `agnes admin list-tables` can tell
        "we tried and it failed" from "we deliberately didn't try this run
        (reason)" instead of leaving the row looking merely stale.

        Reserved for skip reasons meaningful enough to persist across a
        process restart (e.g. `source_filter`, `not_in_target`, `in_flight`)
        — NOT the routine per-tick `due_check`/"not yet due" skip, which
        fires on nearly every scheduler tick for every table with a
        schedule and would otherwise turn this into an UPDATE storm for no
        new information (the row's own `last_sync` already tells that
        story). Callers own that distinction; this method just persists
        whatever reason they pass.

        Same upsert-in-place semantics as `set_error`: preserves the row's
        `last_sync` / `rows` / `hash` from a prior successful sync so a
        table that was previously synced keeps serving its last-good
        parquet while this run's skip reason is surfaced alongside it.
        """
        self.conn.execute(
            """INSERT INTO sync_state (table_id, status, error)
            VALUES (?, 'skipped', ?)
            ON CONFLICT (table_id) DO UPDATE SET
                status = 'skipped',
                error = excluded.error""",
            [table_id, reason],
        )

    def clear_error(self, table_id: str) -> None:
        """Clear an `error` / `status='error'` flag without disturbing the
        rest of the sync_state row. Called after a successful materialize so
        the registry response stops surfacing stale failure messages.
        Idempotent — silently no-ops on rows that don't exist or already
        have status='ok'.
        """
        self.conn.execute(
            """UPDATE sync_state
            SET status = 'ok', error = ''
            WHERE table_id = ? AND status = 'error'""",
            [table_id],
        )

    def clear_for_table(self, table_id: str) -> int:
        """Drop all sync_state + sync_history rows for `table_id`, returning
        the number of sync_state rows removed.

        Called when a table is unregistered: a row synced at any point
        (local/materialized) leaves a sync_state entry that the manifest keeps
        serving to `agnes pull` regardless of registry state, so it must be
        purged alongside the registry row. This repo owns both tables, so both
        deletes live here (formerly inline in the admin unregister handler).
        """
        self.conn.execute("DELETE FROM sync_history WHERE table_id = ?", [table_id])
        removed = self.conn.execute(
            "DELETE FROM sync_state WHERE table_id = ? RETURNING table_id",
            [table_id],
        ).fetchall()
        return len(removed)

    def prune_history_older_than(self, days: int) -> int:
        """Delete ``sync_history`` rows older than ``days``, by
        ``synced_at``. Returns the deleted-row count.

        Scoped to ``sync_history`` only — never touches ``sync_state``, the
        live per-table snapshot (``last_sync``/``rows``/``hash``/``status``)
        that the manifest and `agnes pull` read. That row is current state,
        not a trail entry, and must survive regardless of history retention.

        The caller (``src/audit_retention.py``) owns the "days<=0 = keep
        forever, skip entirely" short-circuit — this method always executes
        the DELETE it's given, no matter the value (mirrors
        ``AuditRepository.prune_older_than``)."""
        rows = self.conn.execute(
            "DELETE FROM sync_history WHERE synced_at < (CURRENT_TIMESTAMP - INTERVAL (?) DAY) RETURNING id",
            [days],
        ).fetchall()
        return len(rows)
