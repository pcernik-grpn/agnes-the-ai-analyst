"""Static-asset presence tests for the cloud-chat web UI (architect finding #3).

`chat.html` and `admin_chat.html` reference `marked.min.js`,
`highlight.min.js`, `highlight.min.css`, and `/static/css/admin.css`.
None of them previously existed on disk — the chat page would throw
`ReferenceError: marked is not defined` on first message render. These
tests pin the vendored files in place so a future "clean the static dir"
sweep fails fast.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user

# Resolve relative to this test file so the assertions don't depend on the
# pytest cwd (worktree paths vary, CI runs from repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDOR = _REPO_ROOT / "app" / "web" / "static" / "vendor"
_CSS = _REPO_ROOT / "app" / "web" / "static" / "css"


TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_app() -> FastAPI:
    from app.web.router import router as web_router

    app = FastAPI()
    app.include_router(web_router)
    app.state.chat_config = SimpleNamespace(enabled=True)
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    return app


@pytest.fixture(autouse=True)
def _grant_chat_access(monkeypatch):
    """Chat is a default-deny RBAC resource; the /chat route redirects users
    without the grant. test_chat_html_references_resolve renders /chat to check
    asset hrefs, so simulate "access granted" by patching can_access."""
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)


@pytest.fixture
def api_client() -> TestClient:
    return TestClient(_make_app())


@pytest.fixture
def logged_in_user():
    return TEST_USER


# ---------------------------------------------------------------------------
# Disk presence + size sanity
# ---------------------------------------------------------------------------


def test_marked_present_and_substantial():
    p = _VENDOR / "marked.min.js"
    assert p.exists(), "marked.min.js missing — re-vendor per LICENSES.md"
    assert p.stat().st_size > 10_000, "marked.min.js suspiciously small"
    # Sanity: it should be the JS file, not an HTML error page.
    head = p.read_bytes()[:200]
    assert b"marked" in head.lower()


def test_highlight_js_present_and_substantial():
    p = _VENDOR / "highlight.min.js"
    assert p.exists(), "highlight.min.js missing — re-vendor per LICENSES.md"
    assert p.stat().st_size > 50_000, "highlight.min.js suspiciously small"
    head = p.read_bytes()[:200]
    assert b"hljs" in head.lower() or b"highlight" in head.lower()


def test_highlight_css_present():
    p = _VENDOR / "highlight.min.css"
    assert p.exists(), "highlight.min.css missing — re-vendor per LICENSES.md"
    assert p.stat().st_size > 1_000


def test_html_to_image_present_and_substantial():
    p = _VENDOR / "html-to-image.min.js"
    assert p.exists(), "html-to-image.min.js missing — re-vendor per LICENSES.md"
    assert p.stat().st_size > 15_000, "html-to-image.min.js suspiciously small"
    head = p.read_bytes()[:400]
    assert b"htmltoimage" in head.lower()


def test_vendor_licenses_documented():
    p = _VENDOR / "LICENSES.md"
    assert p.exists(), "LICENSES.md must document vendored sources/versions"
    text = p.read_text(encoding="utf-8")
    assert "marked" in text.lower()
    assert "highlight" in text.lower()
    assert "html-to-image" in text.lower()
    assert "MIT" in text  # marked
    assert "BSD-3" in text  # highlight.js


def test_admin_css_present():
    p = _CSS / "admin.css"
    assert p.exists(), "admin.css missing — chat + admin pages won't style"
    assert p.stat().st_size > 200


# ---------------------------------------------------------------------------
# Template integration — references resolve to real files
# ---------------------------------------------------------------------------


def test_chat_html_references_resolve(api_client: TestClient, logged_in_user):
    """Every /static/... href in /chat must exist on disk.

    Updated after the chat page migrated to ``base_ds.html``: the page now
    inherits the four base stylesheets (style-custom, design-tokens,
    components, stack_card) instead of bringing in ``admin.css`` directly.
    Chat-specific styles live in ``chat.css`` next to the vendored
    marked.js / highlight.js bundles.
    """
    html = api_client.get("/chat").text
    web_root = _REPO_ROOT / "app" / "web"
    for href in (
        "/static/vendor/marked.min.js",
        "/static/vendor/highlight.min.js",
        "/static/vendor/highlight.min.css",
        "/static/css/chat.css",
        # Base chrome — emitted by base_ds.html. If any of these has an
        # empty href the route silently broke ``_build_context``.
        "/static/style-custom.css",
        "/static/css/design-tokens.css",
        "/static/css/components.css",
    ):
        assert href in html, f"chat.html should reference {href}"
        # ``static_url`` adds a ``?v=…`` query string; the path on disk is
        # the bare href.  Map ``/static/...`` → ``app/web/static/...``.
        on_disk = web_root / href.lstrip("/")
        assert on_disk.exists(), f"{href} referenced but not on disk at {on_disk}"
