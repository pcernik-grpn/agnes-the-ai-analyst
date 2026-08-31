"""Per-assistant-turn token extraction in the usage session processor.

Three concerns, all backend-independent (the PG-backed assertions — that the
turns actually land in ``usage_turns`` — live in
``tests/db_pg/test_usage_processor_turns_pg.py``, because the PG fixtures are
only available under ``tests/db_pg/``):

1. ``iter_turn_usage`` — the pure walk over parsed jsonl turns. Exact token
   values per turn, including the two cache counters the session summary has
   always aggregated but never attributed to a turn.
2. The processor on a **DuckDB** app-state instance. ``usage_turns`` is
   PG-only (A3 ratchet), so there is nothing to write to — the processor must
   still summarize the session, not crash on ``RequiresPostgresBackend``.
3. ``process_single_session`` — the one-file entry point the upload endpoint
   will call. It must never raise: it runs in a background task where an
   exception is invisible to the uploader.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _assistant(uuid: str, parent: str | None, *, ts: str | None, model: str = "claude-opus-5", usage: dict | None):
    msg: dict = {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}]}
    if usage is not None:
        msg["usage"] = usage
    event: dict = {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "sess-1",
        "message": msg,
    }
    if ts is not None:
        event["timestamp"] = ts
    return event


def _two_turn_session() -> list[dict]:
    """A minimal two-assistant-turn session with cache tokens on both turns."""
    return [
        {
            "type": "user",
            "uuid": "u-1",
            "parentUuid": None,
            "sessionId": "sess-1",
            "timestamp": "2026-08-31T10:00:00.000Z",
            "message": {"role": "user", "content": "hello"},
        },
        _assistant(
            "a-1",
            "u-1",
            ts="2026-08-31T10:00:05.000Z",
            usage={
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_input_tokens": 333,
                "cache_creation_input_tokens": 44,
            },
        ),
        {
            "type": "user",
            "uuid": "u-2",
            "parentUuid": "a-1",
            "sessionId": "sess-1",
            "timestamp": "2026-08-31T10:00:10.000Z",
            "message": {"role": "user", "content": "more"},
        },
        _assistant(
            "a-2",
            "u-2",
            ts="2026-08-31T10:00:15.000Z",
            model="claude-haiku-4",
            usage={
                "input_tokens": 5,
                "output_tokens": 6,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 8,
            },
        ),
    ]


def write_session(dir_path: Path, filename: str, events: list[dict]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / filename
    target.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return target


@pytest.fixture
def duckdb_instance(tmp_path, monkeypatch):
    """Fresh, fully-migrated DuckDB app state under a private DATA_DIR."""
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))

    import src.db as db_module

    db_module._system_db_conn = None
    db_module._system_db_path = None
    return db_module.get_system_db()


def _summary(conn, session_key: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM usage_session_summary WHERE session_file = ?",
        [session_key],
    ).fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in conn.description], row))


# ---------------------------------------------------------------------------
# 1. The pure walk
# ---------------------------------------------------------------------------


class TestIterTurnUsage:
    def test_one_record_per_assistant_turn_with_exact_token_values(self):
        from services.session_processors.usage_lib import iter_turn_usage

        turns = list(iter_turn_usage(_two_turn_session()))

        assert [t["turn_uuid"] for t in turns] == ["a-1", "a-2"]
        assert turns[0] == {
            "turn_uuid": "a-1",
            "parent_uuid": "u-1",
            "model": "claude-opus-5",
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_read_tokens": 333,
            "cache_creation_tokens": 44,
            "occurred_at": turns[0]["occurred_at"],
        }
        assert turns[0]["occurred_at"].isoformat() == "2026-08-31T10:00:05+00:00"
        assert turns[1]["model"] == "claude-haiku-4"
        assert (turns[1]["input_tokens"], turns[1]["output_tokens"]) == (5, 6)
        assert (turns[1]["cache_read_tokens"], turns[1]["cache_creation_tokens"]) == (7, 8)

    def test_the_walk_agrees_with_the_session_summary(self):
        """Per-turn rows must sum to exactly what the summary reports — the two
        readings of the same session are the same numbers."""
        from services.session_processors.usage_lib import compute_summary, iter_turn_usage

        events = _two_turn_session()
        summary = compute_summary(events, [])
        turns = list(iter_turn_usage(events))

        for column in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
            assert sum(t[column] for t in turns) == summary[column], column

    def test_an_assistant_event_without_usage_is_not_a_turn(self):
        """No usage block = nothing measured; a zero row would inflate the turn
        count and pretend a measurement happened."""
        from services.session_processors.usage_lib import iter_turn_usage

        events = [_assistant("a-1", None, ts="2026-08-31T10:00:00.000Z", usage=None)]

        assert list(iter_turn_usage(events)) == []

    def test_a_user_turn_is_never_a_usage_turn(self):
        from services.session_processors.usage_lib import iter_turn_usage

        events = [
            {
                "type": "user",
                "uuid": "u-1",
                "timestamp": "2026-08-31T10:00:00.000Z",
                "message": {"role": "user", "content": "hi", "usage": {"input_tokens": 99}},
            }
        ]

        assert list(iter_turn_usage(events)) == []

    def test_an_event_without_a_uuid_is_skipped(self):
        """``turn_uuid`` is half the idempotency key — a row without one could
        not be de-duplicated on re-process."""
        from services.session_processors.usage_lib import iter_turn_usage

        events = [_assistant("", None, ts="2026-08-31T10:00:00.000Z", usage={"input_tokens": 1})]

        assert list(iter_turn_usage(events)) == []

    def test_missing_columns_default_to_zero(self):
        """Pre-prompt-caching sessions carry no ``cache_*`` keys at all."""
        from services.session_processors.usage_lib import iter_turn_usage

        events = [_assistant("a-1", None, ts="2026-08-31T10:00:00.000Z", usage={"input_tokens": 3})]

        (turn,) = list(iter_turn_usage(events))
        assert turn["output_tokens"] == 0
        assert turn["cache_read_tokens"] == 0
        assert turn["cache_creation_tokens"] == 0

    def test_a_corrupt_timestamp_leaves_the_turn_untimed_rather_than_stamping_now(self):
        """A turn that cannot be placed in time must not be counted as "now" —
        that would drag an old backfilled session into today's window."""
        from services.session_processors.usage_lib import iter_turn_usage

        events = [_assistant("a-1", None, ts="not-a-timestamp", usage={"input_tokens": 1})]

        (turn,) = list(iter_turn_usage(events))
        assert turn["occurred_at"] is None


