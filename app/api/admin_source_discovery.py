"""Browse a configured data source's catalog before registering from it.

The "Add data source" wizard's Keboola step renders a bucket-grouped checkbox
picker from ``GET /api/admin/source-connections/{id}/tables``. That endpoint is
tied to a *connection record*, which only Keboola has — Snowflake's coordinates
live in ``data_source.snowflake`` (instance.yaml / ``/admin/server-config``), so
its wizard step had no picker at all and asked the operator to type the schema
and table by hand. Nothing checked those strings against the account, and a
mistyped one becomes a permanent registry row pointing at a table that does not
exist (only a re-save re-runs the remote-extract build, so it never heals).

This router is the connection-less half of that primitive, keyed on
``source_type`` rather than a connection id so the next source to grow a picker
adds a branch instead of a route.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/data-sources", tags=["admin"])

# Source types whose catalog this endpoint can browse. Keboola is absent on
# purpose: its listing is per-connection and already served by
# `/api/admin/source-connections/{id}/tables`.
_SUPPORTED = ("snowflake",)

# #1616: a failed catalog listing used to forward the raw exception verbatim
# — three layers of wrapper prose (this endpoint's own `f"could not list
# Snowflake tables: {exc}"`, `attach_snowflake`'s `f"snowflake ATTACH failed
# ({type(exc).__name__}): "`) around whatever DuckDB/Snowflake driver text
# came back, e.g. `[Snowflake] 390106 (08004): Specified password has
# expired. ... [<request-id>]`. Unreadable, and it leaked driver/DuckDB
# internals (exception class name, SQLSTATE, Snowflake error code, request
# id) into the browser for no operator benefit — the raw text already goes
# to the server log below, which is where a diagnosis actually happens.
#
# Snowflake's own authentication error codes live in the 3901xx range
# (expired/incorrect password, disabled user, invalid/expired token, ...);
# the substring hints are a backstop for driver builds/versions that spell
# the same failure out in English without the numeric code.
_SF_AUTH_CODE_RE = re.compile(r"\b390(1\d\d)\b")
_SF_AUTH_HINTS = (
    "password has expired",
    "incorrect username or password",
    "authentication failed",
    "authentication token has expired",
    "authentication token is invalid",
    "invalid credentials",
    "jwt token is invalid",
    "invalid oauth access token",
    "user is disabled",
    "account is locked",
)
_SF_AUTH_MESSAGE = (
    "Snowflake rejected the stored credential — commonly an expired or "
    "incorrect password. Reconnect with a fresh credential on this "
    "connection, then try again."
)
_SF_GENERIC_MESSAGE = (
    "Could not connect to the configured Snowflake account. Check the "
    "account, network reachability and connection settings, then try again."
)


def _classify_snowflake_error(exc: Exception) -> str:
    """Turn a raw Snowflake/DuckDB attach failure into one operator-readable
    sentence — never the driver's own text. Coarse on purpose: the one fact
    worth naming specifically is "this is a credential problem" (the common
    case — see #1616), because that is the one that points the operator
    somewhere useful (reconnect with a new credential); everything else gets
    a generic, still-actionable fallback. The full, unredacted exception
    stays server-side — the caller logs it before raising.
    """
    text = str(exc)
    if _SF_AUTH_CODE_RE.search(text) or any(hint in text.lower() for hint in _SF_AUTH_HINTS):
        return _SF_AUTH_MESSAGE
    return _SF_GENERIC_MESSAGE


@router.get("/{source_type}/tables")
async def list_source_tables(
    source_type: str,
    schema: str | None = Query(default=None, description="Narrow the listing to one schema."),
    _user: dict = Depends(require_admin),
):
    """List the schemas + tables the configured Snowflake user can see.

    Powers the "Add data source" wizard's Snowflake table picker, so the
    operator selects real names instead of typing them. Read-only: it attaches,
    reads ``information_schema.tables`` and detaches — no extract is written and
    no registry row is touched (registration stays
    ``POST /api/admin/register-table``).

    REST-only — admin-UI browse helper with no analyst-facing CLI/MCP analogue,
    exactly like its Keboola sibling (see ``_EXEMPT`` in
    ``tests/test_documentation_api_triple_surface.py``). The registration step
    it feeds is already triple-surface.

    400 when ``source_type`` is not browsable, or when Snowflake is not
    configured on this instance, or when the resolved host is outside
    ``AGNES_REMOTE_ATTACH_HOST_ALLOWLIST``. 502 when the driver or the catalog
    query fails — deliberately not an empty listing, which would read as "the
    account has no tables".

    Returns ``{"source_type", "database", "schemas": [{"name", "tables":
    [{"name", "table_type"}, ...]}, ...]}``.
    """
    source_type = (source_type or "").strip().lower()
    if source_type not in _SUPPORTED:
        raise HTTPException(
            status_code=400,
            detail=(
                f"table listing is not supported for source_type={source_type!r} "
                f"(supported: {', '.join(_SUPPORTED)}; Keboola lists per connection at "
                "/api/admin/source-connections/{id}/tables)"
            ),
        )

    from connectors.snowflake.discovery import list_tables

    try:
        # Blocking DuckDB + driver work; the extension install on a cold
        # container makes this slow enough that holding the event loop would
        # stall every other request.
        listing = await run_in_threadpool(list_tables, schema)
    except ValueError as exc:
        # The host-allowlist refusal — an operator misconfiguration, not an
        # upstream fault.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # #1616: the full driver text (exception class, SQLSTATE, Snowflake
        # error code, request id — never a credential; `attach_snowflake`
        # already scrubbed those) is exactly what an operator needs to
        # diagnose this, so it goes to the log at WARNING. The HTTP response
        # gets the classified, one-sentence version instead — see
        # `_classify_snowflake_error`.
        logger.warning("snowflake table listing failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=_classify_snowflake_error(exc),
        ) from exc

    if listing is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Snowflake is not configured on this instance — set "
                "data_source.snowflake.* (account, user, database, warehouse) plus the "
                "password / key-pair secret in /admin/server-config, then browse again"
            ),
        )

    return {"source_type": source_type, **listing}
