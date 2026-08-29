"""ASGI safety net: log any authenticated mutating request whose handler
wrote no audit row of its own (F1 — audit-full-coverage plan, Task 2).

Every handler that already calls ``log_safe`` (directly, or through an
intra-module ``_audit`` wrapper) increments
``src.audit_context.audit_written_count()`` on the way — see
``AuditRepository.log()`` on both backends (Task 1). This middleware runs
the request through unchanged, and only AFTER the response has started
checks whether that counter is still zero: if so, and the request carried an
authenticated identity, it writes one generic ``http.request`` row keyed by
``"{METHOD} {route_template}"`` so no mutating endpoint can silently leave
zero trace. ``src.audit_posture.POSTURE`` lets a route opt out entirely via
an ``"exempt:<reason>"`` entry — everything else (a real cataloged action,
the placeholder ``"fallback"``, or simply absent from the dict) still gets
the generic row when the handler didn't write one; ``POSTURE`` documents
which routes have upgraded to their own dedicated action, not which routes
this middleware may cover.

**Sync path functions break the two contextvar reads above** — a large
share of this codebase's routes and the ``get_current_user`` dependency are
plain ``def``, which FastAPI/Starlette auto-offload to the anyio thread
pool (see ``app/auth/dependencies.py::get_current_user``'s own docstring).
``anyio.to_thread.run_sync`` copies the CURRENT context into the worker
thread; a ``ContextVar.set()`` performed inside that offloaded call (an
endpoint's own audit write via the repo layer's ``log()`` method →
``mark_audit_written()``, or ``_stash_user``'s ``set_audit_identity(...)``)
mutates only that thread's copy and is silently lost once the call returns
— this middleware, back in the original async context, would see a stale
``audit_written_count() == 0`` even though the sync endpoint DID write its
own row (e.g. ``POST /api/sync/trigger``'s plain-``def`` handler), and
duplicate it. Two independent workarounds, chosen per read because they
fail differently:

- **Identity** — prefer ``request.state.user``. Unlike a ContextVar,
  ``Request.state`` is an ordinary object keyed into the shared ASGI
  ``scope["state"]`` dict (``starlette.requests.HTTPConnection.state``), so
  ``_stash_user``'s ``request.state.user = user`` assignment is visible
  here regardless of which thread set it — the same mechanism the PostHog
  snippet injector and the 500 handler already rely on to read the caller
  without re-running auth. ``auto_audit_identity()`` (the ContextVar) is
  only the fallback for the rarer case where ``request.state.user`` was
  never set (e.g. auth never resolved).
- **"did the handler already write a row?"** — the ContextVar counter is
  trusted when it says ``> 0`` (a true positive can't happen), but a ``== 0``
  reading is ambiguous: genuinely nothing written, or a sync-offloaded write
  this process can't see. Broken ties with one extra read: every audit row
  autofills ``correlation_id`` from the SAME ``AuditTimingMiddleware``-set
  ContextVar (stamped in THIS async context, before any thread-offload, so
  forward propagation into the copy works — only the reverse direction is
  lossy) — querying for any row under this request's ``correlation_id``
  answers the ambiguity authoritatively, at the cost of one query on the
  (already the exception path) branch where the fast check said zero.

Deliberately NOT a ``BaseHTTPMiddleware`` — same SSE-streaming rationale as
``app/middleware/audit_timing.py``. Must be mounted so it runs INSIDE
``AuditTimingMiddleware`` (i.e. ``add_middleware``d BEFORE it in
``app/main.py``) — ``audit_written_count`` / ``auto_audit_identity`` /
``auto_correlation_id`` are contextvars stamped by ``AuditTimingMiddleware``
and ``app/auth/dependencies.py`` during the SAME request, and this
middleware reads them only after ``await self.app(...)`` returns, by which
point every inner layer (including the endpoint) has already run.
"""

from __future__ import annotations

from starlette.requests import Request

from src.audit_context import audit_written_count, auto_audit_identity, auto_correlation_id
from src.audit_helpers import identity_for_audit, log_safe
from src.audit_posture import MUTATING, POSTURE


class AuditFallbackMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            await self.app(scope, receive, send)
            return

        status_holder: dict = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        await self.app(scope, receive, send_wrapper)

        if audit_written_count() > 0:
            return
        if self._already_covered_by_correlation_id():
            return

        request = Request(scope)
        state_user = getattr(request.state, "user", None)
        if state_user is not None:
            user_id, _email = identity_for_audit(state_user)
        else:
            user_id, _email = auto_audit_identity()
        if user_id is None:
            # Unauthenticated (or a request that never reached auth
            # resolution, e.g. it 404'd before routing) — not an audit_log
            # concern; nothing attributable to write.
            return

        route = scope.get("route")
        template = getattr(route, "path", None) or scope.get("path", "?")
        key = f"{scope['method']} {template}"
        if POSTURE.get(key, "").startswith("exempt:"):
            return

        log_safe(
            user_id=user_id,
            action="http.request",
            resource=key,
            params={"status": status_holder.get("status")},
        )

    @staticmethod
    def _already_covered_by_correlation_id() -> bool:
        """True when a row already exists for this request's correlation
        id — the tie-break for a sync-offloaded write the contextvar
        counter couldn't see (see module docstring)."""
        correlation_id = auto_correlation_id()
        if not correlation_id:
            return False
        from src.repositories import audit_repo

        try:
            rows, _ = audit_repo().query(correlation_id=correlation_id, limit=1)
        except Exception:
            return False
        return bool(rows)
