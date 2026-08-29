"""Keboola semantic layer refresh — owner of the sync_semantic_layer() call path.

POST /api/admin/run-keboola-semantic-layer-refresh — called by the
scheduler container (auth: shared scheduler token resolves to a synthetic
admin user, same mechanism as app/api/bq_metadata_refresh.py) on the
SCHEDULER_KEBOOLA_SEMANTIC_LAYER_REFRESH_INTERVAL cadence. Also callable by
a real admin on demand.

Single-flight guarded (mirrors app/api/bq_metadata_refresh.py): a second
concurrent call while a sync is in flight gets 409 already_running instead
of racing a second Metastore fetch + upsert/prune pass against the same
metric_definitions rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from connectors.keboola.semantic_layer import (
    MasterTokenRequiredError,
    sync_semantic_layer,
)
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)
router = APIRouter()

_refresh_lock = asyncio.Lock()
# Claimed SYNCHRONOUSLY (no await between check and set) before either caller
# touches the lock. `_refresh_lock.locked()` alone leaves the skip decision
# and the acquisition as two steps; the uncontended asyncio.Lock.acquire fast
# path happens not to yield between them on CPython, but that is an
# implementation detail, and a second caller passing the check would QUEUE a
# full duplicate sync instead of skipping (Devin Review on PR #1328). The
# flag flip is atomic on the single event loop by construction.
_refresh_claimed = False
# In-flight tracking (`run_id`/`started_at`, cleared once a run finishes) plus
# the LAST COMPLETED run's summary (`last_completed_at`/`last_status`/
# `last_result`), so an admin who hasn't synced yet — or whose last sync
# failed — sees that state instead of nothing (#953). Deliberately in-memory
# (since last process restart) rather than a new DB table/migration: cheap,
# low-risk v1 for a status display.
_refresh_state: dict[str, Any] = {
    "run_id": None,
    "started_at": None,
    "last_completed_at": None,
    "last_status": None,
    "last_result": None,
}


def get_last_refresh_summary() -> dict[str, Any]:
    """Read accessor for the admin UI — the last completed run's summary,
    without reaching into the module-private `_refresh_state` dict directly."""
    return {
        "last_completed_at": _refresh_state.get("last_completed_at"),
        "last_status": _refresh_state.get("last_status"),
        "last_result": _refresh_state.get("last_result"),
    }


# Error codes `sync_semantic_layer` attaches to a returned {"status": "error"}
# and the HTTP status each deserves. Anything unmapped (including a missing
# code, e.g. an older caller) stays 502, so the fallback is the historical
# behavior rather than a silently-wrong 400.
_ERROR_CODE_STATUS = {
    "credentials_not_configured": 400,
    "upstream_client_error": 400,
    # The connection's master token opens a different project than the one it
    # is bound to — a mis-paste to correct, not an outage.
    "project_mismatch": 400,
    # The stored token is no longer a master token. The single-source paths
    # reach this endpoint as a raised MasterTokenRequiredError and answer 400;
    # the multi-source loop captures it per connection, so it needs the code
    # to answer the same.
    "master_token_required": 400,
    "upstream_error": 502,
}


def _status_for_error_code(code: Any) -> int:
    """HTTP status for a sync error code — 400 when the admin can fix it
    (nothing configured, Keboola refused the token), 502 when the upstream is
    genuinely unreachable or broken."""
    return _ERROR_CODE_STATUS.get(code, 502) if isinstance(code, str) else 502


def _record_completion(status: str, result: Any) -> None:
    _refresh_state["last_completed_at"] = datetime.now(timezone.utc).isoformat()
    _refresh_state["last_status"] = status
    _refresh_state["last_result"] = result


async def run_semantic_layer_refresh_background(*, trigger: str) -> None:
    """Fire the same guarded sync the admin endpoint owns, for background
    callers (the Keboola multi-project login provisions master tokens and
    wants the metrics live without an admin click). Shares the single-flight
    lock and the status dict, so the admin UI shows these runs too. Skips
    silently when a run is already in flight — the next login or the
    scheduler catches up — and never raises. The skip decision and the slot
    acquisition are one atomic step (see ``_refresh_claimed``)."""
    global _refresh_claimed
    if _refresh_claimed or _refresh_lock.locked():
        logger.info("keboola semantic layer refresh (%s): already running, skipped", trigger)
        return
    _refresh_claimed = True
    try:
        async with _refresh_lock:
            run_id = uuid.uuid4().hex[:8]
            _refresh_state["run_id"] = run_id
            _refresh_state["started_at"] = datetime.now(timezone.utc).isoformat()
            try:
                result = await asyncio.to_thread(sync_semantic_layer)
            except Exception as e:  # noqa: BLE001 — background: record, never raise
                _record_completion("error", str(e))
                logger.warning("keboola semantic layer refresh (%s) failed: %s", trigger, e)
                return
            finally:
                _refresh_state["run_id"] = None
                _refresh_state["started_at"] = None
            if result.get("status") == "error":
                _record_completion("error", result.get("error", "Keboola semantic layer sync failed"))
                logger.warning(
                    "keboola semantic layer refresh (%s) reported an error: %s", trigger, result.get("error")
                )
                return
            _record_completion("ok", result)
            logger.info(
                "keboola semantic layer refresh (%s): run_id=%s created_or_updated=%s pruned=%s sources=%s",
                trigger,
                run_id,
                result.get("created_or_updated"),
                result.get("pruned"),
                len(result.get("sources") or []),
            )
    finally:
        _refresh_claimed = False


@router.post("/api/admin/run-keboola-semantic-layer-refresh")
async def run_keboola_semantic_layer_refresh(
    user: dict = Depends(require_admin),
):
    """Sync the configured Keboola project's semantic layer into
    metric_definitions. See connectors/keboola/semantic_layer.py for the
    mapping/prune logic.

    409 if a sync is already in flight. 400 when the sync fails for a reason
    the admin controls — no Keboola credentials configured, or a token the
    Storage/Metastore API refuses (4xx). 502 only when the upstream is
    unreachable or answers 5xx.
    """
    global _refresh_claimed
    if _refresh_claimed or _refresh_lock.locked():
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "already_running",
                "run_id": _refresh_state.get("run_id"),
                "started_at": _refresh_state.get("started_at"),
                "hint": "A refresh is already in flight; this caller is a no-op.",
            },
        )

    _refresh_claimed = True
    try:
        async with _refresh_lock:
            run_id = uuid.uuid4().hex[:8]
            started_at = datetime.now(timezone.utc).isoformat()
            _refresh_state["run_id"] = run_id
            _refresh_state["started_at"] = started_at
            try:
                result = await asyncio.to_thread(sync_semantic_layer)
            except MasterTokenRequiredError as e:
                _record_completion("error", str(e))
                raise HTTPException(status_code=400, detail=str(e))
            except Exception as e:
                _record_completion("error", str(e))
                raise
            else:
                # sync_semantic_layer() reports config/upstream failures (missing
                # credentials, Storage/Metastore API errors) as a returned
                # {"status": "error"} dict rather than an exception — those must
                # be recorded and surfaced the same way as the exception paths
                # above, or the admin UI shows a false "OK" after a failed sync.
                if result.get("status") == "error":
                    message = result.get("error", "Keboola semantic layer sync failed")
                    _record_completion("error", message)
                    # Only a real upstream failure is a Bad Gateway. "Nothing is
                    # configured yet" and "Keboola refused this token" are the
                    # admin's to fix, and answering 502 for them reads as an Agnes
                    # outage — the exact misdiagnosis this endpoint kept causing.
                    raise HTTPException(status_code=_status_for_error_code(result.get("code")), detail=message)
                _record_completion("ok", result)
            finally:
                _refresh_state["run_id"] = None
                _refresh_state["started_at"] = None
    finally:
        _refresh_claimed = False

    logger.info(
        "keboola semantic layer refresh: run_id=%s status=%s created_or_updated=%s "
        "pruned=%s skipped_unresolved_table=%s skipped_foreign_alias=%s "
        "skipped_embedded_comment=%s sources=%s",
        run_id,
        result.get("status"),
        result.get("created_or_updated"),
        result.get("pruned"),
        result.get("skipped_unresolved_table"),
        result.get("skipped_foreign_alias"),
        result.get("skipped_embedded_comment"),
        len(result.get("sources") or []),
    )
    log_safe(
        user_id=user.get("id"),
        action="run_keboola_semantic_layer_refresh",
        resource="job:keboola-semantic-layer-refresh",
        params={
            "run_id": run_id,
            "status": result.get("status"),
            "created_or_updated": result.get("created_or_updated"),
            "pruned": result.get("pruned"),
            "sources": len(result.get("sources") or []),
        },
    )
    return {**result, "run_id": run_id, "started_at": started_at}


@router.get("/api/admin/semantic-layer/coverage")
async def get_semantic_layer_coverage(
    warnings_only: bool = False,
    user: dict = Depends(require_admin),
):
    """How much of each connected Keboola project's semantic layer actually
    reaches Agnes, recomputed live (see
    ``connectors.keboola.semantic_layer.compute_semantic_coverage``).

    Read-only and stateless — it does not touch metric_definitions and does not
    read the last sync's counters, which live in a process-local dict that
    empties on restart. Two conditions are worth acting on and are surfaced as
    ``warnings[]``: a connection whose storage and master tokens point at
    different projects, and a project none of whose metrics can bind to a
    registered table. Tables the instance simply does not register are reported
    as a plain count, never as pending work.

    ``?warnings_only=true`` answers with the token-identity checks alone and
    skips the Metastore enumeration — two `verify_token` calls per connection
    instead of every project's whole semantic model. The Data sources page
    draws its warning strip from that: it uses only the mismatch messages, and
    pulling the full report on every page view was work nobody asked for.
    Counts are zeroed in that mode. (Devin Review on this PR.)

    Upstream calls run off the event loop — one project's Metastore being slow
    must not stall every other request in the process.
    """
    from connectors.keboola.semantic_layer import compute_semantic_coverage

    return await asyncio.to_thread(compute_semantic_coverage, warnings_only=warnings_only)
