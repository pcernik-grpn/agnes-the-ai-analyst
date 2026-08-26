"""Per-instance feature flag for Agent profiles (`agent_profiles.enabled` /
`AGNES_AGENT_PROFILES_ENABLED`) — same mechanism as the Studio flag
(`studio.enabled` / `get_studio_enabled`, see `tests/test_web_studio.py`).

Covers:
- `get_agent_profiles_enabled()` defaults on (registry + resolver), see
  `tests/test_feature_flags.py` for the resolution-order/truthy-parsing
  coverage shared by every flag.
- Router-level 403 `{"kind": "agent_profiles_disabled"}` on each of the five
  agent routers (`agents_admin`, `agent_runtime`, `agent_sessions`,
  `agent_webhooks`, `agent_memory`) — this also covers the CLI (`agnes
  agent`/`agnes chat` are pure clients of this API).
- `GET /agents` redirects home when disabled.
- The flag stays on with default config (regression guard — a disabled flag
  must be an explicit opt-out, never the out-of-the-box behavior).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def flag_env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from app.chat.types import Surface
    from src.db import get_system_db
    from src.repositories import agents_repo, chat_session_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    conn.close()

    agent_id = str(uuid.uuid4())
    agents_repo().create(id=agent_id, owner_user_id="owner1", name="Bot", slug="bot")

    session = chat_session_repo().create_session(user_email="owner@test.com", surface=Surface.API, agent_id=agent_id)

    client = TestClient(shared_app)
    return {
        "client": client,
        "owner_token": create_access_token("owner1", "owner@test.com"),
        "agent_id": agent_id,
        "slug": "bot",
        "session_id": session.id,
    }


def test_flag_defaults_on_out_of_the_box(flag_env):
    """No env var, no instance.yaml `agent_profiles` block — the surface must
    work exactly as before this flag was introduced."""
    c = flag_env["client"]
    resp = c.get("/api/v1/agents", headers=_auth(flag_env["owner_token"]))
    assert resp.status_code == 200


class TestRouterLevelGuard:
    """`AGNES_AGENT_PROFILES_ENABLED=0` closes every agent router at once."""

    def test_agents_admin_router(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.get("/api/v1/agents", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    def test_agent_runtime_router(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.post(
            f"/api/v1/agents/{flag_env['slug']}/responses",
            json={"input": "hi"},
            headers=_auth(flag_env["owner_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    def test_agent_runtime_usage_route(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.get(
            f"/api/v1/agents/{flag_env['slug']}/usage",
            headers=_auth(flag_env["owner_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    def test_agent_sessions_router(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.post(
            f"/api/v1/agents/{flag_env['slug']}/sessions",
            json={},
            headers=_auth(flag_env["owner_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    def test_agent_sessions_router_session_scoped_route(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.get(
            f"/api/v1/sessions/{flag_env['session_id']}",
            headers=_auth(flag_env["owner_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    def test_agent_webhooks_router(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.get(
            f"/api/v1/agents/{flag_env['slug']}/webhooks",
            headers=_auth(flag_env["owner_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    def test_agent_memory_router(self, flag_env, monkeypatch):
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        resp = c.post(
            f"/api/v1/sessions/{flag_env['session_id']}/memories",
            json={"content": "remember this"},
            headers=_auth(flag_env["owner_token"]),
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == {"kind": "agent_profiles_disabled"}

    # `test_builder_crud_router` used to live here: it asserted the kill
    # switch also closed the /agents builder's OWN CRUD router
    # (`/api/agents`), a second registry over the same `agents` table as
    # `/api/v1/agents*`. That router was deleted outright by the
    # remediation-program's "one agent model" Track C1 (Task C1.2) — v1
    # absorbed its wire shape in Task C1.1 — so there is no longer a second
    # API for the flag to leave open; the coverage above is complete on its
    # own.

    def test_re_enabling_restores_access_without_data_loss(self, flag_env, monkeypatch):
        """Data survives a disable/re-enable cycle, like Studio."""
        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "0")
        c = flag_env["client"]
        assert c.get("/api/v1/agents", headers=_auth(flag_env["owner_token"])).status_code == 403

        monkeypatch.setenv("AGNES_AGENT_PROFILES_ENABLED", "1")
        resp = c.get("/api/v1/agents", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 200
        slugs = {row["slug"] for row in resp.json()["data"]}
        assert flag_env["slug"] in slugs


class TestWebRedirect:
    def test_agents_page_redirects_home_when_disabled(self, flag_env, monkeypatch):
        monkeypatch.setattr("app.web.router.get_agent_profiles_enabled", lambda: False)
        c = flag_env["client"]
        resp = c.get("/agents", headers=_auth(flag_env["owner_token"]), follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers.get("location", "") == "/"

    def test_agents_page_renders_when_enabled(self, flag_env):
        c = flag_env["client"]
        resp = c.get("/agents", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 200


class TestNavVisibility:
    def test_nav_hidden_when_disabled(self, flag_env, monkeypatch):
        c = flag_env["client"]
        # Sanity: entry present by default.
        resp = c.get("/dashboard", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 200
        assert 'href="/agents"' in resp.text
        assert "href: '/agents'" in resp.text  # command palette row

        monkeypatch.setattr("app.web.router.get_agent_profiles_enabled", lambda: False)
        resp = c.get("/dashboard", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 200
        assert 'href="/agents"' not in resp.text
        assert "href: '/agents'" not in resp.text

    def test_rail_nav_hidden_when_disabled(self, flag_env, monkeypatch):
        """The rail chrome (`AGNES_UI_LAYOUT=rail`) has its own Agents
        destination row (`_app_rail.html`) — it must honor the flag like the
        topnav dropdown and the palette."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        c = flag_env["client"]
        resp = c.get("/dashboard", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 200
        assert 'href="/agents"' in resp.text

        monkeypatch.setattr("app.web.router.get_agent_profiles_enabled", lambda: False)
        resp = c.get("/dashboard", headers=_auth(flag_env["owner_token"]))
        assert resp.status_code == 200
        assert 'href="/agents"' not in resp.text
