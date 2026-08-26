"""Unified admin REST API for user_groups, members, and resource_grants.

Replaces ``app.api.role_management`` and ``app.api.plugin_access`` with a
single namespace under ``/api/admin``:

  - ``GET/POST/DELETE /api/admin/groups``
  - ``GET/POST/DELETE /api/admin/groups/{group_id}/members``
  - ``GET/POST/DELETE /api/admin/grants``
  - ``GET            /api/admin/resource-types``

Every endpoint is gated by ``require_admin``. Audit log entries are written
for every mutation so an admin's group/grant changes are traceable.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import exc as sa_exc

from app.auth.access import is_user_admin, require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType, list_resource_types
from src.repositories.user_groups import SystemGroupProtected

from src.repositories import (
    audit_repo,
    resource_grants_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["access"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[dict] = None,
) -> None:
    try:
        safe = None
        if params:
            safe = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in params.items()}
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params=safe)
    except Exception:
        # Audit failures must never break the mutation. Logged at WARN.
        logger.warning("audit log failed for %s/%s", action, resource)


def _is_google_managed(g: dict) -> bool:
    """Whether a group row is owned by Google sync — admin UI/API treat such
    rows as read-only.

    Two ways a group can be Google-managed:

    1. ``created_by='system:google-sync'`` — auto-created by the OAuth
       callback when the user belonged to a prefix-matching Workspace
       group; ``name`` is the full Workspace email.
    2. ``is_system=TRUE`` AND the group's name matches the env-configured
       admin/everyone Workspace email — the OAuth callback routes
       memberships from those Workspace groups into the seeded system
       row instead of creating a separate user_groups row, so the system
       row effectively *becomes* a Google-synced row in this deployment.
       Without the env mapping, system groups stay regular admin-managed
       rows (renaming Admin is still blocked separately by
       ``UserGroupsRepository`` for code-reference safety).
    """
    if (g.get("created_by") or "") == "system:google-sync":
        return True
    if g.get("is_system"):
        from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP

        admin_email = os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "").strip().lower()
        everyone_email = os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip().lower()
        if admin_email and g.get("name") == SYSTEM_ADMIN_GROUP:
            return True
        if everyone_email and g.get("name") == SYSTEM_EVERYONE_GROUP:
            return True
    return False


def _guard_google_managed(g: dict) -> None:
    """Raise 409 google_managed_readonly when the group is Google-managed."""
    if _is_google_managed(g):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "google_managed_readonly",
                "message": (
                    "This group is managed by Google Workspace and is "
                    "read-only here. Add or remove members via "
                    "admin.google.com, or sign in again to refresh."
                ),
            },
        )


def _validate_resource_type(value: str) -> ResourceType:
    try:
        return ResourceType(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=(f"Unknown resource_type {value!r}. Known types: {[rt.value for rt in ResourceType]}"),
        )


# ---------------------------------------------------------------------------
# Resource types (read-only, from Python enum)
# ---------------------------------------------------------------------------


@router.get("/resource-types", response_model=List[dict])
async def get_resource_types(
    user: dict = Depends(require_admin),
):
    """List the resource types defined in app.resource_types.

    No DB call — these come from the ``ResourceType`` StrEnum. The shape
    is ``[{key, display_name, description, id_format}]`` so the admin UI
    can render the create-grant form's resource_type dropdown plus a
    placeholder hint for the ``resource_id`` input.
    """
    return list_resource_types()


# ---------------------------------------------------------------------------
# Access overview — single-shot payload for the group Access tab
# ---------------------------------------------------------------------------


@router.get("/access-overview", response_model=dict)
async def access_overview(
    user: dict = Depends(require_admin),
):
    """One-shot snapshot for the group detail page's Access tab.

    Returns:
      - ``groups``: every user_group with member + grant counts
      - ``grants``: every (group_id, resource_type, resource_id) row
      - ``resources``: per-resource-type hierarchical layout, where each
        type has a list of *blocks* (parent entities, e.g. a marketplace)
        and each block has *items* (concrete grantable resources).

    UI stitches the three pieces into the two-column layout: groups on
    the left, resources tree on the right with per-item checkboxes whose
    state derives from ``grants``.
    """
    groups_rows = user_groups_repo().list_all()
    members_repo = user_group_members_repo()
    grants_repo = resource_grants_repo()

    groups = []
    for g in groups_rows:
        groups.append(
            {
                "id": g["id"],
                "name": g["name"],
                "description": g.get("description"),
                "is_system": bool(g.get("is_system", False)),
                "created_by": g.get("created_by"),
                # Same origin / google-management surface as `/api/admin/groups`
                # so the group list can render the identical pill +
                # subtitle treatment without a second source of truth.
                "origin": _derive_origin(g),
                "is_google_managed": _is_google_managed(g),
                "mapped_email": _mapped_email(g),
                # The workspace on /admin/access is the only group surface
                # now, so the snapshot has to carry what the retired list
                # table showed in its own column — including when the group
                # came into being. Same `str()` projection as the single-group
                # payload below, so both spell a timestamp the same way.
                "created_at": str(g["created_at"]) if g.get("created_at") else None,
                "member_count": members_repo.count_members(g["id"]),
                "grant_count": grants_repo.count_for_group(g["id"]),
            }
        )

    grants = [
        {
            "id": r["id"],
            "group_id": r["group_id"],
            "resource_type": r["resource_type"],
            "resource_id": r["resource_id"],
            # The tier belongs in the snapshot: the editor on /admin/access
            # renders an Optional/Automatic pair per grant off this payload,
            # and without the field every grant read back as Optional — a
            # grant saved as Automatic (here, or through the group drawer,
            # or by `agnes admin grant`) showed the wrong half lit until the
            # page was reloaded from a different endpoint. Same default the
            # single-grant response uses.
            "requirement": r.get("requirement") or "available",
        }
        for r in grants_repo.list_all()
    ]

    # Per-resource-type hierarchies. Driven by the registry in
    # app.resource_types — adding a new type there is the one place that
    # surfaces here, no extra wiring. Disabled types (none as of v19 — see
    # `is_resource_type_enabled`) are skipped so the admin UI does not render
    # a chip for grants the runtime cannot enforce yet.
    from app.resource_types import enabled_resource_types

    resources = [
        {
            "type_key": spec.key.value,
            "type_display": spec.display_name,
            "type_description": spec.description,
            "blocks": spec.list_blocks(),
        }
        for spec in enabled_resource_types()
    ]

    return {"groups": groups, "grants": grants, "resources": resources}


# ---------------------------------------------------------------------------
# User groups
# ---------------------------------------------------------------------------


class GroupResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    is_system: bool = False
    # 'system' | 'custom' | 'google_sync'. ``custom`` = created by an admin
    # via the UI/CLI (no system marker, no google-sync marker on
    # ``created_by``). Mapped Admin/Everyone (system row wired to a
    # Workspace group via AGNES_GROUP_{ADMIN,EVERYONE}_EMAIL) report
    # 'google_sync' here — Workspace is the authoritative source of
    # membership for those rows, so the chip should advertise that, not
    # the seed mechanism. Unmapped Admin/Everyone stay 'system'.
    origin: str = "custom"
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    member_count: int = 0
    grant_count: int = 0
    # True iff the row is owned by Google sync — admin UI hides edit/delete
    # affordances and the API rejects mutations with 409 google_managed_readonly.
    is_google_managed: bool = False
    # When the row is the seeded Admin / Everyone system group AND the
    # corresponding env-mapping is configured, this is the upstream
    # Workspace group email that funnels members in. The admin UI renders
    # it as a subtitle under the canonical name (`Admin / admins@...`)
    # so operators can see *which* Workspace group is wired to the system
    # row. Null for regular google_sync rows (their email is already in
    # `name`) and for unmapped system rows.
    mapped_email: Optional[str] = None


class CreateGroupRequest(BaseModel):
    name: str
    description: Optional[str] = None


class UpdateGroupRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


def _derive_origin(g: dict) -> str:
    """Project a 3-value origin tag from existing user_groups columns.

    - mapped via ``AGNES_GROUP_{ADMIN,EVERYONE}_EMAIL`` → 'google_sync'
      (the seed badge is suppressed when the row is wired to Workspace —
      Workspace is the authoritative source of membership)
    - ``is_system=TRUE`` (otherwise)                   → 'system'
    - ``created_by`` starts with 'system:google'       → 'google_sync'
    - other ``system:`` prefixed creator               → 'system'
    - else                                             → 'custom'
      (admin-created via UI/CLI — the value is named after the *origin*,
      not the creator's role, so it doesn't visually clash with the
      seeded `Admin` system row in the chip layer)
    """
    is_system = bool(g.get("is_system"))
    cb = g.get("created_by") or ""
    name = g.get("name") or ""
    if is_system:
        from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP

        admin_email = os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "").strip()
        everyone_email = os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip()
        if (admin_email and name == SYSTEM_ADMIN_GROUP) or (everyone_email and name == SYSTEM_EVERYONE_GROUP):
            return "google_sync"
        return "system"
    if cb.startswith("system:google"):
        return "google_sync"
    if cb.startswith("system:"):
        return "system"
    return "custom"


def _mapped_email(g: dict) -> Optional[str]:
    """The Workspace group email that funnels members into a system row.

    Only returns a value when the row is the seeded ``Admin`` / ``Everyone``
    system group AND the matching env var is configured. Null otherwise —
    regular google_sync rows already carry the email in ``name``, and
    unmapped system rows have nothing to show.
    """
    if not g.get("is_system"):
        return None
    from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP

    name = g.get("name")
    if name == SYSTEM_ADMIN_GROUP:
        v = os.environ.get("AGNES_GROUP_ADMIN_EMAIL", "").strip()
        return v or None
    if name == SYSTEM_EVERYONE_GROUP:
        v = os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip()
        return v or None
    return None


def _group_to_response(
    g: dict,
    members_repo,
    grants_repo,
) -> GroupResponse:
    return GroupResponse(
        id=g["id"],
        name=g["name"],
        description=g.get("description"),
        is_system=bool(g.get("is_system", False)),
        origin=_derive_origin(g),
        created_at=str(g["created_at"]) if g.get("created_at") else None,
        created_by=g.get("created_by"),
        member_count=members_repo.count_members(g["id"]),
        grant_count=grants_repo.count_for_group(g["id"]),
        is_google_managed=_is_google_managed(g),
        mapped_email=_mapped_email(g),
    )


@router.get("/groups", response_model=List[GroupResponse])
async def list_groups(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    groups = user_groups_repo().list_all()
    members_repo = user_group_members_repo()
    grants_repo = resource_grants_repo()
    return [_group_to_response(g, members_repo, grants_repo) for g in groups]


@router.get("/groups/{group_id}", response_model=GroupResponse)
async def get_group(
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Single-group payload for one group.

    Fed the retired ``/admin/groups/{id}`` header; still the endpoint the
    group drawer reads back after a create or rename.
    """
    g = user_groups_repo().get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    members_repo = user_group_members_repo()
    grants_repo = resource_grants_repo()
    return _group_to_response(g, members_repo, grants_repo)


