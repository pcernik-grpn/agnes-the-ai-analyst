"""Admin REST API for the SharePoint file-source connect wizard (spec
2026-08-27 §13.2 — "Connect wizard (file source): three steps").

Every route on this router is gated FIRST by the module-level
``dependencies=[Depends(_require_sharepoint_enabled)]`` — the whole SharePoint
admin surface answers ``409 feature_disabled`` when the single ``sharepoint``
switch (``app/switches.py``) is off, before any per-route auth even runs. On
top of that, most individual routes are also gated by ``Depends(require_admin)``
(see those routes' own
docstrings).

Surface:

  GET    /api/admin/sharepoint/connections/{id}/tree        — one level of the live
                                                                Graph folder tree
                                                                (sites -> drives -> root
                                                                children -> arbitrary-depth
                                                                subfolder children);
                                                                ``?site_id=``/``?drive_id=``/
                                                                ``?item_id=`` pick the level
                                                                (TCRD-240).
  GET    /api/admin/sharepoint/connections/{id}/tree/search  — bounded BFS folder search
                                                                (``?q=``, ``?mode=``) over the
                                                                same tree — never Graph's own
                                                                ``/search`` (TCRD-240).
  POST   /api/admin/sharepoint/connections/{id}/manual-sites — resolve a site by URL (the
                                                                same ``Sites.Selected`` escape
                                                                hatch as ``?site_url=`` above)
                                                                AND persist it on the
                                                                connection's own
                                                                ``config.manual_sites``, so it
                                                                survives a wizard reopen
                                                                (2026-09-01 bug: the tree
                                                                endpoint alone only ever
                                                                resolved, never stored).
                                                                Idempotent on the resolved
                                                                site id.
  DELETE /api/admin/sharepoint/connections/{id}/manual-sites — forget one site added by URL
                                                                (``?site_id=``).
  GET    /api/admin/sharepoint/connections/{id}/scopes       — list the connection's
                                                                confirmed scope rows,
                                                                enriched with collection +
                                                                group-grant info for the
                                                                wizard's step-2/3 preview.
  POST   /api/admin/sharepoint/connections/{id}/scopes       — confirm one scope: creates
                                                                (or reuses) its collection,
                                                                upserts the scope row, and
                                                                optionally applies group
                                                                grants (step 3).
  DELETE /api/admin/sharepoint/connections/{id}/scopes       — unselect a scope
                                                                (``?source_scope_id=``);
                                                                removes the row, leaves any
                                                                already-created collection
                                                                alone.
  GET    /api/admin/sharepoint/connections/{id}/certificate  — read-only certificate metadata
                                                                (thumbprint, subject/issuer, expiry)
                                                                derived at request time from the
                                                                connection's already-stored PEM.
                                                                Never the private key. ``certificate:
                                                                null`` (plus ``reason``) when no
                                                                certificate is configured or it
                                                                cannot be parsed — never a 500.
  POST   /api/admin/sharepoint/connections/{id}/extract       — one-off admin trigger for the
                                                                existing ``corpus-extraction`` job
                                                                kind (TCRD-226). Refuses BEFORE
                                                                enqueueing (typed 409, never a job
                                                                that fails 30 minutes later in a
                                                                worker) when ``sharepoint.enabled``
                                                                is off or the ``extraction`` extra
                                                                is not installed — see
                                                                ``_extraction_readiness``.
                                                                Deduped on a stable
                                                                per-connection idempotency key
                                                                shared with the sweep below.
  POST   /api/admin/sharepoint/extraction/run-due             — scheduler-driven sweep (TCRD-226):
                                                                fires ``corpus-extraction`` for
                                                                every SharePoint connection whose
                                                                cadence (``extraction.schedule``, a
                                                                single instance-wide setting applied
                                                                to each connection's own last-run
                                                                stamp) says it is due. A clean no-op
                                                                when the feature isn't usable or no
                                                                schedule is configured — mirrors
                                                                ``POST /api/v1/agents/run-due``'s
                                                                shape (walk + per-row due-check +
                                                                enqueue into an EXISTING job kind,
                                                                no second scheduling mechanism).
  POST   /api/admin/sharepoint/connections/{id}/acl-sync       — admin "sync now" trigger (spec
                                                                §5.1; 2026-08-30 plan, Task 5) for
                                                                the ``sharepoint-acl-sync`` job
                                                                (``connectors/sharepoint/
                                                                acl_sync.py::run_acl_sync``). Same
                                                                enqueue/dedup mechanics as
                                                                ``.../extract`` above; the
                                                                router-level ``sharepoint.enabled``
                                                                gate already refuses with ``409
                                                                feature_disabled`` when the
                                                                connector is off.
  POST   /api/admin/sharepoint/connections/{id}/subtree-sweep  — admin "re-check subtrees now"
                                                                trigger (2026-08-31 plan, Task 8)
                                                                for the ``sharepoint-subtree-sweep``
                                                                job (``connectors/sharepoint/
                                                                acl_sync.py::run_subtree_sweep``).
                                                                Identical enqueue/dedup mechanics
                                                                to ``.../acl-sync`` above — the
                                                                explicit-connection payload bypasses
                                                                the job's own per-connection
                                                                due-guard, so this is always a real,
                                                                immediate sweep.

Scope rows live inside the connection's own ``config.scopes`` — a JSON list,
no new table (``source_connections.config`` is already a JSON column on both
backends). Each row is ``{source_scope_id, display_path, anonymize,
collection_id, access_mode, drive_id, audience_classes}`` (spec §13.2,
extended by §2.5/§5 for ACL mirroring and §4.1-4.3 for the per-scope
audience-class mapping — 2026-08-30 plan, Task 8); group grants are NOT
duplicated here — they are ordinary
``resource_grants`` rows on the collection, same primitive ``/admin/access``
already reads (spec: "facts are never granted... zero new grant type"),
except a ``sharepoint-acl-sync``-written (sentinel-owned, ``assigned_by
='system:sharepoint-acl-sync'``) grant, which this wizard's own group-grant
checkboxes never delete (see :func:`confirm_scope`).

Idempotency: confirming the SAME ``source_scope_id`` twice reuses the
existing row's ``collection_id`` rather than creating a second collection —
the storage anchor is the source scope id, not the display path (§6's
"stored by source folder/drive id, not by path" principle, applied to the
wizard's own bookkeeping as well as the eventual document anchor). The same
anchor survives an untick: :func:`remove_scope` tombstones the scope's
collection under ``config.retired_scope_collections`` so a later re-tick
re-adopts it (see :func:`_readopted_scope_collection_id`) instead of
minting a duplicate.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.public_url import public_base_url
from app.resource_types import ResourceType
from src.audit_helpers import log_safe
from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL, zone_rows
from connectors.sharepoint.graph_client import (
    SharePointGraphError,
    build_folder_matcher,
    certificate_metadata,
    get_app_token,
    get_item_by_path,
    get_site_by_path,
    list_drives,
    list_item_children,
    list_root_children,
    list_root_children_with_url,
    list_sites,
    probe_unique_permissions,
    search_document_count,
    search_folders,
)
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from connectors.sharepoint.site_split import format_group_name, pack_folders_into_groups
from src.repositories import (
    corpus_file_events_repo,
    corpus_files_repo,
    file_corpora_repo,
    resource_grants_repo,
    source_connections_repo,
    user_groups_repo,
)

logger = logging.getLogger(__name__)


def _require_sharepoint_enabled() -> None:
    """Router-level dependency: refuse the whole SharePoint admin surface
    with a typed ``409 feature_disabled`` when the ``sharepoint`` switch
    (``app/switches.py``) is off.

    ``409``, not ``404``: unlike the anonymous Graph webhook receiver
    (``app/api/sharepoint_webhooks.py``'s ``require_sharepoint_enabled`` in
    ``app/auth/access.py``, which 404s because Graph is an unauthenticated
    caller that never had a route to discover), every route here already
    requires admin — a reachable,
    authenticated caller being told a KNOWN feature is off is exactly what
    409 means elsewhere in this module (``extraction_disabled``,
    ``acl_sync_already_running``, ...).
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "feature_disabled",
                "message": "sharepoint.enabled is false — enable it in instance.yaml (or AGNES_SHAREPOINT_ENABLED) first.",
            },
        )


router = APIRouter(
    prefix="/api/admin/sharepoint",
    tags=["admin"],
    dependencies=[Depends(_require_sharepoint_enabled)],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AudienceClassIn(BaseModel):
    """One per-scope audience-class row (2026-08-30 plan, Task 8; spec
    §4.1-4.3) — ``name`` is the tag Slice 4b's ``claims.audience`` column
    will carry (same identifier pattern that column's ingest validation
    uses), ``group_ids`` is the set of Agnes groups whose membership defines
    "holds this class". Order carries no ordinal field of its own — position
    in ``ConfirmScopeBody.audience_classes``'s list IS the privilege rank."""

    name: str = Field(..., min_length=1, pattern=r"^[a-z0-9_-]{1,64}$")
    group_ids: List[str] = Field(default_factory=list)


class ConfirmScopeBody(BaseModel):
    source_scope_id: str = Field(..., min_length=1)
    display_path: str = Field(..., min_length=1)
    anonymize: bool = False
    # SharePoint ACL mirroring (2026-08-30 plan, Task 5). ``manual`` (the
    # default) is today's behavior unchanged; ``mirrored`` opts this scope
    # into the ``sharepoint-acl-sync`` job's per-scope group/grant
    # reconciliation (connectors/sharepoint/acl_sync.py). Always persisted
    # on confirm — like ``anonymize`` above, not "omitted means unchanged"
    # like ``group_ids`` below.
    access_mode: Literal["manual", "mirrored"] = "manual"
    # The Graph drive id for this scope root — REQUIRED when
    # ``access_mode='mirrored'`` (the ACL sync needs both a drive id and an
    # item id to read `.../permissions`; see connectors/sharepoint/
    # acl_sync.py's module docstring "Gap closed" note). Optional for
    # ``manual`` scopes, which never read it. ``None`` means "not supplied
    # on this call" — same always-overwritten-on-confirm semantics as
    # ``access_mode``/``anonymize``.
    drive_id: Optional[str] = None
    # Step 3: applied as ordinary `resource_grants` rows on the collection —
    # never stored on the scope row itself (see module docstring). ``None``
    # (the field omitted) and ``[]`` mean DIFFERENT things: omitted is "this
    # call is not about sharing" (step 2 confirming a scope, a rename, an
    # anonymize toggle), while an explicit empty list is the admin unticking
    # the last group — the very state the row's "indexed but invisible"
    # warning describes, so it has to be honoured rather than read as silence.
    group_ids: Optional[List[str]] = None
    # Broken-inheritance subtree sweep (2026-08-30 plan, Task 7 —
    # ``connectors/sharepoint/acl_sync.py::run_subtree_sweep``). ``should_not``
    # guarantee mode ONLY: "include anyway" a subtree the sweep detected as
    # broken-inheritance and (by default) excluded from the crawl — an
    # advisory override, audited (``sharepoint_acl.subtree_override``).
    # Under ``must_not`` (the fail-closed default) this is REFUSED with
    # ``409 must_not_forbids_subtree_override`` — see :func:`confirm_scope`.
    # Always persisted on confirm, same "not omitted-means-unchanged"
    # semantics as ``access_mode``/``anonymize`` above.
    include_excluded_subtrees: bool = False
    # Per-scope audience-class mapping (2026-08-30 plan, Task 8; spec
    # §4.1-4.3) — index-time claim variants (Slice 4b) select among these by
    # caller membership (Slice 3). ORDERED MOST-PRIVILEGED FIRST: the order
    # is the privilege ranking Slice 4b's projection dedup and the tiered-
    # collections document-text gate both rank by, not just presentation.
    # ``None`` (omitted) leaves the scope's existing mapping untouched;
    # ``[]`` explicitly clears it (back to non-tiered) — the SAME omitted-
    # vs-empty semantics as ``group_ids`` above, for the same reason: a step
    # 2/step 3 confirm that says nothing about audience tiers must not
    # silently wipe a configured mapping.
    audience_classes: Optional[List[AudienceClassIn]] = None


class ScopeCollectionRef(BaseModel):
    """The collection a scope maps to — same trio ``_scope_out`` projects."""

    id: str
    slug: str
    name: str


class ScopeRemovalOut(BaseModel):
    """What untick did to the scope's collection (see :func:`remove_scope`):
    ``collection_kept=True`` carries the kept collection's ref so the wizard
    can point the admin at the Library for the deliberate delete; ``False``
    means it was empty and tidied away (or already gone)."""

    collection_kept: bool
    collection: Optional[ScopeCollectionRef] = None


class AddManualSiteBody(BaseModel):
    """The URL an admin pasted into "Add a site by URL" (step 2) — the SAME
    input :func:`browse_tree`'s ``?site_url=`` already resolves, just carried
    in a POST body instead of a query param so this call can also persist the
    result (see :func:`add_manual_site`)."""

    site_url: str = Field(..., min_length=1)


class BulkScopeBody(BaseModel):
    """One shot: turn a list of admin-typed folder paths into confirmed
    scopes, without the wizard's own click-through-the-tree flow (see
    :func:`bulk_add_scopes`). ``drive_id`` is optional — omit it to reuse
    the drive of an existing scope on this SAME connection; a brand-new
    connection with no scopes yet (the split-a-big-site workflow's typical
    starting point, see :func:`clone_connection`) must supply it."""

    paths: List[str] = Field(..., min_length=1)
    drive_id: Optional[str] = None


class CloneConnectionBody(BaseModel):
    """The new sibling connection's own name — everything else (identity,
    credential references, site/host discovery bookkeeping) is copied from
    the source (see :func:`clone_connection`)."""

    name: str = Field(..., min_length=1)


class SplitApplyBody(BaseModel):
    """``POST …/splits`` — how many sibling connections to create and, for
    each, the per-connection extraction knobs a split usually wants set from
    the start (see :func:`apply_split`). ``n`` is capped at
    :data:`_SPLIT_MAX_N` — this endpoint fans out ``n`` Graph Search calls
    per top-level folder plus ``n`` connection creates; nothing here needs
    more than a few dozen even on a genuinely huge site."""

    n: int = Field(..., ge=1, le=50)
    #: Same admin-supplied ``YYYY-MM-DD`` filter as :class:`SplitPlanQuery`
    #: below — passed straight through onto each created connection's
    #: ``config.extraction.crawl.min_modified`` (see :func:`apply_split`'s
    #: docstring for why that key, not a new one, and why the crawl does not
    #: honor it yet).
    min_modified: Optional[str] = None
    transport: Optional[Literal["sync", "batch"]] = None
    retry_mode: Optional[str] = None
    #: Enqueue each clone's ``corpus-extraction`` job immediately after
    #: creating it (the same job ``POST …/{id}/extract`` enqueues), in
    #: creation order — no stagger, since the jobs queue itself already
    #: serializes worker pickup. Default ``False``: an admin who wants to
    #: review the split before it starts crawling gets exactly the clones,
    #: nothing running yet.
    start: bool = False


#: Config keys :func:`clone_connection` does NOT carry over into a clone —
#: deliberately a SMALLER set than :data:`SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS`
#: (which governs a DIFFERENT concern: what the generic connection editor
#: must preserve across an ordinary edit). ``scopes`` is the confirmed-scope
#: rows themselves — the whole point of a clone is to hold a DIFFERENT
#: subset of them. ``extraction`` is the extraction schedule's own
#: last-run/last-job dispatch bookkeeping — a clone has never run, so
#: carrying it over would misreport its due-ness to the very first sweep
#: that looks at it. Every OTHER server-written key (``manual_sites``,
#: ``webhook_secret``, ``retired_scope_collections``) carries over: under
#: ``Sites.Selected`` a bookmarked ``manual_sites`` entry is how the clone
#: resolves the site AT ALL (``/sites`` enumeration 403s), and the other two
#: are inert bookkeeping until the clone has scopes/subscriptions of its own.
_CLONE_EXCLUDED_CONFIG_KEYS = ("scopes", "extraction")

#: Upper bound on ``SplitApplyBody.n`` / the ``split-plan`` preview's ``n``
#: query param — see :class:`SplitApplyBody`'s own docstring for why.
_SPLIT_MAX_N = 50


def _validate_min_modified(value: Optional[str]) -> None:
    """An admin-supplied ``min_modified`` filter (``GET …/split-plan?
    min_modified=`` and ``SplitApplyBody.min_modified``) is the exact shape
    :func:`connectors.sharepoint.graph_client.search_document_count` splices
    into its KQL ``LastModifiedTime>=`` clause unescaped, so this is a
    structural gate, not cosmetic validation (same "never build a request
    from an unchecked value" rule as :func:`_validate_graph_id`) — and the
    SAME check ``PATCH …/extraction/crawl-config`` runs on the identical
    ``config.extraction.crawl.min_modified`` value
    (``app/api/admin_extraction.py``), so a caller sees one validation rule
    for this key regardless of which endpoint sets it: ``date.fromisoformat``
    (catches a structurally YYYY-MM-DD-shaped but calendar-invalid date, e.g.
    month 13, that a bare regex would let through), ``400
    invalid_min_modified`` on failure.
    """
    if value is None:
        return
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=400, detail={"error": "invalid_min_modified"}) from None


