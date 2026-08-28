"""Postgres-backed sync state + history repository.

Mirrors ``src/repositories/sync_state.py``. The upsert in
``update_sync`` uses PG's ``ON CONFLICT (table_id) DO UPDATE`` so the
DuckDB and PG impls converge on identical semantics.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from src.repositories.sync_state import _deserialize_parts


class SyncStatePgRepository:
    def __init__(self, engine: Engine):
        self._engine = engine

    def get_table_state(self, table_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text("SELECT * FROM sync_state WHERE table_id = :t"),
                    {"t": table_id},
                )
                .mappings()
                .first()
            )
        return _deserialize_parts(dict(row)) if row else None

    def get_last_sync(self, table_id: str) -> Optional[datetime]:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT last_sync FROM sync_state WHERE table_id = :t"),
                {"t": table_id},
            ).first()
        return row[0] if row else None

    def get_all_states(self) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT * FROM sync_state ORDER BY table_id")).mappings().all()
        return [_deserialize_parts(dict(r)) for r in rows]

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
        single-file table and writes SQL NULL (backward compatible).
        """
        now = datetime.now(timezone.utc) if bump_last_sync else None
        parts_json = json.dumps(parts) if parts is not None else None
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO sync_state
                       (table_id, last_sync, rows, file_size_bytes,
                        uncompressed_size_bytes, columns, hash, parts, status, error)
                       VALUES (:table_id, :now, :rows, :fsb, :usb, :cols, :hash,
                               CAST(:parts AS JSONB), :status, :error)
                       ON CONFLICT (table_id) DO UPDATE SET
                         last_sync = COALESCE(EXCLUDED.last_sync, sync_state.last_sync),
                         rows = EXCLUDED.rows,
                         file_size_bytes = EXCLUDED.file_size_bytes,
                         uncompressed_size_bytes = EXCLUDED.uncompressed_size_bytes,
                         columns = EXCLUDED.columns,
                         hash = EXCLUDED.hash,
                         parts = EXCLUDED.parts,
                         status = EXCLUDED.status,
                         error = EXCLUDED.error"""
                ),
                {
                    "table_id": table_id,
                    "now": now,
                    "rows": rows,
                    "fsb": file_size_bytes,
                    "usb": uncompressed_size_bytes,
                    "cols": columns,
                    "hash": hash,
                    "parts": parts_json,
                    "status": status,
                    "error": error,
                },
            )
            conn.execute(
                sa.text(
                    """INSERT INTO sync_history
                       (id, table_id, synced_at, rows, duration_ms, status, error)
                       VALUES (:id, :table_id, :now, :rows, :dms, :status, :error)"""
                ),
                {
                    "id": str(uuid.uuid4()),
                    "table_id": table_id,
                    # History rows always carry a real timestamp — `now` is
                    # None when the caller preserves last_sync, but the
                    # history event still happened.
                    "now": now or datetime.now(timezone.utc),
                    "rows": rows,
                    "dms": duration_ms,
                    "status": status,
                    "error": error,
                },
            )

    def get_sync_history(self, table_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        """SELECT * FROM sync_history WHERE table_id = :t
                       ORDER BY synced_at DESC LIMIT :limit"""
                    ),
                    {"t": table_id, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_recent(
        self,
        *,
        since: datetime,
        limit: int = 100,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM sync_history WHERE synced_at >= :since"
        params: Dict[str, Any] = {"since": since, "limit": limit}
        if status is not None:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY synced_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]

    def status_counts_since(self, since: datetime) -> Dict[str, int]:
        """Mirrors ``SyncStateRepository.status_counts_since``."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT status, COUNT(*) FROM sync_history WHERE synced_at >= :since GROUP BY status"),
                {"since": since},
            ).all()
        return {r[0]: int(r[1]) for r in rows}

    def set_error(self, table_id: str, error_message: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO sync_state (table_id, status, error)
                       VALUES (:t, 'error', :err)
                       ON CONFLICT (table_id) DO UPDATE SET
                         status = 'error',
                         error = EXCLUDED.error"""
                ),
                {"t": table_id, "err": error_message},
            )

    def set_skipped(self, table_id: str, reason: str) -> None:
        """Mirrors ``SyncStateRepository.set_skipped`` — see its docstring
        for the reasoning (#754) on which skip reasons are worth persisting."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """INSERT INTO sync_state (table_id, status, error)
                       VALUES (:t, 'skipped', :reason)
                       ON CONFLICT (table_id) DO UPDATE SET
                         status = 'skipped',
                         error = EXCLUDED.error"""
                ),
                {"t": table_id, "reason": reason},
            )

    def clear_error(self, table_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    """UPDATE sync_state SET status = 'ok', error = ''
                       WHERE table_id = :t AND status = 'error'"""
                ),
                {"t": table_id},
            )

    def clear_for_table(self, table_id: str) -> int:
        """Drop all sync_state + sync_history rows for `table_id`, returning
        the number of sync_state rows removed.

        Mirrors ``SyncStateRepository.clear_for_table``. Both deletes run in
        one transaction; the returned count is the sync_state delete's
        rowcount.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM sync_history WHERE table_id = :t"),
                {"t": table_id},
            )
            result = conn.execute(
                sa.text("DELETE FROM sync_state WHERE table_id = :t"),
                {"t": table_id},
            )
        return result.rowcount

    def prune_history_older_than(self, days: int) -> int:
        """Mirrors ``SyncStateRepository.prune_history_older_than``."""
        with self._engine.begin() as conn:
            rows = conn.execute(
                sa.text(
                    "DELETE FROM sync_history "
                    "WHERE synced_at < (CURRENT_TIMESTAMP - (:days * INTERVAL '1 day')) "
                    "RETURNING 1"
                ),
                {"days": days},
            ).all()
        return len(rows)