@router.post("/groups", response_model=GroupResponse, status_code=201)
async def create_group(
    payload: CreateGroupRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    repo = user_groups_repo()
    if repo.get_by_name(name):
        raise HTTPException(status_code=409, detail=f"Group {name!r} already exists")
    g = repo.create(
        name=name,
        description=payload.description,
        created_by=user.get("email"),
    )
    _audit(
        conn,
        user["id"],
        "user_group.created",
        f"group:{g['id']}",
        {"name": name},
    )
    members_repo = user_group_members_repo()
    grants_repo = resource_grants_repo()
    return _group_to_response(g, members_repo, grants_repo)


@router.patch("/groups/{group_id}", response_model=GroupResponse)
async def update_group(
    group_id: str,
    payload: UpdateGroupRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = user_groups_repo()
    g = repo.get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    _guard_google_managed(g)
    if g.get("is_system") and payload.name is not None and payload.name.strip() != g["name"]:
        # System groups: block renames (the canonical names "Admin" /
        # "Everyone" are referenced from app.auth.access and the
        # marketplace filter), but description edits are cosmetic and
        # allowed (admins curate them in /admin/access). The repo
        # layer's narrowed guard (src/repositories/user_groups.py) is
        # the second line of defense.
        raise HTTPException(
            status_code=409,
            detail="System groups cannot be renamed",
        )
    updates: dict = {}
    if payload.name is not None and payload.name.strip() != g["name"]:
        updates["name"] = payload.name.strip()
    if payload.description is not None:
        updates["description"] = payload.description
    if updates:
        try:
            repo.update(group_id, **updates)
        except SystemGroupProtected:
            raise HTTPException(
                status_code=409,
                detail="System groups cannot be renamed",
            )
        _audit(conn, user["id"], "user_group.updated", f"group:{group_id}", updates)
    g = repo.get(group_id)
    members_repo = user_group_members_repo()
    grants_repo = resource_grants_repo()
    return _group_to_response(g, members_repo, grants_repo)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = user_groups_repo()
    g = repo.get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    _guard_google_managed(g)
    if g.get("is_system"):
        raise HTTPException(status_code=409, detail="Cannot delete a system group")
    # Cascade members + grants BEFORE the parent row, through the backend
    # factory — NOT raw conn.execute(). The _get_db conn is always DuckDB, so
    # on a Postgres deployment a raw-conn cascade no-ops against an empty DuckDB
    # while repo.delete() hits PG, leaving resource_grants.group_id /
    # user_group_members.group_id still referencing the about-to-be-deleted
    # group → FK violation → 500. Routing the cascade through the same factory
    # as repo.delete() keeps the children and the parent on the active backend.
    # Each repo call autocommits, so by the time the parent DELETE runs the
    # children are already gone and the v14 FK check passes (the same ordering
    # the pre-#430 DuckDB fix relied on — children before parent).
    try:
        user_group_members_repo().delete_all_for_group(group_id)
        resource_grants_repo().delete_all_for_group(group_id)
        repo.delete(group_id)
    except SystemGroupProtected:
        raise HTTPException(status_code=409, detail="Cannot delete a system group")
    _audit(
        conn,
        user["id"],
        "user_group.deleted",
        f"group:{group_id}",
        {"name": g["name"]},
    )


# ---------------------------------------------------------------------------
# Group members
# ---------------------------------------------------------------------------


class MemberResponse(BaseModel):
    user_id: str
    email: str
    name: Optional[str] = None
    active: bool = True
    source: str
    added_at: Optional[str] = None
    added_by: Optional[str] = None


class AddMemberRequest(BaseModel):
    email: str


@router.get("/groups/{group_id}/members", response_model=List[MemberResponse])
async def list_members(
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if not user_groups_repo().get(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    rows = user_group_members_repo().list_members_for_group(group_id)
    return [
        MemberResponse(
            user_id=r["id"],
            email=r["email"],
            name=r.get("name"),
            active=bool(r.get("active", True)),
            source=r["source"],
            added_at=str(r["added_at"]) if r.get("added_at") else None,
            added_by=r.get("added_by"),
        )
        for r in rows
    ]


@router.post("/groups/{group_id}/members", response_model=MemberResponse, status_code=201)
async def add_member(
    group_id: str,
    payload: AddMemberRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    g = user_groups_repo().get(group_id)
    if not g:
        raise HTTPException(status_code=404, detail="Group not found")
    _guard_google_managed(g)
    # Case-insensitive: an operator typing the address the way a person
    # writes it must reach the account an OAuth claim created.
    target = users_repo().get_by_email_ci((payload.email or "").strip())
    if not target:
        raise HTTPException(status_code=404, detail=f"User {payload.email!r} not found")
    members = user_group_members_repo()
    if members.has_membership(target["id"], group_id):
        raise HTTPException(status_code=409, detail="User already a member")
    members.add_member(
        user_id=target["id"],
        group_id=group_id,
        source="admin",
        added_by=user.get("email"),
    )
    _audit(
        conn,
        user["id"],
        "user_group.member_added",
        f"group:{group_id}",
        {"user_email": target["email"]},
    )
    return MemberResponse(
        user_id=target["id"],
        email=target["email"],
        name=target.get("name"),
        active=bool(target.get("active", True)),
        source="admin",
        added_at=None,
        added_by=user.get("email"),
    )


@router.delete("/groups/{group_id}/members/{user_id}", status_code=204)
async def remove_member(
    group_id: str,
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    members = user_group_members_repo()
    # Last-admin guard: refuse to remove anyone from the seeded Admin group
    # when they are the only active admin — recovery from zero admins
    # requires direct DB access. Same protection as delete_user / update_user
    # (active=False) in app/api/users.py.
    group = user_groups_repo().get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    _guard_google_managed(group)
    if group["name"] == "Admin" and is_user_admin(user_id, conn) and users_repo().count_admins(active_only=True) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot remove the last admin — at least one user must remain in the Admin group",
        )
    # Only delete admin-source rows from this endpoint. Google-sync rows
    # rebuild themselves on next login; system_seed rows survive deploys.
    removed = members.remove_member(user_id, group_id, require_source="admin")
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="No admin-managed membership for this user in this group",
        )
    _audit(
        conn,
        user["id"],
        "user_group.member_removed",
        f"group:{group_id}",
        {"user_id": user_id},
    )


# ---------------------------------------------------------------------------
# Resource grants
# ---------------------------------------------------------------------------


class GrantResponse(BaseModel):
    id: str
    group_id: str
    group_name: str
    resource_type: str
    resource_id: str
    assigned_at: Optional[str] = None
    assigned_by: Optional[str] = None
    # v49: 'available' | 'required' — Required tier is in-stack by default
    # for every group member without an explicit subscription.
    requirement: str = "available"


class CreateGrantRequest(BaseModel):
    group_id: str
    resource_type: str
    resource_id: str
    # v49 added the ``requirement`` enum on ``resource_grants``; the POST
    # endpoint must accept it so clients can create a grant at the
    # ``required`` tier in one round-trip. Without this, /admin/access
    # + the inline RBAC matrices (Edit Data Package / Edit Memory Domain
    # / Edit Recipe) silently fell through to the column default
    # (``available``), and a re-open of the same modal showed the
    # admin's "required" pick as "available" — looks like the save
    # silently failed. Default kept at None so callers that don't
    # explicitly pass a value still land at DB's column default.
    requirement: Optional[str] = None


def _grant_to_response(g: dict) -> GrantResponse:
    return GrantResponse(
        id=g["id"],
        group_id=g["group_id"],
        group_name=g.get("group_name", ""),
        resource_type=g["resource_type"],
        resource_id=g["resource_id"],
        assigned_at=str(g["assigned_at"]) if g.get("assigned_at") else None,
        assigned_by=g.get("assigned_by"),
        requirement=g.get("requirement") or "available",
    )


@router.get("/grants", response_model=List[GrantResponse])
async def list_grants(
    resource_type: Optional[str] = None,
    group_id: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if resource_type:
        _validate_resource_type(resource_type)
    rows = resource_grants_repo().list_all(
        resource_type=resource_type,
        group_id=group_id,
    )
    return [_grant_to_response(r) for r in rows]


@router.post("/grants", response_model=GrantResponse, status_code=201)
async def create_grant(
    payload: CreateGrantRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    rt = _validate_resource_type(payload.resource_type)
    # Feature gate: refuse to mint grants for a resource type disabled by
    # `is_resource_type_enabled` (all types are enabled unconditionally as of
    # v19 — this exists for a future type whose runtime enforcement isn't
    # wired up yet). Listing + deleting existing rows still works so
    # operators can clean up legacy data.
    from app.resource_types import is_resource_type_enabled

    if not is_resource_type_enabled(rt):
        raise HTTPException(
            status_code=422,
            detail=f"resource_type {rt.value!r} is not currently enabled.",
        )
    if not payload.resource_id.strip():
        raise HTTPException(status_code=400, detail="resource_id is required")
    if not user_groups_repo().get(payload.group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    grants = resource_grants_repo()
    # v49 ``requirement`` is part of the create-grant contract. Validate
    # the enum here so the 422 message matches the endpoint contract
    # rather than leaking a ValueError from the repo layer.
    if payload.requirement is not None and payload.requirement not in (
        "available",
        "required",
    ):
        raise HTTPException(
            status_code=422,
            detail="requirement must be 'available' or 'required'",
        )
    if payload.requirement == "required":
        _reject_required_on_user_published(rt, payload.resource_id)
    try:
        grant_id = grants.create(
            group_id=payload.group_id,
            resource_type=rt.value,
            resource_id=payload.resource_id,
            assigned_by=user.get("email"),
            requirement=payload.requirement,
        )
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        # Both backends must surface a duplicate as the same 409 — on
        # Postgres the unique violation arrives as SQLAlchemy IntegrityError,
        # which previously fell through to a 500 and made batch-granting
        # clients (e.g. the /admin/linked-apps wizard) misreport an
        # already-granted row as a failure (Devin Review on #1116).
        raise HTTPException(
            status_code=409,
            detail="Grant already exists for this group/resource_type/resource_id",
        )
    if payload.requirement == "required":
        _fanout_required_store_entity(rt, payload.group_id, payload.resource_id)
    _audit(
        conn,
        user["id"],
        "resource_grant.created",
        f"grant:{grant_id}",
        {
            "group_id": payload.group_id,
            "resource_type": rt.value,
            "resource_id": payload.resource_id,
        },
    )
    # Re-read with the group name joined for the response.
    rows = grants.list_all()
    fresh = next((r for r in rows if r["id"] == grant_id), None)
    if not fresh:
        raise HTTPException(status_code=500, detail="Grant created but lookup failed")
    return _grant_to_response(fresh)


def _fanout_required_store_entity(rt: ResourceType, group_id: str, resource_id: str) -> None:
    """Materialize installs for a newly-Required store entity.

    Curated plugins inherit "always in the stack" from ``user_plugin_optouts``
    presence semantics, but a store entity is served per-person out of
    ``user_store_installs`` — without this the lock would render on a card the
    user does not actually have. Members who join the group later are picked up
    by the resolve-time top-up (see ``required_store_entity_ids``).
    """
    if rt is not ResourceType.STORE_ENTITY:
        return
    from src.repositories import user_store_installs_repo

    user_store_installs_repo().install_for_group_members(group_id, resource_id)


def _reject_required_on_user_published(rt: ResourceType, resource_id: str) -> None:
    """ "In stack, locked" is admissible only on organization-published items.

    Requiring something means every member of the group must carry it and
    cannot remove it. That is an institutional commitment, so the organization
    has to be the publisher standing behind it — an admin cannot conscript a
    colleague's personal upload into everyone's stack.

    Other resource types are unaffected: curated ``marketplace_plugin`` rows are
    organization-published by construction (an admin registered the marketplace),
    and the data types have no publisher axis at all.
    """
    if rt is not ResourceType.STORE_ENTITY:
        return
    from src.repositories import store_entities_repo

    entity = store_entities_repo().get(resource_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Store entity not found")
    if (entity.get("publisher_kind") or "user") != "organization":
        raise HTTPException(
            status_code=422,
            detail=(
                "requirement 'required' needs an organization-published item — publish it as the organization first"
            ),
        )


class UpdateGrantRequirementRequest(BaseModel):
    requirement: str  # 'available' | 'required'


@router.put("/grants/{grant_id}", response_model=GrantResponse)
async def update_grant_requirement(
    grant_id: str,
    payload: UpdateGrantRequirementRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Update the ``requirement`` enum on an existing grant.

    The ``required → available`` downgrade path depends on the stack
    membership mode (``features.stack_auto_membership``, spec
    2026-08-07-default-chrome-ux-parity; flipped to default-on in Wave 0,
    2026-08). Auto-membership (the default): both ``required`` and
    ``available`` are automatically in every granted user's stack
    (``StackResolver.stack``), so the downgrade only lifts the "always
    downloaded locally" guarantee and no row is written. Classic (explicit
    opt-out via ``features.stack_auto_membership: false``): membership is
    the subscribe model, so the downgrade eagerly fans out
    ``user_stack_subscriptions`` rows to the group's members — the
    pre-redesign v49 behavior — or they would silently lose the resource.

    ``marketplace_plugin`` grants are the one remaining exception: plugin
    visibility is resolved by ``resolve_user_marketplace`` off
    ``user_plugin_optouts``, a separate (opt-out, not opt-in) mechanism
    outside the StackResolver's auto-membership — so a ``required →
    available`` downgrade there still fans out eagerly to keep every group
    member's served set from silently shrinking.
    """
    if payload.requirement not in ("available", "required"):
        raise HTTPException(
            status_code=400,
            detail="requirement must be 'available' or 'required'",
        )
    grants = resource_grants_repo()
    existing = grants.get(grant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Grant not found")
    if payload.requirement == "required":
        _reject_required_on_user_published(
            _validate_resource_type(existing["resource_type"]),
            existing["resource_id"],
        )

    prior = grants.update_requirement(grant_id, payload.requirement)
    if payload.requirement == "required":
        _fanout_required_store_entity(
            _validate_resource_type(existing["resource_type"]),
            existing["group_id"],
            existing["resource_id"],
        )
    # marketplace_plugin soft-downgrade: required → available still needs the
    # eager opt-out fan-out (see docstring) — data_package/memory_domain do
    # not, since auto-membership already keeps them in every granted user's
    # stack regardless of requirement tier.
    if prior == "required" and payload.requirement == "available" and existing["resource_type"] == "marketplace_plugin":
        # Plugin subscriptions live in ``user_plugin_optouts`` (read by
        # ``resolve_user_marketplace``), not ``user_stack_subscriptions``
        # — fanning out to the stack table would let every group
        # member's served set silently shrink on downgrade.
        # resource_id format is ``<marketplace_slug>/<plugin_name>``.
        from src.repositories import user_curated_subscriptions_repo

        slug, _, plugin_name = existing["resource_id"].partition("/")
        if plugin_name:
            user_curated_subscriptions_repo().subscribe_group_members(
                existing["group_id"],
                slug,
                plugin_name,
            )
    elif prior == "required" and payload.requirement == "available":
        # Classic stack mode (spec 2026-08-07-default-chrome-ux-parity,
        # explicit opt-out via features.stack_auto_membership=false):
        # membership is the subscribe model, so a required → available
        # downgrade MUST eagerly materialize subscriptions for the group's
        # members — the pre-redesign v49 fan-out verbatim — or every member
        # silently loses the resource from their stack. Under
        # auto-membership (the default) the grant itself keeps the resource
        # in every member's stack, so no row needs writing.
        from app.instance_config import get_stack_auto_membership

        if not get_stack_auto_membership():
            from src.repositories import user_stack_subscriptions_repo

            user_stack_subscriptions_repo().subscribe_group_members(
                existing["group_id"],
                existing["resource_type"],
                existing["resource_id"],
            )

    _audit(
        conn,
        user["id"],
        "resource_grant.requirement_updated",
        f"grant:{grant_id}",
        {
            "prior": prior,
            "new": payload.requirement,
            "resource_type": existing["resource_type"],
            "resource_id": existing["resource_id"],
            "group_id": existing["group_id"],
        },
    )

    # Re-read with the group name joined for the response.
    rows = grants.list_all()
    fresh = next((r for r in rows if r["id"] == grant_id), None)
    if not fresh:
        raise HTTPException(status_code=500, detail="Grant updated but lookup failed")
    return _grant_to_response(fresh)


@router.delete("/grants/{grant_id}", status_code=204)
async def delete_grant(
    grant_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    grants = resource_grants_repo()
    existing = grants.get(grant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Grant not found")

    # v39: refuse to revoke a grant whose underlying plugin is system-marked.
    # The mark_system endpoint materializes per-group rows precisely so the
    # plugin reaches every user; allowing per-group revoke here would punch
    # a hole in the mandatory tier silently. Admin must unmark on
    # /admin/marketplaces first, then revoke individual groups.
    if existing["resource_type"] == "marketplace_plugin":
        rid = existing["resource_id"] or ""
        if "/" in rid:
            mp_id, plugin_name = rid.split("/", 1)
            from src.repositories import marketplace_plugins_repo

            plugin_rows = marketplace_plugins_repo().list_for_marketplace(mp_id)
            sys_plugin = next(
                (p for p in plugin_rows if p["name"] == plugin_name and p.get("is_system")),
                None,
            )
            if sys_plugin is not None:
                raise HTTPException(
                    status_code=409,
                    detail="cannot_revoke_system_grant",
                )

    grants.delete(grant_id)

    # v24: re-grant of the same plugin must reset every user to the default
    # (subscribed). Drop matching subscriptions at the same time we drop the
    # grant so state stays consistent — see
    # src/repositories/user_curated_subscriptions.py.
    optouts_dropped = 0
    if existing["resource_type"] == "marketplace_plugin":
        rid = existing["resource_id"] or ""
        if "/" in rid:
            mp_id, plugin_name = rid.split("/", 1)
            from src.repositories import user_curated_subscriptions_repo

            optouts_dropped = user_curated_subscriptions_repo().delete_for_plugin(
                mp_id,
                plugin_name,
            )
        try:
            from app.marketplace_server import packager

            packager.invalidate_etag_cache()
        except Exception:
            pass

    _audit(
        conn,
        user["id"],
        "resource_grant.deleted",
        f"grant:{grant_id}",
        {
            "group_id": existing["group_id"],
            "resource_type": existing["resource_type"],
            "resource_id": existing["resource_id"],
            "optouts_dropped": optouts_dropped,
        },
    )


# ---------------------------------------------------------------------------
# User-centric views — back the /admin/users/{id} detail page.
# ---------------------------------------------------------------------------


class UserMembershipResponse(BaseModel):
    group_id: str
    group_name: str
    is_system: bool = False
    # 'system' | 'custom' | 'google_sync' — same shared helper as
    # /api/admin/groups + /api/users so the user detail page colors the
    # membership chips identically to the user list and the groups page.
    origin: str = "custom"
    source: str
    added_at: Optional[str] = None
    added_by: Optional[str] = None


class AddUserToGroupRequest(BaseModel):
    group_id: str


@router.get(
    "/users/{user_id}/memberships",
    response_model=List[UserMembershipResponse],
)
async def list_user_memberships(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Groups a user belongs to, joined with group metadata for display.

    Includes ``source`` so the UI can distinguish admin-managed memberships
    (deletable from this page) from Google-synced or system-seeded ones
    (read-only — managed by their own writer).
    """
    if not users_repo().get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    rows = user_group_members_repo().list_groups_with_meta_for_user(user_id)
    return [
        UserMembershipResponse(
            group_id=r["group_id"],
            group_name=r["name"],
            is_system=bool(r["is_system"]),
            origin=_derive_origin(
                {"is_system": bool(r["is_system"]), "name": r["name"], "created_by": r["created_by"]}
            ),
            source=r["source"],
            added_at=None,
            added_by=None,
        )
        for r in rows
    ]


@router.post(
    "/users/{user_id}/memberships",
    response_model=UserMembershipResponse,
    status_code=201,
)
async def add_user_to_group(
    user_id: str,
    payload: AddUserToGroupRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Add a user to a group from the user-centric page.

    Mirror of POST /api/admin/groups/{id}/members but keyed on the user.
    Always writes ``source='admin'`` so the row survives Google sync.
    """
    if not users_repo().get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    group = user_groups_repo().get(payload.group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    _guard_google_managed(group)
    members = user_group_members_repo()
    if members.has_membership(user_id, payload.group_id):
        raise HTTPException(status_code=409, detail="Already a member")
    members.add_member(
        user_id=user_id,
        group_id=payload.group_id,
        source="admin",
        added_by=user.get("email"),
    )
    _audit(
        conn,
        user["id"],
        "user_group.member_added",
        f"user:{user_id}",
        {"group_id": payload.group_id, "group_name": group["name"]},
    )
    return UserMembershipResponse(
        group_id=payload.group_id,
        group_name=group["name"],
        is_system=bool(group.get("is_system", False)),
        origin=_derive_origin(
            {
                "is_system": bool(group.get("is_system", False)),
                "name": group["name"],
                "created_by": group.get("created_by"),
            }
        ),
        source="admin",
        added_at=None,
        added_by=user.get("email"),
    )


@router.delete(
    "/users/{user_id}/memberships/{group_id}",
    status_code=204,
)
async def remove_user_from_group(
    user_id: str,
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove a user from a group from the user-centric page.

    Only deletes admin-source rows (Google-sync / system-seed managed
    elsewhere). Last-admin guard: refuse to remove anyone from Admin
    when they are the only active admin — recovery from zero admins
    requires direct DB access.
    """
    group = user_groups_repo().get(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    _guard_google_managed(group)
    if group["name"] == "Admin" and is_user_admin(user_id, conn) and users_repo().count_admins(active_only=True) <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot remove the last admin — at least one user must remain in the Admin group",
        )
    members = user_group_members_repo()
    removed = members.remove_member(user_id, group_id, require_source="admin")
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="No admin-managed membership for this user in this group",
        )
    _audit(
        conn,
        user["id"],
        "user_group.member_removed",
        f"user:{user_id}",
        {"group_id": group_id, "group_name": group["name"]},
    )


class EffectiveAccessItem(BaseModel):
    resource_type: str
    resource_id: str
    via_groups: List[dict]  # [{group_id, group_name}]


class TablePolicyDiagnosis(BaseModel):
    """Per-table access-policy self-audit (table access policies design
    doc §10.2, §16). Always present on every table entry, never omitted —
    a non-policied table reports ``applies=False`` rather than dropping the
    key, so a caller can rely on one shape instead of branching on key
    presence (documented choice; the design doc leaves this pick open).

    ``applies`` is a STRUCTURAL fact about the table — it carries an
    ``access_policy_sql`` — independent of whether THIS persona is actually
    filtered by it: an admin-bypass target (§12) still reports
    ``applies=True`` for a policied table, with ``rows_visible`` showing
    the unfiltered count instead (exactly what a live read with that
    identity would return).

    ``note`` is additional human-readable context beyond the fixed
    ``reason`` enum — e.g. why ``rows_visible`` is ``null`` despite
    ``reason='ok'`` (a remote/BigQuery table, counted-skipped to avoid a
    live scan), or which mapping table + ``last_sync`` produced
    ``reason='mapping_empty'``. ``None`` when the reason is self-explanatory.
    """

    applies: bool
    rows_visible: Optional[int] = None
    reason: str  # 'ok' | 'empty_slice' | 'mapping_empty' | 'policy_error' | 'identity_unresolvable'
    note: Optional[str] = None


class TableAccessDiagnosis(BaseModel):
    table_id: str
    policy: TablePolicyDiagnosis


class EffectiveAccessResponse(BaseModel):
    is_admin: bool
    items: List[EffectiveAccessItem]
    # §10.2 — one entry per table this principal can actually read, in the
    # STACK-GATED sense `src.rbac.get_accessible_tables` already uses for
    # authorization — NOT the legacy per-table `resource_grants` rows
    # `items` above carries (those are a no-op for analyst visibility, see
    # `can_access_table`'s own docstring, and for a typical analyst whose
    # access flows entirely through a data-package stack `items` would
    # carry zero `resource_type='table'` rows at all). Additive: `items`
    # keeps its existing shape and meaning untouched.
    tables: List[TableAccessDiagnosis] = []


def _table_access_diagnoses(principal: dict, conn: duckdb.DuckDBPyConnection) -> List[TableAccessDiagnosis]:
    """§10.2 — one :class:`TableAccessDiagnosis` per table ``principal`` can
    actually read. Internal tables (``agnes_sessions`` / ``_telemetry`` /
    ``_audit``) have their own row-level RBAC and no ``table_registry`` row
    at all — they never carry an access policy, so they are skipped rather
    than reported with a trivially-false ``applies``.
    """
    from src.rbac import get_accessible_tables
    from src.repositories import table_registry_repo

    accessible = get_accessible_tables(principal, conn)
    repo = table_registry_repo()
    if accessible is None:
        rows = repo.list_all()
    else:
        rows = []
        for table_id in accessible:
            row = repo.get(table_id)
            if row:
                rows.append(row)

    return [
        TableAccessDiagnosis(table_id=row["id"], policy=TablePolicyDiagnosis(**_table_policy_diagnosis(row, principal)))
        for row in sorted(rows, key=lambda r: r["id"])
    ]


def _table_policy_diagnosis(row: dict, principal: dict) -> dict:
    """The ``policy`` block for one ``table_registry`` row (§10.2)."""
    policy_sql = row.get("access_policy_sql")
    if not policy_sql:
        return {"applies": False, "rows_visible": None, "reason": "ok", "note": None}

    from src.access_policy import PolicyError, PolicyIdentityUnresolvable, PolicyMappingEmpty, policied_relation

    table_id = row["id"]
    try:
        relation = policied_relation(table_id, principal)
    except PolicyIdentityUnresolvable as exc:
        return {"applies": True, "rows_visible": None, "reason": "identity_unresolvable", "note": str(exc)}
    except PolicyError:
        return _policy_error_diagnosis(table_id, stage="resolve")

    # §15.1 — an empty (or never-synced) policy_mapping dependency only
    # matters for a persona actually reading THROUGH the policy; the admin
    # bypass (§12) reads unfiltered, so it is irrelevant to what they see.
    if relation.policied:
        try:
            _raise_if_policy_mapping_empty(policy_sql)
        except PolicyMappingEmpty as exc:
            return {"applies": True, "rows_visible": None, "reason": "mapping_empty", "note": str(exc)}

    if (row.get("query_mode") or "local") == "remote":
        # A live COUNT(*) through the policy would run a real BigQuery
        # scan on every effective-access page load — the same cost
        # `agnes-conventions/command-ux.md` already guards against
        # elsewhere. Local/materialized/server_only tables get a real
        # count below; a remote one gets an explicit skip note instead of
        # a silent (and possibly misleading) `rows_visible: 0`.
        return {
            "applies": True,
            "rows_visible": None,
            "reason": "ok",
            "note": "row count skipped for a remote (BigQuery) table to avoid a live scan",
        }

    try:
        rows_visible = _count_through_relation(relation)
    except PolicyError:
        return _policy_error_diagnosis(table_id, stage="execute")

    reason = "empty_slice" if (relation.policied and rows_visible == 0) else "ok"
    return {"applies": True, "rows_visible": rows_visible, "reason": reason, "note": None}


def _policy_error_diagnosis(table_id: str, *, stage: str) -> dict:
    return {
        "applies": True,
        "rows_visible": None,
        "reason": "policy_error",
        "note": f"access policy for table {table_id!r} failed to {stage}",
    }


def _raise_if_policy_mapping_empty(policy_sql: str) -> None:
    """§15.1 — fail closed with a NAMED reason when a ``policy_mapping``
    table this policy body references currently has zero (or never-synced)
    rows, rather than let a broken upstream sync read as "you legitimately
    have no data" via a bare zero count.

    Cheap by design: reads ``sync_state`` — the row count already recorded
    by the last successful sync — rather than a live ``COUNT(*)`` against
    every mapping dependency. This runs for every policied+accessible table
    on every effective-access call, and "the last sync landed empty (or
    never ran)" is exactly what ``sync_state`` already tracks (and gives
    ``last_sync`` for free, per §16's "must say" column).
    """
    import sqlglot
    from sqlglot import exp

    from src.access_policy import PolicyMappingEmpty
    from src.repositories import sync_state_repo, table_registry_repo

    try:
        statement = sqlglot.parse_one(policy_sql, read="duckdb")
    except Exception:
        # policied_relation() already parsed this SQL successfully to reach
        # this point (or raised PolicyError, handled by the caller before
        # this runs) — defensive no-op only, never a NEW failure mode.
        return
    referenced_names = {t.name for t in statement.find_all(exp.Table) if t.name}
    if not referenced_names:
        return

    mapping_rows = [
        r for r in table_registry_repo().list_all() if r.get("policy_mapping") and r.get("name") in referenced_names
    ]
    for mapping_row in mapping_rows:
        state = sync_state_repo().get_table_state(mapping_row["id"])
        rows = state.get("rows") if state else None
        if not rows:
            raise PolicyMappingEmpty(mapping_row["name"], state.get("last_sync") if state else None)


def _count_through_relation(relation) -> int:
    """Live ``COUNT(*)`` through a resolved ``PoliciedRelation`` (§10.2) —
    the same pattern ``app/api/admin.py``'s policy-preview endpoint and
    ``src.access_policy.effective_schema`` already use: wrap the relation's
    own SELECT in an outer COUNT, bound with its own params, against the
    analytics connection every registered table's master view lives on.
    """
    from src.access_policy import PolicyError
    from src.db import get_analytics_db_readonly

    conn = get_analytics_db_readonly()
    try:
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM ({relation.relation_sql}) AS __agnes_effective_access_count__",
                relation.params,
            ).fetchone()[0]
        except Exception as exc:
            raise PolicyError(relation.table_id) from exc
    finally:
        conn.close()


@router.get(
    "/users/{user_id}/effective-access",
    response_model=EffectiveAccessResponse,
)
async def user_effective_access(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List resources the user effectively has access to, with which group
    grants each one. ``is_admin`` reflects the real Admin-group check but
    no longer short-circuits the response — admins get the same explicit
    grant breakdown as everyone else, so the admin viewing a target user
    can see precisely what's been granted via which group rather than a
    flat "Full access" pill that hides the wiring.

    Note: actual authorization at runtime still gives Admin-group members
    god-mode (see ``app.auth.access.is_user_admin``); this endpoint is a
    debugging/audit view of the explicit grant graph, not the enforcement
    surface. ``tables`` (§10.2) is the exception — it reports what the
    TARGET user ``user_id`` (not the calling admin) actually sees through
    any access policy, since that is precisely the "X says Agnes shows her
    nothing" question this field exists to answer in one page load.
    """
    target = users_repo().get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    target_principal = {"id": target["id"], "email": target.get("email")}
    tables = _table_access_diagnoses(target_principal, conn)

    # Compose the effective-access view from the factory-backed repos so the
    # endpoint stays backend-agnostic. Per-row JOIN isn't necessary — we have
    # all the data via list_groups_with_meta_for_user + list_for_groups.
    membership_rows = user_group_members_repo().list_groups_with_meta_for_user(user_id)
    if not membership_rows:
        return EffectiveAccessResponse(is_admin=is_user_admin(user_id), items=[], tables=tables)

    by_gid = {m["group_id"]: m["name"] for m in membership_rows}
    grants_rows = resource_grants_repo().list_for_groups(list(by_gid.keys()))

    grouped: dict[tuple[str, str], EffectiveAccessItem] = {}
    for gr in sorted(grants_rows, key=lambda r: (r["resource_type"], r["resource_id"], by_gid.get(r["group_id"], ""))):
        rt, rid, gid = gr["resource_type"], gr["resource_id"], gr["group_id"]
        gname = by_gid.get(gid, gid)
        key = (rt, rid)
        if key not in grouped:
            grouped[key] = EffectiveAccessItem(
                resource_type=rt,
                resource_id=rid,
                via_groups=[],
            )
        grouped[key].via_groups.append({"group_id": gid, "group_name": gname})

    return EffectiveAccessResponse(
        is_admin=is_user_admin(user_id),
        items=list(grouped.values()),
        tables=tables,
    )


class LibraryPreviewItem(BaseModel):
    """One row of a person's Library, as the Simulate lens previews it."""

    id: str
    name: str
    # The wire enum ('required' | 'available') — the UI translates to the
    # human tier words (Automatic / Optional), same as everywhere else.
    requirement: str
    in_stack: bool
    # True iff `agnes pull` keeps a local copy (always for required; for
    # available only once subscribed) — the delivery half of the answer.
    materialized: bool
    # The analyst page for the row, when a slug exists to link to.
    href: Optional[str] = None


class LibraryPreviewSection(BaseModel):
    kind: str
    label: str
    items: List[LibraryPreviewItem]


class LibraryPreviewResponse(BaseModel):
    # 'auto' (every grant is a membership) or 'classic' (available grants
    # need a subscription) — the UI words the not-in-stack state off this.
    mode: str
    sections: List[LibraryPreviewSection]


@router.get(
    "/users/{user_id}/library-preview",
    response_model=LibraryPreviewResponse,
)
async def user_library_preview(
    user_id: str,
    user: dict = Depends(require_admin),
):
    """What the person's Library actually shows — the RESULT, where
    /effective-access is the why.

    Computed by ``StackResolver.browse``, the same grants-based projection
    the /library page renders the target's shared bands from — deliberately
    NOT ``browse_admin`` and NOT a re-derivation from the grant rows, so
    this preview cannot drift from the page it claims to predict (the
    Library's contract is no admin god-mode, and that applies to a preview
    OF a person just as it does to the person themselves). Covers the two
    governed kinds the resolver serves to the Library (data packages,
    memory domains); the other granted kinds keep their per-type fold in
    the Simulate chain.
    """
    from app.instance_config import get_stack_auto_membership
    from app.services.stack_resolver import StackResolver
    from src.repositories import data_packages_repo, memory_domains_repo

    if not users_repo().get_by_id(user_id):
        raise HTTPException(status_code=404, detail="User not found")

    resolver = StackResolver()
    slugs = {
        ResourceType.DATA_PACKAGE: {r["id"]: r.get("slug") for r in data_packages_repo().list(limit=100000)},
        ResourceType.MEMORY_DOMAIN: {r["id"]: r.get("slug") for r in memory_domains_repo().list(limit=100000)},
    }
    href_base = {
        ResourceType.DATA_PACKAGE: "/catalog/p/",
        ResourceType.MEMORY_DOMAIN: "/memory/d/",
    }
    labels = {
        ResourceType.DATA_PACKAGE: "Data packages",
        ResourceType.MEMORY_DOMAIN: "Memory",
    }

    sections: list[LibraryPreviewSection] = []
    for rt in (ResourceType.DATA_PACKAGE, ResourceType.MEMORY_DOMAIN):
        entries = resolver.browse(user_id, rt)
        if not entries:
            continue
        items = [
            LibraryPreviewItem(
                id=e.id,
                name=e.name,
                requirement=e.requirement or "available",
                in_stack=bool(e.in_stack),
                materialized=bool(e.materialized),
                href=(href_base[rt] + slugs[rt][e.id]) if slugs[rt].get(e.id) else None,
            )
            for e in sorted(entries, key=lambda e: (e.name or "").lower())
        ]
        sections.append(LibraryPreviewSection(kind=rt.value, label=labels[rt], items=items))

    return LibraryPreviewResponse(
        mode="auto" if get_stack_auto_membership() else "classic",
        sections=sections,
    )


# ---------------------------------------------------------------------------
# Self-service: /api/me/effective-access — non-admin can view their own.
# ---------------------------------------------------------------------------

# Separate router so it bypasses the admin gate. Mounted at /api/me/...
me_router = APIRouter(prefix="/api/me", tags=["me"])


@me_router.get(
    "/effective-access",
    response_model=EffectiveAccessResponse,
)
async def my_effective_access(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Same payload as /api/admin/users/{id}/effective-access but scoped to
    the calling user. Drives the /me/profile page's read-only access summary —
    so non-admin callers can self-audit without elevation. Admins get the
    same explicit grant breakdown as everyone else (no short-circuit) so
    the profile page audits the actual grant graph; runtime authorization
    still gives Admin god-mode regardless of this list.

    ``tables`` (§10.2) resolves the policy diagnosis against the FULL
    ``user`` principal from ``get_current_user`` — including
    ``credential_surface`` when the caller authenticated with a PAT — so a
    ``surface='stack'`` admin token (the ``agnes init`` default) audits
    itself as filtered, exactly like a live ``agnes query`` call with that
    same token would be (§12: "policies follow the credential surface").
    """
    user_id = user["id"]
    tables = _table_access_diagnoses(user, conn)

    membership_rows = user_group_members_repo().list_groups_with_meta_for_user(user_id)
    if not membership_rows:
        return EffectiveAccessResponse(is_admin=is_user_admin(user_id), items=[], tables=tables)

    by_gid = {m["group_id"]: m["name"] for m in membership_rows}
    grants_rows = resource_grants_repo().list_for_groups(list(by_gid.keys()))

    grouped: dict[tuple[str, str], EffectiveAccessItem] = {}
    for gr in sorted(grants_rows, key=lambda r: (r["resource_type"], r["resource_id"], by_gid.get(r["group_id"], ""))):
        rt, rid, gid = gr["resource_type"], gr["resource_id"], gr["group_id"]
        gname = by_gid.get(gid, gid)
        key = (rt, rid)
        if key not in grouped:
            grouped[key] = EffectiveAccessItem(
                resource_type=rt,
                resource_id=rid,
                via_groups=[],
            )
        grouped[key].via_groups.append({"group_id": gid, "group_name": gname})

    return EffectiveAccessResponse(
        is_admin=is_user_admin(user_id),
        tables=tables,
        items=list(grouped.values()),
    )
