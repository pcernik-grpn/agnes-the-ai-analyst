"""F2d — messaging surfaces (audit-full-coverage plan, Task 6).

Covers the audit rows this task adds:
  - Telegram: telegram.bind (HTTP link route), telegram.message (inbound
    polling), telegram.script_run (the sudo path — highest value in this
    task).
  - Slack: slack.message (DM + mention), slack.command (slash commands).
  - Chat REST lifecycle: chat.session.create/delete/archive/ticket.
  - The chat manager's user_msg ingress: chat.user_message (metadata only —
    session id + character count, never the text).
  - Co-presence: chat.copresence.invite (renamed from the legacy
    "co_session_fork" — see src.audit_events.LEGACY_ALIASES)/join/leave.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src.db import get_system_db


def _params(row: dict) -> dict:
    """``AuditRepository.query()`` returns ``params`` as the raw stored JSON
    string, not a deserialized dict (unlike ``query_unified``) — parse it
    once here instead of repeating ``json.loads`` at every assertion site."""
    p = row["params"]
    return json.loads(p) if isinstance(p, str) else (p or {})


# ---------------------------------------------------------------------------
# Telegram — services/telegram_bot/bot.py, app/api/telegram.py
# ---------------------------------------------------------------------------


def test_telegram_script_run_is_audited(tmp_path, monkeypatch):
    """Highest-value row in this task: the sudo path
    (services/telegram_bot/runner.py's ``sudo -u <username> notify-scripts``).
    """
    import services.telegram_bot.bot as bot

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "get_username_by_chat_id", lambda chat_id: "alice")
    monkeypatch.setattr(bot, "run_user_script", lambda username, script_name: None)  # simulated failure
    monkeypatch.setattr(bot, "answer_callback_query", AsyncMock())
    monkeypatch.setattr(bot, "send_message", AsyncMock())

    callback_query = {"id": "cb1", "message": {"chat": {"id": 555}}, "data": "run:report.py"}
    asyncio.run(bot.handle_callback_query(callback_query))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="telegram.script_run", limit=5)
    assert rows, "expected a telegram.script_run row"
    row = rows[0]
    assert _params(row) == {"script": "report.py", "os_user": "alice"}
    assert row["result"] == "error"  # run_user_script returned None
    assert row["client_kind"] == "telegram"


def test_telegram_script_run_success_result(tmp_path, monkeypatch):
    import services.telegram_bot.bot as bot

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "get_username_by_chat_id", lambda chat_id: "alice")
    monkeypatch.setattr(bot, "run_user_script", lambda username, script_name: {"notify": False})
    monkeypatch.setattr(bot, "answer_callback_query", AsyncMock())
    monkeypatch.setattr(bot, "send_message", AsyncMock())

    callback_query = {"id": "cb2", "message": {"chat": {"id": 555}}, "data": "run:report.py"}
    asyncio.run(bot.handle_callback_query(callback_query))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="telegram.script_run", limit=5)
    assert rows and rows[0]["result"] == "success"


def test_telegram_message_is_audited_for_linked_user(tmp_path, monkeypatch):
    import services.telegram_bot.bot as bot

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "get_username_by_chat_id", lambda chat_id: "alice")
    monkeypatch.setattr(bot, "send_message", AsyncMock())
    monkeypatch.setattr(bot, "send_message_with_buttons", AsyncMock())

    asyncio.run(bot.handle_message({"chat": {"id": 555}, "text": "/status"}))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="telegram.message", limit=5)
    assert rows and _params(rows[0]) == {"command": "/status"}


def test_telegram_message_not_audited_for_unlinked_user(tmp_path, monkeypatch):
    """No account to attribute a row to — the bot must not invent one."""
    import services.telegram_bot.bot as bot

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(bot, "get_username_by_chat_id", lambda chat_id: None)
    monkeypatch.setattr(bot, "create_verification_code", lambda chat_id: "123456")
    monkeypatch.setattr(bot, "send_message", AsyncMock())

    asyncio.run(bot.handle_message({"chat": {"id": 999}, "text": "/start"}))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="telegram.message", limit=5)
    assert rows == []


def test_telegram_bind_is_audited(e2e_env, shared_app):
    from app.auth.jwt import create_access_token
    from fastapi.testclient import TestClient
    from src.repositories import notifications_pending_code_repo
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="tg1", email="tg@example.com", name="TG")
    notifications_pending_code_repo().create_code("424242", 777)

    client = TestClient(shared_app)
    hdr = {"Authorization": f"Bearer {create_access_token('tg1', 'tg@example.com')}"}
    r = client.post("/api/telegram/verify", json={"code": "424242"}, headers=hdr)
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="telegram.bind", limit=5)
    assert any(row["user_id"] == "tg1" and _params(row) == {"chat_id": 777} for row in rows), rows
    conn.close()


# ---------------------------------------------------------------------------
# Slack — services/slack_bot/events.py, commands.py
# ---------------------------------------------------------------------------


def test_slack_dm_message_is_audited(monkeypatch, e2e_env):
    import services.slack_bot.events as ev
    import app.auth.access as access_mod
    from tests.test_slack_bot import _build_slack_app_state
    from services.slack_bot.binding import _ensure_table

    monkeypatch.setattr(ev, "send_thread_reply", AsyncMock())
    # Chat's default-deny RBAC gate isn't this test's concern — see
    # test_slack_bot.py's identical monkeypatch for why.
    monkeypatch.setattr(access_mod, "can_access", lambda *a, **k: True)

    app, _repo, _mgr, conn = _build_slack_app_state()
    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U_DM' WHERE email = 'bob@example.com'")

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U_DM",
        "ts": "1.1",
        "text": "hello agnes",
    }
    asyncio.run(ev.dispatch_event(app, event))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="slack.message", limit=5)
    assert rows and _params(rows[0]) == {"surface": "dm"}
    assert rows[0]["client_kind"] == "slack"
    conn.close()


def test_slack_mention_message_is_audited(monkeypatch, e2e_env):
    import services.slack_bot.events as ev
    from tests.test_slack_bot import _FakeApp, _FakeMgr, _allow_channel, _seed_bound_chat_user

    monkeypatch.setattr(ev, "send_ephemeral_to_user", AsyncMock())
    conn = get_system_db()
    _seed_bound_chat_user(conn, email="mention@example.com", slack_id="U_MENTION")
    _allow_channel(conn, channel="C_MENTION")
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    asyncio.run(
        ev._handle_mention(
            app, {"channel": "C_MENTION", "ts": "9.1", "user": "U_MENTION", "text": "<@U07BOT> revenue?"}
        )
    )

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="slack.message", limit=5)
    assert any(_params(r) == {"surface": "mention"} for r in rows), rows
    conn.close()


def test_slack_command_is_audited(monkeypatch, e2e_env):
    """One row per dispatched slash command, resolved to the bound Agnes
    user — the actual command handlers are irrelevant to this audit wrapper
    (covered by tests/test_slack_commands.py), so they're stubbed out."""
    import services.slack_bot.commands as cmds
    from services.slack_bot.binding import _ensure_table

    monkeypatch.setattr(cmds, "_cmd_status", AsyncMock())

    class _FakeState:
        pass

    class _FakeApp:
        pass

    conn = get_system_db()
    _ensure_table(conn)  # adds users.slack_user_id
    conn.execute("INSERT INTO users(id, email, name, slack_user_id) VALUES ('u_cmd', 'cmd@x', 'C', 'U_CMD')")

    app = _FakeApp()
    app.state = _FakeState()
    app.state.chat_repo = object()  # lookup_user_email resolves via the repo factory, not this object

    asyncio.run(cmds.dispatch_command(app, {"command": "/agnes-status", "user_id": "U_CMD", "response_url": ""}))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="slack.command", limit=5)
    assert any(r["user_id"] == "u_cmd" and _params(r) == {"command": "/agnes-status"} for r in rows), rows
    conn.close()


