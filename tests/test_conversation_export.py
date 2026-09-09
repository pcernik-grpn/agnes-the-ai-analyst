"""Pure unit tests for the conversation-corpus export (design 2026-09-08
§3.12): the record builder, the keyset cursor codec, and ``iter_conversations``
against fakes — no database, no HTTP.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.conversation_export import (
    ConversationExportRepoBundle,
    build_conversation_record,
    decode_cursor,
    encode_cursor,
    iter_conversations,
    serialize_jsonl,
)
from src.llm_pricing import cost_usd


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _session(**overrides):
    base = {"id": "chat_1", "surface": "web", "agent_id": None, "user_id": "user_123"}
    base.update(overrides)
    return base


def _msg(**overrides):
    base = {
        "role": "user",
        "content": "hello",
        "parts": None,
        "turn_id": "turn_1",
        "created_at": _dt("2026-01-01T10:00:00"),
        "tokens_in": None,
        "tokens_out": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "model": None,
        # Deliberately present on the raw input — the point of the "never an
        # email" tests below is that build_conversation_record drops it even
        # when a caller's row still carries it.
        "sender_email": "alice@example.com",
    }
    base.update(overrides)
    return base


_ALL_FIELDS = {
    "thread_id",
    "source",
    "surface",
    "agent_id",
    "user_id",
    "deployment_environment",
    "conversation_start",
    "conversation_end",
    "duration_seconds",
    "turn_count",
    "message_count",
    "tool_call_count",
    "tool_calls_sequence",
    "llm_run_count",
    "total_prompt_tokens",
    "total_completion_tokens",
    "llm_cache_read_tokens",
    "llm_cache_creation_tokens",
    "total_cost",
    "primary_model",
    "provider",
    "cost_status",
    "messages_json",
    "tool_calls_json",
    "first_user_message",
    "last_message_role",
    "final_assistant_message_complete",
    "last_run_status",
    "has_error",
    "error_types",
    "feedback_json",
    "memory_writes_json",
    "content_mode",
    "exported_at",
}


class TestShape:
    def test_record_has_every_spec_3_12_field(self):
        record = build_conversation_record(_session(), [_msg()], None, [], [], content_mode="full")
        assert _ALL_FIELDS <= set(record)
        assert record["thread_id"] == "chat_1"
        assert record["source"] == "agnes"
        assert record["surface"] == "web"
        assert record["content_mode"] == "full"

    def test_unknown_content_mode_is_rejected(self):
        with pytest.raises(ValueError):
            build_conversation_record(_session(), [_msg()], None, [], [], content_mode="off")

    def test_pseudonymized_without_an_anonymizer_is_rejected(self):
        with pytest.raises(ValueError):
            build_conversation_record(_session(), [_msg()], None, [], [], content_mode="pseudonymized")


class TestOrderingAndEmailSafety:
    def test_messages_json_preserves_order(self):
        messages = [
            _msg(role="user", content="hi", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(role="assistant", content="hello back", turn_id="t1", created_at=_dt("2026-01-01T10:00:05")),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert [m["role"] for m in record["messages_json"]] == ["user", "assistant"]
        assert [m["content"] for m in record["messages_json"]] == ["hi", "hello back"]
        assert record["message_count"] == 2
        assert record["last_message_role"] == "assistant"
        assert record["final_assistant_message_complete"] is True

    def test_no_message_ever_carries_sender_email(self):
        record = build_conversation_record(_session(), [_msg()], None, [], [], content_mode="full")
        for m in record["messages_json"]:
            assert "sender_email" not in m

    def test_user_id_never_contains_an_at_sign_and_record_has_no_email_field(self):
        feedback = [
            {
                "turn_id": "turn_1",
                "user_id": "user_123",
                "verdict": "up",
                "comment": None,
                "created_at": _dt("2026-01-01T10:00:00"),
            }
        ]
        record = build_conversation_record(_session(), [_msg()], None, feedback, [], content_mode="full")
        assert "@" not in record["user_id"]
        for f in record["feedback_json"]:
            assert "@" not in f["user_id"]
        assert "sender_email" not in record
        # A full sweep of the serialized record: the email that was on the
        # raw message input must not survive anywhere in the built record.
        assert "alice@example.com" not in json.dumps(record, default=str)


class TestToolCallsFlattening:
    def test_tool_calls_flattened_in_message_order_with_started_at(self):
        parts = [
            {"type": "text", "text": "let me check"},
            {
                "type": "tool",
                "tool_use_id": "tu1",
                "tool": "Bash",
                "args": {"cmd": "ls"},
                "state": "output-available",
                "result": "a.txt",
                "is_error": False,
            },
        ]
        messages = [
            _msg(role="assistant", content="checking", parts=parts, turn_id="t1", created_at=_dt("2026-01-01T10:00:00"))
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["tool_calls_json"] == [
            {
                "turn_id": "t1",
                "tool_name": "Bash",
                "input": {"cmd": "ls"},
                "output": "a.txt",
                "is_error": False,
                "started_at": "2026-01-01T10:00:00+00:00",
            }
        ]
        assert record["tool_call_count"] == 1
        assert record["tool_calls_sequence"] == ["Bash"]

    def test_tool_calls_fall_back_to_legacy_column_when_parts_absent(self):
        """A message written before ``parts`` existed (schema v123) keeps
        its calls only in the legacy positionless ``tool_calls`` column
        (``app/chat/message_parts.py::parts_to_tool_calls``'s shape:
        ``[{"tool": ..., "args": ...}]``, no result/error ever recorded).
        Without a fallback a historical conversation exports as if the
        model used no tools at all (#2365 review)."""
        messages = [
            _msg(
                role="assistant",
                content="checking",
                parts=None,
                tool_calls=[{"tool": "Bash", "args": {"cmd": "ls"}}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:00"),
            )
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["tool_calls_json"] == [
            {
                "turn_id": "t1",
                "tool_name": "Bash",
                "input": {"cmd": "ls"},
                # Never recorded for a pre-parts row -- left honestly
                # unknown rather than invented.
                "output": None,
                "is_error": False,
                "started_at": "2026-01-01T10:00:00+00:00",
            }
        ]
        assert record["tool_call_count"] == 1
        assert record["tool_calls_sequence"] == ["Bash"]

    def test_tool_calls_from_parts_win_over_legacy_column_when_both_present(self):
        """A row with `parts` never doubles up on its own legacy
        `tool_calls` projection (`parts_to_tool_calls` derives one FROM the
        other on write) -- the legacy column is a fallback for ABSENT
        parts only."""
        parts = [{"type": "tool", "tool": "Bash", "args": {"cmd": "ls"}, "result": "a.txt", "is_error": False}]
        messages = [
            _msg(
                role="assistant",
                content="checking",
                parts=parts,
                tool_calls=[{"tool": "Grep", "args": {"pattern": "x"}}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:00"),
            )
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["tool_calls_sequence"] == ["Bash"]

    def test_legacy_interrupted_marker_is_not_read_as_a_tool_call(self):
        """The interrupted/cancelled marker (`_partial_save`'s
        `tool_calls=[{"interrupted": True, ...}]`) has no `tool` key and
        must not surface as a bogus tool call once the legacy column
        becomes a real fallback source."""
        messages = [
            _msg(
                role="assistant",
                content="",
                parts=None,
                tool_calls=[{"interrupted": True, "reason": "killed"}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:00"),
            )
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["tool_calls_json"] == []
        assert record["tool_call_count"] == 0

    def test_legacy_tool_calls_fallback_is_pseudonymized(self):
        def fake_anonymizer(text: str) -> str:
            return f"REDACTED({text})"

        messages = [
            _msg(
                role="assistant",
                content="checking",
                parts=None,
                tool_calls=[{"tool": "Bash", "args": {"cmd": "echo bob@example.com"}}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:00"),
            )
        ]
        record = build_conversation_record(
            _session(), messages, None, [], [], content_mode="pseudonymized", anonymizer=fake_anonymizer
        )
        assert record["tool_calls_json"][0]["input"] == {"cmd": "REDACTED(echo bob@example.com)"}


class TestLegacyAndCancelledTranscripts:
    """Two review findings about conversations the corpus described wrongly:
    one written before `turn_id` existed, and one a person cancelled."""

    def test_a_pre_turn_id_transcript_reports_its_real_turn_count(self):
        """Every message of a historical conversation has `turn_id=None`, so
        counting ids alone reported a long transcript as ZERO turns -- false
        metadata for an evaluation pipeline."""
        messages = [
            _msg(role="user", content="first question", turn_id=None, created_at=_dt("2026-01-01T10:00:00")),
            _msg(role="assistant", content="first answer", turn_id=None, created_at=_dt("2026-01-01T10:00:05")),
            _msg(role="user", content="second question", turn_id=None, created_at=_dt("2026-01-01T10:01:00")),
            _msg(role="assistant", content="second answer", turn_id=None, created_at=_dt("2026-01-01T10:01:05")),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["turn_count"] == 2

    def test_a_mixed_transcript_counts_ids_and_legacy_turns_once_each(self):
        messages = [
            _msg(role="user", content="legacy question", turn_id=None, created_at=_dt("2026-01-01T10:00:00")),
            _msg(role="assistant", content="legacy answer", turn_id=None, created_at=_dt("2026-01-01T10:00:05")),
            _msg(role="user", content="new question", turn_id="t9", created_at=_dt("2026-01-01T10:02:00")),
            _msg(role="assistant", content="new answer", turn_id="t9", created_at=_dt("2026-01-01T10:02:05")),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["turn_count"] == 2

    def test_a_cancelled_answer_is_not_a_complete_one(self):
        """`ChatManager.cancel` persists `tool_calls=[{"cancelled": True}]` on
        a real assistant row. Without reading it the corpus called a
        cancelled answer complete."""
        messages = [
            _msg(role="user", content="question", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                parts=None,
                tool_calls=[{"cancelled": True}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
            ),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["final_assistant_message_complete"] is False
        assert record["last_run_status"] == "cancelled"
        assert record["has_error"] is False, "a person pressing stop is not an error"

    def test_a_cancel_does_not_turn_its_own_cut_stream_into_an_error(self):
        """Cancelling cuts the stream, so the broker files that call as
        `incomplete` -- and the session-wide has_error would then report the
        cancel itself as a failure (#2365 review)."""
        messages = [
            _msg(role="user", content="question", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                parts=None,
                tool_calls=[{"cancelled": True}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
            ),
        ]
        statuses = {
            "last_run_status": "incomplete",
            "has_error": True,
            "error_types": ["stream_incomplete"],
            "error_count": 0,
            "incomplete_count": 1,
        }
        record = build_conversation_record(_session(), messages, statuses, [], [], content_mode="full")
        assert record["has_error"] is False
        assert record["last_run_status"] == "cancelled"
        assert record["error_types"] == []

    def test_a_real_error_earlier_in_the_session_survives_the_cancel(self):
        messages = [
            _msg(role="user", content="question", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                parts=None,
                tool_calls=[{"cancelled": True}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
            ),
        ]
        statuses = {
            "last_run_status": "incomplete",
            "has_error": True,
            "error_types": ["429", "stream_incomplete"],
            "error_count": 1,
            "incomplete_count": 1,
        }
        record = build_conversation_record(_session(), messages, statuses, [], [], content_mode="full")
        assert record["has_error"] is True, "an unrelated earlier failure must not be cleared by a cancel"
        assert "429" in record["error_types"]

    def test_a_second_incomplete_call_is_not_explained_by_the_cancel(self):
        messages = [
            _msg(role="user", content="question", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                parts=None,
                tool_calls=[{"cancelled": True}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
            ),
        ]
        statuses = {
            "last_run_status": "incomplete",
            "has_error": True,
            "error_types": ["stream_incomplete"],
            "error_count": 0,
            "incomplete_count": 2,
        }
        record = build_conversation_record(_session(), messages, statuses, [], [], content_mode="full")
        assert record["has_error"] is True

    def test_a_cancel_during_a_tool_does_not_forgive_an_earlier_cut_stream(self):
        """`ChatManager.cancel` can land while a TOOL runs, after the
        completion that asked for it finished cleanly -- the transcript gets
        its cancelled marker and the ledger gets no incomplete row from it.
        Forgiving one anyway would erase a genuine cut stream from an
        earlier turn (#2365 review)."""
        messages = [
            _msg(role="user", content="question", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                parts=None,
                tool_calls=[{"cancelled": True}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
            ),
        ]
        statuses = {
            # The newest call SUCCEEDED (it asked for the tool); the
            # incomplete row belongs to an earlier turn.
            "last_run_status": "ok",
            "has_error": True,
            "error_types": ["stream_incomplete"],
            "error_count": 0,
            "incomplete_count": 1,
        }
        record = build_conversation_record(_session(), messages, statuses, [], [], content_mode="full")
        assert record["has_error"] is True, "an earlier cut stream must survive a tool-time cancel"
        assert record["error_types"] == ["stream_incomplete"]
        assert record["last_run_status"] == "cancelled"

    def test_an_interrupted_answer_still_wins_over_the_cancel_status(self):
        messages = [
            _msg(
                role="assistant",
                content="partial",
                parts=None,
                tool_calls=[{"interrupted": True, "reason": "killed"}, {"cancelled": True}],
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
            )
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["final_assistant_message_complete"] is False
        assert record["last_run_status"] == "interrupted"
        assert record["has_error"] is True


class TestFeedbackAndMemoryJoins:
    def test_feedback_rows_map_to_the_spec_shape(self):
        feedback = [
            {
                "turn_id": "t1",
                "user_id": "user_123",
                "verdict": "down",
                "comment": "wrong number",
                "created_at": _dt("2026-01-01T10:05:00"),
            }
        ]
        record = build_conversation_record(_session(), [_msg()], None, feedback, [], content_mode="full")
        assert record["feedback_json"] == [
            {
                "turn_id": "t1",
                "user_id": "user_123",
                "verdict": "down",
                "comment": "wrong number",
                "created_at": "2026-01-01T10:05:00+00:00",
            }
        ]

    def test_memory_rows_map_to_the_spec_shape_without_the_content_itself(self):
        memories = [
            {
                "id": "mem_1",
                "agent_id": "agent_1",
                "owner_user_id": "user_123",
                "content": "remember this fact",
                "source_session_id": "chat_1",
                "status": "pending",
                "created_at": _dt("2026-01-01T10:06:00"),
                "source_turn_id": "t1",
                "source_message_id": "msg_2",
            }
        ]
        record = build_conversation_record(_session(), [_msg()], None, [], memories, content_mode="full")
        assert record["memory_writes_json"] == [
            {"memory_id": "mem_1", "turn_id": "t1", "status": "pending", "content_length": len("remember this fact")}
        ]
        assert "remember this fact" not in json.dumps(record)


class TestContentPolicy:
    def test_full_mode_never_calls_the_anonymizer(self):
        def boom(text):
            raise AssertionError("anonymizer must not be called in full mode")

        messages = [_msg(content="hi bob@example.com")]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full", anonymizer=boom)
        assert record["messages_json"][0]["content"] == "hi bob@example.com"

    def test_pseudonymized_mode_runs_every_leaf_through_the_anonymizer_once(self):
        seen: list[str] = []

        def fake_anonymizer(text: str) -> str:
            seen.append(text)
            return f"REDACTED({text})"

        parts = [
            {"type": "text", "text": "call bob@example.com"},
            {
                "type": "tool",
                "tool_use_id": "tu1",
                "tool": "Bash",
                "args": {"cmd": "echo bob@example.com"},
                "state": "output-available",
                "result": {"out": "bob@example.com"},
                "is_error": False,
            },
        ]
        messages = [_msg(role="assistant", content="hi bob@example.com", parts=parts, turn_id="t1")]
        record = build_conversation_record(
            _session(), messages, None, [], [], content_mode="pseudonymized", anonymizer=fake_anonymizer
        )

        assert record["messages_json"][0]["content"] == "REDACTED(hi bob@example.com)"
        text_part, tool_part = record["messages_json"][0]["parts"]
        assert text_part["text"] == "REDACTED(call bob@example.com)"
        assert tool_part["args"] == {"cmd": "REDACTED(echo bob@example.com)"}
        assert tool_part["result"] == {"out": "REDACTED(bob@example.com)"}
        # tool_calls_json reads the SAME transformed parts — no second pass.
        assert record["tool_calls_json"][0]["input"] == {"cmd": "REDACTED(echo bob@example.com)"}
        assert record["tool_calls_json"][0]["output"] == {"out": "REDACTED(bob@example.com)"}
        assert seen.count("echo bob@example.com") == 1
        assert seen.count("bob@example.com") == 1
        # ids/turn_id/timestamps are never touched.
        assert tool_part["tool_use_id"] == "tu1"
        assert record["messages_json"][0]["turn_id"] == "t1"

    def test_pseudonymized_mode_scrubs_the_feedback_comment_exactly_once(self):
        """``feedback_json[].comment`` is free text too — a record whose
        ``content_mode`` says `pseudonymized` must not leak it verbatim."""
        seen: list[str] = []

        def fake_anonymizer(text: str) -> str:
            seen.append(text)
            return f"REDACTED({text})"

        feedback = [
            {
                "turn_id": "t1",
                "user_id": "user_123",
                "verdict": "down",
                "comment": "call me at bob@example.com",
                "created_at": _dt("2026-01-01T10:05:00"),
            }
        ]
        record = build_conversation_record(
            _session(), [_msg()], None, feedback, [], content_mode="pseudonymized", anonymizer=fake_anonymizer
        )
        assert record["feedback_json"][0]["comment"] == "REDACTED(call me at bob@example.com)"
        assert seen.count("call me at bob@example.com") == 1

    def test_pseudonymized_mode_leaves_an_absent_feedback_comment_alone(self):
        seen: list[str] = []

        def fake_anonymizer(text: str) -> str:
            seen.append(text)
            return f"REDACTED({text})"

        feedback = [
            {
                "turn_id": "t1",
                "user_id": "user_123",
                "verdict": "down",
                "comment": None,
                "created_at": _dt("2026-01-01T10:05:00"),
            }
        ]
        record = build_conversation_record(
            _session(), [_msg()], None, feedback, [], content_mode="pseudonymized", anonymizer=fake_anonymizer
        )
        assert record["feedback_json"][0]["comment"] is None
        assert None not in seen


class TestCostStatus:
    def test_ledger_when_llm_calls_rows_exist(self):
        calls = {
            "llm_run_count": 3,
            "total_prompt_tokens": 100,
            "total_completion_tokens": 50,
            "llm_cache_read_tokens": 10,
            "llm_cache_creation_tokens": 5,
            "total_cost": 0.0021,
            "primary_model": "claude-sonnet-5",
            "provider": "anthropic",
            "last_run_status": "ok",
            "has_error": False,
            "error_types": [],
        }
        record = build_conversation_record(_session(), [_msg()], calls, [], [], content_mode="full")
        assert record["cost_status"] == "ledger"
        assert record["llm_run_count"] == 3
        assert record["total_cost"] == 0.0021
        assert record["primary_model"] == "claude-sonnet-5"
        assert record["provider"] == "anthropic"

    def test_transcript_fallback_from_message_token_columns(self):
        messages = [
            _msg(
                role="assistant",
                tokens_in=1000,
                tokens_out=200,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                model="claude-haiku-4-5",
            )
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["cost_status"] == "transcript"
        assert record["total_prompt_tokens"] == 1000
        assert record["total_completion_tokens"] == 200
        assert record["primary_model"] == "claude-haiku-4-5"
        # No provider column on chat_messages — reported unknown, not guessed.
        assert record["provider"] is None
        expected_cost = round(cost_usd(model="claude-haiku-4-5", input_tokens=1000, output_tokens=200), 6)
        assert record["total_cost"] == expected_cost

    def test_unavailable_when_neither_calls_nor_message_tokens_exist(self):
        record = build_conversation_record(_session(), [_msg()], None, [], [], content_mode="full")
        assert record["cost_status"] == "unavailable"
        assert record["total_cost"] == 0.0
        assert record["llm_run_count"] == 0
        assert record["total_prompt_tokens"] == 0


class TestInterruptedTurn:
    """A killed/interrupted turn persists a real assistant row
    (``app/chat/manager.py``'s ``_partial_save``:
    ``tool_calls=[{"interrupted": True, "reason": ...}, ...]``) so the
    session never dead-ends. The corpus record must not read that row as a
    genuine complete answer (gap 8a), and must fold the marker into the
    error signals it otherwise reads only from ``llm_calls`` (gap 8b).
    """

    def test_an_interrupted_last_assistant_row_is_not_complete(self):
        messages = [
            _msg(role="user", content="hi", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="partial answer",
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
                tool_calls=[{"interrupted": True, "reason": "killed"}],
            ),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["last_message_role"] == "assistant"
        assert record["final_assistant_message_complete"] is False

    def test_a_genuine_last_assistant_row_is_still_complete(self):
        messages = [
            _msg(role="user", content="hi", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(role="assistant", content="done", turn_id="t1", created_at=_dt("2026-01-01T10:00:05")),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["final_assistant_message_complete"] is True

    def test_interrupted_marker_folds_into_error_signals_with_no_ledger_row(self):
        messages = [
            _msg(role="user", content="hi", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
                tool_calls=[{"interrupted": True, "reason": "session_kill"}],
            ),
        ]
        record = build_conversation_record(_session(), messages, None, [], [], content_mode="full")
        assert record["has_error"] is True
        assert record["last_run_status"] == "interrupted"
        assert "interrupted:session_kill" in record["error_types"]

    def test_interrupted_marker_folds_into_an_existing_ledger_verdict(self):
        calls = {
            "llm_run_count": 3,
            "total_prompt_tokens": 100,
            "total_completion_tokens": 50,
            "llm_cache_read_tokens": 10,
            "llm_cache_creation_tokens": 5,
            "total_cost": 0.0021,
            "primary_model": "claude-sonnet-5",
            "provider": "anthropic",
            "last_run_status": "ok",
            "has_error": False,
            "error_types": [],
        }
        messages = [
            _msg(role="user", content="hi", turn_id="t1", created_at=_dt("2026-01-01T10:00:00")),
            _msg(
                role="assistant",
                content="",
                turn_id="t1",
                created_at=_dt("2026-01-01T10:00:05"),
                tool_calls=[{"interrupted": True, "reason": "turn_idle_timeout"}],
            ),
        ]
        record = build_conversation_record(_session(), messages, calls, [], [], content_mode="full")
        # The ledger's own most-recent-call status ("ok") is superseded: the
        # killed turn's own completion may never have reached llm_calls at
        # all, so "ok" would otherwise describe an EARLIER, unrelated turn
        # as how the session ended.
        assert record["last_run_status"] == "interrupted"
        assert record["has_error"] is True
        assert record["error_types"] == ["interrupted:turn_idle_timeout"]

    def test_no_interrupted_marker_leaves_ledger_error_signals_untouched(self):
        calls = {
            "llm_run_count": 1,
            "total_prompt_tokens": 10,
            "total_completion_tokens": 5,
            "llm_cache_read_tokens": 0,
            "llm_cache_creation_tokens": 0,
            "total_cost": 0.0001,
            "primary_model": "claude-sonnet-5",
            "provider": "anthropic",
            "last_run_status": "error",
            "has_error": True,
            "error_types": ["rate_limited"],
        }
        record = build_conversation_record(_session(), [_msg()], calls, [], [], content_mode="full")
        assert record["last_run_status"] == "error"
        assert record["error_types"] == ["rate_limited"]


class TestCursorCodec:
    def test_roundtrip(self):
        ts = _dt("2026-01-01T10:00:00")
        token = encode_cursor(ts, "chat_1")
        decoded_ts, decoded_id = decode_cursor(token)
        assert decoded_ts == ts
        assert decoded_id == "chat_1"

    def test_malformed_cursor_raises_value_error(self):
        with pytest.raises(ValueError):
            decode_cursor("not-a-valid-cursor!!")


# ---------------------------------------------------------------------------
# iter_conversations against fakes — keyset pagination + bulk-by-session-id
# reads, no per-session (N+1) calls.
# ---------------------------------------------------------------------------


class _FakeSessionsRepo:
    def __init__(self, rows):
        self._rows = rows
        self.calls = 0

    def list_completed_between(self, since, until, *, surfaces=None, agent_id=None, limit=50, after=None):
        self.calls += 1
        rows = [r for r in self._rows if since <= r["last_message_at"] < until]
        if surfaces:
            rows = [r for r in rows if r["surface"] in surfaces]
        if agent_id is not None:
            rows = [r for r in rows if r["agent_id"] == agent_id]
        rows = sorted(rows, key=lambda r: (r["last_message_at"], r["id"]))
        if after is not None:
            rows = [r for r in rows if (r["last_message_at"], r["id"]) > after]
        return rows[:limit]


class _FakeBulkRepo:
    def __init__(self, by_session=None):
        self._by_session = by_session or {}
        self.calls = 0

    def list_for_sessions(self, session_ids):
        self.calls += 1
        return {sid: self._by_session[sid] for sid in session_ids if sid in self._by_session}


class _FakeCallsRepo:
    def __init__(self, totals=None, statuses=None):
        self._totals = totals or {}
        self._statuses = statuses or {}
        self.calls = 0

    def totals_for_sessions(self, session_ids):
        self.calls += 1
        return {sid: self._totals[sid] for sid in session_ids if sid in self._totals}

    def statuses_for_sessions(self, session_ids):
        return {sid: self._statuses[sid] for sid in session_ids if sid in self._statuses}


class _FakeUsersRepo:
    def __init__(self, by_email):
        self._by_email = by_email
        self.calls = 0

    def get_by_email(self, email):
        self.calls += 1
        uid = self._by_email.get(email)
        return {"id": uid} if uid else None


def _fake_bundle(rows, *, messages_by_session=None, content_mode="full"):
    return ConversationExportRepoBundle(
        sessions=_FakeSessionsRepo(rows),
        messages=_FakeBulkRepo(messages_by_session or {}),
        calls=_FakeCallsRepo(),
        feedback=_FakeBulkRepo(),
        memories=_FakeBulkRepo(),
        users=_FakeUsersRepo({"a@x.com": "user_1"}),
        content_mode=content_mode,
    )


class TestIterConversations:
    def _rows(self, n):
        return [
            {
                "id": f"chat_{i}",
                "surface": "web",
                "agent_id": None,
                "user_email": "a@x.com",
                "last_message_at": _dt(f"2026-01-0{i}T10:00:00"),
            }
            for i in range(1, n + 1)
        ]

    def test_pages_through_the_cursor_until_exhausted(self):
        rows = self._rows(3)
        messages_by_session = {r["id"]: [_msg(role="user", content=f"hi {r['id']}", turn_id="t1")] for r in rows}
        bundle = _fake_bundle(rows, messages_by_session=messages_by_session)

        records, next_cursor, keys = iter_conversations(
            bundle, since=_dt("2026-01-01T00:00:00"), until=_dt("2026-02-01T00:00:00"), limit=2
        )
        assert [r["thread_id"] for r in records] == ["chat_1", "chat_2"]
        assert next_cursor is not None
        assert records[0]["user_id"] == "user_1"
        assert keys == [(rows[0]["last_message_at"], rows[0]["id"]), (rows[1]["last_message_at"], rows[1]["id"])]

        records2, next_cursor2, keys2 = iter_conversations(
            bundle,
            since=_dt("2026-01-01T00:00:00"),
            until=_dt("2026-02-01T00:00:00"),
            limit=2,
            cursor=next_cursor,
        )
        assert [r["thread_id"] for r in records2] == ["chat_3"]
        assert next_cursor2 is None
        assert keys2 == [(rows[2]["last_message_at"], rows[2]["id"])]

    def test_bulk_reads_are_one_call_per_page_not_one_per_session(self):
        rows = self._rows(5)
        messages_by_session = {r["id"]: [_msg(turn_id="t1")] for r in rows}
        bundle = _fake_bundle(rows, messages_by_session=messages_by_session)

        records, _, keys = iter_conversations(
            bundle, since=_dt("2026-01-01T00:00:00"), until=_dt("2026-02-01T00:00:00"), limit=5
        )
        assert len(records) == 5
        assert len(keys) == 5
        assert bundle.sessions.calls == 1
        assert bundle.messages.calls == 1
        assert bundle.calls.calls == 1  # totals_for_sessions, one call for the whole page

    def test_empty_window_returns_no_records_and_no_cursor(self):
        bundle = _fake_bundle([])
        records, next_cursor, keys = iter_conversations(
            bundle, since=_dt("2026-01-01T00:00:00"), until=_dt("2026-02-01T00:00:00"), limit=50
        )
        assert records == []
        assert next_cursor is None
        assert keys == []

    def test_malformed_cursor_raises_value_error(self):
        bundle = _fake_bundle(self._rows(1))
        with pytest.raises(ValueError):
            iter_conversations(
                bundle,
                since=_dt("2026-01-01T00:00:00"),
                until=_dt("2026-02-01T00:00:00"),
                limit=10,
                cursor="garbage!!",
            )

    def test_surfaces_tuple_is_pushed_into_the_query_not_filtered_after(self):
        rows = self._rows(3)
        rows[1]["surface"] = "slack_dm"
        messages_by_session = {r["id"]: [_msg(role="user", content="hi", turn_id="t1")] for r in rows}
        bundle = _fake_bundle(rows, messages_by_session=messages_by_session)

        records, next_cursor, keys = iter_conversations(
            bundle,
            since=_dt("2026-01-01T00:00:00"),
            until=_dt("2026-02-01T00:00:00"),
            limit=50,
            surfaces=("web",),
        )
        assert [r["thread_id"] for r in records] == ["chat_1", "chat_3"]
        assert next_cursor is None
        # the key is the SESSION row's own (last_message_at, id) -- never
        # derived from the built record's conversation_end.
        assert keys == [(rows[0]["last_message_at"], rows[0]["id"]), (rows[2]["last_message_at"], rows[2]["id"])]


def test_serialize_jsonl_yields_one_line_per_record():
    records = [{"thread_id": "a"}, {"thread_id": "b"}]
    body = b"".join(serialize_jsonl(records))
    lines = body.decode("utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["thread_id"] == "a"
    assert json.loads(lines[1])["thread_id"] == "b"
