"""Admin REST API for Data Packages (v49 unified stack).

A Data Package is a curated bundle of tables (M:N to ``table_registry``) that
serves as the unit of "Add to stack" on /catalog. Section 6 of the unified
stack design specifies a CRUD surface under ``/api/admin/data-packages``:

  - ``GET    /api/admin/data-packages``           — list + search
  - ``POST   /api/admin/data-packages``           — create
  - ``GET    /api/admin/data-packages/{id}``      — detail with embedded tables
  - ``PUT    /api/admin/data-packages/{id}``      — update metadata
  - ``DELETE /api/admin/data-packages/{id}``      — delete (cascades junction)
  - ``POST   /api/admin/data-packages/{id}/tables``         — add table
  - ``DELETE /api/admin/data-packages/{id}/tables/{table}`` — remove table

Every mutation writes an ``audit_log`` row (see Section 9.1 of the design).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator

# Six-digit hex colors only — the /catalog cards use the value directly as
# a CSS background, so accepting anything else lets malformed input land in
# the DB and break the card layout (E2E found "#ff5733#e0f2fe" stored
# verbatim after the create modal's text input concatenated values).
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _validate_color(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not _HEX_COLOR_RE.match(value):
        raise ValueError("color must be a 6-digit hex like '#e0f2fe'")
    return value.lower()


from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.repositories import (
    audit_repo,
    data_packages_repo,
    table_registry_repo,
    tool_registry_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/data-packages", tags=["data-packages"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


# v51: lifecycle status enum — used by the hero filter checkboxes on
# /catalog. Kept as a frozen tuple so the validator + tests share one
# source of truth.
_PACKAGE_STATUSES = ("prod", "poc", "coming-soon", "draft")


def _validate_status(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().lower()
    if not v:
        return None
    if v not in _PACKAGE_STATUSES:
        raise ValueError(f"status must be one of {sorted(_PACKAGE_STATUSES)}")
    return v


# v56 validation caps. Names match the Foundry spec checklist
# (≤300 chars card desc, ≤8 use/skip bullets, etc.) — strict enough
# to keep the rendered detail page from overflowing without being
# annoying to fill in.
_TAGS_MAX_COUNT = 8
_TAG_MAX_CHARS = 30
_LONG_DESCRIPTION_MAX = 4000
_BULLETS_MAX_COUNT = 8
_BULLET_MAX_CHARS = 200
_EXAMPLE_QUESTIONS_MAX_COUNT = 12
_EXAMPLE_QUESTION_MAX_CHARS = 200


def _validate_tags(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return None
    if len(v) > _TAGS_MAX_COUNT:
        raise ValueError(f"tags: max {_TAGS_MAX_COUNT} entries")
    for t in v:
        if not isinstance(t, str):
            raise ValueError("tags: each entry must be a string")
        if len(t) > _TAG_MAX_CHARS:
            raise ValueError(f"tags: each entry max {_TAG_MAX_CHARS} chars")
    return v


def _validate_long_description(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if len(v) > _LONG_DESCRIPTION_MAX:
        raise ValueError(f"long_description: max {_LONG_DESCRIPTION_MAX} chars")
    return v


def _validate_bullet_list(v: Optional[List[str]], *, field: str) -> Optional[List[str]]:
    if v is None:
        return None
    if len(v) > _BULLETS_MAX_COUNT:
        raise ValueError(f"{field}: max {_BULLETS_MAX_COUNT} bullets")
    for b in v:
        if not isinstance(b, str):
            raise ValueError(f"{field}: each entry must be a string")
        if len(b) > _BULLET_MAX_CHARS:
            raise ValueError(f"{field}: each bullet max {_BULLET_MAX_CHARS} chars")
    return v


def _validate_example_questions(v: Optional[List[str]]) -> Optional[List[str]]:
    if v is None:
        return None
    if len(v) > _EXAMPLE_QUESTIONS_MAX_COUNT:
        raise ValueError(f"example_questions: max {_EXAMPLE_QUESTIONS_MAX_COUNT} entries")
    for q in v:
        if not isinstance(q, str):
            raise ValueError("example_questions: each entry must be a string")
        if len(q) > _EXAMPLE_QUESTION_MAX_CHARS:
            raise ValueError(f"example_questions: each entry max {_EXAMPLE_QUESTION_MAX_CHARS} chars")
    return v


class CreateDataPackageRequest(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    cover_image_url: Optional[str] = None
    # v51: lifecycle + classification surface for /catalog cards.
    status: Optional[str] = None
    category: Optional[str] = None
    # v56: extended-content fields for the /catalog/p/<slug> rewrite.
    owner_name: Optional[str] = None
    owner_team: Optional[str] = None
    tags: Optional[List[str]] = None
    long_description: Optional[str] = None
    when_to_use: Optional[List[str]] = None
    when_not_to_use: Optional[List[str]] = None
    example_questions: Optional[List[str]] = None

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return _validate_color(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return _validate_status(v)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v):
        return _validate_tags(v)

    @field_validator("long_description")
    @classmethod
    def _check_long_desc(cls, v):
        return _validate_long_description(v)

    @field_validator("when_to_use")
    @classmethod
    def _check_when_to_use(cls, v):
        return _validate_bullet_list(v, field="when_to_use")

    @field_validator("when_not_to_use")
    @classmethod
    def _check_when_not_to_use(cls, v):
        return _validate_bullet_list(v, field="when_not_to_use")

    @field_validator("example_questions")
    @classmethod
    def _check_example_questions(cls, v):
        return _validate_example_questions(v)


class UpdateDataPackageRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    # v50: cover image override. Sending `""` clears the cover (admin
    # pressed Remove); sending a non-empty string sets it; omitting the
    # field leaves it unchanged (Optional-is-no-op contract).
    cover_image_url: Optional[str] = None
    # v51: status follows the same enum allowlist; category accepts free
    # text. Sending `""` for category clears it; omitting leaves it.
    status: Optional[str] = None
    category: Optional[str] = None
    # v56: same fields as Create. JSON-list fields use Optional-is-no-op;
    # pass an empty list to actively clear (writes "[]" → decodes back to []).
    owner_name: Optional[str] = None
    owner_team: Optional[str] = None
    tags: Optional[List[str]] = None
    long_description: Optional[str] = None
    when_to_use: Optional[List[str]] = None
    when_not_to_use: Optional[List[str]] = None
    example_questions: Optional[List[str]] = None

    @field_validator("color")
    @classmethod
    def _check_color(cls, v: Optional[str]) -> Optional[str]:
        return _validate_color(v)

    @field_validator("status")
    @classmethod
    def _check_status(cls, v: Optional[str]) -> Optional[str]:
        return _validate_status(v)

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v):
        return _validate_tags(v)

    @field_validator("long_description")
    @classmethod
    def _check_long_desc(cls, v):
        return _validate_long_description(v)

    @field_validator("when_to_use")
    @classmethod
    def _check_when_to_use(cls, v):
        return _validate_bullet_list(v, field="when_to_use")

    @field_validator("when_not_to_use")
    @classmethod
    def _check_when_not_to_use(cls, v):
        return _validate_bullet_list(v, field="when_not_to_use")

    @field_validator("example_questions")
    @classmethod
    def _check_example_questions(cls, v):
        return _validate_example_questions(v)


class AddTableRequest(BaseModel):
    table_id: str


class AddToolRequest(BaseModel):
    tool_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[Dict[str, Any]] = None,
    params_before: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit row. Mirrors the helper in ``app/api/access.py``."""
    try:
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


