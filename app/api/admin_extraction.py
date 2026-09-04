"""Admin REST API for extraction OBSERVABILITY — what is running right now,
what the last N runs did, and how the pipeline is configured.

Design: ``docs/superpowers/specs/2026-08-31-extraction-observability-ui-design.md``
(§4 "What is running right now", §6 "How it is configured", §9's endpoint
table A1/A2/A3/A5). Its first principle is the one every shape below serves:

    every number carries its own freshness and its own source; a value that
    could not be read renders as FAILED, never as 0, "—", or the previous
    value.

A SEPARATE module from ``app/api/admin_sharepoint.py`` (which owns the
connect wizard: tree browse, scope confirmation, corpus map, the extraction
TRIGGER) on purpose — these are read-only observability endpoints over a
different store (``extraction_runs``), and keeping them apart means a change
to how a run is watched can never accidentally change how a scope is
confirmed. What they DO share, they share by import rather than by copy:
``_scope_out`` for the per-scope rows, so the config drawer, the wizard's
step-3 preview and the source card cannot drift (design §6.2).

Surface (all gated by ``Depends(require_admin)``):

  GET /api/admin/sharepoint/connections/{id}/extraction/status
      A1 — live run state + the last completed run's summary, for the
      source card's crawl cell and `Run` row. Polled (3 s active / 30 s
      idle, visibility-gated), hence `exempt:noise`.
  GET /api/admin/sharepoint/connections/{id}/extraction/runs
      A2 — the run-history drawer's rows.
  GET /api/admin/sharepoint/connections/{id}/extraction/runs/{run_id}
      A3 — one run's stored report, usage and (capped) skip list.
  GET /api/admin/sharepoint/connections/{id}/extraction/config
      A5 — the effective extraction configuration with an ORIGIN and a lock
      state per leaf, plus the per-scope rows. Cataloged (not exempt): it
      discloses credential env-var NAMES and the per-scope audience mapping.
  POST /api/admin/sharepoint/connections/{id}/extraction/stop
      Cooperative stop (owner-frustration fix, 2026-09-01: "I can't stop
      it") — sets ``config.extraction.stop_requested_at`` on the connection
      row (``connectors.sharepoint.crawler.request_stop``), which the crawl
      itself polls at the same quiescent points its timeout already checks.
      Works on BOTH app-state backends (``source_connections``/
      ``config_patch`` predate the A3 Postgres-only ratchet) — unlike the
      rest of this module, it is never gated behind ``extraction_runs``.
      Always ``202`` once the connection exists: a stop requested while
      nothing is visibly running simply waits for the next run to consume
      (and clear) it.
  POST /api/admin/sharepoint/anonymization/preview
      Run this instance's REAL anonymizer over a pasted sample and return
      what it would redact — the answer to "what will a crawl over ten
      thousand documents do to mine?" that no amount of configuration
      documentation can give. Cataloged; the sample text itself is never
      logged and never stored.
  GET /api/admin/sharepoint/connections/{id}/extraction/completeness
      A6 — "did we really get everything?" (TCRD-296 B.9): Graph Search's
      own document count per scope (and, for a single whole-drive scope,
      per top-level folder) vs. what actually landed in the corpus, with
      the crawl's own failed/empty/skipped/oversize reasons applied before
      calling a gap unexplained. All the math is in ``connectors.sharepoint.
      completeness`` — see that module's docstring. Cataloged (not exempt),
      same reasoning as ``…/split-plan``: it discloses folder names and
      per-scope/per-folder document counts, never document content.

**PG-only, and honest about it.** ``extraction_runs`` is a post-A3 table, so
resolving its repository on a DuckDB-backed instance raises the typed
``RequiresPostgresBackend``, which the app-wide handler in ``app/main.py``
turns into a clean ``501 requires_postgres_backend``. These handlers let it
surface rather than improvising an empty-but-healthy-looking answer — the
card stops polling on a 501 and says why (design §4.4). ``…/extraction/config``,
``…/extraction/stop`` and ``…/extraction/completeness`` read/write no run rows
and therefore answer on BOTH backends: configuration, the stop signal, and the
completeness check (crawl state + ``corpus_files`` + the job queue, none of
them ``extraction_runs``) are all knowable without a database that can show
run history.

**Liveness is DERIVED, never trusted.** A SIGKILLed worker finalizes
nothing, so a row can say ``running`` forever. ``status`` therefore reports
``stalled`` for a run whose last checkpoint is older than
``extraction.stall_after_s`` (:func:`_stall_after_s`, default 900s — and
says how old), and consults the run's ``jobs`` row when one is known.
Nothing here renders an unbounded "running" pulse. The fleet dashboard's own
``stuck`` flag (below) uses the SAME threshold — a run flagged ``stalled``
here and ``stuck`` there is one rule, read twice, never two thresholds that
can quietly disagree (2026-09-03 gap: they used to be independent, so a row
could show a calm "running" badge right next to a red "Stuck?" tag).

**No fraction, no bar, no ETA.** The crawl enumerates and processes in
lockstep per 200-row delta page, so "files seen" and "files done" are equal
at every checkpoint — a percentage over that is arithmetic dressed up as
knowledge — and ``files_per_s`` counts only new+changed documents, so an ETA
derived from it is wrong by construction on any run with a real `unchanged`
share. This endpoint returns ABSOLUTE counters and elapsed time only.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.access import require_admin
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sharepoint", tags=["admin"])

#: The two worker-queue lanes an extraction run actually enqueues into —
#: `trigger_extraction`/`retry_empty_extraction` (this module's sibling,
#: `app/api/admin_sharepoint.py`) enqueue `corpus-extraction`; the
#: standalone facts trigger enqueues `sharepoint-facts-extraction`. Read by
#: :func:`fleet_extraction_runs` for the ``jobs`` lane-starvation strip. A
#: literal tuple, deliberately NOT derived from
#: `app.worker.registry.JOB_MAX_ATTEMPTS_BY_KIND` — that dict's membership
#: answers a different question (crash-recovery lease budget) that happens
#: to share today's two kinds; a future addition there for lease-budget
#: reasons alone must not silently start (or stop) appearing here too.
_EXTRACTION_JOB_KINDS: Tuple[str, ...] = ("corpus-extraction", "sharepoint-facts-extraction")

#: How far back the fleet endpoint's own in-memory rate sampler looks when
#: deriving files/min. NOT read from stored history — ``extraction_runs``
#: keeps only the LATEST checkpoint per run, never a series — this is a
#: series the endpoint builds itself across repeated polls (the fleet page
#: polls every 5s while a run is active, which is what makes "consecutive
#: checkpoints" a meaningful phrase here).
_RATE_WINDOW_S = 900

#: Safety cap on samples kept per run id. At one new sample roughly every
#: 5s of polling this is ~15x the window — headroom for a slower poller
#: (or several browser tabs) to still land two samples inside it, without
#: letting a run polled for 20 hours grow its series without bound.
_RATE_SAMPLES_CAP = 400

#: Process-local, best-effort: a restart loses the series and the next call
#: simply falls back to the since-``started_at`` average (see
#: :func:`_files_per_min`) until two fresh samples land. Keyed by run id so
#: a new run for the same connection starts its own series rather than
#: inheriting the previous run's rate.
_rate_samples_lock = threading.Lock()
_rate_samples: Dict[str, "Deque[Tuple[float, int]]"] = {}


#: Default for :func:`_stall_after_s` — how stale a ``running`` run's last
#: checkpoint may be before the UI is told to call it ``stalled``, absent an
#: ``extraction.stall_after_s`` override. The crawl checkpoints once per
#: 200-row delta page; a page that downloads and converts 200 documents can
#: legitimately take several minutes, so this is deliberately generous —
#: several times the worst plausible cadence. Being late to say "stalled"
#: costs an admin a little patience; being early costs them trust in every
#: other number here. Admin-editable (``/admin/server-config`` → Extraction
#: → Stall threshold) since a fleet whose checkpoint cadence legitimately
#: runs longer (a slow tenant, a large-file-heavy scope) needs a looser
#: tripwire without an instance.yaml edit + restart.
_STALL_AFTER_S = 900


def _stall_after_s() -> int:
    """The EFFECTIVE stall threshold — ``extraction.stall_after_s`` if set,
    else :data:`_STALL_AFTER_S`. Read fresh on every call (no restart
    needed, same posture as every other ``extraction.*`` leaf) rather than
    cached, since a config change should be visible on the very next poll.

    A malformed or out-of-range value (a hand-edited YAML, not something
    ``/admin/server-config``'s own validation would ever write) falls back
    to the default rather than raising — this function backs a READ path
    that must never 500 an operator out of the one screen that would show
    them the misconfiguration.
    """
    from app.instance_config import get_value

    value = get_value("extraction", "stall_after_s", default=_STALL_AFTER_S)
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return _STALL_AFTER_S
    return resolved if resolved > 0 else _STALL_AFTER_S


#: Run outcomes in SEVERITY order. A crashed run is both "did not finish"
#: and "broke"; the more severe word wins, always, so a crash can never be
#: softened into the benign `interrupted` (with its reassuring "the next run
#: resumes" copy). `stalled` is derived at read time and never stored.
OUTCOME_PRECEDENCE = ("failed", "stalled", "interrupted", "done", "running")

#: Stop reasons (``CrawlStats.report()['interrupted_reason']``) whose run is
#: resumable BY CONSTRUCTION: the crawl persisted its deltaLinks/cTags on the
#: way out, so the next run skips what this one already ingested.
#:
#: This is deliberately keyed on the REASON, not on the outcome word. A
#: timeout, a tenant-throttle abort, and an admin-requested stop all finalize
#: as ``failed`` — correctly, because the job did not finish its corpus and
#: an operator should see it — yet all three cost re-work rather than
#: coverage. Gating the "next run resumes" copy on ``outcome ==
#: "interrupted"`` withheld it from exactly the cases that have earned it,
#: which is how an operator ends up re-running a four-hour crawl out of doubt.
#:
#: The crawl's own vocabulary is ``"timeout"`` | ``"stopped"`` |
#: ``"throttled"`` | ``"error"`` | ``None``, classified in one place on its
#: side (``_STOP_REASONS`` / ``_stop_reason()``): the named values are
#: exactly the stops that leave consistent state on disk. ``"abandoned"``
#: is the one member of this set the crawl process itself never sets — it
#: is written by ``ExtractionRunsPgRepository.abandon_stale_running``, from
#: an ENTIRELY different (later) process, when a NEW run for the same
#: connection finds a still-``running`` row left behind by a worker that
#: died outright (a native crash, a killed process) — resumable for the
#: exact same reason a self-detected stop is: the per-item cTag write only
#: ever happens after a durable ingest, so a dead run's persisted state is
#: never ahead of what it actually finished. ``"cancelled"`` is written by
#: THIS module's own ``POST …/extraction/runs/{run_id}/cancel`` — an
#: admin-forced close, not a crawl-detected stop — for the exact same
#: reason ``"abandoned"`` qualifies: the per-item cTag write only ever
#: happens after a durable ingest, so whatever the run's last checkpoint
#: recorded is real regardless of how the run ended. ``"error"`` is
#: deliberately absent from the set below, and an unknown reason claims
#: nothing — a new stop has to be vouched for here explicitly before this
#: surface will promise anything about it.
RESUMABLE_STOP_REASONS = frozenset({"timeout", "throttled", "stopped", "abandoned", "cancelled"})


def _sharepoint_connection_or_404(connection_id: str) -> Dict[str, Any]:
    """The connection row, or a 404 — resolved BEFORE any PG-only repo, so
    an unknown id is a 404 on every backend rather than a 501."""
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(connection_id)
    if row is None or row.get("source_type") != "sharepoint":
        raise HTTPException(status_code=404, detail="connection_not_found")
    return row


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_s(value: Any, *, now: Optional[datetime] = None) -> Optional[float]:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    now = now or datetime.now(timezone.utc)
    return round(max((now - parsed).total_seconds(), 0.0), 1)


def _job_status(job_id: Optional[str]) -> Optional[str]:
    """This run's job status, when the run knows its job id.

    Best-effort by construction: a run triggered outside the worker (a test,
    a manual payload) may still have no ``job_id`` — the worker's own
    dispatcher merges the claimed job's id in
    (``app/worker/kinds.py::_payload_for_handler``), but nothing forces every
    caller of ``run_builtin_crawl`` through it — and a lookup failure is a
    missing signal, not an error: the checkpoint-age fallback below still
    answers.
    """
    if not job_id:
        return None
    try:
        from src.repositories import jobs_repo

        job = jobs_repo().get(job_id)
    except Exception as exc:  # noqa: BLE001 — a liveness hint, never a 500
        logger.debug("extraction status: job lookup failed for %s: %s", job_id, exc)
        return None
    return str(job.get("status")) if job else None


def _derived_outcome(run: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """``{outcome, stored_status, stale_s, evidence}`` for one run row.

    The stored status is never overwritten in the database — a worker that
    was killed cannot write anything, which is exactly why the *reader* has
    to do this work. ``evidence`` names why the answer differs from the
    stored value, so the UI can say WHY rather than just asserting.
    """
    stored = str(run.get("status") or "running")
    if stored != "running":
        return {"outcome": stored, "stored_status": stored, "stale_s": None, "evidence": None}

    job_status = _job_status(run.get("job_id"))
    if job_status in ("failed", "cancelled", "canceled"):
        return {
            "outcome": "failed",
            "stored_status": stored,
            "stale_s": _age_s(run.get("checkpoint_at"), now=now),
            "evidence": f"the job that owned this run ended as {job_status}, but the run was never finalized",
        }

    stale_s = _age_s(run.get("checkpoint_at"), now=now)
    if stale_s is not None and stale_s > _stall_after_s():
        return {
            "outcome": "stalled",
            "stored_status": stored,
            "stale_s": stale_s,
            "evidence": (
                f"no checkpoint for {int(stale_s)}s — a worker killed outright finalizes nothing, "
                "so this row may be a leftover rather than live work"
            ),
        }
    return {"outcome": "running", "stored_status": stored, "stale_s": stale_s, "evidence": None}


def _is_resumable(run: Dict[str, Any], report: Dict[str, Any]) -> bool:
    """Whether the next run demonstrably picks up where this one stopped.

    Derived here rather than in the template for the same reason liveness is:
    it is a RULE about what Agnes may claim, not a rendering choice, and a
    rule that lives in one testable place cannot be re-derived differently
    by a second caller.

    Deliberately conservative. A cancellation and a state-persisting stop
    reason both qualify; a crash does not, because nothing is known about
    how far the crawl state got before it died — and "your work is safe" is
    precisely the sentence that must never be guessed.
    """
    if str(run.get("status") or "") == "interrupted":
        return True
    reason = str(report.get("interrupted_reason") or "").strip().lower()
    return reason in RESUMABLE_STOP_REASONS


def _shard_expected_by_key(connection_id: str) -> Dict[str, int]:
    """Every persisted shard's own ``expected`` (plan) document count, keyed
    by the SAME ``shard_key`` a child's own ``extraction_runs.shard_key``
    column carries (``connectors.sharepoint.crawler._run_shard_crawl_async``:
    ``",".join(t.state_key for t in targets)``) — read straight off this
    connection's ``crawl`` state row (``state["shard_plan"]["shards"]``,
    written once by ``connectors.sharepoint.crawler._enqueue_shard_plan``).

    ``expected`` is never stored on the run row itself (it is a PLANNING
    fact, not something the crawl measures) — this is the only place it can
    be read back from. Best-effort and read-only: a missing state row, a
    connection re-planned since an OLD parent's children were created (a
    resync always re-plans fresh — design §4.1), or any repo hiccup simply
    yields no match for the affected keys, never a raised error on this
    observability path.
    """
    from src.repositories import sharepoint_state_repo

    try:
        state = sharepoint_state_repo().get(connection_id, "crawl") or {}
    except Exception as exc:  # noqa: BLE001 — best-effort, never load-bearing
        logger.debug("shard rollup: could not read shard_plan state for %s: %s", connection_id, exc)
        return {}
    plan = state.get("shard_plan") or {}
    out: Dict[str, int] = {}
    for shard in plan.get("shards") or []:
        targets = shard.get("targets") or []
        key = ",".join(str(t.get("state_key")) for t in targets if t.get("state_key"))
        if key:
            out[key] = int(shard.get("expected") or 0)
    return out


def _rollup_children(
    parent: Dict[str, Any],
    children: List[Dict[str, Any]],
    *,
    expected_by_key: Optional[Dict[str, int]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The shard-aware additive keys a PARENT (planner) run's own
    projection gains once its CHILD rows are known (2026-09-03 auto-
    parallel-crawl design §4.7, plan Task 8): ``expected_documents``,
    ``seen_documents``, ``shards[]`` — one row per child, in the SAME order
    ``children`` was given (``ExtractionRunsPgRepository.children_for``'s
    own ``ORDER BY parent_run_id, shard_key``), each ``{index, label,
    outcome, files_done, files_seen, expected, checkpoint_at, error,
    stuck}``.

    Pure: never touches a repository itself — ``expected_by_key``
    (:func:`_shard_expected_by_key`) is the caller's job, so this function
    stays unit-testable without a database.

    ``seen_documents`` sums each child's own ``new + changed + unchanged +
    filtered_by_age`` (the completeness definition design §4.7 states,
    "seen = new+changed+unchanged (+filtered_by_age)") off its live
    report/progress — never ``files_seen``, which also counts items culled
    before that classification runs, and is a placeholder (0) on the
    PARENT's own row until finalize regardless (design §4.3: only the
    child's own row is checkpointed while sharding is in progress).

    ``expected_documents`` is the sum of every shard's own ``expected``
    ONLY when every shard resolved one — a partial sum would silently
    understate the site's real target, which is worse than admitting the
    total is unknown (``None``, never a lowball number).

    ``stuck`` on each shard row is the SAME rule the parent's/fleet's own
    ``stuck`` flag uses (``outcome == "stalled"``, :func:`_derived_outcome`)
    — one rule, read per shard here too, so a dead shard is visible even
    while sibling shards keep the parent's own bumped ``checkpoint_at``
    looking fresh.
    """
    expected_by_key = expected_by_key or {}
    shards_out: List[Dict[str, Any]] = []
    seen_documents = 0
    for index, child in enumerate(children, start=1):
        child_out = _run_out(child, now=now)
        report = child.get("report") or {}
        progress = child.get("progress") or {}
        live = report or progress
        seen_documents += sum(
            int(live.get(k) or 0) for k in ("new", "changed", "unchanged", "renamed", "filtered_by_age")
        )
        shards_out.append(
            {
                "index": index,
                "label": child.get("shard_label"),
                "outcome": child_out["outcome"],
                "files_done": child_out["files_done"],
                "files_seen": child_out["files_seen"],
                "expected": expected_by_key.get(str(child.get("shard_key") or "")),
                "checkpoint_at": child.get("checkpoint_at"),
                "error": child_out["error"],
                "stuck": child_out["outcome"] == "stalled",
            }
        )

    expected_documents: Optional[int] = None
    if shards_out:
        knowns = [s["expected"] for s in shards_out]
        if all(v is not None for v in knowns):
            expected_documents = sum(knowns)

    return {
        "expected_documents": expected_documents,
        "seen_documents": seen_documents if shards_out else None,
        "shards": shards_out,
    }


