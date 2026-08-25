"""The `parts` model on the NATIVE (docker) path, end to end.

`tests/test_kai_engine_stub.py` proves the engine provider produces an
interleaved turn. This file proves the same for the provider Agnes ships by
default — because everything that builds and stores `parts` lives in
`ChatManager`, not in a provider, and a change there would silently affect
both. A real sandbox needs infrastructure, so the
turn is driven through `FakeProvider`, which stands in at exactly the
`SandboxProvider` seam E2B plugs into and speaks the native runner's frame
vocabulary.

What this pins: an interleaved native turn is persisted with its shape, the
tool entries carry the outcome the runner reported, and `tool_calls` stays a
faithful projection — so a reload renders the same transcript no matter which
provider produced it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import duckdb
import pytest

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager
from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS, _wait_until


def _workdir_mgr(tmp_path: Path, repo: ChatRepository) -> WorkdirManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")
    return WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.86.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )


@pytest.fixture
def native(tmp_path: Path):
    """A manager wired to a fake sandbox — the default (non-kai) provider seam."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    mgr = ChatManager(
        provider=AsyncMock(),
        workdir_mgr=_workdir_mgr(tmp_path, repo),
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )
    return mgr, repo


#: The frame sequence app/chat/runner.py emits for prose → tool → prose → tool
#: → prose. `is_error` rides tool_result on this path from the SDK block; the
#: manager's envelope overwrites `id`, which is why pairing uses tool_use_id.
_NATIVE_TURN = [
    {"type": "token", "text": "Let me check the catalog. "},
    {"type": "tool_call", "tool_use_id": "toolu_1", "tool": "Bash", "args": {"command": "agnes catalog"}},
    {"type": "tool_result", "tool_use_id": "toolu_1", "tool": "toolu_1", "result": "35 tables", "is_error": False},
    {"type": "token", "text": "\n\n35 tables. Now the failing one:"},
    {"type": "tool_call", "tool_use_id": "toolu_2", "tool": "Bash", "args": {"command": "agnes query 'x'"}},
    {
        "type": "tool_result",
        "tool_use_id": "toolu_2",
        "tool": "toolu_2",
        "result": "Catalog Error: Table with name nope does not exist!",
        "is_error": True,
    },
    {"type": "token", "text": "\n\nThat table is missing."},
    {
        "type": "assistant_message",
        "content": "Let me check the catalog.\n\n35 tables. Now the failing one:\n\nThat table is missing.",
    },
    {"type": "done"},
]


async def _drive(mgr: ChatManager, repo: ChatRepository, frames: list[dict]):
    """Attach to a session on the fake sandbox and feed it a turn's frames.

    Goes through `mgr.attach`, the real entry point, which owns the stdout
    pump and registers the sink. A test must NOT start its own pump: two
    coroutines reading one queue consume alternate frames and race on
    `live.turn_buffer`, which surfaces as a turn whose final text part has
    vanished — a convincing phantom product bug (it cost me a debugging
    detour before the interleaved broadcast trace gave it away).
    """
    handle = FakeHandle()
    mgr._provider.spawn = AsyncMock(return_value=handle)
    session = await mgr.create_session(user_email="u@x.com", surface=Surface.WEB)
    ws = FakeWS()
    attach = asyncio.create_task(mgr.attach(session.id, ws))
    assert await _wait_until(lambda: session.id in mgr._live), "the session never went live"
    for frame in frames:
        handle.emit(frame)
    assert await _wait_until(lambda: any(m.role == "assistant" for m in repo.list_messages(session.id))), (
        "the turn never persisted an assistant message"
    )
    handle.emit_eof()
    attach.cancel()
    return session, ws


