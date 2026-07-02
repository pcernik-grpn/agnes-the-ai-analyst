"""POST /api/v2/scan and POST /api/v2/scan/estimate (spec §3.4 + §3.5)."""

from __future__ import annotations
import logging
import re
import time
from typing import Optional

import pyarrow as pa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.db import _open_duckdb
from app.instance_config import get_value
from src.audit_helpers import client_kind_from_user
from src.rbac import can_access_table
from app.api.where_validator import (
    safe_where_predicate,
    WhereValidationError,
)

from src.repositories import (
    audit_repo,
    table_registry_repo,
)
from app.api.v2_schema import build_schema  # reused for column resolution
from app.api.v2_arrow import CONTENT_TYPE, arrow_to_ipc_bytes_capped
from app.api.v2_quota import QuotaTracker, QuotaExceededError
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access
from connectors.bigquery.labels import job_labels_for

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])


class ScanRequest(BaseModel):
    table_id: str
    select: Optional[list[str]] = None
    where: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1)
    order_by: Optional[list[str]] = None


def _resolve_schema(conn, user, table_id: str, bq: BqAccess) -> dict:
    """Get {column: type} dict for the target table — used by validator + projection check."""
    s = build_schema(conn, user, table_id, bq=bq)
    return {c["name"]: c["type"] for c in s.get("columns", [])}


def _bq_dry_run_bytes(
    bq: BqAccess, sql: str, *, user: dict | None = None, agent_name: str = "scan"
) -> int:
    """Run a BQ dry-run via the google-cloud-bigquery client and return totalBytesProcessed.

    SQL here is user-derived (built from req.select/where/order_by), so BadRequest → 400
    (`bad_request_status="client_error"`).
    """
    from google.cloud import bigquery
    from connectors.bigquery.access import translate_bq_error

    client = bq.client()  # raises BqAccessError(bq_lib_missing/auth_failed) — propagates
    try:
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                dry_run=True,
                use_query_cache=False,
                labels=job_labels_for(user, agent_name),
            ),
        )
        return int(job.total_bytes_processed or 0)
    except Exception as e:
        raise translate_bq_error(e, bq.projects, bad_request_status="client_error")


_COLUMN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_ORDER_BY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$", re.IGNORECASE)


def _validate_select_columns(select: list[str] | None, schema: dict) -> None:
    """Reject SELECT column names that don't fit the safe-identifier shape.

    Schema-existence is checked separately; this guard is defense-in-depth
    so a backtick / double-quote in a column name can't break out of the
    `…` (BQ) or "…" (DuckDB) identifier wrapper in `_build_bq_sql` and the
    local-scan path. Today, schema names from BQ INFORMATION_SCHEMA never
    contain those characters — but Devin called this out as relying on an
    implicit upstream constraint. Make it explicit."""
    if not select:
        return
    for entry in select:
        if not _COLUMN_NAME_RE.match(entry or ""):
            raise ValueError(f"invalid column name: {entry!r}")


def _validate_order_by(order_by: list[str] | None, schema: dict) -> None:
    """Reject anything other than `<column>` or `<column> ASC|DESC` against the schema.
    Without this, `order_by` is concatenated raw into the FROM clause SQL — exploitable."""
    if not order_by:
        return
    known = {c.lower() for c in schema}
    for entry in order_by:
        s = (entry or "").strip()
        if not _ORDER_BY_RE.match(s):
            raise ValueError(f"invalid order_by entry: {entry!r}")
        col = s.split()[0].lower()
        if col not in known:
            raise ValueError(f"unknown order_by column: {entry!r}")


def _quote_order_by_bq(entry: str) -> str:
    """Backtick-quote the column part of an order_by entry, preserve direction."""
    parts = entry.strip().split()
    return f"`{parts[0]}`" + ("" if len(parts) == 1 else " " + " ".join(parts[1:]))


def _quote_order_by_duckdb(entry: str) -> str:
    parts = entry.strip().split()
    return f'"{parts[0]}"' + ("" if len(parts) == 1 else " " + " ".join(parts[1:]))


