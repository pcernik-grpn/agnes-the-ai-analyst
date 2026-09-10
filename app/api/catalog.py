"""Catalog endpoints — table profiles, metrics."""

import json
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.utils import get_data_dir as _get_data_dir
from src.rbac import can_access_table, get_accessible_tables

from src.repositories import (
    profile_repo,
    table_registry_repo,
)

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


class CatalogTableItem(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    source_type: Optional[str] = None
    sync_strategy: Optional[str] = None
    query_mode: str = "local"


class CatalogTablesResponse(BaseModel):
    tables: List[CatalogTableItem]
    count: int


def _profile_restriction(table_name: str, user: dict) -> Optional[dict]:
    """Table access policies (§11's "sharper leak"): a stored profile's
    min/max/sample_values/top_values are row CONTENT, computed from the
    physical table at sync/refresh time — independent of any policy
    attached to the row later. Neither ``get_table_profile`` nor
    ``refresh_profile`` filtered any of that before this task, so a
    policied table's per-column stats were fully visible to any caller who
    merely passed the table-level ``can_access_table`` check, the exact gap
    a policy exists to close.

    Returns the replacement payload when the stats must be withheld, or
    ``None`` when the caller should see the profile unchanged — no policy
    on this table (including a table absent from the registry entirely,
    e.g. the legacy ``profiles.json`` fallback below), OR an admin (§12's
    bypass, decided the same way ``policied_relation`` decides it
    everywhere else).

    Deliberately checked in two steps rather than one ``policied_relation``
    call: ``policied_relation`` raises ``PolicyError`` for "table not
    registered" and gives no way to tell that apart from "registered, but
    some other resolution problem" — and the former is the routine case for
    a name that only exists in ``profiles.json``, which must NOT be treated
    as restricted (this feature applies to registered tables only). Once a
    row with ``access_policy_sql`` is confirmed to exist, ``policied_relation``
    resolves it and any FURTHER failure (unresolvable identity, a live
    policy-execution problem) fails closed here exactly like every other
    policy surface (§17): withhold the stats rather than guess.
    """
    from src.repositories import table_registry_repo

    row = table_registry_repo().get(table_name) or table_registry_repo().get_by_name(table_name)
    if not row or not row.get("access_policy_sql"):
        return None

    from src.access_policy import PolicyError, PolicyIdentityUnresolvable, policied_relation

    try:
        relation = policied_relation(row["id"], user)
    except (PolicyIdentityUnresolvable, PolicyError):
        relation = None

    if relation is not None and not relation.policied:
        return None

    return {
        "table_id": row["id"],
        "policy_restricted": True,
        "message": (
            "this table has an access policy attached; per-column profile statistics "
            "(min/max/sample values/top values) are withheld for non-admin callers"
        ),
    }


@router.get("/profile/{table_name}")
def get_table_profile(
    table_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get profiler data for a specific table."""
    # Check table-level access
    if not can_access_table(user, table_name, conn):
        raise HTTPException(status_code=403, detail=f"Access denied to table '{table_name}'")
    repo = profile_repo()
    profile = repo.get(table_name)
    if not profile:
        # Fallback: try loading from profiles.json on disk
        profiles_path = _get_data_dir() / "src_data" / "metadata" / "profiles.json"
        if profiles_path.exists():
            try:
                all_profiles = json.loads(profiles_path.read_text())
                tables = all_profiles.get("tables", all_profiles)
                if table_name in tables:
                    return _profile_restriction(table_name, user) or tables[table_name]
            except Exception:
                pass
        raise HTTPException(status_code=404, detail=f"Profile not found for '{table_name}'")
    return _profile_restriction(table_name, user) or profile


@router.get("/tables", response_model=CatalogTablesResponse)
def list_catalog_tables(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all available tables from table_registry."""
    repo = table_registry_repo()
    all_tables = repo.list_all()

    # Resolve the accessible-table set ONCE per request (was: one
    # ``can_access_table`` call per row — an N+1 that serialized ~8-9
    # round-trips per table over ~115 tables). ``None`` means admin/all.
    _accessible_ids = get_accessible_tables(user, conn)
    _allowed = None if _accessible_ids is None else set(_accessible_ids)
    all_tables = [t for t in all_tables if _allowed is None or t["id"] in _allowed]

    tables = [
        {
            "id": t["id"],
            "name": t["name"],
            "description": t.get("description"),
            "source_type": t.get("source_type"),
            "sync_strategy": t.get("sync_strategy"),
            "query_mode": t.get("query_mode", "local"),
        }
        for t in all_tables
    ]
    return {"tables": tables, "count": len(tables)}


@router.get("/metrics/{metric_path:path}", deprecated=True)
def get_metric(
    metric_path: str,
    user: dict = Depends(get_current_user),
):
    """Deprecated: use GET /api/metrics/{metric_id} instead."""
    from fastapi.responses import RedirectResponse

    metric_id = metric_path.replace(".yml", "")
    return RedirectResponse(url=f"/api/metrics/{metric_id}", status_code=301)


@router.post("/profile/{table_name}/refresh")
def refresh_profile(
    table_name: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-generate profile for a table on demand."""
    # Check table-level access
    if not can_access_table(user, table_name, conn):
        raise HTTPException(status_code=403, detail=f"Access denied to table '{table_name}'")
    from app.utils import resolve_local_layout_target
    from src.profiler import profile_table, TableInfo

    # The single-file lookup goes through `resolve_local_parquet` rather than a
    # fourth hand-rolled `rglob(f"data/{name}.parquet")`. The name arrives on
    # the request path and was pasted straight into that pattern, so it was not
    # naming a table so much as searching for one: `*` matched whichever parquet
    # came first and stored ITS statistics under the requested name, and none of
    # the segment validation or realpath containment the resolvers gained
    # applied here (Devin Review on #1198). This endpoint resolves a target
    # before the registry is consulted, so it cannot lean on a row existing.
    #
    # A partitioned table is a DIRECTORY of per-period parquets, so the
    # single-file lookup finds nothing for one that is perfectly synced.
    # `profile_table` takes the directory as-is (it builds a recursive `**`
    # read expression from it), which is exactly what the scheduled profiling
    # run already passes — only this manual refresh could not reach it.
    #
    # `resolve_local_layout_target` rather than
    # `resolve_local_parquet(...) or resolve_local_partition_dir(...)`: that
    # hand-written expression was a third copy of the layout precedence (flat
    # always wins), so a table holding BOTH layouts (#1339) got profiled from
    # whichever one the read surfaces were not serving. The helper applies the
    # shared freshness comparator and, absent a collision, resolves exactly as
    # the expression did — including across sources.
    target = resolve_local_layout_target(table_name)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No parquet for '{table_name}'")

    try:
        table_info = TableInfo(name=table_name, table_id=table_name)
        profile = profile_table(table_info, target, [], {}, {})
        # Recompute + persist unconditionally — the STORED profile must stay
        # fresh for the admin/no-policy case (app/api/sync.py's own scheduled
        # run does the same, unfiltered, for the same reason). Only the
        # RESPONSE to THIS caller is gated.
        profile_repo().save(table_name, profile)
        restricted = _profile_restriction(table_name, user)
        if restricted is not None:
            return {"status": "ok", **restricted}
        return {"status": "ok", "table": table_name, "columns": len(profile.get("columns", {}))}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Profile failed: {e}")