def _cloned_base_config(row: Dict[str, Any]) -> Dict[str, Any]:
    """The base config for a sibling connection wired to the same credential
    material as ``row`` — shared by :func:`clone_connection` and the
    site-split apply endpoint (:func:`apply_split`), which creates its own
    clones the same way but writes ``scopes``/``extraction`` itself
    (see :data:`_CLONE_EXCLUDED_CONFIG_KEYS`)."""
    return {k: v for k, v in (row.get("config") or {}).items() if k not in _CLONE_EXCLUDED_CONFIG_KEYS}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Keys THIS module writes into a SharePoint connection's ``config`` outside
#: the generic ``PUT /api/admin/source-connections/{id}`` editor's own
#: request body: the wizard's confirmed-scope rows (``scopes`` —
#: :func:`confirm_scope` / :func:`remove_scope`), the in-Agnes extraction
#: schedule's own dispatch bookkeeping (``extraction`` —
#: :func:`_record_extraction_dispatch`, TCRD-226), and the Graph
#: change-notification receiver's shared secret (``webhook_secret`` —
#: :func:`rotate_webhook_secret`).
#:
#: A key earns a place here on ONE test: the server writes it into this
#: connection's ``config`` and the generic connection-editor FORM never
#: renders it. ``app/api/admin_source_connections.py::update_connection``
#: imports this tuple to carry each key forward across an update that omits
#: it — that endpoint replaces ``config`` wholesale, so without this an
#: ordinary edit (a rename, a certificate change) silently erases whatever
#: isn't listed here.
#:
#: This is NOT optional bookkeeping — it is a ratcheted list.
#: ``tests/test_sharepoint_config_carry_forward_ratchet.py`` statically scans
#: THIS file for every literal key a local writer assigns into the variable
#: it then passes as ``config=`` to ``source_connections_repo().update(...)``
#: and fails if that set is not exactly this tuple — so adding a THIRD
#: server-written key here without adding it to this tuple in the SAME
#: change fails a test that names the fix, rather than shipping a silent
#: erasure the way ``scopes`` (2026-08-29 morning) and ``extraction``
#: (2026-08-29, same day, TCRD-226) both did before this ratchet existed.
#: ``webhook_secret`` is the third instance of this same class — added
#: here in the same change that introduces the writer, not after.
#: ``retired_scope_collections`` (2026-08-31) is the fourth: the
#: untick tombstones :func:`remove_scope` writes so :func:`confirm_scope`
#: can re-adopt a scope's previous collection on re-tick instead of minting
#: a duplicate. ``manual_sites`` (2026-09-01) is the fifth: the sites an
#: admin added by URL under the ``Sites.Selected`` escape hatch
#: (:func:`add_manual_site` / :func:`remove_manual_site`) — without this the
#: wizard forgot every one of them the moment the connection was next edited
#: through the generic form, forcing a re-paste of the same URL.
SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS = (
    "scopes",
    "extraction",
    "webhook_secret",
    "retired_scope_collections",
    "manual_sites",
)


def _sharepoint_connection_or_404(connection_id: str) -> Dict[str, Any]:
    row = source_connections_repo().get(connection_id)
    if row is None or row.get("source_type") != "sharepoint":
        raise HTTPException(status_code=404, detail="connection_not_found")
    return row


# Graph drive/site/item ids observed in practice are base64url-ish
# (letters, digits, `-`/`_`) with an occasional `!`, `.`, `,` or `:` (site
# ids compose a hostname, a GUID and a GUID with commas; some drive ids use
# `!`). Never a `/` — the one character that would let a value escape its
# own URL path segment.
_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9!_.,:=-]+$")


def _validate_graph_id(value: str, field: str) -> None:
    """Structural validation for an id headed straight into a Graph URL path
    segment (``item_id``, and the search endpoint's ``drive_id``) — never
    build the request path from an unchecked value (security playbook:
    "validate ... paths built from untrusted names"). A typed 422, not a
    500 from a Graph call that silently misrouted."""
    if not value or not _GRAPH_ID_RE.match(value):
        raise HTTPException(status_code=422, detail={"error": f"invalid_{field}", "message": f"malformed {field}"})


_SITE_URL_MAX_LEN = 2048
#: RFC-1123-ish: lowercase letters/digits/dots/hyphens, must start with an
#: alphanumeric and contain a dot. Deliberately NOT pinned to
#: ``*.sharepoint.com`` — sovereign clouds (`.sharepoint.us`, `.sharepoint.cn`,
#: …) and vanity domains are legitimate hosts; the Graph base URL is a
#: constant, so a wrong host can only make Graph itself answer 400/404.
_SITE_HOSTNAME_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


def _invalid_site_url(reason: str) -> None:
    raise HTTPException(status_code=422, detail={"error": "invalid_site_url", "message": f"invalid site_url: {reason}"})


def _parse_site_url(raw: str) -> Tuple[str, str]:
    """An admin-pasted SharePoint URL -> ``(hostname, server_relative_path)``
    for Graph's by-path site addressing.

    Admins paste whatever their browser shows, so a URL deep inside the site
    (a library page, a document) is trimmed to the site itself when the path
    starts with a site-collection managed path (``/sites/…``, ``/teams/…``
    -> first two segments). Any other path shape is kept as given — the
    tenant root site (empty path) included.

    This is request validation, not transport safety: every segment is
    ALSO percent-encoded at the Graph client (`get_site_by_path`), so the
    typed 422s here exist to tell the admin what to fix, not to be the only
    thing standing between a pasted string and the Graph URL. Rejections are
    structural (scheme, userinfo/port smuggling, dot-dot or control-character
    segments), never a guess at which tenants are plausible.
    """
    value = (raw or "").strip()
    if not value:
        _invalid_site_url("empty")
    if len(value) > _SITE_URL_MAX_LEN:
        _invalid_site_url("too long")
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme != "https":
        _invalid_site_url("only https:// URLs are accepted")
    host = parts.netloc.lower()
    if "@" in host or ":" in host:
        _invalid_site_url("hostname must not carry credentials or a port")
    if "." not in host or not _SITE_HOSTNAME_RE.match(host):
        _invalid_site_url("malformed hostname")
    segments = [unquote(seg) for seg in parts.path.split("/") if seg]
    for seg in segments:
        if seg in (".", "..") or "\\" in seg or any(ord(ch) < 32 for ch in seg):
            _invalid_site_url("malformed path segment")
    if segments and segments[0].lower() in ("sites", "teams"):
        if len(segments) < 2:
            _invalid_site_url("the URL names a managed path but no site")
        segments = segments[:2]
    return host, "/".join(segments)


#: A single browse click never fans out into more Graph calls than this many
#: folders' worth of unique-permissions probing — Graph's own children page
#: can be far larger than a sane one-click batch fan-out (the real library
#: this cap was sized against has 97,899 folders total; a single folder's
#: OWN children rarely approach that, but nothing bounds it upstream).
#: Folders beyond the cap are left unprobed (``unique_permissions: None`` —
#: "unknown", same as any other probe failure), never silently dropped from
#: the listing itself.
_MAX_PERMISSION_PROBE_ITEMS = 200


