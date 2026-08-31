"""SessionPrincipal — the auth subject of a live co-drive session.

A co-session is driven by 2+ humans. Its effective authority is the
*intersection* of all live participants' grants (never any one user's full
set, never the Admin god-mode short-circuit). The resolver builds this from
``chat_session_participants WHERE left_at IS NULL`` on every request; the JWT
carries no participant identity (SR-4), so this object is always live-fresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Union


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

    ``caller_user_id``/``caller_email`` (C2.3, shared-agent runtime) are the
    identity of whoever is actually DRIVING this turn — the agent's OWNER
    when they run their own agent, but a different user when the agent was
    shared to them via a ``ResourceType.AGENT`` grant. Kept ALONGSIDE
    ``owner_user_id``/``owner_email`` (never replacing them): the agent's
    *authority* (``intersection``, above) still derives from the owner/
    granter per C2.2's ``resolve_agent_authority`` — only *row-level access
    policies* (``src/access_policy.py``) bind to the caller, so each user
    sharing one agent sees their own rows. Default ``None`` for callers that
    construct this dataclass without a distinct caller identity (tests, and
    any resolution path predating C2.3); ``src/access_policy.py`` falls back
    to the owner identity in that case, which reproduces the exact pre-C2.3
    behavior — every REAL production construction site
    (``app/auth/pat_resolver.py``) always supplies both, derived from the
    chat session's own stored ``user_email`` (server-side, set at session
    creation), never from a client-supplied claim, so this fallback is never
    exercised there.
    """

    session_id: str
    agent_id: str
    owner_user_id: str
    owner_email: str
    intersection: dict[str, frozenset[str]]
    caller_user_id: str | None = None
    caller_email: str | None = None


@dataclass(frozen=True)
class ProducerPrincipal:
    """Auth subject of a corpus-extraction producer's scoped callback
    credential — see ``app.auth.producer_token`` for how it is minted
    (``app/worker/kinds.py::_agnes_producer_callback_env``) and resolved.

    Replaces the historical over-grant of the scheduler shared-secret
    token (which resolved to a synthetic Admin-group user) with a narrow,
    short-lived JWT naming exactly one connection and its own confirmed
    scope collections.

    Deliberately carries NO ``intersection`` field the way
    ``SessionPrincipal``/``AgentPrincipal`` do: its authority is not
    modeled by the generic per-resource-type grant-intersection primitive
    those two share (``app.auth.access.can_access_session``) — that helper
    rejects this principal type outright (see its own docstring).
    ``app.auth.producer_token`` fail-closes it to a small, FIXED set of
    endpoints before this object is ever constructed, and each of those
    endpoints applies its own explicit scope check against
    ``connection_id``/``collection_ids`` — never a generic grant table.
    """

    connection_id: str
    collection_ids: frozenset[str]
    jti: str

    #: EMPTY, and deliberately so — this principal has no grant-table
    #: authority at all. It exists because ``PRINCIPAL_TYPES`` conflates two
    #: questions its members are branched on: "not a full user dict?" (true
    #: here) and "read its ``intersection``?" (meaningless here). Five sites
    #: ask the first and then do the second —``src.rbac.get_accessible_ids``,
    #: ``src.marketplace_filter``, ``app.services.stack_resolver``,
    #: ``app.api.knowledge_search``, ``app.api.memory`` — so a member without
    #: the attribute makes each an ``AttributeError`` (a 500) instead of a
    #: clean deny. Unreachable today only because ``app.auth.producer_token``
    #: fail-closes this principal to five endpoints none of those sit behind;
    #: an empty mapping makes the seam TOTAL, so adding a sixth endpoint
    #: denies rather than crashes. Never the authorization itself: that stays
    #: the surface allowlist plus each endpoint's own explicit
    #: ``connection_id``/``collection_ids`` check.
    intersection: Mapping[str, frozenset[str]] = field(default_factory=dict)


#: Either restricted principal. Consumers that mean "not a full user dict —
#: use the intersection, deny admin" should branch on this union, not on one
#: member, so a new principal kind cannot silently bypass a seam.
Principal = Union[SessionPrincipal, AgentPrincipal, ProducerPrincipal]

#: Runtime companion to :data:`Principal` for ``isinstance`` checks —
#: ``isinstance(x, Principal)`` is a TypeError on a ``typing.Union``. Every
#: seam that means "restricted principal" MUST test against this tuple, never
#: against a single member, so adding a third principal kind cannot silently
#: leave a seam behind. Checks that are deliberately co-drive-specific (the
#: participant bookkeeping in ``app/api/chat_copresence.py``, the resolver's
#: construction site in ``app/auth/pat_resolver.py``) keep naming
#: ``SessionPrincipal`` directly — that is the signal they are NOT a
#: restricted-principal seam.
PRINCIPAL_TYPES: tuple[type, ...] = (SessionPrincipal, AgentPrincipal, ProducerPrincipal)