def _run_out(
    run: Dict[str, Any], *, now: Optional[datetime] = None, children: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """One run, in the shape both the history drawer and the status endpoint
    render. Absolute counters only — no fraction, no percentage, no ETA.

    ``mode``/``shards_total``/``shards_done`` are additive keys read
    straight off the row (2026-09-03 auto-parallel-crawl design §4.7):
    ``mode`` is ``"sharded"`` exactly when ``shards_total`` is not ``None``
    — a PARENT (planner) row — else ``"inline"``, the ordinary crawl every
    run before this design, and every shard CHILD's own row, both are.
    ``expected_documents``/``seen_documents``/``shards`` stay ``None``
    unless the caller passes ``children`` (this run's own child rows, from
    :meth:`ExtractionRunsPgRepository.children_for`) — callers that have not
    fetched them (most of this module's call sites, for a non-sharded run)
    pay nothing for a rollup that has nothing to roll up.
    """
    report = run.get("report") or {}
    progress = run.get("progress") or {}
    live = report or progress
    outcome = _derived_outcome(run, now=now)
    skips = run.get("skips") or {}
    shards_total = run.get("shards_total")
    rollup = None
    if children is not None:
        expected_by_key = _shard_expected_by_key(str(run.get("connection_id") or ""))
        rollup = _rollup_children(run, children, expected_by_key=expected_by_key, now=now)
    return {
        "id": run.get("id"),
        "job_id": run.get("job_id"),
        # The run row's own phase column — `"crawl"` while the crawl is
        # walking the corpus, `"facts"` once the LLM stage takes over
        # (`connectors.sharepoint.crawler._RunRecorder.checkpoint_facts`).
        # The card's per-phase copy keys off `activity.phase` below (it
        # rides the SAME checkpoint and is already what a finished run
        # collapses to `None`); this is the raw source of truth for a
        # caller that wants it without unpacking `activity`.
        "phase": run.get("phase"),
        "outcome": outcome["outcome"],
        "stored_status": outcome["stored_status"],
        "stale_s": outcome["stale_s"],
        "liveness_note": outcome["evidence"],
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        # What the card's "as of" caption prints. NEVER re-stamped at read
        # time: this is when the numbers below were last true.
        "checkpoint_at": run.get("checkpoint_at"),
        "duration_s": report.get("duration_s"),
        "elapsed_s": progress.get("elapsed_s"),
        "files_done": run.get("files_done") or 0,
        "files_seen": run.get("files_seen") or 0,
        "enumeration_done": bool(run.get("enumeration_done")),
        "new": live.get("new"),
        "changed": live.get("changed"),
        "unchanged": live.get("unchanged"),
        # A rename/move within the same drive: content unchanged (same
        # cTag/eTag), only `corpus_files.path`/`filename` moved — a subset
        # of what would otherwise be `unchanged` (D.18). See
        # `connectors.sharepoint.crawler._Ingestor.rename`.
        "renamed": live.get("renamed"),
        "deleted": live.get("deleted"),
        "bytes_downloaded": live.get("bytes_downloaded"),
        "bytes_downloaded_human": live.get("bytes_downloaded_human"),
        "http_429": live.get("http_429"),
        "throttle_wait_s": live.get("throttle_wait_s"),
        "errors": live.get("errors"),
        # A file no conversion backend even attempts (video/audio with no
        # usable codec path, Power BI, OneNote, ...) — never an error, so it
        # is a separate counter (`CrawlStats.skipped_unsupported`), not
        # folded into `errors` above. The itemized list rides only in
        # `report.skipped_items` (the full-report endpoint), same as
        # `failed_items`/`errors_detail`.
        "skipped_unsupported": live.get("skipped_unsupported"),
        # Skipped WITHOUT a download because the failure history says this
        # document is doomed — see `CrawlStats.skipped_doomed` (2026-09-04
        # finding #66 item 2). Absent from a live `progress` payload, same
        # as `skipped_unsupported` above — only a finished run's `report`
        # carries it.
        "skipped_doomed": live.get("skipped_doomed"),
        # `extraction.crawl.min_modified` age filter — see `CrawlStats.
        # filtered_by_age`/`age_unknown`. `live` already picks `report` (a
        # finished run) or `progress` (a running one), so this reads the
        # same counter an operator watching a LIVE run sees mid-crawl, not
        # only once the run is done.
        "filtered_by_age": live.get("filtered_by_age"),
        "age_unknown": live.get("age_unknown"),
        "skips_total": skips.get("total"),
        "skips_listed": skips.get("listed"),
        "oversize_files": (report.get("skipped_oversize") or {}).get("files", progress.get("oversize_files")),
        "error": run.get("error"),
        # WHY a run stopped early, when the crawl recorded a cause (today:
        # `"timeout"`, from the run-timeout ceiling). A run that ended short
        # should name its exit rather than leave an operator inferring one
        # from a duration — and this is the field that distinguishes "the
        # ceiling did its job" from "something broke".
        "interrupted_reason": report.get("interrupted_reason"),
        # Whether Agnes can ASSERT the next run picks up where this one
        # stopped. True for a cancellation, and for a stop reason whose exit
        # path persists state (see RESUMABLE_STOP_REASONS). False elsewhere —
        # including a crash, where nothing is known about how far the state
        # file got, and claiming resumability would be the reassuring kind
        # of unverified value this whole surface exists to avoid.
        "resumable": _is_resumable(run, report),
        # `{}` means NO tokens were spent, which is a different claim from
        # "$0.00" — the card must keep the two tellable apart (design §7.2).
        "usage": run.get("usage") or {},
        # What the crawl is touching RIGHT NOW (owner-frustration fix,
        # 2026-09-01: "I can't see what's happening in the extraction") —
        # `CrawlStats.activity_snapshot()`, riding the SAME checkpoint this
        # whole projection already reads. `live` collapses to `report` for a
        # FINISHED run (nothing is in flight any more, honestly `None` here)
        # and to `progress` for a running one — no separate lookup needed.
        "activity": live.get("activity"),
        # `{docs_done, docs_total}` for the facts phase specifically — absent
        # (`None`) while the crawl phase is running or for a run recorded
        # before this field existed. `docs_total` is the number of documents
        # SUBMITTED so far, not the final corpus size: like `files_seen`
        # above, it grows until the facts walk is exhausted (see
        # `connectors.sharepoint.facts_extraction.run_facts_extraction`'s
        # `on_progress` docstring) — never invented ahead of that.
        "facts_progress": live.get("facts"),
        # Scan OCR's own block (`src.ingest.scan_ocr.
        # triage_run_usage`, wired through the crawl's `report["scan_ocr"]`)
        # — triage decision counters, plus, once a permanent provider
        # refusal has fired this run, `disabled_reason`/`provider_error`
        # naming why scan OCR paused itself. Absent (`None`) when the
        # switch is off, nothing has been previewed, and no refusal has
        # fired — never invented ahead of the crawl reporting it. `live`
        # again collapses to `report`/`progress` the same way every other
        # field on this projection does.
        "scan_ocr": live.get("scan_ocr"),
        # ``{folders_done, folders_total}`` while `phase == "planning"`
        # (2026-09-04 finding #65 item 3) — absent otherwise, same
        # "layered onto `progress`, never invented ahead of it" contract as
        # `facts_progress` above. What lets the fleet view/source card say
        # "planning k/N folders" instead of showing nothing for the whole
        # planning window.
        "planning_progress": live.get("planning"),
        # 2026-09-03 auto-parallel-crawl design §4.7 — additive, present on
        # EVERY run: "sharded" for a PARENT (planner) row, "inline" for an
        # ordinary crawl and for a shard CHILD's own row alike (neither is
        # itself sharded).
        "mode": "sharded" if shards_total is not None else "inline",
        "shards_total": shards_total,
        "shards_done": run.get("shards_done"),
        # `None` unless the caller passed `children=` — see this function's
        # own docstring.
        "expected_documents": rollup["expected_documents"] if rollup else None,
        "seen_documents": rollup["seen_documents"] if rollup else None,
        "shards": rollup["shards"] if rollup else None,
    }


# ---------------------------------------------------------------------------
# Fleet view (`GET /extraction/runs`, `/admin/extraction`) — one row per
# SharePoint connection, for an operator running several crawls at once.
# ---------------------------------------------------------------------------


def _files_per_min(run: Dict[str, Any]) -> Optional[float]:
    """Files/min over :data:`_RATE_WINDOW_S`, derived from consecutive
    checkpoints THIS PROCESS has observed for this run.

    ``extraction_runs`` stores only the LATEST checkpoint, never a history,
    so there is no series to read back — this endpoint builds its own by
    recording one sample per distinct ``checkpoint_at`` it sees across
    repeated polls (the fleet page polls every 5s while a run is active,
    which is what makes "consecutive checkpoints" a real signal here rather
    than a single point).

    Falls back to the run's average rate since ``started_at`` when the
    window holds only one sample — the very first observation of this run,
    or a fresh process that lost its in-memory series. Returns ``None`` when
    neither is computable (no checkpoint yet, or zero files done).
    """
    run_id = run.get("id")
    checkpoint_at = _parse_ts(run.get("checkpoint_at"))
    files_done = int(run.get("files_done") or 0)
    if not run_id or checkpoint_at is None:
        return None

    ts = checkpoint_at.timestamp()
    with _rate_samples_lock:
        series = _rate_samples.setdefault(str(run_id), deque(maxlen=_RATE_SAMPLES_CAP))
        if not series or series[-1][0] != ts:
            series.append((ts, files_done))
        # Evict samples older than the window, relative to the NEWEST one —
        # `checkpoint_at` is "when this was last true", never wall-clock now.
        cutoff = ts - _RATE_WINDOW_S
        while len(series) > 1 and series[0][0] < cutoff:
            series.popleft()
        oldest_ts, oldest_done = series[0]
        newest_ts, newest_done = series[-1]

    if newest_ts > oldest_ts and newest_done >= oldest_done:
        elapsed_min = (newest_ts - oldest_ts) / 60.0
        if elapsed_min > 0:
            return round((newest_done - oldest_done) / elapsed_min, 2)

    started_at = _parse_ts(run.get("started_at"))
    if started_at is None or files_done <= 0:
        return None
    elapsed_min = max((checkpoint_at - started_at).total_seconds(), 1.0) / 60.0
    return round(files_done / elapsed_min, 2)


def _run_total_cost_usd(run: Optional[Dict[str, Any]]) -> float:
    """Every stage's own priced cost, summed. ``usage`` is keyed by stage
    (``ner`` / ``ocr`` / ``facts``), each carrying its OWN
    ``estimated_cost_usd`` (see ``connectors.sharepoint.crawler.
    _detector_usage`` / ``_ocr_run_usage`` and ``connectors.sharepoint.
    facts_extraction._Report.render`` — the one place per stage a token
    count becomes USD). A stage absent from ``usage`` spent nothing and
    contributes 0, never an invented estimate. Written once, at
    ``finish()`` — a still-``running`` run's cost is genuinely unknown
    until then, not zero.
    """
    if not run:
        return 0.0
    usage = run.get("usage") or {}
    total = 0.0
    for stage in usage.values():
        if isinstance(stage, dict):
            total += float(stage.get("estimated_cost_usd") or 0)
    return total


#: The empty facts shape — a connection whose latest run never reached the
#: facts phase (or has no run at all) renders every field ``None``, never
#: ``0``: "0 documents done" and "the facts phase hasn't started" are
#: different claims, and this surface never blurs them.
_EMPTY_FLEET_FACTS: Dict[str, Any] = {
    "phase_active": False,
    "docs_done": None,
    "docs_total": None,
    "docs_extracted": None,
    "docs_unchanged": None,
    "docs_skipped_tabular": None,
    "docs_skipped_no_text": None,
    "docs_skipped_not_indexed": None,
    "docs_skipped_garbled_text": None,
    "docs_skipped_too_large_tabular": None,
    "facts_failed": None,
    "facts_failed_reasons": None,
    # TCRD-296 C.12 — the pass's own single end-of-pass orphan sweep (see
    # `connectors.sharepoint.facts_extraction._Report.orphans_swept`'s
    # docstring for the live finding). `None` (not `0`) for a run that
    # never reached facts, same "unstarted vs genuinely zero" rule every
    # other field in this shape follows.
    "orphans_swept": None,
    "orphans_sweep_skipped": None,
    "usage": {},
    # TCRD-296 gap #61 — see `_fleet_facts`'s docstring for why these two
    # are connection-level, not read off the run row like everything else
    # in this shape.
    "facts_pending_documents": None,
    "facts_pass_running": False,
    # TCRD-296 gap #67 — every in-flight partition (or the single legacy
    # job) of this connection's own pass, plus the throughput/ETA line
    # ("3/4 passes running, 1,400 docs/h, ETA") the fleet view renders
    # from it. See `_facts_jobs_in_flight`/`_facts_throughput_and_eta`.
    "facts_jobs": [],
    "facts_passes_running": 0,
    "facts_passes_total": None,
    "facts_docs_per_hour": None,
    "facts_eta_seconds": None,
    # TCRD-296 synthesis F.25 — always overwritten by `_fleet_facts`'s own
    # final assignment; listed here purely so this dict documents the
    # complete shape of one row's `facts` object.
    "provider_limit": None,
}


def _active_provider_limit_conditions() -> List[Dict[str, Any]]:
    """Every currently-active ``provider_limit`` condition (TCRD-296
    synthesis F.25) — best-effort: a broken read here must never break the
    whole fleet view, only omit the banner. Delegates to
    ``connectors.sharepoint.facts_extraction`` (which already fails clean
    to ``[]`` on a DuckDB-backed instance), never
    ``extraction_conditions_repo()`` directly — same layering as every
    other cross-module read in this file.
    """
    try:
        from connectors.sharepoint.facts_extraction import active_provider_limit_conditions

        return active_provider_limit_conditions()
    except Exception:  # noqa: BLE001 — observability, never load-bearing
        return []


def _matching_provider_limit_condition(connection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The active ``provider_limit`` condition (if any) matching
    ``connection``'s own RESOLVED facts provider — the per-connection
    counterpart of ``_active_provider_limit_conditions``' fleet-wide list,
    used by the source card's status endpoint. Best-effort, same posture:
    a resolution failure means "nothing to report", never a broken card.
    """
    try:
        from connectors.sharepoint.facts_extraction import resolve_effective_provider

        effective_provider, _source = resolve_effective_provider(connection)
    except Exception:  # noqa: BLE001 — observability, never load-bearing
        return None
    for condition in _active_provider_limit_conditions():
        if condition.get("provider") == effective_provider:
            return condition
    return None


def _fleet_facts(
    run: Optional[Dict[str, Any]],
    connection_id: str,
    *,
    connection: Optional[Dict[str, Any]] = None,
    provider_limit: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The facts stage's own numbers for one connection's latest run — read
    off the SAME row the crawl side already reads, never a second
    per-connection query or a re-read of the per-document idempotency state
    file. Crawl and facts are literally the same run row: ``phase`` flips
    from ``"crawl"`` to ``"facts"`` mid-run
    (``connectors.sharepoint.crawler._RunRecorder.checkpoint_facts``), so
    there is nothing else to fetch.

    ``docs_done`` / ``docs_total`` come from ``progress.facts`` while the
    facts phase is live (the pass's own growing "submitted so far" counters
    — see :func:`run_facts_extraction`'s docstring for why ``docs_total`` is
    a lower bound, not a corpus size). Once the pass FINISHES,
    ``report.facts`` carries its outcome breakdown
    (``docs_extracted``/``docs_unchanged``/the ``skipped-*`` reasons/
    ``facts_failed``/``orphans_swept``/``orphans_sweep_skipped`` — the
    pass's own single end-of-pass sweep, TCRD-296 C.12) and ``docs_done``
    falls back to ``docs_extracted`` so a finished run still answers "how
    many did it do".

    ``facts_pending_documents``/``facts_pass_running`` are NOT read off
    ``run`` at all — a connection's outstanding backlog and whether a job
    is chasing it are properties of the CONNECTION, not of its latest
    crawl run (which may be long finished, or may never have reached the
    facts phase). Computed unconditionally, even for a connection with no
    ``run`` — the exact "three connections sat idle for hours" case
    TCRD-296 gap #61 reported, which a run-keyed field would have stayed
    blind to.

    ``provider_limit`` (TCRD-296 synthesis F.25) is the caller's own
    lookup — the active condition (if any) matching THIS connection's
    resolved facts provider, or ``None`` — passed in rather than resolved
    here so a fleet page rendering N connections looks the active
    conditions up ONCE, not once per row. The source card renders it as
    "paused: provider limit".

    ``facts_jobs``/``facts_passes_running``/``facts_passes_total``
    (TCRD-296 gap #67) are the same "properties of the CONNECTION, not
    the run" story as ``facts_pending_documents`` above, extended for a
    fanned-out pass: every currently in-flight partition
    (:func:`_facts_jobs_in_flight`), how many of them are actually
    ``running`` right now, and the generation's total partition count
    (``None`` when nothing is in flight — a finished/never-run connection
    has no "total" to report). ``facts_docs_per_hour``/``facts_eta_seconds``
    (:func:`_facts_throughput_and_eta`) need ``connection`` (the full row,
    not just its id) to resolve its own collections — omitted (``None``
    for both) when the caller has no connection dict handy.
    """
    out = dict(_EMPTY_FLEET_FACTS)
    if run:
        progress = run.get("progress") or {}
        report = run.get("report") or {}
        live_facts = progress.get("facts") or {}
        final_facts = report.get("facts") or {}
        out["phase_active"] = str(run.get("phase") or "") == "facts" and str(run.get("status") or "") == "running"
        if live_facts:
            out["docs_done"] = live_facts.get("docs_done")
            out["docs_total"] = live_facts.get("docs_total")
        if final_facts:
            out["docs_extracted"] = final_facts.get("docs_extracted")
            out["docs_unchanged"] = final_facts.get("docs_unchanged")
            out["docs_skipped_tabular"] = final_facts.get("docs_skipped_tabular")
            out["docs_skipped_no_text"] = final_facts.get("docs_skipped_no_text")
            out["docs_skipped_not_indexed"] = final_facts.get("docs_skipped_not_indexed")
            out["docs_skipped_garbled_text"] = final_facts.get("docs_skipped_garbled_text")
            out["docs_skipped_too_large_tabular"] = final_facts.get("docs_skipped_too_large_tabular")
            out["facts_failed"] = final_facts.get("facts_failed")
            out["facts_failed_reasons"] = final_facts.get("facts_failed_reasons")
            out["orphans_swept"] = final_facts.get("orphans_swept")
            out["orphans_sweep_skipped"] = final_facts.get("orphans_sweep_skipped")
            if out["docs_done"] is None:
                out["docs_done"] = final_facts.get("docs_extracted")
        # The priced usage for JUST this stage — see `_run_total_cost_usd`
        # for why it is only known once the run has finished.
        out["usage"] = (run.get("usage") or {}).get("facts") or {}
    pending = _facts_pending_documents(connection_id)
    out["facts_pending_documents"] = pending
    out["facts_pass_running"] = _facts_job_in_flight(connection_id) is not None
    jobs = _facts_jobs_in_flight(connection_id)
    out["facts_jobs"] = jobs
    out["facts_passes_running"] = sum(1 for job in jobs if job["status"] == "running")
    out["facts_passes_total"] = max((job["partition_count"] or 1 for job in jobs), default=None)
    if connection is not None:
        out.update(_facts_throughput_and_eta(connection, pending=pending))
    out["provider_limit"] = provider_limit
    return out


@router.get("/extraction/runs")
def fleet_extraction_runs(
    active: bool = Query(False, description="Only connections with a currently running run (the default scope)"),
    show_all: bool = Query(
        False, alias="all", description="Every SharePoint connection, running or not — wins over `active`"
    ),
    _user: dict = Depends(require_admin),
):
    """One row per SharePoint connection — the fleet dashboard for an
    operator running several crawls at once: is it on pace, is anything
    stuck, what is it costing.

    Default scope (and ``?active=1``) is connections with a run CURRENTLY
    ``running`` — an idle connection has nothing to say about "on pace" and
    its absence here is the honest answer, not an omission. ``?all=1``
    broadens to every SharePoint connection, each with its own latest run
    (``null`` if it has never run) — ``all`` wins if both are passed.

    Each row's ``run`` reuses :func:`_run_out` — the SAME projection the
    per-connection status/history endpoints render, so a fleet row and a
    source card can never disagree about one run — plus two fleet-only
    additions: ``files_per_min`` (:func:`_files_per_min`) and ``stuck``
    (``True`` exactly when that same ``run.outcome`` is ``"stalled"`` — ONE
    rule, read twice, so the fleet's "Stuck?" badge and the per-run outcome
    word can never disagree; before 2026-09-03 this used its own, tighter,
    independent threshold, which could show a calm "running" badge right
    next to a red "Stuck?" tag on the same row). ``facts`` is the facts
    stage's own numbers, read off the same row (:func:`_fleet_facts`).
    ``failed_items_count``/``empty_items_count`` are the SAME persisted-
    backlog counts ``extraction/status`` carries (:meth:`SharepointStatePg
    Repository.backlog_counts`) — what the table's own "Retry failed (N)"/
    "Retry empty (N)" buttons show, one cheap query per row. A ``stalled``
    row (or one still merely ``running``) can also be force-cancelled — see
    ``POST …/extraction/runs/{run_id}/cancel`` below.

    A SHARDED site's row is its PARENT run (2026-09-03 auto-parallel-crawl
    design §4.7) — a child never appears as its own fleet row
    (``list_latest_for_connections`` already filters ``parent_run_id IS
    NULL``). ``run.mode == "sharded"`` names it; ``run.shards_total``/
    ``run.shards_done`` come straight off the parent row, and
    ``run.shards[]``/``run.expected_documents``/``run.seen_documents`` are
    filled in from a SINGLE batched :meth:`ExtractionRunsPgRepository.
    children_for` call across every parent this page is about to render
    (never one round trip per sharded connection) — see
    :func:`_rollup_children`.

    PG-only, same as every other route in this module: ``extraction_runs``
    is a post-A3 table, so a DuckDB-backed instance gets the typed ``501``
    from ``extraction_runs_repo()`` via the app-wide handler in
    ``app/main.py`` — nothing here needs its own DuckDB fallback.

    ``jobs`` is ``{kind: {queued, running}}`` for :data:`_EXTRACTION_JOB_KINDS`
    — the extraction pipeline's own worker lanes — read in ONE grouped query
    off the (backend-agnostic) jobs table via
    :meth:`JobsPgRepository.counts_by_kind`, independent of ``active``/
    ``all``: lane starvation (a growing ``queued`` count with ``running``
    stuck at 0 — every worker slot busy elsewhere, or none configured for
    this lane) is exactly the fact an operator scanning ONLY the active
    scope would otherwise never see, since a starved connection's job has
    no ``extraction_runs`` row yet to show up as a table row at all.

    ``next_run_at`` (D.16) on each row is the same best-effort "when next
    swept" hint the crawl-config PATCH response and ``extraction/status``
    carry (:func:`_crawl_schedule_next_run_at`) — pure computation, no extra
    query per row.

    Plain ``def`` (not ``async def``, zero ``await``s below): blocking,
    synchronous SQLAlchemy I/O, so FastAPI dispatches it to the anyio thread
    pool rather than the single event loop (Tier-1 convention,
    ``tests/test_event_loop_offload_guard.py``).
    """
    from src.repositories import extraction_runs_repo, jobs_repo, sharepoint_state_repo, source_connections_repo

    connections = sorted(
        source_connections_repo().list(source_type="sharepoint"),
        key=lambda c: str(c.get("name") or c.get("id") or ""),
    )
    connection_ids = [str(c["id"]) for c in connections]
    running_only = not show_all
    repo = extraction_runs_repo()
    latest = repo.list_latest_for_connections(connection_ids, running_only=running_only)

    # Shard children (2026-09-03 auto-parallel-crawl design §4.7): ONE
    # batched `children_for` call for every PARENT (planner) row this page
    # is about to render, never one round trip per sharded connection —
    # the whole reason `children_for` takes a LIST of parent ids.
    parent_ids = [str(run["id"]) for run in latest.values() if run.get("shards_total") is not None]
    children_by_parent = repo.children_for(parent_ids) if parent_ids else {}

    # Fleet-level provider-refusal conditions (TCRD-296 synthesis F.25) —
    # ONE read for the whole page (never one query per connection), keyed
    # by provider so each connection's row can look up whether ITS
    # resolved facts provider is the one currently refusing.
    from connectors.sharepoint.facts_extraction import resolve_effective_provider

    conditions_by_provider = {str(c["provider"]): c for c in _active_provider_limit_conditions()}

    now = datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    totals: Dict[str, Any] = {
        "connections": 0,
        "active": 0,
        "stuck": 0,
        "files_done": 0,
        "files_seen": 0,
        "files_per_min": 0.0,
        "facts_docs_done": 0,
        "facts_docs_total": 0,
        "estimated_cost_usd": 0.0,
        # Standing fleet-wide count of documents skipped WITHOUT a download
        # this run because their failure history says they are doomed
        # (2026-09-04 finding #66 item 2) — summed across every row's own
        # `run.skipped_doomed`, same shape as `files_done`/`files_seen`.
        "skipped_doomed": 0,
    }
    for connection in connections:
        connection_id = str(connection["id"])
        run = latest.get(connection_id)
        if run is None and not show_all:
            # Nothing currently running for this connection — omitted from
            # the active scope entirely, not represented as a blank row.
            continue

        stored_status = str(run.get("status") or "") if run else ""
        children = children_by_parent.get(str(run.get("id"))) if run and run.get("shards_total") is not None else None
        run_out = _run_out(run, now=now, children=children) if run else None
        files_per_min = _files_per_min(run) if run else None
        checkpoint_age_s = _age_s(run.get("checkpoint_at"), now=now) if run else None
        # A shard-aware "is anything stuck": the parent's OWN derived
        # outcome (its checkpoint is bumped by every child, so it usually
        # stays fresh even when one shard died) OR any individual shard
        # flagged `stuck` by `_rollup_children` — one dead shard must be
        # visible even while its siblings keep working.
        stuck = bool(
            run_out
            and (run_out.get("outcome") == "stalled" or any(s.get("stuck") for s in (run_out.get("shards") or [])))
        )

        effective_provider, _provider_source = resolve_effective_provider(connection)
        facts = _fleet_facts(
            run, connection_id, connection=connection, provider_limit=conditions_by_provider.get(effective_provider)
        )
        cost = _run_total_cost_usd(run)
        backlog = sharepoint_state_repo().backlog_counts(connection_id, "crawl")

        totals["connections"] += 1
        if stored_status == "running":
            totals["active"] += 1
        if stuck:
            totals["stuck"] += 1
        if run_out:
            totals["files_done"] += int(run_out.get("files_done") or 0)
            totals["files_seen"] += int(run_out.get("files_seen") or 0)
            totals["skipped_doomed"] += int(run_out.get("skipped_doomed") or 0)
        if files_per_min:
            totals["files_per_min"] += files_per_min
        if facts.get("docs_done"):
            totals["facts_docs_done"] += int(facts["docs_done"] or 0)
        if facts.get("docs_total"):
            totals["facts_docs_total"] += int(facts["docs_total"] or 0)
        totals["estimated_cost_usd"] += cost

        rows.append(
            {
                "connection_id": connection_id,
                "connection_name": connection.get("name"),
                "run": run_out,
                "files_per_min": files_per_min,
                "checkpoint_age_s": checkpoint_age_s,
                "stuck": stuck,
                "facts": facts,
                "estimated_cost_usd": round(cost, 4),
                # Same persisted-backlog counts `extraction/status` carries —
                # what the fleet table's own "Retry failed (N)"/"Retry empty
                # (N)" buttons show, so an operator does not need to open a
                # source card just to see whether there is anything to retry.
                "failed_items_count": backlog["failed_items_count"],
                "empty_items_count": backlog["empty_items_count"],
                # D.16 — same best-effort display hint the crawl-config
                # PATCH response and `extraction/status` use
                # (`_crawl_schedule_next_run_at`), so an operator scanning
                # `?all=1` can see which idle connections are about to be
                # swept without opening each source card.
                "next_run_at": _crawl_schedule_next_run_at(connection, now=now),
            }
        )

    totals["files_per_min"] = round(totals["files_per_min"], 2)
    totals["estimated_cost_usd"] = round(totals["estimated_cost_usd"], 4)

    jobs = jobs_repo().counts_by_kind(list(_EXTRACTION_JOB_KINDS))

    return {
        "connections": rows,
        "totals": totals,
        "jobs": jobs,
        "as_of": now.isoformat(),
        # Fleet-level provider-refusal conditions (TCRD-296 synthesis
        # F.25) — additive: `[]` on every instance before this shipped,
        # and forever on a DuckDB-backed one (`extraction_conditions` is
        # PG-only, A3 ratchet; the whole rest of THIS route already is
        # too, so no extra guard is needed here). The fleet page renders
        # this as a banner and the crawl's own streamed trigger
        # (`crawler._enqueue_streamed_facts_pass`) is what actually stops
        # re-enqueueing while one is active — this list is the operator
        # SIGNAL, not the enforcement.
        "conditions": _active_provider_limit_conditions(),
    }


@router.post("/extraction/runs/{run_id}/cancel")
def cancel_extraction_run(
    run_id: str,
    user: dict = Depends(require_admin),
):
    """Force-close a run the cooperative Stop cannot reach — a crawl whose
    loop is genuinely stuck (no I/O yielding, never reaching a checkpoint)
    never observes ``config.extraction.stop_requested_at`` either, so the
    run stays ``running`` with an ever-extending lease until an operator
    intervenes by hand (the 2026-09-02 incident this endpoint answers: two
    manual SQL updates and a re-trigger to end one dead crawl).

    Cancel = stop + force-close, reusing rather than duplicating the
    cooperative path:

    1. :func:`connectors.sharepoint.crawler.request_stop` — the SAME signal
       the Stop button sets. If the crawl loop is merely slow (not truly
       stuck), it notices at its next checkpoint and exits cleanly on its
       own, exactly like a normal stop.
    2. The owning job (when known) is force-finalized to ``failed`` via
       ``JobsRepository.cancel``/``JobsPgRepository.cancel`` — an
       admin-initiated override that needs no lease token (the admin never
       claimed the job). Clearing the lease is what stops the worker's
       heartbeat loop on its own: the next ``heartbeat()`` call re-checks
       ``status = 'running'``, finds it false, returns ``False``, and
       ``app/worker/runtime.py``'s ``_heartbeat_loop`` stops extending —
       no separate mechanism needed on the worker side. A zombie handler
       thread may keep running a while longer (Python cannot force-kill a
       thread), but its eventual ``complete()``/``fail()`` call carries the
       now-stale lease token and is a guaranteed no-op against the state
       this call just wrote — the same reclaim-race guard every stale
       worker call already respects.
    3. The ``extraction_runs`` row is closed HERE, immediately, as
       ``interrupted`` with ``interrupted_reason: "cancelled"`` — never
       waiting on the crawl to notice, because a genuinely stuck loop might
       not. Whatever the last checkpoint recorded stays exactly what it
       recorded; only the outcome and the finish timestamp change. Resumable
       by the same rule every ``interrupted`` run is (:func:`_is_resumable`):
       the per-item state on disk is durable as of the last checkpoint
       regardless of how the run ended.

    ``404 run_not_found`` for an unknown run id. ``409 run_not_active`` when
    the run's STORED status is not ``running`` — a finished run (including
    one already cancelled) is not cancellable again; the caller sees the
    current state in the error body rather than a silent no-op. Returns
    ``{connection_id, ...}`` where the rest is the run's new projection
    (:func:`_run_out`), the same shape every other run read in this module
    returns, so the caller can repaint immediately without a second fetch.

    Handler writes its own audit row (more than the fallback middleware
    could say: the connection id, the job id, and whether a job was
    actually force-finalized vs. there being none to touch) — see
    ``sharepoint_extraction_run.cancel`` in ``src/audit_events.py``.
    """
    from src.repositories import extraction_runs_repo

    repo = extraction_runs_repo()
    run = repo.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run_not_found")
    stored_status = str(run.get("status") or "")
    if stored_status != "running":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "run_not_active",
                "message": f"this run is already {stored_status!r} — only a running run can be cancelled",
                "status": stored_status,
            },
        )

    connection_id = str(run.get("connection_id") or "")
    job_id = run.get("job_id")

    from connectors.sharepoint.crawler import request_stop

    stop_requested_at = request_stop(connection_id) if connection_id else None

    job_cancelled = False
    if job_id:
        from src.repositories import jobs_repo

        job_cancelled = jobs_repo().cancel(str(job_id), error="cancelled_by_admin")

    existing_report = dict(run.get("report") or {})
    existing_report["interrupted"] = True
    existing_report["interrupted_reason"] = "cancelled"
    repo.finish(
        run_id,
        status="interrupted",
        report=existing_report,
        usage=run.get("usage"),
        skips=run.get("skips"),
        error="cancelled by admin",
    )

    log_safe(
        user_id=user.get("id"),
        action="sharepoint_extraction_run.cancel",
        resource=f"extraction_run:{run_id}",
        params={
            "connection_id": connection_id,
            "job_id": job_id,
            "job_cancelled": job_cancelled,
            "stop_requested_at": stop_requested_at,
        },
    )

    return {"connection_id": connection_id, **_run_out(repo.get(run_id))}


