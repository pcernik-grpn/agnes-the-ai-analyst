"""Unit tests for Slack slash commands (Phase 2)."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _shared_slack_db(monkeypatch):
    """Point get_system_db() and the repo factory at one shared in-memory
    DuckDB. The Slack command handler resolves the caller via users_repo()
    (factory) now, not repo._conn, so a user seeded in _agnes_app must be
    visible through the factory on the default DuckDB backend."""
    import duckdb
    from src.db import _ensure_schema

    shared = duckdb.connect(":memory:")
    _ensure_schema(shared)
    monkeypatch.setattr("src.db.get_system_db", lambda: shared)
    monkeypatch.setattr("src.repositories.get_system_db", lambda: shared)
    yield shared


def test_send_ephemeral_posts_to_response_url(monkeypatch):
    from services.slack_bot import sender as snd

    posted = {}

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            posted["url"] = url
            posted["json"] = json
            return _FakeResp()

    monkeypatch.setattr(snd.httpx, "AsyncClient", _FakeClient)
    asyncio.run(snd.send_ephemeral("https://hooks.slack/r/1", "hi", blocks=None))
    assert posted["url"] == "https://hooks.slack/r/1"
    assert posted["json"]["response_type"] == "ephemeral"
    assert posted["json"]["text"] == "hi"
    assert "blocks" not in posted["json"]


def test_open_im_returns_channel_id(monkeypatch):
    from services.slack_bot import sender as snd

    class _FakeResp:
        status_code = 200
        def json(self):
            return {"ok": True, "channel": {"id": "D777"}}

    class _FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            assert url.endswith("/conversations.open")
            assert json == {"users": "U123"}
            return _FakeResp()

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(snd.httpx, "AsyncClient", _FakeClient)
    got = asyncio.run(snd.open_im("U123"))
    assert got == "D777"


def test_open_im_returns_none_without_token(monkeypatch):
    from services.slack_bot import sender as snd
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert asyncio.run(snd.open_im("U123")) is None


def _sign_ok(monkeypatch):
    import services.slack_bot.sigverify as sv
    monkeypatch.setattr(sv, "verify_slack_signature", lambda *a, **k: True)
    import app.api.slack as slack_api
    monkeypatch.setattr(slack_api, "verify_slack_signature", lambda *a, **k: True)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")


def _make_client(monkeypatch, scheduled):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app.api.slack as slack_api

    async def fake_dispatch(app, cmd):
        scheduled.append(cmd)

    monkeypatch.setattr(slack_api, "dispatch_command", fake_dispatch)
    app = FastAPI()
    app.include_router(slack_api.router)
    app.state.chat_repo = SimpleNamespace()
    app.state.chat_manager = SimpleNamespace()
    return TestClient(app)


def test_commands_bad_signature_401(monkeypatch):
    import app.api.slack as slack_api
    monkeypatch.setattr(slack_api, "verify_slack_signature", lambda *a, **k: False)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shhh")
    scheduled: list = []
    client = _make_client(monkeypatch, scheduled)
    r = client.post("/api/slack/commands", data={"command": "/agnes", "text": "hi"})
    assert r.status_code == 401
    assert scheduled == []  # forged command never dispatched


def test_commands_help_is_synchronous(monkeypatch):
    _sign_ok(monkeypatch)
    scheduled: list = []
    client = _make_client(monkeypatch, scheduled)
    r = client.post(
        "/api/slack/commands",
        data={"command": "/agnes", "text": "help", "user_id": "U1",
              "channel_id": "C1", "response_url": "https://r/1"},
    )
    assert r.status_code == 200
    assert "/agnes-new" in r.json()["text"]
    assert scheduled == []  # help did no async work


def test_commands_schedules_dispatch(monkeypatch):
    _sign_ok(monkeypatch)
    scheduled: list = []
    client = _make_client(monkeypatch, scheduled)
    r = client.post(
        "/api/slack/commands",
        data={"command": "/agnes", "text": "what is mrr", "user_id": "U1",
              "channel_id": "C1", "response_url": "https://r/1"},
    )
    assert r.status_code == 200
    assert len(scheduled) == 1
    assert scheduled[0]["command"] == "/agnes"
    assert scheduled[0]["text"] == "what is mrr"


def _agnes_app(monkeypatch, *, bound=True, can_chat=True):
    from types import SimpleNamespace
    from src.db import _ensure_schema, get_system_db
    from app.chat.persistence import ChatRepository
    from app.chat.types import ChatSession, Surface
    from datetime import datetime, timezone
    from services.slack_bot.binding import _ensure_table

    # get_system_db is patched by the autouse _shared_slack_db fixture to a
    # shared in-memory conn that the repo factory (users_repo) also resolves to,
    # so a user seeded here is visible to the factory-routed Slack binding
    # lookup (which no longer reads repo._conn directly).
    conn = get_system_db()
    _ensure_schema(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid1','bob@example.com','Bob')")
    repo = ChatRepository(conn)
    _ensure_table(conn)
    if bound:
        from src.repositories import users_repo
        users_repo().update(id="uid1", slack_user_id="U1")

    created: list = []
    attached: list = []
    sent: list = []

    async def create_session(*, user_email, surface, slack_channel_id=None, **kw):
        s = ChatSession(
            id="dm-1", user_email=user_email, surface=surface,
            slack_channel_id=slack_channel_id, slack_thread_ts=None, title=None,
            started_at=datetime.now(timezone.utc), last_message_at=None,
            message_count=0, archived=False,
        )
        created.append(s)
        return s

    async def attach(chat_id, sink):
        attached.append((chat_id, sink))
        await sink.send_json({"type": "assistant_message", "content": "the answer"})

    async def send_user_message(chat_id, text):
        sent.append((chat_id, text))

    async def wait_until_live(chat_id, *, timeout=30.0):
        return True

    mgr = SimpleNamespace(
        list_live=lambda: [], create_session=create_session, attach=attach,
        wait_until_live=wait_until_live,
        send_user_message=send_user_message,
        _config=SimpleNamespace(concurrency_per_user=3),
        _created=created, _attached=attached, _sent=sent,
    )
    app = SimpleNamespace(state=SimpleNamespace(
        chat_repo=repo, chat_manager=mgr, public_url="https://agnes.example.com"))

    import app.auth.access as _access
    monkeypatch.setattr(_access, "can_access", lambda *a, **k: can_chat)
    import services.slack_bot.commands as cmds
    async def fake_open_im(uid): return "D1"
    monkeypatch.setattr(cmds, "open_im", fake_open_im)
    return app, cmds


def test_agnes_happy_path_keys_on_im_channel(monkeypatch):
    from services.slack_bot import sink as sink_mod
    app, cmds = _agnes_app(monkeypatch)
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)
    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes", "text": "what is mrr", "user_id": "U1",
           "channel_id": "C_PUBLIC", "response_url": "https://r/1"}

    async def _run():
        await cmds.dispatch_command(app, cmd)
        import asyncio as _a; await _a.sleep(0.1)
    __import__("asyncio").run(_run())

    mgr = app.state.chat_manager
    assert mgr._created[0].slack_channel_id == "D1"   # IM channel, NOT C_PUBLIC
    assert mgr._sent == [("dm-1", "what is mrr")]
    assert eph == [("https://r/1", "the answer")]


def test_agnes_unbound_user_gets_code(monkeypatch):
    app, cmds = _agnes_app(monkeypatch, bound=False)
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes", "text": "hi", "user_id": "U_NEW",
           "channel_id": "C1", "response_url": "https://r/2"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))
    assert eph and "/slack/bind?code=" in eph[0][1]
    assert app.state.chat_manager._created == []   # no session for unbound


def test_agnes_no_chat_grant_denied(monkeypatch):
    app, cmds = _agnes_app(monkeypatch, can_chat=False)
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes", "text": "hi", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/3"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))
    assert eph and "admin" in eph[0][1].lower()
    assert app.state.chat_manager._created == []


def test_agnes_cap_hit_ephemeral(monkeypatch):
    app, cmds = _agnes_app(monkeypatch)
    from app.chat.manager import ConcurrencyCapHit
    async def boom(**kw): raise ConcurrencyCapHit("at cap")
    app.state.chat_manager.create_session = boom
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes", "text": "hi", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/4"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))
    assert eph and "/agnes-new" in eph[0][1]


def test_agnes_skips_ephemeral_when_session_already_attached(monkeypatch):
    """When a permanent sink (web/DM) is already pumping the resolved DM
    session, /agnes must inject the user turn but post NOTHING to
    response_url — the persistent sink keeps streaming the answer."""
    from types import SimpleNamespace
    app, cmds = _agnes_app(monkeypatch)
    mgr = app.state.chat_manager
    # The session create_session returns is keyed "dm-1"; report it as live.
    mgr.list_live = lambda: [SimpleNamespace(chat_id="dm-1")]
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes", "text": "what is mrr", "user_id": "U1",
           "channel_id": "C_PUBLIC", "response_url": "https://r/9"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))

    assert mgr._sent == [("dm-1", "what is mrr")]  # message injected
    assert eph == []                                # no ephemeral posted
    assert mgr._attached == []                      # no new sink attached


def test_agnes_new_archives_existing(monkeypatch):
    app, cmds = _agnes_app(monkeypatch)
    mgr = app.state.chat_manager

    from app.chat.types import ChatSession, Surface
    from datetime import datetime, timezone
    existing = ChatSession(
        id="dm-old", user_email="bob@example.com", surface=Surface.SLACK_DM,
        slack_channel_id="D1", slack_thread_ts=None, title=None,
        started_at=datetime.now(timezone.utc), last_message_at=None,
        message_count=1, archived=False,
    )
    killed: list = []
    archived: list = []

    # Handler calls these on the REPO:
    app.state.chat_repo.get_slack_dm_session = lambda ch: existing if ch == "D1" else None
    app.state.chat_repo.archive_session = lambda cid: archived.append(cid)
    # Handler calls kill on the MANAGER:
    async def kill(chat_id, *, reason): killed.append((chat_id, reason))
    mgr.kill = kill

    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes-new", "text": "", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/5"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))

    assert killed == [("dm-old", "agnes_new")]
    assert archived == ["dm-old"]
    assert eph and "fresh" in eph[0][1].lower()


def test_agnes_new_no_existing_still_confirms(monkeypatch):
    app, cmds = _agnes_app(monkeypatch)
    app.state.chat_repo.get_slack_dm_session = lambda ch: None
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes-new", "text": "", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/6"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))
    assert eph  # always confirms


def test_agnes_status_reports_count_and_cap(monkeypatch):
    app, cmds = _agnes_app(monkeypatch)
    mgr = app.state.chat_manager
    mgr.active_count_for_user = lambda email: 2
    mgr._config = __import__("types").SimpleNamespace(concurrency_per_user=3)
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)

    cmd = {"command": "/agnes-status", "text": "", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/7"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))
    assert eph
    body = eph[0][1]
    assert "2" in body and "3" in body
    assert "https://agnes.example.com/chat" in body


def test_agnes_status_unbound_gets_code(monkeypatch):
    app, cmds = _agnes_app(monkeypatch, bound=False)
    eph: list = []
    async def fake_eph(url, text, blocks=None): eph.append((url, text))
    monkeypatch.setattr(cmds, "send_ephemeral", fake_eph)
    cmd = {"command": "/agnes-status", "text": "", "user_id": "U_NEW",
           "channel_id": "C1", "response_url": "https://r/8"}
    __import__("asyncio").run(cmds.dispatch_command(app, cmd))
    assert eph and "/slack/bind?code=" in eph[0][1]


def test_ephemeral_command_sink_forwards_first_assistant_message(monkeypatch):
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_send)

    async def _run():
        s = sink_mod.EphemeralCommandSink(response_url="https://r/1")
        await s.send_json({"type": "token", "text": "noisy"})   # dropped
        await s.send_json({"type": "ready"})                    # dropped
        await s.send_json({"type": "assistant_message", "content": "answer"})
        await s.send_json({"type": "assistant_message", "content": "second"})  # ignored
        await s.close()

    asyncio.run(_run())
    assert sent == [("https://r/1", "answer")]


def test_ephemeral_command_sink_leaves_sender_limit_refusals_to_the_command_handler(monkeypatch):
    """Review finding on #2050: on the local `/agnes` path the sink is attached
    before the send, so a sender-limit refusal reached the user twice — the
    raw ``kind: message`` frame from here, then the tailored copy from
    ``_send_or_explain_limit_ephemeral``. The sink now skips exactly those
    kinds (and still treats the turn as over); every other error frame is
    surfaced as before."""
    from app.chat.manager import SENDER_LIMIT_FRAME_KINDS
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_send)

    async def _run():
        for kind in sorted(SENDER_LIMIT_FRAME_KINDS):
            s = sink_mod.EphemeralCommandSink(response_url="https://r/limit")
            await s.send_json({"type": "error", "kind": kind, "message": "refused"})
            assert s._delivered is True
        other = sink_mod.EphemeralCommandSink(response_url="https://r/other")
        await other.send_json({"type": "error", "kind": "engine_error", "message": "boom"})

    asyncio.run(_run())
    assert sent == [("https://r/other", ":warning: engine_error: boom")]


def test_local_agnes_refusal_is_posted_exactly_once(monkeypatch):
    """The two halves together, as the local path runs them: the manager
    broadcasts the refusal frame to the attached sink AND raises out of
    ``send_user_message``; the response_url must receive one message — the
    tailored copy — not two."""
    from app.chat.manager import session_token_budget_message
    from services.slack_bot import commands as cmds
    from services.slack_bot import sink as sink_mod
    from services.slack_bot.events import _SENDER_LIMIT_MESSAGES

    posted: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        posted.append((url, text))

    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_send)
    monkeypatch.setattr(cmds, "send_ephemeral", fake_send)
    sink = sink_mod.EphemeralCommandSink(response_url="https://r/once")

    async def refusing_send():
        # What enforce_sender_limits does, in order: frame to the sinks, then raise.
        await sink.send_json(
            {"type": "error", "kind": "max_session_tokens", "message": session_token_budget_message(150, 100)}
        )
        raise RuntimeError("max_session_tokens_exhausted")

    accepted = asyncio.run(cmds._send_or_explain_limit_ephemeral(refusing_send(), "https://r/once"))
    assert accepted is False
    assert posted == [("https://r/once", _SENDER_LIMIT_MESSAGES["max_session_tokens_exhausted"])]


def test_help_body_is_nonempty_and_lists_commands():
    from services.slack_bot.commands import _help_body
    body = _help_body()
    assert "/agnes" in body
    assert "/agnes-new" in body
    assert "/agnes-status" in body


def test_dispatch_command_routes_unknown_to_noop():
    """Unknown command must not raise — log + return."""
    from services.slack_bot import commands as cmds

    cmd = {"command": "/nope", "text": "", "user_id": "U1",
           "channel_id": "C1", "response_url": "https://r/x"}

    # Should complete without raising.
    asyncio.run(cmds.dispatch_command(app=object(), cmd=cmd))


def test_run_logged_swallows_and_posts_ephemeral(monkeypatch):
    """_run_logged must not propagate; it posts a best-effort ephemeral."""
    from services.slack_bot import commands as cmds

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(cmds, "send_ephemeral", fake_send)

    async def _boom():
        raise RuntimeError("kaboom")

    # Completes without raising; posts to the response_url it was given.
    asyncio.run(cmds._run_logged(_boom(), response_url="https://r/err"))
    assert sent and sent[0][0] == "https://r/err"
    assert "went wrong" in sent[0][1].lower()


def test_run_logged_no_response_url_still_swallows(monkeypatch):
    from services.slack_bot import commands as cmds

    async def _boom():
        raise RuntimeError("kaboom")

    # No response_url → nothing posted, but still no raise.
    asyncio.run(cmds._run_logged(_boom(), response_url=None))


def test_ephemeral_command_sink_forwards_error(monkeypatch):
    """A non-limit error frame is surfaced once, raw. Sender-limit kinds are
    the one exception — see
    test_ephemeral_command_sink_leaves_sender_limit_refusals_to_the_command_handler."""
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str]] = []

    async def fake_send(url, text, blocks=None):
        sent.append((url, text))

    monkeypatch.setattr(sink_mod, "send_ephemeral", fake_send)

    async def _run():
        s = sink_mod.EphemeralCommandSink(response_url="https://r/2")
        await s.send_json({"type": "error", "kind": "engine_error", "message": "engine turn failed"})
        await s.close()

    asyncio.run(_run())
    assert len(sent) == 1
    assert sent[0][1].startswith(":warning:")  # intact emoji (leading colon)
    assert "engine_error" in sent[0][1] and "engine turn failed" in sent[0][1]
