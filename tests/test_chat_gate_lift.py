"""Task 7 (wave-2F) — lift the multi-worker/replica chat gate for the redis
coordination backend, and make Slack HTTP webhook handlers thin producers.

Everything the multi-replica chat path needs (tickets, session-routing
leases + takeover, frame replay, inbound command streams, notifications
pub/sub) was already coordination-backed by the end of wave-2F tasks 1-6 —
this task only lifts the two gates that still hard-refused ``UVICORN_WORKERS
> 1`` / role-split regardless of backend, and hardens the Slack webhook
handlers so a request landing on a non-owning gateway replica doesn't try to
locally attach/spawn a session it doesn't own.

Two groups of tests:

1. Chat-gate tests mirror ONLY the (now backend-aware) ``UVICORN_WORKERS``
   branch of app/main.py's CHAT-INIT block — the same minimal-app
   convention ``tests/test_chat_deployment_gates.py`` established — but
   route the backend check through ``app.main._chat_coordination_backend``
   so monkeypatching that one function drives the mirrored branch exactly
   like the real lifespan would.

2. The Slack producer test verifies ``services.slack_bot.events``'s wave-2F
   task 7 fix: a webhook landing on a gateway replica that does NOT own the
   session's routing lease must not attempt a local attach/spawn — it calls
   straight through to ``send_user_message``, relying on
   ``ChatManager.send_user_message``'s own cross-gateway forwarding (wave-2F
   task 4, exercised end-to-end in tests/test_chat_inbound.py) to deliver it
   to the owning gateway.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main_mod
from app.roles import Role, reset_roles_cache, role_enabled

# ---------------------------------------------------------------------------
# Group 1: the UVICORN_WORKERS / coordination-backend chat gate
# ---------------------------------------------------------------------------


def _make_app_with_worker_gate() -> FastAPI:
    """Minimal app whose lifespan mirrors the CHAT-INIT gate order from
    app/main.py: gateway-role gate first, then the (now backend-aware)
    multi-worker/replica gate."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        from app.chat.config import ChatConfig

        app.state.chat_config = ChatConfig(enabled=True)

        if app.state.chat_config.enabled:
            if not role_enabled(Role.GATEWAY):
                app.state.chat_manager = None
            elif int(os.environ.get("UVICORN_WORKERS", "1")) > 1 and main_mod._chat_coordination_backend() != "redis":
                app.state.chat_manager = None
            else:
                app.state.chat_manager = object()  # sentinel: "something"

        yield

    return FastAPI(lifespan=_lifespan)


@pytest.fixture(autouse=True)
def _reset_role_cache(monkeypatch):
    monkeypatch.delenv("AGNES_ROLE", raising=False)
    reset_roles_cache()
    yield
    reset_roles_cache()


def test_redis_backend_multi_worker_enables_chat(monkeypatch):
    """coordination.backend=redis + UVICORN_WORKERS=2 -> chat_manager
    enabled (not None): tickets/leases/replay/inbound/notifications are all
    coordination-backed now, so a redis-backed multi-worker/replica
    deployment is safe."""
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr(main_mod, "_chat_coordination_backend", lambda: "redis")

    app = _make_app_with_worker_gate()
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is not None


def test_memory_backend_multi_worker_still_disables_chat(monkeypatch):
    """coordination.backend=memory (the default) + UVICORN_WORKERS=2 ->
    still disabled — unchanged S-tier posture. Process-local memory state
    cannot be shared across worker processes, so multi-worker chat stays
    unsafe on this backend."""
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr(main_mod, "_chat_coordination_backend", lambda: "memory")

    app = _make_app_with_worker_gate()
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is None


def test_single_worker_memory_backend_enables_chat(monkeypatch):
    """Baseline unchanged: UVICORN_WORKERS=1 (default), memory backend -> chat
    still enabled — the gate only ever fired at workers>1."""
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(main_mod, "_chat_coordination_backend", lambda: "memory")

    app = _make_app_with_worker_gate()
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is not None


