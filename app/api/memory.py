"""Corporate memory endpoints — knowledge items, voting, governance admin, contradictions."""

import asyncio
import json
import logging
import uuid
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
import duckdb

from app.auth.dependencies import get_current_user, _get_db
from app.auth.access import require_admin, is_user_admin, can_access, can_access_session
from app.auth.session_principal import PRINCIPAL_TYPES

from src.audit_helpers import identity_for_audit, log_safe
from src.knowledge_directive_scan import DELIVERY_NOTICE, scan_item
from src.repositories import (
    audit_repo,
    knowledge_repo,
    memory_domains_repo,
    resource_grants_repo,
    usage_repo,
    user_group_members_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memory", tags=["memory"])

# v49: ``mandatory`` is no longer a lifecycle status — Required tier rides on
# ``knowledge_items.is_required``. ``status`` covers lifecycle only (pending,
# approved, rejected, revoked, expired).
VALID_STATUSES = ["pending", "approved", "rejected", "revoked", "expired"]

BUNDLE_TOKEN_BUDGET = 6000
# Rough chars-per-token estimate (conservative).
_CHARS_PER_TOKEN = 4

# v49: domain set is no longer a hardcoded enum — it lives in the
# ``memory_domains`` table and is administrable via /admin/memory-domains.
# Validation uses ``MemoryDomainsRepository.exists_by_slug``.


def _validate_domain_slug(slug: Optional[str], conn: duckdb.DuckDBPyConnection) -> None:
    """Raise 400 if ``slug`` is truthy but doesn't resolve to a memory_domains row."""
    if not slug:
        return
    if not memory_domains_repo().exists_by_slug(slug):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown memory domain slug: {slug!r}",
        )


# API-layer allowlist for ``POST /api/memory/admin/bulk-update``. The repo's
# ``_UPDATABLE_FIELDS`` is intentionally broader (``status``, ``sensitivity``,
# ``is_personal``, ``confidence``, valid_from/until, supersedes, etc.) so the
# narrow per-item ``update`` path can still touch them; bulk-edit must NOT,
# because changing status / personal-flag / sensitivity in bulk bypasses the
# proper governance flow (``/admin/mandate``, ``/admin/revoke``,
# ``/{id}/personal``) and its dedicated audit rows. Callers that need those
# fields in bulk should use the per-item endpoints. See PR #126 review.
_BULK_UPDATE_ALLOWED = frozenset(
    {
        "category",
        "domain",
        "tags",
        "tags_add",
        "tags_remove",
        "audience",
        "title",
        "content",
    }
)


def _is_privileged_viewer(user, conn: duckdb.DuckDBPyConnection) -> bool:
    """Admins (members of the Admin system group, per RBAC v13) are the
    privileged viewer tier. Pre-v13 the schema also had a km_admin role; v13
    collapsed the role hierarchy into groups, so the corporate-memory admin
    capability now lives on top of plain admin membership. Module authors
    needing a finer-grained gate (curator-only, etc.) should add a
    ``ResourceType.CORPORATE_MEMORY_ADMIN`` resource type and gate with
    ``require_resource_access`` instead of extending this helper.

    A restricted principal is NEVER a privileged viewer — neither a
    co-session nor an agent-session holds admin authority, even when the
    agent's owner is an admin.
    """
    if isinstance(user, PRINCIPAL_TYPES):
        return False
    user_id = user.get("id")
    if not user_id:
        return False
    return is_user_admin(user_id, conn)


def _effective_groups(user, conn: duckdb.DuckDBPyConnection) -> Optional[List[str]]:
    """Audience-filter group list for the caller, or ``None`` for admins
    (no filter — see all items regardless of audience).

    Reads from ``user_group_members`` JOIN ``user_groups`` (the v13 model).
    Pre-v13 this read ``users.groups`` JSON; that column was dropped in v13
    and the membership is now materialized in ``user_group_members`` with a
    ``source`` discriminator (admin / google_sync / system_seed).

    For a restricted principal the result is always a non-None list (never
    admin) and audience-filtering is skipped (empty list → no audience filter
    applied, which is safe because MEMORY_DOMAIN grants gate the actual domain
    access).
    """
    if isinstance(user, PRINCIPAL_TYPES):
        # Restricted principals are never admins; use no audience restriction
        # (items are already filtered by granted_domains from the intersection).
        return []
    if _is_privileged_viewer(user, conn):
        return None
    user_id = user.get("id")
    if not user_id:
        return []
    names = user_group_members_repo().list_group_names_for_user(user_id)
    return [f"group:{n}" for n in names]


def _caller_granted_memory_domains(
    user,
    conn: duckdb.DuckDBPyConnection,
) -> Optional[List[str]]:
    """Domains the caller has been granted access to via resource_grants.

    The grant model is generic — admins assign ``MEMORY_DOMAIN`` resources
    (e.g. ``md_finance``) to ``user_groups`` rows via ``/admin/access``.
    This helper resolves the caller's group memberships against
    ``resource_grants`` and returns the union of ``memory_domains.id``
    values (v49: the migration re-pointed grants from slug to id).

    Returns ``None`` for privileged viewers (admins see everything regardless
    of grants — same convention as ``_effective_groups``). Returns an
    empty list when the caller has no grants — the SQL EXISTS-join collapses
    in that case, preserving pre-RBAC behaviour.

    For a restricted principal, returns the intersection's memory_domain set
    (never None — no principal receives admin god-mode).
    """
    if isinstance(user, PRINCIPAL_TYPES):
        from app.resource_types import ResourceType

        return list(user.intersection.get(ResourceType.MEMORY_DOMAIN.value, frozenset()))
    if _is_privileged_viewer(user, conn):
        return None
    user_id = user.get("id")
    if not user_id:
        return []
    return resource_grants_repo().list_resource_ids_for_user(user_id, "memory_domain")


def _can_view_item(user: dict, item: dict, is_priv: bool) -> bool:
    """Personal items are visible only to the contributor and privileged
    viewers. Non-personal items are visible to any authenticated user.

    ``is_priv`` is pre-computed by the caller (one DB hit per request) so
    a per-item loop doesn't re-query ``user_group_members`` for every row.
    """
    if not item.get("is_personal"):
        return True
    if is_priv:
        return True
    return item.get("source_user") == user.get("email")


class CreateKnowledgeRequest(BaseModel):
    title: str
    content: str
    # Allow callers to POST either `domain_slug` (new canonical name,
    # matching admin/repo/template layers) or `domain` (legacy alias kept
    # for one release so existing API callers don't break — Pydantic v2
    # accepts the alias on input, Python code reads `request.domain_slug`).
    model_config = ConfigDict(populate_by_name=True)
    category: str
    tags: Optional[List[str]] = None
    domain_slug: Optional[str] = Field(default=None, alias="domain")
    entities: Optional[List[str]] = None
    source_type: Optional[str] = None


class VoteRequest(BaseModel):
    vote: int


class PersonalFlagRequest(BaseModel):
    is_personal: bool


class AdminActionRequest(BaseModel):
    reason: Optional[str] = None
    audience: Optional[str] = None


class EditRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None


class BatchActionRequest(BaseModel):
    item_ids: List[str]
    action: str  # approve, reject, mandate, revoke
    reason: Optional[str] = None
    audience: Optional[str] = None


class ResolveContradictionRequest(BaseModel):
    resolution: str  # kept_a, kept_b, merged, both_valid


class CreateContradictionRequest(BaseModel):
    item_a_id: str
    item_b_id: str
    explanation: str
    severity: Optional[str] = None
    suggested_resolution: Optional[str] = None


