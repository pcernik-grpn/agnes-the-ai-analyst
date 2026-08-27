"""Fact graph over Collections — read + write surfaces (build order steps
2+3+4 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

All routes sit behind the ``facts`` feature flag
(``app.auth.access.require_facts_enabled`` — router-level, so the whole
``/api/facts*`` surface answers `404` when the flag is off):

Read surface (build order steps 2+3), any authenticated caller:

- ``POST /api/facts/search``          — typed subject search with
  attribute filters, projected per-caller (spec §12).
- ``POST /api/facts/neighbors``       — bounded graph traversal from one
  subject (depth <= 2, capped fanout/result, statement-timeout guarded).
- ``GET  /api/facts/{subject_id}/claims`` — the readable evidence for one
  subject: quote, document, attrs, document_date.

Read auth: ``Depends(get_current_user)``, the SAME dependency the agent/PAT
paths resolve through (it can yield a plain ``dict`` user OR a restricted
``Principal`` such as ``AgentPrincipal``; see ``require_session_or_user_pat``'s
docstring in ``app/auth/dependencies.py`` for the isinstance check other
endpoints use on its result). There is deliberately **no** admin gate and
**no** ``require_resource_access`` on these three routes — the tools are not
collection-scoped in their signatures (``subject_id`` names a fact/edge, not
a collection), so the declarative route gate does not apply; **every bit of
enforcement lives in ``src/repositories/facts_pg.py``**'s shared visibility
helper (spec §5). Never add a general SQL escape hatch over these tables on
any surface.

Write surface (build order step 4), ``Depends(require_admin)`` — accepts
either a human admin session/PAT or the scheduler shared-secret bearer token
(``app/auth/scheduler_token.py`` resolves it to the synthetic
``scheduler@system.local`` user, a member of the ``Admin`` group, through
``get_current_user`` — the SAME dual-accept pattern ``app/api/jobs.py`` uses,
no special-casing needed here). CSRF is n/a — bearer auth only, never a
cookie session:

- ``POST /api/facts/ingest``          — the producer contract (spec §7.2):
  batch caps, doc_id resolution, the verbatim gate (§8), union/replace
  modes, alias/edge resolution, correction re-attachment, the orphan sweep.
  Returns the run report.
- ``PUT/DELETE /api/facts/corrections/{subject_kind}/{subject_id}`` — admin
  correction management (spec §4): ``wrong``/``restricted``/``revealed``,
  each reasoned and audit-logged.
- ``GET  /api/facts/corrections``      — the producer export (spec §7.4):
  every ``wrong`` subject's natural keys, so re-extraction does not
  resurrect what an admin withdrew.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import require_admin, require_facts_enabled
from app.auth.dependencies import get_current_user
from src.repositories import audit_repo, facts_repo
from src.repositories.facts_pg import (
    FactNotFound,
    IngestBatchTooLarge,
    IngestDocumentExceedsClaimCap,
    IngestUnresolvedDocIds,
)

router = APIRouter(
    prefix="/api/facts",
    tags=["facts"],
    dependencies=[Depends(require_facts_enabled)],
)


class FactsSearchRequest(BaseModel):
    type: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    limit: int = Field(default=20, ge=1, le=100)


class FactsNeighborsRequest(BaseModel):
    subject_id: str
    edge_types: Optional[List[str]] = None
    depth: int = Field(default=1, ge=1, le=2)
    fanout: int = Field(default=100, ge=1, le=100)
    limit: int = Field(default=500, ge=1, le=500)


@router.post("/search")
def facts_search(body: FactsSearchRequest, user=Depends(get_current_user)) -> Dict[str, Any]:
    """Search typed subjects (facts) by ``type`` and attribute ``filters``.

    Visibility (spec §4/§5) is enforced entirely in the repository: a
    subject is returned only if the caller can read at least one of its
    claims (or ALL of them, under `facts.visibility_mode: all_evidence`),
    or it carries an active `revealed` correction. ``attrs`` are projected
    from readable claims only, per-key latest-document_date-wins with a
    `conflicted` marker on a genuine tie — the projection runs in SQL so
    `filters` evaluate against it BEFORE the `limit` is applied (never a
    Python post-filter, which would leak a shortfall signal). Response:
    ``{"subjects": [{"id", "type", "aliases", "attrs", "claim_count",
    "quote_count", "revealed"}], "limit_applied"}`` — `limit_applied` is
    True only when the CALLER'S OWN readable result set exceeds `limit`,
    never a signal that grants hid additional matches.
    """
    try:
        return facts_repo().search(user, type=body.type, filters=body.filters or {}, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/neighbors")
def facts_neighbors(body: FactsNeighborsRequest, user=Depends(get_current_user)) -> Dict[str, Any]:
    """Bounded graph traversal from one subject.

    An edge appears in the response only if it has its own readable claim
    (never inferred from its endpoints' visibility) AND both its endpoint
    facts are independently visible to the caller — traversal never walks
    from a visible subject into one whose claims are unreadable, and never
    reveals that a path continues past it (spec §5 rule 3). `depth`
    defaults to 1 (max 2), `fanout` per node and total `limit` are capped,
    and the underlying query runs under a Postgres statement timeout.
    Response: ``{"nodes": [...], "edges": [...], "truncated": {"depth",
    "fanout", "result"}}``. `404` (never `403`) when `subject_id` does not
    exist or has no readable claim — indistinguishable from the caller's
    point of view (spec §5 rule 2).
    """
    try:
        return facts_repo().neighbors(
            user,
            body.subject_id,
            edge_types=body.edge_types,
            depth=body.depth,
            fanout=body.fanout,
            limit=body.limit,
        )
    except FactNotFound:
        raise HTTPException(status_code=404, detail="fact_not_found")


@router.get("/{subject_id}/claims")
def facts_claims(subject_id: str, user=Depends(get_current_user)) -> Dict[str, Any]:
    """The caller's readable evidence for one subject (fact or edge).

    Each entry carries the evidencing document's identity (``corpus_id``,
    ``corpus_file_id``, ``document: {name, path, source_url?}``), the
    verbatim ``quote``, that claim's own (unprojected) ``attrs``, and
    ``document_date``. A subject under an active `revealed` correction is
    served to every authenticated caller regardless of grants, but with
    every ``quote`` suppressed to an empty string (`"revealed": true` on
    the response marks this). `404` (never `403`) when `subject_id` does
    not exist or has no readable claim — same status, body shape, and cost
    profile either way (spec §5 rule 2): this endpoint never lets a caller
    distinguish "nothing there" from "something you can't see".
    """
    # authz: repository-enforced — a subject id names a fact/edge, not a
    # collection, so no route-template gate can express the check; every
    # repo read method filters by the caller (spec §5), tested S1-S6.
    try:
        return facts_repo().claims(user, subject_id)
    except FactNotFound:
        raise HTTPException(status_code=404, detail="fact_not_found")


# ---------------------------------------------------------------------------
# write path (build order step 4) — ingest + corrections management.
# ---------------------------------------------------------------------------


class FactsIngestRequest(BaseModel):
    """Wire format accepted verbatim (spec §7.0/§7.2) — ``documents`` are the
    crawler's ``make_row`` rows each EXTENDED with ``corpus_id``; ``nodes``/
    ``edges`` carry ``{id/src+type+dst, attrs, evidence: [{doc_id, quote}]}``.
    Deliberately plain ``Dict[str, Any]`` items rather than a strict nested
    schema — the producer contract explicitly tolerates unknown fields
    (underscore-prefixed crawler internals are stripped server-side, not
    rejected), so a rigid Pydantic model would reject valid producer input
    on every crawler-side field addition."""

    documents: List[Dict[str, Any]] = Field(default_factory=list)
    full_documents: List[str] = Field(default_factory=list)
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)


@router.post("/ingest")
def facts_ingest(body: FactsIngestRequest, user=Depends(require_admin)) -> Dict[str, Any]:
    """Ingest one producer batch (spec §7.2) — scheduler token or admin PAT.

    Batch caps (≤500 documents, ≤5000 claims/request) 413; a single
    document's evidence alone exceeding the claim cap is a distinct 422
    protocol error (never split across requests, per §7.2). ``documents``
    may be omitted only when every evidence ``doc_id`` already resolves
    through a prior upload's ``corpus_file_sources`` mapping — otherwise
    400 with the unresolved ids itemized. Everything else — the verbatim
    gate, deferred-vs-rejected, union vs `full_documents` replace, alias/
    edge resolution, correction re-attachment, the post-ingest orphan
    sweep — happens in :meth:`FactsPgRepository.ingest_batch`; this
    handler only translates its typed exceptions to HTTP status codes.
    Response IS the run report: ``{claims_written, claims_rejected:
    [{row, reason}], deferred: [...], subjects_created, subjects_deleted,
    corrections_active: [...], review_items: [...]}``.
    """
    try:
        return facts_repo().ingest_batch(
            documents=body.documents,
            full_documents=body.full_documents,
            nodes=body.nodes,
            edges=body.edges,
        )
    except IngestBatchTooLarge as exc:
        raise HTTPException(status_code=413, detail=exc.detail)
    except IngestDocumentExceedsClaimCap as exc:
        raise HTTPException(
            status_code=422,
            detail={"reason": "document_exceeds_claim_cap", "doc_id": exc.doc_id, "count": exc.count},
        )
    except IngestUnresolvedDocIds as exc:
        raise HTTPException(status_code=400, detail={"reason": "unresolved_doc_ids", "doc_ids": exc.unresolved})


class FactsCorrectionRequest(BaseModel):
    verdict: str = Field(pattern="^(wrong|restricted|revealed)$")
    reason: str = Field(min_length=1, max_length=2000)


@router.put("/corrections/{subject_kind}/{subject_id}")
def upsert_correction(
    subject_kind: str,
    subject_id: str,
    body: FactsCorrectionRequest,
    user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Record (or replace) an admin correction on one fact or edge (spec
    §4): ``wrong`` (withheld everywhere, exported to the producer so
    re-extraction does not resurrect it — §7.4), ``restricted`` (withheld
    regardless of grants — legal hold), or ``revealed`` (served without
    quotes to every authenticated caller instance-wide, regardless of
    grants). ``natural_keys`` is snapshotted from the subject's CURRENT
    aliases (fact) or endpoint aliases (edge) at write time, so a subject
    later deleted and re-created re-attaches this correction (spec §3).
    Every decision is audit-logged with its ``reason``.
    """
    if subject_kind not in ("fact", "edge"):
        raise HTTPException(status_code=422, detail="invalid_subject_kind")
    repo = facts_repo()
    natural_keys = repo.natural_keys_for(subject_kind, subject_id)
    repo.upsert_correction(
        subject_kind=subject_kind,
        subject_id=subject_id,
        natural_keys=natural_keys,
        verdict=body.verdict,
        reason=body.reason,
        decided_by=user.get("email") or user.get("id", "admin"),
    )
    audit_repo().log(
        user_id=user.get("id"),
        action="facts.correction.upsert",
        resource=f"{subject_kind}/{subject_id}",
        params={"verdict": body.verdict, "reason": body.reason},
    )
    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "verdict": body.verdict,
        "reason": body.reason,
        "natural_keys": natural_keys,
    }


@router.delete("/corrections/{subject_kind}/{subject_id}", status_code=204)
def delete_correction(
    subject_kind: str,
    subject_id: str,
    user: dict = Depends(require_admin),
) -> None:
    """Remove a correction — the subject reverts to normal grant-based
    visibility (spec §4). Idempotent: deleting an already-absent
    correction still returns 204."""
    if subject_kind not in ("fact", "edge"):
        raise HTTPException(status_code=422, detail="invalid_subject_kind")
    facts_repo().delete_correction(subject_kind=subject_kind, subject_id=subject_id)
    audit_repo().log(
        user_id=user.get("id"),
        action="facts.correction.delete",
        resource=f"{subject_kind}/{subject_id}",
    )


@router.get("/corrections")
def list_corrections(user: dict = Depends(require_admin)) -> Dict[str, Any]:
    """The producer export (spec §7.4): every ``wrong`` subject with its
    ``natural_keys`` snapshot, so a producer's re-extraction pass can prune
    them before re-asserting claims. Server-side, corrections are ALSO
    enforced at read time regardless (§4) — a producer that ignores this
    export cannot resurrect a withheld fact, this just saves it the wasted
    work. Scheduler token or admin PAT."""
    return {"corrections": facts_repo().list_wrong_corrections()}
