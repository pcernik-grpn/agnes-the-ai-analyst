"""ChatManager tests — Task 5.1: create_session (+ 5.2: attach/send/cancel/crash).

Uses asyncio.run() per the project convention (no pytest-asyncio required).
See tests/test_chat_subprocess_provider.py for precedent.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import duckdb
import pytest

from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS, _wait_until

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    """The per-sender message-rate window and daily-token counters
    (wave-2C task 4) now live in the coordination-backend singleton, which
    persists across tests in this file that reuse the same "u@x" identity
    — reset it so one test's rate/quota usage never bleeds into another's."""
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _make_workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir()
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


@pytest.fixture
def manager(tmp_path: Path) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )


# ---------------------------------------------------------------------------
# Task 5.1 tests
# ---------------------------------------------------------------------------


def test_create_session_persists(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        assert s.id.startswith("chat_")
        assert s.surface == Surface.WEB

    asyncio.run(_run())


def test_create_session_web_archives_prior_empty(manager: ChatManager):
    """Clicking '+ New chat' repeatedly should never accumulate orphan
    Untitled-chat rows. create_session on the WEB surface soft-archives
    every empty session this user has, except the just-created one."""

    async def _run():
        a = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        b = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        # `a` has zero messages → should be archived by `b`'s create.
        ar = manager._repo.get_session(a.id)
        br = manager._repo.get_session(b.id)
        assert ar is not None and ar.archived is True
        assert br is not None and br.archived is False

    asyncio.run(_run())


def test_create_session_web_does_not_archive_sessions_with_messages(
    manager: ChatManager,
):
    async def _run():
        a = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=a.id, role="user", content="hi")
        _ = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ar = manager._repo.get_session(a.id)
        assert ar is not None and ar.archived is False

    asyncio.run(_run())


def test_create_session_slack_dm_does_not_run_empty_gc(manager: ChatManager):
    """The empty-session GC is web-only — Slack DM/thread surfaces
    de-dupe via channel/thread id at the manager layer and their
    "empty" sessions are intentionally kept for re-attach."""

    async def _run():
        # First Slack DM session, no messages.
        a = await manager.create_session(
            user_email="u@x",
            surface=Surface.SLACK_DM,
            slack_channel_id="C1",
        )
        # Create a WEB session for the same user — must not touch `a`.
        _ = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ar = manager._repo.get_session(a.id)
        assert ar is not None and ar.archived is False

    asyncio.run(_run())


def test_create_session_disabled_raises(manager: ChatManager):
    """create_session raises RuntimeError when chat.enabled is False."""
    disabled_mgr = ChatManager(
        provider=manager._provider,
        workdir_mgr=manager._workdir_mgr,
        repo=manager._repo,
        config=ChatConfig(enabled=False),
    )

    async def _run():
        with pytest.raises(RuntimeError, match="chat.enabled is false"):
            await disabled_mgr.create_session(user_email="u@x", surface=Surface.WEB)

    asyncio.run(_run())


# FakeHandle and FakeWS live in tests/chat_fakes (imported above).

# ---------------------------------------------------------------------------
# Task 5.2 tests
# ---------------------------------------------------------------------------


def test_spawn_sets_agnes_server_not_agnes_api(manager: ChatManager, tmp_path, monkeypatch):
    """The runner env must carry AGNES_SERVER (the var the CLI reads) sourced
    from SERVER_URL — not the dead AGNES_API the CLI ignores."""
    monkeypatch.setenv("SERVER_URL", "https://chat.example.com")
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle()

    manager._provider.spawn = fake_spawn

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, tmp_path)

    asyncio.run(_run())

    env = captured["env"]
    assert env["AGNES_SERVER"] == "https://chat.example.com"
    assert "AGNES_API" not in env
    # Chat sandbox secret broker (2026-07-14): the real session JWT is never
    # forwarded into the sandbox env — it flows to the runner via a
    # ticket_push stdin frame instead (see tests/test_chat_manager.py's
    # secret-broker section below).
    assert "AGNES_TOKEN" not in env


def test_attach_pumps_tokens_to_ws(manager: ChatManager):
    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        # Wait for ws to be seated as a sink (not just the LiveSession to
        # exist) — the pump task can drain an already-queued emit() and
        # broadcast it before _seat_sink runs, in which case the frame is
        # gone for good (broadcast doesn't replay to late-seated sinks).
        await _wait_until(lambda: _ws_seated(manager, s.id, ws))
        handle.emit({"type": "token", "text": "Hi"})
        await _wait_until(lambda: any(m.get("type") == "token" for m in ws.sent))
        # Frames now also carry seq/id (wave-2F task 2 envelope) — assert on
        # the original fields via subset containment rather than exact dict
        # equality so this test doesn't couple to the envelope's shape.
        tokens = [m for m in ws.sent if m.get("type") == "token"]
        assert any(m.get("text") == "Hi" for m in tokens)

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_kill_revokes_broker_tickets(manager: ChatManager):
    """kill() revokes the session's broker tickets so no stale rows linger in
    the DB past teardown (Devin review on #849)."""
    from src.repositories import ticket_repo

    async def _run():
        manager._provider.spawn = AsyncMock(return_value=FakeHandle())
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        tok = ticket_repo().mint(s.id, "main")
        assert ticket_repo().resolve(tok) is not None
        await manager.kill(s.id, reason="test_done")
        assert ticket_repo().resolve(tok) is None

    asyncio.run(_run())


def test_send_writes_to_stdin(manager: ChatManager):
    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live)
        await manager.send_user_message(s.id, "hello")
        assert any(b'"hello"' in b for b in handle._stdin_buf)
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_send_user_message_emits_chat_message_usage_event(manager: ChatManager, monkeypatch):
    """Every user chat turn lands one chat.message row in usage_events (via
    the server-event emitter) so /admin/telemetry and the adoption DAU count
    web + Slack chat activity, not just desktop CC sessions."""
    import app.chat.manager as manager_mod

    emitted: list[dict] = []

    class _FakeUsageRepo:
        def emit_server_event(self, **kw):
            emitted.append(kw)
            return "evt-1"

    class _FakeUsersRepo:
        def get_by_email(self, email):
            return {"id": "user-123", "email": email}

    monkeypatch.setattr(manager_mod, "usage_repo", lambda: _FakeUsageRepo())
    monkeypatch.setattr(manager_mod, "users_repo", lambda: _FakeUsersRepo())

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live)
        await manager.send_user_message(s.id, "hello")
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task
        return s.id

    sid = asyncio.run(_run())
    assert len(emitted) == 1
    ev = emitted[0]
    assert ev["event_type"] == "chat.message"
    assert ev["username"] == "u@x"
    assert ev["user_id"] == "user-123"
    assert ev["props"] == {"surface": "web", "session_id": sid}


def test_send_user_message_emit_failure_does_not_break_send(manager: ChatManager, monkeypatch):
    """A broken telemetry backend must never block a chat turn."""
    import app.chat.manager as manager_mod

    def _boom():
        raise RuntimeError("telemetry down")

    monkeypatch.setattr(manager_mod, "usage_repo", _boom)

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live)
        await manager.send_user_message(s.id, "hello")
        assert any(b'"hello"' in b for b in handle._stdin_buf)
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_send_user_message_emits_slack_surface(manager: ChatManager, monkeypatch):
    """Slack DM turns carry surface='slack_dm' in the emitted event props."""
    import app.chat.manager as manager_mod

    emitted: list[dict] = []

    class _FakeUsageRepo:
        def emit_server_event(self, **kw):
            emitted.append(kw)
            return "evt-1"

    monkeypatch.setattr(manager_mod, "usage_repo", lambda: _FakeUsageRepo())
    monkeypatch.setattr(manager_mod, "users_repo", lambda: (_ for _ in ()).throw(RuntimeError("no users db")))

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="D123")
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live)
        await manager.send_user_message(s.id, "hello")
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())
    assert len(emitted) == 1
    assert emitted[0]["props"]["surface"] == "slack_dm"
    # users lookup failed → falls back to user_id=None, username still set
    assert emitted[0]["user_id"] is None
    assert emitted[0]["username"] == "u@x"


def test_cancel_emits_synthetic_tool_result(manager: ChatManager):
    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(manager, s.id, ws))
        handle.emit({"type": "tool_call", "tool": "run_query", "args": {}})
        await _wait_until(lambda: any(m.get("type") == "tool_call" for m in ws.sent))
        await manager.cancel(s.id)
        await _wait_until(lambda: any(m.get("type") == "cancelled" for m in ws.sent))
        cancelled = [m for m in ws.sent if m.get("type") == "cancelled"]
        assert cancelled, "expected a {'type': 'cancelled'} frame after cancel"
        # Synthetic tool_result must be emitted before the `cancelled` frame
        # so the agent sees the cancellation in its conversation history.
        synthetic = [
            m
            for m in ws.sent
            if m.get("type") == "tool_result"
            and isinstance(m.get("result"), dict)
            and m["result"].get("cancelled") is True
        ]
        assert synthetic, f"expected synthetic tool_result with cancelled=true; got {ws.sent}"
        # And it must be persisted so crash-respawn replay sees it too.
        msgs = manager._repo.list_messages(s.id)
        persisted_cancels = [
            m
            for m in msgs
            if m.tool_calls and any(isinstance(tc, dict) and tc.get("cancelled") is True for tc in m.tool_calls)
        ]
        assert persisted_cancels, "expected persisted cancel marker in chat_messages"
        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_crash_respawns_with_notice(manager: ChatManager):
    async def _run():
        handles = [FakeHandle(), FakeHandle()]
        spawn_calls = iter(handles)

        async def fake_spawn(**kw):
            return next(spawn_calls)

        manager._provider.spawn = fake_spawn

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(manager, s.id, ws))
        # Simulate crash by signalling EOF and non-zero exit
        handles[0].emit_eof()
        handles[0].killed = True  # makes wait() return 137 immediately
        await _wait_until(
            lambda: any(m.get("type") == "error" and m.get("kind") == "subprocess_crashed" for m in ws.sent)
        )
        crashed = [m for m in ws.sent if m.get("type") == "error" and m.get("kind") == "subprocess_crashed"]
        assert crashed, "expected crash notice"
        ready = [m for m in ws.sent if m.get("type") == "ready"]
        assert ready, "expected ready frame after respawn"

        await manager.kill(s.id, reason="test_done")
        handles[1].emit_eof()
        await attach_task

    asyncio.run(_run())


def test_send_user_message_rejects_when_rate_limit_exceeded(tmp_path):
    """Per-user sliding-window rate limit: more than rate_messages_per_hour
    messages within the last hour from one user gets refused.
    """
    from datetime import datetime, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True,
        concurrency_per_user=5,
        rate_messages_per_hour=3,
        daily_anthropic_spend_usd=10**6,
        max_session_tokens=10**9,
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        # 3 messages allowed
        for i in range(3):
            await mgr.send_user_message(s.id, f"msg{i}")
        # 4th gets refused
        with pytest.raises(RuntimeError, match="rate_limit_exceeded"):
            await mgr.send_user_message(s.id, "msg3")
        kinds = [c.args[0].get("kind") for c in ws.send_json.call_args_list]
        assert "rate_limit" in kinds

    asyncio.run(_run())


def test_send_user_message_rejects_when_session_tokens_exhausted(tmp_path):
    """send_user_message must refuse new turns when the session's cumulative
    tokens exceed ChatConfig.max_session_tokens.

    Before this knob was wired the value lived only in instance.yaml.
    """
    from datetime import datetime, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True,
        concurrency_per_user=5,
        max_session_tokens=100,
        daily_anthropic_spend_usd=10**6,  # disable daily cap
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        # Stuff history past the cap.
        for _ in range(3):
            repo.append_message(
                session_id=s.id,
                role="assistant",
                content="x",
                tokens_in=30,
                tokens_out=30,
                model="fake",
            )
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with pytest.raises(RuntimeError, match="max_session_tokens_exhausted"):
            await mgr.send_user_message(s.id, "next")
        # Refusal frame surfaced to the WS.
        kinds = [c.args[0].get("kind") for c in ws.send_json.call_args_list]
        assert "max_session_tokens" in kinds

    asyncio.run(_run())


def test_idle_reaper_kills_sessions_older_than_max_session_seconds(tmp_path):
    """Sessions that exceed ChatConfig.max_session_seconds get killed by the
    idle reaper independently of idle TTL.

    Before this knob was wired the value lived only in instance.yaml — operators
    set it and nothing happened.
    """
    from datetime import datetime, timedelta, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True,
        concurrency_per_user=5,
        # Pin a tiny wallclock cap so the test is fast and deterministic.
        max_session_seconds=1,
        idle_ttl_seconds=10**9,  # disable idle path
        # Deliberately the DEFAULT pause policy: max_session_seconds is a hard
        # ceiling and must KILL even when on_detach="pause" — pausing would
        # re-trip on every post-resume sweep (infinite pause/resume loop,
        # PR #605 review finding).
        on_detach="pause",
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        now = datetime.now(timezone.utc)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        # Inject an "old" live session — active_seconds_accum already past the cap.
        # No sinks: with no attached browser the reaper kills outright (no pause).
        live = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=None,
            started_at=now - timedelta(seconds=5),
            last_activity=now,
            sinks=[],
        )
        # Set accumulated active time past max_session_seconds=1 so the reaper fires.
        live.active_seconds_accum = 5.0
        mgr._live[s.id] = live

        await mgr._reap_once()  # single sweep; no sleep loop
        assert s.id not in mgr._live, "expected stale session to be killed"
        # Killed for real — not paused: no sandbox refs left to resume from.
        row = repo.get_session(s.id)
        assert row.sandbox_paused_at is None and row.sandbox_id is None

    asyncio.run(_run())


def test_crash_respawn_does_not_accumulate_pump_tasks(manager: ChatManager):
    """Each respawn must replace (not append to) the per-session pump task.

    Pre-fix: every crash respawn created a new pump task and pushed it onto
    ``live.tasks`` while leaving the old (already-exited) one on the list.
    After N crashes the manager held N+1 pump tasks of which only the
    latest read from the live handle — a leak; tests can also see it
    grow unboundedly.
    """

    async def _run():
        handles = [FakeHandle(), FakeHandle(), FakeHandle()]
        spawn_calls = iter(handles)

        async def fake_spawn(**kw):
            return next(spawn_calls)

        manager._provider.spawn = fake_spawn

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        # _spawn_live inserts the LiveSession into self._live BEFORE it
        # creates+assigns the pump/wait tasks, so bare `_live` membership
        # races ahead of `live.tasks` being populated — wait for the tasks
        # too, matching the very first assertion below.
        await _wait_until(lambda: s.id in manager._live and len(manager._live[s.id].tasks) == 2)
        live = manager._live[s.id]
        initial_tasks = list(live.tasks)
        assert len(initial_tasks) == 2  # pump + wait
        assert live.current_pump is not None
        assert live.current_pump in initial_tasks

        # First crash → respawn. `live.handle` is reassigned to the new
        # handle well before the new pump task replaces the old one in
        # `live.tasks` (history replay + ticket push happen in between) —
        # poll for the actual pump-task swap, not just the handle change,
        # or the "still exactly two tasks" assert below can race ahead of
        # the respawn actually finishing.
        old_pump = live.current_pump
        handles[0].emit_eof()
        handles[0].killed = True
        await _wait_until(
            lambda: live.current_pump is not old_pump and len([t for t in live.tasks if not t.done()]) == 2
        )
        # After respawn, still exactly two tasks (one wait + one pump),
        # not three.  current_pump points at the NEW pump.
        post_crash_tasks = [t for t in live.tasks if not t.done()]
        assert len(post_crash_tasks) == 2, f"expected 2 live tasks after crash respawn, got {len(post_crash_tasks)}"
        assert live.current_pump is not None
        assert live.current_pump in post_crash_tasks

        # Second crash → respawn again
        old_pump = live.current_pump
        handles[1].emit_eof()
        handles[1].killed = True
        await _wait_until(
            lambda: live.current_pump is not old_pump and len([t for t in live.tasks if not t.done()]) == 2
        )
        post_crash2_tasks = [t for t in live.tasks if not t.done()]
        assert len(post_crash2_tasks) == 2, f"expected 2 live tasks after 2nd respawn, got {len(post_crash2_tasks)}"

        # Cleanup
        try:
            await manager.kill(s.id, reason="test_done")
        except Exception:
            pass
        for h in handles:
            h.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            attach_task.cancel()

    asyncio.run(_run())


