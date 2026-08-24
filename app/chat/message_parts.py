"""Assemble an assistant turn's ordered ``parts`` from its frame buffer.

An assistant message is a SEQUENCE: prose, then a tool call, then more prose
explaining what came back. Agnes's frame protocol already carries that order —
``token`` / ``tool_call`` / ``tool_result`` arrive interleaved — but the
persisted message flattened it into ``content`` (one string) plus
``tool_calls`` (a positionless list). Everything downstream then had to guess:
the web client rebuilt the interleaving from the live frame order and could
not rebuild it at all after a reload, so a refreshed turn showed every tool
block appended under the whole answer (issue #1504).

``parts`` is the fix, and the shape is not invented here — it is the one
``apps/kai-agent`` persists and ``apps/kbc-ui`` renders in Keboola's frontend
monorepo: one ordered array per message, where a tool's result MUTATES the
already-positioned entry rather than appending a new one, so position is
preserved by construction and ordering is array index end to end, never a
timestamp and never a client-side reconstruction.

Two part kinds, snake_case to match every other Agnes frame field (the
frontend's ``dynamic-tool``/``toolCallId``/``input`` spelling is TypeScript
convention; the ``state`` vocabulary below is worth borrowing verbatim
because it names exactly the distinctions a renderer needs):

    {"type": "text", "text": "…"}

    {"type": "tool", "tool_use_id": "…", "tool": "Bash",
     "args": {...},
     "state": "input-available" | "output-available" | "output-error",
     "result": <any>,          # absent while input-available
     "is_error": bool}         # absent while input-available

``state`` is what lets a reloaded tool card look like a live one. Without it
the reload path could only render the tool's NAME — no status icon, no
outcome, no result body — because the old ``{tool, args}`` row evidenced
none of those, and drawing a success tick anyway would have asserted an
outcome the record did not hold.

Deliberately NOT stored: the tool's duration. Neither producer puts elapsed
time on the wire (the client measures it locally between the two frames), so
a persisted value would be fabricated. A replayed card therefore shows no
timing — the one honest gap left between the two renderings.
"""

from __future__ import annotations

from typing import Any, Optional

#: A tool call whose result has not arrived (the turn died mid-tool, or it is
#: still running when this is called for a mid-turn partial save).
STATE_INPUT_AVAILABLE = "input-available"
#: Result arrived, tool succeeded.
STATE_OUTPUT_AVAILABLE = "output-available"
#: Result arrived, tool failed. Distinct from a failed TURN — the answer
#: usually continues past it, which is why this rides the part and not the
#: message.
STATE_OUTPUT_ERROR = "output-error"


def build_message_parts(frames: list[dict]) -> Optional[list[dict]]:
    """Fold a turn's buffered frames into the ordered ``parts`` array.

    ``frames`` is the manager's ``live.turn_buffer`` — ``token``,
    ``tool_call`` and ``tool_result`` in arrival order. Frames have already
    been through ``frame_seq.stamp_frame``, which OVERWRITES ``id`` with
    ``chat_id:seq``, so a call and its result are paired on ``tool_use_id``
    (the same reason the web client pairs on it).

    Returns ``None`` for a turn with nothing in it, so the column stays NULL
    rather than holding an empty array.
    """
    parts: list[dict] = []
    # Index into `parts` per tool_use_id, so a result mutates the entry where
    # it already sits. Rebuilding by scanning would work too; the map just
    # makes it obvious that position is never recomputed.
    tool_positions: dict[str, int] = {}

    for frame in frames:
        ftype = frame.get("type")

        if ftype == "token":
            text = frame.get("text")
            if not text:
                continue
            # Coalesce a RUN of token frames into one text part: the deltas
            # are a transport detail, and one part per delta would make the
            # array unreadable and the renderer's job harder for no gain.
            if parts and parts[-1].get("type") == "text":
                parts[-1]["text"] += text
            else:
                parts.append({"type": "text", "text": text})
            continue

        if ftype == "tool_call":
            tool = frame.get("tool")
            if not isinstance(tool, str) or not tool:
                # The cancelled/interrupted markers the manager stores in
                # place of a real call carry no name; rendering them produced
                # `tool: undefined` blocks, so they are not parts.
                continue
            tool_use_id = str(frame.get("tool_use_id") or "")
            part: dict[str, Any] = {
                "type": "tool",
                "tool_use_id": tool_use_id,
                "tool": tool,
                "args": frame.get("args") or {},
                "state": STATE_INPUT_AVAILABLE,
            }
            if tool_use_id:
                tool_positions[tool_use_id] = len(parts)
            parts.append(part)
            continue

        if ftype == "tool_result":
            tool_use_id = str(frame.get("tool_use_id") or "")
            index = tool_positions.get(tool_use_id)
            if index is None:
                # A result with no matching call in this buffer — a mid-turn
                # reconnect replayed the result after the buffer was cleared,
                # or a provider emitted one unpaired. Dropping it is right:
                # appending a resultless tool part here would invent a
                # position the call never had.
                continue
            part = parts[index]
            is_error = bool(frame.get("is_error"))
            part["state"] = STATE_OUTPUT_ERROR if is_error else STATE_OUTPUT_AVAILABLE
            part["result"] = frame.get("result")
            part["is_error"] = is_error
            continue

    # Trim the text parts LAST, so a run split across frames is stripped once
    # as a whole. `_TurnState.text()` in the engine provider does the same
    # (`"\n\n".join(part.strip() …)`), which keeps `content` and `parts`
    # describing the same answer instead of differing at the seams.
    cleaned: list[dict] = []
    for part in parts:
        if part.get("type") == "text":
            text = (part.get("text") or "").strip()
            if not text:
                continue
            cleaned.append({"type": "text", "text": text})
        else:
            cleaned.append(part)
    return cleaned or None


def parts_to_tool_calls(parts: Optional[list[dict]]) -> Optional[list[dict]]:
    """Project ``parts`` back to the legacy positionless ``{tool, args}`` list.

    ``tool_calls`` stays populated for readers that predate ``parts`` — the
    transcript export, the sources verdict, `verify()`'s haystack, and any
    persisted row written before this column existed. One projection function
    so the two can never disagree about which calls a turn made.
    """
    if not parts:
        return None
    calls = [
        {"tool": p.get("tool"), "args": p.get("args") or {}}
        for p in parts
        if p.get("type") == "tool" and isinstance(p.get("tool"), str)
    ]
    return calls or None


def parts_to_content(parts: Optional[list[dict]]) -> str:
    """Join a turn's text parts the way both producers build ``content``.

    Blank line between parts, matching ``_TurnState.text()`` and the runner's
    TextBlock consolidation. Used to reconstruct ``content`` for a row that
    has ``parts`` but no stored content, and by tests asserting the two agree.
    """
    if not parts:
        return ""
    return "\n\n".join(p["text"] for p in parts if p.get("type") == "text" and p.get("text"))
