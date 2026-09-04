"""Admin maintenance for the fact-graph collection-stats summary (TCRD-296
synthesis E.21) — see ``src/repositories/facts_pg.py``'s "Collection stats
summary" section for the maintenance contract.

- ``POST /api/admin/facts/stats/rebuild`` — recompute
  `fact_collection_stats`/`fact_collection_membership`/
  `edge_collection_membership` from `claims`. The one-time backfill an
  operator runs after the migration that creates these tables (which does
  NOT backfill them itself), and the general-purpose repair for "the
  summary looks stale" on any live instance.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.auth.access import require_admin
from src.audit_helpers import identity_for_audit, log_safe
from src.repositories import facts_repo

router = APIRouter(prefix="/api/admin/facts", tags=["admin"])


class StatsRebuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Omit (or send ``null``) to rebuild every collection that currently
    #: carries at least one claim — the full backfill. A non-empty list
    #: scopes the rebuild to exactly those collections (the targeted
    #: repair for "this one collection's numbers look wrong").
    corpus_ids: Optional[List[str]] = Field(default=None)


@router.post("/stats/rebuild")
def facts_stats_rebuild(
    body: Optional[StatsRebuildRequest] = None,
    user: dict = Depends(require_admin),
) -> dict:
    """Recompute the collection-stats summary from `claims` — bounded PER
    COLLECTION (see `FactsPgRepository.rebuild_collection_stats`'s
    docstring), so this is safe to run on a live, multi-million-claim
    instance. Response: ``{"collections_rebuilt": n}``.
    """
    corpus_ids = body.corpus_ids if body else None
    result = facts_repo().rebuild_collection_stats(corpus_ids=corpus_ids)
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="facts.stats.rebuild",
        params={
            "collections_rebuilt": result["collections_rebuilt"],
            "scoped": corpus_ids is not None,
        },
    )
    return result
