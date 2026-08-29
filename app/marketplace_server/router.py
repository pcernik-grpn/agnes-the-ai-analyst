"""FastAPI router for the aggregated marketplace endpoint.

Three GET routes:
  - /marketplace/info                    → JSON summary (diagnostic / admin / UI package list)
  - /marketplace.zip                     → ZIP download with ETag / If-None-Match
  - /marketplace/cowork/{name}.zip       → single plugin repackaged for Cowork upload

Both gated by the existing `get_current_user` dependency (Bearer PAT or cookie).
The git smart-HTTP channel lives in git_router.py and is mounted separately
because it needs raw WSGI I/O that FastAPI doesn't model natively.

Every handler here is a plain ``def``, deliberately: their bodies are pure
blocking work — walking plugin trees, SHA-256 hashing every file, reading
hundreds of MB and running ZIP_DEFLATED — with not a single ``await``. As
``async def`` they ran that work directly on the single uvicorn event loop,
so one large-plugin build froze the whole process (health checks, every
other user, and the download itself when queued behind another build) for
the build's duration — observed as a Cowork package download that hangs on
instances with a big plugin. Plain ``def`` makes FastAPI run them in the
anyio thread pool (PR #188's event-loop offload convention; pinned by
``tests/test_event_loop_offload_guard.py``).
"""

from __future__ import annotations

import logging
import re

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.auth.dependencies import _get_db, get_current_user
from app.marketplace_server import cowork_packager, packager
from src import marketplace_filter
from src.audit_helpers import identity_for_audit, log_safe

logger = logging.getLogger(__name__)

router = APIRouter(tags=["marketplace"])


@router.get("/marketplace/info")
def marketplace_info(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> JSONResponse:
    info = packager.build_info(conn, user)
    return JSONResponse(info)


@router.get("/marketplace.zip")
def marketplace_zip(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Response:
    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
    # Resolve the etag first — this lets a 304 short-circuit before we read
    # every plugin file off disk and run ZIP_DEFLATED. Hot path on every
    # Claude Code SessionStart.
    etag, plugins = packager.compute_etag_for_user(conn, user)
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    data, _ = packager.build_zip(conn, user, plugins=plugins, etag=etag)
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="marketplace.bundle_download",
        resource="marketplace.zip",
        params={"plugin_count": len(plugins)},
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{etag}"',
            "Content-Disposition": 'attachment; filename="agnes-marketplace.zip"',
        },
    )


@router.get("/marketplace/cowork/{prefixed_name}.zip")
def cowork_plugin_zip(
    prefixed_name: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> Response:
    """Download a single plugin packaged for Claude Desktop's Cowork upload.

    Cowork expects one plugin per zip, at the zip root (no marketplace.json
    wrapper) and run through a stricter validator than Claude Code's — see
    ``cowork_packager`` for the transforms. RBAC is enforced implicitly:
    ``resolve_user_marketplace`` only returns plugins the caller is granted,
    so an unknown / ungranted ``prefixed_name`` is simply absent → 404.
    """
    plugins = marketplace_filter.resolve_user_marketplace(conn, user)
    match = next((p for p in plugins if p["prefixed_name"] == prefixed_name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="plugin_not_found")

    try:
        data, etag = cowork_packager.get_cowork_zip(match)
    except cowork_packager.CoworkZipError as exc:
        # Plugin can't fit Cowork's file-count / size caps even after the
        # data/ concat — tell the caller rather than serve a zip the upload
        # validator will silently reject.
        raise HTTPException(status_code=422, detail=str(exc))

    if_none_match = request.headers.get("if-none-match", "").strip().strip('"')
    if if_none_match and if_none_match == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    # Filename from the matched (DB-sourced, regex-safe) prefixed_name — never
    # the raw path param — and control chars stripped so it can't inject
    # response headers via Content-Disposition.
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "", match["prefixed_name"]) or "plugin"
    user_id, _email = identity_for_audit(user)
    log_safe(
        user_id=user_id,
        action="marketplace.bundle_download",
        resource=f"marketplace:cowork:{prefixed_name}",
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{etag}"',
            "Content-Disposition": f'attachment; filename="{safe_name}.zip"',
        },
    )