async def _annotate_unique_permissions(token: str, drive_id: str, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Attach a best-effort ``unique_permissions: true|false|null`` to every
    FOLDER item (files are never scoped by the wizard, so they are never
    probed and never carry the key) — ADVISORY ONLY (Decision #2, module
    docstring: Agnes never derives or enforces anything from a SharePoint
    ACL). Bounded by :data:`_MAX_PERMISSION_PROBE_ITEMS`; on any probe
    failure (caught defensively here too, though
    :func:`connectors.sharepoint.graph_client.probe_unique_permissions`
    already never raises) every folder degrades to ``null`` rather than
    failing the browse — the advisory signal must never be able to make an
    otherwise-successful tree fetch fail.
    """
    folder_ids = [item["id"] for item in items if item.get("is_folder")][:_MAX_PERMISSION_PROBE_ITEMS]
    if not folder_ids:
        return items
    try:
        flags = await probe_unique_permissions(token, drive_id, folder_ids)
    except Exception:  # noqa: BLE001 — advisory probe must never fail the browse
        logger.warning("sharepoint unique-permissions probe raised; degrading to unknown", exc_info=True)
        flags = {}
    return [{**item, "unique_permissions": flags.get(item["id"])} if item.get("is_folder") else item for item in items]


def _scopes(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    scopes = (row.get("config") or {}).get("scopes")
    return list(scopes) if isinstance(scopes, list) else []


def _inferred_drive_id(row: Dict[str, Any], drive_id: Optional[str]) -> str:
    """Same fallback :func:`bulk_add_scopes` uses: an explicit ``drive_id``
    wins, else reuse the first existing scope's — the site-split planner
    reads an already-connected site's drive root, so a connection with at
    least one confirmed scope is the expected starting point. ``400
    drive_id_required`` (never a 500 from a Graph call with no drive to
    address) when neither is available."""
    if drive_id:
        _validate_graph_id(drive_id, "drive_id")
        return drive_id
    inferred = next((s.get("drive_id") for s in _scopes(row) if s.get("drive_id")), None)
    if not inferred:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "drive_id_required",
                "message": (
                    "drive_id was not supplied and this connection has no existing scope to infer "
                    "one from — pass drive_id explicitly (GET …/tree finds one)."
                ),
            },
        )
    return inferred


def _manual_sites(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    sites = (row.get("config") or {}).get("manual_sites")
    return list(sites) if isinstance(sites, list) else []


async def _resolved_token(row: Dict[str, Any]) -> str:
    """Resolve this connection's certificate and exchange it for a Graph
    access token — a single typed-error seam so the tree endpoint's 409/502
    split (cert missing vs. Graph/Entra rejected it) lives in exactly one
    place."""
    try:
        settings = resolve_sharepoint_settings(row)
    except SharePointSettingsError as exc:
        # Surface absence rather than fail the crawl silently (spec §13.2):
        # a typed 409 the wizard renders as "certificate not configured yet",
        # never a bare 500.
        raise HTTPException(
            status_code=409,
            detail={"error": "sharepoint_cert_unresolved", "message": str(exc)},
        ) from exc
    try:
        return await get_app_token(
            settings.tenant_id, settings.client_id, settings.private_key, client_secret=settings.client_secret
        )
    except SharePointGraphError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc


def _group_ids_for_collection(collection_id: str) -> List[str]:
    grants = resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
    return [g["group_id"] for g in grants if g.get("resource_id") == collection_id]


def _latest_run_anonymized_corpus_ids() -> set:
    """Which collection ids the LATEST persisted ``facts_ingest_runs`` row
    declares it anonymized (spec §9.2 — the producer's own declaration, see
    ``app/api/facts.py``'s ``FactsIngestAnonymizationReport``). This is what
    turns "requested" (the wizard's checkbox, below) into "anonymized" —
    never rendering the latter from the checkbox alone (spec §13.2: "on a
    collection detail it is a state plus a named batch task, never a
    toggle").

    Best-effort: ``facts_ingest_runs_repo()`` is PG-only (A3 ratchet) and
    may not exist yet on a DuckDB-backed instance, or there may be no runs
    yet — either degrades to "nothing declared yet", never a 500 on the
    wizard's own scope listing (a badge that cannot prove itself should
    read as unproven, not crash the page that shows it).
    """
    try:
        from src.repositories import facts_ingest_runs_repo

        runs = facts_ingest_runs_repo().list_recent(limit=1)
    except Exception:
        return set()
    if not runs:
        return set()
    anonymization = runs[0].get("anonymization") or {}
    scopes = anonymization.get("scopes")
    return set(scopes.keys()) if isinstance(scopes, dict) else set()


def _acl_sync_summary(connection: Optional[Dict[str, Any]], scope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The scope row's slice of the connection's last ``sharepoint-acl-sync``
    run (``connectors/sharepoint/acl_sync.py::_sync_connection`` writes the
    full block into ``config["acl_sync_last_run"]``, aggregated across every
    mirrored scope on the connection) — ``None`` when no connection was
    given or no run has completed yet, so ``_scope_out`` can omit the key
    entirely rather than emit a block of nulls."""
    if not connection:
        return None
    last_run = (connection.get("config") or {}).get("acl_sync_last_run")
    if not isinstance(last_run, dict):
        return None
    stale_scopes = last_run.get("stale_scopes") or []
    return {
        "at": last_run.get("at"),
        "ok": last_run.get("ok"),
        "matched": last_run.get("matched"),
        "unmatched": last_run.get("unmatched"),
        "stale": scope.get("source_scope_id") in stale_scopes,
    }


def _scope_audience_classes_out(scope: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Projects a scope row's stored ``audience_classes`` — ``[{"name":
    str, "group_ids": [str]}]`` in the wizard-persisted privilege order
    (most-privileged first). A pre-Task-8 row (or one whose mapping was
    explicitly cleared with ``[]``) has no key or an empty list; either
    reads as ``[]`` here, never a null. This is the WIZARD's read shape only
    — the runtime read path Tasks 9-11 consume is
    ``src.audience_classes.audience_class_map()``, which scans the same
    stored field but is keyed by collection id across every connection."""
    raw = scope.get("audience_classes")
    if not isinstance(raw, list):
        return []
    return [
        {"name": cls.get("name"), "group_ids": list(cls.get("group_ids") or [])}
        for cls in raw
        if isinstance(cls, dict) and cls.get("name")
    ]


def _scope_out(
    scope: Dict[str, Any],
    declared_corpus_ids: Optional[set] = None,
    connection: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    collection = file_corpora_repo().get(scope.get("collection_id") or "")
    group_ids = _group_ids_for_collection(scope.get("collection_id") or "")
    if declared_corpus_ids is None:
        declared_corpus_ids = _latest_run_anonymized_corpus_ids()
    anonymize = bool(scope.get("anonymize"))
    out = {
        "source_scope_id": scope.get("source_scope_id"),
        "display_path": scope.get("display_path"),
        "anonymize": anonymize,
        # The checkbox above only RECORDS an admin's intent — this field is
        # the honest other half: whether the producer's LAST ingest run
        # actually declared this collection anonymized. `anonymize=true,
        # anonymization_declared=false` is "anonymization requested"; both
        # true is "anonymized". Never collapse the two (see module docstring
        # note + docs/anonymization.md's badge semantics).
        "anonymization_declared": anonymize and (scope.get("collection_id") in declared_corpus_ids),
        "collection_id": scope.get("collection_id"),
        "collection": (
            {"id": collection["id"], "slug": collection["slug"], "name": collection["name"]} if collection else None
        ),
        "group_ids": group_ids,
        # Spec §13.2: "warn on any collection leaving with no group ('indexed
        # but invisible' is the worst silent state)" — the wizard's step-3
        # preview reads this per row rather than re-deriving it client-side.
        "no_group_warning": no_group_warning(group_ids),
        # SharePoint ACL mirroring (2026-08-30 plan, Task 5) — `manual`
        # (today's behavior) unless the admin opted this scope into
        # `sharepoint-acl-sync`'s reconciliation; `None` (a pre-Task-5 row)
        # reads as `manual` too, never a bare null.
        "access_mode": scope.get("access_mode") or "manual",
        "drive_id": scope.get("drive_id"),
        # Broken-inheritance subtree sweep (2026-08-30 plan, Task 7) — the
        # advisory surface's data: how many subtrees the sweep excluded from
        # the crawl, each as {item_id, path, rel_path, kind} (never the raw
        # {detected_at} timestamp — the wizard doesn't need it), plus
        # whether an admin has overridden the exclusion for this scope
        # (`should_not` mode only). `excluded_file_count` (2026-08-31 plan,
        # Task 8) is the `kind == "file"` slice of the same list — sweep v2
        # (Task 3) now probes files as well as folders, and a scope with a
        # lot of excluded folders but a handful of excluded files (or vice
        # versa) reads very differently to an admin deciding whether to
        # override. A legacy entry with no `kind` (written before sweep v2)
        # reads as a folder — never counted here, same "treat-missing-as-
        # legacy" rule the sweep's own matching uses.
        "excluded_subtree_count": len(scope.get("excluded_subtrees") or []),
        "excluded_file_count": sum(
            1
            for item in (scope.get("excluded_subtrees") or [])
            if isinstance(item, dict) and item.get("kind") == "file"
        ),
        "excluded_subtrees": [
            {
                "item_id": item.get("item_id"),
                "path": item.get("path"),
                "rel_path": item.get("rel_path"),
                "kind": item.get("kind") or "folder",
            }
            for item in (scope.get("excluded_subtrees") or [])
            if isinstance(item, dict)
        ],
        "include_excluded_subtrees": bool(scope.get("include_excluded_subtrees")),
    }
    audience_classes = _scope_audience_classes_out(scope)
    out["audience_classes"] = audience_classes
    # Non-empty audience_classes IS tiered (2026-08-30 plan, Task 8; spec
    # §4.1-4.3) — Slice 4b's predicate and Slice 4c's document-text gate
    # both key off this flag via src.audience_classes.tiered_collection_ids.
    out["tiered"] = bool(audience_classes)
    summary = _acl_sync_summary(connection, scope)
    if summary is not None:
        out["acl_sync_last_run"] = summary
    return out


def _zone_out(zone: Dict[str, Any]) -> Dict[str, Any]:
    """Wizard-facing projection of one ``config["acl_zones"]`` row
    (2026-08-31 plan, Task 3/8 — see ``connectors/sharepoint/acl_sync.py::
    zone_rows``) — id/route/status bookkeeping only, never the ``rel_path``/
    ``drive_id``/``parent_scope_id`` internals the sweep and sync need but an
    admin reading the connection detail does not."""
    return {
        "zone_item_id": zone.get("zone_item_id"),
        "display_path": zone.get("display_path"),
        "collection_id": zone.get("collection_id"),
        "status": zone.get("status"),
        "detected_at": zone.get("detected_at"),
    }


def no_group_warning(group_ids: List[str]) -> bool:
    """A collection with no granted group is indexed but invisible — the
    worst silent state (spec §13.2). Exposed as a standalone function so the
    rule is independently testable, not just observable through the API."""
    return len(group_ids) == 0


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80].strip("-") or "sharepoint"


def _create_scope_collection(*, connection_name: str, display_path: str, source_scope_id: str, created_by: str) -> str:
    """Create the collection a freshly confirmed scope maps to.

    Slug collisions (two different scopes whose display paths normalize to
    the same slug, e.g. two "Contracts" folders under different sites) are
    resolved by appending a short, stable suffix derived from the scope's
    own id — deterministic, so a caller who retries after a collision sees
    the same slug both times, and no path/name text ever needs parsing to
    reproduce it.
    """
    repo = file_corpora_repo()
    base_name = display_path.strip() or source_scope_id
    slug = _slugify(f"{connection_name}-{base_name}")
    name = f"{connection_name}: {base_name}"
    description = f"SharePoint scope · {display_path}"
    try:
        return repo.create(name=name, slug=slug, description=description, created_by=created_by)
    except Exception as exc:  # noqa: BLE001 — DuckDB ConstraintException / PG IntegrityError, message-sniffed elsewhere too
        err = str(exc).lower()
        if "unique" not in err and "duplicate" not in err and "constraint" not in err:
            raise
        suffix = hashlib.sha256(source_scope_id.encode()).hexdigest()[:8]
        return repo.create(name=name, slug=f"{slug}-{suffix}", description=description, created_by=created_by)


def _readopted_scope_collection_id(tombstone: Any) -> Optional[str]:
    """The collection a re-ticked scope should re-adopt, or ``None`` to mint.

    ``tombstone`` is :func:`remove_scope`'s ``retired_scope_collections``
    entry for this scope — ``{"collection_id": ..., "auto_deleted": bool}``,
    the provenance that keys re-adoption on ``(connection, source_scope_id)``
    rather than on slug/name text (both derive from the mutable
    ``display_path``, so matching on them would fork on any rename).

    Two branches, deliberately asymmetric:

    * ``auto_deleted=True`` — :func:`remove_scope` itself soft-deleted the
      empty collection on untick, so re-tick RESTORES it: nothing but the
      wizard ever touched it, and its slug still holds the UNIQUE slot any
      replacement would collide with (``_create_scope_collection``'s
      deterministic suffix absorbs only one collision, so mint-instead
      500s by the second untick/re-tick cycle).
    * ``auto_deleted=False`` — the collection was KEPT (it had files); it is
      re-adopted only while still live. An admin's deliberate Library delete
      in between is respected — a background flow never resurrects it.
    """
    if not isinstance(tombstone, dict):
        return None
    collection_id = tombstone.get("collection_id")
    if not collection_id:
        return None
    repo = file_corpora_repo()
    if tombstone.get("auto_deleted"):
        husk = repo.get(collection_id, include_deleted=True)
        if husk is None:
            return None
        if husk.get("deleted_at") is not None:
            repo.restore(collection_id)
        return collection_id
    return collection_id if repo.get(collection_id) is not None else None


def _extraction_readiness() -> Tuple[bool, Optional[Dict[str, str]]]:
    """Whether the ``corpus-extraction`` job kind can actually run right now,
    read here so an admin (or the scheduled sweep below) finds out BEFORE a
    job is queued rather than 30 minutes later when a worker claims it and
    the handler raises.

    Two gates. ``sharepoint.enabled`` is the one
    ``app/worker/kinds.py::_run_corpus_extraction`` itself checks. The second
    is the ``extraction`` optional dependency extra: since the built-in
    pipeline became the only pipeline (owner decision 2026-08-31) the crawl
    runs IN-PROCESS, so on a server without the converter backends installed
    every document of a run would fail with the same
    ``MissingConversionDependency``. ``connectors.sharepoint.convert`` is
    deliberately importable WITHOUT the extra (its backends are imported
    lazily), so the probe has to reach past it to the backends themselves.

    The first gate already honors a deploy-time env override ahead of
    ``instance.yaml`` — ``AGNES_SHAREPOINT_ENABLED`` (via ``feature_enabled``
    below) — so this function and the handler it mirrors see the identical
    truth regardless of which source (env or yaml) an instance configures
    through.

    Returns ``(True, None)`` when usable, or ``(False, {"error": ...,
    "message": ...})`` naming the exact fix.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("sharepoint", "enabled", env_var="AGNES_SHAREPOINT_ENABLED", default=False):
        return False, {
            "error": "extraction_disabled",
            "message": "sharepoint.enabled is false — enable it in instance.yaml (or AGNES_SHAREPOINT_ENABLED) first.",
        }

    try:
        import markitdown  # noqa: F401
        import pypdfium2  # noqa: F401

        import connectors.sharepoint.convert  # noqa: F401
    except ImportError:
        return False, {
            "error": "extraction_dependencies_missing",
            "message": (
                "The document converter is not installed — run "
                "pip install 'agnes[extraction]' on the process that runs the extraction lane."
            ),
        }

    return True, None


def _extraction_idempotency_key(connection_id: str) -> str:
    """A STABLE per-connection idempotency key, shared by the manual
    trigger and the scheduled sweep below — ``JobsRepository.enqueue``
    dedups on it while a matching job is still ``queued``/``running``, so a
    manual trigger and a scheduled run for the same connection can never
    both be in flight, and either path's 409/backlog handling reads the
    SAME existing job."""
    return f"corpus-extraction:{connection_id}"


def _record_extraction_dispatch(row: Dict[str, Any], job_id: str) -> None:
    """Persist this connection's own extraction dispatch bookkeeping —
    ``last_run_at`` (the scheduled sweep's due-check input, see
    :func:`_dispatch_extraction_if_due`) and ``last_job_id`` — into the
    connection's own ``config.extraction`` sub-object. No new table: same
    pattern the connect wizard's ``config.scopes`` already uses on this
    same JSON column. Called by BOTH the manual trigger and the sweep, so
    either path resets the "next due" clock — a manual run moments before
    the schedule would fire must not also queue a second run a tick later.
    """
    config = dict(row.get("config") or {})
    # Writing a NEW key here (or in any other function in this module)?
    # Add it to SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS above IN THE SAME
    # CHANGE — the generic connection editor's carry-forward
    # (app/api/admin_source_connections.py::update_connection) only
    # preserves keys listed there, and the ratchet test
    # (tests/test_sharepoint_config_carry_forward_ratchet.py) will fail
    # otherwise.
    config["extraction"] = {
        "last_run_at": datetime.now(timezone.utc).isoformat(),
        "last_job_id": job_id,
    }
    source_connections_repo().update(row["id"], config=config)


def _extraction_schedule_config() -> Optional[str]:
    """The single instance-wide extraction cadence (``extraction.schedule``
    in ``instance.yaml``'s ``extraction:`` block) — applied independently to
    each SharePoint connection's own ``last_run_at`` by
    :func:`_dispatch_extraction_if_due`. Off by default: absent/empty means
    no scheduled sweep (mirrors ``sharepoint.enabled``'s own default)."""
    from app.instance_config import get_value

    raw = get_value("extraction", "schedule", default="")
    raw = str(raw or "").strip()
    return raw or None


def _dispatch_extraction_if_due(row: Dict[str, Any], schedule: str, now: datetime) -> bool:
    """Evaluate one SharePoint connection against the extraction cadence
    and, if due, enqueue ``corpus-extraction`` for it.

    Due-ness reuses :func:`src.scheduler.is_table_due` against THIS
    connection's own ``config.extraction.last_run_at`` — the same primitive
    every other cadence in this codebase is evaluated with (no second
    scheduling mechanism). Returns ``True`` iff this call actually
    consumed the tick (a fresh enqueue OR a dedup against an already
    in-flight job for this connection — the "backlog" case, mirroring
    ``app/api/agent_schedules.py::_dispatch_if_due``'s same-shape guard so
    a stuck previous run doesn't get re-logged every tick); ``False`` when
    not due yet.
    """
    from src.scheduler import is_table_due

    extraction_state = (row.get("config") or {}).get("extraction") or {}
    last_run_at = extraction_state.get("last_run_at")
    if not is_table_due(schedule, last_run_at, now=now):
        return False

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue(
        "corpus-extraction",
        {"connection_id": row["id"]},
        idempotency_key=_extraction_idempotency_key(row["id"]),
    )
    if job["deduped"]:
        logger.info(
            "extraction:run-due — connection %s already has a corpus-extraction job in flight (%s); "
            "consuming this tick without a second enqueue",
            row["id"],
            job["id"],
        )
    _record_extraction_dispatch(row, job["id"])
    return True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/connections/{connection_id}/tree")
async def browse_tree(
    connection_id: str,
    site_id: Optional[str] = None,
    drive_id: Optional[str] = None,
    item_id: Optional[str] = None,
    site_url: Optional[str] = None,
    with_permissions: bool = False,
    _user: dict = Depends(require_admin),
):
    """One level of the live SharePoint folder tree.

    No ``site_id`` -> the reachable sites. ``site_id`` alone -> that site's
    document libraries (drives). ``drive_id`` -> the drive's root children.
    ``drive_id`` + ``item_id`` -> that folder's own children (TCRD-240:
    subfolder browsing at any depth — ``item_id`` is the previous call's own
    item id, never a path, and is structurally validated before it reaches
    a Graph URL).

    ``site_url`` (exclusive with all three coordinates above) resolves ONE
    site addressed directly by a pasted URL and returns it as a one-item
    ``sites`` level. This is the ``Sites.Selected`` escape hatch: that
    permission 403-forbids every enumeration Graph offers, so the sites
    level above is unreachable for such an app registration, while a granted
    site it can NAME stays readable (Graph by-path addressing). A deep URL
    (a library page, a document) is trimmed to its site when it starts with
    a site-collection managed path — see :func:`_parse_site_url`.

    ``with_permissions=1`` (default off) additionally probes each listed
    FOLDER for ``hasUniqueRoleAssignments`` — an ADVISORY-ONLY signal
    (Decision #2: Agnes never derives or enforces anything from a
    SharePoint ACL; see :func:`connectors.sharepoint.graph_client.
    probe_unique_permissions`) — and adds ``unique_permissions:
    true|false|null`` to each folder item. Off by default so plain browsing
    never pays for it; batched and bounded
    (:data:`_MAX_PERMISSION_PROBE_ITEMS`) when on, and a probe failure never
    fails the browse itself — affected folders just come back ``null``
    ("unknown").
    """
    if site_url is not None and (site_id or drive_id or item_id):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "site_url_exclusive",
                "message": "site_url resolves a site on its own — it cannot be combined with site_id/drive_id/item_id",
            },
        )
    site_by_url: Optional[Tuple[str, str]] = None
    if site_url is not None:
        site_by_url = _parse_site_url(site_url)  # typed 422 before any token resolution
    if item_id is not None:
        if not drive_id:
            raise HTTPException(status_code=422, detail={"error": "item_id_requires_drive_id"})
        _validate_graph_id(item_id, "item_id")
    row = _sharepoint_connection_or_404(connection_id)
    token = await _resolved_token(row)
    try:
        if site_by_url is not None:
            hostname, site_path = site_by_url
            site = await get_site_by_path(token, hostname, site_path)
            return {"level": "sites", "items": [site]}
        if drive_id:
            items = await (
                list_item_children(token, drive_id, item_id) if item_id else list_root_children(token, drive_id)
            )
            if with_permissions:
                items = await _annotate_unique_permissions(token, drive_id, items)
            return {"level": "items", "site_id": site_id, "drive_id": drive_id, "item_id": item_id, "items": items}
        if site_id:
            items = await list_drives(token, site_id)
            return {"level": "drives", "site_id": site_id, "items": items}
        items = await list_sites(token)
        return {"level": "sites", "items": items}
    except SharePointGraphError as exc:
        # A Graph 403 is a PERMISSION verdict on an issued token, never an
        # outage — classify the two cases this endpoint can make actionable
        # instead of letting them read as "SharePoint did not answer" (the
        # generic wrap below, which one real Sites.Selected tenant surfaced
        # for a working certificate).
        if exc.status_code == 403 and site_by_url is not None:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "sharepoint_site_not_granted",
                    "message": (
                        "Graph refused this site (HTTP 403): the app registration has no grant on it. "
                        "Grant the app access to this site (Sites.Selected), or check the URL."
                    ),
                },
            ) from exc
        if exc.status_code == 403 and not site_id and not drive_id:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "sharepoint_discovery_forbidden",
                    "message": (
                        "Graph refused to list sites (HTTP 403). An app registration holding only "
                        "Sites.Selected cannot enumerate sites — add a granted site directly by its URL instead."
                    ),
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc


