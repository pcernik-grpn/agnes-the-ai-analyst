"""A chat turn is a trace (spec 3.2).

ChatManager mints one ``turn_id`` per delivered user message, opens an
``agnes.chat.turn`` span for it, nests one span per tool call under it,
stamps the id onto every frame the client receives and publishes the span
context so the broker can parent its completion spans under the same turn.

The load-bearing property, tested last and separately: none of that may
cost a turn. A tracer that raises leaves the message persisted and the
session ready for the next turn.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.turn_context import read_turn
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests
from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS, _wait_until
from tests.test_otel_export import otel_exporter  # noqa: F401


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture
def manager(tmp_path: Path) -> ChatManager:
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
    provider.spawn = AsyncMock()
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        # The native provider keeps `chat_<hex>` ids and the runner-frame
        # path this suite drives.
        config=ChatConfig(enabled=True, concurrency_per_user=2, provider="docker"),
    )


def _attach_live(mgr: ChatManager, chat_id: str, user_email: str, sink, *, surface: str = Surface.WEB.value):
    handle = FakeHandle()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(UTC),
        last_activity=datetime.now(UTC),
        surface=surface,
        sinks=[SinkEntry(participant_email=user_email, sink=sink)],
    )
    mgr._live[chat_id] = live
    return live


async def _pump(manager: ChatManager, live: LiveSession, frames: list[dict], *, until=None):
    pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
    for frame in frames:
        live.handle.emit(frame)
    await _wait_until(until or (lambda: not live.turn_in_flight))
    pump_task.cancel()
    try:
        await pump_task
    except asyncio.CancelledError:
        pass


_ASSISTANT = {
    "type": "assistant_message",
    "content": "done",
    "tokens_in": 10,
    "tokens_out": 5,
    "cache_read_tokens": 100,
    "cache_creation_tokens": 2,
    "model": "claude-sonnet-5",
}


def test_turn_span_wraps_tool_spans_and_stamps_every_frame(manager: ChatManager, otel_exporter):  # noqa: F811
    """One INTERNAL turn span, one nested tool span, ``turn_id`` on the
    frames the client sees, and the record published for the broker."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        ws = FakeWS()
        live = _attach_live(manager, s.id, "u@x", ws)
        await manager._deliver_local_user_message(live, "hello", message_id="msg_u1")

        rec = read_turn(live.chat_id)
        assert rec is not None
        assert rec.turn_id == live.turn_id
        assert rec.message_id == "msg_u1"
        assert rec.trace_id and rec.span_id
        assert rec.workload == "chat" and rec.surface == Surface.WEB.value

        await _pump(
            manager,
            live,
            [
                {"type": "tool_call", "tool": "Bash", "tool_use_id": "tu1", "args": {"cmd": "ls /secret"}},
                {"type": "tool_result", "tool_use_id": "tu1", "result": "ok", "is_error": False},
                dict(_ASSISTANT),
            ],
        )

        stamped = [f for f in ws.sent if f.get("type") in ("tool_call", "tool_result", "assistant_message")]
        assert len(stamped) == 3
        assert all(f.get("turn_id") == rec.turn_id for f in stamped), stamped

        spans = {sp.name: sp for sp in otel_exporter.get_finished_spans()}
        turn, tool = spans["agnes.chat.turn"], spans["agnes.chat.tool Bash"]
        assert tool.parent is not None and tool.parent.span_id == turn.context.span_id
        assert turn.kind.name == "INTERNAL" and tool.kind.name == "INTERNAL"
        attrs = dict(turn.attributes)
        assert attrs["agnes.kind"] == "turn"
        assert attrs["agnes.turn_id"] == rec.turn_id
        assert attrs["agnes.session_id"] == live.chat_id
        assert attrs["agnes.surface"] == Surface.WEB.value
        assert attrs["agnes.workload"] == "chat"
        assert attrs["agnes.tool_calls"] == 1
        assert attrs["gen_ai.usage.input_tokens"] == 10
        assert attrs["gen_ai.usage.output_tokens"] == 5
        assert attrs["gen_ai.usage.cache_read_input_tokens"] == 100
        assert attrs["agnes.cost_usd"] > 0
        assert turn.status.status_code.name == "OK"

        tool_attrs = dict(tool.attributes)
        assert tool_attrs["agnes.kind"] == "tool" and tool_attrs["agnes.tool"] == "Bash"
        assert tool_attrs["agnes.args_hash"] and tool_attrs["agnes.is_error"] is False
        assert "ls /secret" not in json.dumps(tool_attrs), "tool arguments must never reach a span"

    asyncio.run(_run())


