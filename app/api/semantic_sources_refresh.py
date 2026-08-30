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
* ``claim_source_for_import()`` and ``duplicate_upstream_reason()`` carry over
  the two guards those triggers had built in: a migrated Keboola source is
  skipped (``skipped_running``) while the login-triggered sync that writes the
  same rows is in flight, and the second of two sources resolving to ONE
  upstream project is skipped (``skipped_duplicate_project``) instead of
  importing that project a second time under a second prune scope.

The Databricks row (``databricks_default``) is now swept like any other:
exactly once per run, by this sweep alone. While the dedicated Databricks
refresh still existed, the two were NOT disjoint — that job called the very
same ``import_source('databricks_default')`` on the very same row under the
identical ``ossie_connection``/``databricks_default`` provenance, so the sweep
had to skip the row (``skipped_legacy_owned``) to avoid importing it twice per
tick. Retiring the job removed the second writer and with it the reason for
the skip, so both are gone and the ``skipped_legacy_owned`` counter no longer
appears in this endpoint's response.

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
in-memory last-completed summary the admin page's status strip reads. That
per-row state is also what ``get_sync_status_summary()`` falls back to when
the in-memory summary is empty, so a freshly restarted process reports the
sources' real history instead of claiming nothing ever synced.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from src.audit_helpers import log_safe
from src.repositories import semantic_source_repo
from src.semantic.legacy_migration import (
    claim_source_for_import,
    duplicate_upstream_reason,
    ensure_legacy_semantic_sources,
    new_sweep_state,
    reconcile_after_import,
)
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
    without reaching into the module-private `_refresh_state` dict.

    In-memory only, by the design above: empty means "no sweep in THIS
    process", never "nothing has ever synced". Callers rendering a claim
    about history want `get_sync_status_summary()` instead.
    """
    return {
        "last_completed_at": _refresh_state.get("last_completed_at"),
        "last_status": _refresh_state.get("last_status"),
        "last_result": _refresh_state.get("last_result"),
    }


#: `last_sync_status` values that mean the source was actually IMPORTED
#: from — the only ones the fallback may speak for. `record_sync` also
#: stamps `last_sync_at` for a `'skipped'` row (the duplicate-upstream skip
#: below), which never ran an import: counting one would let a source that
#: has never been read set the "last source sync" time, i.e. the same
#: over-claim this fallback exists to remove.
_ATTEMPTED_SYNC_STATUSES = frozenset({"ok", "error"})


def _as_iso(value: Any) -> str | None:
    """One comparable, renderable form for a `last_sync_at` off either
    backend — DuckDB hands back a naive datetime, Postgres a tz-aware one,
    and a raw string is tolerated.

    Normalized to second precision HERE, before the max() below, so what is
    compared is exactly what is rendered: microseconds are noise in a status
    strip, and trimming after the comparison could print a stamp that was
    never the one selected. Within one backend the format stays uniform, so
    max() over these still sorts chronologically.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return value.strip() or None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return None


def _no_fallback(*, unavailable: bool = False) -> dict[str, Any]:
    return {"last_sync_at": None, "synced_count": 0, "source_count": 0, "unavailable": unavailable}


def _last_source_sync() -> dict[str, Any]:
    """The durable fallback: `max(last_sync_at)` across the `semantic_sources`
    rows that have actually been imported from, how many those are, and how
    many are registered in total.

    Read-time derivation over state each row already carries — no new table,
    no schema change, and `list_all()` exists on both halves of the pair, so
    a DuckDB instance answers this exactly as a Postgres one does.

    A read that fails reports `unavailable`, NOT an empty result: rendering
    "never synced" because the sources could not be read would be a claim
    about history made from a failure to read it — the very shape of bug this
    function exists to fix. Either way it degrades rather than 500-ing the
    page it decorates.
    """
    try:
        sources = semantic_source_repo().list_all()
    except Exception as exc:  # noqa: BLE001 - a status strip must not break the page
        logger.warning("semantic sources refresh: could not read the sources' last sync: %s", exc)
        return _no_fallback(unavailable=True)
    stamps = [
        iso
        for iso in (
            _as_iso(source.get("last_sync_at"))
            for source in sources
            if (source.get("last_sync_status") or "") in _ATTEMPTED_SYNC_STATUSES
        )
        if iso
    ]
    return {
        "last_sync_at": max(stamps) if stamps else None,
        "synced_count": len(stamps),
        "source_count": len(sources),
        "unavailable": False,
    }