def test_slack_command_unbound_user_gets_synthetic_identity(monkeypatch, e2e_env):
    """No bound Agnes account — still one searchable row, never a dropped one."""
    import services.slack_bot.commands as cmds

    monkeypatch.setattr(cmds, "_cmd_new", AsyncMock())

    class _FakeState:
        pass

    class _FakeApp:
        pass

    app = _FakeApp()
    app.state = _FakeState()
    app.state.chat_repo = object()

    asyncio.run(cmds.dispatch_command(app, {"command": "/agnes-new", "user_id": "U_UNBOUND", "response_url": ""}))

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="slack.command", limit=5)
    assert any(r["user_id"] == "slack:U_UNBOUND" for r in rows), rows


# ---------------------------------------------------------------------------
# Chat REST lifecycle — app/api/chat.py
# ---------------------------------------------------------------------------


@pytest.fixture
def chat_lifecycle_api(e2e_env, shared_app):
    from app.chat.persistence import ChatRepository
    from app.auth.jwt import create_access_token
    from fastapi.testclient import TestClient
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository
    from tests.test_chat_api import _make_mock_manager

    conn = get_system_db()
    UserRepository(conn).create(id="u_lifecycle", email="lifecycle@example.com", name="Lifecycle")
    grp = UserGroupsRepository(conn).create(name="chat-lifecycle", description="chat", created_by="test")
    UserGroupMembersRepository(conn).add_member("u_lifecycle", grp["id"], source="admin", added_by="test")
    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="chat", resource_id="chat", assigned_by="test", requirement="required"
    )
    repo = ChatRepository(conn)
    app = shared_app
    app.state.chat_repo = repo
    app.state.chat_manager = _make_mock_manager(repo)
    client = TestClient(app)
    hdr = {"Authorization": f"Bearer {create_access_token('u_lifecycle', 'lifecycle@example.com')}"}
    yield client, hdr
    conn.close()


