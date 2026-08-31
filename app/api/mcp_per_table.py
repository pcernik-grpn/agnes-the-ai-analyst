"""Per-table outbound MCP tools (RFC #461 §7).

For every row in ``table_registry`` we expose a single REST endpoint
``POST /api/mcp/query-table/{table_id}`` that runs a constrained
SELECT against the local DuckDB view backing that table. The filter
shape is intentionally simple — a flat ``{column: value}`` equality
dict — so the schema stays guessable by AI clients and the SQL we
build is trivially safe to parameterize.

* RBAC: the caller must have access to ``ResourceType.TABLE`` for
  ``table_id`` (admin short-circuits via ``can_access``).
* Validation: every filter key must be present in the table's
  current schema (read via DuckDB ``DESCRIBE``); unknown keys
  return 400 with a list of allowed columns so the AI can correct.
* Limit: capped at ``MAX_LIMIT`` rows per call so a poorly-formed
  call can't smoke a large table.

The intention is that a stdio / SSE MCP server later surfaces one
FastMCP tool per ``table_registry`` row that proxies through here,
matching how passthrough tools surface today. That generator is a
small follow-up — it reads the catalog and dynamically registers
named tools. For now the REST endpoint is the source of truth.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.dependencies import _get_db, get_current_user
from src.audit_helpers import identity_for_audit, log_safe
from src.db import get_analytics_db_readonly
from src.rbac import can_access_table
from src.access_policy import (
    PolicyError,
    PolicyIdentityUnresolvable,
    assert_unique_output_columns,
    policied_from_sql,
    policied_relation,
)
from src.repositories import table_registry_repo
from src.sql_ident import quote_ident

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mcp/query-table", tags=["mcp-per-table"])


MAX_LIMIT = 1000


class TableQueryRequest(BaseModel):
    filter: Dict[str, Any] = Field(default_factory=dict)
    limit: int = 100


class TableQueryResponse(BaseModel):
    table_id: str
    rows: List[Dict[str, Any]]
    row_count: int
    columns: List[str]
    truncated: bool


def _column_names(analytics_conn: duckdb.DuckDBPyConnection, table_view_name: str) -> List[str]:
    """Best-effort column lookup for the view backing ``table_view_name``.

    Uses ``DESCRIBE`` which works on DuckDB views the orchestrator
    creates over attached extract.duckdb files. Returns an empty list
    on any error so the caller can 400 with a clear message rather
    than 500-ing on a missing view.
    """
    try:
        rows = analytics_conn.execute(f"DESCRIBE {quote_ident(table_view_name)}").fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _describe_columns(analytics_conn: duckdb.DuckDBPyConnection, from_sql: str, params: Dict[str, Any]) -> List[str]:
    """Effective columns for an ALREADY FROM-able SQL fragment (Task 8's
    ``policied_from_sql`` output) — unlike ``_column_names``, ``from_sql``
    here is a full parenthesized expression, not a bare identifier that
    still needs quoting.

    A masked (``EXCLUDE``d) column must never be revealed (§8) — not in a
    result row, and not in the "unknown filter column" 400's ``allowed``
    list either. Deriving the column list from a ``DESCRIBE`` of the
    policy-wrapped relation itself is the cheap way to get that right. Same
    best-effort/empty-list-on-error contract as ``_column_names``.

    Task 9 added ``src.access_policy.effective_schema`` as the canonical,
    reusable version of this same ``DESCRIBE``-diff idea — but it is a
    poor fit for THIS call site and was deliberately left unswapped:
    ``effective_schema`` opens its own ``get_analytics_db_readonly()``
    connection and re-resolves ``policied_relation`` internally, whereas
    ``query_table`` below already holds an open ``analytics_conn`` and an
    already-resolved ``relation`` — swapping would pay for a second
    connection open (re-ATTACHing every extract.duckdb file, the exact
    cost this module's own ``query_table`` docstring cites as the reason
    it stays a plain ``def``) and a second live-groups read on every call,
    for a column list this endpoint already knows how to derive from what
    it has in hand.
    """
    try:
        rows = analytics_conn.execute(f"DESCRIBE {from_sql}", params).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


@router.post("/{table_id}", response_model=TableQueryResponse)
def query_table(
    table_id: str,
    body: TableQueryRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> TableQueryResponse:
    """Run a constrained filter+limit query against a registered table.

    Pure SELECT — no aggregation, no projection control. AI clients
    that need richer queries should use the generic ``query(sql)``
    surface from the Agnes MCP foundation; this endpoint is the
    "fast path" for the common per-table lookup.

    Plain ``def`` (not ``async def``) so FastAPI auto-offloads the call to
    the anyio thread pool — see ``execute_query`` in ``app/api/query.py``
    for the same rationale. It didn't matter while this endpoint reused
    the pooled read-write singleton (a cheap cursor), but
    ``get_analytics_db_readonly()`` opens a fresh connection and
    re-ATTACHes every extract.duckdb file on each call; under ``async def``
    that synchronous I/O would hold the single uvicorn event loop and
    stall every other in-flight request for its duration.
    """
    if body.limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be > 0")
    limit = min(body.limit, MAX_LIMIT)
    truncated = body.limit > MAX_LIMIT

    # Registry lookup + RBAC. Internal tables (agnes_sessions /
    # agnes_telemetry / agnes_audit) hold no special status here any more:
    # they are members of the seeded `agnes-usage` data package and gate
    # through `can_access_table` like every other table.
    tables_repo = table_registry_repo()
    table = tables_repo.get(table_id)
    if table is None:
        raise HTTPException(status_code=404, detail="table_not_found")
    if not can_access_table(user, table_id, conn):
        raise HTTPException(status_code=403, detail=f"no grant on table {table_id!r}")

    # The orchestrator creates views in analytics.duckdb under the
    # table_registry id. Use the id rather than .name — the view
    # always exists under the id; .name is a UX label that may collide.
    view_name = table_id

    # Table access policies (§5): this endpoint's own base relation is
    # `view_name` on the already-open analytics connection, not the
    # analytics-catalog master view a policy body's `FROM <name>` names
    # when name != id — resolve first so the wrap below can bind the two
    # together. The inert (not policied) branch stays byte-identical to
    # the pre-existing code.
    try:
        relation = policied_relation(table_id, user)
    except PolicyIdentityUnresolvable:
        raise HTTPException(status_code=403, detail={"reason": "policy_identity_unresolvable"})
    except PolicyError as exc:
        raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})

    # A transient, per-call read-only connection — the same helper
    # `/api/query` and `/api/query/hybrid` use — not the process-wide
    # read-write singleton (`get_analytics_db()`). This endpoint used to
    # reuse that singleton to dodge DuckDB's "second connection, different
    # configuration" error when opening a read-only handle alongside an
    # open writer; the singleton then stayed open for the rest of the
    # process's life (nothing ever closed it), so any later call to
    # `get_analytics_db_readonly()` hit that exact same conflict and 500'd
    # — permanently, for every `/api/query` and `/api/query/hybrid` request
    # — the moment any authenticated user hit this endpoint once. Safety
    # against accidental mutation now comes from BOTH the read-only DuckDB
    # open (blocks CREATE/INSERT/UPDATE/DELETE at the engine level — the
    # backstop `/api/query` relies on behind its own SQL blocklist) and
    # this endpoint's own SQL builder: SELECT only, parameterized, plus
    # the column allow-list below.
    analytics_conn = get_analytics_db_readonly()
    try:
        if relation.policied:
            from_sql = policied_from_sql(relation, table_name=table["name"], source_sql=quote_ident(view_name))
            columns = _describe_columns(analytics_conn, from_sql, relation.params)
            # Read-path guard: duplicate output names (a masking policy that
            # re-derives a column `*` still emits) leak the plaintext copy
            # through the pandas `.to_dict` rename below. Fail closed.
            try:
                assert_unique_output_columns(columns, relation.table_id)
            except PolicyError as exc:
                raise HTTPException(status_code=500, detail={"reason": "policy_error", "table": exc.table_id})
        else:
            from_sql = quote_ident(view_name)
            columns = _column_names(analytics_conn, view_name)

        if not columns:
            raise HTTPException(
                status_code=409,
                detail=f"table view {view_name!r} is not present in analytics.duckdb (sync may not have run yet)",
            )

        unknown_keys = [k for k in body.filter.keys() if k not in columns]
        if unknown_keys:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "unknown_filter_columns",
                    "unknown": unknown_keys,
                    "allowed": columns,
                },
            )

        base_params = relation.params if relation.policied else None
        sql, params = _build_select(from_sql, body.filter, limit, base_params)
        rows = analytics_conn.execute(sql, params).fetchdf()
        # Coerce dataframe scalars to native Python types — fastapi's JSON
        # encoder doesn't know about numpy / pandas scalars.
        result_records = _coerce_records(rows.to_dict(orient="records"))

        # F2c (audit-full-coverage plan, Task 5): filter COLUMN NAMES only,
        # never the filter values — those may be PII/business-sensitive.
        user_id, _email = identity_for_audit(user)
        log_safe(
            user_id=user_id,
            action="query.table_scoped",
            resource=f"table:{table_id}",
            params={
                "row_count": len(result_records),
                "filter_columns": sorted(body.filter.keys()),
                "truncated": truncated,
            },
        )

        return TableQueryResponse(
            table_id=table_id,
            rows=result_records,
            row_count=len(result_records),
            columns=columns,
            truncated=truncated,
        )
    finally:
        analytics_conn.close()


def _build_select(
    from_sql: str,
    filter_dict: Dict[str, Any],
    limit: int,
    base_params: Optional[Dict[str, Any]] = None,
) -> tuple[str, Any]:
    """Build a parameterized SELECT * FROM <from_sql> WHERE col = ... LIMIT N.

    ``from_sql`` is an ALREADY FROM-able expression — a quoted view name
    (the pre-existing shape) or a policy-wrapped derived table (Task 8's
    ``policied_from_sql``); this function itself never quotes an
    identifier for the FROM target, only for filter COLUMN names.

    ``base_params`` distinguishes the two binding styles this endpoint
    needs. ``None`` (the default, and the only shape the not-policied call
    site ever passes) binds filter values positionally (``?``, a
    ``list``) — unchanged behavior for the inert case. A dict — even an
    empty one — means a policied relation's own named
    ``$user_email``/``$user_id``/``$user_groups`` parameters are already
    in play, so filter values are ALSO bound as named parameters, under
    synthetic ``__filter_N`` keys that can never collide with a policy
    variable name (or with each other, even if the caller's filter
    happens to target a column literally named ``user_groups``) — DuckDB
    1.5.2 refuses to mix positional and named parameters in one statement
    (verified empirically).
    """
    where_parts: List[str] = []
    params: Any
    if base_params is None:
        params = []
        for col, val in filter_dict.items():
            where_parts.append(f"{quote_ident(col)} = ?")
            params.append(val)
    else:
        params = dict(base_params)
        for i, (col, val) in enumerate(filter_dict.items()):
            key = f"__filter_{i}"
            where_parts.append(f"{quote_ident(col)} = ${key}")
            params[key] = val

    where_clause = " WHERE " + " AND ".join(where_parts) if where_parts else ""
    sql = f"SELECT * FROM {from_sql}{where_clause} LIMIT {int(limit)}"
    return sql, params


def _coerce_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert pandas / numpy scalar types to JSON-friendly Python natives.

    DuckDB returns timestamps as pd.Timestamp and ints as np.int64;
    pydantic + json both handle native datetime / int just fine but
    not the pandas wrappers. ISO-format timestamps, int() the numerics,
    leave everything else alone.
    """
    out: List[Dict[str, Any]] = []
    for rec in records:
        row: Dict[str, Any] = {}
        for k, v in rec.items():
            if v is None:
                row[k] = None
            elif hasattr(v, "isoformat"):  # datetime / pd.Timestamp
                row[k] = v.isoformat()
            elif hasattr(v, "item"):  # numpy scalar
                try:
                    row[k] = v.item()
                except Exception:
                    row[k] = str(v)
            else:
                row[k] = v
        out.append(row)
    return out
