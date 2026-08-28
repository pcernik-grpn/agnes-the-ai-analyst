"""Admin REST API for the SharePoint file-source connect wizard (spec
2026-08-27 §13.2 — "Connect wizard (file source): three steps").

Surface (all gated by ``Depends(require_admin)``):

  GET    /api/admin/sharepoint/connections/{id}/tree        — one level of the live
                                                                Graph folder tree
                                                                (sites -> drives -> root
                                                                children); ``?site_id=``/
                                                                ``?drive_id=`` pick the level.
  GET    /api/admin/sharepoint/connections/{id}/scopes       — list the connection's
                                                                confirmed scope rows,
                                                                enriched with collection +
                                                                group-grant info for the
                                                                wizard's step-2/3 preview.
  POST   /api/admin/sharepoint/connections/{id}/scopes       — confirm one scope: creates
                                                                (or reuses) its collection,
                                                                upserts the scope row, and
                                                                optionally applies group
                                                                grants (step 3).
  DELETE /api/admin/sharepoint/connections/{id}/scopes       — unselect a scope
                                                                (``?source_scope_id=``);
                                                                removes the row, leaves any
                                                                already-created collection
                                                                alone.
  GET    /api/admin/sharepoint/connections/{id}/corpus-map   — producer handoff: the flat
                                                                ``{source_scope_id: collection_id}``
                                                                mapping ``ship_to_agnes.py
                                                                --corpus-map`` consumes.

Scope rows live inside the connection's own ``config.scopes`` — a JSON list,
no new table (``source_connections.config`` is already a JSON column on both
backends). Each row is exactly ``{source_scope_id, display_path, anonymize,
collection_id}`` (spec §13.2); group grants are NOT duplicated here — they
are ordinary ``resource_grants`` rows on the collection, same primitive
``/admin/access`` already reads (spec: "facts are never granted... zero new
grant type").

Idempotency: confirming the SAME ``source_scope_id`` twice reuses the
existing row's ``collection_id`` rather than creating a second collection —
the storage anchor is the source scope id, not the display path (§6's
"stored by source folder/drive id, not by path" principle, applied to the
wizard's own bookkeeping as well as the eventual document anchor).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.resource_types import ResourceType
from connectors.sharepoint.graph_client import (
    SharePointGraphError,
    get_app_token,
    list_drives,
    list_root_children,
    list_sites,
)
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from src.repositories import (
    file_corpora_repo,
    resource_grants_repo,
    source_connections_repo,
    user_groups_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sharepoint", tags=["admin"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ConfirmScopeBody(BaseModel):
    source_scope_id: str = Field(..., min_length=1)
    display_path: str = Field(..., min_length=1)
    anonymize: bool = False
    # Step 3: applied as ordinary `resource_grants` rows on the collection —
    # never stored on the scope row itself (see module docstring).
    group_ids: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sharepoint_connection_or_404(connection_id: str) -> Dict[str, Any]:
    row = source_connections_repo().get(connection_id)
    if row is None or row.get("source_type") != "sharepoint":
        raise HTTPException(status_code=404, detail="connection_not_found")
    return row


def _scopes(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    scopes = (row.get("config") or {}).get("scopes")
    return list(scopes) if isinstance(scopes, list) else []


async def _resolved_token(row: Dict[str, Any]) -> str:
    """Resolve this connection's certificate and exchange it for a Graph
    access token — a single typed-error seam so the tree endpoint's 409/502
    split (cert missing vs. Graph/Entra rejected it) lives in exactly one
    place."""
    try:
        settings = resolve_sharepoint_settings(row)
    except SharePointSettingsError as exc:
        # Surface absence rather than fail the crawl silently (spec §13.2):
        # a typed 409 the wizard renders as "certificate not configured yet",
        # never a bare 500.
        raise HTTPException(
            status_code=409,
            detail={"error": "sharepoint_cert_unresolved", "message": str(exc)},
        ) from exc
    try:
        return await get_app_token(settings.tenant_id, settings.client_id, settings.private_key)
    except SharePointGraphError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc


def _group_ids_for_collection(collection_id: str) -> List[str]:
    grants = resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
    return [g["group_id"] for g in grants if g.get("resource_id") == collection_id]


def _scope_out(scope: Dict[str, Any]) -> Dict[str, Any]:
    collection = file_corpora_repo().get(scope.get("collection_id") or "")
    group_ids = _group_ids_for_collection(scope.get("collection_id") or "")
    return {
        "source_scope_id": scope.get("source_scope_id"),
        "display_path": scope.get("display_path"),
        "anonymize": bool(scope.get("anonymize")),
        "collection_id": scope.get("collection_id"),
        "collection": (
            {"id": collection["id"], "slug": collection["slug"], "name": collection["name"]} if collection else None
        ),
        "group_ids": group_ids,
        # Spec §13.2: "warn on any collection leaving with no group ('indexed
        # but invisible' is the worst silent state)" — the wizard's step-3
        # preview reads this per row rather than re-deriving it client-side.
        "no_group_warning": no_group_warning(group_ids),
    }


def no_group_warning(group_ids: List[str]) -> bool:
    """A collection with no granted group is indexed but invisible — the
    worst silent state (spec §13.2). Exposed as a standalone function so the
    rule is independently testable, not just observable through the API."""
    return len(group_ids) == 0


def _slugify(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80].strip("-") or "sharepoint"


def _create_scope_collection(*, connection_name: str, display_path: str, source_scope_id: str, created_by: str) -> str:
    """Create the collection a freshly confirmed scope maps to.

    Slug collisions (two different scopes whose display paths normalize to
    the same slug, e.g. two "Contracts" folders under different sites) are
    resolved by appending a short, stable suffix derived from the scope's
    own id — deterministic, so a caller who retries after a collision sees
    the same slug both times, and no path/name text ever needs parsing to
    reproduce it.
    """
    repo = file_corpora_repo()
    base_name = display_path.strip() or source_scope_id
    slug = _slugify(f"{connection_name}-{base_name}")
    name = f"{connection_name}: {base_name}"
    description = f"SharePoint scope · {display_path}"
    try:
        return repo.create(name=name, slug=slug, description=description, created_by=created_by)
    except Exception as exc:  # noqa: BLE001 — DuckDB ConstraintException / PG IntegrityError, message-sniffed elsewhere too
        err = str(exc).lower()
        if "unique" not in err and "duplicate" not in err and "constraint" not in err:
            raise
        suffix = hashlib.sha256(source_scope_id.encode()).hexdigest()[:8]
        return repo.create(name=name, slug=f"{slug}-{suffix}", description=description, created_by=created_by)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/connections/{connection_id}/tree")
async def browse_tree(
    connection_id: str,
    site_id: Optional[str] = None,
    drive_id: Optional[str] = None,
    _user: dict = Depends(require_admin),
):
    """One level of the live SharePoint folder tree.

    No ``site_id`` -> the reachable sites. ``site_id`` alone -> that site's
    document libraries (drives). Both -> the drive's root children. Exactly
    "sites -> drives -> root children, one level per call" (spec §13.2) —
    there is no deeper recursive browse; a folder's own children are not
    fetched until the admin picks it.
    """
    row = _sharepoint_connection_or_404(connection_id)
    token = await _resolved_token(row)
    try:
        if drive_id:
            items = await list_root_children(token, drive_id)
            return {"level": "items", "site_id": site_id, "drive_id": drive_id, "items": items}
        if site_id:
            items = await list_drives(token, site_id)
            return {"level": "drives", "site_id": site_id, "items": items}
        items = await list_sites(token)
        return {"level": "sites", "items": items}
    except SharePointGraphError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc


@router.get("/connections/{connection_id}/scopes")
async def list_scopes(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """The wizard's step-2/3 source of truth: every confirmed scope row,
    enriched with its collection and current group grants."""
    row = _sharepoint_connection_or_404(connection_id)
    return {"items": [_scope_out(s) for s in _scopes(row)]}


@router.post("/connections/{connection_id}/scopes", status_code=201)
async def confirm_scope(
    connection_id: str,
    body: ConfirmScopeBody,
    user: dict = Depends(require_admin),
):
    """Confirm one selected site/library/folder as a scope.

    Creates its collection on first confirmation; re-confirming the same
    ``source_scope_id`` reuses that same collection (idempotent) and updates
    ``display_path``/``anonymize`` in place — a rename or move in the source
    does not fork a second collection (§6 applied to the wizard's own
    bookkeeping). ``group_ids``, if given, are applied as ordinary
    ``resource_grants`` rows on the collection (step 3) — additive, never
    replacing grants set elsewhere (e.g. the collection detail page,
    ``/admin/access``).
    """
    row = _sharepoint_connection_or_404(connection_id)

    if body.group_ids:
        groups_repo = user_groups_repo()
        unknown = [gid for gid in body.group_ids if groups_repo.get(gid) is None]
        if unknown:
            raise HTTPException(status_code=400, detail={"error": "invalid_group_id", "group_ids": unknown})

    scopes = _scopes(row)
    existing = next((s for s in scopes if s.get("source_scope_id") == body.source_scope_id), None)

    if existing is not None:
        collection_id = existing["collection_id"]
        existing["display_path"] = body.display_path
        existing["anonymize"] = body.anonymize
    else:
        collection_id = _create_scope_collection(
            connection_name=row.get("name") or connection_id,
            display_path=body.display_path,
            source_scope_id=body.source_scope_id,
            created_by=user.get("id"),
        )
        scopes.append(
            {
                "source_scope_id": body.source_scope_id,
                "display_path": body.display_path,
                "anonymize": body.anonymize,
                "collection_id": collection_id,
            }
        )

    new_config = {**(row.get("config") or {}), "scopes": scopes}
    source_connections_repo().update(connection_id, config=new_config)

    for group_id in body.group_ids:
        resource_grants_repo().ensure_grant(
            group_id,
            ResourceType.COLLECTION.value,
            collection_id,
            assigned_by=user.get("id"),
        )

    logger.info(
        "sharepoint connection %s: scope %s confirmed -> collection %s",
        connection_id,
        body.source_scope_id,
        collection_id,
    )
    updated_row = next(s for s in scopes if s.get("source_scope_id") == body.source_scope_id)
    return _scope_out(updated_row)


@router.delete("/connections/{connection_id}/scopes", status_code=204)
async def remove_scope(
    connection_id: str,
    source_scope_id: str,
    _user: dict = Depends(require_admin),
):
    """Unselect a scope — an explicit exclusion (spec §13.2: "unselected rows
    are explicit exclusions"). Removes the wizard's own bookkeeping row only;
    any collection already created for it is left alone (deleting a
    collection is a separate, deliberate operation, not a side effect of
    unchecking a wizard row)."""
    row = _sharepoint_connection_or_404(connection_id)
    scopes = _scopes(row)
    remaining = [s for s in scopes if s.get("source_scope_id") != source_scope_id]
    if len(remaining) == len(scopes):
        raise HTTPException(status_code=404, detail="scope_not_found")
    new_config = {**(row.get("config") or {}), "scopes": remaining}
    source_connections_repo().update(connection_id, config=new_config)


@router.get("/connections/{connection_id}/corpus-map")
async def corpus_map(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Producer handoff (spec §13.2 / item 3): the flat
    ``{source_scope_id: collection_id}`` mapping the external crawl pipeline
    reads via ``ship_to_agnes.py --corpus-map`` until crawling moves inside
    Agnes. Not wrapped in an envelope key — the producer consumes this
    verbatim as the mapping itself."""
    row = _sharepoint_connection_or_404(connection_id)
    return {s["source_scope_id"]: s["collection_id"] for s in _scopes(row) if s.get("source_scope_id")}
