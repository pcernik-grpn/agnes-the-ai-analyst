"""Slice 4a's single read surface for per-scope audience-class mappings
(2026-08-30 plan, Task 8; spec 2026-08-28-sharepoint-acl-mirroring-design.md
§4.1-4.3), extended by Slice 3 (Task 9; spec §4.2, §7 step 6) with the
CALLER-side resolution: which of a tiered collection's audience classes does
a given caller — plain user, admin, or a restricted ``Principal``
(``AgentPrincipal``/``SessionPrincipal``) — actually hold.

An audience class (e.g. ``"full"``, ``"redacted"``) is configured per
SharePoint scope at wizard time as an ORDERED mapping ``{class_name ->
group_ids}`` — most-privileged class first, the order Task 10's projection
dedup and Task 11's "top class only" rule both rank by
(``app/api/admin_sharepoint.py``'s ``ConfirmScopeBody.audience_classes`` /
``_scope_out``). This module is the ONLY place that scans ``source_connections``
rows' ``config.scopes`` to read that mapping back at runtime, AND the ONLY
place that resolves a caller's membership against it — Tasks 10/11 (the
``facts_pg`` visibility predicate and the collections document-text gate)
must call :func:`audience_class_map` / :func:`tiered_collection_ids` /
:func:`audience_classes_for_caller` / :func:`top_class_for` rather than
re-deriving any of this themselves, so both the on-disk shape and the
caller-resolution rule have exactly one reader each.

Plain functions, no caching layer beyond what the underlying config/group
reads already do — resolution is LIVE per request, exactly like
``accessible_collection_ids``'s own group-grant read: a membership change or
a revoked pin takes effect on the very next call, never stale-cached beyond
the request that calls it.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from src.repositories import source_connections_repo, user_group_members_repo


def audience_class_map() -> Dict[str, List[Tuple[str, FrozenSet[str]]]]:
    """``{collection_id: [(class_name, frozenset(group_ids)), ...]}``, each
    collection's classes in the wizard-persisted privilege order (most-
    privileged first). Built by scanning every ``source_type='sharepoint'``
    connection's ``config.scopes`` — a scope with no ``audience_classes`` (or
    an empty list) contributes no entry, so a plain (non-tiered) collection
    is simply absent from the result rather than mapped to ``[]``.
    """
    result: Dict[str, List[Tuple[str, FrozenSet[str]]]] = {}
    for connection in source_connections_repo().list(source_type="sharepoint"):
        scopes = (connection.get("config") or {}).get("scopes")
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict):
                continue
            collection_id = scope.get("collection_id")
            classes = scope.get("audience_classes")
            if not collection_id or not isinstance(classes, list) or not classes:
                continue
            ordered = [
                (cls["name"], frozenset(cls.get("group_ids") or []))
                for cls in classes
                if isinstance(cls, dict) and cls.get("name")
            ]
            if ordered:
                result[collection_id] = ordered
    return result


def tiered_collection_ids() -> FrozenSet[str]:
    """The set of collection ids carrying a non-empty ``audience_classes``
    mapping — i.e. ``tiered`` per ``app/api/admin_sharepoint.py::_scope_out``.
    Convenience for Tasks 10/11 so neither re-derives it from
    :func:`audience_class_map` by hand."""
    return frozenset(audience_class_map().keys())


def top_class_for(collection_id: str) -> Optional[str]:
    """The most-privileged class name for ``collection_id``, or ``None`` for
    a non-tiered (or unknown) collection. First entry of
    :func:`audience_class_map`'s ordered list — Task 11's "serve document
    text to the top class only" rule reads this directly rather than
    re-indexing the map itself."""
    classes = audience_class_map().get(collection_id)
    return classes[0][0] if classes else None


def _group_based_classes(user_id: str, classes: List[Tuple[str, FrozenSet[str]]]) -> FrozenSet[str]:
    """Which of ``classes`` (one tiered collection's ordered list) ``user_id``
    holds by plain group membership alone — deliberately NEVER applies the
    Admin god-mode short-circuit, even when the caller turns out to be an
    admin. Two callers share this: the plain-user branch of
    :func:`audience_classes_for_caller` (which applies god-mode itself,
    separately, before ever reaching here) and the AgentPrincipal pin check
    (which must NOT — an admin OWNER's agent contributes only the owner's
    explicit grants, never a god-mode elevation, the same SR-1 discipline
    ``src.agent_scope_intersection`` applies everywhere else; see that
    module's ``_owner_ids_for_type`` docstring for the precedent). Reads
    live via the repo factory, matching ``accessible_collection_ids``'s own
    membership-read idiom (``app/auth/access.py::_user_group_ids``)."""
    if not user_id:
        return frozenset()
    member_groups = frozenset(user_group_members_repo().list_groups_for_user(user_id))
    if not member_groups:
        return frozenset()
    return frozenset(name for name, group_ids in classes if group_ids & member_groups)


def _agent_pinned_class(caller: Any, collection_id: str, classes: List[Tuple[str, FrozenSet[str]]]) -> Optional[str]:
    """The class name an ``AgentPrincipal``'s scope pin resolves to for
    ``collection_id``, or ``None`` when there is no pin, the pinned class no
    longer exists on this collection, or the OWNER does not currently hold
    it (or better) — every one of those is "no pin", i.e. the caller
    degrades to the least-privileged default. Never raises: a malformed or
    stale pin must fail toward redacted, not 500."""
    from app.auth.session_principal import AgentPrincipal

    if not isinstance(caller, AgentPrincipal):
        return None

    from src.agent_scope_intersection import audience_class_pins

    pinned_name = audience_class_pins(caller.agent_id).get(collection_id)
    if not pinned_name:
        return None

    ordered_names = [name for name, _ in classes]
    if pinned_name not in ordered_names:
        return None  # class renamed/removed since the pin was set

    owner_classes = _group_based_classes(caller.owner_user_id, classes)
    if not owner_classes:
        return None

    pinned_rank = ordered_names.index(pinned_name)
    owner_best_rank = min(ordered_names.index(name) for name in owner_classes)
    # Lower index = more privileged (Task 8's ordering is most-privileged
    # first). The owner qualifies for the pin when their best class is at
    # least as privileged as the one pinned.
    return pinned_name if owner_best_rank <= pinned_rank else None


def audience_classes_for_caller(caller: Any, collection_ids: Iterable[str]) -> Dict[str, FrozenSet[str]]:
    """``{collection_id: frozenset(class_name, ...)}`` — the audience classes
    ``caller`` holds, for every TIERED collection in ``collection_ids``
    (§4.2). A non-tiered collection is OMITTED from the result entirely; a
    TIERED collection the caller can reach is always PRESENT, even when the
    caller holds none of its classes (an empty frozenset) — Task 10 binds
    ``:audience_pairs`` straight off this dict's items, and a present-but-
    empty entry and an absent one must read the same way there (no pairs
    contributed either way), so the choice is otherwise arbitrary; PRESENT
    is chosen because it lets a caller (or a test) distinguish "this
    collection has no tiers at all" from "it is tiered and I hold nothing
    in it" without a second lookup.

    Resolution rule (spec §4.2, §7 step 6), all live per-request — no caching
    beyond what the underlying repo reads do:

    - plain user dict: every class whose ``group_ids`` intersects the
      user's current group memberships (:func:`_group_based_classes`).
    - admin user (the same god-mode check ``accessible_collection_ids``
      applies, via ``app.auth.access.is_user_admin``): ALL classes of the
      collection.
    - a restricted ``Principal`` (``AgentPrincipal``/``SessionPrincipal``,
      ``app.auth.session_principal.PRINCIPAL_TYPES``): default is the
      LEAST-privileged class only (the last entry of the ordered list) — an
      ``AgentPrincipal`` may instead get its scope-pinned class when the
      pin is honored (:func:`_agent_pinned_class`); a ``SessionPrincipal``
      (no owner identity, no scope to pin) always gets the default. Fail
      toward redacted, never toward privileged: any resolution failure
      (missing owner classes, a stale/renamed pin, an unrecognized
      principal) degrades to least-privileged, never to nothing-checked.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    class_map = audience_class_map()
    result: Dict[str, FrozenSet[str]] = {}

    if isinstance(caller, PRINCIPAL_TYPES):
        for collection_id in collection_ids:
            classes = class_map.get(collection_id)
            if not classes:
                continue
            pinned = _agent_pinned_class(caller, collection_id, classes)
            least_privileged = classes[-1][0]
            result[collection_id] = frozenset({pinned or least_privileged})
        return result

    user_id = caller.get("id") if isinstance(caller, dict) else None

    admin = False
    if user_id:
        from app.auth.access import is_user_admin

        admin = is_user_admin(user_id)

    for collection_id in collection_ids:
        classes = class_map.get(collection_id)
        if not classes:
            continue
        if admin:
            result[collection_id] = frozenset(name for name, _ in classes)
        elif user_id:
            result[collection_id] = _group_based_classes(user_id, classes)
        else:
            # No resolvable identity at all — fail toward redacted rather
            # than omit (the collection IS tiered; the caller just holds
            # nothing in it, same shape as a plain user in no class).
            result[collection_id] = frozenset()
    return result
