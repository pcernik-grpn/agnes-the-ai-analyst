"""Authorization helpers — group membership and resource grants.

Two layers of access control replace the v9 internal_roles / group_mappings
machinery:

1. **App-level access** is whether the user is in the ``Admin`` group. There
   is no hierarchy — ``Admin`` is god mode (short-circuits every grant
   check), every other group is just a label binding members to grants.

2. **Resource access** is whether any group the user is in holds a grant on
   ``(resource_type, resource_id)`` in ``resource_grants``. ``Admin`` group
   short-circuits this so admins never need explicit grants.

Two FastAPI dependencies cover the API surface:

  - ``require_admin`` — gates app-level mutations (admin UI, user mgmt,
    settings, …). 403 unless user is in Admin.
  - ``require_resource_access(resource_type, path_template)`` — gates
    entity-scoped endpoints. The path_template is a Python format string
    resolved against the request's path_params at call time — e.g.
    ``"{slug}/{plugin_name}"`` becomes the resource_id we look up.

The resolver is intentionally cache-less: every authorization check does one
or two DuckDB queries. DuckDB is in-process, so a per-request DB hit costs
sub-millisecond — the upstream session.internal_roles cache + dual-path
fallback solved a problem we don't have. (The god-mode observability layer
below keeps its own short-lived caches, but they only dedupe log lines — the
access decision itself stays cache-less.)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import _get_db, get_current_user
from app.auth.session_principal import AgentPrincipal, PRINCIPAL_TYPES, Principal, SessionPrincipal
from app.resource_types import ResourceType
from src.db import SYSTEM_ADMIN_GROUP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google group self-heal: re-sync on access-miss
# ---------------------------------------------------------------------------
# Per-user last-resync timestamp (in-process cache). Guards against repeated
# Admin SDK calls on every denied request — one resync attempt per user per
# RESYNC_COOLDOWN_SECONDS window, regardless of outcome.
_google_resync_last: dict[str, float] = {}
_RESYNC_COOLDOWN_SECONDS = 60


def _maybe_resync_google_groups(user_id: str, email: str) -> bool:
    """Re-fetch Workspace groups for *user_id* if Google sync is configured
    and the per-user cooldown has passed.

    Returns True when a resync was attempted (caller should re-read groups),
    False when skipped (cooldown active, no Google config, or fetch error).

    Fail-soft: any exception is swallowed and logged; the existing membership
    snapshot is never cleared by this path.
    """
    if "GOOGLE_ADMIN_SDK_SUBJECT" not in os.environ and "GOOGLE_ADMIN_SDK_MOCK_GROUPS" not in os.environ:
        return False

    now = time.monotonic()
    if now - _google_resync_last.get(user_id, 0) < _RESYNC_COOLDOWN_SECONDS:
        return False

    _google_resync_last[user_id] = now
    try:
        from app.auth.group_sync import apply_user_groups

        # apply_user_groups ignores conn and routes through the repo factory,
        # so no raw connection is needed here (backend-safe on PG too).
        result = apply_user_groups(user_id, email, None)
        logger.info(
            "google-group self-heal: user=%s applied=%s groups=%s",
            user_id,
            result.applied,
            result.relevant,
        )
        return True
    except Exception:
        logger.warning("google-group self-heal failed for user %s", user_id, exc_info=True)
        return False


def _get_group_id_by_name(name: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> Optional[str]:
    """Look up a group's id by its (unique) name. Returns None if absent —
    typically only happens during the very first migration pass before
    _seed_system_groups has run, or in mis-seeded test fixtures.

    Honors ``conn`` only when the active backend is DuckDB and ``conn``
    is a DuckDB connection (test-isolation escape hatch for fixtures that
    seed into a per-test DuckDB). When the active backend is Postgres,
    ``conn`` is the local DuckDB view-handle which would be stale; we
    route through the global factory which reads from PG instead.
    """
    from src.repositories import use_pg, user_groups_repo

    if conn is not None and not use_pg():
        from src.repositories.user_groups import UserGroupsRepository

        row = UserGroupsRepository(conn).get_by_name(name)
    else:
        row = user_groups_repo().get_by_name(name)
    return row["id"] if row else None


def _user_group_ids(user_id: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> set[str]:
    """Set of group_ids the user is in.

    Returns only the rows present in ``user_group_members``. The implicit
    "every user is in Everyone" virtual row was removed when Google-prefix
    mapping landed — every membership is now sourced from a concrete row
    (``admin``, ``google_sync``, or ``system_seed``) so an operator
    auditing /admin/access sees the same set the authorization layer
    enforces. Callers that want Everyone-style "always granted" plugins
    must grant them to a real group the user is a member of.

    Honors ``conn`` only in DuckDB-backend mode (see ``_get_group_id_by_name``
    for rationale); routes through the global factory otherwise.
    """
    from src.repositories import use_pg, user_group_members_repo

    if conn is not None and not use_pg():
        from src.repositories.user_group_members import UserGroupMembersRepository

        return set(UserGroupMembersRepository(conn).list_groups_for_user(user_id))
    return set(user_group_members_repo().list_groups_for_user(user_id))


def is_user_admin(user_id: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> bool:
    """True iff the user is a member of the Admin system group.

    ``conn`` honored when explicitly passed (test isolation); falls back
    to the global factory otherwise.
    """
    admin_id = _get_group_id_by_name(SYSTEM_ADMIN_GROUP, conn=conn)
    if admin_id is None:
        # No Admin group seeded — defensively deny. Fail-closed beats the
        # alternative of silently granting elevated access.
        logger.warning("is_user_admin: Admin group missing in user_groups; denying access")
        return False
    return admin_id in _user_group_ids(user_id, conn=conn)


# ---------------------------------------------------------------------------
# God-mode observability
# ---------------------------------------------------------------------------
# When the Admin short-circuit in ``can_access`` grants a resource the admin
# holds no explicit group grant for, emit one deduplicated log line. Pure
# observability — never changes the decision — but it is the data that shows
# which surfaces actually rely on god-mode before any future narrowing.
# Best-effort in-process dedup (same pattern as ``_google_resync_last``); a
# benign race at worst duplicates a line.
#
# NOTE on ``resource_type == 'table'``: "explicit grant" here means a direct
# ``resource_grants`` row, but analyst table visibility actually flows through
# data packages / the stack (``src/rbac.py``), which this check does not
# consult. So an admin who would ALSO reach a table via a granted package is
# still counted as a god-mode hit — table bypass counts are an UPPER BOUND
# (over-counts reliance, the safe direction for "which surfaces need god-mode"
# data). (review note on #1143.)
_god_mode_logged: "dict[tuple[str, str, str], float]" = {}
_GOD_MODE_LOG_COOLDOWN_SECONDS = 900
_GOD_MODE_CACHE_MAX = 4096

# Short-TTL memoization of the per-(user, resource_type) grant set, so a
# list-style endpoint that checks N distinct resource_ids in a tight loop
# pays ONE grant query, not N — the observability lookup must not add a
# per-item DB round trip to the auth hot path (review finding on #1143).
_god_mode_grants: "dict[tuple[str, str], tuple[float, frozenset[str]]]" = {}
_GOD_MODE_GRANTS_TTL_SECONDS = 5.0


def _god_mode_allowed_ids(
    user_id: str,
    resource_type: str,
    now: float,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset:
    # Only memoize the production path (conn is None → global repo factory,
    # one stable backend per process). When an explicit conn is passed (test
    # isolation, or a caller pinning a specific handle) the cache key can't
    # capture which backend/handle produced the set, so a memo could return a
    # result resolved against a different connection within the TTL — skip the
    # cache entirely and read fresh (review finding on #1143).
    if conn is not None:
        return _allowed_ids_for_user(user_id, resource_type, conn=conn)
    key = (user_id, resource_type)
    cached = _god_mode_grants.get(key)
    if cached is not None and (now - cached[0]) < _GOD_MODE_GRANTS_TTL_SECONDS:
        return cached[1]
    ids = _allowed_ids_for_user(user_id, resource_type, conn=conn)
    if len(_god_mode_grants) >= _GOD_MODE_CACHE_MAX:
        _god_mode_grants.clear()
    _god_mode_grants[key] = (now, ids)
    return ids


def _note_god_mode_hit(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> None:
    # The ENTIRE body is guarded: observability must never break
    # authorization. This runs on FastAPI's thread pool (require_* deps are
    # plain ``def``), so the lock-free dedup cache can race — e.g. the
    # eviction sweep iterating while another thread inserts raises
    # RuntimeError — and any such failure must degrade to a lost log line,
    # never to an exception out of ``can_access``.
    try:
        key = (user_id, resource_type, resource_id)
        # monotonic, matching _google_resync_last above: a wall-clock jump
        # (NTP step) must not widen or collapse a dedup window.
        now = time.monotonic()
        last = _god_mode_logged.get(key)
        if last is not None and (now - last) < _GOD_MODE_LOG_COOLDOWN_SECONDS:
            return
        # Do the grant lookup BEFORE marking the key as seen — if it raises
        # (transient DB blip), the key stays unrecorded so the NEXT request
        # retries instead of the cooldown swallowing this audit line for the
        # whole window (review finding on #1143).
        explicit = resource_id in _god_mode_allowed_ids(user_id, resource_type, now, conn=conn)
        if len(_god_mode_logged) >= _GOD_MODE_CACHE_MAX:
            cutoff = now - _GOD_MODE_LOG_COOLDOWN_SECONDS
            for k in [k for k, t in list(_god_mode_logged.items()) if t < cutoff]:
                _god_mode_logged.pop(k, None)
            if len(_god_mode_logged) >= _GOD_MODE_CACHE_MAX:
                # pathological churn: reset rather than grow without bound
                _god_mode_logged.clear()
        _god_mode_logged[key] = now
        if not explicit:
            logger.info(
                "god_mode_bypass: admin %s accessed %s:%s with no explicit group grant",
                user_id,
                resource_type,
                resource_id,
            )
    except Exception:
        logger.warning(
            "god_mode_bypass: observability failed for %s %s:%s",
            user_id,
            resource_type,
            resource_id,
            exc_info=True,
        )


def can_access(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """Generic access check. Admin short-circuits; otherwise group JOIN.

    God-mode hits on resources the admin has no explicit grant for are
    logged (deduplicated) via :func:`_note_god_mode_hit` — observability
    only, the decision is unchanged.

    Internal data-source tables (``agnes_sessions``/``agnes_telemetry``/
    ``agnes_audit``) used to be waved through here for every authenticated
    user. They are not any more: they belong to the seeded ``agnes-usage``
    data package and resolve through the standard path like any other
    resource, so an admin controls who may query usage data (design
    ``docs/superpowers/specs/2026-08-31-usage-package-and-per-turn-tokens-
    design.md`` §2, **BREAKING**). The row-level filter in
    ``connectors/internal/access.py`` is unchanged — it decides which rows,
    never whether the table is visible.

    ``conn`` honored when explicitly passed (test isolation); falls back
    to the global factory otherwise.
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    admin_id = _get_group_id_by_name(SYSTEM_ADMIN_GROUP, conn=conn)
    if admin_id is not None and admin_id in group_ids:
        from app.auth.elevation import elevation_paused

        if not elevation_paused(user_id):
            _note_god_mode_hit(user_id, resource_type, resource_id, conn=conn)
            return True
        # Elevation paused (consent gate): fall through to the explicit
        # group-grant path — the admin sees exactly what their grants say.

    if not group_ids:
        return False

    from src.repositories import use_pg, resource_grants_repo

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        return ResourceGrantsRepository(conn).has_grant(
            list(group_ids),
            resource_type,
            resource_id,
        )
    return resource_grants_repo().has_grant(
        list(group_ids),
        resource_type,
        resource_id,
    )


