"""Tests for password reset + setup web flows (closes #34)."""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fresh_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        from src.db import close_system_db

        close_system_db()
        yield tmp
        close_system_db()


@pytest.fixture
def app_client(fresh_db, monkeypatch):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
    from app.main import app

    return TestClient(app, follow_redirects=False)


def _seed_user(
    email: str,
    *,
    password_hash: str | None = None,
    setup_token: str | None = None,
    setup_token_created: datetime | None = None,
    reset_token: str | None = None,
    reset_token_created: datetime | None = None,
    role: str = "analyst",
) -> str:
    """Create a user, return its id."""
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    from app.auth.token_hash import hash_token

    uid = str(uuid.uuid4())
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        repo.create(id=uid, email=email, name=email.split("@")[0], password_hash=password_hash)
        updates: dict = {}
        # Tokens are stored hashed at rest (audit M3); the caller passes the raw
        # value it will later submit, so seed its digest to mirror the mint path.
        if setup_token is not None:
            updates["setup_token"] = hash_token(setup_token)
        if setup_token_created is not None:
            updates["setup_token_created"] = setup_token_created
        if reset_token is not None:
            updates["reset_token"] = hash_token(reset_token)
        if reset_token_created is not None:
            updates["reset_token_created"] = reset_token_created
        if updates:
            repo.update(id=uid, **updates)
        return uid
    finally:
        conn.close()


def _get_user(email: str) -> dict:
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        return UserRepository(conn).get_by_email(email)
    finally:
        conn.close()


def _seed_admin() -> str:
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from app.auth.jwt import create_access_token
    from tests.helpers.auth import grant_admin

    conn = get_system_db()
    try:
        uid = str(uuid.uuid4())
        UserRepository(conn).create(id=uid, email="admin@test", name="Admin")
        grant_admin(conn, uid)
        return create_access_token(user_id=uid, email="admin@test")
    finally:
        conn.close()


# ---- GET pages ----


class TestResetGet:
    def test_renders_form_with_params(self, app_client, fresh_db):
        _seed_user("reset-me@test.com")
        resp = app_client.get("/auth/password/reset", params={"email": "reset-me@test.com", "token": "abc"})
        assert resp.status_code == 200
        assert "Reset Your Password" in resp.text
        # Hidden inputs with email + token are rendered
        assert 'name="email"' in resp.text
        assert 'value="reset-me@test.com"' in resp.text
        assert 'value="abc"' in resp.text

    def test_renders_request_form_without_params(self, app_client, fresh_db):
        """No token → the 'enter your email' request form, not a redirect.

        The login page's Forgot Password link lands here; before this page
        existed the only way to request a reset was a hidden-email POST from
        the login form, which silently submitted an empty address."""
        resp = app_client.get("/auth/password/reset")
        assert resp.status_code == 200
        assert 'name="email"' in resp.text
        assert 'action="/auth/password/reset"' in resp.text
        # It's the request form, not the set-new-password form.
        assert 'name="confirm_password"' not in resp.text

    def test_email_without_token_renders_request_form(self, app_client, fresh_db):
        resp = app_client.get("/auth/password/reset", params={"email": "someone@test.com"})
        assert resp.status_code == 200
        assert 'name="confirm_password"' not in resp.text
        assert 'value="someone@test.com"' in resp.text