@router.post("/connections/{connection_id}/manual-sites", status_code=201)
async def add_manual_site(
    connection_id: str,
    body: AddManualSiteBody,
    _user: dict = Depends(require_admin),
):
    """Resolve a site by URL AND persist it on the connection (2026-09-01 bug
    report): ``GET .../tree?site_url=`` (the ``Sites.Selected`` escape hatch
    documented on :func:`browse_tree`) only ever RESOLVED a site — the result
    lived in the wizard's own client-side ``spManualSites`` and was reset
    every time the wizard opened, forcing the admin to re-paste the same URL
    on every visit. This route reuses the exact same validation
    (:func:`_parse_site_url`) and resolution (``get_site_by_path``) as that
    query param, then stores the result on ``config.manual_sites`` — a plain
    list of ``{id, name, web_url}`` rows, the same normalized shape
    ``get_site_by_path`` already returns — so a reopen (or a page reload) can
    read it straight back from the connection listing instead of losing it.

    Idempotent on the resolved site id: adding the same site twice (the same
    URL, or two URLs that resolve to the same site) replaces its row in
    place rather than appending a duplicate — the same "storage anchor is
    the id, not what the admin typed" principle the confirmed-scope
    collections use (module docstring).
    """
    row = _sharepoint_connection_or_404(connection_id)
    hostname, site_path = _parse_site_url(body.site_url)  # typed 422 before any token resolution
    token = await _resolved_token(row)
    try:
        site = await get_site_by_path(token, hostname, site_path)
    except SharePointGraphError as exc:
        # Same classification as `browse_tree`'s own `?site_url=` branch —
        # a Graph 403 here is a permission verdict on a NAMED site, never an
        # outage.
        if exc.status_code == 403:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "sharepoint_site_not_granted",
                    "message": (
                        "Graph refused this site (HTTP 403): the app registration has no grant on it. "
                        "Grant the app access to this site (Sites.Selected), or check the URL."
                    ),
                },
            ) from exc
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc

    manual_sites = [s for s in _manual_sites(row) if s.get("id") != site["id"]]
    manual_sites.append(site)
    # See SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS's docstring above if you are
    # adding a new key here rather than editing this one.
    new_config = {**(row.get("config") or {}), "manual_sites": manual_sites}
    source_connections_repo().update(connection_id, config=new_config)
    return site


@router.delete("/connections/{connection_id}/manual-sites", status_code=204)
async def remove_manual_site(
    connection_id: str,
    site_id: str,
    _user: dict = Depends(require_admin),
):
    """Forget one site previously added by URL (see :func:`add_manual_site`).
    Purely local bookkeeping — unlike a confirmed scope, a manual site owns
    no collection and no grants, so there is nothing else to reconcile."""
    row = _sharepoint_connection_or_404(connection_id)
    manual_sites = _manual_sites(row)
    remaining = [s for s in manual_sites if s.get("id") != site_id]
    if len(remaining) == len(manual_sites):
        raise HTTPException(status_code=404, detail="manual_site_not_found")
    # See SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS's docstring above if you are
    # adding a new key here rather than editing this one.
    new_config = {**(row.get("config") or {}), "manual_sites": remaining}
    source_connections_repo().update(connection_id, config=new_config)


# Real library scale (measured, 2026-08-29): 443k files / 97,899 folders in
# ONE library. The previous caps (depth 10 / visited 2000) made a
# whole-library search almost always `truncated` right at the top — safe,
# but nearly useless for the scale this wizard actually needs to serve.
#
# New caps, still conservative against Graph throttling: each visited
# folder costs exactly one Graph "list children" call, and
# `search_folders`' BFS walks them ONE AT A TIME — a single `await` in one
# loop, never fanned out concurrently — so a 20000-visited walk is 20000
# *sequential* Graph calls. That is self-throttling by construction (one
# admin's one search can only ever have one children-call in flight), unlike
# a concurrent fan-out that could burn a shared per-app-per-tenant Graph
# budget in one burst. 12 is a small bump on the previous depth cap of 10 —
# enough headroom for one more level without materially changing the walk's
# shape or its worst-case call count (`sum` of a wide tree's per-level
# fan-out, not a multiplicative blowup from depth alone).
#
# These remain CAPS, not defaults for automation: a routine narrow search
# still returns fast and well under the ceiling; only a genuinely
# whole-library, unscoped walk approaches it, and even then may still
# truncate at 97,899 real folders — the honest answer is to scope the
# search (§ the `hint` field below), not to raise the cap without limit.
_SEARCH_MAX_DEPTH_CAP = 12
_SEARCH_MAX_VISITED_CAP = 20000

#: The UI's truncation banner reads this verbatim (`spw-search-truncated` in
#: admin_data_sources.html) rather than composing its own guess at what an
#: admin should do next — one wording, defined once.
_SEARCH_TRUNCATED_HINT = "Scope the search to a site or folder, or narrow the pattern."


@router.get("/connections/{connection_id}/tree/search")
async def search_tree(
    connection_id: str,
    q: str = Query(..., min_length=2),
    mode: Literal["prefix", "contains", "glob"] = "prefix",
    drive_id: Optional[str] = None,
    item_id: Optional[str] = None,
    max_depth: int = 5,
    max_visited: int = 2000,
    _user: dict = Depends(require_admin),
):
    """Bounded breadth-first folder search (TCRD-240) — the server-side
    stand-in for Graph's own ``/search``, which silently under-returns
    under app-only auth (``connectors.sharepoint.graph_client`` module
    docstring). Never a single call: it walks ``.../root/children`` and
    ``.../items/{id}/children`` the same way the tree browser does, capped
    by ``max_depth``/``max_visited`` — CLAMPED to their caps
    (:data:`_SEARCH_MAX_DEPTH_CAP` / :data:`_SEARCH_MAX_VISITED_CAP`) rather
    than rejected, so asking for more than the server allows still returns
    the best bounded answer instead of a 422.

    Root: ``drive_id`` + ``item_id`` scopes to that folder's subtree;
    ``drive_id`` alone scopes to the whole drive; neither given searches
    every drive of every reachable site. ``item_id`` without ``drive_id``
    is rejected — there is no drive to resolve it against.

    A site or folder the app registration cannot read (Graph 403/404) is
    skipped, not fatal — app-only permissions are never uniform across a
    real tenant. It is never silently dropped: see ``skipped`` below.

    Response: ``{matches: [{item_id, drive_id, display_path}], visited,
    truncated, skipped, hint}``.

    - ``truncated`` is ``True`` whenever a cap (``max_depth``/``max_visited``)
      is what stopped the walk — never a silently partial result.
    - ``visited`` is how many "list children" calls it took to get there.
    - ``skipped`` lists every site/folder the walk could not enter for
      permissions reasons, each as ``{scope: "site"|"folder", reason:
      "forbidden"|"not_found", status_code, site_id, site_name, drive_id,
      item_id, display_path}`` — deliberately separate from ``truncated``:
      a cap and a permission refusal are different facts and call for
      different admin actions (narrow the search vs. request access).
    - ``hint`` is a short, actionable string (scope the search, narrow the
      pattern) when ``truncated`` is ``True``, else ``null`` — unrelated to
      ``skipped``, which speaks for itself.
    """
    if item_id and not drive_id:
        raise HTTPException(status_code=422, detail={"error": "item_id_requires_drive_id"})
    if drive_id:
        _validate_graph_id(drive_id, "drive_id")
    if item_id:
        _validate_graph_id(item_id, "item_id")

    row = _sharepoint_connection_or_404(connection_id)
    try:
        matcher = build_folder_matcher(q, mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_search_pattern", "message": str(exc)}) from exc

    token = await _resolved_token(row)
    clamped_depth = max(1, min(max_depth, _SEARCH_MAX_DEPTH_CAP))
    clamped_visited = max(1, min(max_visited, _SEARCH_MAX_VISITED_CAP))
    try:
        result = await search_folders(
            token,
            matcher=matcher,
            drive_id=drive_id,
            item_id=item_id,
            max_depth=clamped_depth,
            max_visited=clamped_visited,
        )
        result["hint"] = _SEARCH_TRUNCATED_HINT if result.get("truncated") else None
        return result
    except SharePointGraphError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc


@router.get("/connections/{connection_id}/scopes")
async def list_scopes(
    connection_id: str,
    user: dict = Depends(require_admin),
):
    """The wizard's step-2/3 source of truth: every confirmed scope row,
    enriched with its collection and current group grants, plus this
    connection's permission zones (2026-08-31 plan, Task 3/8 — ``"zones"``,
    ACTIVE and DISSOLVED alike so the wizard can show a zone's history
    rather than have it vanish the moment it dissolves; see :func:`_zone_out`
    for the exact projection).

    """
    row = _sharepoint_connection_or_404(connection_id)
    declared = _latest_run_anonymized_corpus_ids()  # one lookup for the whole list, not per row
    return {
        "items": [_scope_out(s, declared, row) for s in _scopes(row)],
        "zones": [_zone_out(z) for z in zone_rows(row)],
    }


@router.post("/connections/{connection_id}/scopes", status_code=201)
async def confirm_scope(
    connection_id: str,
    body: ConfirmScopeBody,
    user: dict = Depends(require_admin),
):
    """Confirm one selected site/library/folder as a scope.

    Creates its collection on first confirmation — unless the same
    ``source_scope_id`` was unticked earlier on this connection, in which
    case its previous collection is re-adopted via :func:`remove_scope`'s
    tombstone (see :func:`_readopted_scope_collection_id`) rather than a
    duplicate minted. Re-confirming a LIVE scope (same
    ``source_scope_id``) reuses that same collection (idempotent) and updates
    ``display_path``/``anonymize``/``access_mode``/``drive_id`` in place — a
    rename or move in the source does not fork a second collection (§6
    applied to the wizard's own bookkeeping). ``group_ids``, **if the field
    is present**, is the complete set of groups for this collection (step
    3): listed groups are granted, and any other group's grant on this
    collection is revoked — EXCEPT a mirrored (sentinel-owned) grant, which
    this checkbox can never touch (see the revoke loop below); "stop
    mirroring" is ``access_mode``, not a checkbox. The wizard's checkboxes
    are pre-checked from the grants that exist and its row warns the moment
    the last one is unticked, so the screen already promises that unticking
    removes access — making the handler additive-only meant the admin was
    shown a revocation that never happened. Omitting the field touches no
    grant at all, which is what keeps a rename or an anonymize toggle from
    stripping access as a side effect.

    ``access_mode='mirrored'`` (spec §2.5) opts this scope into the
    ``sharepoint-acl-sync`` job's reconciliation and REQUIRES ``drive_id``
    (``400 missing_drive_id`` otherwise — the sync needs both a drive id and
    an item id to address the scope root on Graph). Switching an already-
    mirrored scope back to ``manual`` deletes the sync's own sentinel-owned
    grants for this collection and converts nothing — the admin re-grants
    manually, same as any other scope that was never mirrored.

    ``include_excluded_subtrees=true`` (2026-08-30 plan, Task 7) asks to
    "include anyway" a broken-inheritance subtree the
    ``sharepoint-subtree-sweep`` job detected on this scope and excluded from
    the crawl by default (spec §3(b)). Refused with ``409
    must_not_forbids_subtree_override`` under the ``must_not`` guarantee mode
    (the fail-closed default — §1.2 disqualifies this override outright);
    accepted and audited (``sharepoint_acl.subtree_override``) under
    ``should_not``. This handler writes that ONE audit row INSTEAD of
    relying on the route's declared fallback action
    (``sharepoint_connection.scope_confirm``) for a request that turns the
    override on — see the audit playbook's "never write both for the same
    event" rule; every other confirm (no override transition) is still
    covered by the fallback, unchanged.
    """
    row = _sharepoint_connection_or_404(connection_id)

    if body.access_mode == "mirrored" and not body.drive_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_drive_id",
                "message": "access_mode='mirrored' requires drive_id — the ACL sync cannot address this scope root without it.",
            },
        )
    if body.drive_id:
        _validate_graph_id(body.drive_id, "drive_id")

    if body.include_excluded_subtrees:
        from app.switches import switch_value

        if switch_value("acl_guarantee_mode") == "must_not":
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "must_not_forbids_subtree_override",
                    "message": (
                        "acl_sync.guarantee_mode=must_not forbids including an excluded "
                        "broken-inheritance subtree — switch to should_not to allow this override."
                    ),
                },
            )

    if body.audience_classes is not None:
        names = [cls.name for cls in body.audience_classes]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise HTTPException(
                status_code=400,
                detail={"error": "duplicate_audience_class", "names": duplicates},
            )

    # Every group id referenced anywhere in this request — the share-step
    # checkboxes AND each audience class's membership — must already exist.
    # One check, one error shape (``invalid_group_id``), for both sources.
    _group_ids_to_check: List[str] = list(body.group_ids or [])
    for cls in body.audience_classes or []:
        _group_ids_to_check.extend(cls.group_ids)
    if _group_ids_to_check:
        groups_repo = user_groups_repo()
        seen: set = set()
        unknown = []
        for gid in _group_ids_to_check:
            if gid in seen:
                continue
            seen.add(gid)
            if groups_repo.get(gid) is None:
                unknown.append(gid)
        if unknown:
            raise HTTPException(status_code=400, detail={"error": "invalid_group_id", "group_ids": unknown})

    scopes = _scopes(row)
    existing = next((s for s in scopes if s.get("source_scope_id") == body.source_scope_id), None)
    previous_access_mode = (existing or {}).get("access_mode") or "manual"
    previous_override = bool((existing or {}).get("include_excluded_subtrees"))

    # Untick tombstone for this scope, if any (see :func:`remove_scope`) —
    # popped unconditionally: once this confirm lands, the scope row itself
    # is the bookkeeping again and a stale tombstone would only mislead.
    retired = dict((row.get("config") or {}).get("retired_scope_collections") or {})
    tombstone = retired.pop(body.source_scope_id, None)

    if existing is not None:
        collection_id = existing["collection_id"]
        existing["display_path"] = body.display_path
        existing["anonymize"] = body.anonymize
        existing["access_mode"] = body.access_mode
        existing["drive_id"] = body.drive_id
        existing["include_excluded_subtrees"] = body.include_excluded_subtrees
    else:
        # Re-adopt before minting: unticking and re-ticking the SAME folder
        # must map back to the scope's previous collection, never fork a
        # slug-suffixed duplicate (the pre-2026-08-31 behavior, which left
        # an orphaned 0-file collection next to its re-tick twin).
        collection_id = _readopted_scope_collection_id(tombstone)
        if collection_id is None:
            collection_id = _create_scope_collection(
                connection_name=row.get("name") or connection_id,
                display_path=body.display_path,
                source_scope_id=body.source_scope_id,
                created_by=user.get("id"),
            )
        scopes.append(
            {
                "source_scope_id": body.source_scope_id,
                "display_path": body.display_path,
                "anonymize": body.anonymize,
                "collection_id": collection_id,
                "access_mode": body.access_mode,
                "drive_id": body.drive_id,
                "include_excluded_subtrees": body.include_excluded_subtrees,
            }
        )

    # Audience-class mapping (2026-08-30 plan, Task 8): omitted (``None``)
    # leaves whatever is already on the row untouched — a step 2/step 3
    # confirm that says nothing about audience tiers must not wipe a
    # configured mapping; ``[]`` explicitly clears it. Same
    # omitted-vs-empty contract as ``group_ids`` above, applied to the ONE
    # scope row this request is confirming (new or existing — `existing`
    # is `None` for a brand-new scope, whose freshly appended dict is the
    # last entry of `scopes`).
    target_scope = existing if existing is not None else scopes[-1]
    if body.audience_classes is not None:
        target_scope["audience_classes"] = [
            {"name": cls.name, "group_ids": list(cls.group_ids)} for cls in body.audience_classes
        ]

    # See SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS's docstring above if you are
    # adding a new key here rather than editing this one.
    new_config = {**(row.get("config") or {}), "scopes": scopes}
    if tombstone is not None:
        new_config["retired_scope_collections"] = retired
    source_connections_repo().update(connection_id, config=new_config)

    # Audit the override TRANSITION only (false -> true) — never on a
    # re-confirm that resends an already-active override, same
    # avoid-audit-noise posture as _replace_membership_and_audit in
    # connectors/sharepoint/acl_sync.py. See this function's own docstring
    # for why this REPLACES the route's declared fallback action for this
    # one request.
    if body.include_excluded_subtrees and not previous_override:
        log_safe(
            user_id=user.get("id"),
            action="sharepoint_acl.subtree_override",
            resource=f"file_corpus:{collection_id}",
            params={"source_scope_id": body.source_scope_id},
            result="success",
        )

    grants = resource_grants_repo()

    if body.group_ids is not None:
        wanted = set(body.group_ids)
        for group_id in wanted:
            grants.ensure_grant(
                group_id,
                ResourceType.COLLECTION.value,
                collection_id,
                assigned_by=user.get("id"),
            )
        # Revoke what was unticked. Scoped to grants on THIS collection, so a
        # group's access to anything else is untouched — and a mirrored
        # (sentinel-owned) row is never touched here: it would only
        # resurrect at the next sync, and "stop mirroring" is access_mode,
        # not this checkbox (spec §2.3).
        for grant in grants.list_all(resource_type=ResourceType.COLLECTION.value):
            if grant.get("resource_id") != collection_id or grant.get("group_id") in wanted:
                continue
            if (grant.get("assigned_by") or "") == ACL_SYNC_SENTINEL:
                continue
            grants.delete(grant["id"])

    if previous_access_mode == "mirrored" and body.access_mode == "manual":
        # Spec §2.5: switching mirrored -> manual deletes the sync's own
        # grants for this scope's collection and converts nothing — the
        # admin re-grants manually.
        for grant in grants.list_all(resource_type=ResourceType.COLLECTION.value):
            if grant.get("resource_id") == collection_id and (grant.get("assigned_by") or "") == ACL_SYNC_SENTINEL:
                grants.delete(grant["id"])

    logger.info(
        "sharepoint connection %s: scope %s confirmed -> collection %s",
        connection_id,
        body.source_scope_id,
        collection_id,
    )
    updated_row = next(s for s in scopes if s.get("source_scope_id") == body.source_scope_id)
    return _scope_out(updated_row, connection=row)


