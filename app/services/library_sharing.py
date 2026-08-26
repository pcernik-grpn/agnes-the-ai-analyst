"""Owner-initiated sharing for Library items.

Every endpoint in ``app/api/access.py`` is gated by ``require_admin`` — that
layer models *admin-curated* RBAC (which groups get which governed data). It
deliberately does not let a normal user share something they created, so
before this module a user had no way to share their own uploads or agents.

This module is the owner-scoped counterpart: the person who CREATED a Library
item may grant it to groups **they themselves belong to**, plus the
``Everyone`` system group (workspace-wide). It writes the same
``resource_grants`` rows the admin layer does, so one access model stays in
force — the difference is only who may write which rows.

Two invariants make that safe:

  - **Ownership.** Only ``created_by == caller`` (or an admin) may change an
    item's sharing. Ownership is resolved per resource type by
    :data:`_OWNER_RESOLVERS`.
  - **Group containment.** A non-admin may only add or remove grants for
    groups in their own shareable set. Grants an admin made to some other
    group are preserved untouched across an owner's ``set_shares`` call, so an
    owner can never revoke access an admin deliberately granted, nor push an
    item into a team they aren't part of.

Skills are deliberately absent: a store entity is visible to every
authenticated user once approved, so a ``resource_grants`` row on one would be
dead mechanics — nothing reads it. The Library reflects a skill's real store
visibility instead.

That exclusion is settled for *grant* sharing, but it is NOT the whole story, and
the difference is a real gap rather than a decision:

  TODO — an owner cannot change their own store entity's ACCESS at all. A skill or
  plugin kept Private has no path to Everyone from any surface: ``access`` is
  accept-only at create (``POST /api/store/entities`` and its ``from-markdown``
  sibling), and ``PUT /api/store/entities/{id}`` carries no ``access`` field. So
  "share my private skill" is missing an endpoint, not a grant type — do not
  "fix" it by adding ``skill`` to ``_OWNER_RESOLVERS`` here, which would write
  exactly the dead row this module's exclusion exists to prevent.

  The endpoint owes: private→everyone re-entering the guardrail/LLM review
  pipeline as a fresh ``access=everyone`` upload does (landing on ``pending``,
  never flipping ``visibility_status`` straight to ``approved``);
  everyone→private as the cheap direction; owner-or-admin gating; DuckDB +
  Postgres repo siblings in one change; REST × CLI × MCP coverage. The Library's
  explains-only sharing badge (``library.html``, the ``{% else %}`` branch of the
  Sharing cell) is where the control lands once it exists.

``agent`` IS shareable here even though agents are not listed in the Library
(they live on ``/agents``): the grants are real because ``GET /api/v1/agents``
honours them, so a shared agent shows up in the grantee's own agent list. The
owner-facing UI for it is the admin ``/admin/access`` page today — an agent
Share control on ``/agents`` is the natural next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from app.resource_types import ResourceType

#: Resource types an owner may share through this module. Each maps to a
#: callable returning the owning user id for a resource id (or ``None`` when
#: the resource doesn't exist).
_OWNER_RESOLVERS: Dict[str, Callable[[str], Optional[str]]] = {}


def _collection_owner(resource_id: str) -> Optional[str]:
    from src.repositories import file_corpora_repo

    row = file_corpora_repo().get(resource_id)
    return row.get("created_by") if row else None


def _corpus_file_owner(resource_id: str) -> Optional[str]:
    """A file's owner is its parent collection's creator — a file has no
    independent ``created_by``, and whoever owns the folder owns what's in it."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    row = corpus_files_repo().get(resource_id)
    if not row:
        return None
    parent = file_corpora_repo().get(row.get("corpus_id") or "")
    return parent.get("created_by") if parent else None


def _agent_owner(resource_id: str) -> Optional[str]:
    from src.repositories import agents_repo

    # main's agents table is canonical after the combine: owner is
    # ``owner_user_id`` (the builder's ``created_by`` maps to it), and the
    # lookup is ``get_by_id`` (includes soft-deleted, fine for owner checks).
    row = agents_repo().get_by_id(resource_id)
    return row.get("owner_user_id") if row else None


def _data_app_owner(resource_id: str) -> Optional[str]:
    """``resource_id`` is the app's SLUG, not its row id — grants on this
    type are slug-keyed everywhere (``app.api.data_apps._can_view`` checks
    ``can_access(uid, 'data_app', row['slug'])``, and the admin layer writes
    the same rows), so the owner-sharing surface must key the same way or the
    grants it writes would be read by nothing.

    LINKED rows (``repo_mode='linked'``, synthetic ``system`` owner) are
    deliberately NOT carved out here, unlike the ``_reject_linked`` set on
    the data-apps control plane (deploy/secrets/logs/preview). Those refuse
    because they mutate or expose an upstream-managed runtime; sharing is
    visibility policy, which admins already control for linked apps via
    ``/admin/access`` writing the very same ``(data_app, <slug>)`` grant
    rows. No real user matches the ``system`` owner, so only an admin passes
    ``_require_owned`` — same authority, friendlier surface — and grants
    keyed on the reconciler's deterministic slug surviving a re-sync is the
    point, not a leak (Devin Review on #1321)."""
    from src.repositories import data_apps_repo

    row = data_apps_repo().get_by_slug(resource_id)
    return row.get("owner_user_id") if row else None


