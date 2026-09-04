"""Shared HTTP shaping for the empty-``policy_mapping`` refusal (S3, #1979 /
#2023 / #2147).

``src.access_policy.raise_if_policy_mapping_empty`` is the ONE domain-level
check that answers "does this policy body join a ``policy_mapping``
dependency with zero (or never-synced) rows" — it raises the bare
``PolicyMappingEmpty``, with no HTTP shape of its own (``src/`` never depends
on FastAPI). This module is the ONE place that turns it into the structured
``500 {"reason": "policy_mapping_empty", ...}`` response every read surface
that actually EXECUTES a policied relation returns, so the four call sites
below can never drift on the reason code, the field names, or the datetime
serialization:

- ``POST /api/query`` (``app/api/query.py::execute_query``) and its
  ``--from-query`` snapshot-materialize sibling
  (``run_remote_select_to_arrow``) — the original S3 wiring (#2023).
- ``GET /api/v2/sample/{table_id}`` (``app/api/v2_sample.py::build_sample``)
  — the local-parquet branch, the only one that resolves a POLICIED
  relation and actually reads it (the BigQuery and non-BQ-remote branches
  fail closed on ``relation.policied`` before this check would ever apply —
  see docs/table-access-policies.md's v1 limitations).
- ``POST /api/v2/scan`` (``app/api/v2_scan.py::run_scan``) — same local-
  parquet branch; the Databricks/BigQuery branches are refused earlier for
  the same reason.
- ``POST /api/mcp/query-table/{table_id}``
  (``app/api/mcp_per_table.py::query_table``).

Each call site already resolves a registry ``row`` (or has a
``PoliciedRelation`` whose ``.table_id`` is the normalized registry id) before
reaching this check, so the signature takes the two directly rather than
re-resolving anything — this function does no registry I/O of its own beyond
what ``raise_if_policy_mapping_empty`` itself performs against
``sync_state``/``table_registry``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from src.access_policy import PolicyMappingEmpty, raise_if_policy_mapping_empty


def jsonable_last_sync(last_sync: Any) -> str | None:
    """Serialize ``PolicyMappingEmpty.last_sync`` for an ``HTTPException``
    detail (PR #2023 review, finding 2 — moved here unchanged for #2147).

    ``fastapi.exception_handlers.http_exception_handler`` builds a plain
    Starlette ``JSONResponse`` from ``exc.detail`` — unlike a
    ``response_model`` return value, it never runs through
    ``jsonable_encoder``, so a raw ``datetime`` here would blow up
    ``json.dumps`` inside the response instead of reaching the caller as
    the structured error this whole check exists to produce. Naive inputs
    are assumed UTC (DuckDB's ``SET GLOBAL TimeZone='UTC'`` pin, see
    ``app/serialization.py``), matching how every other datetime this app
    returns is labeled.
    """
    if not isinstance(last_sync, datetime):
        return last_sync
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=UTC)
    return last_sync.isoformat()


def assert_no_empty_policy_mapping(*, table_id: str, row: dict | None) -> None:
    """Refuse a live read through ``table_id``'s policy when a
    ``policy_mapping`` dependency it joins has zero (or never-synced) rows,
    rather than let it silently return ``row_count: 0`` for everyone —
    indistinguishable from "you legitimately have no data"
    (docs/table-access-policies.md's "empty-mapping trap").

    A no-op when ``row`` carries no ``access_policy_sql`` — the common case,
    and the same short-circuit every call site already applied before this
    check existed for it. Callers pass the table's OWN registry row so its
    mandatory ``FROM <itself>`` is never mistaken for an empty mapping
    dependency (``raise_if_policy_mapping_empty``'s own
    ``table_id``/``table_name`` exclusion).
    """
    policy_sql = (row or {}).get("access_policy_sql")
    if not policy_sql:
        return
    try:
        raise_if_policy_mapping_empty(policy_sql, table_id=table_id, table_name=(row or {}).get("name"))
    except PolicyMappingEmpty as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "reason": "policy_mapping_empty",
                "table": table_id,
                "mapping_table": exc.mapping_table,
                "note": str(exc),
                "last_sync": jsonable_last_sync(exc.last_sync),
            },
        )
