"""Cover images can be requested as resized WebP variants via ``?w=``.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: tests/test_cover_image_perf_contract.py
Deps:   Pillow (PIL), fastapi.testclient
Tested: covers src/images/variants.py + the ``?w=`` wiring in
  app/web/cover_files.py, app/api/marketplace.py and app/api/store.py

Key responsibilities:
- A 480/960 width request returns a resized WebP smaller than the original.
- Any other width value serves the original bytes untouched.
- The disk-fill / CPU guards (oversized source, no upscale) hold.
- Path containment on the uploads mount still refuses traversal with ``?w=``
  attached.
- The variant is generated once and reused on a second request (cache hit).

Design constraints:
- Uses the session-shared ``seeded_app`` / fresh ``seeded_app_fresh``
  fixtures per tests/conftest.py's contract — never a private
  ``create_app()`` call.
- Test images are generated at runtime with Pillow, not checked into the
  repo (pre-commit rejects files > 500 KB).
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

from PIL import Image

from src.images.variants import variant_path


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


def _flea_skill_zip() -> bytes:
    """Minimal valid skill .zip for POST /api/store/entities (mirrors
    tests/test_marketplace_v32_endpoints.py::_flea_zip_for_skill)."""
    buf = io.BytesIO()
    body = (
        "Body explaining when to invoke the skill and the expected outputs. "
        "Long enough to clear the 200-char content guardrail floor. " * 2
    )
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "SKILL.md",
            "---\nname: coverpermtest\n"
            "description: Use when validating cover-image variant endpoint integrations\n"
            f"---\n\n{body}\n",
        )
    return buf.getvalue()


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


def test_variant_path_uses_post_transpose_dimensions(tmp_path):
    """A raw-encoded portrait JPEG (EXIF Orientation=6, 90 deg rotated) has
    swapped width/height at open time relative to what it displays as. The
    never-upscale bail-out must compare against the corrected (post
    ``exif_transpose``) dimensions, not the raw encoded ones, or a phone
    portrait photo silently skips its variant.
    """
    # Raw encoded 400x1200; Orientation=6 means the logical/displayed image
    # is 1200x400 once rotated the right way up.
    im = Image.new("RGB", (400, 1200), color="red")
    exif = Image.Exif()
    exif[274] = 6  # Orientation tag
    src = tmp_path / "portrait.jpg"
    im.save(src, "JPEG", exif=exif)

    variant = variant_path(src, 480)
    assert variant is not None
    with Image.open(variant) as out:
        assert out.width == 480


# --- /api/store/entities/{id}/photo?w= -------------------------------------


def test_store_entity_photo_variant(seeded_app):
    """c6: the store entity photo route serves a resized variant too."""
    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_skill_zip(), "application/zip")),
            ("photo", ("cover.png", _NOISE_PNG, "image/png")),
        ],
        data={
            "type": "skill",
            "version": "1.0",
            "description": "Use when validating cover-image variant endpoint integrations",
        },
    )
    assert r.status_code == 201, r.text
    entity_id = r.json()["id"]

    r2 = client.get(f"/api/store/entities/{entity_id}/photo?w=480", headers=headers)
    assert r2.status_code == 200
    assert r2.headers.get("content-type") == "image/webp"
    assert len(r2.content) < 60_000

    r3 = client.get(f"/api/store/entities/{entity_id}/photo?w=1", headers=headers)
    assert r3.status_code == 200
    assert r3.content == _NOISE_PNG

    # A non-integer or empty width must never 4xx -- it just serves the
    # original, same as any other unlisted width.
    r4 = client.get(f"/api/store/entities/{entity_id}/photo?w=abc", headers=headers)
    assert r4.status_code == 200
    assert r4.content == _NOISE_PNG

    r5 = client.get(f"/api/store/entities/{entity_id}/photo?w=", headers=headers)
    assert r5.status_code == 200
    assert r5.content == _NOISE_PNG


# --- /api/marketplace/curated/{mp}/{plugin}/asset/{path}?w= ----------------


def test_curated_asset_variant(seeded_app):
    """c7: a curated-marketplace cover asset also serves a resized variant.

    No sync/RBAC plumbing needed — ``curated_asset`` is login-only and reads
    straight off ``get_marketplaces_dir()/<marketplace_id>``, so seeding a
    file directly (as tests/test_marketplace_v32_endpoints.py's own
    traversal test does) is enough.
    """
    data_dir = Path(seeded_app["env"]["data_dir"])
    repo_root = data_dir / "marketplaces" / "cover-variant-mp"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "cover.png").write_bytes(_NOISE_PNG)

    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    r = client.get(
        "/api/marketplace/curated/cover-variant-mp/demo/asset/cover.png?w=480",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.headers.get("content-type") == "image/webp"
    assert len(r.content) < 60_000


def test_curated_mirrored_variant(seeded_app):
    """curated_mirrored has its own ``?w=`` wiring, structurally separate
    from ``curated_asset`` (different dir root) — a resized WebP, an
    unlisted width falling back to the original, and cache-hit reuse.

    ``seeded_app`` is session-shared, so the cache dir may already hold
    ``-w480.webp`` files from other tests' sources; identify the one this
    test produced by set difference rather than assuming it's the only one.
    """
    data_dir = Path(seeded_app["env"]["data_dir"])
    cache_root = data_dir / "marketplace-cache" / "cover-variant-mirrored-mp" / "demo"
    cache_root.mkdir(parents=True, exist_ok=True)
    (cache_root / "cover.png").write_bytes(_NOISE_PNG)

    client = seeded_app["client"]
    headers = _auth(seeded_app["admin_token"])
    url = "/api/marketplace/curated/cover-variant-mirrored-mp/demo/mirrored/cover.png"
    variant_dir = data_dir / "cache" / "img"
    before = set(variant_dir.glob("*-w480.webp"))

    r = client.get(f"{url}?w=480", headers=headers)
    assert r.status_code == 200
    assert r.headers.get("content-type") == "image/webp"
    assert len(r.content) < 60_000

    r2 = client.get(f"{url}?w=1", headers=headers)
    assert r2.status_code == 200
    assert r2.content == _NOISE_PNG

    new_variants = set(variant_dir.glob("*-w480.webp")) - before
    assert len(new_variants) == 1
    variant = new_variants.pop()
    mtime_before = variant.stat().st_mtime_ns

    r3 = client.get(f"{url}?w=480", headers=headers)
    assert r3.status_code == 200
    assert variant.stat().st_mtime_ns == mtime_before
