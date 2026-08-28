"""Keboola semantic layer — the login-triggered sync + the coverage report.

This module used to own ``POST /api/admin/run-keboola-semantic-layer-refresh``
and its scheduler cadence. Both are gone (#1707 Block 3 step 4): the ONE
scheduled semantic refresh is now the generic sweep over ``semantic_sources``
(``app/api/semantic_sources_refresh.py``), which auto-registers one source per
Keboola connection holding a master token and imports it under the SAME
``(source='keboola_metastore', source_ref=<connection id>)`` provenance this
connector has always stamped — see ``src/semantic/legacy_migration.py``.

What stayed here, because neither is a scheduled trigger:

* :func:`run_semantic_layer_refresh_background` — the Keboola multi-project
  login flow provisions master tokens and wants the metrics live without
  waiting for the next sweep. It runs ``sync_semantic_layer()``, i.e. the same
  adapter + central projector under the same provenance, so it can never
  duplicate what the sweep writes.
* ``GET /api/admin/semantic-layer/coverage`` — a read-only report.

Single-flight guarded: the background sync claims the slot before it starts,
so two logins landing together cannot race a second Metastore fetch +
upsert/prune pass against the same rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends

from app.auth.access import require_admin
from connectors.keboola.semantic_layer import sync_semantic_layer

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
    """The last completed LOGIN-TRIGGERED sync's summary, without reaching
    into the module-private `_refresh_state` dict directly.

    The /admin/semantic-layer status strip reads the generic sweep's summary
    (``app.api.semantic_sources_refresh.get_last_refresh_summary``) since the
    scheduled trigger moved there — this one covers the background path that
    stayed behind."""
    return {
        "last_completed_at": _refresh_state.get("last_completed_at"),
        "last_status": _refresh_state.get("last_status"),
        "last_result": _refresh_state.get("last_result"),
    }


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