def test_a_failed_tool_marks_its_own_span_only(manager: ChatManager, otel_exporter):  # noqa: F811
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        await _pump(
            manager,
            live,
            [
                {"type": "tool_call", "tool": "Bash", "tool_use_id": "tu1", "args": {}},
                {"type": "tool_result", "tool_use_id": "tu1", "result": "boom", "is_error": True},
                dict(_ASSISTANT),
            ],
        )
        spans = {sp.name: sp for sp in otel_exporter.get_finished_spans()}
        assert dict(spans["agnes.chat.tool Bash"].attributes)["agnes.is_error"] is True
        assert spans["agnes.chat.tool Bash"].status.status_code.name == "ERROR"
        assert spans["agnes.chat.turn"].status.status_code.name == "OK"

    asyncio.run(_run())


def test_an_error_frame_becomes_the_turns_error_kind(manager: ChatManager, otel_exporter):  # noqa: F811
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        await _pump(
            manager,
            live,
            [
                {"type": "error", "kind": "turn_idle_timeout", "message": "gave up"},
                {"type": "done"},
            ],
        )
        (turn,) = [sp for sp in otel_exporter.get_finished_spans() if sp.name == "agnes.chat.turn"]
        assert turn.status.status_code.name == "ERROR"
        assert dict(turn.attributes)["error.type"] == "turn_idle_timeout"

    asyncio.run(_run())


def test_an_unfinished_tool_span_is_closed_with_the_turn(manager: ChatManager, otel_exporter):  # noqa: F811
    """A turn that ends while a tool is still running still exports the
    tool span — an unended span is never exported at all."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        await _pump(
            manager,
            live,
            [
                {"type": "tool_call", "tool": "Read", "tool_use_id": "tu9", "args": {}},
                dict(_ASSISTANT),
            ],
        )
        names = [sp.name for sp in otel_exporter.get_finished_spans()]
        assert "agnes.chat.tool Read" in names
        assert live.turn_tool_spans == {}

    asyncio.run(_run())


def test_turn_key_survives_turn_end_and_the_next_turn_overwrites(manager: ChatManager):
    """The key is never deleted at turn end: a completion that lands after
    the assistant frame still attributes to the turn that caused it."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "one")
        first = live.turn_id
        await _pump(manager, live, [dict(_ASSISTANT)])

        after_end = read_turn(live.chat_id)
        assert after_end is not None and after_end.turn_id == first

        await manager._deliver_local_user_message(live, "two")
        second = read_turn(live.chat_id)
        assert second is not None and second.turn_id == live.turn_id != first

    asyncio.run(_run())


def test_close_turn_republishes_with_ended_at_set_and_keeps_ids(manager: ChatManager):
    """``_close_turn`` re-publishes the SAME record with ``ended_at`` set
    rather than deleting it — a reader after this point (memory provenance,
    finding B) can tell the turn is over; the broker's late-completion
    linkage (finding A) still finds every other id unchanged."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "one", message_id="msg_1")

        before = read_turn(live.chat_id)
        assert before is not None and before.ended_at is None and before.is_open() is True

        await _pump(manager, live, [dict(_ASSISTANT)])

        after = read_turn(live.chat_id)
        assert after is not None
        assert after.turn_id == before.turn_id
        assert after.ended_at is not None
        assert after.is_open() is False
        # every other field survives the re-publish unchanged
        assert after.trace_id == before.trace_id
        assert after.span_id == before.span_id
        assert after.user_id == before.user_id
        assert after.agent_id == before.agent_id
        assert after.surface == before.surface
        assert after.workload == before.workload
        assert after.message_id == before.message_id == "msg_1"

    asyncio.run(_run())


def test_a_second_message_mid_turn_does_not_orphan_the_open_span(manager: ChatManager, otel_exporter):  # noqa: F811
    """A co-driver's message landing before the answer starts the next turn;
    the turn it interrupted is closed rather than left unended (an unended
    span is never exported at all)."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "one")
        await manager._deliver_local_user_message(live, "two")
        turns = [sp for sp in otel_exporter.get_finished_spans() if sp.name == "agnes.chat.turn"]
        assert len(turns) == 1, "the interrupted turn's span must be ended"

    asyncio.run(_run())


def test_a_session_turn_records_the_api_surface_as_agent_api_work(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.API)
        live = _attach_live(manager, s.id, "u@x", FakeWS(), surface=Surface.API.value)
        await manager._deliver_local_user_message(live, "hello")
        rec = read_turn(live.chat_id)
        assert rec is not None and rec.workload == "agent_api"

    asyncio.run(_run())


def test_usage_turns_row_uses_the_turn_id(manager: ChatManager, monkeypatch):
    """The per-turn token table and the trace agree on what a turn is."""

    class _RecordingTurnsRepo:
        def __init__(self) -> None:
            self.rows: list[dict] = []

        def insert_batch(self, rows) -> int:
            materialized = [dict(r) for r in rows]
            self.rows.extend(materialized)
            return len(materialized)

    class _FakeUsers:
        def get_by_email(self, email: str):
            return {"id": "user-1"}

    turns = _RecordingTurnsRepo()
    monkeypatch.setattr("app.chat.manager.use_pg", lambda: True)
    monkeypatch.setattr("app.chat.manager.usage_turns_repo", lambda: turns)
    monkeypatch.setattr("app.chat.manager.users_repo", lambda: _FakeUsers())

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        turn_id = live.turn_id
        await _pump(manager, live, [dict(_ASSISTANT)])
        assert len(turns.rows) == 1
        assert turns.rows[0]["turn_uuid"] == turn_id

    asyncio.run(_run())


