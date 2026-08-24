"""The ordered `parts` assembler — the model that makes a turn's shape survive.

Issue #1504: an assistant turn is prose → tool → prose, and the persisted
message flattened it to `content` + a positionless `tool_calls`, so the web
client rebuilt the interleaving from live frame order and could not rebuild it
at all after a reload. These tests pin the structure that replaces the guessing
— the same one `apps/kai-agent` persists and `apps/kbc-ui` renders.
"""

from __future__ import annotations

from app.chat.message_parts import (
    STATE_INPUT_AVAILABLE,
    STATE_OUTPUT_AVAILABLE,
    STATE_OUTPUT_ERROR,
    build_message_parts,
    parts_to_content,
    parts_to_tool_calls,
)


def _stamped(frame: dict, seq: int) -> dict:
    """Frames reach the buffer AFTER frame_seq.stamp_frame, which overwrites
    `id`. Reproduced here so nothing can start pairing on `id` again."""
    return {**frame, "id": f"chat_x:{seq}", "frame_seq": seq}


def _interleaved_turn() -> list[dict]:
    return [
        _stamped({"type": "token", "text": "Let me check "}, 1),
        _stamped({"type": "token", "text": "the server. "}, 2),
        _stamped({"type": "tool_call", "tool_use_id": "c1", "tool": "server_info", "args": {}}, 3),
        _stamped({"type": "tool_result", "tool_use_id": "c1", "result": '{"ok":true}', "is_error": False}, 4),
        _stamped({"type": "token", "text": "Healthy. Now the counts:"}, 5),
        _stamped(
            {"type": "tool_call", "tool_use_id": "c2", "tool": "Bash", "args": {"command": "agnes query 'x'"}},
            6,
        ),
        _stamped(
            {
                "type": "tool_result",
                "tool_use_id": "c2",
                "result": {"columns": ["a"], "rows": [[1]]},
                "is_error": False,
            },
            7,
        ),
        _stamped({"type": "token", "text": "CZ leads."}, 8),
    ]


def test_parts_preserve_the_turn_order():
    parts = build_message_parts(_interleaved_turn())
    assert [p["type"] for p in parts] == ["text", "tool", "text", "tool", "text"], (
        "the array IS the order — text before, between and after the two calls"
    )
    assert parts[0]["text"] == "Let me check the server."
    assert parts[2]["text"] == "Healthy. Now the counts:"
    assert parts[4]["text"] == "CZ leads."


def test_a_run_of_token_frames_becomes_one_text_part():
    """Deltas are a transport detail; one part per delta would make the array
    unreadable and buy nothing."""
    parts = build_message_parts(_interleaved_turn())
    assert len([p for p in parts if p["type"] == "text"]) == 3
    assert parts[0]["text"] == "Let me check the server.", "two frames coalesced, then stripped once as a whole"


def test_a_result_mutates_the_positioned_part_instead_of_appending():
    """The whole reason ordering survives: the result lands ON the call's
    existing entry, so its index never moves."""
    parts = build_message_parts(_interleaved_turn())
    tools = [p for p in parts if p["type"] == "tool"]
    assert len(tools) == 2, "two calls, two parts — a result must not add a third"
    assert tools[0]["tool_use_id"] == "c1"
    assert tools[0]["state"] == STATE_OUTPUT_AVAILABLE
    assert tools[0]["result"] == '{"ok":true}'
    assert tools[0]["is_error"] is False
    assert parts.index(tools[0]) == 1, "still sitting where the call arrived"


def test_a_failed_tool_is_output_error_not_a_turn_failure():
    frames = [
        _stamped({"type": "token", "text": "Trying."}, 1),
        _stamped({"type": "tool_call", "tool_use_id": "c1", "tool": "Bash", "args": {}}, 2),
        _stamped(
            {
                "type": "tool_result",
                "tool_use_id": "c1",
                "result": "Catalog Error: Table with name nope does not exist!",
                "is_error": True,
            },
            3,
        ),
        _stamped({"type": "token", "text": "No such table."}, 4),
    ]
    parts = build_message_parts(frames)
    tool = next(p for p in parts if p["type"] == "tool")
    assert tool["state"] == STATE_OUTPUT_ERROR
    assert tool["is_error"] is True
    # The answer continues past it — the failure is the part's, not the turn's.
    assert parts[-1] == {"type": "text", "text": "No such table."}


