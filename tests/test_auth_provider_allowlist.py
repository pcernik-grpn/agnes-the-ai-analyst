"""auth.providers allowlist: unset = today's behavior; set = allowlist ∩ availability;
excluded providers' endpoints 404 — including the shared-router POST /auth/token."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def make_client(tmp_path, monkeypatch, shared_app):
    def _make(providers_env: str | None):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        # email.is_available() requires SMTP/SendGrid config (or local-dev
        # mode, which has its own side effect of auto-seeding + redirecting
        # /login away entirely). The login-page assertions below need the
        # email provider to actually be available so the allowlist — not
        # availability — is what's under test; no email is ever sent.
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        if providers_env is None:
            monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        else:
            monkeypatch.setenv("AGNES_AUTH_PROVIDERS", providers_env)

        return TestClient(shared_app)

    return _make


class TestRegistry:
    def test_unset_allows_everything(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth.provider_registry import provider_allowed

        assert all(provider_allowed(p) for p in ("google", "email", "password", "keboola", "microsoft"))

    def test_microsoft_is_known(self):
        from app.auth.provider_registry import KNOWN_PROVIDERS

        assert "microsoft" in KNOWN_PROVIDERS

    def test_set_narrows(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "google")
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        # Narrowing is what's under test, not availability — make the named
        # provider count as configured so the lockout rescue stays out of it.
        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (True, False))
        assert provider_allowed("google") is True
        assert provider_allowed("password") is False

    def test_unknown_names_ignored_empty_result_treated_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "definitely-not-a-provider")
        from app.auth.provider_registry import configured_allowlist

        assert configured_allowlist() is None  # fail-open, loudly logged


class TestLockoutRescue:
    """An allowlist naming only *unconfigured* providers admits nobody; the
    reader treats it as unset (all providers), so the env/static-file path —
    which the admin API's write-time guard never sees — cannot lock the
    instance out (Devin Review on PR #1288)."""

    def test_all_named_providers_unconfigured_falls_back_to_local_sign_in(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "keboola")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist, provider_allowed

        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (False, False))
        assert configured_allowlist() == ["password", "email"]
        assert provider_allowed("password") is True

    def test_rescue_does_not_re_enable_self_provisioning_providers(self, monkeypatch):
        """The rescue must not be a way to widen who may sign in.

        An operator who narrowed to a single OAuth provider and then mistyped
        its configuration — ``MICROSOFT_TENANT_ID`` with the Application ID
        pasted in place of the Directory ID, say, which is a GUID either way —
        makes that provider unavailable. Rescuing to "all providers" would put
        Google back on the login page, and with ``auth.allowed_domain`` unset
        any Google account on earth then self-provisions an account. Password
        and magic link both require an existing user row, so falling back to
        those keeps the instance reachable without widening anything.
        """
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "microsoft")
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (False, False))
        assert provider_allowed("google") is False
        assert provider_allowed("keboola") is False
        assert provider_allowed("microsoft") is False
        assert provider_allowed("password") is True
        assert provider_allowed("email") is True

    def test_allowlist_stands_when_any_named_provider_is_configured(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "keboola,google")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist, provider_allowed

        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (name == "google", False))
        assert configured_allowlist() == ["keboola", "google"]
        assert provider_allowed("password") is False

    def test_rescue_says_what_it_can_actually_do_without_a_mail_transport(self, monkeypatch, caplog):
        """The fallback authenticates only EXISTING accounts, and without a mail
        transport that narrows to password alone. On an OAuth-only instance that
        can mean no usable door at all — the operator has to learn that from
        this log line, not from the login page, so it must name the recovery
        path instead of asserting the instance stays reachable."""
        import logging

        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "microsoft")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist

        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (False, False))
        monkeypatch.setattr(provider_registry, "_LOCKOUT_RESCUE_LOGGED", None)
        with caplog.at_level(logging.ERROR, logger="app.auth.provider_registry"):
            configured_allowlist()
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "break-glass" in text, text
        assert "stays reachable" not in text, text

    def test_a_raising_probe_does_not_trigger_the_rescue(self, monkeypatch):
        """A probe that RAISED is not the same as one that answered "no".

        The probes are in-memory config reads, but they are wrapped in a
        try/except that reads a raise as unavailable. While the rescue WIDENED
        that was harmless; now that it NARROWS, letting a transient fault fire
        it would 404 the operator's intended login door for the fault's
        duration on no information at all. Each provider is still gated by its
        own is_available() at the route, so leaving the allowlist alone cannot
        make anything broken reachable.
        """
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "microsoft")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist

        def boom(_name):
            raise RuntimeError("transient")

        monkeypatch.setattr(provider_registry, "_AVAILABILITY_PROBES", {"microsoft": "app.auth.providers.microsoft"})
        monkeypatch.setattr(
            provider_registry.importlib,
            "import_module",
            lambda path: boom(path),
        )
        assert configured_allowlist() == ["microsoft"]

    def test_web_lockout_config_still_offers_usable_logins(self, make_client):
        # End-to-end: keboola named alone with no stack configured — exactly
        # the lockout scenario. Login page must still offer usable methods and
        # the shared-router password grant must answer.
        client = make_client("keboola")
        html = client.get("/login").text
        assert "Sign in with Email Link" in html
        resp = client.post("/auth/token", data={"email": "nobody@example.com", "password": "x"})
        assert resp.status_code != 404