def test_redis_backend_role_split_gateway_enables_chat(monkeypatch):
    """coordination.backend=redis + AGNES_ROLE=gateway (role-split) +
    UVICORN_WORKERS=2 -> chat still enabled. A gateway-role replica passes
    the role gate, and with the coordination backend=redis the worker-count
    gate no longer disables it either."""
    monkeypatch.setenv("AGNES_ROLE", "gateway")
    reset_roles_cache()
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.setattr(main_mod, "_chat_coordination_backend", lambda: "redis")

    app = _make_app_with_worker_gate()
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is not None


def test_non_gateway_role_disables_chat_regardless_of_backend(monkeypatch):
    """A non-gateway role-split replica (e.g. AGNES_ROLE=worker) never owns
    chat — unrelated to, and unaffected by, the coordination backend."""
    monkeypatch.setenv("AGNES_ROLE", "worker")
    reset_roles_cache()
    monkeypatch.setenv("UVICORN_WORKERS", "1")
    monkeypatch.setattr(main_mod, "_chat_coordination_backend", lambda: "redis")

    app = _make_app_with_worker_gate()
    with TestClient(app):
        assert getattr(app.state, "chat_manager", None) is None


# ---------------------------------------------------------------------------
# Group 1b: services.slack_bot.socket_mode_client.socket_mode_preflight's
# matching backend-aware relaxation (Slack Socket Mode's own leader lease —
# WS C-3 — makes N workers safe under redis the same way).
# ---------------------------------------------------------------------------


def test_socket_preflight_redis_multi_worker_ok(monkeypatch):
    from services.slack_bot import socket_mode_client as smc

    monkeypatch.setattr(smc, "_slack_sdk_importable", lambda: True)
    ok, reason = smc.socket_mode_preflight(
        workers=2,
        app_token="xapp-a",
        bot_token="xoxb-b",
        backend="redis",
    )
    assert ok is True
    assert reason == ""


def test_socket_preflight_memory_multi_worker_still_fails_closed(monkeypatch):
    from services.slack_bot import socket_mode_client as smc

    monkeypatch.setattr(smc, "_slack_sdk_importable", lambda: True)
    ok, reason = smc.socket_mode_preflight(
        workers=2,
        app_token="xapp-a",
        bot_token="xoxb-b",
        backend="memory",
    )
    assert ok is False
    assert "UVICORN_WORKERS" in reason


def test_socket_preflight_default_backend_is_memory(monkeypatch):
    """Omitting ``backend`` must not silently become permissive — the
    default keeps today's fail-closed behavior for any caller that hasn't
    been updated to pass it explicitly."""
    from services.slack_bot import socket_mode_client as smc

    monkeypatch.setattr(smc, "_slack_sdk_importable", lambda: True)
    ok, reason = smc.socket_mode_preflight(workers=2, app_token="xapp-a", bot_token="xoxb-b")
    assert ok is False
    assert "UVICORN_WORKERS" in reason


# ---------------------------------------------------------------------------
# Group 2: Slack webhook handlers as thin producers (events.py)
# ---------------------------------------------------------------------------


def test_owned_by_other_gateway_true_for_foreign_live_owner(monkeypatch):
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: "gw-B")
    assert asyncio.run(ev._owned_by_other_gateway("sess-1")) is True


def test_owned_by_other_gateway_false_when_unclaimed(monkeypatch):
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: None)
    assert asyncio.run(ev._owned_by_other_gateway("sess-1")) is False


def test_owned_by_other_gateway_false_when_self_owned(monkeypatch):
    import services.slack_bot.events as ev

    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: "gw-A")
    assert asyncio.run(ev._owned_by_other_gateway("sess-1")) is False


