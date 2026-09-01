"""Postgres-only repository for ``memory_detection_runs`` (issue #1971 Part 3
— corporate-memory detection run logs).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.memory_detection_runs_repo()``; on a DuckDB-backed
instance that factory call raises ``RequiresPostgresBackend`` (translated to
a ``501`` by the app-wide handler in ``app/main.py``).

Every caller writes through ``src.memory_detection_logging.record_detection_run``,
never this repository directly, so a resolution failure (DuckDB backend, or
any other hiccup) degrades to one warning log line instead of ever failing
the detection run it describes.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: JSONB column(s) — decoded back to a dict on read so a caller never has to
#: care whether the driver handed back a string or a mapping.
_JSON_FIELDS = ("token_usage",)


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    for key in _JSON_FIELDS:
        value = row.get(key)
        if isinstance(value, str):
            try:
                row[key] = json.loads(value)
            except (ValueError, TypeError):
                row[key] = {}
        elif value is None:
            row[key] = {}
    return row


class MemoryDetectionRunsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        *,
        source: str,
        started_at: datetime,
        finished_at: Optional[datetime] = None,
        sessions_scanned: int = 0,
        items_proposed: int = 0,
        items_filtered: int = 0,
        items_inserted: int = 0,
        items_routed_side_domain: int = 0,
        token_usage: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        dry_run: bool = False,
        policy_fingerprint: Optional[str] = None,
    ) -> str:
        """Insert one completed (or dry-run) detection-run row; returns its id.

        Writes a WHOLE row at once — unlike ``extraction_runs``, a
        detection run has no live-progress phase worth checkpointing
        (a verification-processor call or a collector pass is one shot,
        not a long crawl), so there is no separate ``start()``/``finish()``
        pair.
        """
        run_id = "mdr_" + secrets.token_hex(8)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO memory_detection_runs "
                    "(id, source, started_at, finished_at, sessions_scanned, "
                    " items_proposed, items_filtered, items_inserted, "
                    " items_routed_side_domain, token_usage, error, dry_run, "
                    " policy_fingerprint) "
                    "VALUES (:id, :source, :started_at, :finished_at, :sessions_scanned, "
                    " :items_proposed, :items_filtered, :items_inserted, "
                    " :items_routed_side_domain, :token_usage, :error, :dry_run, "
                    " :policy_fingerprint)"
                ),
                {
                    "id": run_id,
                    "source": source,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "sessions_scanned": int(sessions_scanned or 0),
                    "items_proposed": int(items_proposed or 0),
                    "items_filtered": int(items_filtered or 0),
                    "items_inserted": int(items_inserted or 0),
                    "items_routed_side_domain": int(items_routed_side_domain or 0),
                    "token_usage": json.dumps(token_usage or {}),
                    "error": error,
                    "dry_run": bool(dry_run),
                    "policy_fingerprint": policy_fingerprint,
                },
            )
        return run_id

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM memory_detection_runs WHERE id = :id"), {"id": run_id})
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def list_recent(self, *, limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
        """Most recent runs first — the admin observability panel's rows."""
        limit = max(1, min(int(limit or 20), 200))
        offset = max(0, int(offset or 0))
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT * FROM memory_detection_runs ORDER BY started_at DESC LIMIT :limit OFFSET :offset"),
                    {"limit": limit, "offset": offset},
                )
                .mappings()
                .all()
            )
        return [_decode_row(dict(r)) for r in rows]

    def count(self) -> int:
        """Total runs recorded — so "N more" is never a silent truncation."""
        with self._engine.connect() as conn:
            value = conn.execute(sa.text("SELECT COUNT(*) FROM memory_detection_runs")).scalar()
        return int(value or 0)
