"""Session restore, part 2 (issue #1973).

#1914/#1924 put ``?session=<id>`` in the address bar; what was still broken
was the rest of the restore lifecycle. Four behaviors, each covered here:

1. A refresh during a running response could lose the reply for good — a turn
   interrupted before its first TEXT token persisted nothing at all, so the
   session dead-ended holding only the user's question
   (``ChatManager._partial_save``), and the idle reaper would pause a
   sink-less session mid-answer, discarding the turn the detach path has
   always waited for.
2. After a mid-stream refresh the UI looked dead for seconds — nothing told
   the client a turn was still running (``is_turn_in_flight``, the ticket
   response's ``turn_in_flight``, the ``ready`` frame's own verdict).
3. A deep link to an existing session could silently fall back to a new chat
   — the hero stayed up and the URL param was stripped before anything had
   failed (client-side; static-source guards below).
4. A restored session opened at the top of the conversation.

The client half is pinned as static-source guards against
app/web/static/js/chat.js, the same contract style as
test_chat_session_url_sync.py — there is no headless browser in CI.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests
from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle

CHAT_JS = Path("app/web/static/js/chat.js")


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _make_manager(
    tmp_path: Path,
    *,
    idle_ttl_seconds: int = 30 * 60,
    on_detach: str = "pause",
) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir(exist_ok=True)
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
    provider.spawn = AsyncMock()
    provider.pause = AsyncMock()
    provider.keepalive = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        # Pin the native provider: this file asserts on chat_<hex> ids, which
        # `engine_session_id` replaces with UUIDs under the default provider.
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=5,
            provider="docker",
            idle_ttl_seconds=idle_ttl_seconds,
            on_detach=on_detach,
        ),
    )


def _seat_live(
    mgr: ChatManager,
    chat_id: str,
    *,
    turn_in_flight: bool = False,
    turn_buffer: list | None = None,
    sinks: list | None = None,
    last_activity: datetime | None = None,
) -> LiveSession:
    live = LiveSession(
        chat_id=chat_id,
        user_email="u@x",
        state=SessionState.ACTIVE,
        handle=cast(Any, FakeHandle()),
        started_at=datetime.now(timezone.utc),
        last_activity=last_activity or datetime.now(timezone.utc),
        sinks=sinks if sinks is not None else [],
    )
    live.turn_in_flight = turn_in_flight
    if turn_buffer:
        live.turn_buffer.extend(turn_buffer)
    mgr._live[chat_id] = live
    return live


# ---------------------------------------------------------------------------
# (1) the in-flight reply is never silently dropped
# ---------------------------------------------------------------------------


class TestPartialSaveNeverDeadEnds:
    def test_turn_interrupted_before_any_token_still_persists_a_row(self, tmp_path):
        """THE reported stuck session: two user messages, no assistant row.

        The old guard was ``if partial:`` over TEXT tokens only, so an answer
        killed while its first tool call was still running left the session
        with the question and nothing else — unrecoverable by any reload.
        """
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            mgr._repo.append_message(session_id=s.id, role="user", content="list every dataset")
            _seat_live(
                mgr,
                s.id,
                turn_in_flight=True,
                turn_buffer=[{"type": "tool_call", "tool": "Bash", "args": {"command": "ls"}, "seq": 1}],
            )
            await mgr.kill(s.id, reason="ws_disconnect")
            msgs = mgr._repo.list_messages(s.id)
            assert [m.role for m in msgs] == ["user", "assistant"]
            saved = msgs[-1]
            assert any(tc.get("interrupted") is True for tc in (saved.tool_calls or []))
            assert saved.tool_calls[0]["reason"] == "ws_disconnect"

        asyncio.run(_run())

    def test_turn_in_flight_with_an_empty_buffer_still_persists_a_row(self, tmp_path):
        """Killed between delivery and the first frame — the buffer is empty,
        so the old ``if live.turn_buffer:`` gate skipped the save entirely."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            _seat_live(mgr, s.id, turn_in_flight=True)
            await mgr.kill(s.id, reason="pause_failed")
            msgs = mgr._repo.list_messages(s.id)
            assert [m.role for m in msgs] == ["assistant"]
            assert msgs[0].content == ""

        asyncio.run(_run())

    def test_accumulated_text_is_still_saved(self, tmp_path):
        """Regression on the behavior that already worked."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            _seat_live(
                mgr,
                s.id,
                turn_in_flight=True,
                turn_buffer=[
                    {"type": "token", "text": "Half an ", "seq": 1},
                    {"type": "token", "text": "answer", "seq": 2},
                ],
            )
            await mgr.kill(s.id, reason="idle_ttl")
            msgs = mgr._repo.list_messages(s.id)
            assert msgs[-1].content == "Half an answer"

        asyncio.run(_run())

    def test_no_turn_in_flight_saves_nothing(self, tmp_path):
        """A killed IDLE session must not grow a phantom assistant row."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            _seat_live(mgr, s.id, turn_in_flight=False)
            await mgr.kill(s.id, reason="idle_ttl")
            assert mgr._repo.list_messages(s.id) == []

        asyncio.run(_run())


