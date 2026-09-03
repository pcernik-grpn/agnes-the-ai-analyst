"""User stack API — subscribe / unsubscribe / list.

Three user-facing endpoints under ``/api/stack``:

  - ``GET    /api/stack?type=data_package|memory_domain`` — user's effective
    stack
  - ``POST   /api/stack/subscribe``                       — join / download
  - ``DELETE /api/stack/subscription/{type}/{id}``        — leave / remove

Stack resolution is delegated to ``app/services/stack_resolver.py``, whose
semantics fork on ``features.stack_auto_membership`` (spec
2026-08-07-default-chrome-ux-parity; flipped to default-on in Wave 0,
2026-08). Auto-membership — the default: every grant is a member
(required ∪ available); subscribing only requests a LOCAL COPY of an
available resource, flagged via ``materialized``. Classic — explicit
opt-out (``features.stack_auto_membership: false``): membership is the
subscribe model (required ∪ subscribed-available); subscribing JOINS the
stack, unsubscribing leaves it, and every member is materialized (downloaded
by ``agnes pull``). The endpoint contract
(payload fields, status codes) is identical in both modes. The resolver
raises HTTPException directly for the two business-rule errors
(``already_required`` on subscribe, ``cannot_remove_required`` on
unsubscribe) — required resources are always in the stack and downloaded, so
there's nothing to opt in/out of.

Server-side telemetry — ``stack.subscribe`` / ``stack.unsubscribe`` events
land in ``usage_events`` via ``UsageRepository.emit_server_event``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import can_access
from app.auth.dependencies import get_current_user
from app.resource_types import ResourceType
from app.services.journey import mark_journey
from app.services.stack_resolver import StackResolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stack", tags=["stack"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SubscribeRequest(BaseModel):
    resource_type: str
    resource_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_type(value: str) -> ResourceType:
    """Resolve a string into the ResourceType enum, restricted to types the
    StackResolver supports. Marketplace plugins are explicitly excluded
    (design D1 — they keep their own resolver)."""
    try:
        rt = ResourceType(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"unknown_resource_type:{value}",
        )
    if rt not in (ResourceType.DATA_PACKAGE, ResourceType.MEMORY_DOMAIN):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported_stack_type:{rt.value}",
        )
    return rt


def _reject_co_session(user: object) -> None:
    """Stack management is per-user — a restricted principal has no identity
    to subscribe with (a co-session has no single one; an agent-session must
    not mutate its owner's stack), and ``user["id"]`` would blow up on the
    frozen dataclass anyway. Every /api/stack endpoint must call this first."""
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(403, "co_session cannot manage stack")


def _emit_event(
    *,
    event_type: str,
    user: dict,
    props: dict,
) -> None:
    """Fire-and-forget — telemetry must never break the user's action."""
    try:
        from src.repositories import usage_repo

        usage_repo().emit_server_event(
            event_type=event_type,
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props=props,
        )
    except Exception:
        logger.warning("usage_events emit failed for %s", event_type)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_stack(
    type: str,
    user: dict = Depends(get_current_user),
):
    """Return the user's effective stack for the given resource type.

    Auto-membership (the default): every grant on the caller's groups is a
    member, no subscription needed — ``materialized`` then flags which ones
    are also kept as a local copy (always true for required, true for
    available only once subscribed). Classic (explicit opt-out via
    ``features.stack_auto_membership: false``): effective stack = required
    grants plus the ``available`` grants the caller subscribed to; every
    member is ``materialized`` (downloaded by `agnes pull`).
    """
    _reject_co_session(user)
    rt = _validate_type(type)
    resolver = StackResolver()
    items = [
        {
            "id": e.id,
            "name": e.name,
            "description": e.description,
            "icon": e.icon,
            "color": e.color,
            "requirement": e.requirement,
            "in_stack": e.in_stack,
            "materialized": e.materialized,
        }
        for e in resolver.stack(user["id"], rt)
    ]
    return {"items": items}


@router.get("/browse")
async def browse_stack(
    type: str,
    user: dict = Depends(get_current_user),
):
    """List every resource of ``type`` the caller could see (RBAC-granted).

    The full candidate set (issue #621), independent of membership mode.
    Auto-membership (the default): every granted item is ``in_stack: true``
    and the scope equals ``GET /api/stack``; ``materialized`` then tells an
    analyst's Claude what is already downloaded locally vs. only server-side
    queryable. Classic (explicit opt-out via
    ``features.stack_auto_membership: false``): ``in_stack`` marks the
    members (required or subscribed) and the rest are addable via
    ``POST /api/stack/subscribe``.

    Scoped per-user by ``StackResolver.browse`` (group grants only), so the
    only authorization gate is authentication — no extra RBAC dependency.
    """
    _reject_co_session(user)
    rt = _validate_type(type)
    resolver = StackResolver()
    items = [
        {
            "id": e.id,
            "name": e.name,
            "description": e.description,
            "icon": e.icon,
            "color": e.color,
            "requirement": e.requirement,
            "in_stack": e.in_stack,
            "materialized": e.materialized,
        }
        for e in resolver.browse(user["id"], rt)
    ]
    return {"items": items}


@router.post("/subscribe")
async def subscribe(
    payload: SubscribeRequest,
    user: dict = Depends(get_current_user),
):
    """Subscribe to an ``available`` resource. Auto-membership (the
    default): the resource is already in the stack, so this only requests a
    downloaded local copy. Classic (explicit opt-out via
    ``features.stack_auto_membership: false``): this ADDS the resource to
    the caller's stack. Refuses if the resource is required (always in the
    stack and downloaded automatically — clients shouldn't bother)."""
    _reject_co_session(user)
    rt = _validate_type(payload.resource_type)
    # The user must have *some* grant on the resource — otherwise this is a
    # 403 (you can't subscribe to something you can't access). can_access
    # short-circuits for admins, which is the intended behavior.
    if not can_access(user["id"], rt.value, payload.resource_id):
        raise HTTPException(status_code=403, detail="no_grant")
    resolver = StackResolver()
    try:
        resolver.add_to_stack(user["id"], rt, payload.resource_id)
    except HTTPException:
        raise
    _emit_event(
        event_type="stack.subscribe",
        user=user,
        props={
            "resource_type": rt.value,
            "resource_id": payload.resource_id,
        },
    )
    # The onboarding step is "Put knowledge in your stack" — this is that,
    # whichever surface asked for it (Library page, chat, CLI, MCP).
    mark_journey(user.get("id"), stack_setup_done=True)
    return {"subscribed": True}


@router.delete("/subscription/{resource_type}/{resource_id}", status_code=204)
async def unsubscribe(
    resource_type: str,
    resource_id: str,
    user: dict = Depends(get_current_user),
):
    """Unsubscribe from an ``available`` resource. Auto-membership (the
    default): the resource stays in the stack (still queryable
    server-side), only the downloaded local copy is dropped. Classic
    (explicit opt-out via ``features.stack_auto_membership: false``): this
    REMOVES the resource from the caller's stack. Returns 400
    ``cannot_remove_required`` when the resource is required for any of the
    user's groups (required is always in the stack and downloaded, no
    opt-out).

    Returns 204 No Content on success — DELETE idempotency convention
    enforced by the API design rules test.
    """
    _reject_co_session(user)
    rt = _validate_type(resource_type)
    resolver = StackResolver()
    try:
        resolver.remove_from_stack(user["id"], rt, resource_id)
    except HTTPException:
        raise
    _emit_event(
        event_type="stack.unsubscribe",
        user=user,
        props={
            "resource_type": rt.value,
            "resource_id": resource_id,
        },
    )


# ---------------------------------------------------------------------------
# Artifacts (file_corpora "collections") — added to My Stack (#feature)
# ---------------------------------------------------------------------------
#
# Artifacts are NOT routed through StackResolver (see module docstring for
# data_package/memory_domain) — there is no admin-RBAC "required" tier for a
# personal upload. Permission is ownership/sharing (checked via
# ``can_access_collection``, the same primitive /artefacts and /library use),
# and Stack membership is stored as a plain ``user_stack_subscriptions`` row
# with ``resource_type='collection'`` — the generic subscribe()/unsubscribe()
# already used above is idempotent, satisfying "prevent duplicate
# memberships" for free. Small, dedicated endpoints rather than threading
# artifacts through ``_validate_type``/``StackResolver``.
#
# NOTE (deferred follow-up — see the product spec's scope note): adding an
# artifact here makes it *queryable as Stack data*, but the default agent's
# retrieval tools (``knowledge_search``/``collections_search`` in
# app/api/mcp/foundation_tools.py, backed by /api/knowledge/search and
# /api/collections/search) do not yet gate results by Stack membership — they
# still fan out over every RBAC-accessible collection. Wiring that gate is an
# intentionally separate, higher-risk change; this feature only makes the
# membership data correct and queryable.


@router.post("/artefacts/{corpus_id}")
async def add_artefact_to_stack(
    corpus_id: str,
    user: dict = Depends(get_current_user),
):
    """Add an artifact (a ``file_corpora`` collection) to the caller's Stack
    so the default agent can use it. 404 if the collection doesn't exist
    (or is soft-deleted); 403 if the caller cannot access it (not owned, not
    shared with one of their groups, not workspace-published). Idempotent —
    adding an already-in-stack artifact just re-confirms membership.

    Returns the same catalog-card shape My Stack renders (``card``), so the
    picker/Artefacts-page JS can insert the new row live without a reload.
    """
    _reject_co_session(user)
    from app.auth.access import can_access_collection
    from app.services.artefact_access import (
        build_artefact_access_context,
        collection_visibility,
        owner_label_for,
    )
    from src.repositories import corpus_files_repo, file_corpora_repo, user_stack_subscriptions_repo

    uid = user["id"]
    col = file_corpora_repo().get(corpus_id)
    if not col:
        raise HTTPException(status_code=404, detail="artefact_not_found")
    if not can_access_collection(uid, corpus_id):
        raise HTTPException(status_code=403, detail="no_access")

    user_stack_subscriptions_repo().subscribe(uid, ResourceType.COLLECTION.value, corpus_id)
    _emit_event(event_type="stack.artefact_add", user=user, props={"resource_id": corpus_id})
    # Same onboarding milestone as /subscribe above: a file collection joining
    # the stack is "Put knowledge in your stack" just as much as a package is.
    mark_journey(uid, stack_setup_done=True)

    # Local import — the card normalizer lives in app/web/router.py (the web
    # page that owns its presentation contract); deferred to request time to
    # avoid a module-load-order dependency between the two routers.
    from app.web.router import _catalog_card_stack_artefact

    cf_repo = corpus_files_repo()
    try:
        files = cf_repo.list_for_corpus(corpus_id)
    except Exception:
        files = []
    file_count = len(files)
    first_file = None
    if file_count == 1:
        f0 = files[0]
        first_file = {
            "filename": f0.get("filename"),
            "file_type": f0.get("file_type"),
            "size_bytes": f0.get("size_bytes"),
        }
    ctx = build_artefact_access_context(uid)
    visibility, visibility_label = collection_visibility(ctx, corpus_id)
    card = _catalog_card_stack_artefact(
        {**col, "file_count": file_count, "first_file": first_file},
        visibility=visibility,
        visibility_label=visibility_label,
        owner_label=owner_label_for(ctx, col),
        accessible=True,
    )
    return {"added": True, "card": card}


@router.delete("/artefacts/{corpus_id}", status_code=204)
async def remove_artefact_from_stack(
    corpus_id: str,
    user: dict = Depends(get_current_user),
):
    """Remove an artifact from the caller's Stack — drops agent access only.

    The artifact itself, its files, ownership and sharing are untouched;
    this only deletes the ``user_stack_subscriptions`` membership row. No
    "required" concept exists for artifacts, so there is no 400 case (unlike
    the data_package/memory_domain unsubscribe endpoint) — always 204.
    """
    _reject_co_session(user)
    from src.repositories import user_stack_subscriptions_repo

    user_stack_subscriptions_repo().unsubscribe(user["id"], ResourceType.COLLECTION.value, corpus_id)
    _emit_event(event_type="stack.artefact_remove", user=user, props={"resource_id": corpus_id})


@router.get("/artefacts/candidates")
async def stack_artefact_candidates(
    user: dict = Depends(get_current_user),
):
    """List every artifact eligible for the "Add artifacts to Stack" picker:
    collections accessible to the caller (owned, or shared with them/their
    team, or workspace-published) that are NOT already in their Stack.

    ``total_accessible`` counts every accessible artifact regardless of
    Stack membership, distinguishing "no artifacts exist at all" from "all
    accessible artifacts are already in your Stack" for the picker's empty
    states. Small dataset per caller in practice (mirrors /artefacts, which
    also fetches everything server-side) — the caller's search/visibility
    filter runs client-side over this list, no server-side ``q`` param.
    """
    _reject_co_session(user)
    from app.services.artefact_access import list_candidate_collections

    items, total_accessible = list_candidate_collections(user["id"])
    return {"items": items, "total_accessible": total_accessible}
