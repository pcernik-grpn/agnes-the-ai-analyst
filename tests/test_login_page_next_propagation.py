"""``/login`` and ``/login/password`` must route ``?next=`` through
``safe_next_path`` instead of re-implementing the open-redirect rule.

``app/auth/_common.py::safe_next_path`` already knows one shape beyond a
same-origin relative path: an absolute URL on this deployment's own data-app
origin (``_is_own_data_app_origin`` — see
``test_safe_next_path_data_app_origin.py``). Hosted data apps are served from
their own subdomain, so a signed-out visitor opening an app is bounced to the
main host's ``/login`` carrying the app URL as an ABSOLUTE ``next``.

The login PAGE routes, though, had their own hand-rolled copy of the OLD
rule (same-origin relative path only) predating that guard, so an app-origin
``next`` was silently blanked before it ever reached ``safe_next_path`` —
the provider links rendered with no ``?next=`` at all, and after OAuth the
visitor landed on the home route instead of back in the app. This file pins
the rendered HTML, not just the helper, because the helper accepting the
shape is not the same as the route actually using the helper.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

BASE = "apps.example.com"
APP_ORIGIN = f"https://s.{BASE}/"


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def apps_on(monkeypatch):
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "true")
    monkeypatch.setenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", BASE)


# --- /login: provider links carry `next` -----------------------------------


def test_login_page_app_origin_next_survives_into_provider_link(web_client, apps_on):
    resp = web_client.get("/login", params={"next": APP_ORIGIN})
    assert resp.status_code == 200
    expected = f'href="/login/password?next={quote(APP_ORIGIN, safe="")}"'
    assert expected in resp.text, resp.text


def test_login_page_relative_next_survives_into_provider_link(web_client, apps_on):
    """Regression: the existing same-origin-path contract must not break."""
    resp = web_client.get("/login", params={"next": "/catalog"})
    assert resp.status_code == 200
    expected = f'href="/login/password?next={quote("/catalog", safe="")}"'
    assert expected in resp.text, resp.text


@pytest.mark.parametrize(
    "hostile",
    ["//evil.example/", "http://evil.example/", "javascript:alert(1)", "dashboard", ""],
)
def test_login_page_hostile_next_is_blanked(web_client, apps_on, hostile):
    resp = web_client.get("/login", params={"next": hostile})
    assert resp.status_code == 200
    assert 'href="/login/password?next=' not in resp.text, resp.text
    assert 'href="/login/password"' in resp.text, resp.text


def test_login_page_app_origin_next_blanked_when_data_apps_disabled(web_client, monkeypatch):
    monkeypatch.delenv("AGNES_DATA_APPS_ENABLED", raising=False)
    monkeypatch.delenv("AGNES_DATA_APPS_SUBDOMAIN_BASE", raising=False)
    resp = web_client.get("/login", params={"next": APP_ORIGIN})
    assert resp.status_code == 200
    assert 'href="/login/password?next=' not in resp.text, resp.text
    assert 'href="/login/password"' in resp.text, resp.text


# --- /login/password: hidden form field carries `next` ----------------------


def test_login_password_page_renders_app_origin_next_but_the_post_still_drops_it(web_client, apps_on):
    """Pins RENDERING only — the password journey is NOT fixed end to end.

    The form this renders POSTs to the password provider's web handler, which
    keeps its own copy of the pre-widening rule and replaces any non-`/` target
    with the home route. So a green assertion here does not mean a visitor
    signing in with a password lands back in the app: they do not.

    Named for what it proves rather than for what it looks like it proves,
    because that gap is precisely how this bug shipped — the previous test
    asserted the guard accepts the shape and was read as proof the route used
    the guard. Do not rename this to something reassuring without also fixing
    the POST handler.
    """
    resp = web_client.get("/login/password", params={"next": APP_ORIGIN})
    assert resp.status_code == 200
    assert f'name="next" value="{APP_ORIGIN}"' in resp.text, resp.text


def test_login_password_page_relative_next_survives_into_hidden_field(web_client, apps_on):
    resp = web_client.get("/login/password", params={"next": "/catalog"})
    assert resp.status_code == 200
    assert 'name="next" value="/catalog"' in resp.text, resp.text


@pytest.mark.parametrize(
    "hostile",
    ["//evil.example/", "http://evil.example/", "javascript:alert(1)", "dashboard", ""],
)
def test_login_password_page_hostile_next_is_blanked(web_client, apps_on, hostile):
    resp = web_client.get("/login/password", params={"next": hostile})
    assert resp.status_code == 200
    assert 'name="next" value=""' in resp.text, resp.text