def _build_bq_sql(
    table_row: dict,
    project_id: str,
    req: ScanRequest,
    *,
    safe_where: str | None = None,
) -> str:
    """Build the BQ SQL string. ``safe_where`` MUST be the comment-stripped
    fragment from ``safe_where_predicate`` — splicing ``req.where`` raw lets a
    `1=1 --` predicate comment out everything that follows (LIMIT/ORDER BY).

    Identifier quoting: column names are validated against the schema before
    we get here, but reserved words (`order`, `group`, `timestamp`, …) still
    need backticks to parse as identifiers in BQ.
    """
    from src.identifier_validation import validate_quoted_identifier

    bucket = table_row.get("bucket") or ""
    src_table = table_row.get("source_table") or req.table_id
    if not (
        validate_quoted_identifier(project_id, "BQ project")
        and validate_quoted_identifier(bucket, "BQ dataset")
        and validate_quoted_identifier(src_table, "BQ source_table")
    ):
        raise ValueError("unsafe BQ identifier in registry — refusing to build SQL")

    select_sql = ", ".join(f"`{c}`" for c in req.select) if req.select else "*"
    table_ref = f"`{project_id}.{bucket}.{src_table}`"
    sql = f"SELECT {select_sql} FROM {table_ref}"
    if safe_where:
        sql += f" WHERE {safe_where}"
    if req.order_by:
        sql += f" ORDER BY {', '.join(_quote_order_by_bq(e) for e in req.order_by)}"
    if req.limit:
        sql += f" LIMIT {int(req.limit)}"
    return sql


def estimate(conn, user, raw_request: dict, *, bq: BqAccess) -> dict:
    req = ScanRequest(**raw_request)
    repo = table_registry_repo()
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    schema = _resolve_schema(conn, user, req.table_id, bq)
    dialect = "bigquery" if (row.get("source_type") or "") == "bigquery" else "duckdb"

    # Validate WHERE and capture the comment-stripped fragment for splicing.
    safe_where = safe_where_predicate(req.where, req.table_id, schema, dialect=dialect) if req.where else None
    # Validate select columns exist (case-insensitive, matching order_by).
    if req.select:
        _validate_select_columns(req.select, schema)
        known = {c.lower() for c in schema}
        unknown = [c for c in req.select if c.lower() not in known]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
    _validate_order_by(req.order_by, schema)

    if (row.get("source_type") or "") != "bigquery":
        return {
            "table_id": req.table_id,
            "estimated_scan_bytes": 0,
            "estimated_result_rows": None,
            "estimated_result_bytes": None,
            "bq_cost_estimate_usd": 0.0,
        }

    bq_sql = _build_bq_sql(row, bq.projects.data, req, safe_where=safe_where)
    scan_bytes = _bq_dry_run_bytes(bq, bq_sql, user=user)

    cost_per_tb = float(get_value("api", "scan", "bq_cost_per_tb_usd", default=5.0) or 5.0)
    cost = (scan_bytes / 1_099_511_627_776) * cost_per_tb  # 1 TiB = 2^40

    # Heuristic for result row/byte estimate. A row contains all selected
    # columns, so per-row bytes = sum of per-column estimates (NOT average).
    # If req.select is set, narrow to those columns; otherwise use full schema.
    # Case-insensitive lookup matches the SELECT-validation policy — analysts
    # often write a lowercased column name where INFORMATION_SCHEMA returned
    # mixed-case; the schema lookup must follow.
    schema_lower = {k.lower(): v for k, v in schema.items()}
    cols_for_estimate = [schema_lower[c.lower()] for c in (req.select or []) if c.lower() in schema_lower] or list(
        schema.values()
    )
    avg_row_bytes = max(1, sum(_avg_bytes_for_type(t) for t in cols_for_estimate))
    rows_est = scan_bytes // max(avg_row_bytes, 1)
    if req.limit:
        rows_est = min(rows_est, req.limit)

    return {
        "table_id": req.table_id,
        "estimated_scan_bytes": int(scan_bytes),
        "estimated_result_rows": int(rows_est),
        "estimated_result_bytes": int(rows_est * avg_row_bytes),
        "bq_cost_estimate_usd": round(cost, 4),
    }