def test_chat_session_create_is_audited(chat_lifecycle_api):
    client, hdr = chat_lifecycle_api
    r = client.post("/api/chat/sessions", json={"surface": "web"}, headers=hdr)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.session.create", limit=5)
    assert any(row["resource"] == f"session:{sid}" for row in rows), rows


def test_chat_session_ticket_is_audited(chat_lifecycle_api):
    client, hdr = chat_lifecycle_api
    sid = client.post("/api/chat/sessions", json={"surface": "web"}, headers=hdr).json()["id"]
    r = client.post(f"/api/chat/sessions/{sid}/ticket", headers=hdr)
    assert r.status_code == 201, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.session.ticket", limit=5)
    assert any(row["resource"] == f"session:{sid}" for row in rows), rows


def test_chat_session_archive_via_put_is_audited(chat_lifecycle_api):
    client, hdr = chat_lifecycle_api
    sid = client.post("/api/chat/sessions", json={"surface": "web"}, headers=hdr).json()["id"]
    r = client.put(f"/api/chat/sessions/{sid}/archived", json={"archived": True}, headers=hdr)
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.session.archive", limit=5)
    assert any(row["resource"] == f"session:{sid}" and _params(row) == {"archived": True} for row in rows), rows


def test_chat_session_archive_via_delete_is_audited(chat_lifecycle_api):
    client, hdr = chat_lifecycle_api
    sid = client.post("/api/chat/sessions", json={"surface": "web"}, headers=hdr).json()["id"]
    r = client.delete(f"/api/chat/sessions/{sid}", headers=hdr)
    assert r.status_code == 204, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.session.archive", limit=5)
    assert any(row["resource"] == f"session:{sid}" for row in rows), rows


def test_chat_session_delete_permanently_is_audited(chat_lifecycle_api):
    client, hdr = chat_lifecycle_api
    sid = client.post("/api/chat/sessions", json={"surface": "web"}, headers=hdr).json()["id"]
    r = client.delete(f"/api/chat/sessions/{sid}/permanent", headers=hdr)
    assert r.status_code == 204, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.session.delete", limit=5)
    assert any(row["resource"] == f"session:{sid}" for row in rows), rows


# ---------------------------------------------------------------------------
# The chat manager's user_msg ingress — app/chat/manager.py
# ---------------------------------------------------------------------------


def test_send_user_message_audits_chat_user_message(tmp_path, monkeypatch):
    from app.chat.config import ChatConfig
    from app.chat.manager import ChatManager
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    from app.chat.workdir import WorkdirManager
    import duckdb
    from src.db import _ensure_schema
    from tests.chat_fakes import FakeHandle, FakeWS, _wait_until
    from unittest.mock import AsyncMock as _AsyncMock, MagicMock

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "audit_state"))

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")
    workdir_mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    provider = MagicMock()
    provider.spawn = _AsyncMock()
    manager = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2, provider="docker"),
    )

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = _AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live)
        await manager.send_user_message(s.id, "hello there, a secret question")
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task
        return s.id

    sid = asyncio.run(_run())

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.user_message", limit=5)
    matches = [r for r in rows if _params(r).get("session_id") == sid]
    assert matches, rows
    assert _params(matches[0])["chars"] == len("hello there, a secret question")
    # Content never, metadata always — the message text itself must not
    # appear anywhere in the row's params.
    assert "secret question" not in json.dumps(_params(matches[0]))


