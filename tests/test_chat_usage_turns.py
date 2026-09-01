"""Chat turns are recorded in ``usage_turns`` as they happen.

A chat turn's prompt-cache figures had nowhere to live: ``chat_messages``
cannot grow a column (the DuckDB app-state ladder is frozen under A3), and the
session jsonl is only exported when the session ends, so the usage processor
saw a chat's cost hours late or never. The per-turn table is the home for
them, written at the manager's single frame-persist seam — the one place every
chat surface (web, Slack, Telegram, agent API) funnels through.

Two properties matter as much as the write itself:

* the row must carry all four token kinds and the *originating surface*, since
  "which surface burns the cache" is the question the table exists to answer;
* the write must be unable to harm a turn. Telemetry that is down, a
  Postgres-only repository resolved on a DuckDB instance, or any other failure
  must leave the assistant message persisted and the turn cleanly finished.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import duckdb
import pytest

from app.chat.config import ChatConfig
from app.chat.manager import ChatManager, LiveSession, SinkEntry
from app.chat.persistence import ChatRepository
from app.chat.turn_usage import add_turn_usage, drain_turn_usage
from app.chat.types import SessionState, Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests
from src.db import _ensure_schema
from src.repositories import RequiresPostgresBackend
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
        # The native provider keeps `chat_<hex>` ids and the runner-frame
        # path this suite drives.
        config=ChatConfig(enabled=True, concurrency_per_user=2, provider="docker"),
    )


class _RecordingTurnsRepo:
    """Stand-in for the PG-only ``usage_turns`` repository."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_batch(self, rows) -> int:
        materialized = [dict(r) for r in rows]
        self.rows.extend(materialized)
        return len(materialized)


class _RaisingTurnsRepo:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def insert_batch(self, rows) -> int:
        raise self._exc


class _FakeUsers:
    def __init__(self, by_email: dict) -> None:
        self._by_email = by_email

    def get_by_email(self, email: str):
        return self._by_email.get(email)


def _use_postgres(monkeypatch, turns_repo, *, users=None) -> None:
    """Pretend the instance runs the Postgres backend and hand the manager
    the given ``usage_turns`` repository."""
    monkeypatch.setattr("app.chat.manager.use_pg", lambda: True)
    monkeypatch.setattr("app.chat.manager.usage_turns_repo", lambda: turns_repo)
    monkeypatch.setattr(
        "app.chat.manager.users_repo",
        lambda: _FakeUsers(users if users is not None else {"u@x": {"id": "user-1"}}),
    )


def _attach_live(mgr: ChatManager, chat_id: str, user_email: str, sink, *, surface: str = Surface.WEB.value):
    """Insert a LiveSession backed by a FakeHandle (emit/readline) + one sink."""
    from datetime import timezone

    handle = FakeHandle()
    live = LiveSession(
        chat_id=chat_id,
        user_email=user_email,
        state=SessionState.ACTIVE,
        handle=handle,
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
        surface=surface,
        sinks=[SinkEntry(participant_email=user_email, sink=sink)],
    )
    mgr._live[chat_id] = live
    return live


async def _pump_one_turn(manager: ChatManager, live: LiveSession, frame: dict) -> None:
    """Drive a single assistant turn through the manager's frame loop."""
    pump_task = asyncio.create_task(manager._pump_subprocess_to_ws(live))
    live.handle.emit(frame)
    await _wait_until(lambda: not live.turn_in_flight)
    pump_task.cancel()
    try:
        await pump_task
    except asyncio.CancelledError:
        pass


_FULL_FRAME = {
    "type": "assistant_message",
    "content": "Hi",
    "tokens_in": 11,
    "tokens_out": 22,
    "cache_read_tokens": 3333,
    "cache_creation_tokens": 44,
    "model": "claude-sonnet-5",
}