def _avg_bytes_for_type(t: str) -> int:
    t = (t or "").upper()
    if t in ("INT64", "FLOAT64", "DATE", "TIMESTAMP", "DATETIME", "TIME"):
        return 8
    if t == "STRING":
        return 32  # rough average
    if t == "BYTES":
        return 64
    if t == "BOOL":
        return 1
    return 16


@router.post("/scan/estimate")
def scan_estimate_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` so FastAPI auto-offloads to the anyio thread pool — the
    # estimate path calls into google-cloud-bigquery's `client.query(...,
    # dry_run=True)` which blocks until BQ returns the dry-run cost. Under
    # ``async def`` that wait holds the event loop. See PR #188's Tier 1
    # entry for the wider rollout.
    t0 = time.monotonic()
    table_id = raw.get("table_id", "") if isinstance(raw, dict) else ""
    resource = f"table:{table_id}"[:256]
    try:
        result = estimate(conn, user, raw, bq=bq)
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="snapshot.estimate",
                resource=resource,
                params={
                    "bytes_estimated": result.get("estimated_scan_bytes"),
                    "where_present": bool(raw.get("where") if isinstance(raw, dict) else False),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for snapshot.estimate; continuing")
        return result
    except (WhereValidationError, PermissionError, FileNotFoundError, ValueError, BqAccessError) as exc:
        try:
            if isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, FileNotFoundError):
                status_code = 404
            elif isinstance(exc, (WhereValidationError, ValueError)):
                status_code = 400
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=user.get("id"),
                action="snapshot.estimate",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for snapshot.estimate; continuing")
        if isinstance(exc, WhereValidationError):
            raise HTTPException(
                status_code=400,
                detail={"error": "validator_rejected", "kind": exc.kind, "details": exc.detail or {}},
            )
        if isinstance(exc, PermissionError):
            from src.rbac import table_not_in_stack_message

            raise HTTPException(
                status_code=403,
                detail=table_not_in_stack_message(str(exc) or "<unknown>"),
            )
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail=f"table {exc!s} not found")
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc))
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(exc.kind, 500),  # type: ignore[union-attr]
            detail={"error": exc.kind, "message": exc.message, "details": exc.details},  # type: ignore[union-attr]
        )


# `_build_quota_tracker` lives in `app.api.v2_quota` so /api/query (issue #160)
# can share the same singleton without inverting the dep direction
# (api/query → api/v2/scan would couple a high-level endpoint to a sibling).
# Re-exported here so existing test sites that call
# `v2_scan._build_quota_tracker()` (7 in tests/test_v2_scan.py) keep working.
# Do NOT re-export `_quota_singleton` — `from X import var` copies the
# binding at import time, so a re-exported singleton would never see the
# initialized value (#160 review caveat).
from app.api.v2_quota import _build_quota_tracker  # noqa: E402  # re-export


def _max_result_bytes() -> int:
    return int(get_value("api", "scan", "max_result_bytes", default=2_147_483_648) or 2_147_483_648)


def _max_limit() -> int:
    return int(get_value("api", "scan", "max_limit", default=10_000_000) or 10_000_000)


def _run_bq_scan(bq: BqAccess, sql: str) -> pa.Table:
    """Run a BQ query via DuckDB BQ extension. Returns Arrow table.

    SQL here is user-derived → BadRequest → 400 (`bad_request_status="client_error"`).
    """
    from connectors.bigquery.access import translate_bq_error

    with bq.duckdb_session() as conn:
        try:
            return conn.execute(
                "SELECT * FROM bigquery_query(?, ?)",
                [bq.projects.billing, sql],
            ).arrow()
        except Exception as e:
            raise translate_bq_error(e, bq.projects, bad_request_status="client_error")