def _allowed_ids_for_user(
    user_id: str,
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Set of resource_ids the user is granted for ``resource_type``.

    Deliberately does NOT apply the Admin god-mode short-circuit — it
    reports only what was explicitly granted to a group the user belongs
    to. (There is no internal-table carve-out left to skip either: those
    tables are ordinary members of the ``agnes-usage`` package.) This is the single
    no-short-circuit grant primitive that both ``can_access`` (union/admin
    path) and ``compute_grant_intersection`` build on, so an admin-leak
    cannot reappear by drift.

    Routes through the repository factory (same split as ``can_access``) so
    DuckDB and Postgres behave identically — never raw SQL on ``conn``.
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    if not group_ids:
        return frozenset()
    from src.repositories import use_pg, resource_grants_repo

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        rows = ResourceGrantsRepository(conn).list_for_groups(
            list(group_ids),
            resource_type,
        )
    else:
        rows = resource_grants_repo().list_for_groups(
            list(group_ids),
            resource_type,
        )
    return frozenset(r["resource_id"] for r in rows)


def granted_store_entity_ids(
    user_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Store entities the user's groups have been granted, at any tier.

    A ``store_entity`` grant used to be accepted and mean nothing: the row was
    written, and the group still got 404 on the item, never saw it in a
    listing, and was refused an install with ``entity_not_approved``. This is
    the read side that makes the grant true — private stops meaning "nobody
    but me" and starts meaning "not everyone".

    Not admin-short-circuited, on purpose: this answers "which hidden entities
    should be SERVED to this person", and an admin's god-mode belongs at the
    authorization gate (``can_access``), not in the set of bundles written into
    their workspace. Same backend-split rule as :func:`_allowed_ids_for_user`.
    """
    return _allowed_ids_for_user(user_id, ResourceType.STORE_ENTITY.value, conn=conn)


def required_store_entity_ids(
    user_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Store entities the user's groups hold at ``requirement='required'``.

    Backs the two halves of the Required ("In stack, locked") tier for authored
    items: refusing an uninstall, and topping up a user who joined the group
    after the grant was written.

    Deliberately NOT admin-short-circuited: Required is about what a person must
    carry, not about what they may see, so an admin is subject to exactly the
    same locks as everyone else in the group. Same backend-split rule as
    :func:`_allowed_ids_for_user` — reads go through the repository factory.
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    if not group_ids:
        return frozenset()
    from app.resource_types import ResourceType
    from src.repositories import resource_grants_repo, use_pg

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        rows = ResourceGrantsRepository(conn).list_for_groups(
            list(group_ids),
            ResourceType.STORE_ENTITY.value,
        )
    else:
        rows = resource_grants_repo().list_for_groups(
            list(group_ids),
            ResourceType.STORE_ENTITY.value,
        )
    return frozenset(r["resource_id"] for r in rows if (r.get("requirement") or "available") == "required")


def has_explicit_grant(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """True iff one of the user's groups holds an explicit ``resource_grant``
    for ``(resource_type, resource_id)``.

    Unlike :func:`can_access`, this does **not** short-circuit for the Admin
    god-mode group — it reports only what was explicitly granted to a group
    the user belongs to.

    Use it for UI affordances that should reflect actual rollout state rather
    than *effective* access: e.g. hiding the cloud-chat nav link until chat is
    granted to a group, even for admins (who can still reach the page by URL,
    since the route guard uses :func:`can_access` and admins keep god-mode
    there). Never use it as a security gate — that is :func:`can_access`'s job.

    ``conn`` honored only in DuckDB-backend mode (test isolation); routes
    through the global factory otherwise — same backend-split rule as
    :func:`can_access`. (Previously this ran a raw ``conn.execute`` against
    ``resource_grants``, which read the stale/empty DuckDB table on a
    Postgres-backed instance and hid the nav link even when chat was granted.)
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    if not group_ids:
        return False
    from src.repositories import use_pg, resource_grants_repo

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        return ResourceGrantsRepository(conn).has_grant(
            list(group_ids),
            resource_type,
            resource_id,
        )
    return resource_grants_repo().has_grant(
        list(group_ids),
        resource_type,
        resource_id,
    )


def can_access_session(
    principal: "Principal",
    resource_type: str,
    resource_id: str,
) -> bool:
    """Restricted-principal access: membership in the live intersection.

    Must NOT call is_user_admin / can_access (PR checklist item) — both a
    ``SessionPrincipal``'s and an ``AgentPrincipal``'s ``intersection`` were
    already built without the admin short-circuit, so consulting either here
    would re-introduce god-mode through the back door.

    A ``ProducerPrincipal`` (or any future ``Principal`` outside this pair)
    has no ``intersection`` at all — its authority is enforced elsewhere
    (see ``app.auth.producer_token``'s module docstring) via a small,
    explicit, per-endpoint scope check, never this generic grant-table
    primitive. Fail closed here rather than raise ``AttributeError`` — this
    is what makes ``require_resource_access``/``require_collection_access``
    403 such a principal cleanly on every route that doesn't know it
    exists, instead of 500ing."""
    if not isinstance(principal, (SessionPrincipal, AgentPrincipal)):
        return False
    return resource_id in principal.intersection.get(resource_type, frozenset())


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def is_admin_session(user) -> bool:
    """Boolean form of :func:`require_admin`'s checks, for routes that
    resolve the user OPTIONALLY (``Depends(get_optional_user)``) and so
    cannot mount ``require_admin`` itself — e.g. the SSO provider's
    ``?mode=test`` leg, where the same route must also serve anonymous
    normal-mode traffic.

    Composes the exact primitives ``require_admin`` uses, in the same order:
    restricted-principal hard-deny first (an agent is a *restriction* of its
    owner, never an elevation), then the live Admin-membership read, then the
    elevation consent gate. Keep the two in lockstep — a check added to
    ``require_admin`` belongs here too (``tests/test_auth_providers.py``
    pins the shared primitive set).
    """
    if not user or isinstance(user, PRINCIPAL_TYPES):
        return False
    if not is_user_admin(user["id"]):
        return False
    from app.auth.elevation import elevation_paused

    return not elevation_paused()


def require_admin(
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Dependency: require user is in the Admin group. Raises 403 otherwise.

    Replaces the v9 ``require_role(Role.ADMIN)`` and
    ``require_internal_role("core.admin")`` thin wrappers. Same calling
    convention as before — endpoints write ``Depends(require_admin)`` (no
    parens) and receive the user dict.

    Any restricted principal (``SessionPrincipal`` co-session runner token,
    ``AgentPrincipal`` agent-session sandbox token) is HARD-DENIED before any
    ``is_user_admin`` check. This ordering is load-bearing: an
    ``AgentPrincipal`` carries its owner's user id, so a lookup that ran
    first would return True for an admin-owned agent and hand the sandbox
    god-mode. An agent is a *restriction* of its owner, never an elevation.

    Plain ``def`` (not ``async def``) so FastAPI offloads it to the anyio
    thread pool — the body is a sync ``is_user_admin`` RBAC read that must
    not run on the event loop (Tier 1, PR #188).
    """
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    if not is_user_admin(user["id"], conn):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    from app.auth.elevation import elevation_paused

    if elevation_paused():
        # Consent gate: the caller IS an admin but has paused their own
        # elevation for this browser. Distinct detail so clients can offer
        # a "re-enable admin mode" action instead of a generic 403.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_elevation_paused",
        )
    return user


def require_admin_or_producer(
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Like :func:`require_admin`, but ALSO accepts a ``ProducerPrincipal``
    — the corpus-extraction producer's own ingest/corrections callback
    credential (see ``app.auth.producer_token``). Used by
    ``POST /api/facts/ingest`` and ``GET /api/facts/corrections``, neither
    of which has a single path-scoped resource id to check up front the
    way ``require_admin_or_producer_connection`` / ``require_collection_
    write_or_producer_access`` below do — each of those two routes applies
    its own body-level (ingest) or documented (corrections) scope handling
    instead.
    """
    from app.auth.session_principal import ProducerPrincipal

    if isinstance(user, ProducerPrincipal):
        return user
    return require_admin(user=user, conn=conn)


def require_admin_or_producer_connection(path_template: str):
    """Dependency factory: admin (see :func:`require_admin`) OR a
    ``ProducerPrincipal`` whose own ``connection_id`` matches the path's
    resolved connection id — the corpus-map / scopes handoff a
    corpus-extraction producer calls back into (see
    ``app.auth.producer_token``). A producer token minted for a DIFFERENT
    connection, or any other non-admin credential, 403s.
    """

    def dep(
        request: Request,
        user=Depends(get_current_user),
        conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    ):
        from app.auth.session_principal import ProducerPrincipal

        if isinstance(user, ProducerPrincipal):
            try:
                resource_id = path_template.format(**request.path_params)
            except KeyError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        f"require_admin_or_producer_connection: path_template {path_template!r} "
                        f"references missing path_param {e}"
                    ),
                )
            if resource_id != user.connection_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="producer_wrong_connection",
                )
            return user
        return require_admin(user=user, conn=conn)

    return dep


def require_collection_write_or_producer_access(path_template: str):
    """Dependency factory mirroring :func:`require_collection_access`, but
    ALSO accepting a ``ProducerPrincipal`` scoped to this collection (the
    corpus-extraction producer's upload callback — see
    ``app.auth.producer_token``). Used ONLY by
    ``POST /api/collections/{collection_id}/files`` — every OTHER
    collection route keeps ``require_collection_access`` unchanged, so a
    producer token that authenticates here still 403s on
    read/delete/reingest/preview/raw for the SAME collection.
    """
    base_dep = require_collection_access(path_template)

    def dep(
        request: Request,
        user=Depends(get_current_user),
        conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    ):
        from app.auth.session_principal import ProducerPrincipal

        if isinstance(user, ProducerPrincipal):
            try:
                resource_id = path_template.format(**request.path_params)
            except KeyError as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=(
                        f"require_collection_write_or_producer_access: path_template {path_template!r} "
                        f"references missing path_param {e}"
                    ),
                )
            if resource_id not in user.collection_ids:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to collection {resource_id!r}",
                )
            return user
        return base_dep(request=request, user=user, conn=conn)

    return dep


