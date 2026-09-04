"""Tests for Slack identity binding (verification code flow).

Fixture note: the plan's spec names ``open_db`` / ``migrate`` but those don't
exist in src/db.py.  The real equivalents are:
  - ``duckdb.connect(":memory:")``   to open an in-memory connection
  - ``_ensure_schema(conn)``         to migrate it to the current version
"""

import duckdb
import pytest
from src.db import _ensure_schema, get_system_db

from services.slack_bot.binding import (
    issue_verification_code,
    lookup_user_email,
    redeem_verification_code,
)


@pytest.fixture(autouse=True)
def _shared_slack_db(monkeypatch):
    """Slack identity binding now reads/writes through the repo factory
    (``users_repo()`` → ``get_system_db()``), not the test's standalone conn.
    Point both the test module's ``get_system_db`` and the factory's at one
    shared in-memory DuckDB so a user seeded in a test is visible to the
    binding lookup/redeem on the (default DuckDB) backend."""
    shared = duckdb.connect(":memory:")
    _ensure_schema(shared)
    monkeypatch.setattr("tests.test_slack_bot.get_system_db", lambda: shared, raising=False)
    monkeypatch.setattr("src.repositories.get_system_db", lambda: shared)
    yield shared


@pytest.fixture
def conn():
    c = get_system_db()
    _ensure_schema(c)
    c.execute("INSERT INTO users(id, email, name) VALUES ('uid1', 'u@x', 'U')")
    return c


def test_issue_and_redeem(conn):
    code = issue_verification_code(conn, slack_user_id="U123")
    assert len(code) == 6 and code.isdigit()
    ok = redeem_verification_code(conn, user_email="u@x", code=code)
    assert ok is True
    assert lookup_user_email(_RepoStub(conn), "U123") == "u@x"


def test_redeem_rejects_bad_code(conn):
    issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code="000000") is False


def test_redeem_rejects_expired(conn, monkeypatch):
    import services.slack_bot.binding as b

    monkeypatch.setattr(b, "_CODE_TTL_SECONDS", -1)
    code = issue_verification_code(conn, slack_user_id="U123")
    assert redeem_verification_code(conn, user_email="u@x", code=code) is False


class _RepoStub:
    def __init__(self, conn):
        self._conn = conn

    def get_slack_thread_session(self, slack_channel_id, slack_thread_ts):
        """Minimal impl: look up a chat_sessions row by channel+thread_ts."""
        from app.chat.types import ChatSession, Surface
        from datetime import datetime

        # Mirrors the REAL projection (app/chat/persistence.py): message_count
        # is DERIVED from chat_messages (the column is inert), and agent_id
        # rides along — the mention router branches on both.
        row = self._conn.execute(
            "SELECT s.id, s.user_email, s.surface, s.slack_channel_id, s.slack_thread_ts, "
            "s.title, s.started_at, MAX(m.created_at), COUNT(m.id), s.archived, s.agent_id "
            "FROM chat_sessions s LEFT JOIN chat_messages m ON m.session_id = s.id "
            "WHERE s.surface = 'slack_thread' "
            "AND s.slack_channel_id = ? AND s.slack_thread_ts = ? AND s.archived = FALSE "
            "GROUP BY ALL",
            [slack_channel_id, slack_thread_ts],
        ).fetchone()
        if not row:
            return None
        return ChatSession(
            id=row[0],
            user_email=row[1],
            surface=Surface(row[2]),
            slack_channel_id=row[3],
            slack_thread_ts=row[4],
            title=row[5],
            started_at=row[6] or datetime.now(),
            last_message_at=row[7],
            message_count=int(row[8]) if row[8] is not None else 0,
            archived=bool(row[9]),
            agent_id=row[10],
        )


# ---------------------------------------------------------------------------
# SlackSinkBridge unit tests (architect finding #4)
# ---------------------------------------------------------------------------


def test_slack_sink_forwards_assistant_message(monkeypatch):
    """assistant_message frames hit send_thread_reply with the content body."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
        await bridge.send_json({"type": "assistant_message", "content": "hello"})
        await bridge.send_json({"type": "token", "text": "noisy"})  # dropped
        await bridge.send_json({"type": "ready"})  # dropped
        await bridge.close()

    asyncio.run(_run())
    assert sent == [("D1", "1.1", "hello")]


def test_slack_sink_strips_both_wire_trailers(monkeypatch):
    """The sandbox prompt mandates two machine-readable trailers on every
    answer — ```sources (chips on the web) and ```next_actions (one-click
    buttons on the web). Slack renders neither, so a reply must carry
    neither: it would just be a fenced block of wire format under every
    answer."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    content = "Revenue was 4.2M.\n\n```sources\ntable: orders\n```\n\n```next_actions\n- Break it down by country\n```"

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
        await bridge.send_json({"type": "assistant_message", "content": content})
        await bridge.close()

    asyncio.run(_run())
    assert sent == [("D1", "1.1", "Revenue was 4.2M.")]


