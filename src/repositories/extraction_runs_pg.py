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

#: ``report`` keys never read by :func:`app.api.admin_extraction._run_out`
#: or ``_fleet_facts`` — only by the single-run detail endpoint
#: (``GET .../extraction/runs/{run_id}``, which reads the row through
#: :meth:`ExtractionRunsPgRepository.get`, untouched here). Each can hold up
#: to ``_FAILED_ITEMS_CAP`` (5000, ``connectors/sharepoint/crawler.py``)
#: itemized ``{path, reason, detail}`` entries — for a finished run that is
#: the majority of the row's bytes. The LIST projections below strip them at
#: the SQL level (a JSONB ``-`` key removal is cheap relative to shipping and
#: re-parsing megabytes of JSON the caller immediately discards), so a
#: fleet/history listing costs bytes proportional to the number of RUNS
#: listed, never to how many items any one of them failed on.
_REPORT_LIST_PROJECTION = "(report - 'failed_items' - 'skipped_items') AS report"
_RUN_LIST_COLUMNS = (
    "id, connection_id, job_id, status, phase, started_at, finished_at, checkpoint_at, "
    "files_seen, files_done, enumeration_done, " + _REPORT_LIST_PROJECTION + ", progress, usage, skips, error"
)


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

    def list_latest_for_connections(
        self, connection_ids: List[str], *, running_only: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        """One row per connection id — its NEWEST run — keyed by
        ``connection_id``. A connection with no run at all (or, when
        ``running_only``, no CURRENTLY running run) is simply absent from
        the returned mapping rather than represented with a placeholder —
        the caller (the fleet dashboard) already walks its own connection
        list and treats a missing key as "idle"/"never run".

        ``running_only`` narrows to ``status = 'running'`` rows only — the
        fleet view's default "what is on pace right now" scope, as opposed
        to its ``?all=1`` mode which wants the last run regardless of
        outcome. One query for the whole fleet (``DISTINCT ON``, Postgres-
        specific — this table has no DuckDB sibling) rather than one round
        trip per connection, so an operator's 8-connection dashboard costs
        the same as a 1-connection one.

        ``report`` comes back with ``failed_items``/``skipped_items``
        stripped (see :data:`_RUN_LIST_COLUMNS`) — this is a LIST view, and
        nothing that renders a fleet row reads either key.
        """
        if not connection_ids:
            return {}
        sql = f"SELECT DISTINCT ON (connection_id) {_RUN_LIST_COLUMNS} FROM extraction_runs WHERE connection_id = ANY(:ids)"
        params: Dict[str, Any] = {"ids": list(connection_ids)}
        if running_only:
            sql += " AND status = :running"
            params["running"] = RUNNING
        sql += " ORDER BY connection_id, started_at DESC"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return {str(r["connection_id"]): _decode_row(dict(r)) for r in rows}

    def abandon_stale_running(self, connection_id: str) -> List[str]:
        """Close out every ``running`` row for this connection — call this
        right before opening a NEW one (see ``_RunRecorder.start``).

        Only one crawl per connection runs at a time by construction — the
        manual/scheduled trigger's own idempotency dedup on the OWNING job
        (``app/api/admin_sharepoint.py::trigger_extraction``) refuses a
        second one while the first is still ``queued``/``running``. So if
        execution has reached the point where a NEW run is legitimately
        starting for this connection, any row still marked ``running`` is
        not that new run — it is a previous attempt whose worker died (a
        native crash, a killed process) without ever calling
        :meth:`finish`. Left alone it stays ``running`` forever: the source
        card reads the newest such row and renders a run that will never
        move, with no way for an operator to tell "in progress" from "died
        an hour ago".

        Recorded ``interrupted`` with ``report.interrupted_reason ==
        "abandoned"`` — a new, named member of the SAME vocabulary
        ``report.interrupted_reason`` already carries for a stop the crawl
        detects itself (``timeout`` / ``throttled`` / ``error``), not a new
        top-level status word. The row's existing ``report``/``progress``/
        ``files_seen``/``files_done`` (whatever the last checkpoint
        captured) are left untouched — a dead run's own numbers are real
        and are not overwritten with a claim of completion it never made.

        Returns the abandoned run ids (empty when there was nothing to
        close), so a caller that wants to log or test this can.
        """
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT id, report FROM extraction_runs WHERE connection_id = :cid AND status = :running"),
                    {"cid": connection_id, "running": RUNNING},
                )
                .mappings()
                .all()
            )
            abandoned: List[str] = []
            for row in rows:
                report = row["report"]
                if isinstance(report, str):
                    try:
                        report = json.loads(report)
                    except (ValueError, TypeError):
                        report = {}
                report = dict(report or {})
                report["interrupted"] = True
                report["interrupted_reason"] = "abandoned"
                conn.execute(
                    sa.text(
                        "UPDATE extraction_runs SET "
                        "  status = :status, "
                        "  finished_at = :finished_at, "
                        "  report = :report, "
                        "  error = :error "
                        "WHERE id = :id"
                    ),
                    {
                        "id": row["id"],
                        "status": INTERRUPTED,
                        "finished_at": _now(),
                        "report": json.dumps(report),
                        "error": (
                            "the worker that owned this run never finalized it — recorded abandoned "
                            "when a new run started for this connection"
                        ),
                    },
                )
                abandoned.append(row["id"])
        return abandoned

    def fail_for_job(self, job_id: str, *, error: str) -> Optional[str]:
        """Finalize this JOB's own still-``running`` row to ``failed`` —
        called from ``app/worker/runtime.py`` the moment the ``jobs`` row
        itself reaches a terminal ``failed`` state (an unhandled exception
        past its last retry, or ``reap_exhausted()`` closing out an
        attempts-exhausted lease), in the SAME code path as that
        finalize (2026-09 incident: a reclaim-exhausted `corpus-extraction`
        job flipped its `jobs` row to `failed` while its `extraction_runs`
        row stayed `running` forever — no reader, however patient, was
        ever going to see this end on its own).

        Matches on ``job_id``, not ``connection_id`` — unlike
        :meth:`abandon_stale_running`, which runs at the START of a brand
        new run for the same connection and may legitimately find an
        earlier run's leftover row. This runs from a WORKER-INTERNAL sweep
        that has no new run in flight and must touch only the row THIS job
        actually opened; a fresh run already begun for the same connection
        under a different ``job_id`` is a different row entirely and must
        never be touched.

        ``failed``, never ``interrupted`` — unlike a self-detected stop
        (timeout, admin-requested), the JOB dying is unambiguously the
        severity-first outcome ``_RunRecorder``'s own docstring already
        picks for a crash (`failed` beats `interrupted`), and it is exactly
        what `app/api/admin_extraction.py`'s ``_derived_outcome`` already
        infers a job-status of `failed` as, at read time — this just makes
        that inference a durable fact instead of only ever a live guess.

        Returns the closed run id, or ``None`` when no ``running`` row
        matched this ``job_id`` (the job never opened one — e.g. every kind
        except ``corpus-extraction`` — or it had already finalized itself).
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "UPDATE extraction_runs SET "
                    "  status = :status, "
                    "  finished_at = :finished_at, "
                    "  checkpoint_at = :finished_at, "
                    "  error = :error "
                    "WHERE job_id = :job_id AND status = :running "
                    "RETURNING id"
                ),
                {
                    "status": FAILED,
                    "finished_at": _now(),
                    "error": error,
                    "job_id": job_id,
                    "running": RUNNING,
                },
            ).first()
        return str(row[0]) if row else None

    def repoint_connection(self, *, from_connection_id: str, to_connection_id: str) -> int:
        """Re-point every run recorded under ``from_connection_id`` onto
        ``to_connection_id`` — the split-merge operation
        (``POST …/connections/{id}/splits/merge``) folding a sibling
        connection's run history onto the surviving target, so an admin can
        still see "when did this document last get crawled" after the
        connection that originally crawled it is gone.

        Each moved row's ``progress`` gains ``merged_from`` — set to
        ``from_connection_id`` ONLY when not already present, so a run
        already carried over by an EARLIER merge (a target that is itself
        later folded into a bigger target) keeps recording its ORIGINAL
        connection, not whichever connection happened to fold it most
        recently. Nothing else about the row (``status``, ``report``,
        ``usage``, ``checkpoint_at``, …) is touched — this is a re-point,
        never a rewrite of what actually happened.

        Idempotent: a repeat call with the same ``from_connection_id`` finds
        no rows left to move (they already carry ``to_connection_id``) and
        returns ``0`` rather than raising — the split-merge route's own
        resumable-step design relies on this.

        Returns the number of rows moved.
        """
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    sa.text("SELECT id, progress FROM extraction_runs WHERE connection_id = :from_id"),
                    {"from_id": from_connection_id},
                )
                .mappings()
                .all()
            )
            for row in rows:
                progress = row["progress"]
                if isinstance(progress, str):
                    try:
                        progress = json.loads(progress)
                    except (ValueError, TypeError):
                        progress = {}
                progress = dict(progress or {})
                progress.setdefault("merged_from", from_connection_id)
                conn.execute(
                    sa.text("UPDATE extraction_runs SET connection_id = :to_id, progress = :progress WHERE id = :id"),
                    {"to_id": to_connection_id, "progress": json.dumps(progress), "id": row["id"]},
                )
        return len(rows)

    def last_failed(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """The newest run that ended in ``failed`` — surfaced ALONGSIDE
        :meth:`last_completed`, never merged into it: that method's own
        contract (and its test coverage) deliberately keeps a hard failure
        out of the "last run" figures an operator reads for corpus-health
        trend numbers. Callers that want "what actually happened most
        recently, including a break" (the source card, after a job the
        runtime marked failed via :meth:`fail_for_job`) read this instead.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM extraction_runs "
                        "WHERE connection_id = :cid AND status = :failed "
                        "ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"cid": connection_id, "failed": FAILED},
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
        """Most recent runs first — the run-history drawer's rows.

        ``report`` comes back with ``failed_items``/``skipped_items``
        stripped, same as :meth:`list_latest_for_connections` — see
        :data:`_RUN_LIST_COLUMNS`. The single-run detail endpoint reads
        through :meth:`get`, which keeps every key.
        """
        limit = max(1, min(int(limit or 10), 100))
        sql = f"SELECT {_RUN_LIST_COLUMNS} FROM extraction_runs WHERE connection_id = :cid"
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