class TestReaperRespectsAnInFlightTurn:
    def test_idle_sweep_does_not_pause_a_session_mid_turn(self, tmp_path):
        """``_pause_live`` cancels the pump, so pausing mid-answer discards the
        reply. The detach path (``_linger_then_pause``) has always waited for
        the turn to finish; the reaper must agree."""
        mgr = _make_manager(tmp_path, idle_ttl_seconds=1, on_detach="pause")

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            stale = datetime.now(timezone.utc) - timedelta(seconds=600)
            _seat_live(mgr, s.id, turn_in_flight=True, last_activity=stale)
            await mgr._reap_once()
            assert mgr._live[s.id].state == SessionState.ACTIVE
            mgr._provider.pause.assert_not_awaited()

        asyncio.run(_run())

    def test_idle_sweep_still_pauses_an_idle_session(self, tmp_path):
        mgr = _make_manager(tmp_path, idle_ttl_seconds=1, on_detach="pause")

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            stale = datetime.now(timezone.utc) - timedelta(seconds=600)
            _seat_live(mgr, s.id, turn_in_flight=False, last_activity=stale)
            await mgr._reap_once()
            assert mgr._live[s.id].state == SessionState.PAUSED

        asyncio.run(_run())

    def test_keepalive_covers_a_sinkless_in_flight_turn(self, tmp_path):
        """The exact shape a mid-answer browser reload creates: no sink, but an
        answer still being written that must outlive the sandbox timeout."""
        mgr = _make_manager(tmp_path, idle_ttl_seconds=1800)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            _seat_live(mgr, s.id, turn_in_flight=True, sinks=[])
            await mgr._reap_once()
            mgr._provider.keepalive.assert_awaited()

        asyncio.run(_run())

    def test_an_in_flight_turn_past_the_idle_ttl_still_gets_a_keepalive(self, tmp_path):
        """The sweep refuses to pause it — and must not then skip the sweep
        entirely, because that session is precisely the one whose sandbox has
        to be kept alive."""
        mgr = _make_manager(tmp_path, idle_ttl_seconds=1, on_detach="pause")

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            stale = datetime.now(timezone.utc) - timedelta(seconds=600)
            _seat_live(mgr, s.id, turn_in_flight=True, last_activity=stale)
            await mgr._reap_once()
            assert mgr._live[s.id].state == SessionState.ACTIVE
            mgr._provider.keepalive.assert_awaited()

        asyncio.run(_run())

    def test_keepalive_still_skipped_for_an_idle_sinkless_session(self, tmp_path):
        mgr = _make_manager(tmp_path, idle_ttl_seconds=1800)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            _seat_live(mgr, s.id, turn_in_flight=False, sinks=[])
            await mgr._reap_once()
            mgr._provider.keepalive.assert_not_awaited()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# (1b) one submit can never persist two questions
# ---------------------------------------------------------------------------


