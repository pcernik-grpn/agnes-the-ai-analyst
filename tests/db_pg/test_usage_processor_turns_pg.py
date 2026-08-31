"""The usage session processor writing per-turn rows — Postgres backend.

``usage_turns`` is PG-only (A3 PG-first ratchet), so this is where the
processor's turn emission can actually be observed. The DuckDB half of the
contract — the processor still summarizes, without turns and without a crash —
lives in ``tests/test_usage_processor_turns.py`` (PG fixtures exist only under
this directory).

Two properties carry their weight here:

* **Idempotence.** The sweep re-walks a session file whenever its hash changes
  and the upload endpoint processes the same file one-shot; a second pass must
  add nothing, or every re-process would double a user's token bill.
* **Chat files are not the processor's to emit.** ``chat-*.jsonl`` turns are
  written live at message persist; the processor would mint a second,
  differently-keyed copy of every one of them. It overlays the summary's cache
  totals from the live rows instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

pytestmark = pytest.mark.integration


def _assistant(uuid: str, parent: str | None, ts: str, model: str, usage: dict) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": "sess-1",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "ok"}],
            "usage": usage,
        },
    }


def _two_turn_session() -> list[dict]:
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
            "2026-08-31T10:00:05.000Z",
            "claude-opus-5",
            {
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_read_input_tokens": 333,
                "cache_creation_input_tokens": 44,
            },
        ),
        _assistant(
            "a-2",
            "a-1",
            "2026-08-31T10:00:15.000Z",
            "claude-haiku-4",
            {
                "input_tokens": 5,
                "output_tokens": 6,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 8,
            },
        ),
    ]


def _write(dir_path: Path, filename: str, events: list[dict]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / filename
    target.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return target


@pytest.fixture
def pg_instance(pg_engine_with_schema, tmp_path, monkeypatch):
    """Point the repository factory at the module-scoped Postgres schema.

    Mirrors ``state_backend``'s pg branch without paying for a per-test
    ``alembic upgrade head``; the autouse truncate fixture still hands every
    test empty tables.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_DATA_DIR", str(tmp_path / "user_sessions"))
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", pg_engine_with_schema.url.render_as_string(hide_password=False))

    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories import use_pg

    assert use_pg(), "fixture failed to select the Postgres backend"
    yield pg_engine_with_schema
    db_pg.dispose()


def _turns(session_file: str) -> list[dict]:
    from src.repositories import usage_turns_repo

    return usage_turns_repo().list_for_session_file(session_file)


def _summary(engine, session_file: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                sa.text("SELECT * FROM usage_session_summary WHERE session_file = :sf"),
                {"sf": session_file},
            )
            .mappings()
            .first()
        )
    return dict(row) if row else None


def _process(path: Path, session_key: str, user_id: str = "u1") -> None:
    from services.session_processors.usage import UsageProcessor

    UsageProcessor().process_session(path, "alice@example.com", session_key, None, user_id=user_id)


# ---------------------------------------------------------------------------
# Claude Code sessions
# ---------------------------------------------------------------------------


def test_one_turn_row_per_assistant_event_with_exact_token_values(pg_instance, tmp_path):
    path = _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

    _process(path, "u1/s1.jsonl")

    rows = _turns("u1/s1.jsonl")
    assert [r["turn_uuid"] for r in rows] == ["a-1", "a-2"]
    first, second = rows
    assert (first["input_tokens"], first["output_tokens"]) == (11, 22)
    assert (first["cache_read_tokens"], first["cache_creation_tokens"]) == (333, 44)
    assert first["model"] == "claude-opus-5"
    assert first["parent_uuid"] == "u-1"
    assert first["occurred_at"].startswith("2026-08-31T")
    assert (second["input_tokens"], second["cache_read_tokens"]) == (5, 7)
    assert second["model"] == "claude-haiku-4"


def test_turn_rows_carry_the_session_identity_and_surface(pg_instance, tmp_path):
    path = _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

    _process(path, "u1/s1.jsonl", user_id="user-42")

    rows = _turns("u1/s1.jsonl")
    assert len(rows) == 2
    for row in rows:
        assert row["session_file"] == "u1/s1.jsonl"
        assert row["session_id"] == "sess-1"
        assert row["user_id"] == "user-42"
        assert row["surface"] == "claude_code"


def test_reprocessing_the_same_session_adds_no_turns(pg_instance, tmp_path):
    path = _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())
    _process(path, "u1/s1.jsonl")

    _process(path, "u1/s1.jsonl")

    from src.repositories import usage_turns_repo

    assert len(_turns("u1/s1.jsonl")) == 2
    assert usage_turns_repo().totals_for_user("u1", None)["input_tokens"] == 16


def test_a_grown_session_contributes_only_its_new_turns(pg_instance, tmp_path):
    """The realistic re-process: Claude Code appended to an open session."""
    events = _two_turn_session()
    path = _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", events[:2])
    _process(path, "u1/s1.jsonl")

    _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", events)
    _process(path, "u1/s1.jsonl")

    assert [r["turn_uuid"] for r in _turns("u1/s1.jsonl")] == ["a-1", "a-2"]


