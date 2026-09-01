"""View-as: stamp the request, and refuse everything that is not a read.

Two jobs, one pass, because they must agree by construction:

1. **Stamp** the request-scoped ticket (``app.auth.view_as``) so the
   authorization guards and the audit attribution downstream can see it. This
   has to happen HERE, in the request's own async task — ``get_current_user``
   is a plain ``def`` that FastAPI offloads to the anyio thread pool, where a
   ``ContextVar.set()`` mutates a copy and is lost on return (the trap
   ``app/middleware/audit_fallback.py`` documents). Forward propagation into
   dependencies and handlers works; backward does not.
2. **Refuse** every non-GET/HEAD request while the ticket is active, plus
   every WebSocket handshake.

The refusal is by METHOD, never by route: an allow-list of "safe" routes is a
list somebody has to remember to update, and the one time they don't is the
hole. Anything unrouted, anything added tomorrow, anything mounted by a
sub-application is covered the moment it is not a GET. ``OPTIONS`` is refused
with everything else — the dashboard is same-origin, so nothing legitimate
pre-flights, and "exactly {GET, HEAD}" is a rule a reviewer can check at a
glance.

WebSockets are refused rather than narrowed: a socket carries frames this
guard cannot classify (chat sends messages over one), and a read-only mode
that admits a bidirectional channel is not read-only.

**The one exception** is ``view_as.EXIT_PATH``, matched exactly. Its handler
clears the ticket cookie and writes an audit row; it takes no other input and
performs no other effect, so letting it through is what makes the mode
escapable rather than a trap. It carries its own CSRF check.

Why the stamp needs the session cookie too
------------------------------------------
The ticket names its viewer, and this middleware refuses to engage unless the
request's OWN session cookie decodes to that same person. Two consequences,
both wanted: a copied cookie is inert in anyone else's browser, and the
read-only freeze can never be inflicted on a user who is not actually in the
mode (an attacker who could plant a cookie on a victim's browser would
otherwise have a denial-of-service against every mutation they make). The
decode is a signature check on a JWT already in hand — no database, no cost.

Precedence mirrors ``get_current_user`` exactly: an ``Authorization: Bearer``
header wins over the cookie there, so a bearer-authenticated request is not a
browser session and the mode does not engage here either. If those two ever
disagreed, the dangerous direction would be "auth swapped the principal but
the guard let a mutation through", so the guard's condition is deliberately
the WEAKER of the two (it does not re-check that the viewer is still an admin,
which needs a database read) — auth engaging always implies the guard
engaging, never the reverse.

Pure ASGI (never buffers a body) so it is SSE / streamable-MCP safe, matching
``app.middleware.csrf_origin`` and ``app.middleware.security_headers``.
"""

from __future__ import annotations

import json

from starlette.requests import HTTPConnection

from app.auth.view_as import (
    EXIT_PATH,
    VIEW_AS_COOKIE,
    reset_for_request,
    set_active_for_request,
    verify_ticket,
)

#: The only methods a view-as session may use. Everything else is refused.
_READ_METHODS = frozenset({"GET", "HEAD"})

#: Typed refusal — ``error`` is the machine-readable code clients branch on
#: (the ``requires_postgres_backend`` shape), ``detail`` the human sentence.
_REFUSED_BODY = json.dumps(
    {
        "error": "view_as_read_only",
        "detail": (
            "This browser session is viewing Agnes as another user. View-as is read-only — exit it to make changes."
        ),
    }
).encode()


#: Cheapest possible "is this even a view-as request?" test, so the ~100% of
#: traffic that is not pays a bytes scan of one header and nothing else — no
#: Request object, no cookie parse, no signature check. A false positive here
#: costs only the full check below; a false negative is impossible (the cookie
#: name must appear literally in the header for a browser to have sent it).
_COOKIE_HEADER = b"cookie"
_COOKIE_NEEDLE = VIEW_AS_COOKIE.encode() + b"="


def _might_carry_ticket(scope) -> bool:
    for name, value in scope.get("headers") or ():
        if name == _COOKIE_HEADER and _COOKIE_NEEDLE in value:
            return True
    return False


def _routed_path(scope) -> str:
    """The path the ROUTER will match, i.e. with any ASGI ``root_path`` prefix
    removed.

    Only used to recognise the exit route. Getting it wrong in the strict
    direction (not recognising it) would strand an admin in a mode they cannot
    leave until the ticket expires, on any deployment mounted under a
    sub-path — and the check must stay an exact match on the ROUTED path, so
    that nothing but the exit route itself can ever slip through the one hole
    in the read-only guard.
    """
    path = scope.get("path") or ""
    root = scope.get("root_path") or ""
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


class ViewAsReadOnlyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not _might_carry_ticket(scope):
            await self.app(scope, receive, send)
            return

        ticket = self._resolve_ticket(scope)
        if ticket is None:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "websocket":
            await self._refuse_websocket(send)
            return

        if scope["method"] not in _READ_METHODS and _routed_path(scope) != EXIT_PATH:
            await self._refuse(send)
            return

        token = set_active_for_request(ticket)
        try:
            await self.app(scope, receive, send)
        finally:
            # Always, on every exit path including an exception: the mode is
            # request-scoped, and a leaked stamp would narrow (or freeze) the
            # NEXT request served by this worker.
            reset_for_request(token)

    @staticmethod
    def _resolve_ticket(scope):
        """The active ticket for this request, or ``None``.

        ``None`` for: no cookie, a forged/expired one, a request that
        authenticates with a bearer token instead of the session cookie, and
        — the binding check — a ticket minted for somebody other than
        whoever this request's session cookie names.

        ``HTTPConnection``, not ``Request``: this runs for WebSocket scopes
        too, and ``Request.__init__`` asserts ``scope["type"] == "http"`` —
        an AssertionError here would turn a clean handshake refusal into a
        crash, which is a refusal that looks like a bug rather than a policy.
        """
        request = HTTPConnection(scope)
        raw = request.cookies.get(VIEW_AS_COOKIE)
        if not raw:
            return None
        if request.headers.get("authorization"):
            return None
        session_token = request.cookies.get("access_token")
        if not session_token:
            return None

        ticket = verify_ticket(raw)
        if ticket is None:
            return None

        from app.auth.jwt import verify_token

        payload = verify_token(session_token) or {}
        if str(payload.get("sub") or "") != ticket.viewer_user_id:
            return None
        return ticket

    @staticmethod
    async def _refuse(send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REFUSED_BODY)).encode()),
                    # Never cache a refusal: the same URL succeeds the moment
                    # the viewer exits the mode.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REFUSED_BODY})

    @staticmethod
    async def _refuse_websocket(send) -> None:
        # Closing before accepting is the ASGI way to reject a handshake;
        # the server turns it into an HTTP 403 for the client.
        await send({"type": "websocket.close", "code": 1008})
