"""Databricks semantic layer refresh — owner of the sync-trigger call path.

POST /api/admin/run-databricks-semantic-layer-refresh — called by the
scheduler container (auth: shared scheduler token resolves to a synthetic
admin user, same mechanism as app/api/keboola_semantic_layer_refresh.py) on
the SCHEDULER_DATABRICKS_SEMANTIC_LAYER_REFRESH_INTERVAL cadence. Also
callable by a real admin on demand.

Before the semantic-source adapter cutover (Track D6) this endpoint ran
``connectors.databricks.semantic_layer.sync_semantic_layer()``, a direct
``metric_definitions`` writer. It now ensures the Databricks connection is
registered as a `connection`-kind semantic source
(``connectors.databricks.semantic_layer.ensure_semantic_source``) and syncs
it through the same pipeline every other semantic source uses
(``src.semantic.transports.import_source``), then reconciles any
``metric_definitions`` rows the retired direct writer left behind
(``purge_legacy_metric_rows``) — see that module's docstring for the
provenance-cutover rationale. The scheduler cadence and endpoint path are
unchanged; only what runs behind them.

Single-flight guarded (mirrors the Keboola sibling): a second concurrent
call while a sync is in flight gets 409 already_running instead of racing a
second warehouse fetch + upsert/prune pass against the same
metric_definitions / semantic_models rows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin
from connectors.databricks.client import DatabricksApiError
from connectors.databricks.semantic_layer import ensure_semantic_source, purge_legacy_metric_rows
from src.semantic.transports import import_source

logger = logging.getLogger(__name__)
router = APIRouter()

_refresh_lock = asyncio.Lock()
# In-flight bookkeeping only — both fields are read by the 409 branch below.
_refresh_state: dict = {
    "run_id": None,
    "started_at": None,
}


def _run_sync() -> dict:
    """Runs off the event loop (``asyncio.to_thread``): ensure the semantic
    source exists, sync it, then reconcile any pre-cutover rows. All three
    are synchronous (repo calls + the adapter's own warehouse HTTP calls)."""
    source_id = ensure_semantic_source()
    report = import_source(source_id)
    purged_legacy = purge_legacy_metric_rows()
    result = asdict(report)
    result["purged_legacy"] = purged_legacy
    return result


@router.post("/api/admin/run-databricks-semantic-layer-refresh")
async def run_databricks_semantic_layer_refresh(
    user: dict = Depends(require_admin),
):
    """Sync the configured Databricks workspace's Unity Catalog metric views
    into the semantic layer. See connectors/databricks/semantic_ossie.py for
    the composition logic and connectors/databricks/semantic_layer.py for
    connection resolution + the legacy-provenance cutover.

    409 if a sync is already in flight. 400 when the sync fails for a reason
    the admin controls — Databricks not configured, or a request the
    workspace refuses (4xx). 502 when the upstream is unreachable, answers
    5xx, or the sync fails for any other reason.
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
            result = await asyncio.to_thread(_run_sync)
        except DatabricksApiError as exc:
            # Checked BEFORE RuntimeError: DatabricksApiError subclasses it,
            # so the broader clause would otherwise shadow this one and every
            # upstream 5xx would misreport as an admin-fixable 400.
            status = 400 if (exc.status is not None and 400 <= exc.status < 500) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except RuntimeError as exc:
            # DatabricksSemanticAdapter.extract() raises a plain RuntimeError
            # for everything the admin controls — not configured, no catalog.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
            raise HTTPException(status_code=502, detail=f"sync failed: {exc}") from exc
        finally:
            _refresh_state["run_id"] = None
            _refresh_state["started_at"] = None

    logger.info(
        "databricks semantic layer refresh: run_id=%s models_written=%s models_pruned=%s invalid=%s purged_legacy=%s",
        run_id,
        result.get("models_written"),
        len(result.get("models_pruned") or []),
        len(result.get("invalid") or []),
        result.get("purged_legacy"),
    )
    return {**result, "status": "ok", "run_id": run_id, "started_at": started_at}