def require_agent_profiles_enabled() -> None:
    """Dependency: 403 the whole request when the instance-level Agent
    profiles toggle is off.

    Mounted as a router-level ``dependencies=[...]`` entry (not per-endpoint)
    on all six agent routers (``agents_admin``, ``agent_runtime``,
    ``agent_sessions``, ``agent_webhooks``, ``agent_memory``,
    ``agent_schedules``) — the entire ``/api/v1/agents*`` +
    ``/api/v1/sessions*`` HTTP surface closes at once,
    same "close the whole surface" posture as Studio's
    ``get_studio_enabled()`` guard. Covers the CLI for free: `agnes agent`
    and `agnes chat` are pure clients of this API.

    Does NOT gate the in-process mechanisms a disabled instance must keep
    running — default-agent seeding, chat attribution to the default agent,
    the broker's agent policy — none of those call through these routers.
    One broker-replayed call DOES land here: the in-sandbox "remember" tool
    (``POST /api/v1/sessions/{id}/memories`` on ``agent_memory``). That is
    intended — memory notebooks are agent-profile surface — and the sandbox
    prompt stops advertising the tool when the flag is off
    (``app.chat.agent_profile``), so a well-behaved agent never hits the 403.
    """
    from app.instance_config import get_agent_profiles_enabled

    if not get_agent_profiles_enabled():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"kind": "agent_profiles_disabled"},
        )