def test_daily_token_budget_uses_shared_coordination_counter(tmp_path):
    """Wave-2C task 4: the daily-spend check no longer hits the DB aggregate
    on every send — it reads coordination-backend counters that
    ``_record_daily_tokens`` keeps up to date as turns complete (see
    ``ChatManager._daily_token_totals``). The very first check of the day
    for a user is a ``(0, 0)`` counter reading, which is ambiguous (fresh
    quota vs. restart-lost history — see
    ``ChatManager._seed_daily_tokens_from_db_if_needed``) so it DOES consult
    ``repo.daily_anthropic_tokens`` once as a fallback seed; a second,
    same-day send must NOT call it again (the per-day "seeded" marker skips
    the DB round trip once a bucket has been checked). And — the actual
    point of routing this through the coordination backend — a second
    ChatManager instance (standing in for another app process) must see the
    same running total once a turn's tokens are recorded, not an
    independent zero.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True,
        concurrency_per_user=5,
        daily_anthropic_spend_usd=10**6,  # effectively unlimited
        max_session_tokens=10**9,
        rate_messages_per_hour=10**6,
    )
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=cfg,
    )

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with patch.object(repo, "daily_anthropic_tokens", wraps=repo.daily_anthropic_tokens) as mock_fn:
            await mgr.send_user_message(s.id, "msg1")
            await mgr.send_user_message(s.id, "msg2")
            assert mock_fn.call_count == 1, (
                f"expected the DB aggregate consulted exactly once (the first-ever miss's "
                f"restart-fallback seed), never again once the day-bucket is marked seeded; "
                f"got {mock_fn.call_count} calls"
            )

        # A completed turn's tokens are recorded against the running
        # counters (this is what _pump_subprocess_to_ws does for a real
        # assistant_message frame).
        mgr._record_daily_tokens("u@x", 1000, 2000)

        # Another ChatManager instance (another process, sharing the same
        # coordination backend) must see the identical accumulated total —
        # not its own independent, zeroed cache.
        mgr2 = ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg)
        assert mgr2._daily_token_totals("u@x") == (1000, 2000)

    asyncio.run(_run())


def test_daily_token_totals_seeds_from_db_after_restart(tmp_path):
    """Restart-forgets-spend regression (review finding): a process restart
    under the default ``memory`` coordination backend wipes the running
    daily-token counters. Without a DB fallback this silently reset a
    user's spend to 0, re-opening the full daily budget on a routine
    mid-day deploy even though ``chat_messages`` still held the real
    spend. Record real spend directly in the DB (the durable source of
    truth), simulate a restart by wiping the coordination backend, then
    confirm the very next check seeds from the DB aggregate
    (``ChatRepository.daily_anthropic_tokens``) and still blocks an
    over-budget user instead of handing them a fresh, forgotten quota.
    """
    from datetime import datetime, timezone

    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    cfg = ChatConfig(
        enabled=True,
        concurrency_per_user=5,
        daily_anthropic_spend_usd=1.0,  # low cap — 100k output tokens blows well past it
        max_session_tokens=10**9,
        rate_messages_per_hour=10**6,
    )
    mgr = ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=cfg)

    async def _seed_history():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        # A turn's tokens landed in chat_messages before the "restart" —
        # this is what ChatRepository.daily_anthropic_tokens still sees.
        repo.append_message(
            session_id=s.id,
            role="assistant",
            content="hi",
            tokens_in=0,
            tokens_out=100_000,
        )
        return s

    s = asyncio.run(_seed_history())
    assert repo.daily_anthropic_tokens("u@x") == (0, 100_000)

    # Simulate a process restart: the memory coordination backend's
    # running counters (and any "seeded" marker) are wiped. The DB row
    # above is untouched — it lives in a separate, durable store.
    reset_coordination_for_tests()

    async def _check_after_restart():
        ws = MagicMock()
        ws.send_json = AsyncMock()
        mgr._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            sinks=[SinkEntry(participant_email="u@x", sink=ws)],
        )
        with pytest.raises(RuntimeError, match="daily_budget_exhausted"):
            await mgr.send_user_message(s.id, "msg-after-restart")

    asyncio.run(_check_after_restart())

    # The seed is durable for the rest of the day-bucket, not just the one
    # check: the running counter itself now reflects the DB aggregate.
    assert mgr._daily_token_totals("u@x") == (0, 100_000)


def test_daily_token_totals_seed_race_has_single_winner(tmp_path):
    """Double-seed race regression: two requests racing on the exact same
    first-ever ``(0, 0)`` miss must not both seed the coordination counter
    from the DB aggregate — that would double-count today's real spend.
    The short-lived seed lease
    (``ChatManager._seed_daily_tokens_from_db_if_needed``) must make
    exactly one of them perform the seed; the persisted counter must land
    on the DB aggregate exactly once, never doubled, regardless of which
    thread "wins".
    """
    import threading

    from app.coordination.factory import coordination

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    mgr = ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=ChatConfig(enabled=True))

    async def _seed_history():
        s = await mgr.create_session(user_email="race@x", surface=Surface.WEB)
        repo.append_message(session_id=s.id, role="assistant", content="hi", tokens_in=500, tokens_out=700)

    asyncio.run(_seed_history())
    assert repo.daily_anthropic_tokens("race@x") == (500, 700)

    barrier = threading.Barrier(2)
    results: list[tuple[int, int]] = []
    results_lock = threading.Lock()

    def _check():
        barrier.wait(timeout=5)
        result = mgr._daily_token_totals("race@x")
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=_check) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 2

    key_in, key_out = mgr._daily_token_keys("race@x")
    final_in = coordination().incr(key_in, amount=0, ttl_s=60)
    final_out = coordination().incr(key_out, amount=0, ttl_s=60)
    assert (final_in, final_out) == (500, 700), (
        f"expected the counter seeded exactly once from the DB aggregate (500, 700); "
        f"got ({final_in}, {final_out}) — looks double-seeded"
    )


def test_double_crash_dies_after_three(manager: ChatManager):
    handles = [FakeHandle(), FakeHandle(), FakeHandle(), FakeHandle()]
    spawn_calls = iter(handles)

    async def fake_spawn(**kw):
        return next(spawn_calls)

    manager._provider.spawn = fake_spawn

    async def go():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        # Wait for ws to actually be seated as a sink, not just for the
        # LiveSession to exist — _broadcast (the crash-notice/ready frames
        # below) only reaches sinks already in live.sinks, so racing ahead
        # of _seat_sink can silently drop the first crash notice.
        await _wait_until(lambda: _ws_seated(manager, s.id, ws))
        # First crash → respawn
        handles[0].emit_eof()
        handles[0].killed = True
        await _wait_until(lambda: manager._live.get(s.id) is not None and manager._live[s.id].handle is handles[1])
        # Second crash → respawn
        handles[1].emit_eof()
        handles[1].killed = True
        await _wait_until(lambda: manager._live.get(s.id) is not None and manager._live[s.id].handle is handles[2])
        # Third crash → DEAD (no further respawn — 3x-crash terminal state)
        handles[2].emit_eof()
        handles[2].killed = True
        await _wait_until(lambda: (live := manager._live.get(s.id)) is None or live.state == SessionState.DEAD)
        # Should have three crashed notices, at least three ready notices
        crashed = [m for m in ws.sent if m.get("type") == "error" and m.get("kind") == "subprocess_crashed"]
        ready = [m for m in ws.sent if m.get("type") == "ready"]
        assert len(crashed) == 3, f"expected 3 crash notices, got {len(crashed)}"
        # First ready is the initial; respawns add 2 more
        assert len(ready) >= 3, f"expected >=3 ready, got {len(ready)}"
        # Session should now be DEAD
        live = manager._live.get(s.id)
        assert live is None or live.state == SessionState.DEAD

        # Cleanup
        try:
            await manager.kill(s.id, reason="test_done")
        except Exception:
            pass
        for h in handles[:3]:
            h.emit_eof()
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            attach_task.cancel()

    asyncio.run(go())


def test_active_count_for_user_matches_private(monkeypatch):
    from types import SimpleNamespace
    from app.chat.manager import ChatManager
    from app.chat.types import SessionState

    mgr = ChatManager.__new__(ChatManager)  # bypass __init__; we set only _live
    mgr._live = {
        "a": SimpleNamespace(user_email="x@e.com", state=SessionState.ACTIVE),
        "b": SimpleNamespace(user_email="x@e.com", state=SessionState.IDLE),
        "c": SimpleNamespace(user_email="y@e.com", state=SessionState.ACTIVE),
        "d": SimpleNamespace(user_email="x@e.com", state=SessionState.DEAD),
    }
    assert mgr.active_count_for_user("x@e.com") == 2
    assert mgr.active_count_for_user("x@e.com") == mgr._active_count_for_user("x@e.com")


# ---------------------------------------------------------------------------
# Task 7 tests: per-turn frame buffer — mid-turn sink replay + partial save
# ---------------------------------------------------------------------------


def _attach_fake_live_with_fake_handle(
    mgr: ChatManager, chat_id: str, user_email: str, sink, *, surface: str = Surface.WEB.value, extra_sinks=()
):
    """Insert a LiveSession with FakeHandle (has emit/readline) and one sink."""
    from datetime import datetime, timezone
    from app.chat.manager import LiveSession

    handle = FakeHandle()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        surface=surface,
        sinks=[SinkEntry(participant_email=user_email, sink=s) for s in (sink, *extra_sinks)],
    )
    mgr._live[chat_id] = live
    return live


def test_midturn_sink_gets_buffered_frames_replayed(manager: ChatManager):
    """A sink added mid-turn receives the buffered token frames exactly once;
    ws1 does not receive duplicates."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws1)

        # Simulate a user message to set turn_in_flight=True, turn_buffer cleared
        await manager.send_user_message(s.id, "hello")

        # Pump two token frames (no assistant_message yet)
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "token", "text": "Hel"})
        live.handle.emit({"type": "token", "text": "lo"})
        await _wait_until(lambda: len(live.turn_buffer) >= 2)  # let pump process frames

        # Now add ws2 mid-turn — must see the two buffered token frames
        ws2 = FakeWS()
        await manager.add_sink(s.id, ws2, "u@x")

        token_frames_ws2 = [f for f in ws2.sent if f.get("type") == "token"]
        assert len(token_frames_ws2) == 2, f"ws2 should see 2 buffered token frames, got {token_frames_ws2}"

        # ws1 must NOT see duplicates: still exactly 2 tokens (already received before add_sink)
        token_frames_ws1 = [f for f in ws1.sent if f.get("type") == "token"]
        assert len(token_frames_ws1) == 2, f"ws1 should see 2 tokens without duplicates, got {token_frames_ws1}"

        # Cleanup
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_turn_buffer_cleared_after_assistant_message(manager: ChatManager):
    """After a full turn completes (assistant_message frame), a newly added
    sink must NOT receive any token replay."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws1)

        await manager.send_user_message(s.id, "hello")

        # Pump token + assistant_message (full turn)
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "token", "text": "Hi"})
        live.handle.emit(
            {
                "type": "assistant_message",
                "content": "Hi",
                "tokens_in": 1,
                "tokens_out": 1,
            }
        )
        await _wait_until(lambda: not live.turn_in_flight)

        # Add a sink after the turn completed — should see no token replay
        ws2 = FakeWS()
        await manager.add_sink(s.id, ws2, "u@x")

        token_frames_ws2 = [f for f in ws2.sent if f.get("type") == "token"]
        assert len(token_frames_ws2) == 0, (
            f"buffer should be cleared after assistant_message; ws2 got {token_frames_ws2}"
        )
        assert not live.turn_in_flight, "turn_in_flight should be False after assistant_message"
        assert live.turn_buffer == [], "turn_buffer should be empty after assistant_message"

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_kill_midturn_persists_partial_assistant_message(manager: ChatManager):
    """kill() mid-turn must persist accumulated token text as an interrupted
    assistant message with tool_calls=[{interrupted: True, reason: ...}]."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws)

        await manager.send_user_message(s.id, "hello")

        # Pump two token frames — no assistant_message (mid-turn)
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "token", "text": "Hel"})
        live.handle.emit({"type": "token", "text": "lo"})
        await _wait_until(lambda: len(live.turn_buffer) >= 2)

        # Kill mid-turn
        await manager.kill(s.id, reason="idle_ttl")

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

        msgs = manager._repo.list_messages(s.id)
        assistant_rows = [m for m in msgs if m.role == "assistant"]
        assert assistant_rows, "expected at least one assistant row after kill mid-turn"
        partial = assistant_rows[-1]
        assert partial.content == "Hello", f"expected 'Hello' content, got {partial.content!r}"
        assert partial.tool_calls is not None, "expected tool_calls metadata"
        interrupted = [tc for tc in partial.tool_calls if isinstance(tc, dict) and tc.get("interrupted") is True]
        assert interrupted, f"expected interrupted=True in tool_calls, got {partial.tool_calls}"
        assert interrupted[0].get("reason") == "idle_ttl"

    asyncio.run(_run())


def test_kill_between_turns_persists_nothing_extra(manager: ChatManager):
    """kill() after a completed turn must NOT add an extra interrupted row."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws)

        await manager.send_user_message(s.id, "hello")

        # Complete a full turn
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        live.handle.emit(
            {
                "type": "assistant_message",
                "content": "A",
                "tokens_in": 1,
                "tokens_out": 1,
            }
        )
        await _wait_until(lambda: not live.turn_in_flight)

        # Kill between turns (buffer should be empty)
        await manager.kill(s.id, reason="test_done")

        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass

        msgs = manager._repo.list_messages(s.id)
        assistant_rows = [m for m in msgs if m.role == "assistant"]
        assert len(assistant_rows) == 1, f"expected exactly 1 assistant row, got {len(assistant_rows)}"
        # No interrupted marker
        if assistant_rows[0].tool_calls:
            interrupted = [
                tc for tc in assistant_rows[0].tool_calls if isinstance(tc, dict) and tc.get("interrupted") is True
            ]
            assert not interrupted, "no interrupted marker expected after full turn"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 8 tests: manager owns session lifecycle — detach/linger/pause/resume
# ---------------------------------------------------------------------------

from tests.chat_fakes import FakeProvider  # noqa: E402


def _make_pause_manager(tmp_path, linger_seconds=0):
    """ChatManager with FakeProvider and on_detach='pause'."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = FakeProvider()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=5,
            on_detach="pause",
            detach_linger_seconds=linger_seconds,
            idle_grace_seconds=linger_seconds,
            paused_ttl_seconds=7 * 24 * 3600,
            idle_ttl_seconds=10**9,
        ),
    )


def _make_kill_manager(tmp_path):
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = FakeProvider()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=5,
            on_detach="kill",
            detach_linger_seconds=0,
            idle_grace_seconds=0,
            paused_ttl_seconds=7 * 24 * 3600,
            idle_ttl_seconds=10**9,
        ),
    )


def monkeypatch_workdir(mgr: ChatManager) -> None:
    """Bypass the real WorkdirManager filesystem operations for testing."""
    import unittest.mock as mock

    mgr._workdir_mgr.ensure_user_workdir = mock.MagicMock()
    mgr._workdir_mgr.prepare_session_dir = mock.MagicMock(return_value=Path("/tmp/fake-session-dir"))


def _stdin_user_texts(handle) -> list[str]:
    """Texts of user_msg frames written to a FakeHandle's stdin (ignores
    ticket_push and other control frames)."""
    out = []
    for raw in handle._stdin_buf:
        try:
            frame = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if frame.get("type") == "user_msg":
            out.append(frame.get("text", ""))
    return out


def _ws_seated(mgr: ChatManager, chat_id: str, ws) -> bool:
    """True once ``ws`` is registered as a sink on ``chat_id``'s live session.

    ``chat_id in mgr._live`` alone is NOT a reliable "attach() is done"
    signal: ``ChatManager._spawn_live`` inserts the ``LiveSession`` into
    ``self._live`` (and, for a fresh spawn, sets ``live.handle``) well
    before ``attach()`` gets around to calling ``_seat_sink`` and actually
    registering ``ws`` in ``live.sinks``. A caller that polls on bare
    membership and then immediately calls ``detach_sink(chat_id, ws)`` can
    race ahead of that seating step: it observes ``live.sinks`` still empty
    (a fresh LiveSession starts with none), treats it as "last sink gone",
    and fires the linger→pause task — which, when the real ``_seat_sink``
    call lands moments later, sees a non-empty ``live.sinks`` and bails out
    thinking "a sink came back", permanently skipping the pause. Under
    pytest-xdist CPU contention this reliably reproduced as ``provider.paused``
    staying empty forever (not just late). Polling for the sink being seated
    instead of bare ``_live`` membership closes this race."""
    live = mgr._live.get(chat_id)
    return live is not None and any(e.sink is ws for e in live.sinks)