_OWNER_RESOLVERS[ResourceType.COLLECTION.value] = _collection_owner
_OWNER_RESOLVERS[ResourceType.AGENT.value] = _agent_owner
_OWNER_RESOLVERS[ResourceType.CORPUS_FILE.value] = _corpus_file_owner
_OWNER_RESOLVERS[ResourceType.DATA_APP.value] = _data_app_owner

#: Human labels for the shareable types, used in error messages.
SHAREABLE_TYPES: Dict[str, str] = {
    ResourceType.COLLECTION.value: "artefact",
    ResourceType.AGENT.value: "agent",
    ResourceType.CORPUS_FILE.value: "file",
    ResourceType.DATA_APP.value: "app",
}


@dataclass(frozen=True)
class ShareTarget:
    """A group the caller may share into."""

    id: str
    name: str
    is_everyone: bool

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "is_everyone": self.is_everyone}


def is_shareable(resource_type: str) -> bool:
    """Whether owner-initiated sharing is defined for this resource type."""
    return resource_type in _OWNER_RESOLVERS


def resolve_owner(resource_type: str, resource_id: str) -> Optional[str]:
    """Owning user id for a resource, or ``None`` if it doesn't exist."""
    resolver = _OWNER_RESOLVERS.get(resource_type)
    return resolver(resource_id) if resolver else None


def share_targets(user_id: str, *, is_admin: bool = False) -> List[ShareTarget]:
    """Groups ``user_id`` may share into.

    The caller's own group memberships plus ``Everyone`` (workspace-wide).
    ``Everyone`` is listed first — it is the "publish to the whole workspace"
    choice and is auto-membership, so every caller is legitimately in it.
    An admin may share into any group, matching their god-mode elsewhere.
    """
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_group_members_repo, user_groups_repo

    everyone_id: Optional[str] = None
    targets: List[ShareTarget] = []
    try:
        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        if everyone:
            everyone_id = everyone["id"]
            targets.append(ShareTarget(id=everyone["id"], name="Everyone (workspace)", is_everyone=True))
    except Exception:
        pass

    try:
        if is_admin:
            groups = [dict(g) for g in user_groups_repo().list_all()]
        else:
            groups = [dict(g) for g in user_group_members_repo().list_groups_with_meta_for_user(user_id)]
    except Exception:
        groups = []

    for g in groups:
        gid = g.get("id") or g.get("group_id")
        if not gid or gid == everyone_id:
            continue  # Everyone already leads the list
        targets.append(ShareTarget(id=gid, name=g.get("name") or gid, is_everyone=False))
    return targets


def shareable_group_ids(user_id: str, *, is_admin: bool = False) -> Set[str]:
    """Id set of :func:`share_targets` — the containment check for writes."""
    return {t.id for t in share_targets(user_id, is_admin=is_admin)}


def current_share_group_ids(resource_type: str, resource_id: str) -> Set[str]:
    """Group ids currently granted this resource (any writer)."""
    from src.repositories import resource_grants_repo

    try:
        return {
            g["group_id"]
            for g in resource_grants_repo().list_all(resource_type=resource_type)
            if g["resource_id"] == resource_id
        }
    except Exception:
        return set()


def visibility_for(resource_type: str, resource_id: str) -> str:
    """``private`` | ``shared`` | ``workspace`` for one resource.

    Mirrors ``app/services/artefact_access.py::collection_visibility`` — an
    ``Everyone`` grant means workspace-wide, any other grant means shared with
    a specific group, no grant at all means private.
    """
    from src.db import SYSTEM_EVERYONE_GROUP
    from src.repositories import user_groups_repo

    granted = current_share_group_ids(resource_type, resource_id)
    if not granted:
        return "private"
    try:
        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        if everyone and everyone["id"] in granted:
            return "workspace"
    except Exception:
        pass
    return "shared"


def set_shares(
    *,
    resource_type: str,
    resource_id: str,
    group_ids: List[str],
    actor_id: str,
    is_admin: bool = False,
) -> Dict[str, Any]:
    """Replace the caller-controllable grants on a resource.

    ``group_ids`` is the desired end state *within the caller's shareable
    set*. Grants to groups outside that set (e.g. one an admin made) are left
    alone — an owner can neither revoke nor forge them.

    Returns ``{visibility, group_ids, added, removed}``. Raises ``ValueError``
    with a stable machine token when a requested group isn't shareable by this
    caller.
    """
    from src.repositories import resource_grants_repo

    allowed = shareable_group_ids(actor_id, is_admin=is_admin)
    requested = {g for g in group_ids if g}
    forbidden = requested - allowed
    if forbidden:
        raise ValueError("group_not_shareable")

    grants_repo = resource_grants_repo()
    existing = current_share_group_ids(resource_type, resource_id)

    # Only touch the intersection with `allowed`; everything else is preserved.
    to_add = requested - existing
    to_remove = (existing & allowed) - requested

    for gid in sorted(to_add):
        grants_repo.ensure_grant(gid, resource_type, resource_id, assigned_by=actor_id)
    for gid in sorted(to_remove):
        for g in grants_repo.list_all(resource_type=resource_type, group_id=gid):
            if g["resource_id"] == resource_id:
                grants_repo.delete(g["id"])

    return {
        "visibility": visibility_for(resource_type, resource_id),
        "group_ids": sorted(current_share_group_ids(resource_type, resource_id)),
        "added": sorted(to_add),
        "removed": sorted(to_remove),
    }
