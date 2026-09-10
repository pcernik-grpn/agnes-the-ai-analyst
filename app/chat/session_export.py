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

Called from four places, all best-effort (never raises):
  - ``app.chat.manager.ChatManager._kill_locked`` — the session-end/kill
    path every teardown route (archive, delete, idle reaper) funnels
    through.
  - ``app.api.chat``'s archive routes — a direct safety net alongside the
    ``kill()`` call those routes already make.
  - the session-pipeline sweep (``services/session_pipeline/runner.py``) —
    catches anything the two hooks above missed (a crash before ``kill()``
    ran, or a still-active session an admin wants to inspect early).
  - ``app.api.admin_sessions.transcript`` (on demand, via
    :func:`ensure_chat_transcript_current` below) — the sweep above only
    (re-)writes a still-live session's jsonl on its own scheduler cadence
    (``SCHEDULER_USAGE_PROCESSOR_INTERVAL``, default 10 minutes), so an
    operator opening the transcript viewer inside that window used to see
    a stale file or a bare "session not found" — indistinguishable from a
    session that never had a transcript at all (measured on a production
    instance, 2026-09-09). The on-demand path shares this module's exact
    staleness rule (:func:`is_chat_export_stale`) so "is this transcript
    current" has one definition regardless of which of the four callers
    asks.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

# ``list_messages`` (both chat_message repo backends) defaults its own
# ``limit`` to 500 and returns oldest-first -- calling it once and taking
# that page as "the conversation" silently truncated any chat past 500
# messages: the export got a current mtime while missing the newest tail,
# so ``is_chat_export_stale`` reported it fresh forever. This is the page
# size ``_list_all_chat_messages`` pages through with, not a cap.
_EXPORT_PAGE_SIZE = 500


def _session_data_dir() -> Path:
    return Path(os.environ.get("SESSION_DATA_DIR", _DEFAULT_SESSION_DATA_DIR))


def _list_all_chat_messages(chat_id: str, repo: Any) -> list[ChatMessage]:
    """Every message for ``chat_id``, oldest-first -- paging through
    ``repo.list_messages`` rather than trusting its single-call default
    limit. Cursors on the previous page's last id (the same ``after_id``
    contract ``list_messages`` already offers callers that want to resume a
    partial read), so a page exactly ``_EXPORT_PAGE_SIZE`` long is followed
    by one more call to confirm there is nothing after it."""
    messages: list[ChatMessage] = []
    after_id: str | None = None
    while True:
        page = repo.list_messages(chat_id, after_id=after_id, limit=_EXPORT_PAGE_SIZE)
        if not page:
            break
        messages.extend(page)
        after_id = page[-1].id
        if len(page) < _EXPORT_PAGE_SIZE:
            break
    return messages


def _as_utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to UTC-aware. DuckDB returns
    naive ``TIMESTAMP`` values for ``created_at`` even though every write
    path stores UTC (see the identical note on
    ``services/session_pipeline/runner.py::_as_utc``); Postgres rows are
    already aware. Treating naive as already-UTC matches every other read
    of this column."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _export_watermark(messages: list[ChatMessage]) -> datetime:
    """The newest ``created_at`` actually present among *messages* -- i.e.
    exactly what the exported file's content can vouch for. Recorded
    alongside the file by :func:`_write_export_watermark` and read back by
    :func:`is_chat_export_stale`, deliberately NOT via the file's own mtime
    (see that function's docstring for why the two must stay separate)."""
    newest = max((m.created_at for m in messages if m.created_at is not None), default=None)
    return _as_utc(newest) if newest is not None else datetime.now(UTC)


def _watermark_path(target: Path) -> Path:
    """The sidecar file :func:`_write_export_watermark` /
    :func:`_read_export_watermark` use to record the content watermark for
    *target* (an exported chat jsonl), named alongside it rather than
    reusing ``target``'s own mtime -- see :func:`is_chat_export_stale`."""
    return target.with_name(target.name + ".watermark")


def _write_export_watermark(target: Path, watermark: datetime) -> None:
    """Atomically record *watermark* (:func:`_export_watermark` of the
    messages just written to *target*) in its sidecar file. Same
    ``.tmp`` + ``os.replace`` pattern as the jsonl write itself, so a
    reader never observes a half-written watermark."""
    wm_path = _watermark_path(target)
    wm_tmp = wm_path.with_name(wm_path.name + ".tmp")
    wm_tmp.write_text(watermark.isoformat(), encoding="utf-8")
    os.replace(wm_tmp, wm_path)