def test_detach_last_sink_does_not_kill(tmp_path):
    """With on_detach='pause', removing the last sink must NOT kill the session;
    it stays in _live with state ACTIVE through the linger window."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        assert s.id in mgr._live
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.05)
        # Session must still be alive (linger window is 999 s)
        assert s.id in mgr._live, "session killed immediately on detach — expected linger"
        live = mgr._live[s.id]
        assert live.state == SessionState.ACTIVE
        # Cleanup
        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_linger_then_pause_persists_refs(tmp_path):
    """linger_seconds=0: provider.pause called, repo row has sandbox refs, state PAUSED."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        await mgr.detach_sink(s.id, ws)
        await _wait_until(lambda: provider.paused)  # linger=0, pause should have fired
        # Provider should have paused the sandbox
        assert provider.paused, "expected sandbox to be paused in provider"
        # Repo row should reflect the pause
        session = mgr._repo.get_session(s.id)
        assert session is not None
        assert session.sandbox_id is not None
        assert session.runner_pid is not None
        assert session.sandbox_paused_at is not None
        # Live entry state should be PAUSED
        live = mgr._live.get(s.id)
        assert live is None or live.state == SessionState.PAUSED

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_reattach_during_linger_cancels_pause(tmp_path):
    """A new sink arriving inside the linger window must cancel the pause task."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws1))
        await mgr.detach_sink(s.id, ws1)
        await asyncio.sleep(0.02)  # inside linger window
        # Re-attach before linger expires
        ws2 = FakeWS()
        await mgr.add_sink(s.id, ws2, "u@x")
        await asyncio.sleep(0.05)
        # Pause must NOT have been called
        assert not provider.paused, "pause should not fire when sink returned during linger"
        live = mgr._live.get(s.id)
        assert live is not None and live.state == SessionState.ACTIVE

        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_pause_waits_for_inflight_turn(tmp_path):
    """When turn_in_flight is True, _linger_then_pause waits for it to clear."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        live = mgr._live[s.id]
        # Mark a turn in flight before detaching
        live.turn_in_flight = True
        await mgr.detach_sink(s.id, ws)
        await asyncio.sleep(0.15)  # linger=0 but turn still in flight
        # Pause should NOT have fired yet
        assert not provider.paused, "pause should wait for in-flight turn"
        # Simulate turn completing
        live.turn_in_flight = False
        await _wait_until(lambda: provider.paused)
        # Now pause should fire
        assert provider.paused, "expected pause after turn_in_flight cleared"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_linger_bails_when_runner_dies_during_inflight_turn(tmp_path):
    """Regression — Devin Review BUG_0001 follow-up on #605.

    If a runner dies (3× crash → SessionState.DEAD) while a turn is
    in-flight and no sink is attached, `_linger_then_pause` used to spin
    forever on `while live.turn_in_flight` (no pump alive to clear it)
    and the `_live` entry leaked (the reaper skips DEAD sessions). The
    fix adds a state check inside the spin so the linger task bails out
    cleanly when the session has died."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        live = mgr._live[s.id]
        # Set up: turn in flight, no sinks, runner declared DEAD
        # (the 3× crash terminal state — _wait_for_exit_and_respawn sets
        # this without ever emitting a `done` frame to clear the flag).
        live.turn_in_flight = True
        await mgr.detach_sink(s.id, ws)
        live.state = SessionState.DEAD
        # The linger task should bail out promptly rather than spinning
        # forever waiting for the turn to "complete." We grant it a few
        # tick cycles to notice the state transition.
        await _wait_until(lambda: live.linger_task is None or live.linger_task.done())
        assert live.linger_task is None or live.linger_task.done(), (
            "linger task must complete (not spin) when runner died mid-turn with no sinks attached"
        )
        # And no pause was issued (state was already DEAD, not ACTIVE).
        assert not provider.paused, "DEAD session must not be paused"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_attach_to_paused_resumes_same_handle(tmp_path):
    """attach() to a PAUSED live session resumes it; state becomes ACTIVE,
    paused_at cleared, and the pump delivers frames to the new sink."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws1))
        await mgr.detach_sink(s.id, ws1)
        await _wait_until(lambda: provider.paused)  # let pause fire
        assert provider.paused

        # Re-attach: should resume
        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await _wait_until(lambda: (live := mgr._live.get(s.id)) is not None and live.state == SessionState.ACTIVE)
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE
        session = mgr._repo.get_session(s.id)
        assert session.sandbox_paused_at is None

        # Emit a frame and verify ws2 receives it
        live.handle.emit({"type": "token", "text": "hi"})
        await _wait_until(lambda: any(f.get("type") == "token" for f in ws2.sent))
        token_frames = [f for f in ws2.sent if f.get("type") == "token"]
        assert token_frames, "resumed session should deliver frames to new sink"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_attach_to_live_session_does_not_spawn_second_runner(tmp_path):
    """attach() to an already-ACTIVE session must not spawn a new handle."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task1 = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: s.id in mgr._live)
        assert len(provider.spawned) == 1

        # ws1's session is already ACTIVE, so polling on that alone would be
        # trivially true before attach_task2 ever gets scheduled — wait for
        # the actual effect of the second attach() completing (ws2 seated as
        # a sink) so the "must NOT spawn" assertion below genuinely covers
        # the second attach having run, not just raced past it.
        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await _wait_until(lambda: any(e.sink is ws2 for e in mgr._live[s.id].sinks))
        assert len(provider.spawned) == 1, "second attach must NOT spawn another runner"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task1, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_resume_failure_falls_back_to_fresh_spawn(tmp_path):
    """When provider.resume raises, clear_sandbox_ref + fresh spawn + state ACTIVE."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws1))
        await mgr.detach_sink(s.id, ws1)
        await _wait_until(lambda: provider.paused)  # pause fires
        assert provider.paused
        # Make resume fail
        provider.fail_resume = True

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await _wait_until(lambda: (live := mgr._live.get(s.id)) is not None and live.state == SessionState.ACTIVE)
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE, "should fall back to fresh spawn"
        assert len(provider.spawned) == 2, "expected a second spawn on resume failure"
        session = mgr._repo.get_session(s.id)
        assert session.sandbox_paused_at is None

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_send_user_message_resumes_paused_session(tmp_path):
    """send_user_message to a PAUSED session resumes it first (Slack path)."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws1))
        await mgr.detach_sink(s.id, ws1)
        await _wait_until(lambda: provider.paused)  # pause fires
        assert provider.paused

        # send_user_message should resume first
        await mgr.send_user_message(s.id, "hello after pause")
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE

        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_attach_after_restart_resumes_from_repo_row(tmp_path):
    """Post-restart: _live cleared, but repo row has sandbox refs — attach resumes."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws1))
        await mgr.detach_sink(s.id, ws1)
        await _wait_until(lambda: provider.paused)  # pause fires, refs persisted in repo
        assert provider.paused

        # Simulate server restart: clear in-memory _live
        mgr._live.clear()

        # Re-attach must resume purely from repo row
        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await _wait_until(lambda: (live := mgr._live.get(s.id)) is not None and live.state == SessionState.ACTIVE)
        live = mgr._live.get(s.id)
        assert live is not None
        assert live.state == SessionState.ACTIVE

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_on_detach_kill_preserves_legacy_behavior(tmp_path):
    """on_detach='kill': last-sink detach must kill the session immediately."""

    async def _run():
        mgr = _make_kill_manager(tmp_path)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        await mgr.detach_sink(s.id, ws)
        await _wait_until(lambda: s.id not in mgr._live)
        assert s.id not in mgr._live, "on_detach=kill: session should be dead after detach"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Task 9 tests: reaper pauses, paused-TTL GC, active-time cap, shutdown pauses
# ---------------------------------------------------------------------------


def test_idle_ttl_pauses_instead_of_kills(tmp_path):
    """Reaper on an idle ACTIVE session with no sinks → PAUSED, sandbox alive in provider."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live)
        live = mgr._live[s.id]
        # Empty sinks without triggering linger
        live.sinks = []
        # Force last_activity into the past
        from datetime import datetime as _dt, timedelta, timezone as _tz

        live.last_activity = _dt.now(_tz.utc) - timedelta(seconds=1)

        # Patch config to use idle_ttl=0 for fast reap
        original_config = mgr._config

        class _PatchedConfig:
            def __getattr__(self, name):
                if name == "idle_ttl_seconds":
                    return 0
                return getattr(original_config, name)

        mgr._config = _PatchedConfig()
        await mgr._reap_once()
        mgr._config = original_config

        live = mgr._live.get(s.id)
        assert live is None or live.state == SessionState.PAUSED
        assert provider.paused, "sandbox must be in provider.paused after idle reaper"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_paused_ttl_really_kills(tmp_path):
    """Repo row paused before cutoff → provider.destroy called + sandbox refs cleared."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        await mgr.detach_sink(s.id, ws)
        await _wait_until(lambda: provider.paused)  # pause fires
        assert provider.paused

        # Push sandbox_paused_at into the past past the TTL
        from datetime import datetime as _dt, timedelta, timezone as _tz

        mgr._repo.set_sandbox_paused_at(
            s.id,
            _dt.now(_tz.utc) - timedelta(seconds=mgr._config.paused_ttl_seconds + 1),
        )
        # Clear _live so it tests the repo-row-only path
        mgr._live.clear()

        await mgr._reap_once()

        session = mgr._repo.get_session(s.id)
        assert session.sandbox_id is None, "sandbox ref must be cleared after paused-TTL GC"
        assert session.runner_pid is None
        assert session.sandbox_paused_at is None
        assert provider.destroyed, "provider.destroy must be called for expired paused sandbox"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_max_session_seconds_counts_active_time_only(tmp_path):
    """max_session_seconds uses accumulated active time; pause stops the clock."""

    async def _run():
        import time as _time

        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live)
        live = mgr._live[s.id]
        # 1 hour accumulated, currently paused (active_since barely recent)
        live.active_seconds_accum = 3600.0
        live.active_since = _time.monotonic() - 10  # only 10 s since last resume
        live.state = SessionState.PAUSED  # pause stops the clock

        # max_session_seconds=4h: total active ≈ 1h 10s ≪ 4h → should NOT reap
        original_config = mgr._config

        class _PatchedConfig:
            def __getattr__(self, name):
                if name == "max_session_seconds":
                    return 4 * 3600
                if name == "idle_ttl_seconds":
                    return 10**9  # disable idle path
                return getattr(original_config, name)

        mgr._config = _PatchedConfig()
        await mgr._reap_once()
        assert s.id in mgr._live, "paused session with only ~1h active time must not be reaped at 4h cap"

        # Now exceed the cap
        live.active_seconds_accum = 4 * 3600 + 1
        await mgr._reap_once()
        remaining = mgr._live.get(s.id)
        assert remaining is None or remaining.state in (
            SessionState.DEAD,
            SessionState.PAUSED,
        ), "session past active cap must be killed or paused"

        mgr._config = original_config
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_shutdown_pauses_active_sessions(tmp_path):
    """shutdown() with on_detach='pause' pauses ACTIVE sessions instead of killing."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live)
        assert s.id in mgr._live

        await mgr.shutdown()

        assert provider.paused, "shutdown with on_detach=pause should pause active sandboxes"
        session = mgr._repo.get_session(s.id)
        assert session.sandbox_paused_at is not None, "sandbox_paused_at must be set after shutdown-pause"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_keepalive_heartbeat_extends_timeout_while_sinks_attached(tmp_path):
    """The reaper tick calls provider.keepalive for ACTIVE sessions with sinks.

    Awaits ``mgr.attach`` directly rather than firing it via
    ``asyncio.create_task`` + a fixed sleep (the pre-existing pattern here):
    ``attach()`` fully completes seat_sink (and thus registers the sink)
    before returning — it doesn't need the pump/wait tasks it kicks off to
    finish — so there's no concurrency to simulate and nothing to race. A
    fixed 50ms sleep before checking ``live.sinks`` was flaky under a loaded
    CI runner (8 shards x pytest -n auto), matching the direct-await pattern
    already used by sibling non-concurrent attach tests in this file (e.g.
    test_broadcast_dead_sink_sweep_triggers_detach_policy,
    test_seat_sink_does_not_replay_persisted_history)."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        provider = mgr._provider
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await mgr.attach(s.id, ws)
        live = mgr._live.get(s.id)
        assert live is not None and live.sinks  # has a sink

        await mgr._reap_once()
        assert provider.keepalive_calls, "keepalive should be called for ACTIVE session with sinks"

        await mgr.kill(s.id, reason="test_done")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# PR #605 review regressions (Devin findings)
# ---------------------------------------------------------------------------


def test_seat_sink_does_not_replay_persisted_history(manager: ChatManager):
    """The primary WS must NOT receive persisted messages on attach — the web
    client already loaded them via REST; replaying duplicates every bubble.
    Only the in-progress turn buffer (+ ready) goes over the wire."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=s.id, role="user", content="q?")
        manager._repo.append_message(session_id=s.id, role="assistant", content="a!")
        ws = FakeWS()
        await manager.attach(s.id, ws)
        types = [f.get("type") for f in ws.sent]
        assert "assistant_message" not in types
        assert "user_msg" not in types
        assert types[-1] == "ready"
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_broadcast_dead_sink_sweep_triggers_detach_policy(manager: ChatManager):
    """When _broadcast's dead-sink removal empties live.sinks, the on-detach
    policy must fire (linger task scheduled) — a joiner whose socket died
    without a clean detach must not leave the session ownerless until the
    idle reaper."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await manager.attach(s.id, ws)
        live = manager._live[s.id]
        assert live.linger_task is None

        class DeadSink:
            async def send_json(self, data):
                raise RuntimeError("socket gone")

            async def close(self):
                pass

        live.sinks = [SinkEntry(participant_email="u@x", sink=DeadSink())]
        await manager._broadcast(live, {"type": "token", "text": "x"})
        assert not live.sinks
        assert live.linger_task is not None
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_resume_from_row_co_session_uses_ephemeral_dir(manager: ChatManager, monkeypatch):
    """Cold-start resume of a co-session must rebuild the ephemeral
    grant-intersection workspace (SR-6), not a personal one.

    A cold-start ``_resume_from_row`` call whose row's ``relay_protocol_version``
    is unknown/legacy (NULL — e.g. a pre-Tier-1-migration row, simulated here
    by nulling the column after ``set_sandbox_ref`` stamps it) goes through
    the fresh-spawn path (``_spawn_live``) per AC-G-resume-legacy, not
    ``provider.resume()``. That path shares the same co-session ephemeral-dir
    selection this test guards."""
    import app.chat.manager as manager_mod

    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: _FakeTicketRepo())

    async def _run():
        s = await manager.create_session(user_email="owner@x", surface=Surface.WEB)
        manager._repo._conn.execute("UPDATE chat_sessions SET is_co_session = TRUE WHERE id = ?", [s.id])
        manager._repo.add_session_participant(session_id=s.id, user_email="owner@x", user_id="u1", role="owner")
        manager._repo.add_session_participant(session_id=s.id, user_email="peer@x", user_id="u2", role="collaborator")
        manager._repo.set_sandbox_ref(s.id, sandbox_id="sbx-co", runner_pid=42)
        # set_sandbox_ref stamps relay_protocol_version=current (Tier 1) — null
        # it back out to simulate a genuinely legacy/unknown row for this test.
        manager._repo._conn.execute("UPDATE chat_sessions SET relay_protocol_version = NULL WHERE id = ?", [s.id])

        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        monkeypatch.setattr(
            "src.grant_intersection.compute_grant_intersection",
            lambda emails, conn: {},
        )
        eph = MagicMock(return_value=Path("/tmp/eph-dir"))
        personal = MagicMock()
        monkeypatch.setattr(manager._workdir_mgr, "prepare_ephemeral_session_dir", eph)
        monkeypatch.setattr(manager._workdir_mgr, "prepare_session_dir", personal)

        session = manager._repo.get_session(s.id)
        live = await manager._resume_from_row(session)
        assert live is not None
        eph.assert_called_once()
        personal.assert_not_called()
        assert sorted(live.participant_emails) == ["owner@x", "peer@x"]
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tier 1: restart-invariant sandbox reuse (relay_protocol_version column)
# ---------------------------------------------------------------------------


