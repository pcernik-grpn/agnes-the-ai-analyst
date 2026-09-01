"""deny_principal — 403 a restricted principal on human-only routes.

Covers every ``Principal`` kind (co-session runner token, agent-session
sandbox token) and a read-only view-as session. The routes behind this guard
go on to read ``user["id"]`` / ``user["email"]``, which no principal dataclass
can answer — and, more to the point, they are human-driven actions (co-presence
invite/join/leave, parking an upstream OAuth credential, setting a per-source
secret) that must never be performed on somebody's behalf by anything other
than that person.

That last clause is why an active view-as is refused here too. Two of these
routes are state-changing **GET**s — the OAuth connect authorize/callback pair
(``app/api/mcp_oauth_connect.py``), a browser navigation by necessity — so the
method-based read-only guard in ``app/middleware/view_as_readonly.py`` cannot
see them. Without this, an admin viewing as someone could park an upstream
credential on that person's account while a banner promised the session was
read-only. Adding the check HERE rather than at each route keeps it a property
of the guard: a route that adopts ``deny_principal`` tomorrow gets it free, and
there is no list to keep in sync.

The 403 detail string is deliberately unchanged (existing clients and tests
match on it); it under-describes both the agent and the view-as case but says
the operative thing.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.auth.session_principal import PRINCIPAL_TYPES


def deny_principal(user) -> None:
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(status_code=403, detail="not available to co-session token")

    from app.auth.view_as import active_ticket

    if active_ticket() is not None:
        raise HTTPException(status_code=403, detail="not available in view-as")
