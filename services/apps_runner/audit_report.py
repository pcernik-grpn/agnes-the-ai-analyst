"""Best-effort audit report-back from apps-runner to the control plane
(audit-coverage wave 2, Task 3).

apps-runner holds the Docker socket but no database access — it imports
nothing from ``src.``/``app.`` (see the module docstring of
``services/apps_runner/api.py``) — so it cannot write an ``audit_log`` row
itself. ``report_event`` is its only path to one: a small POST to the
control plane's ``POST /api/data-apps/runner-events``, authenticated with
the SAME shared ``X-Runner-Token`` the control plane already presents TO
this sidecar (``APPS_RUNNER_TOKEN``) — just in the reverse direction. The
control plane validates the action against a server-side whitelist and
writes the row itself with ``client_kind="system"`` (see
``app/api/data_apps.py``'s ``record_runner_event``).

A container lifecycle action (start/stop/resume) must never fail — or even
slow down noticeably — because its audit report did: any transport error,
timeout, or non-2xx response is logged and swallowed here, never raised.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# The same well-known compose-network address `src.data_apps.spec
# .AGNES_INTERNAL_URL` bakes into a data app's own env — duplicated here
# (never imported) because apps-runner must import nothing from
# `src.`/`app.` (see module docstring above).
_DEFAULT_CONTROL_PLANE_URL = "http://app:8000"

# Short and best-effort: this call is fire-and-forget from the caller's
# point of view, so a wedged control plane must not stall a container action.
_REPORT_TIMEOUT_S = 5.0


def report_event(action: str, params: dict) -> None:
    """POST one lifecycle event to the control plane. Never raises."""
    base = os.environ.get("AGNES_URL", _DEFAULT_CONTROL_PLANE_URL).rstrip("/")
    token = os.environ.get("APPS_RUNNER_TOKEN", "")
    try:
        httpx.post(
            f"{base}/api/data-apps/runner-events",
            json={"action": action, "params": params},
            headers={"X-Runner-Token": token},
            timeout=_REPORT_TIMEOUT_S,
        )
    except Exception:
        logger.warning("apps-runner: failed to report audit event %r", action, exc_info=True)