def test_resume_from_row_reconnects_after_restart(manager: ChatManager, monkeypatch):
    """A row whose ``relay_protocol_version`` is current (stamped by
    ``set_sandbox_ref``) must be reconnected via ``provider.resume()`` —
    NOT force-respawned — even though this is a brand-new ``ChatManager``
    with an empty ``_known_protocol_sessions`` (i.e. simulating a genuine
    process restart). This is the headline Tier 1 fix: before the
    persisted column existed, EVERY restart force-respawned every
    resumable session regardless of its runner's actual protocol."""
    import app.chat.manager as manager_mod
    from app.chat.types import RELAY_PROTOCOL_VERSION

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        from datetime import datetime as _dt, timezone as _tz

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.set_sandbox_ref(s.id, sandbox_id="sbx-restart", runner_pid=555)
        manager._repo.set_sandbox_paused_at(s.id, _dt.now(_tz.utc))
        row = manager._repo.get_session(s.id)
        assert row is not None
        assert row.relay_protocol_version == RELAY_PROTOCOL_VERSION
        # Precondition: this manager never spawned/ticket-pushed this
        # session in-process — the fast-confirm set is genuinely empty,
        # exactly like right after a process restart.
        assert s.id not in manager._known_protocol_sessions

        handle = FakeHandle()
        manager._provider.resume = AsyncMock(return_value=handle)

        live = await manager._resume_from_row(row)

        assert live is not None
        # env carries the session identity AND the approval knobs for
        # providers whose resumed handle needs them (the kai-agent engine
        # re-mints its session JWT from the identity and keeps the approvals
        # kill-switch sticky across pause/resume); the sandbox providers
        # ignore env on resume.
        manager._provider.resume.assert_awaited_once_with(
            sandbox_id="sbx-restart",
            runner_pid=555,
            env={
                "AGNES_SESSION_ID": s.id,
                "AGNES_USER_EMAIL": s.user_email,
                "AGNES_APPROVAL_TIMEOUT_SECONDS": str(manager._config.approval_timeout_seconds),
                "AGNES_APPROVALS": "on",
            },
        )
        manager._provider.spawn.assert_not_awaited()
        assert live.state == SessionState.ACTIVE
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_resume_from_row_null_protocol_version_forces_fresh_spawn(manager: ChatManager, monkeypatch):
    """The inverse of the above: a row whose ``relay_protocol_version`` is
    NULL (unknown/legacy — e.g. a pre-Tier-1-migration row) must still be
    force-respawned via ``_spawn_live``, never reconnected via
    ``provider.resume()`` — the conservative AC-G-resume-legacy behavior
    this migration preserves for genuinely unknown runners."""
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.set_sandbox_ref(s.id, sandbox_id="sbx-legacy", runner_pid=111)
        # Simulate a genuinely legacy/unknown row: NULL relay_protocol_version.
        manager._repo._conn.execute("UPDATE chat_sessions SET relay_protocol_version = NULL WHERE id = ?", [s.id])
        row = manager._repo.get_session(s.id)
        assert row is not None
        assert row.relay_protocol_version is None

        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        manager._provider.resume = AsyncMock(side_effect=AssertionError("resume must not be called"))
        manager._provider.destroy = AsyncMock()

        live = await manager._resume_from_row(row)

        assert live is not None
        manager._provider.spawn.assert_awaited_once()
        manager._provider.resume.assert_not_awaited()
        manager._provider.destroy.assert_awaited_once_with(sandbox_id="sbx-legacy")
        assert live.state == SessionState.ACTIVE
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_crash_respawn_refreshes_sandbox_refs(manager: ChatManager):
    """A crash-respawn must persist the NEW sandbox's refs — otherwise a later
    pause/resume reconnects the dead sandbox and silently loses the agent's
    in-memory context (PR #605 review finding)."""

    async def _run():
        h1, h2 = FakeHandle(), FakeHandle()
        h1.sandbox_id, h2.sandbox_id = "sbx-old", "sbx-new"
        manager._provider.spawn = AsyncMock(side_effect=[h1, h2])
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await manager.attach(s.id, ws)
        assert manager._repo.get_session(s.id).sandbox_id == "sbx-old"

        # Crash: first handle exits non-zero → _wait_for_exit_and_respawn
        # spawns h2 and must refresh the persisted refs.
        h1.exit_code = 1
        h1.killed = True  # FakeHandle.wait() returns once killed flips
        for _ in range(100):
            await asyncio.sleep(0.02)
            if manager._live[s.id].handle is h2:
                break
        row = manager._repo.get_session(s.id)
        assert row.sandbox_id == "sbx-new"
        assert row.runner_pid == h2.pid
        await manager.kill(s.id, reason="test_done")

    asyncio.run(_run())


def test_reaper_gcs_dead_sessions(manager: ChatManager):
    """DEAD entries (3x-crash leftovers) must be GC'd by the reaper — the
    crash path marks state=DEAD without popping _live, and the reaper used
    to skip non-ACTIVE/IDLE states, leaking one entry per crashed session."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        await manager.attach(s.id, ws)
        manager._live[s.id].state = SessionState.DEAD
        await manager._reap_once()
        assert s.id not in manager._live

    asyncio.run(_run())


def test_spawn_agnes_server_falls_back_to_internal_url(manager: ChatManager, tmp_path, monkeypatch):
    """Plain-HTTP deployments keep SERVER_URL unset (or unusable) and point
    the sandbox data rails at AGNES_INTERNAL_URL instead."""
    monkeypatch.delenv("SERVER_URL", raising=False)
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://10.0.0.5:8000")
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle()

    manager._provider.spawn = fake_spawn

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, tmp_path)

    asyncio.run(_run())

    assert captured["env"]["AGNES_SERVER"] == "http://10.0.0.5:8000"


def test_spawn_uploads_wheel_before_workspace_and_sets_sentinel_env(manager: ChatManager, tmp_path, monkeypatch):
    """The wheel is a single small write whose sentinel unblocks the runner's
    in-sandbox pip install — it must be staged BEFORE the (much slower)
    workspace push so the install overlaps the upload. The runner env carries
    the workspace-ready sentinel path so the runner knows to gate the agent
    CLI spawn on it (empty when the provider mounts the workspace itself)."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

    import app.chat.e2b_workspace_sync as sync_mod
    from app.chat.e2b_workspace_sync import SANDBOX_WORKSPACE_READY

    order: list[str] = []

    async def fake_wheel(stage):
        order.append("wheel")

    async def fake_workspace(sandbox, root, *, max_bytes):
        order.append("workspace")
        return 0

    monkeypatch.setattr(sync_mod, "stage_agnes_wheel", fake_wheel)
    monkeypatch.setattr(sync_mod, "upload_workspace", fake_workspace)

    handle = FakeHandle()
    handle._sandbox = MagicMock()  # sandbox present → E2B sync branch taken
    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return handle

    manager._provider.spawn = fake_spawn
    _make_provider_e2b_shaped(manager)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, tmp_path)

    asyncio.run(_run())

    assert order == ["wheel", "workspace"]
    assert captured["env"]["AGNES_WORKSPACE_SYNC_SENTINEL"] == SANDBOX_WORKSPACE_READY


def _make_provider_e2b_shaped(manager: ChatManager) -> None:
    """Make the fixture's MagicMock provider behave like ``E2BProvider``:
    ``syncs_workspace=False`` (the manager pushes the workspace) plus an async
    ``stage_file`` that writes through the handle's sandbox file API.

    Needed because the fixture provider is a bare ``MagicMock``: its
    auto-attributes are truthy (so ``syncs_workspace`` must be pinned) and its
    ``stage_file`` is not a coroutine function, which is exactly how
    ``ChatManager._file_stager`` detects "this provider cannot stage files".
    """
    manager._provider.syncs_workspace = False

    async def _stage(handle, path, data):
        await handle._sandbox.files.write(path, data)

    manager._provider.stage_file = _stage


def test_spawn_stages_wheel_and_context_for_a_bind_mounting_provider(manager: ChatManager, tmp_path, monkeypatch):
    """`syncs_workspace=True` must skip ONLY the workspace push.

    The CLI wheel and the restore-context transcript are not workspace sync:
    without them a bind-mounting provider loses the `agnes` CLI (the runner
    blocks the full 60 s wheel wait on a `.ready` that never appears) and loses
    conversation history on every crash respawn.
    """
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

    import app.chat.e2b_workspace_sync as sync_mod
    from app.chat.e2b_workspace_sync import SANDBOX_CONTEXT_RESTORE, SANDBOX_WHEEL_READY

    staged: dict = {}
    pushed: list[str] = []

    async def fake_workspace(sandbox, root, *, max_bytes):
        pushed.append("workspace")
        return 0

    monkeypatch.setattr(sync_mod, "upload_workspace", fake_workspace)

    handle = FakeHandle()
    captured: dict = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return handle

    async def _stage(h, path, data):
        staged[path] = data

    manager._provider.spawn = fake_spawn
    manager._provider.syncs_workspace = True
    manager._provider.stage_file = _stage

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=s.id, role="assistant", content="earlier answer")
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, tmp_path)

    asyncio.run(_run())

    assert SANDBOX_WHEEL_READY in staged, "the wheel-ready sentinel must be staged for every provider"
    assert SANDBOX_CONTEXT_RESTORE in staged
    assert "earlier answer" in str(staged[SANDBOX_CONTEXT_RESTORE])
    # The context must land BEFORE the wheel-ready sentinel: that sentinel is
    # the only pre-boot barrier a bind-mounting provider has (the workspace
    # wait is skipped), and the runner reads the context strictly after its
    # sentinel-gated install — staged later, a restarted session could start
    # answering without its history.
    paths = list(staged)
    assert paths.index(SANDBOX_CONTEXT_RESTORE) < paths.index(SANDBOX_WHEEL_READY)
    # ...and only the workspace tarball stays behind the syncs_workspace gate.
    assert pushed == []
    assert captured["env"]["AGNES_WORKSPACE_SYNC_SENTINEL"] == ""


def test_spawn_stages_wheel_and_context_through_the_e2b_files_api(manager: ChatManager, tmp_path, monkeypatch):
    """E2B regression for the same split: with the REAL provider the wheel,
    its sentinel and the restore-context still land through
    ``sandbox.files.write``, at the same paths — restore-context first, so the
    wheel's ``.ready`` sentinel guarantees it on every provider (under E2B the
    trailing workspace-ready sentinel is a second barrier)."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

    from app.chat.e2b_provider import E2BProvider
    from app.chat.e2b_workspace_sync import (
        SANDBOX_CONTEXT_RESTORE,
        SANDBOX_WHEEL_DIR,
        SANDBOX_WHEEL_READY,
        SANDBOX_WORKSPACE_READY,
    )

    wheel = tmp_path / "agnes_the_ai_analyst-0.1.0-py3-none-any.whl"
    wheel.write_bytes(b"WHEELBYTES")
    monkeypatch.setattr("app.api.cli_artifacts._find_wheel", lambda: wheel)

    written: list[str] = []

    handle = FakeHandle()
    sb = MagicMock()
    sb.files = MagicMock()

    async def _write(path, data):
        written.append(path)

    sb.files.write = AsyncMock(side_effect=_write)
    sb.commands = MagicMock()
    sb.commands.run = AsyncMock()
    handle._sandbox = sb

    async def fake_spawn(**kw):
        return handle

    provider = E2BProvider(api_key="k", template_id="t")
    provider.spawn = fake_spawn  # type: ignore[method-assign]
    manager._provider = provider

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.append_message(session_id=s.id, role="assistant", content="earlier answer")
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, tmp_path / "session")

    (tmp_path / "session").mkdir()
    asyncio.run(_run())

    assert written == [
        SANDBOX_CONTEXT_RESTORE,
        f"{SANDBOX_WHEEL_DIR}/{wheel.name}",
        SANDBOX_WHEEL_READY,
        SANDBOX_WORKSPACE_READY,
    ]


def test_agnes_server_url_resolution_chain(monkeypatch):
    """SERVER_URL → AGNES_INTERNAL_URL → loopback; the same chain feeds both
    the sandbox env (AGNES_SERVER) and the workspace seed (WorkdirManager)."""
    from app.chat.manager import agnes_server_url

    monkeypatch.setenv("SERVER_URL", "https://agnes.example.com/")
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://10.0.0.5:8000")
    assert agnes_server_url() == "https://agnes.example.com"

    monkeypatch.delenv("SERVER_URL", raising=False)
    assert agnes_server_url() == "http://10.0.0.5:8000"

    monkeypatch.delenv("AGNES_INTERNAL_URL", raising=False)
    assert agnes_server_url() == "http://127.0.0.1:8000"

    # Empty string is "unset", not a value — .env files with SERVER_URL= must
    # not produce an empty rails URL.
    monkeypatch.setenv("SERVER_URL", "")
    monkeypatch.setenv("AGNES_INTERNAL_URL", "http://10.0.0.5:8000")
    assert agnes_server_url() == "http://10.0.0.5:8000"


# ---------------------------------------------------------------------------
# Chat sandbox secret broker (2026-07-14): ticket mint + stdin push at
# spawn/resume, real-secret-free env, legacy-runner force-respawn.
# ---------------------------------------------------------------------------


class _FakeTicketRepo:
    """Stand-in for src.repositories.ticket_repo() — records mint/revoke
    calls instead of touching a real chat_broker_tickets table."""

    def __init__(self) -> None:
        self.minted: list[tuple[str, str]] = []
        self.revoked: list[str] = []

    def mint(self, session_id: str, scope: str, ttl_seconds: int = 3600) -> str:
        self.minted.append((session_id, scope))
        return f"ticket-{scope}-{len(self.minted)}"

    def revoke_session(self, session_id: str) -> None:
        self.revoked.append(session_id)


def test_spawn_env_has_no_real_secret(manager: ChatManager, monkeypatch):
    """The sandbox spawn env must never carry the real Anthropic key or the
    real Agnes session JWT — both are brokered via tickets pushed over
    stdin instead of injected as env vars (AC-F-nosecret)."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "real-jwt-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-must-not-leak")

    import app.chat.manager as manager_mod

    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: _FakeTicketRepo())

    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle()

    manager._provider.spawn = fake_spawn

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, Path("/tmp"))

    asyncio.run(_run())

    env = captured["env"]
    assert env.get("ANTHROPIC_API_KEY") in (None, "", "sk-dummy-broker")
    assert "AGNES_TOKEN" not in env


