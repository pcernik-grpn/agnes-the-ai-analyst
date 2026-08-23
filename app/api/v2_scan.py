"""POST /api/v2/scan and POST /api/v2/scan/estimate (spec §3.4 + §3.5)."""

from __future__ import annotations
import json
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
from src.audit_helpers import identity_for_audit, client_kind_from_user
from src.rbac import can_access_table
from src.access_policy import (
    PolicyError,
    PolicyIdentityUnresolvable,
    assert_unique_output_columns,
    policied_from_sql,
    policied_relation,
    policy_fingerprint,
    row_scope_payload,
)
from app.api.where_validator import (
    safe_where_predicate,
    WhereValidationError,
)

from src.repositories import (
    audit_repo,
    table_registry_repo,
)
from app.api.v2_schema import NotFound, build_schema  # reused for column resolution
from app.api.v2_arrow import CONTENT_TYPE, arrow_to_ipc_bytes_capped
from app.api.v2_quota import (
    KIND_DAILY_BYTES,
    KIND_DAILY_ESTIMATES,
    QuotaTracker,
    QuotaExceededError,
)
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access
from connectors.bigquery.labels import job_labels_for
from src.sql_ident import quote_ident

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
    try:
        s = build_schema(conn, user, table_id, bq=bq)
    except NotFound as e:
        # build_schema raises its own NotFound (a plain Exception) for a
        # missing registry row OR a materialized/local row whose parquet
        # hasn't been written yet. The scan/estimate endpoints map
        # FileNotFoundError → 404; without this translation NotFound would
        # escape their except tuples and surface as a 500 from the global
        # handler (PR #946 review).
        raise FileNotFoundError(str(e)) from e
    return {c["name"]: c["type"] for c in s.get("columns", [])}


def _executes_on_bigquery(row: dict) -> bool:
    """True only when a scan must run a billable BigQuery job.

    A `query_mode='materialized'` row already has its data as a server-side
    parquet written by the scheduled materialize run — the parquet is the
    source of truth (mirrors the v2_schema branch, issue #261). A missing
    parquet is a 404, NEVER a fallback to scanning the raw upstream table."""
    return (row.get("source_type") or "") == "bigquery" and (row.get("query_mode") or "") != "materialized"


def _executes_on_databricks(row: dict) -> bool:
    """True when a scan must run on a Databricks SQL warehouse.

    Mirrors ``_executes_on_bigquery``: a ``query_mode='materialized'``
    Databricks row already has its server-side parquet, so it belongs on the
    local branch. Only a ``query_mode='remote'`` row — which has no parquet
    anywhere and never will — executes upstream.
    """
    return (row.get("source_type") or "") == "databricks" and (row.get("query_mode") or "") == "remote"


def _assert_scannable_engine(row: dict) -> None:
    """Refuse a scan/estimate on a remote row this endpoint cannot execute.

    ``_executes_on_bigquery`` answers False for every non-BigQuery source, which
    routes the request to the local-parquet branch. That is correct for local
    and materialized rows — the parquet is the source of truth — but a
    ``query_mode='remote'`` row on another engine has no parquet at all, so the
    request would surface as a bare 404 that reads like "your table isn't
    synced yet" when the truth is "this endpoint doesn't speak that engine".

    Databricks is no longer such an engine — it has its own scan branch — so
    the refusal now covers only engines that genuinely cannot execute here.

    Raises ``ValueError``, which both callers already map to HTTP 400.
    """
    source_type = (row.get("source_type") or "").strip()
    if source_type in ("", "bigquery"):
        return
    if (row.get("query_mode") or "") != "remote":
        return
    if _executes_on_databricks(row):
        return
    raise ValueError(
        f"table '{row.get('id') or row.get('name')}' is a query_mode='remote' {source_type} table; "
        "/api/v2/scan cannot execute against that engine. Use `agnes query --remote` for an "
        "interactive answer, or register the table as query_mode='materialized' so it syncs to a parquet."
    )


