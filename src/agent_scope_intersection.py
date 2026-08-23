"""Set-intersection of an agent's owner grants x its declared scope, per
ResourceType (V1d).

Mirrors ``src/grant_intersection.py``'s shape (fail-closed, builds on
``_allowed_ids_for_user``, routed through the repo factory — never raw SQL
on ``conn``) but for a *single* owner narrowed by a *single* agent's four
``*_mode`` columns instead of N co-session participants.

``tables_mode`` governs the whole DATA axis — ``TABLE`` plus
``DATA_PACKAGE`` and ``COLLECTION`` (``TABLES_MODE_EXTRA_TYPES``). That is
what the ``/agents`` builder actually declares: its Knowledge section offers
data packages, memory domains and file collections, never bare table ids. A
declared package therefore also stands for its member tables, expanded LIVE
per request (``_package_table_ids``) so a package edit reaches every agent
scoped to it without a re-save.

The OWNER side of two axes is wider than raw ``resource_grants``, because
that is how owners really hold access (``_owner_ids_for_type``):
``TABLE`` adds the member tables of the owner's granted data packages (the
unified-stack model routes analyst table access through packages, so a
grants-only owner set denied an agent every table its owner reached through
a package), and ``COLLECTION`` adds collections the owner created (ownership
grants access, mirroring ``accessible_collection_ids``). Both derivations
stay god-mode-free: an admin owner contributes their explicit grants, never
the short-circuit.

Fail-closed contract (spec §2, normative):
  - Missing/empty ``owner_user_id`` or ``agent_row`` -> ``{}`` (deny
    everything).
  - Mode ``'all'`` (or a ``ResourceType`` the agent does not model at all,
    e.g. ``RECIPE``/``CHAT``/...) -> the owner's set, unchanged. The agent
    narrows only what it declares; every resource type it stays silent on
    passes through as the owner's authority.
  - Mode ``'selected'`` -> ``owner_set & agent_scope_set`` for that type. A
    scope row naming a resource the owner does NOT hold is silently
    dropped, never surfaced — an agent can never widen beyond its owner.
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
``agent_scope_filter`` below.
"""

from __future__ import annotations

from typing import Optional

import duckdb

from app.resource_types import ResourceType

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
# ``compute_agent_intersection`` narrows.
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


def _agent_scope_ids(
    agent_id: str,
    item_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Set of ``item_id`` the agent's ``agent_scope`` declares for
    ``item_type``, read through the repo factory. ``conn`` is accepted for
    signature symmetry with ``_allowed_ids_for_user`` (and to leave room for
    a future DuckDB-direct fast path) but is currently unused — ``get_scope``
    is factory-routed, backend-agnostic."""
    from src.repositories import agents_repo

    items = agents_repo().get_scope(agent_id)
    return frozenset(item["item_id"] for item in items if item.get("item_type") == item_type)


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

        # Filter in SQL, not in Python. This runs once per brokered request
        # (``compute_agent_intersection`` from ``app/auth/pat_resolver.py``),
        # and reading the whole table to keep one creator's rows put a
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
    """The OWNER side of one axis. Plain group grants for every type, plus
    the two derivations that reflect how owners actually hold data access —
    both deliberately god-mode-free (SR-1: an admin owner's agent gets the
    admin's *explicit* grants, never the short-circuit):

    - TABLE: per-table grants ∪ member tables of the owner's granted data
      packages. The unified-stack model routes analyst table access through
      packages (``src/rbac.py::can_access_table``), so grants-only here
      denied every table to an agent whose owner had a package-shaped stack.
    - COLLECTION: group grants ∪ collections the owner created (ownership
      grants access — ``accessible_collection_ids``).

    ``owner_pkgs`` lets the caller pass the owner's package set in when it has
    already resolved it. :func:`compute_agent_intersection` walks every
    ``ResourceType`` and needed it on three of them, so leaving each to
    re-derive it ran the grant read + ``StackResolver`` three times per
    brokered request (Devin Review on #1515). Omitted, it is resolved here
    exactly as before.
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
      an unrecognized mode value, mirroring ``compute_agent_intersection``.
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


def compute_agent_intersection(
    owner_user_id: str,
    agent_row: Optional[dict],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict[str, frozenset[str]]:
    """Owner's grants narrowed by the agent's declared scope, per
    ``ResourceType`` — see module docstring for the full fail-closed
    contract."""
    if not owner_user_id or not agent_row:
        return {}

    agent_id = agent_row.get("id")

    # Resolved ONCE per call: three axes below need it, and each derivation
    # is a grant read plus a StackResolver pass. This runs on every brokered
    # request (app/auth/pat_resolver.py).
    owner_pkgs = _owner_package_ids(owner_user_id, conn)

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        owner_set = _owner_ids_for_type(owner_user_id, rt.value, conn, owner_pkgs=owner_pkgs)

        axis = _RT_TO_AXIS.get(rt.value)
        if axis is None:
            # Resource type the agent does not model at all -> pass through
            # the owner's set verbatim (narrows only what it declares).
            if owner_set:
                result[rt.value] = owner_set
            continue

        mode_field, item_type = axis
        mode = agent_row.get(mode_field)
        if mode == "all":
            if owner_set:
                result[rt.value] = owner_set
        elif mode == "selected":
            agent_set = _agent_scope_ids(agent_id, item_type, conn)
            if rt.value == ResourceType.TABLE.value:
                # A declared data package stands for its member tables: the
                # builder's Knowledge section offers packages and collections,
                # not raw table ids, so an agent scoped to a package must be
                # able to read the tables in it. Expanded live and intersected
                # with the owner's own reach, so neither a package edit nor a
                # revoked owner grant can widen the agent.
                declared_pkgs = _agent_scope_ids(agent_id, "data_package", conn)
                agent_set = agent_set | _package_table_ids(
                    declared_pkgs & owner_pkgs, conn
                )
            narrowed = owner_set & agent_set
            if narrowed:
                result[rt.value] = narrowed
        else:
            # Unrecognized mode -> fail closed. Only record an empty set
            # when the type would otherwise have appeared, to mirror the
            # "if owner_set" omission pattern above; either way the
            # resulting membership test is empty/deny.
            result[rt.value] = frozenset()

    return result
