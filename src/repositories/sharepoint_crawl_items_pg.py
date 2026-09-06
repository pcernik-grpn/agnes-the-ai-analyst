"""Postgres-only repository for ``sharepoint_crawl_items`` — the per-FILE
``ctag``/``failed_items``/``empty_items`` bookkeeping split OUT of
``sharepoint_connection_state.payload`` (see migration
``0110_sharepoint_crawl_items`` for the measured incident this fixes: a
connection with a few hundred thousand documents grew those three maps to
tens of megabytes inside ONE row's ``payload``, and every crawl checkpoint
rewrote the whole thing — Postgres always produces a brand-new toasted
value for a changed ``jsonb`` column, even via ``jsonb_set`` targeting one
key, so every checkpoint orphaned the previous ~21 MB of TOAST chunks).

One row per ``(connection_id, kind, stable_id)``. Updating one file's ctag
(or failed/empty entry) is a single-row UPDATE/INSERT touching only that
row's own tuple — a checkpoint that changed N files now costs O(N), not
O(every file this connection has ever seen).

PG-first ratchet (A3): brand-new app-state table added after the freeze, so
there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.sharepoint_crawl_items_repo()`` — but note, same as
``sharepoint_state_pg.py``, that ``connectors/sharepoint/state_store.py``
(the module every caller actually uses) never resolves it while the active
backend is DuckDB: a crawl in flight must keep working unchanged there, so
the seam checks ``use_pg()`` itself rather than ever letting
``RequiresPostgresBackend`` reach it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: The three per-file collections this table splits out of the hot blob,
#: and the column each lives in — shared by :meth:`get_all` and
#: :meth:`apply_delta` so the two can never drift on which field maps to
#: which column.
_FIELD_COLUMNS: Dict[str, str] = {
    "ctags": "ctag",
    "failed_items": "failed_entry",
    "empty_items": "empty_entry",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


class SharepointCrawlItemsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get_all(self, connection_id: str, kind: str) -> Dict[str, Dict[str, Any]]:
        """This connection's full ``{"ctags", "failed_items",
        "empty_items"}`` maps, reconstructed from every row of
        ``(connection_id, kind)`` — the same shape
        ``connectors.sharepoint.crawler.load_state`` has always returned,
        just read from N rows instead of decoded out of one blob. A
        never-crawled connection (no rows at all) answers three empty
        dicts, not an error.
        """
        out: Dict[str, Dict[str, Any]] = {"ctags": {}, "failed_items": {}, "empty_items": {}}
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT stable_id, ctag, failed_entry, empty_entry FROM sharepoint_crawl_items "
                    "WHERE connection_id = :cid AND kind = :kind"
                ),
                {"cid": connection_id, "kind": kind},
            ).mappings()
            for row in rows:
                stable_id = row["stable_id"]
                if row["ctag"] is not None:
                    out["ctags"][stable_id] = row["ctag"]
                failed = _decode_json(row["failed_entry"])
                if failed is not None:
                    out["failed_items"][stable_id] = failed
                empty = _decode_json(row["empty_entry"])
                if empty is not None:
                    out["empty_items"][stable_id] = empty
        return out

    def counts(self, connection_id: str, kind: str) -> Dict[str, int]:
        """``{"failed_items_count", "empty_items_count"}`` — the SIZE of
        the two backlogs, without decoding any row's JSON — the "Retry
        failed (N)"/"Retry empty (N)" buttons' cheap, polled-every-few-
        seconds read (mirrors ``SharepointStatePgRepository.
        backlog_counts``' own docstring, now reading this table instead of
        a payload key)."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT "
                        "  count(*) FILTER (WHERE failed_entry IS NOT NULL) AS failed_items_count, "
                        "  count(*) FILTER (WHERE empty_entry IS NOT NULL) AS empty_items_count "
                        "FROM sharepoint_crawl_items WHERE connection_id = :cid AND kind = :kind"
                    ),
                    {"cid": connection_id, "kind": kind},
                )
                .mappings()
                .first()
            )
        if row is None:
            return {"failed_items_count": 0, "empty_items_count": 0}
        return {
            "failed_items_count": int(row["failed_items_count"] or 0),
            "empty_items_count": int(row["empty_items_count"] or 0),
        }

    def apply_delta(
        self,
        connection_id: str,
        kind: str,
        *,
        ctags: Dict[str, Any],
        failed: Dict[str, Any],
        empty: Dict[str, Any],
    ) -> None:
        """Apply one checkpoint's worth of change to the three per-file
        collections, each shaped ``{"set": {stable_id: value}, "removed":
        [stable_id, ...], "reset": bool}`` (``connectors.sharepoint.
        crawler._drain_item_deltas``). ``reset`` clears this field's
        column for EVERY row of ``(connection_id, kind)`` first — the
        resync (``failed_items``) / sharded-finalize (``ctags``) whole-
        collection reset call sites, which reassign the state dict's key
        to a plain ``{}`` rather than mutating it incrementally.

        One transaction for the whole call: either every touched file's
        row lands, or none does — the same atomicity the old single-blob
        write gave, now scoped to this checkpoint's own delta rather than
        the whole connection.
        """
        with self._engine.begin() as conn:
            now = _now()
            for field, column in _FIELD_COLUMNS.items():
                delta = ctags if field == "ctags" else failed if field == "failed_items" else empty
                if delta.get("reset"):
                    conn.execute(
                        sa.text(
                            f"UPDATE sharepoint_crawl_items SET {column} = NULL, updated_at = :now "
                            "WHERE connection_id = :cid AND kind = :kind"
                        ),
                        {"cid": connection_id, "kind": kind, "now": now},
                    )
                for stable_id in delta.get("removed") or ():
                    conn.execute(
                        sa.text(
                            f"UPDATE sharepoint_crawl_items SET {column} = NULL, updated_at = :now "
                            "WHERE connection_id = :cid AND kind = :kind AND stable_id = :sid"
                        ),
                        {"cid": connection_id, "kind": kind, "sid": stable_id, "now": now},
                    )
                set_entries: Dict[str, Any] = delta.get("set") or {}
                if not set_entries:
                    continue
                value_expr = ":value" if field == "ctags" else "CAST(:value AS JSONB)"
                for stable_id, value in set_entries.items():
                    param_value = value if field == "ctags" else json.dumps(value)
                    conn.execute(
                        sa.text(
                            f"INSERT INTO sharepoint_crawl_items (connection_id, kind, stable_id, {column}, updated_at) "
                            f"VALUES (:cid, :kind, :sid, {value_expr}, :now) "
                            "ON CONFLICT (connection_id, kind, stable_id) DO UPDATE SET "
                            f"  {column} = EXCLUDED.{column}, updated_at = EXCLUDED.updated_at"
                        ),
                        {"cid": connection_id, "kind": kind, "sid": stable_id, "value": param_value, "now": now},
                    )