def test_assistant_turn_writes_one_usage_turn_row(manager: ChatManager, monkeypatch):
    """All four token kinds land on exactly one row, keyed on the chat's own
    session file and stamped with the surface it happened on."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert len(turns.rows) == 1, f"expected exactly one turn row, got {turns.rows}"
        row = turns.rows[0]
        assert row["session_file"] == f"chat-{s.id}.jsonl"
        assert row["session_id"] == s.id
        assert row["surface"] == Surface.WEB.value
        assert row["user_id"] == "user-1"
        assert row["model"] == "claude-sonnet-5"
        assert row["input_tokens"] == 11
        assert row["output_tokens"] == 22
        assert row["cache_read_tokens"] == 3333
        assert row["cache_creation_tokens"] == 44
        assert row["turn_uuid"], "a turn row must carry a turn_uuid (the idempotency key)"
        assert isinstance(row["occurred_at"], datetime)

    asyncio.run(_run())


def test_turn_row_carries_the_originating_surface(manager: ChatManager, monkeypatch):
    """Slack/Telegram turns take the same frame path — only the recorded
    surface differs, which is what makes a per-surface cost split possible at
    read time."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(
            user_email="u@x",
            surface=Surface.SLACK_DM,
            slack_channel_id="C1",
        )
        live = _attach_live(manager, s.id, "u@x", FakeWS(), surface=Surface.SLACK_DM.value)
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert [r["surface"] for r in turns.rows] == [Surface.SLACK_DM.value]

    asyncio.run(_run())


def test_unresolved_identity_still_records_the_turn(manager: ChatManager, monkeypatch):
    """A turn whose sender has no ``users`` row is still measurable — the row
    is written with a NULL user_id rather than dropped."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns, users={})

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert len(turns.rows) == 1
        assert turns.rows[0]["user_id"] is None

    asyncio.run(_run())


def test_turn_survives_a_broken_usage_turns_repo(manager: ChatManager, monkeypatch, caplog):
    """Telemetry being down must not cost the user their answer: the message
    is persisted, the turn finishes, nothing escapes the frame loop."""
    _use_postgres(monkeypatch, _RaisingTurnsRepo(RuntimeError("pg is gone")))

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        with caplog.at_level(logging.WARNING):
            await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        messages = manager._repo.list_messages(s.id)
        assistant = [m for m in messages if m.role == "assistant"]
        assert assistant and assistant[-1].content == "Hi", "the answer must be persisted anyway"
        assert not live.turn_in_flight, "the turn must complete despite the telemetry failure"
        assert any("usage_turns" in r.message for r in caplog.records), (
            "a swallowed telemetry failure must still be visible in the log"
        )

    asyncio.run(_run())


def test_duckdb_backend_skips_the_write_silently(manager: ChatManager, monkeypatch, caplog):
    """On a DuckDB app-state instance ``usage_turns`` does not exist (PG-only
    by construction, A3). That is an expected configuration, not a fault: no
    row, no warning, no impact on the turn."""
    calls: list[str] = []

    def _repo_raises():
        calls.append("resolved")
        raise RequiresPostgresBackend("usage_turns")

    monkeypatch.setattr("app.chat.manager.use_pg", lambda: False)
    monkeypatch.setattr("app.chat.manager.usage_turns_repo", _repo_raises)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        with caplog.at_level(logging.WARNING):
            await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert calls == [], "the PG-only repo must not even be resolved on DuckDB"
        assert not any("usage_turns" in r.message for r in caplog.records), (
            "a DuckDB instance must not warn about a table it is not expected to have"
        )
        assert not live.turn_in_flight

    asyncio.run(_run())


def test_requires_postgres_backend_is_never_fatal(manager: ChatManager, monkeypatch, caplog):
    """Belt and braces for a backend flip mid-process: the typed PG-only error
    is caught at the write site, silently, and the turn is unaffected."""
    _use_postgres(monkeypatch, _RaisingTurnsRepo(RequiresPostgresBackend("usage_turns")))

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        with caplog.at_level(logging.WARNING):
            await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert not live.turn_in_flight
        assert not any("usage_turns" in r.message for r in caplog.records)

    asyncio.run(_run())


def test_frame_without_usage_records_nothing(manager: ChatManager, monkeypatch):
    """A usage-less frame with NOTHING brokered either (no turn counters)
    still records nothing — zeros would state a measurement nobody made.
    (When the broker DID observe the turn, hydration fills the frame — see
    test_engine_frame_is_hydrated_from_broker_counters.)"""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, {"type": "assistant_message", "content": "Hi"})

        assert turns.rows == []

    asyncio.run(_run())


_ENGINE_FRAME = {"type": "assistant_message", "content": "Hi"}  # engine provider: no usage fields at all


def test_engine_frame_is_hydrated_from_broker_counters(manager: ChatManager, monkeypatch):
    """The whole feature, end to end at the manager seam: a usage-less frame
    + broker-fed counters → one fully-populated usage_turns row, tokens on
    the persisted message (what max_session_tokens sums), and the daily
    spend counters fed (cache-write folded into the in-bucket)."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        add_turn_usage(
            s.id,
            {
                "model": "claude-sonnet-5",
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_tokens": 3333,
                "cache_creation_tokens": 44,
            },
        )
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_ENGINE_FRAME))

        assert len(turns.rows) == 1, f"expected one hydrated turn row, got {turns.rows}"
        row = turns.rows[0]
        assert row["model"] == "claude-sonnet-5"
        assert row["input_tokens"] == 11
        assert row["output_tokens"] == 22
        assert row["cache_read_tokens"] == 3333
        assert row["cache_creation_tokens"] == 44
        assert row["surface"] == Surface.WEB.value

        messages = manager._repo.list_messages(s.id)
        assistant = [m for m in messages if m.role == "assistant"][-1]
        assert assistant.tokens_in == 11 and assistant.tokens_out == 22
        assert assistant.model == "claude-sonnet-5"

        assert manager._daily_token_totals("u@x") == (11 + 44, 22)
        assert drain_turn_usage(s.id) is None, "hydration must consume the counters"

    asyncio.run(_run())


