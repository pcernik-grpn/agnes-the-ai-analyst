"""Tests for the chat REST API — POST/GET/DELETE sessions, 503 when disabled.

Fixture pattern: build a minimal FastAPI app with the chat router attached,
set up app.state manually (chat_manager + chat_repo), and override the
get_current_user dependency to inject a test user dict.
"""

from __future__ import annotations

from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from src.db import _ensure_schema

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_USER = {"id": "user1", "email": "alice@test.com", "is_admin": False}


def _make_mock_manager(repo: ChatRepository) -> ChatManager:
    """Return a ChatManager wired to a real repo but with a no-op provider."""
    from app.chat.workdir import WorkdirManager

    provider = MagicMock()
    provider.spawn = AsyncMock()

    workdir_mgr = MagicMock(spec=WorkdirManager)
    workdir_mgr.ensure_user_workdir = MagicMock()
    workdir_mgr.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    # Pin the native provider: these tests assert on chat_<hex> session ids,
    # which `engine_session_id` replaces with UUIDs under the (default)
    # kai-agent provider.
    config = ChatConfig(enabled=True, concurrency_per_user=3, provider="docker")
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=config,
    )


def _make_app(*, chat_enabled: bool = True) -> FastAPI:
    """Build a minimal FastAPI test app with the chat router attached."""
    from app.api.chat import router as chat_router

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    if chat_enabled:
        mgr = _make_mock_manager(repo)
        app.state.chat_manager = mgr
    # When chat_enabled=False we intentionally leave chat_manager absent.

    app.state.chat_repo = repo

    # Override auth so we don't need a running DuckDB system.db. Chat is now an
    # RBAC resource, so the endpoints depend on ``require_chat_access`` (which
    # internally resolves the user + checks the grant). Override that gate to
    # *delegate* to whatever get_current_user returns — skipping only the
    # access check (these tests exercise endpoint behavior, and some switch the
    # user mid-test). Default-deny is covered by test_chat_requires_rbac_grant.
    from app.api.chat import require_chat_access

    async def _granted_user(user: dict = Depends(get_current_user)) -> dict:
        return user

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[require_chat_access] = _granted_user

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_client() -> TestClient:
    return TestClient(_make_app(chat_enabled=True))


@pytest.fixture
def api_client_chat_disabled() -> TestClient:
    return TestClient(_make_app(chat_enabled=False))


@pytest.fixture
def logged_in_user():
    """Dummy fixture referenced by plan tests — value unused, auth is overridden."""
    return TEST_USER


# ---------------------------------------------------------------------------
# Tests (5 per plan Step 1)
# ---------------------------------------------------------------------------


