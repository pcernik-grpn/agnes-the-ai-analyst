"""SharePoint ACL-mirroring: permission classification + naming (spec
2026-08-28-sharepoint-acl-mirroring-design.md §2, §8.2).

This module is the pure, I/O-free half of the ACL-mirroring sync: given
already-fetched Microsoft Graph ``permission`` objects for a scope root
(``connectors.sharepoint.graph_client.list_item_permissions``), it decides
which grantees are **honored** (mirrored into an Agnes group) and which are
**out** — counted but never granted, per §8.2's table, so that
under-sharing is visible on the source card rather than silently wrong. The
sync job body that reads Graph, calls this classifier, and writes the
resulting groups/memberships/grants is added to this same module by a later
task; this file has no network calls and no repository imports.

**Sentinel-segregation contract.** Every row the sync writes —
``user_groups`` (``created_by``), ``user_group_members`` (``source``),
``resource_grants`` (``assigned_by``) — is tagged with exactly the two
constants below, defined here once and imported everywhere else (the
worker job, the wizard/API, the contract tests). Reconciliation (the
sync's own DELETE-then-INSERT / diff-and-write passes) finds and touches
ONLY rows carrying these tags, so a hand-assigned admin grant, group, or
membership is never clobbered by this sync, and vice versa — the same
writer-segregation pattern ``google_sync``/``microsoft_sync`` already use
(see ``docs/auth-groups.md``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ACL_SYNC_SENTINEL = "system:sharepoint-acl-sync"
"""Tag written to ``user_groups.created_by`` and ``resource_grants.assigned_by``
for every group/grant this sync creates or reconciles."""

ACL_SYNC_SOURCE = "sharepoint_sync"
"""Tag written to ``user_group_members.source`` for every membership row
this sync writes — the scope ``replace_group_members_for_source`` DELETEs
within (never another source's rows for the same group)."""


def entra_group_name(oid: str) -> str:
    """Canonical ``user_groups.name`` for an Entra security/M365 group found
    on a scope's role assignments. Deliberately the same naming the parent
    spec reserves for the later ``/me/memberOf`` sync, so the two converge
    on the same row instead of creating parallel near-duplicates."""
    return f"entra:{oid}"


def direct_group_name(source_scope_id: str) -> str:
    """Canonical ``user_groups.name`` for the synthetic group that collects
    a scope's direct (non-group) user role assignments — one group per
    scope, not one per user, so grant counts stay O(principal-classes)."""
    return f"sp-direct:{source_scope_id}"


@dataclass
class Classified:
    """Result of classifying one scope root's Graph ``permission`` list."""

    entra_group_oids: List[str] = field(default_factory=list)
    direct_user_emails: List[str] = field(default_factory=list)
    unhonored: List[Dict[str, str]] = field(default_factory=list)


def _permission_email(user: Dict[str, Any]) -> Optional[str]:
    """Email preference order per §8.2: ``email``, then ``mail``, then
    ``userPrincipalName`` (usually the primary SMTP address — the
    case-insensitive join downstream absorbs any case drift between it and
    the Agnes account's login email)."""
    return user.get("email") or user.get("mail") or user.get("userPrincipalName") or None


def _dedupe(values: List[str]) -> List[str]:
    """Stable dedupe — first occurrence wins, order preserved."""
    seen: set = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def classify_permissions(perms: List[Dict[str, Any]]) -> Classified:
    """Classify one scope root's Graph ``permission`` objects per the §8.2
    table.

    Honored: a direct user role assignment with a resolvable email, and an
    Entra security/M365 group assignment. Out — counted, never granted
    (fail closed): SharePoint site groups (not enumerable through the
    app-only Graph surface this connector uses), sharing links ("specific
    people" and "people in your organization"), anonymous links,
    external/guest users (``userPrincipalName`` containing ``#EXT#``),
    application principals, and a user grantee with no resolvable email.
    """
    entra_group_oids: List[str] = []
    direct_user_emails: List[str] = []
    unhonored: List[Dict[str, str]] = []

    for perm in perms:
        link = perm.get("link")
        if link:
            scope = link.get("scope")
            if scope == "anonymous":
                unhonored.append({"kind": "anonymous_link", "detail": "anonymous link"})
            elif scope == "organization":
                unhonored.append({"kind": "org_link", "detail": "people-in-your-organization link"})
            else:
                unhonored.append(
                    {
                        "kind": "sharing_link",
                        "detail": f"specific-people sharing link (scope={scope})",
                    }
                )
            continue

        granted = perm.get("grantedToV2") or {}

        if "siteGroup" in granted:
            site_group = granted.get("siteGroup") or {}
            detail = site_group.get("displayName") or site_group.get("id") or "site group"
            unhonored.append({"kind": "site_group", "detail": str(detail)})
            continue

        if "application" in granted:
            application = granted.get("application") or {}
            detail = application.get("displayName") or application.get("id") or "application"
            unhonored.append({"kind": "application", "detail": str(detail)})
            continue

        if "user" in granted:
            user = granted.get("user") or {}
            upn = user.get("userPrincipalName") or ""
            if "#EXT#" in upn:
                unhonored.append({"kind": "external_guest", "detail": upn})
                continue
            email = _permission_email(user)
            if not email:
                detail = str(user.get("id") or "user")
                unhonored.append({"kind": "no_email", "detail": detail})
                continue
            direct_user_emails.append(email)
            continue

        if "group" in granted:
            group = granted.get("group") or {}
            oid = group.get("id")
            if oid:
                entra_group_oids.append(oid)
            continue

        # No recognized grantee shape — count it rather than drop it silently.
        unhonored.append({"kind": "unknown", "detail": str(perm.get("id") or "permission")})

    return Classified(
        entra_group_oids=_dedupe(entra_group_oids),
        direct_user_emails=_dedupe(direct_user_emails),
        unhonored=unhonored,
    )
