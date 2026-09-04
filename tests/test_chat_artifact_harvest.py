"""Turn-end artifact harvest for interactive chat (#2268).

A chat deliverable only ever existed inside the engine's ephemeral sandbox.
Nothing copied it out, so once the idle reaper paused the session the sandbox
went and the Files panel answered "No files here yet" for a conversation that
had just produced a ``.docx`` — the transcript survived, the file did not.

These tests pin the fix at the BEHAVIOR level, not at the wiring:

- a session that produced a file still lists and still downloads that file
  once every trace of the sandbox is gone (the engine 404s everything);
- an unchanged ``outputs/`` dir is never re-read on the next turn (cost);
- a session at its cap stops harvesting and says so, and never deletes an
  artifact it already has;
- a harvest failure never breaks the chat turn it piggybacks on.

Fakes sit at the same seams ``tests/test_agent_artifacts_api.py`` uses: the
sandbox file API (``handle.files.list`` / ``.read``), the object store, and
the artifacts repo factory. No real sandbox, no real engine, no sockets.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import duckdb
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests
from src.db import _ensure_schema
from tests.chat_fakes import FakeHandle, FakeWS, _wait_until

CHAT_ID = "chat_harvest0001"
OWNER = {"id": "user_owner", "email": "owner@test.com", "is_admin": False}
OTHER = {"id": "user_other", "email": "other@test.com", "is_admin": False}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEntry:
    def __init__(self, name: str, is_dir: bool = False) -> None:
        self.name = name
        self.type = "DIR" if is_dir else "FILE"


class _FakeFiles:
    """The sandbox file API surface the harvest consumes (``.list``/``.read``)."""

    def __init__(self, outputs: dict[str, bytes] | None) -> None:
        self._outputs = dict(outputs) if outputs is not None else None
        self.list_calls: list[str] = []
        self.read_calls: list[str] = []

    async def list(self, path: str) -> list[_FakeEntry]:
        self.list_calls.append(path)
        if self._outputs is None:
            raise FileNotFoundError(path)
        return [_FakeEntry(name) for name in self._outputs]

    async def read(self, path: str, format: str = "bytes") -> bytes:  # noqa: A002 - SDK kwarg name
        self.read_calls.append(path)
        return self._outputs[path.rsplit("/", 1)[-1]]  # type: ignore[index]

    def write(self, name: str, data: bytes) -> None:
        assert self._outputs is not None
        self._outputs[name] = data


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_bytes(self, key: str, data: bytes, md5: str) -> None:
        self.objects[key] = (data, md5)

    def get_bytes(self, key: str):
        entry = self.objects.get(key)
        return entry[0] if entry else None


class _FakeArtifactsRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def create(
        self,
        id,
        session_id,
        agent_id,
        owner_user_id,
        filename,
        object_key,
        size_bytes,
        content_type,
        md5,
    ) -> None:
        self.rows[id] = {
            "id": id,
            "session_id": session_id,
            "agent_id": agent_id,
            "owner_user_id": owner_user_id,
            "filename": filename,
            "object_key": object_key,
            "size_bytes": size_bytes,
            "content_type": content_type,
            "md5": md5,
            "created_at": datetime.now(timezone.utc),
        }

    def get(self, id):
        return self.rows.get(id)

    def list_for_session(self, session_id):
        return [r for r in self.rows.values() if r["session_id"] == session_id]


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    import app.chat.artifact_harvest as ah

    store = _FakeStore()
    monkeypatch.setattr(ah, "object_store", lambda: store)
    return store


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> _FakeArtifactsRepo:
    import app.chat.artifact_harvest as ah

    repo = _FakeArtifactsRepo()
    monkeypatch.setattr(ah, "agent_artifacts_repo", lambda: repo)
    return repo


def _make_manager(tmp_path: Path, *, provider: str = "docker") -> ChatManager:
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
        agnes_version="0.0.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    sandbox_provider = MagicMock()
    sandbox_provider.spawn = AsyncMock()
    return ChatManager(
        provider=sandbox_provider,
        workdir_mgr=workdir_mgr,
        repo=repo,
        config=ChatConfig(
            enabled=True,
            concurrency_per_user=2,
            provider=provider,
            kai_agent_url="http://engine.test",
        ),
    )


def _seat_live(mgr: ChatManager, chat_id: str, email: str, sink, files: _FakeFiles | None):
    from app.chat.manager import LiveSession

    handle = FakeHandle()
    if files is not None:
        handle.files = files  # type: ignore[attr-defined]
    live = LiveSession(
        chat_id=chat_id,
        user_email=email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        surface=Surface.WEB.value,
        sinks=[SinkEntry(participant_email=email, sink=sink)],
    )
    mgr._live[chat_id] = live
    return live


def _stub_owner_lookup(monkeypatch: pytest.MonkeyPatch, user_id: str | None = OWNER["id"]) -> None:
    import app.chat.manager as mgr_mod

    monkeypatch.setattr(mgr_mod, "users_repo", lambda: SimpleNamespace(get_by_email=lambda email: {"id": user_id}))


# ---------------------------------------------------------------------------
# Caps: chat gets its own, per-SESSION budget
# ---------------------------------------------------------------------------


def test_chat_caps_are_their_own_and_larger_than_the_agent_api_defaults() -> None:
    from app.chat.artifact_harvest import (
        CHAT_ARTIFACT_MAX_BYTES,
        CHAT_ARTIFACT_MAX_FILES,
        DEFAULT_ARTIFACT_MAX_BYTES,
        DEFAULT_ARTIFACT_MAX_FILES,
    )

    assert CHAT_ARTIFACT_MAX_BYTES == 100 * 1024 * 1024
    assert CHAT_ARTIFACT_MAX_FILES == 100
    assert CHAT_ARTIFACT_MAX_BYTES > DEFAULT_ARTIFACT_MAX_BYTES
    assert CHAT_ARTIFACT_MAX_FILES > DEFAULT_ARTIFACT_MAX_FILES


def test_budget_counts_what_earlier_turns_already_harvested(fake_repo: _FakeArtifactsRepo) -> None:
    """The cap is per SESSION, not per call — a chat harvests every turn, so a
    per-call cap would let one conversation harvest 100 files per turn."""
    from app.chat.artifact_harvest import CHAT_ARTIFACT_MAX_BYTES, CHAT_ARTIFACT_MAX_FILES, chat_session_budget

    assert chat_session_budget(CHAT_ID) == (CHAT_ARTIFACT_MAX_BYTES, CHAT_ARTIFACT_MAX_FILES)

    fake_repo.create(
        id="a1",
        session_id=CHAT_ID,
        agent_id=None,
        owner_user_id=OWNER["id"],
        filename="deck.pptx",
        object_key=f"agent-artifacts/{CHAT_ID}/deck.pptx",
        size_bytes=1024,
        content_type=None,
        md5="m",
    )
    assert chat_session_budget(CHAT_ID) == (CHAT_ARTIFACT_MAX_BYTES - 1024, CHAT_ARTIFACT_MAX_FILES - 1)


def test_budget_exhausted_stops_harvesting_and_says_so(
    fake_repo: _FakeArtifactsRepo, caplog: pytest.LogCaptureFixture
) -> None:
    from app.chat.artifact_harvest import CHAT_ARTIFACT_MAX_FILES, chat_session_budget

    for i in range(CHAT_ARTIFACT_MAX_FILES):
        fake_repo.create(
            id=f"a{i}",
            session_id=CHAT_ID,
            agent_id=None,
            owner_user_id=OWNER["id"],
            filename=f"f{i}.txt",
            object_key=f"agent-artifacts/{CHAT_ID}/f{i}.txt",
            size_bytes=1,
            content_type=None,
            md5="m",
        )
    with caplog.at_level(logging.WARNING, logger="app.chat.artifact_harvest"):
        assert chat_session_budget(CHAT_ID) is None
    assert any("cap" in r.message.lower() for r in caplog.records), caplog.text
    # Never eviction: everything harvested earlier is still there.
    assert len(fake_repo.list_for_session(CHAT_ID)) == CHAT_ARTIFACT_MAX_FILES


# ---------------------------------------------------------------------------
# Turn end: the harvest actually runs, and runs cheaply
# ---------------------------------------------------------------------------


def test_turn_end_harvests_the_outputs_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """The behavior the issue asks for: after a turn ends, the deliverable
    exists OUTSIDE the sandbox."""
    _stub_owner_lookup(monkeypatch)

    async def _run() -> None:
        mgr = _make_manager(tmp_path)
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        files = _FakeFiles({"report.docx": b"deliverable bytes"})
        live = _seat_live(mgr, session.id, OWNER["email"], FakeWS(), files)

        await mgr._harvest_turn_artifacts(live)

        rows = fake_repo.list_for_session(session.id)
        assert [r["filename"] for r in rows] == ["report.docx"]
        assert fake_store.objects[f"agent-artifacts/{session.id}/report.docx"][0] == b"deliverable bytes"

    asyncio.run(_run())


def test_unchanged_outputs_are_not_re_read_on_the_next_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """Harvesting every turn is only affordable if an unchanged file costs one
    directory listing, never a re-read + re-upload of its bytes."""
    _stub_owner_lookup(monkeypatch)

    async def _run() -> None:
        mgr = _make_manager(tmp_path)
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        files = _FakeFiles({"report.docx": b"deliverable bytes"})
        live = _seat_live(mgr, session.id, OWNER["email"], FakeWS(), files)

        await mgr._harvest_turn_artifacts(live)
        await mgr._harvest_turn_artifacts(live)
        await mgr._harvest_turn_artifacts(live)

        assert len(files.read_calls) == 1, files.read_calls
        assert len(files.list_calls) == 3
        assert len(fake_repo.list_for_session(session.id)) == 1

        # A NEW file on a later turn is still picked up.
        files.write("chart.png", b"png")
        await mgr._harvest_turn_artifacts(live)
        assert {r["filename"] for r in fake_repo.list_for_session(session.id)} == {"report.docx", "chart.png"}
        assert len(files.read_calls) == 2

    asyncio.run(_run())


def test_a_session_at_its_cap_stops_harvesting_and_keeps_what_it_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """Overflow policy (owner decision): stop and log — never evict."""
    from app.chat.artifact_harvest import CHAT_ARTIFACT_MAX_FILES

    _stub_owner_lookup(monkeypatch)

    async def _run() -> None:
        mgr = _make_manager(tmp_path)
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        for i in range(CHAT_ARTIFACT_MAX_FILES):
            fake_repo.create(
                id=f"old{i}",
                session_id=session.id,
                agent_id=None,
                owner_user_id=OWNER["id"],
                filename=f"old{i}.txt",
                object_key=f"agent-artifacts/{session.id}/old{i}.txt",
                size_bytes=1,
                content_type=None,
                md5="m",
            )
        files = _FakeFiles({"one-more.docx": b"x"})
        live = _seat_live(mgr, session.id, OWNER["email"], FakeWS(), files)

        await mgr._harvest_turn_artifacts(live)

        rows = fake_repo.list_for_session(session.id)
        assert len(rows) == CHAT_ARTIFACT_MAX_FILES, "an over-cap harvest must not evict earlier artifacts"
        assert all(r["filename"].startswith("old") for r in rows)
        assert files.read_calls == [], "over the cap, nothing is even read"

    asyncio.run(_run())


def test_harvest_failure_never_breaks_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """`harvest_session_artifacts` promises it never raises into its caller;
    the new chat call path must keep that promise even when the pieces AROUND
    it (owner lookup, budget read) are the thing that blows up."""
    import app.chat.manager as mgr_mod

    def _boom():
        raise RuntimeError("system db is down")

    monkeypatch.setattr(mgr_mod, "users_repo", _boom)

    async def _run() -> None:
        mgr = _make_manager(tmp_path)
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        files = _FakeFiles({"report.docx": b"bytes"})
        live = _seat_live(mgr, session.id, OWNER["email"], FakeWS(), files)

        await mgr._harvest_turn_artifacts(live)  # must not raise

    asyncio.run(_run())


def test_the_done_frame_fires_the_harvest_and_the_turn_still_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """Wiring, driven through the real pump: the harvest hangs off the turn's
    ``done`` frame, and a harvest that explodes leaves the turn intact."""
    _stub_owner_lookup(monkeypatch)
    calls: list[str] = []

    async def _explode(*args, **kwargs):
        calls.append(args[0])
        raise RuntimeError("object store on fire")

    import app.chat.artifact_harvest as ah

    monkeypatch.setattr(ah, "harvest_session_artifacts", _explode)

    async def _run() -> None:
        mgr = _make_manager(tmp_path)
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        ws = FakeWS()
        live = _seat_live(mgr, session.id, OWNER["email"], ws, _FakeFiles({"report.docx": b"bytes"}))
        pump = asyncio.create_task(mgr._pump_subprocess_to_ws(live))
        live.handle.emit({"type": "done"})
        await _wait_until(lambda: any(f.get("type") == "done" for f in ws.sent))
        await _wait_until(lambda: calls == [session.id])
        assert calls == [session.id]
        # The pump is still alive and still delivering after the failed harvest.
        live.handle.emit({"type": "token", "text": "next turn"})
        await _wait_until(lambda: any(f.get("type") == "token" for f in ws.sent))
        assert any(f.get("type") == "token" for f in ws.sent)
        pump.cancel()
        try:
            await pump
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())


def test_a_turn_ending_mid_harvest_is_deferred_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """Two harvests must never run at once (both would list before either
    wrote its rows and duplicate every artifact) — but the turn whose harvest
    is skipped produced files the in-flight one cannot see, so it has to run
    again afterwards rather than wait for a next turn that may never come."""
    _stub_owner_lookup(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[int] = []

    async def _slow_harvest(*args, **kwargs):
        runs.append(1)
        started.set()
        await release.wait()
        return []

    import app.chat.artifact_harvest as ah

    monkeypatch.setattr(ah, "harvest_session_artifacts", _slow_harvest)

    async def _run() -> None:
        mgr = _make_manager(tmp_path)
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        live = _seat_live(mgr, session.id, OWNER["email"], FakeWS(), _FakeFiles({"a.txt": b"a"}))

        mgr._schedule_artifact_harvest(live)  # turn 1
        await started.wait()
        mgr._schedule_artifact_harvest(live)  # turn 2, while turn 1 is still in flight
        mgr._schedule_artifact_harvest(live)  # turn 3 — deferrals collapse
        assert runs == [1], "a second harvest must not start concurrently"

        release.set()
        await _wait_until(lambda: len(runs) >= 2)
        assert runs == [1, 1], "the deferred turn is harvested exactly once, not twice"
        await _wait_until(lambda: not mgr._harvest_tasks)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# kai-agent: the engine's sandbox file routes, shaped as a harvest handle
# ---------------------------------------------------------------------------


def test_engine_files_handle_speaks_the_sandbox_file_api() -> None:
    """The adapter the issue asks for: `handle.files.list/read` over the
    engine's own file routes, translating the sandbox-absolute path the
    harvest uses into the engine's workspace-relative one."""
    from app.chat.artifact_harvest import _entry_type
    from app.chat.kai_engine_files import EngineFilesHandle

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/sandbox/files"):
            assert request.url.params.get("path") == "outputs"
            return httpx.Response(
                200,
                json={
                    "entries": [
                        {"name": "report.docx", "path": "outputs/report.docx", "type": "file", "size": 5},
                        {"name": "scratch", "path": "outputs/scratch", "type": "dir"},
                    ]
                },
            )
        assert request.url.path.endswith("/sandbox/file/download")
        assert request.url.params.get("path") == "outputs/report.docx"
        return httpx.Response(200, content=b"bytes")

    async def _run() -> None:
        handle = EngineFilesHandle(
            base_url="http://engine.test",
            chat_id=CHAT_ID,
            token="jwt",
            max_read_bytes=1024,
            transport=httpx.MockTransport(handler),
        )
        entries = await handle.files.list("/work/outputs")
        assert [(e.name, _entry_type(e)) for e in entries] == [("report.docx", "FILE"), ("scratch", "DIR")]
        assert await handle.files.read("/work/outputs/report.docx", format="bytes") == b"bytes"
        assert all(r.headers["authorization"] == "Bearer jwt" for r in seen)

    asyncio.run(_run())


