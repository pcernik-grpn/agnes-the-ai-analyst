"""Cross-domain semantic coverage — REST surface.

``GET /api/admin/semantic-model/coverage`` answers "what does each connected
data source still lack", across every domain (semantic model, metrics,
glossary, skill, agent, knowledge base) and across every source type. The
POST/DELETE ``…/coverage/tags`` pair maintains the one input the report has
no other way to learn: which skill / agent / knowledge domain is ABOUT which
source.

Deliberately a different path from ``GET /api/admin/semantic-layer/coverage``
(``app/api/keboola_semantic_layer_refresh.py``), which is the Keboola-only
binding-coverage report. That endpoint is unchanged and un-deprecated — it is
one of the providers this one aggregates, not a duplicate to retire.

**Postgres-only** (A3 PG-first ratchet). Every route here resolves
``resource_source_tags_repo()`` through a dependency, so on a DuckDB-backed
instance ``RequiresPostgresBackend`` is raised *before* the request body is
even validated and ``app/main.py`` translates it to a typed
``501 requires_postgres_backend``. No hand-rolled try/except, and no route
that answers 422 on an instance whose real answer is "this needs Postgres".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from src.semantic.coverage import TAG_DOMAIN_BY_RESOURCE_TYPE

logger = logging.getLogger(__name__)
router = APIRouter()

#: The resource types a source can be tagged with — the three domains the
#: coverage report scores off ``resource_source_tags``. Anything else would
#: write a row no column ever reads.
TAGGABLE_RESOURCE_TYPES = tuple(TAG_DOMAIN_BY_RESOURCE_TYPE)


def _tags_repo() -> Any:
    """Resolve the Postgres-only tags repository AS A DEPENDENCY.

    Not inside the handler body: FastAPI solves dependencies before it
    validates the request body, so raising here is what makes a DuckDB-backed
    instance answer the typed ``501`` rather than a ``422`` about a field it
    could never have used anyway.
    """
    from src.repositories import resource_source_tags_repo

    return resource_source_tags_repo()


class CoverageTagCreate(BaseModel):
    resource_type: str
    resource_id: str
    source_id: str


@router.get("/api/admin/semantic-model/coverage")
async def get_cross_domain_coverage(
    source: Optional[str] = None,
    user: dict = Depends(require_admin),
    tags_repo: Any = Depends(_tags_repo),
):
    """Coverage of every connected data source, per domain (admin only).

    Each source carries one entry per domain with ``status``
    (``ok`` | ``partial`` | ``missing`` | ``not_applicable``), a
    one-sentence ``detail``, an optional ``action`` naming the create flow
    that fills it, and a ``raw`` payload as deep as that source type's
    connector can compute (rich for Keboola, thin elsewhere — see
    ``src/semantic/coverage.py``).

    ``?source=<id>`` narrows to one source; ``__local__`` is the synthetic
    bucket for registered tables belonging to no connection.

    Recomputed live, never cached: the Keboola provider makes upstream calls,
    so the work runs off the event loop — one slow project must not stall
    every other request in the process.
    """
    from src.semantic.coverage import compute_cross_domain_coverage

    return await asyncio.to_thread(compute_cross_domain_coverage, source)


@router.post("/api/admin/semantic-model/coverage/tags", status_code=201)
async def create_coverage_tag(
    body: CoverageTagCreate,
    user: dict = Depends(require_admin),
    tags_repo: Any = Depends(_tags_repo),
):
    """Record that a skill / agent / knowledge domain is about a data source.

    409 when the same triple is already tagged — "already recorded" is
    information the admin asked for, not something to swallow into a second
    identical row that would make the roll-up count one skill twice.
    """
    from sqlalchemy.exc import IntegrityError

    from src.repositories import source_connections_repo

    resource_type = (body.resource_type or "").strip()
    resource_id = (body.resource_id or "").strip()
    source_id = (body.source_id or "").strip()

    if resource_type not in TAGGABLE_RESOURCE_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_resource_type",
                "message": (f"{resource_type!r} is not taggable; expected one of {', '.join(TAGGABLE_RESOURCE_TYPES)}"),
            },
        )
    if not resource_id:
        raise HTTPException(
            status_code=400, detail={"error": "missing_resource_id", "message": "resource_id is required"}
        )
    if source_connections_repo().get(source_id) is None:
        # A tag pointing at a source that does not exist can never appear in
        # the report, so accepting it would be a silent no-op forever.
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_source", "message": f"no source connection {source_id!r}"},
        )

    try:
        return tags_repo.create(
            resource_type=resource_type,
            resource_id=resource_id,
            source_id=source_id,
            tagged_by=user.get("email") or user.get("id"),
        )
    except IntegrityError:
        existing = [
            t for t in tags_repo.list_for_resource(resource_type, resource_id) if t.get("source_id") == source_id
        ]
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_tagged",
                "message": f"{resource_type} {resource_id!r} is already tagged to {source_id!r}",
                "tag_id": existing[0]["id"] if existing else None,
            },
        ) from None


@router.delete("/api/admin/semantic-model/coverage/tags/{tag_id}", status_code=204)
async def delete_coverage_tag(
    tag_id: str,
    user: dict = Depends(require_admin),
    tags_repo: Any = Depends(_tags_repo),
):
    """Remove one source tag. 404 when it does not exist, so an admin never
    reads a success for a deletion that removed nothing."""
    if not tags_repo.delete(tag_id):
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_tag", "message": f"no coverage tag {tag_id!r}"},
        )