class TestSetupGet:
    def test_renders_form_with_params(self, app_client, fresh_db):
        _seed_user("new@test.com")
        resp = app_client.get("/auth/password/setup", params={"email": "new@test.com", "token": "xyz"})
        assert resp.status_code == 200
        assert "Set Up Your Account" in resp.text
        assert 'value="new@test.com"' in resp.text
        assert 'value="xyz"' in resp.text

    def test_does_not_leak_user_existence_via_name_prefill(self, app_client, fresh_db):
        """GET /setup must render the same form whether the email exists or not,
        so an attacker can't enumerate users by watching the name field."""
        _seed_user("alice@test.com")  # seeded with name="alice" (derived from email)
        r_known = app_client.get("/auth/password/setup", params={"email": "alice@test.com", "token": "anything"})
        r_unknown = app_client.get("/auth/password/setup", params={"email": "ghost@test.com", "token": "anything"})
        assert r_known.status_code == 200 and r_unknown.status_code == 200
        # Seeded user's display name must NOT be pre-filled in the name input.
        assert 'value="alice"' not in r_known.text
        # The two responses should differ only by URL-reflected values (email).
        for body in (r_known.text, r_unknown.text):
            assert 'name="name"' in body  # the blank name input is always there

    def test_redirects_without_params(self, app_client, fresh_db):
        resp = app_client.get("/auth/password/setup")
        assert resp.status_code == 302


# ---- POST /auth/password/reset (request) ----


