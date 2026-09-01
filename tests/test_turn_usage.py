"""The broker→manager turn-usage seam: coordination-backed per-session
counters written by the broker for every session-bound completion, drained
destructively (Redis GETDEL semantics) exactly once per turn by ChatManager.

Destructive drain is the design's safety property — no watermark state
anywhere, so a process restart or gateway takeover cannot double-count."""

import pytest

from app.chat.turn_usage import add_turn_usage, drain_turn_usage
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


_USAGE = {
    "model": "claude-sonnet-5",
    "input_tokens": 10,
    "output_tokens": 2,
    "cache_read_tokens": 100,
    "cache_creation_tokens": 7,
}


def test_round_trip_accumulates_across_calls():
    add_turn_usage("s1", dict(_USAGE))
    add_turn_usage("s1", {"model": "claude-sonnet-5", "input_tokens": 5, "output_tokens": 3})
    assert drain_turn_usage("s1") == {
        "input_tokens": 15,
        "output_tokens": 5,
        "cache_read_tokens": 100,
        "cache_creation_tokens": 7,
        "model": "claude-sonnet-5",
    }


def test_drain_is_destructive():
    add_turn_usage("s1", dict(_USAGE))
    assert drain_turn_usage("s1") is not None
    assert drain_turn_usage("s1") is None


def test_empty_drain_returns_none():
    assert drain_turn_usage("never-seen") is None


def test_sessions_do_not_bleed_into_each_other():
    add_turn_usage("s1", dict(_USAGE))
    assert drain_turn_usage("s2") is None
    assert drain_turn_usage("s1") is not None


def test_model_is_last_writer():
    add_turn_usage("s1", {"model": "claude-haiku-4-5", "input_tokens": 1})
    add_turn_usage("s1", {"model": "claude-sonnet-5", "input_tokens": 1})
    drained = drain_turn_usage("s1")
    assert drained is not None and drained["model"] == "claude-sonnet-5"


def test_all_zero_usage_records_nothing():
    """Zero tokens is not a measurement worth a row (mirrors the manager's
    'storing zeros asserts a measurement nobody made' rule)."""
    add_turn_usage("s1", {"model": "m", "input_tokens": 0, "output_tokens": 0})
    assert drain_turn_usage("s1") is None


def test_unavailable_backend_never_raises(monkeypatch):
    class _Down:
        def __getattr__(self, name):
            raise CoordinationUnavailable("down")

    monkeypatch.setattr("app.chat.turn_usage.coordination", lambda: _Down())
    add_turn_usage("s1", dict(_USAGE))  # must not raise
    assert drain_turn_usage("s1") is None  # must not raise
