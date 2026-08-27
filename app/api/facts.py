"""Fact graph over Collections — read surface (build order steps 2+3 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

Three endpoints, all behind the ``facts`` feature flag
(``app.auth.access.require_facts_enabled`` — router-level, so the whole
``/api/facts*`` surface answers `404` when the flag is off):

- ``POST /api/facts/search``          — typed subject search with
  attribute filters, projected per-caller (spec §12).
- ``POST /api/facts/neighbors``       — bounded graph traversal from one
  subject (depth <= 2, capped fanout/result, statement-timeout guarded).
- ``GET  /api/facts/{subject_id}/claims`` — the readable evidence for one
  subject: quote, document, attrs, document_date.

Auth: any authenticated caller — ``Depends(get_current_user)``, the SAME
dependency the agent/PAT paths resolve through (it can yield a plain
``dict`` user OR a restricted ``Principal`` such as ``AgentPrincipal``; see
``require_session_or_user_pat``'s docstring in ``app/auth/dependencies.py``
for the isinstance check other endpoints use on its result). There is
deliberately **no** admin gate and **no** ``require_resource_access`` on
these routes — the tools are not collection-scoped in their signatures
(``subject_id`` names a fact/edge, not a collection), so the declarative
route gate does not apply; **every bit of enforcement lives in
``src/repositories/facts_pg.py``**'s shared visibility helper (spec §5).
Never add a general SQL escape hatch over these tables on any surface.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import require_facts_enabled
from app.auth.dependencies import get_current_user
from src.repositories import facts_repo
from src.repositories.facts_pg import FactNotFound

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
    try:
        return facts_repo().claims(user, subject_id)
    except FactNotFound:
        raise HTTPException(status_code=404, detail="fact_not_found")