def test_slack_sink_nudges_to_the_web_for_an_unattended_approval(monkeypatch):
    """Slack renders no approve/deny card, so an unattended approval_request
    posts the reason plus the Continue-on-web button — otherwise the turn
    just stalls silently until the gate times out. The matching
    approval_resolved closes the loop."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    with_blocks: list[tuple[str, list]] = []
    plain: list[str] = []

    async def fake_blocks(ch, ts, text, blocks):
        with_blocks.append((text, blocks))
        return "9.9"

    async def fake_send(ch, ts, text):
        plain.append(text)

    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_blocks)
    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(
            channel="D1", thread_ts="1.1", chat_id="chat_1", web_base="https://agnes.example.com"
        )
        await bridge.send_json(
            {
                "type": "approval_request",
                "request_id": "appr-1",
                "command": "agnes admin user delete bob@x ``` <https://evil.test|click here>",
                "reason": "admin mutation",
                "attended": False,
            }
        )
        await bridge.send_json({"type": "approval_resolved", "request_id": "appr-1", "decision": "allow"})
        await bridge.close()

    asyncio.run(_run())
    assert len(with_blocks) == 1
    text, blocks = with_blocks[0]
    assert "agnes admin user delete bob@x" in text and "admin mutation" in text
    # The agent-authored command cannot close the code fence and turn the
    # rest of the nudge into live mrkdwn.
    assert text.count("```") == 2
    assert blocks[-1]["elements"][0]["url"] == "https://agnes.example.com/chat?session=chat_1"
    assert plain == ["_(approved)_"]


def test_slack_sink_stays_quiet_when_a_web_client_holds_the_card(monkeypatch):
    """`attended` means a browser is already showing the approve/deny
    buttons — nagging the Slack thread would be noise, and the resolution
    then stays silent too."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    posts: list[str] = []

    async def fake_send(ch, ts, text):
        posts.append(text)

    async def fake_blocks(ch, ts, text, blocks):
        posts.append(text)
        return "9.9"

    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)
    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_blocks)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1", chat_id="chat_1", web_base="https://x.test")
        await bridge.send_json({"type": "approval_request", "request_id": "appr-2", "attended": True})
        await bridge.send_json({"type": "approval_resolved", "request_id": "appr-2", "decision": "deny"})
        await bridge.close()

    asyncio.run(_run())
    assert posts == []


def test_slack_sink_is_not_an_approval_client():
    """The bridge must never be counted as a sink that can answer an
    approval — it is push-only, so ChatManager has to keep looking for a
    web client (or auto-deny an agent-API session)."""
    from services.slack_bot import sink as sink_mod

    bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
    assert getattr(bridge, "supports_approvals", False) is False


def test_slack_sink_forwards_error_and_cancelled(monkeypatch):
    """error + cancelled produce visible Slack posts so the user knows."""
    import asyncio
    from services.slack_bot import sink as sink_mod

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    async def _run():
        bridge = sink_mod.SlackSinkBridge(channel="D1", thread_ts="1.1")
        await bridge.send_json({"type": "error", "kind": "daily_budget", "message": "exhausted"})
        await bridge.send_json({"type": "cancelled"})
        await bridge.close()

    asyncio.run(_run())
    assert len(sent) == 2
    assert sent[0][2].startswith(":warning:")  # intact emoji (leading colon)
    assert "daily_budget" in sent[0][2]
    assert "exhausted" in sent[0][2]
    assert "stopped" in sent[1][2]


# ---------------------------------------------------------------------------
# _handle_dm tests — verification code + assistant-back pump
# ---------------------------------------------------------------------------


def _build_slack_app_state():
    """Build an app-shaped object with .state.chat_repo + .state.chat_manager.

    Uses a real ChatRepository over an in-memory DuckDB so the binding
    table CREATE works. ChatManager is mocked — we only need
    `list_live()`, `create_session()`, `attach()`, `send_user_message()`.
    """
    from types import SimpleNamespace

    from app.chat.persistence import ChatRepository
    from app.chat.types import ChatSession
    from datetime import datetime, timezone

    conn = get_system_db()
    _ensure_schema(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid1', 'bob@example.com', 'Bob')")
    repo = ChatRepository(conn)

    created_sessions: list[ChatSession] = []
    attached: list = []
    sent_msgs: list[tuple[str, str]] = []

    async def create_session(*, user_email, surface, slack_channel_id=None, **kw):
        s = ChatSession(
            id="sess-1",
            user_email=user_email,
            surface=surface,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=None,
            title=None,
            started_at=datetime.now(timezone.utc),
            last_message_at=None,
            message_count=0,
            archived=False,
        )
        created_sessions.append(s)
        return s

    async def attach(chat_id, sink):
        attached.append((chat_id, sink))
        # Simulate one assistant_message round-trip through the sink so
        # the test can assert on the reply path.
        await sink.send_json({"type": "ready"})
        await sink.send_json({"type": "assistant_message", "content": "echo: hello agnes"})

    async def send_user_message(chat_id, text):
        sent_msgs.append((chat_id, text))

    async def wait_until_live(chat_id, *, timeout=30.0):
        return True

    mgr = SimpleNamespace(
        list_live=lambda: [],
        create_session=create_session,
        attach=attach,
        wait_until_live=wait_until_live,
        send_user_message=send_user_message,
        _created=created_sessions,
        _attached=attached,
        _sent=sent_msgs,
    )

    state = SimpleNamespace(chat_repo=repo, chat_manager=mgr, public_url="https://agnes.example.com")
    app = SimpleNamespace(state=state)
    return app, repo, mgr, conn


def test_slack_dm_unbound_user_gets_verification_code(monkeypatch):
    """First DM from an unbound user → bot DMs a /slack/bind?code= link."""
    import asyncio
    import re

    from services.slack_bot import events as ev

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)

    app, _repo, _mgr, conn = _build_slack_app_state()
    # The binding tables are created lazily by `issue_verification_code`
    # on first call, but `lookup_user_email` runs *before* that and needs
    # the slack_user_id column on `users`. Force-init now.
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D2",
        "user": "U999",
        "ts": "2.2",
        "text": "hello",
    }

    asyncio.run(ev.dispatch_event(app, event))

    assert sent, "bot must reply to the unbound user"
    # Bot now replies with a one-click /slack/bind?code= magic link.
    assert any(re.search(r"/slack/bind\?code=\d{6}", text) for _ch, _ts, text in sent), sent


def test_slack_dm_bound_user_attaches_sink_and_sends(monkeypatch):
    """Bound DM → no verification code; bridge attached + user_msg forwarded."""
    import asyncio

    from services.slack_bot import events as ev

    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)
    # Chat is a default-deny RBAC resource; the Slack DM handler checks the
    # bound user's grant before spawning. These tests cover the sink/spawn
    # plumbing, not the gate, so grant access. (Default-deny is covered by
    # test_chat_api::test_chat_requires_rbac_grant.)
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app, _repo, mgr, conn = _build_slack_app_state()

    # binding._ensure_table adds the column lazily; force it now.
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U123",
        "ts": "1.1",
        "text": "hello agnes",
    }

    asyncio.run(ev.dispatch_event(app, event))

    # Created exactly one session, attached the bridge, forwarded the text.
    assert len(mgr._created) == 1
    assert mgr._created[0].user_email == "bob@example.com"
    assert len(mgr._attached) == 1
    assert mgr._attached[0][0] == "sess-1"
    # The bridge is the second tuple element — it should be a
    # SlackSinkBridge instance.
    from services.slack_bot.sink import SlackSinkBridge

    assert isinstance(mgr._attached[0][1], SlackSinkBridge)
    assert mgr._sent == [("sess-1", "hello agnes")]