def test_create_web_session(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 201
    data = r.json()
    assert data["id"].startswith("chat_")
    assert "/stream" in data["ws_url"]
    assert data["ws_ticket"]


def test_list_sessions(api_client: TestClient, logged_in_user):
    api_client.post("/api/chat/sessions", json={"surface": "web"})
    r = api_client.get("/api/chat/sessions")
    assert r.status_code == 200
    arr = r.json()
    assert len(arr) == 1
    assert arr[0]["surface"] == "web"


def test_create_session_reports_the_agent_it_runs_as(api_client: TestClient, logged_in_user):
    """``agent_id`` on the wire.

    ``chat_sessions.agent_id`` has recorded this since v101 and both backends
    round-trip it into ``ChatSession``, but no response ever carried it — which
    is what made the composer's agent picker impossible to build: a client had
    no way to learn who a conversation was with. Never null, because an unnamed
    web session is attributed to the caller's default agent.
    """
    from src.repositories import agents_repo

    r = api_client.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 201, r.text
    assert r.json()["agent_id"] == agents_repo().get_or_create_default(TEST_USER["id"])["id"]


def test_list_sessions_reports_the_agent_of_each(api_client: TestClient, logged_in_user):
    """Without this the sidebar cannot restore the picker's label on reopen."""
    created = api_client.post("/api/chat/sessions", json={"surface": "web"})
    assert created.status_code == 201, created.text
    r = api_client.get("/api/chat/sessions")
    assert r.status_code == 200
    row = next(s for s in r.json() if s["id"] == created.json()["id"])
    assert row["agent_id"] == created.json()["agent_id"]


def test_create_session_accepts_known_profile(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web", "profile": "data-package-builder"})
    assert r.status_code == 201, r.text


def test_create_session_rejects_unknown_profile(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions", json={"surface": "web", "profile": "nope"})
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "unknown_profile"


def test_get_messages_empty(api_client: TestClient, logged_in_user):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.get(f"/api/chat/sessions/{c['id']}/messages")
    assert r.status_code == 200
    assert r.json() == []


def test_get_messages_exposes_sender_email(api_client: TestClient, logged_in_user):
    """`chat.js` filters two things on `m.sender_email`: the ArrowUp prompt-recall
    stack (so a co-drive peer's prompt never surfaces under the owner's history)
    and `renderMessage`'s peer-attribution badge. Both read rows from THIS
    endpoint, which did not serialize the field — so `!m.sender_email` was
    unconditionally true, the recall filter was dead code, and peer names
    vanished from the transcript after a reload.

    The value is already carried by the repository layer and already exposed to
    participants by `/api/chat/copresence`; this route is owner-only (a
    non-owner gets 404 before reaching the payload), so serializing it here
    discloses nothing new.
    """
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    repo = api_client.app.state.chat_repo
    repo.append_message(session_id=c["id"], role="user", content="mine", sender_email=None)
    repo.append_message(session_id=c["id"], role="user", content="theirs", sender_email="peer@example.com")

    rows = api_client.get(f"/api/chat/sessions/{c['id']}/messages").json()
    assert len(rows) == 2, rows
    assert all("sender_email" in r for r in rows), (
        "chat.js's recall filter and peer badge both read m.sender_email -- omitting it makes both dead code"
    )
    assert [r["sender_email"] for r in rows] == [None, "peer@example.com"]


def test_archive_session(api_client: TestClient, logged_in_user):
    c = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    r = api_client.delete(f"/api/chat/sessions/{c['id']}")
    assert r.status_code == 204
    r2 = api_client.get("/api/chat/sessions")
    assert r2.json() == []  # archived sessions excluded


def test_create_when_disabled(api_client_chat_disabled: TestClient, logged_in_user):
    r = api_client_chat_disabled.post("/api/chat/sessions", json={"surface": "web"})
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "chat_disabled"


def test_reissue_ticket_for_existing_session(api_client: TestClient, logged_in_user):
    """``POST /sessions/{id}/ticket`` mints a fresh WS ticket against the
    SAME chat_id — used by the frontend when the user clicks an old
    conversation in the sidebar after their WS dropped. Resuming via
    the existing session preserves message history threading. Without
    this endpoint the frontend can only ``POST /sessions`` which creates
    a brand-new session each time, defeating the point of the sidebar."""
    created = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]
    original_ticket = created["ws_ticket"]

    r = api_client.post(f"/api/chat/sessions/{chat_id}/ticket")
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == chat_id
    assert body["ws_ticket"]
    assert body["ws_ticket"] != original_ticket  # fresh
    assert body["ws_url"].startswith(f"/api/chat/sessions/{chat_id}/stream?ticket=")


def test_reissue_ticket_404_for_unknown_session(api_client: TestClient, logged_in_user):
    r = api_client.post("/api/chat/sessions/chat_nonexistent/ticket")
    assert r.status_code == 404


def test_reissue_ticket_404_for_other_users_session(api_client: TestClient, logged_in_user):
    """Ticket re-issue is auth-scoped — Alice cannot mint a ticket for Bob's
    chat. The session_email check inside the handler matches ``get_session``
    against ``user["email"]`` and 404s on mismatch (same shape as the
    messages endpoint, so we don't disclose existence)."""
    created = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]

    # Re-override auth as a DIFFERENT user; ticket endpoint must refuse.
    app = api_client.app
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user2",
        "email": "bob@test.com",
        "is_admin": False,
    }
    try:
        r = api_client.post(f"/api/chat/sessions/{chat_id}/ticket")
        assert r.status_code == 404
    finally:
        # Restore Alice for any subsequent tests sharing the fixture.
        app.dependency_overrides[get_current_user] = lambda: TEST_USER


