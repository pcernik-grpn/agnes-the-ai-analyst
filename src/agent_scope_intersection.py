"""An agent's own resolved authority, per ResourceType (V1d, replaced by
C2.2's D-C2 resolution — ``docs/superpowers/plans/
2026-08-26-one-agent-model.md``).

Mirrors ``src/grant_intersection.py``'s shape (fail-closed, builds on
``_allowed_ids_for_user``, routed through the repo factory — never raw SQL
on ``conn``) but for a *single* agent's ``agent_scope`` rows plus its four
``*_mode`` columns instead of N co-session participants.

``tables_mode`` governs the whole DATA axis — ``TABLE`` plus
``DATA_PACKAGE`` and ``COLLECTION`` (``TABLES_MODE_EXTRA_TYPES``). That is
what the ``/agents`` builder actually declares: its Knowledge section offers
data packages, memory domains and file collections, never bare table ids. A
declared package therefore also stands for its member tables, expanded LIVE
per request (``_package_table_ids``) so a package edit reaches every agent
scoped to it without a re-save.

**C2.2 — the D-C2 staged resolution** (``resolve_agent_authority``, replacing
the old ``compute_agent_intersection``): for a ``'selected'`` axis, EACH
``agent_scope`` row is resolved on its OWN ``granted_by`` (C2.1), not against
one fixed "owner" identity:

  - a row whose granter is a THIRD PARTY — distinct from the agent's own
    ``owner_user_id`` — who is (currently) an ADMIN resolves
    UNCONDITIONALLY — the agent has that authority in its own right, pure
    LD2. A declared ``data_package`` row resolved this way still expands to
    its member tables live.
  - every other row (a non-admin granter, OR a granter who IS the agent's
    own owner — including an admin owner) resolves narrowed to ``item ∩
    that GRANTER's CURRENT access`` — the exact intersection shape this
    module always enforced, just keyed to whoever wrote the row
    (``granted_by``) instead of hard-coded to "the owner". Reuses the very
    same access-reach machinery (``_owner_ids_for_type`` et al.)
    parameterized by the granter's id — a granter IS, for the row they
    wrote, playing the same "how far does this identity's access reach"
    role the owner used to play unconditionally for every row. The
    ``granter_id != owner_user_id`` guard on the admin branch is what keeps
    an admin OWNER's own agent narrowed (SR-1: an admin owner contributes
    their explicit grants, never the god-mode short-circuit) — without it,
    ``_is_user_admin(granter_id)`` would be true merely because the OWNER
    happens to be an admin, for a row that is not a genuine third-party
    grant at all.
  - ``granted_by IS NULL`` (DuckDB, which has no such column at all — see
    ``src/repositories/agents.py::AgentsRepository.set_scope`` — or a
    defensively-possible NULL row on Postgres) falls back to the agent's
    ``owner_user_id`` as the implicit granter — which is, by construction,
    never a "third party" relative to itself, so this always takes the
    narrowing branch above. This is what makes the cutover a no-op:
    migration 0073 backfills every PG row's ``granted_by`` to its agent's
    ``owner_user_id``, and DuckDB never has the column to begin with, so on
    both backends every row that predates C2 resolves exactly as the old
    owner-intersection did — including for an agent whose owner is an
    admin.

Mode ``'all'`` on a modeled axis, and every ``ResourceType`` the agent does
not model at all (e.g. ``RECIPE``/``CHAT``/...), are NOT itemized —there is
no ``agent_scope`` row to carry a granter — so they keep passing through the
OWNER's current access unchanged, exactly as before. This is not a carve-out
from D-C2: it is equivalent to "every row on this axis is implicitly
self-granted by the owner", which is exactly the ``granted_by IS NULL``
fallback above with the owner as the (only possible, today) granter — C2.3
is what opens a non-owner path to writing scope at all.

Fail-closed contract (spec §2, normative — unchanged shape, now per-row):
  - Missing/empty ``agent_id``, a missing/soft-deleted agent row, or a
    missing ``owner_user_id`` -> ``{}`` (deny everything).
  - Mode ``'all'`` (or an unmodeled ``ResourceType``) -> the owner's current
    access, unchanged.
  - Mode ``'selected'`` -> the union of third-party-admin-granted rows
    (unconditioned) and self-granted rows — including an admin OWNER's own
    rows — currently held by their own granter. A scope row naming a
    resource its granter does NOT (or no longer) hold is silently dropped,
    never surfaced — an agent can never widen beyond what backs it.
  - An unrecognized (neither ``'all'`` nor ``'selected'``) mode value ->
    ``frozenset()`` for that type. This is the OPPOSITE of
    ``app.chat.agent_profile.compute_effective_scope``'s audit-only
    fail-open ("treat as all") — that function only *describes* scope for
    admin review; this one *enforces* it, so an unrecognized mode must
    fail closed rather than accidentally grant full access.

``connections_mode`` is deliberately absent from ``MODE_TO_RESOURCE_TYPE``:
there is no ``ResourceType.CONNECTION`` — per-user MCP connections are
authorized through a separate mechanism entirely (``tool_registry``
passthrough grants keyed on groups). Do not "fix" this by inventing a
resource type here; the axis is enforced at its own seam via
``agent_scope_filter`` below, which C2.2 leaves untouched (it never
intersected against an owner's access to begin with — see that function's
own docstring).
"""