def test_spawn_pushes_ticket_frame(manager: ChatManager, monkeypatch):
    """A fresh spawn must mint main+mcp tickets and push a ticket_push frame
    over stdin, under _stdin_lock, before the session is considered ready."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    handle = FakeHandle()
    manager._provider.spawn = AsyncMock(return_value=handle)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live)
        await manager.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())

    frames = [json.loads(b) for b in handle._stdin_buf]
    ticket_frames = [f for f in frames if f.get("type") == "ticket_push"]
    assert ticket_frames, f"expected a ticket_push frame on stdin; got {frames}"
    assert ticket_frames[0]["main"] and ticket_frames[0]["mcp"] and ticket_frames[0]["data_apps"]
    scopes = {scope for (_sid, scope) in fake_tickets.minted}
    assert scopes == {"main", "mcp", "data_apps"}


def test_resume_pushes_fresh_ticket_before_messages(tmp_path, monkeypatch):
    """Resuming a PAUSED in-memory session (current-protocol runner) mints
    fresh tickets, revokes the old ones, and pushes a ticket_push frame over
    stdin under _stdin_lock before any further message is forwarded
    (AC-G-resume-fresh)."""
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        await mgr.detach_sink(s.id, ws)
        await _wait_until(lambda: provider.paused)  # pause fires
        assert provider.paused

        # Discard the initial-spawn mint calls/frame — only the resume matters.
        fake_tickets.minted.clear()
        fake_tickets.revoked.clear()
        parked = next(iter(provider.paused.values()))
        parked._stdin_buf.clear()

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        await _wait_until(lambda: (live := mgr._live.get(s.id)) is not None and live.state == SessionState.ACTIVE)

        live = mgr._live.get(s.id)
        assert live is not None and live.state == SessionState.ACTIVE

        frames = [json.loads(b) for b in parked._stdin_buf]
        assert frames and frames[0]["type"] == "ticket_push", (
            f"expected the FIRST stdin frame after resume to be ticket_push; got {frames}"
        )
        scopes = {scope for (_sid, scope) in fake_tickets.minted}
        assert scopes == {"main", "mcp", "data_apps"}
        assert fake_tickets.revoked == [s.id]

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_legacy_runner_force_respawned(tmp_path, monkeypatch):
    """A PAUSED session this process has no record of ever having pushed a
    current-protocol ticket to must be force-respawned on resume — never
    reconnected: an old runner may not understand the ticket_push stdin
    frame (AC-G-resume-legacy)."""
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(mgr, s.id, ws))
        await mgr.detach_sink(s.id, ws)
        await _wait_until(lambda: provider.paused)  # pause fires
        assert provider.paused
        assert len(provider.spawned) == 1

        # Simulate a pre-broker (legacy) runner: this process never recorded
        # having pushed it a current-protocol ticket, AND the persisted
        # relay_protocol_version is unknown (nulled here — set_sandbox_ref
        # would otherwise have stamped it current at spawn time above).
        mgr._known_protocol_sessions.discard(s.id)
        mgr._repo._conn.execute("UPDATE chat_sessions SET relay_protocol_version = NULL WHERE id = ?", [s.id])

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        # Poll for BOTH the force-respawn (2nd spawn) and the old sandbox's
        # destroy landing — the exact-count asserts right after must not be
        # masked by returning before either side effect has actually fired.
        await _wait_until(lambda: len(provider.spawned) == 2 and len(provider.destroyed) == 1)

        assert len(provider.spawned) == 2, "legacy session must be force-respawned, not resumed"
        # provider.resume() was never invoked (fresh spawn instead), AND the old
        # paused sandbox is destroyed rather than orphaned — resuming a legacy
        # session must not leak a billable microVM (Devin review on #849).
        assert len(provider.destroyed) == 1, "the old paused sandbox must be destroyed on legacy respawn"
        assert not provider.paused, "no paused sandbox may be left orphaned after a legacy respawn"
        # The paused session's old broker tickets must be revoked on the legacy
        # respawn, not left redeemable until TTL (Devin review on #851).
        assert s.id in fake_tickets.revoked, "old broker tickets must be revoked on legacy respawn"
        live = mgr._live.get(s.id)
        assert live is not None and live.state == SessionState.ACTIVE
        assert s.id in mgr._known_protocol_sessions, "the fresh spawn must record the new protocol marker"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    asyncio.run(_run())


def test_legacy_resume_destroys_old_sandbox_before_clearing(tmp_path):
    """Post-restart (legacy) resume must destroy the old paused sandbox BEFORE
    clearing its ref — clearing first NULLs sandbox_paused_at so the reaper can
    never reap it, leaking a billable microVM per session per restart (§11)."""
    import unittest.mock as mock

    async def _run():
        mgr = _make_pause_manager(tmp_path)
        monkeypatch_workdir(mgr)
        session = mgr._repo.create_session(user_email="leak@test.com", surface=Surface.WEB)
        mgr._repo.set_sandbox_ref(session.id, sandbox_id="old-sbx-123", runner_pid=999)
        # set_sandbox_ref stamps relay_protocol_version=current (Tier 1) — null
        # it back out to simulate the legacy/unknown row this test targets.
        mgr._repo._conn.execute("UPDATE chat_sessions SET relay_protocol_version = NULL WHERE id = ?", [session.id])
        row = mgr._repo.get_session(session.id)

        order: list = []
        orig_destroy = mgr._provider.destroy

        async def _destroy(*, sandbox_id):
            order.append(("destroy", sandbox_id))
            return await orig_destroy(sandbox_id=sandbox_id)

        mgr._provider.destroy = _destroy
        orig_clear = mgr._repo.clear_sandbox_ref

        def _clear(sid):
            order.append(("clear", sid))
            return orig_clear(sid)

        mgr._repo.clear_sandbox_ref = _clear
        mgr._spawn_live = mock.AsyncMock(return_value=mock.MagicMock())

        assert session.id not in mgr._known_protocol_sessions  # legacy path
        await mgr._resume_from_row(row)

        assert ("destroy", "old-sbx-123") in order, order
        assert order.index(("destroy", "old-sbx-123")) < order.index(("clear", session.id)), order
        mgr._spawn_live.assert_awaited_once()

    asyncio.run(_run())


def test_spawn_live_destroys_sandbox_on_post_spawn_failure(manager: ChatManager):
    """#867: a spawn that succeeds but then fails during post-spawn setup —
    before the kill-on-exit `wait_task` is wired — must tear the sandbox down
    (else it orphans and later pauses/persists, billable) and not leave a
    half-registered `live` behind."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)

        # Simulate the runner dying on boot: the ticket push over stdin raises
        # (broken pipe) after the sandbox is already up.
        async def _boom(_live):
            raise RuntimeError("runner died on boot")

        manager._push_ticket_frame = _boom

        with pytest.raises(RuntimeError, match="runner died on boot"):
            await manager._spawn_live(s)

        assert handle.killed is True, "orphaned sandbox was not torn down"
        assert s.id not in manager._live, "half-registered live left behind"
        # #867 review: the DB sandbox ref written before the failure must be
        # cleared, else a row points at a dead sandbox that the paused-TTL
        # reaper can never find.
        row = manager._repo.get_session(s.id)
        assert row.sandbox_id is None, "stale sandbox ref left in DB after failed setup"
        assert row.runner_pid is None
        # Devin Review, PR #935: the routing lease claimed before the failure
        # must be released too — else a stale self-claim lingers until its
        # TTL and can misroute a reconnect under the redis multi-gateway
        # backend.
        assert routing.owner_of(s.id) is None, "stale routing lease left after failed setup"

    asyncio.run(_run())


def test_spawn_live_happy_path_does_not_kill_sandbox(manager: ChatManager):
    """Guard against the teardown firing on the success path: a clean spawn
    keeps the sandbox alive and wires the wait_task."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)

        live = await manager._spawn_live(s)
        try:
            assert handle.killed is False
            assert manager._live.get(s.id) is live
            assert live.current_wait is not None
        finally:
            await manager.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Wave-2C task 3: paused-sandbox sweep leader lease
# ---------------------------------------------------------------------------

from app.chat.manager import _PAUSED_SWEEP_LEASE_NAME  # noqa: E402
from app.coordination.factory import coordination  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_coordination_for_sweep_tests():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _paused_expired_session(mgr):
    """Create a session whose sandbox_paused_at is already past the TTL —
    the exact repo-row shape _reap_once's paused sweep looks for."""
    from datetime import datetime as _dt, timedelta, timezone as _tz

    session = mgr._repo.create_session(user_email="sweep@test.com", surface=Surface.WEB)
    mgr._repo.set_sandbox_ref(session.id, sandbox_id="sbx-sweep", runner_pid=123)
    mgr._repo.set_sandbox_paused_at(
        session.id,
        _dt.now(_tz.utc) - timedelta(seconds=mgr._config.paused_ttl_seconds + 1),
    )
    return session


def test_paused_sweep_skips_when_lease_held_elsewhere(tmp_path):
    """If another replica holds the paused-sandbox-sweep lease, this
    replica's _reap_once must NOT destroy/clear the sandbox this tick —
    it should defer to whoever holds the lease and try again next tick."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        session = _paused_expired_session(mgr)

        # Simulate another replica already holding the sweep lease.
        assert coordination().lease_acquire(_PAUSED_SWEEP_LEASE_NAME, "other-replica", ttl_s=90)

        await mgr._reap_once()

        row = mgr._repo.get_session(session.id)
        assert row.sandbox_id == "sbx-sweep", "sweep must have skipped — lease held elsewhere"
        assert "sbx-sweep" not in mgr._provider.destroyed

    asyncio.run(_run())


def test_paused_sweep_runs_when_lease_acquired_and_releases_after(tmp_path):
    """The normal (uncontended) path: this replica acquires the lease,
    performs the sweep, and releases the lease afterwards — a subsequent
    acquirer must not have to wait out the TTL."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        session = _paused_expired_session(mgr)

        await mgr._reap_once()

        row = mgr._repo.get_session(session.id)
        assert row.sandbox_id is None, "sweep must have destroyed/cleared the expired sandbox"
        assert "sbx-sweep" in mgr._provider.destroyed

        # Released, not just expired — a fresh acquirer gets it immediately.
        assert coordination().lease_acquire(_PAUSED_SWEEP_LEASE_NAME, "someone-else", ttl_s=90) is True

    asyncio.run(_run())


def test_paused_sweep_releases_routing_lease(tmp_path):
    """A session torn down via the paused-sandbox-TTL sweep's destroy path
    (`self._live.pop(session.id, None)` directly, bypassing kill()) must
    have its routing lease released immediately rather than left to
    self-heal at the lease's own TTL (Minor finding)."""
    from app.chat import routing

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=0)
        monkeypatch_workdir(mgr)
        session = _paused_expired_session(mgr)

        # Simulate the session having previously claimed its routing lease
        # (the normal spawn/resume path) before it was paused.
        gw = routing.this_gateway_id()
        assert routing.claim_session(session.id, gw, ttl_s=180) is True
        assert routing.owner_of(session.id) == gw

        await mgr._reap_once()

        assert routing.owner_of(session.id) is None, "paused-sweep teardown must release the routing lease"
        # Freed immediately, not just expired — another gateway can claim
        # it right away instead of waiting out the TTL.
        assert routing.claim_session(session.id, "other-gateway:999", ttl_s=60) is True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Wave-2F task 1: session routing leases (app/chat/routing.py) wiring
# ---------------------------------------------------------------------------


def test_spawn_claims_routing_lease(manager: ChatManager):
    """_spawn_live claims `chat:{chat_id}` for this gateway as soon as the
    session is registered in self._live."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: routing.owner_of(s.id) is not None)

        assert routing.owner_of(s.id) == routing.this_gateway_id()

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_kill_releases_routing_lease(manager: ChatManager):
    """kill() releases the routing lease so a takeover (or a later respawn
    on another gateway) doesn't have to wait out the TTL."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: routing.owner_of(s.id) is not None)
        assert routing.owner_of(s.id) is not None

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

        assert routing.owner_of(s.id) is None
        # Freed, not just expired — another gateway could claim it right away.
        assert routing.claim_session(s.id, "other-gateway:999", ttl_s=60) is True

    asyncio.run(_run())


def test_renew_routing_leases_keeps_ownership(manager: ChatManager):
    """_renew_routing_leases (invoked from _reap_once's ~60s tick) extends
    the lease for every non-DEAD live session without changing ownership."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        gw = routing.this_gateway_id()
        await _wait_until(lambda: routing.owner_of(s.id) == gw)
        assert routing.owner_of(s.id) == gw

        await manager._renew_routing_leases()

        assert routing.owner_of(s.id) == gw

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_spawn_continues_when_routing_lease_contended(manager: ChatManager):
    """Another gateway holding `chat:{chat_id}` for a session that was
    never actually spawned anywhere (no sandbox_id/runner_pid persisted —
    e.g. a bare claim_session call, as simulated here) must not block this
    replica from serving it: attach() now runs the wave-2F task 5
    cross-gateway takeover path (steal the lease, no-op destroy since there
    is no old sandbox, fresh spawn), so the session ends up live here AND
    this replica ends up the genuine lease owner — not just "serving
    despite a lost claim" (task 1's original mechanism-only posture, now
    superseded by task 5's real takeover)."""
    from app.chat import routing

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)

        assert routing.claim_session(s.id, "other-gateway:999", ttl_s=60) is True

        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: s.id in manager._live and routing.owner_of(s.id) == routing.this_gateway_id())

        assert s.id in manager._live  # served locally via takeover
        assert routing.owner_of(s.id) == routing.this_gateway_id(), "takeover claims the lease for real"

        await manager.kill(s.id, reason="test_done")
        handle.emit_eof()
        await attach_task

    asyncio.run(_run())


def test_renew_outage_keeps_serving_but_genuine_steal_tears_down(manager: ChatManager, monkeypatch):
    """Critical-3: `renew_session` returning False is ambiguous by design
    (see app.chat.routing's module docstring) — it collapses "another
    gateway genuinely stole the lease" and "the coordination backend is
    unreachable right now" into the same False. `_renew_routing_leases`
    must disambiguate with a second, independent `owner_of` read and only
    tear the local session down when that read POSITIVELY shows a
    different, concrete gateway holding it.

    Scenario A: the coordination backend itself is unreachable (both
    `lease_renew` and `lease_owner` raise `CoordinationUnavailable`, which
    `app.chat.routing` degrades to False/None respectively) — there is no
    positive proof of loss, so the session must keep being served locally.

    Scenario B: a genuine steal — a different, concrete gateway actually
    holds the lease (claimed for real against the same shared coordination
    backend) — renew fails AND `owner_of` positively names someone else.
    This must tear the session down.

    Load-bearing: reverting the Critical-3 fix in
    `ChatManager._renew_routing_leases` (`git stash`) makes Scenario A fail
    — the session is torn down on the bare coordination outage even
    though nobody actually took it over.
    """
    import app.chat.routing as routing_mod
    from app.chat import routing
    from app.coordination.base import CoordinationUnavailable

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        # Poll for attach to BOTH register the session locally AND finish
        # claiming the routing lease. `s.id in _live` is set *before*
        # `_claim_routing_lease`'s to_thread completes, so waiting only on
        # `_live` let Scenario A's `_renew_routing_leases` (and the "still
        # ours" sanity assert) run before the lease existed — `owner_of`
        # returned None and the assert flaked under CI xdist contention.
        # Wait on the real precondition the whole test depends on: we own it.
        _gw0 = routing.this_gateway_id()
        await _wait_until(lambda: s.id in manager._live and routing.owner_of(s.id) == _gw0)
        assert s.id in manager._live

        # --- Scenario A: coordination-backend outage. Both renew_session
        # and owner_of degrade the same way — no way to positively
        # attribute the failed renew to a genuine steal, so this must NOT
        # tear the session down.
        class _BrokenBackend:
            def lease_renew(self, *a, **k):
                raise CoordinationUnavailable("boom")

            def lease_owner(self, *a, **k):
                raise CoordinationUnavailable("boom")

        with monkeypatch.context() as m:
            m.setattr(routing_mod, "coordination", lambda: _BrokenBackend())
            await manager._renew_routing_leases()

        assert s.id in manager._live, (
            "a renew failure that degrades from a coordination-backend outage "
            "(not a positively-confirmed steal) must NOT tear the session down"
        )
        assert manager._live[s.id].state != SessionState.DEAD

        # --- Scenario B: genuine steal against the REAL (memory) backend —
        # a different, concrete gateway actually now holds the lease.
        gw = routing.this_gateway_id()
        assert routing.owner_of(s.id) == gw, "sanity: still ours after the outage blip in Scenario A"
        routing.release_session(s.id, gw)
        assert routing.claim_session(s.id, "other-gateway:999", ttl_s=60) is True

        await manager._renew_routing_leases()

        assert s.id not in manager._live, (
            "a renew failure WITH owner_of positively showing a different gateway must tear the session down"
        )

        handle.killed = True
        try:
            await asyncio.wait_for(attach_task, timeout=1.0)
        except asyncio.TimeoutError:
            attach_task.cancel()

    asyncio.run(_run())


def test_routing_lease_calls_offloaded_to_thread(manager: ChatManager):
    """Important finding: _claim_routing_lease/_renew_routing_leases must
    run the coordination-backend lease call via asyncio.to_thread, not
    synchronously on the event loop — under the redis backend each is a
    blocking socket round-trip (WATCH/MULTI/EXEC) that would otherwise
    stall the whole process for every live session on every reaper tick.

    Proof: install a coordination backend whose lease_acquire/lease_renew
    block synchronously for a noticeable duration, then confirm a
    concurrently-running coroutine keeps making progress (its tick counter
    advances) while the lease call is in flight. If the lease call ran
    on-loop instead of via to_thread, the ticker would be starved and the
    counter would stay near zero.
    """
    import time as _time

    import app.coordination.factory as factory
    from app.coordination.memory import MemoryCoordinationBackend

    class _SlowLeaseBackend(MemoryCoordinationBackend):
        """MemoryCoordinationBackend whose lease primitives block
        synchronously — simulates a slow Redis round-trip."""

        def __init__(self, delay: float) -> None:
            super().__init__()
            self.delay = delay

        def lease_acquire(self, name, holder_id, *, ttl_s):
            _time.sleep(self.delay)
            return super().lease_acquire(name, holder_id, ttl_s=ttl_s)

        def lease_renew(self, name, holder_id, *, ttl_s):
            _time.sleep(self.delay)
            return super().lease_renew(name, holder_id, ttl_s=ttl_s)

    async def _run():
        factory._instance = _SlowLeaseBackend(delay=0.3)
        try:
            ticks = 0

            async def _ticker():
                nonlocal ticks
                for _ in range(60):
                    await asyncio.sleep(0.01)
                    ticks += 1

            ticker_task = asyncio.create_task(_ticker())
            await manager._claim_routing_lease("chat-claim-slow")
            await asyncio.sleep(0)  # let the ticker record its latest tick

            # A 0.3s blocking call, if truly offloaded, overlaps with ~30
            # ticker iterations (0.01s each); a call that instead blocked
            # the loop would leave `ticks` near 0.
            assert ticks > 10, f"event loop appears stalled during _claim_routing_lease (ticks={ticks})"

            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task

            # Same proof for the reaper's per-tick renew path — needs a
            # live (non-DEAD) session in self._live to iterate over.
            from datetime import datetime, timezone

            from app.chat.manager import LiveSession

            manager._live["chat-claim-slow"] = LiveSession(
                chat_id="chat-claim-slow",
                user_email="u@x",
                state=SessionState.ACTIVE,
                handle=None,
                started_at=datetime.now(timezone.utc),
                last_activity=datetime.now(timezone.utc),
                sinks=[],
            )
            ticks = 0
            ticker_task = asyncio.create_task(_ticker())
            await manager._renew_routing_leases()
            await asyncio.sleep(0)
            ticker_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await ticker_task
            assert ticks > 10, f"event loop appears stalled during _renew_routing_leases (ticks={ticks})"
        finally:
            reset_coordination_for_tests()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# _resume_live reentrancy guard: concurrent resume must not double-spawn
