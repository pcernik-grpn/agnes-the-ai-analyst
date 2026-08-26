"""V1c Task 3: memory materialization at spawn.

Covers `app/chat/agent_profile.py::select_in_budget` / `materialize_memories`
(pure unit tests) and the `ChatManager._spawn_live` seam that must call
`materialize_memories` BEFORE `_spawn_runner` uploads the session workdir
into the (remote) sandbox — see that module's docstring for
why the post-spawn `record_snapshot` seam is the wrong place for this.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat import agent_profile

# ---------------------------------------------------------------------------
# select_in_budget
# ---------------------------------------------------------------------------


def _memory(content: str, **overrides) -> dict:
    row = {
        "id": str(uuid.uuid4()),
        "agent_id": "agent-1",
        "owner_user_id": "owner-1",
        "content": content,
        "source_session_id": None,
        "status": "active",
        "created_at": "2026-07-20 10:00:00",
    }
    row.update(overrides)
    return row


def test_select_in_budget_all_fit():
    memories = [_memory("a" * 10), _memory("b" * 10)]
    in_budget, shadowed = agent_profile.select_in_budget(memories, max_chars=100)
    assert in_budget == memories
    assert shadowed == []


def test_select_in_budget_splits_at_boundary():
    """Newest-first: memories[0] is newest. With a budget that fits exactly
    the first two but not the third, the split must land precisely there."""
    memories = [_memory("a" * 10), _memory("b" * 10), _memory("c" * 10)]
    in_budget, shadowed = agent_profile.select_in_budget(memories, max_chars=20)
    assert in_budget == memories[:2]
    assert shadowed == memories[2:]


def test_select_in_budget_nothing_fits():
    memories = [_memory("a" * 50)]
    in_budget, shadowed = agent_profile.select_in_budget(memories, max_chars=10)
    assert in_budget == []
    assert shadowed == memories


def test_select_in_budget_empty_input():
    in_budget, shadowed = agent_profile.select_in_budget([], max_chars=100)
    assert in_budget == []
    assert shadowed == []


# ---------------------------------------------------------------------------
# materialize_memories — pure unit tests against a fake repo
# ---------------------------------------------------------------------------


class _FakeMemoriesRepo:
    def __init__(self, memories=None, raise_on_list=False):
        self._memories = memories or []
        self._raise = raise_on_list

    def list_active(self, agent_id):
        if self._raise:
            raise RuntimeError("db exploded")
        return self._memories


def _patch_repo(monkeypatch, repo):
    monkeypatch.setattr("src.repositories.agent_memories_repo", lambda: repo)


def test_materialize_memories_writes_two_active_memories(tmp_path, monkeypatch):
    memories = [
        _memory("Remember to always confirm budget.", created_at="2026-07-22 09:00:00"),
        _memory("Prefers metric units.", created_at="2026-07-18 09:00:00"),
    ]
    _patch_repo(monkeypatch, _FakeMemoriesRepo(memories))

    count = agent_profile.materialize_memories({"id": "agent-1"}, tmp_path)

    assert count == 2
    memory_file = tmp_path / ".claude" / "agent-memory.md"
    assert memory_file.exists()
    text = memory_file.read_text(encoding="utf-8")
    assert "Remember to always confirm budget." in text
    assert "Prefers metric units." in text
    assert "2026-07-22" in text
    assert "2026-07-18" in text


def test_materialize_memories_respects_char_budget_only_newest_fit(tmp_path, monkeypatch):
    big = "x" * (agent_profile._MEMORY_BUDGET_CHARS - 10)
    memories = [
        _memory(big, created_at="2026-07-22 09:00:00"),  # newest, consumes almost the whole budget
        _memory("this older memory should be shadowed", created_at="2026-07-01 09:00:00"),
    ]
    _patch_repo(monkeypatch, _FakeMemoriesRepo(memories))

    count = agent_profile.materialize_memories({"id": "agent-1"}, tmp_path)

    assert count == 1
    text = (tmp_path / ".claude" / "agent-memory.md").read_text(encoding="utf-8")
    assert big in text
    assert "this older memory should be shadowed" not in text


def test_materialize_memories_no_active_memories_writes_nothing(tmp_path, monkeypatch):
    _patch_repo(monkeypatch, _FakeMemoriesRepo([]))

    count = agent_profile.materialize_memories({"id": "agent-1"}, tmp_path)

    assert count == 0
    assert not (tmp_path / ".claude" / "agent-memory.md").exists()


def test_materialize_memories_no_agent_id_writes_nothing(tmp_path, monkeypatch):
    repo = _FakeMemoriesRepo([_memory("should never be read")])
    _patch_repo(monkeypatch, repo)

    count = agent_profile.materialize_memories({}, tmp_path)

    assert count == 0
    assert not (tmp_path / ".claude").exists()


def test_materialize_memories_repo_raising_returns_zero_no_exception(tmp_path, monkeypatch):
    _patch_repo(monkeypatch, _FakeMemoriesRepo(raise_on_list=True))

    # must not raise
    count = agent_profile.materialize_memories({"id": "agent-1"}, tmp_path)

    assert count == 0
    assert not (tmp_path / ".claude" / "agent-memory.md").exists()


def test_materialize_memories_disk_error_returns_zero_no_exception(tmp_path, monkeypatch):
    _patch_repo(monkeypatch, _FakeMemoriesRepo([_memory("hi")]))
    # session_dir isn't a directory at all -> mkdir()/write_text() blow up.
    bogus_dir = tmp_path / "not_a_dir"
    bogus_dir.write_text("i am a file, not a directory")

    count = agent_profile.materialize_memories({"id": "agent-1"}, bogus_dir)

    assert count == 0


# ---------------------------------------------------------------------------
# Integration seam — ChatManager._spawn_live must materialize memories
# BEFORE _spawn_runner (which uploads session_dir into the remote sandbox).
# Mirrors tests/test_agent_profile_spawn.py's spawn_env fixture shape.
# ---------------------------------------------------------------------------


def _make_workdir_mgr(tmp_path: Path, repo):
    from app.chat.workdir import WorkdirManager

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("generic analyst rails")
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
def spawn_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)

    from app.chat.config import ChatConfig
    from app.chat.manager import ChatManager
    from app.chat.persistence import ChatRepository
    from src.db import get_system_db

    conn = get_system_db()
    repo = ChatRepository(conn)
    workdir_mgr = _make_workdir_mgr(tmp_path, repo)
    provider = MagicMock()
    provider.spawn = AsyncMock()
    mgr = ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=2),
    )
    return mgr, repo


def _fake_handle():
    from tests.chat_fakes import FakeHandle

    return FakeHandle()


def _surface_web():
    from app.chat.types import Surface

    return Surface.WEB


def test_spawn_live_materializes_memories_before_spawn_runner(spawn_env, monkeypatch):
    """The file must exist in session_dir at the moment _spawn_runner (the
    call that uploads the workdir into the sandbox) is invoked — proving
    materialization happened at the pre-spawn seam, not after."""
    import asyncio

    from src.repositories import agent_memories_repo, agents_repo

    mgr, repo = spawn_env
    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="u1",
        name="Memory Agent",
        slug="memory-agent",
        system_prompt="You have memories.",
    )
    agent_memories_repo().create(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        owner_user_id="u1",
        content="Analyst prefers charts over tables.",
        source_session_id=None,
        status="active",
    )

    seen_at_spawn_runner = {}
    real_spawn_runner = mgr._spawn_runner

    async def _spy_spawn_runner(session, session_dir):
        memory_file = session_dir / ".claude" / "agent-memory.md"
        seen_at_spawn_runner["exists"] = memory_file.exists()
        seen_at_spawn_runner["text"] = memory_file.read_text(encoding="utf-8") if memory_file.exists() else None
        return await real_spawn_runner(session, session_dir)

    mgr._spawn_runner = _spy_spawn_runner

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u1@x.com", surface=_surface_web(), agent_id=agent_id)
        live = await mgr._spawn_live(s)
        try:
            assert seen_at_spawn_runner["exists"] is True
            assert "Analyst prefers charts over tables." in seen_at_spawn_runner["text"]
            memory_file = live.session_dir / ".claude" / "agent-memory.md"
            assert memory_file.exists()
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


def test_spawn_live_default_agent_no_memories_writes_nothing(spawn_env):
    """Unchanged-web-chat invariant: the default agent (no memories, empty
    system_prompt) must not get an agent-memory.md — matches the existing
    default-agent-unchanged guarantee for the persona/CLAUDE.md seam."""
    import asyncio

    from src.repositories import agents_repo

    mgr, repo = spawn_env
    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id="u2",
        name="Default",
        slug="default",
        system_prompt="",
    )

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u2@x.com", surface=_surface_web(), agent_id=agent_id)
        live = await mgr._spawn_live(s)
        try:
            assert not (live.session_dir / ".claude" / "agent-memory.md").exists()
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())


def test_spawn_live_no_agent_id_writes_nothing(spawn_env):
    """No agent_id at all must not attempt memory materialization."""
    import asyncio

    mgr, repo = spawn_env

    async def _run():
        handle = _fake_handle()
        mgr._provider.spawn = AsyncMock(return_value=handle)
        s = repo.create_session(user_email="u3@x.com", surface=_surface_web())
        assert s.agent_id is None
        live = await mgr._spawn_live(s)
        try:
            assert not (live.session_dir / ".claude" / "agent-memory.md").exists()
        finally:
            await mgr.kill(s.id, reason="test_done")
            handle.emit_eof()

    asyncio.run(_run())
