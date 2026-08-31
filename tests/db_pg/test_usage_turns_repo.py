"""PG-side tests for ``UsageTurnsPgRepository`` — per-assistant-turn token rows.

PG-ONLY by construction (A3 PG-first ratchet — CLAUDE.md -> "Dual-backend
discipline"): ``usage_turns`` is a brand-new app-state table, so there is no
DuckDB sibling to parametrize against and no cross-engine contract test to
write. Shaped after ``tests/db_pg/test_resource_source_tags_pg.py`` — alembic
upgrade head, then drive the repository directly.

The load-bearing property here is IDEMPOTENCE: the usage processor re-walks a
session file every time its hash changes and the chat manager may retry a
persist, so the same ``(session_file, turn_uuid)`` arrives more than once.
Without the unique key + ``ON CONFLICT DO NOTHING`` every re-process would
double-count a user's tokens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def repo(pg_engine_with_schema):
    """Module-scoped ``alembic upgrade head`` (conftest), not per-test.

    Replaying the whole revision chain for each of this file's tests costs
    ~4 s apiece and pushed the per-item pytest-timeout over its 60 s budget
    under xdist. The autouse ``_truncate_pg_user_tables`` fixture still gives
    every test an empty ``usage_turns``, so isolation is unchanged.
    """
    from src.repositories.usage_turns_pg import UsageTurnsPgRepository

    return UsageTurnsPgRepository(pg_engine_with_schema)


def _turn(**over):
    row = {
        "session_file": "u1/s1.jsonl",
        "session_id": "s1",
        "user_id": "u1",
        "surface": "claude_code",
        "turn_uuid": "t1",
        "parent_uuid": None,
        "model": "claude-opus-5",
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_tokens": 300,
        "cache_creation_tokens": 40,
        "occurred_at": datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc),
        "processor_version": 10,
    }
    row.update(over)
    return row


def test_insert_batch_returns_the_number_of_rows_stored(repo):
    assert repo.insert_batch([_turn(turn_uuid="t1"), _turn(turn_uuid="t2")]) == 2
    assert len(repo.list_for_session_file("u1/s1.jsonl")) == 2


def test_reinserting_the_same_turn_stores_nothing_new(repo):
    """Re-processing a session file must not double-count its tokens."""
    repo.insert_batch([_turn(turn_uuid="t1"), _turn(turn_uuid="t2")])

    assert repo.insert_batch([_turn(turn_uuid="t1"), _turn(turn_uuid="t2")]) == 0
    rows = repo.list_for_session_file("u1/s1.jsonl")
    assert len(rows) == 2
    totals = repo.totals_for_user("u1", None)
    assert totals["input_tokens"] == 200
    assert totals["output_tokens"] == 400


def test_a_mixed_rerun_stores_only_the_turns_that_are_new(repo):
    """The realistic re-process: a session grew by one turn since last tick."""
    repo.insert_batch([_turn(turn_uuid="t1")])

    assert repo.insert_batch([_turn(turn_uuid="t1"), _turn(turn_uuid="t2")]) == 1
    assert {r["turn_uuid"] for r in repo.list_for_session_file("u1/s1.jsonl")} == {"t1", "t2"}


def test_the_same_turn_uuid_in_a_different_session_file_is_a_different_turn(repo):
    """The unique key is the PAIR — chat surfaces mint their own uuids and two
    files may legitimately collide on one."""
    repo.insert_batch([_turn(turn_uuid="t1"), _turn(session_file="u1/s2.jsonl", session_id="s2", turn_uuid="t1")])

    assert len(repo.list_for_session_file("u1/s1.jsonl")) == 1
    assert len(repo.list_for_session_file("u1/s2.jsonl")) == 1


def test_an_empty_batch_is_a_no_op(repo):
    assert repo.insert_batch([]) == 0


def test_omitted_columns_fall_back_to_their_defaults(repo):
    """A caller that only knows the identity of a turn still writes a legal
    row: tokens default to 0 and the surface to Claude Code."""
    assert repo.insert_batch([{"session_file": "u2/s9.jsonl", "turn_uuid": "t9"}]) == 1

    (row,) = repo.list_for_session_file("u2/s9.jsonl")
    assert row["surface"] == "claude_code"
    assert row["input_tokens"] == 0
    assert row["cache_read_tokens"] == 0
    assert row["processor_version"] == 0
    assert row["model"] is None
    assert row["occurred_at"] is None
    assert row["id"]


def test_list_for_session_file_is_scoped_and_time_ordered(repo):
    base = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    repo.insert_batch(
        [
            _turn(turn_uuid="second", occurred_at=base + timedelta(minutes=5)),
            _turn(turn_uuid="first", occurred_at=base),
            _turn(session_file="u1/other.jsonl", turn_uuid="elsewhere"),
        ]
    )

    rows = repo.list_for_session_file("u1/s1.jsonl")
    assert [r["turn_uuid"] for r in rows] == ["first", "second"]
    assert repo.list_for_session_file("u1/nope.jsonl") == []


def test_occurred_at_accepts_an_iso_string(repo):
    """The Claude Code processor reads the jsonl event ``timestamp`` verbatim —
    an ISO-8601 string, including the trailing ``Z`` form.

    Compared as an instant, not as text: the column is ``timestamptz``, so
    the value comes back rendered in the connection's timezone (a run in
    UTC+2 reads ``12:00+02:00``). Same moment, different rendering — asserting
    on the string would fail depending on where the test runs."""
    repo.insert_batch([_turn(occurred_at="2026-08-31T10:00:00Z")])

    (row,) = repo.list_for_session_file("u1/s1.jsonl")
    assert datetime.fromisoformat(row["occurred_at"]) == datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def test_cache_totals_for_session_file_sums_both_cache_columns(repo):
    repo.insert_batch(
        [
            _turn(turn_uuid="t1", cache_read_tokens=300, cache_creation_tokens=40),
            _turn(turn_uuid="t2", cache_read_tokens=7, cache_creation_tokens=3),
            _turn(session_file="u1/other.jsonl", turn_uuid="t3", cache_read_tokens=999, cache_creation_tokens=999),
        ]
    )

    assert repo.cache_totals_for_session_file("u1/s1.jsonl") == {
        "cache_read_tokens": 307,
        "cache_creation_tokens": 43,
    }


def test_cache_totals_for_an_unknown_session_file_are_zero_not_none(repo):
    """The chat-summary overlay reads this for every session — a missing file
    must be a measured zero, never a ``None`` that poisons an addition."""
    assert repo.cache_totals_for_session_file("nobody/none.jsonl") == {
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


def test_totals_for_user_groups_by_model(repo):
    repo.insert_batch(
        [
            _turn(turn_uuid="t1", model="claude-opus-5", input_tokens=10, output_tokens=1),
            _turn(turn_uuid="t2", model="claude-opus-5", input_tokens=5, output_tokens=2),
            _turn(turn_uuid="t3", model="claude-haiku-4", input_tokens=1, output_tokens=1),
            _turn(turn_uuid="t4", user_id="other", model="claude-opus-5", input_tokens=999, output_tokens=999),
        ]
    )

    totals = repo.totals_for_user("u1", None)
    assert totals["input_tokens"] == 16
    assert totals["output_tokens"] == 4
    by_model = {r["model"]: r for r in totals["by_model"]}
    assert set(by_model) == {"claude-opus-5", "claude-haiku-4"}
    assert by_model["claude-opus-5"]["input_tokens"] == 15
    assert by_model["claude-opus-5"]["turns"] == 2
    assert by_model["claude-haiku-4"]["input_tokens"] == 1


def test_totals_for_a_user_with_no_turns_are_zero(repo):
    totals = repo.totals_for_user("nobody", None)
    assert totals == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "turns": 0,
        "by_model": [],
    }


def test_totals_for_user_none_is_instance_wide(repo):
    repo.insert_batch(
        [
            _turn(turn_uuid="t1", user_id="u1", input_tokens=10),
            _turn(turn_uuid="t2", user_id="other", input_tokens=5),
            _turn(turn_uuid="t3", user_id=None, input_tokens=1),
        ]
    )

    assert repo.totals_for_user(None, None)["input_tokens"] == 16


def test_since_days_windows_on_occurred_at(repo):
    now = datetime.now(timezone.utc)
    repo.insert_batch(
        [
            _turn(turn_uuid="recent", occurred_at=now - timedelta(days=1), input_tokens=10),
            _turn(turn_uuid="old", occurred_at=now - timedelta(days=40), input_tokens=5),
        ]
    )

    assert repo.totals_for_user("u1", 7)["input_tokens"] == 10
    assert repo.totals_for_user("u1", None)["input_tokens"] == 15


def test_a_windowed_read_drops_turns_with_no_timestamp(repo):
    """A row with no ``occurred_at`` cannot be placed in time, so a windowed
    read must not silently count it as "now" — it only shows up in the
    all-time read."""
    now = datetime.now(timezone.utc)
    repo.insert_batch(
        [
            _turn(turn_uuid="timed", occurred_at=now - timedelta(days=1), input_tokens=10),
            _turn(turn_uuid="untimed", occurred_at=None, input_tokens=5),
        ]
    )

    assert repo.totals_for_user("u1", 7)["input_tokens"] == 10
    assert repo.totals_for_user("u1", None)["input_tokens"] == 15


def test_turns_with_no_model_still_count_in_the_totals(repo):
    """An unattributed turn must not vanish from the headline numbers just
    because the per-model breakdown has nowhere tidy to put it."""
    repo.insert_batch([_turn(turn_uuid="t1", model=None, input_tokens=7)])

    totals = repo.totals_for_user("u1", None)
    assert totals["input_tokens"] == 7
    assert [r["model"] for r in totals["by_model"]] == [None]