def test_slack_dm_assistant_message_reaches_thread(monkeypatch):
    """End-to-end: bound DM → assistant_message frame → send_thread_reply."""
    import asyncio

    from services.slack_bot import events as ev
    from services.slack_bot import sink as sink_mod

    # The sink talks to send_thread_reply; events also calls it for the
    # binding flow. Patch both so we capture everything.
    sent: list[tuple[str, str, str]] = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)
    monkeypatch.setattr(sink_mod, "send_thread_reply", fake_send)

    # Now that _handle_dm wires chat_id, the sink uses post_thread_reply_with_blocks
    # for the first assistant turn. Capture those too.
    async def fake_post_blocks(ch, ts, text, blocks):
        sent.append((ch, ts, text))
        return "msg-1"

    monkeypatch.setattr(sink_mod, "post_thread_reply_with_blocks", fake_post_blocks)
    # Grant chat access — see note in the sibling bound-user test.
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app, _repo, _mgr, conn = _build_slack_app_state()
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U123",
        "ts": "1.1",
        "text": "hello agnes",
    }

    async def _run():
        await ev.dispatch_event(app, event)
        # The attach() task scheduled by _handle_dm runs in the same loop.
        # Give it a beat to drain the simulated assistant_message frame.
        await asyncio.sleep(0.2)

    asyncio.run(_run())

    assert any(text == "echo: hello agnes" and ch == "D1" for ch, _ts, text in sent), sent


class TestSinkBridgeChatId:
    def test_chat_id_stored_and_optional(self):
        from services.slack_bot.sink import SlackSinkBridge

        b1 = SlackSinkBridge(channel="C1", thread_ts="111.0", chat_id="sess_1")
        assert b1._chat_id == "sess_1"
        b2 = SlackSinkBridge(channel="C1", thread_ts="111.0")
        assert not b2._chat_id  # empty string or None — no chat_id means no buttons


class TestResolveBotUserId:
    def test_returns_user_id_on_ok(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        class _Resp:
            def json(self):
                return {"ok": True, "user_id": "U07BOT"}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None):
                assert url.endswith("/auth.test")
                return _Resp()

        monkeypatch.setattr(ident.httpx, "AsyncClient", _FakeClient)
        assert asyncio.run(ident.resolve_bot_user_id()) == "U07BOT"

    def test_returns_none_on_not_ok(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

        class _Resp:
            def json(self):
                return {"ok": False, "error": "invalid_auth"}

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None):
                return _Resp()

        monkeypatch.setattr(ident.httpx, "AsyncClient", _FakeClient)
        assert asyncio.run(ident.resolve_bot_user_id()) is None

    def test_returns_none_without_token(self, monkeypatch):
        import asyncio
        import services.slack_bot.identity as ident

        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        assert asyncio.run(ident.resolve_bot_user_id()) is None


class TestSendEphemeralToUser:
    def test_posts_ephemeral_with_user_and_token(self, monkeypatch):
        import asyncio
        import services.slack_bot.sender as sender_mod

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        captured = {}

        class _FakeResp:
            pass

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                return _FakeResp()

        monkeypatch.setattr(sender_mod.httpx, "AsyncClient", _FakeClient)
        asyncio.run(sender_mod.send_ephemeral_to_user("C1", "U1", "nope"))
        assert captured["url"].endswith("/chat.postEphemeral")
        assert captured["json"] == {"channel": "C1", "user": "U1", "text": "nope"}
        assert captured["headers"]["Authorization"] == "Bearer xoxb-test"

    def test_no_token_is_noop(self, monkeypatch):
        import asyncio
        import services.slack_bot.sender as sender_mod

        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        # Must not raise even though no HTTP client is patched.
        asyncio.run(sender_mod.send_ephemeral_to_user("C1", "U1", "nope"))


class TestStripBotMention:
    def test_strips_leading_mention(self):
        from services.slack_bot.events import _strip_bot_mention

        assert _strip_bot_mention("<@U07BOT> what is revenue?", "U07BOT") == "what is revenue?"

    def test_strips_mid_text_mention(self):
        from services.slack_bot.events import _strip_bot_mention

        assert _strip_bot_mention("hey <@U07BOT> hello", "U07BOT") == "hey  hello".strip()

    def test_no_bot_id_returns_trimmed(self):
        from services.slack_bot.events import _strip_bot_mention

        assert _strip_bot_mention("  hello  ", None) == "hello"

    def test_handles_angle_with_label(self):
        from services.slack_bot.events import _strip_bot_mention

        assert _strip_bot_mention("<@U07BOT|agnes> hi", "U07BOT") == "hi"


class TestChannelAllowlist:
    def _everyone_gid(self, conn):
        return conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]

    def test_default_deny(self, conn):
        from services.slack_bot.binding import is_channel_allowlisted

        assert is_channel_allowlisted(conn, "C_NEW") is False

    def test_true_after_everyone_grant(self, conn):
        from services.slack_bot.binding import is_channel_allowlisted

        gid = self._everyone_gid(conn)
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_a', ?, 'slack_channel', 'C_OK')",
            [gid],
        )
        assert is_channel_allowlisted(conn, "C_OK") is True

    def test_admin_grant_does_not_open_channel(self, conn):
        """A grant to the Admin group (not Everyone) must NOT allowlist —
        proves we do not use can_access (no admin short-circuit)."""
        from services.slack_bot.binding import is_channel_allowlisted

        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
            "VALUES ('rg_admin', ?, 'slack_channel', 'C_ADMIN')",
            [admin_gid],
        )
        assert is_channel_allowlisted(conn, "C_ADMIN") is False