from __future__ import annotations

from typing import Iterable, Optional, Tuple

import duckdb

from app.resource_types import ResourceType

#: Return shape of :func:`resolve_agent_authority` — also what
#: ``AgentPrincipal.intersection`` (``app/auth/session_principal.py``)
#: carries. Named for what it now IS (an agent's own resolved authority),
#: not the set-intersection implementation detail the old name described.
AgentAuthority = dict[str, frozenset[str]]

# agents.<mode column> -> (agent_scope.item_type, ResourceType.value).
# Reused by the seams (e.g. the sandbox-materialization filter) that need
# the same mode->type mapping this module uses internally.
MODE_TO_RESOURCE_TYPE: dict[str, tuple[str, str]] = {
    "tables_mode": ("table", "table"),
    "plugins_mode": ("plugin", "marketplace_plugin"),
    "memory_mode": ("memory_domain", "memory_domain"),
}

# ``tables_mode`` governs the whole DATA axis, not just bare table ids: the
# builder's "Knowledge" section declares data packages and file collections,
# so those two ResourceTypes narrow (and fail closed) together with TABLE
# under the same mode column. Kept OUT of ``MODE_TO_RESOURCE_TYPE`` so the
# seams consuming that map (one item_type per mode axis) are unaffected.
TABLES_MODE_EXTRA_TYPES: dict[str, str] = {
    # agent_scope.item_type -> ResourceType.value
    "data_package": ResourceType.DATA_PACKAGE.value,
    "collection": ResourceType.COLLECTION.value,
}

# Cap for the owned-collections scan below — see ``_owned_collection_ids``.
_COLLECTION_SCAN_LIMIT = 100_000

# ResourceType.value -> (mode column, agent_scope.item_type) for every axis
# ``resolve_agent_authority`` narrows.
_RT_TO_AXIS: dict[str, tuple[str, str]] = {
    **{rt: (mf, it) for mf, (it, rt) in MODE_TO_RESOURCE_TYPE.items()},
    **{rt: ("tables_mode", it) for it, rt in TABLES_MODE_EXTRA_TYPES.items()},
}