#: The standalone facts pass's job kind and the statuses that mean "in
#: flight" — the same pair `POST …/facts-extract`'s idempotency dedup
#: reasons about (`app/api/admin_sharepoint.py::_facts_extraction_idempotency_key`).
_FACTS_JOB_KIND = "sharepoint-facts-extraction"
_FACTS_JOB_LIVE_STATUSES = ("running", "queued")


def _iso_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _facts_job_in_flight(connection_id: str) -> Optional[Dict[str, Any]]:
    """The queued-or-running ``sharepoint-facts-extraction`` job for this
    connection, projected to ``{id, status, created_at, started_at}`` — or
    ``None`` when there is none.

    The standalone facts pass is a JOB, not a crawl run: it opens no
    ``extraction_runs`` row (``connectors.sharepoint.facts_extraction
    .run_standalone_facts_extraction`` reads already-indexed documents and
    checkpoints nothing here), so the card's poll would otherwise be blind
    to it. Read off the job queue instead, matched on the SAME stable
    idempotency key the trigger dedups on — so "in flight" here means
    exactly what a second click's ``409 facts_extraction_already_running``
    means. ``running`` is checked before ``queued`` only for the projection;
    the dedup key guarantees at most one of the two exists. Backend-agnostic
    (the jobs table lives on both app-state backends), unlike the
    ``extraction_runs`` reads around it.
    """
    from app.api.admin_sharepoint import _facts_extraction_idempotency_key
    from src.repositories import jobs_repo

    # Ask the PRODUCER for the key rather than rebuilding its shape here:
    # `POST …/facts-extract` is the only thing that mints these jobs, so a
    # second copy of the format in this module is a silent-drift hazard —
    # the reader would return `None` forever and the card would go blind
    # with no symptom and no failing test. Pinned by
    # `tests/test_admin_extraction.py::TestFactsJobInFlight
    # ::test_the_lookup_follows_the_triggers_own_key_and_kind`.
    key = _facts_extraction_idempotency_key(connection_id)
    repo = jobs_repo()
    for status in _FACTS_JOB_LIVE_STATUSES:
        for job in repo.list(status=status, kind=_FACTS_JOB_KIND, limit=200):
            if job.get("idempotency_key") != key:
                continue
            return {
                "id": job["id"],
                "status": job["status"],
                "created_at": _iso_or_none(job.get("created_at")),
                "started_at": _iso_or_none(job.get("started_at")),
            }
    return None


