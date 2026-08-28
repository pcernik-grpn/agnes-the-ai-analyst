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


def test_login_password_page_renders_app_origin_next(web_client, apps_on):
    """Pins RENDERING — the first half of the password journey.

    This was named ``..._but_the_post_still_drops_it`` and its docstring said
    not to rename it to anything reassuring without also fixing the POST
    handler, because a green assertion on the rendered form was exactly what
    would otherwise read as proof the journey worked. The handler is fixed in
    this PR (``test_password_form_post_returns_the_visitor_to_the_app_origin``
    below drives the real POST), so the warning has been earned out and the
    name no longer has to carry it.
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


# --- the terminal consumer: the password form's own POST handler -----------


def _seed_password_user(email: str, user_id: str, password: str) -> None:
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(
        id=user_id,
        email=email,
        name="Pw User",
        password_hash=PasswordHasher().hash(password),
    )
    conn.close()


def test_password_form_post_returns_the_visitor_to_the_app_origin(web_client, apps_on):
    """The login PAGE carrying `next` is only half the journey — the form it
    renders POSTs to `/auth/password/login/web`, which held a THIRD copy of the
    old relative-only rule. So a visitor bounced out of a data app, signing in
    with a password, still landed on the home route while OAuth and magic-link
    returned them to the app. That handler calls `safe_next_path` now.
    """
    pw = "TestPass1!"
    _seed_password_user("pwnext@test.com", "pw_next_1", pw)
    resp = web_client.post(
        "/auth/password/login/web",
        data={"email": "pwnext@test.com", "password": pw, "next": APP_ORIGIN},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == APP_ORIGIN, resp.headers


@pytest.mark.parametrize("hostile", ["//evil.example/", "http://evil.example/", "javascript:alert(1)", "dashboard"])
def test_password_form_post_still_refuses_a_hostile_next(web_client, apps_on, hostile):
    """The open-redirect guard is the half that must NOT loosen."""
    pw = "TestPass1!"
    _seed_password_user(f"pwh{abs(hash(hostile)) % 10000}@test.com", f"pw_h_{abs(hash(hostile)) % 10000}", pw)
    resp = web_client.post(
        "/auth/password/login/web",
        data={
            "email": f"pwh{abs(hash(hostile)) % 10000}@test.com",
            "password": pw,
            "next": hostile,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] != hostile, resp.headers
    assert resp.headers["location"].startswith("/"), resp.headers
