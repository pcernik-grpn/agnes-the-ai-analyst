"""Pure-ASGI middleware: rewrite ``<slug>.<subdomain_base>`` host requests
to ``/apps/<slug>/...`` paths (Task 8 — ingress proxy + wake-on-request).

Only active when ``data_apps.subdomain_base`` is configured in
``instance.yaml`` (e.g. ``apps.example.com``). A request whose ``Host``
header is ``s.apps.example.com`` gets its ``scope["path"]`` rewritten to
``/apps/s`` + the original path (and ``scope["agnes_data_app_subdomain"]``
set to ``"s"`` — the marker ``app/api/data_apps_proxy.py`` reads to omit
``X-Forwarded-Prefix``, since a subdomain-origin request has no prefix
from the app's own point of view), then falls through to the normal
routing table — landing on ``app.api.data_apps_proxy``'s catch-all route
exactly as if the caller had hit ``https://<main-host>/apps/s/...``
directly. When ``subdomain_base`` is unset (the default), this middleware
is a no-op passthrough.

Deliberately a plain callable class (not ``BaseHTTPMiddleware``) so it
can inspect/rewrite ``scope`` before ASGI routing without buffering the
request/response bodies — a data-app's WebSocket traffic and the proxy's
streamed HTTP responses (``app/api/data_apps_proxy.py``) must never be
fully buffered in memory.

Audit-coverage wave 2, Task 3: this was one of three surfaces writing no
audit trail at all. Every subdomain-routed request is end-user traffic to a
deployed app, so logging every one would flood ``audit_log`` for no extra
signal — instead a small in-process TTL map (``_seen``) records the first
request per ``(user, slug)`` in a 15-minute window as one
``data_app.access`` row; a same-window repeat is silently skipped. Identity
is read cheaply, without a database round trip, straight off the
``access_token`` session cookie (a self-contained, signature-verified JWT —
see ``app.auth.jwt.verify_token``'s "trusted purely off signature + exp"
contract). A request with no such cookie (PAT/bearer callers, or a caller
who isn't logged in yet — the downstream proxy's own RBAC still gates
those) is simply not attributable here and is not audited by this
middleware.
"""

from __future__ import annotations

import time

# (user_id, slug) -> the monotonic time of the last audited access. Module
# level (one map per process, like the fallback middleware's contextvars) —
# bounded so a churn of distinct users/apps can't leak memory forever.
_seen: dict[tuple[str, str], float] = {}
_SEEN_WINDOW_MINUTES = 15
_SEEN_TTL_S = _SEEN_WINDOW_MINUTES * 60
_SEEN_MAX_ENTRIES = 10_000


def _should_audit_access(user_id: str, slug: str) -> bool:
    """True the first time ``(user_id, slug)`` is seen in the current
    ``_SEEN_TTL_S`` window, False for a repeat within it. Evicts the oldest
    entry once the map hits ``_SEEN_MAX_ENTRIES`` (checked before insert, so
    the map never exceeds the cap by more than the one entry being added)."""
    now = time.monotonic()
    key = (user_id, slug)
    last = _seen.get(key)
    if last is not None and now - last < _SEEN_TTL_S:
        return False
    if key not in _seen and len(_seen) >= _SEEN_MAX_ENTRIES:
        oldest_key = min(_seen, key=_seen.__getitem__)
        _seen.pop(oldest_key, None)
    _seen[key] = now
    return True


def _user_from_session_cookie(scope) -> "str | None":
    """The ``sub`` claim of a valid ``access_token`` session cookie, or
    ``None`` — no DB lookup, see module docstring."""
    from starlette.requests import Request

    from app.auth.jwt import verify_token

    token = Request(scope).cookies.get("access_token")
    if not token:
        return None
    payload = verify_token(token)
    if not payload:
        return None
    return payload.get("sub")


class DataAppSubdomainMiddleware:
    """Rewrite ``<slug>.<base>`` host requests to ``/apps/<slug>/...`` paths."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            from app.instance_config import get_data_apps_config

            # `get_data_apps_config()` is hardened to always return a dict
            # (never `None`, even for an explicit null `data_apps:` block or
            # a config-not-loaded-yet state) — this middleware runs on
            # EVERY request (including `/metrics`, `/healthz`, etc.), so
            # callers here rely on that guarantee rather than re-guarding.
            base = (get_data_apps_config().get("subdomain_base") or "").strip(".")
            if base:
                host = dict(scope.get("headers") or {}).get(b"host", b"").decode().split(":")[0]
                if host.endswith("." + base):
                    slug = host[: -(len(base) + 1)]
                    if "." not in slug:
                        scope = dict(scope)
                        # Marker the proxy reads to decide whether to set
                        # X-Forwarded-Prefix (see app/api/data_apps_proxy.py
                        # `_proxy`) — a subdomain-origin request has no
                        # prefix from the app's own point of view, unlike
                        # the path-prefix form of the same route.
                        scope["agnes_data_app_subdomain"] = slug
                        # The path as the VISITOR asked for it. The rewrite below
                        # is irreversible from downstream's point of view (a real
                        # app path could itself start with `/apps/<slug>`), and
                        # the 401->login redirect has to hand back a return URL
                        # in the visitor's own terms, not ours.
                        scope["agnes_data_app_original_path"] = scope["path"]
                        scope["path"] = f"/apps/{slug}" + scope["path"]

                        user_id = _user_from_session_cookie(scope)
                        if user_id and _should_audit_access(user_id, slug):
                            from src.audit_helpers import log_safe

                            log_safe(
                                user_id=user_id,
                                action="data_app.access",
                                resource=f"data_app:{slug}",
                                params={"window_minutes": _SEEN_WINDOW_MINUTES},
                            )
        await self.app(scope, receive, send)
