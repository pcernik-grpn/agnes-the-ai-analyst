"""GET /api/v2/schema/{table_id} — table column metadata (spec §3.2)."""

from __future__ import annotations
import logging
import time
from fastapi import APIRouter, Depends, HTTPException
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from src.db import _open_duckdb
from src.audit_helpers import identity_for_audit, client_kind_from_user
from src.rbac import can_access_table
from src.access_policy import PolicyError, PolicyIdentityUnresolvable, effective_schema, policy_cache_identity
from app.api.v2_cache import TTLCache
from connectors.bigquery.access import BqAccess, BqAccessError, get_bq_access

from src.repositories import (
    audit_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2", tags=["v2"])

_schema_cache = TTLCache(maxsize=512, ttl_seconds=3600)


class NotFound(Exception):
    pass


_BQ_DIALECT_HINTS = {
    "date_literal": "DATE '2026-01-01'",
    "timestamp_literal": "TIMESTAMP '2026-01-01 00:00:00 UTC'",
    "interval_subtract": "DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)",
    "regex": "REGEXP_CONTAINS(field, r'pattern')",
    "cast": "CAST(x AS INT64)",
}


def _fetch_bq_schema(bq, dataset: str, table: str) -> list[dict]:
    """Fetch column list via the shared ``_fetch_bq_columns_full_impl`` helper.

    Pre-#155 this had its own INFORMATION_SCHEMA.COLUMNS query; consolidating
    with ``_fetch_bq_table_options`` (now also delegating to the same shared
    SQL) halves the BQ job count on cache miss. Returns the schema-endpoint
    column shape: name / type / nullable / description.

    Calls the raising variant so BQ exceptions reach ``translate_bq_error``
    with their original type (Forbidden → 502, BadRequest → 400, etc.).
    """
    from connectors.bigquery.access import _fetch_bq_columns_full_impl, translate_bq_error, BqAccessError

    try:
        rows = _fetch_bq_columns_full_impl(bq, dataset, table)
    except (ValueError, BqAccessError):
        # ValueError ("unsafe identifier") and BqAccessError propagate
        # unchanged — the endpoint's existing handlers expect those types.
        raise
    except Exception as e:
        # Any other BQ-side exception goes through translate_bq_error so
        # the response status is classified correctly.
        raise translate_bq_error(e, bq.projects, bad_request_status="upstream_error")

    return [
        {
            "name": r["name"],
            "type": r["type"],
            "nullable": r["nullable"],
            "description": "",
        }
        for r in rows
    ]


def _fetch_bq_table_options(bq, dataset: str, table: str) -> dict:
    """Best-effort fetch of partition/cluster info via the shared
    `fetch_bq_columns_full` helper.

    Returns ``{}`` on ANY failure (best-effort). Same load-bearing
    contract as before: the /schema endpoint must keep returning 200
    with empty partition info when this fails.
    """
    from connectors.bigquery.access import fetch_bq_columns_full

    rows = fetch_bq_columns_full(bq, dataset, table)
    if not rows:
        return {}

    partition_by = next(
        (r["name"] for r in rows if r["is_partitioning_column"]),
        None,
    )
    clustered_rows = [r for r in rows if r["clustering_ordinal_position"] is not None]
    clustered_rows.sort(key=lambda r: r["clustering_ordinal_position"])
    clustered_by = [r["name"] for r in clustered_rows]
    return {"partition_by": partition_by, "clustered_by": clustered_by}


def build_schema(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    table_id: str,
    *,
    bq: BqAccess,
) -> dict:
    # RBAC + existence check MUST run before cache lookup — otherwise an
    # unauthorized user can read cached schema fetched by an authorized one.
    repo = table_registry_repo()
    row = repo.get(table_id)
    if not row:
        raise NotFound(table_id)

    if not can_access_table(user, table_id, conn):
        raise PermissionError(table_id)

    # Table access policies (§11 + §9): a policied table's schema is
    # caller-scoped the same way its rows are -- a non-admin must never see
    # a column an EXCLUDE'd policy has already removed from every read
    # surface. `_schema_cache` keyed on `table_id` alone would hand one
    # caller's (or the admin's raw) schema to the next caller regardless of
    # identity, so a policied table's key carries the caller's identity
    # tuple instead (`policy_cache_identity`, §9) -- never the bare
    # `table_id` a non-policied table's cache entry lives under.
    # `build_schema_uncached` mirrors the plain-`table_id` write for the
    # non-policied case below; the identity-keyed write for the policied
    # case happens here, after `_apply_effective_schema`, because
    # `build_schema_uncached` deliberately never takes `user`.
    has_access_policy = bool(row.get("access_policy_sql"))
    cache_key = table_id
    if has_access_policy:
        cache_key = f"{table_id}|policy:{policy_cache_identity(user, table_id=table_id)!r}"

    cached = _schema_cache.get(cache_key)
    if cached is not None:
        return cached

    payload = build_schema_uncached(conn, table_id, bq=bq, row=row)

    if has_access_policy:
        payload = _apply_effective_schema(payload, table_id, user)
        _schema_cache.set(cache_key, payload)

    return payload


def _apply_effective_schema(payload: dict, table_id: str, user: dict) -> dict:
    """Override ``payload["columns"]`` with ``effective_schema``'s
    hidden-aware list for a non-admin caller on a policied table.

    ``effective_schema`` itself returns ``None`` for the admin bypass (§12)
    -- same signal ``policied_relation`` gives every other surface -- so an
    admin's raw, unfiltered ``payload`` (the one ``build_schema_uncached``
    just built and, per the caller above, never cached) passes through
    completely untouched. A new dict is returned rather than mutating
    ``payload`` in place, purely to keep this function's contract obvious
    at the call site — nothing else holds a reference to it yet.

    Hidden columns are DROPPED here, not merely marked, even though
    ``effective_schema`` itself keeps them (hidden=True) in its own return
    value. This is the one place that distinction matters: ``build_schema``
    is also `/api/v2/scan`'s own schema source (`_resolve_schema` in
    ``app/api/v2_scan.py``, "used by validator + projection check" — the
    where-validator §11 promises protection to), and that consumer collapses
    the column list to a bare ``{name: type}`` dict with no idea a `hidden`
    key exists. A hidden entry that stayed in ``payload["columns"]`` would
    still read as "a real, referenceable column" there — `select`/`where`/
    `order_by` would keep validating a reference to an EXCLUDE'd column as
    clean, deferring the rejection to a raw engine error deep inside the
    policy-wrapped relation instead of the clean 400 the where-validator
    exists to give. Dropping it here is what actually delivers "the
    where-validator validates against it" for every current and future
    consumer of this shape, without either of them needing to learn about
    `hidden`.
    """
    columns = effective_schema(table_id, user)
    if columns is None:
        return payload
    return {**payload, "columns": [c for c in columns if not c.get("hidden")]}


def build_schema_uncached(
    conn: duckdb.DuckDBPyConnection,
    table_id: str,
    *,
    bq: BqAccess,
    row: dict | None = None,
) -> dict:
    """Build the schema response and populate `_schema_cache`. **Skips
    RBAC and cache-hit short-circuit** — call only from contexts where
    those are unnecessary (warmup) or already enforced upstream
    (`build_schema`).

    Pass `row` from the upstream caller's `repo.get(table_id)` to avoid
    a redundant DB round-trip; if not provided, `build_schema_uncached`
    fetches it itself (the warmup-direct call site).
    """
    if row is None:
        repo = table_registry_repo()
        row = repo.get(table_id)
        if not row:
            raise NotFound(table_id)

    source_type = row.get("source_type") or ""
    query_mode = row.get("query_mode") or ""
    if source_type == "internal":
        # Internal data source — schema lives in system.duckdb, not in the
        # parquet/BQ paths the other branches use. Delegate to the
        # connector's own introspector so /api/v2/schema/agnes_sessions
        # returns the same shape as /api/v2/schema/<keboola-table>.
        from connectors.internal.access import get_schema as _get_internal_schema
        from src.db import _get_state_dir

        system_db_path = str(_get_state_dir() / "system.duckdb")
        cols = _get_internal_schema(system_db_path, table_id)
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "duckdb",
            "columns": [
                {"name": c["name"], "type": c["type"], "nullable": c["nullable"], "description": ""} for c in cols
            ],
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": {},
        }
    # Issue #261: a `source_type='bigquery'` row with `query_mode='materialized'`
    # has the data on local disk as a parquet — same shape as Keboola local
    # tables. Hitting BigQuery INFORMATION_SCHEMA on every schema call was
    # the root cause of the materialized-schema cold-start anomaly observed
    # in the 0.51.0 perf tests (4.6 s vs 1.0 s for remote VIEW). Use the
    # local-parquet branch for any materialized source regardless of
    # `source_type` — the parquet is the source of truth.
    elif source_type == "bigquery" and query_mode != "materialized":
        dataset = row.get("bucket") or ""
        source_table = row.get("source_table") or table_id
        columns = _fetch_bq_schema(bq, dataset, source_table)
        opts = _fetch_bq_table_options(bq, dataset, source_table)
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "bigquery",
            "columns": columns,
            "partition_by": opts.get("partition_by"),
            "clustered_by": opts.get("clustered_by", []),
            "where_dialect_hints": _BQ_DIALECT_HINTS,
        }
    elif source_type == "databricks" and query_mode == "remote":
        # No parquet exists for a remote Databricks row, so the local branch
        # below would 404 — which reads as "not synced yet" and sends the
        # caller looking for a sync that is never going to happen. Read the
        # columns from Unity Catalog instead, and advertise Databricks SQL as
        # the flavor: that is the dialect `agnes query --remote` ships.
        from connectors.databricks.remote import DIALECT_HINTS, DatabricksRemoteError, fetch_schema
        from connectors.databricks.semantic_layer import resolve_databricks_settings

        settings = resolve_databricks_settings()
        if settings is None:
            raise NotFound(table_id)
        try:
            columns = fetch_schema(row, settings=settings)
        except DatabricksRemoteError as exc:
            raise ValueError(exc.message) from exc
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "databricks",
            "columns": columns,
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": DIALECT_HINTS,
        }
    elif source_type == "snowflake" and query_mode == "remote":
        # Same shape as the Databricks branch above and for the same reason: a
        # remote Snowflake row has no parquet, so the local branch below would
        # 404 — and the CLI renders that as "not found in the registry" on a
        # table that is registered and answers `agnes query --remote` fine.
        # Flavor stays `duckdb`: unlike Databricks there is no per-query ship
        # to the warehouse, the analyst queries the ATTACHed `sf` catalog.
        from connectors.snowflake.remote import fetch_schema as snowflake_fetch_schema
        from connectors.snowflake.settings import resolve_snowflake_settings

        settings = resolve_snowflake_settings()
        if settings is None:
            raise NotFound(table_id)
        try:
            # allow_empty=False: a `WHERE table_schema/table_name` miss — a
            # dropped table, a repointed connection, a case-mismatched bucket —
            # would otherwise return `columns: []` as a successful answer AND
            # cache it, hiding for the cache's lifetime exactly the breakage
            # this endpoint is being asked about.
            columns = snowflake_fetch_schema(row, settings=settings, allow_empty=False)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"snowflake schema lookup failed: {exc}") from exc
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "duckdb",
            "columns": columns,
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": {},
        }
    else:
        # Local source — read schema from the parquet via DuckDB. Resolve the
        # parquet by source-name-agnostic lookup: the extract directory is not
        # necessarily the source_type (e.g. the bundled `demo` extract registers
        # tables as source_type='local' but lives under extracts/demo/).
        # `_glob` so a PARTITIONED table resolves too: that sync writes
        # `data/<table_id>/<partition>.parquet`, a directory, so the single-file
        # lookup returned None and a healthy fully-synced table 404-ed here —
        # the same layout gap the preview surface fixed (Devin Review on #1189).
        # DuckDB expands the returned `<dir>/*.parquet` glob, exactly as the
        # extractor's own master view over the partition dir does.
        from app.utils import LOCAL_PARQUET_READ_EXPR, resolve_local_parquet_glob

        # `registry_name`: the write side keys the parquet filename by the
        # row's `name`, not its `id` — see `_physical_key_candidates` in
        # app/utils.py. Without it, any row whose id was slugified from the
        # name 404-ed here while fully synced.
        parquet = resolve_local_parquet_glob(table_id, source_type, registry_name=row.get("name"))
        if parquet is None:
            raise NotFound(table_id)
        local_conn = _open_duckdb(":memory:")
        try:
            cols = local_conn.execute(
                f"DESCRIBE SELECT * FROM {LOCAL_PARQUET_READ_EXPR}",
                [parquet],
            ).fetchall()
        finally:
            local_conn.close()
        payload = {
            "table_id": table_id,
            "source_type": source_type,
            "sql_flavor": "duckdb",
            "columns": [{"name": c[0], "type": c[1], "nullable": c[2] == "YES", "description": ""} for c in cols],
            "partition_by": None,
            "clustered_by": [],
            "where_dialect_hints": {},
        }

    # A policied table's schema is caller-scoped (§11), so it must never
    # land in a cache keyed on `table_id` alone — `row` (not `user`; this
    # function never takes one, see the class-level note in
    # `tests/test_v2_schema.py`) already carries `access_policy_sql`, which
    # is all the information needed to withhold the write here. The
    # identity-keyed write for the policied case happens in `build_schema`
    # instead (§9, `policy_cache_identity`), which has the `user` this
    # function deliberately does not.
    if not row.get("access_policy_sql"):
        _schema_cache.set(table_id, payload)
    return payload


