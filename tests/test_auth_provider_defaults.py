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

A second, narrower rescue (``TestZeroDoorEmailRescue`` below) protects an
already-deployed instance from the default flip itself: one with SMTP
configured, no OAuth, and no user holding a password relied on the magic
link as its only working door before this change — the unset default would
otherwise take that door away on upgrade with no runtime recovery path
(the admin-API lockout guard never ran, since nothing was ever saved; the
misconfiguration rescue doesn't apply either, since the value is unset, not
a narrowed list that resolves to nothing).
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
        # A password holder must exist for password to be a genuinely
        # USABLE door — otherwise this is the zero-door state
        # TestZeroDoorEmailRescue covers, where email is kept as the
        # fallback instead of excluded.
        from argon2 import PasswordHasher

        from src.repositories import users_repo

        client = make_client(None, google_configured=False)
        users_repo().create(
            id="pw-holder-1", email="holder@test.com", name="Holder", password_hash=PasswordHasher().hash("x" * 12)
        )
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


class TestZeroDoorEmailRescue:
    """Unit-level: the unset default excludes email UNLESS it is the
    instance's only usable login door (no OAuth configured, no password
    holder). Deliberately monkeypatches the primitives
    (``_provider_available`` / ``_has_usable_password_holder``) rather than
    the DB, mirroring the style of ``TestLockoutRescue`` in
    ``test_auth_provider_allowlist.py`` — the end-to-end DB-backed variants
    live in ``TestZeroDoorEmailRescueEndToEnd`` below."""

    def test_a_magic_link_only_instance_keeps_its_door(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: name == "email")
        monkeypatch.setattr(provider_registry, "_has_usable_password_holder", lambda: False)
        assert provider_allowed("email") is True

    def test_b_google_configured_excludes_email(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: name in ("email", "google"))
        monkeypatch.setattr(provider_registry, "_has_usable_password_holder", lambda: False)
        assert provider_allowed("email") is False

    def test_b_password_holder_excludes_email(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: name == "email")
        monkeypatch.setattr(provider_registry, "_has_usable_password_holder", lambda: True)
        assert provider_allowed("email") is False

    def test_no_smtp_nothing_to_rescue_and_no_db_hit(self, monkeypatch):
        monkeypatch.delenv("AGNES_AUTH_PROVIDERS", raising=False)
        from app.auth import provider_registry
        from app.auth.provider_registry import provider_allowed

        monkeypatch.setattr(provider_registry, "_provider_available", lambda name: False)
        called = {"holder_checked": False}

        def _holder() -> bool:
            called["holder_checked"] = True
            return False

        monkeypatch.setattr(provider_registry, "_has_usable_password_holder", _holder)
        assert provider_allowed("email") is False
        # Short-circuits on email's own unavailability — never touches the DB.
        assert called["holder_checked"] is False

    def test_c_explicit_allowlist_is_never_rescued(self, monkeypatch):
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "password")
        from app.auth.provider_registry import provider_allowed

        # `password` needs no config — it's genuinely available (no probe
        # patch needed), so the OLD misconfiguration rescue (which only
        # fires when every NAMED provider is unconfigured) never triggers
        # here either; this test is purely about the NEW zero-door rescue
        # never reaching the unset-only branch that grants it.
        assert provider_allowed("email") is False


class TestZeroDoorEmailRescueEndToEnd:
    """Same contract as TestZeroDoorEmailRescue, driven through the real
    login page + a real (test) DB, so a wiring bug between the DB read and
    the login-page render can't hide behind the unit-level monkeypatches
    above."""

    def test_magic_link_only_instance_keeps_its_door(self, make_client):
        client = make_client(None, google_configured=False)
        html = client.get("/login").text
        assert "Sign in with Email Link" in html

    def test_door_excluded_once_a_password_holder_exists(self, make_client):
        from argon2 import PasswordHasher

        from src.repositories import users_repo

        client = make_client(None, google_configured=False)
        users_repo().create(
            id="pw-holder-2", email="holder2@test.com", name="Holder2", password_hash=PasswordHasher().hash("x" * 12)
        )
        html = client.get("/login").text
        assert "Sign in with Email Link" not in html

    def test_door_excluded_once_google_is_configured(self, make_client):
        client = make_client(None, google_configured=True)
        html = client.get("/login").text
        assert "Sign in with Email Link" not in html