class TestUserMessageIdempotency:
    def test_repeated_client_msg_id_persists_one_row(self, tmp_path):
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            ws = MagicMock()
            ws.send_json = AsyncMock()
            _seat_live(mgr, s.id, sinks=[SinkEntry(participant_email="u@x", sink=ws)])
            await mgr.send_user_message(s.id, "same question", client_msg_id="submit-1")
            await mgr.send_user_message(s.id, "same question", client_msg_id="submit-1")
            msgs = [m for m in mgr._repo.list_messages(s.id) if m.role == "user"]
            assert len(msgs) == 1

        asyncio.run(_run())

    def test_a_genuine_re_ask_gets_its_own_turn(self, tmp_path):
        """Same text, new id — a person asking twice is two turns, not a dupe."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            ws = MagicMock()
            ws.send_json = AsyncMock()
            _seat_live(mgr, s.id, sinks=[SinkEntry(participant_email="u@x", sink=ws)])
            await mgr.send_user_message(s.id, "same question", client_msg_id="submit-1")
            await mgr.send_user_message(s.id, "same question", client_msg_id="submit-2")
            msgs = [m for m in mgr._repo.list_messages(s.id) if m.role == "user"]
            assert len(msgs) == 2

        asyncio.run(_run())

    def test_no_id_keeps_todays_exactly_as_delivered_behavior(self, tmp_path):
        """Slack, headless and the agent runtime pass no id."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            ws = MagicMock()
            ws.send_json = AsyncMock()
            _seat_live(mgr, s.id, sinks=[SinkEntry(participant_email="u@x", sink=ws)])
            await mgr.send_user_message(s.id, "hello")
            await mgr.send_user_message(s.id, "hello")
            msgs = [m for m in mgr._repo.list_messages(s.id) if m.role == "user"]
            assert len(msgs) == 2

        asyncio.run(_run())

    def test_a_send_that_raised_before_persisting_does_not_burn_the_id(self, tmp_path):
        """The WS route retries ``send_user_message`` for up to 30 s while a
        sandbox boots. That retry must still work: an id is recorded only once
        the ``chat_messages`` row exists."""
        from app.chat.manager import SessionNotFound

        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            # No live session and no sandbox refs on the row → SessionNotFound,
            # raised before the persist.
            with pytest.raises(SessionNotFound):
                await mgr.send_user_message(s.id, "q", client_msg_id="submit-1")
            assert mgr._repo.list_messages(s.id) == []
            ws = MagicMock()
            ws.send_json = AsyncMock()
            _seat_live(mgr, s.id, sinks=[SinkEntry(participant_email="u@x", sink=ws)])
            await mgr.send_user_message(s.id, "q", client_msg_id="submit-1")
            assert len([m for m in mgr._repo.list_messages(s.id) if m.role == "user"]) == 1

        asyncio.run(_run())

    def test_the_claim_itself_admits_exactly_one_of_two_racing_callers(self):
        """The primitive the guard rests on: ``lease_acquire`` is set-if-absent,
        so of N concurrent claims on one key exactly one returns True."""
        from app.chat.manager import claim_user_message

        async def _run():
            results = await asyncio.gather(
                *[claim_user_message("chat_x", "submit-1") for _ in range(5)]
            )
            assert results.count(True) == 1

        asyncio.run(_run())

    def test_two_interleaved_sends_of_one_submit_persist_one_row(self, tmp_path, monkeypatch):
        """Review finding on the first push: the process-local set was a
        check-then-act — two coroutines could both pass it before either
        recorded the id, and there IS a suspension point between the two (the
        sender-limit gate). Forced here, because whether that gate actually
        yields is an implementation detail this guard must not depend on.

        Verified to FAIL (two rows) with the atomic claim stubbed out, so it
        pins the fix rather than the fast path in front of it."""
        mgr = _make_manager(tmp_path)
        real_limits = mgr._enforce_sender_limits

        async def _yielding_limits(*args, **kwargs):
            await asyncio.sleep(0)          # hand control to the other send
            return await real_limits(*args, **kwargs)

        monkeypatch.setattr(mgr, "_enforce_sender_limits", _yielding_limits)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            ws = MagicMock()
            ws.send_json = AsyncMock()
            _seat_live(mgr, s.id, sinks=[SinkEntry(participant_email="u@x", sink=ws)])
            await asyncio.gather(
                mgr.send_user_message(s.id, "same question", client_msg_id="submit-1"),
                mgr.send_user_message(s.id, "same question", client_msg_id="submit-1"),
            )
            msgs = [m for m in mgr._repo.list_messages(s.id) if m.role == "user"]
            assert len(msgs) == 1

        asyncio.run(_run())

    def test_a_claim_is_released_when_the_persist_itself_fails(self, tmp_path):
        """A burned claim over a row that was never written would turn the
        client's retry into a silently dropped question."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            ws = MagicMock()
            ws.send_json = AsyncMock()
            _seat_live(mgr, s.id, sinks=[SinkEntry(participant_email="u@x", sink=ws)])
            real_append = mgr._repo.append_message
            calls = {"n": 0}

            def _flaky(**kwargs):
                calls["n"] += 1
                if calls["n"] == 1 and kwargs.get("role") == "user":
                    raise RuntimeError("db blip")
                return real_append(**kwargs)

            mgr._repo.append_message = _flaky  # type: ignore[method-assign]
            with pytest.raises(RuntimeError, match="db blip"):
                await mgr.send_user_message(s.id, "q", client_msg_id="submit-1")
            assert [m for m in mgr._repo.list_messages(s.id) if m.role == "user"] == []
            # The retry of the SAME submit must land.
            await mgr.send_user_message(s.id, "q", client_msg_id="submit-1")
            assert len([m for m in mgr._repo.list_messages(s.id) if m.role == "user"]) == 1

        asyncio.run(_run())

    def test_the_claim_fails_open_when_coordination_is_unavailable(self, tmp_path, monkeypatch):
        """A guard against duplicates must never become a reason a real
        question is dropped."""
        import app.chat.manager as mgr_mod

        def _boom(*a, **k):
            raise RuntimeError("coordination down")

        monkeypatch.setattr(mgr_mod, "coordination", _boom)

        async def _run():
            assert await mgr_mod.claim_user_message("chat_x", "submit-1") is True

        asyncio.run(_run())

    def test_no_id_never_claims_anything(self, tmp_path, monkeypatch):
        """Slack, headless and the agent runtime pass no id — they must not
        start writing claims into the coordination backend."""
        import app.chat.manager as mgr_mod

        calls: list[str] = []

        def _tracked():
            calls.append("coordination")
            raise AssertionError("claim_user_message touched the backend for a send with no id")

        monkeypatch.setattr(mgr_mod, "coordination", _tracked)

        async def _run():
            assert await mgr_mod.claim_user_message("chat_x", None) is True
            await mgr_mod.release_user_message_claim("chat_x", None)

        asyncio.run(_run())
        assert calls == []

    def test_the_thin_producer_claims_through_the_same_helper(self, tmp_path):
        """The forwarded path is where a role-split deployment persists, so a
        guard that lived only in the manager's process would not cover it."""
        from app.chat.manager import produce_inbound_user_message

        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            for _ in range(2):
                await produce_inbound_user_message(
                    mgr._repo, mgr._config, s.id, "forwarded", client_msg_id="submit-1"
                )
            msgs = [m for m in mgr._repo.list_messages(s.id) if m.role == "user"]
            assert len(msgs) == 1

        asyncio.run(_run())

    def test_the_accepted_id_map_is_bounded(self, tmp_path):
        from app.chat.manager import _ACCEPTED_MSG_IDS_MAX_ENTRIES

        mgr = _make_manager(tmp_path)
        for i in range(_ACCEPTED_MSG_IDS_MAX_ENTRIES + 50):
            mgr._note_accepted("chat_x", f"id-{i}")
        assert len(mgr._accepted_msg_ids) == _ACCEPTED_MSG_IDS_MAX_ENTRIES
        # Oldest evicted, newest kept.
        assert not mgr._already_accepted("chat_x", "id-0")
        assert mgr._already_accepted("chat_x", f"id-{_ACCEPTED_MSG_IDS_MAX_ENTRIES + 49}")


