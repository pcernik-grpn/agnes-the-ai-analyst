"""BigQuery metadata cache refresh — owner of the ``bq_metadata_cache``
write path.

Three endpoints share this module:

  - ``POST /api/admin/run-bq-metadata-refresh`` — called by the scheduler
    container (auth: shared scheduler token resolves to a synthetic admin
    user). Walks remote rows in ``table_registry``, fetches each via the
    BigQuery metadata provider, UPSERTs into ``bq_metadata_cache``.

  - ``POST /api/v2/metadata-cache/refresh?table=<id>`` — admin-gated, for
    operator on-demand refresh of a single row (e.g. after editing the
    registry entry's ``bucket`` / ``source_table``).

  - ``GET /api/v2/metadata-cache/status`` — auth required, NOT admin-only.
    Returns per-row freshness so analyst tooling (CLI / Claude Code) can
    decide whether to trust the cached numbers or wait for a refresh.

Why this lives outside the catalog endpoint
-------------------------------------------
Earlier releases inlined a per-row BigQuery fetch into ``GET /api/v2/catalog``.
On cold caches that became O(N) sequential BQ jobs API roundtrips inside
one HTTP request — easily 90 s+ on partitioned tables — and reliably blew
the CLI's 30 s ``httpx.ReadTimeout``. Moving the fetch off the hot path
into a scheduled refresh job (default every 4 h, configurable via
``SCHEDULER_BQ_METADATA_REFRESH_INTERVAL``) keeps the catalog response
under tens of milliseconds even at first boot, at the cost of metadata
being up to one refresh-interval stale. The freshness field surfaces
that explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user

from src.audit_helpers import log_safe
from src.repositories import (
    bq_metadata_cache_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Freshness thresholds ──────────────────────────────────────────────────


def _scheduler_interval_seconds() -> int:
    """Return the scheduler's configured refresh interval, mirroring
    ``services/scheduler/__main__.py``. We re-read the env var instead
    of importing the scheduler module because the scheduler runs in a
    sibling container and is not on the app's import path.
    """
    raw = os.environ.get("SCHEDULER_BQ_METADATA_REFRESH_INTERVAL")
    if raw is None or raw == "":
        return 4 * 60 * 60  # 4 h default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 4 * 60 * 60
    return value if value > 0 else 4 * 60 * 60


def _fresh_threshold_seconds() -> int:
    """A row is ``fresh`` when refreshed within this window.

    Two refresh intervals: one refresh might fail (network blip, BQ
    throttle); the analyst should keep seeing the last-known-good row
    as ``fresh`` until two consecutive refreshes have passed without
    success. Beyond that, the response surfaces ``stale`` so the
    consumer knows the numbers might be outdated.
    """
    return 2 * _scheduler_interval_seconds()


def compute_freshness(
    cache_row: Optional[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    fresh_threshold: Optional[int] = None,
) -> str:
    """Classify a cache row's freshness.

    - ``never_fetched``: no row, or no successful refresh yet.
    - ``fresh``: refreshed within the threshold.
    - ``stale``: refreshed earlier than the threshold.
    - ``error``: most recent attempt failed and there is no prior success
      (success row is preserved across errors — analyst keeps using
      last-known-good numbers).
    """
    if cache_row is None:
        return "never_fetched"
    refreshed_at = cache_row.get("refreshed_at")
    error_at = cache_row.get("error_at")
    if refreshed_at is None:
        return "error" if error_at is not None else "never_fetched"
    threshold = fresh_threshold if fresh_threshold is not None else _fresh_threshold_seconds()
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=threshold)
    # DuckDB returns naive datetimes for TIMESTAMP columns; treat as UTC.
    if refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=timezone.utc)
    return "fresh" if refreshed_at >= cutoff else "stale"


# ─── Single-row refresh primitive ──────────────────────────────────────────


def refresh_one(row: dict[str, Any]) -> dict[str, Any]:
    """Fetch BQ metadata for one row and UPSERT the result.

    Synchronous; safe to call from an anyio thread. Returns a small
    outcome dict for the caller (counts, audit).

    Failures are absorbed: the cache row's prior success is preserved
    (``error_at`` + ``error_msg`` set, ``refreshed_at`` left alone).
    """
    from app.api._metadata_models import MetadataRequest
    from connectors.bigquery import metadata as bq_metadata
    from src.identifier_validation import validate_quoted_identifier

    # ``total_ms`` covers the whole per-row cost (identifier validation +
    # the BQ fetch + the local DuckDB upsert/mark_error); ``fetch_ms``
    # isolates the ``bq_metadata.fetch`` call — the up-to-4 sequential BQ
    # jobs-API round-trips the ticket suspects. Both are surfaced per table
    # so a slow refresh cycle is attributable to BigQuery vs. local work.
    t0 = time.monotonic()
    fetch_ms: Optional[int] = None

    table_id = row["id"]
    bucket = row.get("bucket") or ""
    source_table = row.get("source_table") or table_id
    repo = bq_metadata_cache_repo()

    if not (validate_quoted_identifier(bucket, "bucket") and validate_quoted_identifier(source_table, "source_table")):
        repo.mark_error(table_id, "invalid bucket/source_table identifier")
        return {
            "table_id": table_id,
            "status": "error",
            "error": "invalid identifier",
            "fetch_ms": fetch_ms,
            "total_ms": int((time.monotonic() - t0) * 1000),
        }

    req = MetadataRequest(
        table_id=table_id,
        bucket=bucket,
        source_table=source_table,
    )
    fetch_t0 = time.monotonic()
    try:
        result = bq_metadata.fetch(req)
    except Exception as e:
        # bq_metadata.fetch is documented as never-raises, but defense in
        # depth: catch any regression so one bad row doesn't kill the
        # whole scheduler tick.
        fetch_ms = int((time.monotonic() - fetch_t0) * 1000)
        msg = f"{type(e).__name__}: {e}"
        logger.warning("bq metadata refresh failed for %s: %s", table_id, msg)
        repo.mark_error(table_id, msg)
        return {
            "table_id": table_id,
            "status": "error",
            "error": msg,
            "fetch_ms": fetch_ms,
            "total_ms": int((time.monotonic() - t0) * 1000),
        }
    fetch_ms = int((time.monotonic() - fetch_t0) * 1000)

    if result is None:
        repo.mark_error(table_id, "provider returned no data")
        return {
            "table_id": table_id,
            "status": "no_data",
            "fetch_ms": fetch_ms,
            "total_ms": int((time.monotonic() - t0) * 1000),
        }

    repo.upsert_success(
        table_id,
        rows=result.rows,
        size_bytes=result.size_bytes,
        partition_by=result.partition_by,
        clustered_by=result.clustered_by,
        entity_type=result.entity_type,
        known_columns=result.known_columns,
    )
    return {
        "table_id": table_id,
        "status": "ok",
        "rows": result.rows,
        "size_bytes": result.size_bytes,
        "entity_type": result.entity_type,
        "fetch_ms": fetch_ms,
        "total_ms": int((time.monotonic() - t0) * 1000),
    }


def _list_remote_bq_rows() -> list[dict[str, Any]]:
    rows = table_registry_repo().list_all()
    return [r for r in rows if r.get("query_mode") == "remote" and r.get("source_type") == "bigquery"]


def _refresh_concurrency() -> int:
    raw = os.environ.get("AGNES_BQ_METADATA_REFRESH_CONCURRENCY", "4")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 4
    return value if value > 0 else 4


# ─── Single-flight state ──────────────────────────────────────────────────
#
# Module-level guard so a second concurrent
# ``POST /api/admin/run-bq-metadata-refresh`` doesn't fan out duplicate
# BQ work. Pre-0.52 the second call would happily run its own loop and
# do 2× BQ jobs-API traffic against the same set of tables for the same
# eventual UPSERT result — confirmed by stress test C on 2026-05-12.
# DuckDB MVCC kept the rows consistent, but BQ quota leaked.
#
# Semantics: while a refresh is running, additional callers get
# ``409 already_running`` with the in-flight ``run_id`` + ``started_at``
# so they can correlate against logs. Scheduler treats 409 as a no-op
# success (next tick will fire again — usually 4 h later — and find
# the lock free).
_refresh_lock = asyncio.Lock()
_refresh_state: dict[str, Any] = {"run_id": None, "started_at": None}


# ─── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/api/admin/run-bq-metadata-refresh")
async def run_bq_metadata_refresh(
    user: dict = Depends(require_admin),
):
    """Refresh metadata for every remote BQ row in the registry.

    Called by the scheduler at ``SCHEDULER_BQ_METADATA_REFRESH_INTERVAL``
    (default 4 h). Single-flight guarded: if a refresh is already
    running (e.g. operator clicked "Re-warm all" while a scheduler tick
    is in flight, or two scheduler containers raced during an upgrade),
    the second caller gets ``409 already_running`` with the in-flight
    ``run_id`` + ``started_at`` so they can correlate against logs.
    The scheduler treats 409 as a no-op success.

    Bounded concurrency within a run (default 4, override via
    ``AGNES_BQ_METADATA_REFRESH_CONCURRENCY``) so a deployment with
    many remote tables doesn't fan out to dozens of parallel BQ jobs.
    """
    import uuid

    if _refresh_lock.locked():
        # Issue #256: emit 409 instead of doing 2× BQ work.
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
            rows = _list_remote_bq_rows()
            sem = asyncio.Semaphore(_refresh_concurrency())

            async def _one(row: dict[str, Any]) -> dict[str, Any]:
                async with sem:
                    result = await asyncio.to_thread(refresh_one, row)
                # Per-table timing: one INFO line per table so a
                # slow refresh cycle is attributable to specific tables/calls
                # from the logs alone. ``%s`` (not ``%d``) — ``fetch_ms`` is
                # ``None`` when the row failed identifier validation before
                # any BQ call.
                logger.info(
                    "bq metadata refresh table: run_id=%s table_id=%s status=%s fetch_ms=%s total_ms=%s",
                    run_id,
                    result.get("table_id"),
                    result.get("status"),
                    result.get("fetch_ms"),
                    result.get("total_ms"),
                )
                return result

            t0 = time.monotonic()
            results = await asyncio.gather(
                *(_one(r) for r in rows),
                return_exceptions=True,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    succeeded = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "ok")
    no_data = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "no_data")
    failed = sum(1 for r in results if isinstance(r, Exception) or (isinstance(r, dict) and r.get("status") == "error"))

    logger.info(
        "bq metadata refresh: run_id=%s total=%d ok=%d no_data=%d failed=%d duration_ms=%d",
        run_id,
        len(rows),
        succeeded,
        no_data,
        failed,
        duration_ms,
    )
    log_safe(
        user_id=user.get("id"),
        action="run_bq_metadata_refresh",
        resource="job:bq-metadata-refresh",
        params={
            "run_id": run_id,
            "total": len(rows),
            "succeeded": succeeded,
            "no_data": no_data,
            "failed": failed,
            "duration_ms": duration_ms,
        },
    )
    return {
        "run_id": run_id,
        "started_at": started_at,
        "total": len(rows),
        "succeeded": succeeded,
        "no_data": no_data,
        "failed": failed,
        "duration_ms": duration_ms,
    }


@router.post("/api/v2/metadata-cache/refresh")
async def refresh_one_table(
    table: str = Query(..., description="Registry table_id to refresh"),
    user: dict = Depends(require_admin),
):
    """Operator on-demand refresh of one row.

    Useful right after editing the registry row (so the catalog reflects
    new ``bucket`` / ``source_table`` immediately) or after an upstream
    BQ schema change that the operator wants reflected before the next
    scheduled tick.
    """
    row = table_registry_repo().get(table)
    if not row:
        raise HTTPException(status_code=404, detail=f"Unknown table_id: {table}")
    if row.get("query_mode") != "remote" or row.get("source_type") != "bigquery":
        raise HTTPException(
            status_code=400,
            detail="Manual metadata refresh is only meaningful for remote BigQuery tables",
        )
    return await asyncio.to_thread(refresh_one, row)


@router.get("/api/v2/metadata-cache/status")
def metadata_cache_status(
    user: dict = Depends(get_current_user),
):
    """Per-table cache status. Non-admin — analyst tools rely on this to
    decide whether to trust the catalog's ``rows`` / ``size_bytes`` or
    treat the table as opaque until the next refresh.
    """
    cache_rows = bq_metadata_cache_repo().list_all()
    threshold = _fresh_threshold_seconds()
    now = datetime.now(timezone.utc)
    interval = _scheduler_interval_seconds()
    tables = []
    for r in cache_rows:
        refreshed_at = r.get("refreshed_at")
        error_at = r.get("error_at")
        tables.append(
            {
                "table_id": r["table_id"],
                "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
                "rows": r.get("rows"),
                "size_bytes": r.get("size_bytes"),
                "partition_by": r.get("partition_by"),
                "clustered_by": r.get("clustered_by") or [],
                "entity_type": r.get("entity_type"),
                "known_columns": r.get("known_columns") or [],
                "error_at": error_at.isoformat() if error_at else None,
                "error_msg": r.get("error_msg"),
                "freshness": compute_freshness(r, now=now, fresh_threshold=threshold),
            }
        )
    return {
        "scheduler_interval_seconds": interval,
        "fresh_threshold_seconds": threshold,
        "server_time": now.isoformat(),
        "tables": tables,
    }