# ---------------------------------------------------------------------------
# RBAC gate — chat is default-deny
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Task 10: ws disconnect detaches (not kills) + paused flag in session list
# ---------------------------------------------------------------------------


def _make_app_with_fake_provider() -> FastAPI:
    """Like _make_app but wires a real FakeProvider so attach/detach_sink work."""
    import duckdb
    from fastapi import FastAPI

    from app.api.chat import router as chat_router
    from app.chat.config import ChatConfig
    from app.chat.workdir import WorkdirManager
    from src.db import _ensure_schema
    from tests.chat_fakes import FakeProvider

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    fake_provider = FakeProvider()
    workdir_mgr = MagicMock(spec=WorkdirManager)
    workdir_mgr.ensure_user_workdir = MagicMock()
    workdir_mgr.prepare_session_dir = MagicMock(return_value="/tmp/fake")

    config = ChatConfig(
        enabled=True,
        concurrency_per_user=3,
        on_detach="pause",
        detach_linger_seconds=0,
        idle_grace_seconds=0,
    )
    mgr = ChatManager(
        provider=fake_provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=config,
    )
    app.state.chat_manager = mgr
    app.state.chat_repo = repo
    app.state._fake_provider = fake_provider

    from app.api.chat import require_chat_access
    from app.auth.dependencies import get_current_user

    async def _granted_user(user: dict = Depends(get_current_user)) -> dict:
        return user

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[require_chat_access] = _granted_user
    return app


@pytest.fixture
def fake_provider_client():
    return TestClient(_make_app_with_fake_provider())


def test_ws_disconnect_detaches_but_does_not_kill(fake_provider_client: TestClient):
    """After WS closes, the manager session stays in _live (ACTIVE or linger),
    the runner handle is not killed — only the sink is detached."""
    app = fake_provider_client.app
    mgr = app.state.chat_manager
    provider = app.state._fake_provider

    created = fake_provider_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]
    ticket_resp = fake_provider_client.post(f"/api/chat/sessions/{chat_id}/ticket").json()
    ws_url = ticket_resp["ws_url"]

    with fake_provider_client.websocket_connect(ws_url) as ws:
        # Drain the ready frame emitted by _seat_sink.
        frame = ws.receive_json()
        assert frame["type"] == "ready"
        # WS disconnects here (context manager exit).

    # Session must still be in the live registry (not killed).
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        live = mgr._live.get(chat_id)
        assert live is not None, "session was removed from _live on WS close — should stay"
        # Handle must not have been killed (linger_task may or may not have fired
        # yet, but if it fired with linger=0 and paused, handle becomes None after
        # pause — the important invariant is we never called handle.kill()).
        provider_handle = provider.spawned[0] if provider.spawned else None
        if provider_handle is not None:
            assert not provider_handle.killed, "handle was hard-killed on WS disconnect"
    finally:
        loop.close()


def test_ws_stream_closes_4503_on_coordination_unavailable(fake_provider_client: TestClient, monkeypatch):
    """A coordination backend blip (e.g. Redis unreachable) during ticket
    consume must close the WS with 4503, not propagate an uncaught
    ``CoordinationUnavailable`` out of the WS handler — FastAPI's HTTP
    exception handler does not cover the WS scope, so an unhandled raise
    here would drop the connection ungracefully with a traceback."""
    from starlette.websockets import WebSocketDisconnect

    import app.api.chat as chat_mod
    from app.coordination.base import CoordinationUnavailable

    created = fake_provider_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]
    ticket_resp = fake_provider_client.post(f"/api/chat/sessions/{chat_id}/ticket").json()
    ws_url = ticket_resp["ws_url"]

    def _raise(_ticket: str):
        raise CoordinationUnavailable("redis blip")

    monkeypatch.setattr(chat_mod, "_consume_ticket", _raise)

    with pytest.raises(WebSocketDisconnect) as excinfo, fake_provider_client.websocket_connect(ws_url) as ws:
        ws.receive_json()
    assert excinfo.value.code == 4503


