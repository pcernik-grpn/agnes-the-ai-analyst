"""Per-user composition view of the served Claude Code marketplace.

Provides:

  * ``GET  /api/my-stack``                                 — combined view
  * ``PUT  /api/my-stack/curated/{marketplace_id}/{plugin}`` — toggle subscription

Used by the ``agnes my-stack`` CLI subcommand. The web page that historically
backed these endpoints (``/my-ai-stack``) was removed in favor of
``/marketplace?tab=my``, but the API stays as the public CLI surface. Both
endpoints touch the same caches as the Store endpoints (ETag invalidation) so
any change here propagates to ``/marketplace.zip`` + ``/marketplace.git/`` on
the next request.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.dependencies import _get_db, get_current_user
from src.marketplace_filter import required_plugin_keys, resolve_allowed_plugins
from src.repositories import (
    audit_repo,
    marketplace_plugins_repo,
    user_curated_subscriptions_repo,
    user_store_installs_repo,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/my-stack", tags=["my-stack"])


class CuratedPlugin(BaseModel):
    marketplace_id: str
    marketplace_slug: str
    plugin_name: str
    manifest_name: str
    description: Optional[str] = None
    version: Optional[str] = None
    enabled: bool
    # v39: when TRUE, the user cannot unsubscribe (UI disables the
    # toggle, API guard returns 409). Pre-subscribed by mark_system +
    # creation hooks so ``enabled`` is always TRUE here.
    is_system: bool = False
    # Group-scoped Required tier: TRUE when any of the caller's groups
    # holds a ``requirement='required'`` grant for this plugin. Same
    # locked-toggle semantics as ``is_system`` (409 on unsubscribe), but
    # scoped to the granted groups; served without a subscription row,
    # so ``enabled`` reports TRUE even with no explicit subscription.
    is_required: bool = False


class StoreInstallEntry(BaseModel):
    entity_id: str
    type: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    version: str
    owner_user_id: str
    owner_username: str
    invocation_name: str
    install_count: int
    photo_url: Optional[str] = None
    installed_at: Optional[str] = None
    # v35: surface visibility so my_ai_stack.html can render an
    # "Archived by owner" badge on cards whose owner soft-deleted the
    # entity. Bundle still serves to existing installs (per
    # UserStoreInstallsRepository.list_for_user filter).
    visibility_status: Optional[str] = None


class MyStackResponse(BaseModel):
    curated: List[CuratedPlugin]
    store: List[StoreInstallEntry]


class ToggleRequest(BaseModel):
    enabled: bool


class OkResponse(BaseModel):
    ok: bool = True


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target: str,
    params: Optional[dict] = None,
) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=target, params=params)
    except Exception:
        pass


@router.get("", response_model=MyStackResponse)
async def get_my_stack(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Combined view of curated plugins the caller can subscribe to
    and Store entities they have installed.
    """
    granted = resolve_allowed_plugins(conn, user)
    # Model B (v28+): explicit subscriptions decide what's enabled.
    # `enabled` mirrors the legacy "not opted_out" UX so the existing toggle
    # remains semantically intuitive in the my-stack view. Required-tier
    # grant keys are unioned in because the resolver serves them without a
    # subscription row — the toggle must not claim "off" for a plugin the
    # user's sandbox actually has.
    subs = user_curated_subscriptions_repo().subscribed_set(user["id"])
    required = required_plugin_keys(conn, user["id"])

    # v39: surface is_system flag so the template can lock the toggle.
    # One round trip — set membership intersection in Python is cheaper
    # than joining marketplace_plugins per-row inside resolve_allowed_plugins
    # (which is also called from the marketplace_filter / packager hot path).
    system_plugins: set[tuple[str, str]] = set(marketplace_plugins_repo().list_system_keys())

    curated: List[CuratedPlugin] = []
    for p in granted:
        key = (p["marketplace_id"], p["original_name"])
        is_subscribed = key in subs
        is_system = key in system_plugins
        is_required = key in required
        curated.append(
            CuratedPlugin(
                marketplace_id=p["marketplace_id"],
                marketplace_slug=p["marketplace_slug"],
                plugin_name=p["original_name"],
                manifest_name=p["manifest_name"],
                description=p["raw"].get("description"),
                version=p.get("version"),
                enabled=is_subscribed or is_required,
                is_system=is_system,
                is_required=is_required,
            )
        )

    installs = user_store_installs_repo().list_for_user(user["id"])
    store_items: List[StoreInstallEntry] = []
    from app.api.store import entity_cover_url
    from src.store_naming import strip_archive_suffix

    for row in installs:
        photo_url = entity_cover_url(row)
        # Display name strips the archive-rename suffix so the user
        # sees their installed plugin's original label even after the
        # owner archived (and renamed) it. The served ``invocation_name``
        # carries the renamed slug since that's what Claude Code's
        # `/plugin` lookup will resolve to after the next sync — this
        # is the consumer-side rename described in the rename-on-
        # archive plan; the My AI Stack card surfaces it via the
        # "Archived by owner" badge already.
        raw_name = row["name"]
        display_name = strip_archive_suffix(raw_name)
        store_items.append(
            StoreInstallEntry(
                entity_id=row["id"],
                type=row["type"],
                name=display_name,
                description=row.get("description"),
                category=row.get("category"),
                version=row["version"],
                owner_user_id=row["owner_user_id"],
                owner_username=row["owner_username"],
                # v49 phase-3: stored synthetic_name (single source of
                # truth). The column is NOT NULL and `list_for_user`
                # selects it explicitly from the joined store_entities row.
                invocation_name=row["synthetic_name"],
                install_count=int(row.get("install_count") or 0),
                photo_url=photo_url,
                installed_at=_to_iso(row.get("installed_at")),
                visibility_status=row.get("visibility_status") or "approved",
            )
        )

    return MyStackResponse(curated=curated, store=store_items)


