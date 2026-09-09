"""Set-intersection of co-session participants' grants, per ResourceType.

NEVER applies the Admin god-mode short-circuit (SR-1): each participant's
contribution is their real grant set from _allowed_ids_for_user. An admin
participant contributes the full set, so intersect(full, non_admin) ==
non_admin. Fail-closed: an empty participant list, an unknown participant,
or any participant with zero grants for a type collapses that type (or the
whole result) to empty.

PG-parity: resolves emails through the repository factory and reads grants
through _allowed_ids_for_user (which is factory-routed) — no raw SQL on the
passed conn.
"""
from __future__ import annotations

from typing import Optional

import duckdb

from app.resource_types import ResourceType


def compute_grant_intersection(
    participant_emails: list[str],
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict[str, frozenset[str]]:
    if not participant_emails:
        return {}
    from app.auth.access import _allowed_ids_for_user
    from src.repositories import use_pg, users_repo

    def _user_by_email(email: str):
        if conn is not None and not use_pg():
            from src.repositories.users import UserRepository
            return UserRepository(conn).get_by_email(email)
        return users_repo().get_by_email(email)

    user_ids: list[str] = []
    for email in participant_emails:
        row = _user_by_email(email)
        if not row:
            return {}  # unknown participant -> fail closed
        user_ids.append(row["id"])

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        sets = [_allowed_ids_for_user(uid, rt.value, conn) for uid in user_ids]
        acc: Optional[frozenset[str]] = None
        for s in sets:
            acc = s if acc is None else (acc & s)
        if acc:
            result[rt.value] = acc
    return result


def compute_viewer_intersection(
    owner_user_id: str,
    viewer_user_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> dict[str, frozenset[str]]:
    """``owner ∩ viewer`` reach per ResourceType — the authority of a hosted
    data app querying Agnes AS ITS VIEWER (``DataAppViewerPrincipal``).

    Deliberately NOT :func:`compute_grant_intersection`: that one intersects
    RAW ``resource_grants`` rows, and for ``TABLE`` those no longer surface a
    table to an analyst at all — table access flows through data packages
    (``src/rbac.py::can_access_table``), so on a package-shaped deployment
    the raw-grant intersection is empty and every viewer-mode query would be
    refused. This uses ``identity_ids_for_type`` from
    ``src/agent_scope_intersection`` instead — the same god-mode-free,
    package-aware reach ``resolve_agent_authority`` measures for an agent's
    owner (TABLE = per-table grants ∪ member tables of held packages;
    COLLECTION includes owned).

    Both sides are bounded on purpose. The viewer never exceeds their own
    grants (the point of viewer mode). The viewer never exceeds the OWNER's
    grants either: the app is owner-authored code that sees every response,
    so a better-privileged viewer would otherwise be a lens the owner could
    use to read tables the owner cannot. No admin short-circuit on either
    side (SR-1): an Admin viewer contributes only their explicit grants.

    Fail-closed like its sibling: an unknown user on either side -> ``{}``;
    a type where either side is empty is dropped. When owner and viewer are
    the same identity the reach is computed once.
    """
    if not owner_user_id or not viewer_user_id:
        return {}
    from src.agent_scope_intersection import _owner_package_ids, identity_ids_for_type
    from src.repositories import users_repo

    ids = [owner_user_id] if owner_user_id == viewer_user_id else [owner_user_id, viewer_user_id]
    for uid in ids:
        if not users_repo().get_by_id(uid):
            return {}  # unknown identity -> fail closed

    # One package resolution per identity, shared across every axis below
    # (`identity_ids_for_type` re-derives it per call otherwise).
    pkgs = {uid: _owner_package_ids(uid, conn) for uid in ids}

    result: dict[str, frozenset[str]] = {}
    for rt in ResourceType:
        acc: Optional[frozenset[str]] = None
        for uid in ids:
            reach = identity_ids_for_type(uid, rt.value, conn, owner_pkgs=pkgs[uid])
            acc = reach if acc is None else (acc & reach)
            if not acc:
                break
        if acc:
            result[rt.value] = acc
    return result
