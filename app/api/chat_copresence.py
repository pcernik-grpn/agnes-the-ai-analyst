"""Fork / invite / join-ticket / leave / fork-to-private co-presence endpoints.

All endpoints:
  - are RBAC-gated (deny_principal for co-session tokens; ownership/membership
    checks for invite/join/leave/fork).
  - are audited via write_audit.
  - implement SR-8 (summary seed, no blind clone) and SR-9 (join gate).

App-state accessors mirror app/api/chat.py:
  _get_repo(request)    → ChatRepository from request.app.state.chat_repo
  _get_manager(request) → ChatManager from request.app.state.chat_manager
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.access import can_access
from app.auth.dependencies import get_current_user, _get_db
from app.chat.session_principal_guard import deny_principal
from app.resource_types import ResourceType
from src.repositories import users_repo

router = APIRouter(prefix="/api/chat", tags=["chat-copresence"])


class InviteBody(BaseModel):
    invitee_email: str


def _get_repo(request: Request):
    """Return the ChatRepository from app state (mirrors chat.py)."""
    repo = getattr(request.app.state, "chat_repo", None)
    if repo is None:
        raise HTTPException(503, "chat repository not available")
    return repo


def _get_manager(request: Request):
    """Return the ChatManager from app state (mirrors chat.py)."""
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail={"kind": "chat_disabled", "hint": "Operator must enable chat.enabled in instance.yaml"},
        )
    return mgr


def _engine_session_id(request: Request):
    """A caller-owned uuid when the instance runs the embedded engine, else None.

    A fork is a session CREATOR. `chat.provider: kai-agent` needs every session
    id to be a uuid, and a fork that mints the repo's `chat_<hex>` on such an
    instance produces a row that can never spawn — co-drive would be dead on
    arrival, telling the user the conversation "predates" a provider it is
    seconds younger than. Reads the manager's own config so the decision is
    made in exactly one place (`app.chat.manager.engine_session_id`).

    Degrades to None when chat is not configured: this is called on paths that
    already resolved a manager, so that only happens in tests.
    """
    from app.chat.manager import engine_session_id

    mgr = getattr(request.app.state, "chat_manager", None)
    cfg = getattr(mgr, "_config", None)
    return engine_session_id(cfg) if cfg is not None else None


@router.get("/{session_id}/messages")
async def co_session_messages(
    session_id: str,
    request: Request,
    user=Depends(get_current_user),
    conn=Depends(_get_db),
):
    """List messages for a co-session.  Accessible to any live participant
    who STILL holds CHAT access (membership alone is not enough — a revoked
    CHAT grant must take effect immediately, even mid-session)."""
    deny_principal(user)
    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(403, "no chat access")
    repo = _get_repo(request)
    s = repo.get_session(session_id)
    if s is None:
        raise HTTPException(404, "session not found")
    # Allow if caller owns or is a participant
    is_participant = False
    if s.is_co_session:
        parts = repo.get_session_participants(session_id)
        is_participant = any(p.user_email == user["email"] and p.left_at is None for p in parts)
    if s.user_email != user["email"] and not is_participant:
        raise HTTPException(403, "not a participant of this session")
    msgs = repo.list_messages(session_id)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "tool_calls": m.tool_calls,
            "sender_email": m.sender_email,
            "created_at": m.created_at.isoformat(),
        }
        for m in msgs
    ]


@router.post("/{session_id}/invite")
async def invite(
    session_id: str,
    body: InviteBody,
    request: Request,
    user=Depends(get_current_user),
    conn=Depends(_get_db),
):
    """Fork S0 into a co-session and invite body.invitee_email.

    Requires: caller owns S0; invitee independently has CHAT access.
    Seed: intersection summary only (SR-8 — no raw clone).
    Audited.
    """
    deny_principal(user)
    repo = _get_repo(request)
    s0 = repo.get_session(session_id)
    if s0 is None or s0.user_email != user["email"]:
        raise HTTPException(403, "only the owner can invite")
    inv_row = users_repo().get_by_email_ci((body.invitee_email or "").strip())
    if inv_row is None:
        raise HTTPException(403, "invitee not found or lacks chat access")
    inv_user_id = inv_row["id"]
    # Everything persisted downstream must be the RESOLVED account's address,
    # never the spelling the inviter typed. Participation is checked with plain
    # string equality (co_session_messages / join_ticket / leave here, the WS
    # join re-verification in app/api/chat.py and app/chat/manager.py) and
    # compute_grant_intersection resolves participants exactly and fails
    # closed — so a case-variant invite would look like it worked and then 403
    # the invitee on every single operation.
    invitee_email = inv_row["email"]
    if not can_access(inv_user_id, ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(403, "invitee lacks chat access")

    # SR-8: seed with a summary, never a raw clone.
    from app.chat.copresence_summary import build_intersection_summary

    seed = build_intersection_summary(session_id, [user["email"], invitee_email])

    s1 = repo.fork_session_as_co_session(
        source_id=session_id,
        owner_email=user["email"],
        owner_user_id=user["id"],
        invitee_email=invitee_email,
        invitee_user_id=inv_user_id,
        seed_summary=seed,
        session_id=_engine_session_id(request),
    )

    from app.chat.audit import write_audit

    write_audit(
        user_email=user["email"],
        action="co_session_fork",
        details={"source": session_id, "co_session": s1.id, "invitee": invitee_email},
    )
    return {"session_id": s1.id, "is_co_session": True}


@router.post("/{session_id}/join-ticket")
async def join_ticket(
    session_id: str,
    request: Request,
    user=Depends(get_current_user),
    conn=Depends(_get_db),
):
    """Issue a short-lived WS ticket for a live participant of a co-session.

    SR-9: only a user with an active (left_at IS NULL) participant row
    can obtain a ticket.  Strangers receive 403.

    The ticket is an opaque secret (same coordination-backed ticket
    mechanism as the primary stream path) carrying
    (session_id, participant_email).  The WS join
    route /api/chat/sessions/{id}/join consumes it, re-verifies
    participant membership (SR-9), and calls add_sink.
    """
    deny_principal(user)
    repo = _get_repo(request)
    parts = repo.get_session_participants(session_id)
    if not any(p.user_email == user["email"] and p.left_at is None for p in parts):
        raise HTTPException(403, "not a live participant")
    from app.api.chat import _issue_ticket

    ticket = _issue_ticket(session_id, user["email"])
    return {
        "ticket": ticket,
        "ws": f"/api/chat/sessions/{session_id}/join?ticket={ticket}",
    }


@router.post("/{session_id}/leave")
async def leave(
    session_id: str,
    request: Request,
    user=Depends(get_current_user),
    conn=Depends(_get_db),
):
    """Leave a co-session.  Owner leave kills the session; collaborator leave
    triggers the narrowed-intersection respawn (SR-7).
    """
    deny_principal(user)
    repo = _get_repo(request)
    s = repo.get_session(session_id)
    if s is None:
        raise HTTPException(404, "session not found")
    # Resolve the manager only inside the authorized branches — an
    # unauthorized caller is rejected before we touch chat state.
    if s.user_email == user["email"]:
        # Owner leaving ends the co-session entirely.
        await _get_manager(request).kill(session_id, reason="owner_leave")
    elif s.is_co_session:
        # A collaborator may leave only if they are a LIVE participant.
        # Without this gate any authenticated caller who knows a co-session
        # id could trigger leave_session → _respawn_co_runner repeatedly and
        # DoS the real participants (the runner restart fires even when
        # remove_participant matches no row).
        parts = repo.get_session_participants(session_id)
        if not any(p.user_email == user["email"] and p.left_at is None for p in parts):
            raise HTTPException(403, "not a participant of this session")
        await _get_manager(request).leave_session(session_id, user["email"])
    else:
        raise HTTPException(403, "not a participant of this session")
    return {"ok": True}


@router.post("/{session_id}/fork")
async def fork(
    session_id: str,
    request: Request,
    user=Depends(get_current_user),
    conn=Depends(_get_db),
):
    """Fork a co-session into a private session for the calling participant.

    Copies the full co-session transcript (governed by the caller's own grants).
    Only available to live (left_at IS NULL) participants.
    """
    deny_principal(user)
    repo = _get_repo(request)
    parts = repo.get_session_participants(session_id)
    if not any(p.user_email == user["email"] and p.left_at is None for p in parts):
        raise HTTPException(403, "not a participant")
    new_id = repo.fork_co_session_to_private(
        source_session_id=session_id,
        owner_email=user["email"],
        session_id=_engine_session_id(request),
    )
    return {"session_id": new_id}
