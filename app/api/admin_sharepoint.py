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
  GET    /api/admin/sharepoint/connections/{id}/corpus-map   — producer handoff: the flat
                                                                ``{source_scope_id: collection_id}``
                                                                mapping ``ship_to_agnes.py
                                                                --corpus-map`` consumes. Per-scope
                                                                ``anonymize`` is NOT in this shape
                                                                (kept flat/backward-compatible) —
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

Scope rows live inside the connection's own ``config.scopes`` — a JSON list,
no new table (``source_connections.config`` is already a JSON column on both
backends). Each row is exactly ``{source_scope_id, display_path, anonymize,
collection_id}`` (spec §13.2); group grants are NOT duplicated here — they
are ordinary ``resource_grants`` rows on the collection, same primitive
``/admin/access`` already reads (spec: "facts are never granted... zero new
grant type").

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.resource_types import ResourceType
from connectors.sharepoint.graph_client import (
    SharePointGraphError,
    build_folder_matcher,
    certificate_metadata,
    get_app_token,
    list_drives,
    list_item_children,
    list_root_children,
    list_sites,
    probe_unique_permissions,
    search_folders,
)
from connectors.sharepoint.settings import SharePointSettingsError, resolve_sharepoint_settings
from src.repositories import (
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


class ConfirmScopeBody(BaseModel):
    source_scope_id: str = Field(..., min_length=1)
    display_path: str = Field(..., min_length=1)
    anonymize: bool = False
    # Step 3: applied as ordinary `resource_grants` rows on the collection —
    # never stored on the scope row itself (see module docstring). ``None``
    # (the field omitted) and ``[]`` mean DIFFERENT things: omitted is "this
    # call is not about sharing" (step 2 confirming a scope, a rename, an
    # anonymize toggle), while an explicit empty list is the admin unticking
    # the last group — the very state the row's "indexed but invisible"
    # warning describes, so it has to be honoured rather than read as silence.
    group_ids: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        return await get_app_token(settings.tenant_id, settings.client_id, settings.private_key)
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


def _scope_out(scope: Dict[str, Any], declared_corpus_ids: Optional[set] = None) -> Dict[str, Any]:
    collection = file_corpora_repo().get(scope.get("collection_id") or "")
    group_ids = _group_ids_for_collection(scope.get("collection_id") or "")
    if declared_corpus_ids is None:
        declared_corpus_ids = _latest_run_anonymized_corpus_ids()
    anonymize = bool(scope.get("anonymize"))
    return {
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


def _extraction_readiness() -> Tuple[bool, Optional[Dict[str, str]]]:
    """Whether the ``corpus-extraction`` job kind can actually run right now
    — the SAME two gates ``app/worker/kinds.py::_run_corpus_extraction``
    itself checks (``extraction.enabled`` + a configured producer command/
    module), read here so an admin (or the scheduled sweep below) finds out
    BEFORE a job is queued rather than 30 minutes later when a worker claims
    it and the handler raises.

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
                "extraction.producer.module in instance.yaml."
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
    if item_id is not None:
        if not drive_id:
            raise HTTPException(status_code=422, detail={"error": "item_id_requires_drive_id"})
        _validate_graph_id(item_id, "item_id")
    row = _sharepoint_connection_or_404(connection_id)
    token = await _resolved_token(row)
    try:
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

    Response: ``{matches: [{item_id, drive_id, display_path}], visited,
    truncated, hint}``. ``truncated`` is ``True`` whenever a cap is what
    stopped the walk — never a silently partial result; ``visited`` is how
    many "list children" calls it took to get there; ``hint`` is a short,
    actionable string (scope the search, narrow the pattern) when
    ``truncated`` is ``True``, else ``null``.
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
    _user: dict = Depends(require_admin),
):
    """The wizard's step-2/3 source of truth: every confirmed scope row,
    enriched with its collection and current group grants."""
    row = _sharepoint_connection_or_404(connection_id)
    declared = _latest_run_anonymized_corpus_ids()  # one lookup for the whole list, not per row
    return {"items": [_scope_out(s, declared) for s in _scopes(row)]}


@router.post("/connections/{connection_id}/scopes", status_code=201)
async def confirm_scope(
    connection_id: str,
    body: ConfirmScopeBody,
    user: dict = Depends(require_admin),
):
    """Confirm one selected site/library/folder as a scope.

    Creates its collection on first confirmation; re-confirming the same
    ``source_scope_id`` reuses that same collection (idempotent) and updates
    ``display_path``/``anonymize`` in place — a rename or move in the source
    does not fork a second collection (§6 applied to the wizard's own
    bookkeeping). ``group_ids``, **if the field is present**, is the complete
    set of groups for this collection (step 3): listed groups are granted,
    and any other group's grant on this collection is revoked. The wizard's
    checkboxes are pre-checked from the grants that exist and its row warns
    the moment the last one is unticked, so the screen already promises that
    unticking removes access — making the handler additive-only meant the
    admin was shown a revocation that never happened. Omitting the field
    touches no grant at all, which is what keeps a rename or an anonymize
    toggle from stripping access as a side effect.
    """
    row = _sharepoint_connection_or_404(connection_id)

    if body.group_ids:
        groups_repo = user_groups_repo()
        unknown = [gid for gid in body.group_ids if groups_repo.get(gid) is None]
        if unknown:
            raise HTTPException(status_code=400, detail={"error": "invalid_group_id", "group_ids": unknown})

    scopes = _scopes(row)
    existing = next((s for s in scopes if s.get("source_scope_id") == body.source_scope_id), None)

    if existing is not None:
        collection_id = existing["collection_id"]
        existing["display_path"] = body.display_path
        existing["anonymize"] = body.anonymize
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
            }
        )

    new_config = {**(row.get("config") or {}), "scopes": scopes}
    source_connections_repo().update(connection_id, config=new_config)

    if body.group_ids is not None:
        wanted = set(body.group_ids)
        grants = resource_grants_repo()
        for group_id in wanted:
            grants.ensure_grant(
                group_id,
                ResourceType.COLLECTION.value,
                collection_id,
                assigned_by=user.get("id"),
            )
        # Revoke what was unticked. Scoped to grants on THIS collection, so a
        # group's access to anything else is untouched.
        for grant in grants.list_all(resource_type=ResourceType.COLLECTION.value):
            if grant.get("resource_id") == collection_id and grant.get("group_id") not in wanted:
                grants.delete(grant["id"])

    logger.info(
        "sharepoint connection %s: scope %s confirmed -> collection %s",
        connection_id,
        body.source_scope_id,
        collection_id,
    )
    updated_row = next(s for s in scopes if s.get("source_scope_id") == body.source_scope_id)
    return _scope_out(updated_row)


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
    new_config = {**(row.get("config") or {}), "scopes": remaining}
    source_connections_repo().update(connection_id, config=new_config)


@router.get("/connections/{connection_id}/corpus-map")
async def corpus_map(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Producer handoff (spec §13.2 / item 3): the flat
    ``{source_scope_id: collection_id}`` mapping the external crawl pipeline
    reads via ``ship_to_agnes.py --corpus-map`` until crawling moves inside
    Agnes. Not wrapped in an envelope key — the producer consumes this
    verbatim as the mapping itself.

    Deliberately does NOT carry ``anonymize`` — that would break this
    endpoint's flat, backward-compatible shape. A producer that needs to
    know WHICH scopes to anonymize reads ``GET .../scopes`` instead (each
    row already carries ``anonymize``); Agnes's own ``corpus-extraction``
    job handler does the equivalent lookup internally
    (``app/worker/kinds.py::_anonymize_marked_scope_map``)."""
    row = _sharepoint_connection_or_404(connection_id)
    return {s["source_scope_id"]: s["collection_id"] for s in _scopes(row) if s.get("source_scope_id")}


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
    return certificate_metadata(settings.private_key)


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