# ---------------------------------------------------------------------------


def test_concurrent_resume_live_serialized_no_double_spawn(tmp_path, monkeypatch):
    """Two concurrent ``_resume_live`` calls on one PAUSED session (attach()
    racing a simulated inbound-consumer wake) must resume the sandbox
    exactly once.

    Without ``LiveSession._resume_lock`` this races: both calls reach
    ``FakeProvider.resume`` concurrently, which pops the parked handle out
    of ``provider.paused`` — whichever call wins the pop succeeds, and the
    loser's own ``sandbox_id not in self.paused`` check now fails, so it
    falls back to ``_respawn_fresh`` and spawns a SECOND, entirely new
    sandbox. The winner's already-resumed handle is then silently
    overwritten on ``live.handle`` by the loser's fresh spawn and never
    referenced again — an orphaned, still-billable sandbox leak — while the
    winner's own crash-respawn wait task (bound to that now-unreferenced
    handle) is left running, unmonitored, alongside the loser's fresh
    pump/wait pair: 3 alive tasks instead of 2.

    This test is proven load-bearing: reverting the `_resume_lock` guard in
    `ChatManager._resume_live` (`git stash` the fix in `app/chat/manager.py`)
    makes it fail — `len(provider.spawned) == 2` and 3 alive tasks — and it
    passes once the lock guards the method.
    """
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    # Inject realistic resume() latency (per the task) so two concurrent
    # _resume_live callers actually interleave inside the provider call
    # instead of one completing before the other is even scheduled.
    orig_resume = provider.resume

    async def _slow_resume(*, sandbox_id, runner_pid, env):
        await asyncio.sleep(0.05)
        return await orig_resume(sandbox_id=sandbox_id, runner_pid=runner_pid, env=env)

    provider.resume = _slow_resume

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._known_protocol_sessions)
        assert s.id in mgr._known_protocol_sessions, "precondition: non-legacy (known-protocol) resume path"
        await mgr.detach_sink(s.id, ws)
        await _wait_until(lambda: provider.paused)  # linger=0, pause fires
        assert provider.paused, "precondition: sandbox must be parked (paused) before the race"
        assert len(provider.spawned) == 1
        live = mgr._live[s.id]
        assert live.state == SessionState.PAUSED

        # Two concurrent resume triggers on the SAME PAUSED LiveSession —
        # mirrors attach() (WS reconnect) racing _inbound_consumer_loop's
        # resume-on-wake call, both hitting the same PAUSED session.
        results = await asyncio.gather(
            mgr._resume_live(live),
            mgr._resume_live(live),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                raise r

        assert len(provider.spawned) == 1, (
            f"expected exactly ONE resume/spawn for the session, got {len(provider.spawned)} total spawns "
            "— a second spawn means the race produced a leaked, orphaned sandbox"
        )
        assert not provider.paused, f"no sandbox should be left parked/leaked in the provider: {provider.paused}"
        assert live.state == SessionState.ACTIVE
        alive_tasks = [t for t in live.tasks if not t.done()]
        assert len(alive_tasks) == 2, (
            f"expected exactly 2 live tasks (pump+wait), got {len(alive_tasks)} "
            "— extra tasks are orphaned pump/wait survivors from a double-resume"
        )

        await mgr.kill(s.id, reason="test_done")
        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# attach() + send_user_message() session-lock guard: concurrent spawn/resume
# decisions for a chat_id not yet in _live must not double-spawn.
# ---------------------------------------------------------------------------


def test_concurrent_attach_and_send_user_message_no_double_spawn(tmp_path, monkeypatch):
    """attach() (WS reconnect) and send_user_message() (e.g. an inbound
    webhook) racing the SAME post-restart chat_id — no LiveSession in
    memory yet, but the repo row still carries sandbox_id/runner_pid from
    before — must resume exactly ONE runner (no second spawn), and the
    message must reach that single runner.

    Setup mirrors ``test_attach_after_restart_resumes_from_repo_row``:
    spawn+pause a session, then ``mgr._live.clear()`` to simulate the
    in-memory state a process restart leaves behind, while the repo row
    keeps its sandbox refs.

    Without wrapping send_user_message's own "no local live session yet"
    resume-from-row decision in the same ``self._get_session_lock(chat_id)``
    attach() uses, both coroutines read ``self._live.get(chat_id)`` as
    ``None``, both see the repo row's sandbox refs, and both call
    ``_resume_from_row`` concurrently and unserialized against each other.
    ``_resume_from_row``'s own ``provider.resume()`` pops the parked handle
    out of the fake provider's ``paused`` dict — only one caller's pop can
    win; the loser's resume raises, and ``_resume_from_row`` reacts by
    destroying the (now-resumed, still-billable) sandbox and clearing its
    ref, and its caller then falls back to a brand new ``_spawn_live`` —
    a second spawn for a session that should have been a pure resume,
    while the winner's freshly-resumed handle is torn down out from under
    it by that same destroy call.

    This test is proven load-bearing: reverting the session-lock wrap
    around send_user_message's spawn/resume decision (``git stash`` the fix
    in ``app/chat/manager.py``) makes it fail — a second entry appears in
    ``provider.spawned`` (or the race raises/corrupts state entirely) —
    and it passes once the decision is serialized under the same
    per-chat_id lock as ``attach()``.
    """
    import app.chat.manager as manager_mod

    fake_tickets = _FakeTicketRepo()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: fake_tickets)

    mgr = _make_pause_manager(tmp_path, linger_seconds=0)
    monkeypatch_workdir(mgr)
    provider = mgr._provider

    async def _run():
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws1 = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws1))
        await _wait_until(lambda: s.id in mgr._known_protocol_sessions)
        assert s.id in mgr._known_protocol_sessions, "precondition: non-legacy (known-protocol) resume path"
        await mgr.detach_sink(s.id, ws1)
        await _wait_until(lambda: provider.paused)  # linger=0, pause fires
        assert provider.paused, "precondition: sandbox must be parked (paused) before the race"
        assert len(provider.spawned) == 1

        # Simulate server restart: clear in-memory _live. The repo row
        # keeps its sandbox_id/runner_pid, so both racers below see "no
        # local live session, but a resumable repo row" — exactly the
        # window send_user_message's own decision used to run unlocked.
        mgr._live.clear()

        # Inject realistic resume() latency so attach() and
        # send_user_message() actually interleave inside the race window
        # instead of one completing before the other is even scheduled.
        orig_resume = provider.resume

        async def _slow_resume(*, sandbox_id, runner_pid, env):
            await asyncio.sleep(0.05)
            return await orig_resume(sandbox_id=sandbox_id, runner_pid=runner_pid, env=env)

        provider.resume = _slow_resume

        ws2 = FakeWS()
        attach_task2 = asyncio.create_task(mgr.attach(s.id, ws2))
        send_task = asyncio.create_task(mgr.send_user_message(s.id, "hello after restart"))
        results = await asyncio.gather(attach_task2, send_task, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                raise r

        assert len(provider.spawned) == 1, (
            f"expected NO additional spawn (a pure resume), got {len(provider.spawned)} total spawns "
            "— a second spawn means attach() and send_user_message() raced _resume_from_row independently"
        )
        assert not provider.paused, f"no sandbox should be left parked/leaked in the provider: {provider.paused}"
        live = mgr._live[s.id]
        assert live.state == SessionState.ACTIVE
        alive_tasks = [t for t in live.tasks if not t.done()]
        assert len(alive_tasks) == 2, (
            f"expected exactly 2 live tasks (pump+wait), got {len(alive_tasks)} "
            "— extra tasks are orphaned pump/wait survivors from a double-resume/double-spawn race"
        )

        # The message must have reached the single (resumed) runner's
        # stdin exactly once — not lost, not duplicated onto an orphaned
        # second runner.
        handle = live.handle
        payloads = [json.loads(b.decode()) for b in handle._stdin_buf]
        user_msgs = [p for p in payloads if p.get("type") == "user_msg"]
        assert len(user_msgs) == 1, f"expected exactly one user_msg delivered, got {user_msgs}"
        assert user_msgs[0]["text"] == "hello after restart"

        await mgr.kill(s.id, reason="test_done")
        for t in [attach_task, attach_task2]:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


def test_idle_reaper_loop_survives_sweep_error(tmp_path, monkeypatch):
    """#867: a single failing sweep must NOT kill the reaper task. Before the
    guard, one unhandled error in _reap_once propagated out of the loop and the
    reaper died silently — after which sandboxes accumulated with nothing
    reaping them."""
    import app.chat.manager as manager_mod

    mgr = _make_pause_manager(tmp_path)

    attempts = {"n": 0}

    async def flaky_reap():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient sweep failure")

    mgr._reap_once = flaky_reap

    sleeps = {"n": 0}

    async def fake_sleep(_secs):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(manager_mod.asyncio, "sleep", fake_sleep)

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await mgr._idle_reaper_loop()

    asyncio.run(_run())
    assert attempts["n"] >= 2, "reaper loop died after the first failing sweep"


def test_reap_once_paused_sweep_runs_even_if_kill_raises(tmp_path):
    """#867: a failure in the kill phase must not abort the sweep — the
    paused-TTL teardown still runs (per-phase/per-item guards)."""

    async def _run():
        from datetime import datetime as _dt, timedelta, timezone as _tz

        mgr = _make_pause_manager(tmp_path)
        monkeypatch_workdir(mgr)
        provider = mgr._provider

        # An expired paused session (repo row only — no live entry needed).
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        mgr._repo.set_sandbox_ref(s.id, sandbox_id="sbx-paused", runner_pid=1)
        mgr._repo.set_sandbox_paused_at(s.id, _dt.now(_tz.utc) - timedelta(seconds=mgr._config.paused_ttl_seconds + 1))

        # A DEAD live entry → reaper's to_kill (dead_gc); make kill blow up.
        s2 = await mgr.create_session(user_email="u2@x", surface=Surface.WEB)
        ws = FakeWS()
        await mgr.attach(s2.id, ws)
        mgr._live[s2.id].state = SessionState.DEAD

        async def boom_kill(chat_id, reason=None):
            raise RuntimeError("kill boom")

        mgr.kill = boom_kill

        await mgr._reap_once()

        # Despite the kill phase raising, the paused-TTL sweep still destroyed
        # the expired sandbox and cleared its ref.
        assert "sbx-paused" in provider.destroyed, "paused sweep did not run after kill raised"
        assert mgr._repo.get_session(s.id).sandbox_id is None

    asyncio.run(_run())


def test_turn_buffer_holds_tool_results_for_midturn_replay(manager: ChatManager):
    """tool_result frames must ride the turn_buffer alongside token and
    tool_call: a mid-turn reconnect replays the buffer, and replaying the
    calls without their results left every tool block stuck on "running…"
    after a refresh."""

    async def _run():
        handle = FakeHandle()
        manager._provider.spawn = AsyncMock(return_value=handle)

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(manager.attach(s.id, ws))
        await _wait_until(lambda: _ws_seated(manager, s.id, ws))
        handle.emit({"type": "tool_call", "tool_use_id": "toolu_1", "tool": "Bash", "args": {}})
        handle.emit({"type": "tool_result", "tool_use_id": "toolu_1", "result": "ok"})
        await _wait_until(
            lambda: len(manager._live[s.id].turn_buffer) >= 2,
        )
        types = [f["type"] for f in manager._live[s.id].turn_buffer]
        assert types == ["tool_call", "tool_result"]
        # The runner's pairing key survives the envelope stamp untouched.
        assert all(f.get("tool_use_id") == "toolu_1" for f in manager._live[s.id].turn_buffer)
        attach_task.cancel()

    asyncio.run(_run())


class TestRestoreContext:
    """Tier 3 continuity: fresh sandboxes get a restored-conversation
    transcript (system-prompt append) instead of the old 3-user-turn stdin
    replay that dropped assistant context and ran an LLM turn per message."""

    def test_returns_none_for_fresh_session(self, manager: ChatManager):
        async def _run():
            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            sess = manager._repo.get_session(s.id)
            assert manager._build_restore_context(sess) is None

        asyncio.run(_run())

    def test_includes_user_and_assistant_turns_in_order(self, manager: ChatManager):
        async def _run():
            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            manager._repo.append_message(session_id=s.id, role="user", content="how many rows?")
            manager._repo.append_message(session_id=s.id, role="assistant", content="There are 42 rows.")
            manager._repo.append_message(session_id=s.id, role="user", content="which table?")
            sess = manager._repo.get_session(s.id)
            ctx = manager._build_restore_context(sess)
            assert ctx is not None
            assert "Restored conversation context" in ctx
            # Assistant turns are the whole point — the old replay dropped them.
            assert "There are 42 rows." in ctx
            assert ctx.index("how many rows?") < ctx.index("There are 42 rows.") < ctx.index("which table?")

        asyncio.run(_run())

    def test_total_char_budget_keeps_newest(self, manager: ChatManager):
        async def _run():
            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            for i in range(30):
                manager._repo.append_message(session_id=s.id, role="user", content=f"msg-{i} " + "x" * 3000)
            sess = manager._repo.get_session(s.id)
            ctx = manager._build_restore_context(sess)
            assert ctx is not None
            assert len(ctx) <= manager._RESTORE_TOTAL_CHAR_CAP + 2000  # header slack
            assert "msg-29" in ctx, "newest message must survive the budget"
            assert "msg-0 " not in ctx, "oldest message must be dropped first"

        asyncio.run(_run())

    def test_spawn_uploads_restore_context_when_history_exists(self, manager: ChatManager, tmp_path, monkeypatch):
        from app.chat.e2b_workspace_sync import SANDBOX_CONTEXT_RESTORE

        monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

        async def _run():
            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            manager._repo.append_message(session_id=s.id, role="assistant", content="earlier answer")
            sess = manager._repo.get_session(s.id)

            handle = FakeHandle()
            writes: dict = {}
            sb = MagicMock()
            sb.files = MagicMock()

            async def _write(path, data):
                writes[path] = data

            sb.files.write = AsyncMock(side_effect=_write)
            sb.commands = MagicMock()
            sb.commands.run = AsyncMock()
            handle._sandbox = sb

            async def fake_spawn(**kw):
                return handle

            manager._provider.spawn = fake_spawn
            _make_provider_e2b_shaped(manager)
            await manager._spawn_runner(sess, tmp_path)
            assert SANDBOX_CONTEXT_RESTORE in writes
            assert "earlier answer" in str(writes[SANDBOX_CONTEXT_RESTORE])

        asyncio.run(_run())

    def test_sr11_departed_participant_turns_omitted(self, manager: ChatManager):
        """SR-11: a departed co-session participant's user turns must not be
        restored into the fresh runner's context; assistant turns stay (they
        were already visible to every remaining participant)."""

        async def _run():
            s = await manager.create_session(user_email="owner@x", surface=Surface.WEB)
            # Co-session flag lives on the row; participants carry membership.
            manager._repo._conn.execute("UPDATE chat_sessions SET is_co_session = TRUE WHERE id = ?", [s.id])
            manager._repo.add_session_participant(session_id=s.id, user_email="owner@x", user_id="u1", role="owner")
            manager._repo.add_session_participant(session_id=s.id, user_email="guest@x", user_id="u2", role="member")
            manager._repo.append_message(session_id=s.id, role="user", content="owner question", sender_email="owner@x")
            manager._repo.append_message(
                session_id=s.id, role="user", content="guest secret question", sender_email="guest@x"
            )
            manager._repo.append_message(session_id=s.id, role="assistant", content="shared answer")
            manager._repo.remove_participant(s.id, "guest@x")

            sess = manager._repo.get_session(s.id)
            ctx = manager._build_restore_context(sess)
            assert ctx is not None
            assert "owner question" in ctx
            assert "guest secret question" not in ctx, "departed participant's turn leaked into restored context"
            assert "shared answer" in ctx

        asyncio.run(_run())

    def test_crash_respawn_redelivers_trailing_unanswered_question(self, manager: ChatManager, monkeypatch):
        """A crash mid-turn leaves a persisted user turn with no assistant
        reply (send_user_message persists BEFORE delivering). The respawn
        must re-deliver exactly that one question as a LIVE turn — the
        restored transcript is read-only by instruction, so without this the
        user saw a crash notice and then silence (Devin review on #1030)."""
        monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

        async def _run():
            handles = [FakeHandle(), FakeHandle()]
            spawn_calls = iter(handles)

            async def fake_spawn(**kw):
                return next(spawn_calls)

            manager._provider.spawn = fake_spawn

            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws = FakeWS()
            attach_task = asyncio.create_task(manager.attach(s.id, ws))
            await _wait_until(lambda: _ws_seated(manager, s.id, ws))
            # Pending question: persisted, never answered (crash follows).
            manager._repo.append_message(session_id=s.id, role="user", content="pending question?")
            handles[0].emit_eof()
            handles[0].killed = True
            await _wait_until(lambda: any(m.get("type") == "ready" for m in ws.sent))
            # The fresh runner got EXACTLY the one pending question, live.
            await _wait_until(lambda: "pending question?" in _stdin_user_texts(handles[1]))
            assert _stdin_user_texts(handles[1]).count("pending question?") == 1
            # Redelivery must go through the same turn-state bookkeeping as
            # any other live turn — otherwise _linger_then_pause doesn't know
            # a turn is in flight and can pause the sandbox mid-answer if the
            # user disconnects before the reply arrives (Devin review on
            # #1030, follow-up finding).
            assert manager._live[s.id].turn_in_flight, "redelivery must set turn_in_flight"

            await manager.kill(s.id, reason="test_done")
            handles[1].emit_eof()
            await attach_task

        asyncio.run(_run())

    def test_crash_respawn_skips_redelivery_when_last_turn_answered(self, manager: ChatManager, monkeypatch):
        """No trailing unanswered turn → nothing is re-delivered (the old
        3-turn replay re-answered already-answered questions)."""
        monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "tok")

        async def _run():
            handles = [FakeHandle(), FakeHandle()]
            spawn_calls = iter(handles)

            async def fake_spawn(**kw):
                return next(spawn_calls)

            manager._provider.spawn = fake_spawn

            s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
            ws = FakeWS()
            attach_task = asyncio.create_task(manager.attach(s.id, ws))
            await _wait_until(lambda: _ws_seated(manager, s.id, ws))
            manager._repo.append_message(session_id=s.id, role="user", content="answered question")
            manager._repo.append_message(session_id=s.id, role="assistant", content="the answer")
            handles[0].emit_eof()
            handles[0].killed = True
            await _wait_until(lambda: any(m.get("type") == "ready" for m in ws.sent))
            assert _stdin_user_texts(handles[1]) == []

            await manager.kill(s.id, reason="test_done")
            handles[1].emit_eof()
            await attach_task

        asyncio.run(_run())


