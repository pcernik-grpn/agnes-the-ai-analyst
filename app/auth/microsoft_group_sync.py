"""Sync a user's Microsoft Entra ID group membership.

Mirrors ``app.auth.group_sync`` (the Google Workspace sync) — same shape:
``fetch_user_groups`` is the read primitive, ``apply_user_groups`` combines
that fetch with a prefix filter and the ``user_group_members`` write, and
both are called from the OAuth callback (``app/auth/providers/microsoft.py``)
on every sign-in. **This is the ONLY writer that syncs Entra group
membership on login** — the separate, runtime-configured ``sso``
(``entra_oidc``) provider (``app/auth/providers/sso.py``) does not call
into this module; see that module's own docstring and
``docs/auth-microsoft-oauth.md`` for why.

The mechanics differ from Google's, because the two providers hand the
caller different things:

- Google's fetch uses keyless Domain-Wide Delegation — a service-account
  credential the OAuth callback never sees, resolved independently of the
  sign-in.
- Microsoft's fetch reuses the **delegated OAuth access token** the callback
  already holds from the token exchange — no separate credential. It calls
  ``GET https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group``
  with ``Authorization: Bearer <access_token>``, pages via
  ``@odata.nextLink``, and requests ``id``/``mail``/``displayName`` per
  group.

That token only carries this permission if the Entra app registration
requests it and an admin has consented — see
``docs/auth-microsoft-oauth.md`` for the exact delegated permission and the
admin-consent step. Because widening the requested OAuth scope reaches every
signed-in user (not just ones who benefit from it), the whole feature is
config-gated and OFF by default — see ``group_sync_enabled()``.

**Identity-scheme unification (2026-09 fix).** A group is now mirrored into
``user_groups`` keyed on its Entra object id — ``entra:<id>``, via
:func:`src.entra_identity.entra_group_name` — the SAME key the SharePoint
ACL mirror (``connectors.sharepoint.acl_sync``) already uses for the exact
same Entra group. Before this fix the two writers disagreed: this module
keyed a group by its lower-cased ``mail``/``displayName``, the ACL mirror by
``entra:<object-id>``, so the same Entra group landed as two different,
never-converging ``user_groups`` rows. ``mail``/``displayName`` still drive
the prefix filter (below) and are recorded in the group's ``description``
for readability, but the row identity is the id. The **transitive**
membership walk (``/transitiveMemberOf`` instead of the old non-transitive
``/memberOf``) also now matches the ACL mirror's own
``transitiveMembers`` expansion — a user in a NESTED group is honored on
both sides identically.

**Migration for a pre-fix install:** the first time a previously-synced
group is seen again after this fix, :func:`_ensure_entra_group` finds the
OLD mail/displayName-keyed row (only when IT ALSO carries this module's own
``created_by`` sentinel — never seizes an unrelated admin-created group that
happens to share the name) and renames it in place to the new
``entra:<id>`` key, preserving its members and any resource grants — never
creating a duplicate.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

# requests is a hard Agnes dependency (unlike Google's optional
# google-api-python-client, imported locally in that sibling module because
# it's only needed when Workspace sync is actually configured) — safe to
# import at module level, and doing so lets tests monkeypatch
# `app.auth.microsoft_group_sync.requests.get` directly.
import requests

from src.entra_identity import entra_group_name

logger = logging.getLogger(__name__)

# Bypass the real Graph call entirely. Comma-separated ``id|mail|displayName``
# triples (``mail``/``displayName`` optional — e.g. ``id||My Group`` or
# ``id|mail@x``). Empty value -> []. Unset -> the real Graph HTTP path.
# Mirrors GOOGLE_ADMIN_SDK_MOCK_GROUPS in spirit, widened to carry the id
# every fetched group now needs.
MOCK_ENV = "AGNES_MICROSOFT_GRAPH_MOCK_GROUPS"

# Only groups whose mail/displayName starts with this (case-insensitive)
# prefix are mirrored / count toward the login gate. Empty/unset -> every
# fetched group is mirrored. Mirrors AGNES_GOOGLE_GROUP_PREFIX.
PREFIX_ENV = "AGNES_MICROSOFT_GROUP_PREFIX"

#: The switch gating this whole feature — see app/switches.py. Read live
#: (not cached) so an operator's flip takes effect on the very next sign-in,
#: no restart required for the sync gate itself (the OAuth consent SCOPE is
#: a separate, restart-effect concern — see app/auth/providers/microsoft.py).
_SWITCH_NAME = "microsoft_group_sync"

#: Transitive so a user in a NESTED group is honored — same expansion the
#: SharePoint ACL mirror's own ``list_group_transitive_members`` performs
#: (``connectors/sharepoint/graph_client.py``). The
#: ``/microsoft.graph.group`` type-cast segment scopes the navigation
#: property to groups only, same intent as this module's own
#: ``@odata.type`` filter below (kept as defense in depth).
GRAPH_TRANSITIVE_MEMBER_OF_GROUPS_URL = "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group"
_SELECT_FIELDS = "id,mail,displayName"
_REQUEST_TIMEOUT_S = 10
# Defense in depth against a malformed/malicious @odata.nextLink loop —
# 50 pages * up to 999 groups/page is far more than any real tenant's
# per-user membership count.
_MAX_PAGES = 50

#: Writer sentinel this module stamps on every ``user_groups.created_by`` it
#: creates — also the ONLY ``created_by`` value :func:`_ensure_entra_group`
#: will rename-in-place during the identity-scheme migration (never an
#: unrelated admin-created group that happens to share a legacy name).
MICROSOFT_SYNC_SENTINEL = "system:microsoft-sync"


def group_sync_enabled() -> bool:
    """Whether the Microsoft Graph group sync feature is turned on.

    Resolution order (env > server-config overlay > instance.yaml base >
    default False) — the shared convention every switch in
    ``app.switches`` follows. See ``docs/feature-flags.md``.
    """
    from app.switches import switch_value

    return bool(switch_value(_SWITCH_NAME))


def _group_label(group: Dict[str, str]) -> str:
    """The lower-cased identifier the prefix filter matches against and the
    ``fetched``/``relevant`` report lists carry — ``mail`` when present,
    else ``displayName``, else the object id itself (a group with neither,
    which real Graph data should not produce but a hand-rolled mock might)."""
    return (group.get("mail") or group.get("displayName") or group.get("id") or "").strip().lower()


def fetch_user_groups(access_token: str) -> List[Dict[str, str]]:
    """Return ``{"id", "mail", "displayName"}`` for every group
    ``access_token``'s owner transitively belongs to.

    Fail-soft: returns ``[]`` on any error (missing/expired token, insufficient
    Graph permission, network outage, malformed response). The caller in
    ``apply_user_groups`` treats ``[]`` as "no data" and leaves the previous
    membership snapshot intact, exactly like the Google sibling.
    """
    mock = os.environ.get(MOCK_ENV)
    if mock is not None:
        return _parse_mock_groups(mock)
    return _fetch_real(access_token)


def _parse_mock_groups(raw: str) -> List[Dict[str, str]]:
    """``MOCK_ENV``'s format: comma-separated ``id|mail|displayName``
    entries, ``mail``/``displayName`` optional (``id``, ``id|mail`` and
    ``id||displayName`` are all valid). An entry with no ``id`` is skipped —
    the id is what group identity now hangs on."""
    groups: List[Dict[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("|")
        object_id = parts[0].strip()
        if not object_id:
            continue
        mail = parts[1].strip() if len(parts) > 1 else ""
        display_name = parts[2].strip() if len(parts) > 2 else ""
        groups.append({"id": object_id, "mail": mail, "displayName": display_name})
    return groups


def _fetch_real(access_token: str) -> List[Dict[str, str]]:
    if not access_token:
        logger.warning("Microsoft group fetch skipped: no access token on the OAuth callback")
        return []

    groups: List[Dict[str, str]] = []
    url: str | None = f"{GRAPH_TRANSITIVE_MEMBER_OF_GROUPS_URL}?$select={_SELECT_FIELDS}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        for _ in range(_MAX_PAGES):
            if not url:
                break
            resp = requests.get(url, headers=headers, timeout=_REQUEST_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
            for entry in body.get("value", []) or []:
                # The `/microsoft.graph.group` cast already scopes this to
                # groups; the explicit check is defense in depth against a
                # non-conforming response.
                odata_type = entry.get("@odata.type")
                if odata_type is not None and odata_type != "#microsoft.graph.group":
                    continue
                object_id = str(entry.get("id") or "").strip()
                if not object_id:
                    continue
                groups.append(
                    {
                        "id": object_id,
                        "mail": str(entry.get("mail") or "").strip(),
                        "displayName": str(entry.get("displayName") or "").strip(),
                    }
                )
            url = body.get("@odata.nextLink")
        else:
            if url:
                logger.warning(
                    "Microsoft Graph transitiveMemberOf: exceeded %d pages, stopping with a partial result",
                    _MAX_PAGES,
                )
    except requests.RequestException as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Microsoft Graph group fetch failed: %s", e)
        return []
    except Exception as e:  # noqa: BLE001 - fail-soft by design (e.g. malformed JSON body)
        logger.warning("Microsoft Graph group fetch failed unexpectedly: %s", e)
        return []

    return groups


def _ensure_entra_group(ug_repo: Any, group: Dict[str, str]) -> Dict[str, Any]:
    """Get-or-create the ``entra:<id>``-keyed row for ``group``, migrating a
    legacy mail/displayName-keyed row in place the first time this group
    syncs since the identity-scheme unification (module docstring).

    A row already at the new key wins outright (steady state, or a
    fresh install that never had a legacy row). Otherwise, a legacy row is
    looked up by ``mail`` then ``displayName`` (lower-cased, the OLD keying
    this module used) and renamed in place — members and grants untouched —
    but ONLY when that row's ``created_by`` is THIS module's own sentinel:
    an admin-created group that happens to share the old name is never
    seized. No legacy row found -> plain create."""
    object_id = group["id"]
    new_key = entra_group_name(object_id)
    existing = ug_repo.get_by_name(new_key)
    if existing:
        return existing

    mail = group.get("mail") or ""
    display_name = group.get("displayName") or ""
    label = mail or display_name or object_id
    description = f"Mirrored from Microsoft Entra group {label} ({object_id})"

    for legacy_key in (mail.strip().lower(), display_name.strip().lower()):
        if not legacy_key:
            continue
        legacy = ug_repo.get_by_name(legacy_key)
        if legacy is not None and legacy.get("created_by") == MICROSOFT_SYNC_SENTINEL:
            ug_repo.update(legacy["id"], name=new_key, description=description)
            logger.info(
                "Microsoft group sync: migrated legacy group %r -> %r (id=%s)",
                legacy_key,
                new_key,
                legacy["id"],
            )
            return ug_repo.get(legacy["id"])

    return ug_repo.ensure(new_key, description=description, created_by=MICROSOFT_SYNC_SENTINEL)


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
        fetched_groups = fetch_user_groups(access_token)
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Microsoft group fetch raised for %s: %s", email, e)
        result.soft_failed = True
        return result

    if not fetched_groups:
        logger.info(
            "Microsoft group sync for %s: empty result, preserving existing memberships",
            email,
        )
        result.soft_failed = True
        return result

    fetched_labels = [_group_label(g) for g in fetched_groups]
    result.fetched = fetched_labels

    if prefix:
        relevant_groups = [g for g in fetched_groups if _group_label(g).startswith(prefix)]
    else:
        relevant_groups = list(fetched_groups)
    result.relevant = [_group_label(g) for g in relevant_groups]

    if prefix and not relevant_groups:
        # Mirrors the Google gate: existing source='microsoft_sync' rows are
        # NOT cleared here (a prefix mismatch may be transient — Graph lag,
        # an operator typo in PREFIX_ENV). The OAuth callback turns this
        # into a /login?error=microsoft_not_in_allowed_group redirect.
        logger.info(
            "Microsoft group sync for %s denied: no group with prefix %r in %s",
            email,
            prefix,
            fetched_labels,
        )
        result.denied = True
        return result

    try:
        ug_repo = user_groups_repo()
        members_repo = user_group_members_repo()

        group_ids = [_ensure_entra_group(ug_repo, g)["id"] for g in relevant_groups]
        members_repo.replace_microsoft_sync_groups(user_id, group_ids, added_by=MICROSOFT_SYNC_SENTINEL)
    except Exception as e:  # noqa: BLE001 - fail-soft by design
        logger.warning("Microsoft group write failed for %s: %s", email, e)
        result.soft_failed = True
        return result

    result.applied = True
    logger.info(
        "Microsoft group sync for %s: %d group(s) (filtered from %d fetched, prefix=%r) [%s]",
        email,
        len(group_ids),
        len(fetched_labels),
        prefix,
        ", ".join(result.relevant),
    )
    return result
