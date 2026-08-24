"""/chat without a chat grant: the page must work, not crash on submit.

The rail chrome gates EVERY conversation element on ``can_chat``
(_app_rail.html: #new-chat, #chat-list, #pinned-chat-list,
#cloud-chat-empty-state), and ``can_chat`` deliberately reads
``has_explicit_grant`` — god-mode does NOT light it up. The /chat route and
the chat API gate on ``can_access``, where god-mode DOES short-circuit. So an
admin without an explicit chat grant can reach a fully working /chat whose
rail renders none of the sidebar ids chat.js draws into.

chat.js's ``loadSidebar`` dereferenced ``#chat-list`` unconditionally
(``ul.innerHTML = ""``), so on that page every submit died inside
``newChat → loadSidebar`` and surfaced as
``Could not start chat: Cannot set properties of null (setting 'innerHTML')``.

Two guards, same static-source style as test_chat_pin_conversations.py (no
headless browser in CI):

  1. the route really does produce the sidebar-less page for a god-mode
     admin (the scenario is reachable, not hypothetical);
  2. ``loadSidebar`` bails when the list is absent — after caching the
     fetch, which the Cmd+K palette and openSession's title lookup read.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

CHAT_JS = Path("app/web/static/js/chat.js")


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


class TestGodModeAdminReachesSidebarlessChat:
    """The scenario the JS guard exists for is real: rail + chat enabled +
    no explicit grant → /chat renders (can_access lets the admin in) with
    none of the sidebar ids (can_chat keeps the rail's chat chrome out)."""

    def test_chat_renders_without_any_sidebar_list(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)
        # No has_explicit_grant patch: the fixture admin holds no chat grant,
        # which is exactly the state under test.
        resp = web_client.get("/chat", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 200, "god-mode must reach /chat by URL (can_access gate)"
        text = resp.text
        # The rail's chat chrome is absent — every id chat.js renders into.
        for absent in ('id="chat-list"', 'id="pinned-chat-list"', 'id="new-chat"', 'id="cloud-chat-empty-state"'):
            assert absent not in text, f"{absent} must be can_chat-gated out of the rail"
        # ...while the working chat surface itself is all there: this page
        # SUBMITS, so chat.js must survive the missing list, not skip loading.
        for present in ('id="chat-form"', 'id="chat-input"', 'id="chat-messages"'):
            assert present in text, f"{present} is the working surface — it must render"


class TestLoadSidebarSurvivesAbsentList:
    """Static-source contract on chat.js: loadSidebar must bail when
    #chat-list is absent — rail_history.js already guards its renderer on
    the same condition ("no chat grant / history section not rendered")."""

    def _load_sidebar_body(self) -> str:
        js = CHAT_JS.read_text(encoding="utf-8")
        m = re.search(r"async function loadSidebar\(\) \{(.*?)\n\}", js, re.S)
        assert m, "loadSidebar not found in chat.js"
        return m.group(1)

    def test_bails_before_the_first_list_dereference(self):
        body = self._load_sidebar_body()
        assert "if (!ul) return" in body, (
            "loadSidebar must null-guard #chat-list — without it, every submit "
            "on a sidebar-less /chat dies with «Could not start chat: Cannot "
            "set properties of null (setting 'innerHTML')»"
        )
        assert body.index("if (!ul) return") < body.index('ul.innerHTML = ""'), (
            "the guard must run before the first `ul` dereference"
        )

    def test_sessions_cache_is_populated_before_the_bail(self):
        """The fetch must NOT be skipped: the Cmd+K palette filters
        _sessionsCache and openSession resolves titles from it — both still
        live on the sidebar-less page."""
        body = self._load_sidebar_body()
        assert body.index("_sessionsCache = list") < body.index("if (!ul) return"), (
            "cache the sessions fetch before bailing on the absent list"
        )