def test_shutdown_drains_inflight_turn_with_notice(tmp_path):
    """A session whose turn is IN FLIGHT at shutdown and is about to be
    KILLED gets a user-facing 'server_restarting' notice + a done frame (so
    the composer unwedges) — instead of the answer stopping mid-generation
    with no explanation (robustness parity drain notice)."""

    async def _run():
        mgr = _make_kill_manager(tmp_path)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live and mgr._live[s.id].handle is not None and mgr._live[s.id].sinks)
        # Simulate a turn in progress.
        mgr._live[s.id].turn_in_flight = True

        await mgr.shutdown()

        kinds = [m.get("kind") for m in ws.sent if m.get("type") == "error"]
        assert "server_restarting" in kinds, f"expected drain notice; got {ws.sent}"
        assert any(m.get("type") == "done" for m in ws.sent), "expected a done frame to unwedge the composer"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_shutdown_pause_path_suppresses_notice(tmp_path):
    """When the session is successfully PAUSED, the in-flight turn survives
    the restart (the sandbox snapshot keeps it running; `_resume_live`
    delivers its frames on reconnect) — so no 'please resend' notice must be
    sent, or the user would be invited to fire a duplicate turn."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live and mgr._live[s.id].handle is not None and mgr._live[s.id].sinks)
        mgr._live[s.id].turn_in_flight = True

        await mgr.shutdown()

        kinds = [m.get("kind") for m in ws.sent if m.get("type") == "error"]
        assert "server_restarting" not in kinds, f"pause path must stay silent; got {ws.sent}"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_shutdown_pause_failure_falls_back_to_notice(tmp_path):
    """If the pause attempt fails, the session falls back to the kill path —
    the in-flight turn IS lost there, so the drain notice must fire."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live and mgr._live[s.id].handle is not None and mgr._live[s.id].sinks)
        mgr._live[s.id].turn_in_flight = True

        async def _boom(live):
            raise RuntimeError("pause backend down")

        mgr._pause_live = _boom

        await mgr.shutdown()

        kinds = [m.get("kind") for m in ws.sent if m.get("type") == "error"]
        assert "server_restarting" in kinds, f"expected drain notice on pause failure; got {ws.sent}"
        assert any(m.get("type") == "done" for m in ws.sent), "expected a done frame to unwedge the composer"

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


def test_shutdown_no_notice_when_idle(tmp_path):
    """A session with no in-flight turn is drained silently (no spurious
    server_restarting notice)."""

    async def _run():
        mgr = _make_pause_manager(tmp_path, linger_seconds=999)
        monkeypatch_workdir(mgr)
        s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        attach_task = asyncio.create_task(mgr.attach(s.id, ws))
        await _wait_until(lambda: s.id in mgr._live and mgr._live[s.id].handle is not None and mgr._live[s.id].sinks)
        mgr._live[s.id].turn_in_flight = False

        await mgr.shutdown()

        kinds = [m.get("kind") for m in ws.sent if m.get("type") == "error"]
        assert "server_restarting" not in kinds

        attach_task.cancel()
        try:
            await attach_task
        except (asyncio.CancelledError, Exception):
            pass

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Orphan sandbox reconciliation (host-local providers)
# ---------------------------------------------------------------------------


def _orphan_provider(manager: ChatManager, rows: list[dict]) -> list[str]:
    """Give the fixture's provider the host-local-provider extras: an async
    ``list_sandboxes`` (what the docker provider exposes) plus a recording
    ``destroy``. Returns the list destroyed sandbox ids land in."""
    destroyed: list[str] = []

    async def _list():
        return rows

    async def _destroy(*, sandbox_id):
        destroyed.append(sandbox_id)

    manager._provider.list_sandboxes = _list
    manager._provider.destroy = _destroy
    return destroyed


def test_orphan_sweep_destroys_sandboxes_with_no_owner(manager: ChatManager):
    """A gateway that crashed mid-session leaves containers behind with no row
    left to reap them — the paused-TTL sweep only ever sees rows."""

    async def _run():
        destroyed = _orphan_provider(
            manager,
            [{"name": "agnes-chatsbx-gone-1", "chat_id": "chat_gone", "age_seconds": 3600.0}],
        )
        assert await manager.reap_orphan_sandboxes() == 1
        assert destroyed == ["agnes-chatsbx-gone-1"]

    asyncio.run(_run())


def test_orphan_sweep_keeps_a_sandbox_its_row_still_points_at(manager: ChatManager):
    """The paused-and-resumable case: a row whose sandbox_id is this container."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        manager._repo.set_sandbox_ref(s.id, sandbox_id="agnes-chatsbx-live-1", runner_pid=1)
        destroyed = _orphan_provider(
            manager,
            [{"name": "agnes-chatsbx-live-1", "chat_id": s.id, "age_seconds": 3600.0}],
        )
        assert await manager.reap_orphan_sandboxes() == 0
        assert destroyed == []

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Approval-decision routing (review follow-ups on #1145)
# ---------------------------------------------------------------------------


def test_approval_decision_local_delivery(manager: ChatManager):
    """With a live local runner, the decision is written to its stdin — no
    cross-gateway publish."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        handle = FakeHandle()
        live = MagicMock()
        live.handle = handle
        live._stdin_lock = asyncio.Lock()
        manager._live[s.id] = live
        with patch("app.chat.inbound.publish_control", new=AsyncMock()) as pub:
            await manager.deliver_approval_decision(s.id, "appr-1", "allow", sender_email="u@x")
        pub.assert_not_called()
        written = b"".join(handle._stdin_buf)
        assert b"approval_decision" in written
        assert b"appr-1" in written

    asyncio.run(_run())


def test_orphan_sweep_skips_young_sandboxes(manager: ChatManager):
    """_spawn_runner returns before set_sandbox_ref persists — a just-created
    sandbox legitimately has no row yet."""

    async def _run():
        destroyed = _orphan_provider(
            manager,
            [{"name": "agnes-chatsbx-fresh-1", "chat_id": "chat_fresh", "age_seconds": 5.0}],
        )
        assert await manager.reap_orphan_sandboxes() == 0
        assert destroyed == []

    asyncio.run(_run())


def test_approval_decision_dropped_when_no_runner_and_no_owner(manager: ChatManager):
    """No local runner AND no other gateway owns it → drop, never publish a
    junk control entry (which could disconnect the caller if coordination is
    down). Mirrors the kill/cancel owner check (review finding on #1145)."""

    async def _run():
        with (
            patch("app.chat.routing.owner_of", return_value=None),
            patch("app.chat.inbound.publish_control", new=AsyncMock()) as pub,
        ):
            await manager.deliver_approval_decision("chat_dead", "appr-2", "deny", sender_email="u@x")
        pub.assert_not_called()

    asyncio.run(_run())


def test_orphan_sweep_skips_sessions_this_process_serves(manager: ChatManager):
    from datetime import datetime, timezone

    from app.chat.manager import LiveSession

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        now = datetime.now(timezone.utc)
        manager._live[s.id] = LiveSession(
            chat_id=s.id,
            user_email="u@x",
            state=SessionState.ACTIVE,
            handle=FakeHandle(),
            started_at=now,
            last_activity=now,
        )
        destroyed = _orphan_provider(
            manager,
            [{"name": "agnes-chatsbx-inflight-1", "chat_id": s.id, "age_seconds": 3600.0}],
        )
        assert await manager.reap_orphan_sandboxes() == 0
        assert destroyed == []

    asyncio.run(_run())


def test_approval_decision_forwarded_to_remote_owner(manager: ChatManager):
    """No local runner but another gateway owns it → forward over the inbound
    control stream (command='approval')."""

    async def _run():
        with (
            patch("app.chat.routing.owner_of", return_value="other-gateway"),
            patch("app.chat.routing.this_gateway_id", return_value="this-gateway"),
            patch("app.chat.inbound.publish_control", new=AsyncMock()) as pub,
        ):
            await manager.deliver_approval_decision("chat_remote", "appr-3", "allow", sender_email="u@x")
        pub.assert_awaited_once()
        assert pub.await_args.args[1] == "approval"

    asyncio.run(_run())


def test_approval_decision_publish_failure_does_not_escape(manager: ChatManager):
    """A coordination hiccup must not drop the user's chat connection.

    `deliver_approval_decision` runs on the WebSocket reader path, so an
    escaping `InboundPublishFailed` would disconnect the window instead of
    losing just the answer. The gate's own timeout still resolves the
    pending request, so the turn finishes either way. Mirrors the
    cross-gateway kill path, which already swallows this.
    """
    import app.chat.inbound as inbound

    async def _run():
        with (
            patch("app.chat.routing.owner_of", return_value="other-gateway"),
            patch("app.chat.routing.this_gateway_id", return_value="this-gateway"),
            patch(
                "app.chat.inbound.publish_control",
                new=AsyncMock(side_effect=inbound.InboundPublishFailed("coordination down")),
            ),
        ):
            # must not raise
            await manager.deliver_approval_decision("chat_remote", "appr-9", "allow", sender_email="u@x")

    asyncio.run(_run())


def test_approval_decision_hardens_invalid_to_deny(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        handle = FakeHandle()
        live = MagicMock()
        live.handle = handle
        live._stdin_lock = asyncio.Lock()
        manager._live[s.id] = live
        await manager.deliver_approval_decision(s.id, "appr-4", "bogus", sender_email="u@x")
        written = b"".join(handle._stdin_buf)
        assert b'"decision": "deny"' in written

    asyncio.run(_run())


def test_spawn_env_arms_approvals_on_every_surface(manager: ChatManager, monkeypatch):
    """The gate is armed regardless of origin surface — whether anyone can
    answer a request is decided per-request at the fan-out from the attached
    sinks, not once at spawn from `session.surface`. A Slack session
    continued on the web must be able to approve."""
    monkeypatch.setattr("app.auth.access.mint_session_jwt", lambda *a, **k: "jwt")
    import app.chat.manager as manager_mod

    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: _FakeTicketRepo())

    captured = {}

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle()

    manager._provider.spawn = fake_spawn

    async def _env_for(surface, **kw):
        s = await manager.create_session(user_email="u@x", surface=surface, **kw)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, Path("/tmp"))
        return captured["env"]

    async def _run():
        # No surface switches the gate off: the runner defaults to armed and
        # nothing here overrides it (AGNES_APPROVALS survives only as an
        # operator kill-switch).
        for env in (
            await _env_for(Surface.WEB),
            await _env_for(Surface.API),
            await _env_for(Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None),
        ):
            assert env.get("AGNES_APPROVALS", "on") == "on"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# approval_request fan-out: who (if anyone) can answer a pending request
# ---------------------------------------------------------------------------


def _approval_decisions_written(handle) -> list[dict]:
    """Every approval_decision frame the manager wrote to the runner's stdin."""
    out = []
    for line in b"".join(handle._stdin_buf).decode().splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("type") == "approval_decision":
            out.append(frame)
    return out


async def _pump_one_approval_request(mgr: ChatManager, live, *, request_id: str = "appr-1"):
    """Run the pump over a single approval_request frame and stop."""
    pump = asyncio.create_task(mgr._pump_subprocess_to_ws(live))
    live.handle.emit(
        {
            "type": "approval_request",
            "request_id": request_id,
            "tool": "Bash",
            "command": "agnes admin user delete bob@x",
            "reason": "admin mutation",
            "timeout_seconds": 300,
        }
    )
    await _wait_until(lambda: bool(live.pending_approvals))
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass


def test_slack_origin_approval_waits_for_a_client(manager: ChatManager):
    """A Slack-origin session has no card-capable sink, but the user is one
    "Continue on web" click away — so the request is NOT auto-denied. It
    stays pending (the gate's own timeout is the backstop) and rides
    ``pending_approvals`` so a browser attaching later replays the card."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value)
        await _pump_one_approval_request(manager, live)

        assert _approval_decisions_written(live.handle) == [], "a Slack session must not be auto-denied"
        pending = list(live.pending_approvals.values())
        assert pending and pending[0]["attended"] is False

    asyncio.run(_run())


def test_slack_session_continued_on_web_can_approve(manager: ChatManager):
    """The bug this fixes: a session STARTED in Slack and opened through the
    "Continue on web" deep link has a real web sink attached, so the request
    is marked attended, is never auto-denied, and the user's decision
    reaches the runner."""

    async def _run():
        from app.chat.replay import GapReplayGate
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        # The Slack bridge stand-in stays seated; the browser arrives as the
        # real web sink wrapper (GapReplayGate is what app/api/chat.py seats).
        web = GapReplayGate(FakeWS())
        live = _attach_fake_live_with_fake_handle(
            manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value, extra_sinks=(web,)
        )
        await _pump_one_approval_request(manager, live)

        assert _approval_decisions_written(live.handle) == []
        pending = list(live.pending_approvals.values())
        assert pending and pending[0]["attended"] is True

        # …and the approval actually goes through.
        await manager.deliver_approval_decision(s.id, "appr-1", "allow", sender_email="u@x")
        assert _approval_decisions_written(live.handle) == [
            {"type": "approval_decision", "request_id": "appr-1", "decision": "allow"}
        ]

    asyncio.run(_run())


def test_agent_api_one_shot_denies_approval_immediately(manager: ChatManager):
    """The agent-API one-shot path has a HeadlessSink by construction and no
    human who could ever attach a browser, so it keeps the fast deny rather
    than stalling the caller for the full approval timeout. The decision is
    `unattended`, which the runner turns into an actionable message."""

    async def _run():
        from app.chat.headless import HeadlessSink

        s = await manager.create_session(user_email="u@x", surface=Surface.API)
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", HeadlessSink(), surface=Surface.API.value)
        await _pump_one_approval_request(manager, live)

        assert _approval_decisions_written(live.handle) == [
            {"type": "approval_decision", "request_id": "appr-1", "decision": "unattended"}
        ]

    asyncio.run(_run())


def test_web_session_with_no_attached_browser_still_waits(manager: ChatManager):
    """A web session whose browser is closed mid-turn keeps the request
    pending — the user reconnects and answers the replayed card. Only
    Surface.API is treated as permanently clientless."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", None)
        live.sinks.clear()
        await _pump_one_approval_request(manager, live)

        assert _approval_decisions_written(live.handle) == []

    asyncio.run(_run())