# ---------------------------------------------------------------------------
# (2) the client can find out a turn is running
# ---------------------------------------------------------------------------


class TestTurnInFlightIsObservable:
    def test_accessor_reflects_the_live_flag(self, tmp_path):
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            assert mgr.is_turn_in_flight(s.id) is False  # not even live
            live = _seat_live(mgr, s.id, turn_in_flight=True)
            assert mgr.is_turn_in_flight(s.id) is True
            live.turn_in_flight = False
            assert mgr.is_turn_in_flight(s.id) is False

        asyncio.run(_run())

    def test_unknown_session_is_false_not_an_error(self, tmp_path):
        mgr = _make_manager(tmp_path)
        assert mgr.is_turn_in_flight("chat_nope") is False

    def test_ready_frame_carries_the_verdict(self, tmp_path):
        """The client's pre-socket guess (the ticket) is corrected here — in
        both directions."""
        mgr = _make_manager(tmp_path)

        async def _run():
            s = await mgr.create_session(user_email="u@x", surface=Surface.WEB)
            live = _seat_live(mgr, s.id, turn_in_flight=True)
            ws = MagicMock()
            ws.send_json = AsyncMock()
            await mgr._seat_sink(live, ws, is_primary=True)
            ready = [c.args[0] for c in ws.send_json.call_args_list if c.args[0].get("type") == "ready"]
            assert ready and ready[0]["turn_in_flight"] is True

        asyncio.run(_run())


