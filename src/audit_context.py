"""Request-scoped timing context for audit rows.

An ASGI middleware (``app/middleware/audit_timing.py``) stamps the request
start into a contextvar; ``AuditRepository.log`` (both backends) fills
``duration_ms`` from it when the caller didn't pass one. One change covers
every HTTP-triggered audit write — no per-endpoint instrumentation.

Non-HTTP writers (scheduler internals, services) simply see ``None`` and
keep writing NULL duration, exactly as before.
"""

from __future__ import annotations

import time
from contextvars import ContextVar

_request_started: ContextVar[float | None] = ContextVar("audit_request_started", default=None)


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
