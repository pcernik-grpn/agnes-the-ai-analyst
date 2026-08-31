"""Admin REST API for the SharePoint file-source connect wizard (spec
2026-08-27 §13.2 — "Connect wizard (file source): three steps").

Surface (all gated by ``Depends(require_admin)``):

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
  GET    /api/admin/sharepoint/connections/{id}/corpus-map   — producer handoff: the
                                                                ``{"<site>"|"<site>/<folder path>":
                                                                collection_id}`` mapping
                                                                ``ship_to_agnes.py --corpus-map``
                                                                consumes, in the producer
                                                                resolver's own key shape. 409
                                                                ``corpus_map_ambiguous`` rather
                                                                than a best-guess map. Per-scope
                                                                ``anonymize`` is NOT in this shape —
                                                                a producer that needs it reads the
                                                                sibling ``GET .../scopes`` endpoint
                                                                instead (each row already carries
                                                                ``anonymize``). Agnes's own
                                                                ``corpus-extraction`` job handler
                                                                (``app/worker/kinds.py``) builds an
                                                                anonymize-scoped mapping the same
                                                                way, for the same reason: this
                                                                endpoint's contract does not move.
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
                                                                worker) when ``extraction.enabled``
                                                                is off or no producer is configured
                                                                — the same two gates
                                                                ``app/worker/kinds.py::
                                                                _run_corpus_extraction`` itself
                                                                checks. Deduped on a stable
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
                                                                ``.../extract`` above; refuses with
                                                                ``409 feature_disabled`` when
                                                                ``acl_mirroring.enabled`` is off.

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
wizard's own bookkeeping as well as the eventual document anchor).
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.access import require_admin, require_admin_or_producer_connection
from app.auth.public_url import public_base_url
from app.auth.session_principal import ProducerPrincipal
from app.resource_types import ResourceType
from src.audit_helpers import log_safe
from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL
from connectors.sharepoint.graph_client import (
    SharePointGraphError,
    build_folder_matcher,
    certificate_metadata,
    get_app_token,
    get_site_by_path,
    list_drives,
    list_item_children,
    list_root_children,
    list_sites,
    probe_unique_permissions,
    search_folders,
)
from connectors.sharepoint.corpus_map import CorpusMapError, producer_corpus_map
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from src.repositories import (
    corpus_file_events_repo,
    file_corpora_repo,
    resource_grants_repo,
    source_connections_repo,
    user_groups_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sharepoint", tags=["admin"])


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
SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS = ("scopes", "extraction", "webhook_secret")


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
        # the crawl, each as {item_id, path} (never the raw {detected_at}
        # timestamp — the wizard doesn't need it), plus whether an admin has
        # overridden the exclusion for this scope (`should_not` mode only).
        "excluded_subtree_count": len(scope.get("excluded_subtrees") or []),
        "excluded_subtrees": [
            {"item_id": item.get("item_id"), "path": item.get("path")}
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


def _extraction_readiness() -> Tuple[bool, Optional[Dict[str, str]]]:
    """Whether the ``corpus-extraction`` job kind can actually run right now
    — the SAME two gates ``app/worker/kinds.py::_run_corpus_extraction``
    itself checks (``extraction.enabled`` + a configured producer command/
    module), read here so an admin (or the scheduled sweep below) finds out
    BEFORE a job is queued rather than 30 minutes later when a worker claims
    it and the handler raises.

    Both gates already honor a deploy-time env override ahead of
    ``instance.yaml`` — ``AGNES_EXTRACTION_ENABLED`` (via
    ``feature_enabled`` below) and ``AGNES_EXTRACTION_PRODUCER_COMMAND`` /
    ``AGNES_EXTRACTION_PRODUCER_MODULE`` (inside
    :func:`app.worker.kinds._extraction_producer_argv`, called below) — so
    this function and the handler it mirrors see the identical truth
    regardless of which source (env or yaml) an instance configures
    through.

    Returns ``(True, None)`` when usable, or ``(False, {"error": ...,
    "message": ...})`` naming the exact fix.
    """
    from app.instance_config import feature_enabled

    if not feature_enabled("extraction", "enabled", env_var="AGNES_EXTRACTION_ENABLED", default=False):
        return False, {
            "error": "extraction_disabled",
            "message": "extraction.enabled is false — enable it in instance.yaml (or AGNES_EXTRACTION_ENABLED) first.",
        }

    from app.worker.kinds import _extraction_producer_argv

    if _extraction_producer_argv() is None:
        return False, {
            "error": "extraction_producer_not_configured",
            "message": (
                "No producer configured — set extraction.producer.command or "
                "extraction.producer.module in instance.yaml (or "
                "AGNES_EXTRACTION_PRODUCER_COMMAND / AGNES_EXTRACTION_PRODUCER_MODULE)."
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
    no scheduled sweep (mirrors ``extraction.enabled``'s own default)."""
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
    user=Depends(require_admin_or_producer_connection("{connection_id}")),
):
    """The wizard's step-2/3 source of truth: every confirmed scope row,
    enriched with its collection and current group grants.

    Also the corpus-extraction producer's own callback read (TCRD-...):
    a ``ProducerPrincipal`` scoped to THIS connection may call this too
    (see ``require_admin_or_producer_connection``) — self-audited here
    (``sharepoint_connection.scopes_read``, ``client_kind="producer"``)
    since a restricted principal's identity is never stashed onto
    ``request.state.user``, so the generic audit-fallback middleware would
    otherwise see no attributable caller and write nothing at all.
    """
    row = _sharepoint_connection_or_404(connection_id)
    declared = _latest_run_anonymized_corpus_ids()  # one lookup for the whole list, not per row
    if isinstance(user, ProducerPrincipal):
        log_safe(
            action="sharepoint_connection.scopes_read",
            resource=connection_id,
            client_kind="producer",
        )
    return {"items": [_scope_out(s, declared, row) for s in _scopes(row)]}