#: How long a computed pending-documents count is reused before the next
#: poll recomputes it (TCRD-296 gap #72) — the source card polls
#: ``…/extraction/status`` every few seconds and the fleet view
#: (``…/extraction/runs``) polls it once PER ROW, so even after the O(N)
#: round-trip fix above, an admin with several large connections open still
#: reissues the same bounded query several times a second. Process-local
#: (not shared across worker processes) and best-effort, same trade-off as
#: the completeness cache above — a miss just recomputes, never a
#: correctness issue. Not configurable, deliberately (CLAUDE.md's "no
#: speculative config knobs"): 30s keeps the card and fleet row visibly
#: live while collapsing the redundant polls that made this a hot path.
_FACTS_PENDING_CACHE_TTL_S = 30
_facts_pending_cache_lock = threading.Lock()
_facts_pending_cache: Dict[str, Tuple[float, int]] = {}


def _facts_pending_documents(connection_id: str) -> int:
    """Thin, TTL-CACHED delegate to ``connectors.sharepoint.
    facts_extraction.count_pending_documents`` — see that function's
    docstring for the (cheap, no-document-text-read, single-query as of
    TCRD-296 gap #72) definition of "pending". A tiny wrapper rather than
    an inline import at each of this module's two call sites (the
    per-connection status endpoint and the fleet row), same reasoning as
    ``_facts_job_in_flight`` above.

    The returned count may lag the true value by up to
    :data:`_FACTS_PENDING_CACHE_TTL_S` — see that constant's docstring.
    Worker-side callers (``enqueue_facts_extraction_passes``,
    ``maybe_continue_pass``) call ``count_pending_documents`` directly and
    bypass this cache entirely, since a stale count there would mis-size a
    fan-out or a continuation decision, not just a status display.
    """
    with _facts_pending_cache_lock:
        cached = _facts_pending_cache.get(connection_id)
    if cached is not None:
        computed_at, value = cached
        if time.monotonic() - computed_at <= _FACTS_PENDING_CACHE_TTL_S:
            return value

    from connectors.sharepoint.facts_extraction import count_pending_documents

    value = count_pending_documents(connection_id)
    with _facts_pending_cache_lock:
        _facts_pending_cache[connection_id] = (time.monotonic(), value)
    return value


def _facts_jobs_in_flight(connection_id: str) -> List[Dict[str, Any]]:
    """Every queued/running ``sharepoint-facts-extraction`` job for this
    connection — ANY partition of a fanned-out pass (TCRD-296 gap #67), or
    the single legacy job — each projected like :func:`_facts_job_in_flight`
    plus ``partition_index``/``partition_count`` (both ``None`` for a
    legacy, un-partitioned job). Sorted by partition index (legacy/``None``
    first) so the fleet/status payload always lists partitions in order.

    Matched by PAYLOAD (``connection_id``), not by a single idempotency
    key — a partitioned pass mints a DIFFERENT key per partition, so
    :func:`_facts_job_in_flight`'s exact-key lookup only ever finds ONE of
    several live partitions. Same payload-scan pattern
    ``connectors.sharepoint.crawler._standalone_facts_pass_in_flight``
    already uses for the identical reason. ``_facts_job_in_flight`` itself
    is left unchanged (still used for the existing, singular ``facts_job``
    field) — this is an ADDITIVE reader, not a replacement.
    """
    from src.repositories import jobs_repo

    repo = jobs_repo()
    out: List[Dict[str, Any]] = []
    for status in _FACTS_JOB_LIVE_STATUSES:
        for job in repo.list(status=status, kind=_FACTS_JOB_KIND, limit=200):
            payload = job.get("payload_json") or {}
            if str(payload.get("connection_id")) != str(connection_id):
                continue
            partition = payload.get("partition") or {}
            out.append(
                {
                    "id": job["id"],
                    "status": job["status"],
                    "created_at": _iso_or_none(job.get("created_at")),
                    "started_at": _iso_or_none(job.get("started_at")),
                    "partition_index": partition.get("index"),
                    "partition_count": partition.get("count"),
                }
            )
    out.sort(key=lambda j: (j["partition_index"] is None, j["partition_index"] or 0))
    return out


#: Trailing window (minutes) :func:`_facts_throughput_and_eta` sums
#: ``facts_ingest_runs.documents_seen`` over — short enough that a fleet
#: view reflects the CURRENT pace of a multi-partition pass (not a
#: multi-hour average that would hide a stalled partition), long enough to
#: smooth over one connection's own batch-flush cadence
#: (``DEFAULT_BATCH_DOCUMENTS`` = 25 documents per flush).
_FACTS_THROUGHPUT_WINDOW_MINUTES = 10


def _facts_throughput_and_eta(connection: Dict[str, Any], *, pending: int) -> Dict[str, Optional[float]]:
    """``{"facts_docs_per_hour", "facts_eta_seconds"}`` — the fleet view's
    "3/4 passes running, 1,400 docs/h, ETA" line (TCRD-296 gap #67).

    Throughput is documents ingested in the trailing
    :data:`_FACTS_THROUGHPUT_WINDOW_MINUTES` minutes, summed across every
    ``facts_ingest_runs`` row (any partition, any run) whose ``corpus_ids``
    overlaps this connection's OWN collections
    (``connectors.sharepoint.facts_extraction.collection_ids_for``) —
    connection-scoped the same way ``_sharepoint_pipeline_cell`` already
    resolves a connection's crawl/extract/facts counts, since a run report
    itself carries no connection id (see
    :meth:`~src.repositories.facts_ingest_runs_pg.FactsIngestRunsPgRepository
    .documents_done_since`'s docstring). ``None`` for both fields when
    there is no throughput signal yet — no recent run, an unresolvable
    connection, or a DuckDB-backed instance (``facts_ingest_runs_repo()``
    is PG-only, A3 ratchet) — never a fabricated ``0``/ETA. ``facts_eta_seconds``
    is additionally ``None`` whenever ``pending`` is already ``0`` (nothing
    left to estimate), even though throughput itself may still be
    reported (a pass finishing its LAST few documents).

    Best-effort: any failure resolving either number is swallowed and
    answers "no signal" — this is observability, never load-bearing.
    """
    try:
        from connectors.sharepoint.facts_extraction import collection_ids_for
        from src.repositories import facts_ingest_runs_repo

        corpus_ids = collection_ids_for(connection)
        if not corpus_ids:
            return {"facts_docs_per_hour": None, "facts_eta_seconds": None}
        since = datetime.now(timezone.utc) - timedelta(minutes=_FACTS_THROUGHPUT_WINDOW_MINUTES)
        documents = facts_ingest_runs_repo().documents_done_since(corpus_ids, since)
    except Exception:  # noqa: BLE001 — observability, never load-bearing
        return {"facts_docs_per_hour": None, "facts_eta_seconds": None}
    if documents <= 0:
        return {"facts_docs_per_hour": None, "facts_eta_seconds": None}
    docs_per_hour = documents * (60.0 / _FACTS_THROUGHPUT_WINDOW_MINUTES)
    eta_seconds = round((pending / docs_per_hour) * 3600.0) if pending > 0 else None
    return {"facts_docs_per_hour": round(docs_per_hour, 1), "facts_eta_seconds": eta_seconds}


