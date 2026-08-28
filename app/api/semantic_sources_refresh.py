"""Semantic sources refresh — the ONE scheduled sync over every registered
``semantic_sources`` row (git / upload / connection kinds).

POST /api/admin/run-semantic-sources-refresh — called by the scheduler
container (auth: shared scheduler token, resolved to a synthetic admin user)
on the SCHEDULER_SEMANTIC_SOURCES_REFRESH_INTERVAL cadence. Also callable by
a real admin on demand; the /admin/semantic-layer page's "Sync now" button
posts here.

Scope (Block 3 of issue #1707): this walks every row in ``semantic_sources``
and calls ``src.semantic.transports.import_source`` on each one whose
``enabled`` flag is not ``False``. Disabled rows are skipped and counted,
never synced.

Since step 4 of #1707 this is the ONLY scheduled semantic refresh. The two
connector-owned endpoints that used to run beside it —
``run-keboola-semantic-layer-refresh`` and
``run-databricks-semantic-layer-refresh``, each with its own scheduler
cadence, provenance label and prune scope — are gone. Their sync LOGIC is
not: the same adapters compose the same documents and the same central
projector writes them. What moved is the trigger, and
``src.semantic.legacy_migration`` is what makes that a no-op for an existing
instance:

* ``ensure_legacy_semantic_sources()`` runs at the start of every sweep and
  registers the ``semantic_sources`` row each retired trigger implied — one
  per Keboola connection holding a master token, plus the Databricks
  workspace when one is configured. A migrated Keboola row carries a
  provenance override so its rows keep the exact ``(source, source_ref)``
  pair they already have; re-importing the same upstream under a new label
  would orphan every existing metric and write a duplicate beside it.
* ``reconcile_after_import()`` runs after each successful import and performs
  the post-sync legacy-row cleanup those endpoints used to do inline.

Both are failure-isolated: a migration or reconciliation that raises is
logged and the sweep continues. A sweep that could not migrate is still a
sweep over whatever is registered.

Single-flight guarded: a second concurrent call while a sweep is in flight
gets 409 already_running instead of racing a duplicate pass over the same
semantic_sources rows.

A single failing source's ``import_source`` call is caught per-source and
never aborts the sweep over the rest. ``import_source`` already records
``last_sync_at`` / ``last_sync_status`` / ``last_sync_error`` on the source
row itself (success or failure) — this module does not write that state a
second time, it only aggregates the per-run HTTP response, plus the
in-memory last-completed summary the admin page's status strip reads.
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
from src.semantic.legacy_migration import ensure_legacy_semantic_sources, reconcile_after_import
from src.semantic.transports import import_source

logger = logging.getLogger(__name__)
router = APIRouter()

_refresh_lock = asyncio.Lock()
# In-flight bookkeeping (`run_id`/`started_at`, cleared once a run finishes)
# plus the LAST COMPLETED run's summary, so an admin who has not synced yet —
# or whose last sync failed — sees that state instead of nothing. Deliberately
# in-memory (since last process restart) rather than a new table: each source
# row already carries its own durable `last_sync_*`; this is the whole-sweep
# view the status strip renders.
_refresh_state: dict[str, Any] = {
    "run_id": None,
    "started_at": None,
    "last_completed_at": None,
    "last_status": None,
    "last_result": None,
}


def get_last_refresh_summary() -> dict[str, Any]:
    """Read accessor for the admin UI — the last completed sweep's summary,
    without reaching into the module-private `_refresh_state` dict."""
    return {
        "last_completed_at": _refresh_state.get("last_completed_at"),
        "last_status": _refresh_state.get("last_status"),
        "last_result": _refresh_state.get("last_result"),
    }


def _record_completion(status: str, result: Any) -> None:
    _refresh_state["last_completed_at"] = datetime.now(timezone.utc).isoformat()
    _refresh_state["last_status"] = status
    _refresh_state["last_result"] = result


def _run_sweep() -> dict[str, Any]:
    """Runs off the event loop. Migrates the legacy refreshes' sources on
    first sight, then walks every registered semantic source, skips disabled
    ones, imports the rest, and never lets one failure abort the sweep."""
    migrated = [row["id"] for row in ensure_legacy_semantic_sources()]

    repo = semantic_source_repo()
    sources = repo.list_all()

    synced = 0
    failed = 0
    skipped_disabled = 0
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
        try:
            report = import_source(source_id)
        except Exception as exc:  # noqa: BLE001 - recorded per-source, sweep continues
            failed += 1
            results.append({"id": source_id, "name": name, "status": "error", "error": str(exc)})
            logger.warning("semantic sources refresh: source %s failed: %s", source_id, exc)
            continue
        synced += 1
        entry: dict[str, Any] = {"id": source_id, "name": name, "status": "ok"}
        # Post-sync cleanup a migrated legacy source still owes (see
        # src/semantic/legacy_migration.py). Best-effort by contract: a
        # reconciliation failure never turns a successful sync into a
        # failed one.
        reconciled = reconcile_after_import(source, report)
        if reconciled:
            entry["reconciled_legacy"] = reconciled
        results.append(entry)

    return {
        "status": "ok",
        "synced": synced,
        "failed": failed,
        "skipped_disabled": skipped_disabled,
        "migrated": migrated,
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
        except Exception as exc:
            # Recorded before it propagates: an admin whose sweep blew up
            # must see the failure in the status strip, not a stale "OK"
            # from the run before it.
            _record_completion("error", str(exc))
            raise
        else:
            _record_completion("ok", result)
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    logger.info(
        "semantic sources refresh: run_id=%s synced=%s failed=%s skipped_disabled=%s migrated=%s",
        run_id,
        result["synced"],
        result["failed"],
        result["skipped_disabled"],
        len(result["migrated"]),
    )
    return {**result, "run_id": run_id, "started_at": started_at}
