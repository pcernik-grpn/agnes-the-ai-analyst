"""Multi-project login: the wildcard verify gates and the callback's
discovery → provisioning wiring per mode (auto / select / disabled).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import keboola_provisioning as kprov
from app.auth.providers import keboola_projects as kp
from app.auth.providers import keboola_verify as kv


def _payload(role="guest", owner_id=99):
    return {
        "isMasterToken": True,
        "owner": {"id": owner_id, "name": "Home project"},
        "adminOwner": {"email": "jane@example.com", "name": "Jane"},
        "admin": {"role": role},
    }


class TestWildcardVerifyGates:
    """Under the wildcard (discovery mode + project_id '*'/unset) the OAuth
    path drops the single-project binding and the home-project role gate —
    discovery enforces roles across every project instead. The header path
    keeps the role gate: a plain Storage token cannot call introspect."""

    @pytest.fixture(autouse=True)
    def _wildcard(self, monkeypatch):
        monkeypatch.setattr(kv, "multi_project_active", lambda: True)
        monkeypatch.setattr(kv, "configured_project_id", lambda: None)
        monkeypatch.setattr(kv, "allowed_roles", lambda: ["admin"])

    def test_oauth_path_skips_project_and_home_role_gates(self):
        identity = kv._identity_from_payload(_payload(role="guest"), source="oauth")
        assert identity.email == "jane@example.com"
        assert identity.project_id == "99"

    def test_header_path_keeps_the_role_gate(self):
        with pytest.raises(kv.KeboolaVerifyError) as err:
            kv._identity_from_payload(_payload(role="guest"), source="header")
        assert err.value.reason == "role_forbidden"

    def test_header_path_passes_with_allowed_role(self):
        identity = kv._identity_from_payload(_payload(role="admin"), source="header")
        assert identity.project_id == "99"

    def test_non_master_token_still_rejected(self):
        payload = _payload(role="admin")
        payload["isMasterToken"] = False
        with pytest.raises(kv.KeboolaVerifyError):
            kv._identity_from_payload(payload, source="header")

    def test_pinned_project_keeps_the_single_project_gate(self, monkeypatch):
        monkeypatch.setattr(kv, "configured_project_id", lambda: "12345")
        with pytest.raises(kv.KeboolaVerifyError) as err:
            kv._identity_from_payload(_payload(role="admin", owner_id=99), source="oauth")
        assert err.value.reason == "project_mismatch"

    def test_configured_base_url_accepts_missing_project_id(self, monkeypatch):
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        assert kv._configured_base_url() == "https://connection.example.com"

    def test_disabled_mode_still_requires_project_id(self, monkeypatch):
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        monkeypatch.setattr(kv, "multi_project_active", lambda: False)
        with pytest.raises(kv.KeboolaVerifyError) as err:
            kv._configured_base_url()
        assert err.value.reason == "not_configured"

    def test_leftover_wildcard_without_active_mode_is_not_configured(self, monkeypatch):
        """project_id '*' with the mode turned off must self-describe as a
        configuration problem — not fail every token as project_mismatch."""
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        monkeypatch.setattr(kv, "multi_project_active", lambda: False)
        monkeypatch.setattr(kv, "configured_project_id", lambda: "*")
        with pytest.raises(kv.KeboolaVerifyError) as err:
            kv._configured_base_url()
        assert err.value.reason == "not_configured"
        assert "multi_project_mode" in err.value.detail


class TestModeResolution:
    def test_default_is_disabled_and_inactive(self):
        assert kv.multi_project_mode() == "disabled"
        assert kv.multi_project_active() is False
        assert kv.is_wildcard_project() is False

    def test_wildcard_requires_an_active_mode(self, monkeypatch):
        monkeypatch.setattr(kv, "multi_project_active", lambda: False)
        monkeypatch.setattr(kv, "configured_project_id", lambda: "*")
        assert kv.is_wildcard_project() is False

    def test_star_and_unset_project_are_wildcard_when_active(self, monkeypatch):
        monkeypatch.setattr(kv, "multi_project_active", lambda: True)
        for pid in (None, "*"):
            monkeypatch.setattr(kv, "configured_project_id", lambda p=pid: p)
            assert kv.is_wildcard_project() is True

    def test_provider_available_without_project_id_in_discovery_mode(self, monkeypatch):
        from app.auth.providers import keboola as kb

        monkeypatch.setattr(kv, "client_id", lambda: "cid")
        monkeypatch.setattr(kv, "client_secret", lambda: "cs")
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        monkeypatch.setattr(kv, "configured_project_id", lambda: None)
        monkeypatch.setattr(kv, "multi_project_active", lambda: False)
        assert kb.is_available() is False
        monkeypatch.setattr(kv, "multi_project_active", lambda: True)
        assert kb.is_available() is True

    def test_header_auth_enabled_without_project_id_in_discovery_mode(self, monkeypatch):
        from app.auth import keboola_header as kh

        monkeypatch.setattr("app.switches.switch_value", lambda name: name == "keboola_token_header")
        monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
        monkeypatch.setattr(kv, "configured_project_id", lambda: None)
        monkeypatch.setattr(kv, "multi_project_active", lambda: True)
        assert kh.enabled() is True
        monkeypatch.setattr(kv, "multi_project_active", lambda: False)
        assert kh.enabled() is False


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
    monkeypatch.setattr(kv, "stack_url", lambda: "https://connection.example.com")
    monkeypatch.setattr(kv, "configured_project_id", lambda: None)
    monkeypatch.setattr(kv, "client_id", lambda: "cid")
    monkeypatch.setattr(kv, "client_secret", lambda: "csecret")

    return TestClient(shared_app)


class TestMultiProjectCallback:
    """The callback's discovery wiring. The OAuth exchange, the verify and
    the SSRF validator are stubbed exactly like the single-project callback
    tests; discovery and provisioning are spied per test."""

    def _patch_flow(self, monkeypatch, *, mode="auto"):
        from app.auth.providers import keboola as kb

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
                project_id="99",
                project_name="Home",
                email="jane@example.com",
                name="Jane",
                role="admin",
            ),
        )
        monkeypatch.setattr(kv, "multi_project_mode", lambda: mode)
        monkeypatch.setattr(kv, "multi_project_active", lambda: mode in ("select", "auto"))
        monkeypatch.setattr(kv, "is_wildcard_project", lambda: mode in ("select", "auto"))

    def test_auto_mode_provisions_and_signs_in(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="auto")
        discovered = [kp.DiscoveredProject(id="516", name="A", role="admin")]
        monkeypatch.setattr(kp, "discover_allowed_projects", lambda tok: discovered)
        calls = {}

        def fake_provision(user, to_provision, all_discovered, access_token):
            calls["user"] = user["email"]
            calls["projects"] = [p.id for p in to_provision]
            calls["token"] = access_token
            return kprov.ProvisionSummary()

        monkeypatch.setattr(kprov, "provision_projects", fake_provision)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies
        assert calls == {"user": "jane@example.com", "projects": ["516"], "token": "at-123"}

    def test_auto_mode_runs_the_background_tail(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="auto")
        monkeypatch.setattr(
            kp, "discover_allowed_projects", lambda tok: [kp.DiscoveredProject(id="516", name="A", role="admin")]
        )
        summary = kprov.ProvisionSummary(connections_needing_chat_tools=["c1"])
        monkeypatch.setattr(kprov, "provision_projects", lambda *a, **k: summary)
        ran = {}

        async def fake_tail(s):
            ran["summary"] = s

        monkeypatch.setattr(kprov, "finish_login_provisioning", fake_tail)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        # TestClient runs response background tasks before returning.
        assert ran["summary"] is summary

    def test_wildcard_discovery_failure_fails_the_login_closed(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="auto")

        def boom(tok):
            raise kp.KeboolaProjectApiError("introspect_failed")

        monkeypatch.setattr(kp, "discover_allowed_projects", boom)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_oauth_failed" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_pinned_instance_discovery_surprise_fails_soft(self, client, monkeypatch):
        """With a concrete project_id the trust boundary (the single-project
        verify) has already passed — an UNEXPECTED discovery crash (any
        exception, not just the client's typed error) is a provisioning-only
        failure and must not break the login (Devin Review on this PR,
        thirteenth round)."""
        self._patch_flow(monkeypatch, mode="auto")
        monkeypatch.setattr(kv, "is_wildcard_project", lambda: False)

        def boom(tok):
            raise RuntimeError("unexpected introspect shape")

        monkeypatch.setattr(kp, "discover_allowed_projects", boom)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=" not in resp.headers["location"]
        assert "access_token" in resp.cookies

    def test_wildcard_discovery_surprise_stays_fail_closed(self, client, monkeypatch):
        """Under the wildcard, discovery IS the trust boundary — the same
        unexpected crash must keep failing the login closed (via the outer
        backstop), exactly like the typed introspect failure."""
        self._patch_flow(monkeypatch, mode="auto")

        def boom(tok):
            raise RuntimeError("unexpected introspect shape")

        monkeypatch.setattr(kp, "discover_allowed_projects", boom)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_oauth_failed" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_wildcard_zero_projects_is_not_permitted(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="auto")
        monkeypatch.setattr(kp, "discover_allowed_projects", lambda tok: [])
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "error=keboola_not_permitted" in resp.headers["location"]
        assert "access_token" not in resp.cookies

    def test_provisioning_failure_does_not_block_the_login(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="auto")
        monkeypatch.setattr(
            kp, "discover_allowed_projects", lambda tok: [kp.DiscoveredProject(id="516", name="A", role="admin")]
        )

        def boom(*a, **k):
            raise RuntimeError("vault exploded")

        monkeypatch.setattr(kprov, "provision_projects", boom)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies

    def test_select_mode_stashes_instead_of_provisioning(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="select")
        discovered = [kp.DiscoveredProject(id="516", name="A", role="admin")]
        monkeypatch.setattr(kp, "discover_allowed_projects", lambda tok: discovered)
        calls = {}
        monkeypatch.setattr(
            kprov,
            "provision_projects",
            lambda user, to_provision, all_discovered, token: (
                calls.setdefault("membership_pass", [p.id for p in to_provision]) or kprov.ProvisionSummary()
            ),
        )
        monkeypatch.setattr(
            kprov,
            "store_pending_discovery",
            lambda user, projects, token: calls.setdefault("stashed", [p.id for p in projects]) or True,
        )
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies
        assert calls["membership_pass"] == []  # nothing imported uninvited
        assert calls["stashed"] == ["516"]

    def test_disabled_mode_never_discovers(self, client, monkeypatch):
        self._patch_flow(monkeypatch, mode="disabled")
        monkeypatch.setattr(kv, "configured_project_id", lambda: "99")
        called = {"discover": False}

        def spy(tok):
            called["discover"] = True
            return []

        monkeypatch.setattr(kp, "discover_allowed_projects", spy)
        resp = client.get("/auth/keboola/callback?code=x&state=y", follow_redirects=False)
        assert resp.status_code == 302
        assert "access_token" in resp.cookies
        assert called["discover"] is False