@router.post("/connections/{connection_id}/scopes", status_code=201)
async def confirm_scope(
    connection_id: str,
    body: ConfirmScopeBody,
    user: dict = Depends(require_admin),
):
    """Confirm one selected site/library/folder as a scope.

    Creates its collection on first confirmation; re-confirming the same
    ``source_scope_id`` reuses that same collection (idempotent) and updates
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

    if existing is not None:
        collection_id = existing["collection_id"]
        existing["display_path"] = body.display_path
        existing["anonymize"] = body.anonymize
        existing["access_mode"] = body.access_mode
        existing["drive_id"] = body.drive_id
        existing["include_excluded_subtrees"] = body.include_excluded_subtrees
    else:
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


@router.delete("/connections/{connection_id}/scopes", status_code=204)
async def remove_scope(
    connection_id: str,
    source_scope_id: str,
    _user: dict = Depends(require_admin),
):
    """Unselect a scope — an explicit exclusion (spec §13.2: "unselected rows
    are explicit exclusions"). Removes the wizard's own bookkeeping row only;
    any collection already created for it is left alone (deleting a
    collection is a separate, deliberate operation, not a side effect of
    unchecking a wizard row)."""
    row = _sharepoint_connection_or_404(connection_id)
    scopes = _scopes(row)
    remaining = [s for s in scopes if s.get("source_scope_id") != source_scope_id]
    if len(remaining) == len(scopes):
        raise HTTPException(status_code=404, detail="scope_not_found")
    # See SHAREPOINT_SERVER_WRITTEN_CONFIG_KEYS's docstring above if you are
    # adding a new key here rather than editing this one.
    new_config = {**(row.get("config") or {}), "scopes": remaining}
    source_connections_repo().update(connection_id, config=new_config)


@router.get("/connections/{connection_id}/corpus-map")
async def corpus_map(
    connection_id: str,
    user=Depends(require_admin_or_producer_connection("{connection_id}")),
):
    """Producer handoff (spec §13.2 / item 3): the corpus map the external
    crawl pipeline reads via ``ship_to_agnes.py --corpus-map``. Keys are in
    the producer resolver's OWN shape — ``"<site display name>"`` or
    ``"<site display name>/<drive-relative folder path>"`` — built by the
    same shared translation the in-Agnes ``corpus-extraction`` job handler
    uses for its ``AGNES_EXTRACTION_CORPUS_MAP`` env handoff
    (``connectors/sharepoint/corpus_map.py``), so the two surfaces cannot
    drift. The earlier flat ``{source_scope_id: collection_id}`` shape was
    unusable for routing: the resolver matches keys against crawler rows'
    site/path components, which a Graph scope id never equals.

    ``409 corpus_map_ambiguous`` when the confirmed scopes cannot form an
    unambiguous map (e.g. a site scope plus a drive scope of the same
    site) — never a best-guess map.

    Deliberately does NOT carry ``anonymize`` — a producer that needs to
    know WHICH scopes to anonymize reads ``GET .../scopes`` instead (each
    row already carries ``anonymize``); Agnes's own ``corpus-extraction``
    job handler does the equivalent lookup internally
    (``app/worker/kinds.py::_anonymize_marked_scope_map``).

    THIS is the primary callback the corpus-extraction producer itself
    calls (TCRD-...): a ``ProducerPrincipal`` scoped to THIS connection may
    call it too (see ``require_admin_or_producer_connection``) —
    self-audited here (``sharepoint_connection.corpus_map_read``,
    ``client_kind="producer"``) for the same reason ``list_scopes`` above
    self-audits its own producer branch.
    """
    row = _sharepoint_connection_or_404(connection_id)
    try:
        mapping = producer_corpus_map(_scopes(row))
    except CorpusMapError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "corpus_map_ambiguous", "message": str(exc)},
        ) from exc
    if isinstance(user, ProducerPrincipal):
        log_safe(
            action="sharepoint_connection.corpus_map_read",
            resource=connection_id,
            client_kind="producer",
        )
    return mapping


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
    secret and return the receiver URL alongside it, so an operator can run
    the producer's own ``subscriptions.py create --url <webhook_url>``
    against this connection without hand-assembling either value.

    Always mints a FRESH random secret — there is no "read the current
    one" verb, matching the outbound-webhook pattern
    (``app/api/agent_webhooks.py``): a caller who wants to see it again
    calls this again, which also rotates it, invalidating whatever Graph
    subscription was signed with the old value (the operator must then
    re-point the subscription's ``clientState``, or simply create a new
    subscription — Agnes does not manage Graph subscriptions itself).

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


@router.post("/connections/{connection_id}/extract", status_code=202)
async def trigger_extraction(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Admin-triggered one-off extraction run for this connection
    (TCRD-226) — enqueues the existing ``corpus-extraction`` job kind
    (``app/worker/kinds.py::_run_corpus_extraction``) with
    ``{"connection_id": connection_id}``, the exact payload shape that
    handler documents.

    404 on an unknown/non-sharepoint connection BEFORE any other work.
    Then refuses cleanly (never a job that fails 30 minutes later in a
    worker) when the feature isn't usable: ``409 extraction_disabled``
    (``extraction.enabled`` is false) or ``409
    extraction_producer_not_configured`` (neither ``extraction.producer
    .command`` nor ``.module`` is set) — see :func:`_extraction_readiness`.

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

    job = jobs_repo().enqueue(
        "corpus-extraction",
        {"connection_id": connection_id},
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

    404 on an unknown/non-sharepoint connection BEFORE any other work. Then
    refuses cleanly with ``409 feature_disabled`` when ``acl_mirroring
    .enabled`` is off — the job handler itself would just no-op (spec §5.1
    "the scheduler enqueues this kind unconditionally"), but a manual
    trigger should tell the admin why nothing happened rather than return a
    202 for a run that will do nothing.

    Deduped on a STABLE per-connection idempotency key
    (:func:`_acl_sync_idempotency_key`) — a second trigger while one is
    already queued/running for this connection gets ``409
    acl_sync_already_running`` instead of a second job.
    """
    from app.instance_config import feature_enabled

    _sharepoint_connection_or_404(connection_id)

    if not feature_enabled("acl_mirroring", "enabled", env_var="AGNES_ACL_MIRRORING_ENABLED", default=False):
        raise HTTPException(status_code=409, detail={"error": "feature_disabled"})

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
    default posture as ``extraction.enabled``).

    A clean, typed no-op (never an error) when the feature isn't usable —
    ``extraction.enabled`` is false, no producer is configured, or no
    schedule is configured — since this endpoint, once registered, fires
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
