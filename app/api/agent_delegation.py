"""Track C7 MVP — @delegation between shared agents (server-side handoff).

`POST /api/v1/agents/{slug}/delegate` lets a live, user-driven agent turn
(agent A) hand ONE sub-request off to another agent the CALLER may run
(agent B), mid-turn, and get B's answer back into A's turn. `{slug}` names
B, resolved exactly as `app.api.agent_runtime.require_agent_runtime_principal`
resolves a runtime target (`agents_repo().get_runnable_by_slug`) — the
CALLER's own runnable set, never A's owner's.

This route has exactly one caller in practice: agent A's own in-process
delegation tool (`app/chat/runner.py`'s `_agnes_delegation_mcp_server`),
invoked by the model mid-turn and reaching this endpoint through the SAME
in-sandbox relay + broker envelope replay every other Agnes MCP tool call
already rides (`app/chat/relay.py`, `app/api/broker.py::_replay`) — never a
bare user credential. The replay mints a FRESH identity JWT for A's own
chat session (`app.api.broker._mint_identity_jwt`), so the auth dependency
below sees exactly what that minting produces: an `AgentPrincipal` (a
restricted/shared agent) or a plain user dict carrying a stashed
`request.state.chat_session_id` (the "passthrough" optimization for a user
running their own unrestricted default agent) — see
`require_delegating_session`'s docstring for how both resolve to the SAME
(session_id, caller_user_id, caller_email) triple `ChatManager.
handle_delegation` needs. All of the RBAC gate, the depth-1 guard, the
one-delegation-per-turn guard, and — THE SECURITY INVARIANT — spawning B's
child session under the ORIGINAL CALLER's identity (never A's owner's) live
in `ChatManager.handle_delegation`; this module is thin request plumbing
around it.

Standing exemption from the triple-surface (REST+CLI+MCP) ratchet
(`tests/test_documentation_api_triple_surface.py`): this route is a
sandbox-internal RPC, reachable only by a live turn's own delegation tool
under that turn's own session-scoped ticket — never a durable user
credential an analyst would hold at a terminal. A CLI/MCP "delegate now"
command is a plausible FUTURE feature, but exposing THIS exact route as a
generic analyst-facing tool would let a caller puppet another agent's turn
outside the depth/one-per-turn/caller-binding guarantees this route
enforces for a LIVE delegating turn (see the module docstring above).
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from app.auth.access import require_agent_profiles_enabled
from app.auth.dependencies import get_current_user
from app.auth.session_principal import AgentPrincipal, SessionPrincipal
from app.chat.manager import get_current_chat_manager

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["agent-runtime"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)


class DelegateRequest(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("message must be non-empty")
        return v


def require_delegating_session(request: Request, user=Depends(get_current_user)) -> Tuple[str, str, str]:
    """Resolve the (session_id, caller_user_id, caller_email) triple for
    the LIVE session this delegation call is running inside of.

    Two shapes reach here, both minted server-side by
    `app.api.broker._mint_identity_jwt` for a chat session's own ticket —
    never client-shaped:

    - `AgentPrincipal` (a restricted/shared agent's session): `session_id`/
      `caller_user_id`/`caller_email` are carried directly on the
      principal (C2.3 — the ORIGINAL caller, not the agent's owner).
    - A plain user dict (the "passthrough" optimization for a session
      whose agent is unrestricted AND whose caller IS the agent's owner —
      `_mint_identity_jwt` skips the AgentPrincipal machinery entirely
      when owner authority and agent authority are identical): the
      caller IS that user, and the session id rides the token's
      `chat_session_id` claim, stashed onto `request.state` by
      `get_current_user` (`app.auth.dependencies
      ._stash_chat_session_id_from_token`) for exactly this kind of
      chat-scoped consumer.

    A co-drive `SessionPrincipal` (no single driving identity) or a plain
    token with no stashed chat session is `403` — delegation requires a
    single, session-bound driver.

    FAIL CLOSED on an `AgentPrincipal` missing a resolved caller: every real
    construction site (`app/auth/pat_resolver.py`'s `typ="agent_session"`
    branch) always populates both `caller_user_id` and `caller_email` from
    the session's own stored `user_email`, itself failing closed if that
    lookup comes up empty — so this should never fire in production. But
    `AgentPrincipal.caller_user_id`/`caller_email` default to `None` on the
    dataclass (for callers/tests predating C2.3), and this function must
    NEVER paper over a missing caller by silently substituting A's OWNER —
    that would spawn the delegated child session under the owner's identity
    instead of the caller's, laundering the caller's restricted view into
    the owner's wider one through B. A future refactor that leaves either
    field unset must be refused outright, not quietly downgraded.
    """
    if isinstance(user, AgentPrincipal):
        if not user.caller_user_id or not user.caller_email:
            raise HTTPException(
                status_code=403,
                detail={"code": "delegation_requires_resolved_caller"},
            )
        return user.session_id, user.caller_user_id, user.caller_email
    if isinstance(user, SessionPrincipal):
        raise HTTPException(status_code=403, detail={"code": "delegation_requires_single_driver"})
    session_id: Optional[str] = getattr(request.state, "chat_session_id", None)
    if not session_id:
        raise HTTPException(status_code=403, detail={"code": "delegation_requires_agent_session"})
    return session_id, user["id"], user["email"]


@router.post("/agents/{slug}/delegate")
async def delegate_to_agent(
    slug: str,
    body: DelegateRequest,
    principal: Tuple[str, str, str] = Depends(require_delegating_session),
) -> dict:
    """Delegate the current turn's ``message`` to agent ``slug``.

    Returns ``ChatManager.handle_delegation``'s result dict verbatim —
    ``{"status": "ok"|"denied"|"degraded", "reason": str | None,
    "agent_slug": str, "answer": str | None, "message": str | None}``. A
    denial or degrade is a normal `200` response body, not an HTTP error:
    the caller (agent A's own delegation tool) is expected to read
    ``status``/``reason`` and continue the turn on its own judgment — see
    the module docstring.
    """
    session_id, caller_user_id, caller_email = principal
    manager = get_current_chat_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail={"code": "chat_disabled"})
    return await manager.handle_delegation(
        session_id,
        target_slug=slug,
        message=body.message,
        caller_user_id=caller_user_id,
        caller_email=caller_email,
    )