class _FakeApp:
    """Mimics the bits of `app` _handle_mention touches."""

    class _State:
        pass

    def __init__(self, conn, mgr, *, bot_user_id="U07BOT", public_url="https://example.com"):
        self.state = _FakeApp._State()
        self.state.chat_repo = _RepoStub(conn)
        self.state.chat_manager = mgr
        self.state.slack_bot_user_id = bot_user_id
        self.state.public_url = public_url


class _FakeMgr:
    def __init__(self):
        self.created = []
        self.sent = []
        self.attached = []
        self._live = []

    def list_live(self):
        return self._live

    async def create_session(self, **kw):
        from app.chat.types import ChatSession
        from datetime import datetime

        self.create_kwargs = getattr(self, "create_kwargs", [])
        self.create_kwargs.append(kw)

        sess = ChatSession(
            id="sess_new",
            user_email=kw["user_email"],
            surface=kw["surface"],
            slack_channel_id=kw.get("slack_channel_id"),
            slack_thread_ts=kw.get("slack_thread_ts"),
            title=None,
            started_at=datetime.now(),
            last_message_at=None,
            message_count=0,
            archived=False,
        )
        self.created.append(sess)
        return sess

    async def attach(self, chat_id, sink):
        self.attached.append((chat_id, sink))

    async def wait_until_live(self, chat_id, *, timeout=30.0):
        return True

    async def send_user_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        self.sent_kwargs = kw


def test_mention_bot_loop_guard_returns_silently(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev

    posts = []
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: posts.append(a))
    conn = get_system_db()
    _ensure_schema(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"bot_id": "B1", "channel": "C1", "ts": "1.0", "user": "U07BOT"}))
    assert posts == [] and mgr.created == []


def test_mention_self_user_loop_guard_returns_silently(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev

    posts = []
    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: posts.append(a))
    conn = get_system_db()
    _ensure_schema(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C1", "ts": "1.0", "user": "U07BOT", "text": "<@U07BOT> hi"}))
    assert posts == [] and mgr.created == []


def test_mention_not_allowlisted_ephemeral_deny(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    from services.slack_bot.binding import _ensure_table

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append((ch, u, txt))

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = get_system_db()
    _ensure_schema(conn)
    _ensure_table(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_X", "ts": "1.0", "user": "U1", "text": "<@U07BOT> hi"}))
    assert posts and "isn't enabled" in posts[0][2]
    assert mgr.created == []


def test_mention_unbound_user_gets_code(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev
    from services.slack_bot.binding import _ensure_table

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append((ch, u, txt))

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = get_system_db()
    _ensure_schema(conn)
    _ensure_table(conn)
    gid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg1', ?, 'slack_channel', 'C_OK')",
        [gid],
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "1.0", "user": "U_NEW", "text": "<@U07BOT> hi"}))
    assert posts and "/slack/bind?code=" in posts[0][2]
    assert mgr.created == []


def _seed_bound_chat_user(conn, *, email="u@x", slack_id="U_OK"):
    """Seed a user bound to slack_id, in Everyone, with a CHAT grant.
    Primes the lazy users.slack_user_id column first (binding._ensure_table)."""
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)  # adds users.slack_user_id if missing
    uid = f"uid_{slack_id}"
    conn.execute("DELETE FROM users WHERE email = ?", [email])
    conn.execute(
        "INSERT INTO users(id, email, name, slack_user_id) VALUES (?, ?, 'U', ?)",
        [uid, email, slack_id],
    )
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'system_seed')",
        [uid, egid],
    )
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) "
        "VALUES ('rg_chat', ?, 'chat', 'chat') ON CONFLICT DO NOTHING",
        [egid],
    )
    return uid


def _allow_channel(conn, channel="C_OK"):
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO resource_grants(id, group_id, resource_type, resource_id) VALUES ('rg_ch', ?, 'slack_channel', ?)",
        [egid, channel],
    )


