"""Service-account identity — shared constants, helper and typed errors
(issue #1534).

A service account is a ``users`` row flagged ``kind='service'`` (PG-only —
see ``migrations/versions/0096_users_kind.py``) rather than a bespoke table:
it exists purely to hold its own group grants and mint its own independently
revocable PATs, never to sign in interactively. This module is the ONE place
that constant and the "is this row a service account" question live, so the
guards scattered across ``app/auth/jwt.py`` (no interactive session),
``src/repositories/user_group_members(_pg).py`` (no Admin-group membership)
and ``src/repositories/users(_pg).py`` (excluded from ``search_recent``, the
people-picker feed) all agree on the same definition by construction.

Kept free of any DB/FastAPI import so every one of those modules — including
the low-level, framework-agnostic ``app/auth/jwt.py`` — can import it without
pulling in a heavier dependency chain.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# The two values the PG-only `users.kind` column may hold. 'human' is the
# column's own server default — every pre-existing row, and every row created
# through any door OTHER than `create_service_account`, is 'human'.
HUMAN_KIND = "human"
SERVICE_ACCOUNT_KIND = "service"


def is_service_account(user: Optional[Mapping[str, Any]]) -> bool:
    """Whether *user* (a ``users`` row dict, or ``None``) is a service account.

    Safe to call with a DuckDB-backed row too: that backend has no ``kind``
    column at all (frozen post-A3 schema), so the key is simply absent and
    this returns ``False`` — a service account cannot exist there in the
    first place (``create_service_account`` is PG-only), so "no such row can
    ever be flagged" and "this predicate is always false" agree by
    construction, not by a defensive special case.
    """
    if not user:
        return False
    return user.get("kind") == SERVICE_ACCOUNT_KIND


class ServiceAccountInteractiveLoginError(RuntimeError):
    """Raised by ``app.auth.jwt.create_access_token`` when asked to mint an
    interactive (``typ="session"``) token for a ``kind='service'`` user.

    This is the SINGLE choke point every login provider (Google, Microsoft,
    password, email magic-link, Keboola, SSO, the legacy ``/auth/token``
    endpoint, MCP-OAuth's authorization-code exchange and refresh) goes
    through to mint the credential a completed login hands back — none of
    them share any other common helper — so guarding here once covers all of
    them with no provider-by-provider patch. Minting a PAT *for* the service
    account (``typ="pat"``, via
    ``POST /api/admin/service-accounts/{id}/tokens``) is unaffected: that
    call always passes an explicit non-``"session"`` ``typ``.
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"user {user_id!r} is a service account and cannot hold an interactive session")


class ServiceAccountAdminGroupForbidden(Exception):
    """Raised by ``add_member`` when asked to add a ``kind='service'`` user
    to the system Admin group.

    A service account's authority is exactly its own group grants — handing
    it Admin's god-mode short-circuit (``app/auth/access.py``) would make
    every scope/grant check on every OTHER surface moot for it. Translated to
    a ``409 service_account_admin_forbidden`` by the app-wide handler in
    ``app/main.py``, the same pattern as ``RequiresPostgresBackend`` -> 501.
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        super().__init__(f"user {user_id!r} is a service account and cannot join the Admin group")