def run_scan(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    raw_request: dict,
    *,
    bq: BqAccess,
    quota: QuotaTracker,
) -> bytes:
    """Validate → quota → execute → serialize. Returns Arrow IPC bytes.

    Raises:
        WhereValidationError, QuotaExceededError, FileNotFoundError, PermissionError,
        ValueError, BqAccessError
    """
    # `from_query` mode (#616): materialize a raw SELECT, reusing /api/query's
    # RBAC + registry-gating but bypassing the remote_scan_too_large cap. The
    # raw query carries its own projection, so select/where/order_by are
    # rejected as mutually exclusive.
    if isinstance(raw_request, dict) and raw_request.get("from_query"):
        if any(raw_request.get(k) for k in ("select", "where", "order_by", "limit")):
            raise ValueError("from_query is mutually exclusive with select/where/order_by/limit")
        from app.api.query import run_remote_select_to_arrow

        table = run_remote_select_to_arrow(
            conn,
            user,
            raw_request["from_query"],
            bq=bq,
            quota=quota,
        )
        return arrow_to_ipc_bytes_capped(table, _max_result_bytes())

    req = ScanRequest(**raw_request)
    repo = table_registry_repo()
    row = repo.get(req.table_id)
    if not row:
        raise FileNotFoundError(req.table_id)
    if not can_access_table(user, req.table_id, conn):
        raise PermissionError(req.table_id)

    if req.limit and req.limit > _max_limit():
        raise ValueError(f"limit {req.limit} exceeds max {_max_limit()}")

    schema = _resolve_schema(conn, user, req.table_id, bq)
    dialect = "bigquery" if (row.get("source_type") or "") == "bigquery" else "duckdb"
    # Validate WHERE and capture the comment-stripped fragment for splicing.
    safe_where = safe_where_predicate(req.where, req.table_id, schema, dialect=dialect) if req.where else None
    if req.select:
        # Case-insensitive (BQ identifiers are case-insensitive; mixed-case
        # names from INFORMATION_SCHEMA.COLUMNS shouldn't 400-reject the
        # lowercased form a typical analyst writes).
        _validate_select_columns(req.select, schema)
        known = {c.lower() for c in schema}
        unknown = [c for c in req.select if c.lower() not in known]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
    _validate_order_by(req.order_by, schema)

    source_type = row.get("source_type") or ""
    user_id = user.get("email") or "anon"

    # Pre-flight quota check — fail BEFORE running the BQ scan so the user
    # doesn't pay for a query whose result we'd then refuse to return.
    quota.check_daily_budget(user=user_id)

    with quota.acquire(user=user_id):
        if source_type != "bigquery":
            # Local source: query parquet directly. Resolve by source-name-agnostic
            # lookup — the extract directory is not necessarily the source_type
            # (e.g. the bundled `demo` extract registers tables as 'local' but
            # lives under extracts/demo/), and `source_type` may be NULL/empty for
            # legacy rows. resolve_local_parquet handles both.
            from app.utils import resolve_local_parquet

            parquet = resolve_local_parquet(req.table_id, source_type)
            if parquet is None:
                raise FileNotFoundError(req.table_id)
            local = _open_duckdb(":memory:")
            try:
                projection = ", ".join(f'"{c}"' for c in req.select) if req.select else "*"
                sql = f"SELECT {projection} FROM read_parquet(?)"
                if safe_where:
                    sql += f" WHERE {safe_where}"
                if req.order_by:
                    sql += f" ORDER BY {', '.join(_quote_order_by_duckdb(e) for e in req.order_by)}"
                if req.limit:
                    sql += f" LIMIT {int(req.limit)}"
                table = local.execute(sql, [str(parquet)]).arrow()
            finally:
                local.close()
        else:
            bq_sql = _build_bq_sql(row, bq.projects.data, req, safe_where=safe_where)
            table = _run_bq_scan(bq, bq_sql)

        # Enforce max_result_bytes guard (spec §3.4 step 8). Streams with the
        # cap applied, so a RecordBatchReader (duckdb>=1.5 `.arrow()`) is
        # never fully materialized on an over-cap result.
        ipc = arrow_to_ipc_bytes_capped(table, _max_result_bytes())

        # Record bytes for daily quota
        quota.record_bytes(user=user_id, n=len(ipc))
        return ipc


