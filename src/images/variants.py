"""Generate and cache resized WebP variants of cover images.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: src/images/variants.py
Deps:   Pillow (PIL)
Tested: tests/test_cover_image_perf_contract.py

Key responsibilities:
- Resize + re-encode a cover image to one of a fixed set of widths, once,
  caching the WebP result on disk keyed by the source's path + mtime + size.
- Cache a negative verdict too (a ``.skip`` sentinel) so a source that can
  never produce a variant isn't re-decoded on every request forever.
- Evict a source's own stale variants/sentinels when it's re-uploaded at the
  same path, so the cache doesn't grow unbounded across re-uploads.

Design constraints:
- The requested width is caller-controlled (it arrives on a public query
  string), so it is restricted to a hardcoded allowlist — anything else
  would let a caller fill the disk with arbitrary-width variants or burn
  CPU resizing huge images on demand. The cache key's hash input is the
  source's own path + stat, never caller-controlled, so a cache hit can't
  be spoofed into serving a different source's bytes.
- Never raises to the caller: any decode/encode failure is logged and
  answered with None so the caller falls back to serving the original file.
- The decompression-bomb guard is an explicit pixel-count check on the
  already-open (header-only, no full decode) image against
  ``Image.MAX_IMAGE_PIXELS`` — not a process-global ``warnings.simplefilter``
  that would silently mutate every other Pillow caller in the process too.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Final

from PIL import Image, ImageOps, UnidentifiedImageError

from src.db import _get_data_dir

logger = logging.getLogger(__name__)

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


def _digest(text: str) -> str:
    """SHA256 hex digest of ``text`` -- used for cache-key components."""
    return hashlib.sha256(text.encode()).hexdigest()


def _cache_target(src: Path, width: int, st: os.stat_result) -> tuple[Path, str]:
    """Cache path for ``src`` at ``width``, keyed by path + mtime + size.

    Split into a path-digest and a stat-digest component (rather than one
    combined hash) so a later write can find and evict siblings that share
    the path digest but carry a stale stat digest -- the same source
    re-uploaded at the same URL, whose old variant would otherwise be
    orphaned on disk forever.
    """
    path_digest = _digest(str(src.resolve()))
    stat_digest = _digest(f"{st.st_mtime_ns}|{st.st_size}")
    target = _get_data_dir() / CACHE_SUBDIR / f"{path_digest}-{stat_digest}-w{width}.webp"
    return target, path_digest


def _evict_stale(cache_dir: Path, path_digest: str, width: int, keep: str) -> None:
    """Delete sibling variants/sentinels for the same source path + width
    whose stat-digest component is no longer current."""
    for suffix in ("webp", "skip"):
        for stale in cache_dir.glob(f"{path_digest}-*-w{width}.{suffix}"):
            if stale.name == keep:
                continue
            try:
                stale.unlink()
            except OSError as exc:
                logger.warning("cover variant cleanup failed for %s: %s", stale, exc, exc_info=True)


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
        the original instead — bad width, oversized source, a decompression
        bomb, animated source, a source already narrower than ``width``
        (never upscale), or a decode/encode failure.
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

    target, path_digest = _cache_target(src, width, st)
    if target.exists():
        return target
    sentinel = target.with_suffix(".skip")
    if sentinel.exists():
        return None

    def _decline() -> None:
        """Record that this exact source (by digest) can never produce a
        variant at this width, so future requests skip straight past the
        decode -- templates unconditionally ask for ?w=480 on every page
        load. A source change (new mtime/size) changes the digest, so the
        sentinel self-invalidates without any explicit cleanup on write.

        Never raises: a sentinel write failure (disk full, permissions) is
        logged and swallowed, same as :func:`_evict_stale`'s own unlink --
        this is called again, unguarded, from the ``except`` block below, so
        it must not be a second place an OSError can escape to the caller.
        """
        try:
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
            _evict_stale(sentinel.parent, path_digest, width, keep=sentinel.name)
        except OSError as exc:
            logger.warning("cover variant sentinel write failed for %s: %s", sentinel, exc, exc_info=True)

    tmp = target.with_name(f"{target.stem}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        with Image.open(src) as im:
            if Image.MAX_IMAGE_PIXELS is not None and im.width * im.height > Image.MAX_IMAGE_PIXELS:
                logger.warning(
                    "cover variant declined for %s: %dx%d exceeds MAX_IMAGE_PIXELS",
                    src,
                    im.width,
                    im.height,
                )
                _decline()
                return None
            if getattr(im, "is_animated", False):
                _decline()
                return None
            # No im.draft() here: draft() decodes at a coarser JPEG DCT scale
            # for speed, *before* orientation correction, so a source whose
            # raw (pre-rotation) width happened to equal an allowed width
            # could draft down below it and then wrongly hit the no-upscale
            # bail below, permanently serving the original. This whole path
            # only runs once per source+width (cached after), so the decode
            # speedup isn't worth that correctness gap.
            im = ImageOps.exif_transpose(im) or im
            if im.width <= width:
                _decline()
                return None
            im = im.convert(_target_mode(im))
            height = max(1, round(im.height * width / im.width))
            im = im.resize((width, height), Image.Resampling.LANCZOS, reducing_gap=3.0)
            target.parent.mkdir(parents=True, exist_ok=True)
            im.save(tmp, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        os.replace(tmp, target)
    except OSError as exc:
        # Transient I/O (disk pressure, a read hitting a mid-replace file)
        # must not pin a permanent .skip sentinel for this source revision;
        # the next request is free to retry the decode.
        logger.warning("cover variant failed for %s: %s", src, exc, exc_info=True)
        tmp.unlink(missing_ok=True)
        return None
    except (
        ValueError,  # Pillow: corrupt/truncated data in several codecs
        SyntaxError,  # Pillow: malformed container (e.g. PNG chunk errors)
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        # Structural: this source revision can never produce a variant, so
        # the negative verdict is safe to cache.
        logger.warning("cover variant failed for %s: %s", src, exc, exc_info=True)
        tmp.unlink(missing_ok=True)
        _decline()
        return None
    _evict_stale(target.parent, path_digest, width, keep=target.name)
    return target
