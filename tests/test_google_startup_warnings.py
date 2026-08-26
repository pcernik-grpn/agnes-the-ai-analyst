"""Google OAuth's boot-time identity-boundary warning.

Mirrors ``app.auth.providers.microsoft``'s ``startup_warnings()`` (see
``TestMicrosoftIdentityResolution.test_unpinned_allowed_domain_warns_about_guests``
in ``tests/test_auth_providers.py``) — before this, Microsoft's single-tenant
config with no ``auth.allowed_domain`` got a loud boot-log warning while
Google, which has no tenant boundary at all, had none: an operator running
Google sign-in with ``auth.allowed_domain`` unset gets open self-provisioning
sign-up for ANY Google account and no signal says so.

Found during RBAC review of PR #1569 (D1 config-ownership slice 1), which
demoted ``config/loader.py``'s "required fields" check from a raised
``ValueError`` to a warning — a self-hosted static-config instance missing
``auth.allowed_domain`` used to get an accidental loud signal (the whole
static config discarded + an ERROR log) that is now just a passive WARNING.
Google needs its own explicit check to not rely on that accident.
"""

from app.auth.providers import google


class TestGoogleStartupWarnings:
    def test_silent_when_not_configured(self, monkeypatch):
        """Unconfigured (no client id/secret) is opt-in — say nothing, same
        as Microsoft's unconfigured case."""
        monkeypatch.setattr(google, "GOOGLE_CLIENT_ID", "")
        monkeypatch.setattr(google, "GOOGLE_CLIENT_SECRET", "")
        assert google.startup_warnings() == []

    def test_warns_when_configured_without_allowed_domain(self, monkeypatch):
        monkeypatch.setattr(google, "GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setattr(google, "GOOGLE_CLIENT_SECRET", "secret")
        monkeypatch.setattr(google, "get_allowed_domains", lambda: [])

        warnings = google.startup_warnings()

        assert any("allowed_domain" in w for w in warnings), warnings
        assert any("any google account" in w.lower() for w in warnings), warnings

    def test_silent_when_allowed_domain_is_set(self, monkeypatch):
        monkeypatch.setattr(google, "GOOGLE_CLIENT_ID", "cid")
        monkeypatch.setattr(google, "GOOGLE_CLIENT_SECRET", "secret")
        monkeypatch.setattr(google, "get_allowed_domains", lambda: ["example.com"])

        assert google.startup_warnings() == []


class TestMainWiresGoogleStartupWarningsIntoBootLog:
    """The check exists only if something calls it. Mirrors the wiring
    `app.main` already has for Microsoft's startup_warnings()."""

    def test_main_imports_and_iterates_google_startup_warnings(self):
        import inspect

        import app.main as main

        src = inspect.getsource(main)
        assert "from app.auth.providers.google import startup_warnings" in src
        assert "google_startup_warnings()" in src
