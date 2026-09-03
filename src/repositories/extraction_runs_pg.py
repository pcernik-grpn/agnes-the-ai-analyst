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
#: ``shards_total``/``shards_done`` (migration ``0103_crawl_shards``) MUST
#: ride every LIST projection, not just the full-row reads (``get``/
#: ``get_running``/``last_completed``/``last_failed``, all ``SELECT *``):
#: a top-level PARENT (planner) row's ``mode`` (``app.api.admin_extraction.
#: _run_out``) is derived from ``shards_total is not None`` alone, so
#: omitting it here silently reports every sharded site's fleet/history row
#: as ``"inline"`` — caught by ``tests/db_pg/test_extraction_api_pg.py``'s
#: shard-rollup tests (2026-09-03 auto-parallel-crawl design §4.7, plan
#: Task 8). A CHILD row's own ``shards_total`` is always ``NULL`` by
#: construction (a shard is never itself sharded), so this costs nothing on
#: :meth:`children_for`'s use of the same column list.
_RUN_LIST_COLUMNS = (
    "id, connection_id, job_id, status, phase, started_at, finished_at, checkpoint_at, "
    "files_seen, files_done, enumeration_done, " + _REPORT_LIST_PROJECTION + ", progress, usage, skips, error, "
    "shards_total, shards_done"
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
        parent_run_id: Optional[str] = None,
        shard_key: Optional[str] = None,
        shard_label: Optional[str] = None,
        shards_total: Optional[int] = None,
    ) -> str:
        """Open a ``running`` row for a crawl that is about to begin.

        ``parent_run_id``/``shard_key``/``shard_label`` (2026-09-03 auto-
        parallel-crawl design §4.2, migration ``0103_crawl_shards``) mark
        this row as a SHARD CHILD's own — all three ``None`` (every caller
        before sharding existed, and every inline run since) is today's
        plain row. ``shards_total`` marks a PARENT (planner) row instead —
        the two are never both set on the same row: a shard is not itself
        sharded.
        """
        run_id = "er_" + secrets.token_hex(8)
        now = _now()
        with self._engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO extraction_runs "
                    "(id, connection_id, job_id, status, phase, started_at, checkpoint_at, "
                    " parent_run_id, shard_key, shard_label, shards_total) "
                    "VALUES (:id, :connection_id, :job_id, :status, :phase, :started_at, :checkpoint_at, "
                    " :parent_run_id, :shard_key, :shard_label, :shards_total)"
                ),
                {
                    "id": run_id,
                    "connection_id": connection_id,
                    "job_id": job_id,
                    "status": RUNNING,
                    "phase": phase,
                    "started_at": now,
                    "checkpoint_at": now,
                    "parent_run_id": parent_run_id,
                    "shard_key": shard_key,
                    "shard_label": shard_label,
                    "shards_total": shards_total,
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

    # -- shard-crawl (2026-09-03 auto-parallel-crawl design §4.3) ---------

    def bump_parent_checkpoint(self, parent_run_id: str) -> None:
        """Advance a PARENT run's ``checkpoint_at`` to now — called from a
        CHILD's own :meth:`checkpoint` (``connectors.sharepoint.crawler
        ._RunRecorder.checkpoint``) so a live parent whose children are all
        still crawling never reads as stale to a liveness check derived
        from checkpoint age, even though the parent's own row stopped
        writing the moment it finished enqueueing.

        Scoped to ``status = 'running'`` — same "a late write can never
        resurrect a finalized row" rule :meth:`checkpoint` already applies
        to an ordinary run.
        """
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE extraction_runs SET checkpoint_at = :now WHERE id = :id AND status = :running"),
                {"id": parent_run_id, "now": _now(), "running": RUNNING},
            )

    def finish_shard(self, parent_run_id: str) -> Optional[Dict[str, int]]:
        """Atomically increment a PARENT's ``shards_done`` by one — called
        once by each CHILD as it finishes (success, failure, or stopped).

        Returns ``{"shards_done": <new total>, "shards_total": <int or
        None>}`` — the child that observes ``shards_done == shards_total``
        is the one that finalizes (see :meth:`claim_finalize`). ``None``
        when the parent row does not exist (defensive — should never
        happen in practice, since a child's own payload always carries a
        ``parent_run_id`` its planner just opened).
        """
        with self._engine.begin() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "UPDATE extraction_runs SET shards_done = shards_done + 1 "
                        "WHERE id = :id RETURNING shards_done, shards_total"
                    ),
                    {"id": parent_run_id},
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        return {"shards_done": int(row["shards_done"]), "shards_total": row["shards_total"]}

    def claim_finalize(self, parent_run_id: str) -> bool:
        """Win the race to finalize a PARENT run — at most one caller ever
        gets ``True`` for a given ``parent_run_id``, however many children
        observe ``shards_done == shards_total`` at once (design §4.3: "the
        LAST child to finish finalizes the parent", made race-safe here).

        ``phase`` is the flag: flips ``'plan'``/``'crawl'``/whatever the
        parent's phase last was to ``'finalizing'`` in one atomic
        ``UPDATE ... WHERE phase <> 'finalizing' RETURNING id`` — a second,
        concurrent caller's ``WHERE`` matches zero rows and gets ``False``.
        Idempotent in the sense that matters: a caller that loses the race
        does no work and does not finalize twice, but a genuinely STUCK
        parent (finalizer died mid-way, phase left at ``'finalizing'``
        forever) is not un-stuck by calling this again — that recovery path
        is "the next ``POST …/extract`` finalizes instead of re-planning"
        (design §4.3), not a retry of this method.
        """
        with self._engine.begin() as conn:
            row = conn.execute(
                sa.text(
                    "UPDATE extraction_runs SET phase = 'finalizing' "
                    "WHERE id = :id AND phase IS DISTINCT FROM 'finalizing' "
                    "RETURNING id"
                ),
                {"id": parent_run_id},
            ).first()
        return row is not None

    # -- read -------------------------------------------------------------

    def children_for(self, parent_run_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
        """Every shard child row for the given PARENT run ids, in ONE
        query — keyed by ``parent_run_id`` — so the fleet view and the
        finalizer never pay one round trip per parent.

        ``report`` comes back with ``failed_items``/``skipped_items``
        stripped (see :data:`_RUN_LIST_COLUMNS`) — same LIST-view contract
        as :meth:`list_latest_for_connections`; the finalizer's own
        aggregation reads only the counters and top-level keys every
        report carries, never the itemized lists.
        """
        if not parent_run_ids:
            return {}
        sql = (
            f"SELECT {_RUN_LIST_COLUMNS}, parent_run_id, shard_key, shard_label FROM extraction_runs "
            "WHERE parent_run_id = ANY(:ids) ORDER BY parent_run_id, shard_key"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), {"ids": list(parent_run_ids)}).mappings().all()
        by_parent: Dict[str, List[Dict[str, Any]]] = {pid: [] for pid in parent_run_ids}
        for r in rows:
            by_parent.setdefault(str(r["parent_run_id"]), []).append(_decode_row(dict(r)))
        return by_parent

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._engine.connect() as conn:
            row = (
                conn.execute(sa.text("SELECT * FROM extraction_runs WHERE id = :id"), {"id": run_id}).mappings().first()
            )
        return _decode_row(dict(row)) if row else None

    def get_running(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """The newest still-``running`` TOP-LEVEL row for this connection,
        or None — ``parent_run_id IS NULL`` excludes a shard CHILD's own
        row (2026-09-03 auto-parallel-crawl design §4.3): a child never
        appears as this connection's "the" running row, only its PARENT
        (planner) row does, for the whole time any of its children are
        still crawling.

        "Newest" rather than "the only one": the trigger endpoint's
        idempotency key already prevents two concurrent TOP-LEVEL runs per
        connection, but a row orphaned by a hard-killed worker (no finalize
        ever ran) would otherwise shadow the real one forever.
        """
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM extraction_runs "
                        "WHERE connection_id = :cid AND status = :running AND parent_run_id IS NULL "
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
        sql = (
            f"SELECT DISTINCT ON (connection_id) {_RUN_LIST_COLUMNS} FROM extraction_runs "
            "WHERE connection_id = ANY(:ids) AND parent_run_id IS NULL"
        )
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
        """The newest TOP-LEVEL run that ended in ``failed`` (``parent_run_id
        IS NULL`` — a shard child's own failure surfaces through its
        PARENT's aggregated outcome, never as a top-level row of its own,
        2026-09-03 auto-parallel-crawl design §4.7) — surfaced ALONGSIDE
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
                        "WHERE connection_id = :cid AND status = :failed AND parent_run_id IS NULL "
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
        """Most recent TOP-LEVEL runs first (``parent_run_id IS NULL`` — a
        shard child never appears in the run-history drawer as its own
        row; its parent's row, with its aggregated ``shards_done``/
        ``shards_total``, does) — the run-history drawer's rows.

        ``report`` comes back with ``failed_items``/``skipped_items``
        stripped, same as :meth:`list_latest_for_connections` — see
        :data:`_RUN_LIST_COLUMNS`. The single-run detail endpoint reads
        through :meth:`get`, which keeps every key.
        """
        limit = max(1, min(int(limit or 10), 100))
        sql = f"SELECT {_RUN_LIST_COLUMNS} FROM extraction_runs WHERE connection_id = :cid AND parent_run_id IS NULL"
        params: Dict[str, Any] = {"cid": connection_id, "limit": limit}
        if not include_running:
            sql += " AND status <> :running"
            params["running"] = RUNNING
        sql += " ORDER BY started_at DESC LIMIT :limit"
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [_decode_row(dict(r)) for r in rows]

    def count_for_connection(self, connection_id: str) -> int:
        """Total TOP-LEVEL runs recorded for this connection (``parent_run_id
        IS NULL``) — the drawer button's count, so "5 more runs" is never a
        silent truncation, and never inflated by a sharded run's own
        children."""
        with self._engine.connect() as conn:
            value = conn.execute(
                sa.text("SELECT COUNT(*) FROM extraction_runs WHERE connection_id = :cid AND parent_run_id IS NULL"),
                {"cid": connection_id},
            ).scalar()
        return int(value or 0)

    def last_completed(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """The newest TOP-LEVEL run that actually ended (``done`` or
        ``interrupted``, ``parent_run_id IS NULL``) — the card's "last run"
        figures come from here, never from a ``failed`` row that has no
        counters, never from a live one, and never from a shard child's own
        row (its parent's aggregated row is what "last run" means for a
        sharded site)."""
        with self._engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT * FROM extraction_runs "
                        "WHERE connection_id = :cid AND status IN (:done, :interrupted) "
                        "AND parent_run_id IS NULL "
                        "ORDER BY started_at DESC LIMIT 1"
                    ),
                    {"cid": connection_id, "done": DONE, "interrupted": INTERRUPTED},
                )
                .mappings()
                .first()
            )
        return _decode_row(dict(row)) if row else None
