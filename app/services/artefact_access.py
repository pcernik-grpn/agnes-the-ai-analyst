"""Shared caller-scoped access computation for artefacts (``file_corpora``).

Three surfaces need the exact same "which collections can I see, and how
are they shared" computation:

  - ``GET /artefacts``            (app/web/router.py::artefacts_page)
  - ``GET /stack``'s Artefacts tab (app/web/router.py::my_stack_page)
  - ``GET /api/stack/artefacts/candidates`` (the "Add artefacts" picker)

This module is the single place that computes it, instead of three copies
of the ``resource_grants`` + ``file_corpora`` joins. Permission model is
ownership/sharing, NOT the admin-RBAC-grant tier ``StackResolver`` models
for data packages/memory domains — there is no "required" concept for
artefacts, so this deliberately does not route through ``StackResolver``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from app.resource_types import ResourceType


@dataclass
class ArtefactAccessContext:
    """Caller-scoped snapshot of collection sharing state."""

    uid: str
    granted_to_me: Set[str] = field(default_factory=set)
    """Collection ids granted to one of the caller's groups."""
    shared_ids: Set[str] = field(default_factory=set)
    """Collection ids carrying ANY grant (shared by their owner)."""
    workspace_ids: Set[str] = field(default_factory=set)
    """Collection ids granted to the ``Everyone`` system group."""
    owner_name: Dict[str, str] = field(default_factory=dict)
    """user_id -> display name, for the owner/"Shared by" columns."""


def build_artefact_access_context(user_id: str) -> ArtefactAccessContext:
    """Compute the caller's artefact-sharing snapshot in one pass.

    Reads through the ``src.repositories`` factory (never a raw connection)
    so this works identically on the DuckDB and Postgres backends.
    """
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import resource_grants_repo, user_groups_repo, users_repo

    ct = ResourceType.COLLECTION.value
    grants_repo = resource_grants_repo()

    try:
        granted_to_me = set(grants_repo.list_resource_ids_for_user(user_id, ct))
    except Exception:
        granted_to_me = set()

    shared_ids: Set[str] = set()
    workspace_ids: Set[str] = set()
    try:
        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        everyone_id = everyone["id"] if everyone else None
        for g in grants_repo.list_all(resource_type=ct):
            shared_ids.add(g["resource_id"])
            if everyone_id and g["group_id"] == everyone_id:
                workspace_ids.add(g["resource_id"])
    except Exception:
        pass

    owner_name: Dict[str, str] = {}
    try:
        for u in users_repo().list_all():
            owner_name[u["id"]] = u.get("name") or u.get("email") or "Someone"
    except Exception:
        pass

    return ArtefactAccessContext(
        uid=user_id,
        granted_to_me=granted_to_me,
        shared_ids=shared_ids,
        workspace_ids=workspace_ids,
        owner_name=owner_name,
    )


#: The user-facing sharing vocabulary, and the SINGLE SOURCE OF TRUTH for it.
#: ``visibility_label`` travels onto every Library row from here, and both the
#: table badge and the grid card print it verbatim — see ``SHARING_LABEL`` in
#: ``library.html`` for the small client-side mirror (needed only to relabel a
#: badge in place after the share dialog saves, without a reload).
#:
#: Every label answers ONE question — *who can see this* — so the set reads as a
#: single axis rather than three unrelated words.
#:
#: "Everyone" rather than "Workspace" deliberately: ``workspace`` is the
#: internal key, and the product calls that scope the *organization* everywhere
#: it speaks to a user (the Library lede, the Organization trust marker, the
#: skill builder). "Workspace" was the enum leaking into the UI, and it was the
#: odd one out grammatically — "Private" and "Specific groups" answer *who*,
#: while "Workspace" named a container.
#:
#: Whether the caller can CHANGE the state is deliberately NOT encoded in these
#: words: that rides the control's form (a bordered chip with a chevron where
#: the state is changeable, bare text where it is not). One question per
#: channel — the previous design answered "can I change this?" by overwriting
#: the *who* answer, which is how a workspace-published skill someone else owns
#: came to read "Shared with you" on its card and "Workspace" on its own row.
VISIBILITY_LABELS: dict[str, str] = {
    "private": "Private",
    "shared": "Specific groups",
    "workspace": "Everyone",
}