def test_sessions_list_exposes_paused(fake_provider_client: TestClient):
    """GET /api/chat/sessions includes 'paused': true for paused sessions."""
    created = fake_provider_client.post("/api/chat/sessions", json={"surface": "web"}).json()
    chat_id = created["id"]

    # Directly set paused_at on the repo row to simulate a paused session.
    from datetime import datetime

    repo = fake_provider_client.app.state.chat_repo
    repo.set_sandbox_paused_at(chat_id, datetime.now(UTC))

    r = fake_provider_client.get("/api/chat/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["paused"] is True


def test_sessions_list_not_paused_for_active(fake_provider_client: TestClient):
    """GET /api/chat/sessions includes 'paused': false for non-paused sessions."""
    fake_provider_client.post("/api/chat/sessions", json={"surface": "web"})
    r = fake_provider_client.get("/api/chat/sessions")
    assert r.status_code == 200
    sessions = r.json()
    assert len(sessions) == 1
    assert sessions[0]["paused"] is False


# ---------------------------------------------------------------------------
# Pinned conversations — PUT /sessions/{id}/pin
# ---------------------------------------------------------------------------


def test_sessions_list_unpinned_by_default(api_client: TestClient, logged_in_user):
    api_client.post("/api/chat/sessions", json={"surface": "web"})
    sessions = api_client.get("/api/chat/sessions").json()
    assert sessions[0]["pinned"] is False
    assert sessions[0]["pinned_at"] is None


def test_pin_and_unpin_round_trip(api_client: TestClient, logged_in_user):
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]

    r = api_client.put(f"/api/chat/sessions/{chat_id}/pin", json={"pinned": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": chat_id, "pinned": True}

    pinned = api_client.get("/api/chat/sessions").json()[0]
    assert pinned["pinned"] is True
    assert pinned["pinned_at"] is not None  # the timestamp orders the Pinned group

    assert api_client.put(f"/api/chat/sessions/{chat_id}/pin", json={"pinned": False}).status_code == 200
    unpinned = api_client.get("/api/chat/sessions").json()[0]
    assert unpinned["pinned"] is False
    assert unpinned["pinned_at"] is None


def test_pin_survives_messages(api_client: TestClient, logged_in_user):
    """Pinning happens on real conversations, i.e. AFTER chat_messages rows
    exist. On DuckDB 1.5.3 that combination is exactly what the FK+index bug
    breaks if ``pinned_at`` is ever indexed — this is the regression guard for
    that (the same guard test_chat_pg.py applies to the sandbox-ref columns)."""
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
    repo = api_client.app.state.chat_repo
    repo.append_message(session_id=chat_id, role="user", content="hello")
    repo.append_message(session_id=chat_id, role="assistant", content="hi")

    assert api_client.put(f"/api/chat/sessions/{chat_id}/pin", json={"pinned": True}).status_code == 200
    assert api_client.get("/api/chat/sessions").json()[0]["pinned"] is True


def test_pinned_sessions_lead_the_list(api_client: TestClient, logged_in_user):
    """The panel renders the server order, so an old pinned chat must come back
    ahead of newer unpinned ones — even though its own recency puts it last.

    Each session gets a message before the next is created: creating a session
    archives the caller's still-empty ones, so a message is what makes a session
    outlive the next ``POST /sessions``.
    """
    repo = api_client.app.state.chat_repo
    ids = []
    for n in range(3):
        chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
        repo.append_message(session_id=chat_id, role="user", content=f"msg {n}")
        ids.append(chat_id)
    first, second, third = ids

    # Sanity: plain recency order is newest-first, i.e. the reverse of creation.
    assert [s["id"] for s in api_client.get("/api/chat/sessions").json()] == [third, second, first]

    api_client.put(f"/api/chat/sessions/{first}/pin", json={"pinned": True})
    ordered = [s["id"] for s in api_client.get("/api/chat/sessions").json()]
    assert ordered == [first, third, second], "the pin leads despite being the least recent"

    # Most-recently-pinned leads within the pinned block.
    api_client.put(f"/api/chat/sessions/{second}/pin", json={"pinned": True})
    assert [s["id"] for s in api_client.get("/api/chat/sessions").json()] == [second, first, third]


def test_pin_404_for_unknown_session(api_client: TestClient, logged_in_user):
    r = api_client.put("/api/chat/sessions/chat_nonexistent/pin", json={"pinned": True})
    assert r.status_code == 404


def test_pin_404_for_other_users_session(api_client: TestClient, logged_in_user):
    """Pinning is auth-scoped — Alice cannot pin Bob's chat, and the refusal is
    a 404 (not 403) so the endpoint can't be used to probe for session ids."""
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]

    app = api_client.app
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user2",
        "email": "bob@test.com",
        "is_admin": False,
    }
    try:
        r = api_client.put(f"/api/chat/sessions/{chat_id}/pin", json={"pinned": True})
        assert r.status_code == 404
    finally:
        app.dependency_overrides[get_current_user] = lambda: TEST_USER

    # And Alice's own pin state is untouched by the refused call.
    assert api_client.get("/api/chat/sessions").json()[0]["pinned"] is False


