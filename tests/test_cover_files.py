"""Uploaded cover images are served with immutable cache headers.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: tests/test_cover_files.py
Deps:   Pillow (PIL), fastapi.testclient
Tested: covers the app/web/cover_files.py CoverFiles class, incl. its
  ``?w=`` resize-variant wiring on the ``/uploads`` StaticFiles mount.

Key responsibilities:
- Verify 200 responses carry Cache-Control: immutable.
- Verify 404 and other errors do not carry the immutable cache header.
- Verify path containment (no directory traversal), including with a
  ``?w=`` query string attached.
- Verify a 480 width request returns a resized WebP smaller than the
  original, an unlisted width serves the original bytes untouched, and
  the resized variant is cached to disk and reused on a repeat GET.

Design constraints:
- Use seeded_app_fresh because every test here reads from the disk-mounted
  /uploads/ directory, which StaticFiles points to at app construction time
  (see tests/test_shared_app_uploads_binding.py -- the session-shared
  sibling fixture can't honor this mount).
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from PIL import Image


def _auth(token: str) -> dict:
    """Build an Authorization header from a Bearer token."""
    return {"Authorization": f"Bearer {token}"}


def _noise_png() -> bytes:
    """A 1500x800 random-noise PNG — incompressible, lands at ~3-4 MB, under
    the 5 MiB source cap so the resize path (not the size guard) is exercised.
    """
    im = Image.frombytes("RGB", (1500, 800), os.urandom(1500 * 800 * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _small_png() -> bytes:
    """A 320x160 PNG — smaller than either allowed width, for the
    never-upscale case."""
    im = Image.new("RGB", (320, 160), color="blue")
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


_NOISE_PNG = _noise_png()
_SMALL_PNG = _small_png()


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


# --- /uploads/covers/*.png?w= (StaticFiles mount) -------------------------


def test_upload_cover_variant_resizes_and_caches(seeded_app_fresh):
    """c1/c2/c5: a 480 variant is a small WebP; an unlisted width serves the
    original; the cache file lands once and a repeat GET doesn't rewrite it.
    """
    app_data = seeded_app_fresh
    client = app_data["client"]
    files = {"file": ("noise.png", io.BytesIO(_NOISE_PNG), "image/png")}
    upload = client.post(
        "/api/admin/uploads/cover-image",
        files=files,
        headers=_auth(app_data["admin_token"]),
    )
    assert upload.status_code == 200
    cover_url = upload.json()["url"]

    # c1: allowed width -> resized WebP, immutable cache header, small body.
    r = client.get(f"{cover_url}?w=480")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "image/webp"
    assert "immutable" in r.headers.get("cache-control", "").lower()
    assert len(r.content) < 60_000

    # c2: an unlisted width serves the original bytes untouched.
    r2 = client.get(f"{cover_url}?w=333")
    assert r2.status_code == 200
    assert r2.content == _NOISE_PNG

    # c5: the variant is cached to disk, and a second hit doesn't rewrite it.
    data_dir = Path(app_data["env"]["data_dir"])
    cache_dir = data_dir / "cache" / "img"
    variants = list(cache_dir.glob("*-w480.webp"))
    assert len(variants) == 1
    mtime_before = variants[0].stat().st_mtime_ns

    r3 = client.get(f"{cover_url}?w=480")
    assert r3.status_code == 200
    assert variants[0].stat().st_mtime_ns == mtime_before


def test_upload_cover_variant_traversal_still_blocked(seeded_app_fresh):
    """c3: a ``?w=`` query string doesn't loosen the existing traversal guard."""
    client = seeded_app_fresh["client"]
    resp = client.get("/uploads/covers/%2e%2e/%2e%2e/etc/passwd?w=480")
    assert resp.status_code == 404


def test_upload_cover_variant_post_still_405s(seeded_app_fresh):
    """A ``?w=`` query string doesn't loosen the GET/HEAD-only StaticFiles
    contract -- POST to a cover URL still 405s, same as without ``?w=``."""
    app_data = seeded_app_fresh
    client = app_data["client"]
    files = {"file": ("noise.png", io.BytesIO(_NOISE_PNG), "image/png")}
    upload = client.post(
        "/api/admin/uploads/cover-image",
        files=files,
        headers=_auth(app_data["admin_token"]),
    )
    assert upload.status_code == 200
    cover_url = upload.json()["url"]

    resp = client.post(f"{cover_url}?w=480")
    assert resp.status_code == 405


def test_upload_cover_variant_overlong_filename_404s_not_500s(seeded_app_fresh):
    """An overlong filename with ``?w=480`` 404s the same way the plain URL
    does, instead of the lookup's raw OSError leaking out as a 500."""
    client = seeded_app_fresh["client"]
    overlong_name = "a" * 300 + ".png"
    resp = client.get(f"/uploads/covers/{overlong_name}?w=480")
    assert resp.status_code == 404


def test_upload_cover_variant_conditional_get_304s(seeded_app_fresh):
    """c6: a conditional GET (If-None-Match) against a ``?w=`` variant URL
    gets a 304, matching the base StaticFiles behavior for the original."""
    app_data = seeded_app_fresh
    client = app_data["client"]
    files = {"file": ("noise.png", io.BytesIO(_NOISE_PNG), "image/png")}
    upload = client.post(
        "/api/admin/uploads/cover-image",
        files=files,
        headers=_auth(app_data["admin_token"]),
    )
    assert upload.status_code == 200
    cover_url = upload.json()["url"]

    first = client.get(f"{cover_url}?w=480")
    assert first.status_code == 200
    etag = first.headers.get("etag")
    assert etag

    second = client.get(f"{cover_url}?w=480", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert "immutable" in second.headers.get("cache-control", "").lower()


def test_upload_cover_variant_never_upscales(seeded_app_fresh):
    """c4: a source narrower than the requested width serves the original."""
    app_data = seeded_app_fresh
    client = app_data["client"]
    files = {"file": ("small.png", io.BytesIO(_SMALL_PNG), "image/png")}
    upload = client.post(
        "/api/admin/uploads/cover-image",
        files=files,
        headers=_auth(app_data["admin_token"]),
    )
    assert upload.status_code == 200
    cover_url = upload.json()["url"]

    r = client.get(f"{cover_url}?w=480")
    assert r.status_code == 200
    assert r.content == _SMALL_PNG