class TestEndpointGating:
    def test_password_endpoints_404_when_excluded(self, make_client):
        # `email` is configured by the fixture (SMTP_HOST), so the allowlist
        # stands on its own and the lockout rescue never enters the picture.
        client = make_client("email")
        # Router-level dependency: any matched route under /auth/password 404s.
        # (POST — the login form route; a GET would 405 before dependencies run.)
        assert client.post("/auth/password/login/web", data={}).status_code == 404
        # The easy-to-miss one: the shared-router password grant.
        resp = client.post("/auth/token", data={"email": "a@b.c", "password": "x"})
        assert resp.status_code == 404
        # Login sub-page is gated too.
        assert client.get("/login/password").status_code == 404

    def test_email_endpoints_404_when_excluded(self, make_client):
        # Symmetric to the password case: excluding `email` must 404 both the
        # magic-link sub-page and its form/JSON send-link endpoints, so an
        # instance can't be narrowed to a provider whose door still answers.
        client = make_client("password")
        assert client.get("/login/email").status_code == 404
        assert client.post("/auth/email/send-link/web", data={"email": "a@b.c"}).status_code == 404
        assert client.post("/auth/email/send-link", json={"email": "a@b.c"}).status_code == 404

    def test_microsoft_endpoints_404_when_excluded(self, make_client):
        # Symmetric to the keboola/password cases: excluding `microsoft` must
        # 404 its login door even though the router is always registered.
        client = make_client("email")
        assert client.get("/auth/microsoft/login", follow_redirects=False).status_code == 404

    def test_password_endpoints_live_when_unset(self, make_client):
        client = make_client(None)
        resp = client.post("/auth/token", data={"email": "nobody@example.com", "password": "x"})
        assert resp.status_code != 404  # 401/422 is fine — the endpoint exists

    def test_login_page_hides_excluded_buttons(self, make_client):
        client = make_client("email")
        html = client.get("/login").text
        assert "Sign in with Email Link" in html
        assert "Sign in with Email &amp; Password" not in html and "Sign in with Email & Password" not in html

    def test_login_page_unset_is_todays_behavior(self, make_client):
        client = make_client(None)
        html = client.get("/login").text
        # No Google credentials in the test env → password + email link, exactly as before.
        assert "Sign in with Email & Password" in html or "Sign in with Email &amp; Password" in html
        assert "Sign in with Email Link" in html
