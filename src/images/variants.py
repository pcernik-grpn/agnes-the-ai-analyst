"""Generate and cache resized WebP variants of cover images.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: src/images/variants.py
Deps:   Pillow (PIL)
Tested: tests/test_cover_image_perf_contract.py

Key responsibilities:
- Resize + re-encode a cover image to one of a fixed set of widths, once,
  caching the WebP result on disk keyed by the source's path + mtime + size.

Design constraints:
- The requested width is caller-controlled (it arrives on a public query
  string), so it is restricted to a hardcoded allowlist — anything else
  would let a caller fill the disk with arbitrary-width variants or burn
  CPU resizing huge images on demand. The cache key's hash input is the
  source's own path + stat, never caller-controlled, so a cache hit can't
  be spoofed into serving a different source's bytes.
- Never raises to the caller: any decode/encode failure is logged and
  answered with None so the caller falls back to serving the original file.
- The DecompressionBombWarning-as-error filter set at import time is
  process-global (``warnings.simplefilter``), not scoped to this module —
  any other Pillow ``Image.open()`` elsewhere in the process is affected
  too once this module has been imported.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import warnings
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from src.db import _get_data_dir

logger = logging.getLogger(__name__)

# A crafted image whose declared dimensions decode to a huge pixel count
# would otherwise just warn and proceed; treat that as fatal like any other
# decode failure.
warnings.simplefilter("error", Image.DecompressionBombWarning)

ALLOWED_WIDTHS: Final = (480, 960)
WEBP_QUALITY: Final = 80
WEBP_METHOD: Final = 4
MAX_SOURCE_BYTES: Final = 5 * 1024 * 1024
CACHE_SUBDIR: Final = "cache/img"


def coerce_width(raw: str | None) -> int | None:
    """Parse a caller-supplied ``w`` query value into an int, or ``None``.

    Never raises: missing, empty, or non-integer input all coerce to
    ``None`` so the caller falls back to serving the original file instead
    of a 4xx — the width allowlist (:data:`ALLOWED_WIDTHS`) is a separate
    check the caller applies to the result.
    """
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _cache_path(src: Path, width: int, st: os.stat_result) -> Path:
    """Cache path for ``src`` at ``width``, keyed by path + mtime + size."""
    key = f"{src.resolve()}|{st.st_mtime_ns}|{st.st_size}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return _get_data_dir() / CACHE_SUBDIR / f"{digest}-w{width}.webp"


def _target_mode(im: Image.Image) -> str:
    """RGBA when the source carries alpha (directly or via a P palette's
    transparency entry), else RGB."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        return "RGBA"
    return "RGB"


def variant_path(src: Path, width: int) -> Path | None:
    """Return the cached WebP variant of ``src`` at ``width``, generating it
    on first call.

    Args:
        src: Path to the source image on disk.
        width: Target width in pixels; must be one of :data:`ALLOWED_WIDTHS`.

    Returns:
        Path to the cached WebP file, or ``None`` if the caller should serve
        the original instead — bad width, oversized source, animated source,
        a source already narrower than ``width`` (never upscale), or a
        decode/encode failure.
    """
    if width not in ALLOWED_WIDTHS:
        return None
    try:
        st = src.stat()
    except OSError as exc:
        logger.warning("cover variant failed for %s: %s", src, exc, exc_info=True)
        return None
    if st.st_size > MAX_SOURCE_BYTES:
        return None

    target = _cache_path(src, width, st)
    if target.exists():
        return target

    tmp = target.with_name(f"{target.stem}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        with Image.open(src) as im:
            if getattr(im, "is_animated", False):
                return None
            im.draft("RGB", (width, width))
            im = ImageOps.exif_transpose(im) or im
            if im.width <= width:
                return None
            im = im.convert(_target_mode(im))
            height = max(1, round(im.height * width / im.width))
            im = im.resize((width, height), Image.Resampling.LANCZOS, reducing_gap=3.0)
            target.parent.mkdir(parents=True, exist_ok=True)
            im.save(tmp, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        os.replace(tmp, target)
    except (
        OSError,
        ValueError,  # Pillow: corrupt/truncated data in several codecs
        SyntaxError,  # Pillow: malformed container (e.g. PNG chunk errors)
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        logger.warning("cover variant failed for %s: %s", src, exc, exc_info=True)
        tmp.unlink(missing_ok=True)
        return None
    return target
