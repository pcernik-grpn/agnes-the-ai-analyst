"""Configurable favicon (``instance.favicon`` / ``AGNES_INSTANCE_FAVICON``) —
end-to-end rendering on both base layouts (``base_ds.html`` / ``base_login.html``),
plus the two context builders that must expose it (#996's single-owner rule).

Uses ``/privacy`` (unauthenticated, ``base_login.html``) and
``/first-time-setup`` (unauthenticated when no users exist yet,
``base_ds.html``) so no login/seeding is required.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    from src.db import close_system_db

    close_system_db()
    from fastapi.testclient import TestClient


    app = shared_app
    yield TestClient(app)
    close_system_db()


def _favicon_link(html: str) -> str:
    import re

    m = re.search(r'<link rel="icon"[^>]*href="([^"]*)"', html)
    assert m, 'no <link rel="icon"> tag found'
    return m.group(1)


def test_default_favicon_renders_orb_on_base_login(web_client):
    from app.web.router import _static_url

    resp = web_client.get("/privacy")
    assert resp.status_code == 200
    assert _favicon_link(resp.text) == _static_url("img/agnes-orb.png")


def test_default_favicon_renders_orb_on_base_ds(web_client):
    from app.web.router import _static_url

    resp = web_client.get("/first-time-setup")
    assert resp.status_code == 200
    assert _favicon_link(resp.text) == _static_url("img/agnes-orb.png")


def test_configured_data_uri_favicon_renders_verbatim(web_client, monkeypatch):
    data_uri = "data:image/png;base64,iVBORw0KGgo="
    monkeypatch.setenv("AGNES_INSTANCE_FAVICON", data_uri)
    resp = web_client.get("/privacy")
    assert resp.status_code == 200
    assert _favicon_link(resp.text) == data_uri


def test_configured_absolute_url_favicon_renders_verbatim(web_client, monkeypatch):
    url = "https://cdn.example.com/favicon.ico"
    monkeypatch.setenv("AGNES_INSTANCE_FAVICON", url)
    resp = web_client.get("/first-time-setup")
    assert resp.status_code == 200
    assert _favicon_link(resp.text) == url


def test_configured_static_path_favicon_resolved_through_static_url(web_client, monkeypatch):
    """A relative path (not a data:/absolute URL) still gets the
    cache-buster query string every other static asset gets."""
    from app.web.router import _static_url

    monkeypatch.setenv("AGNES_INSTANCE_FAVICON", "img/agnes-orb.png")
    resp = web_client.get("/privacy")
    assert resp.status_code == 200
    assert _favicon_link(resp.text) == _static_url("img/agnes-orb.png")


def _synthetic_request(path: str = "/"):
    from types import SimpleNamespace as _NS

    from starlette.requests import Request

    app = _NS(state=_NS(chat_config=_NS(enabled=False)))
    scope = {
        "type": "http",
        "app": app,
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "scheme": "http",
        "client": ("1.2.3.4", 9),
    }
    return Request(scope)


def test_chrome_ctx_includes_instance_favicon(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    from app.web.router import _chrome_ctx, _static_url

    ctx = _chrome_ctx(_synthetic_request(), None)
    assert ctx["instance_favicon"] == _static_url("img/agnes-orb.png")


def test_build_context_includes_instance_favicon(tmp_path, monkeypatch):
    """``_build_context`` composes ``_chrome_ctx`` (#996) — the key must
    survive that composition, not just exist on the inner dict."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    from src.db import close_system_db

    close_system_db()
    from app.web.router import _build_context, _static_url

    ctx = _build_context(_synthetic_request())
    assert ctx["instance_favicon"] == _static_url("img/agnes-orb.png")
    close_system_db()


def test_icon_link_declares_no_type(web_client):
    """The href is operator-configurable — a `.png`, `.svg`, `.ico` or a `data:`
    URI are all valid — so a literal `type="image/png"` would be provably wrong
    for several of them. Per the HTML spec `type` is only a hint a UA MAY use to
    skip a resource it cannot render, and there is exactly one icon link per
    page, so it buys nothing and is dropped rather than derived."""
    for path in ("/login", "/how-it-works"):
        html = web_client.get(path).text
        links = [ln for ln in html.splitlines() if 'rel="icon"' in ln]
        assert links, f"{path} renders no icon link"
        for ln in links:
            assert "type=" not in ln, f"{path} still declares a favicon type: {ln.strip()}"