class PatchItemRequest(BaseModel):
    """Partial update for a knowledge item via PATCH /api/memory/admin/{id}.

    Replaces the narrow ``EditRequest`` (title + content only). Any field
    left as ``None`` is unchanged. Domain is validated against
    ``VALID_DOMAINS`` when supplied.

    ``domain_ids`` is the M:N junction write path (knowledge_item_domains)
    used by the admin item-edit modal's chip-input — pass a list of
    memory_domains.id strings and the endpoint replaces the item's full
    domain membership atomically. Empty list ``[]`` clears all
    memberships. Supplying both ``domain`` and ``domain_ids`` is allowed
    (the legacy single ``domain`` write happens first, the junction
    replace overrides it).
    """

    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    domain: Optional[str] = None
    domain_ids: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    audience: Optional[str] = None


class BulkUpdateRequest(BaseModel):
    """Apply ``updates`` to every id in ``item_ids``. Issue #62."""

    item_ids: List[str]
    updates: dict


class ResolveDuplicateRequest(BaseModel):
    """Resolve a duplicate-candidate relation row.

    ``resolution`` is one of ``duplicate`` / ``different`` / ``dismissed``
    (decision 2 in issue #62 — no auto-merge action; merging is a separate
    larger feature).
    """

    resolution: str


# ---- Memory domain catalog (v49 — frontend typeahead + admin dropdowns) ----


@router.get("/domains")
async def list_memory_domains(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all memory domains for chip-input typeahead + dropdown population.

    v49: replaces the hardcoded ``VALID_DOMAINS`` constant. Returns every
    row in ``memory_domains`` (admin-administered + the six canonical seed
    rows) so the frontend can render the picker without a separate /admin
    endpoint. Authenticated users only — domain catalog is non-sensitive
    metadata that powers the item-edit UI.
    """
    domains = memory_domains_repo().list()
    return {
        "domains": [
            {
                "id": d["id"],
                "slug": d["slug"],
                "name": d["name"],
                "description": d["description"],
                "icon": d["icon"],
                "color": d["color"],
            }
            for d in domains
        ]
    }


# ---- User endpoints ----


@router.get("")
async def list_knowledge(
    status_filter: Optional[str] = None,
    category: Optional[str] = None,
    domain: Optional[str] = None,
    source_type: Optional[str] = None,
    search: Optional[str] = None,
    exclude_personal: bool = True,
    upvoted_by_me: bool = False,
    hide_dismissed: bool = False,
    is_required: Optional[bool] = None,
    page: int = 1,
    per_page: int = 50,
    sort: str = "updated_at",
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List knowledge items with filtering, pagination, search.

    ``upvoted_by_me=true`` narrows to items the caller upvoted (powers the
    "My Upvotes" filter on /corporate-memory — replaces the old dead
    "My Rules" category sentinel).
    """
    repo = knowledge_repo()
    page = max(page, 1)
    offset = (page - 1) * per_page
    # Privacy: non-privileged viewers can never opt out of the personal filter.
    # Their own personal contributions are visible via /my-contributions, not here.
    effective_exclude_personal = True if not _is_privileged_viewer(user, conn) else exclude_personal
    effective_groups = _effective_groups(user, conn)
    granted_domains = _caller_granted_memory_domains(user, conn)
    statuses = [status_filter] if status_filter else None
    upvoted_by_user_id = user["id"] if upvoted_by_me else None
    # v46: caller's id is plumbed to repo filters when hide_dismissed=True so
    # the SQL can NOT-EXISTS-subquery against knowledge_item_user_dismissed.
    # Mandatory items are exempted by the subquery's status guard.
    dismissed_by_user_id = user["id"]
    if search:
        items = repo.search(
            search,
            exclude_personal=effective_exclude_personal,
            user_groups=effective_groups,
            granted_domains=granted_domains,
            statuses=statuses,
            category=category,
            domain=domain,
            source_type=source_type,
            is_required=is_required,
            dismissed_by_user=dismissed_by_user_id,
            hide_dismissed=hide_dismissed,
            limit=per_page,
            offset=offset,
        )
        if upvoted_by_user_id:
            # Best-effort post-filter for the search() path (which doesn't
            # plumb the upvote filter into its SQL). Search + "My Upvotes"
            # is rare enough that a post-filter is fine.
            votes_by_item = repo.get_votes_by_user(upvoted_by_user_id)
            upvoted_ids = {item_id for item_id, v in votes_by_item.items() if v > 0}
            items = [it for it in items if it["id"] in upvoted_ids]
    else:
        items = repo.list_items(
            statuses=statuses,
            category=category,
            domain=domain,
            source_type=source_type,
            is_required=is_required,
            exclude_personal=effective_exclude_personal,
            user_groups=effective_groups,
            granted_domains=granted_domains,
            upvoted_by_user=upvoted_by_user_id,
            dismissed_by_user=dismissed_by_user_id,
            hide_dismissed=hide_dismissed,
            limit=per_page,
            offset=offset,
        )

    # Enrich with votes + per-user dismissal flag. The set lookup keeps the
    # per-item annotation O(1); the frontend uses ``dismissed_by_me`` to
    # render the gray-out state without a separate roundtrip.
    dismissed_set = set(repo.list_dismissed_ids(user["id"]))
    for item in items:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]
        item["score"] = votes["upvotes"] - votes["downvotes"]
        item["dismissed_by_me"] = item["id"] in dismissed_set

    import math

    total_count = repo.count_items(
        search=search,
        statuses=statuses,
        category=category,
        domain=domain,
        source_type=source_type,
        is_required=is_required,
        exclude_personal=effective_exclude_personal,
        user_groups=effective_groups,
        granted_domains=granted_domains,
        dismissed_by_user=dismissed_by_user_id,
        hide_dismissed=hide_dismissed,
    )
    total_pages = math.ceil(total_count / per_page) if per_page > 0 else 1

    payload = {
        "items": items,
        "count": len(items),
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
    }

    # The All Items tab approves from this list, so it needs the same
    # annotation the review queue gets — a warning that only appears on one of
    # the two surfaces an approval can be issued from does not do its job.
    # Admin-only: nobody else can approve, and every other caller would pay
    # for a scan they cannot act on. (Devin Review on #1258.)
    if _is_privileged_viewer(user, conn):
        payload["items"] = [_with_delivery_warnings(it) for it in payload["items"]]
        # Sent whenever the tab has anything to act on, not only when
        # something is flagged — the review queue always sends it, and an
        # admin approving from a clean page should still know what approving
        # does. That is the whole point of a standing explainer.
        # (Devin Review on #1258.)
        payload["delivery_notice"] = DELIVERY_NOTICE if payload["items"] else None
    return payload