def test_an_unresolved_call_stays_input_available():
    """A turn that died mid-tool keeps the call visible with no invented
    outcome, which is what lets the renderer show it honestly."""
    frames = [
        _stamped({"type": "tool_call", "tool_use_id": "c1", "tool": "Bash", "args": {"command": "sleep 99"}}, 1),
    ]
    parts = build_message_parts(frames)
    assert parts[0]["state"] == STATE_INPUT_AVAILABLE
    assert "result" not in parts[0] and "is_error" not in parts[0], (
        "no result arrived, so the part must not carry either field"
    )


def test_pairing_is_on_tool_use_id_not_id():
    """`frame_seq.stamp_frame` overwrites `id` with `chat_id:seq`, so a call
    and its result never share it — pairing on `id` is what left every tool
    block stuck on 'running…' before."""
    frames = [
        _stamped({"type": "tool_call", "tool_use_id": "c1", "tool": "Bash", "args": {}}, 1),
        _stamped({"type": "tool_result", "tool_use_id": "c1", "result": "done", "is_error": False}, 9),
    ]
    parts = build_message_parts(frames)
    assert parts[0]["state"] == STATE_OUTPUT_AVAILABLE
    assert parts[0]["result"] == "done"


def test_a_nameless_tool_marker_is_not_a_part():
    """The cancelled/interrupted markers the manager stores in place of a real
    call carry no tool name; they used to render as `tool: undefined`."""
    frames = [
        _stamped({"type": "token", "text": "hi"}, 1),
        _stamped({"type": "tool_call", "tool_use_id": "", "tool": None, "args": {}}, 2),
    ]
    parts = build_message_parts(frames)
    assert [p["type"] for p in parts] == ["text"]


def test_an_orphan_result_is_dropped_rather_than_inventing_a_position():
    """A mid-turn reconnect can replay a result after the buffer was cleared.
    Appending a tool part for it would fabricate a position its call never had."""
    frames = [
        _stamped({"type": "tool_result", "tool_use_id": "gone", "result": "x", "is_error": False}, 1),
        _stamped({"type": "token", "text": "answer"}, 2),
    ]
    parts = build_message_parts(frames)
    assert [p["type"] for p in parts] == ["text"]


def test_blank_and_empty_turns_are_null_not_an_empty_array():
    assert build_message_parts([]) is None
    assert build_message_parts([_stamped({"type": "token", "text": "   "}, 1)]) is None
    assert build_message_parts([_stamped({"type": "token", "text": ""}, 1)]) is None


def test_non_turn_frames_are_ignored():
    frames = [
        _stamped({"type": "approval_request", "request_id": "r1"}, 1),
        _stamped({"type": "token", "text": "hi"}, 2),
        _stamped({"type": "assistant_message", "content": "hi"}, 3),
        _stamped({"type": "done"}, 4),
    ]
    assert build_message_parts(frames) == [{"type": "text", "text": "hi"}]


def test_tool_calls_projection_matches_what_the_turn_ran():
    """`tool_calls` stays populated for readers that predate `parts` — the
    transcript export, the sources verdict, verify()'s haystack. One
    projection so the two can never disagree."""
    parts = build_message_parts(_interleaved_turn())
    calls = parts_to_tool_calls(parts)
    assert calls == [
        {"tool": "server_info", "args": {}},
        {"tool": "Bash", "args": {"command": "agnes query 'x'"}},
    ]
    assert parts_to_tool_calls(None) is None
    assert parts_to_tool_calls([{"type": "text", "text": "no tools here"}]) is None


def test_content_projection_matches_the_producers_join():
    """Both producers build `content` as a blank-line join of stripped text
    blocks. The projection has to agree, or `content` and `parts` would
    describe the same answer differently."""
    parts = build_message_parts(_interleaved_turn())
    assert parts_to_content(parts) == "Let me check the server.\n\nHealthy. Now the counts:\n\nCZ leads."
    assert parts_to_content(None) == ""
