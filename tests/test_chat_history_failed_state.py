"""TCRD-207: a FAILED fetch of the conversation list must not render as EMPTY.

DES-153 found this live: both the rail's own renderer (rail_history.js, used
on every non-/chat page) and chat.js's boot path caught a failed
`GET /api/chat/sessions` and unhid the exact same "No conversations yet."
copy a genuinely empty account gets — indistinguishable from "your account
cannot see the conversation list", even though the conversations ARE being
saved. Fixed with the shared `state.panel('failed', ...)` component
(macros/_state.html) rendering a distinct, danger-toned row alongside the
existing empty-state paragraph, and both JS renderers now reveal ONE of the
two states, never blur them into each other.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
RAIL_JS = STATIC / "js" / "rail_history.js"
CHAT_JS = STATIC / "js" / "chat.js"


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
    from src.db import get_system_db

    from app.chat.persistence import ChatRepository

    app.state.chat_repo = ChatRepository(get_system_db())

    async def _kill(chat_id, reason=None):
        return None

    app.state.chat_manager = SimpleNamespace(kill=_kill)
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


def _enable_chat(web_client, monkeypatch):
    import app.auth.access as access

    monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
    web_client.app.state.chat_config = SimpleNamespace(enabled=True)


class TestRailMarkup:
    def test_rail_renders_a_hidden_failed_state_beside_the_empty_state(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        html = web_client.get("/library", cookies=admin_cookie).text
        assert 'id="cloud-chat-empty-state"' in html
        assert 'id="cloud-chat-failed-state"' in html
        # Both start hidden — the JS renderer picks exactly one, never both.
        rail = html[html.index('id="cloud-chat-empty-state"') - 200 : html.index('id="cloud-chat-failed-state"') + 400]
        assert "hidden" in rail
        # The shared component, danger-toned — never the neutral empty tone.
        failed_tag = rail[rail.index('id="cloud-chat-failed-state"') - 200 :]
        assert "state-panel--danger" in failed_tag
        assert "state-panel--neutral" not in failed_tag

    def test_failed_state_offers_a_retry_action(self, web_client, admin_cookie, monkeypatch):
        _enable_chat(web_client, monkeypatch)
        html = web_client.get("/library", cookies=admin_cookie).text
        assert "data-state-retry" in html


class TestRailHistoryJs:
    """rail_history.js — the renderer used on every page except /chat."""

    def test_load_failure_reveals_failed_not_empty(self):
        js = RAIL_JS.read_text(encoding="utf-8")
        assert "cloud-chat-failed-state" in js, "rail_history.js must know about the new element"
        # The old collapse: a caught error just unhid the empty paragraph.
        # That line must be gone in favor of the two-state branch.
        assert "if (emptyEl) emptyEl.hidden = false;" not in js.split("async function load()")[1], (
            "load()'s catch must no longer treat a failed fetch as the empty state"
        )

    def test_retry_button_re_triggers_load(self):
        js = RAIL_JS.read_text(encoding="utf-8")
        assert "data-state-retry" in js


class TestChatJsBootPath:
    """chat.js's own copy — the /chat page never delegates to rail_history.js."""

    def test_sidebar_load_failure_reveals_failed_not_empty(self):
        js = CHAT_JS.read_text(encoding="utf-8")
        assert "cloud-chat-failed-state" in js

    def test_success_path_hides_the_failed_state(self):
        """A later successful load must clear a failed state left over from a
        prior retry — otherwise a transient blip stays on screen forever."""
        js = CHAT_JS.read_text(encoding="utf-8")
        sidebar_fn = js.split("async function loadSidebar()")[1].split("\nasync function", 1)[0]
        assert "cloud-chat-failed-state" in sidebar_fn
        assert "failed.hidden = true" in sidebar_fn or "failedEl.hidden = true" in sidebar_fn