@router.get("/connections/{connection_id}/extraction/status")
def extraction_status(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """A1 — the source card's live crawl cell and `Run` row.

    ``running`` is the newest un-finalized run WITH its liveness derived
    (see :func:`_derived_outcome`); ``last_completed`` is the newest run
    that actually ended. Both may be null, and null is rendered as "never
    run", never as zeros.

    ``as_of`` is this response's own read time — distinct from each run's
    ``checkpoint_at``, which is when its numbers were last true. The card
    prints the run's, not this one, wherever it shows a counter.

    ``next_run_at`` (D.16) is when this connection's own crawl cadence
    (``config.extraction.crawl.schedule`` — off / follow-the-instance-
    cadence / its own interval) next fires the scheduled sweep, best-effort
    and DISPLAY ONLY (:func:`_crawl_schedule_next_run_at`) — ``None`` when
    this connection is ``off`` or the instance-wide sweep has no cadence
    configured at all.

    ``facts_job`` is the queued/running standalone facts pass for this
    connection (:func:`_facts_job_in_flight`) or ``null`` — a job, never a
    run: it is what lets the card say "a facts pass is running" and lock
    its own "Extract facts now" button while one is, since that pass never
    appears in ``running``/``last_completed``.

    ``last_failed`` is the newest run whose OWNING JOB the worker itself
    marked ``failed`` (``app/worker/runtime.py``'s
    ``_finalize_extraction_run_for_job`` — 2026-09 incident: an
    attempts-exhausted `corpus-extraction` job must still be visible here,
    even though :meth:`ExtractionRunsPgRepository.last_completed` itself
    deliberately excludes a ``failed`` row). Only populated when nothing is
    currently ``running`` AND it postdates ``last_completed`` — an old
    failure from long before the run that actually finished last must
    never eclipse it.

    ``failed_items_count``/``empty_items_count`` are the SIZE of this
    connection's persisted ``failed_items``/``empty_items`` backlogs
    (``connectors.sharepoint.crawler.load_state``), read with
    :meth:`SharepointStatePgRepository.backlog_counts` — a cheap
    ``jsonb_object_keys`` count, never a decode of the (potentially huge)
    payload on this polled-every-few-seconds path. They are what the
    source card's "Retry failed (N)"/"Retry empty (N)" buttons show as
    ``N``, and are ``0`` (never ``null``) for a connection that has never
    crawled — an honest "nothing to retry", not a missing signal.
    ``skipped_unsupported_count`` is NOT a persisted backlog (no retry
    mechanism replays it — see ``CrawlStats.skipped_unsupported``'s
    docstring), so it is read off whichever of ``running``/``last_failed``/
    ``last_completed`` above is most recent, in that order, and is ``null``
    when none of the three exist.
    """
    connection = _sharepoint_connection_or_404(connection_id)
    from src.repositories import extraction_runs_repo, sharepoint_state_repo

    repo = extraction_runs_repo()
    now = datetime.now(timezone.utc)
    running = repo.get_running(connection_id)
    last_completed = repo.last_completed(connection_id)
    last_failed = None if running else repo.last_failed(connection_id)
    if last_failed and last_completed:
        failed_at = _parse_ts(last_failed.get("started_at"))
        completed_at = _parse_ts(last_completed.get("started_at"))
        if failed_at is not None and completed_at is not None and failed_at <= completed_at:
            last_failed = None

    # Shard children (2026-09-03 auto-parallel-crawl design §4.7): at most
    # three parent ids ever need one here (`running`/`last_completed`/
    # `last_failed`), fetched in ONE batched `children_for` call rather than
    # up to three.
    shard_parent_ids = [
        str(run["id"]) for run in (running, last_completed, last_failed) if run and run.get("shards_total") is not None
    ]
    children_by_parent = repo.children_for(shard_parent_ids) if shard_parent_ids else {}

    def _out(run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if run is None:
            return None
        children = children_by_parent.get(str(run["id"])) if run.get("shards_total") is not None else None
        return _run_out(run, now=now, children=children)

    running_out = _out(running)
    last_completed_out = _out(last_completed)
    last_failed_out = _out(last_failed)
    skipped_unsupported_count = None
    for candidate in (running_out, last_failed_out, last_completed_out):
        if candidate is not None and candidate.get("skipped_unsupported") is not None:
            skipped_unsupported_count = candidate["skipped_unsupported"]
            break

    backlog = sharepoint_state_repo().backlog_counts(connection_id, "crawl")
    facts_job = _facts_job_in_flight(connection_id)
    facts_pending = _facts_pending_documents(connection_id)
    facts_jobs = _facts_jobs_in_flight(connection_id)
    return {
        "connection_id": connection_id,
        "running": running_out,
        "last_completed": last_completed_out,
        "last_failed": last_failed_out,
        "runs_total": repo.count_for_connection(connection_id),
        "facts_job": facts_job,
        # TCRD-296 gap #61: a pass that stopped on its own time budget left
        # nothing visible once the crawl that triggered it was long over —
        # an operator had to notice a stale corpus and re-trigger by hand.
        # `facts_pending_documents` (`connectors.sharepoint.
        # facts_extraction.count_pending_documents`, a cheap corpus-vs-
        # state scan, no document text read) answers "how much is left";
        # `facts_pass_running` (`facts_job is not None`) answers "is
        # anything doing it right now" — together the card can say
        # "pending · continuing" vs. "pending · not running" instead of
        # a silent gap. See `maybe_continue_pass` for the auto-chain that
        # normally keeps the second one true whenever the first is > 0.
        "facts_pending_documents": facts_pending,
        "facts_pass_running": facts_job is not None,
        # TCRD-296 gap #67 — every in-flight partition of a fanned-out
        # pass (or the single legacy job), plus the throughput/ETA line
        # the source card renders as "3/4 passes running, 1,400 docs/h,
        # ETA ~40m". See `_facts_jobs_in_flight`/`_facts_throughput_and_eta`.
        "facts_jobs": facts_jobs,
        "facts_passes_running": sum(1 for job in facts_jobs if job["status"] == "running"),
        "facts_passes_total": max((job["partition_count"] or 1 for job in facts_jobs), default=None),
        **_facts_throughput_and_eta(connection, pending=facts_pending),
        # TCRD-296 synthesis F.25 — the active `provider_limit` condition
        # matching THIS connection's resolved facts provider, or `null`.
        # The source card renders it as "paused: provider limit" on the
        # same line as the pending-documents count above.
        "provider_limit": _matching_provider_limit_condition(connection),
        # `POST …/extraction/stop` (below) always exists and always works —
        # the flag lives on `source_connections`, not on this PG-only table
        # — so there is now an honest Stop control to draw whenever a run is
        # actually active. Stated as data rather than left for the template
        # to infer: a button without a mechanism would be a lie, and this is
        # the field that says the mechanism exists.
        "can_stop": True,
        "failed_items_count": backlog["failed_items_count"],
        "empty_items_count": backlog["empty_items_count"],
        "skipped_unsupported_count": skipped_unsupported_count,
        # D.16 — best-effort "when will this connection next be swept",
        # same computation the crawl-config PATCH response and the fleet
        # view use (`_crawl_schedule_next_run_at`). `None` when this
        # connection is `off` or the instance-wide sweep has no cadence
        # configured at all.
        "next_run_at": _crawl_schedule_next_run_at(connection, now=now),
        "as_of": now.isoformat(),
    }


@router.post("/connections/{connection_id}/extraction/stop", status_code=202)
def request_extraction_stop(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Cooperative stop (owner-frustration fix, 2026-09-01: "je to strašný
    blackbox, nevidím co se děje v extrakci a nemůžu jí stopnout" — "it's a
    total black box, I can't see what's happening in the extraction and I
    can't stop it").

    Sets ``config.extraction.stop_requested_at`` on the connection row
    (:func:`connectors.sharepoint.crawler.request_stop`) — the SAME JSON
    column the extraction dispatch bookkeeping already lives in, carried
    forward on every generic connection edit
    (``SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS``). The crawl polls it at the
    same quiescent points its own timeout already checks (between delta
    pages always, between files every 10 completed items) and raises
    ``CrawlStopped`` there, which records the run exactly like a timeout —
    ``interrupted_reason: "stopped"``, resumable, state saved.

    Works on BOTH app-state backends: unlike every other route in this
    module, this one touches no ``extraction_runs`` row, so it is never
    gated behind the A3 Postgres-only ratchet — a DuckDB-backed instance can
    request a stop exactly as a Postgres-backed one can.

    Always ``202`` once the connection exists, whether or not a run is
    visibly active: a stop requested with nothing running simply waits on
    the connection row until the next run starts, at which point it is
    consumed (or, if unconsumed, cleared) — see ``note`` in the response
    when this instance can tell no run is currently active. ``404`` for an
    unknown or non-SharePoint connection. Deliberately NOT gated on
    ``sharepoint.enabled`` — this module's whole surface reads/writes
    observability state rather than running a crawl, the same posture its
    GET siblings already take (an admin can still see run history after
    disabling the connector; they can equally still stop a run already in
    flight from before it was disabled).
    """
    _sharepoint_connection_or_404(connection_id)
    from connectors.sharepoint.crawler import request_stop

    stop_requested_at = request_stop(connection_id)

    note: Optional[str] = None
    try:
        from src.repositories import extraction_runs_repo

        if extraction_runs_repo().get_running(connection_id) is None:
            note = (
                "no run currently appears active — the flag will be honored by the next "
                "run to start, and cleared unconsumed if that run never comes"
            )
    except Exception as exc:  # noqa: BLE001 — best-effort liveness hint only
        # A DuckDB-backed instance (or any other repo hiccup) cannot say
        # whether a run is LIVE — `extraction_runs` is PG-only (A3) — but the
        # stop signal itself is unaffected: it lives on `source_connections`,
        # which both backends have always had.
        logger.debug("extraction stop: could not check run liveness for %s: %s", connection_id, exc)
        note = "run activity cannot be checked on this backend, but the stop flag applies to any run in progress or about to start"

    # No explicit `log_safe` here — the route is declared in
    # `src.audit_posture.POSTURE` (`extraction.stop_requested`), and the
    # fallback middleware already carries everything this event needs
    # (the caller, the connection id via the resource's path params, the
    # response status). Writing a second row here would say nothing the
    # middleware doesn't already say.
    return {
        "connection_id": connection_id,
        "stop_requested_at": stop_requested_at,
        "note": note,
    }


class FactsConfigPatch(BaseModel):
    #: `None` means BOTH "not provided" and "clear the override" — a PATCH
    #: body that omits the field entirely and one that sends `null` do the
    #: same thing (fall back to the instance-level default), which is the
    #: least surprising reading of "unset this".
    retry_mode: Optional[str] = None
    #: `transport` is different: it is only touched when the field is PRESENT
    #: in the body (``model_fields_set``) — `"sync"`/`"batch"` sets the
    #: override, an explicit `null` clears it, and omitting it leaves it
    #: alone — so a caller setting only `retry_mode` cannot silently move a
    #: connection off the Batches API.
    transport: Optional[str] = None
    #: `provider` follows `transport`'s own PRESENT-in-body convention (never
    #: `retry_mode`'s always-touched one), for the same reason: a caller
    #: setting only `retry_mode` must not silently move a connection off (or
    #: onto) Vertex. `"inherit"`/`"anthropic"`/`"vertex"` sets the override,
    #: an explicit `null` clears it, omitting it leaves it alone.
    provider: Optional[str] = None
    #: `vertex_region` follows `transport`/`provider`'s own PRESENT-in-body
    #: convention: a lowercase-letters/digits/dash Vertex region (`"global"`
    #: allowed) sets the override, an explicit `null` clears it, omitting it
    #: leaves it alone. Only meaningful when the pass's resolved provider is
    #: `vertex` — harmless (accepted, stored, resolved, simply unused) on a
    #: connection pinned to (or inheriting) `anthropic`.
    vertex_region: Optional[str] = None


@router.patch("/connections/{connection_id}/extraction/facts-config")
def patch_extraction_facts_config(
    connection_id: str,
    body: FactsConfigPatch,
    _user: dict = Depends(require_admin),
):
    """Per-connection override for the facts-extraction retry policy
    (cost-levers task, lever A) — a single high-value connection (curated,
    high-stakes folders) can keep the corrective retry ON, since a dropped
    quote there is a lost citation on stage, while a long-tail connection
    runs with it OFF, without an instance.yaml edit that would flip every
    connection at once.

    Writes ``config.extraction.facts.retry_mode`` on the connection row —
    a sibling of ``config.extraction.stop_requested_at`` (the Stop
    control's own field, above): the established home for per-connection
    extraction state, carried forward on every generic connection edit.
    ``retry_mode: null`` (or the field simply omitted) CLEARS the override
    and falls back to the instance-level ``extraction.facts.retry_mode``
    (see :func:`connectors.sharepoint.facts_extraction.resolve_retry_mode`).
    A value outside ``{"off", "on_gate_fail", "always"}`` is refused with a
    plain ``422`` rather than silently ignored — a caller setting a value
    expects it to take effect.

    ``provider`` is a sibling override (``config.extraction.facts.provider``):
    ``"inherit"`` (the default) follows this instance's ``ai.provider``,
    ``"anthropic"``/``"vertex"`` pin this stage regardless of it — see
    :func:`connectors.sharepoint.facts_extraction.resolve_effective_provider`.
    The Anthropic Batches API has no Vertex equivalent, so a connection
    resolved to ``provider: vertex`` always runs the ``sync`` transport
    regardless of its own ``transport`` setting (one warning log line, never
    an error) — the response's ``provider.effective`` field is what a pass
    actually builds a client from, which can differ from ``provider.value``
    when the latter is ``"inherit"``.

    ``vertex_region`` is a further sibling override
    (``config.extraction.facts.vertex_region``), only meaningful when the
    resolved provider is ``vertex`` — it pins WHICH Vertex region a pass's
    client talks to, on top of this instance's own ``ai.vertex.region``.
    Google enforces Claude-on-Vertex quotas PER REGION, so a caller can
    spread several connections' facts passes across regions to multiply the
    account's effective throughput at the same per-call price. Validated
    with the same character class ``ai.vertex.region`` itself is held to
    (lowercase letters, digits, dash; ``"global"`` allowed) — a malformed
    value is refused with a plain ``422`` rather than silently ignored.

    Works on BOTH app-state backends, like the Stop control above: this
    touches only ``source_connections``, never a PG-only table.
    """
    connection = _sharepoint_connection_or_404(connection_id)
    from connectors.sharepoint.facts_extraction import (
        _VALID_PROVIDERS,
        _VALID_RETRY_MODES,
        _VALID_TRANSPORTS,
        _region_looks_valid,
        resolve_effective_provider,
        resolve_provider,
        resolve_retry_mode,
        resolve_transport,
        resolve_vertex_region,
    )

    if body.retry_mode is not None and body.retry_mode not in _VALID_RETRY_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"retry_mode must be one of {sorted(_VALID_RETRY_MODES)} or null (to clear the override)",
        )
    transport_given = "transport" in body.model_fields_set
    if transport_given and body.transport is not None and body.transport not in _VALID_TRANSPORTS:
        raise HTTPException(
            status_code=422,
            detail=f"transport must be one of {sorted(_VALID_TRANSPORTS)} or null (to clear the override)",
        )
    provider_given = "provider" in body.model_fields_set
    if provider_given and body.provider is not None and body.provider not in _VALID_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"provider must be one of {sorted(_VALID_PROVIDERS)} or null (to clear the override)",
        )
    vertex_region_given = "vertex_region" in body.model_fields_set
    if (
        vertex_region_given
        and body.vertex_region is not None
        and not _region_looks_valid(body.vertex_region.strip().lower())
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "vertex_region must be lowercase letters, digits and dash ('global' allowed), or null "
                "(to clear the override)"
            ),
        )
    if vertex_region_given and body.vertex_region is not None:
        # Live finding (b), TCRD-296 synthesis F.25: Sonnet outside
        # `global` (no regional quota bucket at all) answers 429 on
        # every call. A connection has no per-connection model override,
        # so this checks against the INSTANCE's currently configured
        # model — the one a pass for THIS connection would actually use.
        from connectors.sharepoint.facts_extraction import (
            VERTEX_REGION_MODEL_MATRIX,
            _model,
            vertex_region_supports_model,
        )

        region_norm = body.vertex_region.strip().lower()
        model = _model()
        if not vertex_region_supports_model(region_norm, model):
            matrix_hint = "; ".join(
                f"{tier}: {', '.join(regions)}" for tier, regions in VERTEX_REGION_MODEL_MATRIX.items()
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    f"vertex_region={region_norm!r} has no documented Claude-on-Vertex quota bucket for the "
                    f"instance's configured model ({model!r}) — every call would answer 429. Supported "
                    f"region×model matrix: {matrix_hint}."
                ),
            )

    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    extraction = dict((connection.get("config") or {}).get("extraction") or {})
    facts_cfg = dict(extraction.get("facts") or {})
    if body.retry_mode is None:
        facts_cfg.pop("retry_mode", None)
    else:
        facts_cfg["retry_mode"] = body.retry_mode
    if transport_given:
        if body.transport is None:
            facts_cfg.pop("transport", None)
        else:
            facts_cfg["transport"] = body.transport
    if provider_given:
        if body.provider is None:
            facts_cfg.pop("provider", None)
        else:
            facts_cfg["provider"] = body.provider
    if vertex_region_given:
        if body.vertex_region is None:
            facts_cfg.pop("vertex_region", None)
        else:
            facts_cfg["vertex_region"] = body.vertex_region.strip().lower()
    if facts_cfg:
        extraction["facts"] = facts_cfg
    else:
        extraction.pop("facts", None)
    updated = repo.config_patch(connection_id, {"extraction": extraction})

    mode, source = resolve_retry_mode(updated)
    transport_value, transport_source = resolve_transport(updated)
    provider_value, provider_source = resolve_provider(updated)
    effective_provider, effective_provider_source = resolve_effective_provider(updated)
    vertex_region_value, vertex_region_source = resolve_vertex_region(updated)

    # More than the fallback middleware can say (it sees only path params
    # and the response status, never the body) — the VALUE an admin set or
    # cleared, and what it resolved to, is exactly the audit trail's job
    # here. Not secret, not document content: a five-word enum choice.
    log_safe(
        user_id=_user.get("id"),
        action="extraction.facts_retry_mode_set",
        resource=f"sharepoint_connection:{connection_id}",
        params={
            "retry_mode": body.retry_mode,
            "resolved": mode,
            "source": source,
            "transport": body.transport if transport_given else "(untouched)",
            "transport_resolved": transport_value,
            "transport_source": transport_source,
            "provider": body.provider if provider_given else "(untouched)",
            "provider_resolved": provider_value,
            "provider_source": provider_source,
            "provider_effective": effective_provider,
            "vertex_region": body.vertex_region if vertex_region_given else "(untouched)",
            "vertex_region_resolved": vertex_region_value,
            "vertex_region_source": vertex_region_source,
        },
    )

    return {
        "connection_id": connection_id,
        "retry_mode": {"value": mode, "source": source},
        "transport": {"value": transport_value, "source": transport_source},
        "provider": {
            "value": provider_value,
            "source": provider_source,
            "effective": effective_provider,
            "effective_source": effective_provider_source,
        },
        "vertex_region": {"value": vertex_region_value, "source": vertex_region_source},
    }


class CrawlConfigPatch(BaseModel):
    #: `None` means BOTH "not provided" and "clear the override" — same
    #: reading as `FactsConfigPatch.retry_mode` above. An ISO `YYYY-MM-DD`
    #: string, validated below; anything else is a 400.
    min_modified: Optional[str] = None
    #: D.16 — this connection's own crawl cadence. UNLIKE `min_modified`
    #: above, "not provided" and "clear" are DIFFERENT here (checked via
    #: `model_fields_set`, the same discipline `FactsConfigPatch.transport`
    #: uses): a caller touching only `min_modified` must not silently reset
    #: an already-set `schedule` back to the instance default, and vice
    #: versa — see the handler's own docstring. `None` (given explicitly)
    #: clears the override back to `CRAWL_SCHEDULE_INSTANCE`; omitted
    #: leaves it untouched.
    schedule: Optional[str] = None


def _crawl_schedule_next_run_at(connection: Dict[str, Any], *, now: datetime) -> Optional[str]:
    """The Crawl schedule panel's "next run" hint (D.16) — best-effort,
    DISPLAY ONLY, mirroring :func:`src.scheduler.next_due_at`'s own "not the
    source of truth" posture. ``None`` when this connection's own cadence
    resolves to ``off``, when the INSTANCE-WIDE sweep itself has no schedule
    configured (the sweep never runs at all, regardless of any per-
    connection override — see ``app/api/admin_sharepoint.py::
    run_due_extraction``), or when neither cadence has a well-defined next
    occurrence (a ``cron`` schedule — see ``next_due_at``).
    """
    from connectors.sharepoint.crawler import CRAWL_SCHEDULE_INSTANCE, CRAWL_SCHEDULE_OFF, resolve_crawl_schedule
    from src.scheduler import next_due_at

    own_schedule, _source = resolve_crawl_schedule(connection)
    if own_schedule == CRAWL_SCHEDULE_OFF:
        return None
    if own_schedule == CRAWL_SCHEDULE_INSTANCE:
        from app.api.admin_sharepoint import _extraction_schedule_config

        effective_schedule = _extraction_schedule_config()
    else:
        effective_schedule = own_schedule
    if not effective_schedule:
        return None
    last_run_at = ((connection.get("config") or {}).get("extraction") or {}).get("last_run_at")
    due_at = next_due_at(effective_schedule, last_run_at, now=now)
    return due_at.isoformat() if due_at else None


@router.patch("/connections/{connection_id}/extraction/crawl-config")
def patch_extraction_crawl_config(
    connection_id: str,
    body: CrawlConfigPatch,
    _user: dict = Depends(require_admin),
):
    """Per-connection crawl levers: the ``min_modified`` age filter (a
    backfill lever — crawl only files modified on/after a cutoff date
    instead of re-walking a whole multi-year corpus) and, since D.16, this
    connection's own sweep cadence (``schedule``) — "keeping a site current
    without an operator" needs more than the one instance-wide cadence
    ``extraction.schedule`` offers.

    Writes ``config.extraction.crawl.min_modified``/``config.extraction.
    crawl.schedule`` on the connection row — a sibling of ``config.
    extraction.facts.retry_mode`` (the facts-config endpoint above) and
    ``config.extraction.stop_requested_at`` (the Stop control): the
    established home for per-connection extraction state, carried forward
    on every generic connection edit.

    ``min_modified: null`` (or the field simply OMITTED) CLEARS the
    override — there is no instance-level fallback to fall back to (see
    :func:`connectors.sharepoint.crawler.resolve_min_modified`'s own
    docstring for why); a value that is not a parseable ISO ``YYYY-MM-DD``
    date is refused with a plain ``400`` (``invalid_min_modified``).

    ``schedule`` follows a DIFFERENT omitted-vs-null contract, because this
    endpoint now sets two independent knobs and neither may silently reset
    the other: omitted leaves the stored ``schedule`` untouched (checked via
    ``"schedule" in body.model_fields_set``, the SAME pattern ``…/facts-
    config``'s ``transport``/``provider``/``vertex_region`` already use for
    this exact reason), ``null`` (given explicitly) CLEARS the override back
    to ``CRAWL_SCHEDULE_INSTANCE`` (follow the instance-wide cadence — see
    :func:`connectors.sharepoint.crawler.resolve_crawl_schedule`), and any
    other value must be ``"off"``, ``"instance"``, or a cadence string
    :func:`connectors.sharepoint.crawler.is_valid_crawl_schedule` accepts
    (the SAME grammar ``extraction.schedule`` itself uses instance-wide) —
    refused with ``400 invalid_crawl_schedule`` otherwise. The response's
    ``schedule.next_run_at`` is a best-effort display hint
    (:func:`_crawl_schedule_next_run_at`), ``None`` when this connection is
    ``off`` or the instance-wide sweep has no cadence configured at all (see
    ``app/api/admin_sharepoint.py::run_due_extraction`` — the instance
    switch is still what turns the sweep ON, regardless of any per-
    connection override).

    Works on BOTH app-state backends, like its siblings above: this touches
    only ``source_connections``, never a PG-only table.
    """
    connection = _sharepoint_connection_or_404(connection_id)
    from connectors.sharepoint.crawler import is_valid_crawl_schedule, resolve_crawl_schedule, resolve_min_modified

    if body.min_modified is not None:
        try:
            date.fromisoformat(body.min_modified)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_min_modified") from None

    schedule_given = "schedule" in body.model_fields_set
    if schedule_given and body.schedule is not None and not is_valid_crawl_schedule(body.schedule):
        raise HTTPException(status_code=400, detail="invalid_crawl_schedule")

    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    extraction = dict((connection.get("config") or {}).get("extraction") or {})
    crawl_cfg = dict(extraction.get("crawl") or {})
    if body.min_modified is None:
        crawl_cfg.pop("min_modified", None)
    else:
        crawl_cfg["min_modified"] = body.min_modified
    if schedule_given:
        if body.schedule is None:
            crawl_cfg.pop("schedule", None)
        else:
            crawl_cfg["schedule"] = body.schedule
    if crawl_cfg:
        extraction["crawl"] = crawl_cfg
    else:
        extraction.pop("crawl", None)
    updated = repo.config_patch(connection_id, {"extraction": extraction})

    cutoff, mm_source = resolve_min_modified(updated)
    schedule_value, schedule_source = resolve_crawl_schedule(updated)
    next_run_at = _crawl_schedule_next_run_at(updated, now=datetime.now(timezone.utc))

    # More than the fallback middleware can say (it never sees the body) —
    # same reasoning as the facts-config endpoint's own log_safe above.
    log_safe(
        user_id=_user.get("id"),
        action="extraction.min_modified_set",
        resource=f"sharepoint_connection:{connection_id}",
        params={
            "min_modified": body.min_modified,
            "resolved": cutoff.isoformat() if cutoff else None,
            "source": mm_source,
            "schedule": body.schedule if schedule_given else "(untouched)",
            "schedule_resolved": schedule_value,
            "schedule_source": schedule_source,
        },
    )

    return {
        "connection_id": connection_id,
        "min_modified": {"value": cutoff.isoformat() if cutoff else None, "source": mm_source},
        "schedule": {"value": schedule_value, "source": schedule_source, "next_run_at": next_run_at},
    }


@router.get("/connections/{connection_id}/extraction/runs")
def extraction_runs(
    connection_id: str,
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_admin),
):
    """A2 — the run-history drawer's rows, newest first.

    ``total`` is every recorded run, not just the returned page, so "5 more
    runs" is never a silent truncation. A SHARDED site's row carries its own
    ``shards[]`` (2026-09-03 auto-parallel-crawl design §4.7) — one batched
    :meth:`ExtractionRunsPgRepository.children_for` call across every parent
    on this page, never one round trip per row.
    """
    _sharepoint_connection_or_404(connection_id)
    from src.repositories import extraction_runs_repo

    repo = extraction_runs_repo()
    now = datetime.now(timezone.utc)
    rows = repo.list_for_connection(connection_id, limit=limit)
    parent_ids = [str(r["id"]) for r in rows if r.get("shards_total") is not None]
    children_by_parent = repo.children_for(parent_ids) if parent_ids else {}
    runs_out = [
        _run_out(
            r, now=now, children=children_by_parent.get(str(r["id"])) if r.get("shards_total") is not None else None
        )
        for r in rows
    ]
    return {
        "connection_id": connection_id,
        "runs": runs_out,
        "total": repo.count_for_connection(connection_id),
        "as_of": now.isoformat(),
    }


@router.get("/connections/{connection_id}/extraction/runs/{run_id}")
def extraction_run_detail(
    connection_id: str,
    run_id: str,
    _user: dict = Depends(require_admin),
):
    """A3 — one run's stored report, usage and capped skip list.

    The skip list carries ``listed`` alongside ``total``: only oversize
    skips keep a path, so a run that refused 27 documents and can name 20 of
    them says exactly that instead of implying the list is the whole story.

    The per-file error detail (download/convert/ingest failures — a path, a
    reason, an upstream status code when known, and a message) lives at
    ``report.errors_detail``, same ``{items, total, listed, truncated}``
    envelope. It rides inside ``report`` rather than getting its own
    top-level key or column because it is exactly as itemizable as the rest
    of a run's numbers, never a separate concern — the source card's
    error-count line fetches this endpoint on first expand to render it.

    ``report.failed_items`` (convert-stage failures — ``convert_failed`` /
    ``convert_empty``, each with ``item_id``/``drive_id`` alongside ``path``,
    ``reason_type`` and a truncated ``reason``, capped at 5000 with
    ``report.failed_items_truncated``) is the operator-facing list an
    admin-requested ``retry_failed`` run works from and reads to see WHICH
    documents are missing from the corpus — never the raw ``path`` for an
    anonymize-marked scope's item. ``report.skipped_items`` /
    ``report.skipped_unsupported`` is the same shape for a file no
    conversion backend even attempts (never counted an error).
    """
    _sharepoint_connection_or_404(connection_id)
    from src.repositories import extraction_runs_repo

    repo = extraction_runs_repo()
    run = repo.get(run_id)
    if run is None or run.get("connection_id") != connection_id:
        raise HTTPException(status_code=404, detail="run_not_found")
    # A SHARDED (parent) run's own `shards[]` (2026-09-03 auto-parallel-
    # crawl design §4.7) — read on-demand, only ever for this ONE run.
    children = repo.children_for([run_id]).get(run_id) if run.get("shards_total") is not None else None
    out = _run_out(run, children=children)
    out["report"] = run.get("report") or {}
    out["progress"] = run.get("progress") or {}
    out["skips"] = run.get("skips") or {"items": [], "listed": 0, "total": 0, "truncated": False}
    return out


# ---------------------------------------------------------------------------
# A5 — effective configuration, with an origin and a lock state per leaf.
#
# The origin vocabulary is the one `GET /api/admin/config-surface` already
# returns (`env` / `yaml` / `default`), plus `builtin` for a value that is a
# code constant with no config key at all. Editability is read from the
# SWITCH REGISTRY at render time rather than restated here, so this drawer
# cannot drift from what `/admin/server-config` actually accepts.
# ---------------------------------------------------------------------------

_MISSING = object()


def _switch_for(config_keys: tuple) -> Any:
    """The registry entry whose ``config_keys`` match this leaf, or None."""
    from app.switches import SWITCHES

    for switch in SWITCHES:
        if tuple(switch.config_keys) == tuple(config_keys):
            return switch
    return None


def _config_row(
    label: str,
    config_keys: tuple,
    *,
    env_var: Optional[str] = None,
    default: Any = None,
    value: Any = _MISSING,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """One row of the configuration read-out: its effective value, where
    that value came from, and whether an admin can change it here.

    Three honesty rules are enforced in this one place:

    * **An env-set value is LOCKED**, whatever the switch registry says
      about the section. An admin edit through ``/admin/server-config``
      writes YAML, and YAML loses to the environment — offering the edit
      would be offering a change that silently does nothing.
    * **A value nobody set says so.** ``origin: "default"`` is the built-in
      fallback, distinct from ``"yaml"`` — "50 MB because that is the
      default" and "50 MB because someone chose it" are different facts.
    * **A key's NAME is never its VALUE.** Rows that point at a credential
      env var carry ``env_name`` only; no caller of this function may pass
      a secret as ``value``.
    """
    from app.instance_config import get_value

    env_value = os.environ.get(env_var) if env_var else None
    yaml_value = get_value(*config_keys, default=_MISSING) if config_keys else _MISSING

    if env_value is not None:
        origin = "env"
        effective: Any = env_value
    elif yaml_value is not _MISSING:
        origin = "yaml"
        effective = yaml_value
    elif not config_keys and not env_var:
        # A code constant: there is no key, no env var, and nothing to set.
        # Distinct from an env-only knob nobody has set, which is `default`.
        origin = "builtin"
        effective = default
    else:
        origin = "default"
        effective = default

    if value is not _MISSING:
        # A caller that already resolved the effective value through the
        # SAME resolver the runtime uses (the NER model's two-path order,
        # for example) passes it here — the origin above still describes
        # where it came from, but the value is the resolver's, not a second
        # re-derivation that could disagree with it.
        effective = value

    switch = _switch_for(config_keys) if config_keys else None
    if origin == "env":
        editable = False
        lock_reason = (
            f"set by the environment ({env_var}) — an admin edit writes instance.yaml, "
            "which the environment overrides, so the change would silently do nothing"
        )
    elif switch is not None:
        editable = bool(switch.editable)
        lock_reason = "" if editable else (switch.lock_reason or "not editable from the UI")
    elif not config_keys and not env_var:
        editable = False
        lock_reason = "a code constant — there is no setting to change"
    elif not config_keys:
        editable = False
        lock_reason = (
            f"environment-only: set {env_var} on the process. There is no instance.yaml key "
            "for it, so there is nothing an admin form could write"
        )
    else:
        editable = False
        lock_reason = (
            "deploy-time configuration: the `extraction` section is deliberately not "
            "admin-writable (it is the section a producer command line lives in), so "
            "this value is changed in instance.yaml and applied on deploy"
        )

    return {
        "key": ".".join(config_keys) if config_keys else None,
        "label": label,
        "value": effective,
        "origin": origin,
        "env_name": env_var,
        "default": default,
        "editable": editable,
        "lock_reason": lock_reason,
        "note": note,
    }


def _ner_model_row() -> Dict[str, Any]:
    """The NER model row, labelled with WHICH of the two config paths won.

    ``src/anonymization_ner.py`` resolves ``corporate_memory.extraction.model``
    first, then ``extraction.model``, then a built-in fallback. A drawer that
    read only ``extraction.model`` would mislabel every instance that set the
    corporate-memory path — so the order is walked here, once, and the row
    names the winner.
    """
    from app.instance_config import get_value

    for keys in (("corporate_memory", "extraction", "model"), ("extraction", "model")):
        raw = get_value(*keys, default=None)
        if raw:
            return _config_row("NER model", keys, default=None, value=raw)
    row = _config_row("NER model", (), default="claude-haiku-4-5")
    row["note"] = (
        "nothing configured — the built-in fallback. Set corporate_memory.extraction.model "
        "or extraction.model to pin one."
    )
    return row


#: What each `extraction.anonymization.detector` value actually costs and
#: catches. The note is chosen by the EFFECTIVE value, never written as if
#: one of them were always in force: a drawer that explains `regex` while
#: the instance is set to `llm` describes a pipeline nobody is running.
_DETECTOR_NOTES = {
    "regex": (
        "the deterministic detector alone — exact on emails, phones and ids, blind to "
        "names. No LLM call and no tokens spent, which is a different claim from a $0.00 cost."
    ),
    "llm": (
        "the deterministic detector AND an LLM pass over the same text, unioned — the regex "
        "tier is not swapped out, it is added to. Spends tokens per document."
    ),
}


def _detector_row() -> Dict[str, Any]:
    """The NER detector row, annotated for the value actually in force.

    Matching lowercases and strips, because the RUNTIME does
    (``crawler._entity_detector`` compares a normalized value against
    ``"llm"``) — a drawer that read ``"LLM"`` as unrecognized would disagree
    with the engine it is describing.

    An unrecognized value is not an error at runtime: anything that is not
    ``llm`` resolves to the deterministic tier, deliberately, because a typo
    hard-failing every extraction run is worse than quietly running the
    free, safe tier. So the note says BOTH things — the value is wrong, and
    here is what actually executes. Naming only the first would leave an
    operator guessing whether anything ran at all.
    """
    row = _config_row("NER detector", ("extraction", "anonymization", "detector"), default="regex")
    key = str(row["value"]).strip().lower() if row["value"] is not None else ""
    if not key:
        # Empty/unset resolves to the deterministic tier, same as the
        # default — the row already renders the value itself as "not set".
        key = "regex"
    note = _DETECTOR_NOTES.get(key)
    if note is None:
        note = (
            f"unrecognized value — anything but `llm` runs the regex tier, so this instance uses "
            f"{_DETECTOR_NOTES['regex']} Fix the value or remove the key."
        )
    row["note"] = note
    return row


def _deterministic_tier_rows() -> List[Dict[str, Any]]:
    """The deterministic identifier tiers and the operator's custom terms.

    These belong next to the detector row rather than in a page of their
    own: "which detector" and "which shapes" are one question an admin asks
    once, before a crawl. The preview panel below the drawer is what turns
    the answer from a claim into something they can check.

    The custom-term row reports a COUNT, not the terms. They are ordinary
    admin-readable config, but they are also the very words an operator
    considered sensitive enough to redact; a read-out that prints them into
    every config drawer render (and every screenshot of one) earns nothing
    the count does not.
    """
    from src.anonymization import CustomTermError, compile_custom_terms, rules_from_config

    rows = [
        _config_row(
            f"Detect {name}",
            ("extraction", "anonymization", "detect", key),
            default=True,
            note=note,
        )
        for key, name, note in (
            (
                "phones",
                "phone numbers",
                "international (+CC / 00CC) and the Czech 3-3-3 grouping. A bare nine-digit "
                "run is deliberately not matched — that shape is an order number at least as often",
            ),
            (
                "ibans",
                "IBANs",
                "mod-97 validated, so an IBAN-shaped product code is not redacted — and a "
                "typo'd or OCR-mangled account number is not either",
            ),
            (
                "national_ids",
                "national ids",
                "Czech birth number (rodné číslo), slashed or solid; the date field and the "
                "mod-11 check must both pass",
            ),
        )
    ]

    row = _config_row("Custom terms", ("extraction", "anonymization", "custom_terms"), default=[])
    try:
        terms = rules_from_config().custom_terms
        # Compiled, not just counted: validation lives in the compiler, and
        # a row that reported "3 term(s)" for three terms no crawl can use
        # would be the reassuring kind of wrong this drawer exists to avoid.
        compile_custom_terms(terms)
        count = len(terms)
    except CustomTermError as exc:
        # A configured term that cannot compile is the operator's own value
        # being wrong, and every crawl over an anonymize-marked scope will
        # drop its documents until it is fixed. The drawer says so here
        # rather than letting the run report be the first hint.
        row["value"] = "invalid"
        row["note"] = f"unusable — {exc}"
        return [*rows, row]
    row["value"] = f"{count} term(s)"
    row["note"] = (
        "literal text redacted as TERM_<hmac>; the terms themselves are not printed here. "
        "Regular expressions are refused — see docs/anonymization.md"
    )
    return [*rows, row]


def _facts_provider_row(connection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The facts-extraction provider row — the RESOLVED value
    (``connectors.sharepoint.facts_extraction.resolve_effective_provider``),
    not just the raw ``extraction.facts.provider`` setting: ``"inherit"``
    (the default) resolves through this instance's ``ai.provider``, and an
    operator debugging "why did this connection's last pass spend against
    Anthropic instead of Vertex" needs the resolved answer, the same
    "pre-resolved ``value=``" pattern :func:`_ner_model_row` already uses for
    its own two-path resolution.
    """
    from connectors.sharepoint.facts_extraction import resolve_effective_provider

    provider, source = resolve_effective_provider(connection)
    return _config_row(
        "Facts provider",
        ("extraction", "facts", "provider"),
        default="inherit",
        value=provider,
        note=(
            f"resolved via {source} — 'inherit' (the default) follows this instance's ai.provider; "
            "'anthropic'/'vertex' pin this stage regardless of it. A connection can override it on "
            "its source card."
        ),
    )


def _facts_transport_row(connection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The facts-extraction transport row — the RESOLVED value
    (``connectors.sharepoint.facts_extraction.resolve_transport``). Does NOT
    account for the provider-vertex-forces-sync fallback a live PASS applies
    (that is a per-run outcome, reported on the run itself, not a static
    setting this row describes) — see the run report's own ``transport``
    field for what a given pass actually used.
    """
    from connectors.sharepoint.facts_extraction import resolve_transport

    transport, source = resolve_transport(connection)
    return _config_row(
        "Facts transport",
        ("extraction", "facts", "transport"),
        default="sync",
        value=transport,
        note=(
            f"resolved via {source} — the Anthropic Batches API has no Vertex equivalent, so a pass "
            "resolved to provider: vertex always runs sync regardless of this setting"
        ),
    )


def _facts_vertex_region_row(connection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The facts-extraction Vertex region row — the RESOLVED value
    (``connectors.sharepoint.facts_extraction.resolve_vertex_region``), one
    level deeper than ``resolve_provider``/``resolve_transport``: a
    per-connection override, then ``extraction.facts.vertex_region``, then
    this instance's own ``ai.vertex.region``. Only meaningful for a pass
    resolved to ``provider: vertex`` — shown regardless, the same
    "resolved even where it does not apply" posture :func:`_facts_transport_row`
    already takes.
    """
    from connectors.sharepoint.facts_extraction import resolve_vertex_region

    region, source = resolve_vertex_region(connection)
    return _config_row(
        "Facts Vertex region",
        ("extraction", "facts", "vertex_region"),
        default=None,
        value=region,
        note=(
            f"resolved via {source} — only used when the facts provider above resolves to vertex. "
            "Google enforces Claude-on-Vertex quotas per region, so pinning different connections to "
            "different regions raises the account's effective throughput. A connection can override "
            "it on its source card."
        ),
    )


def _extraction_config_rows(connection: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The effective ``extraction`` block, one row per leaf (design §6.2).

    ``extraction.producer.*`` is deliberately absent: the built-in pipeline
    has no producer command, and rendering a command line an admin cannot
    change (and this instance does not run) would be noise at best and a
    pointer at an executable at worst.
    """
    return [
        _config_row(
            "Enabled (whole connector)", ("sharepoint", "enabled"), env_var="AGNES_SHAREPOINT_ENABLED", default=False
        ),
        _config_row(
            "Schedule",
            ("extraction", "schedule"),
            env_var="SCHEDULER_EXTRACTION_SCHEDULE",
            default="",
            note="one instance-wide cadence, applied per connection against its own last-run stamp",
        ),
        _config_row(
            "Timeout",
            ("extraction", "timeout_s"),
            default=3600,
            note=(
                "a real ceiling on one run: at expiry the crawl stops, persists its state and the "
                "job fails. Nothing already ingested is lost and the next run resumes from the "
                "persisted deltaLinks/cTags, so a timeout costs re-work, never coverage. 0 = unbounded."
            ),
        ),
        _config_row(
            "Stall threshold (s)",
            ("extraction", "stall_after_s"),
            default=_STALL_AFTER_S,
            value=_stall_after_s(),
            note=(
                "how stale a running run's last checkpoint may get before this connection's Run "
                "row and the fleet view (/admin/extraction) call it stalled instead of running — "
                "the SAME rule both surfaces read, so they can never disagree. A stalled run can "
                "be force-cancelled from either surface."
            ),
        ),
        _config_row(
            "Max file size (MB)",
            ("extraction", "crawler", "max_file_mb"),
            default=50,
            note=(
                "a document over this cap is never downloaded, never converted, and never "
                "appears in the collection — it is counted in the run's skips, and nowhere else"
            ),
        ),
        _detector_row(),
        _ner_model_row(),
        _facts_provider_row(connection),
        _facts_transport_row(connection),
        _facts_vertex_region_row(connection),
        _config_row(
            "Anonymization key",
            ("extraction", "anonymization", "hmac_key_env"),
            default="",
            note="the NAME of the env var holding the per-instance pseudonym key — never its value",
        ),
        _config_row(
            "Scan transcription model",
            (),
            env_var="AGNES_VISION_MODEL",
            default=None,
            note="instance-wide, environment-only — every scope gets the same model; there is no per-scope control",
        ),
        _config_row(
            "Checkpoint granularity",
            (),
            default="every 200 delta rows",
            note="a code constant, shown so nobody hunts for the knob",
        ),
        *_deterministic_tier_rows(),
    ]


@router.get("/connections/{connection_id}/extraction/config")
def extraction_config(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """A5 — the read-only effective configuration for this connection's
    extraction, with an origin and a lock state on every row.

    Cataloged rather than exempt (``sharepoint_connection.extraction_config_read``):
    it discloses credential env-var NAMES and the per-scope audience-class
    mapping — the same disclosure class as its ``scopes_read`` /
    ``certificate_read`` siblings.

    Answers on BOTH backends: nothing here reads ``extraction_runs``, and
    an admin locked out of the configuration read-out because their instance
    is on DuckDB would be a degradation with no cause.

    ``min_modified`` carries the resolved crawl age filter (the SAME
    ``{value, source}`` shape the ``…/extraction/crawl-config`` PATCH
    response returns) — the drawer's Crawl filter panel needs the CURRENT
    override to pre-fill its date input, not just a place to write a new one.
    ``schedule`` (D.16) is this connection's own sweep cadence, same shape
    plus a ``next_run_at`` display hint (:func:`_crawl_schedule_next_run_at`)
    — the panel's "Crawl schedule" control needs the same round trip.
    """
    connection = _sharepoint_connection_or_404(connection_id)

    from connectors.sharepoint.crawler import resolve_crawl_schedule, resolve_min_modified

    cutoff, min_modified_source = resolve_min_modified(connection)
    schedule_value, schedule_source = resolve_crawl_schedule(connection)
    now = datetime.now(timezone.utc)

    scopes: List[Dict[str, Any]] = []
    try:
        # Imported, never re-implemented: the wizard's step-3 preview and the
        # source card render this exact projection, so a fourth copy cannot
        # drift (design §6.2, principle P4).
        from app.api.admin_sharepoint import _scope_out

        raw_scopes = (connection.get("config") or {}).get("scopes") or []
        scopes = [_scope_out(s, connection=connection) for s in raw_scopes if isinstance(s, dict)]
    except Exception as exc:  # noqa: BLE001 — one block degrades, the drawer still opens
        logger.warning("extraction config: per-scope rows unavailable for %s: %s", connection_id, exc)

    # Read from the registry at render time, never asserted (UX review M5):
    # the section became admin-editable once the producer command line — the
    # one security reason to keep it read-only — was removed with external
    # mode. Per-leaf env pins still lock their own rows above.
    from app.api.admin import _EDITABLE_SECTIONS

    section_editable = "extraction" in _EDITABLE_SECTIONS

    return {
        "connection_id": connection_id,
        "effective": _extraction_config_rows(connection),
        "scopes": scopes,
        "section_editable": section_editable,
        "section_lock_reason": None
        if section_editable
        else "The `extraction` section is not admin-writable on this instance.",
        "min_modified": {"value": cutoff.isoformat() if cutoff else None, "source": min_modified_source},
        "schedule": {
            "value": schedule_value,
            "source": schedule_source,
            "next_run_at": _crawl_schedule_next_run_at(connection, now=now),
        },
        "as_of": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Completeness check — "did we really get everything?" (TCRD-296 synthesis
# item B.9). An operator's ad hoc script compared Graph Search document
# counts per top-level folder against corpus_files rows per scope
# collection; this is that script, promoted to a read-only admin surface.
# All the actual math lives in ``connectors.sharepoint.completeness`` — see
# that module's docstring for the row shape, the status vocabulary
# (unknown/complete/accounted/missing) and each reason count's honest
# attribution limits. This section owns only: connection/param resolution,
# the Graph token, the in-process TTL cache, and the "is a crawl running
# right now" liveness flag.
# ---------------------------------------------------------------------------

#: How long a computed report is reused before a repeat open of the drawer
#: recomputes it — one Graph Search call per scope/folder (an
#: ``_COUNT_CONCURRENCY``-wide fan-out, same throttle the site-split planner
#: uses), so this is the only thing standing between "click Recount twice"
#: and rate-limiting an admin's own tenant. Best-effort and process-local
#: (not shared across worker processes, same trade-off as the fleet
#: endpoint's own rate sampler above) — a cache miss just recomputes, never
#: a correctness issue. ``refresh=true`` bypasses AND repopulates the entry.
_COMPLETENESS_CACHE_TTL_S = 600
_completeness_cache_lock = threading.Lock()
_completeness_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Dict[str, Any]]] = {}


def _completeness_cache_get(key: Tuple[str, Optional[str]]) -> Optional[Dict[str, Any]]:
    with _completeness_cache_lock:
        entry = _completeness_cache.get(key)
    if entry is None:
        return None
    computed_at, payload = entry
    if time.monotonic() - computed_at > _COMPLETENESS_CACHE_TTL_S:
        return None
    return payload


def _completeness_cache_put(key: Tuple[str, Optional[str]], payload: Dict[str, Any]) -> None:
    with _completeness_cache_lock:
        _completeness_cache[key] = (time.monotonic(), payload)


def _completeness_crawl_running(connection_id: str) -> bool:
    """Whether a ``corpus-extraction`` job is queued/running for this
    connection right now — the SAME job-queue lookup :func:`_facts_job_in_
    flight` uses for the facts pass, matched on the trigger's own
    idempotency key. Deliberately NOT ``extraction_runs`` (PG-only): this
    keeps the completeness check answerable on both app-state backends,
    same reasoning as ``…/extraction/config`` above."""
    from app.api.admin_sharepoint import _extraction_idempotency_key
    from src.repositories import jobs_repo

    key = _extraction_idempotency_key(connection_id)
    repo = jobs_repo()
    for status in ("running", "queued"):
        for job in repo.list(status=status, kind="corpus-extraction", limit=200):
            if job.get("idempotency_key") == key:
                return True
    return False


@router.get("/connections/{connection_id}/extraction/completeness")
async def extraction_completeness(
    connection_id: str,
    min_modified: Optional[str] = None,
    refresh: bool = False,
    _user: dict = Depends(require_admin),
):
    """A6 — "did we really get everything?": one row per confirmed scope,
    plus (only for a connection with exactly one whole-drive scope) one row
    per top-level folder under it, plus a totals row.

    ``min_modified`` defaults to the connection's OWN resolved crawl cutoff
    (:func:`connectors.sharepoint.crawler.resolve_min_modified` — same
    ``{value, source}`` shape ``…/extraction/config`` returns) so "expected"
    counts exactly the population the last crawl would have attempted, not
    an unfiltered superset; an explicit query param overrides it
    (``source: "query"``). ``400 invalid_min_modified`` for a malformed
    override, same validation ``…/split-plan`` runs on the identical key.

    Cached per ``(connection_id, resolved min_modified)`` for
    :data:`_COMPLETENESS_CACHE_TTL_S` — the response's own ``cached`` field
    says whether this answer was reused. ``refresh=true`` bypasses the
    cache and recomputes.

    ``provisional: true`` when a ``corpus-extraction`` job is currently
    queued/running for this connection — the numbers are still returned
    (a stale-but-labeled answer beats none), just flagged as a snapshot
    mid-crawl rather than a settled one.

    Read-only: no crawl state is written, no corpus row is touched. ``404``
    for an unknown/non-SharePoint connection id; the same ``409``/``502``
    ``…/split-plan`` raises when this connection's certificate is
    unresolved or Graph itself rejects the token exchange.
    """
    connection = _sharepoint_connection_or_404(connection_id)
    from app.api.admin_sharepoint import _resolved_token, _validate_min_modified
    from connectors.sharepoint.completeness import compute_completeness
    from connectors.sharepoint.crawler import resolve_min_modified

    _validate_min_modified(min_modified)
    if min_modified:
        resolved_min_modified: Optional[str] = min_modified
        min_modified_source = "query"
    else:
        cutoff, min_modified_source = resolve_min_modified(connection)
        resolved_min_modified = cutoff.isoformat() if cutoff else None

    cache_key = (connection_id, resolved_min_modified)
    cached = None if refresh else _completeness_cache_get(cache_key)
    if cached is not None:
        report = cached
        was_cached = True
    else:
        scopes = [s for s in (connection.get("config") or {}).get("scopes") or [] if isinstance(s, dict)]
        # No token exchange for a connection with no confirmed scope yet —
        # there is nothing to count against, and a not-yet-configured
        # certificate must not turn "no scopes" into a 409.
        token = await _resolved_token(connection) if scopes else None
        report = await compute_completeness(connection, min_modified=resolved_min_modified, token=token)
        _completeness_cache_put(cache_key, report)
        was_cached = False

    return {
        **report,
        "min_modified": {"value": resolved_min_modified, "source": min_modified_source},
        "cached": was_cached,
        "provisional": _completeness_crawl_running(connection_id),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Extraction breakdown (2026-09-04) — "how many documents did we get, how
# many did we not, broken down by file type and by reason" — the answer an
# operator finishing a large crawl cannot get from any other screen today,
# only by hand-writing SQL against Postgres on the box. A SEPARATE surface
# from A6 completeness above: completeness answers "does the corpus match
# what Graph Search says exists" (an external reference count); this
# answers "of what the crawl itself touched, what became of it" — entirely
# from data the crawl and the corpus already persisted, no Graph calls.
# ---------------------------------------------------------------------------

#: Field names :func:`_run_out` already normalizes per run as ``live =
#: report or progress`` (present in BOTH a finished run's report and a
#: live/abandoned run's progress checkpoint) — reused here rather than
#: re-deriving the same report-or-progress pick a second time.
_BREAKDOWN_LIVE_FIELDS: Tuple[str, ...] = (
    "new",
    "changed",
    "unchanged",
    "renamed",
    "deleted",
    "bytes_downloaded",
    "http_429",
    "throttle_wait_s",
    "filtered_by_age",
    "age_unknown",
    "skipped_unsupported",
    "skipped_doomed",
    "oversize_files",
)

#: Scalars ``_progress_snapshot`` (``connectors/sharepoint/crawler.py``)
#: never carries — only a run that finished long enough to call
#: ``CrawlStats.report()`` has them. An "abandoned" run (worker killed
#: outright, closed by ``ExtractionRunsPgRepository.abandon_stale_
#: running`` with whatever ``report`` it already had — see this module's
#: own docstring on the interrupted-run trap) contributes NOTHING to these,
#: never a lowball number silently averaged in. Read straight off
#: ``report`` (not through ``_run_out``, which does not expose them).
_BREAKDOWN_REPORT_ONLY_FIELDS: Tuple[str, ...] = (
    "permission_skips",
    "excluded_subtree_skips",
    "requests",
    "item_seconds",
    "duration_s",
)

#: The one ``reason_type`` this surface calls out separately from every
#: other convert-stage failure: the document converted fine and produced no
#: extractable text (``CrawlStats.note_failed_item``'s ``convert_empty``
#: call site, reason text always exactly "conversion succeeded but produced
#: no extractable text") — usually a scanned PDF that needs OCR, not a
#: broken pipeline, and typically the single largest cohort in
#: ``failed_items``.
_EMPTY_TEXT_REASON_TYPE = "convert_empty"

#: Every item that reaches the download/convert stage is fetched to a local
#: temp file first (``tempfile.mkstemp()``, default ``"tmp"`` prefix —
#: ``connectors/sharepoint/crawler.py``'s ``download_to_temp``), and a
#: convert-stage exception's message is built as ``f"{filename}: {message}"``
#: (``ConversionError.__init__``, ``src/ingest/convert.py``) where
#: ``filename`` is that temp file's own basename — so 1 444 files that all
#: hit the SAME underlying fault ("markitdown could not convert this file")
#: read as 1 444 DIFFERENT reason strings under a naive ``GROUP BY reason``,
#: one per random temp filename. This strips exactly that prefix so they
#: collapse back into one row. Bounded quantifiers only (no nested/unbounded
#: repetition) — linear-time over tenant-controlled text, per the security
#: playbook; a reason with no such prefix (most non-``ConversionError``
#: faults, e.g. a bare timeout message) passes through unchanged, since
#: those already read the same across files with no per-file noise to strip.
_TMP_FILENAME_PREFIX_RE = re.compile(r"^tmp[\w\-]{1,64}\.[\w]{1,12}:\s*")


def _normalize_failure_reason(reason: str) -> str:
    """A ``failed_items[].reason`` string, with a leading local-temp-filename
    prefix stripped (see :data:`_TMP_FILENAME_PREFIX_RE`) so every file that
    hit the same underlying fault groups under the same row."""
    reason = (reason or "").strip()
    if not reason:
        return "(no reason recorded)"
    stripped = _TMP_FILENAME_PREFIX_RE.sub("", reason, count=1).strip()
    return stripped or reason


def _extension_of_suffix(suffix: Any) -> str:
    """``failed_items``/``skips`` items carry their OWN ``suffix`` (set by
    the crawler at note-time, already the real extension, lowercased,
    ``Path(name).suffix.lower()`` — never a stored-artifact type like
    ``corpus_files.file_type``, which is always ``"md"`` for a converted
    document). Normalized to match :meth:`CorpusFilesRepository.
    extension_status_counts`'s own convention: lowercase, no leading dot,
    ``""`` when absent — so the two sources bucket under the same key."""
    return str(suffix or "").lower().lstrip(".")


def _parse_window_bound(value: Optional[str], *, param: str) -> Optional[datetime]:
    """A ``since``/``until`` query param — ``None`` when absent, else a
    tz-aware ``datetime``. ``400 invalid_{param}`` on anything
    ``datetime.fromisoformat`` rejects (accepts both a bare ``YYYY-MM-DD``
    and a full ISO timestamp) — the same "never build a filter from an
    unchecked value, name which param" posture ``_validate_min_modified``
    (``app/api/admin_sharepoint.py``) already uses for its own date filter.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": f"invalid_{param}"}) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _breakdown_scalars(runs: List[Dict[str, Any]], *, now: datetime) -> Dict[str, Any]:
    """The run-level reconciliation scalars, summed across every run in the
    window — the raw material for "seen reconciles to indexed with nothing
    unexplained in between".

    Each :data:`_BREAKDOWN_LIVE_FIELDS` key is read through :func:`_run_out`
    (the SAME ``live = report or progress`` pick the status/history/fleet
    endpoints already use — reused, not re-derived) so a value present on
    either a finished run's report OR a still-checkpointing run's progress
    counts; :data:`_BREAKDOWN_REPORT_ONLY_FIELDS` are read straight off
    ``report`` and are simply absent from a run that never got one — see
    both data structures' own docstrings for why the split matters (the
    task's own live-fleet trap: an 82-of-96-interrupted connection's
    ``bytes_downloaded`` undercounts by ~3x if only ``report`` is read).
    ``contributed_runs`` names, per key, how many of the window's runs
    actually had a value to add — the honest complement to a bare sum: a
    ``permission_skips`` total built from 14 of 96 runs is a different claim
    than one built from all 96, and this is what lets a caller tell them
    apart instead of reading one indistinguishable number.
    """
    totals: Dict[str, float] = {}
    contributed: Dict[str, int] = {}
    runs_with_report = 0
    runs_progress_only = 0

    def _add(key: str, value: Any) -> None:
        if value is None:
            return
        totals[key] = totals.get(key, 0.0) + float(value)
        contributed[key] = contributed.get(key, 0) + 1

    for run in runs:
        report = run.get("report") or {}
        if report:
            runs_with_report += 1
        else:
            runs_progress_only += 1
        out = _run_out(run, now=now)
        for key in _BREAKDOWN_LIVE_FIELDS:
            _add(key, out.get(key))
        for key in _BREAKDOWN_REPORT_ONLY_FIELDS:
            _add(key, report.get(key))
        _add("concurrency_downshifts", (report.get("concurrency") or {}).get("downshifts"))
        _add("oversize_bytes", (report.get("skipped_oversize") or {}).get("bytes"))

    # Every summed field here is an integer count/byte/request tally except
    # `throttle_wait_s`/`item_seconds`/`duration_s`, which are seconds —
    # rounding those to 1 decimal keeps sub-second precision without a
    # trailing float artifact (`12.300000000000001`) on the wire.
    seconds_keys = {"throttle_wait_s", "item_seconds", "duration_s"}
    values = {k: (round(v, 1) if k in seconds_keys else int(v)) for k, v in totals.items()}
    return {
        "runs_considered": len(runs),
        "runs_with_report": runs_with_report,
        "runs_progress_only": runs_progress_only,
        "values": values,
        "contributed_runs": contributed,
    }


def _breakdown_failures(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """``report.failed_items`` across every run in the window, split into
    the "converted fine, produced no text" cohort
    (:data:`_EMPTY_TEXT_REASON_TYPE`) and everything else, the latter
    grouped by NORMALIZED reason (:func:`_normalize_failure_reason`) —
    without this, 1 444 files failing the SAME markitdown fault would list
    as 1 444 one-row reasons, exactly the naive-``GROUP BY`` bug this
    endpoint exists to not reproduce.

    ``listed`` is the number of itemized failures actually available across
    every contributing run's own (5000-item-capped) ``failed_items`` list —
    NOT a true total: a run's ``failed_items_truncated`` flag says it hit
    its own cap, but the crawl does not persist how many MORE there were
    past it, so this can only ever be a lower bound when ``truncated`` is
    true, never a number this endpoint invents. A run with an empty
    ``report`` (progress-only — see :func:`_breakdown_scalars`) contributes
    no items here at all, since ``failed_items`` is never in ``progress``
    either — this cohort is understated by exactly the same runs that
    undercount every other report-only figure.
    """
    empty_text_count = 0
    empty_text_by_ext: Dict[str, int] = {}
    failed_by_ext: Dict[str, int] = {}
    reasons: Dict[Tuple[str, str], Dict[str, Any]] = {}
    listed = 0
    truncated = False

    for run in runs:
        report = run.get("report") or {}
        items = report.get("failed_items") or []
        if report.get("failed_items_truncated"):
            truncated = True
        for item in items:
            if not isinstance(item, dict):
                continue
            listed += 1
            ext = _extension_of_suffix(item.get("suffix"))
            reason_type = str(item.get("reason_type") or "unknown")
            if reason_type == _EMPTY_TEXT_REASON_TYPE:
                empty_text_count += 1
                empty_text_by_ext[ext] = empty_text_by_ext.get(ext, 0) + 1
                continue
            failed_by_ext[ext] = failed_by_ext.get(ext, 0) + 1
            normalized = _normalize_failure_reason(str(item.get("reason") or ""))
            key = (reason_type, normalized)
            bucket = reasons.setdefault(
                key, {"reason_type": reason_type, "reason": normalized, "count": 0, "by_extension": {}}
            )
            bucket["count"] += 1
            bucket["by_extension"][ext] = bucket["by_extension"].get(ext, 0) + 1

    by_reason = sorted(reasons.values(), key=lambda r: -r["count"])
    return {
        "empty_text": {
            "count": empty_text_count,
            "by_extension": empty_text_by_ext,
            "note": "conversion succeeded but produced no extractable text — usually needs OCR, not a broken pipeline",
        },
        "failed": {
            # `listed` counts every item this loop visited (both cohorts);
            # subtracting the empty-text share leaves the real-failure one
            # without a second pass over every run's `failed_items`.
            "count": listed - empty_text_count,
            "by_extension": failed_by_ext,
        },
        "by_reason": by_reason,
        "listed": listed,
        "truncated": truncated,
    }


def _breakdown_by_extension(
    corpus_counts: Dict[str, Dict[str, Dict[str, int]]],
    *,
    failed_by_ext: Dict[str, int],
    empty_text_by_ext: Dict[str, int],
) -> List[Dict[str, Any]]:
    """One row per file extension — ``corpus_files.path``-derived (via
    :meth:`CorpusFilesRepository.extension_status_counts`, NEVER
    ``filename``/``file_type`` — see that method's own docstring for the
    trap) merged with the failure counts from :func:`_breakdown_failures`,
    keyed the same way (:func:`_extension_of_suffix`).

    ``indexed``/``rejected``/``processing``/``pending``/``needs_review``
    reflect the corpus's CURRENT state — every file ever landed for this
    connection, not scoped to the run window — while ``failed``/
    ``empty_text`` are windowed (only the runs the caller selected). The two
    are DIFFERENT populations by construction whenever a window narrower
    than "every run" is requested; see the response's own
    ``reconciliation.note``. ``needs_review`` (``src/ingest/runner.py``) is
    a DIFFERENT "produced no text" signal from ``empty_text`` above — it
    fires when conversion succeeded and chunking still yielded zero chunks
    (e.g. all-whitespace content), one stage later in the pipeline than the
    crawler's own ``convert_empty`` — kept as its own column rather than
    merged into either sibling.
    """
    _EMPTY_STATUS = {"count": 0, "bytes": 0}
    extensions = set(corpus_counts) | set(failed_by_ext) | set(empty_text_by_ext)
    rows: List[Dict[str, Any]] = []
    for ext in extensions:
        statuses = corpus_counts.get(ext, {})
        rows.append(
            {
                "extension": ext or "(none)",
                "indexed": statuses.get("indexed", _EMPTY_STATUS),
                "rejected": statuses.get("rejected", _EMPTY_STATUS),
                "processing": statuses.get("processing", _EMPTY_STATUS),
                "pending": statuses.get("pending", _EMPTY_STATUS),
                "needs_review": statuses.get("needs_review", _EMPTY_STATUS),
                "failed": failed_by_ext.get(ext, 0),
                "empty_text": empty_text_by_ext.get(ext, 0),
            }
        )
    rows.sort(
        key=lambda r: (
            -(
                r["indexed"]["count"]
                + r["rejected"]["count"]
                + r["processing"]["count"]
                + r["pending"]["count"]
                + r["needs_review"]["count"]
                + r["failed"]
                + r["empty_text"]
            )
        )
    )
    return rows


@router.get("/connections/{connection_id}/extraction/breakdown")
def extraction_breakdown(
    connection_id: str,
    since: Optional[str] = Query(None, description="Only runs started on/after this ISO date/datetime"),
    until: Optional[str] = Query(None, description="Only runs started before this ISO date/datetime"),
    _user: dict = Depends(require_admin),
):
    """ "How many documents did we get, how many did we not, broken down by
    file type and by reason, and how much of the corpus is silently empty"
    (2026-09-04) — read entirely from what the crawl and the corpus already
    persisted (``extraction_runs``/``corpus_files``), no Graph calls, unlike
    A6 completeness above.

    ``since``/``until`` scope which RUNS contribute to ``scalars``/
    ``failures``/``skips`` (default: every run on record for this
    connection — the "everywhere" default the rest of Agnes's read surfaces
    use, per the command-UX scope model — capped defensively at 500 runs,
    newest first, by :meth:`ExtractionRunsPgRepository.list_full_for_
    connection`). ``by_extension``'s ``indexed``/``rejected``/
    ``processing``/``pending`` counts are UNSCOPED by design — they read
    the corpus's current state, not just what this window's runs touched —
    so narrowing the window makes ``reconciliation.unexplained`` an
    increasingly approximate figure; see its own ``note``.

    Every run selected — PARENT (planner) rows AND shard children alike,
    unlike the fleet/history endpoints above which show only parents — so a
    sharded site's real per-shard failures are counted once each rather
    than folded into (or missing from) a near-empty parent report.

    ``400 invalid_since``/``invalid_until`` for a malformed bound (mirrors
    ``_validate_min_modified``'s own posture). ``404`` for an unknown or
    non-SharePoint connection id.

    PG-only, same as every other route in this module: both
    ``extraction_runs`` and ``corpus_files`` resolve through their own
    ``*_repo()`` factory, so a DuckDB-backed instance gets the typed
    ``501`` from whichever resolves first via the app-wide handler in
    ``app/main.py`` — nothing here needs its own DuckDB fallback.
    """
    connection = _sharepoint_connection_or_404(connection_id)
    since_dt = _parse_window_bound(since, param="since")
    until_dt = _parse_window_bound(until, param="until")

    from connectors.sharepoint.facts_extraction import collection_ids_for
    from src.repositories import corpus_files_repo, extraction_runs_repo

    now = datetime.now(timezone.utc)
    runs = extraction_runs_repo().list_full_for_connection(connection_id, since=since_dt, until=until_dt)

    scalars = _breakdown_scalars(runs, now=now)
    failures = _breakdown_failures(runs)

    corpus_ids = collection_ids_for(connection)
    corpus_counts = corpus_files_repo().extension_status_counts(corpus_ids) if corpus_ids else {}
    by_extension = _breakdown_by_extension(
        corpus_counts,
        failed_by_ext=failures["failed"]["by_extension"],
        empty_text_by_ext=failures["empty_text"]["by_extension"],
    )

    indexed_total = sum(row["indexed"]["count"] for row in by_extension)
    needs_review_total = sum(row["needs_review"]["count"] for row in by_extension)
    values = scalars["values"]
    seen = (
        values.get("new", 0) + values.get("changed", 0) + values.get("unchanged", 0) + values.get("filtered_by_age", 0)
    )
    accounted_for = (
        indexed_total
        + needs_review_total
        + failures["failed"]["count"]
        + failures["empty_text"]["count"]
        + values.get("skipped_unsupported", 0)
        + values.get("permission_skips", 0)
        + values.get("excluded_subtree_skips", 0)
        + values.get("oversize_files", 0)
    )

    return {
        "connection_id": connection_id,
        "window": {
            "since": since_dt.isoformat() if since_dt else None,
            "until": until_dt.isoformat() if until_dt else None,
        },
        "runs": {
            "considered": scalars["runs_considered"],
            "with_report": scalars["runs_with_report"],
            "progress_only": scalars["runs_progress_only"],
            "note": (
                "counters below use each run's REPORT when it finished normally, and its live "
                "PROGRESS checkpoint otherwise (an interrupted run whose worker died before writing "
                "a report — see runs.progress_only). Fields report-only by construction "
                "(permission_skips, excluded_subtree_skips, requests, item_seconds, duration_s, "
                "oversize_bytes) are undercounted by exactly those runs; see scalars.contributed_runs."
            ),
        },
        "scalars": {**values, "contributed_runs": scalars["contributed_runs"]},
        "failures": failures,
        "skips": {
            "oversize": {
                "files": values.get("oversize_files", 0),
                "bytes": values.get("oversize_bytes", 0),
            }
        },
        "by_extension": by_extension,
        "reconciliation": {
            "seen": seen,
            "indexed": indexed_total,
            "accounted_for": accounted_for,
            "unexplained": seen - accounted_for,
            "note": (
                "seen = new + changed + unchanged + filtered_by_age, summed across the window's runs. "
                "accounted_for = indexed + needs_review + failed + empty_text + skipped_unsupported + "
                "permission_skips + excluded_subtree_skips + oversize_files. indexed/needs_review read "
                "the corpus's CURRENT state, not just this window, so a since/until narrower than the "
                "connection's full history makes unexplained increasingly approximate rather than "
                "exact — it is never hidden either way."
            ),
        },
        "as_of": now.isoformat(),
    }


# ---------------------------------------------------------------------------
# Anonymization preview — see what a crawl would redact, before running one.
#
# The config drawer above can tell an admin WHICH tiers are on. It cannot
# tell them what those tiers will do to THEIR documents, and that is the
# question actually being asked before a crawl over thousands of files:
# "does this catch our case numbers? does it eat our product codes?" This
# endpoint answers it by running the real anonymizer — the same key
# resolution, the same tier ordering, the same detector choice — over one
# pasted sample.
#
# Three properties make it safe to expose:
#
# * **Nothing is persisted.** No collection row, no extraction run, no
#   stored sample. The only write is the audit row, and that row carries
#   counts, never content.
# * **The response cannot carry an original.** It returns the redacted text
#   (which by construction no longer holds what was redacted) plus the
#   pseudonyms and per-kind counts. `AnonymizeResult.pseudonyms` is
#   deliberately a list of tokens, not a mapping from originals.
# * **No fake key.** An instance with no resolvable key gets a 409 naming
#   `extraction.anonymization.hmac_key_env`, never a preview computed under
#   a throwaway key — a pseudonym an admin cannot reproduce in production is
#   worse than no preview at all.
# ---------------------------------------------------------------------------

#: Ceiling on one pasted sample. A preview is for a representative page or
#: two, not a corpus: the regex tiers are linear but the LLM detector chunks
#: at 30k characters a call, so an unbounded paste is an unbounded bill.
#: Above this the answer is 413 — with the limit named, so the admin trims
#: rather than guesses.
_PREVIEW_MAX_CHARS = 50_000

_PREVIEW_DETECTORS = ("regex", "llm")


class AnonymizationPreviewRequest(BaseModel):
    """Body for ``POST /anonymization/preview``.

    ``detector`` defaults to ``regex`` — never to the instance's configured
    value. The panel names which one it is asking for, and the LLM tier
    spends tokens per call: a default that quietly followed configuration
    would bill an admin for opening a drawer.
    """

    text: str
    detector: Optional[str] = None


def _preview_key() -> bytes:
    """This instance's real anonymization key, or a 409 that names the knob.

    Delegates to ``app.worker.kinds._resolve_anonymization_key`` — the one
    owner of that resolution, including the allowlist check on the
    admin-writable env-var NAME. The key value never leaves this function:
    the raised detail is the resolver's own message, which names the
    variable, never its contents.
    """
    from app.worker.kinds import AnonymizationKeyError, _resolve_anonymization_key

    try:
        return str(_resolve_anonymization_key()).encode("utf-8")
    except AnonymizationKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "anonymization_key_unavailable",
                "message": str(exc),
                "config_key": "extraction.anonymization.hmac_key_env",
            },
        ) from exc


@router.post("/anonymization/preview")
def preview_anonymization(
    request: AnonymizationPreviewRequest,
    user: dict = Depends(require_admin),
):
    """Redact a pasted sample with this instance's real anonymizer.

    Body: ``{text, detector?: "regex" | "llm"}``. Returns the redacted text,
    the per-kind counts, the distinct pseudonyms produced, and — for the
    ``llm`` detector — that call's own token usage, so the cost of the tier
    is visible at the moment it is being evaluated rather than at the end of
    a thousand-document crawl.

    Errors are specific because each one has a different fix: ``413`` (too
    long — trim), ``409`` (no key — set the env var this names), ``400``
    (the instance's ``custom_terms`` are unusable — fix the config), ``502``
    (the LLM tier could not answer — the same fail-closed posture a crawl
    takes, never a silent downgrade to regex-only).

    Audited under ``anonymization.preview`` with the sample's LENGTH and the
    redaction counts. The text itself is never written to the audit trail,
    never logged, and never stored anywhere else: an admin pasting a real
    document into a preview box must not thereby file it into the system
    they are previewing.
    """
    text = request.text or ""
    if not text.strip():
        raise HTTPException(status_code=422, detail="text_required: paste a sample to preview")
    if len(text) > _PREVIEW_MAX_CHARS:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "preview_text_too_long",
                "message": (
                    f"the sample is {len(text)} characters; the preview accepts at most "
                    f"{_PREVIEW_MAX_CHARS}. Paste a representative page rather than the whole document."
                ),
                "limit": _PREVIEW_MAX_CHARS,
            },
        )

    choice = (request.detector or "regex").strip().lower()
    if choice not in _PREVIEW_DETECTORS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown_detector: {choice!r} — expected one of {', '.join(_PREVIEW_DETECTORS)}",
        )

    key = _preview_key()

    from src.anonymization import CustomTermError, anonymize_markdown

    detector: Any = None
    usage: Dict[str, Any] = {}
    llm: Any = None
    # An empty tuple in an `except` clause matches nothing, which is exactly
    # the semantics wanted on the regex path: the LLM module is not imported
    # at all there, so its exception type must not have to exist to write
    # the handler.
    detection_unavailable: tuple = ()
    if choice == "llm":
        from src.anonymization_ner import DetectionUnavailable, LLMDetector, hybrid_detector

        detection_unavailable = (DetectionUnavailable,)

    try:
        if choice == "llm":
            # Constructed INSIDE the try: `LLMDetector()` resolves the model,
            # which for a self-hosted endpoint runs the host and key-env
            # allowlist gates and raises `DetectionUnavailable` on a
            # misconfiguration. Built outside, that escaped as an unhandled
            # 500 — losing precisely the 502 whose message names
            # AGNES_ANONYMIZATION_LLM_HOST_ALLOWLIST as the fix.
            llm = LLMDetector()
            detector = hybrid_detector(llm)
        result = anonymize_markdown(text, key=key, detector=detector)
    except CustomTermError as exc:
        # The operator's own custom_terms are unusable. A 400 rather than a
        # 500: nothing is broken, a value they wrote is, and the message
        # says which entry and why.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "custom_terms_invalid",
                "message": str(exc),
                "config_key": "extraction.anonymization.custom_terms",
            },
        ) from exc
    except detection_unavailable as exc:
        # The same fail-closed posture the crawl takes: the LLM tier could
        # not answer, so there is no answer — never a preview silently
        # computed from the regex tier alone under an "llm" label.
        raise HTTPException(
            status_code=502,
            detail={
                "error": "detector_unavailable",
                "message": (
                    f"the LLM detector could not answer: {exc}. A crawl treats this the same "
                    "way — the document is counted in anonymize_failed and dropped, never "
                    "ingested with a redaction that quietly degraded to regex-only."
                ),
            },
        ) from exc

    if llm is not None:
        usage = dict(llm.last_usage)

    counts = dict(result.counts)
    log_safe(
        user_id=user.get("id"),
        action="anonymization.preview",
        resource="extraction.anonymization",
        params={
            # Length, never content. The sample is an admin-pasted document
            # and the audit trail is the one place it must never land.
            "chars": len(text),
            "detector": choice,
            "counts_by_kind": counts,
            "replaced": result.replaced,
        },
    )

    return {
        "detector": choice,
        "redacted_text": result.text,
        "counts_by_kind": counts,
        "replaced": result.replaced,
        # Tokens only — never the values behind them. See the block comment
        # above: this response is incapable of echoing an original back.
        "entities": [{"kind": kind, "pseudonym": token} for kind, token in result.pseudonyms],
        # `{}` for the regex tier means NO tokens were spent — a different
        # claim from "$0.00", the same distinction the run card draws.
        "usage": usage,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