def _read_export_watermark(target: Path) -> datetime | None:
    """The content watermark :func:`_write_export_watermark` recorded for
    *target*, or ``None`` when there is no sidecar to read (an export
    written before this mechanism existed, or one whose sidecar write
    failed) -- the caller (:func:`is_chat_export_stale`) falls back to
    *target*'s own mtime in that case."""
    try:
        text = _watermark_path(target).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return None


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
        created = m.created_at or datetime.now(UTC)
        ts = created.isoformat()

        if m.role == "assistant":
            blocks, result_blocks = _assistant_blocks(m)
            message: dict[str, Any] = {"role": "assistant", "content": blocks}
            if m.model:
                message["model"] = m.model
            if m.tokens_in or m.tokens_out or m.cache_read_tokens or m.cache_creation_tokens:
                message["usage"] = {
                    "input_tokens": m.tokens_in or 0,
                    "output_tokens": m.tokens_out or 0,
                }
                # Prompt-cache halves ride along when recorded (Postgres
                # app-state; the frozen DuckDB backend has no column and
                # leaves them None) — without them the transcript viewer's
                # token line under-reports a cache-heavy chat session by
                # exactly the dominant term. Keys match the Anthropic usage
                # shape every downstream reader already parses; omitted
                # (not zeroed) when unrecorded, so "unknown" stays distinct
                # from "measured zero".
                if m.cache_read_tokens is not None:
                    message["usage"]["cache_read_input_tokens"] = m.cache_read_tokens
                if m.cache_creation_tokens is not None:
                    message["usage"]["cache_creation_input_tokens"] = m.cache_creation_tokens
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