def require_facts_enabled() -> None:
    """Dependency: 404 the whole request when ``facts.enabled`` is off.

    Mounted as a router-level ``dependencies=[...]`` entry (not
    per-endpoint) on ``app/api/facts.py``'s router — the entire
    ``/api/facts*`` surface disappears at once, same "close the whole
    surface" posture as ``require_agent_profiles_enabled`` above, but `404`
    rather than `403`: this is a genuinely new, off-by-default feature
    (fact-graph-over-Collections design doc §2), not an existing surface an
    operator deliberately switched off — a caller with no route to have
    ever discovered should see "not found", not "forbidden".
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("facts", "enabled", env_var="AGNES_FACTS_ENABLED", default=False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="facts_disabled")


def require_extraction_webhook_enabled() -> None:
    """Dependency: 404 the whole request when ``extraction_webhook.enabled``
    is off.

    Mounted as a router-level ``dependencies=[...]`` entry on
    ``app/api/sharepoint_webhooks.py``'s router — same "close the whole
    surface" posture as :func:`require_facts_enabled`, `404` rather than
    `403`: a caller (Microsoft Graph) that never had a route to discover
    should see "not found", not "forbidden". This is the ONLY gate on that
    router — it carries no session/PAT auth (Graph is the caller; its own
    ``clientState`` is verified inside the handler body, never a
    ``Depends`` chain).

    ``extraction_webhook`` is its OWN top-level config section, deliberately
    NOT nested under ``extraction`` — see the ``Switch`` entry's own comment
    in ``app/switches.py`` for why (mixing this always-editable switch into
    the locked ``extraction`` section would trip
    ``test_no_section_mixes_editable_and_locked_switches``).
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("extraction_webhook", "enabled", env_var="AGNES_EXTRACTION_WEBHOOK_ENABLED", default=False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="extraction_webhook_disabled")