@router.put(
    "/curated/{marketplace_id}/{plugin_name}",
    response_model=OkResponse,
)
async def toggle_curated(
    marketplace_id: str,
    plugin_name: str,
    body: ToggleRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Toggle subscribe/unsubscribe for a single curated plugin.

    UI thinks in terms of *enabled* (default off in Model B). v28+ the
    repository stores *subscribed* rows (presence = enabled in served set);
    ``enabled=true`` writes a row, ``enabled=false`` removes it.
    """
    # Sanity: caller must actually have the plugin granted (otherwise the
    # toggle is meaningless and would just leak rows for ungranted plugins).
    granted = resolve_allowed_plugins(conn, user)
    has_grant = any(p["marketplace_id"] == marketplace_id and p["original_name"] == plugin_name for p in granted)
    if not has_grant:
        raise HTTPException(status_code=404, detail="grant_not_found")

    # v39: system plugins are pinned in every user's stack — refuse the
    # unsubscribe path. Subscribe is still allowed (no-op on the
    # already-materialized row).
    if not body.enabled:
        row = marketplace_plugins_repo().get(marketplace_id, plugin_name)
        if row and bool(row.get("is_system")):
            raise HTTPException(
                status_code=409,
                detail="cannot_unsubscribe_system_plugin",
            )
        # Group-scoped Required tier: the resolver serves required-granted
        # plugins regardless of the subscription row, so honoring the
        # unsubscribe would leave the toggle claiming "off" for a plugin
        # still in the user's sandbox. Refuse, mirroring is_system.
        if (marketplace_id, plugin_name) in required_plugin_keys(conn, user["id"]):
            raise HTTPException(
                status_code=409,
                detail="cannot_unsubscribe_required_plugin",
            )

    repo = user_curated_subscriptions_repo()
    if body.enabled:
        repo.subscribe(user["id"], marketplace_id, plugin_name)
    else:
        repo.unsubscribe(user["id"], marketplace_id, plugin_name)
    _audit(
        conn,
        user["id"],
        "my_stack.curated.toggle",
        f"plugin:{marketplace_id}/{plugin_name}",
        {"enabled": body.enabled},
    )

    try:
        from app.marketplace_server import packager

        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")

    return OkResponse()
