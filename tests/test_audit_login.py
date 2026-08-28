"""A completed sign-in leaves an audit_log row — for every provider (TCRD-220).

Until this landed, `login_failed` was the only authentication event the trail
carried: an instance whose people sign in through Google had no record that
anyone had ever signed in at all, and the invited → activated → signed-in
lifecycle was recorded only at its first step.

The per-provider tests below assert the behaviour where the flow can be driven
end to end. `test_every_cookie_minting_provider_audits_its_success_path` is the
part that keeps this from rotting: it walks the provider package rather than a
hand-maintained list, so a seventh provider cannot ship its success path
silently the way the first six did.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.token_hash import hash_token
from src.db import get_system_db


def _audit_rows(action):
    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT user_id, params, result FROM audit_log WHERE action = ? ORDER BY timestamp DESC",
            [action],
        ).fetchall()
    finally:
        conn.close()
    return [{"user_id": r[0], "params": json.loads(r[1]) if r[1] else {}, "result": r[2]} for r in rows]


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google,email,password,keboola,microsoft")

    from src.repositories.users import UserRepository

    conn = get_system_db()
    ur = UserRepository(conn)
    try:
        from argon2 import PasswordHasher

        pw_hash = PasswordHasher().hash("testpass123")
    except ImportError:
        import hashlib

        pw_hash = hashlib.sha256(b"testpass123").hexdigest()

    ur.create(id="pw1", email="pw@test.com", name="PW User", password_hash=pw_hash)
    ur.create(id="invitee1", email="invitee@test.com", name="Invitee")
    ur.update(
        id="invitee1",
        setup_token=hash_token("setup-token-123"),
        setup_token_created=datetime.now(timezone.utc),
    )
    ur.create(id="pending1", email="pending@test.com", name="Pending")
    # A second invitee, for the JSON /setup sibling of the web form above.
    ur.create(id="invitee2", email="invitee-json@test.com", name="Invitee JSON")
    ur.update(
        id="invitee2",
        setup_token=hash_token("setup-token-json"),
        setup_token_created=datetime.now(timezone.utc),
    )
    # An existing account that asked for a password reset.
    ur.create(id="resetter1", email="resetter@test.com", name="Resetter", password_hash=pw_hash)
    ur.update(
        id="resetter1",
        reset_token=hash_token("reset-token-123"),
        reset_token_created=datetime.now(timezone.utc),
    )
    conn.close()

    return TestClient(shared_app)


class TestSignInIsRecorded:
    def test_web_form_login_writes_a_login_success_row(self, client):
        """The browser path: the one people actually use, and the one that
        set a session cookie without leaving a trace."""
        resp = client.post(
            "/auth/password/login/web",
            data={"email": "pw@test.com", "password": "testpass123"},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text

        rows = _audit_rows("login_success")
        assert len(rows) == 1, "a successful web login must write exactly one audit row"
        assert rows[0]["user_id"] == "pw1"
        assert rows[0]["params"].get("provider") == "password"
        assert rows[0]["result"] == "success"

    def test_json_login_writes_a_login_success_row(self, client):
        """The programmatic path (CLI/desktop). Same event, different client
        kind — an audit trail that called this a browser session would
        misrepresent a non-interactive credential."""
        resp = client.post(
            "/auth/password/login",
            json={"email": "pw@test.com", "password": "testpass123"},
        )
        assert resp.status_code == 200, resp.text

        rows = _audit_rows("login_success")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "pw1"
        assert rows[0]["params"].get("provider") == "password"

    def test_failed_login_still_writes_only_the_failure(self, client):
        """The pre-existing `login_failed` row is the reason this file can
        assert on counts at all; prove the new row does not fire on the
        failure branch too."""
        resp = client.post(
            "/auth/password/login/web",
            data={"email": "pw@test.com", "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        assert _audit_rows("login_success") == []
        assert len(_audit_rows("login_failed")) == 1


class TestOAuthSignInIsRecorded:
    """The structural guard below proves every provider *mentions* the helper.
    This proves one of them actually reaches it at runtime, so a dead import
    could not satisfy the guard on its own.

    Keboola is the provider whose callback can be driven end to end without a
    live identity provider (`tests/test_keboola_oauth_provider.py` established
    the harness); Google and Microsoft share the same success-path shape.
    """

    @pytest.fixture
    def oauth_client(self, tmp_path, monkeypatch, shared_app):
        from app.auth.providers import keboola as kb
        from app.auth.providers import keboola_verify as kv

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "keboola")
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        monkeypatch.setattr(kv, "configured_project_id", lambda: "12345")
        monkeypatch.setattr(kv, "client_id", lambda: "cid")
        monkeypatch.setattr(kv, "client_secret", lambda: "csecret")

        async def fake_authorize_access_token(request):
            return {"access_token": "at-123"}

        class FakeApp:
            authorize_access_token = staticmethod(fake_authorize_access_token)

        monkeypatch.setattr(kb, "_oauth_client", lambda: FakeApp())
        monkeypatch.setattr("app.api.admin._validate_url_not_private", lambda url, field_name="url": None)
        monkeypatch.setattr(
            kv,
            "verify_oauth_access_token",
            lambda tok: kv.VerifiedKeboolaIdentity(
                token_id="204",
                project_id="12345",
                project_name="Acme DWH",
                email="jane@example.com",
                name="Jane",
                role="admin",
            ),
        )
        return TestClient(shared_app)

    def test_oauth_callback_writes_a_login_success_row(self, oauth_client):
        resp = oauth_client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302, resp.text
        assert "access_token" in resp.cookies

        rows = _audit_rows("login_success")
        assert len(rows) == 1, "an OAuth sign-in must write exactly one audit row"
        assert rows[0]["params"].get("provider") == "keboola"


class TestInviteLifecycleIsRecorded:
    """`user.invite` was already logged. Everything after it was not, so the
    trail said a person had been invited and never showed them arriving."""

    def test_activating_an_invite_is_recorded(self, client):
        resp = client.post(
            "/auth/password/setup/confirm",
            data={
                "email": "invitee@test.com",
                "token": "setup-token-123",
                "password": "brand-new-pass-1",
                "confirm_password": "brand-new-pass-1",
                "name": "Invitee",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302, 303), resp.text

        rows = _audit_rows("account_activated")
        assert len(rows) == 1, "activating an account from an invite must be recorded"
        assert rows[0]["user_id"] == "invitee1"

    def test_completing_a_password_reset_is_recorded(self, client):
        """A finished reset signs the person straight back in — same session
        the login routes mint — so it owes the trail the same row. It wrote
        nothing (Devin Review on this PR); the module-level provider walk
        missed it because password.py already audited elsewhere."""
        resp = client.post(
            "/auth/password/reset/confirm",
            data={
                "email": "resetter@test.com",
                "token": "reset-token-123",
                "password": "reset-new-pass-1",
                "confirm_password": "reset-new-pass-1",
            },
            follow_redirects=False,
        )
        # A rejected reset re-renders the form with 200; only the redirect
        # means the password was actually changed and the cookie set.
        assert resp.status_code in (302, 303), resp.text

        rows = _audit_rows("login_success")
        assert [r["user_id"] for r in rows] == ["resetter1"], rows
        assert rows[0]["params"]["provider"] == "password"

    def test_json_setup_route_records_activation_and_sign_in(self, client):
        """Its web sibling writes two rows; this one returned a bearer token
        and wrote none. Handing back a credential is a completed sign-in, and
        `client_kind` must say `cli` — calling a non-interactive token grant a
        browser session would misrepresent it."""
        resp = client.post(
            "/auth/password/setup",
            json={"email": "invitee-json@test.com", "token": "setup-token-json", "password": "json-new-pass-1"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_token"]

        activated = _audit_rows("account_activated")
        assert [r["user_id"] for r in activated] == ["invitee2"], activated

        signed_in = _audit_rows("login_success")
        assert [r["user_id"] for r in signed_in] == ["invitee2"], signed_in

    def test_self_service_setup_request_is_recorded(self, client):
        """It mints a setup token and sends mail. Anti-enumeration means the
        response is identical either way, so the audit row is the only place
        the real outcome is visible."""
        resp = client.post(
            "/auth/password/setup/request",
            data={"email": "pending@test.com"},
            follow_redirects=False,
        )
        assert resp.status_code in (200, 302, 303), resp.text

        rows = _audit_rows("setup_link_requested")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "pending1"


class TestUserManagementAuditFailuresAreVisible:
    def test_a_failing_audit_write_is_logged_not_swallowed(self, monkeypatch, caplog):
        """`app/api/users.py` swallowed every audit exception with a bare
        `pass` and no log line, unlike every other audit helper in the tree.
        A trail that can lose rows silently is worse than one with a known
        gap, because nothing anywhere says a row went missing."""
        import logging

        from app.api import users as users_api

        def boom(**_kwargs):
            raise RuntimeError("audit backend down")

        monkeypatch.setattr("src.repositories.audit_repo", lambda: type("R", (), {"log": boom})())

        with caplog.at_level(logging.ERROR):
            users_api._audit(None, "actor", "user.invite", "target", {"email": "x@y.z"})

        assert caplog.records, "a dropped audit row must leave something in the application log"


def test_every_cookie_minting_provider_audits_its_success_path():
    """Walk the package instead of listing providers by hand.

    Every module that mints a session cookie is claiming someone signed in,
    so it owes the trail a row. Discovering them by source means the next
    provider is caught the day it is added, which is exactly what did not
    happen for the five that shipped without auditing anything.
    """
    providers = Path("app/auth/providers")
    offenders = []
    for path in sorted(providers.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        mints_cookie = "_set_login_cookie" in source or 'key="access_token"' in source
        if not mints_cookie:
            continue
        if "login_audit" not in source:
            offenders.append(path.name)

    assert not offenders, (
        "these providers set a login cookie without recording it: "
        f"{offenders}. Import audit_login_success from app.auth.login_audit "
        "and call it on the success path."
    )


def test_every_session_minting_ROUTE_audits_its_own_success_path():
    """Per-callsite, not per-module — the module-level check above passes the
    moment a file imports `login_audit` even once, so a provider that audits
    four of its six sign-in routes looks clean. That is exactly what happened:
    `reset_confirm` and the JSON `password_setup` both completed a sign-in
    (one sets the cookie, the other returns a bearer token) with nothing in
    the trail, inside a module the walker above was already happy with.

    So: every FUNCTION that mints a session must audit inside that same
    function. `create_access_token` is included because handing a token back
    to a client is as much a completed sign-in as setting a cookie is.
    """
    import ast

    MINTERS = {"_set_login_cookie", "create_access_token"}
    AUDITORS = {"audit_login_success", "audit_auth_event"}

    def called_names(node):
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    out.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    out.add(fn.attr)
        return out

    offenders = []
    for path in sorted(Path("app/auth/providers").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = called_names(node)
            # `_set_login_cookie` itself is the helper, not a sign-in route.
            if node.name.startswith("_"):
                continue
            if names & MINTERS and not (names & AUDITORS):
                offenders.append(f"{path.name}::{node.name}")

    assert not offenders, (
        "these routes complete a sign-in without recording it: "
        f"{offenders}. Call audit_login_success (and audit_auth_event with "
        "ACCOUNT_ACTIVATED when an invite is being consumed) on the success "
        "path of each."
    )
