"""Authoring Suggestions API (v77) — generic non-admin suggestion queue for
the authoring studio + admin moderation.

Two routers split by audience:

  - ``POST /api/studio/suggestions``                       — any auth user submits
  - ``GET  /api/studio/suggestions/mine``                  — caller sees their own
  - ``GET  /api/admin/authoring-suggestions``              — admin queue
  - ``POST /api/admin/authoring-suggestions/{id}/approve`` — admin resolves
  - ``POST /api/admin/authoring-suggestions/{id}/reject``  — admin resolves

A non-admin who lacks the admin mutation right submits a proposed create
``payload`` here; an admin reviews and approves or rejects it. Approve/reject
are guarded state transitions (only flip a ``pending`` row) and write an
``audit_log`` row so the Activity Center surfaces them.

Approval auto-creates the real resource for all four domains by REPLAYING the
payload through each domain's own validation + repo create path (the pydantic
request models are the re-validation, design spec §5 — the stored payload is
never trusted blindly). The confused-deputy risk for ``mcp``/``marketplace``
(a stdio ``command`` / git ``url`` in the payload) is mitigated because the
moderation UI renders the COMPLETE payload before the admin clicks approve:
approval is informed consent. See ``_SAFE_REPLAY``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from app.instance_config import get_studio_enabled
from app.web.studio import get_domain


def _require_studio_enabled() -> None:
    """403 when the instance-level Studio toggle is off.

    Applied to the WHOLE suggestion surface — public submit/read-own AND the
    admin moderation endpoints — mirroring the web routes (owner decision on
    PR #973: the toggle closes the entire Studio, moderation included).
    Pending rows are untouched; they reappear in the queue when the instance
    re-enables Studio. The store submission endpoints
    (``/api/store/entities/from-markdown``) are deliberately NOT gated here —
    that is the Flea store's own surface (also used by the CLI and the MCP
    foundation tool) with its own guardrail/review pipeline.
    """
    if not get_studio_enabled():
        raise HTTPException(status_code=403, detail={"kind": "studio_disabled"})


from src.repositories import (
    audit_repo,
    authoring_suggestions_repo,
    data_packages_repo,
    memory_domains_repo,
)

logger = logging.getLogger(__name__)


# Approval auto-creates the real resource by REPLAYING the payload through the
# same validation + repo create path the domain's own endpoint uses. The
# pydantic models below ARE the re-validation (design spec §5) — the stored
# payload is never trusted blindly. The confused-deputy risk (a proposer slipping
# a stdio ``command`` / git ``url`` past the admin) is mitigated because the
# moderation UI renders the COMPLETE payload before the admin clicks approve:
# approval is informed consent, not a silent replay.
def _replay_data_package(payload: dict, by: str, submitted_by: Optional[str] = None) -> str:
    return data_packages_repo().create(
        name=payload["name"],
        slug=payload["slug"],
        description=payload.get("description"),
        icon=None,
        color=None,
        created_by=by,
    )


def _replay_corporate_memory(payload: dict, by: str, submitted_by: Optional[str] = None) -> str:
    """Create the domain AND the knowledge the author wrote.

    Copying only name/slug/description silently dropped the whole point of a
    corporate-memory submission: the author filled in the knowledge, saw
    "Submitted for approval", and approval produced an empty domain. The
    seeding is shared with the admin endpoint so the two creation paths cannot
    diverge again. (Devin Review on #1263.)
    """
    from app.api.memory_domains import seed_domain_item

    # Normalize ONCE, and use the same values for both writes: the domain was
    # being created with the slug exactly as submitted while the item was
    # filed under a trimmed copy, so a submission whose slug carried a stray
    # space failed every approval attempt — the seeding raises on a slug it
    # cannot resolve, which rolls the whole approval back, forever.
    # (Devin Review on #1263.)
    name = (payload["name"] or "").strip()
    slug = (payload["slug"] or "").strip()
    if not name or not slug:
        # Present-but-empty is as unusable as absent, and the caller maps a
        # KeyError to 400 invalid_payload — creating a nameless, slugless
        # domain instead would be the worst of both. (Devin Review on #1263.)
        raise KeyError("name" if not name else "slug")
    domain_id = memory_domains_repo().create(
        name=name,
        slug=slug,
        description=payload.get("description"),
        icon=None,
        color=None,
        created_by=by,
    )
    try:
        seed_domain_item(
            slug=slug,
            name=name,
            content=payload.get("content"),
            content_title=payload.get("content_title"),
            # The SUBMITTER, not the approver — `by` is the admin who approved,
            # and attributing someone else's knowledge to them would be wrong in
            # the one field corporate memory uses to say who knows this.
            source_user=submitted_by or by,
        )
    except Exception:
        # The caller reopens the suggestion on failure so the admin can retry —
        # but the domain is already created, and a retry then dies on the
        # duplicate slug, leaving the submission impossible to approve and an
        # empty domain nobody asked for. Undo our own half before letting the
        # error out. (Devin Review on #1263.)
        try:
            # HARD delete: the soft one only stamps `deleted_at`, and the slug
            # stays unique-constrained — a retry would still collide, which is
            # the whole failure being undone. Nothing was ever published under
            # this domain; it was created moments ago in this same request.
            memory_domains_repo().hard_delete(domain_id)
        except Exception:
            logger.exception(
                "authoring: seeding failed for domain %s and the rollback failed too — "
                "the domain must be deleted by hand before this suggestion can be approved",
                domain_id,
            )
        raise
    return domain_id


def _replay_mcp(payload: dict, by: str, submitted_by: Optional[str] = None) -> str:
    # Re-validate transport/shape via the endpoint's own request model.
    from app.api.admin_mcp import CreateMCPSourceRequest, _require_safe_source_name
    from src.repositories import mcp_sources_repo

    req = CreateMCPSourceRequest(**payload)
    name = (req.name or "").strip()
    _require_safe_source_name(name)
    repo = mcp_sources_repo()
    if repo.get_by_name(name) is not None:
        raise ValueError("name_exists")
    source_id = str(uuid.uuid4())
    repo.upsert(
        id=source_id,
        name=name,
        transport=req.transport,
        command=req.command,
        args=req.args,
        env=req.env,
        url=req.url,
        auth_method=req.auth_method,
        auth_secret_env=req.auth_secret_env,
        enabled=req.enabled,
        scope=req.scope or "shared",
    )
    return source_id


def _replay_marketplace(payload: dict, by: str, submitted_by: Optional[str] = None) -> str:
    from app.api.marketplaces import CreateMarketplaceRequest
    from src.repositories import marketplace_registry_repo

    req = CreateMarketplaceRequest(**payload)
    repo = marketplace_registry_repo()
    if repo.get(req.slug) is not None:
        raise ValueError("slug_exists")
    repo.register(
        id=req.slug,
        name=req.name,
        url=req.url,
        branch=req.branch,
        description=req.description,
        registered_by=by,
        curator_name=req.curator_name,
        curator_email=req.curator_email,
    )
    return req.slug


def _replay_semantic_model(payload: dict, by: str, submitted_by: Optional[str] = None) -> str:
    """Apply a proposed Ossie document through the SAME pipeline the admin
    ``/api/semantic-models/apply`` branch uses — full document re-validation
    plus the source-ownership guard run again here, so a payload that rotted
    while pending (or a slug an imported model claimed meanwhile) fails the
    approve with 409 ``create_failed`` and the suggestion reopens, rather
    than shadowing an imported model or storing a half-valid document.
    ``SemanticApplyError`` is a ``ValueError``, so the caller's generic
    exception mapping needs nothing special."""
    from app.api.semantic_models import apply_manual_model

    row = apply_manual_model(
        document=payload["document"],
        description=payload.get("description"),
        expected_content_hash=payload.get("expected_content_hash"),
    )
    return row["id"]


_SAFE_REPLAY = {
    "data-package": _replay_data_package,
    "corporate-memory": _replay_corporate_memory,
    "mcp": _replay_mcp,
    "marketplace": _replay_marketplace,
    "semantic-layer": _replay_semantic_model,
}

public_router = APIRouter(prefix="/api/studio", tags=["authoring-suggestions"])
admin_router = APIRouter(prefix="/api/admin", tags=["authoring-suggestions"])


class CreateSuggestionBody(BaseModel):
    domain: str
    payload: Dict[str, Any]


class ResolveBody(BaseModel):
    note: Optional[str] = None


@public_router.post("/suggestions", status_code=201)
async def submit_suggestion(
    body: CreateSuggestionBody,
    user: dict = Depends(get_current_user),
):
    _require_studio_enabled()
    spec = get_domain(body.domain)
    if spec is None:
        raise HTTPException(status_code=400, detail={"kind": "unknown_domain", "hint": body.domain})
    if spec.submit_directly:
        raise HTTPException(
            status_code=400,
            detail={"kind": "domain_submits_directly", "hint": spec.endpoint},
        )
    if not body.payload:
        raise HTTPException(status_code=400, detail={"kind": "empty_payload"})
    sid = authoring_suggestions_repo().create(domain=body.domain, payload=body.payload, created_by=user["email"])
    audit_repo().log(
        user_id=user["id"],
        action="authoring_suggestion.submit",
        resource=sid,
        params={"domain": body.domain},
    )
    return {"id": sid, "status": "pending"}


@public_router.get("/suggestions/mine")
async def my_suggestions(
    user: dict = Depends(get_current_user),
):
    _require_studio_enabled()
    return authoring_suggestions_repo().list(created_by=user["email"])


@admin_router.get("/authoring-suggestions")
async def list_suggestions(
    status: Optional[str] = None,
    domain: Optional[str] = None,
    _admin: dict = Depends(require_admin),
):
    _require_studio_enabled()
    return authoring_suggestions_repo().list(status=status, domain=domain)


@admin_router.post("/authoring-suggestions/{sid}/approve")
async def approve_suggestion(
    sid: str,
    body: ResolveBody,
    admin: dict = Depends(require_admin),
):
    _require_studio_enabled()
    repo = authoring_suggestions_repo()
    sug = repo.get(sid)
    if sug is None:
        raise HTTPException(status_code=404, detail={"kind": "not_found"})
    # Atomically CLAIM the suggestion (pending -> approved) BEFORE the
    # side-effecting replay. On a concurrent approve (e.g. PG multi-worker) only
    # the admin who wins the flip runs replay(); the loser gets 409 and never
    # creates a duplicate/orphan resource. If replay then fails, reopen() rolls
    # the claim back to pending so the admin can retry.
    if not repo.resolve(sid, status="approved", resolved_by=admin["email"], resolution_note=body.note):
        raise HTTPException(status_code=409, detail={"kind": "already_resolved"})
    created_resource_id = None
    replay = _SAFE_REPLAY.get(sug["domain"])
    if replay is not None:
        try:
            created_resource_id = replay(sug.get("payload") or {}, admin["email"], sug.get("created_by"))
        except KeyError as exc:
            repo.reopen(sid)
            raise HTTPException(status_code=400, detail={"kind": "invalid_payload", "hint": str(exc)})
        except Exception as exc:  # validation / UNIQUE collision — roll the claim back
            repo.reopen(sid)
            raise HTTPException(status_code=409, detail={"kind": "create_failed", "hint": str(exc)})
        repo.set_created_resource_id(sid, created_resource_id)
    audit_repo().log(
        user_id=admin["id"],
        action="authoring_suggestion.approved",
        resource=sid,
        params={"note": body.note, "created_resource_id": created_resource_id},
    )
    return {"id": sid, "status": "approved", "created_resource_id": created_resource_id}


@admin_router.post("/authoring-suggestions/{sid}/reject")
async def reject_suggestion(
    sid: str,
    body: ResolveBody,
    admin: dict = Depends(require_admin),
):
    _require_studio_enabled()
    return _resolve(sid, "rejected", body.note, admin)


def _resolve(
    sid: str,
    status: str,
    note: Optional[str],
    admin: dict,
    created_resource_id: Optional[str] = None,
) -> dict:
    repo = authoring_suggestions_repo()
    if repo.get(sid) is None:
        raise HTTPException(status_code=404, detail={"kind": "not_found"})
    flipped = repo.resolve(
        sid,
        status=status,
        resolved_by=admin["email"],
        resolution_note=note,
        created_resource_id=created_resource_id,
    )
    if not flipped:
        raise HTTPException(status_code=409, detail={"kind": "already_resolved"})
    audit_repo().log(
        user_id=admin["id"],
        action=f"authoring_suggestion.{status}",
        resource=sid,
        params={"note": note, "created_resource_id": created_resource_id},
    )
    return {"id": sid, "status": status, "created_resource_id": created_resource_id}
