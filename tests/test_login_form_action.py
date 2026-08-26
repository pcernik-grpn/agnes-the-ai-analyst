"""Pin the sign-in form action URL.

The e2e smoke helper (``scripts/e2e/_login.sh``) scopes its CSS selectors by
``form[action="/auth/password/login/web"]`` to disambiguate the Sign-In form
from the sibling Forgot-Password and Sign-Up forms in
``app/web/templates/login_email.html``. If anyone refactors the password
router prefix or the template's form action, smoke silently breaks until the
next nightly. This test pins the contract — same idea as the OpenAPI snapshot
test, but for the smoke-relevant HTML attribute.

If this test fails, either (a) ``scripts/e2e/_login.sh:LOGIN_FORM`` must move
in lockstep, or (b) the form action was changed inadvertently and should be
restored.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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


def test_password_login_form_action_pinned(web_client):
    """``form[action="/auth/password/login/web"]`` is the contract with smoke."""
    resp = web_client.get("/login/password")
    assert resp.status_code == 200
    html = resp.text
    # Exactly one occurrence — multiple would mean the form was duplicated
    # somewhere (login + signup forms have distinct actions in the template).
    assert html.count('action="/auth/password/login/web"') == 1, (
        "scripts/e2e/_login.sh pins this selector — keep it stable, or move "
        "the LOGIN_FORM constant in lockstep."
    )


def test_password_page_is_not_labelled_external_without_sso(web_client):
    """With no Google OAuth configured, `/login/password` is the ONLY door —
    every employee and the instance admin sign in here.

    It used to be headed "External User Access — For partners, investors, and
    other external users", which made first-time admins stop and check whether
    they had been given the wrong URL. The external-user framing is correct
    only when SSO is the primary path.
    """
    resp = web_client.get("/login/password")
    assert resp.status_code == 200
    html = resp.text
    assert "External User Access" not in html
    assert "For partners, investors" not in html
    assert "<h2>Sign In</h2>" in html
