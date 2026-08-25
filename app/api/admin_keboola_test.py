"""POST /api/admin/keboola/test-connection — admin-only health probe.

Lets an admin verify the saved Keboola config from /admin/server-config
WITHOUT having to wait for a sync failure. Reads stack_url and token_env
from instance config (same path as the Discover endpoint), then calls
KeboolaClient.buckets.list() — a minimal round-trip that confirms the
token is valid and the stack URL is reachable.

This probes ONLY instance-level credentials (``data_source.keboola.*`` +
``KEBOOLA_STORAGE_TOKEN``) — never the ``source_connections`` registry
(per-project connections have their own probe:
``POST /api/admin/source-connections/{id}/test``, which would be duplicated
here otherwise). Every response therefore names the layer it checked
(``scope: "instance"``) so an operator whose real Keboola projects live
entirely in the registry doesn't read a ``not_configured`` instance-level
result as "Keboola is broken".
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException

from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/keboola", tags=["admin"])

#: Probed by `test_connection` below — kept as a module constant so the
#: docstring/response and the actual check can't drift apart.
_SCOPE = "instance"


def _registry_has_keboola_connections() -> bool:
    """True if `source_connections` has at least one keboola row.

    Read-only signal used ONLY to decide whether to extend the
    `not_configured` hint below with a pointer to the per-project card —
    never changes what this endpoint tests. Wrapped so an unreadable
    registry (backend hiccup, migration mid-flight) degrades to "no
    signal" rather than turning an already-failing health probe into a 500.
    """
    try:
        from src.repositories import source_connections_repo

        return bool(source_connections_repo().list(source_type="keboola"))
    except Exception:
        logger.warning("keboola test-connection: could not read source_connections registry", exc_info=True)
        return False


def _not_configured_hint(base_hint: str) -> str:
    """`base_hint` plus a pointer to the per-project card, but ONLY when the
    registry actually has a keboola row — otherwise the plain hint stands,
    since there is nothing else to point the operator at."""
    if _registry_has_keboola_connections():
        return f"{base_hint} Per-project Keboola connections are tested on their own card at /admin/data-sources."
    return base_hint


@router.post("/test-connection")
def test_connection(_user: dict = Depends(require_admin)):
    """Verify the Keboola Storage API token by listing buckets.

    Declared as a plain ``def`` (not ``async``) so FastAPI runs it in the
    default threadpool executor — the underlying KeboolaClient does
    synchronous file I/O on init and synchronous HTTP on buckets.list(),
    neither of which is safe to call on the async event-loop thread.

    Returns 200 with ``{ok, scope, stack_url, bucket_count, elapsed_ms}`` on
    success. Every response — success or error — carries ``scope: "instance"``
    naming the layer this probe checks (instance-level config, never the
    per-project ``source_connections`` registry).

    Error responses:
    - 400 ``not_configured`` — token or URL not set. ``hint`` additionally
      points at ``/admin/data-sources`` when the registry has a keboola
      connection, so "not configured here" doesn't read as "Keboola is
      broken" on an instance whose real projects live there.
    - 400 ``invalid_token`` — Keboola returned 401
    - 502 ``keboola_upstream_error`` — other API error
    """
    from app.instance_config import get_value

    stack_url = get_value("data_source", "keboola", "stack_url", default="")
    if not stack_url:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "not_configured",
                "scope": _SCOPE,
                "hint": _not_configured_hint("stack_url is not set. Configure it in Instance settings → Data source."),
            },
        )

    token_env = get_value("data_source", "keboola", "token_env", default="KEBOOLA_STORAGE_TOKEN")
    from app.datasource_secrets import keboola_instance_token  # noqa: PLC0415

    # The ONE place this 3-step fallback (token_env -> KEBOOLA_STORAGE_TOKEN
    # -> vault) is implemented — this used to duplicate it inline, which is
    # exactly the drift `keboola_instance_token()` was extracted to prevent.
    token, _provenance = keboola_instance_token(token_env or "")
    if not token:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "not_configured",
                "scope": _SCOPE,
                "hint": _not_configured_hint(
                    f"Token env var {token_env!r} is not set. Add it to your .env file or the "
                    "datasource-credentials vault."
                ),
            },
        )

    try:
        from connectors.keboola.client import KeboolaClient

        client = KeboolaClient(token=token, url=stack_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "not_configured",
                "scope": _SCOPE,
                "hint": str(exc),
            },
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "kind": "keboola_upstream_error",
                "scope": _SCOPE,
                "hint": str(exc),
            },
        )

    started = time.monotonic()
    try:
        buckets = client.client.buckets.list()
        bucket_count = len(buckets) if buckets else 0
    except Exception as exc:
        # Inspect the HTTP status code directly from the requests HTTPError
        # rather than string-matching the message (fragile across library versions).
        http_status = None
        try:
            http_status = exc.response.status_code  # requests.exceptions.HTTPError
        except AttributeError:
            pass
        if http_status == 401 or http_status == 403:
            raise HTTPException(
                status_code=400,
                detail={
                    "kind": "invalid_token",
                    "scope": _SCOPE,
                    "hint": "Storage API token is invalid or expired. Check the token in your .env file.",
                },
            )
        raise HTTPException(
            status_code=502,
            detail={
                "kind": "keboola_upstream_error",
                "scope": _SCOPE,
                "hint": str(exc),
            },
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return {
        "ok": True,
        "scope": _SCOPE,
        "stack_url": stack_url,
        "bucket_count": bucket_count,
        "elapsed_ms": elapsed_ms,
    }
