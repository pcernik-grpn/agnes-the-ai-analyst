"""Table-access checks — thin wrappers over ``app.auth.access.can_access``.

Module exists for legacy import paths (``app/api/data.py``, ``app/api/sync.py``,
``app/api/catalog.py``, ``app/api/v2_*``, ``app/api/query.py``) that already
import ``can_access_table`` / ``get_accessible_tables`` from here. The
authorization itself flows through ``app.auth.access`` — this file is a
shim mapping the table-grain helpers onto the generic resource_grants check.
"""

from typing import Optional

import duckdb
from fastapi import HTTPException

from src.db import get_system_db


def _credential_surface(user) -> str:
    """The credential's data-read surface: ``'all'`` or ``'stack'`` (v106).

    Stashed onto the user dict by ``resolve_token_to_user`` from the PAT
    row's ``surface`` column. Every non-PAT credential (session JWT,
    scheduler shared-secret, local-dev bypass) and every internal caller
    that builds a bare user dict simply lacks the key — and MUST read as
    ``'all'`` so their behavior is unchanged; only a PAT explicitly minted
    with ``surface='stack'`` narrows an admin to the stack branch. Any
    unrecognized value fails closed to ``'stack'`` (never widens).
    """
    if not isinstance(user, dict):
        return "all"
    value = user.get("credential_surface", "all") or "all"
    return "all" if value == "all" else "stack"


def table_not_in_stack_message(table_id: str) -> str:
    """Standardized 403 detail string for table-access denial.

    All CLI surfaces (`agnes query`, `agnes snapshot create`,
    `agnes data <id>/download`, `/api/v2/schema`, `/api/v2/sample`)
    funnel through ``can_access_table`` and return this same string so
    the analyst's mental model stays consistent: "the table I asked
    about isn't in my stack — admin needs to add it to a Data Package".

    Internal tables (``agnes_sessions`` & co.) get their own wording: they
    are stack-gated like any other table since the ``agnes-usage`` package
    (see ``connectors/internal/registry.py``), but they are never
    distributed to the laptop — `agnes pull` skips them — so the generic
    "then run `agnes pull` to refresh" tail would name a next step that
    cannot work. Same convention as ``cli/query_hints.py``: a denial says
    what to do next, and only things that actually help.
    """
    from connectors.internal.access import is_internal_table

    if is_internal_table(table_id):
        from connectors.internal.registry import USAGE_PACKAGE_SLUG

        return (
            f"Table '{table_id}' is not in your stack. It carries your own "
            f"Agnes usage data — ask an admin to grant you the "
            f"'{USAGE_PACKAGE_SLUG}' Data Package that carries this table. "
            f"It is queryable server-side only (`agnes query`), never synced "
            f"to your laptop."
        )
    return (
        f"Table '{table_id}' is not in your stack. Ask an admin to add it "
        f"to a Data Package you have access to (Required or in your stack), "
        f"then run `agnes pull` to refresh."
    )


