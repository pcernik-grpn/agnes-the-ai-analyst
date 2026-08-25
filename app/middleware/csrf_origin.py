"""Origin / ``Sec-Fetch-Site`` CSRF gate for the cookie-authenticated API (F2).

Agnes' double-submit ``web_csrf`` token guards only the pure-HTML form POST
handlers. The far larger ``/api/**`` JSON surface (admin, sync, …) is called
with ``fetch(credentials:"include")`` and authenticated by the ``access_token``
session cookie with no token. Its protection had been *implicit*: a Pydantic
body forces ``application/json`` (a CORS pre-flighted content-type the allowlist
rejects cross-origin) and ``SameSite=Lax`` keeps the cookie off a truly
cross-site POST. That implicit story has two holes:

* mutations that take **no JSON body** (``POST /api/sync/trigger``, the admin
  ``run-*`` family — query-param / empty-body) are CORS *simple* requests: no
  pre-flight, no content-type barrier at all; and
* when data-apps are hosted on sibling sub-domains the session cookie is scoped
  to the shared parent (``Domain=.<base>``, see
  ``app.instance_config.session_cookie_domain``), so a **same-site** POST from
  user-authored data-app code carries it — ``SameSite=Lax`` treats a sibling
  sub-domain as same-site and does not strip the cookie.

This middleware makes the defense explicit and closes both holes: a
state-changing request that is authenticated *only* by the session cookie is
refused when the browser reports it as cross-origin — ``Sec-Fetch-Site:
same-site|cross-site`` (authoritative, set by the browser and unforgeable by
page script), or, for browsers too old to send it, an ``Origin`` that is
neither same-origin nor on the operator's explicit CORS allowlist.

Deliberately conservative — it only ever acts on **positive** cross-origin
evidence, so anything without it passes through unchanged:

* bearer/PAT callers (CLI, MCP, agent API) send ``Authorization`` and no
  session cookie — explicit auth is not a CSRF target;
* same-origin UI ``fetch`` sends ``Sec-Fetch-Site: same-origin``;
* header-less clients (server-to-server, test tooling, ``curl``) present no
  cross-origin evidence and a real browser always sends at least ``Origin`` on a
  state-changing request, so failing open here costs no realistic protection.

Pure ASGI (never buffers a body) so it is SSE / streamable-MCP safe, matching
``app.middleware.security_headers.SecurityHeadersMiddleware``.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from starlette.requests import Request

# Only unsafe (state-changing) methods are gated. GET/HEAD/OPTIONS/TRACE are
# safe and must never be blocked (and there are no state-changing GET routes —
# an invariant the security playbook already requires).
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Reverse-proxied data-app content (`/apps/<slug>/...`, 3+ path segments,
# including sub-domain requests DataAppSubdomainMiddleware rewrites to that
# shape). It is user-authored and has its own request semantics — the Agnes
# CSRF gate must not sit in front of it. Matches SecurityHeadersMiddleware.
_DATA_APP_PROXY_PATH_RE = re.compile(r"^/apps/[^/]+/")

_SEC_FETCH_SAME = frozenset({"same-origin", "none"})
_SEC_FETCH_CROSS = frozenset({"same-site", "cross-site"})

_FORBIDDEN_BODY = json.dumps({"detail": "cross-origin request blocked"}).encode()


class CsrfOriginMiddleware:
    """Reject cookie-authenticated state-changing requests that a browser
    reports as cross-origin, unless the origin is explicitly allowlisted."""

    def __init__(self, app, allowed_origins: set[str] | None = None, enabled: bool = True):
        self.app = app
        # Exact-string match against the operator's CORS_ORIGINS, mirroring
        # Starlette's own CORSMiddleware comparison; "*" means allow any.
        self._allowed = set(allowed_origins or set())
        self._allow_any = "*" in self._allowed
        self.enabled = enabled

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        if scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        if _DATA_APP_PROXY_PATH_RE.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # Bearer/PAT callers are explicitly authenticated, not ambient-cookie
        # authenticated → not a CSRF target. The gate applies only when the
        # request rides the session cookie and nothing else.
        if request.headers.get("authorization"):
            await self.app(scope, receive, send)
            return
        if not request.cookies.get("access_token"):
            await self.app(scope, receive, send)
            return

        if self._is_cross_origin(request):
            await self._forbid(send)
            return

        await self.app(scope, receive, send)

    def _is_cross_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin")
        origin_ok = bool(origin) and (self._allow_any or origin in self._allowed)

        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
        if sec_fetch_site:
            # Authoritative: the browser classified the request itself.
            if sec_fetch_site in _SEC_FETCH_SAME:
                return False
            if sec_fetch_site in _SEC_FETCH_CROSS:
                return not origin_ok
            # Unknown/future value → treat as cross-origin unless allowlisted.
            return not origin_ok

        # Older browsers omit Sec-Fetch-Site: fall back to Origin. A real
        # browser always sends Origin on a state-changing request.
        if origin:
            return not (origin_ok or origin == _request_origin(request))

        # No cross-origin evidence at all → not the threat this gate defends.
        return False

    async def _forbid(self, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_FORBIDDEN_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _FORBIDDEN_BODY})


def _request_origin(request: Request) -> str:
    """The request's own origin (``scheme://host[:port]``), for the
    Sec-Fetch-Site-less fallback. ``request.url.scheme`` reflects
    X-Forwarded-Proto when uvicorn runs with ``--proxy-headers``."""
    host = request.headers.get("host", "")
    scheme = request.url.scheme or "http"
    # Normalize away a default port so `https://h:443` still matches `https://h`.
    parts = urlsplit(f"{scheme}://{host}")
    hostname = parts.hostname or ""
    port = parts.port
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if port is None or default:
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"
