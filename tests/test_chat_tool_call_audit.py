"""``chat.tool_call`` carries the tool's measured wall time.

The manager used to write the row the moment the ``tool_call`` frame
arrived — before the tool had run — so every row's ``duration_ms`` was NULL
and the observability KPIs could say nothing about how long chat tools take.
The engine emits a ``tool_result`` frame for every call, paired on
``tool_use_id`` (``app/chat/runner.py`` and ``app/chat/kai_engine_provider.py``
both stamp it), so the row is now written when the RESULT arrives, timed from
the call frame's arrival at the manager, with ``result`` taken from the
result frame's own ``is_error`` verdict. A call whose result never comes
(the turn was aborted, the sandbox died) is still recorded — at turn end,
without a duration — so the audit trail keeps capturing the attempt.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from app.chat.audit import hash_args
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests
from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS, _wait_until


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
        config=ChatConfig(enabled=True, concurrency_per_user=2, provider="docker"),
    )


@pytest.fixture
def audit_rows(monkeypatch) -> list[dict]:
    """Capture every ``write_audit`` call the manager makes, as kwargs."""
    rows: list[dict] = []

    def _record(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr("app.chat.manager.write_audit", _record)
    return rows


def _attach_live(mgr: ChatManager, chat_id: str, user_email: str) -> LiveSession:
    handle = FakeHandle()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        surface=Surface.WEB.value,
        sinks=[SinkEntry(participant_email=user_email, sink=FakeWS())],
    )
    mgr._live[chat_id] = live
    return live


def _tool_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("action") == "chat.tool_call"]


_CALL = {"type": "tool_call", "id": "tu1", "tool_use_id": "tu1", "tool": "Bash", "args": {"command": "ls"}}


async def _with_pump(manager: ChatManager, live: LiveSession, body):
    pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
    try:
        await body()
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass


def test_row_is_written_when_the_result_arrives_with_the_measured_duration(manager, audit_rows):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit(dict(_CALL))
            await _wait_until(lambda: len(live.turn_buffer) >= 1)
            assert _tool_rows(audit_rows) == [], "the row must wait for the result — it carries the duration"
            await asyncio.sleep(0.02)
            live.handle.emit({"type": "tool_result", "tool_use_id": "tu1", "tool": "tu1", "result": "ok"})
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 1)

        await _with_pump(manager, live, _body)

        rows = _tool_rows(audit_rows)
        assert len(rows) == 1
        row = rows[0]
        assert row["user_email"] == "u@x"
        # Params unchanged from before: identifiers and a hash, never the args.
        assert row["details"] == {"session_id": s.id, "tool": "Bash", "args_hash": hash_args({"command": "ls"})}
        assert row["result"] == "success"
        assert row["duration_ms"] is not None and row["duration_ms"] >= 15, row["duration_ms"]

    asyncio.run(_run())


def test_failed_result_marks_the_row_as_an_error(manager, audit_rows):
    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit(dict(_CALL))
            live.handle.emit(
                {"type": "tool_result", "tool_use_id": "tu1", "tool": "tu1", "result": "boom", "is_error": True}
            )
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 1)

        await _with_pump(manager, live, _body)
        row = _tool_rows(audit_rows)[0]
        assert row["result"] == "error"
        assert row["duration_ms"] is not None and row["duration_ms"] >= 0

    asyncio.run(_run())


def test_two_calls_in_flight_are_paired_on_tool_use_id(manager, audit_rows):
    """Results may arrive in any order; each row gets ITS call's name."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit({**_CALL, "id": "a", "tool_use_id": "a", "tool": "Read", "args": {"p": 1}})
            live.handle.emit({**_CALL, "id": "b", "tool_use_id": "b", "tool": "Grep", "args": {"p": 2}})
            live.handle.emit({"type": "tool_result", "tool_use_id": "b", "tool": "b", "result": "x"})
            live.handle.emit({"type": "tool_result", "tool_use_id": "a", "tool": "a", "result": "y"})
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 2)

        await _with_pump(manager, live, _body)
        by_tool = {r["details"]["tool"]: r for r in _tool_rows(audit_rows)}
        assert set(by_tool) == {"Read", "Grep"}
        assert by_tool["Read"]["details"]["args_hash"] == hash_args({"p": 1})
        assert by_tool["Grep"]["details"]["args_hash"] == hash_args({"p": 2})

    asyncio.run(_run())


