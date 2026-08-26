"""Cross-domain semantic coverage, and muting its checks — REST surface.

``GET /api/admin/semantic-model/coverage`` answers "what does each connected
data source still lack", across every domain (semantic model, metrics,
glossary, skill, agent, knowledge base) and across every source type. The
POST/DELETE ``…/coverage/tags`` pair maintains the one input the report has
no other way to learn: which skill / agent / knowledge domain is ABOUT which
source.

The ``/api/admin/semantic-layer/mutes`` trio (F4.3) is the other side of the
same page: an admin who has read a finding, decided it is expected, and wants
it to stop shouting can silence it — but never anonymously. A mute stores who
muted it, when, and (asked for everywhere, required nowhere) why, and every
read hands those back, so a check that stopped appearing can always be told
apart from a check that was fixed. F4.2's health roll-up consumes these; this
file only creates, lists and deletes them.

Deliberately a different path from ``GET /api/admin/semantic-layer/coverage``
(``app/api/keboola_semantic_layer_refresh.py``), which is the Keboola-only
binding-coverage report. That endpoint is unchanged and un-deprecated — it is
one of the providers this one aggregates, not a duplicate to retire.

**Postgres-only** (A3 PG-first ratchet). Every route here resolves its
repository (``resource_source_tags_repo()`` / ``semantic_health_mutes_repo()``)
through a dependency, so on a DuckDB-backed instance
``RequiresPostgresBackend`` is raised *before* the request body is even
validated and ``app/main.py`` translates it to a typed
``501 requires_postgres_backend``. No hand-rolled try/except, and no route
that answers 422 on an instance whose real answer is "this needs Postgres".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from src.models.semantic_health_mutes import MUTE_SCOPE_FORMS, parse_mute_scope
from src.semantic.coverage import LOCAL_BUCKET_ID, TAG_DOMAIN_BY_RESOURCE_TYPE

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


def _mutes_repo() -> Any:
    """Resolve the Postgres-only mutes repository AS A DEPENDENCY — same
    reasoning as :func:`_tags_repo`."""
    from src.repositories import semantic_health_mutes_repo

    return semantic_health_mutes_repo()


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


# ---------------------------------------------------------------------------
# Muting a check (F4.3) — turning one off is allowed; doing it anonymously is
# not. Every route below is `require_admin`: muting is what makes a finding
# stop shouting, so the authority to do it is the authority that reads the
# report. (Widening this to a source's owner waits on F2's `SEMANTIC_SOURCE`
# resource type, which does not exist yet.)
# ---------------------------------------------------------------------------


class MuteCreate(BaseModel):
    """One mute. ``muted_by`` is deliberately NOT a field — it is taken from the
    authenticated caller, because a signature you can address to somebody else
    is not a signature.

    The caps matter for the same reason they do on feedback: the columns are
    ``TEXT``/``VARCHAR`` and generous enough that no genuine mute hits them.
    """

    scope: str = Field(max_length=200)
    reason: Optional[str] = Field(default=None, max_length=2000)
    # Parsed by pydantic from ISO-8601. `None` = permanent until unmuted.
    expires_at: Optional[datetime] = None


def _parsed_scope(raw: str) -> tuple[str, Optional[str]]:
    """Normalize + validate a scope, returning ``(scope, source_id)``, or raise
    the typed 400.

    A malformed scope is refused rather than stored: the row would otherwise
    sit in the muted list looking like a silenced check while the check it
    meant to silence carries on firing — the one outcome worse than either
    "muted" or "not muted".

    Only the source half comes back: it is the one part this endpoint can check
    against something (the connections table). The domain half is validated for
    shape and then travels inside the stored scope string, which is what F4.2's
    roll-up matches on.
    """
    scope = (raw or "").strip()
    try:
        source_id, _domain = parse_mute_scope(scope)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_scope",
                "message": str(exc),
                "expected": list(MUTE_SCOPE_FORMS),
            },
        ) from None
    return scope, source_id


def _future_expiry(value: Optional[datetime]) -> Optional[datetime]:
    """Reject an expiry that has already passed; treat a naive one as UTC.

    A mute expired at the moment it is written silences nothing but is
    indistinguishable, in the list, from one that works. A naive timestamp is
    read as UTC rather than as the server's local zone so the same request body
    means the same instant regardless of where the process runs.
    """
    if value is None:
        return None
    expires_at = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "expires_in_past",
                "message": f"{expires_at.isoformat()} has already passed — a mute expiring in the past silences nothing",
            },
        )
    return expires_at


@router.post("/api/admin/semantic-layer/mutes", status_code=201)
async def create_mute(
    body: MuteCreate,
    user: dict = Depends(require_admin),
    mutes_repo: Any = Depends(_mutes_repo),
):
    """Silence one check, on the record (admin only).

    Returns the stored row — ``muted_by``, ``muted_at``, ``reason`` and all —
    so the caller sees the signature it just left rather than a bare ``201``.

    409 when an ACTIVE mute already covers the scope: two rows for one check
    read as two independent judgements when it is one, and unmuting either
    would leave the check silent with no visible reason why. 404 when the scope
    names a source connection that does not exist — a mute pointing at nothing
    silences nothing, forever, while looking like it works.
    """
    from src.repositories import audit_repo, source_connections_repo

    scope, source_id = _parsed_scope(body.scope)

    if source_id is not None and source_id != LOCAL_BUCKET_ID:
        # `__local__` is exempt: it is a real row in the coverage report
        # (registered tables belonging to no connection) but not a
        # `source_connections` id, and refusing it would leave the row admins
        # most often want to mute unmutable.
        if source_connections_repo().get(source_id) is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "unknown_source", "message": f"no source connection {source_id!r}"},
            )

    expires_at = _future_expiry(body.expires_at)

    existing = mutes_repo.find_active_for_scope(scope)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_muted",
                "message": (
                    f"{scope!r} is already muted by {existing.get('muted_by') or 'someone'} — "
                    "unmute it first to replace the reason"
                ),
                "mute_id": existing["id"],
            },
        )

    reason = (body.reason or "").strip() or None
    row = mutes_repo.create(
        scope=scope,
        reason=reason,
        muted_by=user.get("email") or user.get("id"),
        expires_at=expires_at,
    )

    audit_repo().log(
        user_id=user.get("id"),
        action="semantic_health_mute.create",
        resource=row["id"],
        params={"scope": scope, "reason": reason, "expires_at": expires_at.isoformat() if expires_at else None},
    )
    return row


@router.get("/api/admin/semantic-layer/mutes")
async def list_mutes(
    include_expired: bool = False,
    user: dict = Depends(require_admin),
    mutes_repo: Any = Depends(_mutes_repo),
):
    """Every check currently silenced, and by whom (admin only).

    Its own route rather than a corner of the health report: this is the answer
    to "what are we not being told about", and an admin auditing that should
    not have to fetch (and re-compute) the whole health roll-up to read it.
    F4.2's health response includes the same rows — the list is deliberately
    reachable from both.

    ``?include_expired=true`` adds the lapsed ones. The silence ends at the
    expiry; the record of who chose it does not.
    """
    items = mutes_repo.list_all() if include_expired else mutes_repo.list_active()
    return {"items": items, "count": len(items)}


@router.delete("/api/admin/semantic-layer/mutes/{mute_id}", status_code=204)
async def delete_mute(
    mute_id: str,
    user: dict = Depends(require_admin),
    mutes_repo: Any = Depends(_mutes_repo),
):
    """Unmute — the check starts reporting again (admin only).

    404 when the mute does not exist, so an admin never reads a success for an
    unmute that unmuted nothing and then wonders why the warning is still
    quiet.
    """
    from src.repositories import audit_repo

    if not mutes_repo.delete(mute_id):
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_mute", "message": f"no semantic-layer mute {mute_id!r}"},
        )

    audit_repo().log(
        user_id=user.get("id"),
        action="semantic_health_mute.delete",
        resource=mute_id,
        params={},
    )