# v56: how long a package is considered "new" — 30 days from creation.
_NEW_BADGE_DAYS = 30


def _badges_for(pkg: Dict[str, Any], conn: Optional[duckdb.DuckDBPyConnection] = None) -> List[str]:
    """Derive the virtual badge list shown on /catalog cards + the
    package-detail hero. ONE badge now, render-time-computed so backdating
    ``created_at`` picks up automatically:

      * ``new`` — ``created_at`` within :data:`_NEW_BADGE_DAYS`.

    ``curated`` is **gone** (v113). It was derived here from "is ``created_by``
    currently in the Admin group", which is a fact about the person, not about
    the package: an admin leaving the Admin group silently un-curated
    everything they had ever created, and the badge could not be set or
    corrected by anyone. The trust claim now lives in the stored
    ``publisher_kind`` column — the same axis ``store_entities`` carries — and
    is surfaced by :func:`_serialize` rather than invented here, so every
    surface renders one shared marker off one stored value.

    Note the shape that remains: ``new`` is a genuine function of the clock, so
    deriving it is correct. That is the line — derive what follows from data on
    the row, store what somebody decided.
    """
    from datetime import datetime, timedelta, timezone

    badges: List[str] = []

    created_at = pkg.get("created_at")
    if created_at:
        try:
            ts = created_at if isinstance(created_at, datetime) else None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if ts and (now - ts) < timedelta(days=_NEW_BADGE_DAYS):
                badges.append("new")
        except Exception:
            logger.warning("badge new lookup failed for %s", pkg.get("id"))

    return badges