def access_denied_detail(resource_type: ResourceType, resource_id: str) -> str:
    """Human-readable 403 detail for a resource-scoped denial.

    Most resource types have many instances, so naming the type and the id
    ("Access denied to table 'orders'") is exactly right. A **singleton**
    feature-gate resource does not: cloud chat is modelled as the resource
    type ``chat`` holding one resource whose id is the literal string
    ``"chat"`` (``id_format="chat"`` in ``app/resource_types.py``), so the
    generic form rendered as::

        Access denied to chat 'chat'

    — which reads like a bug and tells the reader nothing. The Studio
    builders print this straight into their assistant panel, so it is what a
    user without the grant actually sees on a page whose banner advertises
    "AI-ASSISTED". For that shape, name the feature instead.
    """
    from app.resource_types import RESOURCE_TYPES

    spec = RESOURCE_TYPES.get(resource_type)
    # A whole-feature switch is one whose `id_format` is a LITERAL id rather
    # than a shape (`"chat"`, not `"<table_id>"`). Keying on that instead of
    # on the id happening to equal the type's own name means the friendlier
    # wording follows the property that makes it true, and a feature switch
    # whose id differs from its type name still gets it. (Devin Review on
    # #1263.)
    id_format = (getattr(spec, "id_format", "") or "").strip()
    if id_format and "<" not in id_format and resource_id == id_format:
        label = getattr(spec, "display_name", None) or resource_type.value
        return f"Access denied to {label} — it is not enabled for your account."
    return f"Access denied to {resource_type.value} {resource_id!r}"