def _allowed_ids_for_user(
    user_id: str,
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Module-attribute indirection onto ``app.auth.access``'s no-admin-
    short-circuit grant primitive — kept as a separate module attribute (not
    inlined at the call site) so tests can monkeypatch it, mirroring
    ``src/grant_intersection.py``."""
    from app.auth.access import _allowed_ids_for_user as _impl

    return _impl(user_id, resource_type, conn)


def _is_user_admin(
    user_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """Module-attribute indirection onto ``app.auth.access.is_user_admin`` —
    same reason as :func:`_allowed_ids_for_user`: ``resolve_agent_authority``
    calls it per row (D-C2's admin/self-granted branch), and tests need to
    control "is this granter an admin" without standing up a real Admin
    group membership for every scenario."""
    from app.auth.access import is_user_admin as _impl

    return _impl(user_id, conn)


def _agent_scope_rows(
    agent_id: str,
    item_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> list[dict]:
    """``[{"item_id": ..., "granted_by": ...}, ...]`` the agent's
    ``agent_scope`` declares for ``item_type``, read through the repo
    factory. ``conn`` is accepted for signature symmetry with
    ``_allowed_ids_for_user`` (and to leave room for a future DuckDB-direct
    fast path) but is currently unused — ``get_scope`` is factory-routed,
    backend-agnostic.

    Kept as a separate module attribute (not inlined at the call site), like
    ``_allowed_ids_for_user``, so tests can monkeypatch it. Superseded
    ``_agent_scope_ids`` (which discarded ``granted_by``) in C2.2 — every
    caller now needs the granter to resolve a row per D-C2.
    """
    from src.repositories import agents_repo

    items = agents_repo().get_scope(agent_id)
    return [
        {"item_id": item.get("item_id"), "granted_by": item.get("granted_by")}
        for item in items
        if item.get("item_type") == item_type
    ]


def _package_table_ids(
    package_ids: frozenset[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Member-table ids of ``package_ids``, expanded LIVE through the repo
    factory — a package edit flows into every agent's effective scope on the
    next request, instead of freezing membership at scope-save time.

    Fail-SAFE per package: a lookup error contributes nothing (narrower is
    the safe direction on both the owner and the agent side). ``conn`` is
    accepted for signature symmetry only.
    """
    if not package_ids:
        return frozenset()
    out: set[str] = set()
    try:
        from src.repositories import data_packages_repo

        repo = data_packages_repo()
        for pkg_id in package_ids:
            try:
                out.update(t["id"] for t in repo.list_tables(pkg_id))
            except Exception:
                continue
    except Exception:
        return frozenset()
    return frozenset(out)


def _owner_package_ids(
    owner_user_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Data packages the owner **actually holds**, as the table-authorization
    boundary defines it — bounded from both sides on purpose:

    - ``⊆`` the owner's raw group grants (``_allowed_ids_for_user``, which
      never applies the Admin short-circuit), so an admin owner contributes
      only explicit grants. ``StackResolver.stack`` alone would breach this:
      for an admin it also surfaces raw subscriptions with no backing grant.
    - ``⊆`` the owner's effective stack (``StackResolver.stack``), which is
      what ``src/rbac.py::can_access_table`` authorizes a table read against.
      Raw grants alone would breach THIS one: the classic membership formula
      (``features.stack_auto_membership: false``) is
      ``required ∪ (subscribed ∩ available)``, so an *available* package the
      owner never subscribed to is not in their stack and its tables 403 for
      them — while raw grants would still hand those tables to their agent.

    The intersection of the two is therefore the only set that cannot exceed
    the owner on either axis. Fail-safe: any resolver error narrows to the
    raw-grant set intersected with nothing, i.e. empty.
    """
    granted = _allowed_ids_for_user(owner_user_id, ResourceType.DATA_PACKAGE.value, conn)
    if not granted:
        return frozenset()
    try:
        from app.resource_types import ResourceType as _RT
        from app.services.stack_resolver import StackResolver

        in_stack = {e.id for e in StackResolver(conn).stack(owner_user_id, _RT.DATA_PACKAGE)}
    except Exception:
        return frozenset()
    return granted & frozenset(in_stack)


def _owned_collection_ids(owner_user_id: str) -> frozenset[str]:
    """Collections the owner CREATED — ownership grants access without a
    group grant (mirrors ``app.auth.access.accessible_collection_ids``), so
    the owner side of the COLLECTION axis must include them or an agent
    could never be scoped to its owner's own uploads. Fail-safe: an error
    reads as "owns nothing" (narrower)."""
    if not owner_user_id:
        return frozenset()
    try:
        from src.repositories import file_corpora_repo

        # Filter in SQL, not in Python. This runs once per identity per
        # brokered request (``resolve_agent_authority`` from
        # ``app/auth/pat_resolver.py``), and reading the whole table to keep
        # one creator's rows put a
        # table-sized scan on the authorization path (Devin Review on #1515).
        #
        # The cap stays explicit and high: ``list()`` defaults to 200, and a
        # silent truncation inside an authorization input would deny an agent
        # its owner's own uploads with no signal. Same reasoning as
        # ``_GRANT_PROJECTION_LIMIT`` in app/resource_types.py — it now bounds
        # one creator's collections rather than the whole table, so it is far
        # further from ever binding.
        rows = file_corpora_repo().list(created_by=owner_user_id, limit=_COLLECTION_SCAN_LIMIT)
        return frozenset(r["id"] for r in rows)
    except Exception:
        return frozenset()


def _owner_ids_for_type(
    owner_user_id: str,
    rt_value: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    owner_pkgs: Optional[frozenset[str]] = None,
) -> frozenset[str]:
    """How far ONE identity's access reaches for one axis. Named for its
    original caller (the owner side of the old owner-intersection), but
    since C2.2 (D-C2) also called with a ``granted_by`` GRANTER's id — a
    self-granted ``agent_scope`` row narrows to ``item ∩ this function's
    result for that row's granter``, not hard-coded to the agent's owner.
    "Owner" below means "whichever identity this call is checking".

    Plain group grants for every type, plus the two derivations that reflect
    how a real user actually holds data access — both deliberately
    god-mode-free (SR-1: an admin identity's agent gets the admin's
    *explicit* grants, never the short-circuit):

    - TABLE: per-table grants ∪ member tables of the identity's granted data
      packages. The unified-stack model routes analyst table access through
      packages (``src/rbac.py::can_access_table``), so grants-only here
      denied every table to an agent whose backing identity had a
      package-shaped stack.
    - COLLECTION: group grants ∪ collections the identity created (ownership
      grants access — ``accessible_collection_ids``).

    ``owner_pkgs`` lets the caller pass this identity's package set in when it
    has already resolved it. :func:`resolve_agent_authority` may call this
    for the owner AND for one or more granters per request, so leaving each
    call to re-derive it would run the grant read + ``StackResolver`` pass
    once per (identity, axis) pair (Devin Review on #1515, still true under
    C2.2 — the caller caches per identity). Omitted, it is resolved here.
    """
    base = _allowed_ids_for_user(owner_user_id, rt_value, conn)
    if rt_value == ResourceType.TABLE.value:
        pkgs = _owner_package_ids(owner_user_id, conn) if owner_pkgs is None else owner_pkgs
        return base | _package_table_ids(pkgs, conn)
    if rt_value == ResourceType.DATA_PACKAGE.value:
        # Narrowed the same way, so the agent's own package list cannot show
        # (or authorize) a package its owner does not effectively hold.
        return _owner_package_ids(owner_user_id, conn) if owner_pkgs is None else owner_pkgs
    if rt_value == ResourceType.COLLECTION.value:
        return base | _owned_collection_ids(owner_user_id)
    return base


def agent_scope_filter(
    agent_id: Optional[str],
    mode_field: str,
    item_type: str,
) -> Optional[frozenset[str]]:
    """Live allow-set for ONE scope axis, or ``None`` when the agent does not
    narrow that axis.

    The intersection map can only carry resources authorized through
    ``resource_grants``. Two axes are authorized elsewhere and therefore need
    a filter at their own seam, reading the agent's ``agent_scope`` rows
    directly:

    - ``connections_mode`` / ``connection`` — per-user MCP connections
      (``tool_registry`` grants keyed on groups); item_id is an
      ``mcp_sources.id``.
    - ``plugins_mode`` / ``plugin`` for **Store installs** — personal
      flea-market installs live in ``user_store_installs``, never in
      ``resource_grants``, so they survive the intersection untouched. (The
      admin-curated half of the same axis IS in the intersection and is
      filtered there; this helper covers only the store union.)

    Return contract, deliberately three-valued:

    - ``None`` — mode is ``'all'``: the agent does not narrow this axis, so
      the caller must apply **no** filter. Keying off this (rather than off
      "the caller is an ``AgentPrincipal``") is the whole point: the broker
      mints an agent-session token as soon as an agent narrows *anything*, so
      a tables-narrowed agent legitimately keeps every connection and store
      install its owner has.
    - a ``frozenset`` — mode is ``'selected'``: exactly the declared
      ``item_id`` set, possibly empty (an empty allowlist is a real answer,
      never a pass-through).
    - ``frozenset()`` — fail closed for a missing / soft-deleted agent row or
      an unrecognized mode value, mirroring ``resolve_agent_authority``.
    """
    from src.repositories import agents_repo

    if not agent_id:
        return frozenset()
    repo = agents_repo()
    agent = repo.get_by_id(agent_id)
    if not agent or agent.get("deleted_at") is not None:
        return frozenset()
    mode = agent.get(mode_field)
    if mode == "all":
        return None
    if mode != "selected":
        return frozenset()
    return frozenset(item["item_id"] for item in repo.get_scope(agent_id) if item.get("item_type") == item_type)


def agent_narrows(agent_row: dict) -> bool:
    """True when ANY of the four mode columns is ``'selected'`` — i.e. this
    agent actually restricts its owner rather than passing everything
    through unchanged. Used by the broker for the default-agent carve-out
    (an all-'all' agent needs no intersection work)."""
    for mode_field in ("tables_mode", "plugins_mode", "connections_mode", "memory_mode"):
        if agent_row.get(mode_field) == "selected":
            return True
    return False


def agent_is_passthrough(agent_row: dict) -> bool:
    """True only when EVERY mode column is explicitly ``'all'`` — the sole
    shape allowed to skip the intersection and ride the owner's plain
    identity. Deliberately stricter than ``not agent_narrows(...)``: an
    unknown/misconfigured mode value must take the enforced agent-session
    path (fail closed), not silently widen to full owner authority
    (security review on the live-enforcement PR)."""
    return all(
        agent_row.get(mode_field) == "all"
        for mode_field in ("tables_mode", "plugins_mode", "connections_mode", "memory_mode")
    )


def resolve_agent_authority(
    agent_id: Optional[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> AgentAuthority:
    """An agent's own resolved authority, per ``ResourceType`` — C2.2's
    replacement for ``compute_agent_intersection``. See the module docstring
    for the full D-C2 contract this implements.

    Parameterized by ``agent_id`` ALONE (unlike the old owner-parameterized
    function) — authority now comes from the agent's own ``agent_scope``
    rows and each row's ``granted_by``, not from a caller-supplied owner
    identity. The agent row (incl. ``owner_user_id``, needed for the
    'all'-mode / unmodeled-axis pass-through and as the implicit granter of
    a ``granted_by IS NULL`` row) is read here.
    """
    if not agent_id:
        return {}

    from src.repositories import agents_repo

    agent_row = agents_repo().get_by_id(agent_id)
    if not agent_row or agent_row.get("deleted_at") is not None:
        return {}
    owner_user_id = agent_row.get("owner_user_id")
    if not owner_user_id:
        return {}

    # One data-package-reach resolve per identity this call touches (the
    # owner, plus any distinct non-admin granter a 'selected' row names),
    # cached so a request scoped to N rows from the same granter pays for
    # the grant read + StackResolver pass once, not N times.
    pkg_cache: dict[str, frozenset[str]] = {}

    def _pkgs_for(user_id: str) -> frozenset[str]:
        if user_id not in pkg_cache:
            pkg_cache[user_id] = _owner_package_ids(user_id, conn)
        return pkg_cache[user_id]

    owner_pkgs = _pkgs_for(owner_user_id)

    # Per-item_type resolved id cache: TABLE's expansion and DATA_PACKAGE's
    # own axis both need the declared package set resolved the same way, and
    # both are reached from the ResourceType loop below — compute it once.
    selected_cache: dict[str, frozenset[str]] = {}

    def _resolve_selected(item_type: str, rt_value: str) -> frozenset[str]:
        if item_type in selected_cache:
            return selected_cache[item_type]
        resolved: set[str] = set()
        for row in _agent_scope_rows(agent_id, item_type, conn):
            item_id = row.get("item_id")
            if not item_id:
                continue
            granter_id = row.get("granted_by") or owner_user_id
            if granter_id != owner_user_id and _is_user_admin(granter_id, conn):
                # Genuine third-party admin grant: unconditioned — the
                # agent has this authority in its own right (pure LD2).
                # Deliberately excludes the agent's OWN owner even when the
                # owner is (also) an admin — ``granted_by`` for a
                # self-declared / owner-fallback row is not a distinct
                # granter identity conferring authority, it is the same
                # person the row is being narrowed against, so an admin
                # OWNER must never take this branch (SR-1: an admin owner
                # contributes their explicit grants, never the god-mode
                # short-circuit).
                resolved.add(item_id)
                continue
            granter_set = _owner_ids_for_type(granter_id, rt_value, conn, owner_pkgs=_pkgs_for(granter_id))
            if item_id in granter_set:
                resolved.add(item_id)
        selected_cache[item_type] = frozenset(resolved)
        return selected_cache[item_type]

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        axis = _RT_TO_AXIS.get(rt.value)
        if axis is None:
            # Resource type the agent does not model at all -> pass through
            # the owner's current access, unchanged (no scope row exists to
            # carry a different granter for it).
            owner_set = _owner_ids_for_type(owner_user_id, rt.value, conn, owner_pkgs=owner_pkgs)
            if owner_set:
                result[rt.value] = owner_set
            continue

        mode_field, item_type = axis
        mode = agent_row.get(mode_field)
        if mode == "all":
            owner_set = _owner_ids_for_type(owner_user_id, rt.value, conn, owner_pkgs=owner_pkgs)
            if owner_set:
                result[rt.value] = owner_set
        elif mode == "selected":
            resolved = _resolve_selected(item_type, rt.value)
            if rt.value == ResourceType.TABLE.value:
                # A declared data package stands for its member tables: the
                # builder's Knowledge section offers packages and
                # collections, not raw table ids, so an agent scoped to a
                # package must be able to read the tables in it. The package
                # set is resolved by the SAME admin/self-granted rule as any
                # other row (a declared package this agent has no real
                # authority over expands to nothing), so no extra narrowing
                # is needed here.
                declared_pkgs = _resolve_selected("data_package", ResourceType.DATA_PACKAGE.value)
                resolved = resolved | _package_table_ids(declared_pkgs, conn)
            if resolved:
                result[rt.value] = resolved
        else:
            # Unrecognized mode -> fail closed. Only record an empty set
            # when the type would otherwise have appeared, to mirror the
            # "if owner_set" omission pattern above; either way the
            # resulting membership test is empty/deny.
            result[rt.value] = frozenset()

    return result


# ---------------------------------------------------------------------------
# C2.1 — the D-C2 staged write-gate (docs/superpowers/plans/
# 2026-08-26-one-agent-model.md, "Decision point D-C2"): a non-admin writer
# may only grant a DATA-authority `agent_scope` item they currently hold
# themselves; an admin writer is unconditioned (the "admin-granted" half of
# D-C2 — task C2.2 later resolves such rows without re-checking the granter's
# live access). `plugin`/`memory_domain`/`slack_channel` are not data
# authority and keep today's rules — never checked here.
# ---------------------------------------------------------------------------

#: item_type values the write-gate governs. Mirrors `TABLES_MODE_EXTRA_TYPES`
#: plus bare `table`, plus `connection` (authorized outside `resource_grants`
#: entirely — see `writer_can_access_item`).
DATA_AUTHORITY_ITEM_TYPES: frozenset[str] = frozenset({"table", "data_package", "collection", "connection"})


def writer_can_access_item(
    writer_user_id: str,
    item_type: str,
    item_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """True when ``writer_user_id`` currently holds ``item_id`` of
    ``item_type`` through their OWN access.

    Deliberately the RAW reach check — ``_allowed_ids_for_user`` (plus the
    same package/collection-ownership widening :func:`_owner_ids_for_type`
    applies), NOT the full stack-narrowed set that function derives for the
    live runtime intersection. An "available" (granted but never subscribed)
    data package is a real grant the write-gate must accept — refusing it
    here would 403 a builder save merely reorganizing scope, exactly the
    hazard the pre-C2.1 ``_classify_knowledge`` docstring called out. The
    stack-aware narrowing still applies downstream, at resolve time
    (``resolve_agent_authority``, as of C2.2) — a grant that is not
    currently "in stack" makes the declared item inert there, never a
    write-time rejection.

    Parameterized by the WRITER rather than "the owner": every write path
    that exists today is ownership-gated (``_load_agent(require_owner=
    True)``, ``app/api/agents_admin.py``), so the two are the same person
    for every currently reachable call; a future write path with a real
    writer/owner split narrows correctly the moment one exists, with no
    change here.

    ``connection`` has no ``ResourceType`` of its own (per the module
    docstring — per-user MCP connections are authorized via ``tool_registry``
    grants, not ``resource_grants``); ``item_id`` is an ``mcp_sources.id``,
    checked the same way ``app/api/mcp_user_secrets.py::_require_source_grant``
    already gates a caller's own connection reach.

    The final ``return True`` is a pass-through for item_types genuinely
    OUTSIDE the DATA axis (``plugin``/``memory_domain``/``slack_channel``) —
    those are not data authority and are never checked here, by design.
    It is NOT a catch-all for a ``DATA_AUTHORITY_ITEM_TYPES`` member this
    function has not (yet) grown a branch for: that shape fails CLOSED
    instead, on purpose — the type is data-authority-shaped by the caller's
    own contract (``DATA_AUTHORITY_ITEM_TYPES``), so silently passing it
    would be a fail-open convention inversion for the one function this
    whole module leans on to keep the write-gate fail-closed.
    """
    if item_type == "table":
        base = _allowed_ids_for_user(writer_user_id, ResourceType.TABLE.value, conn)
        pkgs = _allowed_ids_for_user(writer_user_id, ResourceType.DATA_PACKAGE.value, conn)
        return item_id in (base | _package_table_ids(pkgs, conn))
    if item_type == "data_package":
        return item_id in _allowed_ids_for_user(writer_user_id, ResourceType.DATA_PACKAGE.value, conn)
    if item_type == "collection":
        base = _allowed_ids_for_user(writer_user_id, ResourceType.COLLECTION.value, conn)
        return item_id in (base | _owned_collection_ids(writer_user_id))
    if item_type == "connection":
        from app.api.mcp_passthrough import _visible_passthrough_tools

        granted_source_ids = {t["source_id"] for t in _visible_passthrough_tools(writer_user_id)}
        return item_id in granted_source_ids
    if item_type not in DATA_AUTHORITY_ITEM_TYPES:
        return True
    # A DATA_AUTHORITY_ITEM_TYPES member with no handled branch above — fail
    # closed rather than silently pass (see docstring).
    return False


def first_inaccessible_data_item(
    writer_user_id: str,
    items: Iterable[Tuple[str, str]],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[Tuple[str, str]]:
    """The first ``(item_type, item_id)`` pair in ``items`` that the write-
    gate refuses, or ``None`` when every one passes.

    An admin writer short-circuits to ``None`` unconditionally — the
    "admin-granted = unconditioned" half of D-C2 is exactly this: admins
    skip the check, never merely widen it. Every non-``DATA_AUTHORITY_
    ITEM_TYPES`` pair is untouched regardless of writer.
    """
    from app.auth.access import is_user_admin

    if is_user_admin(writer_user_id, conn):
        return None
    for item_type, item_id in items:
        if item_type not in DATA_AUTHORITY_ITEM_TYPES:
            continue
        if not writer_can_access_item(writer_user_id, item_type, item_id, conn):
            return (item_type, item_id)
    return None
