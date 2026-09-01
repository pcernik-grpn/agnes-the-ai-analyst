"""Admin endpoints for per-user session files, plus one unrelated "session"
concern that happens to share the noun and the URL family.

Endpoints:
- GET  /api/admin/users/{user_id}/sessions            — paginated session list
- GET  /api/admin/users/{user_id}/sessions/download-all — bulk ZIP download
- GET  /api/admin/users/{user_id}/sessions/{session_file:path}/download — single JSONL
- POST /api/admin/users/{user_id}/revoke-sessions       — end the user's live
  AUTH sessions (issue #1676 remainder) — NOT the JSONL transcript files
  above. See :func:`revoke_user_sessions`'s docstring for the distinction.

All admin-gated. Both download endpoints and revoke-sessions write audit_log
rows.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.audit_helpers import log_safe

from src.repositories import (
    audit_repo,
    usage_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SESSION_FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.jsonl$")


def _session_data_dir() -> Path:
    return Path(os.environ.get("SESSION_DATA_DIR", "/data/user_sessions"))


def _resolve_user(user_id: str, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    repo = users_repo()
    target = repo.get_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return target


def _username_from_user(user: dict[str, Any]) -> str:
    """Derive a filesystem username from the users row.

    The session collector places files under the OS username of the agent
    process, which for most deployments is the email local-part.  The
    `users` row stores the e-mail; we use the local-part (before '@') as
    the best available approximation.  If the server was configured with a
    different SESSION_DATA_DIR layout, operators can subclass / monkey-patch
    this helper — it is the single mapping point.
    """
    email: str = user.get("email", "") or ""
    return email.split("@")[0] if "@" in email else email


def _user_session_dirs(user_id: str, username: str) -> list[Path]:
    """Return the session directories to scan for a user, in priority order.

    Two ingestion paths write under ``SESSION_DATA_DIR`` with DIFFERENT
    directory names:

    * the legacy session collector uses the OS username (email local-part),
    * the upload API (``/api/upload/sessions``) uses ``users.id``.

    Scanning only the username dir made every API-uploaded session
    invisible to the admin list/download endpoints until the usage
    processor indexed it — and the single-file download 404'd on them
    forever. Both dirs are scanned; the username dir wins filename
    collisions (it existed first).

    Empty components are dropped: a user without an email yields an
    empty username, and ``base / ""`` is ``base`` itself — scanning the
    whole SESSION_DATA_DIR root for that user.
    """
    base = _session_data_dir()
    dirs = [base / name for name in dict.fromkeys([username, user_id]) if name]
    return dirs


# ---------------------------------------------------------------------------
# GET /api/admin/users/{user_id}/sessions
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/sessions")
def list_user_sessions(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return a paginated session list for *user_id*.

    Each row joins ``usage_session_summary`` (preferred, ``processed=true``)
    with a filesystem scan of ``${SESSION_DATA_DIR}/<username>/*.jsonl`` so
    the response surfaces sessions even when the UsageProcessor hasn't run yet
    (``processed=false`` for those rows).

    ``processed=false`` rows carry only: ``session_file``, ``session_id``
    (extracted from the filename when possible), ``started_at`` (file mtime),
    and zeroed-out counters.
    """
    target = _resolve_user(user_id, conn)
    username = _username_from_user(target)
    user_dirs = _user_session_dirs(user_id, username)

    # ------------------------------------------------------------------
    # Pull processed rows from usage_session_summary
    # ------------------------------------------------------------------
    # Match on both user_id (stable, v45+) and username (legacy) so the
    # admin view shows sessions from both ingestion paths and pre-v45 rows.
    try:
        rows_db = usage_repo().list_sessions_for_user_admin(user_id=user_id, username=username)
    except Exception:
        rows_db = []

    processed_files: dict[str, dict] = {}
    if rows_db:
        for d in rows_db:
            # Normalise timestamps to ISO strings
            for k in ("started_at", "ended_at"):
                v = d.get(k)
                if v is not None and hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
            d["processed"] = True
            # Key by BASENAME: the session pipeline writes session_file as
            # "<dir>/<filename>" (runner's session_key), while the
            # filesystem merge below compares bare filenames — keying the
            # raw value would make every prefixed row miss the dedup check
            # and list the same session twice (processed + "unprocessed").
            processed_files[d["session_file"].rsplit("/", 1)[-1]] = d

    # ------------------------------------------------------------------
    # Merge with filesystem scan — unindexed files become processed=false
    # ------------------------------------------------------------------
    all_rows: list[dict] = list(processed_files.values())

    seen_fs: set[str] = set()
    for user_dir in user_dirs:
        if not user_dir.is_dir():
            continue
        for p in sorted(user_dir.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            fname = p.name
            # Relative key used as session_file value (matches what the
            # processor writes: "<username>/<filename>" or just "<filename>").
            # We normalise to basename-only to avoid path-separator surprises.
            # Filename collisions across the two ingestion dirs collapse to
            # the first-listed dir (username) — same file, two layouts.
            if fname not in processed_files and fname not in seen_fs:
                seen_fs.add(fname)
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                # Try to extract a session_id from the filename: the collector
                # names files like "<session_id>.jsonl" or "sess-<id>.jsonl".
                sid = p.stem
                all_rows.append(
                    {
                        "session_file": fname,
                        "session_id": sid,
                        "started_at": mtime.isoformat(),
                        "ended_at": None,
                        "active_seconds": None,
                        "wall_seconds": None,
                        "tool_calls": 0,
                        "tool_errors": 0,
                        "primary_model": None,
                        "processed": False,
                    }
                )

    # Sort: processed (have started_at) first then unprocessed, both newest-first
    def _sort_key(r: dict):
        ts = r.get("started_at") or ""
        return (1 if r["processed"] else 0, "" if not ts else ts)

    all_rows.sort(key=_sort_key, reverse=True)

    total = len(all_rows)
    page = all_rows[offset : offset + limit]

    return {
        "rows": page,
        "pagination": {"limit": limit, "offset": offset, "total": total},
    }


# ---------------------------------------------------------------------------
# GET /api/admin/users/{user_id}/sessions/download-all
# NOTE: this route MUST be declared BEFORE the /{session_file:path}/download
# route so FastAPI matches it first (exact segment wins over :path capture).
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/sessions/download-all")
def download_all_sessions(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream a ZIP of every *.jsonl under the user's session directories
    (both ingestion layouts — collector username dir + upload-API user_id
    dir).

    Returns 404 when neither directory exists.
    Returns 200 + empty ZIP when a directory exists but has no JSONL files.
    """
    target = _resolve_user(user_id, conn)
    username = _username_from_user(target)
    user_dirs = [d for d in _user_session_dirs(user_id, username) if d.is_dir()]

    if not user_dirs:
        raise HTTPException(status_code=404, detail="No session directory for this user")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    zip_filename = f"{username}-sessions-{today}.zip"

    total_bytes = 0
    file_count = 0

    # We need total_bytes and file_count for the audit row, but we also need
    # to stream.  For session files (typically < a few MB each) we build the
    # ZIP in memory first so we can measure the totals, then yield it.
    # If the corpus grows into GB territory, revisit with SpooledTemporaryFile.
    seen_names: set[str] = set()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for user_dir in user_dirs:
            user_dir_resolved = user_dir.resolve()
            for p in sorted(user_dir.glob("*.jsonl")):
                # Same filename in both layouts = same session; first dir wins.
                if p.name in seen_names:
                    continue
                # Guard against symlinks pointing outside the user's session directory.
                try:
                    p.resolve().relative_to(user_dir_resolved)
                except ValueError:
                    logger.warning(
                        "download_all_sessions: skipping symlink escape: %s -> %s",
                        p,
                        p.resolve(),
                    )
                    continue
                data = p.read_bytes()
                zf.writestr(p.name, data)
                seen_names.add(p.name)
                total_bytes += len(data)
                file_count += 1
    zip_bytes = buf.getvalue()

    audit_repo().log(
        user_id=user.get("id"),
        action="session_bulk_download",
        resource=f"users/{user_id}/sessions",
        params={"file_count": file_count, "total_bytes": total_bytes, "username": username},
    )

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_filename}"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


# ---------------------------------------------------------------------------
# GET /api/admin/users/{user_id}/sessions/{session_file:path}/download
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/sessions/{session_file:path}/download")
def download_session(
    user_id: str,
    session_file: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Stream the raw JSONL for a single session.

    Path-traversal is guarded by three layers:
    1. ``safe_name = Path(session_file).name``  — strips any ``../`` etc.
    2. The name must match ``^[A-Za-z0-9._-]+\\.jsonl$``.
    3. ``path.resolve()`` must still be under the session directory.
    """
    # --- guard 1: basename extraction
    safe_name = Path(session_file).name
    if safe_name != session_file:
        raise HTTPException(
            status_code=400,
            detail="session_file must be a plain basename (no path separators)",
        )

    # --- guard 2: character allowlist
    if not _SESSION_FILE_RE.match(safe_name):
        raise HTTPException(
            status_code=400,
            detail="session_file must match ^[A-Za-z0-9._-]+\\.jsonl$",
        )

    target = _resolve_user(user_id, conn)
    username = _username_from_user(target)
    # Both ingestion layouts (collector username dir + upload-API user_id
    # dir); first match wins.
    path = None
    user_dir = None
    for candidate_dir in _user_session_dirs(user_id, username):
        candidate = candidate_dir / safe_name
        if candidate.exists():
            path = candidate
            user_dir = candidate_dir
            break

    if path is None:
        raise HTTPException(status_code=404, detail="Session file not found")

    # --- guard 3: resolved path still within session dir
    try:
        resolved = path.resolve()
        base_resolved = user_dir.resolve()
        resolved.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Resolved path escapes session directory")

    size = path.stat().st_size

    audit_repo().log(
        user_id=user.get("id"),
        action="session_download",
        resource=f"users/{user_id}/sessions/{safe_name}",
        params={"bytes": size, "session_file": safe_name, "username": username},
    )

    def _iter_file():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _iter_file(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Length": str(size),
        },
    )


# ---------------------------------------------------------------------------
# GET /api/admin/users/{user_id}/activity
# ---------------------------------------------------------------------------


@router.get("/users/{user_id}/activity")
def list_user_activity(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List audit_log rows for a specific user.

    Resolves user_id to the user record (404 if not found), filters audit_log
    on the user_id field, returns paginated rows newest first.
    """

    _resolve_user(user_id, conn)

    audit = audit_repo()
    rows, _ = audit.query(user_id=user_id, limit=limit + offset)
    # Apply offset via slicing — cursor-based pagination is per-page only
    rows = rows[offset : offset + limit]

    # Normalise timestamps to ISO strings and decode JSON params
    for r in rows:
        for k in ("timestamp",):
            v = r.get(k)
            if v is not None and hasattr(v, "isoformat"):
                r[k] = v.isoformat()
        params_val = r.get("params")
        if isinstance(params_val, str):
            try:
                r["params"] = json.loads(params_val) if params_val else None
            except (ValueError, TypeError):
                pass

    total = audit_repo().count_for_user(user_id)

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="admin.user_activity_read",
            resource=f"users/{user_id}/activity"[:256],
            params={"target_user_id": user_id, "limit": limit, "offset": offset, "row_count": len(rows)},
            result="success",
            client_kind="web",
        )
    except Exception:
        logger.exception("audit_log write failed for admin.user_activity_read; continuing")

    return {
        "rows": rows,
        "pagination": {"limit": limit, "offset": offset, "total": int(total)},
    }


# ---------------------------------------------------------------------------
# POST /api/admin/users/{user_id}/revoke-sessions
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/revoke-sessions")
def revoke_user_sessions(
    user_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Force-end *user_id*'s live browser/API sessions, without deactivating
    the account (issue #1676 remainder).

    Unrelated to the session-FILE endpoints above this one in the same
    module — this bumps ``users.session_revoked_before`` (PG-only column,
    A3 ratchet), the same floor ``POST /auth/logout`` and a self-serve
    password change/reset now bump too (``app/auth/providers/password.py``).
    Every ``typ="session"`` JWT for this user whose ``iat`` predates the new
    floor is refused on its next use by
    ``app.auth.pat_resolver.resolve_token_to_user``. PATs are untouched —
    they run their own ``personal_access_tokens.revoked_at`` chain; use
    ``DELETE /auth/admin/tokens/{token_id}`` for those.

    On a DuckDB-backed instance ``revoke_sessions`` is a documented no-op
    (no such column exists there — frozen post-A3 schema), so this still
    answers 200 rather than a raw failure, but the body says honestly
    whether anything actually changed instead of claiming a revocation
    that did not happen.
    """
    target = _resolve_user(user_id, conn)

    from src.repositories import use_pg

    users_repo().revoke_sessions(user_id)
    backend_supports_revocation = use_pg()

    log_safe(
        user_id=user.get("id"),
        action="user.revoke_sessions",
        resource=f"users/{user_id}",
        params={"target_email": target.get("email"), "backend_supports_revocation": backend_supports_revocation},
    )

    return {
        "user_id": user_id,
        "revoked": backend_supports_revocation,
        "backend_supports_revocation": backend_supports_revocation,
    }
