"""Sync a user's Microsoft Entra ID group membership.

Mirrors ``app.auth.group_sync`` (the Google Workspace sync) — same shape:
``fetch_user_groups`` is the read primitive, ``apply_user_groups`` combines
that fetch with a prefix filter and the ``user_group_members`` write, and
both are called from the OAuth callback (``app/auth/providers/microsoft.py``)
on every sign-in.

The mechanics differ from Google's, because the two providers hand the
caller different things:

- Google's fetch uses keyless Domain-Wide Delegation — a service-account
  credential the OAuth callback never sees, resolved independently of the
  sign-in.
- Microsoft's fetch reuses the **delegated OAuth access token** the callback
  already holds from the token exchange — no separate credential. It calls
  ``GET https://graph.microsoft.com/v1.0/me/memberOf`` with
  ``Authorization: Bearer <access_token>`` and pages via ``@odata.nextLink``.

That token only carries this permission if the Entra app registration
requests it and an admin has consented — see
``docs/auth-microsoft-oauth.md`` for the exact delegated permission and the
admin-consent step. Because widening the requested OAuth scope reaches every
signed-in user (not just ones who benefit from it), the whole feature is
config-gated and OFF by default — see ``group_sync_enabled()``.

Entra groups are not required to be mail-enabled, so unlike Google (where
every group has an ``email``) the identifier mirrored into ``user_groups.name``
is the group's ``mail`` when present, else its ``displayName``. Both are
lower-cased before use so prefix matching and dedup behave the same way
Google's do.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import List

# requests is a hard Agnes dependency (unlike Google's optional
# google-api-python-client, imported locally in that sibling module because
# it's only needed when Workspace sync is actually configured) — safe to
# import at module level, and doing so lets tests monkeypatch
# `app.auth.microsoft_group_sync.requests.get` directly.
import requests

logger = logging.getLogger(__name__)

# Bypass the real Graph call entirely. Comma-separated group identifiers
# (mail or displayName, whichever this module would have used). Empty value
# -> []. Unset -> the real Graph HTTP path. Mirrors GOOGLE_ADMIN_SDK_MOCK_GROUPS.
MOCK_ENV = "AGNES_MICROSOFT_GRAPH_MOCK_GROUPS"

# Only groups whose identifier starts with this (case-insensitive) prefix are
# mirrored / count toward the login gate. Empty/unset -> every fetched group
# is mirrored. Mirrors AGNES_GOOGLE_GROUP_PREFIX.
PREFIX_ENV = "AGNES_MICROSOFT_GROUP_PREFIX"

#: The switch gating this whole feature — see app/switches.py. Read live
#: (not cached) so an operator's flip takes effect on the very next sign-in,
#: no restart required for the sync gate itself (the OAuth consent SCOPE is
#: a separate, restart-effect concern — see app/auth/providers/microsoft.py).
_SWITCH_NAME = "microsoft_group_sync"

GRAPH_MEMBER_OF_URL = "https://graph.microsoft.com/v1.0/me/memberOf"
_REQUEST_TIMEOUT_S = 10
# Defense in depth against a malformed/malicious @odata.nextLink loop —
# 50 pages * up to 999 groups/page is far more than any real tenant's
# per-user membership count.
_MAX_PAGES = 50


def group_sync_enabled() -> bool:
    """Whether the Microsoft Graph group sync feature is turned on.

    Resolution order (env > server-config overlay > instance.yaml base >
    default False) — the shared convention every switch in
    ``app.switches`` follows. See ``docs/feature-flags.md``.
    """
    from app.switches import switch_value

    return bool(switch_value(_SWITCH_NAME))


def fetch_user_groups(access_token: str) -> List[str]:
    """Return the identifiers of the groups ``access_token``'s owner belongs to.

    Fail-soft: returns ``[]`` on any error (missing/expired token, insufficient
    Graph permission, network outage, malformed response). The caller in
    ``apply_user_groups`` treats ``[]`` as "no data" and leaves the previous
    membership snapshot intact, exactly like the Google sibling.
    """
    mock = os.environ.get(MOCK_ENV)
    if mock is not None:
        return [g.strip() for g in mock.split(",") if g.strip()]
    return _fetch_real(access_token)


def _fetch_real(access_token: str) -> List[str]:
    if not access_token:
        logger.warning("Microsoft group fetch skipped: no access token on the OAuth callback")
        return []

    identifiers: List[str] = []
    url: str | None = GRAPH_MEMBER_OF_URL
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        for _ in range(_MAX_PAGES):
            if not url:
                break
            resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
            for entry in body.get("value", []) or []:
                # /me/memberOf can return directoryRole and other non-group
                # directory objects the caller belongs to — only mirror groups.
                if entry.get("@odata.type") != "#microsoft.graph.group":
                    continue
                identifier = str(entry.get("mail") or entry.get("displayName") or "").strip()
                if identifier:
                    identifiers.append(identifier)
            url = body.get("@odata.nextLink")
        else:
            if url:
                logger.warning(
                    "Microsoft Graph memberOf: exceeded %d pages, stopping with a partial result",
                    _MAX_PAGES,
                )
    except requests.RequestException as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Microsoft Graph group fetch failed: %s", e)
        return []
    except Exception as e:  # noqa: BLE001 - fail-soft by design (e.g. malformed JSON body)
        logger.warning("Microsoft Graph group fetch failed unexpectedly: %s", e)
        return []

    return identifiers


@dataclass
class SyncResult:
    """Outcome of an ``apply_user_groups`` call. Mirrors
    ``app.auth.group_sync.SyncResult`` field-for-field — see that class's
    docstring for what each field means; the semantics are identical here,
    substituting "Graph" for "Admin SDK" and ``source='microsoft_sync'`` for
    ``source='google_sync'``.
    """

    fetched: List[str] = field(default_factory=list)
    relevant: List[str] = field(default_factory=list)
    denied: bool = False
    soft_failed: bool = False
    applied: bool = False


def apply_user_groups(user_id: str, email: str, access_token: str, conn) -> SyncResult:
    """Refresh this user's ``source='microsoft_sync'`` group memberships.

    Called from the OAuth callback on every Microsoft sign-in. Fail-soft:
    any internal error (Graph fetch, repo write, group ensure) is logged and
    returns a ``soft_failed=True`` result rather than raising — the caller
    treats the call as a no-op and login proceeds unaffected.

    A no-op (default ``SyncResult()``, nothing fetched or written) when
    ``group_sync_enabled()`` is False — the common case, since the feature
    defaults off. ``fetch_user_groups`` is not even called in that case, so
    an instance that never opted in makes no Graph traffic on sign-in.

    ``conn`` is accepted for call-site symmetry with
    ``app.auth.group_sync.apply_user_groups`` (whose OAuth callback opens a
    DuckDB system-db handle on that backend) but unused here — reads/writes
    route through the ``user_groups_repo()`` / ``user_group_members_repo()``
    factory pair, which respects the active backend regardless of what
    ``conn`` points at. The Microsoft OAuth callback always passes ``None``.
    """
    del conn  # unused — see docstring
    result = SyncResult()

    if not group_sync_enabled():
        logger.debug("Microsoft group sync disabled; skipping Graph call for %s", email)
        return result

    from src.repositories import user_group_members_repo, user_groups_repo

    prefix = os.environ.get(PREFIX_ENV, "").strip().lower()

    try:
        group_identifiers = fetch_user_groups(access_token)
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Microsoft group fetch raised for %s: %s", email, e)
        result.soft_failed = True
        return result

    if not group_identifiers:
        logger.info(
            "Microsoft group sync for %s: empty result, preserving existing memberships",
            email,
        )
        result.soft_failed = True
        return result

    fetched = [g.lower() for g in group_identifiers]
    result.fetched = fetched

    if prefix:
        relevant = [g for g in fetched if g.startswith(prefix)]
    else:
        relevant = list(fetched)
    result.relevant = relevant

    if prefix and not relevant:
        # Mirrors the Google gate: existing source='microsoft_sync' rows are
        # NOT cleared here (a prefix mismatch may be transient — Graph lag,
        # an operator typo in PREFIX_ENV). The OAuth callback turns this
        # into a /login?error=microsoft_not_in_allowed_group redirect.
        logger.info(
            "Microsoft group sync for %s denied: no group with prefix %r in %s",
            email,
            prefix,
            fetched,
        )
        result.denied = True
        return result

    try:
        ug_repo = user_groups_repo()
        members_repo = user_group_members_repo()

        group_ids = [ug_repo.ensure(name, created_by="system:microsoft-sync")["id"] for name in relevant]
        members_repo.replace_microsoft_sync_groups(user_id, group_ids, added_by="system:microsoft-sync")
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Microsoft group write failed for %s: %s", email, e)
        result.soft_failed = True
        return result

    result.applied = True
    logger.info(
        "Microsoft group sync for %s: %d group(s) (filtered from %d fetched, prefix=%r) [%s]",
        email,
        len(group_ids),
        len(fetched),
        prefix,
        ", ".join(relevant),
    )
    return result