class TestTicketReportsTurnInFlight:
    """``POST /sessions/{id}/ticket`` is the last call before the socket, so it
    is where the client learns to paint the working state."""

    def _client(self):
        from fastapi.testclient import TestClient

        from tests.test_chat_api import _make_app

        return TestClient(_make_app(chat_enabled=True))

    def test_flag_is_false_for_an_idle_session(self):
        client = self._client()
        chat_id = client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
        body = client.post(f"/api/chat/sessions/{chat_id}/ticket").json()
        assert body["turn_in_flight"] is False

    def test_flag_is_true_while_a_turn_runs(self):
        client = self._client()
        chat_id = client.post("/api/chat/sessions", json={"surface": "web"}).json()["id"]
        mgr = client.app.state.chat_manager
        live = MagicMock()
        live.turn_in_flight = True
        mgr._live[chat_id] = live
        body = client.post(f"/api/chat/sessions/{chat_id}/ticket").json()
        assert body["turn_in_flight"] is True

    def test_ticket_still_mints_without_a_chat_manager(self):
        """An api-role replica hosts no sessions. It must still mint tickets —
        it just reports False, which is the pre-#1973 client behavior."""
        from fastapi.testclient import TestClient

        from tests.test_chat_api import _make_app

        app = _make_app(chat_enabled=True)
        repo = app.state.chat_repo
        session = repo.create_session(user_email="alice@test.com", surface=Surface.WEB)
        del app.state.chat_manager
        client = TestClient(app)
        r = client.post(f"/api/chat/sessions/{session.id}/ticket")
        assert r.status_code == 201
        assert r.json()["turn_in_flight"] is False


class TestWsRouteThreadsTheIdempotencyKey:
    def test_ws_stream_reads_client_msg_id_and_caps_it(self):
        src = Path("app/api/chat.py").read_text(encoding="utf-8")
        assert 'raw_cmid = frame.get("client_msg_id")' in src
        assert "raw_cmid[:128]" in src
        assert src.count("client_msg_id=client_msg_id") == 2, "owner + co-drive sockets both"


# ---------------------------------------------------------------------------
# (3) + (4) client restore contract — static-source guards
# ---------------------------------------------------------------------------


def _chat_js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def _slice(js: str, start_marker: str, end_marker: str) -> str:
    """`end_marker` is whatever declaration follows the one under test, so it
    moves whenever that neighbour does. The markers below were
    `chatErrorCopy` until the copy helpers moved to `chat_errors.js` (one home
    for the sentences, shared with /_debug/error-surfaces); `handleFrame` is
    openSession's neighbour now. A move fails here with a bare ValueError —
    the fix is to re-point the marker, not to change what is asserted."""
    start = js.index(start_marker)
    return js[start : js.index(end_marker, start)]


