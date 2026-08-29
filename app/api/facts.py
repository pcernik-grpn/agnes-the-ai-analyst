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
- ``GET  /api/facts/ingest-runs``      — persisted run reports (spec
  §7.2/§13.2), newest first: what the ``/admin/data-sources`` source card
  reads for its pipeline-strip counts and per-category error badges.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.access import require_admin, require_facts_enabled
from app.auth.dependencies import get_current_user
from src.audit_helpers import identity_for_audit, log_safe
from src.repositories import audit_repo, facts_ingest_runs_repo, facts_repo
from src.repositories.facts_pg import (
    FactNotFound,
    IngestBatchTooLarge,
    IngestDocumentExceedsClaimCap,
    IngestReservedStableId,
    IngestUnresolvedDocIds,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/facts",
    tags=["facts"],
    dependencies=[Depends(require_facts_enabled)],
)


class FactsSearchRequest(BaseModel):
    # extra='forbid' (TCRD follow-up from a live finding): this endpoint had
    # no free-text parameter at all, so an unknown field like the `q` a
    # caller might guess at was silently swallowed by pydantic's default
    # extra='ignore' — the call degenerated to an unfiltered, id-ordered
    # dump instead of erroring. An unknown field on ANY facts request model
    # must 422, never be swallowed into a convincing wrong answer.
    model_config = ConfigDict(extra="forbid")

    type: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    q: Optional[str] = Field(default=None, max_length=200)
    limit: int = Field(default=20, ge=1, le=100)


class FactsNeighborsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_id: str
    edge_types: Optional[List[str]] = None
    depth: int = Field(default=1, ge=1, le=2)
    fanout: int = Field(default=100, ge=1, le=100)
    limit: int = Field(default=500, ge=1, le=500)


DEFAULT_FACET_TYPES = ("client", "industry", "service_offering", "doc_type")


@router.get("/facets")
def facts_facets(
    types: Optional[str] = Query(
        default=None,
        description="Comma-separated fact types to facet on. Defaults to the Library's four.",
        max_length=200,
    ),
    limit_per_type: int = Query(default=50, ge=1, le=200),
    user=Depends(get_current_user),
) -> Dict[str, Any]:
    """Filterable entity values per type, with a document count each — what
    the Library's filter menu offers instead of hand-entered tags.

    The vocabulary comes from the extraction pass (client, industry, service
    offering, document type), so it is maintained by ingestion rather than by
    somebody remembering to tag a file.

    Gated exactly as :func:`facts_search` is: a facet lists only subjects
    this caller could reach, and each `document_count` counts only documents
    in collections they can read — a `revealed` subject's unreadable evidence
    is not tallied, since that would report how many files sit in a
    collection they cannot open. Response: ``{"facets": {type: [{"subject_id",
    "label", "document_count"}]}}``.
    """
    wanted = [t.strip() for t in types.split(",") if t.strip()] if types else list(DEFAULT_FACET_TYPES)
    if not wanted:
        raise HTTPException(status_code=422, detail="no facet types requested")
    if len(wanted) > 12:
        raise HTTPException(status_code=422, detail="too many facet types (max 12)")
    return {"facets": facts_repo().facet_values(user, types=wanted, limit_per_type=limit_per_type)}