def collection_visibility(ctx: ArtefactAccessContext, collection_id: str) -> Tuple[str, str]:
    """``(visibility_key, visibility_label)`` for one collection.

    Mirrors the Everyone-grant convention already used for Slack channels
    (see ``app/resource_types.py::_slack_channel_blocks``): any grant to the
    ``Everyone`` group means "published workspace-wide"; any other grant
    means "shared" (with a specific group); no grant at all means "private".
    This is independent of ownership — a workspace-published collection you
    don't own is still "Everyone", not "Specific groups".

    Labels come from :data:`VISIBILITY_LABELS`; see it for the vocabulary.
    """
    if collection_id in ctx.workspace_ids:
        key = "workspace"
    elif collection_id in ctx.shared_ids:
        key = "shared"
    else:
        key = "private"
    return key, VISIBILITY_LABELS[key]


def owner_label_for(ctx: ArtefactAccessContext, col: dict) -> str:
    """ "You" for the caller's own artefacts, else the owner's display name."""
    created_by = col.get("created_by")
    if created_by == ctx.uid:
        return "You"
    return ctx.owner_name.get(created_by or "", "Someone")


def is_owned_or_shared_with_me(ctx: ArtefactAccessContext, col: dict) -> bool:
    """Owned-by-caller OR granted to one of the caller's groups — the same
    "can I see this on /artefacts" scope (deliberately not admin god-mode)."""
    return col.get("created_by") == ctx.uid or col["id"] in ctx.granted_to_me


def _artefact_type_label(file_count: int) -> str:
    return "File" if file_count == 1 else "Collection"


def list_candidate_collections(user_id: str) -> Tuple[List[dict], int]:
    """Candidates for the "Add artefacts to Stack" picker.

    Returns ``(candidates, total_accessible)``: ``candidates`` are
    collections accessible to the caller (owned ∪ granted to one of their
    groups) that are NOT already in the caller's Stack, shaped for the
    picker row. ``total_accessible`` counts every accessible collection
    regardless of Stack membership, so the caller can distinguish "no
    artefacts exist at all" from "all accessible artefacts are already
    added" (both render a picker empty state, but with different copy).
    """
    from src.repositories import (
        corpus_files_repo,
        file_corpora_repo,
        user_stack_subscriptions_repo,
    )

    ctx = build_artefact_access_context(user_id)
    fc_repo = file_corpora_repo()
    cf_repo = corpus_files_repo()

    try:
        in_stack = set(user_stack_subscriptions_repo().list_for_user(user_id, ResourceType.COLLECTION.value))
    except Exception:
        in_stack = set()

    accessible = [col for col in fc_repo.list() if is_owned_or_shared_with_me(ctx, col)]

    # One grouped count for every corpus, rather than reading every row of
    # every accessible collection to arrive at a per-collection number.
    try:
        file_counts = cf_repo.count_by_corpus()
    except Exception:
        file_counts = {}

    candidates: List[dict] = []
    for col in accessible:
        if col["id"] in in_stack:
            continue
        candidates.append(_candidate_shape(col, ctx, file_counts.get(col["id"], 0)))

    return candidates, len(accessible)


def _candidate_shape(col: dict, ctx: ArtefactAccessContext, file_count: int) -> dict:
    visibility, visibility_label = collection_visibility(ctx, col["id"])
    owned = col.get("created_by") == ctx.uid
    updated = col.get("updated_at") or col.get("created_at")
    return {
        "id": col["id"],
        "title": col.get("name") or col.get("slug"),
        "type_label": _artefact_type_label(file_count),
        "description": col.get("description") or "",
        "owner_label": owner_label_for(ctx, col),
        "mine": owned,
        "visibility": visibility,
        "visibility_label": visibility_label,
        "updated_iso": updated.isoformat() if updated is not None else None,
        "href": f"/library/{col.get('slug')}",
    }