def _build_isolated_dm_app_state(monkeypatch):
    """Self-contained equivalent of tests.test_slack_bot._build_slack_app_state,
    using its OWN fresh in-memory DuckDB connection instead of the shared
    on-disk system.duckdb.

    tests.test_slack_bot's own helper relies on that module's
    ``_shared_slack_db`` autouse fixture to redirect ``get_system_db()`` to a
    fresh ``:memory:`` connection per test — an isolation mechanism scoped to
    tests collected FROM that module, not to helpers merely imported from it.
    Calling that helper here would hit the real, process-persistent
    ${DATA_DIR}/state/system.duckdb instead and collide with any other test
    (in this file or another) that seeded the same hardcoded ``uid1`` row.

    ``services.slack_bot.binding.lookup_user_email`` resolves through
    ``src.repositories.users_repo()`` (not ``repo._conn`` directly), so the
    factory's own ``get_system_db`` must be pointed at this same isolated
    connection too — otherwise the identity lookup silently misses and the
    handler takes the "unbound user" branch instead of the bound-user path
    these tests exercise.
    """
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import duckdb

    from app.chat.persistence import ChatRepository
    from app.chat.types import ChatSession
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid1', 'bob@example.com', 'Bob')")
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
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

    sent_kwargs: list[dict] = []

    async def send_user_message(chat_id, text, **kw):
        sent_msgs.append((chat_id, text))
        sent_kwargs.append(kw)

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
        _sent_kwargs=sent_kwargs,
    )
    state = SimpleNamespace(chat_repo=repo, chat_manager=mgr, public_url="https://agnes.example.com")
    app = SimpleNamespace(state=state)
    return app, repo, mgr, conn


def test_slack_dm_owned_elsewhere_forwards_without_local_attach(monkeypatch):
    """A DM webhook for a session owned by a DIFFERENT, still-live gateway
    replica must not attempt a local attach/spawn (which would trigger
    ChatManager.attach's cross-gateway takeover) — it goes straight to
    send_user_message, which forwards over the chat-in:{chat_id}
    coordination stream (app.chat.manager.ChatManager._forward_inbound_message,
    exercised in tests/test_chat_inbound.py)."""
    import services.slack_bot.events as ev

    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: "gw-B")

    app, _repo, mgr, conn = _build_isolated_dm_app_state(monkeypatch)
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

    assert mgr._attached == [], "must not locally attach/spawn a session owned by another gateway"
    assert mgr._sent == [("sess-1", "hello agnes")], "message must still reach send_user_message (forward path)"


def test_slack_mention_owned_elsewhere_forwards_without_local_attach(monkeypatch):
    """Same fix, channel-mention path (_handle_mention).

    Needs the same self-containment as ``_build_isolated_dm_app_state``
    above: ``is_channel_allowlisted``/``lookup_user_email``/``can_access``
    all resolve through ``src.repositories.*_repo()`` factories, which read
    ``src.repositories.get_system_db()`` — NOT the ``conn`` object passed
    around locally. Without redirecting that factory hook to this test's own
    in-memory ``conn``, the allowlist/binding lookups silently miss (hitting
    the real, process-persistent ``${DATA_DIR}/state/system.duckdb`` instead)
    and the handler takes the "channel not allowlisted" ephemeral-deny branch
    instead of the forward path this test exercises — which then crashes on
    the monkeypatched non-async ``send_ephemeral_to_user`` stub. See that
    helper's docstring for the full explanation.
    """
    import duckdb

    from src.db import _ensure_schema
    from tests.test_slack_bot import _FakeApp, _FakeMgr, _allow_channel, _seed_bound_chat_user

    import services.slack_bot.events as ev

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: "gw-B")

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    mgr = _FakeMgr()
    app = _FakeApp(conn=conn, mgr=mgr)

    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.1", "user": "U_OK", "text": "<@U07BOT> revenue?"}))

    assert mgr.attached == [], "must not locally attach/spawn a session owned by another gateway"
    assert mgr.sent and mgr.sent[0][1] == "revenue?"


