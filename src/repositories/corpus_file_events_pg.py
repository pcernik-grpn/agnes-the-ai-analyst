"""Postgres-only repository for ``corpus_file_events`` — the append-only
observed-change log behind the SharePoint "what changed between A and B"
feed (``GET /api/admin/sharepoint/connections/{id}/changes``).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.corpus_file_events_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler in ``app/main.py``). ``record()`` is called
from ``app/api/collections.py``'s upload/delete handlers on EVERY backend —
those call sites wrap it in a best-effort try/except so a DuckDB-backed
instance's upload/delete path never breaks over this table not existing
there (mirrors ``facts_ingest_runs``'s own best-effort report write).
"""

from __future__ import annotations

import base64
import binascii
import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: The four change kinds this log ever writes. Kept here (not re-derived from
#: the API layer) so the repository itself can validate its own contract.
CHANGE_KINDS = ("added", "updated", "renamed", "deleted")


def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    if row.get("observed_at") is not None:
        row["observed_at"] = row["observed_at"].isoformat()
    return row


def encode_cursor(observed_at: datetime, event_id: str) -> str:
    """Opaque, URL-safe pagination token for one (observed_at, id) position."""
    raw = f"{observed_at.isoformat()}|{event_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> Tuple[datetime, str]:
    """Inverse of :func:`encode_cursor`. Raises ``ValueError`` on a malformed
    token — the caller (the API layer) turns that into a typed 400, never a
    500 from a bad row-value comparison downstream."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, event_id = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), event_id
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError(f"malformed cursor: {cursor!r}") from exc


class CorpusFileEventsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self,
        *,
        corpus_id: str,
        file_id: str,
        change: str,
        name: str,
        path: Optional[str] = None,
        source_stable_id: Optional[str] = None,
        observed_at: Optional[datetime] = None,
    ) -> str:
        """Append one observed-change row. Returns the generated id.

        ``observed_at`` is normally left ``None`` (the column's own
        ``CURRENT_TIMESTAMP`` default) — the optional override exists for
        tests that need deterministic, explicit timestamps to exercise
        pagination/ordering; no production call site passes it.
        """
        if change not in CHANGE_KINDS:
            raise ValueError(f"unknown change kind {change!r}; expected one of {CHANGE_KINDS}")
        event_id = "cfe_" + secrets.token_hex(8)
        columns = ["id", "corpus_id", "file_id", "source_stable_id", "change", "name", "path"]
        params: Dict[str, Any] = {
            "id": event_id,
            "corpus_id": corpus_id,
            "file_id": file_id,
            "source_stable_id": source_stable_id,
            "change": change,
            "name": name,
            "path": path,
        }
        if observed_at is not None:
            columns.append("observed_at")
            params["observed_at"] = observed_at
        placeholders = ", ".join(f":{c}" for c in columns)
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(f"INSERT INTO corpus_file_events ({', '.join(columns)}) VALUES ({placeholders})"),
                params,
            )
        return event_id

    def list_for_corpus_ids(
        self,
        corpus_ids: List[str],
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """One page of events across ``corpus_ids``, ordered ``(observed_at,
        id)`` ascending — deterministic regardless of how many events share
        the same timestamp. ``cursor`` (from a prior page's ``next_cursor``)
        resumes strictly AFTER that position; empty ``corpus_ids`` short-
        circuits to an empty page without a query.

        Returns ``(items, next_cursor)`` — ``next_cursor`` is ``None`` when
        this page reached the end of the window.
        """
        if not corpus_ids:
            return [], None
        limit = max(1, min(limit, 500))

        clauses = ["corpus_id = ANY(:corpus_ids)"]
        params: Dict[str, Any] = {"corpus_ids": list(corpus_ids), "limit": limit + 1}
        if since is not None:
            clauses.append("observed_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("observed_at <= :until")
            params["until"] = until
        if cursor is not None:
            cursor_ts, cursor_id = decode_cursor(cursor)
            clauses.append("(observed_at, id) > (:cursor_ts, :cursor_id)")
            params["cursor_ts"] = cursor_ts
            params["cursor_id"] = cursor_id

        query = (
            "SELECT * FROM corpus_file_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY observed_at ASC, id ASC LIMIT :limit"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(query), params).mappings().all()

        rows = [dict(r) for r in rows]
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            next_cursor = encode_cursor(last["observed_at"], last["id"])
        return [_decode_row(r) for r in rows], next_cursor
