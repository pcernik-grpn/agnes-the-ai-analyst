"""SharePoint ACL-mirroring: permission classification + naming (spec
2026-08-28-sharepoint-acl-mirroring-design.md §2, §8.2).

This module has two halves. Classification (below) is pure and I/O-free:
given already-fetched Microsoft Graph ``permission`` objects for a scope root
(``connectors.sharepoint.graph_client.list_item_permissions``), it decides
which grantees are **honored** (mirrored into an Agnes group) and which are
**out** — counted but never granted, per §8.2's table, so that
under-sharing is visible on the source card rather than silently wrong.

The sync job body (2026-08-30 plan, Task 4 — ``run_acl_sync``) is the other
half: it reads Graph (via :mod:`connectors.sharepoint.graph_client`),
classifies each scope root's permissions with the functions below, and
writes the resulting groups (:mod:`src.repositories.user_groups`),
memberships (:mod:`src.repositories.user_group_members`
.replace_group_members_for_source) and collection grants
(:mod:`src.repositories.resource_grants`) — one worker job kind
(``sharepoint-acl-sync``, ``app/worker/kinds.py``), enqueued nightly by
``services/scheduler/__main__.py`` and on demand by an admin "sync now"
action (spec §5.1). Entirely behind the single ``sharepoint`` switch (default off);
see :func:`run_acl_sync`'s own docstring for the sync lifecycle.

**Gap closed (2026-08-30 plan, Task 5):** a confirmed scope row
(``app/api/admin_sharepoint.py``'s ``ConfirmScopeBody``/``confirm_scope``)
now persists an optional ``drive_id`` alongside ``{source_scope_id,
display_path, anonymize, collection_id, access_mode}`` — REQUIRED
(``400 missing_drive_id``) when ``access_mode='mirrored'``, since addressing
a Graph item needs both a drive id and an item id and nothing in this repo
resolves the former from the latter alone. A scope confirmed before Task 5
(or a manual-mode scope that never set one) still has no ``drive_id`` on its
row; ``run_acl_sync`` reads the OPTIONAL ``scope["drive_id"]`` and, when it
is absent, skips that scope with a typed ``missing_drive_id`` per-scope
error rather than crashing the whole connection's run — the same
degradation as before, now only reachable for scopes confirmed before this
field existed.

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

**Subtree sweep (2026-08-30 plan, Task 7 — ``run_subtree_sweep``), a THIRD
half added here.** Spec §3(b)'s recommendation: a SharePoint folder can
break permission inheritance at a grain finer than Agnes's collection model
can express, so this walks each ``access_mode='mirrored'`` scope's folder
tree (``graph_client.list_item_children`` + the already-``$batch``-based
``graph_client.probe_unique_permissions``) and EXCLUDES — never descends
into, never crawls — every broken-inheritance subtree it finds, by ROOT.
Detection only; acting on the exclusion list is the crawl's job — this
module writes the list, and the built-in crawler
(``connectors/sharepoint/crawler.py``) honors it in-process (fail-closed:
an unresolvable exclusion refuses the scope). The external-producer env
handoff (``AGNES_SP_EXCLUDED_SUBTREE_IDS``) was removed with external
mode (2026-08-31, builtin-only decision).
Own job kind (``sharepoint-subtree-sweep``), own weekly scheduler cadence
(§6.2's cost model: a full probe pass over a large library is multi-hour,
not a nightly job) — see :func:`run_subtree_sweep`'s own docstring.

**Sweep v2 — file probes, permission zones, retroactive cleanup (2026-08-31
plan, Tasks 3/4/6).** Three additions layered onto the sweep above without
changing its detection-only mandate at the folder level:

1. The walk now probes **files** too, not just folders — a file with
   unique permissions is excluded (``kind="file"``, fail-closed, never
   mirrored per-file — see :func:`_walk_subtree_sweep`).
2. A broken-inheritance FOLDER is no longer merely excluded — it is ALWAYS
   promoted to its own **permission zone** (whenever the connector itself
   is on — there is no separate opt-in switch for this anymore, see
   :func:`_walk_subtree_sweep`'s own docstring): a fresh, grant-less collection
   (:func:`_create_zone_collection`) that :func:`_sync_connection` (Task 4)
   mirrors an ACL into exactly like a normal scope, and the walk
   *descends* into it instead of stopping (:func:`_reconcile_zones`); a
   zone whose root re-links inheritance is marked ``status="dissolved"``,
   never removed from the list (audit trail).
3. :func:`_cleanup_connection_content` (Task 6) retroactively purges
   already-ingested ``corpus_files`` rows that fall under a subtree/file
   excluded — or a zone activated — on THIS OR ANY PRIOR run, and fully
   retires a dissolved zone's collection (files, grants, the collection
   row itself) — using the exact machinery ``DELETE /files/{id}`` uses
   (``app.api.collections._purge_file_row`` and friends), never a bespoke
   delete path.

Zones live in the connection's OWN top-level ``config["acl_zones"]`` key —
never inside ``scopes`` rows (that stays the wizard's own automated-writer
boundary, spec §5's race contract) — and are exported via :func:`zone_rows`
/ :func:`active_zone_rows` for every other task to read.

**2026-09 fixes (live-tenant readiness pass — a read-only audit found these
because no scope had ever been ``mirrored`` in practice, so none had fired).**
Four to this module specifically: (1) ``classify_permissions`` now honors a
``grantedToV2.siteUser`` grantee (a claims-based membership-provider user)
when it resolves to an email, instead of falling into the generic
``unknown`` kind; (2) an optional ``site_group_map`` parameter honors a
SharePoint site group (Owners/Members/Visitors, or custom — never
enumerable through the app-only Graph surface this connector uses) mapped
to existing Agnes group(s) via the connection's own
``config["acl_site_group_map"]`` (``app/api/admin_sharepoint.py``'s
``set_acl_site_group_map``); (3) :func:`_sync_scope` no longer reconciles
its own collection's grants — :func:`_sync_connection` accumulates every
scope's (and zone's) honored group set per ``collection_id`` across a whole
run and reconciles each collection exactly ONCE against the union, fixing
two mirrored scopes sharing one collection (a bulk-add ``collection_id``
target) flip-flopping each other's grants; (4) ``entra_group_name`` moved to
:mod:`src.entra_identity`, the single naming rule this module and the
login-time Microsoft Entra ID group sync (``app.auth.microsoft_group_sync``)
now BOTH use, so the same Entra group converges on one ``user_groups`` row
regardless of which writer sees it first — see that module's own docstring
for the full identity-scheme unification and its legacy-key migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.resource_types import ResourceType
from connectors.sharepoint import graph_client
from connectors.sharepoint.graph_client import SharePointGraphError
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from src.audit_helpers import log_safe
from src.entra_identity import entra_group_name
from src.repositories import (
    RequiresPostgresBackend,
    corpus_file_sources_repo,
    corpus_files_repo,
    file_corpora_repo,
    resource_grants_repo,
    source_connections_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

ACL_SYNC_SENTINEL = "system:sharepoint-acl-sync"
#: What this module is, to `resource_grants.source` (src/grant_sources.py).
#: Distinct from the sentinel: the sentinel identifies rows to THIS module,
#: the source tells /admin/access that a revoke here cannot hold.
ACL_SYNC_GRANT_SOURCE = "sharepoint_acl_sync"
"""Tag written to ``user_groups.created_by`` and ``resource_grants.assigned_by``
for every group/grant this sync creates or reconciles."""

ACL_SYNC_SOURCE = "sharepoint_sync"
"""Tag written to ``user_group_members.source`` for every membership row
this sync writes — the scope ``replace_group_members_for_source`` DELETEs
within (never another source's rows for the same group)."""

ACL_SYNC_SERVER_WRITTEN_CONFIG_KEYS = (
    "acl_sync_last_run",
    "acl_sync_last_success_at",
    # 2026-08-30 plan, Task 7 — written by :func:`_sweep_connection`.
    "acl_sweep_last_run",
    "acl_sweep_last_full",
    # 2026-08-31 plan, Task 3 — permission zone rows, written by the SAME
    # function (:func:`_sweep_connection`) via :func:`_reconcile_zones`.
    "acl_zones",
)
"""Keys THIS module (a worker job, not an ``app/api/admin_sharepoint.py``
endpoint) writes into a SharePoint connection's ``config`` — see
:func:`_sync_connection` and :func:`_sweep_connection`.
``app/api/admin_source_connections.py::update_connection`` carries these
forward by hand (imported from here) the same way it carries forward
``SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS`` (``app/api/admin_sharepoint.py``)
— but deliberately NOT added to that OTHER tuple:
``tests/test_sharepoint_config_carry_forward_ratchet.py`` statically scans
``admin_sharepoint.py``'s own writers only (its own module docstring states
the one-file scope), so a key written from this module would show up there
as "declared but no writer found" and fail the ratchet in the wrong
direction. Carried forward without a mechanical ratchet across this module
boundary — reviewed by hand instead.

Note the sweep does NOT add anything to ``SHAREPOINT_SERVER_WRITTEN_CONFIG_
KEYS`` even though it rewrites the ``scopes`` key too (each scope row's own
``excluded_subtrees``) — that key is already declared there (written by
``admin_sharepoint.py``'s own ``confirm_scope``/``remove_scope``), so the
carry-forward already covers it; only the genuinely NEW top-level keys
above need adding."""


def direct_group_name(source_scope_id: str) -> str:
    """Canonical ``user_groups.name`` for the synthetic group that collects
    a scope's direct (non-group) user role assignments — one group per
    scope, not one per user, so grant counts stay O(principal-classes)."""
    return f"sp-direct:{source_scope_id}"


def scope_rel_root(display_path: str) -> str:
    """Drive-relative prefix of a scope root, derived from the wizard
    breadcrumb: segments are
    slash-split and stripped; segment[0] is the site, segment[1] the
    document library (both absent from drive-relative paths). ``"Site"`` or
    ``"Site/Documents"`` -> ``""``; ``"Site/Documents/Team/Sub"`` ->
    ``"Team/Sub"``."""
    segments = [s.strip() for s in (display_path or "").split("/") if s.strip()]
    return "/".join(segments[2:])


@dataclass
class Classified:
    """Result of classifying one scope root's Graph ``permission`` list."""

    entra_group_oids: List[str] = field(default_factory=list)
    direct_user_emails: List[str] = field(default_factory=list)
    #: Agnes ``user_groups.id`` values honored via a ``site_group_map``
    #: match (see :func:`classify_permissions`) — these are ALREADY Agnes
    #: group ids (an admin's own mapping), never names to ``ensure()``.
    site_group_ids: List[str] = field(default_factory=list)
    unhonored: List[Dict[str, str]] = field(default_factory=list)


def _permission_email(user: Dict[str, Any]) -> Optional[str]:
    """Email preference order per §8.2: ``email``, then ``mail``, then
    ``userPrincipalName`` (usually the primary SMTP address — the
    case-insensitive join downstream absorbs any case drift between it and
    the Agnes account's login email)."""
    return user.get("email") or user.get("mail") or user.get("userPrincipalName") or None


def _site_user_email(site_user: Dict[str, Any]) -> Optional[str]:
    """Best-effort email for a ``grantedToV2.siteUser`` grantee.

    ``email``/``mail`` (when Graph includes them) win outright. Otherwise
    fall back to the claims-based ``loginName`` SharePoint always sets for a
    membership-provider user — ``i:0#.f|membership|user@example.com`` — and
    take the segment after the last ``|``. A Windows-claims login name
    (``i:0#.w|domain\\user``) has no ``@`` in that segment and is refused
    rather than guessed at (fail-closed, same posture as an email-less
    ``user`` grantee)."""
    email = site_user.get("email") or site_user.get("mail")
    if email:
        return str(email)
    login_name = str(site_user.get("loginName") or "")
    candidate = login_name.rsplit("|", 1)[-1].strip()
    return candidate if "@" in candidate else None


def _dedupe(values: List[str]) -> List[str]:
    """Stable dedupe — first occurrence wins, order preserved."""
    seen: set = set()
    out: List[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def classify_permissions(
    perms: List[Dict[str, Any]],
    *,
    site_group_map: Optional[Dict[str, List[str]]] = None,
) -> Classified:
    """Classify one scope root's Graph ``permission`` objects per the §8.2
    table.

    Honored: a direct user role assignment with a resolvable email, a
    ``siteUser`` grantee whose claims ``loginName`` (or ``email``/``mail``)
    yields an email, an Entra security/M365 group assignment, and — only
    when ``site_group_map`` names it — a SharePoint site group (Owners/
    Members/Visitors or a custom one) whose ``displayName`` is an EXACT key
    in the map; the mapped value is one or more Agnes ``user_groups.id``
    values granted directly (never synthesized/``ensure``d — an admin
    picked those groups). Out — counted, never granted (fail closed): an
    UNMAPPED site group, sharing links ("specific people" and "people in
    your organization"), anonymous links, external/guest users
    (``userPrincipalName`` containing ``#EXT#``), application principals,
    and a user/siteUser grantee with no resolvable email.
    """
    entra_group_oids: List[str] = []
    direct_user_emails: List[str] = []
    site_group_ids: List[str] = []
    unhonored: List[Dict[str, str]] = []
    site_group_map = site_group_map or {}

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
            display_name = str(site_group.get("displayName") or "")
            mapped = site_group_map.get(display_name)
            if mapped:
                site_group_ids.extend(mapped)
                continue
            detail = display_name or site_group.get("id") or "site group"
            unhonored.append({"kind": "site_group", "detail": str(detail)})
            continue

        if "siteUser" in granted:
            site_user = granted.get("siteUser") or {}
            email = _site_user_email(site_user)
            if not email:
                detail = site_user.get("displayName") or site_user.get("id") or "site user"
                unhonored.append({"kind": "site_user_no_email", "detail": str(detail)})
                continue
            direct_user_emails.append(email)
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
        site_group_ids=_dedupe(site_group_ids),
        unhonored=unhonored,
    )


# ---------------------------------------------------------------------------
# Permissions snapshot (TCRD-296 gap #79) — "who can see this folder in
# SharePoint" captured as METADATA, independent of ``access_mode``. This is a
# deliberately separate concern from ``classify_permissions`` above:
# ``classify_permissions`` decides which grantees Agnes MIRRORS into a real
# grant (honored vs. unhonored), and only ever runs for ``access_mode=
# 'mirrored'`` scopes; :func:`snapshot_principals` below shows EVERY grantee
# SharePoint itself reports — a site group Agnes would never mirror, an
# anonymous link, an external guest — for EVERY scope regardless of mode,
# because the point is "what does SharePoint say", not "what does Agnes do
# about it". A manual scope gets a snapshot and nothing else: no group, no
# membership, no grant (see :func:`_snapshot_scope` and the module docstring).
#
# Storage reuses ``sharepoint_connection_state`` (migration
# ``0108_sp_acl_snapshot_kind``, widening the same CHECK constraint
# ``0103_crawl_shards`` already widened once) — one row per scope, keyed
# ``acl_snapshot:<source_scope_id>`` — rather than a new table, and rather
# than growing ``config["acl_sync_last_run"]`` (that block is a per-RUN
# summary that gets fully overwritten every sync; a permissions snapshot is
# per-SCOPE state that should survive a run in which that particular scope
# was skipped or errored). PG-only, same posture as the ``crawl:<state_key>``
# shard rows it sits beside: see :func:`_store_acl_snapshot`.
# ---------------------------------------------------------------------------

ACL_SNAPSHOT_KIND_PREFIX = "acl_snapshot:"


def acl_snapshot_kind(source_scope_id: str) -> str:
    """``sharepoint_connection_state.kind`` for one scope's stored ACL
    snapshot row."""
    return f"{ACL_SNAPSHOT_KIND_PREFIX}{source_scope_id}"


def _principal_row(kind: str, principal_id: str, display_name: str, roles: List[str], via: str) -> Dict[str, Any]:
    return {
        "principal_kind": kind,
        "principal_id": principal_id,
        "display_name": display_name,
        "roles": roles,
        "via": via,
    }


def _identity_name_id_kind(granted: Dict[str, Any]) -> "tuple[str, str, str]":
    """Best-effort ``(display_name, principal_id, principal_kind)`` for one
    Graph ``grantedToV2``-shaped (or ``grantedToIdentitiesV2`` element)
    identity dict — the informational-snapshot sibling of
    :func:`classify_permissions`'s honored/unhonored grantee dispatch, minus
    the honoring decision itself. Reuses :func:`_permission_email` /
    :func:`_site_user_email` so the two functions never disagree about what
    a grantee's email is."""
    if "group" in granted:
        group = granted.get("group") or {}
        return (str(group.get("displayName") or group.get("id") or "group"), str(group.get("id") or ""), "entra_group")
    if "siteGroup" in granted:
        site_group = granted.get("siteGroup") or {}
        name = site_group.get("displayName") or site_group.get("id") or "site group"
        pid = site_group.get("id") or site_group.get("displayName") or ""
        return (str(name), str(pid), "site_group")
    if "siteUser" in granted:
        site_user = granted.get("siteUser") or {}
        email = _site_user_email(site_user)
        name = site_user.get("displayName") or email or site_user.get("id") or "site user"
        return (str(name), str(email or site_user.get("id") or ""), "site_user")
    if "application" in granted:
        application = granted.get("application") or {}
        name = application.get("displayName") or application.get("id") or "application"
        return (str(name), str(application.get("id") or ""), "application")
    if "user" in granted:
        user = granted.get("user") or {}
        email = _permission_email(user)
        name = user.get("displayName") or email or user.get("id") or "user"
        return (str(name), str(email or user.get("id") or ""), "user")
    return ("Unknown principal", str(granted.get("id") or ""), "unknown")


def snapshot_principals(perms: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ACL-snapshot's per-principal rows for one scope root's raw Graph
    ``permission`` objects — informational only, independent of
    :func:`classify_permissions`'s honored/unhonored split. Never calls
    Graph, never writes anything — a pure transform, same posture as
    ``classify_permissions`` itself.

    Each row: ``{principal_kind, principal_id, display_name, roles, via}``.
    ``via`` is ``"link"`` (this permission IS a sharing link), ``"inherited"``
    (Graph's own ``inheritedFrom`` marks it as coming from an ancestor of the
    scope root), or ``"direct"`` (set on this item itself). ``principal_kind``
    is one of ``entra_group``, ``site_group``, ``site_user``, ``user``,
    ``application``, ``link_anonymous``, ``link_organization``,
    ``link_people``, ``unknown``.
    """
    rows: List[Dict[str, Any]] = []
    for perm in perms:
        roles = [str(r) for r in (perm.get("roles") or [])]
        via = "link" if perm.get("link") else ("inherited" if perm.get("inheritedFrom") else "direct")
        link = perm.get("link")

        if link:
            scope = link.get("scope") or "unknown"
            if scope == "anonymous":
                rows.append(
                    _principal_row("link_anonymous", str(perm.get("id") or ""), "Anyone with the link", roles, via)
                )
                continue
            if scope == "organization":
                rows.append(
                    _principal_row(
                        "link_organization", str(perm.get("id") or ""), "People in the organization", roles, via
                    )
                )
                continue
            identities = perm.get("grantedToIdentitiesV2") or []
            if not identities:
                rows.append(
                    _principal_row("link_people", str(perm.get("id") or ""), "Specific people (link)", roles, via)
                )
                continue
            for identity in identities:
                name, pid, kind = _identity_name_id_kind(identity)
                rows.append(_principal_row(kind, pid, name, roles, "link"))
            continue

        granted = perm.get("grantedToV2") or perm.get("grantedTo") or {}
        name, pid, kind = _identity_name_id_kind(granted)
        rows.append(_principal_row(kind, pid, name, roles, via))
    return rows


def summarize_snapshot_principals(principals: List[Dict[str, Any]]) -> Dict[str, int]:
    """Per-scope count by ``principal_kind`` — the ``summary`` stored
    alongside a scope's ``principals`` list."""
    counts: Dict[str, int] = {}
    for p in principals:
        kind = str(p.get("principal_kind") or "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def aggregate_acl_snapshot(snapshots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Connection-wide rollup over several scopes' already-stored
    :func:`snapshot_principals` payloads — the counts the source card and
    the CLI's aggregate view show. Purely a fold over already-captured
    payloads; never reads Graph itself.

    Returns ``{entra_groups, site_groups, folders_with_org_links,
    folders_with_individual_users, scopes_captured, captured_at}`` —
    ``entra_groups``/``site_groups`` are DISTINCT principal counts across
    every scope; ``folders_with_org_links``/``folders_with_individual_users``
    count SCOPES (one connection could have the same Entra group on ten
    scopes — that's still one "distinct Entra group" but ten "folders with
    individual users" if each also names a person directly).
    ``captured_at`` is the latest of every scope's own ``captured_at``, or
    ``None`` when ``snapshots`` is empty.
    """
    entra_group_ids: set = set()
    site_group_ids: set = set()
    folders_with_org_links = 0
    folders_with_individual_users = 0
    captured_ats: List[str] = []

    for snap in snapshots:
        has_org_link = False
        has_individual_user = False
        for p in snap.get("principals") or []:
            kind = p.get("principal_kind")
            pid = p.get("principal_id")
            if kind == "entra_group" and pid:
                entra_group_ids.add(pid)
            elif kind == "site_group" and pid:
                site_group_ids.add(pid)
            elif kind == "link_organization":
                has_org_link = True
            elif kind in ("user", "site_user"):
                has_individual_user = True
        if has_org_link:
            folders_with_org_links += 1
        if has_individual_user:
            folders_with_individual_users += 1
        captured_at = snap.get("captured_at")
        if captured_at:
            captured_ats.append(str(captured_at))

    return {
        "entra_groups": len(entra_group_ids),
        "site_groups": len(site_group_ids),
        "folders_with_org_links": folders_with_org_links,
        "folders_with_individual_users": folders_with_individual_users,
        "scopes_captured": len(snapshots),
        "captured_at": max(captured_ats) if captured_ats else None,
    }


def _store_acl_snapshot(
    connection_id: str,
    source_scope_id: Optional[str],
    display_path: Optional[str],
    perms: List[Dict[str, Any]],
) -> None:
    """Persist one scope's ACL snapshot — Postgres only (same posture as the
    auto-parallel-crawl shard rows this reuses the table for): a DuckDB-
    backed instance silently captures no snapshot rather than raising,
    because this is a background job body, never an HTTP route (see
    ``connectors/sharepoint/state_store.py``'s module docstring for the same
    fail-clean posture applied to crawl/facts state). A write failure is
    logged and swallowed — a snapshot write must never fail the sync it
    rides along with.
    """
    if not source_scope_id:
        return
    from src.repositories import use_pg

    if not use_pg():
        return

    from connectors.sharepoint import state_store

    principals = snapshot_principals(perms)
    payload = {
        "source_scope_id": source_scope_id,
        "display_path": display_path,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "principals": principals,
        "summary": summarize_snapshot_principals(principals),
    }
    try:
        state_store.put(acl_snapshot_kind(source_scope_id), connection_id, payload)
    except Exception:  # noqa: BLE001 — never let a snapshot write fail the sync itself
        logger.warning(
            "sharepoint-acl-sync: failed to persist ACL snapshot for scope %s (connection %s)",
            source_scope_id,
            connection_id,
            exc_info=True,
        )


async def _snapshot_scope(connection_id: str, scope: Dict[str, Any], token: str) -> Dict[str, Any]:
    """Capture-only path for a scope :func:`_sync_scope` never touches (any
    ``access_mode`` other than ``'mirrored'`` — typically ``'manual'``):
    read + snapshot + store, no honored/unhonored decision, no group/
    membership sync, no grant reconciliation (module docstring's
    "Permissions snapshot" note). Costs exactly one Graph permissions read
    per scope — never called for a scope :func:`_sync_scope` already reads
    (see :func:`_snapshot_only_scopes`), so a connection with N scopes never
    costs more than N Graph permission reads total for this feature.

    Returns ``{"source_scope_id", "error"}`` — ``error`` is ``None`` on
    success, or ``"missing_drive_id"``/the Graph error string.
    """
    source_scope_id = scope.get("source_scope_id")
    drive_id = scope.get("drive_id")
    display_path = scope.get("display_path")
    if not drive_id or not source_scope_id:
        return {"source_scope_id": source_scope_id, "error": "missing_drive_id"}
    try:
        perms = await graph_client.list_item_permissions(token, drive_id, source_scope_id)
    except SharePointGraphError as exc:
        return {"source_scope_id": source_scope_id, "error": str(exc)}
    _store_acl_snapshot(connection_id, source_scope_id, display_path, perms)
    return {"source_scope_id": source_scope_id, "error": None}


def _snapshot_only_scopes(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's scope rows :func:`_sync_scope` never reads — every
    scope whose ``access_mode`` is not ``'mirrored'`` (typically the default,
    ``'manual'``) — the complement of :func:`_mirrored_scopes`. These get an
    ACL snapshot only (see :func:`_snapshot_scope`)."""
    scopes = (connection.get("config") or {}).get("scopes")
    if not isinstance(scopes, list):
        return []
    return [s for s in scopes if isinstance(s, dict) and s.get("access_mode") != "mirrored"]


def _has_any_scopes(connection: Dict[str, Any]) -> bool:
    """This connection has at least one scope row, mirrored or not — the
    nightly sweep's inclusion filter (:func:`_run_acl_sync_async`). Broader
    than :func:`_mirrored_scopes` alone: a manual-only connection still needs
    its scopes' ACL snapshots captured (independent of ``access_mode`` — see
    the module docstring's "Permissions snapshot" note) even though it has
    nothing to mirror."""
    scopes = (connection.get("config") or {}).get("scopes")
    return isinstance(scopes, list) and bool(scopes)


# ---------------------------------------------------------------------------
# Sync job body (2026-08-30 plan, Task 4). See the module docstring for the
# division of labor with classify_permissions() above.
# ---------------------------------------------------------------------------

#: must_not-mode default grace window (hours) before a connection's mirrored
#: grants are suspended after a failed sync — overridable via the
#: ``acl_max_stale_hours`` switch (``acl_sync.max_stale_hours``).
_DEFAULT_MAX_STALE_HOURS = 72


def _mirrored_scopes(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's ``access_mode == 'mirrored'`` scope rows.

    ``access_mode`` is written by ``app/api/admin_sharepoint.py::
    confirm_scope`` (2026-08-30 plan, Task 5's ``ConfirmScopeBody
    .access_mode``, defaulting to ``'manual'``) — a real instance returns
    ``[]`` here until an admin opts a scope into mirroring through that
    endpoint.
    """
    scopes = (connection.get("config") or {}).get("scopes")
    if not isinstance(scopes, list):
        return []
    return [s for s in scopes if isinstance(s, dict) and s.get("access_mode") == "mirrored"]


def zone_rows(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's ``config["acl_zones"]`` rows (2026-08-31 plan,
    Task 3) — tolerant of the key being entirely absent (a connection swept
    before this plan, or ``acl_zones`` never enabled). Each row:
    ``{zone_item_id, parent_scope_id, drive_id, name, display_path,
    rel_path, collection_id, detected_at, status}`` with
    ``status in {"active", "dissolved"}``. Never mutate a row returned from
    here in place — callers that need to change one must copy first (see
    :func:`_reconcile_zones`)."""
    zones = (connection.get("config") or {}).get("acl_zones")
    if not isinstance(zones, list):
        return []
    return [z for z in zones if isinstance(z, dict)]


def active_zone_rows(connection: Dict[str, Any]) -> List[Dict[str, Any]]:
    """This connection's ACTIVE permission zones — see :func:`zone_rows`.
    Consumed by :func:`_sync_connection` (Task 4, mirrors each zone's own
    ACL), the ingest gate (Task 5) and the producer handoff (Task 7)."""
    return [z for z in zone_rows(connection) if z.get("status") == "active"]


def run_acl_sync(payload: dict) -> dict:
    """Entry point for the ``sharepoint-acl-sync`` worker job kind (spec §5).

    ``payload = {"connection_id": str | None}`` — a specific connection (the
    admin "sync now" action, or a targeted retry) or ``None`` to sweep every
    ``source_type='sharepoint'`` connection that has at least one
    ``access_mode='mirrored'`` scope (the nightly cadence).

    Per connection: resolves the certificate → app-only Graph token
    (typed failures mark that connection's run failed and audit
    ``sharepoint_acl.sync_failed``, then the sweep continues with the next
    connection — one connection's outage never blocks another's); for each
    mirrored scope, reads the scope root's permissions, classifies them
    (:func:`classify_permissions`), expands honored Entra groups via
    ``transitiveMembers``, resolves principals to Agnes accounts
    case-insensitively (``users_repo().get_by_email_ci`` — never
    ``get_by_email``; an unmatched principal grants nobody and is counted,
    fail-closed), and reconciles this sync's own groups/memberships/grants
    against the computed target state. Grant ADDS are written before
    REMOVES so a scope never passes through a granted-to-nobody window it
    did not already have (spec §5.1 step 4).

    Every ACTIVE permission zone (2026-08-31 plan, Task 3/4) is synced right
    after its parent connection's real scopes, as its own pseudo-scope keyed
    on the zone's ``zone_item_id``/``collection_id``/``drive_id`` — see the
    zone loop inside :func:`_sync_connection`. A zone's ACL is re-read on
    every run exactly like a scope's, so a zone-level revocation lands
    within the same window as a scope-level one.

    A group whose ``transitiveMembers`` read fails keeps its PREVIOUS
    membership (fail-soft on identity resolution, not on reachability — the
    Google-sync precedent) and marks that scope stale in the run report; a
    scope root permissions read that fails aborts that scope's
    reconciliation entirely (fail-closed — no partial diff is ever applied).

    Records a last-run block into the connection's own
    ``config["acl_sync_last_run"]`` (matched/unmatched counts, unhonored
    permission types, per-collection grant deltas, stale scopes, the zone
    count, the error if any) — the source card's data, no new table. ``config
    ["acl_sync_last_success_at"]`` tracks the last time a connection's run
    completed WITHOUT error, independent of the (possibly-failing) last run,
    so ``acl_sync.guarantee_mode == "must_not"`` can measure staleness
    correctly across a run of consecutive failures.

    Staleness (Q7 fork, ``acl_guarantee_mode`` switch): under ``must_not``, a
    FAILED run past ``acl_sync.max_stale_hours`` (default 72) since the last
    success suspends every sentinel-owned grant on that connection's
    mirrored collections AND active zone collections (deleted, not merely
    flagged — the next successful sync rewrites them;
    ``accessible_collection_ids`` itself is never touched, spec §5.4) and
    audits ``sharepoint_acl.grants_suspended``. Under ``should_not``,
    staleness is never enforced — the connection's mirrored grants persist
    with the staleness visible on the source card.

    Returns ``{"connections": N, "scopes": M, "matched": X, "unmatched": Y,
    "errors": [...]}`` (aggregated across every connection processed), or
    ``{"skipped": "sharepoint disabled"}`` when the feature flag is off —
    the scheduler enqueues this kind unconditionally, so the no-op has to be
    cheap and harmless, same posture as ``ducklake-maintenance``
    (``app/worker/kinds.py``).
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        return {"skipped": "sharepoint disabled"}

    return asyncio.run(_run_acl_sync_async(payload))


async def _run_acl_sync_async(payload: dict) -> dict:
    connections_repo = source_connections_repo()
    connection_id = payload.get("connection_id")

    errors: List[Dict[str, Any]] = []
    if connection_id:
        row = connections_repo.get(connection_id)
        if row is not None and row.get("source_type") == "sharepoint":
            connections = [row]
        else:
            connections = []
            errors.append({"connection_id": connection_id, "error": "connection_not_found"})
    else:
        # `_has_any_scopes`, not `_mirrored_scopes` alone (TCRD-296 gap #79):
        # a manual-only connection has nothing to MIRROR but still needs its
        # scopes' ACL SNAPSHOTs captured every run — see the "Permissions
        # snapshot" section above and `_sync_connection`'s own snapshot loop.
        connections = [c for c in connections_repo.list(source_type="sharepoint") if _has_any_scopes(c)]

    totals: Dict[str, Any] = {"connections": 0, "scopes": 0, "matched": 0, "unmatched": 0, "errors": errors}
    for connection in connections:
        result = await _sync_connection(connection)
        totals["connections"] += 1
        totals["scopes"] += result["scopes"]
        totals["matched"] += result["matched"]
        totals["unmatched"] += result["unmatched"]
        if result.get("error"):
            totals["errors"].append({"connection_id": connection["id"], "error": result["error"]})
    return totals


async def _sync_connection(connection: Dict[str, Any]) -> Dict[str, Any]:
    """Sync one connection's mirrored scopes (and, 2026-08-31 plan Task 4,
    its active permission zones); persists the last-run block, audits the
    run-level actions, and applies must_not staleness suspension. See
    :func:`run_acl_sync` for the full contract.

    Invariant: this and ``_sweep_connection`` never write a whole-``config``
    snapshot — both patch only their own bookkeeping keys via
    ``source_connections_repo().config_patch`` (re-reads ``config`` fresh
    inside its own transaction), so a nightly sync and the daily sweep
    landing together on one connection can never drop each other's
    just-written key (never grants — those live in ``resource_grants``).

    **Shared-collection reconcile (2026-09 fix).** More than one mirrored
    scope (a bulk-add ``collection_id`` target) or a zone re-homed onto a
    scope's own collection can route to the SAME ``collection_id``.
    ``_sync_scope`` no longer reconciles that collection's grants itself —
    it only reports its OWN honored ``target_group_ids``; this function
    accumulates every scope's and zone's contribution per ``collection_id``
    (``honored_by_collection``) and calls :func:`_reconcile_grants` exactly
    ONCE per collection, against the UNION of everyone who routed to it
    this run. The single-scope-per-collection case (still the overwhelming
    majority) degrades to exactly the old behavior — a union of one set is
    that set. A collection with ANY scope/zone that errored this run
    (``uncertain_collections``) is skipped entirely, same fail-closed
    posture ``_sync_scope`` always had for its OWN collection: no partial
    diff is ever applied, and a collection this run could not fully read is
    left with whatever grants the last successful run computed."""
    connection_id = connection["id"]
    scopes = _mirrored_scopes(connection)
    zones = active_zone_rows(connection)
    site_group_map = (connection.get("config") or {}).get("acl_site_group_map") or {}
    t0 = time.monotonic()

    error: Optional[str] = None
    token: Optional[str] = None
    try:
        settings = resolve_sharepoint_settings(connection)
        token = await graph_client.get_app_token(
            settings.tenant_id, settings.client_id, settings.private_key, client_secret=settings.client_secret
        )
    except (SharePointSettingsError, SharePointGraphError) as exc:
        error = str(exc)

    matched_total = 0
    unmatched_total = 0
    unhonored_all: List[Dict[str, Any]] = []
    stale_scopes: List[str] = []
    honored_by_collection: Dict[str, set] = {}
    first_scope_by_collection: Dict[str, str] = {}
    uncertain_collections: set = set()

    def _accumulate(report: Dict[str, Any]) -> None:
        collection_id = report.get("collection_id")
        if not collection_id:
            return
        if report.get("error"):
            uncertain_collections.add(collection_id)
            return
        honored_by_collection.setdefault(collection_id, set()).update(report.get("target_group_ids") or [])
        first_scope_by_collection.setdefault(collection_id, report.get("source_scope_id"))

    if token is not None:
        for scope in scopes:
            scope_report = await _sync_scope(connection_id, scope, token, site_group_map=site_group_map)
            matched_total += scope_report["matched"]
            unmatched_total += scope_report["unmatched"]
            unhonored_all.extend(
                {**item, "scope": scope_report["source_scope_id"]} for item in scope_report["unhonored"]
            )
            if scope_report.get("stale"):
                stale_scopes.append(scope_report["source_scope_id"])
            _accumulate(scope_report)
            if scope_report.get("error") and error is None:
                error = scope_report["error"]

        # 2026-08-31 plan, Task 4: every ACTIVE permission zone (Task 3) is
        # synced as its own pseudo-scope, keyed on the zone's OWN item id
        # and collection. `_sync_scope` needs no change to support this —
        # `direct_group_name(zone_item_id)` already yields a unique
        # `sp-direct:<zone_item_id>` group, and its contribution accumulates
        # into the same per-collection union as every real scope's.
        for zone in zones:
            pseudo_scope = {
                "source_scope_id": zone.get("zone_item_id"),
                "collection_id": zone.get("collection_id"),
                "drive_id": zone.get("drive_id"),
            }
            zone_report = await _sync_scope(connection_id, pseudo_scope, token, site_group_map=site_group_map)
            matched_total += zone_report["matched"]
            unmatched_total += zone_report["unmatched"]
            unhonored_all.extend(
                {**item, "scope": zone_report["source_scope_id"], "zone_rel_path": zone.get("rel_path")}
                for item in zone_report["unhonored"]
            )
            if zone_report.get("stale"):
                stale_scopes.append(zone_report["source_scope_id"])
            _accumulate(zone_report)
            if zone_report.get("error") and error is None:
                error = zone_report["error"]

        # TCRD-296 gap #79: every scope `_sync_scope` above never reads
        # (any access_mode other than 'mirrored') still gets its
        # informational ACL snapshot captured — but ONLY when there is
        # somewhere to put it (`use_pg()`), so a DuckDB-backed instance never
        # spends a Graph call on a feature it cannot persist. Snapshot
        # failures are swallowed here (never fed into `error`/`ok`): they
        # must never trip must_not staleness suspension for the connection's
        # actual grant-mirroring health, which is what `error` governs below.
        from src.repositories import use_pg

        if use_pg():
            for scope in _snapshot_only_scopes(connection):
                await _snapshot_scope(connection_id, scope, token)

    grant_deltas: Dict[str, Dict[str, List[str]]] = {}
    for collection_id, group_ids in honored_by_collection.items():
        if collection_id in uncertain_collections:
            continue
        delta = _reconcile_grants(collection_id, sorted(group_ids), first_scope_by_collection.get(collection_id))
        if delta:
            grant_deltas[collection_id] = delta

    ok = error is None
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    duration_ms = int((time.monotonic() - t0) * 1000)

    config_patch: Dict[str, Any] = {
        "acl_sync_last_run": {
            "at": now_iso,
            "ok": ok,
            "matched": matched_total,
            "unmatched": unmatched_total,
            "unhonored": unhonored_all,
            "grant_deltas": grant_deltas,
            "stale_scopes": stale_scopes,
            "zones": len(zones),
            "error": error,
            "duration_ms": duration_ms,
        }
    }
    if ok:
        config_patch["acl_sync_last_success_at"] = now_iso
    source_connections_repo().config_patch(connection_id, config_patch)

    if ok:
        log_safe(
            action="sharepoint_acl.sync_completed",
            resource=f"source_connection:{connection_id}",
            params={"scopes": len(scopes), "matched": matched_total, "unmatched": unmatched_total},
            result="success",
            duration_ms=duration_ms,
            client_kind="scheduler",
        )
    else:
        log_safe(
            action="sharepoint_acl.sync_failed",
            resource=f"source_connection:{connection_id}",
            params={"error": error},
            result="error",
            duration_ms=duration_ms,
            client_kind="scheduler",
        )

    _handle_staleness(connection, connection_id, ok, now)

    if unmatched_total:
        log_safe(
            action="sharepoint_acl.principal_unmatched",
            resource=f"source_connection:{connection_id}",
            params={"count": unmatched_total},
            result="success",
            client_kind="scheduler",
        )

    return {"scopes": len(scopes), "matched": matched_total, "unmatched": unmatched_total, "error": error}


async def _sync_scope(
    connection_id: str,
    scope: Dict[str, Any],
    token: str,
    *,
    site_group_map: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Sync one mirrored scope: read → classify → resolve group membership.

    Does NOT reconcile this scope's collection's grants — it only reports
    its own honored ``target_group_ids``; :func:`_sync_connection`
    accumulates every scope's (and zone's) contribution per
    ``collection_id`` and reconciles ONCE per collection after everything
    this run has been read (see that function's own docstring, "Shared-
    collection reconcile").

    Returns a report dict with ``source_scope_id``, ``collection_id``,
    ``matched``, ``unmatched``, ``unhonored``, ``stale``, ``error`` and
    ``target_group_ids`` (this scope's own honored Agnes ``user_groups.id``
    values — empty on any error).
    """
    source_scope_id = scope.get("source_scope_id")
    collection_id = scope.get("collection_id")
    drive_id = scope.get("drive_id")

    base: Dict[str, Any] = {
        "source_scope_id": source_scope_id,
        "collection_id": collection_id,
        "matched": 0,
        "unmatched": 0,
        "unhonored": [],
        "stale": False,
        "error": None,
        "target_group_ids": [],
    }

    if not drive_id:
        # See the module docstring's "Known gap" note — no scope row carries
        # a drive_id today. Skip rather than crash the connection's run.
        logger.warning(
            "sharepoint-acl-sync: scope %s (connection %s) has no drive_id on its scope row — "
            "skipping until the wizard persists one",
            source_scope_id,
            connection_id,
        )
        return {**base, "error": "missing_drive_id", "stale": True}

    try:
        perms = await graph_client.list_item_permissions(token, drive_id, source_scope_id)
    except SharePointGraphError as exc:
        return {**base, "error": str(exc), "stale": True}

    # TCRD-296 gap #79: the SAME read above also feeds this scope's
    # informational ACL snapshot — no second Graph call. Independent of
    # `classify_permissions` below (that decision never affects what the
    # snapshot shows) and never allowed to affect this scope's grant
    # mirroring — see `_store_acl_snapshot`'s own fail-swallowed posture.
    _store_acl_snapshot(connection_id, source_scope_id, scope.get("display_path"), perms)

    classified = classify_permissions(perms, site_group_map=site_group_map)

    groups_repo = user_groups_repo()
    members_repo = user_group_members_repo()
    users = users_repo()

    target_group_ids: List[str] = []
    matched = 0
    unmatched = 0
    stale = False

    for oid in classified.entra_group_oids:
        group = groups_repo.ensure(
            name=entra_group_name(oid),
            description=f"Mirrored from Entra group {oid} by SharePoint ACL sync",
            created_by=ACL_SYNC_SENTINEL,
        )
        group_id = group["id"]
        target_group_ids.append(group_id)

        try:
            members = await graph_client.list_group_transitive_members(token, oid)
        except SharePointGraphError:
            # Fail-soft on identity resolution (Google-sync precedent): keep
            # this group's previous membership rather than emptying it on a
            # transient Graph error. The group stays HONORED (its grant is
            # still computed below) — only its membership refresh is skipped.
            stale = True
            continue

        matched_ids: List[str] = []
        for member in members:
            email = member.get("mail") or member.get("userPrincipalName")
            user = users.get_by_email_ci(email) if email else None
            if user is None:
                unmatched += 1
                continue
            matched_ids.append(user["id"])
            matched += 1

        _replace_membership_and_audit(members_repo, group_id, matched_ids, source_scope_id)

    if classified.direct_user_emails:
        group = groups_repo.ensure(
            name=direct_group_name(source_scope_id),
            description=f"Direct user role assignments on scope {source_scope_id}",
            created_by=ACL_SYNC_SENTINEL,
        )
        group_id = group["id"]
        target_group_ids.append(group_id)

        matched_ids = []
        for email in classified.direct_user_emails:
            user = users.get_by_email_ci(email)
            if user is None:
                unmatched += 1
                continue
            matched_ids.append(user["id"])
            matched += 1

        _replace_membership_and_audit(members_repo, group_id, matched_ids, source_scope_id)

    # Site-group-mapped grants (2026-09 fix): already Agnes group ids an
    # admin picked, never synthesized — no `ensure()`/membership sync, just
    # honored directly. Contributes no matched/unmatched principal count
    # (there is no per-user membership to resolve here).
    target_group_ids.extend(classified.site_group_ids)

    return {
        **base,
        "matched": matched,
        "unmatched": unmatched,
        "unhonored": classified.unhonored,
        "stale": stale,
        "target_group_ids": target_group_ids,
    }


def _replace_membership_and_audit(
    members_repo: Any, group_id: str, matched_ids: List[str], source_scope_id: Optional[str]
) -> None:
    """``replace_group_members_for_source`` plus a ``membership_replaced``
    audit row — but ONLY when the computed member set actually differs from
    what was there before, so an idempotent re-run (identical Graph
    membership) writes no audit row at all."""
    before = {m["id"] for m in members_repo.list_members_for_group(group_id) if m.get("source") == ACL_SYNC_SOURCE}
    after = set(matched_ids)
    members_repo.replace_group_members_for_source(
        group_id, matched_ids, source=ACL_SYNC_SOURCE, added_by=ACL_SYNC_SENTINEL
    )
    if before != after:
        log_safe(
            action="sharepoint_acl.membership_replaced",
            resource=f"user_group:{group_id}",
            params={"scope": source_scope_id, "added": len(after - before), "removed": len(before - after)},
            result="success",
            client_kind="scheduler",
        )


def _reconcile_grants(
    collection_id: str, target_group_ids: List[str], source_scope_id: Optional[str]
) -> Optional[dict]:
    """Diff this scope's honored groups against its collection's
    sentinel-owned grants and write the delta — ADDS before REMOVES (spec
    §5.1 step 4) so the collection never passes through a
    granted-to-nobody window it did not already have. Grants owned by any
    OTHER ``assigned_by`` (an admin's manual grant) are never read from or
    written to here — the sentinel-segregation contract.

    ``target_group_ids=[]`` removes every sentinel-owned grant on
    ``collection_id`` — used by :func:`_cleanup_connection_content`
    (2026-08-31 plan, Task 6) as the first step of a dissolved zone's
    teardown, before the harder ``delete_by_resource`` sweep."""
    grants = resource_grants_repo()
    current = [
        g
        for g in grants.list_all(resource_type=ResourceType.COLLECTION.value)
        if g.get("resource_id") == collection_id and (g.get("assigned_by") or "") == ACL_SYNC_SENTINEL
    ]
    current_group_ids = {g["group_id"] for g in current}
    target_set = set(target_group_ids)

    added: List[str] = []
    removed: List[str] = []

    for group_id in target_set - current_group_ids:
        # `source` as well as the sentinel. The sentinel is what THIS module
        # matches on to find its own rows; `source` is what /admin/access
        # reads to decide whether to offer a revoke. Without it these rows
        # had no recorded writer, so the page filed them under "change
        # here" and drew a Revoke that the next sync silently undid.
        grants.ensure_grant(
            group_id,
            ResourceType.COLLECTION.value,
            collection_id,
            assigned_by=ACL_SYNC_SENTINEL,
            source=ACL_SYNC_GRANT_SOURCE,
        )
        added.append(group_id)
        log_safe(
            action="sharepoint_acl.grant_added",
            resource=f"file_corpus:{collection_id}",
            params={"group_id": group_id, "scope": source_scope_id},
            result="success",
            client_kind="scheduler",
        )
    for grant in current:
        if grant["group_id"] not in target_set:
            grants.delete(grant["id"])
            removed.append(grant["group_id"])
            log_safe(
                action="sharepoint_acl.grant_removed",
                resource=f"file_corpus:{collection_id}",
                params={"group_id": grant["group_id"], "scope": source_scope_id},
                result="success",
                client_kind="scheduler",
            )

    if not added and not removed:
        return None
    return {"added": added, "removed": removed}


def _handle_staleness(connection: Dict[str, Any], connection_id: str, ok: bool, now: datetime) -> Optional[int]:
    """must_not-mode staleness suspension (spec §5.4, Q7 fork). Returns the
    number of grants suspended, or ``None`` when suspension does not apply
    (a successful run, ``should_not`` mode, or still within the grace
    window)."""
    if ok:
        return None

    from app.switches import switch_value

    if switch_value("acl_guarantee_mode") != "must_not":
        return None

    max_stale_hours = switch_value("acl_max_stale_hours") or _DEFAULT_MAX_STALE_HOURS
    last_success_raw = (connection.get("config") or {}).get("acl_sync_last_success_at")
    last_success: Optional[datetime] = None
    if last_success_raw:
        try:
            last_success = datetime.fromisoformat(last_success_raw)
        except ValueError:
            last_success = None
    if last_success is not None and last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=timezone.utc)

    hours_stale = float("inf") if last_success is None else (now - last_success).total_seconds() / 3600
    if hours_stale <= max_stale_hours:
        return None

    removed = _suspend_connection_grants(connection)
    if removed:
        log_safe(
            action="sharepoint_acl.grants_suspended",
            resource=f"source_connection:{connection_id}",
            params={"removed": removed, "hours_stale": None if hours_stale == float("inf") else round(hours_stale, 1)},
            result="success",
            client_kind="scheduler",
        )
    return removed


def _suspend_connection_grants(connection: Dict[str, Any]) -> int:
    """Delete every sentinel-owned grant on this connection's mirrored
    scopes' AND active permission zones' collections (2026-08-31 plan,
    Task 4 extended the zone half). ``accessible_collection_ids`` is never
    touched directly — deleting the grant IS the suspension (spec §5.4);
    the next successful sync's :func:`_reconcile_grants` rewrites them."""
    collection_ids = {s.get("collection_id") for s in _mirrored_scopes(connection) if s.get("collection_id")}
    collection_ids |= {z.get("collection_id") for z in active_zone_rows(connection) if z.get("collection_id")}
    if not collection_ids:
        return 0
    grants = resource_grants_repo()
    removed = 0
    for grant in grants.list_all(resource_type=ResourceType.COLLECTION.value):
        if grant.get("resource_id") in collection_ids and (grant.get("assigned_by") or "") == ACL_SYNC_SENTINEL:
            grants.delete(grant["id"])
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# Broken-inheritance subtree sweep (2026-08-30 plan, Task 7; sweep v2 —
# file probes, permission zones, retroactive cleanup — 2026-08-31 plan,
# Tasks 3/6). See the module docstring's "Subtree sweep" / "Sweep v2"
# paragraphs for the wider picture.
# ---------------------------------------------------------------------------

#: must_not/should_not-independent default cadence (days) between two full
#: sweeps of one connection's mirrored scopes — overridable via the
#: ``acl_sweep_interval_days`` switch (``acl_sync.sweep_interval_days``).
#: The scheduler row itself fires DAILY (native cron, see
#: services/scheduler/__main__.py) — this per-connection self-guard is
#: defense-in-depth against a restart-refire (scheduler ``last_run`` state
#: can be lost across a container recreate), the same risk
#: ``app/api/store_lint_admin.py``'s own min-interval self-guard protects
#: against for its own row.
_DEFAULT_SWEEP_INTERVAL_DAYS = 7

#: Safety cap on folders visited in ONE scope's walk — a pathological or
#: misconfigured tenant tree must not pin a worker slot forever. The real
#: reference library (spec §6.2) is ~98k folders TOTAL across the whole
#: crawl; this leaves ample headroom per scope while still bounding the
#: worst case. Hitting it sets ``truncated: true`` in the scope's report
#: rather than looping forever.
_MAX_SWEEP_FOLDERS_VISITED = 200_000


def _sweep_interval_days() -> int:
    from app.switches import switch_value

    value = switch_value("acl_sweep_interval_days")
    try:
        return int(value)
    except (TypeError, ValueError):
        return _DEFAULT_SWEEP_INTERVAL_DAYS


def _sweep_due(connection: Dict[str, Any]) -> bool:
    """Whether this connection's mirrored scopes are due for a full subtree
    sweep — never synced yet, or the last FULL (error-free) sweep is older
    than :func:`_sweep_interval_days`. A connection whose last attempt
    FAILED (``acl_sweep_last_run.ok`` false) but never completed keeps
    ``acl_sweep_last_full`` at its previous value (or absent), so a failed
    run does not push the next attempt a further interval out."""
    last_full_raw = (connection.get("config") or {}).get("acl_sweep_last_full")
    if not last_full_raw:
        return True
    try:
        last_full = datetime.fromisoformat(last_full_raw)
    except ValueError:
        return True
    if last_full.tzinfo is None:
        last_full = last_full.replace(tzinfo=timezone.utc)
    elapsed_days = (datetime.now(timezone.utc) - last_full).total_seconds() / 86400
    return elapsed_days >= _sweep_interval_days()


def _zone_slugify(text: str) -> str:
    """Local mirror of ``app.api.admin_sharepoint._slugify`` — cannot import
    it directly: that module already imports ``ACL_SYNC_SENTINEL`` from
    here, so the reverse import would be circular."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80].strip("-") or "sharepoint-zone"


def _create_zone_collection(*, name: str, zone_item_id: str) -> str:
    """Idempotent-by-caller creation of one permission zone's collection.

    Grant-less on purpose (module docstring's "Semantics locked here", Task
    3): invisible to everyone until Task 4's ACL sync mirrors the zone
    root's own permissions into it — that ordering is what keeps zone
    creation MUST-NOT-safe.

    Collision handling mirrors ``admin_sharepoint.py::_create_scope_
    collection`` (one retry with a short, stable sha256-derived suffix) —
    the retry SHAPE is mirrored rather than the function imported (see
    :func:`_zone_slugify`'s note on why that import would be circular).
    """
    repo = file_corpora_repo()
    slug = _zone_slugify(name)
    description = f"SharePoint permission zone · {name}"
    try:
        return repo.create(name=name, slug=slug, description=description, created_by=ACL_SYNC_SENTINEL)
    except Exception as exc:  # noqa: BLE001 — DuckDB ConstraintException / PG IntegrityError, message-sniffed elsewhere too
        err = str(exc).lower()
        if "unique" not in err and "duplicate" not in err and "constraint" not in err:
            raise
        suffix = hashlib.sha256(zone_item_id.encode()).hexdigest()[:8]
        return repo.create(name=name, slug=f"{slug}-{suffix}", description=description, created_by=ACL_SYNC_SENTINEL)


async def _walk_subtree_sweep(
    token: str,
    drive_id: str,
    root_item_id: str,
    root_path: str,
    *,
    rel_root: str = "",
    known_zone_ids: "frozenset[str] | set[str]" = frozenset(),
) -> Dict[str, Any]:
    """Breadth-first walk of one scope's folder tree, probing
    ``hasUniqueRoleAssignments`` per child — folder OR file (2026-08-31
    plan, Task 3 — earlier this only probed folders) — via
    ``graph_client.probe_unique_permissions`` (already ``$batch``-based —
    20 items per call).

    Per child, in order:

    * a FILE with a ``True``/``None`` flag is excluded (``kind="file"``) —
      files are never descended into either way (spec §3, file-grain
      fidelity is fail-closed exclusion, never per-file mirroring);
    * a FOLDER with flag ``None`` (unknown — the probe's own honest answer
      when the signal could not be read) is excluded (``kind="folder"``),
      never descended — fail-closed, same as a confirmed break;
    * a FOLDER with flag ``True``: ALWAYS becomes a **permission zone
      candidate** (``zone_candidates``) and the walk DESCENDS into it —
      nested breaks become further candidates, unlike the exclude-and-stop
      path. There is no separate opt-in switch for this anymore (2026-09-01:
      the four SharePoint feature flags collapsed into the single
      ``sharepoint`` switch) — whenever the connector itself is on, a
      confirmed break is always promoted to a zone, never merely excluded;
    * a FOLDER with flag ``False``: inheritance intact, the walk descends
      normally; if its id is in ``known_zone_ids`` (an ACTIVE zone from a
      PRIOR run rooted here), it is recorded in ``relinked_zone_ids`` — its
      folder re-linked inheritance since the zone was created.

    The scope ROOT ITSELF is never probed or excluded here — its own
    permissions are what :func:`_sync_scope` already mirrors; this sweep
    only ever excludes/zones something FINER than the scope.

    Raises :class:`SharePointGraphError` on a fatal read failure partway
    through the walk — the caller (:func:`_sweep_scope`) treats that as
    "leave this scope's previous exclusion/zone state untouched"
    (fail-closed, no partial diff is ever persisted).

    Returns ``{"excluded_subtrees", "zone_candidates", "relinked_zone_ids",
    "requests", "unknown_probes", "truncated"}``.
    """
    excluded: List[Dict[str, Any]] = []
    zone_candidates: List[Dict[str, Any]] = []
    relinked_zone_ids: List[str] = []
    requests = 0
    unknown_probes = 0
    visited = 0
    truncated = False
    queue: List[tuple] = [(root_item_id, root_path, rel_root)]

    while queue:
        if visited >= _MAX_SWEEP_FOLDERS_VISITED:
            truncated = True
            break
        item_id, path, rel_path_prefix = queue.pop(0)
        visited += 1

        children = await graph_client.list_item_children(token, drive_id, item_id)
        requests += 1
        if not children:
            continue

        ids = [c["id"] for c in children]
        flags = await graph_client.probe_unique_permissions(token, drive_id, ids)
        # One `$batch` POST per up-to-20 items (graph_client._GRAPH_BATCH_SIZE_CAP)
        # — matches probe_unique_permissions's own batching exactly, so the
        # request count here is observed against the SAME chunking, not a
        # separate guess.
        requests += -(-len(ids) // graph_client._GRAPH_BATCH_SIZE_CAP)

        for child in children:
            child_path = f"{path}/{child['name']}" if path else child["name"]
            child_rel_path = f"{rel_path_prefix}/{child['name']}" if rel_path_prefix else child["name"]
            flag = flags.get(child["id"])
            is_folder = bool(child.get("is_folder"))

            if flag is None:
                unknown_probes += 1

            if not is_folder:
                if flag is None or flag is True:
                    excluded.append(
                        {
                            "item_id": child["id"],
                            "path": child_path,
                            "rel_path": child_rel_path,
                            "kind": "file",
                            "detected_at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                continue  # a file is never descended into either way

            if flag is None:
                excluded.append(
                    {
                        "item_id": child["id"],
                        "path": child_path,
                        "rel_path": child_rel_path,
                        "kind": "folder",
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                continue  # never descend into an unknown-signal subtree

            if flag is True:
                # ALWAYS a zone candidate — see this function's own docstring
                # (no separate zones-opt-in switch anymore).
                zone_candidates.append(
                    {
                        "zone_item_id": child["id"],
                        "name": child["name"],
                        "path": child_path,
                        "rel_path": child_rel_path,
                    }
                )
                queue.append((child["id"], child_path, child_rel_path))
                continue

            # flag is False -> inheritance intact, descend normally.
            if child["id"] in known_zone_ids:
                relinked_zone_ids.append(child["id"])
            queue.append((child["id"], child_path, child_rel_path))

    return {
        "excluded_subtrees": excluded,
        "zone_candidates": zone_candidates,
        "relinked_zone_ids": relinked_zone_ids,
        "requests": requests,
        "unknown_probes": unknown_probes,
        "truncated": truncated,
    }


async def _sweep_scope(
    token: str,
    scope: Dict[str, Any],
    *,
    known_zone_ids: "frozenset[str] | set[str]" = frozenset(),
) -> Dict[str, Any]:
    """Sweep one mirrored scope. Returns ``{"source_scope_id",
    "excluded_subtrees", "zone_candidates", "relinked_zone_ids", "requests",
    "unknown_probes", "truncated", "error"}`` — ``excluded_subtrees`` is
    ``None`` on a fatal error (missing ``drive_id``, or a Graph read failure
    partway through the walk): the caller must then leave this scope's
    PREVIOUSLY recorded exclusion/zone state untouched rather than persist a
    partial/incomplete one.
    """
    source_scope_id = scope.get("source_scope_id")
    drive_id = scope.get("drive_id")
    root_path = scope.get("display_path") or source_scope_id or ""
    rel_root = scope_rel_root(scope.get("display_path") or "")

    base: Dict[str, Any] = {
        "source_scope_id": source_scope_id,
        "excluded_subtrees": None,
        "zone_candidates": [],
        "relinked_zone_ids": [],
        "requests": 0,
        "unknown_probes": 0,
        "truncated": False,
        "error": None,
    }
    if not drive_id or not source_scope_id:
        # See the module docstring's "Gap closed" note (Task 5) — a scope
        # confirmed before drive_id existed. Skip rather than crash the
        # connection's sweep.
        return {**base, "error": "missing_drive_id"}

    try:
        walk = await _walk_subtree_sweep(
            token,
            drive_id,
            source_scope_id,
            root_path,
            rel_root=rel_root,
            known_zone_ids=known_zone_ids,
        )
    except SharePointGraphError as exc:
        return {**base, "error": str(exc)}

    return {
        **base,
        "excluded_subtrees": walk["excluded_subtrees"],
        "zone_candidates": walk["zone_candidates"],
        "relinked_zone_ids": walk["relinked_zone_ids"],
        "requests": walk["requests"],
        "unknown_probes": walk["unknown_probes"],
        "truncated": walk["truncated"],
    }


def _reconcile_zones(
    connection_id: str,
    scope: Dict[str, Any],
    walk: Dict[str, Any],
    existing_zones: List[Dict[str, Any]],
    now_iso: str,
) -> "tuple[list[dict], list[dict]]":
    """Merge one scope's walk output (``zone_candidates``,
    ``relinked_zone_ids``) into the zone rows THIS SCOPE owns. Idempotent on
    ``zone_item_id`` — a zone's collection is created once and reused
    forever. Returns ``(this scope's updated zone rows, newly dissolved
    rows)`` — the caller folds the first into the connection-wide
    ``acl_zones`` list; the second is informational only
    (:func:`_cleanup_connection_content` independently discovers dissolved
    zones that still need tearing down, so it does not need this list).
    """
    source_scope_id = scope.get("source_scope_id")
    by_item_id = {
        z["zone_item_id"]: dict(z)
        for z in existing_zones
        if z.get("parent_scope_id") == source_scope_id and z.get("zone_item_id")
    }

    newly_dissolved: List[Dict[str, Any]] = []

    for zone_item_id in walk.get("relinked_zone_ids") or []:
        row = by_item_id.get(zone_item_id)
        if row is not None and row.get("status") == "active":
            row["status"] = "dissolved"
            newly_dissolved.append(row)
            log_safe(
                action="sharepoint_acl.zone_dissolved",
                resource=f"source_connection:{connection_id}",
                params={
                    "zone_item_id": zone_item_id,
                    "collection_id": row.get("collection_id"),
                    "scope": source_scope_id,
                },
                result="success",
                client_kind="scheduler",
            )

    parent_label = scope.get("display_path") or source_scope_id

    for candidate in walk.get("zone_candidates") or []:
        zone_item_id = candidate["zone_item_id"]
        row = by_item_id.get(zone_item_id)
        if row is None:
            name = f"{parent_label}/{candidate['name']}"
            collection_id = _create_zone_collection(name=name, zone_item_id=zone_item_id)
            row = {
                "zone_item_id": zone_item_id,
                "parent_scope_id": source_scope_id,
                "drive_id": scope.get("drive_id"),
                "name": candidate["name"],
                "display_path": f"{parent_label}/{candidate['path']}",
                "rel_path": candidate["rel_path"],
                "collection_id": collection_id,
                "detected_at": now_iso,
                "status": "active",
            }
            by_item_id[zone_item_id] = row
            log_safe(
                action="sharepoint_acl.zone_created",
                resource=f"source_connection:{connection_id}",
                params={"zone_item_id": zone_item_id, "collection_id": collection_id, "scope": source_scope_id},
                result="success",
                client_kind="scheduler",
            )
        else:
            # An existing active zone re-seen as a candidate: refresh the
            # detection timestamp only — no audit (transition-only
            # auditing, same posture as membership replacement). A
            # previously DISSOLVED zone re-seen as a candidate (its folder
            # broke inheritance again after re-linking) reactivates the same
            # way — it is still the SAME zone_item_id/collection, so no new
            # collection is minted.
            row["detected_at"] = now_iso
            row["status"] = "active"

    return (list(by_item_id.values()), newly_dissolved)


async def _sweep_connection(connection: Dict[str, Any]) -> Dict[str, Any]:
    """Sweep one connection's mirrored scopes; persists each swept scope's
    ``excluded_subtrees``, the connection's own ``acl_sweep_last_run``/
    ``acl_sweep_last_full``/``acl_zones`` bookkeeping, then (2026-08-31 plan,
    Task 6) retroactively purges already-ingested content that now falls
    under an excluded subtree/file or an active zone, and fully retires any
    zone dissolved this (or a prior, incompletely-torn-down) run. See
    :func:`run_subtree_sweep` for the full contract."""
    connection_id = connection["id"]
    scopes = _mirrored_scopes(connection)
    t0 = time.monotonic()

    existing_zones = zone_rows(connection)
    known_zone_ids_by_scope: Dict[str, set] = {}
    for z in existing_zones:
        if z.get("status") == "active" and z.get("parent_scope_id"):
            known_zone_ids_by_scope.setdefault(z["parent_scope_id"], set()).add(z["zone_item_id"])

    error: Optional[str] = None
    token: Optional[str] = None
    try:
        settings = resolve_sharepoint_settings(connection)
        token = await graph_client.get_app_token(
            settings.tenant_id, settings.client_id, settings.private_key, client_secret=settings.client_secret
        )
    except (SharePointSettingsError, SharePointGraphError) as exc:
        error = str(exc)

    excluded_total = 0
    requests_total = 0
    unknown_total = 0
    truncated_any = False
    exclusions_by_scope: Dict[str, List[Dict[str, Any]]] = {}
    zone_updates_by_scope: Dict[str, "tuple[list[dict], list[dict]]"] = {}
    now_iso = datetime.now(timezone.utc).isoformat()

    if token is not None:
        for scope in scopes:
            known_ids = known_zone_ids_by_scope.get(scope.get("source_scope_id"), set())
            report = await _sweep_scope(token, scope, known_zone_ids=known_ids)
            requests_total += report["requests"]
            unknown_total += report["unknown_probes"]
            truncated_any = truncated_any or report["truncated"]
            if report["excluded_subtrees"] is not None:
                excluded_total += len(report["excluded_subtrees"])
                exclusions_by_scope[report["source_scope_id"]] = report["excluded_subtrees"]
                zone_updates_by_scope[report["source_scope_id"]] = _reconcile_zones(
                    connection_id, scope, report, existing_zones, now_iso
                )
            if report.get("error") and error is None:
                error = report["error"]

    duration_ms = int((time.monotonic() - t0) * 1000)

    # A full sweep walk can run for a long time (module docstring: "multi-hour,
    # not a nightly job"), so `connection` (read once at the start of this
    # call) can be well out of date by now. Re-read fresh immediately before
    # building the `scopes` patch rather than rebuild it from the stale
    # snapshot — otherwise this sweep's own `scopes` write would silently
    # revert anything an admin (or the sync) wrote to `scopes` in the
    # meantime. `config_patch` below still re-reads a SECOND time inside its
    # own transaction, so this fresh read only narrows the race window, it
    # doesn't need to close it perfectly (a per-scope-field merge finer than
    # whole-`scopes`-list replacement would, but `excluded_subtrees` is a
    # per-scope value inside a list column, and `confirm_scope`/`remove_scope`
    # racing this sweep is a human admin action, not a second automated
    # writer — not worth the extra complexity here).
    fresh_connection = source_connections_repo().get(connection_id) or connection
    all_scopes = list((fresh_connection.get("config") or {}).get("scopes") or [])
    updated_scopes = [
        {**s, "excluded_subtrees": exclusions_by_scope[s["source_scope_id"]]}
        if s.get("source_scope_id") in exclusions_by_scope
        else s
        for s in all_scopes
    ]

    # Zone rows from scopes NOT touched this run (a manual scope, a
    # missing_drive_id scope, or the whole connection failing before any
    # scope was swept) pass through unchanged; touched scopes contribute
    # their freshly reconciled rows (new/refreshed/dissolved).
    touched_scope_ids = set(zone_updates_by_scope.keys())
    all_zone_rows = [z for z in existing_zones if z.get("parent_scope_id") not in touched_scope_ids]
    for updated_rows, _dissolved in zone_updates_by_scope.values():
        all_zone_rows.extend(updated_rows)

    config_patch: Dict[str, Any] = {
        "scopes": updated_scopes,
        "acl_zones": all_zone_rows,
        "acl_sweep_last_run": {
            "at": now_iso,
            "ok": error is None,
            "scopes": len(scopes),
            "excluded": excluded_total,
            # Request count is exact (one $batch POST per up-to-20 folders, plus
            # one "list children" call per visited folder). A literal 429-vs-
            # other-failure breakdown is NOT available without deeper
            # instrumentation of graph_client.probe_unique_permissions (which
            # collapses every failure mode — network error, non-200, malformed
            # body, AND 429 — into the same "unknown" answer, by design: see its
            # own docstring); `unknown_probes` is the honest proxy this module
            # can report today, not a literal 429 counter.
            "requests": requests_total,
            "unknown_probes": unknown_total,
            "truncated": truncated_any,
            "error": error,
            "duration_ms": duration_ms,
        },
    }
    if error is None:
        config_patch["acl_sweep_last_full"] = now_iso
    source_connections_repo().config_patch(connection_id, config_patch)

    # 2026-08-31 plan, Task 6: retroactive cleanup. Runs AFTER the detection
    # state above is durably persisted, so a crash mid-cleanup never loses
    # the sweep's own findings — the next sweep (or an admin re-check) would
    # simply re-attempt the same cleanup, which is idempotent by design (see
    # _cleanup_connection_content's own docstring).
    exclusions_by_scope_full = {
        s["source_scope_id"]: s.get("excluded_subtrees") or [] for s in updated_scopes if s.get("source_scope_id")
    }
    cleanup = _cleanup_connection_content(connection, exclusions_by_scope_full, all_zone_rows)
    if cleanup["removed_files"] or cleanup["dissolved_zones"]:
        source_connections_repo().config_patch(
            connection_id,
            {"acl_sweep_last_run": {**config_patch["acl_sweep_last_run"], **cleanup}},
        )

    return {"scopes": len(scopes), "excluded": excluded_total, "error": error}


def _cleanup_connection_content(
    connection: Dict[str, Any],
    exclusions_by_scope: Dict[str, List[Dict[str, Any]]],
    zone_rows_all: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Retroactive cleanup (2026-08-31 plan, Task 6): purge already-ingested
    content whose source subtree/file has since been excluded — or whose
    parent-side copy now falls under an ACTIVE zone (re-homing to a zone's
    OWN collection is a COPY, not a move; the parent copy must die too) —
    and fully retire any permission zone whose folder has re-linked
    inheritance (``status == "dissolved"``).

    Matching, per the plan's "Semantics locked here": stable-id matching is
    EXACT (``graph:<item-id>`` against a ``kind="file"`` exclusion entry's
    ``item_id``); path matching is component-safe prefix (``path == p or
    path.startswith(p + "/")``) against a ``kind="folder"`` exclusion
    entry's ``rel_path`` and an active zone's ``rel_path``. A legacy
    exclusion entry with no ``rel_path`` (pre-Task-3) cannot be path-matched
    — its subtree was never crawled, so nothing arrives for it under that
    path anyway.

    KNOWN GAP, not yet closed (#2011): for an anonymize-marked scope, path
    matching above compares an ANONYMIZED ``corpus_files.path`` against a
    REAL ``rel_path`` (the admin's exclusion/zone config never was, and
    should never be, anonymized) — they can never agree, so a folder
    exclusion or zone dissolution added after a document was already
    ingested into such a scope no longer retroactively purges it. Stable-id
    (file-kind exclusion) matching is unaffected. See the inline comment at
    the match site.

    Deletion uses the EXACT machinery ``DELETE /files/{id}`` uses
    (``app.api.collections._purge_file_row`` / ``_record_corpus_file_event``
    / ``_sweep_facts_orphans_after_delete`` — imported locally to avoid a
    module-level dependency from a connector onto the API layer), one
    ``content_purged`` audit row per collection with a non-zero removal
    count (never per-file — volume), and one orphan sweep per connection
    when anything was actually removed.

    Stable-id matching needs ``corpus_file_sources_repo()`` (PG-only,
    :class:`RequiresPostgresBackend` on a DuckDB-backed instance) — the
    first such failure flips an internal flag so every LATER candidate row
    in this same call skips the lookup instead of raising again; path
    matching (which needs no PG-only table) still runs fully. A DuckDB-
    backed instance can never have facts/claims either way, so this only
    narrows WHICH files get caught by this pass, never breaks it.

    Dissolved-zone teardown order (never relax): purge files → sentinel
    grant removal (``_reconcile_grants(collection_id, [], zone_item_id)``)
    → ``resource_grants_repo().delete_by_resource`` (closes the verified
    dangling-grants gap — ``DELETE /api/collections/{id}`` never touches
    ``resource_grants``) → ``file_corpora_repo().soft_delete``. Idempotent:
    a dissolved zone whose collection is already soft-deleted (``get()``
    returns ``None``) is skipped — this also means a PRIOR run's partially
    completed teardown is safely retried on the next sweep.

    Returns ``{"removed_files": int, "dissolved_zones": int}``.
    """
    from app.api.collections import _purge_file_row, _record_corpus_file_event, _sweep_facts_orphans_after_delete

    scopes_by_id = {s["source_scope_id"]: s for s in _mirrored_scopes(connection) if s.get("source_scope_id")}

    removed_files = 0
    any_removed = False
    stable_ids_available = True

    def _stable_id_for(corpus_file_id: str) -> Optional[str]:
        nonlocal stable_ids_available
        if not stable_ids_available:
            return None
        try:
            anchor = corpus_file_sources_repo().get(corpus_file_id)
        except RequiresPostgresBackend:
            stable_ids_available = False
            return None
        return anchor.get("source_stable_id") if anchor else None

    zones_by_parent: Dict[str, List[Dict[str, Any]]] = {}
    for zone in zone_rows_all:
        parent = zone.get("parent_scope_id")
        if parent:
            zones_by_parent.setdefault(parent, []).append(zone)

    for source_scope_id, scope in scopes_by_id.items():
        collection_id = scope.get("collection_id")
        if not collection_id:
            continue

        exclusions = exclusions_by_scope.get(source_scope_id) or []
        folder_prefixes = [e["rel_path"] for e in exclusions if e.get("kind") == "folder" and e.get("rel_path")]
        excluded_file_ids = {
            f"graph:{e['item_id']}" for e in exclusions if e.get("kind") == "file" and e.get("item_id")
        }
        zone_prefixes = [
            z["rel_path"]
            for z in zones_by_parent.get(source_scope_id, [])
            if z.get("status") == "active" and z.get("rel_path")
        ]
        prefixes = folder_prefixes + zone_prefixes
        if not prefixes and not excluded_file_ids:
            continue

        removed_here = 0
        for row in corpus_files_repo().list_for_corpus(collection_id):
            # KNOWN GAP for an anonymize-marked scope: `row["path"]` is the
            # ANONYMIZED path once the source scope anonymizes (the crawler
            # never stores the real one — see
            # `connectors.sharepoint.crawler._anonymize_identity`), but
            # `prefix` below is the REAL folder path an admin picked in the
            # exclusion/zone UI. The two can never prefix-match each other,
            # so a folder-kind exclusion or a zone dissolution added AFTER a
            # document was already ingested into such a scope silently stops
            # retroactively purging it here — file-kind exclusions (the
            # `excluded_file_ids` stable-id branch below) are UNAFFECTED.
            # Tracked, not silently accepted (#2011): reconstructing `prefix`
            # through the same per-instance anonymization (deterministic, so
            # it CAN be derived) is the fix; it needs the scope's own
            # key/detector threaded in here, which is more than this pass
            # does today.
            path = row.get("path")
            matched = bool(path and any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes))
            if not matched and excluded_file_ids:
                matched = _stable_id_for(row["id"]) in excluded_file_ids
            if not matched:
                continue
            _purge_file_row(collection_id, row)
            _record_corpus_file_event(
                corpus_id=collection_id,
                file_id=row["id"],
                change="deleted",
                name=row.get("filename"),
                path=path,
                source_stable_id=None,
            )
            removed_here += 1

        if removed_here:
            removed_files += removed_here
            any_removed = True
            log_safe(
                action="sharepoint_acl.content_purged",
                resource=f"file_corpus:{collection_id}",
                params={"collection_id": collection_id, "removed": removed_here, "trigger": "sharepoint-acl-cleanup"},
                result="success",
                client_kind="scheduler",
            )

    dissolved_zones = 0
    for zone in zone_rows_all:
        if zone.get("status") != "dissolved":
            continue
        zone_collection_id = zone.get("collection_id")
        if not zone_collection_id or file_corpora_repo().get(zone_collection_id) is None:
            continue  # never had a collection, or already torn down — idempotent skip

        zone_removed = 0
        for row in corpus_files_repo().list_for_corpus(zone_collection_id):
            _purge_file_row(zone_collection_id, row)
            _record_corpus_file_event(
                corpus_id=zone_collection_id,
                file_id=row["id"],
                change="deleted",
                name=row.get("filename"),
                path=row.get("path"),
                source_stable_id=None,
            )
            zone_removed += 1

        _reconcile_grants(zone_collection_id, [], zone.get("zone_item_id"))
        resource_grants_repo().delete_by_resource(ResourceType.COLLECTION.value, zone_collection_id)
        file_corpora_repo().soft_delete(zone_collection_id)
        dissolved_zones += 1

        if zone_removed:
            removed_files += zone_removed
            any_removed = True
            log_safe(
                action="sharepoint_acl.content_purged",
                resource=f"file_corpus:{zone_collection_id}",
                params={
                    "collection_id": zone_collection_id,
                    "removed": zone_removed,
                    "trigger": "sharepoint-acl-cleanup-zone-dissolved",
                },
                result="success",
                client_kind="scheduler",
            )

    if any_removed:
        _sweep_facts_orphans_after_delete(trigger="sharepoint-acl-cleanup")

    return {"removed_files": removed_files, "dissolved_zones": dissolved_zones}


def run_subtree_sweep(payload: dict) -> dict:
    """Entry point for the ``sharepoint-subtree-sweep`` worker job kind
    (spec §3(b), §6.2, §6.3; sweep v2 — 2026-08-31 plan, Tasks 3/6).

    ``payload = {"connection_id": str | None}`` — same shape as
    :func:`run_acl_sync`. A specific id sweeps just that connection and
    BYPASSES the per-connection cadence self-guard below (explicit demand
    wins — same posture as ``app/api/store_lint_admin.py``'s own ``force``
    flag); ``None`` sweeps every ``source_type='sharepoint'`` connection
    with at least one mirrored scope THAT IS DUE (see :func:`_sweep_due`).

    Cadence: the scheduler row fires this job DAILY via native cron (see
    ``services/scheduler/__main__.py``). Each connection ALSO tracks its own
    ``config["acl_sweep_last_full"]`` and is skipped by the unconditional
    sweep-all payload (``connection_id=None``) until
    ``acl_sync.sweep_interval_days`` (default 1) has elapsed since its last
    FULL (error-free) sweep — defense-in-depth against a restart-refire, not
    a second scheduling mechanism.

    Detection AND, since Task 3/6, three further actions: (1) probes files
    as well as folders; (2) ALWAYS promotes a broken-inheritance folder to
    its own permission zone (mirrored by :func:`run_acl_sync`) instead of
    excluding it outright — no separate opt-in switch, see
    :func:`_walk_subtree_sweep`'s own docstring — and dissolves a zone whose
    root re-links inheritance; (3) retroactively purges already-ingested
    content that now falls under an exclusion or an active zone, and fully
    retires a dissolved zone's collection. Each mirrored scope's
    ``excluded_subtrees`` (``{item_id, path, rel_path, kind, detected_at}``
    per detected root/file) and the connection's ``acl_zones`` are handed to
    the external producer via ``app/worker/kinds.py::_run_corpus_extraction``'s
    ``AGNES_SP_EXCLUDED_SUBTREE_IDS`` env var / corpus map (2026-08-31 plan,
    Task 7) — HONORING them on the crawl side stays external-producer work;
    Agnes now also enforces them server-side at ingest time (Task 5).

    Feature-gated by the single ``sharepoint`` switch (same flag as
    :func:`run_acl_sync`) — disabled instance returns ``{"skipped":
    "sharepoint disabled"}``, harmless for the scheduler's unconditional
    daily enqueue.

    Returns ``{"connections": N, "scopes": M, "excluded": X, "skipped_not_due":
    Y, "errors": [...]}`` aggregated across every connection actually swept.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        return {"skipped": "sharepoint disabled"}

    return asyncio.run(_run_subtree_sweep_async(payload))


async def _run_subtree_sweep_async(payload: dict) -> dict:
    connections_repo = source_connections_repo()
    connection_id = payload.get("connection_id")

    errors: List[Dict[str, Any]] = []
    targets: List[tuple] = []  # (connection, explicit) — explicit bypasses the due-check
    if connection_id:
        row = connections_repo.get(connection_id)
        if row is not None and row.get("source_type") == "sharepoint":
            targets = [(row, True)]
        else:
            errors.append({"connection_id": connection_id, "error": "connection_not_found"})
    else:
        targets = [(c, False) for c in connections_repo.list(source_type="sharepoint") if _mirrored_scopes(c)]

    totals: Dict[str, Any] = {
        "connections": 0,
        "scopes": 0,
        "excluded": 0,
        "skipped_not_due": 0,
        "errors": errors,
    }
    for connection, explicit in targets:
        if not explicit and not _sweep_due(connection):
            totals["skipped_not_due"] += 1
            continue
        result = await _sweep_connection(connection)
        totals["connections"] += 1
        totals["scopes"] += result["scopes"]
        totals["excluded"] += result["excluded"]
        if result.get("error"):
            totals["errors"].append({"connection_id": connection["id"], "error": result["error"]})
    return totals
