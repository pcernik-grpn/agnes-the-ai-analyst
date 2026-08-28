"""Admin API for the agent-sharing approval queue (Track C6).

  GET   /api/admin/share-requests        require_admin
  PATCH /api/admin/share-requests/{id}   require_admin  -- {"decision": "approve"|"reject"}

The decision rides in the body rather than a verb path segment, mirroring
the existing moderation precedent this router follows on purpose:
``PATCH /api/v1/agents/{agent_id}/memories/{memory_id}`` with an ``action``
field (``app/api/agents_admin.py``'s ``MemoryActionRequest`` /
``_MEMORY_ACTIONS``) — see ``tests/test_api_design_rules.py::
test_no_new_verbs_in_path``, which forbids a new verb-segment in a path.

A non-admin owner's ``PUT /api/sharing/agent/{id}`` queues a
``share_requests`` row instead of writing the ``resource_grants`` row
immediately (``app/services/library_sharing.py::set_shares``). Approve here
is the ONLY place that write actually lands — via the same
``resource_grants_repo().ensure_grant`` the admin-curated ``/admin/access``
layer uses, so an approved share reaches the grantee through the exact same
mechanism C2.3's shared-agent runtime already honors. Reject leaves no
grant.

PG-only (A3 ratchet): every route here resolves ``share_requests_repo()``,
which raises ``RequiresPostgresBackend`` (translated to a ``501`` by the
app-wide handler in ``app/main.py``) on a DuckDB-backed instance — the
approval queue is not available there, same as the rest of the fact-graph
build order's admin surfaces.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import require_admin
from src.repositories import (
    agents_repo,
    audit_repo,
    resource_grants_repo,
    share_requests_repo,
    user_groups_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/share-requests", tags=["share-requests"])

# Wire vocabulary mirrors MemoryActionRequest's "approve"/"archive" shape
# (present-tense verb IN THE BODY, never in the path). Maps to the stored
# `share_requests.status` values, which stay past-tense ("approved" /
# "rejected") to read correctly as a noun-state once decided.
_DECISIONS = frozenset({"approve", "reject"})
_DECISION_TO_STATUS = {"approve": "approved", "reject": "rejected"}


class ShareRequestDecisionRequest(BaseModel):
    decision: str


def _audit(actor_id: str, action: str, resource: str, params: Optional[dict] = None) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params=params)
    except Exception:
        # Audit failures must never break the mutation — see app/api/access.py's
        # identical posture.
        logger.warning("audit log failed for %s/%s", action, resource)


def _display_name(resource_type: str, resource_id: str) -> str:
    """Best-effort human label for the queue table. Only `agent` is ever
    queued today (the only approval-gated resource type), so this resolves
    an agent's name; a future gated type falls back to the raw id rather
    than a crash."""
    if resource_type == "agent":
        row = agents_repo().get_by_id(resource_id)
        if row:
            return row.get("name") or resource_id
    return resource_id


def _serialize(row: Dict[str, Any]) -> Dict[str, Any]:
    group = user_groups_repo().get(row["requested_group_id"])
    requester = users_repo().get_by_id(row["requested_by"])
    decider = users_repo().get_by_id(row["decided_by"]) if row.get("decided_by") else None
    return {
        "id": row["id"],
        "resource_type": row["resource_type"],
        "resource_id": row["resource_id"],
        "resource_name": _display_name(row["resource_type"], row["resource_id"]),
        "requested_group_id": row["requested_group_id"],
        "requested_group_name": (group or {}).get("name") or row["requested_group_id"],
        "requested_by": row["requested_by"],
        "requested_by_email": (requester or {}).get("email") or row["requested_by"],
        "status": row["status"],
        "decided_by": row.get("decided_by"),
        "decided_by_email": (decider or {}).get("email") if decider else None,
        "decided_at": str(row["decided_at"]) if row.get("decided_at") else None,
        "note": row.get("note"),
        "created_at": str(row["created_at"]) if row.get("created_at") else None,
    }


@router.get("")
async def list_share_requests(
    status: Optional[str] = None,
    limit: int = 100,
    skip: int = 0,
    user: dict = Depends(require_admin),
):
    """Comma-separated ``status`` (``pending``/``approved``/``rejected``),
    e.g. ``?status=pending``. Omitted returns every decision, newest first —
    the queue is also the audit trail of past approve/reject calls."""
    statuses: Optional[List[str]] = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        invalid = [s for s in statuses if s not in ("pending", "approved", "rejected")]
        if invalid:
            raise HTTPException(status_code=400, detail=f"invalid_status: {invalid}")
    limit = max(1, min(int(limit), 500))
    skip = max(0, int(skip))

    items, total = share_requests_repo().list_for_admin(status=statuses, limit=limit, skip=skip)
    return {
        "data": [_serialize(r) for r in items],
        "total": total,
        "limit": limit,
        "skip": skip,
    }


def _decide(request_id: str, *, decision: str, user: dict) -> Dict[str, Any]:
    row = share_requests_repo().decide(request_id, status=decision, decided_by=user["id"])
    if row is None:
        raise HTTPException(status_code=404, detail="share_request_not_found_or_already_decided")

    if decision == "approved":
        # The one write this whole surface exists to gate — the exact same
        # ensure_grant an admin-curated /admin/access write uses, so C2.3's
        # shared-agent runtime honors it identically either way.
        resource_grants_repo().ensure_grant(
            row["requested_group_id"],
            row["resource_type"],
            row["resource_id"],
            assigned_by=user["id"],
        )

    _audit(
        user["id"],
        f"share_request.{decision}",
        request_id,
        {
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "requested_group_id": row["requested_group_id"],
            "requested_by": row["requested_by"],
        },
    )
    return _serialize(row)


@router.patch("/{request_id}")
async def decide_share_request(
    request_id: str,
    payload: ShareRequestDecisionRequest,
    user: dict = Depends(require_admin),
):
    if payload.decision not in _DECISIONS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid_decision: must be one of {sorted(_DECISIONS)}, got {payload.decision!r}",
        )
    return _decide(request_id, decision=_DECISION_TO_STATUS[payload.decision], user=user)
