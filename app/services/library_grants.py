"""Granted Library content for the kinds ``StackResolver`` does not serve.

Two surfaces answer "what does this person's Library show" and they must never
disagree: ``/library`` renders it for the person themselves
(``app.web.router.library_page``) and the admin Simulate lens previews it for
somebody else (``GET /api/admin/users/{user_id}/library-preview``). For data
packages and memory domains they already share one projection —
:meth:`StackResolver.browse`. For the OTHER granted kinds there was no shared
definition, only the page's own inline derivation, which is why the preview
could answer "I see it in admin but they don't see it in their Library" for
governed data and stay silent about a curated plugin — the same question about
a different row.

This module is that missing definition, for the two kinds
``StackResolver._fetch_entries`` refuses (it raises ``ValueError`` for
anything but the two governed types): curated **marketplace plugins** and
**recipes**. Data packages and memory domains are deliberately NOT here —
copying the resolver's output into this shape would be the same drift with an
extra step.

Nothing here is admin-aware. Every read is grants-based for the NAMED user, so
a preview of somebody's Library can never show more than that person sees —
the Library's no-god-mode contract applies to a preview OF a person exactly as
it does to the person themselves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.resource_types import ResourceType

logger = logging.getLogger(__name__)


@dataclass
class GrantedItem:
    """One granted row, with the state both surfaces render off it.

    ``id`` is the id the grant carries (a recipe id; for a plugin the
    canonical ``"<marketplace_id>/<plugin_name>"`` path). ``requirement`` is
    the wire tier (``'required'`` | ``'available'``); ``in_stack`` means the
    person can use it as-is; ``materialized`` means a copy actually reaches
    their machine. ``row`` is the untouched repo row, for callers that render
    more than this (the Library needs the category, trust marks and the
    install endpoint).
    """

    id: str
    name: str
    description: str | None = None
    href: str | None = None
    requirement: str = "available"
    in_stack: bool = False
    materialized: bool = False
    row: Dict[str, Any] = field(default_factory=dict)


def _granted_ids(user_id: str, resource_type: ResourceType) -> set:
    """Resource ids of *resource_type* granted to any of the person's groups."""
    from src.repositories import resource_grants_repo

    return set(resource_grants_repo().list_resource_ids_for_user(user_id, resource_type.value))


def granted_plugins(user_id: str) -> List[GrantedItem]:
    """Curated marketplace plugins the person's groups reach.

    A plugin is the one granted kind whose membership is NOT automatic: the
    grant is eligibility, and ``resolve_user_marketplace`` serves
    ``subscriptions ∪ required-tier grants``. So an available-tier plugin
    nobody installed is genuinely absent from their Claude Code — ``in_stack``
    says so, derived from ``_curated_stack_sets``, the same helper
    ``GET /api/marketplace/items`` computes its ``installed`` flag from.

    ``admin_disabled`` rows are dropped: that flag is instance-wide "does not
    exist" for every user-facing surface, grants notwithstanding.
    """
    paths = _granted_ids(user_id, ResourceType.MARKETPLACE_PLUGIN)
    if not paths:
        return []

    from app.api.marketplace import _curated_stack_sets
    from src.repositories import marketplace_plugins_repo

    # `conn=None`: the DuckDB fast path is optional, and neither caller holds a
    # request-scoped connection — passing one would be the backend-split bug
    # class on a Postgres instance.
    in_stack_keys, required_keys = _curated_stack_sets(None, user_id)

    items: List[GrantedItem] = []
    for row in marketplace_plugins_repo().list_all():
        if row.get("admin_disabled"):
            continue
        marketplace_id, plugin_name = row.get("marketplace_id"), row.get("name")
        path = f"{marketplace_id}/{plugin_name}"
        if path not in paths:
            continue
        key = (marketplace_id, plugin_name)
        served = key in in_stack_keys
        items.append(
            GrantedItem(
                id=path,
                name=row.get("display_name") or row.get("name") or path,
                description=row.get("description"),
                href=f"/marketplace/curated/{marketplace_id}/{plugin_name}",
                requirement=("required" if key in required_keys else "available"),
                in_stack=served,
                # A served plugin IS delivered: `agnes update` writes it into
                # the workspace off the same aggregated marketplace. There is
                # no third state where it is theirs but not on their machine.
                materialized=served,
                row=row,
            )
        )
    return items


def granted_recipes(user_id: str) -> List[GrantedItem]:
    """Recipes the person's groups reach.

    A recipe has no tier and no subscription — analysts use a recipe, they
    don't opt in — so the grant IS the reading right (``in_stack``) and there
    is nothing to download (``agnes pull`` distributes data, not recipes).
    """
    ids = _granted_ids(user_id, ResourceType.RECIPE)
    if not ids:
        return []

    from src.repositories import recipes_repo

    return [
        GrantedItem(
            id=row["id"],
            name=row.get("title") or row.get("slug") or row["id"],
            description=row.get("description"),
            href=f"/catalog/r/{row.get('slug') or row['id']}",
            requirement="available",
            in_stack=True,
            materialized=False,
            row=row,
        )
        for row in recipes_repo().list(limit=100000)
        if row["id"] in ids
    ]
