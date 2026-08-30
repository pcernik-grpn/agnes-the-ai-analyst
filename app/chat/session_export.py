"""Materialize a web-chat session as a Claude-Code-shaped session jsonl.

F4 (audit-full-coverage plan, Task 8): ``chat_messages`` rows never left the
``chat_messages`` table, so a chat conversation had no admin transcript
viewer and never fed the session pipeline's usage rollups the way a CLI/
analyst session does. This module bridges the gap by writing the same
``{"type": "user"|"assistant", "message": {...}}`` jsonl shape
``services/session_pipeline/lib.parse_jsonl`` reads and
``app/api/admin_sessions.py::_render_transcript`` +
``services/session_processors/usage_lib`` already consume, under
``${SESSION_DATA_DIR}/<users.id>/chat-<chat_id>.jsonl`` — the exact layout
``services/session_pipeline/runner.py`` scans. Once the file exists, every
downstream reader (the admin sessions list/transcript, the usage rollups)
picks the chat session up unchanged; nothing downstream needed to change.

Gated by ``sessions.include_chat`` (default ON — see
``config/instance.yaml.example``): a chat transcript carries the same
customer-data sensitivity as an uploaded CLI session jsonl, which the
pipeline has always stored and admins have always been able to browse at
``/admin/sessions``; the flag is the escape hatch back to the old
no-materialized-copy behavior for an instance that wants chat kept out of
the filesystem session store entirely.

Called from three places, all best-effort (never raises):
  - ``app.chat.manager.ChatManager._kill_locked`` — the session-end/kill
    path every teardown route (archive, delete, idle reaper) funnels
    through.
  - ``app.api.chat``'s archive routes — a direct safety net alongside the
    ``kill()`` call those routes already make.
  - the session-pipeline sweep (``services/session_pipeline/runner.py``) —
    catches anything the two hooks above missed (a crash before ``kill()``
    ran, or a still-active session an admin wants to inspect early).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.chat.types import ChatMessage
from app.instance_config import feature_enabled
from src.audit_helpers import log_safe

logger = logging.getLogger(__name__)

# Same default the rest of the session pipeline uses
# (services/session_pipeline/runner.py::DEFAULT_SESSION_DATA_DIR,
# app/api/admin_user_sessions.py::_session_data_dir) — read at CALL time
# (not module import time) so a test's ``monkeypatch.setenv`` always takes
# effect.
_DEFAULT_SESSION_DATA_DIR = "/data/user_sessions"


def _session_data_dir() -> Path:
    return Path(os.environ.get("SESSION_DATA_DIR", _DEFAULT_SESSION_DATA_DIR))


def _result_to_content(result: Any) -> Any:
    """Normalize a tool part's ``result`` (message_parts.py: ``<any>``) into
    the ``str | list[{"type": "text", ...}]`` shape
    ``admin_sessions._flatten_text_content`` renders. Non-string/list values
    (dict, number, bool) are JSON-stringified rather than dropped — a tool
    result is real evidence for an operator debugging a failure."""
    if result is None:
        return ""
    if isinstance(result, (str, list)):
        return result
    return json.dumps(result, default=str)


def _assistant_blocks(m: ChatMessage) -> tuple[list[dict], list[dict]]:
    """Build (content_blocks, tool_result_blocks) for one assistant message.

    Prefers the ordered ``parts`` array (text/tool interleaved in arrival
    order, app/chat/message_parts.py — #1504); falls back to ``content`` +
    the positionless legacy ``tool_calls`` for rows written before schema
    v123 (``parts`` NULL). A tool part whose ``state`` is not
    ``input-available`` (i.e. it has a result) gets a matching
    ``tool_result`` block returned separately — real Claude Code
    transcripts carry the result as a SEPARATE follow-up turn, never inline
    in the assistant turn, and ``_render_transcript`` / the usage
    processor's error-map both expect that shape.

    Not attempted: splitting one ``ChatMessage`` into several assistant
    turns around each tool boundary (what a live multi-round-trip Claude
    Code session would actually produce). Agnes persists one whole agent
    turn — possibly several sequential tool round-trips — as a single row
    with one aggregate ``tokens_in``/``tokens_out``; dividing that total
    across split turns would attribute usage we cannot actually measure per
    piece. The one observable consequence: any prose AFTER a mid-message
    tool call renders before that tool's result turn in the transcript
    viewer, not after — a display-order quirk for the (rare) multi-tool
    turn, not a data-loss issue; the result itself is never dropped.
    """
    content_blocks: list[dict] = []
    result_blocks: list[dict] = []

    if m.parts:
        for i, part in enumerate(m.parts):
            ptype = part.get("type")
            if ptype == "text":
                text = part.get("text") or ""
                if text:
                    content_blocks.append({"type": "text", "text": text})
            elif ptype == "tool":
                tool_use_id = part.get("tool_use_id") or f"{m.id}:tool:{i}"
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": part.get("tool"),
                        "input": part.get("args") or {},
                    }
                )
                if part.get("state") != "input-available":
                    result_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "is_error": bool(part.get("is_error", False)),
                            "content": _result_to_content(part.get("result")),
                        }
                    )
        if content_blocks or result_blocks:
            return content_blocks, result_blocks

    # Legacy fallback: plain content + positionless tool_calls, no results
    # (the pre-parts schema never stored them).
    if m.content:
        content_blocks.append({"type": "text", "text": m.content})
    for i, call in enumerate(m.tool_calls or []):
        if not isinstance(call, dict) or not call.get("tool"):
            continue
        content_blocks.append(
            {
                "type": "tool_use",
                "id": f"{m.id}:legacy_tool:{i}",
                "name": call.get("tool"),
                "input": call.get("args") or {},
            }
        )
    return content_blocks, result_blocks


def messages_to_turns(chat_id: str, messages: list[ChatMessage]) -> list[dict]:
    """Convert persisted ``chat_messages`` rows (chronological order, as
    ``chat_message_repo().list_messages`` returns them) into the
    Claude-Code jsonl turn shape ``services/session_pipeline/lib.parse_jsonl``
    parses and ``app/api/admin_sessions.py::_render_transcript`` +
    ``services/session_processors/usage_lib`` (``iter_events``,
    ``compute_summary``) consume.

    A user message becomes one ``"user"`` turn. An assistant message
    becomes one ``"assistant"`` turn, immediately followed by a synthetic
    ``"user"`` turn carrying any tool results (see ``_assistant_blocks``) —
    mirroring how a real Claude Code session always puts a tool's result in
    the NEXT turn, never the same one.
    """
    turns: list[dict] = []
    for m in messages:
        created = m.created_at or datetime.now(timezone.utc)
        ts = created.isoformat()

        if m.role == "assistant":
            blocks, result_blocks = _assistant_blocks(m)
            message: dict[str, Any] = {"role": "assistant", "content": blocks}
            if m.model:
                message["model"] = m.model
            if m.tokens_in or m.tokens_out:
                message["usage"] = {
                    "input_tokens": m.tokens_in or 0,
                    "output_tokens": m.tokens_out or 0,
                }
            turns.append(
                {
                    "type": "assistant",
                    "sessionId": chat_id,
                    "uuid": m.id,
                    "timestamp": ts,
                    "message": message,
                }
            )
            if result_blocks:
                turns.append(
                    {
                        "type": "user",
                        "sessionId": chat_id,
                        "uuid": f"{m.id}:tool_result",
                        "timestamp": ts,
                        "message": {"role": "user", "content": result_blocks},
                    }
                )
        else:
            turns.append(
                {
                    "type": "user",
                    "sessionId": chat_id,
                    "uuid": m.id,
                    "timestamp": ts,
                    "message": {
                        "role": m.role,
                        "content": [{"type": "text", "text": m.content or ""}],
                    },
                }
            )
    return turns


def export_chat_session_jsonl(chat_id: str) -> Optional[Path]:
    """Write ``chat_id``'s messages to
    ``${SESSION_DATA_DIR}/<users.id>/chat-<chat_id>.jsonl`` (atomic:
    ``.tmp`` then ``os.replace``) and return the path — or ``None``,
    never raising, when:

      - ``sessions.include_chat`` is off,
      - the session doesn't exist or has no messages,
      - the owner email can't be resolved to a ``users.id``,
      - the active app-state backend is DuckDB and a PG-only repo in this
        call path raises ``RequiresPostgresBackend`` (fail clean per the
        A3 ratchet — chat itself doesn't run on such an instance either),
      - or the read/write itself fails (logged, swallowed — an export
        failure must never break the kill/archive request that triggered
        it).

    Idempotent: re-exporting an unchanged session overwrites the file with
    identical content; the session pipeline's hash-based dedup
    (``services/session_pipeline/lib.compute_file_hash``) treats that as
    already-processed, so a repeat call from the sweep costs one write and
    no reprocessing.
    """
    if not feature_enabled("sessions", "include_chat", env_var="AGNES_SESSIONS_INCLUDE_CHAT", default=True):
        return None

    from src.repositories import RequiresPostgresBackend, chat_message_repo, chat_session_repo, users_repo

    try:
        session = chat_session_repo().get_session(chat_id)
        if session is None:
            return None
        owner = users_repo().get_by_email(session.user_email)
        if not owner:
            return None
        messages = chat_message_repo().list_messages(chat_id)
    except RequiresPostgresBackend:
        return None
    except Exception:
        logger.warning("chat session export: could not load session %s", chat_id, exc_info=True)
        return None

    if not messages:
        return None

    turns = messages_to_turns(chat_id, messages)
    if not turns:
        return None

    target_dir = _session_data_dir() / owner["id"]
    target = target_dir / f"chat-{chat_id}.jsonl"
    tmp = target.with_name(target.name + ".tmp")
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as fh:
            for turn in turns:
                fh.write(json.dumps(turn, default=str))
                fh.write("\n")
        os.replace(tmp, target)
    except OSError:
        logger.warning("chat session export: write failed for %s", chat_id, exc_info=True)
        return None

    log_safe(
        user_id=owner["id"],
        action="chat.session_exported",
        resource=f"session:{chat_id}",
        params={"messages": len(messages)},
        result="success",
    )
    return target