@router.get("/type-map")
def facts_type_map(user=Depends(get_current_user)) -> Dict[str, Any]:
    """Live counts per node type over everything the caller can see — the
    head of the Library's Knowledge tab, where each type is a way in.

    Same visibility gate as :func:`facts_search` with no ``type``, just
    aggregated: a type's ``count`` is exactly how many subjects a
    ``search(type=...)`` would let this caller reach. A type nobody can see
    is absent rather than reported as ``0``, so the response never
    distinguishes "no such type here" from "none you may read" — the same
    non-disclosure ``search()`` makes. Response: ``{"types": [{"type",
    "count"}], "total"}``, ordered by type.
    """
    counts = facts_repo().count_visible_facts_by_type(user)
    return {
        "types": [{"type": t, "count": n} for t, n in counts.items()],
        "total": sum(counts.values()),
    }


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
    Python post-filter, which would leak a shortfall signal). ``q`` is an
    OPTIONAL free-text name lookup matched against alias natural keys ONLY
    (never claim text) — see :meth:`FactsPgRepository.search` for the
    normalization and ranking rules. Response: ``{"subjects": [{"id",
    "type", "aliases", "attrs", "claim_count", "quote_count", "revealed"}],
    "limit_applied"}`` — `limit_applied` is True only when the CALLER'S OWN
    readable result set exceeds `limit`, never a signal that grants hid
    additional matches.
    """
    try:
        result = facts_repo().search(user, type=body.type, filters=body.filters or {}, q=body.q, limit=body.limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="facts.search",
        params={"type": body.type, "result_count": len(result.get("subjects", [])), "limit": body.limit},
    )
    return result


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
        result = facts_repo().neighbors(
            user,
            body.subject_id,
            edge_types=body.edge_types,
            depth=body.depth,
            fanout=body.fanout,
            limit=body.limit,
        )
    except FactNotFound:
        raise HTTPException(status_code=404, detail="fact_not_found")
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="facts.neighbors",
        resource=f"fact:{body.subject_id}",
        params={"node_count": len(result.get("nodes", [])), "edge_count": len(result.get("edges", []))},
    )
    return result


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
        result = facts_repo().claims(user, subject_id)
    except FactNotFound:
        raise HTTPException(status_code=404, detail="fact_not_found")
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="facts.claims",
        resource=f"fact:{subject_id}",
        params={"claim_count": len(result.get("claims", []))},
    )
    return result


# ---------------------------------------------------------------------------
# write path (build order step 4) — ingest + corrections management.
# ---------------------------------------------------------------------------


class FactsIngestAnonymizationScope(BaseModel):
    """One corpus's anonymization tally within this batch (spec §9.2). A
    self-reported COUNT, never independently verified by Agnes — see
    ``docs/anonymization.md``'s "current limits"."""

    docs_anonymized: int = Field(default=0, ge=0)
    docs_skipped: int = Field(default=0, ge=0)


class FactsIngestAnonymizationReport(BaseModel):
    """OPTIONAL producer declaration that (some of) this batch went through
    the anonymize-in-front pipeline (spec §9: source -> crawl -> convert ->
    anonymize -> Agnes) before ingestion. Additive to the wire contract — a
    producer that never anonymizes omits this field entirely.

    ``scopes`` is keyed by ``corpus_id`` (the collection id, matching the
    connect wizard's own scope-row shape); a key that does not resolve to a
    real collection is KEPT, not rejected — this is a self-reported tally,
    not a join against ``file_corpora`` (spec §9.2's "current limits": Agnes
    records the declaration, it cannot verify the content was anonymized).
    """

    declared: bool = False
    scopes: Dict[str, FactsIngestAnonymizationScope] = Field(default_factory=dict)


class FactsIngestRequest(BaseModel):
    """Wire format accepted verbatim (spec §7.0/§7.2) — ``documents`` are the
    crawler's ``make_row`` rows each EXTENDED with ``corpus_id``; ``nodes``/
    ``edges`` carry ``{id/src+type+dst, attrs, evidence: [{doc_id, quote}]}``.
    Deliberately plain ``Dict[str, Any]`` items rather than a strict nested
    schema — the producer contract explicitly tolerates unknown fields
    (underscore-prefixed crawler internals are stripped server-side, not
    rejected), so a rigid Pydantic model would reject valid producer input
    on every crawler-side field addition.

    ``anonymization`` is the one exception: a small, OPTIONAL, strictly
    typed block (spec §9.2) — malformed input there is a real protocol
    error (422), not tolerated crawler noise."""

    documents: List[Dict[str, Any]] = Field(default_factory=list)
    full_documents: List[str] = Field(default_factory=list)
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    anonymization: Optional[FactsIngestAnonymizationReport] = None


