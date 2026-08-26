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

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from app.auth.access import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/data-sources", tags=["admin"])

# Source types whose catalog this endpoint can browse. Keboola is absent on
# purpose: its listing is per-connection and already served by
# `/api/admin/source-connections/{id}/tables`.
_SUPPORTED = ("snowflake",)


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
        logger.warning("snowflake table listing failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"could not list Snowflake tables: {exc}",
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