class TestDeepLinkRestoreIsNotSilent:
    def test_restore_starts_before_the_sidebar_fetch(self):
        """It used to wait for loadSidebar() and then a rAF — seconds of hero
        for a conversation that already exists."""
        boot = _chat_js().rsplit("(async () => {\n  renderCapabilities();", 1)[1]
        assert boot.index("_restoreInitialSessionEarly();") < boot.index("await loadSidebar();")

    def test_the_hero_comes_down_synchronously_for_a_deep_link(self):
        body = _slice(
            _chat_js(),
            "function _restoreInitialSessionEarly() {",
            "function _maybeOpenInitialSession() {",
        )
        # No await before the hide — a deep link must never paint the
        # pre-conversation dashboard, not even for one frame.
        assert body.index("hideCapabilities();") < body.index("openSession(")
        assert "await" not in body.split("hideCapabilities();")[0]
        assert "{ restoring: true }" in body

    def test_the_two_open_paths_cannot_both_fire(self):
        body = _slice(
            _chat_js(),
            "function _maybeOpenInitialSession() {",
            "function _resyncOpenSessionMeta() {",
        )
        assert "if (_initialRestorePromise) {" in body
        assert "_initialSessionId = null;" in body

    def test_a_restore_keeps_the_session_param(self):
        body = _slice(
            _chat_js(),
            "async function openSession(chatId, wsUrlOverride, { restoring = false, reconnecting = false, turnInFlight: turnInFlightHint = null } = {}) {",
            "function handleFrame(frame) {",
        )
        assert "if (!restoring) _syncSessionUrl(_sessionHasTurns ? chatId : null);" in body

    def test_a_failed_restore_renders_an_error_instead_of_a_new_chat(self):
        body = _slice(
            _chat_js(),
            "async function openSession(chatId, wsUrlOverride, { restoring = false, reconnecting = false, turnInFlight: turnInFlightHint = null } = {}) {",
            "function handleFrame(frame) {",
        )
        assert "if (restoring && !hydrated.ok) {" in body
        # A history failure means the conversation could not be READ — that,
        # and only that, gets the destructive invalid-session state.
        assert body.count("_renderRestoreFailure(") == 1

    def test_a_ticket_failure_keeps_the_transcript_it_just_proved_good(self):
        """Review finding on the first push: history had already loaded, so a
        failed WS ticket is usually a blip — erasing the transcript, the id and
        the URL, and saying the chat may belong to someone else, was wrong on
        every count."""
        body = _slice(
            _chat_js(),
            "async function openSession(chatId, wsUrlOverride, { restoring = false, reconnecting = false, turnInFlight: turnInFlightHint = null } = {}) {",
            "function handleFrame(frame) {",
        )
        ticket_catch = body[body.index("const t = await api(`/api/chat/sessions/${chatId}/ticket`") :]
        assert "_renderResumeFailure(err.message);" in ticket_catch
        # The destructive renderer is not CALLED here (the prose above the
        # branch names it to explain why it must not be).
        assert "_renderRestoreFailure(" not in ticket_catch

    def test_the_resume_failure_renderer_destroys_nothing(self):
        body = _slice(
            _chat_js(),
            "function _renderResumeFailure(detail) {",
            "/** Open (or resume) a chat session.",
        )
        assert "currentChatId = null" not in body
        assert "_syncSessionUrl(null)" not in body
        assert 'innerHTML = ""' not in body
        assert "showCapabilities()" not in body
        assert "renderSystemNote(" in body       # and it says how to retry

    def test_the_failure_renderer_leaves_a_clean_pre_conversation_state(self):
        body = _slice(
            _chat_js(),
            "function _renderRestoreFailure(detail) {",
            "/** Open (or resume) a chat session.",
        )
        assert "currentChatId = null;" in body
        assert "_syncSessionUrl(null);" in body     # a dead id must not survive a reload
        assert "renderSystemNote(" in body          # said IN the transcript, not only in a status pill
        assert "showToast(" in body                 # the report was explicit: "no error, no toast"
        assert "showCapabilities();" in body

    def test_history_load_reports_its_outcome(self):
        body = _slice(
            _chat_js(),
            "async function loadAndRenderHistory(chatId) {",
            "/** A deep-link restore that could not be completed",
        )
        assert "return { ok: false, error: err.message, count: 0, authHandled };" in body
        assert "return { ok: true, error: null, count: history.length };" in body

    def test_history_load_reports_an_auth_failure_it_already_answered(self):
        """`handleExpiredSession` writes the only sentence a signed-out
        reader gets before the redirect fires, and a 403 is a missing grant
        rather than the "deleted, archived, or someone else's" that
        `_renderRestoreFailure` claims. Both are lost if the restoring
        caller paints over them, so the outcome carries whether the auth
        case was already answered — and the caller honours it.
        """
        body = _slice(
            _chat_js(),
            "async function loadAndRenderHistory(chatId) {",
            "/** A deep-link restore that could not be completed",
        )
        assert "const authHandled = handleExpiredSession(err);" in body

        opener = _slice(
            _chat_js(),
            "  const hydrated = await loadAndRenderHistory(chatId);",
            "  // A recovery nobody asked for must not cost the reader their transcript.",
        )
        # Still the panel for an ORDINARY restore failure (404, a 5xx) —
        # the case it was written for — and only that.
        assert "if (!hydrated.authHandled) _renderRestoreFailure(hydrated.error);" in opener