def _assert_scan_policy_supported(row: dict, table_id: str, user) -> None:
    """Refuse a policied table on an engine this endpoint cannot filter on.

    ``/api/query`` CAN carry a policy to a Databricks warehouse: it substitutes
    the policy body into a statement the caller wrote. This endpoint has no
    caller statement to substitute into — it BUILDS one from
    ``table_id`` + ``select`` + ``where`` — so the two need different plumbing,
    and the statement this builder produces is unfiltered. Shipping it would
    return exactly the rows the policy exists to hide (§17).

    Runs BEFORE the schema resolve, for two reasons. It refuses without doing
    any work, and — the reason it is not merely tidier — ``build_schema``
    resolves the *effective* (policy-shaped) schema, which for a remote row it
    cannot compute locally, so leaving this check downstream surfaced the
    refusal as a bare ``policy_error`` instead of the reason and next step.

    ``estimate`` is covered too, not just ``run_scan``: it returns a row COUNT
    over the caller's predicate, and the count of rows someone may not see is
    itself the thing a policy withholds.
    """
    if not _executes_on_databricks(row):
        return
    try:
        relation = policied_relation(table_id, user)
    except PolicyIdentityUnresolvable:
        raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
    except PolicyError as exc:
        raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
    if not relation.policied:
        return
    raise HTTPException(
        status_code=400,
        detail={
            "reason": "policy_unsupported_on_scan_engine",
            "engine": "databricks",
            "table": table_id,
            "message": (
                "This table carries an access policy and would be scanned on Databricks, "
                "where this endpoint cannot apply it. Refused."
            ),
            "hint": (
                "Query it with `agnes query --remote`, which does enforce the policy on "
                "Databricks, or register a query_mode='materialized' row so the scan reads "
                "a local parquet."
            ),
        },
    )


def _databricks_settings_or_400(table_id: str) -> dict:
    from connectors.databricks.semantic_layer import resolve_databricks_settings

    settings = resolve_databricks_settings()
    if settings is None:
        raise ValueError(
            f"table '{table_id}' is a Databricks table but this instance has no Databricks connection "
            "configured; set data_source.databricks.host + warehouse_id and DATABRICKS_TOKEN."
        )
    return settings


def _build_databricks_sql(
    table_row: dict,
    req: ScanRequest,
    settings: dict,
    *,
    safe_where: str | None = None,
    count_only: bool = False,
) -> str:
    """Build the warehouse-native SQL for a scan or its row-count estimate.

    Identifier quoting goes through ``quote_dbx_path``, which REFUSES a
    registry segment outside the safe alphabet rather than escaping it — the
    same rule the interactive remote path applies, kept identical so a row
    means one physical table no matter which surface reads it.
    """
    from connectors.databricks.remote import quote_dbx_path, row_target

    catalog, schema, table = row_target(table_row, str(settings.get("catalog") or ""))
    table_ref = quote_dbx_path(catalog, schema, table)

    if count_only:
        sql = f"SELECT COUNT(*) AS n FROM {table_ref}"
        if safe_where:
            sql += f" WHERE {safe_where}"
        return sql

    select_sql = ", ".join(f"`{c}`" for c in req.select) if req.select else "*"
    sql = f"SELECT {select_sql} FROM {table_ref}"
    if safe_where:
        sql += f" WHERE {safe_where}"
    if req.order_by:
        sql += f" ORDER BY {', '.join(_quote_order_by_databricks(e) for e in req.order_by)}"
    if req.limit:
        sql += f" LIMIT {int(req.limit)}"
    return sql


def _execution_dialect(row: dict, use_bq: bool) -> str:
    """Which SQL flavor the scan actually executes in.

    BigQuery for a live BQ scan, Databricks for a remote Databricks row, and
    DuckDB for everything served from a local parquet — including
    ``query_mode='materialized'`` rows on either engine, whose scan is a local
    read no matter where the data came from."""
    if use_bq:
        return "bigquery"
    if _executes_on_databricks(row):
        return "databricks"
    return "duckdb"


def _validated_where_fragment(req: "ScanRequest", schema: dict, row: dict, use_bq: bool) -> str | None:
    """Validate ``req.where`` and return the comment-stripped fragment in the
    EXECUTION dialect.

    Parse dialect follows the source_type (clients are taught BQ flavor for
    bigquery-sourced tables); render dialect follows the execution engine.
    For `query_mode='materialized'` BQ rows those differ — the scan runs on a
    local DuckDB read of the server-side parquet, so the fragment is
    transpiled BQ → DuckDB.

    Dialect-mismatch note: the schema endpoint advertises
    ``sql_flavor='duckdb'`` for materialized rows (#261), so clients write
    either flavor. That is fine — sqlglot's BigQuery parser is permissive
    and accepts DuckDB-style syntax (``x::int`` parses and normalizes to
    ``CAST``), and rendering in the execution dialect makes the result
    correct in both cases (pinned by test_accepts_duckdb_flavor_where).

    A remote Databricks row parses AND renders as ``databricks``: the schema
    endpoint advertises ``sql_flavor='databricks'`` for it, so that is the
    flavor the client was taught to write, and it is also the flavor the
    warehouse executes."""
    if not req.where:
        return None
    source_type = (row.get("source_type") or "").strip()
    if source_type == "bigquery":
        parse_dialect = "bigquery"
    elif _executes_on_databricks(row):
        parse_dialect = "databricks"
    else:
        parse_dialect = "duckdb"
    render_dialect = _execution_dialect(row, use_bq)
    return safe_where_predicate(req.where, req.table_id, schema, dialect=parse_dialect, render_dialect=render_dialect)


