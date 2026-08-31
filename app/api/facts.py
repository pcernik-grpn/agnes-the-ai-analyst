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
cookie session. ``POST /api/facts/ingest`` and ``GET /api/facts/corrections``
use ``Depends(require_admin_or_producer)`` instead — the SAME admin/scheduler
acceptance, plus a ``ProducerPrincipal`` (a corpus-extraction producer's own
scoped callback credential, ``app.auth.producer_token``), scope-checked
per-document at ``/ingest`` and unfiltered (documented TODO) at
``/corrections``:

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
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth.access import require_admin, require_admin_or_producer, require_facts_enabled
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


def _anonymize_marked_corpus_ids() -> set:
    """Collection ids at least one SharePoint connection's confirmed scope
    marks ``anonymize=true`` (the connect wizard's step-2 checkbox,
    ``app/api/admin_sharepoint.py``, stored on the connection's own
    ``config.scopes[].anonymize`` — see that module's docstring) — the
    admin's INTENT half of "requested vs declared" (``docs/anonymization.md``).

    Read straight off ``source_connections`` (a DuckDB<->PG frozen pair, so
    this answers on either backend) rather than a cache: cheap — an instance
    has few SharePoint connections, each a small JSON config — and always
    current, with nothing to invalidate on a wizard edit.
    """
    from src.repositories import source_connections_repo

    out: set = set()
    for connection in source_connections_repo().list(source_type="sharepoint"):
        scopes = (connection.get("config") or {}).get("scopes")
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if isinstance(scope, dict) and scope.get("anonymize") and scope.get("collection_id"):
                out.add(str(scope["collection_id"]))
    return out


def _refuse_undeclared_anonymize_marked_corpora(body: "FactsIngestRequest") -> None:
    """Fail-closed gate (anonymize-fail-closed hardening): refuse a batch
    that carries a document for a corpus whose SharePoint scope is
    anonymize-marked unless THIS batch's own ``anonymization`` block
    declares that corpus.

    This is the enforcement point, independent of whether the connect
    wizard's ``config.scopes`` survived an unrelated edit, whether the
    per-instance HMAC key is configured, or whether the producer remembered
    to run the anonymizer — none of that machinery has to fail loudly for
    THIS gate to hold, because it does not trust any of it: it re-derives
    "was this corpus supposed to be anonymized" from the connection's own
    scope rows on every call and refuses outright when the batch does not
    say it happened. Called before ``facts_repo().ingest_batch()`` — a
    refusal here means nothing from this batch is ever written.

    A lookup failure (the ``source_connections`` table unreadable) is NOT
    treated as "nothing is marked" — that would silently accept plaintext
    into a corpus this instance cannot currently prove is safe. It is
    refused the same way, with its own reason, so a transient failure never
    degrades into the exact silent-accept this gate exists to close.
    """
    requested_corpus_ids = {d.get("corpus_id") for d in body.documents if d.get("corpus_id")}
    if not requested_corpus_ids:
        return
    try:
        anonymize_marked = _anonymize_marked_corpus_ids()
    except Exception as exc:  # noqa: BLE001 — fail closed, not open
        logger.exception(
            "facts.ingest: anonymize-mark lookup failed; refusing rather than risking a silent un-anonymized accept"
        )
        raise HTTPException(
            status_code=503,
            detail={
                "reason": "anonymization_check_unavailable",
                "message": (
                    "could not determine whether the target corpus is anonymize-marked; refusing this "
                    "batch rather than risking an un-anonymized write"
                ),
            },
        ) from exc
    declared_corpus_ids = set(body.anonymization.scopes.keys()) if body.anonymization is not None else set()
    undeclared = sorted(requested_corpus_ids & anonymize_marked - declared_corpus_ids)
    if undeclared:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "anonymization_not_declared",
                "corpus_ids": undeclared,
                "message": (
                    "these collections are anonymize-marked in the SharePoint connect wizard, but this "
                    "batch's `anonymization` block does not declare them — include the corpus id(s) under "
                    "anonymization.scopes, or unmark the scope in the connect wizard if this batch is "
                    "intentionally unanonymized"
                ),
            },
        )