def require_resource_access(
    resource_type: ResourceType,
    path_template: str,
):
    """Dependency factory: require access to ``resource_type`` at the path
    derived from ``path_template`` formatted with the request's path_params.

    Example::

        @router.get("/marketplace/{slug}/plugins/{name}/install")
        async def install_plugin(
            slug: str, name: str,
            user = Depends(require_resource_access(
                ResourceType.MARKETPLACE_PLUGIN, "{slug}/{name}",
            )),
        ): ...

    Admin short-circuits — admins never need explicit grants. Non-admins
    raise 403 with the resolved path in the detail so the client knows what
    they failed against.
    """

    # Plain ``def`` (not ``async def``) so FastAPI offloads the returned
    # dependency to the anyio thread pool — its body is a sync RBAC read
    # (``can_access`` / ``can_access_session``) that must not run on the
    # event loop (Tier 1, PR #188).
    def dep(
        request: Request,
        user=Depends(get_current_user),
        conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    ):
        try:
            resource_id = path_template.format(**request.path_params)
        except KeyError as e:
            # Path template references a param the route doesn't expose —
            # programmer error, fail loud.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(f"require_resource_access: path_template {path_template!r} references missing path_param {e}"),
            )
        if isinstance(user, PRINCIPAL_TYPES):
            # Restricted principal (co-session or agent-session): the live
            # intersection is the sole authority — no admin short-circuit,
            # no owner-group resolution, and no ``user["id"]`` to subscript.
            allowed = can_access_session(user, resource_type.value, resource_id)
        else:
            allowed = can_access(user["id"], resource_type.value, resource_id, conn)
            if not allowed and _maybe_resync_google_groups(user["id"], user.get("email", "")):
                # Groups were refreshed — re-check with the updated snapshot.
                allowed = can_access(user["id"], resource_type.value, resource_id, conn)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=access_denied_detail(resource_type, resource_id),
            )
        return user

    return dep