@router.get("/stats")
async def get_stats(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get corporate memory statistics.

    Aggregations exclude personal items for non-privileged callers — otherwise
    `total` and the `by_*` counts would change in observable ways when a
    colleague flags or unflags a personal item, leaking existence info per
    ADR Decision 1.

    Uses SQL aggregation rather than ``repo.list_items()`` to keep the
    endpoint cheap on large knowledge bases (the loader path materializes
    every row + parses JSON tags/contributors per row, which blocks the
    event loop on N>1k items). Audience filter mirrors what list_items
    applies: ``audience IS NULL OR audience = 'all'`` plus, for non-admins,
    membership in any of the caller's group-prefixed audiences.
    """
    is_priv = _is_privileged_viewer(user, conn)
    groups = _effective_groups(user, conn)
    granted_domains = _caller_granted_memory_domains(user, conn)

    repo = knowledge_repo()
    exclude_personal_for_caller = not is_priv

    # Backend-aware: total + breakdowns go through the repo factory so a
    # Postgres-backed instance counts PG state (was raw conn.execute on the
    # always-DuckDB _get_db connection). The repo methods build the same
    # audience-OR-MEMORY_DOMAIN visibility filter internally.
    total = repo.count_items(
        exclude_personal=exclude_personal_for_caller,
        user_groups=groups,
        granted_domains=granted_domains,
    )
    breakdown = repo.stats_breakdown(
        exclude_personal=exclude_personal_for_caller,
        user_groups=groups,
        granted_domains=granted_domains,
    )
    by_status = breakdown["by_status"]
    categories = breakdown["categories"]
    by_domain = breakdown["by_domain"]
    by_source_type = breakdown["by_source_type"]

    # by_tag + by_audience extend stats for the chip-filter UI (issue #62).
    # The repo helpers honor the same audience + personal-item filters.
    by_tag = repo.count_by_tag(
        exclude_personal=exclude_personal_for_caller,
        user_groups=groups,
        granted_domains=granted_domains,
    )
    by_audience = repo.count_by_audience(
        exclude_personal=exclude_personal_for_caller,
        user_groups=groups,
        granted_domains=granted_domains,
    )

    return {
        "total": total,
        "by_status": by_status,
        "categories": categories,
        "by_domain": by_domain,
        "by_source_type": by_source_type,
        "by_tag": by_tag,
        "by_audience": by_audience,
    }


@router.post("", status_code=201)
async def create_knowledge(
    request: CreateKnowledgeRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # Mirror the validation already enforced by PATCH /admin/{id} and bulk-update
    # so an item can't be created with a domain it can't be patched to. Empty /
    # missing domain is fine — only reject non-empty values outside the allowlist.
    # See PR #126 review.
    _validate_domain_slug(request.domain_slug, conn)
    repo = knowledge_repo()
    item_id = str(uuid.uuid4())

    # Best-effort auto-tagging — runs only when an LLM extractor is configured.
    tags = list(request.tags) if request.tags else []
    try:
        from config.loader import load_instance_config
        from connectors.llm import create_extractor
        from services.corporate_memory.tagger import auto_tag_items

        cfg = load_instance_config()
        ai_cfg = cfg.get("ai")
        if ai_cfg:
            extractor = create_extractor(ai_cfg)
            stub = [{"id": item_id, "title": request.title, "content": request.content}]
            assignments = await asyncio.to_thread(auto_tag_items, stub, extractor)
            topics = assignments.get(item_id, [])
            if topics:
                seen: set[str] = set()
                merged: list[str] = []
                for t in topics + tags:
                    if t not in seen:
                        seen.add(t)
                        merged.append(t)
                tags = merged
    except Exception:
        pass  # tagging is non-critical — never block item creation

    # #1573: this endpoint used to hardcode status="pending" regardless of
    # corporate_memory.approval_mode — the one config knob that DID have a
    # reader (services/corporate_memory/collector.py) simply wasn't
    # consulted here, so a human asserting a known fact got the same
    # mandatory-review treatment as an unverified LLM extraction even on an
    # instance explicitly configured for auto_publish. Route through the
    # same resolver the collector uses so the two ingestion paths can't
    # drift (services/corporate_memory/governance.py).
    from app.instance_config import get_corporate_memory_config
    from services.corporate_memory.governance import resolve_initial_status

    governance_config = get_corporate_memory_config()
    confidence = 0.50
    initial_status = resolve_initial_status(governance_config, confidence=confidence)

    create_kwargs = dict(
        id=item_id,
        title=request.title,
        content=request.content,
        category=request.category,
        source_user=user.get("email"),
        tags=tags or None,
        domain=request.domain_slug,
        entities=request.entities,
        confidence=confidence,
        status=initial_status,
    )
    if request.source_type:
        create_kwargs["source_type"] = request.source_type
    repo.create(**create_kwargs)
    return {"id": item_id, "status": initial_status}


@router.post("/{item_id}/vote")
async def vote_knowledge(
    item_id: str,
    request: VoteRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    if request.vote not in (1, -1, 0):
        raise HTTPException(status_code=400, detail="Vote must be 1, -1, or 0 (retract)")
    repo = knowledge_repo()
    item = repo.get_by_id(item_id)
    if not item or not _can_view_item(user, item, _is_privileged_viewer(user, conn)):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    if request.vote == 0:
        repo.unvote(item_id, user["id"])
    else:
        repo.vote(item_id, user["id"], request.vote)
    return repo.get_votes(item_id)


@router.get("/my-votes")
async def get_my_votes(
    user: dict = Depends(get_current_user),
):
    """Get current user's votes on all items."""
    return knowledge_repo().get_votes_by_user(user["id"])


@router.get("/my-contributions")
async def get_my_contributions(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get knowledge items contributed by the current user."""
    repo = knowledge_repo()
    email = user.get("email", "")
    items = repo.get_user_contributions(email)
    for item in items:
        votes = repo.get_votes(item["id"])
        item["upvotes"] = votes["upvotes"]
        item["downvotes"] = votes["downvotes"]
        item["score"] = votes["upvotes"] - votes["downvotes"]
    return {"items": items, "count": len(items)}


@router.post("/{item_id}/personal")
async def toggle_personal_flag(
    item_id: str,
    request: PersonalFlagRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Toggle personal/excluded flag on a knowledge item (only by the contributor)."""
    repo = knowledge_repo()
    item = repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    if item.get("source_user") != user.get("email"):
        raise HTTPException(status_code=403, detail="Only the contributor can flag personal items")
    repo.set_personal(item_id, request.is_personal)
    return {"id": item_id, "is_personal": request.is_personal}


@router.post("/{item_id}/dismiss")
async def dismiss_item(
    item_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-user opt-out — remove an item from the caller's AI bundle.

    Idempotent: re-dismissing an already-dismissed item is a no-op success.
    Mandatory items can never be dismissed — the governance hard rule —
    so a POST against one returns 400 with a clear detail message.
    """
    repo = knowledge_repo()
    item = repo.get_by_id(item_id)
    if not item or not _can_view_item(user, item, _is_privileged_viewer(user, conn)):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    # v49: Required tier rides on ``is_required`` (was status='mandatory').
    if item.get("is_required") is True:
        raise HTTPException(status_code=400, detail="Cannot dismiss a mandatory item")
    repo.dismiss(user["id"], item_id)
    # v49 Section 9.2 — telemetry. domain_ids surfaces the per-item domain
    # membership so /admin/telemetry can correlate dismissals with the
    # domain they came from.
    try:
        domain_ids = [d["id"] for d in memory_domains_repo().list_domains_of_item(item_id)]
        usage_repo().emit_server_event(
            event_type="memory.dismiss",
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props={"item_id": item_id, "domain_ids": domain_ids},
        )
    except Exception:
        pass
    return {"id": item_id, "dismissed": True}


@router.delete("/{item_id}/dismiss", status_code=204)
async def undismiss_item(
    item_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Idempotent un-dismiss — a second DELETE still returns 204.

    Returns 404 if the item itself doesn't exist (consistent with the rest
    of the per-item endpoints); the dismissal row's existence is not
    consulted because absence is the success state.
    """
    repo = knowledge_repo()
    item = repo.get_by_id(item_id)
    if not item or not _can_view_item(user, item, _is_privileged_viewer(user, conn)):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    repo.undismiss(user["id"], item_id)
    # v49 Section 9.2 — telemetry. Best-effort fire-and-forget. Endpoint
    # returns 204 No Content (the decorator status_code overrides any
    # body), so no return value needed; telemetry is the only side effect
    # we still want.
    try:
        usage_repo().emit_server_event(
            event_type="memory.undismiss",
            user_id=user["id"],
            username=user.get("email") or user["id"],
            props={"item_id": item_id},
        )
    except Exception:
        pass


@router.get("/{item_id}/provenance")
async def get_provenance(
    item_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get source provenance for a knowledge item."""
    repo = knowledge_repo()
    item = repo.get_by_id(item_id)
    if not item or not _can_view_item(user, item, _is_privileged_viewer(user, conn)):
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    return {
        "id": item_id,
        "source_type": item.get("source_type"),
        "source_ref": item.get("source_ref"),
        "source_user": item.get("source_user"),
        "confidence": item.get("confidence"),
        "domain": item.get("domain"),
        "entities": item.get("entities"),
        "valid_from": item.get("valid_from"),
        "valid_until": item.get("valid_until"),
        "supersedes": item.get("supersedes"),
        "created_at": item.get("created_at"),
    }


# ---- Admin governance endpoints ----


def _get_item_or_404(repo, item_id: str) -> dict:
    item = repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge item not found")
    return item


def _with_delivery_warnings(item: dict) -> dict:
    """Annotate a row with the spans of it that read as orders to an agent.

    Approving an item is not only a judgement about the content — ``agnes
    pull`` writes approved and required items into every analyst's
    ``.claude/rules/``, which Claude Code loads as project rules. A note
    phrased as a next step therefore lands as a standing instruction. The
    approval surfaces had no way to say so, so an ordinary session recap
    ("type /exit and rerun claude … with recaps disabled") shipped fleet-wide
    as a directive.

    Advisory, never a gate: the field annotates the decision, and every
    governance endpoint still does exactly what it is asked.
    """
    warnings = scan_item(item)
    if not warnings:
        return {**item, "delivery_warnings": []}
    return {**item, "delivery_warnings": warnings}


def _audit_action(conn, admin: dict, action: str, item_id: str, details: dict = None):
    """Write an admin governance audit row.

    Action names use the ``corporate_memory.<action>`` namespace as advertised
    in the 0.15.0 CHANGELOG. Pre-#62 the code wrote ``km_<action>`` — the
    audit-tab filter (see ``admin_audit`` below) accepts both prefixes so
    historical rows still surface.

    ``user_id`` records ``users.id`` (email only as a fallback for synthetic
    callers) so the Activity Center facets don't split one person into an
    email row and a UUID row.
    """
    audit = audit_repo()
    audit.log(
        user_id=admin.get("id") or admin.get("email"),
        action=f"corporate_memory.{action}",
        resource=item_id,
        params=details,
    )


@router.post("/admin/approve")
async def admin_approve(
    item_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Approve an item — status → approved.

    The response echoes ``delivery_warnings`` because approval is the moment
    the item enters the rules channel, and a caller that approves through the
    API (or the CLI on top of it) never sees the review page's banner. The
    count also lands on the audit row, so "who approved a note carrying agent
    directives" is answerable after the fact.
    """
    repo = knowledge_repo()
    item = _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "approved")
    warnings = scan_item(item)
    _audit_action(conn, user, "approve", item_id, {"delivery_warning_count": len(warnings)})
    return {
        "id": item_id,
        "status": "approved",
        "delivery_warnings": warnings,
        "delivery_notice": DELIVERY_NOTICE if warnings else None,
    }


@router.post("/admin/reject")
async def admin_reject(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = knowledge_repo()
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "rejected")
    _audit_action(conn, user, "reject", item_id, {"reason": request.reason})
    return {"id": item_id, "status": "rejected"}


@router.post("/admin/mandate")
async def admin_mandate(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """v49: Required tier rides on ``knowledge_items.is_required`` boolean —
    ``status`` is reserved for lifecycle (pending/approved/rejected/revoked/
    expired). This endpoint keeps the path stable for back-compat; response
    shape now surfaces ``is_required: True`` instead of ``status: 'mandatory'``.
    """
    repo = knowledge_repo()
    item = _get_item_or_404(repo, item_id)
    repo.set_is_required(item_id, True)
    if request.audience is not None:
        repo.update(item_id, audience=request.audience)
    # Marking required puts the item into `.claude/rules/` exactly as approval
    # does — the batch endpoint reports the instruction-shaped spans for this
    # action and this one did not, so the single-item path published them
    # silently and left no count on the audit row. (Devin Review on #1258.)
    warnings = scan_item(item)
    _audit_action(
        conn,
        user,
        "mandate",
        item_id,
        {
            "reason": request.reason,
            "audience": request.audience,
            "delivery_warning_count": len(warnings),
        },
    )
    # v49 Section 9.1 — spec table maps both mark-mandatory and the legacy
    # mandate endpoint to the canonical ``memory_item.set_required`` action
    # with a boolean payload so audit consumers can stop splitting on path.
    try:
        audit_repo().log(
            user_id=user["id"],
            action="memory_item.set_required",
            resource=f"knowledge_item:{item_id}",
            params={"new_value": True},
        )
    except Exception:
        pass
    return {
        "id": item_id,
        "is_required": True,
        "status": "mandatory",
        "delivery_warnings": warnings,
        "delivery_notice": DELIVERY_NOTICE if warnings else None,
    }


@router.post("/items/{item_id}/mark-mandatory")
async def mark_mandatory(
    item_id: str,
    user: dict = Depends(require_admin),
):
    """Promote an item to required (``is_required = TRUE``).

    v49: explicit path-segment variant of the legacy ``/admin/mandate`` query-
    param endpoint, matching the spec's Section 6 mapping table. Same audit
    pattern but no audience / reason fields — those stay on /admin/mandate.
    """
    repo = knowledge_repo()
    item = _get_item_or_404(repo, item_id)
    repo.set_is_required(item_id, True)
    # Same channel, same report as `/admin/mandate` and the batch path: this
    # is the endpoint the page calls, so fixing only its sibling would leave
    # the surface admins actually use publishing silently.
    # (Devin Review on #1258, once per endpoint.)
    warnings = scan_item(item)
    # Best-effort like the sibling /admin/mandate — the flag flip has already
    # committed; an audit hiccup must not surface as a 500.
    try:
        audit_repo().log(
            user_id=user["id"],
            action="memory_item.set_required",
            resource=f"knowledge_item:{item_id}",
            params={"new_value": True, "delivery_warning_count": len(warnings)},
        )
    except Exception:
        pass
    return {
        "id": item_id,
        "is_required": True,
        "delivery_warnings": warnings,
        "delivery_notice": DELIVERY_NOTICE if warnings else None,
    }


@router.post("/items/{item_id}/mark-unmandatory")
async def mark_unmandatory(
    item_id: str,
    user: dict = Depends(require_admin),
):
    """Demote an item from required (``is_required = FALSE``).

    v49 — inverse of mark-mandatory. The item stays in the catalog with its
    existing ``status`` (typically ``approved``); only the required-tier flag
    flips. Audit row writes ``memory_item.set_required`` with
    ``{new_value: false}``.
    """
    repo = knowledge_repo()
    _get_item_or_404(repo, item_id)
    repo.set_is_required(item_id, False)
    # Best-effort like the sibling /admin/mandate.
    try:
        audit_repo().log(
            user_id=user["id"],
            action="memory_item.set_required",
            resource=f"knowledge_item:{item_id}",
            params={"new_value": False},
        )
    except Exception:
        pass
    return {"id": item_id, "is_required": False}


@router.post("/admin/revoke")
async def admin_revoke(
    item_id: str,
    request: AdminActionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = knowledge_repo()
    _get_item_or_404(repo, item_id)
    repo.update_status(item_id, "revoked")
    _audit_action(conn, user, "revoke", item_id, {"reason": request.reason})
    return {"id": item_id, "status": "revoked"}


@router.post("/admin/edit")
async def admin_edit(
    item_id: str,
    request: EditRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = knowledge_repo()
    _get_item_or_404(repo, item_id)
    updates = {}
    if request.title is not None:
        updates["title"] = request.title
    if request.content is not None:
        updates["content"] = request.content
    if updates:
        repo.update(item_id, **updates)
    _audit_action(conn, user, "edit", item_id, updates)
    return {"id": item_id, "updated": list(updates.keys())}


@router.post("/admin/batch")
async def admin_batch(
    request: BatchActionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Batch governance action on multiple items.

    v49: ``mandate`` flips the new ``is_required`` boolean to TRUE (was
    ``status='mandatory'`` overload). Other actions still drive ``status``.

    ``approve`` and ``mandate`` are the two actions that put an item into the
    ``.claude/rules/`` delivery channel, so those responses carry
    ``delivery_warnings`` per item — this endpoint backs ``agnes admin memory
    approve``, where a batch of ids is exactly the path on which nobody reads
    the content first. ``reject`` and ``revoke`` take items *out* of the
    channel and report nothing.
    """
    repo = knowledge_repo()
    # mandate is special — it writes is_required, not status. All other
    # actions stay on the status lifecycle column.
    status_actions = {
        "approve": "approved",
        "reject": "rejected",
        "revoke": "revoked",
    }
    if request.action not in (*status_actions, "mandate"):
        raise HTTPException(status_code=400, detail=f"Invalid action: {request.action}")

    delivers_to_rules = request.action in ("approve", "mandate")
    results: dict = {"success": [], "not_found": []}
    warnings_by_item: dict[str, list[dict]] = {}
    for item_id in request.item_ids:
        item = repo.get_by_id(item_id)
        if not item:
            results["not_found"].append(item_id)
            continue
        if request.action == "mandate":
            repo.set_is_required(item_id, True)
            if request.audience is not None:
                repo.update(item_id, audience=request.audience)
        else:
            repo.update_status(item_id, status_actions[request.action])
        details = {
            "reason": request.reason,
            "audience": request.audience,
            "batch": True,
        }
        if delivers_to_rules:
            found = scan_item(item)
            if found:
                warnings_by_item[item_id] = found
            details["delivery_warning_count"] = len(found)
        _audit_action(
            conn,
            user,
            request.action,
            item_id,
            details,
        )
        results["success"].append(item_id)

    if delivers_to_rules:
        results["delivery_warnings"] = warnings_by_item
        results["delivery_notice"] = DELIVERY_NOTICE if warnings_by_item else None
    return results


@router.get("/admin/pending")
async def admin_pending(
    category: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get pending items queue for admin review.

    ``search`` narrows by title/content substring (same ``repo.search``
    backing the main list endpoint, pinned to ``status='pending'``) so an
    admin can find specific items inside a large review queue without
    paging through it.

    Each row carries ``delivery_warnings`` — the spans that will read as
    instructions once the item is approved and ``agnes pull`` writes it into
    every analyst's ``.claude/rules/``. ``delivery_notice`` states what that
    channel is, once per response rather than per row.
    """
    repo = knowledge_repo()
    page = max(page, 1)
    offset = (page - 1) * per_page
    if search:
        items = repo.search(
            search,
            statuses=["pending"],
            category=category,
            limit=per_page,
            offset=offset,
        )
    else:
        items = repo.list_items(statuses=["pending"], category=category, limit=per_page, offset=offset)
    items = [_with_delivery_warnings(it) for it in items]
    return {"items": items, "count": len(items), "delivery_notice": DELIVERY_NOTICE}


@router.get("/admin/audit")
async def admin_audit(
    page: int = 1,
    per_page: int = 50,
    action: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Get governance audit log.

    Filters ``corporate_memory.<action>`` rows AND legacy ``km_<action>``
    rows. The dual prefix is here because rows already in the audit log keep
    the legacy ``km_*`` action name (no migration of historical audit rows —
    they are write-once); new rows use the ``corporate_memory.*`` namespace.
    See issue #62 decision E.
    """
    # Pagination: page is 1-indexed; offset must apply to BOTH branches so the
    # UI's per-page navigation actually returns subsequent rows. Pre-fix, both
    # SQL paths had LIMIT only and silently returned page 1 for every page.
    offset = (max(page, 1) - 1) * per_page
    entries = audit_repo().query_governance(action=action, limit=per_page, offset=offset)
    return {"entries": entries, "count": len(entries)}


# ---- Admin contradiction endpoints ----


@router.get("/admin/contradictions")
async def admin_contradictions(
    resolved: Optional[bool] = None,
    exclude_personal: bool = True,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List knowledge contradictions for admin review.

    By default (`exclude_personal=True`), personal items are replaced with
    {id, hidden: true} so the contradiction record is still visible for
    governance but personal content is not exposed. Pass exclude_personal=false
    to opt in to full content (KM_ADMIN only — see ADR Decision 1).
    """
    repo = knowledge_repo()
    contradictions = repo.list_contradictions(resolved=resolved)
    # Collect all distinct item IDs and fetch in one query (M5 batch optimisation).
    all_item_ids = list({id_ for c in contradictions for id_ in (c["item_a_id"], c["item_b_id"])})
    items_by_id = repo.get_by_ids(all_item_ids)
    for c in contradictions:
        item_a = items_by_id.get(c["item_a_id"])
        item_b = items_by_id.get(c["item_b_id"])
        if exclude_personal:
            c["item_a"] = {"id": c["item_a_id"], "hidden": True} if item_a and item_a.get("is_personal") else item_a
            c["item_b"] = {"id": c["item_b_id"], "hidden": True} if item_b and item_b.get("is_personal") else item_b
        else:
            c["item_a"] = item_a
            c["item_b"] = item_b
    return {"contradictions": contradictions, "count": len(contradictions)}


@router.post("/admin/contradictions", status_code=201)
async def admin_create_contradiction(
    request: CreateContradictionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin endpoint for manually recording a contradiction between two knowledge items."""
    repo = knowledge_repo()
    if not repo.get_by_id(request.item_a_id):
        raise HTTPException(status_code=404, detail=f"Item A not found: {request.item_a_id}")
    if not repo.get_by_id(request.item_b_id):
        raise HTTPException(status_code=404, detail=f"Item B not found: {request.item_b_id}")

    cid = repo.create_contradiction(
        item_a_id=request.item_a_id,
        item_b_id=request.item_b_id,
        explanation=request.explanation,
        severity=request.severity,
        suggested_resolution=request.suggested_resolution,
    )
    return {"id": cid}


@router.post("/admin/contradictions/{contradiction_id}/resolve")
async def admin_resolve_contradiction(
    contradiction_id: str,
    request: ResolveContradictionRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Resolve a knowledge contradiction."""
    repo = knowledge_repo()
    contradiction = repo.get_contradiction(contradiction_id)
    if not contradiction:
        raise HTTPException(status_code=404, detail="Contradiction not found")
    if contradiction.get("resolved"):
        raise HTTPException(status_code=400, detail="Contradiction already resolved")

    valid_resolutions = ["kept_a", "kept_b", "merged", "both_valid"]
    if request.resolution not in valid_resolutions:
        raise HTTPException(
            status_code=400,
            detail=f"Resolution must be one of: {valid_resolutions}",
        )

    repo.resolve_contradiction(contradiction_id, user["email"], request.resolution)
    _audit_action(
        conn,
        user,
        "resolve_contradiction",
        contradiction_id,
        {
            "resolution": request.resolution,
            "item_a_id": contradiction["item_a_id"],
            "item_b_id": contradiction["item_b_id"],
        },
    )
    return {"id": contradiction_id, "resolved": True, "resolution": request.resolution}


# ---- Admin duplicate-candidate endpoints (issue #62) ----

VALID_DUPLICATE_RESOLUTIONS = ["duplicate", "different", "dismissed"]
DUPLICATE_RELATION_TYPE = "likely_duplicate"


def _strip_personal(item: Optional[dict], hide: bool) -> Optional[dict]:
    """Return a placeholder dict when ``item`` is personal and ``hide`` is set."""
    if item is None:
        return None
    if hide and item.get("is_personal"):
        return {"id": item.get("id"), "hidden": True}
    return item


@router.get("/admin/duplicate-candidates")
async def admin_duplicate_candidates(
    resolved: Optional[bool] = None,
    exclude_personal: bool = True,
    limit: int = 100,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List duplicate-candidate relations for admin review.

    Pass ``resolved=true`` or ``resolved=false`` to filter; omit both to fetch
    every state (the original UI default). The web UI keeps surfacing the
    actionable backlog by passing ``resolved=false`` explicitly.

    With ``exclude_personal=true`` (default) personal items in the pair are
    replaced with ``{id, hidden: true}`` — the relation row is still visible
    so admins can resolve it, but content stays inside the personal-item
    privacy boundary (ADR Decision 1 precedent).
    """
    repo = knowledge_repo()
    relations = repo.list_relations(
        relation_type=DUPLICATE_RELATION_TYPE,
        resolved=resolved,
        limit=limit,
    )
    item_ids = list({id_ for r in relations for id_ in (r["item_a_id"], r["item_b_id"])})
    items_by_id = repo.get_by_ids(item_ids) if item_ids else {}
    for r in relations:
        r["item_a"] = _strip_personal(items_by_id.get(r["item_a_id"]), exclude_personal)
        r["item_b"] = _strip_personal(items_by_id.get(r["item_b_id"]), exclude_personal)
    return {"relations": relations, "count": len(relations)}


@router.post("/admin/duplicate-candidates/resolve")
async def admin_resolve_duplicate_candidate(
    item_a_id: str,
    item_b_id: str,
    request: ResolveDuplicateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Resolve a duplicate-candidate relation.

    Admin chooses: ``duplicate`` (acknowledge), ``different`` (false
    positive), or ``dismissed`` (don't surface again, but no judgment).
    Idempotent re-resolve is rejected with 400 — the audit trail wants one
    decision per pair.
    """
    if request.resolution not in VALID_DUPLICATE_RESOLUTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"resolution must be one of: {VALID_DUPLICATE_RESOLUTIONS}",
        )
    repo = knowledge_repo()
    existing = repo.get_relation(item_a_id, item_b_id, DUPLICATE_RELATION_TYPE)
    if not existing:
        raise HTTPException(status_code=404, detail="Duplicate-candidate relation not found")
    if existing.get("resolved"):
        raise HTTPException(status_code=400, detail="Relation already resolved")

    repo.resolve_relation(
        item_a_id=item_a_id,
        item_b_id=item_b_id,
        relation_type=DUPLICATE_RELATION_TYPE,
        resolved_by=user["email"],
        resolution=request.resolution,
    )
    # Resource-id of audit row is the canonical (a,b) pair for grep-ability.
    a, b = sorted([item_a_id, item_b_id])
    _audit_action(
        conn,
        user,
        "resolve_duplicate",
        f"{a}::{b}",
        {"resolution": request.resolution, "item_a_id": a, "item_b_id": b},
    )
    return {
        "item_a_id": a,
        "item_b_id": b,
        "resolved": True,
        "resolution": request.resolution,
    }


# ---- Admin PATCH + bulk-update + tree endpoints (issue #62) ----


@router.get("/admin/{item_id}")
async def admin_get_item(
    item_id: str,
    user: dict = Depends(require_admin),
):
    """Single-item GET — powers the ``#item-<id>`` deep link from
    `/memory/d/<slug>`'s Edit affordance. The admin page uses this to
    fetch the row directly (bypassing pagination of the All-Items list)
    so the edit modal opens reliably regardless of which page the item
    happens to fall on. Returns the same dict shape as the list rows.

    Route placement note: declared AFTER all named ``/admin/<word>`` GET
    routes (pending, audit, contradictions, duplicate-candidates) so the
    catch-all ``{item_id}`` doesn't shadow them — FastAPI matches in
    declaration order.

    Carries ``delivery_warnings`` for the same reason the pending queue does:
    this is the row the edit modal reads, and an admin editing a note before
    approving it should see which of its lines will read as an instruction.
    """
    repo = knowledge_repo()
    item = repo.get_by_id(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item_not_found")
    return _with_delivery_warnings(item)


@router.patch("/admin/{item_id}")
async def admin_patch_item(
    item_id: str,
    request: PatchItemRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Partial update — accepts category/domain/tags/audience/title/content.

    Replaces the narrow ``POST /api/memory/admin/edit`` (kept one release as
    a thin alias). Audit row tagged ``corporate_memory.update_item`` records
    which fields changed (not the full diff — keep audit rows compact).
    """
    repo = knowledge_repo()
    _get_item_or_404(repo, item_id)

    # ``exclude_unset=True`` preserves explicit ``null`` values from the request
    # body so callers can clear previously-set Optional fields (e.g. PATCH
    # ``{"audience": null}`` resets audience to NULL). With ``exclude_none=True``
    # those nulls were silently dropped — the only path to clear was the
    # empty-string short-circuit on ``domain``, and ``audience`` had no clearing
    # path at all. See PR #126 round-4 review.
    updates = request.model_dump(exclude_unset=True)
    if "domain" in updates and updates["domain"]:
        _validate_domain_slug(updates["domain"], conn)
    # ``title`` is NOT NULL in the schema. ``exclude_unset=True`` lets explicit
    # ``null`` through, which would 500 on a DuckDB constraint violation. Reject
    # at the boundary so the caller gets a 400 with a clear message instead.
    if "title" in updates and updates["title"] is None:
        raise HTTPException(status_code=400, detail="title cannot be null")

    # M:N domain membership lives in ``knowledge_item_domains`` and is
    # written via a separate junction repo call — strip from the legacy
    # ``repo.update(**)`` kwargs since the knowledge_items row has no
    # ``domain_ids`` column.
    domain_ids = updates.pop("domain_ids", None)

    if not updates and domain_ids is None:
        return {"id": item_id, "updated": []}

    # tags is a list — JSON-encode to match the column type, mirroring create().
    repo_kwargs = dict(updates)
    if "tags" in repo_kwargs:
        repo_kwargs["tags"] = json.dumps(repo_kwargs["tags"]) if repo_kwargs["tags"] else None
    if repo_kwargs:
        repo.update(item_id, **repo_kwargs)

    # Junction write — replace the item's full domain membership atomically.
    # Resolve ids → slugs because replace_domains_for_item takes slugs;
    # unknown ids raise 400 (admin's chip-input only picks from
    # /api/admin/memory-domains so a missing id means a race or an
    # already-deleted domain).
    if domain_ids is not None:
        dom_repo = memory_domains_repo()
        if domain_ids:
            id_to_slug = dom_repo.resolve_ids_to_slugs(domain_ids)
            missing = [i for i in domain_ids if i not in id_to_slug]
            if missing:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown_domain_ids: {missing}",
                )
            slugs = [id_to_slug[i] for i in domain_ids]
        else:
            slugs = []
        dom_repo.replace_domains_for_item(item_id, slugs, added_by=user["email"])

    audit_keys = sorted(updates.keys())
    if domain_ids is not None:
        audit_keys.append("domain_ids")
    _audit_action(
        conn,
        user,
        "update_item",
        item_id,
        {"updated_fields": audit_keys},
    )
    return {"id": item_id, "updated": audit_keys}


@router.post("/admin/bulk-update")
async def admin_bulk_update(
    request: BulkUpdateRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Apply ``updates`` to every id in ``item_ids``. Per-id audit rows.

    Returns a per-id status map plus rolled-up convenience lists (200 even on
    partial failure — the body distinguishes successes from misses).
    """
    repo = knowledge_repo()
    updates = dict(request.updates or {})
    # Reject governance-sensitive fields BEFORE hitting the repo. _UPDATABLE_FIELDS
    # in the repo is broad on purpose; this endpoint is the narrow path. Callers
    # that need to flip status/sensitivity/is_personal must use the dedicated
    # governance endpoints so the right audit row is written. See PR #126 review.
    disallowed = sorted(k for k in updates.keys() if k not in _BULK_UPDATE_ALLOWED)
    if disallowed:
        raise HTTPException(
            status_code=400,
            detail=(f"updates contains disallowed field(s): {disallowed}. Allowed: {sorted(_BULK_UPDATE_ALLOWED)}"),
        )
    if "domain" in updates and updates["domain"]:
        _validate_domain_slug(updates["domain"], conn)
    # Mirror the PATCH boundary check — title is NOT NULL in the schema, so
    # an explicit null here would fall through to a per-item Constraint Error
    # in repo.bulk_update() instead of a clean 400 to the caller.
    if "title" in updates and updates["title"] is None:
        raise HTTPException(status_code=400, detail="title cannot be null")
    if not request.item_ids:
        return {"updated": [], "not_found": [], "errors": {}}

    statuses = repo.bulk_update(request.item_ids, updates)

    # Allowlist already enforced above, so every key in updates is auditable.
    audited_fields = sorted(updates.keys())
    updated: List[str] = []
    not_found: List[str] = []
    errors: dict = {}
    for item_id, status in statuses.items():
        if status == "updated":
            updated.append(item_id)
            _audit_action(
                conn,
                user,
                "bulk_update",
                item_id,
                {"updated_fields": audited_fields, "batch": True},
            )
        elif status == "not_found":
            not_found.append(item_id)
        else:
            errors[item_id] = status
    return {"updated": updated, "not_found": not_found, "errors": errors}


# Axes the tree endpoint groups by. Anything else → 400. Order matters for
# the default chip rendering in the UI.
_TREE_AXES = ("domain", "category", "tag", "audience")


def _label_for_axis(axis: str, key: Optional[str]) -> str:
    """Pretty bucket label for the tree UI. Falls back to the raw key."""
    if key is None or key == "":
        return {
            "domain": "(no domain)",
            "category": "(no category)",
            "tag": "(no tag)",
            "audience": "All users",
        }.get(axis, "(unset)")
    if axis == "audience" and key == "all":
        return "All users"
    if axis == "audience" and key.startswith("group:"):
        return f"Group: {key[len('group:') :]}"
    return key


def _matches_chip_filters(
    item: dict,
    *,
    status_filter: Optional[str],
    source_type: Optional[str],
    audience: Optional[str],
    has_duplicate_ids: Optional[set],
    q: Optional[str],
) -> bool:
    """Apply chip filters to an already-RBAC-filtered item."""
    if status_filter and item.get("status") != status_filter:
        return False
    if source_type and item.get("source_type") != source_type:
        return False
    if audience:
        # Treat NULL audience as 'all' so chip-filter behavior matches the
        # SQL audience filter, ``count_by_audience`` (COALESCE→'all'), and the
        # tree's ``_bucket_key`` (NULL → 'all'). Without this coalesce,
        # NULL-audience items disappear from the ``audience=all`` chip even
        # though the rest of the system treats them as visible-to-everyone.
        item_audience = item.get("audience") or "all"
        if item_audience != audience:
            return False
    if has_duplicate_ids is not None and item.get("id") not in has_duplicate_ids:
        return False
    if q:
        needle = q.lower()
        title = (item.get("title") or "").lower()
        content = (item.get("content") or "").lower()
        if needle not in title and needle not in content:
            return False
    return True


@router.get("/tree")
async def get_tree(
    axis: str = "domain",
    status_filter: Optional[str] = None,
    source_type: Optional[str] = None,
    audience: Optional[str] = None,
    q: Optional[str] = None,
    has_duplicate: bool = False,
    exclude_personal: bool = True,
    is_required: Optional[bool] = None,
    page: int = 1,
    per_page: int = 50,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Server-side grouping for the Browse / Group-by tree (issue #62).

    Returns ``{groups: [{key, label, count, items: [...]}]}`` already
    RBAC-filtered + chip-filtered. The same ``_effective_groups`` and
    ``_can_view_item`` helpers used by ``GET /api/memory`` apply, so a
    non-admin caller never sees personal items belonging to others, and
    audience-restricted items only surface for members of the audience
    group.

    On the ``tag`` axis a single item appears once per tag it holds — that
    is the intended "overlapping bucket" affordance. Every other axis puts
    each item in its single canonical bucket.
    """
    if axis not in _TREE_AXES:
        raise HTTPException(
            status_code=400,
            detail=f"axis must be one of: {list(_TREE_AXES)}",
        )
    repo = knowledge_repo()
    is_priv = _is_privileged_viewer(user, conn)
    effective_groups = _effective_groups(user, conn)
    # Privacy parity with ``GET /api/memory``: non-admin can never opt out.
    effective_exclude_personal = True if not is_priv else exclude_personal

    page = max(page, 1)
    per_page = max(min(per_page, 500), 1)

    # has_duplicate=true narrows the candidate set to items present in any
    # unresolved likely_duplicate relation. Computed once; intersected per
    # item below.
    has_duplicate_ids: Optional[set] = None
    if has_duplicate:
        rels = repo.list_relations(
            relation_type=DUPLICATE_RELATION_TYPE,
            resolved=False,
            limit=10000,
        )
        has_duplicate_ids = {id_ for r in rels for id_ in (r["item_a_id"], r["item_b_id"])}

    # Audience-axis privacy (decision 13): non-admins only see their own
    # group buckets + null/all. Use the audience pre-filter on the SQL side
    # so non-admins never accidentally see another group's bucket count.
    granted_domains = _caller_granted_memory_domains(user, conn)
    statuses = [status_filter] if status_filter else None
    items = repo.list_items(
        statuses=statuses,
        source_type=source_type,
        is_required=is_required,
        exclude_personal=effective_exclude_personal,
        user_groups=effective_groups,
        granted_domains=granted_domains,
        limit=10000,
        offset=0,
    )

    # Apply remaining chip filters that don't have a SQL layer yet.
    visible: List[dict] = []
    for item in items:
        if not _can_view_item(user, item, is_priv):
            continue
        if not _matches_chip_filters(
            item,
            status_filter=None,  # already in SQL
            source_type=None,  # already in SQL
            audience=audience,
            has_duplicate_ids=has_duplicate_ids,
            q=q,
        ):
            continue
        visible.append(item)

    # Group items by axis. tag is multi-bucket; everything else is single.
    groups: dict = {}

    def _bucket_key(item: dict, axis: str) -> List[str]:
        if axis == "tag":
            tags = item.get("tags")
            if isinstance(tags, str):
                try:
                    tags = json.loads(tags)
                except json.JSONDecodeError:
                    tags = []
            if not tags:
                return [""]
            return [str(t) for t in tags]
        if axis == "audience":
            aud = item.get("audience")
            return [aud if aud else "all"]
        if axis == "category":
            return [item.get("category") or ""]
        # default: domain
        return [item.get("domain") or ""]

    for item in visible:
        for key in _bucket_key(item, axis):
            bucket = groups.setdefault(
                key,
                {
                    "key": key,
                    "label": _label_for_axis(axis, key),
                    "items": [],
                },
            )
            bucket["items"].append(item)

    # Stable ordering: alphabetic on key with the empty bucket sinking to
    # the bottom — the UI usually wants the "real" buckets up top.
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["key"] == "", g["key"].lower() if isinstance(g["key"], str) else ""),
    )
    for g in ordered:
        g["count"] = len(g["items"])

    # Page over groups (not items): operators paging through hundreds of
    # tags want bucket-level pagination. The UI expands a bucket to see all
    # its items.
    start = (page - 1) * per_page
    paged = ordered[start : start + per_page]

    # The Browse tab approves too — its cards carry the same actions — so its
    # items need the same annotation the review queue and All Items get, or
    # the warning is missing on one of the three surfaces an approval can be
    # issued from. Only the PAGED groups are scanned, and only for an admin.
    # (Devin Review on #1258.)
    payload_notice = None
    if _is_privileged_viewer(user, conn):
        for group in paged:
            group["items"] = [_with_delivery_warnings(it) for it in group["items"]]
        payload_notice = DELIVERY_NOTICE if any(g["items"] for g in paged) else None

    return {
        "axis": axis,
        "groups": paged,
        "delivery_notice": payload_notice,
        "page": page,
        "per_page": per_page,
        "total_groups": len(ordered),
        "total_items": sum(g["count"] for g in ordered),
    }


# ---- Bundle endpoint ----


def _build_per_domain_markdown(slug: str, user: dict, conn: duckdb.DuckDBPyConnection) -> Response:
    """Render a deterministic markdown bundle for a single memory domain.

    Used by ``agnes pull`` to write ``~/.claude/memory/<slug>/bundle.md``.
    The bundle includes both ``is_required=TRUE`` and approved items so
    the per-domain md5 in ``/api/sync/manifest`` (built from the same
    item set in ``_build_memory_domains_section``) matches the md5 of
    what the CLI just received. Items are sorted by ``id`` to mirror the
    manifest's md5 computation byte-for-byte (Section 5.1 of the
    unified-stack design).

    RBAC: the caller must have a grant on the domain — admins bypass
    via ``can_access``'s admin short-circuit. Anonymous or grantless
    callers get 403.
    """
    repo = memory_domains_repo()
    dom = repo.get_by_slug(slug)
    if not dom:
        raise HTTPException(status_code=404, detail="memory_domain_not_found")
    if isinstance(user, PRINCIPAL_TYPES):
        allowed = can_access_session(user, "memory_domain", dom["id"])
    else:
        allowed = can_access(user["id"], "memory_domain", dom["id"], conn)
    if not allowed:
        raise HTTPException(status_code=403, detail="no_grant")

    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="memory.bundle_download",
        resource=f"memory_domain:{dom['id']}",
        params={"slug": slug},
    )

    # Pull items the same way the manifest md5 helper does — id order,
    # full payload (title/status/is_required pulled via the knowledge
    # repository for content), no token-budget truncation.
    items_meta = repo.list_items_of_domain(dom["id"], limit=10000)
    if not items_meta:
        body = f"# {dom['name']}\n\n_No items in this domain yet._\n"
        return Response(content=body, media_type="text/markdown; charset=utf-8")

    # Fetch full bodies — list_items_of_domain only returns id/title/status.
    knowledge = knowledge_repo()
    full_items: list = []
    for meta in sorted(items_meta, key=lambda r: r["id"]):
        full = knowledge.get_by_id(meta["id"])
        if not full:
            continue
        full_items.append(full)

    lines: list = [f"# {dom['name']}", ""]
    if dom.get("description"):
        lines.append(dom["description"])
        lines.append("")

    required = [it for it in full_items if it.get("is_required")]
    approved = [it for it in full_items if not it.get("is_required") and it.get("status") == "approved"]

    if required:
        lines.append("## Required")
        lines.append("")
        for it in required:
            lines.append(f"### {it.get('title', 'Untitled')}")
            lines.append("")
            lines.append(it.get("content", "") or "")
            lines.append("")

    if approved:
        lines.append("## Approved")
        lines.append("")
        for it in approved:
            lines.append(f"### {it.get('title', 'Untitled')}")
            lines.append("")
            lines.append(it.get("content", "") or "")
            lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    return Response(content=body, media_type="text/markdown; charset=utf-8")


@router.get("/bundle")
async def get_bundle(
    domain: Optional[str] = None,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Token-budgeted bundle of knowledge items for AI agent injection.

    Mandatory items are always included regardless of the token budget.
    Approved items are confidence×recency-ranked and included until the budget
    is exhausted. Audience-filtered by the caller's group memberships (admins
    see everything).

    v49: when ``?domain=<slug>`` is supplied the response shape switches
    to ``text/markdown`` containing a deterministic per-domain bundle —
    that's what ``agnes pull`` writes to ``~/.claude/memory/<slug>/bundle.md``.
    RBAC: the caller must have a ``MEMORY_DOMAIN`` grant on the domain
    (admins bypass per ``can_access``). The markdown body sorts items
    alphabetically by title and includes both required and approved
    items (required first, with a marker) so the bundle md5 in the
    manifest matches what the CLI re-renders.
    """
    from datetime import datetime, timezone

    # ----- Per-domain markdown variant (v49) -----
    if domain:
        return _build_per_domain_markdown(domain, user, conn)

    repo = knowledge_repo()
    effective_groups = _effective_groups(user, conn)
    granted_domains = _caller_granted_memory_domains(user, conn)

    # v46: the bundle is what AI agents inject as context, so the opt-out
    # has real effect here — it's always-on for the calling user. Mandatory
    # items are exempted by the EXISTS subquery's status guard inside
    # ``list_items``; the user's dismissal row for a then-approved item is
    # silently ignored if/when the item is later mandated.
    # A Principal has no dict .get("id"); use None so no user-specific
    # dismissal is applied (neither co-sessions nor agent sessions track
    # per-user dismissals).
    dismissed_by_user_id = None if isinstance(user, PRINCIPAL_TYPES) else user["id"]
    # v49: Required tier rides on is_required boolean. Was statuses=['mandatory'].
    mandatory = repo.list_items(
        is_required=True,
        exclude_personal=True,
        user_groups=effective_groups,
        granted_domains=granted_domains,
        dismissed_by_user=dismissed_by_user_id,
        hide_dismissed=True,
        limit=1000,
        offset=0,
    )

    approved = repo.list_items(
        statuses=["approved"],
        is_required=False,
        exclude_personal=True,
        user_groups=effective_groups,
        granted_domains=granted_domains,
        dismissed_by_user=dismissed_by_user_id,
        hide_dismissed=True,
        limit=1000,
        offset=0,
    )

    # Rank approved by confidence × recency (days since updated_at, max 365).
    # updated_at is intentional: a recently admin-edited item reflects a human
    # who just reviewed and corrected it, making it more trustworthy than an
    # older untouched item. This differs from confidence.py which decays from
    # created_at — the two scores serve different purposes (credibility vs freshness).
    now = datetime.now(timezone.utc)

    def _rank(item: dict) -> float:
        confidence = float(item["confidence"]) if item.get("confidence") is not None else 0.5
        updated_raw = item.get("updated_at")
        if updated_raw:
            try:
                if isinstance(updated_raw, str):
                    from datetime import datetime as dt

                    updated = dt.fromisoformat(updated_raw.replace("Z", "+00:00"))
                else:
                    updated = updated_raw
                if updated.tzinfo is None:
                    from datetime import timezone as tz

                    updated = updated.replace(tzinfo=tz.utc)
                age_days = max((now - updated).days, 0)
            except Exception:
                age_days = 365
        else:
            age_days = 365
        recency = max(0.0, 1.0 - age_days / 365.0)
        return confidence * recency

    approved_ranked = sorted(approved, key=_rank, reverse=True)

    def _token_est(item: dict) -> int:
        return len((item.get("title", "") + " " + item.get("content", ""))) // _CHARS_PER_TOKEN

    budget_remaining = BUNDLE_TOKEN_BUDGET - sum(_token_est(i) for i in mandatory)
    approved_included = []
    for item in approved_ranked:
        cost = _token_est(item)
        if budget_remaining - cost < 0:
            break
        approved_included.append(item)
        budget_remaining -= cost

    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="memory.bundle_download",
        resource="memory:bundle",
        params={"mandatory_count": len(mandatory), "approved_count": len(approved_included)},
    )
    return {
        "mandatory": mandatory,
        "approved": approved_included,
        "token_estimate": BUNDLE_TOKEN_BUDGET - budget_remaining,
        "token_budget": BUNDLE_TOKEN_BUDGET,
    }
