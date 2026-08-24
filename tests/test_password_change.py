"""B6: self-serve change-password for a logged-in user.

``POST /auth/password/change`` — verifies the current password (argon2),
enforces the existing minimum-length policy, rate-limits like the other
password doors, writes an audit row, and on success clears any outstanding
reset/magic-link token (``users.reset_token`` is shared between the two
flows). Session-token only — a PAT must be rejected, matching every other
credential-minting/rotating door in this module
(``app.auth.dependencies.require_session_token``).
"""

from __future__ import annotations

import tempfile
import uuid

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


def _seed_user(email: str, *, password: str | None = None) -> str:
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    uid = str(uuid.uuid4())
    conn = get_system_db()
    try:
        password_hash = PasswordHasher().hash(password) if password else None
        UserRepository(conn).create(id=uid, email=email, name=email.split("@")[0], password_hash=password_hash)
        return uid
    finally:
        conn.close()


def _session_token(user_id: str, email: str) -> str:
    from app.auth.jwt import create_access_token

    return create_access_token(user_id, email)


def _mint_pat(client: TestClient, session_token: str) -> str:
    resp = client.post(
        "/auth/tokens",
        json={"name": "test-pat"},
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _csrf_headers_and_cookies(client: TestClient, session_token: str) -> dict:
    """GET the change-password page to mint the double-submit CSRF cookie,
    return the header a subsequent POST must carry. The cookie itself rides
    the client's own cookie jar (TestClient persists Set-Cookie across
    requests on the same instance)."""
    resp = client.get("/auth/password/change", headers=_auth(session_token))
    assert resp.status_code == 200, resp.text
    import re

    m = re.search(r'data-csrf="([^"]+)"', resp.text)
    assert m, f"no CSRF token embedded in the change-password page: {resp.text[:500]}"
    return {"X-CSRF-Token": m.group(1)}


class TestPasswordChange:
    def test_wrong_current_password_403_and_audited(self, app_client, fresh_db):
        uid = _seed_user("wrong-cur@test.com", password="correct-horse-battery-staple")
        token = _session_token(uid, "wrong-cur@test.com")
        csrf = _csrf_headers_and_cookies(app_client, token)

        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "not-the-password", "new_password": "brand-new-password-1"},
            headers={**_auth(token), **csrf},
        )
        assert resp.status_code == 403, resp.text

        from src.repositories import audit_repo

        rows, _ = audit_repo().query(user_id=uid, action_in=["password_change_failed"])
        assert rows, "expected an audit row for the failed change attempt"

    def test_success_old_password_refused_new_accepted(self, app_client, fresh_db):
        uid = _seed_user("change-me@test.com", password="original-password-123")
        token = _session_token(uid, "change-me@test.com")
        csrf = _csrf_headers_and_cookies(app_client, token)

        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "original-password-123", "new_password": "brand-new-password-456"},
            headers={**_auth(token), **csrf},
        )
        assert resp.status_code == 200, resp.text

        old = app_client.post(
            "/auth/password/login",
            json={"email": "change-me@test.com", "password": "original-password-123"},
        )
        assert old.status_code == 401, old.text

        new = app_client.post(
            "/auth/password/login",
            json={"email": "change-me@test.com", "password": "brand-new-password-456"},
        )
        assert new.status_code == 200, new.text

    def test_outstanding_reset_token_invalidated_after_change(self, app_client, fresh_db):
        from datetime import datetime, timezone

        from app.auth.token_hash import hash_token
        from src.repositories import users_repo

        uid = _seed_user("has-reset-token@test.com", password="original-password-123")
        users_repo().update(
            id=uid,
            reset_token=hash_token("stale-reset-tok"),
            reset_token_created=datetime.now(timezone.utc),
        )
        token = _session_token(uid, "has-reset-token@test.com")
        csrf = _csrf_headers_and_cookies(app_client, token)

        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "original-password-123", "new_password": "brand-new-password-456"},
            headers={**_auth(token), **csrf},
        )
        assert resp.status_code == 200, resp.text

        row = users_repo().get_by_id(uid)
        assert row.get("reset_token") is None, "reset_token must be cleared after a self-serve change"

        # The stale token itself must be unusable now (belt and suspenders on
        # top of the direct row check above).
        confirm = app_client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "has-reset-token@test.com",
                "token": "stale-reset-tok",
                "password": "another-password-789",
                "confirm_password": "another-password-789",
            },
        )
        assert "Invalid or expired" in confirm.text

    def test_pat_authenticated_call_is_403(self, app_client, fresh_db):
        uid = _seed_user("pat-user@test.com", password="original-password-123")
        session_tok = _session_token(uid, "pat-user@test.com")
        pat = _mint_pat(app_client, session_tok)

        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "original-password-123", "new_password": "brand-new-password-456"},
            headers=_auth(pat),
        )
        assert resp.status_code == 403, resp.text

    def test_no_password_hash_user_gets_400_sso_message(self, app_client, fresh_db):
        # The GET page mints web_csrf unconditionally — including for an
        # SSO-only account with no form to submit (Devin Review on PR
        # #1548) — so a caller who did fetch it still reaches the 400.
        uid = _seed_user("google-only@test.com", password=None)
        token = _session_token(uid, "google-only@test.com")
        csrf = _csrf_headers_and_cookies(app_client, token)

        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "anything", "new_password": "brand-new-password-456"},
            headers={**_auth(token), **csrf},
        )
        assert resp.status_code == 400, resp.text
        assert "sso" in resp.text.lower() or "single sign-on" in resp.text.lower()

    def test_no_password_hash_user_change_page_has_no_form(self, app_client, fresh_db):
        uid = _seed_user("google-only-page@test.com", password=None)
        token = _session_token(uid, "google-only-page@test.com")
        resp = app_client.get("/auth/password/change", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        assert 'id="pwchange-form"' not in resp.text
        assert "single sign-on" in resp.text

    def test_csrf_required(self, app_client, fresh_db):
        uid = _seed_user("csrf-check@test.com", password="original-password-123")
        token = _session_token(uid, "csrf-check@test.com")
        # No GET first — no web_csrf cookie, no header.
        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "original-password-123", "new_password": "brand-new-password-456"},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text

    def test_csrf_checked_before_no_password_hash_no_response_shape_leak(self, app_client, fresh_db):
        """CSRF must be checked FIRST, before the no-password-hash lookup,
        so a caller with no CSRF token gets the SAME 403 regardless of
        whether the target account is SSO-only or password-based — the
        ordering the pre-fix code got backwards (Devin Review on PR #1548):
        SSO-only got 400, password accounts got 403, letting the status
        code alone distinguish account type without proving anything."""
        uid = _seed_user("google-only-csrf@test.com", password=None)
        token = _session_token(uid, "google-only-csrf@test.com")
        # No GET first — no web_csrf cookie, no header.
        resp = app_client.post(
            "/auth/password/change",
            json={"current_password": "anything", "new_password": "brand-new-password-456"},
            headers=_auth(token),
        )
        assert resp.status_code == 403, resp.text
        assert "sso" not in resp.text.lower()


class TestPasswordChangeRateLimit:
    def test_rate_limit_triggers_on_hammering(self, fresh_db, monkeypatch):
        monkeypatch.setenv("TESTING", "1")
        monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret-key-minimum-32-chars!!")
        monkeypatch.setenv("AGNES_AUTH_RATELIMIT_ENABLED", "1")
        from app.auth.rate_limit import limiter

        limiter.enabled = True
        limiter.reset()
        from app.main import app

        client = TestClient(app, follow_redirects=False)
        uid = _seed_user("hammered@test.com", password="original-password-123")
        token = _session_token(uid, "hammered@test.com")
        csrf = _csrf_headers_and_cookies(client, token)

        statuses = []
        for _ in range(6):
            resp = client.post(
                "/auth/password/change",
                json={"current_password": "wrong", "new_password": "brand-new-password-456"},
                headers={**_auth(token), **csrf},
            )
            statuses.append(resp.status_code)
        assert statuses[-1] == 429, f"expected the last request to 429, got {statuses}"
