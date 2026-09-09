"""The turn record over the coordination backend (spec 3.2).

The broker parents its completion span under the chat turn that caused it
without the engine propagating anything: ChatManager publishes the turn's
span context under ``chat:turn:{session_id}`` and the broker reads it. This
module is that seam, and — like every other piece of the instrumentation —
it degrades rather than fails: an outage costs the linkage, never the turn.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.chat.turn_context import (
    TURN_TTL_SECONDS,
    TurnRecord,
    publish_turn,
    read_turn,
    started_no_later_than,
    turn_key,
    workload_for_surface,
)
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


def _rec(**kw):
    base = {
        "turn_id": "t1",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "started_at": "2026-09-08T10:00:00+00:00",
        "user_id": "u1",
        "agent_id": None,
        "surface": "web",
        "workload": "chat",
        "message_id": "msg_1",
    }
    base.update(kw)
    return TurnRecord(**base)


def test_key_and_ttl_follow_the_spec():
    assert turn_key("s1") == "chat:turn:s1"
    assert TURN_TTL_SECONDS == 24 * 3600


def test_publish_then_read_round_trips_and_the_next_turn_overwrites():
    publish_turn("s1", _rec())
    assert read_turn("s1") == _rec()
    publish_turn("s1", _rec(turn_id="t2"))
    second = read_turn("s1")
    assert second is not None and second.turn_id == "t2"


def test_read_of_an_unknown_session_is_none():
    assert read_turn("nope") is None


def test_malformed_value_reads_as_none():
    from app.coordination.factory import coordination

    coordination().kv_set(turn_key("s1"), "{not json", ttl_s=10)
    assert read_turn("s1") is None


def test_a_record_without_a_turn_id_is_not_a_record():
    from app.coordination.factory import coordination

    coordination().kv_set(turn_key("s1"), '{"trace_id": "abc"}', ttl_s=10)
    assert read_turn("s1") is None


def test_coordination_outage_never_raises(monkeypatch):
    import app.chat.turn_context as tc
    from app.coordination.base import CoordinationUnavailable

    def _boom():
        raise CoordinationUnavailable("down")

    monkeypatch.setattr(tc, "coordination", _boom)
    publish_turn("s1", _rec())
    assert read_turn("s1") is None


def test_an_unexpected_backend_failure_is_swallowed_too(monkeypatch):
    import app.chat.turn_context as tc

    def _boom():
        raise RuntimeError("backend on fire")

    monkeypatch.setattr(tc, "coordination", _boom)
    publish_turn("s1", _rec())
    assert read_turn("s1") is None


def test_workload_for_surface():
    assert workload_for_surface("api") == "agent_api"
    assert workload_for_surface("web") == "chat"
    assert workload_for_surface(None) == "chat"


# --- ended_at / is_open (finding B) --------------------------------------


def test_a_freshly_built_record_is_open():
    rec = _rec()
    assert rec.ended_at is None
    assert rec.is_open() is True


def test_a_record_with_ended_at_set_is_closed():
    rec = _rec(ended_at="2026-09-08T10:05:00+00:00")
    assert rec.is_open() is False


def test_a_legacy_record_with_no_ended_at_key_is_unknown_not_open():
    """A record published by a replica running before ``ended_at`` existed
    round-trips through ``from_json`` with the key absent entirely — that
    must read as "can't say", never as "still open"."""
    from app.coordination.factory import coordination

    legacy = json.dumps(
        {
            "turn_id": "t1",
            "trace_id": None,
            "span_id": None,
            "started_at": "2026-09-08T10:00:00+00:00",
            "user_id": "u1",
            "agent_id": None,
            "surface": "web",
            "workload": "chat",
            "message_id": "msg_1",
            # no "ended_at" key at all
        }
    )
    coordination().kv_set(turn_key("s1"), legacy, ttl_s=10)
    rec = read_turn("s1")
    assert rec is not None
    assert rec.ended_at is None
    assert rec.is_open() is None  # unknown, not open


def test_publish_then_read_round_trips_ended_at():
    publish_turn("s1", _rec(ended_at="2026-09-08T10:05:00+00:00"))
    rec = read_turn("s1")
    assert rec is not None and rec.ended_at == "2026-09-08T10:05:00+00:00"
    assert rec.is_open() is False


# --- started_no_later_than (finding A / finding B shared rule) -----------


def test_started_no_later_than_true_when_turn_predates_reference():
    rec = _rec(started_at="2026-09-08T10:00:00+00:00")
    reference = datetime(2026, 9, 8, 10, 0, 1, tzinfo=UTC)
    assert started_no_later_than(rec, reference) is True


def test_started_no_later_than_false_when_turn_is_newer_than_reference():
    rec = _rec(started_at="2026-09-08T10:00:05+00:00")
    reference = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)
    assert started_no_later_than(rec, reference) is False


def test_started_no_later_than_true_at_exact_equality():
    moment = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)
    rec = _rec(started_at=moment.isoformat())
    assert started_no_later_than(rec, moment) is True


def test_started_no_later_than_false_on_malformed_started_at():
    rec = _rec(started_at="not-a-timestamp")
    assert started_no_later_than(rec, datetime.now(UTC)) is False


def test_started_no_later_than_false_on_missing_started_at():
    rec = _rec(started_at=None)
    assert started_no_later_than(rec, datetime.now(UTC)) is False


def test_started_no_later_than_tolerates_a_naive_reference_started_at():
    # started_at is always published tz-aware; guard against a naive value
    # anyway rather than raising.
    naive = "2026-09-08T10:00:00"
    rec = _rec(started_at=naive)
    reference = datetime(2026, 9, 8, 10, 0, 1, tzinfo=UTC)
    assert started_no_later_than(rec, reference) is True


def test_a_second_or_two_ago_still_counts_as_no_later_than():
    rec = _rec(started_at=(datetime.now(UTC) - timedelta(seconds=2)).isoformat())
    assert started_no_later_than(rec, datetime.now(UTC)) is True