def test_turns_and_summary_report_the_same_tokens(pg_instance, tmp_path):
    path = _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

    _process(path, "u1/s1.jsonl")

    summary = _summary(pg_instance, "u1/s1.jsonl")
    rows = _turns("u1/s1.jsonl")
    for column in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"):
        assert sum(r[column] for r in rows) == summary[column], column


# ---------------------------------------------------------------------------
# Chat exports — turns are written live, not by this processor
# ---------------------------------------------------------------------------


def test_a_chat_export_emits_no_turns(pg_instance, tmp_path):
    """``chat-*.jsonl`` is a rendering of rows the chat manager already wrote
    live; emitting here would duplicate every one of them under a new key."""
    path = _write(tmp_path / "user_sessions" / "u1", "chat-abc123.jsonl", _two_turn_session())

    _process(path, "u1/chat-abc123.jsonl")

    assert _turns("u1/chat-abc123.jsonl") == []
    assert _summary(pg_instance, "u1/chat-abc123.jsonl") is not None


def test_a_chat_summary_takes_its_cache_totals_from_the_live_turns(pg_instance, tmp_path):
    """The chat export carries no cache counters, so the summary would read
    zero; the live turn rows are the only place those tokens exist.

    Note the two key shapes this crosses: the chat manager stores a turn under
    the BARE ``chat-<chat_id>.jsonl``, while the pipeline keys the session
    ``<dir_name>/<filename>``. Looking the totals up under the pipeline's key
    finds nothing — and the failure is silent, a summary of zeros — so the
    normalization is asserted here rather than assumed.
    """
    from src.repositories import usage_turns_repo

    usage_turns_repo().insert_batch(
        [
            {
                "session_file": "chat-abc123.jsonl",  # bare, as the chat manager writes it
                "turn_uuid": "live-1",
                "surface": "web",
                "cache_read_tokens": 900,
                "cache_creation_tokens": 90,
            }
        ]
    )
    stripped = _two_turn_session()
    for event in stripped:
        usage = (event.get("message") or {}).get("usage")
        if usage:
            usage.pop("cache_read_input_tokens", None)
            usage.pop("cache_creation_input_tokens", None)
    path = _write(tmp_path / "user_sessions" / "u1", "chat-abc123.jsonl", stripped)

    _process(path, "u1/chat-abc123.jsonl")

    summary = _summary(pg_instance, "u1/chat-abc123.jsonl")
    assert summary["cache_read_tokens"] == 900
    assert summary["cache_creation_tokens"] == 90
    # The live row is still the only turn: the processor added none of its own,
    # and it stayed under the key the chat manager wrote it with.
    assert len(_turns("chat-abc123.jsonl")) == 1
    assert _turns("u1/chat-abc123.jsonl") == []


def test_the_chat_overlay_matches_live_turns_by_basename(pg_instance, tmp_path):
    """Regression pin for the key-shape mismatch, isolated: a live turn stored
    under the bare ``chat-x.jsonl`` must be found from the pipeline's
    ``<dir>/chat-x.jsonl`` session key, and its totals are authoritative — they
    replace whatever the export happened to carry."""
    from src.repositories import usage_turns_repo

    usage_turns_repo().insert_batch([{"session_file": "chat-xyz.jsonl", "turn_uuid": "live-1", "cache_read_tokens": 5}])
    path = _write(tmp_path / "user_sessions" / "u9", "chat-xyz.jsonl", _two_turn_session())

    _process(path, "u9/chat-xyz.jsonl")

    summary = _summary(pg_instance, "u9/chat-xyz.jsonl")
    assert summary["cache_read_tokens"] == 5, "overlay did not match the live turn by basename"
    assert summary["cache_creation_tokens"] == 0


def test_a_chat_summary_without_live_turns_keeps_its_own_numbers(pg_instance, tmp_path):
    """Until the chat surfaces write turns, the overlay must be a no-op — not a
    zeroing of whatever the export did carry."""
    path = _write(tmp_path / "user_sessions" / "u1", "chat-abc123.jsonl", _two_turn_session())

    _process(path, "u1/chat-abc123.jsonl")

    summary = _summary(pg_instance, "u1/chat-abc123.jsonl")
    assert summary["cache_read_tokens"] == 340
    assert summary["cache_creation_tokens"] == 52


# ---------------------------------------------------------------------------
# The one-file entry point
# ---------------------------------------------------------------------------


def test_process_single_session_writes_turns(pg_instance, tmp_path):
    from services.session_pipeline.runner import process_single_session

    _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

    assert process_single_session("u1", "s1.jsonl") is True

    assert [r["turn_uuid"] for r in _turns("u1/s1.jsonl")] == ["a-1", "a-2"]
    assert _summary(pg_instance, "u1/s1.jsonl") is not None


def test_process_single_session_run_twice_writes_each_turn_once(pg_instance, tmp_path):
    from services.session_pipeline.runner import process_single_session

    _write(tmp_path / "user_sessions" / "u1", "s1.jsonl", _two_turn_session())

    assert process_single_session("u1", "s1.jsonl") is True
    assert process_single_session("u1", "s1.jsonl") is True

    assert len(_turns("u1/s1.jsonl")) == 2