def test_mention_happy_path_creates_thread_and_sends(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.1", "user": "U_OK", "text": "<@U07BOT> revenue?"}))
    assert len(mgr.created) == 1
    assert mgr.created[0].surface.value == "slack_thread"
    assert mgr.created[0].slack_thread_ts == "9.1"
    assert mgr.attached and mgr.attached[0][0] == "sess_new"
    assert mgr.sent and mgr.sent[0][1] == "revenue?"
    # The mention-path sink must carry the starter's owner + web_base so the
    # owner-gated Stop button and Continue-on-web link work on thread sessions
    # (regression: the sink was previously built without owner/web_base, which
    # made the Stop button encode an empty owner and always deny).
    sink = mgr.attached[0][1]
    assert sink._owner == "u@x"
    assert sink._web_base == "https://example.com"


def test_mention_ownership_reject_ephemeral(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append(txt)

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn, email="owner@x", slack_id="U_OWNER")
    _seed_bound_chat_user(conn, email="other@x", slack_id="U_OTHER")
    _allow_channel(conn)
    # pre-existing thread session owned by owner@x (column is started_at)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_owned', 'owner@x', 'slack_thread', 'C_OK', '9.2', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.2", "user": "U_OTHER", "text": "<@U07BOT> hi"}))
    # owner has a bound slack id → rendered as <@U_OWNER>
    assert posts and "belongs to <@U_OWNER>" in posts[0]
    assert mgr.created == []


def test_mention_same_thread_reuses_session(monkeypatch):
    """A second mention in the same thread by the OWNER must NOT be rejected
    (no ownership reject) and proceeds to send. (Real dedup to a single row is
    ChatManager.create_session's job via get_slack_thread_session; the handler
    only enforces the owner check.)"""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    # existing session owned by the SAME user (column is started_at)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_mine', 'u@x', 'slack_thread', 'C_OK', '9.3', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.3", "user": "U_OK", "text": "<@U07BOT> again"}))
    assert mgr.sent and mgr.sent[0][1] == "again"


def _seed_channel_bound_agent(conn, channel="C_OK", *, owner="uid_U_OK", slug="router", knowledge=None):
    """An agent profile holding ('slack_channel', <channel>) — the mention
    router's lookup target. Uses the repo factory like the handler does.

    ``knowledge`` (design doc §12 tests) is a list of data_package ids the
    agent is grounded in — encoded exactly like the `/agents` builder
    encodes it (opaque JSON text on the `knowledge` column)."""
    import json

    from src.repositories import agents_repo

    agent_id = f"ag_{slug}"
    # Bound agents must be non-passthrough (at least one 'selected' mode) —
    # an all-'all' agent would ride the owner's plain identity and is
    # refused by both the API guard and the routing-time defense.
    agents_repo().create(
        id=agent_id,
        owner_user_id=owner,
        name="Router",
        slug=slug,
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
        knowledge=json.dumps(knowledge or []),
    )
    agents_repo().set_scope(agent_id, [("slack_channel", channel)])
    return agent_id


def test_mention_routed_channel_gets_agent_header_and_reaction(monkeypatch):
    """A channel bound to an agent profile routes the session to that agent:
    create_session carries agent_id, the FIRST turn is prefixed with the
    [slack context: ...] header, and the mention gets an :eyes: ack."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append((channel, ts, emoji))

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    agent_id = _seed_channel_bound_agent(conn, owner=uid)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(
        ev._handle_mention(app, {"channel": "C_OK", "ts": "9.5", "user": "U_OK", "text": "<@U07BOT> draft this"})
    )
    assert mgr.created and mgr.create_kwargs[0].get("agent_id") == agent_id
    assert mgr.sent
    sent_text = mgr.sent[0][1]
    assert sent_text.startswith("[slack context: channel=C_OK thread_ts=9.5 message_ts=9.5 sender=<@U_OK>]")
    assert sent_text.endswith("draft this")
    assert reactions == [("C_OK", "9.5", "eyes")]


def test_mention_routed_channel_with_policied_scope_gets_a_notice(monkeypatch):
    """Design doc §12 — a FRESH routing to an agent whose scope holds an
    access-policied table posts a one-time, channel-visible notice: a
    routed thread runs entirely as the agent's OWNER, so every mention here
    answers with the owner's row slice through that policy, never the
    mentioner's own — unlike this same agent's API/chat/delegated-turn
    callers, who are filtered by their OWN identity instead."""
    import asyncio

    import services.slack_bot.events as ev
    from src.repositories.table_registry import TableRegistryRepository
    from tests.conftest import grant_table_via_package

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    notices = []

    async def _fake_reply(channel, thread_ts, text):
        notices.append((channel, thread_ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", _fake_reply)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)

    registry = TableRegistryRepository(conn)
    registry.register(id="tbl_orders", name="orders", source_type="keboola", query_mode="local", server_only=True)
    registry.set_access_policy(
        "tbl_orders",
        sql="SELECT * FROM orders WHERE list_contains($user_groups, unit)",
        note="unit filter",
        updated_by="admin",
    )
    pkg_id = grant_table_via_package(conn, "tbl_orders", uid, group_name="owner-pkg-orders")
    _seed_channel_bound_agent(conn, owner=uid, slug="router-policy", knowledge=[pkg_id])

    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.51", "user": "U_OK", "text": "<@U07BOT> hi"}))

    assert len(notices) == 1
    channel, thread_ts, text = notices[0]
    assert channel == "C_OK" and thread_ts == "9.51"
    assert "orders" in text
    assert "owner" in text.lower()


def test_mention_routed_channel_without_policied_scope_gets_no_notice(monkeypatch):
    """Contrast case — an agent grounded in nothing policied posts none."""
    import asyncio

    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    notices = []

    async def _fake_reply(channel, thread_ts, text):
        notices.append((channel, thread_ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", _fake_reply)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-nopolicy")

    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.52", "user": "U_OK", "text": "<@U07BOT> hi"}))

    assert notices == []


def test_mention_unbound_channel_stays_unrouted_and_unprefixed(monkeypatch):
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append((channel, ts, emoji))

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.6", "user": "U_OK", "text": "<@U07BOT> hello"}))
    assert mgr.created and mgr.create_kwargs[0].get("agent_id") is None
    assert mgr.sent and mgr.sent[0][1] == "hello"
    assert reactions == []


def test_mention_routed_existing_thread_with_messages_gets_no_second_header(monkeypatch):
    """Dedupe wins over the binding: an existing thread session that already
    DELIVERED a turn keeps its agent and gets no second context header — but
    every mention still acks."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append((channel, ts, emoji))

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-2")
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at, agent_id) VALUES "
        "('s_bound', 'u@x', 'slack_thread', 'C_OK', '9.7', NULL, current_timestamp, 'ag_router-2')"
    )
    # message_count is DERIVED from chat_messages, not the column — seed a row.
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content) VALUES ('m_bound1', 's_bound', 'user', 'hi')"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(
        ev._handle_mention(app, {"channel": "C_OK", "ts": "9.7", "user": "U_OK", "text": "<@U07BOT> follow-up"})
    )
    # No second slack-context header — but follow-ups on a shared routed
    # thread carry sender attribution so the agent knows WHO is asking.
    assert mgr.sent and mgr.sent[0][1] == "[slack sender=<@U_OK>]\nfollow-up"
    assert reactions == [("C_OK", "9.7", "eyes")]


