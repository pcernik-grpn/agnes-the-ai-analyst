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
"""

from __future__ import annotations


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
        await self.app(scope, receive, send)
