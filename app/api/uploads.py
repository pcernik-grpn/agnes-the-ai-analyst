"""Admin file-upload endpoints.

Currently scoped to the cover-image surface used by Data Package and
Memory Domain admin modals (v50). Files are content-addressed
(``<sha256>.<ext>``) under ``${DATA_DIR}/uploads/covers/`` so re-uploading
the same image is idempotent and re-uses any prior write — no orphan
cleanup needed on edit-and-replace.

The file path is exposed through the ``/uploads/`` static mount in
``app/main.py``; the JSON response returns that public URL so the
calling admin modal can stash it on the resource via the regular PUT
endpoint (``/api/admin/data-packages/{id}`` or
``/api/admin/memory-domains/{id}``).

On a fresh write, both resize variants are pre-generated (off-thread) so
the first visitor never pays the in-request Pillow decode that
app/web/cover_files.py's ``?w=`` path would otherwise do on demand.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Tuple

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.auth.access import require_admin
from src.db import _get_data_dir
from src.images.variants import ALLOWED_WIDTHS, variant_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/uploads", tags=["admin-uploads"])


# Allow-list of image content-types accepted by the cover upload. Kept
# narrow on purpose — admins should be putting product imagery here, not
# SVGs (XSS surface) or animated WebP that could be confused for a media
# CDN. PNG + JPEG + GIF + WebP covers every modern stock-photo source.
_ALLOWED_IMAGE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# 5 MiB ceiling — comfortably fits a 2x-retina hero image; rejects users
# trying to upload original-resolution camera dumps.
_MAX_COVER_BYTES = 5 * 1024 * 1024


def _covers_dir() -> Path:
    """Resolve (and lazily create) the on-disk covers directory."""
    p = _get_data_dir() / "uploads" / "covers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validate_and_extension(file: UploadFile) -> Tuple[str, str]:
    """Return ``(content_type, extension)`` or raise ``HTTPException``.

    Trusts the multipart ``content_type`` for the allow-list check — the
    streaming-read in the handler guards against oversize uploads
    regardless, so a spoofed content-type only buys the attacker an
    extension swap, not a sandbox escape.
    """
    ctype = (file.content_type or "").lower()
    ext = _ALLOWED_IMAGE_TYPES.get(ctype)
    if not ext:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported_image_type:{ctype or 'unknown'}",
        )
    return ctype, ext


@router.post("/cover-image")
async def upload_cover_image(
    file: UploadFile = File(...),
    user: dict = Depends(require_admin),
):
    """Persist an admin-uploaded cover image and return its public URL.

    Content-addresses the file under ``${DATA_DIR}/uploads/covers/<sha>.<ext>``
    so duplicate uploads collapse to one write. The response shape
    ``{url, content_type, size}`` is what the admin modals stash on
    Save into ``data_packages.cover_image_url`` /
    ``memory_domains.cover_image_url``.
    """
    _, ext = _validate_and_extension(file)

    # Stream-read into memory while enforcing the size cap. UploadFile's
    # ``read()`` without an argument would happily slurp arbitrarily large
    # payloads; reading in chunks lets us hard-stop the moment we cross
    # the ceiling and return 413 instead of OOMing the worker.
    sha = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    chunk_size = 64 * 1024
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_COVER_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file_too_large:max_{_MAX_COVER_BYTES}_bytes",
            )
        sha.update(chunk)
        chunks.append(chunk)

    if total == 0:
        raise HTTPException(status_code=400, detail="empty_upload")

    digest = sha.hexdigest()
    target = _covers_dir() / f"{digest}{ext}"
    if not target.exists():
        # Atomic enough for the single-writer admin use-case: write to a
        # temp sibling, rename. Avoids torn files if the worker crashes
        # mid-write while a parallel reader is hitting /uploads.
        tmp = target.with_suffix(target.suffix + ".part")
        with tmp.open("wb") as fh:
            for c in chunks:
                fh.write(c)
        tmp.replace(target)

        # Pre-warm both variants now so the first GET never pays the decode.
        # variant_path() never raises, but this is belt-and-suspenders: the
        # upload response must never depend on the cache write succeeding.
        for width in ALLOWED_WIDTHS:
            try:
                await run_in_threadpool(variant_path, target, width)
            except Exception:
                logger.warning("cover variant prewarm failed for %s w=%d", target, width, exc_info=True)

    public_url = f"/uploads/covers/{digest}{ext}"
    logger.info(
        "admin upload: cover-image %s bytes=%d sha=%s by=%s",
        ext, total, digest[:12], user.get("email") or user.get("id"),
    )
    return {
        "url": public_url,
        "content_type": file.content_type,
        "size": total,
    }