@router.delete("/connections/{connection_id}/scopes", response_model=ScopeRemovalOut)
async def remove_scope(
    connection_id: str,
    source_scope_id: str,
    _user: dict = Depends(require_admin),
):
    """Unselect a scope — an explicit exclusion (spec §13.2: "unselected rows
    are explicit exclusions"). Removes the wizard's own bookkeeping row; what
    happens to the scope's collection depends on whether it holds data:

    * **Empty (0 files)** — soft-deleted. Deleting a collection stays a
      separate, deliberate operation where DATA is at stake, but an empty
      scope collection has none, and keeping it is exactly what bred
      orphaned 0-file collections (observed live 2026-08-31). The response
      says ``collection_kept: false``.
    * **Has files** — kept, and the response says so (``collection_kept:
      true`` + the collection ref) so the wizard can tell the admin where to
      delete it deliberately (the Library).

    Either way a tombstone (``config.retired_scope_collections``, keyed by
    ``source_scope_id``) records which collection this scope owned, so a
    later re-tick of the same folder re-adopts it — restoring the
    auto-deleted empty one, re-attaching to the kept one — instead of
    minting a slug-suffixed duplicate (see
    :func:`_readopted_scope_collection_id`).

    Its ``sharepoint-acl-sync``-owned (sentinel-assigned) grants on that
    collection do NOT survive, though (2026-08-31 plan, Task 8): with the
    scope row gone, the sync never reconciles that collection again, so a
    sentinel-owned grant left behind would dangle forever — indistinguishable
    from a deliberate, still-maintained grant to anyone reading ``/admin/
    access``. Same removal loop :func:`confirm_scope` uses for its own
    ``mirrored`` -> ``manual`` transition; an admin-assigned grant on the
    same collection is untouched either way (on the kept collection it keeps
    working; on the soft-deleted one it resurrects with it on re-tick, so
    the share state round-trips the untick like everything else).
    """
    row = _sharepoint_connection_or_404(connection_id)
    scopes = _scopes(row)
    removed = next((s for s in scopes if s.get("source_scope_id") == source_scope_id), None)
    if removed is None:
        raise HTTPException(status_code=404, detail="scope_not_found")
    remaining = [s for s in scopes if s.get("source_scope_id") != source_scope_id]

    collection_id = removed.get("collection_id")
    collection = file_corpora_repo().get(collection_id) if collection_id else None
    kept = False
    if collection is not None:
        if corpus_files_repo().list_for_corpus(collection_id):
            kept = True
        else:
            file_corpora_repo().soft_delete(collection_id)

    # See SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS's docstring above if you are
    # adding a new key here rather than editing this one.
    new_config = {**(row.get("config") or {}), "scopes": remaining}
    if collection is not None:
        retired = dict((row.get("config") or {}).get("retired_scope_collections") or {})
        retired[source_scope_id] = {"collection_id": collection_id, "auto_deleted": not kept}
        new_config["retired_scope_collections"] = retired
    source_connections_repo().update(connection_id, config=new_config)

    if collection_id:
        grants = resource_grants_repo()
        for grant in grants.list_all(resource_type=ResourceType.COLLECTION.value):
            if grant.get("resource_id") == collection_id and (grant.get("assigned_by") or "") == ACL_SYNC_SENTINEL:
                grants.delete(grant["id"])

    return {
        "collection_kept": kept,
        "collection": (
            {"id": collection["id"], "slug": collection["slug"], "name": collection["name"]} if kept else None
        ),
    }


@router.post("/connections/{connection_id}/scopes/bulk")
async def bulk_add_scopes(
    connection_id: str,
    body: BulkScopeBody,
    user: dict = Depends(require_admin),
):
    """Confirm many site/library/folder paths as scopes in one call — the
    fast path for splitting one large SharePoint site across several
    connections (each with its own crawl and facts jobs, so they run in
    parallel): resolve each admin-typed ``paths`` entry to a Graph drive
    item (:func:`connectors.sharepoint.graph_client.get_item_by_path`) and
    write a scope row for it, same shape :func:`confirm_scope` writes
    (``access_mode="manual"``, ``include_excluded_subtrees=False`` — the
    same defaults a single manual confirm gets; #2032 made these round-trip
    through list/confirm and this endpoint honours the same contract) —
    minus the wizard's own group-grant step (``group_ids``), which stays a
    separate, deliberate action on each created scope.

    Never all-or-nothing: every path is resolved and reported independently
    in the response, ``{"created": [...], "skipped": [...], "failed":
    [{"path", "reason"}]}`` —

    * **created** — a fresh scope, collection minted (or re-adopted from a
      matching untick tombstone, same as :func:`confirm_scope`); each entry
      is `{"path", **scope}` (the same projection ``GET …/scopes`` returns).
    * **skipped** — the path resolved to a ``source_scope_id`` ALREADY
      present among this connection's scopes (the idempotency key
      :func:`confirm_scope` itself uses) — ``{"path", "source_scope_id",
      "reason": "already_present"}``.
    * **failed** — Graph could not resolve the path: ``{"path", "reason"}``
      with ``reason`` one of ``"not_found"`` (404) or ``"forbidden"``
      (403) — the same routine-permissions-fact classification
      :func:`connectors.sharepoint.graph_client.search_folders` already
      uses, never surfaced as a whole-request failure.

    Any OTHER Graph failure (401/429/5xx, a network fault) is not a
    per-path fact — it means the whole call is broken, same rule
    :func:`connectors.sharepoint.graph_client.search_folders` documents —
    and aborts the remaining, unprocessed paths with a typed ``502
    sharepoint_graph_error``; whatever was already resolved and created
    before that point is still persisted (already-report paths are not
    rolled back by a later path's outage).

    ``drive_id`` is required unless this connection already has at least
    one scope with one set (reused from the first match) — a brand-new
    connection with zero scopes (:func:`clone_connection`'s own starting
    point) must supply it explicitly (``400 drive_id_required``
    otherwise); a malformed one is a typed ``422`` (see
    :func:`_validate_graph_id`), same as ``POST …/scopes``'s own
    ``drive_id``.
    """
    row = _sharepoint_connection_or_404(connection_id)

    paths = [p.strip() for p in body.paths if p and p.strip()]
    if not paths:
        raise HTTPException(
            status_code=422,
            detail={"error": "empty_paths", "message": "paths must contain at least one non-empty path"},
        )

    if body.drive_id:
        _validate_graph_id(body.drive_id, "drive_id")
        drive_id = body.drive_id
    else:
        drive_id = next((s.get("drive_id") for s in _scopes(row) if s.get("drive_id")), None)
        if not drive_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "drive_id_required",
                    "message": (
                        "drive_id was not supplied and this connection has no existing scope to infer "
                        "one from — pass drive_id explicitly (GET …/tree finds one)."
                    ),
                },
            )

    token = await _resolved_token(row)

    scopes = _scopes(row)
    existing_by_id = {s.get("source_scope_id"): s for s in scopes}
    declared_corpus_ids = _latest_run_anonymized_corpus_ids()  # one lookup for the whole batch, not per path
    # Untick tombstones for this connection (see :func:`remove_scope`) —
    # popped as each is re-adopted below, same "the scope row itself is the
    # bookkeeping again" rule :func:`confirm_scope` applies to a single
    # re-tick.
    retired = dict((row.get("config") or {}).get("retired_scope_collections") or {})
    retired_changed = False

    created: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    def _persist() -> None:
        if not created:
            return
        # See SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS's docstring above if you
        # are adding a new key here rather than editing this one.
        new_config = {**(row.get("config") or {}), "scopes": scopes}
        if retired_changed:
            new_config["retired_scope_collections"] = retired
        source_connections_repo().update(connection_id, config=new_config)

    try:
        for path in paths:
            try:
                item = await get_item_by_path(token, drive_id, path)
            except SharePointGraphError as exc:
                if exc.status_code not in (403, 404):
                    raise
                reason = "forbidden" if exc.status_code == 403 else "not_found"
                failed.append({"path": path, "reason": reason})
                continue

            source_scope_id = item["id"]
            if source_scope_id in existing_by_id:
                skipped.append({"path": path, "source_scope_id": source_scope_id, "reason": "already_present"})
                continue

            tombstone = retired.pop(source_scope_id, None)
            if tombstone is not None:
                retired_changed = True
            collection_id = _readopted_scope_collection_id(tombstone)
            if collection_id is None:
                collection_id = _create_scope_collection(
                    connection_name=row.get("name") or connection_id,
                    display_path=path,
                    source_scope_id=source_scope_id,
                    created_by=user.get("id"),
                )
            scope_row = {
                "source_scope_id": source_scope_id,
                "display_path": path,
                "anonymize": False,
                "collection_id": collection_id,
                "access_mode": "manual",
                "drive_id": drive_id,
                "include_excluded_subtrees": False,
            }
            scopes.append(scope_row)
            existing_by_id[source_scope_id] = scope_row
            created.append({"path": path, **_scope_out(scope_row, declared_corpus_ids, connection=row)})
    except SharePointGraphError as exc:
        _persist()
        raise HTTPException(
            status_code=502,
            detail={"error": "sharepoint_graph_error", "message": str(exc)},
        ) from exc

    _persist()

    log_safe(
        user_id=user.get("id"),
        action="sharepoint_connection.scope_bulk_add",
        resource=f"source_connection:{connection_id}",
        params={"requested": len(paths), "created": len(created), "skipped": len(skipped), "failed": len(failed)},
        result="success",
    )

    return {"created": created, "skipped": skipped, "failed": failed}