def test_assistant_persist_passes_the_turn_id_to_append_message(manager: ChatManager):
    """The PG-only ``chat_messages.turn_id`` column (migration 0115): the
    assistant persist call at the ``assistant_message`` frame branch must
    carry ``turn_id=live.turn_id``. A recording fake stands in for
    ``_messages_pg`` (the DuckDB path silently drops the kwarg — there is no
    column to read it back from), same technique as the PG delegation the
    real ``ChatRepository.append_message`` already performs."""

    class _RecordingMessagesPg:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def append_message(self, **kwargs):
            self.calls.append(kwargs)
            return object()

    recorder = _RecordingMessagesPg()

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        turn_id = live.turn_id
        manager._repo._messages_pg = recorder
        try:
            await _pump(manager, live, [dict(_ASSISTANT)])
        finally:
            manager._repo._messages_pg = None
        assert len(recorder.calls) == 1
        assert recorder.calls[0]["turn_id"] == turn_id

    asyncio.run(_run())


def test_send_user_message_threads_the_message_id_into_the_record(manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        rec = read_turn(live.chat_id)
        messages = manager._repo.list_messages(s.id)
        user_rows = [m for m in messages if m.role == "user"]
        assert rec is not None and user_rows
        assert rec.message_id == user_rows[-1].id

    asyncio.run(_run())


def test_send_user_message_mints_one_turn_id_shared_by_user_and_assistant_rows(manager: ChatManager):
    """Gap fix: the user row used to be persisted with no ``turn_id`` at all
    (only the assistant row carried one), so the corpus export could not
    pair a question with its answer. ``send_user_message`` now mints the id
    BEFORE the user row is persisted and reuses it for the assistant row —
    wrap the real ``append_message`` (rather than swap ``_messages_pg``,
    which several OTHER repo methods on this call path also read) to
    observe what was PASSED while still exercising the real DuckDB persist
    underneath."""
    calls: list[dict] = []
    real_append_message = manager._repo.append_message

    def _recording_append_message(**kwargs):
        calls.append(kwargs)
        return real_append_message(**kwargs)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        manager._repo.append_message = _recording_append_message
        try:
            await manager.send_user_message(s.id, "hello")
            turn_id = live.turn_id
            await _pump(manager, live, [dict(_ASSISTANT)])
        finally:
            manager._repo.append_message = real_append_message
        user_calls = [c for c in calls if c["role"] == "user"]
        assistant_calls = [c for c in calls if c["role"] == "assistant"]
        assert user_calls and assistant_calls
        assert turn_id is not None
        assert user_calls[-1]["turn_id"] == turn_id
        assert assistant_calls[-1]["turn_id"] == turn_id

    asyncio.run(_run())


def test_spans_never_break_the_pump(manager: ChatManager, monkeypatch):
    """Every span call is wrapped: a tracer that raises at open, at the tool
    seam and at close still leaves a persisted answer and a finished turn."""

    def _boom(*a, **kw):
        raise RuntimeError("otel down")

    monkeypatch.setattr("app.chat.manager._otel.start_turn_span", _boom)
    monkeypatch.setattr("app.chat.manager._otel.start_tool_span", _boom)
    monkeypatch.setattr("app.chat.manager._otel.end_tool_span", _boom)
    monkeypatch.setattr("app.chat.manager._otel.end_turn_span", _boom)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        assert live.turn_in_flight is True
        await _pump(
            manager,
            live,
            [
                {"type": "tool_call", "tool": "Bash", "tool_use_id": "tu1", "args": {}},
                {"type": "tool_result", "tool_use_id": "tu1", "is_error": False},
                dict(_ASSISTANT),
            ],
        )
        assert live.turn_in_flight is False
        assert [m.content for m in manager._repo.list_messages(s.id) if m.role == "assistant"] == ["done"]

    asyncio.run(_run())


def test_a_publish_outage_never_breaks_the_turn(manager: ChatManager, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("coordination down")

    monkeypatch.setattr("app.chat.manager.publish_turn", _boom)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        await _pump(manager, live, [dict(_ASSISTANT)])
        assert live.turn_in_flight is False

    asyncio.run(_run())


def test_a_killed_turn_still_closes_its_span(manager: ChatManager, otel_exporter):  # noqa: F811
    """The partial-save path clears ``turn_in_flight`` outside the pump; an
    unended turn span would never be exported."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager._deliver_local_user_message(live, "hello")
        manager._partial_save(live, reason="killed")
        assert live.turn_in_flight is False
        assert [sp.name for sp in otel_exporter.get_finished_spans()] == ["agnes.chat.turn"]

    asyncio.run(_run())
