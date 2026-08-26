"""Regression tests for marketplace-SHA-driven workspace reinit.

Covers the integration of WorkdirManager.needs_reinit with ChatManager.attach:
when the marketplace SHA changes between sessions the next attach must trigger
run_init before spawning the runner.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb

from src.db import _ensure_schema
from app.chat.config import ChatConfig
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager


def test_marketplace_sha_debounced(tmp_path):
    """When debounce_seconds > 0 the marketplace SHA is read at most once per
    interval; intermediate calls return the cached value.

    Operators configure ``marketplace_sha_debounce_seconds`` in instance.yaml
    to limit FS reads on hot paths; before this knob was wired every
    ``needs_reinit`` call re-read the SHA file unconditionally.
    """
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)

    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")

    call_count = {"n": 0}

    def sha_fn():
        call_count["n"] += 1
        return "sha-1"

    mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=sha_fn,
        get_template_status=lambda: None,
        marketplace_sha_debounce_seconds=60,
    )

    # Seed a workdir row at sha-1 so needs_reinit doesn't decide it's missing,
    # plus an on-disk sentinel at the current server_url so the URL check
    # doesn't decide it's stale.
    repo.upsert_workdir(
        user_email="u@x",
        marketplace_sha="sha-1",
        initial_workspace_sha=None,
        agnes_version="0.55.0",
    )
    sentinel = mgr.user_workspace("u@x") / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("server_url: https://example\n")

    # First call → reads SHA.
    assert mgr.needs_reinit("u@x") is False
    assert call_count["n"] == 1
    # Second call within debounce window → cached.
    assert mgr.needs_reinit("u@x") is False
    assert call_count["n"] == 1


def test_marketplace_sha_no_debounce_when_zero(tmp_path):
    """debounce_seconds=0 disables caching; SHA is re-read on every call."""
    import duckdb
    from src.db import _ensure_schema
    from app.chat.persistence import ChatRepository

    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    (bundled / "CLAUDE.md").write_text("d")

    call_count = {"n": 0}

    def sha_fn():
        call_count["n"] += 1
        return "sha-1"

    mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=sha_fn,
        get_template_status=lambda: None,
        marketplace_sha_debounce_seconds=0,
    )

    repo.upsert_workdir(
        user_email="u@x",
        marketplace_sha="sha-1",
        initial_workspace_sha=None,
        agnes_version="0.55.0",
    )

    mgr.needs_reinit("u@x")
    mgr.needs_reinit("u@x")
    mgr.needs_reinit("u@x")
    assert call_count["n"] == 3


def _make_repo(tmp_path: Path) -> ChatRepository:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    return ChatRepository(conn)


def _make_manager(
    tmp_path: Path,
    repo: ChatRepository,
    *,
    sha_fn,
) -> ChatManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    workdir_mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=sha_fn,
        get_template_status=lambda: None,
    )
    provider = MagicMock()
    provider.spawn = AsyncMock(return_value=MagicMock(pid=1))
    return ChatManager(
        provider=provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(enabled=True, concurrency_per_user=3),
    ), workdir_mgr


def test_workdir_needs_reinit_on_marketplace_sha_change(tmp_path: Path):
    """WorkdirManager.needs_reinit returns True after marketplace SHA changes."""
    repo = _make_repo(tmp_path)
    current_sha = ["sha-1"]

    mgr, workdir_mgr = _make_manager(tmp_path, repo, sha_fn=lambda: current_sha[0])

    async def _run():
        # First session — initial init
        await mgr.create_session(user_email="u@x", surface=Surface.WEB)
        workdir_mgr.ensure_user_workdir("u@x")
        # Sentinel must exist and needs_reinit should be False
        assert not workdir_mgr.needs_reinit("u@x")

        # Marketplace SHA changes
        current_sha[0] = "sha-2"
        assert workdir_mgr.needs_reinit("u@x")

    asyncio.run(_run())


def test_workdir_needs_reinit_on_version_change(tmp_path: Path):
    """WorkdirManager.needs_reinit returns True when agnes_version changes."""
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    wm_v1 = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    # Initialize under v1
    wm_v1.ensure_user_workdir("u@y")
    assert not wm_v1.needs_reinit("u@y")

    # Bump to v2 — should trigger reinit
    wm_v2 = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.56.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    assert wm_v2.needs_reinit("u@y")


def test_workdir_needs_reinit_on_server_url_change(tmp_path: Path):
    """A workspace initialized under one SERVER_URL must reinit after the
    instance moves to a new URL — otherwise the rendered CLAUDE.md keeps
    naming the pre-migration host until an unrelated version bump."""
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    common = dict(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    wm_old = WorkdirManager(server_url="http://192.0.2.1:8000", **common)
    wm_old.ensure_user_workdir("u@x")
    assert not wm_old.needs_reinit("u@x")

    # Operator migrates the instance to a new domain; version and SHA stable.
    wm_new = WorkdirManager(server_url="https://analyst.example.com", **common)
    assert wm_new.needs_reinit("u@x")

    # Convergence re-stamps the sentinel; the next check is clean.
    wm_new.ensure_user_workdir("u@x")
    assert not wm_new.needs_reinit("u@x")


def test_workdir_needs_reinit_when_sentinel_lacks_server_url(tmp_path: Path):
    """A legacy sentinel without a server_url line reads as unknown →
    one self-healing reinit re-stamps it."""
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    wm = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    wm.ensure_user_workdir("u@x")
    sentinel = wm.user_workspace("u@x") / ".claude" / "init-complete"
    lines = [ln for ln in sentinel.read_text().splitlines() if not ln.startswith("server_url:")]
    sentinel.write_text("\n".join(lines) + "\n")

    assert wm.needs_reinit("u@x")
    wm.ensure_user_workdir("u@x")
    assert not wm.needs_reinit("u@x")


def test_workdir_no_reinit_needed_when_sha_stable(tmp_path: Path):
    """needs_reinit returns False when SHA and version are unchanged."""
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    wm = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "stable-sha",
        get_template_status=lambda: None,
    )
    wm.ensure_user_workdir("u@z")
    assert not wm.needs_reinit("u@z")
    # Still False on second check
    assert not wm.needs_reinit("u@z")


def test_workdir_no_reinit_loop_when_server_url_has_surrounding_whitespace(tmp_path: Path):
    """A SERVER_URL with stray whitespace must not reinit on every attach.

    `write_sentinel` emits ``server_url: <value>`` and `read_sentinel_server_url`
    strips what it reads back, so an unstripped `self._server_url` never equals
    the value just written — `needs_reinit` stays True forever and every session
    attach pays a full template copy. Non-destructive (the init path only
    overwrites template-owned files, it deletes nothing), but a malformed env
    var should not cost that per session.
    """
    repo = _make_repo(tmp_path)
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")

    wm = WorkdirManager(
        server_url="  https://analyst.example.com  ",
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    wm.ensure_user_workdir("u@x")
    assert not wm.needs_reinit("u@x"), (
        "the sentinel was just re-stamped, yet the workspace still reads as stale — every attach would reinit"
    )
