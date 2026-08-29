"""Serve uploaded cover images with immutable cache headers.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: app/web/cover_files.py
Deps:   starlette
Tested: tests/test_cover_files.py

Key responsibilities:
- Override Starlette StaticFiles to inject Cache-Control headers for covers.

Design constraints:
- Filenames are content-addressed (SHA256 of bytes), so immutable is safe.
- Only 200 responses get the cache header; 304/404/etc. pass through as-is.
- Width variants (?w=) land in a follow-up on this class.
"""

from __future__ import annotations

from typing import Final

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

COVER_CACHE_CONTROL: Final = "public, max-age=2592000, immutable"


class CoverFiles(StaticFiles):
    """Serve cover images with 30-day immutable cache header.

    Content-addressed files (SHA256 in the filename) can be cached
    indefinitely because a changed image is a new URL. This suppresses
    conditional GETs on every page load.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """Fetch the file and add the cache header to 200 responses only.

        X-Content-Type-Options is already stamped app-wide by the security
        headers middleware, so only Cache-Control is set here.
        """
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["cache-control"] = COVER_CACHE_CONTROL
        return response