@router.post("/connections/{connection_id}/clone", status_code=201)
async def clone_connection(
    connection_id: str,
    body: CloneConnectionBody,
    user: dict = Depends(require_admin),
):
    """Create a sibling SharePoint connection wired to the SAME credential
    material as ``connection_id`` — the other half of splitting one large
    site across several connections (each with its own crawl and facts
    jobs, so they run in parallel; see :func:`bulk_add_scopes`).

    Copies every config key EXCEPT :data:`_CLONE_EXCLUDED_CONFIG_KEYS`
    (``scopes``, the confirmed-scope rows, and ``extraction``, the
    extraction schedule's own dispatch bookkeeping) — the clone starts with
    zero scopes and no dispatch history, so no scheduled crawl/ACL-sync/
    subtree-sweep/facts-extraction sweep touches it until an admin confirms
    scopes on it. Everything else carries over, including ``manual_sites``
    — under ``Sites.Selected`` (module docstring: that permission
    403-forbids ``/sites`` enumeration), a bookmarked site added by URL is
    how the clone can resolve anything at all, so leaving it behind would
    make the clone unable to browse the very site it exists to split;
    ``webhook_secret`` and ``retired_scope_collections`` carry over for the
    same "same site/host settings" reason, and are inert until the clone
    has scopes/subscriptions of its own.

    **The certificate/secret VALUE itself is never copied** — only its
    reference. When the source's certificate lives in a deployment env var
    (``config.cert_private_key_env``/``client_secret_env``, the common
    case), the clone resolves the exact same value on its own, no admin
    action required (:func:`connectors.sharepoint.settings.
    resolve_sharepoint_settings`). When it was instead uploaded to the
    source's OWN vault slot (``connection_secrets`` — one row per
    ``connection_id``, see that module's docstring), it is NOT duplicated
    into a second row for the clone: an admin re-uploads it to the clone
    separately. ``404`` for an unknown/non-SharePoint connection id; ``409
    connection_name_exists`` if ``name`` is already taken (same rule as
    ``POST /api/admin/source-connections``).
    """
    row = _sharepoint_connection_or_404(connection_id)

    repo = source_connections_repo()
    if repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="connection_name_exists")

    cloned_config = _cloned_base_config(row)

    new_id = str(uuid4())
    repo.create(
        id=new_id,
        name=body.name,
        source_type="sharepoint",
        config=cloned_config,
        token_env=row.get("token_env"),
        is_default=False,
        created_by=user.get("id"),
    )

    log_safe(
        user_id=user.get("id"),
        action="sharepoint_connection.clone",
        resource=f"source_connection:{new_id}",
        params={"source_connection_id": connection_id, "name": body.name},
        result="success",
    )

    return {"id": new_id, "name": body.name}


#: How many :func:`connectors.sharepoint.graph_client.search_document_count`
#: calls the split planner keeps in flight at once — a genuinely large site
#: can have dozens of top-level folders, and Graph Search has no batch form
#: (unlike :func:`probe_unique_permissions`'s ``/$batch``), so this is the
#: only throttle standing between "click preview" and Graph rate-limiting
#: this admin. Chosen the same way :data:`_MAX_PERMISSION_PROBE_ITEMS` was:
#: comfortably under Graph's per-app throttle for a one-click admin action,
#: not tuned against a measured workload.
_SPLIT_COUNT_CONCURRENCY = 8


def _public_folder(folder: Dict[str, Any]) -> Dict[str, Any]:
    """A folder's shape in an HTTP response — never its Graph item ``id``,
    which :func:`_compute_split_plan`'s internal folder dicts also carry
    (needed by :func:`apply_split` to mint scopes without a second Graph
    round trip) but which no external contract in this module's docstring
    promises."""
    return {"name": folder["name"], "documents": folder["documents"]}


async def _compute_split_plan(
    row: Dict[str, Any], *, n: int, min_modified: Optional[str], drive_id: Optional[str]
) -> Dict[str, Any]:
    """Live Graph read + greedy pack — the shared computation behind
    ``GET …/split-plan`` (a read-only preview) and ``POST …/splits`` (which
    computes the IDENTICAL plan immediately before creating clones from it,
    so what an admin previewed is exactly what gets applied — no separate
    "confirm" step re-derives a possibly-different plan from data that may
    have shifted between the two calls).

    Returns an INTERNAL-shaped dict (folder dicts carry ``id``, needed by
    :func:`apply_split` to mint scopes) — callers project down to the public
    response shape via :func:`_public_folder` before returning to an HTTP
    caller. Never persists anything.

    ``404``/``409``/``502`` etc. are raised as :class:`HTTPException` from
    here (drive-id inference, Graph errors) — both callers propagate them
    unchanged.
    """
    resolved_drive_id = _inferred_drive_id(row, drive_id)
    token = await _resolved_token(row)
    try:
        children = await list_root_children_with_url(token, resolved_drive_id)
    except SharePointGraphError as exc:
        raise HTTPException(status_code=502, detail={"error": "sharepoint_graph_error", "message": str(exc)}) from exc

    folder_items = [c for c in children if c.get("is_folder")]
    loose_root_files = [c["name"] for c in children if not c.get("is_folder")]

    semaphore = asyncio.Semaphore(_SPLIT_COUNT_CONCURRENCY)

    async def _counted(item: Dict[str, Any]) -> Dict[str, Any]:
        async with semaphore:
            # search_document_count() never raises — a folder whose count
            # could not be read still gets assigned a group, at documents=0
            # (module docstring of connectors.sharepoint.site_split).
            documents = await search_document_count(token, item.get("web_url") or "", min_modified=min_modified)
        return {"id": item["id"], "name": item["name"], "documents": documents}

    folders = list(await asyncio.gather(*[_counted(item) for item in folder_items])) if folder_items else []

    groups_raw = pack_folders_into_groups(folders, n)
    source_name = row.get("name") or row["id"]
    groups = [
        {"name": format_group_name(source_name, index, n), "folders": g["folders"], "documents": g["documents"]}
        for index, g in enumerate(groups_raw, start=1)
    ]
    total_documents = sum(f["documents"] for f in folders)

    return {
        "drive_id": resolved_drive_id,
        "folders": folders,
        "loose_root_files": loose_root_files,
        "groups": groups,
        "total_documents": total_documents,
    }


@router.get("/connections/{connection_id}/split-plan")
async def split_plan(
    connection_id: str,
    n: int = Query(..., ge=1, le=_SPLIT_MAX_N),
    min_modified: Optional[str] = None,
    drive_id: Optional[str] = None,
    _user: dict = Depends(require_admin),
):
    """Read-only preview of splitting this connection's site into ``n``
    sibling connections (see the module docstring's "split a large site"
    entry) — greedy-packs the drive root's top-level folders into ``n``
    groups of roughly equal document count, WITHOUT creating anything.

    ``drive_id`` is optional — same inference as ``POST …/scopes/bulk``:
    reused from this connection's first existing scope when omitted, ``400
    drive_id_required`` when neither is available (a split only makes sense
    on a connection that already resolves a drive). ``min_modified``
    (``YYYY-MM-DD``, ``400 invalid_min_modified`` otherwise) narrows each
    folder's document count to files modified on/after that date, via the
    same Graph Search filter :func:`connectors.sharepoint.graph_client.
    search_document_count` builds.

    Drive-root items that are FILES, not folders, are reported under
    ``loose_root_files`` — they are in no folder, so a folder-based split
    (this one, and the manual ``clone`` + ``scopes/bulk`` workflow it
    automates) can never cover them; an admin sees exactly what would be
    left behind rather than discovering it after the fact.

    Response: ``{drive_id, folders: [{name, documents}], loose_root_files:
    [names], groups: [{name, folders: [{name, documents}], documents}],
    total_documents}`` — ``groups[].name`` is the EXACT name ``POST …/splits``
    will give the corresponding clone (:func:`connectors.sharepoint.
    site_split.format_group_name`), so an admin previewing this can see
    ahead of time what will collide with ``409 split_exists`` on a repeat
    apply.

    ``404`` for an unknown/non-SharePoint connection id. A Graph failure
    while listing the root (not a per-folder count failure — those degrade
    to ``documents: 0``, never fail the whole preview) is a typed ``502
    sharepoint_graph_error``.
    """
    row = _sharepoint_connection_or_404(connection_id)
    _validate_min_modified(min_modified)

    plan = await _compute_split_plan(row, n=n, min_modified=min_modified, drive_id=drive_id)

    return {
        "drive_id": plan["drive_id"],
        "folders": [_public_folder(f) for f in plan["folders"]],
        "loose_root_files": plan["loose_root_files"],
        "groups": [
            {"name": g["name"], "folders": [_public_folder(f) for f in g["folders"]], "documents": g["documents"]}
            for g in plan["groups"]
        ],
        "total_documents": plan["total_documents"],
    }


@router.post("/connections/{connection_id}/splits", status_code=201)
async def apply_split(
    connection_id: str,
    body: SplitApplyBody,
    user: dict = Depends(require_admin),
):
    """Apply a site split (see :func:`split_plan` above for the read-only
    preview this computes identically before creating anything): creates
    ``body.n`` sibling connections, each named ``"<source name> — part
    i/n"`` (:func:`connectors.sharepoint.site_split.format_group_name`),
    wired to the same credential material as the source
    (:func:`_cloned_base_config`, the same helper ``POST …/clone`` uses) and
    given its own slice of the source's top-level folders as confirmed
    scopes — the same scope-row shape and collection-minting call
    (:func:`_create_scope_collection`) ``POST …/scopes/bulk`` uses, so a
    split clone looks identical to one built by hand through clone +
    bulk-add.

    ``body.min_modified``, when given, is written onto EACH clone's
    ``config.extraction.crawl.min_modified`` — the crawl does not read that
    key yet (no admin-facing date filter has shipped for the crawl itself at
    the time this endpoint was written), so today it is inert bookkeeping
    that a near-term crawl change will start honoring under the same key,
    not a promise this endpoint enforces itself. ``body.transport`` /
    ``body.retry_mode`` are written onto ``config.extraction.facts`` — the
    exact keys ``PATCH …/extraction/facts-config`` writes — so a split can
    hand every clone the same per-connection retry/transport policy in one
    call instead of ``n`` follow-up PATCHes.

    **Idempotency**: refuses with ``409 split_exists`` BEFORE creating
    anything if a connection named like any of this split's target names
    already exists (the exact names :func:`split_plan` would have shown) —
    a repeat ``POST`` never creates a second, name-colliding batch.

    ``body.start=True`` enqueues each clone's ``corpus-extraction`` job
    immediately after it is created, in creation order — the same job
    ``POST …/{id}/extract`` enqueues, skipped silently (never a 409/500 that
    would make a split appear to have failed) when extraction readiness
    (``sharepoint.enabled`` / the ``extraction`` extra) is not currently
    satisfied; the clones themselves are still created either way.

    Returns ``{"connections": [{id, name, folders: [{name, documents}],
    documents}]}`` — one entry per created clone, in the same order as
    ``split_plan``'s own ``groups``.
    """
    row = _sharepoint_connection_or_404(connection_id)
    _validate_min_modified(body.min_modified)

    if body.retry_mode is not None:
        from connectors.sharepoint.facts_extraction import _VALID_RETRY_MODES

        if body.retry_mode not in _VALID_RETRY_MODES:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "invalid_retry_mode",
                    "message": f"retry_mode must be one of {sorted(_VALID_RETRY_MODES)}",
                },
            )

    source_name = row.get("name") or connection_id
    target_names = [format_group_name(source_name, index, body.n) for index in range(1, body.n + 1)]

    repo = source_connections_repo()
    if any(repo.get_by_name(name) is not None for name in target_names):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "split_exists",
                "message": "connections named like this split already exist — drop them first, or this is a repeat apply",
            },
        )

    plan = await _compute_split_plan(row, n=body.n, min_modified=body.min_modified, drive_id=None)
    base_config = _cloned_base_config(row)

    extraction_cfg: Dict[str, Any] = {}
    if body.min_modified:
        extraction_cfg["crawl"] = {"min_modified": body.min_modified}
    facts_cfg: Dict[str, Any] = {}
    if body.retry_mode is not None:
        facts_cfg["retry_mode"] = body.retry_mode
    if body.transport is not None:
        facts_cfg["transport"] = body.transport
    if facts_cfg:
        extraction_cfg["facts"] = facts_cfg

    created: List[Dict[str, Any]] = []
    for name, group in zip(target_names, plan["groups"]):
        scope_rows = []
        for folder in group["folders"]:
            collection_id = _create_scope_collection(
                connection_name=name,
                display_path=folder["name"],
                source_scope_id=folder["id"],
                created_by=user.get("id"),
            )
            scope_rows.append(
                {
                    "source_scope_id": folder["id"],
                    "display_path": folder["name"],
                    "anonymize": False,
                    "collection_id": collection_id,
                    "access_mode": "manual",
                    "drive_id": plan["drive_id"],
                    "include_excluded_subtrees": False,
                }
            )

        new_config: Dict[str, Any] = {**base_config, "scopes": scope_rows}
        if extraction_cfg:
            new_config["extraction"] = dict(extraction_cfg)

        new_id = str(uuid4())
        repo.create(
            id=new_id,
            name=name,
            source_type="sharepoint",
            config=new_config,
            token_env=row.get("token_env"),
            is_default=False,
            created_by=user.get("id"),
        )
        created.append(
            {
                "id": new_id,
                "name": name,
                "folders": [_public_folder(f) for f in group["folders"]],
                "documents": group["documents"],
            }
        )

    if body.start:
        usable, _readiness_error = _extraction_readiness()
        if usable:
            from src.repositories import jobs_repo

            for entry in created:
                new_row = repo.get(entry["id"])
                if new_row is None:
                    continue
                job = jobs_repo().enqueue(
                    "corpus-extraction",
                    {"connection_id": entry["id"]},
                    idempotency_key=_extraction_idempotency_key(entry["id"]),
                )
                _record_extraction_dispatch(new_row, job["id"])

    log_safe(
        user_id=user.get("id"),
        action="sharepoint_connection.split_apply",
        resource=f"source_connection:{connection_id}",
        params={
            "n": body.n,
            "min_modified": body.min_modified,
            "transport": body.transport,
            "retry_mode": body.retry_mode,
            "start": body.start,
            "created_ids": [c["id"] for c in created],
        },
        result="success",
    )

    return {"connections": created}


