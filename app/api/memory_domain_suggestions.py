"""Memory Domain Suggestions API (v55) — non-admin "Suggest a domain"
affordance + admin moderation queue.

Two routers split by audience:

  - ``POST /api/memory-domain-suggestions``           — any auth user creates
  - ``GET  /api/memory-domain-suggestions/mine``       — caller sees their own
  - ``GET  /api/admin/memory-domain-suggestions``      — admin queue
  - ``POST /api/admin/memory-domain-suggestions/{id}/approve`` — admin
  - ``POST /api/admin/memory-domain-suggestions/{id}/reject``  — admin

Approve creates the real ``memory_domains`` row, stamps the suggestion
with ``created_domain_id``, and audits ``memory_domain_suggestion.approve``.
Reject only updates the row status. Both write a row into ``audit_log``
so the admin Activity Center surfaces them.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.repositories import (
    audit_repo,
    memory_domain_suggestions_repo,
    memory_domains_repo,
)

logger = logging.getLogger(__name__)

public_router = APIRouter(
    prefix="/api/memory-domain-suggestions",
    tags=["memory-domain-suggestions"],
)
admin_router = APIRouter(
    prefix="/api/admin/memory-domain-suggestions",
    tags=["memory-domain-suggestions-admin"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class SuggestRequest(BaseModel):
    name: str
    description: Optional[str] = None
    rationale: Optional[str] = None


class ResolveRequest(BaseModel):
    # For approve: optional slug / metadata override; defaults derive from
    # the suggestion's ``name``. For reject: optional note shown to the
    # requester explaining why.
    slug: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None
    note: Optional[str] = None


def _slugify(name: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "domain"


def _serialize(r: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(r)
    for k in ("created_at", "resolved_at"):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


# ---------------------------------------------------------------------------
# Public endpoints — any authenticated user
# ---------------------------------------------------------------------------


@public_router.post("", status_code=201)
async def suggest_domain(
    payload: SuggestRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="name too long (max 100 chars)")
    repo = memory_domain_suggestions_repo()
    sid = repo.create(
        name=name,
        description=(payload.description or "").strip() or None,
        rationale=(payload.rationale or "").strip() or None,
        created_by=user.get("id"),
    )
    audit_repo().log(
        user_id=user.get("id") or "anonymous",
        action="memory_domain_suggestion.create",
        resource=f"memory_domain_suggestion:{sid}",
        params={"name": name},
        result="success",
        # client_kind intentionally omitted (F0 audit-context autofill, Task 1).
    )
    return {"id": sid}


@public_router.get("/mine")
async def list_my_suggestions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Caller's own suggestions — pending + resolved, newest first. Lets
    the requester see whether admin approved/rejected without exposing
    other users' submissions."""
    repo = memory_domain_suggestions_repo()
    rows = repo.list(created_by=user.get("id"))
    return {"items": [_serialize(r) for r in rows]}


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@admin_router.get("")
async def admin_list_suggestions(
    status: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin moderation queue. Default lists every status; pass
    ?status=pending to narrow to open suggestions."""
    repo = memory_domain_suggestions_repo()
    rows = repo.list(status=status)
    return [_serialize(r) for r in rows]


@admin_router.get("/count-pending")
async def admin_count_pending(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    return {"count": memory_domain_suggestions_repo().count_pending()}


@admin_router.post("/{sid}/approve")
async def approve_suggestion(
    sid: str,
    payload: ResolveRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Approve: create the real ``memory_domains`` row + mark suggestion
    resolved. If a domain with the proposed slug already exists, returns
    409 — admin should reject with a note pointing at the existing one."""
    sugg_repo = memory_domain_suggestions_repo()
    sugg = sugg_repo.get(sid)
    if not sugg:
        raise HTTPException(status_code=404, detail="suggestion_not_found")
    if sugg["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"already_resolved:{sugg['status']}",
        )
    dom_repo = memory_domains_repo()
    slug = (payload.slug or _slugify(sugg["name"])).strip()
    if dom_repo.exists_by_slug(slug):
        raise HTTPException(status_code=409, detail="slug_exists")
    try:
        new_id = dom_repo.create(
            name=sugg["name"],
            slug=slug,
            description=payload.description or sugg.get("description"),
            icon=payload.icon,
            color=payload.color,
            created_by=user.get("email") or user["id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    sugg_repo.resolve(
        sid,
        status="approved",
        resolved_by=user.get("id"),
        resolution_note=payload.note,
        created_domain_id=new_id,
    )
    audit_repo().log(
        user_id=user.get("id") or "system",
        action="memory_domain_suggestion.approve",
        resource=f"memory_domain_suggestion:{sid}",
        params={"created_domain_id": new_id, "slug": slug},
        result="success",
        # client_kind intentionally omitted (F0 audit-context autofill, Task 1).
    )
    return {"id": sid, "created_domain_id": new_id, "status": "approved"}


@admin_router.post("/{sid}/reject")
async def reject_suggestion(
    sid: str,
    payload: ResolveRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = memory_domain_suggestions_repo()
    sugg = repo.get(sid)
    if not sugg:
        raise HTTPException(status_code=404, detail="suggestion_not_found")
    if sugg["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"already_resolved:{sugg['status']}",
        )
    repo.resolve(
        sid,
        status="rejected",
        resolved_by=user.get("id"),
        resolution_note=payload.note,
    )
    audit_repo().log(
        user_id=user.get("id") or "system",
        action="memory_domain_suggestion.reject",
        resource=f"memory_domain_suggestion:{sid}",
        params={"note": payload.note},
        result="success",
        # client_kind intentionally omitted (F0 audit-context autofill, Task 1).
    )
    return {"id": sid, "status": "rejected"}
