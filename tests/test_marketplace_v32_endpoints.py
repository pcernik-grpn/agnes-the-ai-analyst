"""End-to-end smoke tests for v32 endpoints + Flea allowlist behavior.

* GET /marketplace/format-guide — auth-gated (any logged-in user, no admin),
  renders the markdown source.
* /api/marketplace/curated/.../asset/{path} — path-traversal guard, RBAC.
* Flea upload allowlist on /api/store/entities (photo + doc).
"""

from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import quote

import pytest


# --- /marketplace/format-guide -------------------------------------------


_BROWSER_ACCEPT = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


@pytest.mark.parametrize(
    "path",
    [
        "/marketplace/format-guide",
        "/marketplace/guide/flea",
        "/marketplace/flea/96b9d50176604140bc682575402f9304",
        "/marketplace/curated/some-marketplace/some-plugin",
    ],
)
def test_marketplace_html_page_signed_out_redirects_to_login(seeded_app, path):
    """A signed-out browser on any /marketplace/* HTML page lands on /login
    with ``next`` pointing back — the same contract every other HTML page
    honours — never a raw ``{"detail": ...}`` JSON 401.

    Regression: ``"/marketplace/"`` sat in ``_API_PATH_PREFIXES`` (app/main.py),
    so a shared deep link to a plugin looked broken to anyone not signed in.
    """
    client = seeded_app["client"]
    client.cookies.clear()
    r = client.get(path, headers=_BROWSER_ACCEPT, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == f"/login?next={quote(path, safe='')}"


@pytest.mark.parametrize("path", ["/marketplace/info", "/marketplace/cowork/some-plugin.zip"])
def test_marketplace_api_path_signed_out_stays_json_401(seeded_app, path):
    """The machine-facing paths under the same prefix keep the raw JSON 401
    (CLI / installer clients parse ``{"detail": ...}``, never a redirect)."""
    client = seeded_app["client"]
    client.cookies.clear()
    r = client.get(path, headers=_BROWSER_ACCEPT, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"] == "Missing or invalid Authorization header"


def test_format_guide_renders_for_logged_in_user(seeded_app):
    """Any logged-in user (admin in this seeded fixture) sees the rendered page.

    The rendered HTML must:
    * carry the curator-focused title (post-walkthrough rewrite),
    * include the disclaimer scoping the guide to the Curated Marketplace
      channel only (so a curator doesn't think it applies to the Flea
      upload wizard),
    * render the fenced JSON example as a <pre><code> block.
    """
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.get("/marketplace/format-guide", headers=headers)
    assert r.status_code == 200
    body = r.text
    # Title points at curators (rewrite from the original "format guide").
    assert "Curated Marketplace" in body
    # Channel-scoping disclaimer near the top of the page.
    assert "Curated Marketplace channel only" in body
    # JSON examples render as code blocks.
    assert "<pre>" in body or "<code" in body


# --- /api/store/entities — Flea allowlist enforcement --------------------


@pytest.fixture
def _flea_zip_for_skill(tmp_path):
    """Return the bytes of a minimal valid skill .zip for /entities upload.

    Mirrors the layout `app/api/store.py::_validate_and_extract_metadata`
    expects: a single SKILL.md at the top of the archive with frontmatter
    declaring `name` + `description`.
    """
    import zipfile

    buf = io.BytesIO()
    body = (
        "Body explaining when to invoke the skill and the expected outputs. "
        "Long enough to clear the 200-char content guardrail floor. " * 2
    )
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "SKILL.md",
            "---\nname: testskill\n"
            "description: Use when validating flea-market endpoint integrations across guardrails\n"
            f"---\n\n{body}\n",
        )
    return buf.getvalue()


def test_flea_doc_upload_rejects_docx(seeded_app, _flea_zip_for_skill):
    """v32: Flea doc upload allowlist — .docx is rejected with 415."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("docs", ("notes.docx", b"PKfake-docx-content", "application/vnd.openxmlformats")),
        ],
        data={
            "type": "skill",
            "version": "1.0",
            "description": "Use when validating flea-market endpoint integrations across guardrails",
        },
    )
    assert r.status_code == 415
    assert "unsupported_doc_type" in r.text or "doc_extension" in r.text


def test_flea_doc_upload_accepts_pdf(seeded_app, _flea_zip_for_skill):
    """A real PDF (with %PDF magic bytes) makes it through both extension +
    body validation."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    pdf_body = b"%PDF-1.4\n% comment\n"
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("docs", ("setup.pdf", pdf_body, "application/pdf")),
        ],
        data={
            "type": "skill",
            "version": "1.0",
            "description": "Use when validating flea-market endpoint integrations across guardrails",
        },
    )
    assert r.status_code == 201, r.text