def test_cmd_agnes_owned_elsewhere_forwards_without_local_attach(monkeypatch):
    """FINDING 1 (multi-replica gate lift): a `/agnes` slash command landing
    on a replica that does NOT own the session must not attach — commands.py's
    process-local `_is_attached` reads False there, and mgr.attach() would
    fire ChatManager.attach's cross-gateway TAKEOVER (destroy the live
    owner's sandbox + respawn locally) for a plain slash command. Instead the
    message is forwarded via send_user_message (with the slack_origin marker
    so the owner re-establishes its SlackSinkBridge — finding 6) and the
    response_url gets the standard ack."""
    import app.auth.access as _access
    import services.slack_bot.commands as cmds
    import services.slack_bot.events as ev

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: "gw-B")

    app, _repo, mgr, conn = _build_isolated_dm_app_state(monkeypatch)
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")

    async def fake_open_im(uid):
        return "D1"

    ephemerals: list[tuple[str, str]] = []

    async def fake_ephemeral(url, text):
        ephemerals.append((url, text))

    monkeypatch.setattr(cmds, "open_im", fake_open_im)
    monkeypatch.setattr(cmds, "send_ephemeral", fake_ephemeral)

    cmd = {"command": "/agnes", "user_id": "U123", "text": "hi agnes", "response_url": "https://hooks.slack/r1"}
    asyncio.run(cmds.dispatch_command(app, cmd))

    assert mgr._attached == [], "non-owner /agnes must not attach (attach = unwanted takeover)"
    assert mgr._sent == [("sess-1", "hi agnes")], "message must be forwarded via send_user_message"
    assert mgr._sent_kwargs and mgr._sent_kwargs[0].get("slack_origin", {}).get("channel") == "D1"
    assert ephemerals and ephemerals[0][0] == "https://hooks.slack/r1", "response_url must get the standard ack"


def _cmd_agnes_with_refusing_send(monkeypatch, *, owner: str, reason: str) -> list[tuple[str, str]]:
    """Dispatch `/agnes hi` against a manager whose ``send_user_message`` refuses
    with ``RuntimeError(reason)`` — the shape ``enforce_sender_limits`` raises —
    and return the ephemerals posted to the response_url."""
    import app.auth.access as _access
    import services.slack_bot.commands as cmds
    import services.slack_bot.events as ev

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: owner)

    app, _repo, mgr, conn = _build_isolated_dm_app_state(monkeypatch)
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")

    async def refusing_send(chat_id, text, **kw):
        raise RuntimeError(reason)

    mgr.send_user_message = refusing_send

    async def fake_open_im(uid):
        return "D1"

    ephemerals: list[tuple[str, str]] = []

    async def fake_ephemeral(url, text):
        ephemerals.append((url, text))

    monkeypatch.setattr(cmds, "open_im", fake_open_im)
    monkeypatch.setattr(cmds, "send_ephemeral", fake_ephemeral)
    cmd = {"command": "/agnes", "user_id": "U123", "text": "hi agnes", "response_url": "https://hooks.slack/r1"}
    asyncio.run(cmds.dispatch_command(app, cmd))
    return ephemerals


@pytest.mark.parametrize("owner", ["gw-B", "gw-A"])
def test_cmd_agnes_explains_a_sender_limit_refusal_instead_of_a_generic_apology(monkeypatch, owner):
    """A sender-limit refusal (here the conversation token budget) raised out
    of ``send_user_message`` used to escape ``_cmd_agnes`` into ``_run_logged``'s
    catch-all, so the slash-command user read "Something went wrong handling
    that command" while a mention or DM of the same session got the tailored
    ``_SENDER_LIMIT_MESSAGES`` copy. Both the cross-gateway forward (owner
    ``gw-B``) and the local send (owner ``gw-A``) now post that copy to the
    response_url — and never the "On it" ack, which would promise a reply
    that is not coming."""
    from services.slack_bot.events import _SENDER_LIMIT_MESSAGES

    ephemerals = _cmd_agnes_with_refusing_send(monkeypatch, owner=owner, reason="max_session_tokens_exhausted")
    texts = [t for _url, t in ephemerals]
    assert _SENDER_LIMIT_MESSAGES["max_session_tokens_exhausted"] in texts
    assert not any(t.startswith("On it") for t in texts), texts
    assert all(url == "https://hooks.slack/r1" for url, _t in ephemerals)