def _serialize(pkg: Dict[str, Any], conn: Optional[duckdb.DuckDBPyConnection] = None) -> Dict[str, Any]:
    """Project a repo row onto the API response shape.

    ``conn`` is optional only so legacy callers that don't need the
    v56 ``badges`` field can call ``_serialize(pkg)``; pass the conn
    in to opt into badge derivation.

    On Postgres the request ``conn`` (from ``_get_db``) is ``None`` — the
    system DuckDB must never be opened there — but ``_badges_for`` resolves
    its RBAC lookups through the repository factory and ignores ``conn``, so
    badge derivation must still run under ``use_pg()`` to keep the ``badges``
    field at parity with DuckDB.
    """
    out = {
        "id": pkg["id"],
        "slug": pkg["slug"],
        "name": pkg["name"],
        "description": pkg.get("description"),
        "icon": pkg.get("icon"),
        "color": pkg.get("color"),
        "cover_image_url": pkg.get("cover_image_url"),
        # v51: status defaults to 'prod' for legacy rows where the
        # ALTER's DEFAULT didn't backfill (older DuckDB versions don't
        # apply DEFAULT to existing rows on ADD COLUMN).
        "status": pkg.get("status") or "prod",
        "category": pkg.get("category"),
        # v113: stored trust axis, same vocabulary + same values as
        # store_entities. Defaulted here as well as in the repos because a
        # caller may hand _serialize a dict that never went through one.
        "publisher_kind": pkg.get("publisher_kind")
        if pkg.get("publisher_kind") in ("user", "organization")
        else "user",
        # v56: extended content. JSON-list fields decode to [] for NULL
        # via the repo's _decode_row; safe to pass through unchanged.
        "owner_name": pkg.get("owner_name"),
        "owner_team": pkg.get("owner_team"),
        "tags": pkg.get("tags") or [],
        "long_description": pkg.get("long_description"),
        "when_to_use": pkg.get("when_to_use") or [],
        "when_not_to_use": pkg.get("when_not_to_use") or [],
        "example_questions": pkg.get("example_questions") or [],
        "created_by": pkg.get("created_by"),
        "created_at": pkg["created_at"].isoformat() if pkg.get("created_at") else None,
        "updated_at": pkg["updated_at"].isoformat() if pkg.get("updated_at") else None,
    }
    from src.repositories import use_pg

    if conn is not None or use_pg():
        out["badges"] = _badges_for(pkg, conn)
    return out


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[Dict[str, Any]])
async def list_data_packages(
    search: Optional[str] = None,
    include_table_ids: bool = False,
    limit: int = Query(
        200,
        ge=1,
        le=5000,
        description="Page size. The repository always had one (default 200); exposing it lets a composed "
        "reader ask for the whole inventory instead of silently losing every package past the 200th.",
    ),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all packages, optionally name-prefix filtered for the chip-input
    typeahead on ``/admin/tables``.

    ``include_table_ids=true`` (v54): each row carries a
    ``table_ids: [...]`` array. The /admin/tables hydrator uses this to
    collapse the prior N+1 fan-out (one ``GET /api/admin/data-packages/
    {id}`` per package) into a single round-trip — see
    ``DataPackagesRepository.list_member_ids_bulk``.
    """
    repo = data_packages_repo()
    rows = repo.list(search=search, limit=limit)
    serialized = [_serialize(r, conn) for r in rows]
    if include_table_ids:
        members_by_pkg = repo.list_member_ids_bulk()
        for row in serialized:
            row["table_ids"] = members_by_pkg.get(row["id"], [])
    return serialized


@router.post("", status_code=201)
async def create_data_package(
    payload: CreateDataPackageRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create a new Data Package. ``slug`` is the user-visible stable id; the
    UNIQUE constraint in DuckDB raises ``ConstraintException`` on collision and
    we translate that to ``409 slug_exists`` per spec."""
    repo = data_packages_repo()
    if not payload.name.strip() or not payload.slug.strip():
        raise HTTPException(status_code=400, detail="name and slug are required")
    try:
        pkg_id = repo.create(
            name=payload.name.strip(),
            slug=payload.slug.strip(),
            description=payload.description,
            icon=payload.icon,
            color=payload.color,
            cover_image_url=payload.cover_image_url,
            status=payload.status or "prod",
            category=(payload.category or "").strip() or None,
            # v113: 'organization', not the repo's 'user' default. This endpoint
            # is `require_admin`-gated, so a package created through it IS the
            # organization publishing — which is also what the retired derived
            # badge effectively said (creator-is-admin was true by construction
            # here). Writing it explicitly at creation is what stops the claim
            # from evaporating later when that admin's group membership changes.
            publisher_kind="organization",
            owner_name=payload.owner_name,
            owner_team=payload.owner_team,
            tags=payload.tags,
            long_description=payload.long_description,
            when_to_use=payload.when_to_use,
            when_not_to_use=payload.when_not_to_use,
            example_questions=payload.example_questions,
            created_by=user.get("email") or user["id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")
    _audit(
        conn,
        user["id"],
        "data_package.create",
        f"data_package:{pkg_id}",
        {"slug": payload.slug, "name": payload.name},
    )
    return {"id": pkg_id}


@router.get("/{pkg_id}")
async def get_data_package(
    pkg_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view including the list of tables AND related MCP tools."""
    repo = data_packages_repo()
    pkg = repo.get(pkg_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="data_package_not_found")
    out = _serialize(pkg, conn)
    out["tables"] = repo.list_tables(pkg_id)
    out["related_tools"] = repo.list_tools(pkg_id)
    return out


@router.put("/{pkg_id}")
async def update_data_package(
    pkg_id: str,
    payload: UpdateDataPackageRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Patch package metadata. Audit row carries before/after diff so admins
    can reconstruct the rename history."""
    repo = data_packages_repo()
    existing = repo.get(pkg_id)
    if not existing:
        raise HTTPException(status_code=404, detail="data_package_not_found")

    before = {
        "name": existing.get("name"),
        "description": existing.get("description"),
        "icon": existing.get("icon"),
        "color": existing.get("color"),
        "cover_image_url": existing.get("cover_image_url"),
        "status": existing.get("status"),
        "category": existing.get("category"),
    }
    # v50: a literal empty string from the client means "remove the cover
    # image" (the modal's Remove button POSTs ""); a non-empty string sets
    # it; None / omitted leaves it unchanged. Map to the repo's explicit
    # clear flag so the SQL stays unambiguous. v51 applies the same
    # empty-string-clears contract to category.
    clear_cover = payload.cover_image_url == ""
    clear_category = payload.category == ""
    repo.update(
        pkg_id,
        name=payload.name,
        description=payload.description,
        icon=payload.icon,
        color=payload.color,
        cover_image_url=None if clear_cover else payload.cover_image_url,
        clear_cover_image=clear_cover,
        status=payload.status,
        category=None if clear_category else payload.category,
        clear_category=clear_category,
        owner_name=payload.owner_name,
        owner_team=payload.owner_team,
        tags=payload.tags,
        long_description=payload.long_description,
        when_to_use=payload.when_to_use,
        when_not_to_use=payload.when_not_to_use,
        example_questions=payload.example_questions,
    )
    fresh = repo.get(pkg_id)
    after = {
        "name": fresh.get("name") if fresh else None,
        "description": fresh.get("description") if fresh else None,
        "icon": fresh.get("icon") if fresh else None,
        "color": fresh.get("color") if fresh else None,
        "cover_image_url": fresh.get("cover_image_url") if fresh else None,
        "status": fresh.get("status") if fresh else None,
        "category": fresh.get("category") if fresh else None,
    }
    _audit(
        conn,
        user["id"],
        "data_package.update",
        f"data_package:{pkg_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize(fresh, conn) if fresh else {"id": pkg_id}


@router.delete("/{pkg_id}", status_code=204)
async def delete_data_package(
    pkg_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """v54: soft delete — sets ``deleted_at`` instead of hard-removing the
    row, so the junction (``data_package_tables``) + any resource_grants
    survive intact for the undo flow (POST /restore). Hard delete is
    available via ``repo.hard_delete`` but not currently exposed."""
    repo = data_packages_repo()
    existing = repo.get(pkg_id)
    if not existing:
        raise HTTPException(status_code=404, detail="data_package_not_found")
    tables_count = len(repo.list_tables(pkg_id))
    repo.delete(pkg_id)
    _audit(
        conn,
        user["id"],
        "data_package.delete",
        f"data_package:{pkg_id}",
        {"slug": existing.get("slug"), "tables_count": tables_count},
    )


@router.post("/{pkg_id}/restore", status_code=200)
async def restore_data_package(
    pkg_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """v54 undo: reverse a soft delete. Idempotent — restoring an already-
    live package is a no-op. 404 only when the row is truly gone (e.g.
    after a hard_delete)."""
    repo = data_packages_repo()
    existing = repo.get(pkg_id, include_deleted=True)
    if not existing:
        raise HTTPException(status_code=404, detail="data_package_not_found")
    repo.restore(pkg_id)
    _audit(
        conn,
        user["id"],
        "data_package.restore",
        f"data_package:{pkg_id}",
        {"slug": existing.get("slug")},
    )
    return {"id": pkg_id, "restored": True}


# ---------------------------------------------------------------------------
# Junction endpoints — add/remove tables
# ---------------------------------------------------------------------------


@router.post("/{pkg_id}/tables")
async def add_table_to_package(
    pkg_id: str,
    payload: AddTableRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Add a table to the package. 404 if the package or table doesn't exist.
    200 + ``{added: True/False}`` so the chip-input UI can re-render without
    a second roundtrip; idempotent on duplicate."""
    repo = data_packages_repo()
    if not repo.get(pkg_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    # Backend-aware factory — NOT TableRegistryRepository(conn): the raw
    # _get_db conn is always DuckDB, so on a Postgres deployment it never sees
    # tables that live in PG and the attach 404s (table_not_found) for tables
    # that ARE in the catalog. The factory routes to the active backend.
    table_repo = table_registry_repo()
    table = table_repo.get(payload.table_id)
    if not table:
        raise HTTPException(status_code=404, detail="table_not_found")
    added = repo.add_table(pkg_id, payload.table_id, added_by=user.get("email") or user["id"])
    if added:
        _audit(
            conn,
            user["id"],
            "data_package.add_table",
            f"data_package:{pkg_id}",
            {"table_id": payload.table_id, "table_name": table.get("name")},
        )
    return {"added": added}


@router.delete("/{pkg_id}/tables/{table_id}", status_code=204)
async def remove_table_from_package(
    pkg_id: str,
    table_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove a table from the package. Idempotent on missing junction row."""
    repo = data_packages_repo()
    if not repo.get(pkg_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    removed = repo.remove_table(pkg_id, table_id)
    if removed:
        _audit(
            conn,
            user["id"],
            "data_package.remove_table",
            f"data_package:{pkg_id}",
            {"table_id": table_id},
        )


# ---------------------------------------------------------------------------
# MCP tool junction (v64, RFC #461 §6) — symmetric with /tables above
# ---------------------------------------------------------------------------


@router.post("/{pkg_id}/tools")
async def add_tool_to_package(
    pkg_id: str,
    payload: AddToolRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Attach an MCP tool to the package. 404 if either side is missing,
    200 + ``{added: True/False}`` to mirror /tables — idempotent."""
    repo = data_packages_repo()
    if not repo.get(pkg_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    tool_repo = tool_registry_repo()
    tool = tool_repo.get(payload.tool_id)
    if not tool:
        raise HTTPException(status_code=404, detail="tool_not_found")
    added = repo.add_tool(pkg_id, payload.tool_id)
    if added:
        _audit(
            conn,
            user["id"],
            "data_package.add_tool",
            f"data_package:{pkg_id}",
            {"tool_id": payload.tool_id, "exposed_name": tool.get("exposed_name")},
        )
    return {"added": added}


@router.delete("/{pkg_id}/tools/{tool_id}", status_code=204)
async def remove_tool_from_package(
    pkg_id: str,
    tool_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detach an MCP tool from the package. Idempotent on missing row."""
    repo = data_packages_repo()
    if not repo.get(pkg_id):
        raise HTTPException(status_code=404, detail="data_package_not_found")
    removed = repo.remove_tool(pkg_id, tool_id)
    if removed:
        _audit(
            conn,
            user["id"],
            "data_package.remove_tool",
            f"data_package:{pkg_id}",
            {"tool_id": tool_id},
        )