# ---------------------------------------------------------------------------
# Co-presence — app/api/chat_copresence.py
#
# Self-contained fixtures (a copy of tests/test_copresence_api.py's seeding
# recipe, not an import of it — importing another module's `@pytest.fixture`
# purely to use its NAME as a parameter reads as an unused import to the
# post-edit ruff hook and gets auto-stripped, silently breaking fixture
# resolution at collection time).
# ---------------------------------------------------------------------------


def _seed_copresence_users(conn):
    """Owner + collaborator, both with CHAT access. Returns (owner_token,
    collab_token)."""
    from app.auth.jwt import create_access_token
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    users = UserRepository(conn)
    users.create(id="cp_owner", email="cp_owner@example.com", name="Owner")
    users.create(id="cp_collab", email="cp_collab@example.com", name="Collab")

    groups = UserGroupsRepository(conn)
    grp = groups.create(name="cp-chat-users", description="chat", created_by="test")
    members = UserGroupMembersRepository(conn)
    members.add_member("cp_owner", grp["id"], source="admin", added_by="test")
    members.add_member("cp_collab", grp["id"], source="admin", added_by="test")

    ResourceGrantsRepository(conn).create(
        group_id=grp["id"], resource_type="chat", resource_id="chat", assigned_by="test", requirement="required"
    )
    return (
        create_access_token("cp_owner", "cp_owner@example.com"),
        create_access_token("cp_collab", "cp_collab@example.com"),
    )


@pytest.fixture
def copresence_invite_ready(e2e_env, shared_app):
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    from fastapi.testclient import TestClient

    conn = get_system_db()
    owner_token, _collab_token = _seed_copresence_users(conn)
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="cp_owner@example.com", surface=Surface.WEB)
    app = shared_app
    app.state.chat_repo = repo
    client = TestClient(app)
    owner_hdr = {"Authorization": f"Bearer {owner_token}"}
    yield client, s0.id, owner_hdr
    conn.close()


def test_copresence_invite_is_audited(copresence_invite_ready):
    client, s0, owner_hdr = copresence_invite_ready
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": "cp_collab@example.com"}, headers=owner_hdr)
    assert r.status_code == 200, r.text
    s1 = r.json()["session_id"]

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.copresence.invite", limit=5)
    assert any(_params(row).get("co_session") == s1 for row in rows), rows


@pytest.fixture
def copresence_joined_ready(e2e_env, shared_app):
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    from fastapi.testclient import TestClient

    conn = get_system_db()
    _owner_token, collab_token = _seed_copresence_users(conn)
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="cp_owner@example.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        source_id=s0.id,
        owner_email="cp_owner@example.com",
        owner_user_id="cp_owner",
        invitee_email="cp_collab@example.com",
        invitee_user_id="cp_collab",
    )
    app = shared_app
    app.state.chat_repo = repo
    app.state.chat_manager = AsyncMock()
    client = TestClient(app)
    collab_hdr = {"Authorization": f"Bearer {collab_token}"}
    yield client, s1.id, collab_hdr
    conn.close()


def test_copresence_join_is_audited(copresence_joined_ready):
    client, s1, collab_hdr = copresence_joined_ready
    r = client.post(f"/api/chat/{s1}/join-ticket", headers=collab_hdr)
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.copresence.join", limit=5)
    assert any(_params(row).get("session_id") == s1 for row in rows), rows


def test_copresence_leave_is_audited(copresence_joined_ready):
    client, s1, collab_hdr = copresence_joined_ready
    r = client.post(f"/api/chat/{s1}/leave", headers=collab_hdr)
    assert r.status_code == 200, r.text

    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action="chat.copresence.leave", limit=5)
    assert any(_params(row).get("session_id") == s1 for row in rows), rows