def test_orphan_sweep_is_a_noop_for_providers_without_listing(manager: ChatManager):
    """E2B (and any provider whose sandboxes aren't host-local) has no
    list_sandboxes — the sweep must do nothing, not crash the reaper."""

    async def _run():
        assert await manager.reap_orphan_sandboxes() == 0

    asyncio.run(_run())


def test_pending_approvals_live_outside_the_turn_buffer(manager: ChatManager):
    """A pending approval is separate state, not a turn-buffer frame.

    Holding it in the buffer pinned the replay watermark to an old moment,
    so a reconnect skipped everything said after the card appeared; and a
    card retained across a crash respawn rendered with buttons whose
    request_id belonged to the dead runner.
    """
    from types import SimpleNamespace

    live = SimpleNamespace(turn_buffer=[], pending_approvals={})
    # the pump routes frames by type
    for frame in (
        {"type": "token", "text": "hi", "seq": 1},
        {"type": "approval_request", "request_id": "appr-1", "seq": 2},
    ):
        if frame["type"] == "approval_request":
            live.pending_approvals[frame["request_id"]] = frame
        else:
            live.turn_buffer.append(frame)

    # a new turn clears the buffer; the unanswered card is untouched
    live.turn_buffer.clear()
    assert live.pending_approvals == {"appr-1": {"type": "approval_request", "request_id": "appr-1", "seq": 2}}
    assert live.turn_buffer == [], "the card must not pin the buffer's seq watermark"

    # answering it drops it
    live.pending_approvals.pop("appr-1", None)
    assert live.pending_approvals == {}


def test_a_web_sink_that_died_unnoticed_does_not_suppress_the_slack_nudge(manager: ChatManager):
    """`attended` is stamped BEFORE the fan-out — the frame_seq contract wants
    one envelope for every sink — but the fan-out is also where a dead sink is
    discovered. A browser that dropped without a clean close (sleeping laptop,
    lost mobile network) is still in `live.sinks` at stamp time, so the request
    went out marked attended, the Slack bridge stayed quiet, and that same
    broadcast then pruned the sink: the command stalled for the full timeout
    with nothing said anywhere (Devin Review on #1157).
    """

    class DeadWeb:
        """Looks card-capable, but every send fails — the silent-death case."""

        supports_approvals = True

        async def send_json(self, frame):
            raise ConnectionError("socket went away without a close frame")

    class RecordingSlack:
        def __init__(self):
            self.nudges = []

        async def send_json(self, frame):
            pass

        async def _post_approval_request(self, data):
            self.nudges.append(data)

    async def _run():
        slack = RecordingSlack()
        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(
            manager, s.id, "u@x", DeadWeb(), surface=Surface.SLACK_DM.value, extra_sinks=(slack,)
        )
        await _pump_one_approval_request(manager, live)

        assert slack.nudges, "the dead web sink suppressed the nudge and nobody was told"
        assert slack.nudges[0]["attended"] is False
        pending = list(live.pending_approvals.values())
        assert pending and pending[0]["attended"] is False, "the stored card still claims someone is holding it"

    asyncio.run(_run())


def test_replaying_a_pending_card_re_derives_who_is_attending(manager: ChatManager):
    """The stamp records who was attached when the request was RAISED. A Slack
    bridge seated afterwards replays the card, and off a stale `False` it would
    post its nudge even though a browser has since attached and is holding the
    buttons (Devin Review on #1157)."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value)
        await _pump_one_approval_request(manager, live)
        pending = list(live.pending_approvals.values())
        assert pending and pending[0]["attended"] is False

        # A browser attaches: card-capable, so the replay must say attended.
        web = FakeWS()
        web.supports_approvals = True
        live.sinks.append(SinkEntry(participant_email="u@x", sink=web))

        late = FakeWS()
        await manager.add_sink(s.id, late, participant_email="u@x")
        replayed = [f for f in late.sent if f.get("type") == "approval_request"]
        assert replayed and replayed[0]["attended"] is True, "replay carried the stale stamp"

    asyncio.run(_run())


def test_the_kill_switch_is_settable_again(manager: ChatManager):
    """`docs/cloud-chat.md` advertises `AGNES_APPROVALS=off` as the operator
    kill-switch and the runner still honours it, but the sandbox environment is
    exactly the dict the manager builds — the host's is not merged in — so
    dropping the entry left the switch unsettable anywhere while the docs said
    otherwise (Devin Review on #1157)."""
    captured = {}

    class FakeHandle2(FakeHandle):
        pass

    async def fake_spawn(**kw):
        captured.update(kw)
        return FakeHandle2()

    manager._provider.spawn = fake_spawn

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess = manager._repo.get_session(s.id)
        await manager._spawn_runner(sess, Path("/tmp"))
        assert captured["env"]["AGNES_APPROVALS"] == "on"

        import dataclasses

        manager._config = dataclasses.replace(manager._config, approvals_enabled=False)
        s2 = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        sess2 = manager._repo.get_session(s2.id)
        await manager._spawn_runner(sess2, Path("/tmp"))
        assert captured["env"]["AGNES_APPROVALS"] == "off"

    asyncio.run(_run())


def test_a_browser_leaving_later_still_nudges_the_slack_thread(manager: ChatManager):
    """The post-broadcast correction only covers the pump iteration that raised
    the request. A browser that leaves LATER — while the card is still pending
    — would otherwise leave nobody holding it and nobody told: the same stall,
    in a wider window (Devin Review on #1157)."""

    class RecordingSlack:
        def __init__(self):
            self.nudges = []

        async def send_json(self, frame):
            pass

        async def _post_approval_request(self, data):
            self.nudges.append(data)

    async def _run():
        from tests.chat_fakes import FakeWS

        web = FakeWS()
        web.supports_approvals = True
        slack = RecordingSlack()
        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(
            manager, s.id, "u@x", web, surface=Surface.SLACK_DM.value, extra_sinks=(slack,)
        )
        await _pump_one_approval_request(manager, live)
        assert slack.nudges == [], "a browser was holding the card — no nudge expected yet"

        # The browser goes away while the card is still pending.
        await manager.detach_sink(s.id, web)

        assert slack.nudges, "nobody can answer any more and nobody was told"
        assert slack.nudges[0]["attended"] is False
        # Idempotent: a second detach must not re-post.
        await manager.detach_sink(s.id, web)
        assert len(slack.nudges) == 1

    asyncio.run(_run())


def test_a_rebuilt_slack_bridge_learns_about_a_pending_card(manager: ChatManager):
    """`_ensure_slack_sink` appends straight into `live.sinks` instead of going
    through `_seat_sink`/`add_sink`, so it missed the pending-approval replay:
    a bridge rebuilt on the cross-gateway forwarded-message path stayed silent
    about a card already waiting, and the unattended re-check skipped it too
    because the stored frame's `attended` was already latched False
    (Devin Review on #1157)."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value)
        await _pump_one_approval_request(manager, live)
        assert live.pending_approvals, "a card should be waiting"

        seen = []

        class Bridge:
            """A real class, not a lambda: `_ensure_slack_sink` idempotence
            uses isinstance against this symbol."""

            def __init__(self, **kw):
                pass

            async def send_json(self, frame):
                seen.append(frame)

        import services.slack_bot.sink as sink_mod

        original = sink_mod.SlackSinkBridge
        sink_mod.SlackSinkBridge = Bridge
        try:
            await manager._ensure_slack_sink(live, {"channel": "C1", "thread_ts": None})
        finally:
            sink_mod.SlackSinkBridge = original

        cards = [f for f in seen if f.get("type") == "approval_request"]
        assert cards, "the rebuilt bridge never heard about the pending card"

    asyncio.run(_run())


def test_the_rebuilt_slack_bridge_uses_the_documented_public_url(manager: ChatManager, monkeypatch):
    """`_ensure_slack_sink` read SERVER_URL directly while the ordinary Slack
    DM/mention path takes web_base from `get_public_url()` (PUBLIC_URL env >
    server.public_url). A deployment configuring only the yaml key got a
    Continue-on-web button everywhere EXCEPT this path — and the no-link nudge
    then named the wrong knob to fix (Devin Review on #1157)."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value)

        seen = {}

        class Bridge:
            def __init__(self, **kw):
                seen.update(kw)

            async def send_json(self, frame):
                pass

        import services.slack_bot.sink as sink_mod

        original = sink_mod.SlackSinkBridge
        sink_mod.SlackSinkBridge = Bridge
        monkeypatch.delenv("SERVER_URL", raising=False)
        monkeypatch.setenv("PUBLIC_URL", "https://agnes.example.com")
        try:
            await manager._ensure_slack_sink(live, {"channel": "C1", "thread_ts": None})
        finally:
            sink_mod.SlackSinkBridge = original

        assert seen.get("web_base") == "https://agnes.example.com", (
            "the rebuilt bridge ignored the documented public-URL resolution"
        )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Question-answer routing (AskUserQuestion round-trip — mirrors the
# approval-decision routing suite above)
# ---------------------------------------------------------------------------


def _question_answers_written(handle) -> list[dict]:
    """Every question_answer frame the manager wrote to the runner's stdin."""
    out = []
    for line in b"".join(handle._stdin_buf).decode().splitlines():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get("type") == "question_answer":
            out.append(frame)
    return out


async def _pump_one_question_request(mgr: ChatManager, live, *, request_id: str = "ques-1"):
    """Run the pump over a single question_request frame and stop."""
    pump = asyncio.create_task(mgr._pump_subprocess_to_ws(live))
    live.handle.emit(
        {
            "type": "question_request",
            "request_id": request_id,
            "questions": [
                {
                    "question": "Which color?",
                    "header": "Color",
                    "options": [{"label": "Red"}, {"label": "Blue"}],
                    "multiSelect": False,
                }
            ],
            "timeout_seconds": 300,
        }
    )
    await _wait_until(lambda: bool(live.pending_questions))
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass


def test_question_answer_local_delivery(manager: ChatManager):
    """With a live local runner, the answer is written to its stdin — no
    cross-gateway publish, and the manager-only `unattended` resolution can
    never ride a client answer."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        handle = FakeHandle()
        live = MagicMock()
        live.handle = handle
        live._stdin_lock = asyncio.Lock()
        manager._live[s.id] = live
        with patch("app.chat.inbound.publish_control", new=AsyncMock()) as pub:
            await manager.deliver_question_answer(s.id, "ques-1", answers={"Which color?": "Red"}, sender_email="u@x")
        pub.assert_not_called()
        written = _question_answers_written(handle)
        assert written == [{"type": "question_answer", "request_id": "ques-1", "answers": {"Which color?": "Red"}}]

    asyncio.run(_run())


def test_question_answer_hardens_junk_to_dismissed(manager: ChatManager):
    """A payload with no usable str→str entries degrades to a dismissal
    rather than reaching the sandbox as-is."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        handle = FakeHandle()
        live = MagicMock()
        live.handle = handle
        live._stdin_lock = asyncio.Lock()
        manager._live[s.id] = live
        await manager.deliver_question_answer(s.id, "ques-2", answers={"k": 42, 3: "v", "s": "   "}, sender_email="u@x")
        written = _question_answers_written(handle)
        assert written == [{"type": "question_answer", "request_id": "ques-2", "dismissed": True}]

    asyncio.run(_run())


def test_question_answer_forwarded_to_remote_owner(manager: ChatManager):
    """No local runner but another gateway owns the session → ride the inbound
    control stream (command='question')."""

    async def _run():
        with (
            patch("app.chat.routing.owner_of", return_value="gw-other"),
            patch("app.chat.routing.this_gateway_id", return_value="gw-me"),
            patch("app.chat.inbound.publish_control", new=AsyncMock()) as pub,
        ):
            await manager.deliver_question_answer("chat_remote", "ques-3", answers={"q": "a"}, sender_email="u@x")
        pub.assert_awaited_once()
        assert pub.await_args.args[1] == "question"
        extra = pub.await_args.kwargs["extra"]
        assert extra["request_id"] == "ques-3"
        assert extra["answers"] == {"q": "a"}
        assert extra["dismissed"] is False

    asyncio.run(_run())


def test_question_answer_publish_failure_does_not_escape(manager: ChatManager):
    """Same posture as approvals: a coordination hiccup on the WS reader path
    must cost the answer, not the caller's chat window."""

    async def _run():
        from app.chat import inbound

        with (
            patch("app.chat.routing.owner_of", return_value="gw-other"),
            patch("app.chat.routing.this_gateway_id", return_value="gw-me"),
            patch(
                "app.chat.inbound.publish_control",
                new=AsyncMock(side_effect=inbound.InboundPublishFailed("down")),
            ),
        ):
            await manager.deliver_question_answer("chat_remote", "ques-4", answers={"q": "a"}, sender_email="u@x")

    asyncio.run(_run())


def test_slack_origin_question_waits_for_a_client(manager: ChatManager):
    """A Slack-origin session has no card-capable sink, but the user is one
    "Continue on web" click away — the question stays pending (the gate's
    own timeout is the backstop) and rides ``pending_questions`` so a
    browser attaching later replays the card."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(
            user_email="u@x", surface=Surface.SLACK_DM, slack_channel_id="C1", slack_thread_ts=None
        )
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value)
        await _pump_one_question_request(manager, live)

        assert _question_answers_written(live.handle) == [], "a Slack session must not be auto-resolved"
        pending = list(live.pending_questions.values())
        assert pending and pending[0]["attended"] is False

    asyncio.run(_run())


def test_agent_api_one_shot_resolves_question_unattended(manager: ChatManager):
    """The agent-API one-shot path has a HeadlessSink by construction and no
    human who could ever attach a browser — the question resolves immediately
    as `unattended`, which the runner turns into an actionable deny."""

    async def _run():
        from app.chat.headless import HeadlessSink

        s = await manager.create_session(user_email="u@x", surface=Surface.API)
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", HeadlessSink(), surface=Surface.API.value)
        await _pump_one_question_request(manager, live)

        assert _question_answers_written(live.handle) == [
            {"type": "question_answer", "request_id": "ques-1", "unattended": True}
        ]

    asyncio.run(_run())


def test_install_runner_retires_pending_questions(manager: ChatManager):
    """A handle swap broadcasts a question_resolved for every pending card —
    a request_id belongs to the process that raised it, and a fresh gate
    drops unknown ids silently (same rule as pending approvals)."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", ws)
        live.pending_questions["ques-9"] = {"type": "question_request", "request_id": "ques-9"}
        await manager._install_runner(live, FakeHandle())
        assert live.pending_questions == {}
        resolved = [f for f in ws.sent if f.get("type") == "question_resolved"]
        assert resolved and resolved[0]["request_id"] == "ques-9"
        assert resolved[0]["decision"] == "cancelled"

    asyncio.run(_run())


def test_new_sink_gets_pending_question_replayed(manager: ChatManager):
    """A browser attaching mid-question receives the pending card (with
    re-derived attendance), or the AskUserQuestion call stalls invisibly."""

    async def _run():
        from tests.chat_fakes import FakeWS

        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_fake_live_with_fake_handle(manager, s.id, "u@x", FakeWS())
        live.pending_questions["ques-7"] = {
            "type": "question_request",
            "request_id": "ques-7",
            "questions": [],
            "attended": False,
        }
        late = FakeWS()
        await manager._replay_pending_approvals_to(live, late)
        replayed = [f for f in late.sent if f.get("type") == "question_request"]
        assert replayed and replayed[0]["request_id"] == "ques-7"

    asyncio.run(_run())