def test_a_native_turn_persists_its_ordered_shape(native):
    """The heart of it: the default provider's turn is stored as the sequence
    it happened in, not as one flattened string plus a positionless list."""
    mgr, repo = native
    session, _ws = asyncio.run(_drive(mgr, repo, _NATIVE_TURN))

    msgs = [m for m in repo.list_messages(session.id) if m.role == "assistant"]
    assert msgs, "the turn must have persisted an assistant message"
    parts = msgs[-1].parts
    assert parts, "a native turn must persist `parts` — the manager builds them for every provider"
    assert [p["type"] for p in parts] == ["text", "tool", "text", "tool", "text"]
    assert parts[0]["text"] == "Let me check the catalog."
    assert parts[1]["tool"] == "Bash"
    assert parts[1]["args"] == {"command": "agnes catalog"}


def test_a_native_tools_outcome_survives_to_the_row(native):
    """The runner reports `is_error`; the part must record it, or a reloaded
    card is back to guessing from the payload text."""
    mgr, repo = native
    session, _ws = asyncio.run(_drive(mgr, repo, _NATIVE_TURN))

    parts = [m for m in repo.list_messages(session.id) if m.role == "assistant"][-1].parts
    tools = [p for p in parts if p["type"] == "tool"]
    assert len(tools) == 2, "two calls, two entries — a result must not append a third"
    assert tools[0]["state"] == "output-available"
    assert tools[0]["is_error"] is False
    assert tools[1]["state"] == "output-error", "the failing call must be recorded as failed"
    assert tools[1]["is_error"] is True
    assert "nope does not exist" in tools[1]["result"]


def test_tool_calls_stays_a_faithful_projection_on_the_native_path(native):
    """Readers that predate `parts` (the transcript export, the sources
    verdict) must keep seeing the same calls."""
    mgr, repo = native
    session, _ws = asyncio.run(_drive(mgr, repo, _NATIVE_TURN))

    msg = [m for m in repo.list_messages(session.id) if m.role == "assistant"][-1]
    assert msg.tool_calls == [
        {"tool": "Bash", "args": {"command": "agnes catalog"}},
        {"tool": "Bash", "args": {"command": "agnes query 'x'"}},
    ]


def test_the_client_receives_parts_on_the_wire(native):
    """The assistant_message frame carries `parts` too, so a client that never
    reloads still gets the same structure the row holds."""
    mgr, repo = native
    _session, ws = asyncio.run(_drive(mgr, repo, _NATIVE_TURN))

    finals = [f for f in ws.sent if f.get("type") == "assistant_message"]
    assert finals, "the turn must have broadcast an assistant_message"
    assert [p["type"] for p in finals[-1]["parts"]] == ["text", "tool", "text", "tool", "text"]


def test_a_toolless_native_turn_is_all_text(native):
    """The common case must not grow a spurious tool entry."""
    mgr, repo = native
    frames = [
        {"type": "token", "text": "Just prose."},
        {"type": "assistant_message", "content": "Just prose."},
        {"type": "done"},
    ]
    session, _ws = asyncio.run(_drive(mgr, repo, frames))
    parts = [m for m in repo.list_messages(session.id) if m.role == "assistant"][-1].parts
    assert parts == [{"type": "text", "text": "Just prose."}]


def test_parts_and_content_describe_the_same_answer(native):
    """`content` is the producers' blank-line join of the text blocks. If the
    two disagreed, a reader could not tell which one the answer was."""
    from app.chat.message_parts import parts_to_content

    mgr, repo = native
    session, _ws = asyncio.run(_drive(mgr, repo, _NATIVE_TURN))
    msg = [m for m in repo.list_messages(session.id) if m.role == "assistant"][-1]
    assert parts_to_content(msg.parts) == msg.content


def test_the_persisted_parts_are_json_serialisable(native):
    """The column is JSON; a value that cannot round-trip would fail at write
    time on one backend and not the other."""
    mgr, repo = native
    session, _ws = asyncio.run(_drive(mgr, repo, _NATIVE_TURN))
    parts = [m for m in repo.list_messages(session.id) if m.role == "assistant"][-1].parts
    assert json.loads(json.dumps(parts)) == parts