@router.post("/scan")
def scan_endpoint(
    raw: dict,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    quota = _build_quota_tracker()
    t0 = time.monotonic()
    table_id = raw.get("table_id", "") if isinstance(raw, dict) else ""
    snapshot_name = raw.get("as") if isinstance(raw, dict) else None
    resource = (f"table:{table_id}:as:{snapshot_name}" if snapshot_name else f"table:{table_id}")[:256]
    try:
        ipc = run_scan(conn, user, raw, bq=bq, quota=quota)
        # Decode row count from IPC without re-running the scan.
        # bytes_scanned / bytes_billed / bq_job_id are deferred — the BQ
        # extension doesn't expose per-job metadata through the DuckDB
        # execute() path; adding that plumbing is a multi-site change.
        # TODO: surface bytes_scanned / bytes_billed / bq_job_id once the
        # BQ extension or _run_bq_scan is extended to return job metadata.
        try:
            from app.api.v2_arrow import parse_ipc_bytes

            rows_written = parse_ipc_bytes(ipc).num_rows
        except Exception:
            rows_written = None
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="snapshot.create",
                resource=resource,
                params={
                    "rows_written": rows_written,
                    "bytes_scanned": None,  # deferred — see TODO above
                    "bytes_billed": None,  # deferred
                    "bq_job_id": None,  # deferred
                    "snapshot_name": snapshot_name,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for snapshot.create; continuing")
        return Response(content=ipc, media_type=CONTENT_TYPE)
    except HTTPException as exc:
        # `run_remote_select_to_arrow` (from_query mode, #616) raises
        # HTTPException directly for RBAC / SELECT-only / registry
        # rejections and for DuckDB execution errors (Devin Review
        # ANALYSIS_0003 on #620). Without this branch those bypass the
        # structured error block below — the audit-log error path never
        # fires and the response shape diverges from the rest of
        # `scan_endpoint`. Log the error-result audit row, then re-raise
        # the HTTPException unchanged so the client still sees the
        # original status + detail. Devin Review ANALYSIS_0001 on #620.
        try:
            audit_repo().log(
                user_id=user.get("id"),
                action="snapshot.create",
                resource=resource,
                params={
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "error": str(exc.detail)[:200],
                },
                result=f"error.{exc.status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on http_exc path for snapshot.create; continuing")
        raise
    except (
        WhereValidationError,
        QuotaExceededError,
        FileNotFoundError,
        PermissionError,
        ValueError,
        BqAccessError,
    ) as exc:
        try:
            if isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, FileNotFoundError):
                status_code = 404
            elif isinstance(exc, QuotaExceededError):
                status_code = 429
            elif isinstance(exc, (WhereValidationError, ValueError)):
                status_code = 400
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=user.get("id"),
                action="snapshot.create",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for snapshot.create; continuing")
        if isinstance(exc, WhereValidationError):
            raise HTTPException(
                status_code=400,
                detail={"error": "validator_rejected", "kind": exc.kind, "details": exc.detail or {}},
            )
        if isinstance(exc, QuotaExceededError):
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "quota_exceeded",
                    "kind": exc.kind,
                    "current": exc.current,
                    "limit": exc.limit,
                    "retry_after_seconds": exc.retry_after_seconds,
                },
            )
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail="table not found")
        if isinstance(exc, PermissionError):
            from src.rbac import table_not_in_stack_message

            raise HTTPException(
                status_code=403,
                detail=table_not_in_stack_message(str(exc) or "<unknown>"),
            )
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc))
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(exc.kind, 500),  # type: ignore[union-attr]
            detail={"error": exc.kind, "message": exc.message, "details": exc.details},  # type: ignore[union-attr]
        )
