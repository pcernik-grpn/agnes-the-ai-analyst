"""Service-account identity — shared constants, helper and typed errors
(issue #1534).

A service account is a ``users`` row flagged ``kind='service'`` (PG-only —
see ``migrations/versions/0096_users_kind.py``) rather than a bespoke table:
it exists purely to hold its own group grants and mint its own independently
revocable PATs, never to sign in interactively. This module is the ONE place
that constant and the "is this row a service account" question live, so the
guards scattered across ``app/auth/jwt.py`` (no interactive session),
``src/repositories/user_group_members(_pg).py`` (no Admin-group membership)
and ``src/repositories/users(_pg).py`` all agree on the same definition by
construction. Note that ``search_recent`` deliberately does NOT exclude a
service account: adding one to a group through the people picker is the only
way it acquires any authority at all.

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

# A third kind, for the identities Agnes seeds for ITSELF (issue #2256).
# Distinct from 'service': a service account is admin-provisioned and
# user-visible, while these are plumbing nobody creates, manages or sees.
# They are NOT flagged 'service' because that kind carries two behaviours
# they cannot survive — `semantic-drafter` mints an interactive token through
# the broker, and `scheduler` is a member of the Admin group.
SYSTEM_IDENTITY_KIND = "system"

#: The seeded identities that carry :data:`SYSTEM_IDENTITY_KIND`. Canonical
#: here rather than in the two `app.auth` modules that create them, because
#: the repositories and migration 0098 need the list without importing app
#: code. `tests/test_service_accounts.py` asserts the two sides agree.
SYSTEM_IDENTITY_EMAILS = (
    "scheduler@system.local",
    "semantic-drafter@system.local",
    "memory-curator@system.local",
)


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


def is_system_identity(user: Optional[Mapping[str, Any]]) -> bool:
    """Whether *user* is an identity Agnes seeded for itself.

    Reads the ``kind`` column first and falls back to the email, so the
    answer is right on a DuckDB-backed row (no ``kind`` column at all, the
    frozen post-A3 schema) and on a Postgres row that predates the 0098
    backfill.
    """
    if not user:
        return False
    if user.get("kind") == SYSTEM_IDENTITY_KIND:
        return True
    return (user.get("email") or "").strip().lower() in SYSTEM_IDENTITY_EMAILS


def is_person(user: Optional[Mapping[str, Any]]) -> bool:
    """Whether *user* is an account a person signs in as — the population
    ``scope='everyone'`` reaches, and the one "every account" counts.

    A service account is out because its authority is exactly the groups an
    admin put it in (issue #1534); an everyone-scoped grant would widen a
    long-lived PAT every time somebody shared something company-wide. A
    seeded system identity is out because nobody is behind it. Everything
    else is in, including an account in no group at all — which is the case
    the old Everyone-group model could not express.
    """
    if not user:
        return False
    return not is_service_account(user) and not is_system_identity(user)


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