def _batch_references_a_doc_id(body: "FactsIngestRequest") -> bool:
    """True if anything in this batch would make ``ingest_batch`` resolve a
    ``doc_id`` — a ``full_documents`` entry (replace mode: it DELETES that
    document's existing claims) or any node/edge evidence naming one.

    Tolerant of shape, like the rest of this endpoint: ``nodes``/``edges``
    are deliberately ``Dict[str, Any]`` so an unknown producer-side field
    is not a 422, which means ``evidence`` may be absent or not a list and
    an element may not be a dict. Anything unreadable counts as "no doc_id
    here" rather than raising — the repository is the layer that rejects a
    malformed batch, and this helper only decides whether the scope gate
    above has to fire.
    """
    if body.full_documents:
        return True
    for item in (*body.nodes, *body.edges):
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            continue
        for ev in evidence:
            if isinstance(ev, dict) and ev.get("doc_id"):
                return True
    return False


def _refuse_producer_out_of_scope_documents(body: "FactsIngestRequest", user: Any) -> None:
    """Authorization-level gate for a ``ProducerPrincipal`` caller: every
    ``documents[]`` row's ``corpus_id`` must be one of the token's own
    ``collection_ids`` — a producer scoped to collections A/B must never
    write into collection C just because it can reach this endpoint at
    all.

    Independent of (and checked BEFORE) ``FactsPgRepository.ingest_batch``'s
    own ``ambiguous_cross_collection_doc_id`` handling, which is a
    data-integrity rule about EVIDENCE anchoring, not an authorization
    boundary — a document naming an in-scope ``corpus_id`` here can still
    be rejected by that other rule for an unrelated reason.

    A no-op for every other caller (human admin, or the scheduler token
    resolving to the admin user) — neither has a ``collection_ids`` claim
    to check against, and neither is scope-restricted this way.
    """
    from app.auth.session_principal import ProducerPrincipal

    if not isinstance(user, ProducerPrincipal):
        return
    out_of_scope = sorted(
        {
            str(d["corpus_id"])
            for d in body.documents
            if d.get("corpus_id") and str(d["corpus_id"]) not in user.collection_ids
        }
    )
    if out_of_scope:
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "producer_corpus_out_of_scope",
                "corpus_ids": out_of_scope,
                "message": (
                    "this producer credential is scoped to a different set of collections; these "
                    "documents' corpus_id are outside its scope"
                ),
            },
        )

    # …and the batch must DECLARE a corpus whenever anything in it needs a
    # doc_id resolved. Without this the check above is bypassable outright:
    # `FactsPgRepository.ingest_batch`'s doc_id ladder falls back to an
    # UNRESTRICTED, instance-wide scan (`_resolve_doc` tier 3b) precisely
    # when `documents[]` declared no `(doc_id, corpus_id)` pair at all —
    # the documented "documents may be omitted when every doc_id already
    # resolves" replay flow (spec §7.2). A producer scoped to collections
    # A/B could therefore POST `documents: []` plus `nodes`/`edges` whose
    # evidence names a doc_id living in collection C, and its claims would
    # anchor onto C's file — or pass that doc_id in `full_documents` and
    # DELETE C's existing claims for it (replace mode). Neither row carries
    # a `corpus_id` for the loop above to inspect, so both slip through.
    #
    # Refused only for a ProducerPrincipal: tier 3b stays exactly as it was
    # for an admin or the scheduler token, which is what the replay flow's
    # existing callers use and what its docstring says depends on it. A
    # producer that declares at least one `documents[]` pair keeps every
    # tier, because `declared_corpus_ids` is then a subset of the token's
    # own scope (the loop above has already refused any other corpus_id) and
    # resolution cannot leave it.
    declares_corpus = any(d.get("doc_id") and d.get("corpus_id") for d in body.documents)
    if declares_corpus:
        return
    if _batch_references_a_doc_id(body):
        raise HTTPException(
            status_code=403,
            detail={
                "reason": "producer_batch_declares_no_corpus",
                "message": (
                    "a producer credential must declare each document's corpus_id in "
                    "documents[]; a batch that references doc_ids without declaring any "
                    "corpus would resolve them across every collection, escaping this "
                    "credential's scope"
                ),
            },
        )