class TestConcurrentOpensCannotClobberEachOther:
    """Review finding on the first push: `openSession` sets `currentChatId`
    before its awaits and never re-checked afterwards, so a slow open could
    paint into a conversation the user had since switched away from and
    overwrite the global `ws` (leaving two sockets on one frame handler).
    Pre-existing, but the early deep-link restore makes a click landing
    mid-open an ordinary race rather than a rare one."""

    def test_every_await_in_open_session_is_followed_by_a_generation_check(self):
        body = _slice(
            _chat_js(),
            "async function openSession(chatId, wsUrlOverride, { restoring = false, reconnecting = false, turnInFlight: turnInFlightHint = null } = {}) {",
            "function handleFrame(frame) {",
        )
        assert "const openGen = ++_openGeneration;" in body
        # One after the history hydrate, one after a successful ticket mint,
        # one in the ticket's catch, and one immediately before `ws` is claimed.
        assert body.count("openGen !== _openGeneration") == 4

    def test_the_socket_is_claimed_only_by_the_newest_open(self):
        body = _slice(
            _chat_js(),
            "async function openSession(chatId, wsUrlOverride, { restoring = false, reconnecting = false, turnInFlight: turnInFlightHint = null } = {}) {",
            "function handleFrame(frame) {",
        )
        ws_claim = body.index("ws = new WebSocket(")
        guard = body.rindex("openGen !== _openGeneration", 0, ws_claim)
        # Nothing awaits between the last guard and the assignment.
        assert "await" not in body[guard:ws_claim]

    def test_history_render_bails_when_it_has_been_superseded(self):
        body = _slice(
            _chat_js(),
            "async function loadAndRenderHistory(chatId) {",
            "/** Put the newest message in view.",
        )
        assert "const gen = _openGeneration;" in body
        assert body.count("gen !== _openGeneration") == 2, "both the success and the error path"
        # The check sits between the fetch and any rendering.
        fetch = body.index("history = await api(")
        first_render = body.index("renderMessage(m)")
        assert fetch < body.index("gen !== _openGeneration") < first_render


class TestRestoredSessionOpensAtTheLatestMessage:
    def test_full_render_scrolls_to_the_newest_message(self):
        body = _slice(
            _chat_js(),
            "async function loadAndRenderHistory(chatId) {",
            "/** A deep-link restore that could not be completed",
        )
        assert "if (history.length > 0) scrollToLatestMessage();" in body

    def test_the_scroll_helper_is_unconditional(self):
        """``maybeScrollToBottom`` protects a reading position; a fresh render
        has none, and the old behavior was to open at the very top."""
        body = _slice(
            _chat_js(),
            "function scrollToLatestMessage() {",
            "/** Open (or resume) a chat session.",
        )
        assert "el.scrollTop = el.scrollHeight;" in body
        assert "requestAnimationFrame(" in body  # again after layout settles