def can_access_collection(
    user_id: str,
    collection_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """Collection access = admin OR group grant OR ownership.

    An upload is private to its creator: the user whose
    ``file_corpora.created_by`` matches can always reach it without a
    ``resource_grants`` row. Admins and group-granted callers keep access
    via the generic :func:`can_access` path. Ownership is checked here —
    not in the generic grant primitives — so it never leaks into other
    resource types.
    """
    if can_access(user_id, ResourceType.COLLECTION.value, collection_id, conn):
        return True
    from src.repositories import file_corpora_repo

    row = file_corpora_repo().get(collection_id)
    return bool(row and row.get("created_by") == user_id)


# Cap for the owned-collections lookup in ``accessible_collection_ids``.
# Explicit and high for the same reason as ``_GRANT_PROJECTION_LIMIT``
# (app/resource_types.py) and ``_COLLECTION_SCAN_LIMIT``
# (src/agent_scope_intersection.py): ``list()`` defaults to 200, and a silent
# truncation inside an authorization input fails *closed* — the owner of a
# collection past the cap is denied their own upload with no signal.
_OWNED_COLLECTION_SCAN_LIMIT = 100_000


def accessible_collection_ids(user, conn=None):
    """COLLECTION ids the caller may access — group grants (admin => None,
    meaning "all") unioned with the collections they own. ``None`` means
    every collection (admin). The list-surface counterpart to
    :func:`can_access_collection` (My Stack uploads, /library, search).

    A restricted ``Principal`` (co-session or agent-session) has no
    ``created_by`` identity to union in — its authority is the live
    intersection ``get_accessible_ids`` already returned, full stop.
    Consulting ownership here would either crash (``AgentPrincipal`` is a
    frozen dataclass, not a dict — no ``.get("id")``) or, worse, elevate an
    agent past its declared scope via its owner's uploads."""
    from src.rbac import get_accessible_ids

    granted = get_accessible_ids(user, ResourceType.COLLECTION.value, conn)
    if granted is None:
        return None  # admin — sees everything
    if isinstance(user, PRINCIPAL_TYPES):
        return granted
    user_id = user.get("id")
    if not user_id:
        return granted
    from src.repositories import file_corpora_repo

    owned = frozenset(
        # Filter in SQL: this runs on the authorization path, so keeping one
        # creator's rows out of a whole-table read matters (same reasoning as
        # ``_owned_collection_ids`` in src/agent_scope_intersection.py).
        r["id"]
        for r in file_corpora_repo().list(created_by=user_id, limit=_OWNED_COLLECTION_SCAN_LIMIT)
    )
    return frozenset(granted) | owned


def require_collection_access(path_template: str):
    """Dependency factory mirroring :func:`require_resource_access` for
    COLLECTION, but ownership (``created_by``) also grants access — so a
    user can manage the files of an upload they created without a group
    grant. Admin short-circuits; non-admins without grant or ownership
    raise 403.
    """

    def dep(
        request: Request,
        user=Depends(get_current_user),
        conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    ):
        try:
            resource_id = path_template.format(**request.path_params)
        except KeyError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"require_collection_access: path_template {path_template!r} references missing path_param {e}"
                ),
            )
        if isinstance(user, PRINCIPAL_TYPES):
            # Restricted principal (co-session or agent-session): the live
            # intersection is the sole authority — no ownership fallback,
            # and no ``user["id"]`` to subscript.
            allowed = can_access_session(user, ResourceType.COLLECTION.value, resource_id)
        else:
            allowed = can_access_collection(user["id"], resource_id, conn)
            if not allowed and _maybe_resync_google_groups(user["id"], user.get("email", "")):
                allowed = can_access_collection(user["id"], resource_id, conn)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(f"Access denied to collection {resource_id!r}"),
            )
        return user

    return dep