class FactsIngestRequest(BaseModel):
    """Wire format accepted verbatim (spec §7.0/§7.2) — ``documents`` are the
    crawler's ``make_row`` rows each EXTENDED with ``corpus_id``; ``nodes``/
    ``edges`` carry ``{id/src+type+dst, attrs, evidence: [{doc_id, quote,
    audience?}]}``. Deliberately plain ``Dict[str, Any]`` items rather than a
    strict nested schema — the producer contract explicitly tolerates
    unknown fields (underscore-prefixed crawler internals are stripped
    server-side, not rejected), so a rigid Pydantic model would reject valid
    producer input on every crawler-side field addition.

    ``anonymization`` is one exception: a small, OPTIONAL, strictly typed
    block (spec §9.2) — malformed input there is a real protocol error
    (422), not tolerated crawler noise. ``evidence[].audience`` (Task 10,
    spec §4.2) is the other: an OPTIONAL index-time variant tag, format-
    checked against ``_AUDIENCE_PATTERN`` by :func:`_validate_evidence_audience`
    before this batch ever reaches :meth:`FactsPgRepository.ingest_batch`."""

    documents: List[Dict[str, Any]] = Field(default_factory=list)
    full_documents: List[str] = Field(default_factory=list)
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    anonymization: Optional[FactsIngestAnonymizationReport] = None


_AUDIENCE_PATTERN = re.compile(r"^[a-z0-9_-]{1,64}$")


def _validate_evidence_audience(body: "FactsIngestRequest") -> None:
    """Refuse the WHOLE batch (422, nothing written) if any ``nodes``/
    ``edges`` evidence item's ``audience`` fails ``_AUDIENCE_PATTERN``
    (Task 10, 2026-08-30 sharepoint-acl-mirroring plan; spec §4.2) — a
    protocol error, same posture as a malformed ``anonymization`` block,
    checked BEFORE any DB lookup so a producer typo never partially writes.
    An absent/``None`` ``audience`` is untagged (today's behavior) and never
    itemized here."""
    invalid: List[Dict[str, Any]] = []
    for kind, rows in (("nodes", body.nodes), ("edges", body.edges)):
        for row_idx, row in enumerate(rows):
            for ev_idx, ev in enumerate(row.get("evidence") or []):
                audience = ev.get("audience")
                if audience is not None and not _AUDIENCE_PATTERN.match(str(audience)):
                    invalid.append({"row": f"{kind}[{row_idx}].evidence[{ev_idx}]", "audience": audience})
    if invalid:
        raise HTTPException(status_code=422, detail={"reason": "invalid_audience", "items": invalid})


