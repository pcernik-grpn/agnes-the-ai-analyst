"""Admin chat dashboard tests (Task 11.1).

Three tests per the plan:
- list active sessions (admin gets 200 + session list)
- kill session (admin DELETE returns 200)
- non-admin forbidden (403)
"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.access import require_admin
from app.auth.dependencies import get_current_user
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from src.db import _ensure_schema

TEST_ADMIN = {"id": "admin1", "email": "admin@test.com", "is_admin": True}
TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_mock_manager(repo: ChatRepository) -> ChatManager:
    from app.chat.workdir import WorkdirManager

    provider = MagicMock()
    provider.spawn = AsyncMock()

    workdir_mgr = MagicMock(spec=WorkdirManager)
    workdir_mgr.ensure_user_workdir = MagicMock()
    workdir_mgr.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    config = ChatConfig(enabled=True, concurrency_per_user=3)
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=config,
    )


def _make_app(*, as_admin: bool = True) -> tuple[FastAPI, ChatRepository]:
    from app.api.admin_chat import router as admin_chat_router
    from app.api.chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(admin_chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    mgr = _make_mock_manager(repo)
    app.state.chat_manager = mgr
    app.state.chat_repo = repo

    user = TEST_ADMIN if as_admin else TEST_USER

    # Override auth for both user-level and admin-level deps
    app.dependency_overrides[get_current_user] = lambda: user

    # Chat is an RBAC resource (default-deny). These tests create sessions via
    # the chat API to set up admin-dashboard fixtures — they exercise the admin
    # surface, not the chat gate — so delegate require_chat_access to whatever
    # get_current_user returns (skip only the grant check). Default-deny is
    # covered by test_chat_api::test_chat_requires_rbac_grant.
    from app.api.chat import require_chat_access

    async def _granted_user(u: dict = Depends(get_current_user)) -> dict:
        return u

    app.dependency_overrides[require_chat_access] = _granted_user
    if as_admin:
        app.dependency_overrides[require_admin] = lambda: user
    # For non-admin case, require_admin is NOT overridden, so it uses the
    # real implementation — but since get_current_user is overridden to return
    # a non-admin user and require_admin calls is_user_admin() against a real
    # DB that has no Admin group membership for this user, it will 403.
    # Simpler: for non-admin tests, just don't override require_admin but do
    # inject a user without admin rights.  The require_admin dep calls
    # is_user_admin(user["id"], conn) — the in-memory DB has the Admin group
    # seeded but the non-admin user is not a member, so it raises 403.
    # However, require_admin also depends on _get_db (system DB). For a clean
    # unit test without a real system.duckdb, override require_admin to raise
    # 403 explicitly.
    if not as_admin:
        from fastapi import HTTPException

        def _deny():
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[require_admin] = _deny

    return app, repo


@pytest.fixture
def api_client() -> TestClient:
    app, _ = _make_app(as_admin=True)
    return TestClient(app)


@pytest.fixture
def api_client_non_admin() -> TestClient:
    app, _ = _make_app(as_admin=False)
    return TestClient(app)


@pytest.fixture
def logged_in_admin():
    return TEST_ADMIN


@pytest.fixture
def logged_in_user():
    return TEST_USER


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_admin_lists_active_sessions(api_client: TestClient, logged_in_admin):
    """Admin GET /admin/chat returns 200 with sessions list.

    Newly-created sessions are only added to the in-memory live dict after
    a WebSocket attach (see ChatManager.attach). Here we inject a mock live
    session directly into the manager to test the endpoint shape.
    """
    from datetime import datetime
    from unittest.mock import MagicMock

    from app.chat.manager import LiveSession, SinkEntry
    from app.chat.types import SessionState

    # First create a session so we have a real chat_id in the repo.
    create = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = create["id"]

    # Inject the session into the manager's _live dict as if it were attached.
    mgr = api_client.app.state.chat_manager
    now = datetime.now(UTC)
    mgr._live[chat_id] = LiveSession(
        chat_id=chat_id,
        user_email="admin@test.com",
        state=SessionState.ACTIVE,
        handle=None,
        started_at=now,
        last_activity=now,
        sinks=[SinkEntry(participant_email="admin@test.com", sink=MagicMock())],
    )

    r = api_client.get("/admin/chat")
    assert r.status_code == 200
    data = r.json()
    assert any(s["id"] == chat_id for s in data["sessions"])


def test_admin_kills_session(api_client: TestClient, logged_in_admin):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.delete(f"/admin/chat/{c['id']}")
    assert r.status_code == 204


def test_non_admin_forbidden(api_client_non_admin: TestClient, logged_in_user):
    r = api_client_non_admin.get("/admin/chat")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# admin_tail WebSocket — ticket-based auth (architect finding #2)
# ---------------------------------------------------------------------------


def test_admin_tail_rejects_anonymous_ws(api_client: TestClient, logged_in_admin):
    """No ticket → WS closes with 4401 before any frame is sent.

    Confidentiality bypass test: another user's run.log must not stream
    over an unauthenticated WebSocket.
    """
    from starlette.websockets import WebSocketDisconnect

    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with api_client.websocket_connect(f"/admin/chat/{c['id']}/tail") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4401


def test_admin_tail_accepts_valid_ticket(api_client: TestClient, logged_in_admin):
    """A freshly-issued admin ticket opens the WS; we receive a sentinel frame."""
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    tk = api_client.post(f"/admin/chat/{c['id']}/tail-ticket")
    assert tk.status_code == 200
    ticket = tk.json()["ticket"]
    with api_client.websocket_connect(f"/admin/chat/{c['id']}/tail?ticket={ticket}") as ws:
        frame = ws.receive_json()
        # No run.log on disk for a freshly-minted session → no_log frame.
        # Either no_log or line — both prove the WS opened (accept-after-auth).
        assert frame.get("type") in ("no_log", "line")


def test_admin_tail_rejects_non_admin_ticket_request(api_client_non_admin: TestClient, logged_in_user):
    """Non-admin cannot mint a tail-ticket."""
    # Non-admin can still POST a session for themselves
    c = api_client_non_admin.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client_non_admin.post(f"/admin/chat/{c['id']}/tail-ticket")
    assert r.status_code == 403


def test_admin_tail_rejects_expired_or_unknown_ticket(api_client: TestClient, logged_in_admin):
    """A bogus ticket value closes the WS with 4401."""
    from starlette.websockets import WebSocketDisconnect

    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with api_client.websocket_connect(f"/admin/chat/{c['id']}/tail?ticket=not-a-real-ticket") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4401


def test_admin_tail_ticket_expires_after_ttl(api_client: TestClient, logged_in_admin, monkeypatch):
    """The admin tail ticket now rides the coordination backend's TTL — once
    it elapses, the ticket is gone even though it was never explicitly
    consumed (same contract as the chat-WS ticket in app/api/chat.py)."""
    import time

    import app.api.admin_chat as admin_chat_mod

    monkeypatch.setattr(admin_chat_mod, "_ADMIN_TICKET_TTL_SEC", 1)
    ticket = admin_chat_mod._issue_admin_ticket("admin1")
    time.sleep(1.3)
    assert admin_chat_mod._consume_admin_ticket(ticket) is None


def test_admin_tail_closes_4503_on_coordination_unavailable(api_client: TestClient, logged_in_admin, monkeypatch):
    """A coordination backend blip (e.g. Redis unreachable) during ticket
    consume must close the WS with 4503, not propagate an uncaught
    ``CoordinationUnavailable`` out of the WS handler."""
    from starlette.websockets import WebSocketDisconnect

    import app.api.admin_chat as admin_chat_mod
    from app.coordination.base import CoordinationUnavailable

    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    tk = api_client.post(f"/admin/chat/{c['id']}/tail-ticket")
    ticket = tk.json()["ticket"]

    def _raise(_ticket: str):
        raise CoordinationUnavailable("redis blip")

    monkeypatch.setattr(admin_chat_mod, "_consume_admin_ticket", _raise)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with api_client.websocket_connect(f"/admin/chat/{c['id']}/tail?ticket={ticket}") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4503


# ---------------------------------------------------------------------------
# Task B.3: GET /admin/chat HTML page (content-negotiated)
# ---------------------------------------------------------------------------


def test_admin_chat_html_route_renders_for_admin(
    api_client: TestClient,
    logged_in_admin,
):
    """GET /admin/chat with Accept: text/html returns the dashboard shell.

    Same URL serves JSON for programmatic clients (Accept: application/json
    or no preference); browsers get the HTML.
    """
    r = api_client.get("/admin/chat", headers={"Accept": "text/html"})
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers["content-type"]
    assert "Chat runners" in r.text


def test_admin_chat_json_still_works_for_programmatic_caller(
    api_client: TestClient,
    logged_in_admin,
):
    """JSON callers still receive the sessions list (no regression on B.3)."""
    r = api_client.get("/admin/chat", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert "application/json" in r.headers["content-type"]
    assert "sessions" in r.json()


def test_admin_chat_html_route_forbidden_for_non_admin(
    api_client_non_admin: TestClient,
    logged_in_user,
):
    """Non-admin caller gets 403 from /admin/chat regardless of Accept."""
    r = api_client_non_admin.get(
        "/admin/chat",
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 307, 403)


# ---------------------------------------------------------------------------
# /admin/chat/{id}/debug — process-local counter introspection
# (replaces the pre-E2B docker-exec poke; see tests/e2e/test_bq_budget.py)
# ---------------------------------------------------------------------------


def test_admin_debug_returns_bq_bytes_zero_for_fresh_session(
    api_client: TestClient,
    logged_in_admin,
):
    """A freshly-created session has no BQ scan attributed yet → bq_bytes == 0."""
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.get(f"/admin/chat/{c['id']}/debug")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_id"] == c["id"]
    assert body["bq_bytes"] == 0
    # `live` is None until the session is attached.
    assert body["live"] is None


def test_admin_debug_reflects_charged_bq_bytes(
    api_client: TestClient,
    logged_in_admin,
):
    """After accumulate_session_bq_bytes runs, the endpoint reports the total."""
    from app.api.query import _per_session_bq_bytes

    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    _per_session_bq_bytes[c["id"]] = 1_234_567

    try:
        r = api_client.get(f"/admin/chat/{c['id']}/debug")
        assert r.status_code == 200
        assert r.json()["bq_bytes"] == 1_234_567
    finally:
        _per_session_bq_bytes.pop(c["id"], None)


def test_admin_debug_forbidden_for_non_admin(
    api_client_non_admin: TestClient,
    logged_in_user,
):
    """Debug endpoint is admin-only (guards counter introspection)."""
    r = api_client_non_admin.get("/admin/chat/some-id/debug")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Provider-aware "test connections"
# ---------------------------------------------------------------------------


def test_test_connections_probes_only_anthropic_for_the_kai_agent_provider(
    api_client: TestClient, logged_in_admin, monkeypatch
):
    """The engine owns its own sandbox backing — a kai-agent instance gets
    only the anthropic probe, never a sandbox row for infrastructure it does
    not use."""
    api_client.app.state.chat_config = ChatConfig(enabled=True, provider="kai-agent")
    monkeypatch.setattr(
        "app.api.admin_chat.test_anthropic_key",
        AsyncMock(return_value={"ok": True, "detail": "Anthropic API key valid"}),
    )
    body = api_client.post("/admin/chat/secrets/test").json()
    assert body["anthropic_api_key"]["ok"] is True
    assert "docker_sandbox" not in body
    assert "e2b_api_key" not in body


def test_test_connections_probes_the_docker_sandbox_for_the_docker_provider(
    api_client: TestClient, logged_in_admin, monkeypatch
):
    """A docker instance must report the sidecar/daemon/image, not a red row
    for a credential it will never use."""
    api_client.app.state.chat_config = ChatConfig(enabled=True, provider="docker", docker_image="agnes-chat-sandbox:1")
    probe = AsyncMock(return_value={"ok": True, "detail": "docker sandbox runner ready"})
    monkeypatch.setattr("app.api.admin_chat.test_docker_sandbox", probe)
    monkeypatch.setattr(
        "app.api.admin_chat.test_anthropic_key",
        AsyncMock(return_value={"ok": True, "detail": "Anthropic API key valid"}),
    )
    body = api_client.post("/admin/chat/secrets/test").json()
    assert body["docker_sandbox"] == {"ok": True, "detail": "docker sandbox runner ready"}
    assert "e2b_api_key" not in body
    probe.assert_awaited_once_with("agnes-chat-sandbox:1")


def test_readiness_reports_docker_rows(api_client: TestClient, logged_in_admin, monkeypatch):
    monkeypatch.delenv("APPS_RUNNER_TOKEN", raising=False)
    api_client.app.state.chat_config = ChatConfig(enabled=True, provider="docker")
    body = api_client.get("/admin/chat/readiness").json()
    assert body["provider"] == "docker"
    assert body["secrets"]["apps_runner_token"]["required"] is True
    assert "apps_runner_token" in body["missing"]
