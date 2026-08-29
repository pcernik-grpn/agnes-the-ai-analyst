"""Request-scoped context for audit rows (F0 — audit-full-coverage plan).

An ASGI middleware (``app/middleware/audit_timing.py``) stamps the request
start into a contextvar; ``AuditRepository.log`` (both backends) fills
``duration_ms`` from it when the caller didn't pass one. One change covers
every HTTP-triggered audit write — no per-endpoint instrumentation.

Non-HTTP writers (scheduler internals, services) simply see ``None`` and
keep writing NULL duration, exactly as before.

Task 1 widens the same pattern to three more fields ``AuditRepository.log``
autofills when the caller passes ``None``: ``client_ip`` / ``correlation_id``
(stamped once per request by the same middleware) and ``client_kind``
(stamped by ``app/auth/dependencies.py`` after auth resolves, or explicitly
by a non-HTTP surface like MCP/Slack/Telegram). ``set_audit_identity`` /
``auto_audit_identity`` and ``mark_audit_written`` / ``audit_written_count``
exist for the F1 fallback middleware (Task 2) — not consumed anywhere in
this module, just defined here alongside the rest of the request context.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

_request_started: ContextVar[float | None] = ContextVar("audit_request_started", default=None)

# (client_ip, correlation_id) for the current request — stamped once by
# ``app/middleware/audit_timing.py`` from the trusted-proxy-derived IP and
# the request-id logging contextvar, consumed by ``AuditRepository.log()``
# on both backends when the caller passed ``None`` for either field.
_request_meta: ContextVar["tuple[str | None, str | None] | None"] = ContextVar("audit_request_meta", default=None)

# Non-HTTP surfaces (MCP, Slack, Telegram, the scheduler, ...) stamp this
# explicitly since there is no request to derive it from automatically.
# ``app/auth/dependencies.py`` also sets it to "web" for a plain
# authenticated dict user, but never downgrades an already-set non-web kind
# — see ``auto_client_kind``'s docstring for why.
_client_kind: ContextVar["str | None"] = ContextVar("audit_client_kind", default=None)

# (user_id, email) for the authenticated caller of the current request,
# stamped once auth resolves (``app/auth/dependencies.py``). Read by the F1
# fallback middleware (Task 2) to attribute a generic ``http.request`` row
# without re-running the auth dependency.
_audit_identity: ContextVar["tuple[str | None, str | None] | None"] = ContextVar("audit_identity", default=None)

# How many audit rows this request/context has written so far. The F1
# fallback middleware (Task 2) uses this to detect "the handler already
# wrote its own row" and skip the generic fallback row.
_written: ContextVar[int] = ContextVar("audit_written_count", default=0)


def mark_request_start() -> None:
    """Record 'now' as the current request's start (monotonic clock)."""
    _request_started.set(time.monotonic())


def auto_duration_ms() -> int | None:
    """Milliseconds since ``mark_request_start`` in this context, or ``None``
    outside a request scope. Measures request-start → audit-write, i.e. the
    handler work up to the audit point."""
    t0 = _request_started.get()
    if t0 is None:
        return None
    return int((time.monotonic() - t0) * 1000)


def set_request_meta(*, client_ip: str | None, correlation_id: str | None) -> None:
    """Stamp the current request's client IP + correlation id. Called once,
    per request, by ``AuditTimingMiddleware``."""
    _request_meta.set((client_ip, correlation_id))


def auto_client_ip() -> str | None:
    """The current request's trusted client IP, or ``None`` outside a
    request scope / before the middleware has stamped it."""
    meta = _request_meta.get()
    return meta[0] if meta is not None else None


def auto_correlation_id() -> str | None:
    """The current request's correlation id, or ``None`` outside a request
    scope / before the middleware has stamped it."""
    meta = _request_meta.get()
    return meta[1] if meta is not None else None


def set_client_kind(kind: str) -> None:
    """Stamp the current context's client kind (see
    ``src.audit_helpers.CLIENT_KINDS``). Explicit surface-specific stamps
    (mcp/slack/telegram/...) should call this directly rather than going
    through ``client_kind_from_user`` + ``app/auth/dependencies.py``'s
    generic web/cli/scheduler classification."""
    _client_kind.set(kind)


def auto_client_kind() -> str | None:
    """The current context's client kind, or ``None`` if nothing has
    stamped one yet."""
    return _client_kind.get()


def set_audit_identity(user_id: str | None, email: str | None) -> None:
    """Stamp the current context's authenticated identity, for the F1
    fallback middleware (Task 2) to attribute a generic audit row without
    re-running the auth dependency."""
    _audit_identity.set((user_id, email))


def auto_audit_identity() -> "tuple[str | None, str | None]":
    """``(user_id, email)`` stamped by ``set_audit_identity``, or
    ``(None, None)`` if nothing has stamped one yet (unauthenticated
    request, or a non-HTTP context)."""
    identity = _audit_identity.get()
    return identity if identity is not None else (None, None)


def mark_audit_written() -> None:
    """Record that one more audit row was written in this context. Called
    by ``AuditRepository.log()`` (both backends) on every successful
    insert."""
    _written.set(_written.get() + 1)


def audit_written_count() -> int:
    """How many audit rows :func:`mark_audit_written` has recorded in this
    context so far. ``0`` outside any write."""
    return _written.get()


def _reset_for_tests() -> None:
    """Reset every contextvar in this module to its default.

    A ``ContextVar.set()`` call made directly in a synchronous test function
    body (no ``asyncio``/``anyio`` task boundary, no
    ``contextvars.copy_context().run(...)`` wrapper) mutates the SAME
    context pytest keeps running subsequent tests in on that worker process
    — the value leaks forward, silently, to any later test that doesn't
    pass an explicit kwarg. ``tests/conftest.py``'s ``_reset_module_caches``
    autouse fixture calls this before and after every test, the same
    treatment every other module-level cache on that fixture gets. Tests
    that legitimately need isolation WITHIN a single test function (calling
    ``set_request_meta`` twice with different values) still want
    ``contextvars.copy_context().run(...)`` — this only guards the
    ACROSS-test boundary.
    """
    _request_started.set(None)
    _request_meta.set(None)
    _client_kind.set(None)
    _audit_identity.set(None)
    _written.set(0)