@router.post("/ingest")
def facts_ingest(body: FactsIngestRequest, user=Depends(require_admin_or_producer)) -> Dict[str, Any]:
    """Ingest one producer batch (spec §7.2) — scheduler token, admin PAT,
    or a corpus-extraction producer credential (``ProducerPrincipal``, see
    ``app.auth.producer_token``) scoped to a set of collections.

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

    Anonymize-fail-closed hardening: BEFORE any of the above runs, a batch
    that documents a corpus whose SharePoint connect-wizard scope is
    anonymize-marked (``config.scopes[].anonymize``) is REFUSED — whole
    batch, nothing written — unless THIS batch's own ``anonymization``
    block declares that corpus (``403`` ``anonymization_not_declared``,
    itemizing the offending corpus ids). This is the enforcement point for
    the guarantee the connect wizard's checkbox only ever *requested*:
    Agnes still cannot verify a document's CONTENT was anonymized (see
    ``docs/anonymization.md``), but it now refuses to accept a claim for an
    anonymize-marked corpus with no declaration at all, regardless of
    whether the connection's ``config.scopes`` survived an unrelated edit,
    the per-instance HMAC key is configured, or the producer remembered the
    block. A lookup failure while answering "is this corpus marked" is
    itself refused (``503`` ``anonymization_check_unavailable``) rather
    than treated as "nothing is marked" — see
    :func:`_refuse_undeclared_anonymize_marked_corpora`.

    Producer scope gate: a ``ProducerPrincipal`` caller additionally has
    every ``documents[]`` row's ``corpus_id`` checked against its own
    ``collection_ids`` claim — any row naming a corpus outside that set is
    REJECTED whole-batch (``403`` ``producer_corpus_out_of_scope``,
    itemizing the offending corpus ids), an authorization boundary
    independent of the ``ambiguous_cross_collection_doc_id`` data-integrity
    rule above — AND, because the repository's doc_id ladder falls back to
    an unrestricted instance-wide scan exactly when ``documents[]`` declared
    no corpus at all, a producer batch that references any doc_id (node/edge
    evidence, or ``full_documents``' replace-mode delete list) without
    declaring one is refused whole (``403``
    ``producer_batch_declares_no_corpus``). See
    :func:`_refuse_producer_out_of_scope_documents`. A human admin (or the
    scheduler token) has no such claim, keeps the documented
    documents-omitted replay flow, and is unaffected by either half.
    ``evidence[].audience`` (Task 10, spec §4.2) is format-validated FIRST,
    before either gate below — see :func:`_validate_evidence_audience`.
    """
    _validate_evidence_audience(body)
    _refuse_undeclared_anonymize_marked_corpora(body)
    _refuse_producer_out_of_scope_documents(body, user)
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

    from app.auth.session_principal import ProducerPrincipal

    is_producer = isinstance(user, ProducerPrincipal)
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="facts.ingest",
        client_kind="producer" if is_producer else None,
        params={
            "documents": len(body.documents),
            "claims_written": report.get("claims_written", 0),
            "claims_rejected": len(report.get("claims_rejected", [])),
        },
    )

    try:
        corpus_ids = sorted({d.get("corpus_id") for d in body.documents if d.get("corpus_id")})
        caller = f"producer:{user.connection_id}" if is_producer else (user.get("email") or user.get("id", "admin"))
        facts_ingest_runs_repo().create(
            corpus_ids=corpus_ids,
            caller=caller,
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
def list_corrections(user=Depends(require_admin_or_producer)) -> Dict[str, Any]:
    """The producer export (spec §7.4): every ``wrong`` subject with its
    ``natural_keys`` snapshot, so a producer's re-extraction pass can prune
    them before re-asserting claims. Server-side, corrections are ALSO
    enforced at read time regardless (§4) — a producer that ignores this
    export cannot resurrect a withheld fact, this just saves it the wasted
    work. Scheduler token, admin PAT, or a corpus-extraction
    ``ProducerPrincipal`` (``app.auth.producer_token``).

    NOT filtered to the caller's own ``collection_ids`` — a ``corrections``
    row is keyed on ``(subject_kind, subject_id)`` with only a
    ``natural_keys`` snapshot (spec §3: it must survive the subject itself
    being deleted and re-created), so there is no cheap, always-correct way
    to join it back to a collection: the fact/edge it corrected may no
    longer exist at all, and even when it does, "which collection" is a
    property of its CLAIMS' evidence, not of the correction row. A producer
    scoped to two of an instance's five collections therefore currently
    sees every withheld subject, instance-wide, exactly like an admin does
    — TODO(TCRD-...): revisit if/when corrections gain a cheap collection
    join, rather than shipping a filter that would silently return the
    wrong subset today.
    """
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