def require_table_access(
    user: dict,
    table_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> None:
    """Convenience: ``can_access_table`` or raise 403 with the standard
    message. Centralizes the deny path so every CLI surface returns the
    same actionable error.
    """
    if not can_access_table(user, table_id, conn):
        raise HTTPException(
            status_code=403,
            detail=table_not_in_stack_message(table_id),
        )


def can_access_table(
    user,  # dict | Principal
    table_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """True iff the user can read ``table_id``.

    Three sources of access (in precedence order):
      1. Principal callers (co-session / agent-session) — see the branch
         below, including the deliberate internal-table carve-out.
      2. Admin god-mode — members of the Admin system group see every
         registered table (dict users only; a Principal is never admin).
         v106: gated on the credential's data-read surface — a PAT minted
         with ``surface='stack'`` (the `agnes init` default) drops an admin
         into the stack branch below like any analyst. Non-PAT credentials
         (session JWT, scheduler, local-dev) never carry the key and read
         as ``'all'`` — see ``_credential_surface``.
      3. **Stack-gated**: the table must belong to at least one data
         package in the user's stack — auto-membership: required ∪
         available, no subscription needed (``StackResolver.stack``).
         Per-table resource_grants alone NO LONGER grant analyst visibility — the
         unified-stack design routes all analyst access through data
         packages. Admins manage access by adding tables to a package +
         granting the package; ad-hoc per-table grants in
         ``resource_grants`` are a no-op for analysts (still consulted
         for backwards-compat fallback inside admin-only flows).

    Internal data-source tables (``agnes_sessions`` / ``agnes_telemetry`` /
    ``agnes_audit``) used to short-circuit to ``True`` here for EVERY
    caller. They no longer do for dict users: they are members of the
    seeded ``agnes-usage`` data package and resolve through the stack check
    like any other table, so an admin can decide who may query usage data
    at all (design ``docs/superpowers/specs/2026-08-31-usage-package-and-
    per-turn-tokens-design.md`` §2, **BREAKING**). The row-level filter is
    unchanged and independent: package membership decides visibility of the
    TABLE, never row scope.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        from connectors.internal.access import is_internal_table

        # Deliberate carve-out (spec §2): a restricted principal has no
        # personal stack, so "is the package in the caller's stack" has no
        # answer for it. Internal tables stay reachable — the principal's
        # authority is already bounded by owner grants ∩ scope, the row
        # filter yields only rows it is entitled to, and removing them
        # would break delegation for no governance gain. Pinned by
        # tests/test_agent_scope_seams.py, tests/test_copresence_datapath.py
        # and tests/test_query_internal_session_principal.py.
        if is_internal_table(table_id):
            return True

        from app.auth.access import can_access_session
        from app.resource_types import ResourceType

        # Restricted principal (co-session / agent-session): intersection
        # membership, no admin short-circuit, no personal stack. The
        # intersection is the sole authority.
        return can_access_session(user, ResourceType.TABLE.value, table_id)

    user_id = user.get("id")
    if not user_id:
        return False

    # On Postgres never open the system DuckDB — the RBAC helpers below
    # (is_user_admin / StackResolver / data_packages_repo) route through the
    # repository factory when ``conn`` is None (see their use_pg() guards), so
    # a raw system-DB handle is both unnecessary and forbidden on PG.
    from src.repositories import use_pg

    should_close = False
    if conn is None and not use_pg():
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin

        if is_user_admin(user_id, conn) and _credential_surface(user) == "all":
            return True

        # Collection-derived tables (uploaded files → SQL-queryable tables, #4)
        # inherit their owning collection's access — owner OR group share.
        #
        # This is an ADDITIONAL path, not a replacement: it used to `return`
        # here, which made data-package membership a no-op for anything that
        # arrived via a file upload. An admin who followed this module's own
        # denial message — "ask an admin to add it to a Data Package you have
        # access to" — changed nothing, while `/catalog` (package-based)
        # cheerfully showed the table as in-stack and LOCAL. Falling through
        # to the stack check below makes the documented path work and leaves
        # collection sharing untouched.
        from src.repositories import table_registry_repo as _tr_repo

        _row = _tr_repo().get(table_id)
        if _row and (_row.get("source_type") or "") == "collection":
            from app.auth.access import can_access_collection

            if can_access_collection(user_id, _row.get("bucket") or "", conn):
                return True

        from app.services.stack_resolver import StackResolver
        from app.resource_types import ResourceType

        resolver = StackResolver(conn)
        pkg_entries = resolver.stack(user_id, ResourceType.DATA_PACKAGE)
        if not pkg_entries:
            return False
        pkg_ids_set = {e.id for e in pkg_entries}
        from src.repositories import data_packages_repo as _dp_repo

        table_pkg_ids = {p["id"] for p in _dp_repo().list_packages_of_table(table_id)}
        return bool(pkg_ids_set & table_pkg_ids)
    finally:
        if should_close:
            conn.close()


def get_accessible_ids(
    user,  # dict | Principal
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[frozenset]:
    """Grant-based accessible id set for ``resource_type``. ``None`` means
    "all" (admin).

    Symmetric to :func:`get_accessible_tables` but generic over any
    ``resource_grants`` resource_type (RECIPE, COLLECTION, DATA_PACKAGE, …)
    and grant-model only — this is NOT the stack-gated table model, so it
    must not be used for ``table`` access (see :func:`can_access_table` /
    :func:`get_accessible_tables` for that).

    For a ``Principal`` (co-session or agent-session), returns the
    intersection id-set for ``resource_type`` — never ``None``: no admin
    god-mode for a restricted principal, not even one whose owner is an admin.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        return frozenset(user.intersection.get(resource_type, frozenset()))

    user_id = user.get("id")
    if not user_id:
        return frozenset()

    # On Postgres never open the system DuckDB — see can_access_table above;
    # is_user_admin / _allowed_ids_for_user route through the repository factory
    # when conn is None.
    from src.repositories import use_pg

    should_close = False
    if conn is None and not use_pg():
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin, _allowed_ids_for_user

        if is_user_admin(user_id, conn):
            return None  # admin sees everything

        return _allowed_ids_for_user(user_id, resource_type, conn)
    finally:
        if should_close:
            conn.close()


def get_accessible_tables(
    user,  # dict | Principal
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[list[str]]:
    """List of table IDs the user can read. ``None`` means "all" (admin).

    Stack-gated for analysts: the set is
      * tables belonging to data packages in the user's stack
        (``StackResolver.stack``, whose formula forks on
        ``features.stack_auto_membership`` — classic default: required ∪
        subscribed-available; auto: every grant on the caller's groups,
        regardless of subscription). This is the query-authorization
        boundary; a local parquet copy is a separate, narrower concern
        handled by the manifest's per-table ``server_only`` overlay
        (`agnes pull` skip), not by this function.
    Per-table ``resource_grants(group, 'table', …)`` rows are NO LONGER
    consulted for analyst visibility — see :func:`can_access_table`.

    Internal tables (``agnes_sessions`` & co.) are no longer appended
    unconditionally for dict users: they are members of the seeded
    ``agnes-usage`` package and therefore arrive through the same package
    membership as any other table (spec §2, **BREAKING**).

    For a ``Principal`` (co-session or agent-session), returns the
    intersection table-ids plus internal tables — the carve-out documented
    in :func:`can_access_table`, which this branch must keep mirroring or a
    principal would be told a table is unreadable that ``can_access_table``
    still waves through. NEVER ``None`` — ``None`` is the admin "all"
    sentinel, and a restricted principal must always get a concrete list,
    even when its intersection is empty.
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        from app.resource_types import ResourceType
        from connectors.internal.access import INTERNAL_TABLES

        result = list(user.intersection.get(ResourceType.TABLE.value, frozenset()))
        for t in INTERNAL_TABLES:
            if t.registry_id not in result:
                result.append(t.registry_id)
        return result

    user_id = user.get("id")
    if not user_id:
        return []

    # On Postgres never open the system DuckDB — see can_access_table above;
    # all reads below route through the repository factory when conn is None.
    from src.repositories import use_pg

    should_close = False
    if conn is None and not use_pg():
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin

        if is_user_admin(user_id, conn) and _credential_surface(user) == "all":
            # Admin on a full-surface credential sees everything. The None
            # sentinel is also a perf contract (single resolution, no N+1 —
            # see app/api/catalog.py) so a surface='all' admin must get
            # None, never a concrete all-ids list. An admin on a
            # surface='stack' PAT falls through to the stack branch below —
            # StackResolver.stack() already carries the admin subscription
            # bypass, so their curated stack resolves like any analyst's.
            return None

        from app.services.stack_resolver import StackResolver
        from app.resource_types import ResourceType

        resolver = StackResolver(conn)
        pkg_entries = resolver.stack(user_id, ResourceType.DATA_PACKAGE)
        result: list[str] = []
        if pkg_entries:
            pkg_ids_set = {e.id for e in pkg_entries}
            from src.repositories import data_packages_repo as _dp_repo

            result = _dp_repo().list_member_table_ids(pkg_ids_set)
        # Collection-derived tables (#4): uploaded files that became
        # SQL-queryable tables are visible to the owner + anyone the owning
        # collection is shared with — not via the data-package stack. Access
        # tracks accessible_collection_ids (owner ∪ group grants).
        from app.auth.access import accessible_collection_ids as _acc_cols

        allowed_corpora = _acc_cols(user, conn)  # None == admin (returned above)
        if allowed_corpora:
            corpora = set(allowed_corpora)
            from src.repositories import table_registry_repo as _tr_repo

            for r in _tr_repo().list_all():
                if (r.get("source_type") or "") == "collection" and r.get("bucket") in corpora:
                    rid = r.get("id")
                    if rid and rid not in result:
                        result.append(rid)
        # NO internal-table append here on purpose: since the seeded
        # `agnes-usage` package they are ordinary package members, so
        # `list_member_table_ids` above already returns them to a caller who
        # holds the grant — and must NOT return them to one who does not.
        return result
    finally:
        if should_close:
            conn.close()
