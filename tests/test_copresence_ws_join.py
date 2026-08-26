"""FIX 5: Live-join WS path — add_sink + sender_email threading.

Tests:
- A live participant can join via WS and add_sink is called with their email
- A non-participant ticket/email is rejected (SR-9)
- A message sent by a joiner is attributed to the joiner (sender_email=joiner)
- Primary owner path threads sender_email into send_user_message
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from src.db import _ensure_schema
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager


class FakeSink:
    def __init__(self):
        self.frames = []
        self.closed = False

    async def send_json(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed = True


def _make_repo(tmp_path: Path) -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


def _make_manager(tmp_path: Path) -> tuple[ChatManager, ChatRepository]:
    repo = _make_repo(tmp_path)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=5),
    )
    return mgr, repo


def _attach_fake_live(
    manager: ChatManager,
    chat_id: str,
    user_email: str,
    sink,
    participant_emails=None,
) -> LiveSession:
    """Insert a LiveSession with a fake handle + primary sink."""
    handle = MagicMock()
    handle.stdin = MagicMock()
    handle.stdin.drain = AsyncMock()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        sinks=[SinkEntry(participant_email=user_email, sink=sink)],
        participant_emails=participant_emails or [],
    )
    manager._live[chat_id] = live
    return live


# ---------------------------------------------------------------------------
# Tests for add_sink + sender_email
# ---------------------------------------------------------------------------


def test_add_sink_called_with_joiner_email(tmp_path):
    """A live participant joining via add_sink has their email recorded."""

    async def _run():
        mgr, repo = _make_manager(tmp_path)
        # Create a co-session
        s0 = await mgr.create_session(user_email="owner@x.com", surface=Surface.WEB)
        s1 = repo.fork_session_as_co_session(
            s0.id,
            owner_email="owner@x.com",
            owner_user_id="ou1",
            invitee_email="joiner@x.com",
            invitee_user_id="ju1",
        )
        primary_sink = FakeSink()
        live = _attach_fake_live(
            mgr,
            s1.id,
            "owner@x.com",
            primary_sink,
            participant_emails=["owner@x.com", "joiner@x.com"],
        )

        joiner_sink = FakeSink()
        await mgr.add_sink(s1.id, joiner_sink, "joiner@x.com")

        # joiner's sink must now be in live.sinks
        emails = [e.participant_email for e in live.sinks]
        assert "joiner@x.com" in emails, f"add_sink not called for joiner: {emails}"
        # joiner received ready frame
        assert any(f.get("type") == "ready" for f in joiner_sink.frames)

    asyncio.run(_run())


def test_add_sink_rejects_non_participant(tmp_path):
    """A non-participant cannot join via add_sink (SR-9)."""

    async def _run():
        mgr, repo = _make_manager(tmp_path)
        s0 = await mgr.create_session(user_email="owner@x.com", surface=Surface.WEB)
        s1 = repo.fork_session_as_co_session(
            s0.id,
            owner_email="owner@x.com",
            owner_user_id="ou1",
            invitee_email="joiner@x.com",
            invitee_user_id="ju1",
        )
        primary_sink = FakeSink()
        _attach_fake_live(
            mgr,
            s1.id,
            "owner@x.com",
            primary_sink,
            participant_emails=["owner@x.com", "joiner@x.com"],
        )

        stranger_sink = FakeSink()
        with pytest.raises(PermissionError):
            await mgr.add_sink(s1.id, stranger_sink, "stranger@x.com")

    asyncio.run(_run())


def test_send_user_message_joiner_attributed_to_joiner(tmp_path):
    """Messages from the joiner carry sender_email=joiner."""

    async def _run():
        mgr, repo = _make_manager(tmp_path)
        s0 = await mgr.create_session(user_email="owner@x.com", surface=Surface.WEB)
        primary_sink = FakeSink()
        _attach_fake_live(mgr, s0.id, "owner@x.com", primary_sink)

        await mgr.send_user_message(s0.id, "hello from joiner", sender_email="joiner@x.com")
        rows = repo.list_messages(s0.id)
        user_rows = [m for m in rows if m.role == "user"]
        assert user_rows[-1].sender_email == "joiner@x.com"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# HTTP-level tests for the co-join WS ticket and route
# ---------------------------------------------------------------------------


def _seed_copresence_app(conn):
    """Seed owner+collab+co-session; return (co_id, owner_token, collab_token,
    stranger_token)."""
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from app.auth.jwt import create_access_token
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    UserRepository(conn).create(id="wo1", email="wowner@x.com", name="Owner")
    UserRepository(conn).create(id="wc1", email="wcollab@x.com", name="Collab")
    UserRepository(conn).create(id="ws1", email="wstranger@x.com", name="Stranger")

    groups = UserGroupsRepository(conn)
    chat_grp = groups.create(name="ws-chat", description="", created_by="test")
    members = UserGroupMembersRepository(conn)
    members.add_member("wo1", chat_grp["id"], source="admin", added_by="test")
    members.add_member("wc1", chat_grp["id"], source="admin", added_by="test")
    ResourceGrantsRepository(conn).create(
        group_id=chat_grp["id"],
        resource_type="chat",
        resource_id="chat",
        assigned_by="test",
        requirement="required",
    )

    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="wowner@x.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="wowner@x.com",
        owner_user_id="wo1",
        invitee_email="wcollab@x.com",
        invitee_user_id="wc1",
    )

    owner_token = create_access_token("wo1", "wowner@x.com")
    collab_token = create_access_token("wc1", "wcollab@x.com")
    stranger_token = create_access_token("ws1", "wstranger@x.com")
    return s1.id, owner_token, collab_token, stranger_token


@pytest.fixture
def co_ws_app(e2e_env, shared_app):
    from src.db import get_system_db
    from app.chat.persistence import ChatRepository
    from fastapi.testclient import TestClient

    conn = get_system_db()
    co_id, owner_tk, collab_tk, stranger_tk = _seed_copresence_app(conn)
    app = shared_app
    app.state.chat_repo = ChatRepository(conn)
    client = TestClient(app, raise_server_exceptions=False)
    yield client, co_id, owner_tk, collab_tk, stranger_tk
    conn.close()


def test_join_ticket_returns_ws_url(co_ws_app):
    """POST /api/chat/{id}/join-ticket for a live participant → ticket + ws url."""
    client, co_id, owner_tk, collab_tk, stranger_tk = co_ws_app
    r = client.post(
        f"/api/chat/{co_id}/join-ticket",
        headers={"Authorization": f"Bearer {collab_tk}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert "ticket" in data
    assert "ws" in data
    # ws URL must reference the co session id
    assert co_id in data["ws"]


def test_join_ticket_stranger_rejected(co_ws_app):
    """Stranger requesting join-ticket → 403."""
    client, co_id, owner_tk, collab_tk, stranger_tk = co_ws_app
    r = client.post(
        f"/api/chat/{co_id}/join-ticket",
        headers={"Authorization": f"Bearer {stranger_tk}"},
    )
    assert r.status_code == 403, r.text


def test_ws_join_route_rejects_invalid_ticket(co_ws_app):
    """WS /join with a bogus ticket → close 4401."""
    client, co_id, owner_tk, collab_tk, stranger_tk = co_ws_app
    with pytest.raises(Exception):
        # starlette test client raises on WS close codes
        with client.websocket_connect(f"/api/chat/sessions/{co_id}/join?ticket=bogus") as ws:
            ws.receive_json()


def test_ws_join_route_rejects_non_participant_ticket(co_ws_app):
    """WS /join with ticket for a non-participant email → close 4403.

    Issue a valid ticket for the stranger's email directly (bypassing
    join-ticket's SR-9 gate) and connect — the WS route's own SR-9
    re-check must reject it.
    """
    client, co_id, owner_tk, collab_tk, stranger_tk = co_ws_app
    # Directly mint a ticket for a non-participant, bypassing join-ticket
    from app.api.chat import _issue_ticket

    bad_ticket = _issue_ticket(co_id, "wstranger@x.com")
    with pytest.raises(Exception):
        with client.websocket_connect(f"/api/chat/sessions/{co_id}/join?ticket={bad_ticket}") as ws:
            ws.receive_json()


def test_ws_join_route_closes_4503_on_coordination_unavailable(co_ws_app, monkeypatch):
    """A coordination backend blip during ticket consume must close the WS
    with 4503, not propagate an uncaught ``CoordinationUnavailable`` out of
    the WS handler (FastAPI's HTTP exception handler doesn't cover WS scope).
    """
    from starlette.websockets import WebSocketDisconnect

    import app.api.chat as chat_mod
    from app.coordination.base import CoordinationUnavailable

    client, co_id, owner_tk, collab_tk, stranger_tk = co_ws_app

    def _raise(_ticket: str):
        raise CoordinationUnavailable("redis blip")

    monkeypatch.setattr(chat_mod, "_consume_ticket", _raise)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/api/chat/sessions/{co_id}/join?ticket=anything") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4503