def _bq_dry_run_bytes(bq: BqAccess, sql: str, *, user: dict | None = None, agent_name: str = "scan") -> int:
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
    return quote_ident(parts[0]) + ("" if len(parts) == 1 else " " + " ".join(parts[1:]))


def _quote_order_by_databricks(entry: str) -> str:
    """Backtick-quote the column part; Databricks/Spark SQL quotes identifiers
    with backticks like BigQuery, not double quotes. ``_validate_order_by`` has
    already constrained the entry to ``<column>[ ASC|DESC]`` against the
    schema, so this only has to survive reserved words."""
    parts = entry.strip().split()
    return f"`{parts[0]}`" + ("" if len(parts) == 1 else " " + " ".join(parts[1:]))


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
    from connectors.bigquery.extractor import parse_bq_fqn
    from src.identifier_validation import validate_quoted_identifier

    # v51 (issue #343): ``bq_fqn`` carries a per-row fully-qualified path and
    # overrides the legacy configured-project + bucket + source_table triplet
    # the same precedence the extractor applies when building master views.
    # Without it a row whose data lives in another project resolves to a
    # non-existent table and the row is unscannable. Malformed values raise
    # rather than falling back, so a typo can't silently scan a wrong table.
    parsed_fqn = parse_bq_fqn(table_row.get("bq_fqn"))
    if parsed_fqn is not None:
        project_id, bucket, src_table = parsed_fqn
    else:
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
    # Task 10/13: BigQuery policy enforcement is not wired into this SQL
    # builder itself yet — it still builds the raw, unfiltered query (used
    # by both `estimate()` and `run_scan()`). `estimate()` never returns row
    # content (a byte/row/cost NUMBER only), so it stays as-is; `run_scan()`
    # guards its OWN call site (see the Task 13 comment there) so a policied
    # table's non-admin caller never reaches this builder in the first
    # place.
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

    _assert_scannable_engine(row)
    _assert_scan_policy_supported(row, req.table_id, user)
    schema = _resolve_schema(conn, user, req.table_id, bq)
    use_bq = _executes_on_bigquery(row)

    # Validate WHERE and capture the comment-stripped fragment for splicing,
    # rendered in the execution dialect (see _validated_where_fragment).
    safe_where = _validated_where_fragment(req, schema, row, use_bq)
    # Validate select columns exist (case-insensitive, matching order_by).
    if req.select:
        _validate_select_columns(req.select, schema)
        known = {c.lower() for c in schema}
        unknown = [c for c in req.select if c.lower() not in known]
        if unknown:
            raise ValueError(f"unknown columns: {unknown}")
    _validate_order_by(req.order_by, schema)

    if _executes_on_databricks(row):
        return _estimate_databricks(req, row, schema, safe_where, user)

    # Materialized rows join the non-BQ sources here: served from the
    # server-side parquet, so there is no billable scan to estimate.
    if not use_bq:
        return {
            "table_id": req.table_id,
            "engine": "local",
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
        "engine": "bigquery",
        "estimated_scan_bytes": int(scan_bytes),
        "estimated_result_rows": int(rows_est),
        "estimated_result_bytes": int(rows_est * avg_row_bytes),
        "bq_cost_estimate_usd": round(cost, 4),
    }


#: Quota kind → the stable `reason` code a refused estimate reports. Each kind
#: gets its OWN code rather than collapsing into one: an analyst told
#: "daily_estimate_budget_exceeded" knows to stop looping and wait for the
#: UTC-midnight reset, where "concurrent_scans_exceeded" means retry shortly
#: and "daily_budget_exceeded" means their byte quota is gone. Same status,
#: three different next actions.
_ESTIMATE_QUOTA_REASONS = {
    KIND_DAILY_BYTES: "daily_budget_exceeded",
    KIND_DAILY_ESTIMATES: "daily_estimate_budget_exceeded",
}