def test_mention_routed_zero_message_session_still_gets_header(monkeypatch):
    """A first mention that timed out on startup persists the session row
    but delivers nothing — the RETRY must still carry the slack-context
    header, or the agent permanently never learns its channel/thread ids.
    Keyed on message_count == 0, not row absence."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-4")
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at, agent_id) VALUES "
        "('s_stalled', 'u@x', 'slack_thread', 'C_OK', '9.9', NULL, current_timestamp, 'ag_router-4')"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.9", "user": "U_OK", "text": "<@U07BOT> retry"}))
    assert mgr.sent
    assert mgr.sent[0][1].startswith("[slack context: channel=C_OK thread_ts=9.9 ")
    assert mgr.sent[0][1].endswith("retry")


def test_mention_routed_but_foreign_thread_rejected_without_ack(monkeypatch):
    """An ack on a mention we then refuse promises an answer that never
    comes: the ownership gate runs BEFORE the 👀 reaction, so a mention on a
    pre-existing AGENT-LESS thread (owned by a human who is not the bound
    agent's owner) gets the ephemeral rejection and no acknowledgement
    mark."""
    import asyncio
    import services.slack_bot.events as ev

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append(txt)

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append((channel, ts, emoji))

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn, email="owner2@x", slack_id="U_OWNER2")
    _seed_bound_chat_user(conn, email="other2@x", slack_id="U_OTHER2")
    _allow_channel(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid_boss2', 'boss2@x', 'Boss') ON CONFLICT DO NOTHING")
    _seed_channel_bound_agent(conn, owner="uid_boss2", slug="router-3")
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_foreign', 'owner2@x', 'slack_thread', 'C_OK', '9.8', NULL, current_timestamp)"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.8", "user": "U_OTHER2", "text": "<@U07BOT> hi"}))
    assert posts and "belongs to" in posts[0]
    assert reactions == []
    assert mgr.created == []


def test_mention_routed_session_runs_as_the_agents_owner(monkeypatch):
    """A routed session is created AS THE AGENT'S OWNER — session row, sink
    owner, and (downstream) sandbox workspace all key off one identity, so
    the same bound agent presents identical rails regardless of who mentions
    it and never inherits a mentioner's personal CLAUDE.local.md."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)  # mentioner u@x / U_OK
    _allow_channel(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid_boss', 'boss@x', 'Boss') ON CONFLICT DO NOTHING")
    _egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES ('uid_boss', ?, 'system_seed') "
        "ON CONFLICT DO NOTHING",
        [_egid],
    )
    _seed_channel_bound_agent(conn, owner="uid_boss", slug="router-owner")
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.10", "user": "U_OK", "text": "<@U07BOT> draft"}))
    assert mgr.create_kwargs[0]["user_email"] == "boss@x"
    assert mgr.create_kwargs[0]["agent_id"] == "ag_router-owner"
    sink = mgr.attached[0][1]
    assert sink._owner == "boss@x"
    # The mentioner still appears as sender attribution in the context header.
    assert "sender=<@U_OK>" in mgr.sent[0][1]


def test_mention_routed_thread_continued_by_second_gated_user(monkeypatch):
    """Routed threads belong to the agent's owner, so ANY gated channel
    member may continue them — the reviewer-asks-for-a-revision flow. The
    turn carries the new sender's attribution."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)  # first user u@x / U_OK
    _seed_bound_chat_user(conn, email="second@x", slack_id="U_SECOND")
    _allow_channel(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid_boss3', 'boss3@x', 'Boss') ON CONFLICT DO NOTHING")
    _egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES ('uid_boss3', ?, 'system_seed') "
        "ON CONFLICT DO NOTHING",
        [_egid],
    )
    _seed_channel_bound_agent(conn, owner="uid_boss3", slug="router-shared")
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at, agent_id) VALUES "
        "('s_shared', 'boss3@x', 'slack_thread', 'C_OK', '9.11', NULL, current_timestamp, 'ag_router-shared')"
    )
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content) VALUES ('m_shared1', 's_shared', 'user', 'hi')"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(
        ev._handle_mention(app, {"channel": "C_OK", "ts": "9.11", "user": "U_SECOND", "text": "<@U07BOT> shorten it"})
    )
    assert mgr.sent and mgr.sent[0][1] == "[slack sender=<@U_SECOND>]\nshorten it"


def test_mention_prebinding_human_thread_keeps_working_for_its_starter(monkeypatch):
    """A binding governs NEW threads only: a thread started before the
    channel was bound still belongs to its human starter, runs unrouted (no
    header, no ack), and is NOT hijacked or bricked by the binding."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append((channel, ts, emoji))

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)  # starter u@x / U_OK
    _allow_channel(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid_boss4', 'boss4@x', 'Boss') ON CONFLICT DO NOTHING")
    _seed_channel_bound_agent(conn, owner="uid_boss4", slug="router-late")
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at) VALUES "
        "('s_prebind', 'u@x', 'slack_thread', 'C_OK', '9.12', NULL, current_timestamp)"
    )
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content) VALUES ('m_prebind1', 's_prebind', 'user', 'hi')"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(
        ev._handle_mention(app, {"channel": "C_OK", "ts": "9.12", "user": "U_OK", "text": "<@U07BOT> continue"})
    )
    # Unrouted: plain text, no header/prefix, no ack; the starter is served.
    assert mgr.sent and mgr.sent[0][1] == "continue"
    assert reactions == []


def _seed_stored_agent(conn, *, owner_id="uid_boss5", owner_email="boss5@x", slug="stored"):
    """A non-passthrough agent whose owner holds CHAT and which holds NO
    ('slack_channel', …) scope item — the shape a service thread's stored
    agent_id points at once its channel has been unbound."""
    from src.repositories import agents_repo

    conn.execute(
        "INSERT INTO users(id, email, name) VALUES (?, ?, 'Boss') ON CONFLICT DO NOTHING",
        [owner_id, owner_email],
    )
    egid = conn.execute("SELECT id FROM user_groups WHERE name='Everyone'").fetchone()[0]
    conn.execute(
        "INSERT INTO user_group_members(user_id, group_id, source) VALUES (?, ?, 'system_seed') ON CONFLICT DO NOTHING",
        [owner_id, egid],
    )
    agent_id = f"ag_{slug}"
    agents_repo().create(
        id=agent_id,
        owner_user_id=owner_id,
        name="Stored",
        slug=slug,
        plugins_mode="selected",
        connections_mode="selected",
        tables_mode="selected",
        memory_mode="selected",
    )
    return agent_id


