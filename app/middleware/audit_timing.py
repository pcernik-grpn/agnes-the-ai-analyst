"""Pure-ASGI middleware stamping the audit-timing + request-meta contextvars.

Deliberately NOT a ``BaseHTTPMiddleware`` — this app streams SSE through
its middleware stack (see the GZip/broker-SSE incidents) and the pure ASGI
form adds zero buffering or task-group overhead to the hot path.

Must stay mounted INSIDE ``RequestIdMiddleware`` (i.e. ``add_middleware``d
BEFORE it in ``app/main.py``, so ``RequestIdMiddleware`` ends up the more
OUTER of the two and its "before" phase — which sets
``app.logging_config.request_id_var`` — has already run by the time this
middleware reads it). Starlette makes the LAST ``add_middleware`` call the
OUTERMOST layer, so "mounted inside X" means "added before X".
"""

from __future__ import annotations

from starlette.requests import Request

from app.auth.client_ip import trusted_client_ip
from app.logging_config import request_id_var
from src.audit_context import mark_request_start, set_request_meta


class AuditTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            mark_request_start()
            request = Request(scope)
            set_request_meta(client_ip=trusted_client_ip(request), correlation_id=request_id_var.get())
        await self.app(scope, receive, send)
