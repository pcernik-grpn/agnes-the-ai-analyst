"""StackResolver — unified browse + stack + local-download resolver.

Scope: ``DATA_PACKAGE`` + ``MEMORY_DOMAIN`` resource types, plus a
``MEMORY_ITEM`` helper for item-level Required override. Marketplace
plugins keep their own resolver in ``src/marketplace_filter.py`` per
design D1.

Auto-membership model: stack membership is a pure function of RBAC grants,
not of any per-user opt-in. Every resource granted to one of the caller's
groups — ``required`` or ``available`` — is automatically part of the
caller's stack (visible, authorized for server-side query). This replaced
an earlier opt-in "subscribe to add" model where an ``available`` grant
stayed invisible until the user explicitly subscribed.

``user_stack_subscriptions`` survives unchanged (same table, same columns)
but its MEANING is reinterpreted: a row no longer controls stack
membership/visibility — it controls whether the resource is additionally
**materialized** (kept as a local copy `agnes pull` downloads to disk).
``required`` resources are always materialized (no opt-out); ``available``
resources are materialized only once subscribed. Server-side query
(``agnes query --scope auto`` falling back to remote execution) works for
anything in the stack regardless of materialization — a local copy is a
convenience, not a precondition for access.

Resolution algorithm:

    groups          := user_group_members(user_id).group_id
    grants          := resource_grants WHERE group_id IN groups AND resource_type = T
    required_ids    := {g.resource_id | g in grants if g.requirement = 'required'}
    available_ids   := {g.resource_id | g in grants if g.requirement = 'available'}
    # auto-membership mode (features.stack_auto_membership, opt-in):
    effective_ids   := required_ids ∪ available_ids                # stack() / in_stack
    materialized_ids := required_ids ∪ (subscribed_ids ∩ available_ids)  # local download
    # classic mode (the default — the pre-redesign subscribe model):
    effective_ids   := required_ids ∪ (subscribed_ids ∩ available_ids)   # membership
    materialized_ids := effective_ids                                    # members are local
    return fetch_entries(T, effective_ids)  # each entry.materialized = id in materialized_ids

Required precedence: any ``required`` grant beats every ``available`` grant
for the same (user, resource_id) pair. We compute this by set-union: an id
present in ``required_ids`` is required regardless of what other grants on
the same id say.

Admin god-mode: on top of the grants-driven ``materialized_ids`` above, an
admin's own raw ``user_stack_subscriptions`` rows also count even without a
backing grant — admins can self-serve a local copy of a resource their own
groups were never granted (the historical "Add to stack worked but My
Stack stayed empty" fix). This does not extend to non-admins.

Memory item-level Required precedence: per-group MEMORY_ITEM grants
override the global ``knowledge_items.is_required`` flag. See the
``memory_item_is_required`` method docstring for the full rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import HTTPException

from app.resource_types import ResourceType


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


# ── Lifecycle-status gating ────────────────────────────────────────────────
# The status picker makes two promises, and until v114 kept neither: `status`
# drove a pill and a hero filter and nothing else, so a granted `draft`
# package landed in the analyst's Library fully materialized and a
# `coming-soon` one delivered parquets like any other row.
#
# Both promises are kept HERE rather than at each caller, because
# `_fetch_entries` is the one chokepoint every analyst-facing read runs
# through — `browse()`, `stack()`, the Principal (agent/co-session) path, and
# therefore the pull manifest too, which `app/api/sync.py` builds out of
# `resolver.stack`. Gating once is what makes the label true on every surface
# at the same time; gating per caller is how they drifted apart before.
#
# `browse_admin()` deliberately opts out (`include_hidden=True`) — an admin
# authors drafts and has to see them.

#: "admin-only, hidden from analysts" — never reaches a non-admin surface.
HIDDEN_STATUSES = frozenset({"draft"})

#: "visible but not usable yet" — browsable, but never a local copy and never
#: in the pull manifest. A different axis from HIDDEN_STATUSES: the entry is
#: still returned by `browse()` so the card and its "Coming soon" pill render.
UNDELIVERABLE_STATUSES = frozenset({"coming-soon"})


@dataclass
class ResourceEntry:
    """One row in the browse/stack response.

    ``requirement`` reflects the effective requirement after the OR-across-
    grants rule. ``in_stack`` is True iff the resource is granted to one of
    the caller's groups at all (auto-membership — ``required`` and
    ``available`` both count, no subscription needed). ``materialized`` is
    True iff the resource is ALSO kept as a local copy (`agnes pull`
    downloads its parquet / bundle): always True for ``required``, and for
    ``available`` only once the user has subscribed via
    ``POST /api/stack/subscribe``.
    """

    id: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    # v50: optional admin-uploaded cover image URL (served from /uploads/).
    # When set the card renders an <img>; when None the card falls back to
    # the flat-color + initials banner. Symmetric for Data Packages and
    # Memory Domains.
    cover_image_url: Optional[str] = None
    # v51: lifecycle ``status`` (drives hero filter checkboxes + cover
    # status pill) and ``category`` (drives card eyebrow line — Data
    # Packages only; Memory Domains pass None).
    status: Optional[str] = "prod"
    category: Optional[str] = None
    # v56: extended content surfaced on the Browse-grid card. Owner
    # renders as a small chip; tags as inline pills; ``badges`` holds the
    # DERIVED ones only — as of v113 that is ``new`` alone, from
    # ``created_at`` age in :meth:`_fetch_entries`.
    owner_name: Optional[str] = None
    owner_team: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    badges: List[str] = field(default_factory=list)
    # v113: the STORED trust claim, passed through so a card renders the same
    # shared marker as the row and the detail hero. It replaced a `curated`
    # badge derived here from the creator's CURRENT Admin-group membership —
    # a fact about the person, not the resource, so it contradicted the
    # detail page whenever that membership changed. Memory domains carry no
    # publisher axis and leave this None (the macro then renders nothing).
    publisher_kind: Optional[str] = None
    requirement: Literal["available", "required"] = "available"
    in_stack: bool = False
    materialized: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


class StackResolver:
    """Composes ``resource_grants`` ∪ ``user_stack_subscriptions`` →
    effective stack per resource type.

    Backend-aware: every read/write routes through the repository factory
    (``src.repositories``), so the resolver hits whichever backend
    (DuckDB or Postgres) the process is configured for.

    The legacy ``conn`` argument is retained as a *test-isolation escape
    hatch*: when the active backend is DuckDB **and** a DuckDB connection
    is supplied, the resolver reads/writes through that connection (so a
    unit test seeding an in-memory ``:memory:`` DuckDB sees its own data).
    When the backend is Postgres the ``conn`` is ignored and the factory
    routes to PG. This mirrors the same pattern in ``app.auth.access``
    (``_user_group_ids`` / ``can_access``).
    """

    def __init__(self, conn: Any = None):
        self.conn = conn

    # -- Repo accessors (honor the conn escape hatch) ----------------------

    def _use_local_conn(self) -> bool:
        """True iff we should read/write through ``self.conn`` directly.

        Only when a connection was supplied AND the active backend is
        DuckDB — otherwise the factory owns backend selection.
        """
        if self.conn is None:
            return False
        from src.repositories import use_pg

        return not use_pg()

    def _members_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            return UserGroupMembersRepository(self.conn)
        from src.repositories import user_group_members_repo

        return user_group_members_repo()

    def _grants_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.resource_grants import ResourceGrantsRepository

            return ResourceGrantsRepository(self.conn)
        from src.repositories import resource_grants_repo

        return resource_grants_repo()

    def _subscriptions_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.user_stack_subscriptions import (
                UserStackSubscriptionsRepository,
            )

            return UserStackSubscriptionsRepository(self.conn)
        from src.repositories import user_stack_subscriptions_repo

        return user_stack_subscriptions_repo()

    def _groups_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.user_groups import UserGroupsRepository

            return UserGroupsRepository(self.conn)
        from src.repositories import user_groups_repo

        return user_groups_repo()

    def _data_packages_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.data_packages import DataPackagesRepository

            return DataPackagesRepository(self.conn)
        from src.repositories import data_packages_repo

        return data_packages_repo()

    def _memory_domains_repo(self) -> Any:
        if self._use_local_conn():
            from src.repositories.memory_domains import MemoryDomainsRepository

            return MemoryDomainsRepository(self.conn)
        from src.repositories import memory_domains_repo

        return memory_domains_repo()

    # -- Group + grant lookups (private) -----------------------------------

    def _user_group_ids(self, user_id: str) -> List[str]:
        return self._members_repo().list_groups_for_user(user_id)

    def _grants(self, group_ids: List[str], resource_type: ResourceType) -> Tuple[set, set]:
        """Split (required, available) resource_id sets for the user's groups.

        Empty group_ids → ({}, {}); the resolver short-circuits to "no
        entries" for both browse() and stack().
        """
        if not group_ids:
            return set(), set()
        rows = self._grants_repo().list_for_groups(list(group_ids), str(resource_type))
        required_ids = {r["resource_id"] for r in rows if r.get("requirement") == "required"}
        available_ids = {r["resource_id"] for r in rows if r.get("requirement") == "available"}
        # Required-beats-available OR rule — if an id appears in both
        # buckets across grants, the required one wins. Remove it from
        # available to keep the union/intersection math clean everywhere
        # downstream (effective_ids, materialized_ids).
        available_ids -= required_ids
        return required_ids, available_ids

    def _subscribed_ids(self, user_id: str, resource_type: ResourceType) -> set:
        return set(self._subscriptions_repo().list_for_user(user_id, str(resource_type)))

    # -- Public API --------------------------------------------------------

    def stack(self, user_id_or_principal, resource_type: ResourceType) -> List[ResourceEntry]:
        """The user's effective stack — auto-membership: required ∪ available,
        every grant on the caller's groups, no subscription needed to be
        visible/authorized. ``entry.materialized`` additionally flags which
        of those are kept as a local copy (required always; available only
        once subscribed). Admin (god-mode) ALSO surfaces raw subscriptions
        with no backing grant at all — admins legitimately POST
        /api/stack/subscribe without first granting themselves a group, and
        those self-served resources must still show up (materialized) even
        though they're not in ``required_ids``/``available_ids``.

        Also accepts a ``Principal`` (co-session or agent-session): returns
        only the resources whose ids are in that principal's intersection (no
        admin path, no subscription lookup). Every returned entry is marked
        ``in_stack=True``, ``materialized=True``.
        """
        from app.auth.session_principal import PRINCIPAL_TYPES

        if isinstance(user_id_or_principal, PRINCIPAL_TYPES):
            ids = user_id_or_principal.intersection.get(resource_type.value, frozenset())
            entries = self._fetch_entries(resource_type, set(ids), set(ids))
            # A principal's intersection is derived from its owner's grants,
            # so it inherits the same gate: an agent cannot reach a draft its
            # owner cannot see, nor deliver one that is not usable yet.
            entries = [e for e in entries if e.status not in UNDELIVERABLE_STATUSES]
            for e in entries:
                e.in_stack = True
                e.materialized = True
            return entries
        user_id = user_id_or_principal
        groups = self._user_group_ids(user_id)
        required_ids, available_ids = self._grants(groups, resource_type)
        raw_subscribed = self._subscribed_ids(user_id, resource_type)
        # Admin god-mode: zombie-subscription protection doesn't apply —
        # admin sees all their actual subscriptions even without a grant.
        from app.auth.access import is_user_admin

        admin_bypass = is_user_admin(user_id, self.conn)
        materialized_ids = raw_subscribed if admin_bypass else (raw_subscribed & available_ids)
        from app.instance_config import get_stack_auto_membership

        if get_stack_auto_membership():
            # Auto-membership: every granted id is in the stack regardless of
            # materialization. Admin god-mode additionally surfaces raw
            # subscriptions that have no backing grant (self-serve local copy).
            effective_ids = required_ids | available_ids | materialized_ids
        else:
            # Classic subscribe model (spec 2026-08-07-default-chrome-ux-parity,
            # the default): membership is required ∪ (subscribed ∩ available) —
            # the pre-redesign formula verbatim. Admin god-mode still surfaces
            # raw self-served subscriptions via materialized_ids above. Every
            # member is local, so the materialized flag below stays truthful.
            effective_ids = required_ids | materialized_ids
        entries = self._fetch_entries(resource_type, effective_ids, required_ids)
        # `app/api/sync.py::_build_data_packages_section` builds the pull
        # manifest from this method, so dropping the undeliverable statuses
        # here is what stops a "coming soon" package shipping parquets.
        entries = [e for e in entries if e.status not in UNDELIVERABLE_STATUSES]
        for e in entries:
            # In stack() every entry is by definition in_stack=True.
            e.in_stack = True
            e.materialized = e.id in required_ids or e.id in materialized_ids
        return entries

    def browse(self, user_id: str, resource_type: ResourceType) -> List[ResourceEntry]:
        """All resources the user could see — required + available. Every
        entry is ``in_stack=True`` (auto-membership); ``materialized`` tells
        the UI whether it's ALSO downloaded locally, driving the
        Download/Remove-local-copy affordance. Admin uses
        :meth:`browse_admin` for the full list; this method stays
        grants-based so non-admin browse is correct."""
        groups = self._user_group_ids(user_id)
        required_ids, available_ids = self._grants(groups, resource_type)
        all_ids = required_ids | available_ids
        subscribed_ids = self._subscribed_ids(user_id, resource_type)
        entries = self._fetch_entries(resource_type, all_ids, required_ids)
        from app.instance_config import get_stack_auto_membership

        auto = get_stack_auto_membership()
        for e in entries:
            # Auto-membership: every grant IS a membership; `materialized`
            # drives the Download/Remove-local-copy affordance. Classic (the
            # default): an unsubscribed available grant reads as ADDABLE
            # (in_stack=False) — the pre-redesign add-to-stack affordance.
            e.materialized = e.id in required_ids or e.id in subscribed_ids
            e.in_stack = True if auto else e.materialized
            if e.status in UNDELIVERABLE_STATUSES:
                # "Visible but not usable yet": the card renders with its
                # Coming-soon pill, but there is no local copy to offer and
                # stack() has already refused to put it in the manifest.
                e.materialized = False
        return entries

    def browse_admin(self, user_id: str, resource_type: ResourceType) -> List[ResourceEntry]:
        """Admin god-mode Browse: ALL entries of ``resource_type`` with
        v51/v56 enrichment (status, category, owner_name, tags, badges).

        ``requirement`` reflects the admin's OWN group grants — required
        packages are still rendered with the disabled "In stack
        (required)" footer button so the admin sees what regular users
        in those groups see, and the macro doesn't render an actionable
        Remove button that the API would 400 on. ``in_stack`` is
        mode-dependent like :meth:`browse`: classic (default) is the
        pre-redesign formula — required or subscribed; auto-membership
        additionally counts the admin's own ``available`` grants (plus
        raw subscriptions — mirrors :meth:`stack`'s admin god-mode),
        with ``materialized`` reflecting only required-or-subscribed.
        """
        # Soft-deleted entries (``deleted_at IS NOT NULL``) are excluded
        # from admin Browse — they're still in the DB for the Undo
        # window but a /catalog or /memory render mustn't surface them.
        if resource_type == ResourceType.DATA_PACKAGE:
            all_ids = {r["id"] for r in self._data_packages_repo().list(limit=100000)}
        elif resource_type == ResourceType.MEMORY_DOMAIN:
            all_ids = {r["id"] for r in self._memory_domains_repo().list(limit=100000)}
        else:
            raise ValueError(f"browse_admin does not support resource_type={resource_type!r}")
        groups = self._user_group_ids(user_id)
        required_ids, available_ids = self._grants(groups, resource_type)
        subscribed_ids = self._subscribed_ids(user_id, resource_type)
        # Admins author drafts, so their own Browse is the one surface that
        # must still show them (see HIDDEN_STATUSES).
        entries = self._fetch_entries(resource_type, all_ids, required_ids, include_hidden=True)
        from app.instance_config import get_stack_auto_membership

        auto = get_stack_auto_membership()
        for e in entries:
            if auto:
                e.in_stack = e.id in required_ids or e.id in available_ids or e.id in subscribed_ids
                e.materialized = e.id in required_ids or e.id in subscribed_ids
            else:
                # Classic subscribe model — pre-redesign formula verbatim
                # (spec 2026-08-07-default-chrome-ux-parity).
                e.in_stack = e.id in required_ids or e.id in subscribed_ids
                e.materialized = e.in_stack
        return entries

    def is_required(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
    ) -> bool:
        """True iff ANY of the user's groups has a ``required`` grant for
        this resource (required-beats-available OR rule)."""
        groups = self._user_group_ids(user_id)
        required_ids, _ = self._grants(groups, resource_type)
        return resource_id in required_ids

    def add_to_stack(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """Subscribe the user to an ``available`` resource — i.e. request a
        LOCAL DOWNLOAD (materialization). The resource is already in the
        user's stack the moment it's granted (auto-membership); this call
        does not change that, it only flips ``entry.materialized`` so
        `agnes pull` fetches a local copy.

        Raises HTTP 400 if the resource is already ``required`` — clients
        shouldn't try to subscribe to a required resource (it's always
        materialized already).

        NOTE: this method does NOT verify the user has an ``available``
        grant for the resource. Authorization is enforced at the API
        layer by ``app/api/stack.py``'s ``can_access`` gate. Direct
        in-process callers (tests, admin scripts) are trusted to have
        gated themselves; ``stack()`` further hides any resulting
        subscription from ``materialized`` on every read by intersecting
        with current available_ids, so a zombie row (grant later revoked)
        never leaks a stale "downloaded" flag into the user-facing
        manifest — the resource itself is already excluded from the stack
        entirely once the grant is gone.
        """
        if self.is_required(user_id, resource_type, resource_id):
            raise HTTPException(status_code=400, detail="already_required")
        self._subscriptions_repo().subscribe(user_id, str(resource_type), resource_id)

    def remove_from_stack(
        self,
        user_id: str,
        resource_type: ResourceType,
        resource_id: str,
    ) -> None:
        """Drop the subscription.

        Raises HTTP 400 if the resource is ``required`` — users can't opt
        out of required grants.
        """
        if self.is_required(user_id, resource_type, resource_id):
            raise HTTPException(status_code=400, detail="cannot_remove_required")
        self._subscriptions_repo().unsubscribe(user_id, str(resource_type), resource_id)

    # -- Memory item-level resolver -----------------------------------------

    def memory_item_is_required(
        self,
        user_id: str,
        item_id: str,
        item_is_required: bool,
    ) -> bool:
        """Per-user effective is_required flag for a single memory item.

        Precedence (top-down):
        1. Any group grant ``MEMORY_ITEM, required`` for this item → True
        2. Any group grant ``MEMORY_ITEM, available`` for this item → False
           (per-group override "this item is NOT required for our group")
        3. Item's global ``knowledge_items.is_required = TRUE`` → True
        4. Otherwise → False

        The required→available precedence within the per-group layer
        follows the same required-beats-available OR rule as the
        DATA_PACKAGE/MEMORY_DOMAIN resolver above. Both required and
        available per-group grants override the global flag.
        """
        groups = self._user_group_ids(user_id)
        if not groups:
            return item_is_required
        rows = [
            r for r in self._grants_repo().list_for_groups(list(groups), "memory_item") if r["resource_id"] == item_id
        ]
        if not rows:
            return item_is_required
        # Per-group grants exist → they override the global flag.
        # Within the per-group layer, required wins over available.
        requirements = {r.get("requirement") for r in rows}
        return "required" in requirements

    # -- Domain entry fetch (private) --------------------------------------

    def _fetch_entries(
        self,
        resource_type: ResourceType,
        ids: set,
        required_ids: set,
        include_hidden: bool = False,
    ) -> List[ResourceEntry]:
        """Rows for *ids*, gated by lifecycle status.

        ``include_hidden`` is the admin escape hatch (see HIDDEN_STATUSES);
        it defaults to False so a new caller fails CLOSED — a surface that
        forgets to think about drafts hides them rather than leaking them.
        """
        if not ids:
            return []
        # v51: status + category. Memory Domains have status but no category
        # (the repo dict simply lacks the key → None). v56: data_packages
        # carry extended content (owner_name, tags) plus badge inputs
        # (created_by + created_at); memory domains stay v51-shaped.
        # Resolver-level badge derivation matches the API's _badges_for()
        # heuristic: 'curated' iff creator is in Admin, 'new' iff created_at
        # < 30 days ago. Soft-deleted entries are already excluded by the
        # repos' ``list()`` (``deleted_at IS NULL``), so a grant whose
        # target was deleted via /admin/* doesn't pull the row back.
        if resource_type == ResourceType.DATA_PACKAGE:
            rows = [r for r in self._data_packages_repo().list(limit=100000) if r["id"] in ids]
        elif resource_type == ResourceType.MEMORY_DOMAIN:
            rows = [r for r in self._memory_domains_repo().list(limit=100000) if r["id"] in ids]
        else:
            raise ValueError(f"StackResolver does not support resource_type={resource_type!r}")

        # Drop `draft` unless the caller is the admin surface. Applied to the
        # raw rows, before any enrichment, so there is exactly one place a
        # hidden status can escape from.
        if not include_hidden:
            rows = [r for r in rows if (r.get("status") or "prod") not in HIDDEN_STATUSES]

        from datetime import datetime, timedelta, timezone as _tz
        import json as _json

        now = datetime.now(_tz.utc)
        entries: List[ResourceEntry] = []
        for r in rows:
            tags_raw = r.get("tags")
            if isinstance(tags_raw, str) and tags_raw:
                try:
                    tags_list = _json.loads(tags_raw)
                    if not isinstance(tags_list, list):
                        tags_list = []
                except Exception:
                    tags_list = []
            elif isinstance(tags_raw, list):
                tags_list = tags_raw
            else:
                tags_list = []

            # v113: `new` is the only DERIVED badge left. `curated` used to be
            # appended here from "is created_by in the Admin group right now",
            # which is a fact about the person rather than the resource — an
            # admin leaving the group silently un-curated everything they had
            # created, and the card then disagreed with its own detail page.
            # The claim is now the stored `publisher_kind` passed through below.
            badges: List[str] = []
            created_at = r.get("created_at")
            if isinstance(created_at, datetime):
                ts = created_at if created_at.tzinfo else created_at.replace(tzinfo=_tz.utc)
                if (now - ts) < timedelta(days=30):
                    badges.append("new")

            rid = r["id"]
            entries.append(
                ResourceEntry(
                    id=rid,
                    name=r.get("name"),
                    description=r.get("description"),
                    icon=r.get("icon"),
                    color=r.get("color"),
                    cover_image_url=r.get("cover_image_url"),
                    status=r.get("status") or "prod",
                    category=r.get("category"),
                    owner_name=r.get("owner_name"),
                    owner_team=r.get("owner_team"),
                    tags=tags_list,
                    badges=badges,
                    # Clamped the same way both repos clamp it, so a NULL or a
                    # stray value can never render as a false 'organization'.
                    publisher_kind=(
                        r.get("publisher_kind") if r.get("publisher_kind") in ("user", "organization") else None
                    ),
                    requirement=("required" if rid in required_ids else "available"),
                )
            )
        # The repos return name-ordered rows already; keep that order.
        return entries