def _estimate_databricks(req: ScanRequest, row: dict, schema: dict, safe_where: str | None, user) -> dict:
    """Estimate a Databricks remote scan.

    ``estimated_scan_bytes`` is ``None``, not ``0``. Databricks has no dry-run
    primitive, so the bytes a statement will scan genuinely cannot be known
    before running it — and ``0`` is not the neutral answer it looks like: on
    every other row in this response it means "served locally, costs nothing",
    which is the opposite of what a warehouse scan does. ``None`` says
    unknown, and the CLI renders it as such.

    What CAN be known exactly, and cheaply, is the row count: a
    ``COUNT(*)`` with the caller's own predicate is an aggregate the warehouse
    answers without shipping the rows. So this estimate is *better* than the
    BigQuery one where it counts (real count vs. bytes/avg-row-size division)
    and honest where it cannot be.
    """
    from connectors.databricks.remote import DatabricksRemoteError, execute_select

    settings = _databricks_settings_or_400(req.table_id)

    # Unlike BigQuery's arm, this is not a free dry-run: a filtered COUNT(*)
    # over a Delta table is a real scan on the warehouse. So it takes the
    # guards the FETCH path applies to every engine — the daily-budget
    # pre-flight and the caller's concurrent-scan slot — and gets the
    # INTERACTIVE deadline rather than the materialize one, since somebody is
    # waiting on an estimate and a 15-minute count is not an estimate.
    #
    # Those two are necessary and not sufficient, for a reason worth stating
    # rather than leaving implied. The concurrent slot caps how many warehouse
    # statements one caller has IN FLIGHT, not how many they issue in a day.
    # The daily budget is denominated in BYTES, and the only byte figure
    # Databricks reports is what a statement RETURNED — there is no
    # scanned-bytes metric and no dry-run to price one — so a COUNT, which
    # returns one row, records essentially nothing against a 50 GiB budget. An
    # agent loop calling this endpoint would therefore run unbounded: every
    # call billable, none of them visible to the ledger.
    #
    # Hence the third guard, denominated in the one unit that is honestly
    # available here: STATEMENTS submitted today. It is deliberately a
    # separate counter rather than a synthetic byte charge, because inventing
    # a byte number for a statement whose bytes are unknown would corrupt the
    # budget for BigQuery, which reports real ones.
    quota = _build_quota_tracker()
    user_id = identity_for_audit(user)[1] or "anon"
    try:
        quota.check_daily_budget(user=user_id)
        quota.check_daily_estimates(user=user_id)
        with quota.acquire(user=user_id):
            count_sql = _build_databricks_sql(row, req, settings, safe_where=safe_where, count_only=True)
            # Counted at submission, before the result is known: what the
            # warehouse bills for is the statement reaching it, so a loop of
            # statements that error out costs the same as a loop that works.
            quota.record_estimate(user=user_id)
            _columns, rows, _truncated, returned_bytes = execute_select(
                count_sql,
                settings=settings,
                limit=1,
                cap_bytes=0,  # a single COUNT row; the byte cap is meaningless here
                timeout_s=_databricks_estimate_timeout_s(),
            )
        # Accurate, and small by construction — recorded so the estimate is not
        # simply invisible to the byte budget, never as the control that bounds
        # it. `record_estimate` above is that control.
        quota.record_bytes(user=user_id, n=int(returned_bytes or 0))
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": _ESTIMATE_QUOTA_REASONS.get(exc.kind, "concurrent_scans_exceeded"),
                "kind": exc.kind,
                "current": exc.current,
                "limit": exc.limit,
                "retry_after_seconds": exc.retry_after_seconds,
            },
        )
    except DatabricksRemoteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail())

    matched = int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0
    if req.limit:
        matched = min(matched, req.limit)

    schema_lower = {k.lower(): v for k, v in schema.items()}
    cols_for_estimate = [schema_lower[c.lower()] for c in (req.select or []) if c.lower() in schema_lower] or list(
        schema.values()
    )
    avg_row_bytes = max(1, sum(_avg_bytes_for_type(t) for t in cols_for_estimate))

    return {
        "table_id": req.table_id,
        "engine": "databricks",
        "estimated_scan_bytes": None,
        "estimated_result_rows": matched,
        "estimated_result_bytes": int(matched * avg_row_bytes),
        "bq_cost_estimate_usd": None,
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
                user_id=identity_for_audit(user)[0],
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
    except HTTPException as exc:
        # Mirrors the branch `scan_endpoint` already carries, and for the same
        # reason it was added there: `estimate()` now raises HTTPException
        # directly for structured rejections — a policied table on this engine,
        # an unresolvable policy identity, a quota refusal, a warehouse error —
        # and none of those are in the tuple below, so the request answered
        # correctly while writing NO audit row. Every other estimate failure
        # writes one. Log it, then re-raise unchanged so the client still sees
        # the original status and detail.
        try:
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="snapshot.estimate",
                resource=resource,
                params={
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "error": str(exc.detail)[:200],
                },
                result=f"error.{exc.status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for snapshot.estimate; continuing")
        raise
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
                user_id=identity_for_audit(user)[0],
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


