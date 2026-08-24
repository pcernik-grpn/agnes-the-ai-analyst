"""Keboola OAuth web login: availability, redirect, callback outcomes."""

import pytest
from fastapi.testclient import TestClient

from app.auth.providers import keboola_verify as kv


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: "12345")
    monkeypatch.setattr(kv, "client_id", lambda: "cid")
    monkeypatch.setattr(kv, "client_secret", lambda: "csecret")

    return TestClient(shared_app)


def _identity(email="jane@example.com"):
    return kv.VerifiedKeboolaIdentity(
        token_id="204",
        project_id="12345",
        project_name="Acme DWH",
        email=email,
        name="Jane",
        role="admin",
    )


class TestLoginRoute:
    def test_redirects_to_oauth_host(self, client):
        resp = client.get("/auth/keboola/login", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"].startswith("https://connection.example.com/oauth/authorize")

    def test_unconfigured_redirects_to_login_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setattr(kv, "client_id", lambda: "")
        from app.main import create_app

        c = TestClient(create_app())
        resp = c.get("/auth/keboola/login", follow_redirects=False)
        assert resp.status_code == 307 or resp.status_code == 302
        assert "error=keboola_not_configured" in resp.headers["location"]


class TestCallback:
    def _patch_flow(self, monkeypatch, identity=None, verify_error=None):
        from app.auth.providers import keboola as kb

        async def fake_authorize_access_token(request):
            return {"access_token": "at-123"}

        class FakeApp:
            authorize_access_token = staticmethod(fake_authorize_access_token)

        monkeypatch.setattr(kb, "_oauth_client", lambda: FakeApp())
        # oauth_host() here is the fixture's fake "connection.example.com",
        # which doesn't resolve via real DNS. These tests exercise the
        # post-exchange flow, not the SSRF gate itself (that has its own
        # dedicated test below) — stub the shared validator permissive,
        # same pattern as tests/test_keboola_verify.py.
        monkeypatch.setattr("app.api.admin._validate_url_not_private", lambda url, field_name="url": None)
        if verify_error is not None:

            def boom(tok):
                raise verify_error

            monkeypatch.setattr(kv, "verify_oauth_access_token", boom)
        else:
            monkeypatch.setattr(kv, "verify_oauth_access_token", lambda tok: identity or _identity())

    def test_happy_path_provisions_and_sets_cookie(self, client, monkeypatch):
        self._patch_flow(monkeypatch)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies
        from src.repositories import users_repo

        assert users_repo().get_by_email("jane@example.com") is not None

    def test_project_mismatch_redirects_with_error(self, client, monkeypatch):
        self._patch_flow(monkeypatch, verify_error=kv.KeboolaVerifyError("project_mismatch", "wrong project"))
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_project_mismatch" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_oauth_non_master_redirects_with_its_own_error_code(self, client, monkeypatch):
        """The login path's master-token failure carries its own reason
        (`oauth_not_master_token` — the platform assumption that interactive
        OAuth tokens are master tokens is unverified), and the callback must
        surface it as a distinct error code rather than folding it into the
        generic not-permitted banner (Devin Review on PR #1288)."""
        self._patch_flow(monkeypatch, verify_error=kv.KeboolaVerifyError("oauth_not_master_token", "unexpected"))
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_oauth_not_master" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_unexpected_verify_exception_redirects_not_500(self, client, monkeypatch):
        """Non-KeboolaVerifyError from verify must hit the backstop, not a 500."""
        self._patch_flow(monkeypatch, verify_error=AttributeError("boom"))
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_oauth_failed" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_unexpected_provisioning_exception_redirects_not_500(self, client, monkeypatch):
        """Non-UserDeactivatedError from ensure_user must hit the backstop, not a 500."""
        from app.auth.providers import keboola as kb

        self._patch_flow(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("db exploded")

        # The provider imports ensure_user by name; patch it on the provider module.
        monkeypatch.setattr(kb, "ensure_user", boom)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_oauth_failed" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_oauth_host_ssrf_rejected_before_token_exchange(self, client, monkeypatch):
        """oauth_host is re-validated at use time (not just store time): a
        private/loopback host must fail closed before the token exchange
        ever runs, exactly like stack_url in keboola_verify._fetch_verify."""
        from app.auth.providers import keboola as kb

        monkeypatch.setattr(kv, "oauth_host", lambda: "http://169.254.169.254")

        called = {"exchange": False}

        async def fake_authorize_access_token(request):
            called["exchange"] = True
            return {"access_token": "at-123"}

        class FakeApp:
            authorize_access_token = staticmethod(fake_authorize_access_token)

        monkeypatch.setattr(kb, "_oauth_client", lambda: FakeApp())
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_oauth_failed" in resp.headers["location"]
        assert "access_token" not in resp.cookies
        assert called["exchange"] is False

    def test_deactivated_user_rejected(self, client, monkeypatch):
        from src.repositories import users_repo
        import uuid

        uid = str(uuid.uuid4())
        users_repo().create(id=uid, email="jane@example.com", name="Jane")
        users_repo().update(id=uid, active=False)
        self._patch_flow(monkeypatch)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=deactivated" in resp.headers["location"]


class TestOAuthClientRegistration:
    """authlib's registry caches the client object at first use; without a
    config fingerprint a later server-config edit (secret rotation, oauth_host
    change) would be ignored until restart while is_available() reads live
    (Devin Review on PR #1288)."""

    def _configure(self, monkeypatch, suffix, host="https://connection.example.com"):
        monkeypatch.setattr(kv, "client_id", lambda: f"cid-{suffix}")
        monkeypatch.setattr(kv, "client_secret", lambda: f"cs-{suffix}")
        monkeypatch.setattr(kv, "oauth_host", lambda: host)

    def test_config_edit_reaches_the_client_without_restart(self, monkeypatch):
        from app.auth.providers import keboola as kb

        self._configure(monkeypatch, "one")
        assert kb._oauth_client().client_id == "cid-one"
        # Rotate the OAuth client credentials (a runtime server-config edit).
        self._configure(monkeypatch, "two")
        client = kb._oauth_client()
        assert client.client_id == "cid-two"
        assert client.client_secret == "cs-two"

    def test_unchanged_config_reuses_the_registered_client(self, monkeypatch):
        from app.auth.providers import keboola as kb

        self._configure(monkeypatch, "same")
        assert kb._oauth_client() is kb._oauth_client()