@router.post("/ingest")
def facts_ingest(body: FactsIngestRequest, user=Depends(require_admin)) -> Dict[str, Any]:
    """Ingest one producer batch (spec §7.2) — scheduler token or admin PAT.

    Batch caps (≤500 documents, ≤5000 claims/request) 413; a single
    document's evidence alone exceeding the claim cap is a distinct 422
    protocol error (never split across requests, per §7.2). ``documents``
    may be omitted only when every evidence ``doc_id`` already resolves
    through a prior upload's ``corpus_file_sources`` mapping — otherwise
    400 with the unresolved ids itemized. A byte-identical copy (TCRD-241)
    can anchor more than one ``corpus_file_sources`` row for the same
    ``doc_id``; resolution is corpus-scoped and deterministic (indexed
    copies preferred, ``corpus_file_id`` as tiebreak) — never an arbitrary
    cross-collection pick, since that would mis-scope a claim's visibility.
    When this batch's ``documents[]`` declared at least one collection but a
    cited ``doc_id`` is anchored only in some OTHER, undeclared collection,
    that claim is REJECTED (``ambiguous_cross_collection_doc_id``, itemized
    in ``claims_rejected`` like any other reason) rather than written under
    a collection wider than the producer's batch ever declared (RBAC
    review, PR #1736) — nothing is ever silently attached cross-collection.
    An entirely `documents[]`-omitted batch (the "already resolves" replay
    above) has no batch-declared scope to escape and is unaffected.
    Everything else — the verbatim gate, deferred-vs-rejected, union vs
    `full_documents` replace, alias/edge resolution, correction
    re-attachment, the post-ingest orphan sweep — happens in
    :meth:`FactsPgRepository.ingest_batch`; this
    handler only translates its typed exceptions to HTTP status codes.
    Response IS the run report: ``{claims_written,
    claims_accepted_via_identity, claims_rejected: [{row, reason}],
    source_urls_rejected: [{doc_id, reason}], deferred: [...],
    subjects_created, subjects_deleted, corrections_active: [...],
    review_items: [...]}``.

    ``claims_accepted_via_identity`` (spec §8) is the subset of
    ``claims_written`` whose quote passed the verbatim gate ONLY via the
    document's own SERVER-STORED ``filename``/``path`` — never a chunk of
    its extracted text — so an operator can see how much evidence is
    filename-grounded rather than content-grounded.

    ``source_urls_rejected`` (O7 follow-up) is a document's ``source_url``
    the validator dropped as invalid (``too_long`` / ``unparseable`` /
    ``not_https`` / ``no_host``) — the claim itself still wrote, only its
    citation link is missing; a producer that never sends ``source_url`` is
    not itemized here at all, only one that sends a value Agnes refuses.

    A copy of that same report is ALSO persisted to ``facts_ingest_runs``
    (``GET /api/facts/ingest-runs``, the source card's pipeline strip and
    error badges — spec §13.2) — deliberately AFTER
    :meth:`FactsPgRepository.ingest_batch` has already committed and OUTSIDE
    its transaction: a run-report write is a side record, never a condition
    of the ingest succeeding, so a failure there is logged and swallowed,
    never surfaced as a 5xx for a batch that in fact wrote its claims fine.

    ``anonymization`` (spec §9.2, optional) rides along INTO that persisted
    run report only — never into the returned report above, and never
    joined against real corpus ids (a corpus id the batch's own
    ``documents`` never mention is kept, not rejected: this is the
    producer's self-reported tally of what it anonymized, not something
    Agnes independently verifies — see ``docs/anonymization.md``).
    """
    try:
        report = facts_repo().ingest_batch(
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
    except IngestReservedStableId as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "reserved_source_stable_id", "stable_ids": exc.stable_ids},
        )

    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="facts.ingest",
        params={
            "documents": len(body.documents),
            "claims_written": report.get("claims_written", 0),
            "claims_rejected": len(report.get("claims_rejected", [])),
        },
    )

    try:
        corpus_ids = sorted({d.get("corpus_id") for d in body.documents if d.get("corpus_id")})
        facts_ingest_runs_repo().create(
            corpus_ids=corpus_ids,
            caller=user.get("email") or user.get("id", "admin"),
            documents_seen=len(body.documents),
            claims_written=report.get("claims_written", 0),
            claims_rejected=report.get("claims_rejected", []),
            source_urls_rejected=report.get("source_urls_rejected", []),
            deferred=report.get("deferred", []),
            subjects_created=report.get("subjects_created", 0),
            subjects_deleted=report.get("subjects_deleted", 0),
            review_items=report.get("review_items", []),
            anonymization=body.anonymization.model_dump() if body.anonymization else None,
        )
    except Exception:  # noqa: BLE001 — never let a report-write failure look like an ingest failure
        logger.warning("facts.ingest: failed to persist the run report (ingest itself succeeded)", exc_info=True)

    return report


class FactsCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


@router.get("/ingest-runs")
def list_ingest_runs(
    limit: int = Query(default=20, ge=1, le=200),
    user: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Persisted run reports (spec §7.2/§13.2), newest first — what the
    ``/admin/data-sources`` source card reads its pipeline-strip counts and
    per-category error badges from (``app/web/router.py``'s
    ``_sharepoint_pipeline_cell``). ``facts_ingest_runs_repo()`` is PG-only
    (A3 ratchet); on a DuckDB-backed instance this raises
    ``RequiresPostgresBackend``, translated to a typed ``501`` by the
    app-wide handler."""
    return {"runs": facts_ingest_runs_repo().list_recent(limit=limit)}
