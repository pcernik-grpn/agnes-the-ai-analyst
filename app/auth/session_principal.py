"""SessionPrincipal — the auth subject of a live co-drive session.

A co-session is driven by 2+ humans. Its effective authority is the
*intersection* of all live participants' grants (never any one user's full
set, never the Admin god-mode short-circuit). The resolver builds this from
``chat_session_participants WHERE left_at IS NULL`` on every request; the JWT
carries no participant identity (SR-4), so this object is always live-fresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: str
    participant_user_ids: list[str]
    participant_emails: list[str]
    intersection: dict[str, frozenset[str]]  # resource_type -> allowed resource_ids


@dataclass(frozen=True)
class AgentPrincipal:
    """Auth subject of a live agent-scoped session (V1d).

    Effective authority = the agent's own resolved authority
    (``resolve_agent_authority``, C2.2) — a *restriction* of its owner's
    self-declared access, never an elevation of it, and never the Admin
    god-mode short-circuit: a self-declared item (including one an admin
    OWNER declared for their own agent) always narrows to that identity's
    CURRENT explicit grants. The one deliberate exception is an item a
    THIRD-PARTY admin explicitly granted to the agent (recorded via
    ``agent_scope.granted_by``, granter distinct from the owner) — that item
    is the agent's own authority in its own right and MAY exceed what the
    owner personally holds (D-C2). Like ``SessionPrincipal`` the
    intersection is rebuilt live per request (the token bakes in no
    grants), so revoking a grant or narrowing the agent takes effect on the
    next request with no stale-replay window.
    """

    session_id: str
    agent_id: str
    owner_user_id: str
    owner_email: str
    intersection: dict[str, frozenset[str]]


#: Either restricted principal. Consumers that mean "not a full user dict —
#: use the intersection, deny admin" should branch on this union, not on one
#: member, so a new principal kind cannot silently bypass a seam.
Principal = Union[SessionPrincipal, AgentPrincipal]

#: Runtime companion to :data:`Principal` for ``isinstance`` checks —
#: ``isinstance(x, Principal)`` is a TypeError on a ``typing.Union``. Every
#: seam that means "restricted principal" MUST test against this tuple, never
#: against a single member, so adding a third principal kind cannot silently
#: leave a seam behind. Checks that are deliberately co-drive-specific (the
#: participant bookkeeping in ``app/api/chat_copresence.py``, the resolver's
#: construction site in ``app/auth/pat_resolver.py``) keep naming
#: ``SessionPrincipal`` directly — that is the signal they are NOT a
#: restricted-principal seam.
PRINCIPAL_TYPES: tuple[type, ...] = (SessionPrincipal, AgentPrincipal)