def test_mention_service_thread_survives_unbinding(monkeypatch):
    """Unbinding a channel stops NEW routed threads; an existing service
    thread (agent_id set) stays continuable by gated members — the session
    keeps its stored agent, which is re-checked (exists, not deleted, not
    all-'all', owner still holds CHAT) on the way through."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    # NO binding for C_OK in this test — but a service thread exists, and its
    # stored agent is a real, still-healthy row (a dangling agent_id is the
    # separate refusal case below).
    agent_id = _seed_stored_agent(conn)
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at, agent_id) VALUES "
        "('s_unbound', 'boss5@x', 'slack_thread', 'C_OK', '9.13', NULL, current_timestamp, ?)",
        [agent_id],
    )
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content) VALUES ('m_unbound1', 's_unbound', 'user', 'hi')"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(
        ev._handle_mention(app, {"channel": "C_OK", "ts": "9.13", "user": "U_OK", "text": "<@U07BOT> tweak it"})
    )
    assert mgr.sent and mgr.sent[0][1] == "[slack sender=<@U_OK>]\ntweak it"


def test_mention_service_thread_refuses_agent_widened_after_unbinding(monkeypatch):
    """A stored agent_id outlives its binding, so the service-thread path
    must re-run the routing invariants the binding path enforces.

    An agent widened to all-'all' AFTER its channel was unbound would
    otherwise keep serving the thread on the owner's PLAIN identity JWT:
    the session was created as the owner, so the broker's
    `_mint_identity_jwt` never diverts to `mint_agent_session_jwt` and the
    owner's admin short-circuit rides along. Refuse instead of delivering.
    """
    import asyncio
    import services.slack_bot.events as ev
    from src.repositories import agents_repo

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append(txt)

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)  # the second, gated member who mentions
    _allow_channel(conn)
    # Owner-owned service thread pointing at an agent that was bound, then
    # unbound (scope item removed) and widened to passthrough.
    agent_id = _seed_stored_agent(conn, slug="widened")
    agents_repo().set_scope(agent_id, [])
    agents_repo().update(agent_id, plugins_mode="all", connections_mode="all", tables_mode="all", memory_mode="all")
    conn.execute(
        "INSERT INTO chat_sessions(id, user_email, surface, slack_channel_id, "
        "slack_thread_ts, title, started_at, agent_id) VALUES "
        "('s_widened', 'boss5@x', 'slack_thread', 'C_OK', '9.19', NULL, current_timestamp, ?)",
        [agent_id],
    )
    conn.execute(
        "INSERT INTO chat_messages(id, session_id, role, content) VALUES ('m_widened1', 's_widened', 'user', 'hi')"
    )
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.19", "user": "U_OK", "text": "<@U07BOT> do it"}))
    assert posts and "no longer available" in posts[-1]
    assert mgr.sent == []
    assert mgr.created == []


def test_mention_cap_hit_gets_ephemeral_not_silence(monkeypatch):
    """Routed sessions pool on the agent owner's concurrency cap; hitting it
    must answer the mentioner, not silently drop the mention."""
    import asyncio
    import services.slack_bot.events as ev
    from app.chat.manager import ConcurrencyCapHit

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append(txt)

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-cap")

    class _CapMgr(_FakeMgr):
        async def create_session(self, **kw):
            raise ConcurrencyCapHit("cap")

    mgr = _CapMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.14", "user": "U_OK", "text": "<@U07BOT> busy"}))
    assert posts and "at capacity" in posts[-1]
    assert mgr.sent == []


def test_mention_cap_hit_on_api_replica_gets_ephemeral_not_silence(monkeypatch):
    """Same refusal on the api-role replica (`app.state.chat_manager is
    None`), which reaches the cap through the thin-producer forward.

    `ConcurrencyCapHit` is a plain `Exception`, not a `RuntimeError`, so
    `_send_or_explain_limit` used to let it unwind into `_run_logged` —
    which logs and swallows, leaving the mentioner with the 👀 ack and then
    nothing at all.
    """
    import asyncio
    import services.slack_bot.events as ev
    from app.chat.manager import ConcurrencyCapHit

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append(txt)

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)

    produced = []

    async def _fake_produce(app, **kw):
        produced.append(kw)
        raise ConcurrencyCapHit("cap")

    monkeypatch.setattr(ev, "_produce_slack_message", _fake_produce)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-cap-api")

    app = _FakeApp(conn=conn, mgr=None)  # api-role replica: no ChatManager
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.20", "user": "U_OK", "text": "<@U07BOT> busy"}))
    assert produced, "the producer forward should have been attempted"
    assert posts and "at capacity" in posts[-1]


def test_mention_binding_skipped_when_owner_lost_chat_grant(monkeypatch):
    """A routed session runs AS the owner, so revoking the owner's CHAT
    access must also stop the binding — mentions degrade to the unrouted
    profile instead of spawning sessions under a revoked identity."""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append((channel, ts, emoji))

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)  # mentioner has CHAT via Everyone
    _allow_channel(conn)
    # Owner exists but holds NO chat grant: not in Everyone, no groups.
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid_nochat', 'nochat@x', 'Gone') ON CONFLICT DO NOTHING")
    _seed_channel_bound_agent(conn, owner="uid_nochat", slug="router-revoked")
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.15", "user": "U_OK", "text": "<@U07BOT> hi"}))
    # Unrouted: mentioner-owned session, no agent, no ack, plain text.
    assert mgr.create_kwargs[0]["user_email"] == "u@x"
    assert mgr.create_kwargs[0]["agent_id"] is None
    assert mgr.sent and mgr.sent[0][1] == "hi"
    assert reactions == []


def test_mention_passthrough_agent_binding_is_never_routed(monkeypatch):
    """Defense in depth: a legacy binding pointing at an all-'all' agent is
    skipped — its routed turns would ride the owner's PLAIN identity (admin
    short-circuit included) via the broker's passthrough optimization."""
    import asyncio
    import services.slack_bot.events as ev
    from src.repositories import agents_repo

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    reactions = []

    async def _fake_react(channel, ts, emoji):
        reactions.append(1)

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    agents_repo().create(id="ag_allall", owner_user_id=uid, name="AllAll", slug="router-allall")
    agents_repo().set_scope("ag_allall", [("slack_channel", "C_OK")])
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.16", "user": "U_OK", "text": "<@U07BOT> hi"}))
    assert mgr.create_kwargs[0].get("agent_id") is None
    assert mgr.sent and mgr.sent[0][1] == "hi"
    assert reactions == []