def test_engine_files_handle_raises_when_there_is_no_outputs_dir() -> None:
    """A 404 listing must raise, because that is how the harvest recognises
    "this run produced nothing" (it catches and returns [])."""
    from app.chat.kai_engine_files import EngineFilesHandle, EngineFilesUnavailable

    async def _run() -> None:
        handle = EngineFilesHandle(
            base_url="http://engine.test",
            chat_id=CHAT_ID,
            token="jwt",
            max_read_bytes=1024,
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
        )
        with pytest.raises((EngineFilesUnavailable, FileNotFoundError)):
            await handle.files.list("/work/outputs")

    asyncio.run(_run())


def test_an_engine_backed_session_harvests_through_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_store: _FakeStore, fake_repo: _FakeArtifactsRepo
) -> None:
    """Under ``provider: kai-agent`` there is no host handle with a file API —
    the harvest must go through the engine adapter instead of silently
    skipping exactly the provider the bug was reported on."""
    _stub_owner_lookup(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sandbox/files"):
            return httpx.Response(
                200,
                json={"entries": [{"name": "deck.pptx", "path": "outputs/deck.pptx", "type": "file", "size": 4}]},
            )
        return httpx.Response(200, content=b"deck")

    import app.chat.kai_engine_files as kef
    from app.api import kai

    monkeypatch.setattr(kai, "mint_engine_session_token", lambda email, chat_id: ("engine-jwt", 0))
    real_cls = kef.EngineFilesHandle
    monkeypatch.setattr(
        kef,
        "EngineFilesHandle",
        lambda **kw: real_cls(**{**kw, "transport": httpx.MockTransport(handler)}),
    )

    async def _run() -> None:
        mgr = _make_manager(tmp_path, provider="kai-agent")
        session = await mgr.create_session(user_email=OWNER["email"], surface=Surface.WEB)
        # The engine handle has NO file API at all — that is the point.
        live = _seat_live(mgr, session.id, OWNER["email"], FakeWS(), None)

        await mgr._harvest_turn_artifacts(live)

        rows = fake_repo.list_for_session(session.id)
        assert [r["filename"] for r in rows] == ["deck.pptx"]
        assert fake_store.objects[f"agent-artifacts/{session.id}/deck.pptx"][0] == b"deck"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The Files panel: harvested first, live sandbox as a fallback
# ---------------------------------------------------------------------------


_KAI_CONFIG = SimpleNamespace(provider="kai-agent", kai_agent_url="http://engine.test")


class _FakeChatRepo:
    def __init__(self, sessions: dict[str, str]):
        self._sessions = sessions

    def get_session(self, chat_id: str):
        email = self._sessions.get(chat_id)
        if email is None:
            return None
        return SimpleNamespace(id=chat_id, user_email=email)


def _files_app(
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    repo: _FakeArtifactsRepo,
    store: _FakeStore,
    engine_handler,
    owner_email: str = OWNER["email"],
    caller: dict | None = None,
) -> TestClient:
    os.environ["DATA_DIR"] = str(data_dir)

    import app.api.chat_session_files as mod
    from app.api import kai

    monkeypatch.setattr(mod, "_ENGINE_TRANSPORT", httpx.MockTransport(engine_handler))
    monkeypatch.setattr(mod, "agent_artifacts_repo", lambda: repo)
    monkeypatch.setattr(mod, "object_store", lambda: store)
    monkeypatch.setattr(kai, "mint_engine_session_token", lambda email, chat_id: ("jwt", 0))

    app = FastAPI()
    app.include_router(mod.router)
    app.state.chat_repo = _FakeChatRepo({CHAT_ID: owner_email})
    app.state.chat_config = _KAI_CONFIG
    app.dependency_overrides[mod.require_chat_access] = lambda: caller or OWNER
    return TestClient(app)


def _seed_artifact(repo: _FakeArtifactsRepo, store: _FakeStore, name: str, data: bytes, session_id=CHAT_ID) -> None:
    key = f"agent-artifacts/{session_id}/{name}"
    store.put_bytes(key, data, "md5")
    repo.create(
        id=f"art-{name}",
        session_id=session_id,
        agent_id=None,
        owner_user_id=OWNER["id"],
        filename=name,
        object_key=key,
        size_bytes=len(data),
        content_type="application/octet-stream",
        md5="md5",
    )


def _dead_engine(request: httpx.Request) -> httpx.Response:
    """Every engine route 404s — the sandbox this chat ran in is long gone."""
    return httpx.Response(404)


def test_a_harvested_file_is_still_listed_after_the_sandbox_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression test for #2268: the conversation produced a file, the
    sandbox is gone, and the Files panel must not say "No files here yet"."""
    repo, store = _FakeArtifactsRepo(), _FakeStore()
    _seed_artifact(repo, store, "report.docx", b"the deliverable")
    client = _files_app(tmp_path, monkeypatch, repo=repo, store=store, engine_handler=_dead_engine)

    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()

    assert [f["path"] for f in body["files"]] == ["outputs/report.docx"]
    assert body["files"][0]["size_bytes"] == len(b"the deliverable")
    assert body["supported"] is True


def test_a_harvested_file_still_downloads_after_the_sandbox_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, store = _FakeArtifactsRepo(), _FakeStore()
    _seed_artifact(repo, store, "report.docx", b"the deliverable")
    client = _files_app(tmp_path, monkeypatch, repo=repo, store=store, engine_handler=_dead_engine)

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs/report.docx"})

    assert resp.status_code == 200, resp.text
    assert resp.content == b"the deliverable"
    assert resp.headers["content-disposition"] == 'attachment; filename="report.docx"'
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_harvested_artifacts_are_session_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Another user's session never surfaces its artifacts — the 404 comes
    from the session-ownership check, before any artifact lookup."""
    repo, store = _FakeArtifactsRepo(), _FakeStore()
    _seed_artifact(repo, store, "report.docx", b"secret")
    client = _files_app(
        tmp_path,
        monkeypatch,
        repo=repo,
        store=store,
        engine_handler=_dead_engine,
        owner_email=OTHER["email"],
    )

    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files").status_code == 404
    assert (
        client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs/report.docx"}).status_code
        == 404
    )


def test_a_live_sandbox_still_wins_for_files_it_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The live sandbox is a fallback for the listing, not a replacement: a
    file the sandbox still holds keeps its live bytes, and files it never
    harvested (outside ``outputs/``) still show up."""
    repo, store = _FakeArtifactsRepo(), _FakeStore()
    _seed_artifact(repo, store, "report.docx", b"harvested copy")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sandbox/files"):
            path = request.url.params.get("path")
            if path == "outputs":
                return httpx.Response(
                    200,
                    json={
                        "entries": [{"name": "report.docx", "path": "outputs/report.docx", "type": "file", "size": 99}]
                    },
                )
            if not path:
                return httpx.Response(
                    200,
                    json={
                        "entries": [
                            {"name": "outputs", "path": "outputs", "type": "dir"},
                            {"name": "scratch.txt", "path": "scratch.txt", "type": "file", "size": 3},
                        ]
                    },
                )
            return httpx.Response(404)
        return httpx.Response(200, content=b"live bytes")

    client = _files_app(tmp_path, monkeypatch, repo=repo, store=store, engine_handler=handler)

    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files").json()
    assert {f["path"] for f in body["files"]} == {"outputs/report.docx", "scratch.txt"}
    # No duplicate row for the file that is BOTH live and harvested.
    assert len(body["files"]) == 2
    assert [f for f in body["files"] if f["path"] == "outputs/report.docx"][0]["size_bytes"] == 99

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs/report.docx"})
    assert resp.content == b"live bytes"


def test_an_unreachable_engine_degrades_to_harvested_instead_of_502(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine outage must not hide files Agnes already holds."""
    repo, store = _FakeArtifactsRepo(), _FakeStore()
    _seed_artifact(repo, store, "report.docx", b"harvested copy")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("engine down")

    client = _files_app(tmp_path, monkeypatch, repo=repo, store=store, engine_handler=handler)

    resp = client.get(f"/api/chat/sessions/{CHAT_ID}/files")
    assert resp.status_code == 200, resp.text
    assert [f["path"] for f in resp.json()["files"]] == ["outputs/report.docx"]

    dl = client.get(f"/api/chat/sessions/{CHAT_ID}/files/download", params={"path": "outputs/report.docx"})
    assert dl.status_code == 200
    assert dl.content == b"harvested copy"


def test_an_unreachable_engine_with_nothing_harvested_still_502s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No harvested copy = no honest answer to give; the operator-visible
    failure stays a 502 rather than an empty, reassuring listing."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("engine down")

    client = _files_app(tmp_path, monkeypatch, repo=_FakeArtifactsRepo(), store=_FakeStore(), engine_handler=handler)
    assert client.get(f"/api/chat/sessions/{CHAT_ID}/files").status_code == 502


def test_a_harvested_file_previews_after_the_sandbox_is_gone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, store = _FakeArtifactsRepo(), _FakeStore()
    _seed_artifact(repo, store, "notes.md", b"# heading\n\nbody")
    client = _files_app(tmp_path, monkeypatch, repo=repo, store=store, engine_handler=_dead_engine)

    body = client.get(f"/api/chat/sessions/{CHAT_ID}/files/preview", params={"path": "outputs/notes.md"}).json()
    assert body["kind"] == "text"
    assert "heading" in body["text"]