class TestReattachShowsThatSomethingIsRunning:
    def test_ticket_flag_paints_the_working_state_before_the_socket(self):
        body = _slice(
            _chat_js(),
            "async function openSession(chatId, wsUrlOverride, { restoring = false, reconnecting = false, turnInFlight: turnInFlightHint = null } = {}) {",
            "function handleFrame(frame) {",
        )
        assert "turnInFlight = !!(t && t.turn_in_flight);" in body
        # #2156 moved the paint behind one state writer, and made it
        # unconditional in both directions: attaching to a conversation whose
        # turn has finished must also take DOWN a Stop button left over from
        # the conversation being switched away from.
        paint = body[body.index("_reattachGuessedTurn = !!turnInFlight;") :]
        assert "setTurnInFlight(!!turnInFlight, { immediate: true });" in paint
        # Painted BEFORE the socket is opened — that wait is the dead window.
        assert body.index("_reattachGuessedTurn = !!turnInFlight;") < body.index("ws = new WebSocket(")

    def test_ready_frame_reconciles_a_stale_guess(self):
        js = _chat_js()
        body = _slice(js, '    case "ready":', '    case "token":')
        assert "if (_reattachGuessedTurn && frame.turn_in_flight === false) {" in body
        assert "setTurnInFlight(false);" in body
        # Reads the TURN state, not the placeholder: since #2156 the
        # placeholder comes and goes many times inside one live turn, so
        # `!thinkingEl` would re-arm a turn that is already running.
        assert "frame.turn_in_flight === true && !_turnInFlight" in body

    def test_only_a_reattachs_turn_is_taken_down_by_ready(self):
        """A submit's own turn must survive a ``ready`` that arrives before
        the server has even received the message."""
        js = _chat_js()
        submit = _slice(js, "async function submitUserMessage(text) {", "/** Resize the composer textarea")
        # The submit disclaims the flag right where it starts its own turn.
        start = submit.index("setTurnInFlight(true, { immediate: true });")
        assert start - submit.index("_reattachGuessedTurn = false;") < 200
        # The claim is dropped when the TURN stops, not when the placeholder
        # is removed. That reset used to live in `clearThinkingPlaceholder`,
        # and #2156 is what made it wrong: the placeholder now comes and goes
        # on every token and every tool call inside one live turn, so dropping
        # the claim on any of those would let a late `ready` frame call off a
        # turn a reattach legitimately owns.
        setter = _slice(js, "function setTurnInFlight(on, { immediate = false } = {}) {", "//: How long the transcript")
        assert "if (!_turnInFlight) _reattachGuessedTurn = false;" in setter
        clear = _slice(js, "function clearThinkingPlaceholder() {", "// Streaming state")
        assert "_reattachGuessedTurn" not in clear, (
            "removing the placeholder must not disclaim the turn (#2156)"
        )

    def test_a_stale_placeholder_cannot_survive_a_transcript_reload(self):
        """``innerHTML = ''`` detaches the node but used to leave the pointer
        set, after which no later placeholder could ever render."""
        body = _slice(
            _chat_js(),
            "async function loadAndRenderHistory(chatId) {",
            "/** A deep-link restore that could not be completed",
        )
        assert body.index("clearThinkingPlaceholder();") < body.index("let history = [];")


class TestSubmitCarriesAnIdempotencyKey:
    def test_one_id_per_submit_rides_the_user_msg_frame(self):
        js = _chat_js()
        assert 'ws.send(JSON.stringify({ type: "user_msg", text, client_msg_id: submitId }));' in js
        submit = _slice(js, "async function submitUserMessage(text) {", "/** Resize the composer textarea")
        assert "const submitId = _newSubmitId();" in submit

    def test_the_generator_degrades_without_crypto_random_uuid(self):
        body = _slice(_chat_js(), "function _newSubmitId() {", "async function submitUserMessage(text) {")
        assert "randomUUID" in body
        assert "Math.random()" in body


class TestInterruptedRowsAreLegible:
    def test_an_interrupted_partial_save_says_so_in_the_transcript(self):
        js = _chat_js()
        body = _slice(js, "function _isInterruptedRow(m) {", "function renderMessage(m) {")
        assert "tc.interrupted === true" in body
        render = _slice(js, "function renderMessage(m) {", "// ---------- Result table enhancement")
        assert "_isInterruptedRow(m)" in render
        assert "interrupted before it finished" in render
        # The case that used to persist nothing at all gets its own wording.
        assert "interrupted before Agnes wrote anything" in render