def test_slash_limit_helper_reraises_unknown_runtime_errors():
    """Non-vacuity: only the three sender-limit reasons are explained; any other
    RuntimeError keeps propagating to ``_run_logged`` exactly as before."""
    import services.slack_bot.commands as cmds

    async def boom():
        raise RuntimeError("sandbox exploded")

    with pytest.raises(RuntimeError, match="sandbox exploded"):
        asyncio.run(cmds._send_or_explain_limit_ephemeral(boom(), "https://hooks.slack/r1"))


def test_slack_dm_live_without_sink_reestablishes_slack_sink(monkeypatch):
    """FINDING 6 (owner side): after a cross-gateway takeover the owner's
    LiveSession exists with sinks=[] — the DM handler used to skip sink
    creation whenever a live entry existed (`_is_attached` is True), so
    replies silently stopped reaching Slack. The handler must now seat a
    SlackSinkBridge in that live-but-sinkless window."""
    from types import SimpleNamespace

    import app.auth.access as _access

    import services.slack_bot.events as ev
    from services.slack_bot.sink import SlackSinkBridge

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: "gw-A")  # THIS replica owns it

    app, _repo, mgr, conn = _build_isolated_dm_app_state(monkeypatch)
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")

    # Post-takeover shape: session is live on this replica but has no sinks.
    live_stub = SimpleNamespace(chat_id="sess-1", sinks=[])
    mgr.list_live = lambda: [live_stub]

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U123",
        "ts": "1.1",
        "text": "hello again",
    }
    asyncio.run(ev.dispatch_event(app, event))

    assert len(mgr._attached) == 1, "a live-but-sinkless Slack session must get its sink re-established"
    assert isinstance(mgr._attached[0][1], SlackSinkBridge)
    assert mgr._sent == [("sess-1", "hello again")]


def test_slack_dm_owned_locally_unaffected(monkeypatch):
    """Sanity check: when the session is unowned/owned by THIS gateway, the
    existing local attach/wait_until_live/send_user_message flow is
    unchanged (no regression from the wave-2F task 7 ownership check)."""
    import services.slack_bot.events as ev

    import app.auth.access as _access

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    monkeypatch.setattr(ev.routing, "this_gateway_id", lambda: "gw-A")
    monkeypatch.setattr(ev.routing, "owner_of", lambda chat_id: None)

    app, _repo, mgr, conn = _build_isolated_dm_app_state(monkeypatch)
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

    assert len(mgr._attached) == 1
    assert mgr._sent == [("sess-1", "hello agnes")]


# ---------------------------------------------------------------------------
# Group 3 (wave-2F final review F1): api-role replicas — chat_manager is None
# — must degrade to the chat-manager-free thin-producer path, never crash.
# ---------------------------------------------------------------------------


def _build_api_role_app_state(monkeypatch):
    """App-shaped state for an API-ROLE replica: a REAL ChatRepository +
    ChatConfig and ``chat_manager=None`` — only ``Role.GATEWAY`` processes
    construct a ChatManager (app/main.py CHAT-INIT), so this is exactly the
    state every Slack webhook handler sees on an api replica. Same
    isolation strategy as ``_build_isolated_dm_app_state`` above (own
    in-memory DuckDB + ``src.repositories.get_system_db`` redirect)."""
    from types import SimpleNamespace

    import duckdb

    from app.chat.config import ChatConfig
    from app.chat.persistence import ChatRepository
    from src.db import _ensure_schema

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    conn.execute("INSERT INTO users(id, email, name) VALUES ('uid1', 'bob@example.com', 'Bob')")
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    repo = ChatRepository(conn)
    state = SimpleNamespace(
        chat_repo=repo,
        chat_manager=None,
        chat_config=ChatConfig(enabled=True, concurrency_per_user=3),
        public_url="https://agnes.example.com",
        slack_bot_user_id="U07BOT",
    )
    return SimpleNamespace(state=state), repo, conn


@pytest.fixture()
def _fresh_coordination():
    """The producer path publishes to the process-wide coordination
    singleton — reset around each test so stream/seq state can't leak."""
    from app.coordination.factory import reset_coordination_for_tests

    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _bind_bob(conn):
    from services.slack_bot.binding import _ensure_table

    _ensure_table(conn)
    conn.execute("UPDATE users SET slack_user_id = 'U123' WHERE email = 'bob@example.com'")


