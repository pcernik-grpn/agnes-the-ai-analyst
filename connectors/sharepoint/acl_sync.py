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
action (spec §5.1). Entirely behind ``acl_mirroring.enabled`` (default off);
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
Detection only; deciding what to do with the exclusion list (the actual
crawl) is the external producer's job (§6.3's division of labor) — this
module writes the list, ``app/worker/kinds.py::_run_corpus_extraction``
hands it to the producer via ``AGNES_SP_EXCLUDED_SUBTREE_IDS``, and
HONORING it is out of this repo's scope (see that function's docstring).
Own job kind (``sharepoint-subtree-sweep``), own weekly scheduler cadence
(§6.2's cost model: a full probe pass over a large library is multi-hour,
not a nightly job) — see :func:`run_subtree_sweep`'s own docstring.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.resource_types import ResourceType
from connectors.sharepoint import graph_client
from connectors.sharepoint.graph_client import SharePointGraphError
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from src.audit_helpers import log_safe
from src.repositories import (
    resource_grants_repo,
    source_connections_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

ACL_SYNC_SENTINEL = "system:sharepoint-acl-sync"
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
carry-forward already covers it; only the two genuinely NEW top-level keys
above need adding."""


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

    A group whose ``transitiveMembers`` read fails keeps its PREVIOUS
    membership (fail-soft on identity resolution, not on reachability — the
    Google-sync precedent) and marks that scope stale in the run report; a
    scope root permissions read that fails aborts that scope's
    reconciliation entirely (fail-closed — no partial diff is ever applied).

    Records a last-run block into the connection's own
    ``config["acl_sync_last_run"]`` (matched/unmatched counts, unhonored
    permission types, per-collection grant deltas, stale scopes, the error if
    any) — the source card's data, no new table. ``config
    ["acl_sync_last_success_at"]`` tracks the last time a connection's run
    completed WITHOUT error, independent of the (possibly-failing) last run,
    so ``acl_sync.guarantee_mode == "must_not"`` can measure staleness
    correctly across a run of consecutive failures.

    Staleness (Q7 fork, ``acl_guarantee_mode`` switch): under ``must_not``, a
    FAILED run past ``acl_sync.max_stale_hours`` (default 72) since the last
    success suspends every sentinel-owned grant on that connection's
    mirrored collections (deleted, not merely flagged — the next successful
    sync rewrites them; ``accessible_collection_ids`` itself is never
    touched, spec §5.4) and audits ``sharepoint_acl.grants_suspended``.
    Under ``should_not``, staleness is never enforced — the connection's
    mirrored grants persist with the staleness visible on the source card.

    Returns ``{"connections": N, "scopes": M, "matched": X, "unmatched": Y,
    "errors": [...]}`` (aggregated across every connection processed), or
    ``{"skipped": "acl_mirroring disabled"}`` when the feature flag is off —
    the scheduler enqueues this kind unconditionally, so the no-op has to be
    cheap and harmless, same posture as ``ducklake-maintenance``
    (``app/worker/kinds.py``).
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("acl_mirroring", "enabled", env_var="AGNES_ACL_MIRRORING_ENABLED", default=False):
        return {"skipped": "acl_mirroring disabled"}

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
        connections = [c for c in connections_repo.list(source_type="sharepoint") if _mirrored_scopes(c)]

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
    """Sync one connection's mirrored scopes; persists the last-run block,
    audits the run-level actions, and applies must_not staleness suspension.
    See :func:`run_acl_sync` for the full contract."""
    connection_id = connection["id"]
    scopes = _mirrored_scopes(connection)
    t0 = time.monotonic()

    error: Optional[str] = None
    token: Optional[str] = None
    try:
        settings = resolve_sharepoint_settings(connection)
        token = await graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key)
    except (SharePointSettingsError, SharePointGraphError) as exc:
        error = str(exc)

    matched_total = 0
    unmatched_total = 0
    unhonored_all: List[Dict[str, Any]] = []
    grant_deltas: Dict[str, Dict[str, List[str]]] = {}
    stale_scopes: List[str] = []

    if token is not None:
        for scope in scopes:
            scope_report = await _sync_scope(connection_id, scope, token)
            matched_total += scope_report["matched"]
            unmatched_total += scope_report["unmatched"]
            unhonored_all.extend(
                {**item, "scope": scope_report["source_scope_id"]} for item in scope_report["unhonored"]
            )
            if scope_report.get("stale"):
                stale_scopes.append(scope_report["source_scope_id"])
            if scope_report.get("grant_delta"):
                grant_deltas[scope_report["collection_id"]] = scope_report["grant_delta"]
            if scope_report.get("error") and error is None:
                error = scope_report["error"]

    ok = error is None
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    duration_ms = int((time.monotonic() - t0) * 1000)

    new_config = dict(connection.get("config") or {})
    new_config["acl_sync_last_run"] = {
        "at": now_iso,
        "ok": ok,
        "matched": matched_total,
        "unmatched": unmatched_total,
        "unhonored": unhonored_all,
        "grant_deltas": grant_deltas,
        "stale_scopes": stale_scopes,
        "error": error,
        "duration_ms": duration_ms,
    }
    if ok:
        new_config["acl_sync_last_success_at"] = now_iso
    source_connections_repo().update(connection_id, config=new_config)

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


async def _sync_scope(connection_id: str, scope: Dict[str, Any], token: str) -> Dict[str, Any]:
    """Sync one mirrored scope: read → classify → resolve → diff → write.

    Returns a report dict with ``source_scope_id``, ``collection_id``,
    ``matched``, ``unmatched``, ``unhonored``, ``stale``, ``error`` and
    ``grant_delta`` (``None`` when nothing changed).
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
        "grant_delta": None,
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

    classified = classify_permissions(perms)

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

    grant_delta = _reconcile_grants(collection_id, target_group_ids, source_scope_id) if collection_id else None

    return {
        **base,
        "matched": matched,
        "unmatched": unmatched,
        "unhonored": classified.unhonored,
        "stale": stale,
        "grant_delta": grant_delta,
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
    written to here — the sentinel-segregation contract."""
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
        grants.ensure_grant(group_id, ResourceType.COLLECTION.value, collection_id, assigned_by=ACL_SYNC_SENTINEL)
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
    scopes' collections. ``accessible_collection_ids`` is never touched
    directly — deleting the grant IS the suspension (spec §5.4); the next
    successful sync's :func:`_reconcile_grants` rewrites them."""
    collection_ids = {s.get("collection_id") for s in _mirrored_scopes(connection) if s.get("collection_id")}
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
# Broken-inheritance subtree sweep (2026-08-30 plan, Task 7). See the module
# docstring's "Subtree sweep" paragraph for the wider picture.
# ---------------------------------------------------------------------------

#: must_not/should_not-independent default cadence (days) between two full
#: sweeps of one connection's mirrored scopes — overridable via the
#: ``acl_sweep_interval_days`` switch (``acl_sync.sweep_interval_days``).
#: The scheduler row itself already fires WEEKLY (native cron, see
#: services/scheduler/__main__.py) rather than nightly — this per-connection
#: self-guard is defense-in-depth against a restart-refire (scheduler
#: ``last_run`` state can be lost across a container recreate), the same
#: risk ``app/api/store_lint_admin.py``'s own min-interval self-guard
#: protects against for its weekly row.
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
    run does not push the next attempt a further week out."""
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


async def _walk_subtree_sweep(token: str, drive_id: str, root_item_id: str, root_path: str) -> Dict[str, Any]:
    """Breadth-first walk of one scope's folder tree, probing
    ``hasUniqueRoleAssignments`` per folder (``graph_client
    .probe_unique_permissions``, already ``$batch``-based — 20 items per
    call) and collecting the ROOT of every broken-inheritance subtree —
    **never descending into a detected subtree** (spec §3(b): its children
    are excluded wholesale, never individually probed or crawled).

    The scope ROOT ITSELF is never probed or excluded here — its own
    permissions are what :func:`_sync_scope` already mirrors; this sweep
    only ever excludes something FINER than the scope.

    A folder whose probe comes back ``None`` ("unknown" —
    ``probe_unique_permissions``'s own honest answer when the signal could
    not be read) is treated THE SAME as a detected break: fail-closed, never
    silently rendered as "clean" just because the signal was unavailable.

    Raises :class:`SharePointGraphError` on a fatal read failure partway
    through the walk — the caller (:func:`_sweep_scope`) treats that as
    "leave this scope's previous exclusion list untouched" (fail-closed, no
    partial diff is ever persisted), mirroring :func:`_sync_scope`'s own
    "a scope-root read failure aborts that scope's reconciliation entirely"
    posture.
    """
    excluded: List[Dict[str, Any]] = []
    requests = 0
    unknown_probes = 0
    visited = 0
    truncated = False
    queue: List[tuple] = [(root_item_id, root_path)]

    while queue:
        if visited >= _MAX_SWEEP_FOLDERS_VISITED:
            truncated = True
            break
        item_id, path = queue.pop(0)
        visited += 1

        children = await graph_client.list_item_children(token, drive_id, item_id)
        requests += 1
        folders = [c for c in children if c.get("is_folder")]
        if not folders:
            continue

        ids = [f["id"] for f in folders]
        flags = await graph_client.probe_unique_permissions(token, drive_id, ids)
        # One `$batch` POST per up-to-20 items (graph_client._GRAPH_BATCH_SIZE_CAP)
        # — matches probe_unique_permissions's own batching exactly, so the
        # request count here is observed against the SAME chunking, not a
        # separate guess.
        requests += -(-len(ids) // graph_client._GRAPH_BATCH_SIZE_CAP)

        for folder in folders:
            child_path = f"{path}/{folder['name']}" if path else folder["name"]
            flag = flags.get(folder["id"])
            if flag is None:
                unknown_probes += 1
            if flag is None or flag is True:
                excluded.append(
                    {
                        "item_id": folder["id"],
                        "path": child_path,
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                continue  # never descend into a detected/unknown subtree
            queue.append((folder["id"], child_path))

    return {
        "excluded_subtrees": excluded,
        "requests": requests,
        "unknown_probes": unknown_probes,
        "truncated": truncated,
    }


async def _sweep_scope(token: str, scope: Dict[str, Any]) -> Dict[str, Any]:
    """Sweep one mirrored scope. Returns ``{"source_scope_id",
    "excluded_subtrees", "requests", "unknown_probes", "truncated",
    "error"}`` — ``excluded_subtrees`` is ``None`` on a fatal error (missing
    ``drive_id``, or a Graph read failure partway through the walk): the
    caller must then leave this scope's PREVIOUSLY recorded exclusion list
    untouched rather than persist a partial/incomplete one.
    """
    source_scope_id = scope.get("source_scope_id")
    drive_id = scope.get("drive_id")
    root_path = scope.get("display_path") or source_scope_id or ""

    base: Dict[str, Any] = {
        "source_scope_id": source_scope_id,
        "excluded_subtrees": None,
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
        walk = await _walk_subtree_sweep(token, drive_id, source_scope_id, root_path)
    except SharePointGraphError as exc:
        return {**base, "error": str(exc)}

    return {
        **base,
        "excluded_subtrees": walk["excluded_subtrees"],
        "requests": walk["requests"],
        "unknown_probes": walk["unknown_probes"],
        "truncated": walk["truncated"],
    }


async def _sweep_connection(connection: Dict[str, Any]) -> Dict[str, Any]:
    """Sweep one connection's mirrored scopes; persists each swept scope's
    ``excluded_subtrees`` and the connection's own ``acl_sweep_last_run``/
    ``acl_sweep_last_full`` bookkeeping. See :func:`run_subtree_sweep` for
    the full contract."""
    connection_id = connection["id"]
    scopes = _mirrored_scopes(connection)
    t0 = time.monotonic()

    error: Optional[str] = None
    token: Optional[str] = None
    try:
        settings = resolve_sharepoint_settings(connection)
        token = await graph_client.get_app_token(settings.tenant_id, settings.client_id, settings.private_key)
    except (SharePointSettingsError, SharePointGraphError) as exc:
        error = str(exc)

    excluded_total = 0
    requests_total = 0
    unknown_total = 0
    truncated_any = False
    exclusions_by_scope: Dict[str, List[Dict[str, Any]]] = {}

    if token is not None:
        for scope in scopes:
            report = await _sweep_scope(token, scope)
            requests_total += report["requests"]
            unknown_total += report["unknown_probes"]
            truncated_any = truncated_any or report["truncated"]
            if report["excluded_subtrees"] is not None:
                excluded_total += len(report["excluded_subtrees"])
                exclusions_by_scope[report["source_scope_id"]] = report["excluded_subtrees"]
            if report.get("error") and error is None:
                error = report["error"]

    now_iso = datetime.now(timezone.utc).isoformat()
    duration_ms = int((time.monotonic() - t0) * 1000)

    all_scopes = list((connection.get("config") or {}).get("scopes") or [])
    updated_scopes = [
        {**s, "excluded_subtrees": exclusions_by_scope[s["source_scope_id"]]}
        if s.get("source_scope_id") in exclusions_by_scope
        else s
        for s in all_scopes
    ]

    new_config = dict(connection.get("config") or {})
    new_config["scopes"] = updated_scopes
    new_config["acl_sweep_last_run"] = {
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
    }
    if error is None:
        new_config["acl_sweep_last_full"] = now_iso
    source_connections_repo().update(connection_id, config=new_config)

    return {"scopes": len(scopes), "excluded": excluded_total, "error": error}


def run_subtree_sweep(payload: dict) -> dict:
    """Entry point for the ``sharepoint-subtree-sweep`` worker job kind
    (spec §3(b), §6.2, §6.3).

    ``payload = {"connection_id": str | None}`` — same shape as
    :func:`run_acl_sync`. A specific id sweeps just that connection and
    BYPASSES the per-connection cadence self-guard below (explicit demand
    wins — same posture as ``app/api/store_lint_admin.py``'s own ``force``
    flag); ``None`` sweeps every ``source_type='sharepoint'`` connection
    with at least one mirrored scope THAT IS DUE (see :func:`_sweep_due`).

    Cadence: the scheduler row fires this job WEEKLY via native cron (see
    ``services/scheduler/__main__.py`` — the same grammar
    ``store-lint-audit`` already uses for its own weekly row), not nightly —
    §6.2's cost model puts one full probe pass over a large library at
    multi-hour, so nightly would starve the shared per-app-per-tenant Graph
    throttle budget the content crawl also depends on. Each connection ALSO
    tracks its own ``config["acl_sweep_last_full"]`` and is skipped by the
    unconditional sweep-all payload (``connection_id=None``) until
    ``acl_sync.sweep_interval_days`` (default 7) has elapsed since its last
    FULL (error-free) sweep — defense-in-depth against a restart-refire, not
    a second scheduling mechanism.

    Detection only — this job decides WHAT is excluded (writes each mirrored
    scope's ``excluded_subtrees``: ``{item_id, path, detected_at}`` per
    detected root) but never crawls or decides access on its own. The
    exclusion list is handed to the external producer via
    ``app/worker/kinds.py::_run_corpus_extraction``'s
    ``AGNES_SP_EXCLUDED_SUBTREE_IDS`` env var — HONORING the list (actually
    skipping those subtrees during crawl) is external-producer work (spec
    §6.3's division of labor; §10's repo boundary), out of this repo.

    Feature-gated by ``acl_mirroring.enabled`` (same flag as
    :func:`run_acl_sync`) — disabled instance returns ``{"skipped":
    "acl_mirroring disabled"}``, harmless for the scheduler's unconditional
    weekly enqueue.

    Returns ``{"connections": N, "scopes": M, "excluded": X, "skipped_not_due":
    Y, "errors": [...]}`` aggregated across every connection actually swept.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("acl_mirroring", "enabled", env_var="AGNES_ACL_MIRRORING_ENABLED", default=False):
        return {"skipped": "acl_mirroring disabled"}

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