def test_frame_usage_wins_and_counters_are_discarded(manager: ChatManager, monkeypatch):
    """The double-count guard: native-sandbox calls ride the same broker
    route, so counters accumulate there too — but a frame that carries its
    own usage records the FRAME's numbers, and the drained counters are
    thrown away so they cannot leak into the next turn."""
    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        add_turn_usage(s.id, {"model": "some-other-model", "input_tokens": 999, "output_tokens": 999})
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_FULL_FRAME))

        assert len(turns.rows) == 1
        row = turns.rows[0]
        assert row["input_tokens"] == 11, "the frame's own numbers must win"
        assert row["model"] == "claude-sonnet-5"
        assert drain_turn_usage(s.id) is None, "discarded counters must not leak into the next turn"

    asyncio.run(_run())


def test_coordination_down_leaves_engine_frame_unrecorded(manager: ChatManager, monkeypatch):
    """Hydration is telemetry: with the coordination backend down, the turn
    behaves exactly as before this feature — message persisted, no row, no
    exception escaping the frame loop."""
    from app.coordination.base import CoordinationUnavailable

    turns = _RecordingTurnsRepo()
    _use_postgres(monkeypatch, turns)

    class _Down:
        def __getattr__(self, name):
            raise CoordinationUnavailable("down")

    monkeypatch.setattr("app.chat.turn_usage.coordination", lambda: _Down())

    async def _run():
        s = await manager.create_session(user_email="u@x", surface=Surface.WEB)
        live = _attach_live(manager, s.id, "u@x", FakeWS())
        await manager.send_user_message(s.id, "hello")
        await _pump_one_turn(manager, live, dict(_ENGINE_FRAME))

        assert turns.rows == []
        messages = manager._repo.list_messages(s.id)
        assert [m.content for m in messages if m.role == "assistant"] == ["Hi"]
        assert not live.turn_in_flight

    asyncio.run(_run())
