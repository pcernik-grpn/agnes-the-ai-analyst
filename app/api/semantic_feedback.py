"""Semantic-layer feedback — REST surface.

The channel coverage (F4.1) and health (F4.2) structurally cannot cover: they
report what is undocumented and what is broken, but not the case where
everything looks fine and the ANSWER was still wrong — an unsupported number, a
metric that means something other than its name, a concept nobody defined.
Only whoever read the answer knows that.

Hence the RBAC split, and hence this file exists separately from
``app/api/semantic_layer_coverage.py`` (whose every route is admin-only):

* ``POST /api/semantic-feedback`` — **any signed-in caller**, human or agent.
  Restricting the report channel to admins would mean the only people who can
  flag a wrong number are the ones who never see it inside an analysis.
* ``GET /api/admin/semantic-feedback[?status=open]`` — admin queue.
* ``POST /api/admin/semantic-feedback/{feedback_id}/resolve`` — admin closes
  one, on the record.

**Postgres-only** (A3 PG-first ratchet). Every route resolves
``semantic_feedback_repo()`` through a dependency, so on a DuckDB-backed
instance ``RequiresPostgresBackend`` is raised *before* the request body is
even validated and ``app/main.py`` translates it to a typed
``501 requires_postgres_backend``. No hand-rolled try/except, and no route that
answers 422 on an instance whose real answer is "this needs Postgres".
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from src.models.semantic_feedback import FEEDBACK_STATUSES

logger = logging.getLogger(__name__)
router = APIRouter(tags=["semantic-feedback"])


def _feedback_repo() -> Any:
    """Resolve the Postgres-only feedback repository AS A DEPENDENCY.

    Not inside the handler body: FastAPI solves dependencies before it
    validates the request body, so raising here is what makes a DuckDB-backed
    instance answer the typed ``501`` rather than a ``422`` about a field it
    could never have used anyway.
    """
    from src.repositories import semantic_feedback_repo

    return semantic_feedback_repo()


class FeedbackCreate(BaseModel):
    """One report. Only the question is required.

    The caps are the point of declaring these at all: the columns are ``TEXT``,
    the submitter is any signed-in caller, and an uncapped field is an
    invitation to store a megabyte of paste per row. They are generous enough
    that no genuine report hits them.
    """

    question: str = Field(max_length=4000)
    sql: Optional[str] = Field(default=None, max_length=20000)
    metric_id: Optional[str] = Field(default=None, max_length=200)
    model_content_hash: Optional[str] = Field(default=None, max_length=128)
    comment: Optional[str] = Field(default=None, max_length=4000)


class FeedbackResolve(BaseModel):
    resolution_note: Optional[str] = Field(default=None, max_length=4000)


def _clean(value: Optional[str]) -> Optional[str]:
    """Trim; treat whitespace-only as absent — a present-but-blank ``sql`` in
    the queue reads as "there was a query" when there was not."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@router.post("/api/semantic-feedback", status_code=201)
async def submit_semantic_feedback(
    body: FeedbackCreate,
    user: dict = Depends(get_current_user),
    repo: Any = Depends(_feedback_repo),
):
    """File a report that an answer looked wrong (any signed-in caller).

    Deliberately NOT ``require_admin``: the analyst who ran the analysis and
    the agent that could not ground its answer are the two callers most likely
    to notice, and neither is an admin. The report lands ``status='open'`` in
    the admin queue.
    """
    question = _clean(body.question)
    if not question:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "missing_question",
                "message": "question is required — it is the evidence for what the semantic layer failed to answer",
            },
        )

    row = repo.create(
        question=question,
        sql=_clean(body.sql),
        metric_id=_clean(body.metric_id),
        model_content_hash=_clean(body.model_content_hash),
        comment=_clean(body.comment),
        created_by=user.get("email") or user.get("id"),
    )

    from src.repositories import audit_repo

    audit_repo().log(
        user_id=user.get("id"),
        action="semantic_feedback.submit",
        resource=row["id"],
        params={"metric_id": row.get("metric_id")},
    )
    return row


@router.get("/api/admin/semantic-feedback")
async def list_semantic_feedback(
    status: Optional[str] = None,
    _admin: dict = Depends(require_admin),
    repo: Any = Depends(_feedback_repo),
):
    """The report queue, newest first (admin only).

    ``?status=open`` is the working view. An unrecognized status is a 400
    rather than an empty list: "nothing to do" is the opposite of the truth
    when the filter itself was a typo.
    """
    if status is not None and status not in FEEDBACK_STATUSES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_status",
                "message": f"{status!r} is not a feedback status; expected one of {', '.join(FEEDBACK_STATUSES)}",
            },
        )
    items = repo.list(status=status)
    return {"items": items, "count": len(items)}


@router.post("/api/admin/semantic-feedback/{feedback_id}/resolve")
async def resolve_semantic_feedback(
    feedback_id: str,
    body: FeedbackResolve,
    admin: dict = Depends(require_admin),
    repo: Any = Depends(_feedback_repo),
):
    """Close one report, recording who closed it and what was done.

    404 when it does not exist; 409 when somebody already resolved it — the
    repository's guarded transition refuses to overwrite the first admin's
    note, so the queue never loses who actually fixed the thing.
    """
    if repo.get(feedback_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_feedback", "message": f"no semantic feedback {feedback_id!r}"},
        )

    resolved_by = admin.get("email") or admin.get("id")
    if not repo.resolve(feedback_id, resolved_by=resolved_by, resolution_note=_clean(body.resolution_note)):
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_resolved",
                "message": f"semantic feedback {feedback_id!r} was already resolved",
            },
        )

    from src.repositories import audit_repo

    audit_repo().log(
        user_id=admin.get("id"),
        action="semantic_feedback.resolved",
        resource=feedback_id,
        params={"resolution_note": _clean(body.resolution_note)},
    )
    row = repo.get(feedback_id)
    assert row is not None  # just resolved it
    return row