class TestResetRequest:
    def test_issues_token_for_existing_user(self, app_client, fresh_db):
        _seed_user("forgot@test.com", password_hash="argon2_placeholder")
        resp = app_client.post("/auth/password/reset", data={"email": "forgot@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("forgot@test.com")
        assert u["reset_token"]  # token was stored
        # audit M3: what's persisted is the sha256 digest, never a raw
        # token_urlsafe value — a DB read must not yield a usable reset token.
        stored = u["reset_token"]
        assert len(stored) == 64 and all(c in "0123456789abcdef" for c in stored), stored

    def test_unknown_email_same_response(self, app_client, fresh_db):
        # Anti-enumeration: should not reveal whether email is registered.
        resp = app_client.post("/auth/password/reset", data={"email": "ghost@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text

    def test_empty_email_reasks_instead_of_lying(self, app_client, fresh_db):
        """Empty submission: nothing could have been sent, so 'Check your
        email' would be false. Re-render the request form and ask for the
        address — an empty email reveals nothing, so anti-enumeration does
        not apply here."""
        resp = app_client.post("/auth/password/reset", data={"email": ""})
        assert resp.status_code == 200
        assert "Check your email" not in resp.text
        assert 'name="email"' in resp.text
        assert 'action="/auth/password/reset"' in resp.text

    def test_login_page_links_to_reset_page(self, app_client, fresh_db):
        """Forgot Password is a plain link to the request page — not a POST
        of a hidden email field that submits empty when the user clicks it
        before typing their address into the sign-in form."""
        resp = app_client.get("/login/password")
        assert resp.status_code == 200
        assert 'href="/auth/password/reset"' in resp.text
        assert 'id="reset-email"' not in resp.text


# ---- POST /auth/password/reset/confirm ----


class TestResetConfirm:
    def test_valid_token_sets_password_and_redirects(self, app_client, fresh_db):
        from argon2 import PasswordHasher

        _seed_user(
            "r1@test.com",
            password_hash=PasswordHasher().hash("oldpass123"),
            reset_token="tok-valid",
            reset_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "r1@test.com",
                "token": "tok-valid",
                "password": "brand-new-pwd",
                "confirm_password": "brand-new-pwd",
            },
        )
        assert resp.status_code == 302
        assert "password_reset" in resp.headers["location"]
        u = _get_user("r1@test.com")
        assert u["reset_token"] is None
        # New password must verify
        PasswordHasher().verify(u["password_hash"], "brand-new-pwd")

    def test_wrong_token_renders_error(self, app_client, fresh_db):
        _seed_user("r2@test.com", reset_token="tok-correct", reset_token_created=datetime.now(timezone.utc))
        resp = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "r2@test.com",
                "token": "tok-WRONG",
                "password": "abcdefgh",
                "confirm_password": "abcdefgh",
            },
        )
        assert resp.status_code == 200
        assert "Invalid or expired" in resp.text

    def test_expired_token_rejected(self, app_client, fresh_db):
        _seed_user("r3@test.com", reset_token="old", reset_token_created=datetime.now(timezone.utc) - timedelta(days=2))
        resp = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "r3@test.com",
                "token": "old",
                "password": "abcdefgh",
                "confirm_password": "abcdefgh",
            },
        )
        assert resp.status_code == 200
        assert "expired" in resp.text.lower()

    def test_password_mismatch(self, app_client, fresh_db):
        _seed_user("r4@test.com", reset_token="t", reset_token_created=datetime.now(timezone.utc))
        resp = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "r4@test.com",
                "token": "t",
                "password": "onething",
                "confirm_password": "another1",
            },
        )
        assert resp.status_code == 200
        assert "do not match" in resp.text

    def test_password_too_short(self, app_client, fresh_db):
        _seed_user("r5@test.com", reset_token="t", reset_token_created=datetime.now(timezone.utc))
        resp = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "r5@test.com",
                "token": "t",
                "password": "short",
                "confirm_password": "short",
            },
        )
        assert resp.status_code == 200
        assert "at least 8" in resp.text

    def test_concurrent_reset_only_one_wins(self, app_client, fresh_db):
        """Two concurrent reset/confirm POSTs — exactly one must succeed.

        Mirrors `test_concurrent_verify_only_one_wins` for the magic-link
        flow. Without the CAS pattern at `_atomic_consume_reset_token`,
        two concurrent POSTs with the same valid token could both write
        different new passwords for the same user (last-write-wins
        semantics). With the CAS, the loser gets the "Invalid or expired"
        form back instead of silently overwriting.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading

        _seed_user(
            "race@test.com",
            reset_token="race-tok",
            reset_token_created=datetime.now(timezone.utc),
        )

        results: list[tuple[int, str]] = []
        barrier = threading.Barrier(2, timeout=5)

        def confirm(payload_password: str):
            barrier.wait()  # both threads hit the endpoint at the same instant
            resp = app_client.post(
                "/auth/password/reset/confirm",
                data={
                    "email": "race@test.com",
                    "token": "race-tok",
                    "password": payload_password,
                    "confirm_password": payload_password,
                },
            )
            results.append((resp.status_code, resp.text))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(confirm, "winner-password"),
                pool.submit(confirm, "loser-password"),
            ]
            for f in as_completed(futures):
                f.result()

        # Exactly one 302 (winner — redirected to login) and one 200
        # (loser — got the reset form back with the standard error).
        redirects = [r for r in results if r[0] == 302]
        rejects = [r for r in results if r[0] == 200]
        assert len(redirects) == 1, f"Expected exactly 1 winner, got {len(redirects)} (results: {results})"
        assert len(rejects) == 1, f"Expected exactly 1 loser, got {len(rejects)} (results: {results})"
        assert "Invalid or expired" in rejects[0][1]


# ---- POST /auth/password/setup/request ----


class TestSetupRequest:
    def test_issues_token_for_pre_approved_user(self, app_client, fresh_db):
        _seed_user("invited@test.com")  # no password_hash
        resp = app_client.post("/auth/password/setup/request", data={"email": "invited@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("invited@test.com")
        assert u["setup_token"]

    def test_no_token_for_user_with_password(self, app_client, fresh_db):
        from argon2 import PasswordHasher

        _seed_user("already@test.com", password_hash=PasswordHasher().hash("x" * 10))
        resp = app_client.post("/auth/password/setup/request", data={"email": "already@test.com"})
        assert resp.status_code == 200  # anti-enumeration — same response
        u = _get_user("already@test.com")
        assert u["setup_token"] is None

    def test_unknown_email_same_response(self, app_client, fresh_db):
        resp = app_client.post("/auth/password/setup/request", data={"email": "who@test.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text


# ---- POST /auth/password/setup/confirm ----


class _BoomSMTP:
    """smtplib.SMTP stand-in whose connect always fails."""

    def __init__(self, *args, **kwargs):
        raise OSError("connection refused (test)")


class TestMailSendFailureIsSurfaced:
    """A configured SMTP transport that fails must not render the
    'Check your email' success page — the person would wait for a mail that
    was never sent and nothing would surface the misconfiguration."""

    def test_reset_request_send_failure_is_an_error(self, app_client, fresh_db, monkeypatch):
        _seed_user("forgot@test.com")
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = app_client.post("/auth/password/reset", data={"email": "forgot@test.com"})
        assert resp.status_code == 500
        assert "Check your email" not in resp.text

    def test_reset_request_unknown_email_keeps_generic_success(self, app_client, fresh_db, monkeypatch):
        """Anti-enumeration: no account → no send attempt → generic success."""
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = app_client.post("/auth/password/reset", data={"email": "ghost@test.com"})
        assert resp.status_code == 200

    def test_setup_request_send_failure_is_an_error(self, app_client, fresh_db, monkeypatch):
        _seed_user("invited@test.com")  # no password yet → pre-approved for setup
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = app_client.post("/auth/password/setup/request", data={"email": "invited@test.com"})
        assert resp.status_code == 500

    def test_setup_request_unknown_email_keeps_generic_success(self, app_client, fresh_db, monkeypatch):
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        monkeypatch.setattr("smtplib.SMTP", _BoomSMTP)
        resp = app_client.post("/auth/password/setup/request", data={"email": "who@test.com"})
        assert resp.status_code == 200


class TestSetupConfirm:
    def test_valid_token_sets_password_and_logs_in(self, app_client, fresh_db):
        _seed_user(
            "s1@test.com",
            setup_token="stok",
            setup_token_created=datetime.now(timezone.utc),
        )
        resp = app_client.post(
            "/auth/password/setup/confirm",
            data={
                "email": "s1@test.com",
                "token": "stok",
                "password": "new-password-x",
                "confirm_password": "new-password-x",
                "name": "Seth One",
            },
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        assert "access_token" in resp.cookies or "access_token" in resp.headers.get("set-cookie", "")
        u = _get_user("s1@test.com")
        assert u["setup_token"] is None
        assert u["name"] == "Seth One"
        from argon2 import PasswordHasher

        PasswordHasher().verify(u["password_hash"], "new-password-x")

    def test_expired_setup_token(self, app_client, fresh_db):
        _seed_user(
            "s2@test.com", setup_token="stok", setup_token_created=datetime.now(timezone.utc) - timedelta(days=10)
        )
        resp = app_client.post(
            "/auth/password/setup/confirm",
            data={
                "email": "s2@test.com",
                "token": "stok",
                "password": "abcdefgh",
                "confirm_password": "abcdefgh",
            },
        )
        assert resp.status_code == 200
        assert "expired" in resp.text.lower()

    def test_wrong_token(self, app_client, fresh_db):
        _seed_user("s3@test.com", setup_token="right", setup_token_created=datetime.now(timezone.utc))
        resp = app_client.post(
            "/auth/password/setup/confirm",
            data={
                "email": "s3@test.com",
                "token": "wrong",
                "password": "abcdefgh",
                "confirm_password": "abcdefgh",
            },
        )
        assert resp.status_code == 200
        assert "Invalid" in resp.text


# ---- Admin API: /api/users/{id}/reset-password, send_invite on create ----


class TestAdminInviteFlow:
    def test_reset_password_returns_reset_url(self, app_client, fresh_db):
        token = _seed_admin()
        _seed_user("target@test.com")
        target_id = _get_user("target@test.com")["id"]

        resp = app_client.post(
            f"/api/users/{target_id}/reset-password",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "reset_url" in data
        assert "/auth/password/reset" in data["reset_url"]
        assert "token=" in data["reset_url"]  # URL still contains the token
        assert "email_sent" in data
        assert data["email_sent"] is False  # no SMTP configured in tests
        assert "reset_token" not in data  # raw token must NOT be in response

    def test_create_user_with_send_invite_returns_invite_url(self, app_client, fresh_db):
        token = _seed_admin()
        resp = app_client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "new@test.com", "name": "New", "role": "analyst", "send_invite": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["invite_url"]
        assert "/auth/password/setup" in data["invite_url"]
        assert data["invite_email_sent"] is False
        # And setup_token is actually stored on the user
        u = _get_user("new@test.com")
        assert u["setup_token"]

    def test_create_user_without_invite_has_no_invite_url(self, app_client, fresh_db):
        token = _seed_admin()
        resp = app_client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "plain@test.com", "name": "Plain", "role": "analyst"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data.get("invite_url") is None
        assert data.get("invite_email_sent") is None


class TestJsonSetupHardening:
    """The JSON POST /auth/password/setup endpoint must enforce the same token
    TTL and active-account gate as the web flow."""

    def test_expired_token_rejected(self, app_client, fresh_db):
        _seed_user(
            "j1@test.com",
            setup_token="tok",
            setup_token_created=datetime.now(timezone.utc) - timedelta(days=10),
        )
        resp = app_client.post(
            "/auth/password/setup", json={"email": "j1@test.com", "token": "tok", "password": "long-enough-1"}
        )
        assert resp.status_code == 400
        assert "expired" in resp.json()["detail"].lower()

    def test_missing_created_timestamp_rejected(self, app_client, fresh_db):
        """A token row without setup_token_created is treated as invalid — we
        cannot verify its age, so it must fail closed."""
        _seed_user("j2@test.com", setup_token="tok")
        resp = app_client.post(
            "/auth/password/setup", json={"email": "j2@test.com", "token": "tok", "password": "long-enough-1"}
        )
        assert resp.status_code == 400

    def test_deactivated_user_rejected(self, app_client, fresh_db):
        uid = _seed_user(
            "j3@test.com",
            setup_token="tok",
            setup_token_created=datetime.now(timezone.utc),
        )
        # Flip user to inactive
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        try:
            UserRepository(conn).update(id=uid, active=False)
        finally:
            conn.close()

        resp = app_client.post(
            "/auth/password/setup", json={"email": "j3@test.com", "token": "tok", "password": "long-enough-1"}
        )
        assert resp.status_code == 403


class TestCaseInsensitiveEmailLookup:
    """Reset/setup requests resolve the account case-insensitively.

    They used to be an exact string match, which meant the person whose row an
    admin stored as ``User.Mixed@Example.com`` silently got no reset mail when
    they typed their address in lower case — indistinguishable, thanks to the
    anti-enumeration response, from "this address is not registered". Identity
    is one thing across the instance now: OAuth provisioning, password login
    and these flows all fold case (``get_by_email_ci``).
    """

    def test_reset_request_preserves_email_case(self, app_client, fresh_db):
        # User stored as-is with mixed-case local-part
        _seed_user("User.Mixed@Example.com", password_hash="x")
        # Caller submits the same exact case → token must be issued
        resp = app_client.post("/auth/password/reset", data={"email": "User.Mixed@Example.com"})
        assert resp.status_code == 200
        u = _get_user("User.Mixed@Example.com")
        assert u["reset_token"]

    def test_reset_request_matches_a_case_variant_address(self, app_client, fresh_db):
        _seed_user("User.Mixed@Example.com", password_hash="x")
        # Different case, same person: the token lands on the stored row. The
        # response is the anti-enumeration page either way.
        resp = app_client.post("/auth/password/reset", data={"email": "user.mixed@example.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("User.Mixed@Example.com")
        assert u["reset_token"]

    def test_reset_request_for_an_unknown_address_issues_nothing(self, app_client, fresh_db):
        """Case folding must not turn into a looser match — anti-enumeration
        still has to be backed by actually doing nothing."""
        _seed_user("User.Mixed@Example.com", password_hash="x")
        resp = app_client.post("/auth/password/reset", data={"email": "someone.else@example.com"})
        assert resp.status_code == 200
        assert "Check your email" in resp.text
        u = _get_user("User.Mixed@Example.com")
        assert u["reset_token"] is None


# ---- Issue #681: invite email delivery + copy button ----


class TestInviteEmailDelivery:
    """Invitation email must be sent when an email transport is configured (#681)."""

    def test_send_invite_calls_send_mail_when_smtp_configured(self, app_client, fresh_db, monkeypatch):
        """When SMTP_HOST is set, send_invite=True must call _send_mail and
        return invite_email_sent=True."""
        import app.auth.providers.password as pw_mod

        sent: list[tuple] = []

        def fake_send(to_email, subject, body, body_html=None):
            sent.append((to_email, subject, body))
            return True

        monkeypatch.setattr(pw_mod, "_send_mail", fake_send)
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")

        token = _seed_admin()
        resp = app_client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "invited@test.com", "name": "Invited", "send_invite": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["invite_url"]
        assert "/auth/password/setup" in data["invite_url"]
        assert data["invite_email_sent"] is True
        assert len(sent) == 1
        to, subject, body = sent[0]
        assert to == "invited@test.com"
        assert data["invite_url"] in body

    def test_send_invite_returns_false_when_no_smtp(self, app_client, fresh_db):
        """Without SMTP or SendGrid configured, invite_email_sent must be False
        and the link is still returned so the admin can share it manually."""
        token = _seed_admin()
        resp = app_client.post(
            "/api/users",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "noemail@test.com", "name": "NoEmail", "send_invite": True},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["invite_url"]
        assert data["invite_email_sent"] is False

    def test_admin_users_page_has_clipboard_fallback(self, app_client, fresh_db):
        """The copy button JS must include a fallback for non-secure contexts
        (plain HTTP) where navigator.clipboard may be undefined (#681)."""
        token = _seed_admin()
        resp = app_client.get(
            "/admin/users",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.text
        # Clipboard API used with fallback for non-secure contexts
        assert "navigator.clipboard" in body
        assert "execCommand" in body

    def test_invite_modal_shows_no_smtp_warning(self, app_client, fresh_db):
        """The invite modal must display a prominent note when email transport
        is not configured (#681)."""
        token = _seed_admin()
        resp = app_client.get(
            "/admin/users",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.text
        # The invite modal must have the transport-note element
        assert "invite-transport-note" in body
        # And the JS that populates it for the no-SMTP case
        assert "Email transport" in body


def _age_row(user_id: str, minutes: int) -> None:
    """Push a row's `created_at` back, so the two variants have a definite
    oldest — which is what `get_by_email_ci` tie-breaks on."""
    from src.db import get_system_db

    conn = get_system_db()
    try:
        conn.execute(
            f"UPDATE users SET created_at = now() - INTERVAL {int(minutes)} MINUTE WHERE id = ?",
            [user_id],
        )
    finally:
        conn.close()


class TestCredentialFollowsTheRowNotTheTieBreak:
    """`get_by_email_ci` answers "which account is this address" — oldest wins.
    That is right for provisioning and wrong for a credential check: on the
    instances the case-insensitive work exists for, two variants coexist and the
    credential may sit on the NEWER row. Checking only the oldest turned a
    working sign-in into `401`, and a valid invitation into "Invalid or expired
    setup link".
    """

    @staticmethod
    def _two_variants_password_on_newer(password: str = "correct-horse-battery"):
        """Older row: no hash. Newer row: the real password. Same address, two
        spellings — the shape that exists on instances predating normalization."""
        from argon2 import PasswordHasher

        old_id = _seed_user("ada@example.com")
        _age_row(old_id, 60)
        new_id = _seed_user("Ada@example.com", password_hash=PasswordHasher().hash(password))
        return old_id, new_id

    def test_json_login_finds_the_password_on_the_newer_variant(self, app_client, fresh_db):
        self._two_variants_password_on_newer()
        r = app_client.post(
            "/auth/password/login",
            json={"email": "ada@example.com", "password": "correct-horse-battery"},
        )
        assert r.status_code == 200, r.text

    def test_token_endpoint_finds_the_password_on_the_newer_variant(self, app_client, fresh_db):
        self._two_variants_password_on_newer()
        r = app_client.post(
            "/auth/token",
            json={"email": "ADA@example.com", "password": "correct-horse-battery"},
        )
        assert r.status_code == 200, r.text

    def test_web_form_login_finds_the_password_on_the_newer_variant(self, app_client, fresh_db):
        self._two_variants_password_on_newer()
        r = app_client.post(
            "/auth/password/login/web",
            data={"email": "ada@example.com", "password": "correct-horse-battery"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert "error=" not in (r.headers.get("location") or ""), r.headers.get("location")

    def test_a_wrong_password_is_still_refused_across_variants(self, app_client, fresh_db):
        """The scan must not become a way in — no row's hash matches, so the
        answer is the same generic 401 as before."""
        self._two_variants_password_on_newer()
        r = app_client.post(
            "/auth/password/login",
            json={"email": "ada@example.com", "password": "wrong-password"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid email or password"

    def test_a_deactivated_row_holding_the_password_is_still_refused(self, app_client, fresh_db):
        """Resolving by credential must not become an offboarding bypass: when
        the row that proves the password is the deactivated one, the answer is
        `Account deactivated`, not a session."""
        from argon2 import PasswordHasher

        from src.db import get_system_db

        old_id = _seed_user("bob@example.com")
        _age_row(old_id, 60)
        new_id = _seed_user("Bob@example.com", password_hash=PasswordHasher().hash("s3cret-pass-phrase"))
        conn = get_system_db()
        try:
            conn.execute("UPDATE users SET active = FALSE WHERE id = ?", [new_id])
        finally:
            conn.close()

        r = app_client.post(
            "/auth/password/login",
            json={"email": "bob@example.com", "password": "s3cret-pass-phrase"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == "Account deactivated"

    def test_setup_confirm_accepts_a_token_on_the_newer_variant(self, app_client, fresh_db):
        """An invitation is minted by user id, so it can land on the variant the
        oldest-wins lookup never returns."""
        old_id = _seed_user("cleo@example.com")
        _age_row(old_id, 60)
        _seed_user(
            "Cleo@example.com",
            setup_token="setup-tok-newer",
            setup_token_created=datetime.now(timezone.utc),
        )
        r = app_client.post(
            "/auth/password/setup/confirm",
            data={
                "email": "cleo@example.com",
                "token": "setup-tok-newer",
                "password": "brand-new-password",
                "confirm_password": "brand-new-password",
                "name": "Cleo",
            },
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), r.text

    def test_setup_json_accepts_a_token_on_the_newer_variant(self, app_client, fresh_db):
        old_id = _seed_user("dan@example.com")
        _age_row(old_id, 60)
        _seed_user(
            "Dan@example.com",
            setup_token="setup-tok-json",
            setup_token_created=datetime.now(timezone.utc),
        )
        r = app_client.post(
            "/auth/password/setup",
            json={
                "email": "dan@example.com",
                "token": "setup-tok-json",
                "password": "brand-new-password",
            },
        )
        assert r.status_code == 200, r.text

    def test_unknown_address_still_404s_on_setup(self, app_client, fresh_db):
        """The token scan must not turn "no such account" into "bad token" —
        the two are reported differently on this endpoint."""
        r = app_client.post(
            "/auth/password/setup",
            json={"email": "nobody@example.com", "token": "x", "password": "brand-new-password"},
        )
        assert r.status_code == 404


class TestADeactivatedIdentityShadowsEveryVariant:
    """Scanning case variants for the credential must not become a route around
    an offboarding.

    `get_by_email_ci` deliberately does not prefer an active row — its docstring
    and the 0.83.48 operator note both say a stale disabled variant shadowing
    the live account is the SAFE failure. So when the row that lookup resolves
    to is disabled, no sibling variant may serve the sign-in, however good its
    password is.

    The wider policy question (should ANY disabled variant refuse, even one that
    is not the resolved identity) is deliberately not answered here.
    """

    @staticmethod
    def _identity_deactivated_password_on_active_variant(password: str = "correct-horse-battery"):
        """Oldest row — the resolved identity — deactivated and password-less.
        Newer row active, carrying the real password."""
        from argon2 import PasswordHasher

        from src.db import get_system_db

        old_id = _seed_user("erin@example.com")
        _age_row(old_id, 60)
        new_id = _seed_user("Erin@example.com", password_hash=PasswordHasher().hash(password))
        conn = get_system_db()
        try:
            conn.execute("UPDATE users SET active = FALSE WHERE id = ?", [old_id])
        finally:
            conn.close()
        return old_id, new_id

    def test_json_login_refuses_when_the_resolved_identity_is_deactivated(self, app_client, fresh_db):
        self._identity_deactivated_password_on_active_variant()
        r = app_client.post(
            "/auth/password/login",
            json={"email": "erin@example.com", "password": "correct-horse-battery"},
        )
        assert r.status_code == 401, r.text
        assert "deactivated" in r.text.lower(), r.text

    def test_token_endpoint_refuses_when_the_resolved_identity_is_deactivated(self, app_client, fresh_db):
        self._identity_deactivated_password_on_active_variant()
        r = app_client.post(
            "/auth/token",
            json={"email": "Erin@example.com", "password": "correct-horse-battery"},
        )
        assert r.status_code == 401, r.text
        assert "deactivated" in r.text.lower(), r.text

    def test_the_refusal_still_requires_proving_a_credential(self, app_client, fresh_db):
        """The shadow is applied AFTER the password verifies, never before.
        Short-circuiting on the identity row's flag would answer "Account
        deactivated" to anyone who typed the address — an oracle for both the
        account's existence and its state."""
        self._identity_deactivated_password_on_active_variant()
        r = app_client.post(
            "/auth/password/login",
            json={"email": "erin@example.com", "password": "not-the-password"},
        )
        assert r.status_code == 401, r.text
        assert "deactivated" not in r.text.lower(), (
            "a wrong password revealed the account's deactivated state without proving anything"
        )

    def test_a_setup_link_on_a_variant_is_refused_when_the_identity_is_deactivated(self, app_client, fresh_db):
        from src.db import get_system_db

        old_id = _seed_user("fred@example.com")
        _age_row(old_id, 60)
        _seed_user(
            "Fred@example.com",
            setup_token="setup-tok-shadowed",
            setup_token_created=datetime.now(timezone.utc),
        )
        conn = get_system_db()
        try:
            conn.execute("UPDATE users SET active = FALSE WHERE id = ?", [old_id])
        finally:
            conn.close()
        r = app_client.post(
            "/auth/password/setup",
            json={
                "email": "fred@example.com",
                "token": "setup-tok-shadowed",
                "password": "brand-new-password",
            },
        )
        assert r.status_code == 403, r.text
        assert "deactivated" in r.text.lower(), r.text

    def test_a_reset_link_on_a_variant_is_refused_when_the_identity_is_deactivated(self, app_client, fresh_db):
        """One instance must not answer "deactivated" at the login form and hand
        out a session cookie on a reset link for the same pair of rows. The
        shadow applies on every door, judged after the token proved itself."""
        from datetime import datetime, timezone

        from src.db import get_system_db

        old_id = _seed_user("gina@example.com")
        _age_row(old_id, 60)
        _seed_user(
            "Gina@example.com",
            reset_token="reset-tok-shadowed",
            reset_token_created=datetime.now(timezone.utc),
        )
        conn = get_system_db()
        try:
            conn.execute("UPDATE users SET active = FALSE WHERE id = ?", [old_id])
        finally:
            conn.close()
        r = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "gina@example.com",
                "token": "reset-tok-shadowed",
                "password": "brand-new-password",
                "confirm_password": "brand-new-password",
            },
            follow_redirects=False,
        )
        assert r.status_code == 200, r.text
        assert "Invalid or expired reset link" in r.text, r.text[:400]