def test_unresolved_call_is_still_recorded_at_turn_end_without_a_duration(manager, audit_rows):
    """The attempt is never lost: a call whose result never arrives gets its
    row when the turn ends, with no duration (nobody measured one) and no
    result verdict."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit(dict(_CALL))
            await _wait_until(lambda: len(live.turn_buffer) >= 1)
            live.handle.emit({"type": "assistant_message", "content": "done"})
            await _wait_until(lambda: not live.turn_in_flight)

        await _with_pump(manager, live, _body)
        rows = _tool_rows(audit_rows)
        assert len(rows) == 1
        assert rows[0]["details"]["tool"] == "Bash"
        assert rows[0].get("duration_ms") is None
        assert rows[0].get("result") is None

    asyncio.run(_run())


def test_call_without_a_pairing_id_is_written_immediately(manager, audit_rows):
    """A producer that stamps no ``tool_use_id`` cannot be paired with its
    result, so the row is written at call time exactly as before — never
    invented a duration for."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit({"type": "tool_call", "tool": "Bash", "args": {"command": "ls"}})
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 1)

        await _with_pump(manager, live, _body)
        row = _tool_rows(audit_rows)[0]
        assert row["details"]["tool"] == "Bash"
        assert row.get("duration_ms") is None

    asyncio.run(_run())


class _SlowWS(FakeWS):
    """A sink whose delivery takes longer than the tool itself — a Slack
    poster mid-HTTP, a browser on a bad link."""

    async def send_json(self, data: dict) -> None:
        await asyncio.sleep(0.05)
        await super().send_json(data)


def _attach_slow_live(mgr: ChatManager, chat_id: str, user_email: str) -> LiveSession:
    live = _attach_live(mgr, chat_id, user_email)
    live.sinks = [SinkEntry(participant_email=user_email, sink=_SlowWS())]
    return live


def test_duration_comes_from_the_producer_clock_not_sink_delivery(manager, audit_rows):
    """The pump is one sequential loop: a slow sink on the call frame delays
    when the result frame is even read, so manager-side arrival times can
    never separate delivery latency from tool time. Both producers stamp
    ``emitted_at`` (their own monotonic clock) on the call AND the result,
    and the difference between the two is the tool's real duration."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_slow_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit({**_CALL, "emitted_at": 100.0})
            live.handle.emit(
                {"type": "tool_result", "tool_use_id": "tu1", "tool": "tu1", "result": "ok", "emitted_at": 100.25}
            )
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 1)

        await _with_pump(manager, live, _body)
        row = _tool_rows(audit_rows)[0]
        # Two slow broadcasts (~100 ms) happened between the frames' arrival
        # at the pump; none of it is the tool's.
        assert row["duration_ms"] == 250, row["duration_ms"]

    asyncio.run(_run())


def test_without_producer_stamps_the_duration_falls_back_to_arrival_times(manager, audit_rows):
    """An older producer that stamps nothing still gets a measured row —
    from the frames' arrival at the pump, which then includes whatever the
    pump was doing in between."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_slow_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit(dict(_CALL))
            live.handle.emit({"type": "tool_result", "tool_use_id": "tu1", "tool": "tu1", "result": "ok"})
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 1)

        await _with_pump(manager, live, _body)
        row = _tool_rows(audit_rows)[0]
        assert row["duration_ms"] is not None and row["duration_ms"] >= 0

    asyncio.run(_run())


def test_a_producer_stamp_on_only_one_side_is_ignored(manager, audit_rows):
    """A stamp can only be compared with a stamp from the same clock; one
    side missing means the arrival fallback, never a mixed-clock number."""

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x")
        await manager.send_user_message(s.id, "hello")

        async def _body():
            live.handle.emit({**_CALL, "emitted_at": 5000.0})
            live.handle.emit({"type": "tool_result", "tool_use_id": "tu1", "tool": "tu1", "result": "ok"})
            await _wait_until(lambda: len(_tool_rows(audit_rows)) >= 1)

        await _with_pump(manager, live, _body)
        row = _tool_rows(audit_rows)[0]
        assert 0 <= row["duration_ms"] < 1000, row["duration_ms"]

    asyncio.run(_run())


def test_both_frame_producers_stamp_emitted_at():
    """The two frame producers must keep stamping the pairing clock, or the
    manager silently degrades to arrival times."""
    import inspect

    from app.chat import kai_engine_provider, runner

    assert '"emitted_at": time.monotonic()' in inspect.getsource(kai_engine_provider)
    assert '"emitted_at": time.monotonic()' in inspect.getsource(runner)