def _databricks_estimate_timeout_s() -> float:
    """Deadline for the estimate's COUNT(*).

    The interactive remote-query timeout, not the snapshot one: `--estimate`
    is what an analyst runs BEFORE deciding whether to fetch, so it has a
    person waiting on it. Letting it inherit the 900 s materialize budget
    would hold a request thread for fifteen minutes to answer "should I bother".

    Delegates to `/api/query`'s reader rather than re-reading the key, so the
    two interactive callers cannot drift apart on what a bad or zero value
    means. (Function-local import: the module-level dependency runs
    query → v2_scan, and reversing it at import time would cycle.)
    """
    from app.api.query import _databricks_statement_timeout_s

    return _databricks_statement_timeout_s()


def _databricks_scan_timeout_s() -> Optional[float]:
    """Statement timeout for the scan path, or ``None`` for no deadline.

    Deliberately NOT `data_source.databricks.remote_query_timeout_seconds`
    (default 120), which bounds an *interactive* answer someone is waiting on.
    A snapshot fetch is a materialize — the analyst expects it to take a while
    — so it gets its own, longer budget, and the byte cap remains the control
    that bounds size.

    `0` disables the deadline, the same meaning the sibling materialize knob
    `statement_timeout_seconds` carries in `app/api/sync.py` — an operator who
    sets it wants an unbounded fetch, not a silent snap back to 900. A
    non-numeric value warns and uses the default: a typo must not be the thing
    that removes the deadline.
    """
    raw = get_value("data_source", "databricks", "scan_timeout_seconds", default=900)
    try:
        t = float(raw) if raw is not None else 900.0
    except (TypeError, ValueError):
        logger.warning(
            "data_source.databricks.scan_timeout_seconds is not numeric (%r); "
            "using the 900s default. Set a number of seconds or 0 to disable.",
            raw,
        )
        t = 900.0
    return t if t > 0 else None


def _run_bq_scan(bq: BqAccess, sql: str, *, user: dict | None = None) -> tuple[pa.Table, dict]:
    """Run the billable BQ scan query via the google-cloud-bigquery client
    (not the DuckDB `bigquery_query()` extension) so the job carries cost-
    attribution labels (`job_labels_for(user, "scan")`) and its job metadata
    (job_id / bytes_processed / bytes_billed) can be surfaced in the scan
    audit log. Mirrors the labeled-job shape of `src.remote_query.register_bq`
    (#751) — a scan result is fully materialized to Arrow anyway, so
    `client.query(...).to_arrow()` is shape-equivalent to the extension call
    it replaces.

    The fully-materialized remote-select path (`agnes query --remote
    --auto-snapshot` / `run_remote_select_to_arrow`) is labeled the same way,
    via the shared `run_bq_query_to_arrow` helper (#752). The interactive,
    LIMIT-capped `/api/query --remote` path still runs its billable job through
    the DuckDB `bigquery_query()` extension (small, bounded byte volume);
    labeling it is deferred — see docs/planning/752-bq-billable-labels.md.

    Returns (arrow_table, job_info) where job_info has keys
    bq_job_id/bytes_scanned/bytes_billed for the caller's audit log.

    SQL here is user-derived → BadRequest → 400 (`bad_request_status="client_error"`).
    """
    from connectors.bigquery.access import run_bq_query_to_arrow

    return run_bq_query_to_arrow(bq, sql, labels=job_labels_for(user, "scan"))