def test_mention_sender_limit_gets_ephemeral_not_silence(monkeypatch):
    """A sender-limit refusal (daily budget / session tokens / rate — keyed
    on the AGENT OWNER for routed threads) answers the mentioner with an
    ephemeral instead of vanishing into the background-task log."""
    import asyncio
    import services.slack_bot.events as ev

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append(txt)

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-limit")

    class _LimitMgr(_FakeMgr):
        async def send_user_message(self, chat_id, text, **kw):
            raise RuntimeError("daily_budget_exhausted")

    mgr = _LimitMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.17", "user": "U_OK", "text": "<@U07BOT> hi"}))
    assert posts and "daily spend cap" in posts[-1]


def test_mention_unknown_runtime_error_still_raises(monkeypatch):
    """Only KNOWN limit reasons are translated — anything else propagates to
    _run_logged so real faults keep their stack trace."""
    import asyncio
    import pytest
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)

    async def _fake_react(channel, ts, emoji):
        return None

    monkeypatch.setattr(ev, "add_reaction", _fake_react)
    conn = get_system_db()
    _ensure_schema(conn)
    uid = _seed_bound_chat_user(conn)
    _allow_channel(conn)
    _seed_channel_bound_agent(conn, owner=uid, slug="router-boom")

    class _BoomMgr(_FakeMgr):
        async def send_user_message(self, chat_id, text, **kw):
            raise RuntimeError("something_else_entirely")

    mgr = _BoomMgr()
    app = _FakeApp(conn=conn, mgr=mgr)
    with pytest.raises(RuntimeError, match="something_else_entirely"):
        asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.18", "user": "U_OK", "text": "<@U07BOT> hi"}))


def test_mention_attach_not_awaited_returns_under_budget(monkeypatch):
    """Smoke test: the handler never blocks on a hanging attach().

    attach() is scheduled fire-and-forget and blocks for the session's
    lifetime, so the handler must reach send_user_message without awaiting it.
    It awaits liveness via wait_until_live (mocked True by _FakeMgr here), not
    attach() — this guards against anyone reintroducing a direct
    ``await mgr.attach(...)`` that would deadlock the dispatch. (The original
    3s-ack framing no longer applies: these handlers run post-ack in
    background tasks.)"""
    import asyncio
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = get_system_db()
    _ensure_schema(conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)

    blocker = asyncio.Event()  # never set

    class _BlockingMgr(_FakeMgr):
        async def attach(self, chat_id, sink):
            self.attached.append((chat_id, sink))
            await blocker.wait()  # would hang if awaited

    mgr = _BlockingMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    async def _run():
        await asyncio.wait_for(
            ev._handle_mention(app, {"channel": "C_OK", "ts": "9.4", "user": "U_OK", "text": "<@U07BOT> q"}),
            timeout=2.0,
        )

    asyncio.run(_run())
    assert mgr.sent  # handler reached step 9 despite attach blocking


def test_slack_app_mention_dispatches(monkeypatch):
    """`app_mention` events are dispatched to `_handle_mention` without error.

    The stub log-based check is replaced now that the full handler is
    implemented. This test verifies dispatch_event routes `app_mention` to
    the handler; the handler returns silently when the channel isn't
    allowlisted (default-deny) — no session is created, no exception raised.
    """
    import asyncio
    import services.slack_bot.events as ev

    posts = []

    async def _fake_ep(ch, u, txt):
        posts.append((ch, u, txt))

    monkeypatch.setattr(ev, "send_ephemeral_to_user", _fake_ep)

    conn = get_system_db()
    _ensure_schema(conn)
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    event = {
        "type": "app_mention",
        "channel": "C1",
        "thread_ts": "1.1",
        "user": "U999",
        "text": "<@U07BOT> hello",
    }

    asyncio.run(ev.dispatch_event(app=app, event=event))

    # Channel not allowlisted → ephemeral "isn't enabled" deny, no session created.
    assert posts and "isn't enabled" in posts[0][2]
    assert mgr.created == []


def test_slack_dm_posts_starting_up_when_session_never_lives(monkeypatch):
    """Bug B timeout branch: if the session never becomes live (sandbox fails
    to come up), the bound DM handler posts a 'starting up' notice and does NOT
    call send_user_message — which would raise SessionNotFound."""
    import asyncio

    from services.slack_bot import events as ev

    sent: list = []

    async def fake_send(ch, ts, text):
        sent.append((ch, ts, text))

    monkeypatch.setattr(ev, "send_thread_reply", fake_send)
    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)

    app, _repo, mgr, conn = _build_slack_app_state()

    async def _never_live(chat_id, *, timeout=30.0):
        return False

    mgr.wait_until_live = _never_live

    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U123",
        "ts": "1.1",
        "text": "hello agnes",
    }
    asyncio.run(ev.dispatch_event(app, event))

    assert mgr._sent == [], "must not inject the turn when the session never became live"
    assert any("starting up" in t for _ch, _ts, t in sent), sent


def test_the_nudge_does_not_promise_a_web_link_it_cannot_build(monkeypatch):
    """`continue_on_web_block` returns None without a configured public URL, so
    the nudge fell back to a plain reply still telling the user to "open the
    chat on the web" — an instruction they cannot act on, followed by the full
    approval timeout. Before this branch a Slack-origin ask was denied
    instantly, so that wording is a regression for that deployment shape
    (Devin Review on #1157)."""
    import asyncio

    from services.slack_bot.sink import SlackSinkBridge

    sent = []

    async def _fake_reply(channel, thread_ts, text):
        sent.append(text)

    import services.slack_bot.sink as sink_mod

    monkeypatch.setattr(sink_mod, "send_thread_reply", _fake_reply)

    bridge = SlackSinkBridge(channel="C1", thread_ts="1.0", chat_id="chat_x", owner="u@x", web_base="")
    asyncio.run(bridge._post_approval_request({"request_id": "a1", "command": "rm -rf /tmp/x", "reason": "why"}))

    assert sent, "no reply was posted"
    assert "open the chat on the web" not in sent[0].lower(), "promised a link it cannot build"
    assert "PUBLIC_URL" in sent[0], "should name the knob that actually feeds this bridge"
