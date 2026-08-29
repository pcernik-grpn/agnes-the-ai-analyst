"""Uploaded cover images are served with immutable cache headers.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: tests/test_cover_files.py
Deps:   Pillow (PIL), fastapi.testclient
Tested: covers the app/web/cover_files.py CoverFiles class

Key responsibilities:
- Verify 200 responses carry Cache-Control: immutable.
- Verify 404 and other errors do not carry the immutable cache header.
- Verify path containment (no directory traversal).

Design constraints:
- Use seeded_app_fresh because the upload test must read from the disk-mounted
  /uploads/ directory, which StaticFiles points to at app construction time.
"""

from __future__ import annotations

import io

from PIL import Image


def _auth(token: str) -> dict:
    """Build an Authorization header from a Bearer token."""
    return {"Authorization": f"Bearer {token}"}


def test_cover_image_200_carries_immutable_cache_header(seeded_app_fresh):
    """A successful cover-image GET must carry Cache-Control: immutable."""
    app_data = seeded_app_fresh
    client = app_data["client"]

    # Generate a minimal test PNG (64×32, red background).
    img = Image.new("RGB", (64, 32), color="red")
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)

    # Upload via the admin endpoint.
    files = {"file": ("test.png", img_bytes, "image/png")}
    admin_token = app_data["admin_token"]
    upload_resp = client.post(
        "/api/admin/uploads/cover-image",
        files=files,
        headers=_auth(admin_token),
    )
    assert upload_resp.status_code == 200
    upload_json = upload_resp.json()
    assert "url" in upload_json
    cover_url = upload_json["url"]

    # GET the uploaded cover and verify headers.
    cover_resp = client.get(cover_url)
    assert cover_resp.status_code == 200
    assert cover_resp.headers.get("content-type") == "image/png"
    assert "immutable" in cover_resp.headers.get("cache-control", "").lower()
    assert "max-age=2592000" in cover_resp.headers.get("cache-control", "")


def test_cover_image_404_does_not_carry_immutable(seeded_app_fresh):
    """A 404 on a missing cover must not carry Cache-Control: immutable."""
    client = seeded_app_fresh["client"]
    resp = client.get("/uploads/covers/does-not-exist.png")
    assert resp.status_code == 404
    # StaticFiles raises for a missing file before the override runs, so the
    # 404 carries no cache policy at all.
    assert "cache-control" not in resp.headers


def test_cover_image_path_containment(seeded_app_fresh):
    """Directory traversal attempts (e.g. ../../etc/passwd) are contained."""
    client = seeded_app_fresh["client"]
    # Try to escape the covers directory.
    # httpx collapses a literal "../" client-side (the request would arrive as
    # GET /etc/passwd and never reach the mount); percent-encoding the dots
    # keeps the escape attempt intact all the way to StaticFiles.lookup_path.
    resp = client.get("/uploads/covers/%2e%2e/%2e%2e/etc/passwd")
    # Starlette's StaticFiles still enforces containment, so this returns 404.
    assert resp.status_code == 404