# ---------------------------------------------------------------------------
# 2. The processor on a DuckDB app-state instance
# ---------------------------------------------------------------------------


def test_duckdb_backend_still_summarizes_without_emitting_turns(duckdb_instance, tmp_path):
    """``usage_turns`` is PG-only, so on DuckDB there is nowhere to write the
    turns — the session summary (which is NOT PG-only) must still be written,
    and resolving the PG-only repo must not escape as a crash."""
    from services.session_processors.usage import UsageProcessor
    from src.repositories import RequiresPostgresBackend, usage_turns_repo

    with pytest.raises(RequiresPostgresBackend):
        usage_turns_repo()  # precondition: this is what the processor must avoid

    path = write_session(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())
    UsageProcessor().process_session(path, "alice@example.com", "u1/s1.jsonl", duckdb_instance, user_id="u1")

    row = _summary(duckdb_instance, "u1/s1.jsonl")
    assert row is not None
    assert row["input_tokens"] == 16
    assert row["output_tokens"] == 28
    assert row["cache_read_tokens"] == 340
    assert row["cache_creation_tokens"] == 52


# ---------------------------------------------------------------------------
# 3. process_single_session — the one-file entry point
# ---------------------------------------------------------------------------


class TestProcessSingleSession:
    def test_processes_one_file_end_to_end(self, duckdb_instance, tmp_path):
        from services.session_pipeline.runner import process_single_session

        write_session(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

        assert process_single_session("u1", "s1.jsonl") is True

        row = _summary(duckdb_instance, "u1/s1.jsonl")
        assert row is not None
        assert row["input_tokens"] == 16
        assert row["assistant_messages"] == 2

    def test_marks_the_file_processed_so_the_sweep_skips_it(self, duckdb_instance, tmp_path):
        """Same ``session_processor_state`` bookkeeping as the sweep loop —
        otherwise every one-shot would be redone on the next tick."""
        from services.session_pipeline.lib import compute_file_hash
        from services.session_pipeline.runner import process_single_session
        from src.repositories import session_processor_state_repo

        path = write_session(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())
        assert process_single_session("u1", "s1.jsonl") is True

        assert session_processor_state_repo().is_processed("usage", "u1/s1.jsonl", compute_file_hash(path))

    def test_resolves_the_uploading_user_identity(self, duckdb_instance, tmp_path):
        """The directory name is ``users.id``; the summary must carry the
        canonical email as ``username`` and the id as ``user_id``, exactly as
        the sweep loop writes them."""
        from services.session_pipeline.runner import process_single_session
        from src.repositories import users_repo

        user_id = "11111111-1111-1111-1111-111111111111"
        users_repo().create(id=user_id, email="alice@example.com", name="Alice")
        write_session(tmp_path / "user_sessions" / user_id, "s1.jsonl", _two_turn_session())

        assert process_single_session(user_id, "s1.jsonl") is True

        row = _summary(duckdb_instance, f"{user_id}/s1.jsonl")
        assert row["username"] == "alice@example.com"
        assert row["user_id"] == user_id

    def test_is_idempotent(self, duckdb_instance, tmp_path):
        from services.session_pipeline.runner import process_single_session

        write_session(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

        assert process_single_session("u1", "s1.jsonl") is True
        assert process_single_session("u1", "s1.jsonl") is True

        row = _summary(duckdb_instance, "u1/s1.jsonl")
        assert row["input_tokens"] == 16

    def test_a_missing_file_returns_false_instead_of_raising(self, duckdb_instance):
        """It runs as a fire-and-forget background task: a raise would surface
        nowhere and, worse, could take the caller's task group with it."""
        from services.session_pipeline.runner import process_single_session

        assert process_single_session("u1", "never-uploaded.jsonl") is False

    def test_a_corrupt_file_returns_false_instead_of_raising(self, duckdb_instance, tmp_path, monkeypatch):
        from services.session_pipeline import runner

        write_session(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())
        monkeypatch.setattr(
            runner,
            "compute_file_hash",
            lambda _p: (_ for _ in ()).throw(OSError("disk went away mid-read")),
        )

        assert runner.process_single_session("u1", "s1.jsonl") is False

    def test_a_traversing_filename_is_refused(self, duckdb_instance, tmp_path):
        """``dir_name``/``filename`` reach this function from an HTTP handler;
        neither may be able to point it outside the session root."""
        from services.session_pipeline.runner import process_single_session

        outside = write_session(tmp_path, "escaped.jsonl", _two_turn_session())
        assert outside.exists()

        assert process_single_session("u1", "../escaped.jsonl") is False
        assert process_single_session("../", "escaped.jsonl") is False
