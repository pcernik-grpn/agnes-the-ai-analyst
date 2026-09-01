"""Postgres-only repository for ``extraction_runs`` — one row per built-in
extraction run (2026-08-31 extraction-observability-ui design §7.1).

PG-first ratchet (A3): brand-new app-state surface added after the freeze,
so there is no DuckDB sibling — see ``docs/migrations.md`` -> "Adding a
PG-only feature". Reach this repo only through
``src.repositories.extraction_runs_repo()``; on a DuckDB-backed instance
that factory call raises ``RequiresPostgresBackend`` (translated to a
``501`` by the app-wide handler in ``app/main.py``).

Deliberately a SEPARATE repository from ``jobs``: ``jobs`` owns the run's
LIFECYCLE (queued/running/failed + the final result payload) and is a frozen
DuckDB<->PG pair; this owns the run's PROGRESS, which needs a mutable
per-connection-indexed row the frozen pair cannot grow. ``job_id`` joins
them.

Every write here is observability, never load-bearing: the caller
(``connectors/sharepoint/crawler.py``) treats a failure to record as a
missing row, not a failed crawl.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

#: Run outcomes, matching the job lifecycle exactly. ``interrupted`` is its
#: own outcome, NOT a flavour of failure — the run ingested what it ingested
#: and the next run resumes from the persisted cTags (design §4.3).
RUNNING = "running"
DONE = "done"
INTERRUPTED = "interrupted"
FAILED = "failed"
STATUSES = (RUNNING, DONE, INTERRUPTED, FAILED)

#: JSONB columns — decoded back to dicts on read so a caller never has to
#: care whether the driver handed back a string or a mapping.
_JSON_FIELDS = ("report", "progress", "usage", "skips")

#: Cap on the itemized skip list a single run stores (design §7.1). The
#: stored shape always carries a ``total`` alongside, so a truncated list is
#: VISIBLY truncated rather than silently short.
_SKIPS_CAP = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    for key in ("started_at", "finished_at", "checkpoint_at"):
        value = row.get(key)
        if value is not None and not isinstance(value, str):
            row[key] = value.isoformat()
    return row


def cap_skips(items: List[Dict[str, Any]], *, total: Optional[int] = None) -> Dict[str, Any]:
    """``{items: [...≤200], listed: N, total: M, truncated: bool}``.

    ``total`` defaults to ``len(items)`` but may be passed explicitly when
    the caller knows about skips it has no path for (a conversion failure
    keeps no filename, an unreadable drive is not a file at all): the run
    then reports "27 skipped, 20 listed" rather than pretending the list is
    the whole story.

    Exposed as a module function (not a private helper) so the crawl and the
    API agree on the shape by construction rather than by convention, and so
    the truncation rule is independently testable.
    """
    items = list(items or [])
    kept = items[:_SKIPS_CAP]
    resolved_total = len(items) if total is None else max(int(total), len(kept))
    return {
        "items": kept,
        "listed": len(kept),
        "total": resolved_total,
        "truncated": resolved_total > len(kept),
    }


class ExtractionRunsPgRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # -- write ------------------------------------------------------------

    def start(
        self,
        *,
        connection_id: str,
        job_id: Optional[str] = None,
        phase: str = "crawl",
    ) -> str:
        """Open a ``running`` row for a crawl that is about to begin."""
        run_id = "er_" + secrets.token_hex(8)
        now = _now()
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO extraction_runs "
                    "(id, connection_id, job_id, status, phase, started_at, checkpoint_at) "
                    "VALUES (:id, :connection_id, :job_id, :status, :phase, :started_at, :checkpoint_at)"
                ),
                {
                    "id": run_id,
                    "connection_id": connection_id,
                    "job_id": job_id,
                    "status": RUNNING,
                    "phase": phase,
                    "started_at": now,
                    "checkpoint_at": now,
                },
            )
        return run_id

    def checkpoint(
        self,
        run_id: str,
        *,
        phase: Optional[str] = None,
        files_seen: int = 0,
        files_done: int = 0,
        enumeration_done: bool = False,
        progress: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record the counters this run has reached, and WHEN it reached
        them (``checkpoint_at`` is what the card's "as of" caption prints).

        A checkpoint on a run that has already finalized is a no-op: the
        ``status = 'running'`` predicate means a late write from a crawl
        that was already recorded as interrupted can never resurrect it.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE extraction_runs SET "
                    "  phase = COALESCE(:phase, phase), "
                    "  files_seen = :files_seen, "
                    "  files_done = :files_done, "
                    "  enumeration_done = :enumeration_done, "
                    "  progress = :progress, "
                    "  checkpoint_at = :checkpoint_at "
                    "WHERE id = :id AND status = :running"
                ),
                {
                    "id": run_id,
                    "phase": phase,
                    "files_seen": int(files_seen or 0),
                    "files_done": int(files_done or 0),
                    "enumeration_done": bool(enumeration_done),
                    "progress": json.dumps(progress or {}),
                    "checkpoint_at": _now(),
                    "running": RUNNING,
                },
            )

    def finish(
        self,
        run_id: str,
        *,
        status: str,
        report: Optional[Dict[str, Any]] = None,
        usage: Optional[Dict[str, Any]] = None,
        skips: Optional[Dict[str, Any]] = None,
        files_seen: Optional[int] = None,
        files_done: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Close the row with its outcome and its final report.

        ``status`` must be one of ``done``/``interrupted``/``failed`` — a
        finalize into ``running`` is refused rather than stored, because a
        row that says "running" with a ``finished_at`` is exactly the kind
        of value that looks checked and is not.
        """
        if status not in (DONE, INTERRUPTED, FAILED):
            raise ValueError(f"extraction run cannot finalize into status {status!r}")
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE extraction_runs SET "
                    "  status = :status, "
                    "  finished_at = :finished_at, "
                    "  checkpoint_at = :finished_at, "
                    "  enumeration_done = TRUE, "
                    "  report = :report, "
                    "  usage = :usage, "
                    "  skips = :skips, "
                    "  files_seen = COALESCE(:files_seen, files_seen), "
                    "  files_done = COALESCE(:files_done, files_done), "
                    "  error = :error "
                    "WHERE id = :id"
                ),
                {
                    "id": run_id,
                    "status": status,
                    "finished_at": _now(),
                    "report": json.dumps(report or {}),
                    "usage": json.dumps(usage or {}),
                    "skips": json.dumps(skips or {}),
                    "files_seen": files_seen,
                    "files_done": files_done,
                    "error": error,
                },
            )

    # -- read -------------------------------------------------------------

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM extraction_runs WHERE id = :id"), {"id": run_id}).mappings().first()
            )
        return _decode_row(dict(row)) if row else None

    def get_running(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """The newest still-``running`` row for this connection, or None.

        "Newest" rather than "the only one": the trigger endpoint's
        idempotency key already prevents two concurrent runs per connection,
        but a row orphaned by a hard-killed worker (no finalize ever ran)
        would otherwise shadow the real one forever.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM extraction_runs "
                        "WHERE connection_id = :cid AND status = :running "
                        "ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"cid": connection_id, "running": RUNNING},
                )
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None

    def list_for_connection(
        self,
        connection_id: str,
        *,
        limit: int = 10,
        include_running: bool = True,
    ) -> List[Dict[str, Any]]:
        """Most recent runs first — the run-history drawer's rows."""
        limit = max(1, min(int(limit or 10), 100))
        sql = "SELECT * FROM extraction_runs WHERE connection_id = :cid"
        params: Dict[str, Any] = {"cid": connection_id, "limit": limit}
        if not include_running:
            sql += " AND status <> :running"
            params["running"] = RUNNING
        sql += " ORDER BY started_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [_decode_row(dict(r)) for r in rows]

    def count_for_connection(self, connection_id: str) -> int:
        """Total runs recorded for this connection — the drawer button's
        count, so "5 more runs" is never a silent truncation."""
        with self._engine.connect() as conn:
            value = conn.execute(
                sa.text("SELECT COUNT(*) FROM extraction_runs WHERE connection_id = :cid"),
                {"cid": connection_id},
            ).scalar()
        return int(value or 0)

    def last_completed(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """The newest run that actually ended (``done`` or ``interrupted``)
        — the card's "last run" figures come from here, never from a
        ``failed`` row that has no counters and never from a live one."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM extraction_runs "
                        "WHERE connection_id = :cid AND status IN (:done, :interrupted) "
                        "ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"cid": connection_id, "done": DONE, "interrupted": INTERRUPTED},
                )
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None
