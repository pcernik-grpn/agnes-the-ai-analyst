"""Force-SSO door tests: an address whose domain is in the enabled SSO
config's ``allowed_email_domains`` must be able to sign in ONLY through the
``sso`` door — never via password, never via magic link.

One test per entry point of the password and magic-link doors (the
acceptance criterion of the 2026-08-28 force-SSO hardening), each paired
with proof that the same door still opens for an address OUTSIDE the forced
domains. The pure predicate truth table lives in
``tests/test_auth_providers.py::TestSsoForcedForEmail``; PG-backed here
because the ``sso_config`` repo is PG-only (A3 ratchet).

Response posture (recorded in the PR body): credential/JSON endpoints answer
their existing generic refusal (anti-enumeration — indistinguishable from a
wrong credential or unknown token), browser-form and emailed-link legs
redirect to ``/auth/sso/login`` (the user already typed or received the
address, and the login page advertises the SSO button anyway).
"""

from __future__ import annotations

import re
import secrets as _secrets
from datetime import datetime, timezone

import pytest
from argon2 import PasswordHasher
from cryptography.fernet import Fernet

from app.auth.token_hash import hash_token
from tests.db_pg._parity_sweep_util import build_seeded_client

TENANT_GUID = "11111111-2222-3333-4444-555555555555"
FORCED_DOMAIN = "fabrikam.com"
OTHER_DOMAIN = "partner.example"

FORCED_EMAIL = f"worker@{FORCED_DOMAIN}"
FREE_EMAIL = f"free@{OTHER_DOMAIN}"
PASSWORD = "correct-horse-battery-9"


def _set_config(domains: list[str]) -> None:
    from src.repositories import sso_config_repo

    repo = sso_config_repo()
    repo.upsert_config(
        tenant_id=TENANT_GUID,
        client_id="app-client-id",
        display_name="Fabrikam",
        allowed_email_domains=domains,
        enabled=True,
        updated_by="admin1",
    )
    repo.set_client_secret("s3cret")


@pytest.fixture
def forced_env(tmp_path, monkeypatch, pg_engine):
    """PG client + enabled SSO config forcing ``fabrikam.com`` + two
    password holders: one inside the forced domain, one outside."""
    monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _set_config([FORCED_DOMAIN])

    from src.repositories import users_repo

    ph = PasswordHasher()
    u = users_repo()
    u.create(id="forced1", email=FORCED_EMAIL, name="Forced", password_hash=ph.hash(PASSWORD))
    u.create(id="free1", email=FREE_EMAIL, name="Free", password_hash=ph.hash(PASSWORD))
    return client, admin_token


def _mint_reset_token(user_id: str) -> str:
    """Persist a reset/magic-link token exactly the way the request legs do."""
    from src.repositories import users_repo

    tok = _secrets.token_urlsafe(32)
    users_repo().update(
        id=user_id,
        reset_token=hash_token(tok),
        reset_token_created=datetime.now(timezone.utc),
    )
    return tok


def _mint_setup_token(user_id: str) -> str:
    from src.repositories import users_repo

    tok = _secrets.token_urlsafe(32)
    users_repo().update(
        id=user_id,
        setup_token=hash_token(tok),
        setup_token_created=datetime.now(timezone.utc),
    )
    return tok


def _row(user_id: str) -> dict:
    from src.repositories import users_repo

    return users_repo().get_by_id(user_id)


# ---------------------------------------------------------------------------
# password door — login legs
# ---------------------------------------------------------------------------


def test_password_login_json_refused_for_forced_domain(forced_env):
    client, _ = forced_env
    r = client.post("/auth/password/login", json={"email": FORCED_EMAIL, "password": PASSWORD})
    # The CORRECT password must not open the door — and the refusal is the
    # same generic 401 a wrong password gets (no domain oracle on the
    # credential endpoint).
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid email or password"


