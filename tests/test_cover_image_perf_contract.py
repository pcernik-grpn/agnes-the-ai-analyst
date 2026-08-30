"""Cover images can be requested as resized WebP variants via ``?w=``.

Project: Agnes — platform for analyzing structured data with extraction,
  facts, and semantic search; operates offline, in-app.
Module: tests/test_cover_image_perf_contract.py
Deps:   Pillow (PIL), fastapi.testclient
Tested: covers src/images/variants.py + the ``?w=`` wiring in
  app/api/marketplace.py and app/api/store.py, plus the ``cover_w`` Jinja
  filter (app/web/router.py) that points a rendered cover ``<img>`` at those
  variants. The ``/uploads`` StaticFiles mount's own ``?w=`` wiring (incl.
  its traversal guard) is tested in tests/test_cover_files.py instead --
  that mount is frozen to the app's DATA_DIR at construction time, which the
  session-shared ``seeded_app`` used elsewhere in this file cannot honor
  (see tests/test_shared_app_uploads_binding.py).

Key responsibilities:
- A 480/960 width request returns a resized WebP smaller than the original.
- Any other width value serves the original bytes untouched.
- The disk-fill / CPU guards (oversized source, no upscale) hold.
- The variant is generated once and reused on a second request (cache hit).
- Server-rendered cover ``<img>``s: the stack-card macro (grid layout,
  width:100% of its column) carries a 480/960 srcset, and an un-mirrored
  external cover URL is left without one; the package hero (fixed-size
  tile, never a fluid grid column) fetches the 480 variant directly and
  never carries a srcset.

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
from functools import lru_cache
from pathlib import Path

from PIL import Image

from src.images.variants import MAX_SOURCE_BYTES, variant_path


def _auth(token: str) -> dict:
    """Build an Authorization header from a Bearer token."""
    return {"Authorization": f"Bearer {token}"}


@lru_cache(maxsize=1)
def _noise_png() -> bytes:
    """A 1500x800 random-noise PNG — incompressible, lands at ~3-4 MB, under
    the 5 MiB source cap so the resize path (not the size guard) is exercised.

    Cached so building it (encoding ~3-4 MB of incompressible noise) is paid
    once per test run, by whichever test needs it first, rather than at
    collection time -- a collection-only run (``pytest --collect-only``,
    ``-k`` filtering everything else out) pays nothing.
    """
    im = Image.frombytes("RGB", (1500, 800), os.urandom(1500 * 800 * 3))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


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


def test_variant_path_rejects_oversized_source(tmp_path):
    """The disk-fill / CPU guard actually gets exercised: a source over
    ``MAX_SOURCE_BYTES`` must never produce a variant, regardless of its
    (declared or actual) image dimensions -- the size check runs before any
    decode is attempted."""
    oversized = tmp_path / "oversized.bin"
    oversized.write_bytes(os.urandom(MAX_SOURCE_BYTES + 1))

    assert variant_path(oversized, 480) is None


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
            ("photo", ("cover.png", _noise_png(), "image/png")),
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
    assert r3.content == _noise_png()

    # A non-integer or empty width must never 4xx -- it just serves the
    # original, same as any other unlisted width.
    r4 = client.get(f"/api/store/entities/{entity_id}/photo?w=abc", headers=headers)
    assert r4.status_code == 200
    assert r4.content == _noise_png()

    r5 = client.get(f"/api/store/entities/{entity_id}/photo?w=", headers=headers)
    assert r5.status_code == 200
    assert r5.content == _noise_png()


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
    (repo_root / "cover.png").write_bytes(_noise_png())

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
    (cache_root / "cover.png").write_bytes(_noise_png())

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
    assert r2.content == _noise_png()

    new_variants = set(variant_dir.glob("*-w480.webp")) - before
    assert len(new_variants) == 1
    variant = new_variants.pop()
    mtime_before = variant.stat().st_mtime_ns

    r3 = client.get(f"{url}?w=480", headers=headers)
    assert r3.status_code == 200
    assert variant.stat().st_mtime_ns == mtime_before


# --- templates ask for the variants (cover_w Jinja filter) -----------------


def test_stack_card_cover_has_srcset_and_perf_attrs():
    """d1: the shared stack-card macro's cover ``<img>`` carries the full
    perf contract (lazy, async-decoded, sized) plus a 480/960 srcset built
    from the ``cover_w`` filter. Rendered through the app's own Jinja
    environment (``app.web.router.templates``) rather than a bare
    ``jinja2.Environment`` (contrast tests/test_web_stack_card_macro.py) —
    ``cover_w`` is registered on the app env only.
    """
    from app.web.router import templates

    tmpl = templates.env.from_string('{% from "macros/_stack_card.html" import card %}{{ card(entry) }}')
    html = tmpl.render(
        entry={
            "id": "p1",
            "name": "Sales bundle",
            "icon": "📦",
            "color": "#fce7f3",
            "requirement": "available",
            "in_stack": False,
            "cover_image_url": "/uploads/covers/abc123.png",
        }
    )
    assert 'loading="lazy"' in html
    assert 'decoding="async"' in html
    assert 'width="480"' in html
    assert 'height="240"' in html
    assert "?w=480 480w" in html
    assert "?w=960 960w" in html
    assert "sizes=" in html


def test_stack_card_cover_external_url_has_no_srcset():
    """d3: an un-mirrored external cover URL passes through ``cover_w``
    unchanged and never grows a ``?w=`` variant request."""
    from app.web.router import templates

    tmpl = templates.env.from_string('{% from "macros/_stack_card.html" import card %}{{ card(entry) }}')
    html = tmpl.render(
        entry={
            "id": "p1",
            "name": "Sales bundle",
            "icon": "📦",
            "color": "#fce7f3",
            "requirement": "available",
            "in_stack": False,
            "cover_image_url": "https://example.com/cover.png",
        }
    )
    assert "?w=" not in html
    assert "srcset=" not in html
    assert 'src="https://example.com/cover.png"' in html


def test_stack_card_cover_protocol_relative_url_has_no_srcset():
    """d4: a protocol-relative ``//host/...`` cover URL is external too --
    the browser resolves it against a foreign host, so it must not be
    treated as one of our own ``?w=``-capable serving routes."""
    from app.web.router import templates

    tmpl = templates.env.from_string('{% from "macros/_stack_card.html" import card %}{{ card(entry) }}')
    html = tmpl.render(
        entry={
            "id": "p1",
            "name": "Sales bundle",
            "icon": "📦",
            "color": "#fce7f3",
            "requirement": "available",
            "in_stack": False,
            "cover_image_url": "//cdn.example.com/cover.png",
        }
    )
    assert "?w=" not in html
    assert "srcset=" not in html
    assert 'src="//cdn.example.com/cover.png"' in html


def test_catalog_package_hero_has_no_srcset_and_eager_high_priority(seeded_app):
    """d2: GET /catalog/p/<slug> — the page hero cover is an admin-uploaded
    (internal, relative) cover, so it gets ``fetchpriority="high"`` and no
    ``loading`` attribute (a hero is above the fold), but fetches the fixed
    480 variant directly: the hero tile is a fixed 108x62 CSS-px box (never
    a fluid grid column like the stack card), so a srcset only offers a
    960 the tile can never show a visible benefit from.
    """
    app_data = seeded_app
    client = app_data["client"]
    headers = _auth(app_data["admin_token"])
    create = client.post(
        "/api/admin/data-packages",
        json={
            "name": "Cover perf package",
            "slug": "cover-perf-package",
            "cover_image_url": "/uploads/covers/deadbeef.png",
        },
        headers=headers,
    )
    assert create.status_code == 201, create.text

    r = client.get("/catalog/p/cover-perf-package", headers=headers)
    assert r.status_code == 200
    html = r.text
    img_start = html.index('<img src="/uploads/covers/deadbeef.png')
    hero_img = html[img_start : html.index(">", img_start) + 1]
    assert 'fetchpriority="high"' in hero_img
    assert "loading=" not in hero_img
    assert 'width="480"' in hero_img
    assert 'height="240"' in hero_img
    assert "?w=480" in hero_img
    assert "srcset=" not in hero_img