def test_flea_doc_upload_rejects_pdf_with_bad_magic_bytes(seeded_app, _flea_zip_for_skill):
    """Defense in depth: a file named .pdf but with non-PDF body bytes is
    rejected by the magic-bytes check."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("docs", ("evil.pdf", b"not a pdf at all", "application/pdf")),
        ],
        data={
            "type": "skill",
            "version": "1.0",
            "description": "Use when validating flea-market endpoint integrations across guardrails",
        },
    )
    assert r.status_code == 415
    assert "magic_bytes" in r.text or "unsupported" in r.text


def test_flea_photo_upload_rejects_svg(seeded_app, _flea_zip_for_skill):
    """SVG photos are rejected — extension check (svg not in allowlist) fires
    first and returns 415 with photo_unsupported_format."""
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.post(
        "/api/store/entities",
        headers=headers,
        files=[
            ("file", ("skill.zip", _flea_zip_for_skill, "application/zip")),
            ("photo", ("logo.svg", b"<svg></svg>", "image/svg+xml")),
        ],
        data={
            "type": "skill",
            "version": "1.0",
            "description": "Use when validating flea-market endpoint integrations across guardrails",
        },
    )
    assert r.status_code == 415
    assert "photo_unsupported_format" in r.text


# --- /api/marketplace/curated/.../asset path-traversal guard -------------


_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_curated_asset_endpoint_blocks_path_traversal(seeded_app, monkeypatch, tmp_path):
    """Hitting the asset endpoint with `../` segments must return 404 (not the
    file outside the marketplace dir). The route's ``Path.resolve(strict=True)
    + is_relative_to`` guard is the load-bearing check."""
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    # Stand up a tiny on-disk marketplace at the configured marketplaces dir.
    # The asset endpoint is image-only post-#234-review; the seeded fixture
    # carries a real PNG (with magic bytes) so the sanity-200 case still
    # works alongside the path-traversal regression check.
    data_dir = Path(seeded_app["env"]["data_dir"])
    repo_root = data_dir / "marketplaces" / "test-mp"
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "ok.png").write_bytes(_PNG_1x1)

    # Register the marketplace (with curator) + grant Admin access to its
    # synthetic plugin so the resource_grants check passes.
    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id="test-mp",
            name="Test",
            url="https://example.com/x.git",
            curator_name="C",
            curator_email="c@example.com",
        )
        # Insert a plugin row so the RBAC check has a target. Resource id is
        # `<slug>/<plugin>`.
        conn.execute("INSERT OR REPLACE INTO marketplace_plugins (marketplace_id, name) VALUES ('test-mp', 'demo')")
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        if admin_group:
            try:
                ResourceGrantsRepository(conn).create(
                    group_id=admin_group[0],
                    resource_type="marketplace_plugin",
                    resource_id="test-mp/demo",
                    assigned_by="test",
                )
            except Exception:
                pass  # already granted from a prior test run
    finally:
        conn.close()

    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Sanity: a normal in-tree image fetch returns 200.
    r = client.get(
        "/api/marketplace/curated/test-mp/demo/asset/ok.png",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.content == _PNG_1x1

    # Traversal attempt: `..` segments. FastAPI's path param accepts the
    # value; our ``_safe_join`` resolves it then checks containment, so the
    # result is 404 (not 200, not 500).
    r = client.get(
        "/api/marketplace/curated/test-mp/demo/asset/../escape.txt",
        headers=headers,
    )
    assert r.status_code == 404


def test_reject_unsafe_segment_blocks_dotdot(seeded_app):
    """Audit L1: a ``..`` (or ``/``/``\\``) marketplace_id/plugin_name escapes
    the marketplaces dir (``marketplaces/..`` → DATA_DIR) and _safe_join then
    re-anchors on the escaped root. The segment guard must reject it (404).

    The trailing-newline and bare-``.`` cases are 2026-08-05 audit follow-ups:
    the previous ``re.match()`` against a ``^…$`` pattern accepted ``"acme\\n"``
    (Python's ``$`` also matches immediately before a trailing newline) and the
    explicit ``seg == ".."`` check missed both ``"."`` and ``"..\\n"``. The
    delegation to ``is_safe_plugin_name`` (``fullmatch`` + explicit ``.``/``..``
    rejection) closed all three; these cases keep them closed.
    """
    from fastapi import HTTPException

    from app.api.marketplace import _reject_unsafe_segment

    for bad in ("..", "a/b", "a\\b", "", "foo/../bar", "acme\n", ".", "..\n", "acme "):
        with pytest.raises(HTTPException) as ei:
            _reject_unsafe_segment(bad)
        assert ei.value.status_code == 404, bad
    # legitimate slugs pass through untouched
    _reject_unsafe_segment("test-mp", "demo_plugin", "a.b-c", "PascalCase")


def test_curated_doc_blocks_cross_plugin_disclosure(seeded_app):
    """Audit M2: the RBAC grant is per-plugin, but the served doc path is
    repo-root-relative. A grant to plugin `public` must NOT read `private`'s
    docs in the same marketplace via a repo-relative path pointing into it."""
    from src.marketplace_asset_validation import DOC_EXTENSIONS

    assert ".md" in DOC_EXTENSIONS  # sanity — the doc ext we use below is allowed
    repo_root = _seed_asset_marketplace(seeded_app, slug="xplug", plugin="public")
    (repo_root / "plugins" / "public" / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "plugins" / "public" / "docs" / "own.md").write_text("mine", encoding="utf-8")
    (repo_root / "plugins" / "private" / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "plugins" / "private" / "docs" / "secret.md").write_text("secret", encoding="utf-8")

    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # The granted plugin's own doc serves fine.
    r_ok = client.get("/api/marketplace/curated/xplug/public/doc/plugins/public/docs/own.md", headers=headers)
    assert r_ok.status_code == 200, r_ok.text

    # A repo-relative path into a DIFFERENT plugin's dir is refused (404),
    # even though the file exists and the URL's plugin_name (public) is granted.
    r = client.get("/api/marketplace/curated/xplug/public/doc/plugins/private/docs/secret.md", headers=headers)
    assert r.status_code == 404, r.text


# --- /asset/{path} XSS hardening (#234 review #3) -------------------------


def _seed_asset_marketplace(seeded_app, *, slug="xss-mp", plugin="demo"):
    """Helper: register marketplace + grant Admin RBAC. Returns repo_root."""
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    data_dir = Path(seeded_app["env"]["data_dir"])
    repo_root = data_dir / "marketplaces" / slug
    repo_root.mkdir(parents=True, exist_ok=True)

    conn = get_system_db()
    try:
        try:
            MarketplaceRegistryRepository(conn).register(
                id=slug,
                name=slug,
                url=f"https://example.com/{slug}.git",
                curator_name="C",
                curator_email="c@example.com",
            )
        except Exception:
            pass  # already registered
        conn.execute(
            "INSERT OR REPLACE INTO marketplace_plugins (marketplace_id, name) VALUES (?, ?)",
            [slug, plugin],
        )
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        if admin_group:
            try:
                ResourceGrantsRepository(conn).create(
                    group_id=admin_group[0],
                    resource_type="marketplace_plugin",
                    resource_id=f"{slug}/{plugin}",
                    assigned_by="test",
                )
            except Exception:
                pass
    finally:
        conn.close()
    return repo_root


def test_curated_asset_rejects_html_extension(seeded_app):
    """A curator-planted ``.html`` in the cloned repo MUST NOT be served as
    ``text/html`` — extension allowlist denies any non-image extension."""
    repo_root = _seed_asset_marketplace(seeded_app, slug="xss-html")
    (repo_root / "evil.html").write_text(
        "<script>alert('xss')</script>",
        encoding="utf-8",
    )
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.get(
        "/api/marketplace/curated/xss-html/demo/asset/evil.html",
        headers=headers,
    )
    assert r.status_code == 415
    assert "unsupported_asset_extension" in r.text


def test_curated_asset_renamed_html_is_neutered_by_headers(seeded_app):
    """A curator who renames ``evil.html`` to ``evil.png`` no longer triggers
    a 415 — magic-bytes validation was dropped from the request path (see
    CHANGELOG entry under [Unreleased] → Changed). The remaining defense
    layers neuter the payload at the browser:

    * ``Content-Type`` pinned to ``image/png`` from the extension table, so
      the browser never parses the body as HTML.
    * ``X-Content-Type-Options: nosniff`` so the browser refuses to second-
      guess the declared Content-Type.
    * Strict CSP (``default-src 'none'``) so even if HTML did render,
      scripts/iframes/etc. couldn't execute.

    Body validation happens at curator-content-acceptance time (git fetch
    against the admin-registered upstream repo), not on every GET request.
    """
    repo_root = _seed_asset_marketplace(seeded_app, slug="xss-rename")
    (repo_root / "evil.png").write_text(
        "<script>alert('xss')</script>",
        encoding="utf-8",
    )
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.get(
        "/api/marketplace/curated/xss-rename/demo/asset/evil.png",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/png")
    assert r.headers.get("x-content-type-options") == "nosniff"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp


def test_curated_asset_rejects_svg_extension(seeded_app):
    """SVG is intentionally OUT of the allowlist — ``<script>`` inside SVG
    executes in the browser, so even valid SVG would carry XSS risk."""
    repo_root = _seed_asset_marketplace(seeded_app, slug="xss-svg")
    (repo_root / "logo.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>alert("xss")</script></svg>',
        encoding="utf-8",
    )
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.get(
        "/api/marketplace/curated/xss-svg/demo/asset/logo.svg",
        headers=headers,
    )
    assert r.status_code == 415


def test_curated_asset_serves_valid_png_with_security_headers(seeded_app):
    """Happy path: a real PNG returns 200, ``Content-Type: image/png``,
    plus the defense-in-depth headers ``X-Content-Type-Options: nosniff``
    and a strict ``Content-Security-Policy`` that blocks script execution
    even if a future regression let HTML through.
    """
    repo_root = _seed_asset_marketplace(seeded_app, slug="xss-ok")
    (repo_root / "cover.png").write_bytes(_PNG_1x1)
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    r = client.get(
        "/api/marketplace/curated/xss-ok/demo/asset/cover.png",
        headers=headers,
    )
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/png")
    assert r.headers.get("x-content-type-options") == "nosniff"
    csp = r.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src" not in csp or "'none'" in csp  # no script source allowed


# --- Content-Disposition force-download on /doc and /mirrored/docs --------


def _seed_marketplace_with_doc(seeded_app, *, slug="dl-mp", plugin="demo"):
    """Helper: register a marketplace, drop a sample doc into its working
    tree, and grant Admin RBAC. Returns the data_dir for further fixturing."""
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.resource_grants import ResourceGrantsRepository

    data_dir = Path(seeded_app["env"]["data_dir"])
    repo_root = data_dir / "marketplaces" / slug
    repo_root.mkdir(parents=True, exist_ok=True)
    (repo_root / "docs").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (repo_root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id=slug,
            name=slug,
            url=f"https://example.com/{slug}.git",
            curator_name="C",
            curator_email="c@example.com",
        )
        conn.execute(
            "INSERT OR REPLACE INTO marketplace_plugins (marketplace_id, name) VALUES (?, ?)",
            [slug, plugin],
        )
        admin_group = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()
        if admin_group:
            try:
                ResourceGrantsRepository(conn).create(
                    group_id=admin_group[0],
                    resource_type="marketplace_plugin",
                    resource_id=f"{slug}/{plugin}",
                    assigned_by="test",
                )
            except Exception:
                pass
    finally:
        conn.close()
    return data_dir


def test_curated_doc_endpoint_sets_attachment_disposition(seeded_app):
    """/doc serves as `Content-Disposition: attachment` so clicks download
    the file rather than open it inline. /asset stays inline (covers belong
    in <img>, not as downloads)."""
    _seed_marketplace_with_doc(seeded_app)
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}

    # Doc → attachment + filename hint
    r = client.get(
        "/api/marketplace/curated/dl-mp/demo/doc/docs/guide.md",
        headers=headers,
    )
    assert r.status_code == 200
    assert "attachment" in r.headers.get("content-disposition", "").lower()
    assert "guide.md" in r.headers.get("content-disposition", "")

    # Asset (cover image) → no attachment header (renders inline)
    r = client.get(
        "/api/marketplace/curated/dl-mp/demo/asset/logo.png",
        headers=headers,
    )
    assert r.status_code == 200
    assert "attachment" not in r.headers.get("content-disposition", "").lower()


def test_curated_doc_endpoint_rejects_non_allowlist_extension(seeded_app):
    """The /doc endpoint refuses to serve a file whose extension isn't in
    the allowlist (PDF / Markdown / plain text). Defense-in-depth even when
    the parser lets a curator's exotic path slip through."""
    data_dir = _seed_marketplace_with_doc(seeded_app)
    # Plant a .docx file in the seeded marketplace
    (data_dir / "marketplaces" / "dl-mp" / "docs" / "evil.docx").write_text(
        "PKfake",
        encoding="utf-8",
    )
    client = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['admin_token']}"}
    r = client.get(
        "/api/marketplace/curated/dl-mp/demo/doc/docs/evil.docx",
        headers=headers,
    )
    assert r.status_code == 415
    assert "unsupported_doc_extension" in r.text