def run_scan(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    raw_request: dict,
    *,
    bq: BqAccess,
    quota: QuotaTracker,
    job_info: dict | None = None,
) -> bytes:
    """Validate → quota → execute → serialize. Returns Arrow IPC bytes.

    ``job_info``, if provided, is populated in place with the billable BQ
    job's ``bq_job_id`` / ``bytes_scanned`` / ``bytes_billed`` (see
    ``_run_bq_scan``) so the caller can attach them to the audit log. Stays
    empty for local-table scans and the ``from_query`` streaming path, which
    doesn't expose per-job metadata.

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

        # Task 11 (§10): `policy_info` reports back which tables (if any)
        # this raw SELECT touched a policy on -- `run_remote_select_to_arrow`
        # has no response envelope of its own to carry it. Folded into the
        # caller's own `job_info` so `scan_endpoint` builds the
        # `X-Agnes-Row-Scope` header the same way for both branches of
        # `run_scan`.
        policy_info: dict = {}
        table = run_remote_select_to_arrow(
            conn,
            user,
            raw_request["from_query"],
            bq=bq,
            quota=quota,
            policy_info=policy_info,
        )
        if job_info is not None and policy_info.get("policied_table_ids"):
            job_info["policied_table_ids"] = policy_info["policied_table_ids"]
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

    _assert_scannable_engine(row)
    _assert_scan_policy_supported(row, req.table_id, user)
    schema = _resolve_schema(conn, user, req.table_id, bq)
    use_bq = _executes_on_bigquery(row)
    # Validate WHERE and capture the comment-stripped fragment for splicing,
    # rendered in the execution dialect (see _validated_where_fragment).
    safe_where = _validated_where_fragment(req, schema, row, use_bq)
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
    user_id = identity_for_audit(user)[1] or "anon"

    # Pre-flight quota check — fail BEFORE running the BQ scan so the user
    # doesn't pay for a query whose result we'd then refuse to return.
    quota.check_daily_budget(user=user_id)

    with quota.acquire(user=user_id):
        if _executes_on_databricks(row):
            from connectors.databricks.remote import DatabricksRemoteError, execute_scan_to_arrow

            settings = _databricks_settings_or_400(req.table_id)
            try:
                # Inside the try: `_build_databricks_sql` raises
                # DatabricksRemoteError for a registry segment outside the safe
                # alphabet, and `scan_endpoint` catches neither that nor
                # anything it is not listed in — so an operator's typo in a
                # registry row surfaced as an unexplained 500 instead of the
                # 400 naming the bad value that the BigQuery sibling returns.
                dbx_sql = _build_databricks_sql(row, req, settings, safe_where=safe_where)
                table = execute_scan_to_arrow(
                    dbx_sql,
                    settings=settings,
                    cap_bytes=_max_result_bytes(),
                    timeout_s=_databricks_scan_timeout_s(),
                )
            except DatabricksRemoteError as exc:
                raise HTTPException(status_code=exc.status, detail=exc.detail())
        elif not use_bq:
            # Local execution: query the parquet directly. Covers non-BQ
            # sources AND `query_mode='materialized'` BQ rows — their parquet
            # was already written by the scheduled materialize run, so scanning
            # the raw upstream table again would re-bill the whole scan
            # (mirrors the v2_schema materialized branch, issue #261). Resolve
            # by source-name-agnostic lookup — the extract directory is not
            # necessarily the source_type (e.g. the bundled `demo` extract
            # registers tables as 'local' but lives under extracts/demo/), and
            # `source_type` may be NULL/empty for legacy rows.
            # resolve_local_parquet_glob handles both; for materialized BQ rows
            # the source_type fast path hits extracts/bigquery/data/<id>.parquet.
            # `_glob` also covers the PARTITIONED layout — a directory of
            # per-period parquets, for which the single-file lookup returned
            # None and a healthy fully-synced table 404-ed here (Devin Review on
            # #1189). DuckDB expands the `<dir>/*.parquet` glob it returns.
            from app.utils import LOCAL_PARQUET_READ_EXPR, resolve_local_parquet_glob

            # `registry_name`: the write side keys the parquet filename by the
            # row's `name`, not its `id` — see `_physical_key_candidates` in
            # app/utils.py. Without it, any row whose id was slugified from
            # the name 404-ed here while fully synced.
            parquet = resolve_local_parquet_glob(req.table_id, source_type, registry_name=row.get("name"))
            if parquet is None:
                raise FileNotFoundError(req.table_id)

            # Table access policies (§5): this connection is a throwaway
            # :memory: DB with nothing but the parquet attached — no
            # analytics catalog, so the policy body's own `FROM <name>` has
            # nothing to bind against unless we wrap it. The inert (not
            # policied) branch below stays byte-identical to the
            # pre-existing code.
            try:
                relation = policied_relation(req.table_id, user)
            except PolicyIdentityUnresolvable:
                raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
            except PolicyError as exc:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
            # Task 11 (§10): report through job_info, the same out-param
            # _run_bq_scan already uses for BQ job metadata, so scan_endpoint
            # builds the X-Agnes-Row-Scope header from one place regardless
            # of which branch of run_scan actually ran.
            if relation.policied and job_info is not None:
                job_info["policied_table_ids"] = [relation.table_id]

            local = _open_duckdb(":memory:")
            try:
                projection = ", ".join(quote_ident(c) for c in req.select) if req.select else "*"
                if relation.policied:
                    # The parquet path is server-resolved, never user
                    # input, so it is safe to splice as an escaped literal
                    # — it must NOT be a `?` placeholder: the policy binds
                    # named `$user_*` parameters, and DuckDB refuses to mix
                    # positional and named parameters in one statement.
                    escaped_parquet = parquet.replace("'", "''")
                    from_sql = policied_from_sql(
                        relation,
                        table_name=row["name"],
                        source_sql=f"read_parquet('{escaped_parquet}', union_by_name=true, hive_partitioning=true)",
                    )
                    # Read-path guard (§17): a masking policy that re-derives a
                    # column `*` still emits yields duplicate output names and
                    # leaks the plaintext copy. DESCRIBE the policy relation
                    # ITSELF (not the outer `SELECT * FROM (...)`, whose binder
                    # silently renames the second dup to `_1`, hiding it).
                    try:
                        _out_cols = [
                            r[0] for r in local.execute(f"DESCRIBE {from_sql}", dict(relation.params)).fetchall()
                        ]
                        assert_unique_output_columns(_out_cols, relation.table_id)
                    except PolicyError as exc:
                        raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
                    sql = f"SELECT {projection} FROM {from_sql}"
                    bind_params: dict | list = dict(relation.params)
                else:
                    sql = f"SELECT {projection} FROM {LOCAL_PARQUET_READ_EXPR}"
                    bind_params = [parquet]
                if safe_where:
                    sql += f" WHERE {safe_where}"
                if req.order_by:
                    sql += f" ORDER BY {', '.join(_quote_order_by_duckdb(e) for e in req.order_by)}"
                if req.limit:
                    sql += f" LIMIT {int(req.limit)}"
                try:
                    table = local.execute(sql, bind_params).arrow()
                except duckdb.InvalidInputException:
                    # Corrupt/unreadable parquet ("No magic bytes found…").
                    # duckdb files it under ProgrammingError, but it is an
                    # operational failure the caller cannot fix by editing
                    # the query — re-raise before the client-error catch
                    # below so it reaches the logged, sanitized 500 handler.
                    raise
                except (duckdb.DataError, duckdb.ProgrammingError) as e:
                    # Fail loud, not 500: a predicate can pass validation (and
                    # BQ→DuckDB transpile for materialized rows) yet still hit
                    # a construct DuckDB can't bind or resolve (Binder/
                    # CatalogException → ProgrammingError) or a data value it
                    # can't convert (ConversionException → DataError). Those
                    # are request-attributable → ValueError → 400 with the
                    # real reason. Other failures (IO → OperationalError,
                    # out-of-memory, internal errors) stay un-caught and reach
                    # the 500 handler.
                    raise ValueError(f"local scan failed for {req.table_id!r}: {e}") from e
            finally:
                local.close()
        else:
            if bool(row.get("access_policy_sql")):
                # Task 13 (§8 ratchet): this branch pushes the scan straight
                # to BigQuery via `_build_bq_sql`/`_run_bq_scan` -- Task 10
                # only wired `policied_relation(dialect="bigquery")` into
                # `/api/query`'s AST-rewrite path, never into this
                # table_id-shaped surface's live-BQ branch (`_build_bq_sql`'s
                # own comment already flagged this), so unguarded it would
                # hand back the RAW, unfiltered physical table -- select/
                # where/order_by are validated against the (COVERED)
                # effective schema above, but that only stops REQUESTING an
                # EXCLUDE'd column by name, not ROW filtering, and an
                # implicit `SELECT *` would still return every column. Fail
                # closed (§17) exactly like the local-parquet branch's own
                # `policied_relation` failure handling just above. Admin
                # bypass preserved via `policied_relation` itself, not a
                # bare `access_policy_sql` check.
                #
                # TODO(follow-up): same as `app/api/v2_sample.py`'s BQ
                # branch -- wire via `policied_relation(dialect="bigquery")`
                # + the BQ jobs API (`run_bq_query_to_arrow`) instead of the
                # `_run_bq_scan` push-down, once this endpoint's cost/quota/
                # label semantics are worked out for that execution path.
                try:
                    bq_relation = policied_relation(req.table_id, user)
                except PolicyIdentityUnresolvable:
                    raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
                except PolicyError as exc:
                    raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
                if bq_relation.policied:
                    raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": req.table_id})
            bq_sql = _build_bq_sql(row, bq.projects.data, req, safe_where=safe_where)
            table, bq_job_info = _run_bq_scan(bq, bq_sql, user=user)
            if job_info is not None:
                job_info.update(bq_job_info)

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
    job_info: dict = {}
    try:
        ipc = run_scan(conn, user, raw, bq=bq, quota=quota, job_info=job_info)
        # Decode row count from IPC without re-running the scan.
        # bytes_scanned / bytes_billed / bq_job_id come from job_info,
        # populated by _run_bq_scan for BigQuery-source scans (#752); stay
        # None for local-table scans and the from_query streaming path.
        try:
            from app.api.v2_arrow import parse_ipc_bytes

            rows_written = parse_ipc_bytes(ipc).num_rows
        except Exception:
            rows_written = None
        try:
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="snapshot.create",
                resource=resource,
                params={
                    "rows_written": rows_written,
                    "bytes_scanned": job_info.get("bytes_scanned"),
                    "bytes_billed": job_info.get("bytes_billed"),
                    "bq_job_id": job_info.get("bq_job_id"),
                    "snapshot_name": snapshot_name,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                },
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for snapshot.create; continuing")
        # Task 11 (§10): this endpoint returns raw Arrow IPC bytes -- no JSON
        # body to carry `row_scope` -- so disclose via a response header
        # instead, built from whichever branch of `run_scan` populated
        # `job_info["policied_table_ids"]`. `json.dumps` default
        # `ensure_ascii=True` keeps the em dash in the note ASCII/Latin-1
        # safe, which raw HTTP header encoding requires (Starlette encodes
        # header values as latin-1).
        response_headers: dict[str, str] = {}
        policied_ids = job_info.get("policied_table_ids")
        row_scope = row_scope_payload(policied_ids)
        if row_scope is not None:
            response_headers["X-Agnes-Row-Scope"] = json.dumps(row_scope)
        # Table access policies §3.4/§10.3 (plan Task 18): the snapshot
        # policy fingerprint -- present only when the read touched EXACTLY
        # one policied table, mirroring `SnapshotMeta.table_id`'s own
        # single-table shape. A `from_query` scan that joined two-or-more
        # policied tables has no single well-defined fingerprint to stamp
        # -- a documented gap, not a silent one: `agnes pull` simply has
        # nothing to compare for that snapshot and never blocks it on
        # policy drift.
        #
        # `X-Agnes-Policy-Table-Id` names WHICH table that fingerprint
        # belongs to, and is what makes the fingerprint usable at all on
        # the `from_query` branch: `SnapshotMeta.table_id` is the snapshot
        # NAME the caller passed positionally there (`agnes snapshot create
        # <name> --from-query …`, and every `agnes query --remote
        # --auto-snapshot`), never a registry id, so `agnes pull` has
        # nothing to look the current fingerprint up by. Without it the
        # manifest lookup resolves to None on every pull, `None != <hash>`
        # reads as "stale", and the snapshot is withheld permanently with
        # no recovery. Sent together with the fingerprint whenever the id
        # is header-safe: a registry id is derived from the table name and
        # is not charset-restricted, and Starlette encodes raw header
        # values as latin-1, so an id outside that range would 500 the
        # whole response. `X-Agnes-Row-Scope` above sidesteps the same
        # hazard via `json.dumps`' ASCII escaping; here the header is a
        # bare id by design, so skip it instead -- the puller then falls
        # back to `SnapshotMeta.table_id` exactly as it did before this
        # header existed, which never blocks a snapshot it cannot resolve.
        if policied_ids and len(policied_ids) == 1:
            fingerprint = policy_fingerprint(policied_ids[0], user)
            if fingerprint:
                response_headers["X-Agnes-Policy-Fingerprint"] = fingerprint
                policied_id = str(policied_ids[0])
                try:
                    policied_id.encode("latin-1")
                except UnicodeEncodeError:
                    logger.warning(
                        "policy table id %r is not header-safe; omitting X-Agnes-Policy-Table-Id",
                        policied_id,
                    )
                else:
                    response_headers["X-Agnes-Policy-Table-Id"] = policied_id
        return Response(content=ipc, media_type=CONTENT_TYPE, headers=response_headers or None)
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
                user_id=identity_for_audit(user)[0],
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
                user_id=identity_for_audit(user)[0],
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
