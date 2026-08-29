"""Semantic sources refresh — the ONE generic scheduled sync over every
registered ``semantic_sources`` row (git / upload / connection kinds).

POST /api/admin/run-semantic-sources-refresh — called by the scheduler
container (auth: shared scheduler token, same mechanism as
app/api/keboola_semantic_layer_refresh.py) on the
SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL cadence. Also callable by a real
admin on demand.

Scope (Block 3 step 2 of issue #1707): this walks every row in
``semantic_sources`` and calls ``src.semantic.transports.import_source`` on
each one whose ``enabled`` flag is not ``False``. Disabled rows are skipped
and counted, never synced.

Deliberately NOT touched here: the legacy Keboola
(``/api/admin/run-keboola-semantic-layer-refresh``) and Databricks
(``/api/admin/run-databricks-semantic-layer-refresh``) refresh endpoints.
They keep running on their own schedules, with their own provenance labels
(``keboola_semantic_layer`` / the pre-cutover Databricks writer) and their
own prune scopes, until steps 3-4 of #1707 migrate their callers onto this
generic sweep and retire them. The Databricks refresh is NOT disjoint from
this sweep: post Phase-1 cutover it calls the very same
``import_source('databricks_default')`` on the very same row, stamping the
identical ``ossie_connection``/``databricks_default`` provenance — so the
sweep SKIPS that row (``skipped_legacy_owned``) while the dedicated job
still owns it, instead of racing it twice per 6 h tick. The skip and the
legacy job are removed together in steps 3-4. Migrating/removing the
legacy paths is otherwise out of scope for this change.

Single-flight guarded (mirrors the Keboola/Databricks siblings): a second
concurrent call while a sweep is in flight gets 409 already_running instead
of racing a duplicate pass over the same semantic_sources rows.

A single failing source's ``import_source`` call is caught per-source and
never aborts the sweep over the rest. ``import_source`` already records
``last_sync_at`` / ``last_sync_status`` / ``last_sync_error`` on the source
row itself (success or failure) — this module does not write that state a
second time, it only aggregates the per-run HTTP response.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from src.repositories import semantic_source_repo
from src.semantic.transports import import_source
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)

# Rows a dedicated legacy refresh job still imports itself (same
# import_source call, same provenance) — the sweep must not run them a
# second time per cycle. Removed together with those jobs in #1707
# steps 3-4.
from connectors.databricks.semantic_layer import DATABRICKS_SEMANTIC_SOURCE_ID

_LEGACY_REFRESH_OWNED_SOURCE_IDS = frozenset({DATABRICKS_SEMANTIC_SOURCE_ID})
router = APIRouter()

_refresh_lock = asyncio.Lock()
# In-flight bookkeeping only — both fields are read by the 409 branch below.
_refresh_state: dict[str, Any] = {
    "run_id": None,
    "started_at": None,
}


def _run_sweep() -> dict[str, Any]:
    """Runs off the event loop. Walks every registered semantic source,
    skips disabled ones, imports the rest, and never lets one failure abort
    the sweep."""
    repo = semantic_source_repo()
    sources = repo.list_all()

    synced = 0
    failed = 0
    skipped_disabled = 0
    skipped_legacy_owned = 0
    results: list[dict[str, Any]] = []

    for source in sources:
        source_id = source["id"]
        name = source.get("name") or source_id
        # `enabled` defaults TRUE at the schema level; treat only an
        # explicit False as "skip" so a NULL/missing value never silently
        # excludes a source.
        if source.get("enabled") is False:
            skipped_disabled += 1
            results.append({"id": source_id, "name": name, "status": "skipped_disabled"})
            continue
        # A row still owned by a dedicated legacy refresh job would be
        # imported TWICE per cycle under the same provenance (the legacy
        # Databricks endpoint calls import_source on this very row) — skip
        # it here until steps 3-4 of #1707 retire that job and this set.
        if source_id in _LEGACY_REFRESH_OWNED_SOURCE_IDS:
            skipped_legacy_owned += 1
            results.append({"id": source_id, "name": name, "status": "skipped_legacy_owned"})
            continue
        try:
            import_source(source_id)
        except Exception as exc:  # noqa: BLE001 - recorded per-source, sweep continues
            failed += 1
            results.append({"id": source_id, "name": name, "status": "error", "error": str(exc)})
            logger.warning("semantic sources refresh: source %s failed: %s", source_id, exc)
            continue
        synced += 1
        results.append({"id": source_id, "name": name, "status": "ok"})

    return {
        "status": "ok",
        "synced": synced,
        "failed": failed,
        "skipped_disabled": skipped_disabled,
        "skipped_legacy_owned": skipped_legacy_owned,
        "sources": results,
    }


@router.post("/api/admin/run-semantic-sources-refresh")
async def run_semantic_sources_refresh(
    user: dict = Depends(require_admin),
):
    """Sync every enabled ``semantic_sources`` row through the shared
    git/upload/connection import pipeline. Disabled sources are skipped and
    counted, never synced — manually or on this schedule. See the module
    docstring for scope versus the legacy Keboola/Databricks endpoints.

    409 if a sweep is already in flight.
    """
    if _refresh_lock.locked():
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "already_running",
                "run_id": _refresh_state.get("run_id"),
                "started_at": _refresh_state.get("started_at"),
                "hint": "A refresh is already in flight; this caller is a no-op.",
            },
        )

    async with _refresh_lock:
        run_id = uuid.uuid4().hex[:8]
        started_at = datetime.now(timezone.utc).isoformat()
        _refresh_state["run_id"] = run_id
        _refresh_state["started_at"] = started_at
        try:
            result = await asyncio.to_thread(_run_sweep)
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    logger.info(
        "semantic sources refresh: run_id=%s synced=%s failed=%s skipped_disabled=%s",
        run_id,
        result["synced"],
        result["failed"],
        result["skipped_disabled"],
    )
    log_safe(
        user_id=user.get("id"),
        action="run_semantic_sources_refresh",
        resource="job:semantic-sources-refresh",
        params={
            "run_id": run_id,
            "synced": result["synced"],
            "failed": result["failed"],
            "skipped_disabled": result["skipped_disabled"],
            "skipped_legacy_owned": result.get("skipped_legacy_owned", 0),
        },
    )
    return {**result, "run_id": run_id, "started_at": started_at}
