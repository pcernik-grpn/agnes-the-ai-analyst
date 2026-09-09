"""``chat.tool_call`` rows that were never timed store a real SQL NULL.

Both audit repositories treat ``duration_ms=None`` as "autofill from the
HTTP request that is running" (``src.audit_context.auto_duration_ms``). A
chat pump is an ``asyncio`` task, and a task copies the context it was
created in — so a pump spawned from inside an HTTP handler (a session
created and spawned by one request, the agent runtime's one-shot call)
inherits that request's start mark for its whole life. An unfinished tool
call flushed hours later would then be stamped with the age of a request
that has nothing to do with it, and read as a measurement. ``write_audit``
therefore distinguishes three intents: an int is a measurement, ``None`` is
"explicitly unmeasured" (stored as NULL whatever the context says), and the
default leaves the autofill to the repository as before.
"""

from __future__ import annotations

import asyncio
import contextvars
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from app.chat.audit import write_audit
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests
from src import audit_context
from src.db import _ensure_schema
from src.repositories import audit_repo
from tests.chat_fakes import FakeHandle, FakeWS, _wait_until


def _in_request_context(fn, *args, **kwargs):
    """Run *fn* in a copied context that looks like the middle of an HTTP
    request — the situation a pump task created by a handler inherits."""
    ctx = contextvars.copy_context()

    def _inner():
        audit_context.mark_request_start()
        return fn(*args, **kwargs)

    return ctx.run(_inner)


def _rows(action: str, session_id: str) -> list[dict]:
    rows, _ = audit_repo().query(action=action, limit=50)
    out = []
    for r in rows:
        params = r["params"]
        if isinstance(params, str):
            import json

            params = json.loads(params)
        if (params or {}).get("session_id") == session_id:
            out.append(r)
    return out


def test_explicit_none_is_stored_as_null_even_inside_a_request(seeded_app):
    sid = "chat_null_1"
    _in_request_context(
        write_audit,
        user_email="admin@test.com",
        action="chat.tool_call",
        details={"session_id": sid, "tool": "Bash", "args_hash": "x"},
        duration_ms=None,
    )
    (row,) = _rows("chat.tool_call", sid)
    assert row["duration_ms"] is None


def test_default_still_autofills_from_the_request(seeded_app):
    """The other chat events (approval decisions, session kills, ...) are
    written from real handlers, where the request's own age IS the right
    duration — the default keeps that behaviour."""
    sid = "chat_null_2"
    _in_request_context(
        write_audit,
        user_email="admin@test.com",
        action="chat.tool_call",
        details={"session_id": sid, "tool": "Bash", "args_hash": "x"},
    )
    (row,) = _rows("chat.tool_call", sid)
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0


def test_a_measurement_passes_through(seeded_app):
    sid = "chat_null_3"
    _in_request_context(
        write_audit,
        user_email="admin@test.com",
        action="chat.tool_call",
        details={"session_id": sid, "tool": "Bash", "args_hash": "x"},
        duration_ms=4321,
        result="success",
    )
    (row,) = _rows("chat.tool_call", sid)
    assert row["duration_ms"] == 4321 and row["result"] == "success"


def test_the_outer_context_is_left_untouched(seeded_app):
    """Storing NULL must not clear the request's own start mark for the
    handler that continues after the write."""

    def _body():
        audit_context.mark_request_start()
        write_audit(
            user_email="admin@test.com",
            action="chat.tool_call",
            details={"session_id": "chat_null_4", "tool": "Bash", "args_hash": "x"},
            duration_ms=None,
        )
        return audit_context.auto_duration_ms()

    assert contextvars.copy_context().run(_body) is not None


# ---------------------------------------------------------------------------
# End to end: a pump created inside a request flushes an unfinished call
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path: Path) -> ChatManager:
    reset_coordination_for_tests()
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
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2, provider="docker"),
    )
    yield mgr
    reset_coordination_for_tests()


def test_pump_created_inside_a_request_flushes_null_durations(seeded_app, manager: ChatManager):
    async def _run():
        s = await manager.create_session(user_email="admin@test.com", surface=Surface.WEB)
        handle = FakeHandle()
        live = LiveSession(
            chat_id=s.id,
            user_email="admin@test.com",
            state=SessionState.ACTIVE,
            handle=handle,
            started_at=datetime.now(timezone.utc),
            last_activity=datetime.now(timezone.utc),
            surface=Surface.WEB.value,
            sinks=[SinkEntry(participant_email="admin@test.com", sink=FakeWS())],
        )
        manager._live[s.id] = live
        await manager.send_user_message(s.id, "hello")

        # The pump task is created the way a spawning HTTP handler creates it:
        # inside a context whose request start has been marked.
        audit_context.mark_request_start()
        pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
        try:
            handle.emit({"type": "tool_call", "id": "tu1", "tool_use_id": "tu1", "tool": "Bash", "args": {}})
            await _wait_until(lambda: len(live.turn_buffer) >= 1)
            handle.emit({"type": "assistant_message", "content": "done"})
            await _wait_until(lambda: not live.turn_in_flight)
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
        return s.id

    sid = contextvars.copy_context().run(asyncio.run, _run())
    (row,) = _rows("chat.tool_call", sid)
    assert row["duration_ms"] is None, "an unfinished call must never inherit the spawning request's age"
    assert row["result"] is None