# ---------------------------------------------------------------------------
# Rename — PUT /sessions/{id}/title (the row menu's Rename action)
# ---------------------------------------------------------------------------


def test_rename_session(api_client: TestClient, logged_in_user):
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]

    r = api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": "Q3 pipeline review"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": chat_id, "title": "Q3 pipeline review"}
    assert api_client.get("/api/chat/sessions").json()[0]["title"] == "Q3 pipeline review"


def test_rename_strips_surrounding_whitespace(api_client: TestClient, logged_in_user):
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
    r = api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": "  padded  "})
    assert r.status_code == 200
    assert r.json()["title"] == "padded"


def test_rename_rejects_blank_title(api_client: TestClient, logged_in_user):
    """An all-whitespace title is a 400, not a silent no-op: the row would
    otherwise render as "Untitled chat" with no explanation."""
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
    for blank in ("", "   ", "\t\n"):
        r = api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": blank})
        assert r.status_code == 400, blank
        assert r.json()["detail"]["kind"] == "invalid_title"


def test_rename_rejects_overlong_title(api_client: TestClient, logged_in_user):
    from app.api.chat import _TITLE_MAX

    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
    assert api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": "x" * _TITLE_MAX}).status_code == 200
    r = api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": "x" * (_TITLE_MAX + 1)})
    assert r.status_code == 400
    assert r.json()["detail"]["kind"] == "invalid_title"


def test_rename_preserves_pin_state(api_client: TestClient, logged_in_user):
    """Renaming a pinned conversation must not unpin it — they are independent
    columns, and the row menu offers both actions side by side."""
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
    api_client.put(f"/api/chat/sessions/{chat_id}/pin", json={"pinned": True})
    api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": "still pinned"})
    row = api_client.get("/api/chat/sessions").json()[0]
    assert (row["title"], row["pinned"]) == ("still pinned", True)


def test_rename_404_for_unknown_session(api_client: TestClient, logged_in_user):
    r = api_client.put("/api/chat/sessions/chat_nonexistent/title", json={"title": "nope"})
    assert r.status_code == 404


