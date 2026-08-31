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

**PG-only, and honest about it.** ``extraction_runs`` is a post-A3 table, so
resolving its repository on a DuckDB-backed instance raises the typed
``RequiresPostgresBackend``, which the app-wide handler in ``app/main.py``
turns into a clean ``501 requires_postgres_backend``. These handlers let it
surface rather than improvising an empty-but-healthy-looking answer — the
card stops polling on a 501 and says why (design §4.4). ``…/extraction/config``
reads no run rows and therefore answers on BOTH backends: configuration is
knowable without a database.

**Liveness is DERIVED, never trusted.** A SIGKILLed worker finalizes
nothing, so a row can say ``running`` forever. ``status`` therefore reports
``stalled`` for a run whose last checkpoint is older than
:data:`_STALL_AFTER_S` (and says how old), and consults the run's ``jobs``
row when one is known. Nothing here renders an unbounded "running" pulse.

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sharepoint", tags=["admin"])


#: How stale a ``running`` run's last checkpoint may be before the UI is
#: told to call it ``stalled``. The crawl checkpoints once per 200-row delta
#: page; a page that downloads and converts 200 documents can legitimately
#: take many minutes, so this is deliberately generous — several times the
#: worst plausible cadence. Being late to say "stalled" costs an admin a
#: little patience; being early costs them trust in every other number here.
_STALL_AFTER_S = 1800

#: Run outcomes in SEVERITY order. A crashed run is both "did not finish"
#: and "broke"; the more severe word wins, always, so a crash can never be
#: softened into the benign `interrupted` (with its reassuring "the next run
#: resumes" copy). `stalled` is derived at read time and never stored.
OUTCOME_PRECEDENCE = ("failed", "stalled", "interrupted", "done", "running")


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

    Best-effort by construction: nothing supplies ``job_id`` today (the
    worker hands the crawl the job's payload, not its id), and a lookup
    failure is a missing signal, not an error — the checkpoint-age fallback
    below still answers.
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
    if stale_s is not None and stale_s > _STALL_AFTER_S:
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


def _run_out(run: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """One run, in the shape both the history drawer and the status endpoint
    render. Absolute counters only — no fraction, no percentage, no ETA."""
    report = run.get("report") or {}
    progress = run.get("progress") or {}
    live = report or progress
    outcome = _derived_outcome(run, now=now)
    skips = run.get("skips") or {}
    return {
        "id": run.get("id"),
        "job_id": run.get("job_id"),
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
        "deleted": live.get("deleted"),
        "bytes_downloaded": live.get("bytes_downloaded"),
        "bytes_downloaded_human": live.get("bytes_downloaded_human"),
        "http_429": live.get("http_429"),
        "throttle_wait_s": live.get("throttle_wait_s"),
        "errors": live.get("errors"),
        "skips_total": skips.get("total"),
        "skips_listed": skips.get("listed"),
        "oversize_files": (report.get("skipped_oversize") or {}).get("files", progress.get("oversize_files")),
        "error": run.get("error"),
        # `{}` means NO tokens were spent, which is a different claim from
        # "$0.00" — the card must keep the two tellable apart (design §7.2).
        "usage": run.get("usage") or {},
    }


@router.get("/connections/{connection_id}/extraction/status")
async def extraction_status(
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
    """
    _sharepoint_connection_or_404(connection_id)
    from src.repositories import extraction_runs_repo

    repo = extraction_runs_repo()
    now = datetime.now(timezone.utc)
    running = repo.get_running(connection_id)
    last_completed = repo.last_completed(connection_id)
    return {
        "connection_id": connection_id,
        "running": _run_out(running, now=now) if running else None,
        "last_completed": _run_out(last_completed, now=now) if last_completed else None,
        "runs_total": repo.count_for_connection(connection_id),
        # v1 has no cooperative cancel flag in the crawl, so there is no
        # honest Stop control to draw. Stated as data rather than left for
        # the template to assume — a button without a mechanism is a lie.
        "can_stop": False,
        "as_of": now.isoformat(),
    }


@router.get("/connections/{connection_id}/extraction/runs")
async def extraction_runs(
    connection_id: str,
    limit: int = Query(10, ge=1, le=100),
    _user: dict = Depends(require_admin),
):
    """A2 — the run-history drawer's rows, newest first.

    ``total`` is every recorded run, not just the returned page, so "5 more
    runs" is never a silent truncation.
    """
    _sharepoint_connection_or_404(connection_id)
    from src.repositories import extraction_runs_repo

    repo = extraction_runs_repo()
    now = datetime.now(timezone.utc)
    rows = repo.list_for_connection(connection_id, limit=limit)
    return {
        "connection_id": connection_id,
        "runs": [_run_out(r, now=now) for r in rows],
        "total": repo.count_for_connection(connection_id),
        "as_of": now.isoformat(),
    }


@router.get("/connections/{connection_id}/extraction/runs/{run_id}")
async def extraction_run_detail(
    connection_id: str,
    run_id: str,
    _user: dict = Depends(require_admin),
):
    """A3 — one run's stored report, usage and capped skip list.

    The skip list carries ``listed`` alongside ``total``: only oversize
    skips keep a path, so a run that refused 27 documents and can name 20 of
    them says exactly that instead of implying the list is the whole story.
    """
    _sharepoint_connection_or_404(connection_id)
    from src.repositories import extraction_runs_repo

    run = extraction_runs_repo().get(run_id)
    if run is None or run.get("connection_id") != connection_id:
        raise HTTPException(status_code=404, detail="run_not_found")
    out = _run_out(run)
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


def _extraction_config_rows() -> List[Dict[str, Any]]:
    """The effective ``extraction`` block, one row per leaf (design §6.2).

    ``extraction.producer.*`` is deliberately absent: the built-in pipeline
    has no producer command, and rendering a command line an admin cannot
    change (and this instance does not run) would be noise at best and a
    pointer at an executable at worst.
    """
    return [
        _config_row("Enabled", ("extraction", "enabled"), env_var="AGNES_EXTRACTION_ENABLED", default=False),
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
                "external-producer mode only — the built-in crawl is not a subprocess and is not killed by this value"
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
        _config_row(
            "NER detector",
            ("extraction", "anonymization", "detector"),
            default="regex",
            note=(
                "regex = the deterministic detector only, no LLM call and no tokens spent — "
                "which is a different claim from a $0.00 cost"
            ),
        ),
        _ner_model_row(),
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
    ]


@router.get("/connections/{connection_id}/extraction/config")
async def extraction_config(
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
    """
    connection = _sharepoint_connection_or_404(connection_id)

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

    return {
        "connection_id": connection_id,
        "effective": _extraction_config_rows(),
        "scopes": scopes,
        # Stated once, at the top of the drawer: the whole section is
        # deploy-time by design, and the reason is a security one.
        "section_editable": False,
        "section_lock_reason": (
            "The `extraction` section is not admin-writable. Making one key editable makes the "
            "WHOLE section editable (server-config validates the section name, then deep-merges), "
            "and this section is where a producer command line lives — that is a security "
            "decision, not a UX one."
        ),
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
