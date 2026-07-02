"""Hybrid query endpoint — two-phase BQ registration + DuckDB execution."""

import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from connectors.bigquery.labels import job_labels_for
from src.audit_helpers import client_kind_from_user
from src.db import get_analytics_db_readonly
from src.remote_query import RemoteQueryEngine, RemoteQueryError, load_config
import duckdb

from src.repositories import (
    audit_repo,
)
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/query", tags=["query"])


class HybridQueryRequest(BaseModel):
    sql: str
    register_bq: Dict[str, str] = {}


@router.post("/hybrid")
async def hybrid_query(
    request: HybridQueryRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    t0 = time.monotonic()
    bq_table = next(iter(request.register_bq), None) if request.register_bq else None
    resource = (
        f"hybrid:{bq_table}" if bq_table else "hybrid:multi" if request.register_bq else "hybrid:local"
    )[:256]
    config = load_config()
    analytics = get_analytics_db_readonly()
    try:
        engine = RemoteQueryEngine(
            analytics,
            max_bq_registration_rows=config.get("max_bq_registration_rows", 500_000),
            max_memory_mb=config.get("max_memory_mb", 2048),
            max_result_rows=config.get("max_result_rows", 100_000),
            timeout_seconds=config.get("timeout_seconds", 300),
        )
        for alias, bq_sql in request.register_bq.items():
            try:
                engine.register_bq(alias, bq_sql, job_labels=job_labels_for(user, "hybrid"))
            except RemoteQueryError as e:
                try:
                    audit_repo().log(
                        user_id=user.get("id"),
                        action="query.hybrid",
                        resource=resource,
                        params={"sql_preview": (request.sql or "")[:200],
                                "bq_subqueries_count": len(request.register_bq),
                                "error": f"BQ '{alias}': {e.error_type}: {e}"[:200],
                                "duration_ms": int((time.monotonic() - t0) * 1000)},
                        result="error.400",
                        client_kind=client_kind_from_user(user),
                    )
                except Exception:
                    logger.exception("audit_log write failed for query.hybrid (bq error); continuing")
                raise HTTPException(status_code=400, detail=f"BQ '{alias}': {e.error_type}: {e}")
        try:
            result = engine.execute(request.sql)
        except RemoteQueryError as e:
            try:
                audit_repo().log(
                    user_id=user.get("id"),
                    action="query.hybrid",
                    resource=resource,
                    params={"sql_preview": (request.sql or "")[:200],
                            "bq_subqueries_count": len(request.register_bq),
                            "error": f"Query: {e.error_type}: {e}"[:200],
                            "duration_ms": int((time.monotonic() - t0) * 1000)},
                    result="error.400",
                    client_kind=client_kind_from_user(user),
                )
            except Exception:
                logger.exception("audit_log write failed for query.hybrid (exec error); continuing")
            raise HTTPException(status_code=400, detail=f"Query: {e.error_type}: {e}")
        # bytes_scanned is not directly surfaced by RemoteQueryEngine; deferred TODO.
        rows_returned = len(result.get("rows", [])) if isinstance(result, dict) else None
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="query.hybrid",
                resource=resource,
                params={
                    "sql_preview": (request.sql or "")[:200],
                    "bq_subqueries_count": len(request.register_bq),
                    "bytes_scanned": None,  # deferred — RemoteQueryEngine doesn't expose BQ job metadata
                    "rows_returned": rows_returned,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for query.hybrid; continuing")
        return result
    finally:
        analytics.close()