@router.get("/schema/{table_id}")
def schema(
    table_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    bq: BqAccess = Depends(get_bq_access),
):
    # Plain ``def`` — opens a `bq.duckdb_session()` and runs sync metadata
    # queries through the BQ extension. See PR #188 Tier 1 entry.
    t0 = time.monotonic()
    resource = f"table:{table_id}"[:256]
    try:
        result = build_schema(conn, user, table_id, bq=bq)
        try:
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="catalog.schema",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000)},
                result="success",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed for catalog.schema; continuing")
        return result
    except (NotFound, PermissionError, ValueError, BqAccessError, PolicyIdentityUnresolvable, PolicyError) as exc:
        try:
            if isinstance(exc, NotFound):
                status_code = 404
            elif isinstance(exc, PermissionError):
                status_code = 403
            elif isinstance(exc, ValueError):
                status_code = 400
            elif isinstance(exc, PolicyIdentityUnresolvable):
                status_code = 403
            elif isinstance(exc, PolicyError):
                status_code = 500
            else:
                status_code = BqAccessError.HTTP_STATUS.get(exc.kind, 500)  # type: ignore[union-attr]
            audit_repo().log(
                user_id=identity_for_audit(user)[0],
                action="catalog.schema",
                resource=resource,
                params={"duration_ms": int((time.monotonic() - t0) * 1000), "error": str(exc)[:200]},
                result=f"error.{status_code}",
                client_kind=client_kind_from_user(user),
            )
        except Exception:
            logger.exception("audit_log write failed on error path for catalog.schema; continuing")
        if isinstance(exc, NotFound):
            raise HTTPException(status_code=404, detail=f"table {table_id!r} not found")
        if isinstance(exc, PermissionError):
            from src.rbac import table_not_in_stack_message

            raise HTTPException(
                status_code=403,
                detail=table_not_in_stack_message(table_id),
            )
        if isinstance(exc, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"error": "unsafe_identifier", "message": str(exc), "details": {}},
            )
        if isinstance(exc, PolicyIdentityUnresolvable):
            raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
        if isinstance(exc, PolicyError):
            raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
        raise HTTPException(
            status_code=BqAccessError.HTTP_STATUS.get(exc.kind, 500),  # type: ignore[union-attr]
            detail={"error": exc.kind, "message": exc.message, "details": exc.details},  # type: ignore[union-attr]
        )
