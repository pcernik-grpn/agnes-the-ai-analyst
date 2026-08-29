"""Upload endpoints — sessions, artifacts, CLAUDE.local.md."""

import logging
import re
import shutil
import tempfile
import uuid
import zlib
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.utils import get_data_dir as _get_data_dir
from app.utils import local_md_filename as _local_md_filename
from app.utils import uploaded_local_md_dir as _uploaded_local_md_dir
from src.audit_helpers import client_kind_from_user, log_safe

from src.repositories import (
    audit_repo,
)

logger = logging.getLogger(__name__)

_FILENAME_RE = re.compile(r"^[A-Za-z0-9._\-]{1,200}$")

router = APIRouter(prefix="/api/upload", tags=["upload"])

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
_CHUNK_SIZE = 64 * 1024  # 64 KB read chunks for streaming size check
_MAX_DECOMP_STEP = 1024 * 1024  # cap bytes produced per decompress() call (bounds peak memory)


async def _stream_to_temp(file: UploadFile) -> tuple[tempfile.NamedTemporaryFile, int]:
    """Stream-upload with cumulative size check. Returns (tempfile, size).

    Aborts once total > MAX_UPLOAD_SIZE — avoids buffering the entire
    body in memory before the size cap rejects it (OOM prevention).
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    total = 0
    try:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_SIZE:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
                )
            tmp.write(chunk)
        tmp.flush()
    except HTTPException:
        raise
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.seek(0)
    return tmp, total


async def _stream_to_temp_gunzip(file: UploadFile) -> tuple[tempfile.NamedTemporaryFile, int]:
    """Stream-decompress a gzip upload with the size cap on DECOMPRESSED bytes.

    Zip-bomb guard: `MAX_UPLOAD_SIZE` binds on the decompressor's output, not
    the transfer size — a few KB on the wire must not expand into gigabytes
    on disk. The raw-transfer counter stays as a second bound. Corrupt or
    truncated streams (zlib error, or EOF before the gzip trailer) are a 400
    `invalid_gzip`: deterministic, so the client files them as permanent
    failures instead of retrying.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tmp")
    decomp = zlib.decompressobj(wbits=31)  # 31 = gzip container
    total = 0  # decompressed bytes (the capped quantity)
    raw_total = 0  # compressed transfer bytes (secondary bound)
    try:
        while True:
            chunk = await file.read(_CHUNK_SIZE)
            if not chunk:
                break
            raw_total += len(chunk)
            if raw_total > MAX_UPLOAD_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
                )
            buf = chunk
            while buf:
                try:
                    out = decomp.decompress(buf, _MAX_DECOMP_STEP)
                except zlib.error:
                    raise HTTPException(status_code=400, detail="invalid_gzip")
                total += len(out)
                if total > MAX_UPLOAD_SIZE:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Decompressed content too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
                    )
                tmp.write(out)
                buf = decomp.unconsumed_tail
        try:
            out = decomp.flush()
        except zlib.error:
            raise HTTPException(status_code=400, detail="invalid_gzip")
        total += len(out)
        if total > MAX_UPLOAD_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"Decompressed content too large (max {MAX_UPLOAD_SIZE // 1024 // 1024}MB)",
            )
        tmp.write(out)
        if not decomp.eof:
            # Stream ended before the gzip trailer — truncated upload.
            raise HTTPException(status_code=400, detail="invalid_gzip")
        tmp.flush()
    except BaseException:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.seek(0)
    return tmp, total


@router.post("/sessions")
async def upload_session(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload a Claude session transcript (JSONL)."""
    user_id = user["id"]

    if not _FILENAME_RE.match(file.filename or ""):
        raise HTTPException(
            status_code=400,
            detail="filename must match [A-Za-z0-9._-]{1,200}",
        )

    # A `.gz` suffix means the body is gzip-compressed (client capability
    # `session-gzip`). The stored name strips the suffix so the on-disk
    # corpus stays plain JSONL for every downstream reader.
    filename = file.filename  # already validated by regex above
    is_gzip = filename.endswith(".gz")
    if is_gzip:
        filename = filename[: -len(".gz")]
        if not _FILENAME_RE.match(filename):
            raise HTTPException(
                status_code=400,
                detail="filename must match [A-Za-z0-9._-]{1,200} before .gz",
            )

    sessions_dir = _get_data_dir() / "user_sessions" / user_id
    sessions_dir.mkdir(parents=True, exist_ok=True)
    target = sessions_dir / filename

    if is_gzip:
        tmp, size = await _stream_to_temp_gunzip(file)
    else:
        tmp, size = await _stream_to_temp(file)
    try:
        tmp.close()
        shutil.move(tmp.name, str(target))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise

    # audit_repo() is factory-routed (honors use_pg()) and opens its own
    # backend connection — no system-DB handle needed here, and opening one on
    # a Postgres instance is forbidden (would create a stale system.duckdb).
    try:
        audit_repo().log(
            user_id=user_id,
            action="session.upload",
            params={"filename": filename[:256], "bytes": size},
            result="success",
            client_kind=client_kind_from_user(user),
        )
    except Exception:
        logger.exception("audit_log write failed for session.upload; continuing")

    return {"status": "ok", "filename": filename, "size": size}


@router.post("/artifacts")
async def upload_artifact(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Upload an artifact (HTML report, PNG chart, etc.)."""
    user_id = user["id"]
    artifacts_dir = _get_data_dir() / "user_artifacts" / user_id
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    raw_name = file.filename or f"artifact_{uuid.uuid4().hex[:8]}"
    filename = Path(raw_name).name  # Strips directory traversal components
    if not filename or filename.startswith("."):
        filename = f"upload_{uuid.uuid4().hex[:8]}"
    target = artifacts_dir / filename

    tmp, size = await _stream_to_temp(file)
    try:
        tmp.close()
        shutil.move(tmp.name, str(target))
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise
    log_safe(
        user_id=user_id,
        action="artifact.upload",
        params={"filename": filename[:256], "bytes": size},
        result="success",
        client_kind=client_kind_from_user(user),
    )
    return {"status": "ok", "filename": filename, "size": size}


class LocalMdRequest(BaseModel):
    content: str


@router.post("/local-md")
async def upload_local_md(
    request: LocalMdRequest,
    user: dict = Depends(get_current_user),
):
    """Upload CLAUDE.local.md content for corporate memory processing."""
    user_email = user["email"]
    md_dir = _uploaded_local_md_dir()
    md_dir.mkdir(parents=True, exist_ok=True)

    # Hashed filename — stable per user, no charset surprises from email.
    # Derived via the shared helper so the corporate-memory collector, which
    # reads these files back in another process, cannot drift from the name
    # (or the directory) written here.
    target = md_dir / _local_md_filename(user_email)
    target.write_text(request.content, encoding="utf-8")
    # NEVER the content — only its byte length enters the audit record.
    log_safe(
        user_id=user.get("id"),
        action="local_md.upload",
        params={"bytes": len(request.content)},
        result="success",
        client_kind=client_kind_from_user(user),
    )
    return {
        "status": "ok",
        "user": user_email,
        "size": len(request.content),
    }