def test_slack_dm_api_role_thin_producer_forwards(monkeypatch, _fresh_coordination):
    """F1: a DM webhook on an api-role replica (chat_manager=None — the
    reference Caddyfile used to route ALL HTTP there) must not crash on the
    None deref — it runs the thin-producer path: session row resolved/
    created, user message persisted, entry published on chat-in:{id} with
    its Slack origin. Load-bearing: pre-fix this dispatch raised
    AttributeError('NoneType' has no attribute 'create_session') and the
    webhook-mode Slack chat was 100% dead on api replicas."""
    import app.auth.access as _access
    import services.slack_bot.events as ev
    from app.chat import inbound

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    app, repo, conn = _build_api_role_app_state(monkeypatch)
    _bind_bob(conn)

    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U123",
        "ts": "1.1",
        "text": "hello api role",
    }
    asyncio.run(ev.dispatch_event(app, event))

    session = repo.get_slack_dm_session("D1")
    assert session is not None, "the producer must create the DM session row"
    user_msgs = [m.content for m in repo.list_messages(session.id) if m.role == "user"]
    assert user_msgs == ["hello api role"], "the producer must persist the user message"
    entries = inbound.read_new(session.id, after_seq=0)
    assert [e.get("text") for e in entries] == ["hello api role"]
    assert entries[0].get("slack", {}).get("channel") == "D1", (
        "the forwarded entry must carry its Slack origin so the owner re-seats the sink"
    )


def test_slack_mention_api_role_thin_producer_forwards(monkeypatch, _fresh_coordination):
    """F1, channel-mention path: same thin-producer degrade on an api-role
    replica, with the mention token stripped before forwarding."""
    from types import SimpleNamespace

    import duckdb

    from src.db import _ensure_schema
    from tests.test_slack_bot import _allow_channel, _seed_bound_chat_user

    import services.slack_bot.events as ev
    from app.chat import inbound
    from app.chat.config import ChatConfig
    from app.chat.persistence import ChatRepository

    monkeypatch.setattr(ev, "send_ephemeral_to_user", lambda *a, **k: None)
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)
    _seed_bound_chat_user(conn)
    _allow_channel(conn)
    repo = ChatRepository(conn)
    state = SimpleNamespace(
        chat_repo=repo,
        chat_manager=None,
        chat_config=ChatConfig(enabled=True, concurrency_per_user=3),
        public_url="https://agnes.example.com",
        slack_bot_user_id="U07BOT",
    )
    app = SimpleNamespace(state=state)

    asyncio.run(ev._handle_mention(app, {"channel": "C_OK", "ts": "9.1", "user": "U_OK", "text": "<@U07BOT> revenue?"}))

    session = repo.get_slack_thread_session("C_OK", "9.1")
    assert session is not None, "the producer must create the thread session row"
    entries = inbound.read_new(session.id, after_seq=0)
    assert [e.get("text") for e in entries] == ["revenue?"]
    assert entries[0].get("slack", {}).get("channel") == "C_OK"


def test_cmd_agnes_api_role_thin_producer_forwards_and_acks(monkeypatch, _fresh_coordination):
    """F1, /agnes slash-command path: producer forward + the standard
    'reply in your DM' response_url ack."""
    import app.auth.access as _access
    import services.slack_bot.commands as cmds
    from app.chat import inbound

    monkeypatch.setattr(_access, "can_access", lambda *a, **k: True)
    app, repo, conn = _build_api_role_app_state(monkeypatch)
    _bind_bob(conn)

    async def fake_open_im(uid):
        return "D1"

    ephemerals: list[tuple[str, str]] = []

    async def fake_ephemeral(url, text):
        ephemerals.append((url, text))

    monkeypatch.setattr(cmds, "open_im", fake_open_im)
    monkeypatch.setattr(cmds, "send_ephemeral", fake_ephemeral)

    cmd = {"command": "/agnes", "user_id": "U123", "text": "hi agnes", "response_url": "https://hooks.slack/r1"}
    asyncio.run(cmds.dispatch_command(app, cmd))

    session = repo.get_slack_dm_session("D1")
    assert session is not None
    entries = inbound.read_new(session.id, after_seq=0)
    assert [e.get("text") for e in entries] == ["hi agnes"]
    assert ephemerals and ephemerals[-1][0] == "https://hooks.slack/r1"
    assert "reply in your DM" in ephemerals[-1][1]