def export_chat_session_jsonl(chat_id: str) -> Path | None:
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

    Reads EVERY message via :func:`_list_all_chat_messages` (paging past
    ``list_messages``'s own 500-row default), never a single truncated
    page — a chat past that many messages must not silently lose its tail.

    Idempotent: re-exporting an unchanged session overwrites the file with
    identical content; the session pipeline's hash-based dedup
    (``services/session_pipeline/lib.compute_file_hash``) treats that as
    already-processed, so a repeat call from the sweep costs one write and
    no reprocessing.

    The jsonl's own mtime is left at ``os.replace``'s natural wall-clock
    write time — ``services/session_processor_state.py::scan_unprocessed_for``
    already gates reprocessing on that mtime advancing past a prior
    ``processed_at``, and messages can be arbitrarily older than the moment
    they are (re-)exported (the exact case this function's own fix for a
    500-row-truncated backlog creates: exporting a message from an hour ago
    must not look, to that OTHER gate, like the file was written an hour
    ago). :func:`_export_watermark` of the messages actually read is
    instead recorded in a separate sidecar
    (:func:`_write_export_watermark`) that only :func:`is_chat_export_stale`
    reads — so a message committed between the read above and the
    ``os.replace`` below (present in neither) is newer than the recorded
    watermark and is caught on the very next check, without perturbing the
    unrelated mtime-based gate.
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
        messages = _list_all_chat_messages(chat_id, chat_message_repo())
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

    watermark = _export_watermark(messages)
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
        _write_export_watermark(target, watermark)
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


def is_chat_export_stale(existing_path: Path | None, last_message_at: datetime | None) -> bool:
    """True when a chat session's exported jsonl is missing, unreadable, or
    older than the session's last message.

    The ONE staleness rule shared by the periodic sweep
    (``services/session_pipeline/runner.py::_sweep_chat_session_exports``)
    and the on-demand admin transcript viewer
    (:func:`ensure_chat_transcript_current`) — kept in a single function so
    "is this transcript current" never drifts between the two callers.

    ``last_message_at=None`` (a session with no messages yet) is never
    stale: there is nothing to export, so re-checking on every call would
    be pointless work for a session that will never produce a file.

    Compares against :func:`_read_export_watermark` — the newest message
    ``export_chat_session_jsonl`` actually wrote, recorded in a sidecar file
    alongside ``existing_path`` — rather than ``existing_path``'s own
    mtime. The file's mtime is deliberately NOT this signal: it is left at
    the wall-clock write time for ``services/session_processor_state.py``'s
    own, unrelated mtime-vs-``processed_at`` invalidation gate, and a
    message can be older than the moment it happens to get (re-)exported
    (backfilling a previously-truncated conversation, for one). Falls back
    to the file's mtime only when there is no sidecar to read — an export
    written before this mechanism existed, or one whose sidecar write
    failed — so an old export is not treated as permanently stale just
    because it predates the sidecar.

    A message committed between :func:`export_chat_session_jsonl`'s read
    and its ``os.replace`` is absent from both the jsonl and the sidecar,
    so it is newer than the recorded watermark and shows up as stale here
    on the very next call — this is what actually closes the write race,
    the file's own mtime plays no part in it.
    """
    if last_message_at is None:
        return False
    if existing_path is None or not existing_path.is_file():
        return True
    watermark = _read_export_watermark(existing_path)
    if watermark is None:
        try:
            watermark = datetime.fromtimestamp(existing_path.stat().st_mtime, tz=UTC)
        except OSError:
            return True
    last_active = _as_utc(last_message_at)
    return last_active > watermark


@dataclass
class ChatTranscriptFreshness:
    """What :func:`ensure_chat_transcript_current` learned about one chat
    session, for a caller (``app/api/admin_sessions.py::transcript``) that
    needs to either render a current transcript or explain, honestly, why
    there isn't one — rather than a bare 404 indistinguishable from "this
    session never had a transcript at all".

    ``path`` is the current, on-disk export when one exists or was just
    created; ``None`` otherwise. The remaining fields say why not:
    ``session_found=False`` means the chat id itself doesn't resolve;
    ``export_disabled=True`` means ``sessions.include_chat`` is off (the
    session may well have messages, but this instance never materializes
    them to disk); ``message_count``/``last_message_at`` describe a
    session that DOES exist but produced no file for some other reason
    (owner unresolvable, or the write itself failed).
    """

    path: Path | None
    session_found: bool
    export_disabled: bool
    message_count: int
    last_message_at: datetime | None


def ensure_chat_transcript_current(chat_id: str) -> ChatTranscriptFreshness:
    """On-demand counterpart to the periodic sweep: bring ``chat_id``'s
    exported jsonl current right now if it is missing or stale
    (:func:`is_chat_export_stale`), reusing the same idempotent writer
    (:func:`export_chat_session_jsonl`) the sweep and the kill/archive hooks
    already call.

    Used by the admin transcript viewer so an operator investigating a live
    incident sees the current transcript immediately, rather than waiting
    up to the sweep's own scheduler cadence for the next tick. Never
    raises: every failure mode (unknown session, disabled feature,
    unresolvable owner, a write error inside ``export_chat_session_jsonl``)
    comes back as ``path=None`` with enough context on the returned
    :class:`ChatTranscriptFreshness` for the caller to explain *why*.
    """
    export_disabled = not feature_enabled(
        "sessions", "include_chat", env_var="AGNES_SESSIONS_INCLUDE_CHAT", default=True
    )

    from src.repositories import chat_session_repo, users_repo

    try:
        session = chat_session_repo().get_session(chat_id)
    except Exception:
        logger.warning("chat transcript freshness: session lookup failed for %s", chat_id, exc_info=True)
        session = None

    if session is None:
        return ChatTranscriptFreshness(
            path=None,
            session_found=False,
            export_disabled=export_disabled,
            message_count=0,
            last_message_at=None,
        )

    freshness = ChatTranscriptFreshness(
        path=None,
        session_found=True,
        export_disabled=export_disabled,
        message_count=session.message_count or 0,
        last_message_at=session.last_message_at,
    )

    # Respect the flag even though the session itself resolved fine — an
    # instance with chat export disabled must never write to disk just
    # because an admin opened the viewer.
    if export_disabled:
        return freshness

    try:
        owner = users_repo().get_by_email(session.user_email)
    except Exception:
        logger.warning("chat transcript freshness: owner lookup failed for %s", chat_id, exc_info=True)
        owner = None
    if not owner:
        return freshness

    existing = _session_data_dir() / owner["id"] / f"chat-{chat_id}.jsonl"
    if existing.is_file() and not is_chat_export_stale(existing, session.last_message_at):
        freshness.path = existing
        return freshness

    freshness.path = export_chat_session_jsonl(chat_id)
    return freshness
