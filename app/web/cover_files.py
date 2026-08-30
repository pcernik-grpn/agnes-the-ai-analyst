"""Serve uploaded cover images with immutable cache headers and ?w= variants.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: app/web/cover_files.py
Deps:   starlette, fastapi, src.images.variants
Tested: tests/test_cover_files.py, tests/test_cover_image_perf_contract.py

Key responsibilities:
- Override Starlette StaticFiles to inject Cache-Control headers for covers.
- Serve a resized WebP variant when the request carries ``?w=480`` or
  ``?w=960``; any other value (or none) serves the original, unchanged.

Design constraints:
- Filenames are content-addressed (SHA256 of bytes), so immutable is safe.
- 200 and 304 responses get the cache header; other statuses pass through.
- The width branch re-derives containment from ``lookup_path`` even though
  Starlette's own lookup already enforces it — defense-in-depth, matching
  the pattern in src/marketplace_asset_mirror.py's ``_write_body``.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs

from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from src.images.variants import ALLOWED_WIDTHS, coerce_width, variant_path

COVER_CACHE_CONTROL: Final = "public, max-age=2592000, immutable"


def _parse_width(query_string: bytes) -> int | None:
    """Extract an integer ``w`` from a raw ASGI query string, or None."""
    values = parse_qs(query_string.decode("latin-1")).get("w")
    return coerce_width(values[0]) if values else None


class CoverFiles(StaticFiles):
    """Serve cover images with 30-day immutable cache header.

    Content-addressed files (SHA256 in the filename) can be cached
    indefinitely because a changed image is a new URL. This suppresses
    conditional GETs on every page load.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Fetch the file, adding the cache header to 200/304 responses.

        X-Content-Type-Options is already stamped app-wide by the security
        headers middleware, so only Cache-Control is set here.
        """
        width = _parse_width(scope.get("query_string", b""))
        if width in ALLOWED_WIDTHS:
            full_path, stat_result = await run_in_threadpool(self.lookup_path, path)
            if stat_result is not None and stat.S_ISREG(stat_result.st_mode) and self.directory is not None:
                try:
                    Path(full_path).resolve().relative_to(Path(self.directory).resolve())
                except ValueError:
                    pass
                else:
                    variant = await run_in_threadpool(variant_path, Path(full_path), width)
                    if variant is not None:
                        response = FileResponse(variant, media_type="image/webp")
                        response.headers["cache-control"] = COVER_CACHE_CONTROL
                        return response

        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers["cache-control"] = COVER_CACHE_CONTROL
        return response