def mint_session_jwt(user_email: str, chat_id: str, *, ttl_seconds: int = 3600) -> str:
    """Mint a short-lived service JWT scoped to one chat session.

    Used by ChatManager._spawn_runner to inject AGNES_TOKEN into the
    subprocess env. The token is verified by the existing get_current_user
    dependency (app/auth/pat_resolver.py calls UserRepository.get_by_id on
    the ``sub`` claim), so ``sub`` MUST be the user's UUID — not the email.

    Encoded with the canonical auth secret (app/auth/jwt) so verify_token
    decodes it in every env — same contract as ``mint_co_session_jwt``. Not a
    bare ``JWT_SECRET_KEY`` env read: that skips the fail-closed resolver,
    signs with the committed dev constant when the var is unset, and under
    local dev misses the auto-generated key (never exported to the env) that
    the verifier actually holds.
    """
    import jwt  # PyJWT — already a project dependency

    from app.auth.jwt import ALGORITHM, get_signing_secret
    from src.repositories import users_repo

    # Factory-routed: honors use_pg() so a Postgres instance reads the live
    # PG users table, not the frozen DuckDB system file (#518).
    row = users_repo().get_by_email(user_email)
    if not row:
        raise ValueError(f"mint_session_jwt: user not found: {user_email!r}")
    user_id = row["id"]

    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "scope": "chat",
        "chat_session_id": chat_id,
        "email": user_email,
    }
    return jwt.encode(payload, get_signing_secret(), algorithm=ALGORITHM)


def mint_co_session_jwt(session_id: str, *, ttl: int = 3600) -> str:
    """Mint a co-session runner token. Carries ONLY chat_session_id +
    typ='co_session' + a synthetic sub (never a user UUID). No participant
    email list is baked in (SR-4) — the resolver reads chat_session_participants
    live as the sole source of truth, eliminating the stale-grant replay window.

    Encoded with the canonical auth secret (app/auth/jwt) so verify_token
    decodes it in every env.
    """
    from datetime import timedelta
    from app.auth.jwt import create_access_token

    return create_access_token(
        user_id=f"session:{session_id}",
        email="",  # no real identity; resolver never reads this
        expires_delta=timedelta(seconds=ttl),
        typ="co_session",
        # scope="chat" triggers the per-session BigQuery budget stash
        # (`_stash_chat_session_id_from_token`), same as the solo path — a
        # co-session is a chat session and must be capped too. (#849)
        extra_claims={"scope": "chat", "chat_session_id": session_id},
    )


def mint_agent_session_jwt(session_id: str, *, ttl: int = 3600) -> str:
    """Mint an agent-scoped session runner token (V1d). Carries ONLY
    chat_session_id + typ='agent_session' + a synthetic sub (never a user
    UUID or the agent_id) — the same no-baked-in-authority contract as
    ``mint_co_session_jwt``: no grants, no real user id, no agent identity.

    The resolver (``app.auth.pat_resolver``) rebuilds the agent's resolved
    authority live per request
    (``src.agent_scope_intersection.resolve_agent_authority``), so narrowing
    an agent or revoking a grant takes effect on the very next request — no
    stale-replay window.

    Encoded with the canonical auth secret (app/auth/jwt) so verify_token
    decodes it in every env.
    """
    from datetime import timedelta
    from app.auth.jwt import create_access_token

    return create_access_token(
        user_id=f"agent-session:{session_id}",
        email="",  # no real identity; resolver never reads this
        expires_delta=timedelta(seconds=ttl),
        typ="agent_session",
        # scope="chat" triggers the per-session BigQuery budget stash
        # (`_stash_chat_session_id_from_token`) — a brokered agent session
        # must stay capped too, same as the co-session and solo paths.
        extra_claims={"scope": "chat", "chat_session_id": session_id},
    )