def get_sync_status_summary() -> dict[str, Any]:
    """What the /admin/semantic-layer status strip renders — the sweep view
    when there is one, a truthful fallback when there is not.

    `_refresh_state` is deliberately in-memory, so EVERY redeploy empties it.
    The strip used to read that emptiness as "Never synced yet." — a claim
    about history made from a fact about this process — while
    /admin/semantic-sources listed the same sources synced hours earlier.

    Four states, and the label moves with the meaning:

    * a sweep ran in this process — render it, it is the richer view
      (counts, per-source results), whether it succeeded or failed;
    * no sweep here, but sources have been imported from — report THAT, and
      say what it is: the last sync of any source, not a sweep;
    * no sweep here and the sources cannot be read — say the status is
      unavailable, never that nothing synced;
    * none of the above — nothing has ever synced, and only there is the old
      sentence true.
    """
    summary = get_last_refresh_summary()
    # Only when there is no sweep to show: the richer in-memory view always
    # wins, and this way the common path costs no query.
    fallback = _last_source_sync() if summary["last_status"] is None else _no_fallback()
    return {
        **summary,
        "fallback_last_sync_at": fallback["last_sync_at"],
        # How many sources that time speaks for, out of how many exist — "2"
        # alone reads as the total when it is a subset.
        "fallback_synced_count": fallback["synced_count"],
        "fallback_source_count": fallback["source_count"],
        "fallback_unavailable": fallback["unavailable"],
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
    state = new_sweep_state()

    synced = 0
    failed = 0
    skipped_disabled = 0
    skipped_running = 0
    skipped_duplicate_project = 0
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

        # Single-flight against the OTHER writer of this source's rows (the
        # Keboola login-triggered sync). Held for the whole import, released
        # however it ends.
        with claim_source_for_import(source) as claimed:
            if not claimed:
                skipped_running += 1
                results.append(
                    {
                        "id": source_id,
                        "name": name,
                        "status": "skipped_running",
                        "hint": "Another writer of this source's rows is in flight; the next sweep picks it up.",
                    }
                )
                continue

            # One upstream, one importer per sweep — two sources resolving to
            # the same project would write it under two refs that then delete
            # each other's rows.
            duplicate = duplicate_upstream_reason(source, state)
            if duplicate:
                skipped_duplicate_project += 1
                results.append(
                    {"id": source_id, "name": name, "status": "skipped_duplicate_project", "error": duplicate}
                )
                logger.warning("semantic sources refresh: %s", duplicate)
                # Recorded on the row too: an admin looking at the source has
                # to be able to see why it never syncs.
                repo.record_sync(source_id, status="skipped", error=duplicate)
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
            # src/semantic/legacy_migration.py). Best-effort by contract, and
            # guarded here as well as inside: it runs AFTER the import already
            # wrote and recorded, so a raise must neither turn that success
            # into a failure nor abort the sources behind it.
            try:
                reconciled = reconcile_after_import(source, report)
            except Exception as exc:  # noqa: BLE001 - the sync stands, the cleanup retries next sweep
                logger.warning(
                    "semantic sources refresh: legacy reconciliation for source %s raised: %s", source_id, exc
                )
                reconciled = {}
            if reconciled:
                entry["reconciled_legacy"] = reconciled
            results.append(entry)

    return {
        "status": "ok",
        "synced": synced,
        "failed": failed,
        "skipped_disabled": skipped_disabled,
        "skipped_running": skipped_running,
        "skipped_duplicate_project": skipped_duplicate_project,
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
        "semantic sources refresh: run_id=%s synced=%s failed=%s skipped_disabled=%s "
        "skipped_running=%s skipped_duplicate_project=%s migrated=%s",
        run_id,
        result["synced"],
        result["failed"],
        result["skipped_disabled"],
        result["skipped_running"],
        result["skipped_duplicate_project"],
        len(result["migrated"]),
    )
    # Mirrors the response shape exactly — no `skipped_legacy_owned`, because
    # retiring the dedicated Keboola/Databricks refreshes removed the second
    # writer this sweep used to yield to, and with it that counter.
    log_safe(
        user_id=user.get("id"),
        action="run_semantic_sources_refresh",
        resource="job:semantic-sources-refresh",
        params={
            "run_id": run_id,
            "synced": result["synced"],
            "failed": result["failed"],
            "skipped_disabled": result["skipped_disabled"],
            "skipped_running": result["skipped_running"],
            "skipped_duplicate_project": result["skipped_duplicate_project"],
            "migrated": len(result["migrated"]),
        },
    )
    return {**result, "run_id": run_id, "started_at": started_at}