@router.get("/connections/{connection_id}/certificate")
async def certificate(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Read-only certificate metadata for the connection's already-stored
    PEM — thumbprint (the ``x5t`` value the client actually presents, plus
    the conventional uppercase-hex fingerprint), subject/issuer, validity
    window, and a derived ``ok``/``expiring_soon``/``expired`` status. Two
    real failure modes this closes: a registered certificate that does not
    match what the connection actually presents (opaque provider auth
    error), and a certificate expiring silently (crawl/sync fails with no
    warning).

    Never the private key — only :func:`connectors.sharepoint.graph_client.
    certificate_metadata`'s CERTIFICATE-block parse reaches the response.
    No certificate configured, or a certificate that fails to resolve or
    parse, is a typed absence (``certificate: null`` plus ``reason``) —
    never a 500.
    """
    row = _sharepoint_connection_or_404(connection_id)
    try:
        settings = resolve_sharepoint_settings(row)
    except SharePointSettingsError as exc:
        return {"certificate": None, "reason": f"sharepoint_cert_unresolved: {exc}"}
    if settings.auth_method == "client_secret":
        # No certificate exists to describe — a typed absence, same shape as
        # the unresolved/unparseable cases, never an error.
        return {"certificate": None, "reason": "client_secret_auth"}
    return certificate_metadata(settings.private_key)


@router.post("/connections/{connection_id}/webhook")
async def rotate_webhook_secret(
    connection_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """(Re)generate this connection's Graph change-notification receiver
    secret and return the receiver URL alongside it. The secret becomes the
    ``clientState`` of every Graph drive subscription
    ``POST .../subscriptions/ensure`` (below) creates for this connection;
    the URL is what those subscriptions push to.

    Always mints a FRESH random secret — there is no "read the current
    one" verb, matching the outbound-webhook pattern
    (``app/api/agent_webhooks.py``): a caller who wants to see it again
    calls this again, which also rotates it, invalidating whatever Graph
    subscription was signed with the old value. A rotation does NOT rewrite
    a live subscription's ``clientState`` (Graph treats it as immutable), so
    follow a rotation with ``POST .../subscriptions/ensure`` — every
    notification signed with the old secret is dropped silently by the
    receiver until you do.

    Unlike the outbound-webhook secret, this one is NOT hidden after
    creation: it lives in this connection's own ``config.webhook_secret``
    (see ``SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS``), the same trust
    boundary ``config.tenant_id``/``client_id`` already sit behind, so any
    admin who can ``GET`` this connection can also read it back later. That
    is an admin-to-admin visibility question, not a public one — the
    receiver route (``app/api/sharepoint_webhooks.py``) never returns it and
    verifies every notification's ``clientState`` against it in constant
    time.
    """
    row = _sharepoint_connection_or_404(connection_id)
    secret = secrets.token_hex(32)
    new_config = {**(row.get("config") or {}), "webhook_secret": secret}
    source_connections_repo().update(connection_id, config=new_config)

    webhook_url = f"{public_base_url(request=request)}/api/webhooks/sharepoint/{connection_id}"
    log_safe(
        user_id=user.get("id"),
        action="sharepoint_connection.webhook_secret_rotate",
        resource=f"source_connection:{connection_id}",
    )
    logger.info("sharepoint connection %s: webhook secret rotated", connection_id)
    return {"webhook_url": webhook_url, "secret": secret}


class ExtractionRunOptions(BaseModel):
    """Optional per-run overrides for one manual extraction trigger. Both
    fields default to the CONFIGURED values (`extraction.crawler.concurrency`
    / `extraction.timeout_s`) when absent — the body itself is optional, so
    the pre-options `POST` with no body keeps working unchanged. Bounds
    mirror the crawler's own clamps so a value the run would silently
    re-clamp is refused here instead, where the admin can see why."""

    concurrency: Optional[int] = Field(
        None,
        ge=1,
        le=16,
        description="Files of one delta page pipelined at once for this run (1 = sequential).",
    )
    timeout_s: Optional[int] = Field(
        None,
        ge=0,
        le=86400,
        description="Hard ceiling for this one run, seconds (0 = unbounded).",
    )
    resync: Optional[bool] = Field(
        None,
        description=(
            "Drop this connection's persisted deltaLinks and item-failure queue before "
            "running, so every drive re-enumerates from scratch (already-ingested files "
            "are not re-downloaded — cTags are kept). The supported recovery path for a "
            "connection whose delta cursor ran past documents it never actually ingested."
        ),
    )
    force_reprocess: Optional[bool] = Field(
        None,
        description=(
            "Ignore this connection's persisted deltaLinks AND cTags for this run only, "
            "so every item is re-downloaded, re-converted and re-ingested even when it "
            "looks unchanged — the control for 're-process everything', e.g. after a "
            "converter or anonymizer setting change with no content diff. Re-downloads "
            "and re-converts the whole corpus and, when facts extraction is on, re-runs "
            "the LLM pass over every document — a materially more expensive run than a "
            "plain trigger or `resync`. Never written to the state file up front: an "
            "interrupted run leaves the connection exactly as resumable as before."
        ),
    )


# --- Graph subscription lifecycle -------------------------------------------
#
# The secret-minting endpoint above is only half of what near-real-time
# crawling needs: something has to tell Graph to push in the first place.
# That was the retired external producer's `subscriptions.py`, run by hand;
# it now lives at `connectors/sharepoint/subscriptions.py` and these three
# routes are its admin surface. The lifecycle module owns every decision
# (which drives, expiry math, per-drive isolation, the state on
# `config.webhook_subscriptions`); these handlers only translate its typed
# refusals into HTTP and audit the outcome.
#
# There is no GET: subscription state is plain server-written config, so it
# rides the connection read (`GET /api/admin/source-connections/{id}`) that
# already returns `config` — a fourth route would be a second name for data
# an admin can already see.

if TYPE_CHECKING:  # import-time only: the runtime imports stay inside the handlers
    from connectors.sharepoint.subscriptions import SubscriptionError


def _subscription_http_error(exc: "SubscriptionError") -> HTTPException:
    """One typed refusal → one typed HTTP error whose body names the fix
    (`{"error", "message"}`, the same detail shape `_resolved_token` and the
    extraction trigger already use)."""
    return HTTPException(status_code=exc.status_code, detail=exc.as_detail())


@router.post("/connections/{connection_id}/subscriptions/ensure")
async def ensure_graph_subscriptions(
    connection_id: str,
    request: Request,
    user: dict = Depends(require_admin),
):
    """Create or renew this connection's Microsoft Graph drive subscriptions
    so its confirmed scopes push change notifications at
    ``POST /api/webhooks/sharepoint/{connection_id}``.

    Idempotent — one subscription per DISTINCT drive named by a confirmed
    scope (several scopes in one library share one subscription), created
    when missing, renewed when within the renewal window, left alone
    otherwise, and deleted when its drive leaves scope. Returns the per-drive
    outcome (``created``/``renewed``/``unchanged``/``removed``/``failed``)
    plus counts: one library failing never hides the others' success.

    Refuses BEFORE touching Graph, with a body naming the fix, when
    ``sharepoint.enabled`` is off (``409
    sharepoint_disabled`` — a subscription pointed at a 404 receiver
    is dead on arrival), no webhook secret has been minted yet (``409
    webhook_secret_missing`` — it is the subscription's ``clientState``), no
    public HTTPS origin is configured (``409 public_url_not_configured`` —
    Graph validates the notification URL synchronously during create), or
    the connection's certificate does not resolve / Entra rejects it (``409
    sharepoint_cert_unresolved`` / ``502 sharepoint_graph_error``).
    """
    from connectors.sharepoint.subscriptions import SubscriptionError, ensure_subscriptions

    row = _sharepoint_connection_or_404(connection_id)
    try:
        result = await ensure_subscriptions(row, request=request)
    except SubscriptionError as exc:
        log_safe(
            user_id=user.get("id"),
            action="sharepoint_connection.subscriptions_ensure",
            resource=f"source_connection:{connection_id}",
            params={"error": exc.error},
            result="error",
        )
        raise _subscription_http_error(exc) from exc

    log_safe(
        user_id=user.get("id"),
        action="sharepoint_connection.subscriptions_ensure",
        resource=f"source_connection:{connection_id}",
        params={
            "created": result["created"],
            "renewed": result["renewed"],
            "unchanged": result["unchanged"],
            "removed": result["removed"],
            "failed": result["failed"],
        },
    )
    return result


class SubscriptionTeardownRow(BaseModel):
    """One recorded subscription's teardown outcome."""

    drive_id: str
    subscription_id: str
    action: Literal["removed", "failed"]
    error: Optional[str] = None


class SubscriptionTeardownResult(BaseModel):
    """Why this DELETE answers ``200`` with a body rather than the house
    ``204``: teardown is PARTIAL by nature. One subscription's delete can
    fail (Graph 5xx, a revoked permission) while the rest succeed, and its
    record is deliberately KEPT so a later call retries — an admin has to be
    able to see which one, and a bodyless 204 cannot say. Declared as a
    response model and allowlisted in
    ``tests/test_api_design_rules.py::_DELETE_200_WITH_BODY_ALLOWLIST``,
    which is exactly the escape hatch that rule documents."""

    connection_id: str
    subscriptions: List[SubscriptionTeardownRow]
    removed: int
    failed: int


@router.delete("/connections/{connection_id}/subscriptions", response_model=SubscriptionTeardownResult)
async def delete_graph_subscriptions(
    connection_id: str,
    user: dict = Depends(require_admin),
):
    """Delete every Graph subscription recorded for this connection and drop
    the records it could delete.

    Deliberately NOT gated on ``sharepoint.enabled``: the moment an
    operator most needs teardown is right after turning the receiver off.
    A connection with no recorded subscriptions is a clean ``{"removed": 0}``
    no-op, not a 404 — "there is nothing to remove" is the state the caller
    asked for. Graph answering ``404`` for a subscription counts as removed;
    a real failure keeps the record so a later call retries — which is why
    this answers ``200`` with a per-subscription body instead of ``204``
    (see :class:`SubscriptionTeardownResult`).
    """
    from connectors.sharepoint.subscriptions import SubscriptionError, remove_subscriptions

    row = _sharepoint_connection_or_404(connection_id)
    try:
        result = await remove_subscriptions(row)
    except SubscriptionError as exc:
        log_safe(
            user_id=user.get("id"),
            action="sharepoint_connection.subscriptions_remove",
            resource=f"source_connection:{connection_id}",
            params={"error": exc.error},
            result="error",
        )
        raise _subscription_http_error(exc) from exc

    log_safe(
        user_id=user.get("id"),
        action="sharepoint_connection.subscriptions_remove",
        resource=f"source_connection:{connection_id}",
        params={"removed": result["removed"], "failed": result["failed"]},
    )
    return result


@router.post("/subscriptions/run-due")
async def run_due_subscription_renewal(
    _user: dict = Depends(require_admin),
):
    """Scheduler-driven renewal sweep: walks every SharePoint connection and
    ensures the ones with due work — a drive with no subscription, one
    expiring within the renewal window (72h by default), or a record whose
    drive left scope.

    Exactly the shape ``POST /extraction/run-due`` above uses (walk +
    per-row due-check + act, no second scheduling mechanism), and for the
    same reason: Graph subscriptions expire (30-day ceiling; Agnes asks for
    25), so renewal is a clock, and this instance already has one. Scheduler
    row ``sharepoint-subscriptions-renew`` in
    ``services/scheduler/__main__.py``, registered only when
    ``sharepoint.enabled`` is on.

    A clean, typed no-op (never an error) when the receiver flag is off,
    since the row once registered fires unconditionally. A connection whose
    own preconditions fail (no secret minted, no public URL, an expired
    certificate) is counted in ``errors`` and the sweep continues — one
    misconfigured connection never costs another its renewal.
    """
    from connectors.sharepoint.subscriptions import renew_due_subscriptions

    return await renew_due_subscriptions()


@router.post("/connections/{connection_id}/extract", status_code=202)
async def trigger_extraction(
    connection_id: str,
    options: Optional[ExtractionRunOptions] = None,
    _user: dict = Depends(require_admin),
):
    """Admin-triggered one-off extraction run for this connection
    (TCRD-226) — enqueues the existing ``corpus-extraction`` job kind
    (``app/worker/kinds.py::_run_corpus_extraction``) with
    ``{"connection_id": connection_id}``, the exact payload shape that
    handler documents. An optional :class:`ExtractionRunOptions` body adds
    the handler's per-run overrides (``concurrency``, ``timeout_s``) for
    THIS run only — configured values stay untouched — plus ``resync``, the
    supported alternative to hand-editing the crawl state file on the data
    disk when a connection's delta cursor ran past documents it never
    ingested (see ``connectors.sharepoint.crawler._apply_resync``), and
    ``force_reprocess``, the stronger "re-process everything" control that
    additionally ignores cTags so already-unchanged documents are
    re-downloaded and re-ingested too — never persisted beyond this one run.

    Every option here is per-run, not a setting: an absent key falls back to
    whatever is currently configured (or, for the two booleans, to "off"),
    and nothing in this endpoint ever writes an option's value anywhere an
    admin could re-read it as the new default.

    404 on an unknown/non-sharepoint connection BEFORE any other work.
    Then refuses cleanly (never a job that fails 30 minutes later in a
    worker) when the feature isn't usable: ``409 extraction_disabled``
    (``sharepoint.enabled`` is false) or ``409
    extraction_dependencies_missing`` (the ``extraction`` optional
    dependency extra is not installed) — see :func:`_extraction_readiness`.

    Deduped on the STABLE per-connection idempotency key
    (:func:`_extraction_idempotency_key`) also used by the scheduled sweep
    below, so a manual trigger and a scheduled run can never both be in
    flight for the same connection. ``enqueue()``'s own ``"deduped"``
    return value (not a pre-check peek — see ``app/api/sync.py::
    trigger_sync``'s docstring for why a peek races a concurrent call)
    decides 202 vs. ``409 extraction_already_running``.
    """
    row = _sharepoint_connection_or_404(connection_id)

    usable, error = _extraction_readiness()
    if not usable:
        raise HTTPException(status_code=409, detail=error)

    from src.repositories import jobs_repo

    payload: Dict[str, Any] = {"connection_id": connection_id}
    if options is not None:
        # Only the keys the admin actually set ride in the payload — an
        # absent key means "the configured value", and the handler/crawler
        # already document exactly that fallback for each.
        if options.concurrency is not None:
            payload["concurrency"] = options.concurrency
        if options.timeout_s is not None:
            payload["timeout_s"] = options.timeout_s
        if options.resync:
            payload["resync"] = True
        if options.force_reprocess:
            payload["force_reprocess"] = True

    job = jobs_repo().enqueue(
        "corpus-extraction",
        payload,
        idempotency_key=_extraction_idempotency_key(connection_id),
    )
    if job["deduped"]:
        raise HTTPException(
            status_code=409,
            detail={"error": "extraction_already_running", "job_id": job["id"]},
        )

    _record_extraction_dispatch(row, job["id"])
    logger.info("sharepoint connection %s: extraction job %s enqueued (manual trigger)", connection_id, job["id"])
    return {"job_id": job["id"], "status": job["status"]}


def _acl_sync_idempotency_key(connection_id: str) -> str:
    """A STABLE per-connection idempotency key for the ``sharepoint-acl-sync``
    job — mirrors :func:`_extraction_idempotency_key`'s shape so a manual
    "sync now" and any other in-flight run for the same connection can never
    both be queued at once."""
    return f"sharepoint-acl-sync:{connection_id}"


@router.post("/connections/{connection_id}/acl-sync", status_code=202)
async def trigger_acl_sync(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Admin-triggered one-off run of the ``sharepoint-acl-sync`` job for
    this connection (spec §5.1's "sync now" action; 2026-08-30 plan, Task
    5) — enqueues ``connectors.sharepoint.acl_sync.run_acl_sync`` (via
    ``app/worker/kinds.py::_run_sharepoint_acl_sync``) with
    ``{"connection_id": connection_id}``, the SAME mechanics as
    :func:`trigger_extraction`.

    404 on an unknown/non-sharepoint connection BEFORE any other work. The
    router-level ``sharepoint.enabled`` gate (``_require_sharepoint_enabled``
    above) already refuses the whole surface with ``409 feature_disabled``
    when the connector is off, so there is no separate per-route check here.

    Deduped on a STABLE per-connection idempotency key
    (:func:`_acl_sync_idempotency_key`) — a second trigger while one is
    already queued/running for this connection gets ``409
    acl_sync_already_running`` instead of a second job.
    """
    _sharepoint_connection_or_404(connection_id)

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue(
        "sharepoint-acl-sync",
        {"connection_id": connection_id},
        idempotency_key=_acl_sync_idempotency_key(connection_id),
    )
    if job["deduped"]:
        raise HTTPException(
            status_code=409,
            detail={"error": "acl_sync_already_running", "job_id": job["id"]},
        )

    logger.info("sharepoint connection %s: acl-sync job %s enqueued (manual trigger)", connection_id, job["id"])
    return {"job_id": job["id"], "status": job["status"]}


def _sweep_idempotency_key(connection_id: str) -> str:
    """A STABLE per-connection idempotency key for the
    ``sharepoint-subtree-sweep`` job — mirrors
    :func:`_acl_sync_idempotency_key`'s shape so a manual "re-check subtrees
    now" and any other in-flight sweep for the same connection can never
    both be queued at once."""
    return f"sharepoint-subtree-sweep:{connection_id}"


@router.post("/connections/{connection_id}/subtree-sweep", status_code=202)
async def trigger_subtree_sweep(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Admin-triggered one-off run of the ``sharepoint-subtree-sweep`` job
    for this connection (2026-08-31 plan, Task 8's "re-check subtrees now")
    — enqueues ``connectors.sharepoint.acl_sync.run_subtree_sweep`` (via
    ``app/worker/kinds.py::_run_sharepoint_subtree_sweep``) with
    ``{"connection_id": connection_id}``, the SAME mechanics as
    :func:`trigger_acl_sync`. The explicit-connection payload also bypasses
    the job's own per-connection ``acl_sync.sweep_interval_days`` due-guard
    (:func:`connectors.sharepoint.acl_sync._sweep_due`), so this always
    triggers a real sweep, never a same-day no-op.

    404 on an unknown/non-sharepoint connection BEFORE any other work. The
    router-level ``sharepoint.enabled`` gate (``_require_sharepoint_enabled``
    above) already refuses the whole surface with ``409 feature_disabled``
    when the connector is off, so there is no separate per-route check here.

    Deduped on a STABLE per-connection idempotency key
    (:func:`_sweep_idempotency_key`) — a second trigger while one is already
    queued/running for this connection gets ``409 sweep_already_running``
    instead of a second job.
    """
    _sharepoint_connection_or_404(connection_id)

    from src.repositories import jobs_repo

    job = jobs_repo().enqueue(
        "sharepoint-subtree-sweep",
        {"connection_id": connection_id},
        idempotency_key=_sweep_idempotency_key(connection_id),
    )
    if job["deduped"]:
        raise HTTPException(
            status_code=409,
            detail={"error": "sweep_already_running", "job_id": job["id"]},
        )

    logger.info("sharepoint connection %s: subtree-sweep job %s enqueued (manual trigger)", connection_id, job["id"])
    return {"job_id": job["id"], "status": job["status"]}


class FactsExtractionRunOptions(BaseModel):
    """Optional per-run overrides for one manual facts-extraction trigger.
    The body itself is optional — a bare ``POST`` runs over the whole
    corpus with the configured budget, mirroring :class:`ExtractionRunOptions`'s
    "absent means configured" contract for the crawl trigger."""

    doc_ids: Optional[List[str]] = Field(
        None,
        description=(
            "Narrow the pass to these documents only (corpus_file_sources.source_doc_id) "
            "— the 'test one document against a prompt change' path, never persisted "
            "beyond this one run."
        ),
    )
    timeout_s: Optional[int] = Field(
        None,
        ge=0,
        le=86400,
        description=(
            "Hard ceiling for this one run, seconds (0 = unbounded). Its OWN budget — "
            "see connectors.sharepoint.facts_extraction.run_standalone_facts_extraction's "
            "docstring for why this is never the crawl's extraction.timeout_s."
        ),
    )


def _facts_extraction_idempotency_key(connection_id: str) -> str:
    """A STABLE per-connection idempotency key for the
    ``sharepoint-facts-extraction`` job — mirrors :func:`_sweep_idempotency_key`'s
    shape so a manual "run facts extraction now" and any other in-flight
    pass for the same connection can never both be queued at once, and so
    this job kind can never dedup against ``corpus-extraction`` or any
    other sibling kind for the same connection (different prefix)."""
    return f"sharepoint-facts-extraction:{connection_id}"


def _facts_extraction_readiness() -> Tuple[bool, Optional[Dict[str, str]]]:
    """Whether a standalone facts-extraction run can be triggered right now
    — checked BEFORE enqueue, same reasoning as :func:`_extraction_readiness`
    above: an admin who has not yet turned on the two facts-specific
    switches gets ``409 facts_extraction_disabled`` immediately, naming the
    exact fix, instead of a job that fails 30+ minutes later in a worker.

    Unlike :func:`_extraction_readiness`, this does NOT re-check
    ``sharepoint.enabled`` (the router-level gate already refuses the whole
    surface before this ever runs) or the ``extraction`` optional-dependency
    probe (facts extraction never touches ``markitdown``/``pypdfium2`` — it
    reads already-converted markdown out of ``corpus_files``, not raw
    documents).

    The refusal names the config key that is off in ``switch`` (additive to
    ``error``/``message``): the source card's "Extract facts now" button
    reads the same verdict through ``app/web/router.py``'s pipeline cell and
    renders that key as its disabled reason, so the UI and the 409 can never
    disagree about what an admin has to flip.
    """
    from connectors.sharepoint.facts_extraction import facts_extraction_enabled, facts_surface_enabled

    if not facts_extraction_enabled():
        return False, {
            "error": "facts_extraction_disabled",
            "switch": "extraction.facts.enabled",
            "message": (
                "extraction.facts.enabled is off — turn it on in /admin/server-config "
                "before running a facts-extraction pass (it is the cost gate: this stage "
                "spends model tokens per document)."
            ),
        }
    if not facts_surface_enabled():
        return False, {
            "error": "facts_extraction_disabled",
            "switch": "facts.enabled",
            "message": (
                "facts.enabled is off — turn it on before running a facts-extraction pass "
                "(writing claims into a surface nothing can read is never useful)."
            ),
        }
    return True, None


@router.post("/connections/{connection_id}/facts-extract", status_code=202)
async def trigger_facts_extraction(
    connection_id: str,
    options: Optional[FactsExtractionRunOptions] = None,
    _user: dict = Depends(require_admin),
):
    """Admin/ops-triggered one-off run of the ``sharepoint-facts-extraction``
    job for this connection — build the fact graph over whatever this
    connection's collections ALREADY hold, without running a crawl first
    (the operator question "how do we get the fact graph populated with
    what we already have?", which previously had no answer but "re-run the
    whole crawl"). Enqueues ``connectors.sharepoint.facts_extraction
    .run_standalone_facts_extraction`` (via
    ``app/worker/kinds.py::_run_sharepoint_facts_extraction``) with
    ``{"connection_id": connection_id}`` plus, only when set, ``doc_ids``
    and ``timeout_s`` from an optional :class:`FactsExtractionRunOptions`
    body — the SAME "absent means configured" mechanics as
    :func:`trigger_extraction`'s ``ExtractionRunOptions`` above.

    404 on an unknown/non-sharepoint connection BEFORE the facts readiness
    gate below (same ordering as :func:`trigger_extraction`). Then refuses
    cleanly — never a job that fails later in a worker — with ``409
    facts_extraction_disabled`` when either of the stage's two switches
    (``extraction.facts.enabled``, ``facts.enabled``) is off; see
    :func:`_facts_extraction_readiness`.

    Deduped on the STABLE per-connection idempotency key
    (:func:`_facts_extraction_idempotency_key`), distinct from every other
    job kind's own key for the same connection — a facts-extraction trigger
    and a crawl trigger (or an ACL sync, or a subtree sweep) can always run
    side by side. ``enqueue()``'s own ``"deduped"`` return value decides
    202 vs. ``409 facts_extraction_already_running``.
    """
    _sharepoint_connection_or_404(connection_id)

    usable, error = _facts_extraction_readiness()
    if not usable:
        raise HTTPException(status_code=409, detail=error)

    from src.repositories import jobs_repo

    payload: Dict[str, Any] = {"connection_id": connection_id}
    if options is not None:
        if options.doc_ids is not None:
            payload["doc_ids"] = options.doc_ids
        if options.timeout_s is not None:
            payload["timeout_s"] = options.timeout_s

    job = jobs_repo().enqueue(
        "sharepoint-facts-extraction",
        payload,
        idempotency_key=_facts_extraction_idempotency_key(connection_id),
    )
    if job["deduped"]:
        raise HTTPException(
            status_code=409,
            detail={"error": "facts_extraction_already_running", "job_id": job["id"]},
        )

    logger.info("sharepoint connection %s: facts-extraction job %s enqueued (manual trigger)", connection_id, job["id"])
    return {"job_id": job["id"], "status": job["status"]}


@router.post("/extraction/run-due")
async def run_due_extraction(
    _user: dict = Depends(require_admin),
):
    """Scheduler-driven sweep (TCRD-226): fires ``corpus-extraction`` for
    every SharePoint connection whose extraction cadence
    (``extraction.schedule`` — a single instance-wide setting in the
    ``extraction:`` config block, applied independently to each
    connection's own ``last_run_at``) says it is due. Same shape as
    ``POST /api/v1/agents/run-due`` (walk + per-row due-check + enqueue
    into an EXISTING job kind) — no second scheduling mechanism.
    Scheduler row: ``extraction-run-due`` in
    ``services/scheduler/__main__.py``, registered only when
    ``extraction.schedule`` is configured (absent/empty = off, same
    default posture as ``sharepoint.enabled``).

    A clean, typed no-op (never an error) when the feature isn't usable —
    ``sharepoint.enabled`` is false, the ``extraction`` extra is missing, or
    no schedule is configured — since this endpoint, once registered, fires
    UNCONDITIONALLY on its own cadence; the JOB HANDLER
    (``_run_corpus_extraction``) raises on the same conditions because a
    ``corpus-extraction`` job only ever exists because something explicitly
    enqueued it, but this sweep is what decides whether to enqueue at all.
    """
    usable, error = _extraction_readiness()
    schedule = _extraction_schedule_config()
    if not usable or not schedule:
        return {
            "dispatched": [],
            "count": 0,
            "skipped": True,
            "reason": (error or {}).get("error") if not usable else "no_schedule_configured",
        }

    now = datetime.now(timezone.utc)
    dispatched: List[str] = []
    for row in source_connections_repo().list(source_type="sharepoint"):
        try:
            if _dispatch_extraction_if_due(row, schedule, now):
                dispatched.append(row["id"])
        except Exception:
            # Never let one bad connection abort the sweep — mirrors
            # app/api/agent_schedules.py::run_due_agent_schedules.
            logger.exception("extraction:run-due — connection %s failed; continuing sweep", row.get("id"))

    return {"dispatched": dispatched, "count": len(dispatched)}


# ---------------------------------------------------------------------------
# Observed-changes feed (2026-08-30) — own section, appended at the end of
# the file on purpose to minimize merge conflicts with the rest of this
# router (see the module's own maintenance note at the top).
#
# "What changed between two timestamps" for a SharePoint connection, derived
# from the append-only ``corpus_file_events`` log
# (``src/repositories/corpus_file_events_pg.py``) that ``app/api/
# collections.py``'s upload/delete handlers already write. See that
# repository's module docstring for why a plain event log is needed here at
# all rather than deriving purely from ``corpus_files``' own timestamps: a
# snapshot table cannot answer "was the last touch an update or a rename"
# after the fact, and a hard-deleted row leaves nothing to snapshot.
# ---------------------------------------------------------------------------


def _ensure_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a caller-supplied ``since``/``until`` to an aware UTC
    datetime — a bare ISO string with no offset parses as naive, and this
    value is about to be compared against Postgres's own tz-aware
    ``observed_at`` column (mirrors the idiom in ``app/api/collections.py::
    _is_stale_processing`` / ``app/auth/pat_resolver.py``)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@router.get("/connections/{connection_id}/changes")
async def connection_changes(
    connection_id: str,
    since: Optional[datetime] = Query(default=None, description="Inclusive lower bound on observed_at (ISO 8601)."),
    until: Optional[datetime] = Query(default=None, description="Inclusive upper bound on observed_at (ISO 8601)."),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: Optional[str] = Query(default=None, description="Opaque token from a prior page's next_cursor."),
    _user: dict = Depends(require_admin),
):
    """Observed content changes for this connection between ``since`` and
    ``until`` — "what changed", for a consumer that would otherwise have to
    diff the whole corpus by hand or re-read everything on every check.

    ``observed_at`` is when AGNES learned of the change (the moment its own
    upload/delete handler recorded it), never a live query against
    Microsoft Graph — this endpoint never calls SharePoint. A document
    edited at the source but not yet re-crawled therefore does NOT appear
    here; it will, once a future sync uploads the new content and this
    feed's window covers that upload's ``observed_at``. Both bounds are
    optional and inclusive; omitting ``since`` reads from the beginning of
    the log, omitting ``until`` reads to now.

    Each item is ``{change, name, path, collection_id, file_id,
    source_stable_id, source_modified, observed_at, ingest_run_id}``:

    * ``change`` — ``added`` (new document), ``updated`` (same identity,
      content/``sha256`` differs), ``renamed`` (same identity and content,
      ``name``/``path`` differs — detectable ONLY when the upload carried a
      ``source_stable_id``, since a bare path match has no identity that
      survives a path change), or ``deleted``.
    * ``source_modified`` — always ``null`` today: no upload path persists
      the source's own modified-timestamp yet (see ``document_dates`` in
      ``app/api/collections.py::upload_files``, accepted but not yet
      stored). Reserved so a future producer that DOES supply it does not
      need a contract change.
    * ``ingest_run_id`` — always ``null`` today: the crawler invokes this
      connection's upload endpoint out-of-process (spec's producer
      subprocess), which carries no run identifier through to
      ``corpus_file_events``. Reserved for the same forward-compatibility
      reason as ``source_modified``.

    Ordering is ``(observed_at, id)`` ascending — deterministic even when
    several events share a timestamp, which pagination depends on:
    ``next_cursor`` (``null`` at the end of the window) resumes strictly
    after the last item's position. A file that changed more than once in
    the window appears once per transition, in order — this is an event
    feed, not a snapshot of current state.

    Scoped to the connection's OWN collections (every ``collection_id`` any
    of its confirmed scopes maps to, per ``config.scopes`` — see
    :func:`_scopes`); a connection with no confirmed scopes yet returns an
    empty page rather than an error. Only top-level file upload/delete via
    ``/api/collections/{id}/files*`` are tracked — a member added or removed
    from inside an uploaded zip bundle is not (see
    ``app/api/collections.py::_purge_children_and_content``).

    Malformed ``cursor`` is a typed **400** (``invalid_cursor``), never a
    500 from a bad row-value comparison downstream.
    """
    row = _sharepoint_connection_or_404(connection_id)
    collection_ids = sorted({s["collection_id"] for s in _scopes(row) if s.get("collection_id")})

    since_utc = _ensure_utc(since)
    until_utc = _ensure_utc(until)

    try:
        events, next_cursor = corpus_file_events_repo().list_for_corpus_ids(
            collection_ids,
            since=since_utc,
            until=until_utc,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_cursor", "message": str(exc)}) from exc

    items = [
        {
            "change": event["change"],
            "name": event["name"],
            "path": event.get("path"),
            "collection_id": event["corpus_id"],
            "file_id": event.get("file_id"),
            "source_stable_id": event.get("source_stable_id"),
            "source_modified": None,
            "observed_at": event["observed_at"],
            "ingest_run_id": None,
        }
        for event in events
    ]
    return {
        "connection_id": connection_id,
        "since": since_utc.isoformat() if since_utc else None,
        "until": until_utc.isoformat() if until_utc else None,
        "items": items,
        "next_cursor": next_cursor,
    }