def test_password_login_json_still_works_outside_forced_domains(forced_env):
    client, _ = forced_env
    r = client.post("/auth/password/login", json={"email": FREE_EMAIL, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_auth_token_refused_for_forced_domain(forced_env):
    client, _ = forced_env
    r = client.post("/auth/token", json={"email": FORCED_EMAIL, "password": PASSWORD})
    # This endpoint already answers "external authentication" for hash-less
    # accounts; a forced address gets the same copy.
    assert r.status_code == 401
    assert "external authentication" in r.json()["detail"]


def test_auth_token_still_works_outside_forced_domains(forced_env):
    client, _ = forced_env
    r = client.post("/auth/token", json={"email": FREE_EMAIL, "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_auth_token_unknown_forced_address_matches_unknown_elsewhere(forced_env):
    """An address with NO account must answer identically inside and outside
    the forced domains — the forcing refuses existing accounts, it must not
    become a domain-membership oracle for arbitrary strings."""
    client, _ = forced_env
    inside = client.post("/auth/token", json={"email": f"ghost@{FORCED_DOMAIN}", "password": PASSWORD})
    outside = client.post("/auth/token", json={"email": f"ghost@{OTHER_DOMAIN}", "password": PASSWORD})
    assert inside.status_code == outside.status_code == 401
    assert inside.json()["detail"] == outside.json()["detail"]


def test_password_login_web_redirects_forced_domain_to_sso(forced_env):
    client, _ = forced_env
    r = client.post(
        "/auth/password/login/web",
        data={"email": FORCED_EMAIL, "password": PASSWORD, "next": "/catalog"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sso/login?next=%2Fcatalog"
    assert "access_token" not in r.headers.get("set-cookie", "")


def test_password_login_web_still_works_outside_forced_domains(forced_env):
    client, _ = forced_env
    r = client.post(
        "/auth/password/login/web",
        data={"email": FREE_EMAIL, "password": PASSWORD, "next": ""},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "access_token" in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# password door — reset legs
# ---------------------------------------------------------------------------


def test_reset_request_forced_domain_redirects_and_mints_nothing(forced_env):
    client, _ = forced_env
    r = client.post("/auth/password/reset", data={"email": FORCED_EMAIL}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sso/login"
    # The hole this change closes: no reset token may be minted for a
    # forced address (an SSO-only account was previously handed one).
    assert _row("forced1").get("reset_token") is None


def test_reset_request_outside_forced_domains_still_mints(forced_env):
    client, _ = forced_env
    r = client.post("/auth/password/reset", data={"email": FREE_EMAIL}, follow_redirects=False)
    assert r.status_code == 200
    assert "Check your email" in r.text
    assert _row("free1").get("reset_token") is not None


def test_reset_page_with_link_redirects_forced_domain_to_sso(forced_env):
    client, _ = forced_env
    r = client.get(
        f"/auth/password/reset?email={FORCED_EMAIL}&token=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/sso/login"


def test_reset_page_with_link_still_renders_outside_forced_domains(forced_env):
    client, _ = forced_env
    r = client.get(
        f"/auth/password/reset?email={FREE_EMAIL}&token=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 200


def test_reset_confirm_refuses_link_minted_before_domain_was_forced(forced_env):
    """The redemption window: a reset link minted while the domain was NOT
    in the allowlist must not still redeem after the domain is added."""
    client, _ = forced_env
    _set_config([OTHER_DOMAIN])  # forced1's domain is momentarily un-forced
    tok = _mint_reset_token("forced1")
    hash_before = _row("forced1")["password_hash"]
    _set_config([FORCED_DOMAIN])  # ...and now the domain is forced

    r = client.post(
        "/auth/password/reset/confirm",
        data={
            "email": FORCED_EMAIL,
            "token": tok,
            "password": "brand-new-password-1",
            "confirm_password": "brand-new-password-1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sso/login"
    assert "access_token" not in r.headers.get("set-cookie", "")
    assert _row("forced1")["password_hash"] == hash_before


def test_reset_confirm_still_works_outside_forced_domains(forced_env):
    client, _ = forced_env
    tok = _mint_reset_token("free1")
    r = client.post(
        "/auth/password/reset/confirm",
        data={
            "email": FREE_EMAIL,
            "token": tok,
            "password": "brand-new-password-1",
            "confirm_password": "brand-new-password-1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "access_token" in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------------------
# password door — setup legs
# ---------------------------------------------------------------------------


def test_setup_page_with_link_redirects_forced_domain_to_sso(forced_env):
    client, _ = forced_env
    r = client.get(
        f"/auth/password/setup?email=newhire@{FORCED_DOMAIN}&token=whatever",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/sso/login"


def test_setup_request_forced_domain_redirects_and_mints_nothing(forced_env):
    client, _ = forced_env
    from src.repositories import users_repo

    users_repo().create(id="jit1", email=f"newhire@{FORCED_DOMAIN}", name="New Hire")
    r = client.post(
        "/auth/password/setup/request",
        data={"email": f"newhire@{FORCED_DOMAIN}"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sso/login"
    assert _row("jit1").get("setup_token") is None


def test_setup_confirm_refuses_invite_minted_before_domain_was_forced(forced_env):
    client, _ = forced_env
    from src.repositories import users_repo

    users_repo().create(id="jit2", email=f"invited@{FORCED_DOMAIN}", name="Invited")
    _set_config([OTHER_DOMAIN])
    tok = _mint_setup_token("jit2")
    _set_config([FORCED_DOMAIN])

    r = client.post(
        "/auth/password/setup/confirm",
        data={
            "email": f"invited@{FORCED_DOMAIN}",
            "token": tok,
            "password": "brand-new-password-1",
            "confirm_password": "brand-new-password-1",
            "name": "Invited",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sso/login"
    assert _row("jit2").get("password_hash") is None


def test_setup_json_refuses_invite_for_forced_domain(forced_env):
    client, _ = forced_env
    from src.repositories import users_repo

    users_repo().create(id="jit3", email=f"api-invited@{FORCED_DOMAIN}", name="Api Invited")
    tok = _mint_setup_token("jit3")

    r = client.post(
        "/auth/password/setup",
        json={"email": f"api-invited@{FORCED_DOMAIN}", "token": tok, "password": "brand-new-password-1"},
    )
    # Same generic copy an unknown token gets — no domain oracle.
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid setup token"
    assert _row("jit3").get("password_hash") is None


def test_setup_json_unknown_forced_address_matches_unknown_elsewhere(forced_env):
    """Same rule on the JSON setup leg: unknown addresses keep this
    endpoint's pre-existing unknown-address response (404) on both sides of
    the allowlist — no domain oracle for arbitrary strings."""
    client, _ = forced_env
    inside = client.post(
        "/auth/password/setup",
        json={"email": f"ghost@{FORCED_DOMAIN}", "token": "whatever", "password": "brand-new-password-1"},
    )
    outside = client.post(
        "/auth/password/setup",
        json={"email": f"ghost@{OTHER_DOMAIN}", "token": "whatever", "password": "brand-new-password-1"},
    )
    assert inside.status_code == outside.status_code == 404
    assert inside.json()["detail"] == outside.json()["detail"] == "User not found"


def test_setup_json_still_works_outside_forced_domains(forced_env):
    client, _ = forced_env
    from src.repositories import users_repo

    users_repo().create(id="inv1", email=f"invitee@{OTHER_DOMAIN}", name="Invitee")
    tok = _mint_setup_token("inv1")

    r = client.post(
        "/auth/password/setup",
        json={"email": f"invitee@{OTHER_DOMAIN}", "token": tok, "password": "brand-new-password-1"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


# ---------------------------------------------------------------------------
# password door — self-serve change (authenticated)
# ---------------------------------------------------------------------------


def _csrf_headers(client, session_token: str) -> dict:
    resp = client.get("/auth/password/change", headers={"Authorization": f"Bearer {session_token}"})
    assert resp.status_code == 200, resp.text
    m = re.search(r'data-csrf="([^"]+)"', resp.text)
    assert m, "no CSRF token embedded in the change-password page"
    return {"X-CSRF-Token": m.group(1)}


def test_change_page_shows_sso_state_for_forced_account(forced_env):
    client, _ = forced_env
    from app.auth.jwt import create_access_token

    token = create_access_token("forced1", FORCED_EMAIL)
    r = client.get("/auth/password/change", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    # The account HOLDS a hash, but the door is forced shut — the page must
    # render the single-sign-on state, not the change form. (The page's JS
    # block renders unconditionally, so probe for the form field itself.)
    assert "single sign-on" in r.text
    assert 'name="current_password"' not in r.text


def test_change_refused_for_forced_account_with_correct_password(forced_env):
    client, _ = forced_env
    from app.auth.jwt import create_access_token

    token = create_access_token("forced1", FORCED_EMAIL)
    csrf = _csrf_headers(client, token)
    r = client.post(
        "/auth/password/change",
        json={"current_password": PASSWORD, "new_password": "brand-new-password-1"},
        headers={"Authorization": f"Bearer {token}", **csrf},
    )
    assert r.status_code == 400
    assert "single sign-on" in r.json()["detail"]


def test_change_still_works_outside_forced_domains(forced_env):
    client, _ = forced_env
    from app.auth.jwt import create_access_token

    token = create_access_token("free1", FREE_EMAIL)
    csrf = _csrf_headers(client, token)
    r = client.post(
        "/auth/password/change",
        json={"current_password": PASSWORD, "new_password": "brand-new-password-1"},
        headers={"Authorization": f"Bearer {token}", **csrf},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# magic-link door
# ---------------------------------------------------------------------------


@pytest.fixture
def email_env(forced_env, monkeypatch):
    """The magic-link door is default-excluded while another door is usable,
    so name it explicitly (the forcing must hold even when an operator
    offers the email door on purpose)."""
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "sso,password,email")
    return forced_env


def test_send_link_json_forced_domain_answers_generic_and_mints_nothing(email_env):
    client, _ = email_env
    r = client.post("/auth/email/send-link", json={"email": FORCED_EMAIL})
    assert r.status_code == 200
    assert r.json()["message"] == "If this email is registered, you will receive a login link."
    assert _row("forced1").get("reset_token") is None


def test_send_link_json_still_mints_outside_forced_domains(email_env):
    client, _ = email_env
    r = client.post("/auth/email/send-link", json={"email": FREE_EMAIL})
    assert r.status_code == 200
    assert _row("free1").get("reset_token") is not None


def test_send_link_web_forced_domain_redirects_to_sso(email_env):
    client, _ = email_env
    r = client.post(
        "/auth/email/send-link/web",
        data={"email": FORCED_EMAIL, "next": "/catalog"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sso/login?next=%2Fcatalog"
    assert _row("forced1").get("reset_token") is None


def test_verify_json_refuses_link_minted_before_domain_was_forced(email_env):
    client, _ = email_env
    _set_config([OTHER_DOMAIN])
    tok = _mint_reset_token("forced1")
    _set_config([FORCED_DOMAIN])

    r = client.post("/auth/email/verify", json={"email": FORCED_EMAIL, "token": tok})
    # Same generic 401 an expired/unknown link gets.
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid or expired link"
    # The token was refused, not consumed.
    assert _row("forced1")["reset_token"] == hash_token(tok)


def test_verify_json_still_works_outside_forced_domains(email_env):
    client, _ = email_env
    tok = _mint_reset_token("free1")
    r = client.post("/auth/email/verify", json={"email": FREE_EMAIL, "token": tok})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_verify_get_redirects_forced_domain_to_sso_without_cookie(email_env):
    client, _ = email_env
    _set_config([OTHER_DOMAIN])
    tok = _mint_reset_token("forced1")
    _set_config([FORCED_DOMAIN])

    r = client.get(
        f"/auth/email/verify?email={FORCED_EMAIL}&token={tok}&next=/catalog",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/sso/login?next=%2Fcatalog"
    assert "access_token" not in r.headers.get("set-cookie", "")


def test_verify_get_still_works_outside_forced_domains(email_env):
    client, _ = email_env
    tok = _mint_reset_token("free1")
    r = client.get(
        f"/auth/email/verify?email={FREE_EMAIL}&token={tok}",
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert "access_token" in r.headers.get("set-cookie", "")
