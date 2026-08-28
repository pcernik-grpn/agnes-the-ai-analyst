"""Observability page support endpoints.

Powers the unified /admin/activity page. The audit-log timeline itself is
served by app/api/activity.py — these endpoints add the bits that page
needs that didn't exist yet:

    GET    /api/admin/observability/facets   distinct (user, action, result, source) for filter dropdowns
    GET    /api/admin/observability/kpis     headline numbers for the top-bar cards
    GET    /api/admin/observability/views    list this user's saved views
    POST   /api/admin/observability/views    save / overwrite a view
    DELETE /api/admin/observability/views/{id}  delete one
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.auth.dependencies import _get_db

from src.repositories import (
    audit_repo,
    observability_views_repo,
    users_repo,
)

router = APIRouter(prefix="/api/admin/observability", tags=["observability"])


# ---------------------------------------------------------------------------
# Facets — distinct values for the filter dropdowns, scoped to the window
# ---------------------------------------------------------------------------

# Source classification lives in the repo layer now — one shared rule
# (src.audit_helpers.AUDIT_SOURCE_CASE_SQL, `action LIKE 'run_%'`), the same
# predicate last_scheduler_tick() uses. The previous hardcoded action list
# here had gone stale (it named four actions that no longer exist), which
# silently bucketed all scheduler ticks as 'other'.


def _window_since(since_minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=since_minutes)


@router.get("/facets")
def facets(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    user_id: Optional[str] = None,
    action_prefix: Optional[str] = None,
    resource_prefix: Optional[str] = None,
    result_pattern: Optional[str] = None,
    result_class: Optional[str] = None,
    q: Optional[str] = None,
    source: Optional[str] = None,
    trail: Optional[str] = Query(
        default=None,
        description="Narrow to one physical trail: audit | sync | llm | agent_scope. "
        "Unset covers the unified timeline across all four — same filter the timeline accepts.",
    ),
    include_self_reads: bool = Query(default=False),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the distinct facet values present across the unified timeline
    (audit_log + sync_history + llm_usage + agent_scope_snapshots) for the
    selected window, each with a count — under the SAME filters the
    timeline uses, so dropdown counts always describe rows the table can
    actually show. `include_self_reads` defaults to False: the Activity
    Center's own `activity.read` audit rows are hidden unless asked for.

    Counts are capped at 50 per facet (largest first). 50 is comfortable in
    a dropdown; tighter windows usually have <20 anyway.
    """
    since = _window_since(since_minutes)

    try:
        data = audit_repo().facets(
            since=since,
            user_id=user_id,
            action_prefix=action_prefix,
            resource_prefix=resource_prefix,
            result_pattern=result_pattern,
            result_class=result_class,
            q=q,
            source=source,
            trail=trail,
            include_self_reads=include_self_reads,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # The facets 'users' bucket carries ids + counts only; resolve readable
    # labels here, reproducing the old COALESCE(email, user_id).
    emails = users_repo().get_by_ids([u["id"] for u in data["users"]])

    return {
        "window_minutes": since_minutes,
        "users": [{"id": u["id"], "label": emails.get(u["id"]) or u["id"], "count": u["count"]} for u in data["users"]],
        "actions": data["actions"],
        "results": data["results"],
        "result_classes": data["result_classes"],
        "resources": data["resources"],
        "sources": data["sources"],
    }


# ---------------------------------------------------------------------------
# KPIs — headline numbers for the top-bar stats cards
# ---------------------------------------------------------------------------


@router.get("/kpis")
def kpis(
    since_minutes: int = Query(default=1440, ge=1, le=43200),
    user_id: Optional[str] = None,
    action_prefix: Optional[str] = None,
    resource_prefix: Optional[str] = None,
    result_pattern: Optional[str] = None,
    result_class: Optional[str] = None,
    q: Optional[str] = None,
    source: Optional[str] = None,
    trail: Optional[str] = Query(
        default=None,
        description="Narrow to one physical trail: audit | sync | llm | agent_scope. "
        "Unset covers the unified timeline across all four — same filter the timeline accepts.",
    ),
    include_self_reads: bool = Query(default=False),
    _user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """KPIs for the top-bar cards — same filter surface as the (unified)
    timeline, so the cards and the table can never tell different stories.
    `active_users` counts people (scheduler/system actors excluded);
    `errors` counts result_class='error'; `duration_coverage` says what
    fraction of rows carry a measured duration (feeds the p95 card's
    honesty sub-label)."""
    since = _window_since(since_minutes)

    try:
        k = audit_repo().kpis(
            since=since,
            user_id=user_id,
            action_prefix=action_prefix,
            resource_prefix=resource_prefix,
            result_pattern=result_pattern,
            result_class=result_class,
            q=q,
            source=source,
            trail=trail,
            include_self_reads=include_self_reads,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    total = k["events_total"]
    errors = k["errors"]
    p95 = k["p95"]

    rate = (errors / total) if total else 0.0
    return {
        "window_minutes": since_minutes,
        "events_total": int(total or 0),
        "active_users": int(k["active_users"] or 0),
        "errors": int(errors or 0),
        "error_rate": round(rate, 4),
        "p95_duration_ms": int(p95) if p95 is not None else None,
        "duration_coverage": k["duration_coverage"],
    }


# ---------------------------------------------------------------------------
# Saved views — per-user CRUD
# ---------------------------------------------------------------------------


@router.get("/views")
def list_views(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    return {"views": observability_views_repo().list_for_user(user_id)}


@router.post("/views")
def save_view(
    payload: dict[str, Any] = Body(...),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    name = (payload.get("name") or "").strip()
    query = payload.get("query")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if not isinstance(query, dict):
        raise HTTPException(status_code=400, detail="query must be an object")
    if len(name) > 80:
        raise HTTPException(status_code=400, detail="name too long (max 80 chars)")
    # Cap the saved-view payload so an admin can't bloat system.duckdb
    # with a malformed save. 64 KiB is generous for the saved-view shape
    # (window + a handful of short filter values + sort).
    import json as _json

    if len(_json.dumps(query)) > 64 * 1024:
        raise HTTPException(
            status_code=400,
            detail="query payload too large (max 64 KiB)",
        )
    # Per-user view-count cap — admin is the only role here, but a
    # runaway script shouldn't be able to fill system.duckdb with
    # thousands of views. 100 is well above any plausible curation
    # ceiling; ON CONFLICT updates an existing name rather than
    # adding rows, so this only bites genuine fan-out.
    # Routed through the factory so the cap reads the active backend (PG /
    # DuckDB) — a raw conn.execute here reads the frozen DuckDB file on a
    # Postgres instance, so the count would always be 0 and never cap.
    views_repo = observability_views_repo()
    existing = views_repo.count_for_user(user_id)
    already_exists = views_repo.name_exists(user_id, name)
    if existing >= 100 and not already_exists:
        raise HTTPException(
            status_code=400,
            detail="saved-view count for this user has reached 100; delete one before adding another",
        )
    return views_repo.create(user_id, name, query)


@router.delete("/views/{view_id}", status_code=204)
def delete_view(
    view_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    user_id = user.get("id") or ""
    ok = observability_views_repo().delete(user_id, view_id)
    if not ok:
        raise HTTPException(status_code=404, detail="view not found")
