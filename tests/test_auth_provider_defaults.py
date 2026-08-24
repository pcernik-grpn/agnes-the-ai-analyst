"""B6: default provider offering — password (+ every other configured
provider) in, email magic link opt-in only.

The magic link's verify endpoint is a single-use GET: a corporate mail
scanner that opens the link before the human clicks burns the token, so it
should never be offered implicitly. Unset ``auth.providers`` now means
"every configured provider except ``email``" instead of "every configured
provider" — a narrower default, not a narrower *allowlist mechanism*: an
operator who wants the magic link back adds ``email`` to ``auth.providers``
explicitly. The misconfiguration lockout rescue
(``provider_registry._rescue_if_unusable``) is untouched by this — it only
fires when ``auth.providers`` is explicitly set to something entirely
unusable, never on the unset path this file exercises.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def make_client(tmp_path, monkeypatch, shared_app):
    def _make(providers_env: str | None, *, google_configured: bool = False):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        # SMTP configured so email.is_available() is true — the allowlist
        # default, not availability, is what's under test.
        monkeypatch.setenv("SMTP_HOST", "smtp.test.invalid")
        if providers_env is None:
            monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        else:
            monkeypatch.setenv("AGNES_AUTH_PROVIDERS", providers_env)

        if google_configured:
            # google.py reads GOOGLE_CLIENT_ID/SECRET into module-level
            # constants at import time, so setting the env var here would
            # not be observed — patch is_available() directly, the same
            # workaround the neighboring provider-allowlist suite uses.
            import app.auth.providers.google as google_provider

            monkeypatch.setattr(google_provider, "is_available", lambda: True)

        return TestClient(shared_app)

    return _make


class TestProviderAllowedDefault:
    """Unit-level: `provider_allowed` when `auth.providers` is unset."""

    def test_unset_excludes_email(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth.provider_registry import provider_allowed

        assert provider_allowed("email") is False

    def test_unset_keeps_every_other_provider(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth.provider_registry import provider_allowed

        assert all(provider_allowed(p) for p in ("google", "password", "keboola", "microsoft"))

    def test_explicit_email_listing_allows_it(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email")
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        # Narrowing/opt-in is what's under test, not availability.
        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (True, False))
        assert provider_allowed("email") is True


class TestLoginPageDefaultOffering:
    """End-to-end: what `/login` actually renders."""

    def test_unset_offers_google_and_password_not_email(self, make_client):
        client = make_client(None, google_configured=True)
        html = client.get("/login").text
        assert "Sign in with Google" in html
        assert "Sign in with Email & Password" in html or "Sign in with Email &amp; Password" in html
        assert "Sign in with Email Link" not in html

    def test_unset_without_google_offers_password_only(self, make_client):
        client = make_client(None, google_configured=False)
        html = client.get("/login").text
        assert "Sign in with Google" not in html
        assert "Sign in with Email & Password" in html or "Sign in with Email &amp; Password" in html
        assert "Sign in with Email Link" not in html

    def test_explicit_email_in_providers_offers_magic_link(self, make_client):
        client = make_client("email,password")
        html = client.get("/login").text
        assert "Sign in with Email Link" in html


class TestRescueBehaviorUnchanged:
    """The typo-allowlist lockout rescue is a different code path (an
    EXPLICIT, entirely-unusable allowlist) and must not be affected by the
    new unset-default. Mirrors `test_auth_provider_allowlist.py`."""

    def test_all_named_providers_unconfigured_still_falls_back_to_password_and_email(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "keboola")
        from app.auth import provider_registry
        from app.auth.provider_registry import configured_allowlist, provider_allowed

        monkeypatch.setattr(provider_registry, "_probe_availability", lambda name: (False, False))
        assert configured_allowlist() == ["password", "email"]
        assert provider_allowed("password") is True
        assert provider_allowed("email") is True