def test_rename_404_for_other_users_session(api_client: TestClient, logged_in_user):
    """Auth-scoped like the sibling routes — 404 (not 403) so the endpoint can't
    be used to probe for other users' session ids."""
    chat_id = api_client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]

    app = api_client.app
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "user2",
        "email": "bob@test.com",
        "is_admin": False,
    }
    try:
        r = api_client.put(f"/api/chat/sessions/{chat_id}/title", json={"title": "hijacked"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides[get_current_user] = lambda: TEST_USER

    assert api_client.get("/api/chat/sessions").json()[0]["title"] != "hijacked"


def test_chat_requires_rbac_grant():
    """Default-deny: a user with no chat grant (and not admin) is refused 403
    by the chat API. This is the whole-feature RBAC gate — chat is off for
    everyone until an admin grants `(group, chat, chat)`."""
    from app.api.chat import router as chat_router
    from app.auth.dependencies import _get_db

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    app.state.chat_repo = repo
    app.state.chat_manager = _make_mock_manager(repo)

    # Real require_chat_access (NOT overridden): the user belongs to no group
    # with a chat grant, so can_access returns False.
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[_get_db] = lambda: conn

    client = TestClient(app)
    for method, path in [
        ("post", "/api/chat/sessions"),
        ("get", "/api/chat/sessions"),
    ]:
        r = client.request(method, path, json={"surface": "web"})
        assert r.status_code == 403, f"{method} {path} -> {r.status_code}"


# ---------------------------------------------------------------------------
# GET/PUT /api/chat/journey — per-user onboarding journey state
# ---------------------------------------------------------------------------


def test_get_journey_defaults(api_client: TestClient, logged_in_user):
    from src.repositories import user_journey_repo

    user_journey_repo().reset(TEST_USER["id"])
    r = api_client.get("/api/chat/journey")
    assert r.status_code == 200
    assert r.json() == {
        "first_asked": False,
        "stack_setup_done": False,
        "explored_stack": False,
        "catalog_discovered": False,
        "use_anywhere": False,
        "agent_created": False,
        "onboarded": False,
        "successful_answers": 0,
    }


def test_put_journey_partial_update(api_client: TestClient, logged_in_user):
    from src.repositories import user_journey_repo

    user_journey_repo().reset(TEST_USER["id"])
    r = api_client.put("/api/chat/journey", json={"first_asked": True})
    assert r.status_code == 200
    assert r.json()["first_asked"] is True
    assert r.json()["onboarded"] is False

    r2 = api_client.put("/api/chat/journey", json={"onboarded": True, "successful_answers": 2})
    assert r2.status_code == 200
    assert r2.json()["first_asked"] is True  # previous update preserved
    assert r2.json()["onboarded"] is True
    assert r2.json()["successful_answers"] == 2

    r3 = api_client.get("/api/chat/journey")
    assert r3.json() == r2.json()


def test_put_journey_can_reset_explicit_false(api_client: TestClient, logged_in_user):
    """The "Start over" journey-panel button (#1038) PUTs explicit `false`
    for every step. `JourneyUpdateBody`'s partial-update filter only drops
    `None` (`if v is not None`), so `False` — a legitimate value, not "field
    absent" — must be written through, not silently skipped."""
    from src.repositories import user_journey_repo

    user_journey_repo().reset(TEST_USER["id"])
    api_client.put(
        "/api/chat/journey",
        json={
            "first_asked": True,
            "stack_setup_done": True,
            "explored_stack": True,
            "catalog_discovered": True,
            "use_anywhere": True,
            "agent_created": True,
        },
    )

    r = api_client.put(
        "/api/chat/journey",
        json={
            "first_asked": False,
            "stack_setup_done": False,
            "explored_stack": False,
            "catalog_discovered": False,
            "use_anywhere": False,
            "agent_created": False,
        },
    )
    assert r.status_code == 200
    assert r.json() == {
        "first_asked": False,
        "stack_setup_done": False,
        "explored_stack": False,
        "catalog_discovered": False,
        "use_anywhere": False,
        "agent_created": False,
        "onboarded": False,
        "successful_answers": 0,
    }


def test_journey_requires_rbac_grant():
    """Same default-deny gate as the rest of chat — no grant means 403."""
    from app.api.chat import router as chat_router
    from app.auth.dependencies import _get_db

    app = FastAPI()
    app.include_router(chat_router)

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    app.state.chat_repo = repo
    app.state.chat_manager = _make_mock_manager(repo)

    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    app.dependency_overrides[_get_db] = lambda: conn

    client = TestClient(app)
    r = client.get("/api/chat/journey")
    assert r.status_code == 403