def test_stop_button_api_role_forwards_cancel(monkeypatch, _fresh_coordination):
    """F1, Stop-button path: on an api-role replica the click forwards a
    control:cancel to the owning gateway instead of dereferencing the
    (absent) local ChatManager."""
    import services.slack_bot.interactivity as inter

    app, repo, conn = _build_api_role_app_state(monkeypatch)
    _bind_bob(conn)

    monkeypatch.setattr("app.chat.routing.owner_of", lambda cid: "gw-B")
    monkeypatch.setattr("app.chat.routing.this_gateway_id", lambda: "api-1")
    published: list[tuple[str, str]] = []

    async def fake_publish_control(cid, command, **kw):
        published.append((cid, command))
        return 1

    monkeypatch.setattr("app.chat.inbound.publish_control", fake_publish_control)

    it = inter.Interaction(
        action_id="chat_stop",
        slack_user_id="U123",
        channel_id="D1",
        response_url="https://hooks.slack/r1",
        value={"chat_id": "sess-9", "owner": "bob@example.com"},
    )
    asyncio.run(inter._on_stop(app, it))

    assert published == [("sess-9", "cancel")]


def test_agnes_new_api_role_forwards_kill_and_archives(monkeypatch, _fresh_coordination):
    """F1, /agnes-new path: on an api-role replica the archive still lands
    locally and the kill is forwarded as a control:kill to the owner."""
    import services.slack_bot.commands as cmds
    from app.chat.types import Surface

    app, repo, conn = _build_api_role_app_state(monkeypatch)
    _bind_bob(conn)
    session = repo.create_session(
        user_email="bob@example.com",
        surface=Surface.SLACK_DM,
        slack_channel_id="D1",
    )

    monkeypatch.setattr("app.chat.routing.owner_of", lambda cid: "gw-B")
    monkeypatch.setattr("app.chat.routing.this_gateway_id", lambda: "api-1")
    published: list[tuple[str, str, str]] = []

    async def fake_publish_control(cid, command, *, reason=None):
        published.append((cid, command, reason))
        return 1

    monkeypatch.setattr("app.chat.inbound.publish_control", fake_publish_control)

    async def fake_open_im(uid):
        return "D1"

    ephemerals: list[str] = []

    async def fake_ephemeral(url, text):
        ephemerals.append(text)

    monkeypatch.setattr(cmds, "open_im", fake_open_im)
    monkeypatch.setattr(cmds, "send_ephemeral", fake_ephemeral)

    asyncio.run(cmds.dispatch_command(app, {"command": "/agnes-new", "user_id": "U123", "response_url": "r1"}))

    assert published == [(session.id, "kill", "agnes_new")]
    assert repo.get_slack_dm_session("D1") is None, "the session row must be archived locally"
    assert ephemerals and "Archived" in ephemerals[-1]


def test_cmd_status_api_role_uses_lease_count(monkeypatch, _fresh_coordination):
    """F1, /agnes-status path: reports the lease-derived count instead of
    dereferencing the absent ChatManager."""
    import services.slack_bot.commands as cmds

    app, repo, conn = _build_api_role_app_state(monkeypatch)
    _bind_bob(conn)

    ephemerals: list[str] = []

    async def fake_ephemeral(url, text):
        ephemerals.append(text)

    monkeypatch.setattr(cmds, "send_ephemeral", fake_ephemeral)

    asyncio.run(cmds.dispatch_command(app, {"command": "/agnes-status", "user_id": "U123", "response_url": "r1"}))

    assert ephemerals and "active sessions: *0* / 3" in ephemerals[-1]
