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

import hashlib
import json
import logging
import os
import uuid
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


@dataclass(frozen=True)
class ExportWatermark:
    """What an exported jsonl's content can vouch for: the newest message it
    actually contains, and how many messages that was.

    The count is not redundant with the timestamp. ``created_at`` ties are
    routine under load -- routine enough that this module's own pagination
    had to start ordering by ``(created_at, id)`` to stop losing a row at a
    page boundary -- so a message committed between the exporter's read and
    its write can carry the SAME timestamp as the newest one written.
    Against a timestamp alone that message is invisible *forever*: the
    strict ``>`` in :func:`is_chat_export_stale` never fires, the session's
    own ``last_message_at`` equals the recorded watermark, and no later
    check ever exports it. ``chat_messages`` maintains ``message_count``
    and ``last_message_at`` in one statement on every append, so the count
    moves for exactly the tie the timestamp cannot see.

    ``messages=None`` is a sidecar written before the count existed: the
    timestamp comparison still applies, the tie check simply cannot.
    """

    last_message_at: datetime
    messages: int | None
    #: SHA-256 of the exported jsonl these two values describe -- see
    #: :func:`_content_digest`. ``None`` on a sidecar written before it
    #: existed, which :func:`is_chat_export_stale` cannot verify and so
    #: treats exactly as it treats a missing sidecar.
    content_sha256: str | None = None
    #: ``(st_size, st_mtime_ns)`` of the transcript at the moment this
    #: sidecar was written -- an identity token, NOT a staleness signal.
    #: See :func:`_transcript_identity`.
    content_identity: tuple[int, int] | None = None


def _content_digest(content: str) -> str:
    """SHA-256 of an exported jsonl's exact bytes -- the token that PAIRS a
    watermark with the transcript it describes.

    The two are separate files, so each ``os.replace`` is atomic but the
    pair is not: two overlapping writers can leave writer A's older
    transcript beside writer B's newer watermark, and a watermark claiming
    coverage the transcript next to it does not have is worse than no
    watermark at all -- it reports the session current forever. Recording
    the digest makes that pairing checkable, so a mismatched pair is simply
    stale and the next export heals it. The content is its own generation
    id; any other one would have to be embedded in the jsonl, whose shape
    the session pipeline consumes.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _transcript_identity(target: Path) -> tuple[int, int] | None:
    """``(st_size, st_mtime_ns)`` of *target*, or ``None`` if it cannot be
    stat'd -- the cheap answer to "is this still the exact file the sidecar
    beside it hashed", recorded by :func:`_write_export_watermark` and
    re-checked by :func:`is_chat_export_stale`.

    Read the boundary carefully, because this module is otherwise emphatic
    that a transcript's mtime must play no part in freshness (it belongs to
    ``session_processor_state.scan_unprocessed_for``'s unrelated gate, see
    :func:`export_chat_session_jsonl`). Nothing here decides whether a
    transcript is CURRENT -- the timestamp and count still do that, alone.
    This decides only whether the file has been replaced since the sidecar
    was written, and it can only ever save work: a match means the pair is
    the one we hashed, so the digest cannot have changed and re-reading the
    whole jsonl to prove it would be pure cost; a mismatch concludes
    nothing at all and falls through to the digest. So a wrong answer here
    -- a filesystem with coarse mtimes, a same-size same-instant rewrite --
    costs a hash, never a wrong verdict.

    That cost matters: the periodic sweep runs this check over up to 200
    recently-active sessions on every processor tick, and hashing every one
    of those transcripts in full each time is exactly the work the sweep's
    own docstring claims it does not do.
    """
    try:
        st = target.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _export_watermark(messages: list[ChatMessage], content: str) -> ExportWatermark:
    """The newest ``created_at`` actually present among *messages*, how many
    there were, and the digest of the *content* those two values describe --
    i.e. exactly what the exported file can vouch for. Recorded alongside
    the file by :func:`_write_export_watermark` and read back by
    :func:`is_chat_export_stale`, deliberately NOT via the file's own mtime
    (see that function's docstring for why the two must stay separate).

    *content* is taken here, next to the values it belongs with, so a
    watermark can never be built without the transcript it vouches for."""
    newest = max((m.created_at for m in messages if m.created_at is not None), default=None)
    return ExportWatermark(
        last_message_at=_as_utc(newest) if newest is not None else datetime.now(UTC),
        messages=len(messages),
        content_sha256=_content_digest(content),
    )


#: Public alias of :func:`_content_digest` for the one caller outside this
#: module that has to answer "are these the bytes that were verified" --
#: ``app/api/admin_sessions.py``'s transcript route. Exported rather than
#: reimplemented so the two can never disagree about the algorithm.
content_digest = _content_digest


def _watermark_path(target: Path) -> Path:
    """The sidecar file :func:`_write_export_watermark` /
    :func:`_read_export_watermark` use to record the content watermark for
    *target* (an exported chat jsonl), named alongside it rather than
    reusing ``target``'s own mtime -- see :func:`is_chat_export_stale`."""
    return target.with_name(target.name + ".watermark")


def _write_export_watermark(target: Path, watermark: ExportWatermark, identity: tuple[int, int] | None = None) -> None:
    """Atomically record *watermark* (:func:`_export_watermark` of the
    messages just written to *target*) in its sidecar file -- via
    :func:`_atomic_write_text`, so a reader never observes a half-written
    watermark AND two writers racing the same sidecar (see that function's
    docstring) can't interleave or clobber each other either.

    Written as a JSON object rather than the bare ISO timestamp this
    sidecar used to hold, because the timestamp alone cannot see a tie
    (see :class:`ExportWatermark`) and neither half can tell whether the
    transcript beside it is the one it describes (see
    :func:`_content_digest`). :func:`_read_export_watermark` still reads the
    old shape, so an instance upgrading in place keeps its existing exports
    instead of re-exporting every session at once.
    """
    _atomic_write_text(
        _watermark_path(target),
        json.dumps(
            {
                "last_message_at": watermark.last_message_at.isoformat(),
                "messages": watermark.messages,
                "content_sha256": watermark.content_sha256,
                # *identity* must come from the caller, taken from the
                # STAGED transcript before it was published (see
                # :func:`_atomic_write_text`). Stat'ing `target` here
                # instead was wrong in a way that defeated the digest: a
                # writer replacing the destination between the transcript's
                # publish and that stat left this sidecar pairing OUR
                # digest and counts with THEIR file's identity, and the
                # identity fast path in `is_chat_export_stale` then
                # skipped the very digest that would have caught the torn
                # pair -- trusting a truncated transcript indefinitely.
                # Omitted (None) is always safe: it only costs a hash.
                "content_size": identity[0] if identity else None,
                "content_mtime_ns": identity[1] if identity else None,
            }
        ),
    )


def _read_export_watermark(target: Path) -> ExportWatermark | None:
    """The content watermark :func:`_write_export_watermark` recorded for
    *target*, or ``None`` when there is no sidecar to read (an export
    written before this mechanism existed, or one whose sidecar write
    failed). :func:`is_chat_export_stale` treats ``None`` as unconditionally
    stale rather than falling back to *target*'s own mtime: that mtime is
    reserved for ``session_processor_state.scan_unprocessed_for``'s own,
    unrelated gate (see :func:`export_chat_session_jsonl`'s docstring), and
    trusting it here whenever the sidecar is merely missing would silently
    reopen the exact race a failed sidecar write can produce -- a message
    committed between the jsonl read and its replace is absent from the
    file either way, and a stale sidecar write is the only thing that would
    have caught it. One extra, harmless re-export costs less than that."""
    try:
        text = _watermark_path(target).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("last_message_at")
        count = payload.get("messages")
        if not isinstance(raw, str):
            return None
        try:
            digest = payload.get("content_sha256")
            size = payload.get("content_size")
            mtime_ns = payload.get("content_mtime_ns")
            identity = (size, mtime_ns) if isinstance(size, int) and isinstance(mtime_ns, int) else None
            return ExportWatermark(
                last_message_at=_as_utc(datetime.fromisoformat(raw)),
                messages=count if isinstance(count, int) else None,
                content_sha256=digest if isinstance(digest, str) else None,
                content_identity=identity,
            )
        except ValueError:
            return None
    # Pre-count sidecar: a bare ISO timestamp. Readable, just blind to ties.
    try:
        return ExportWatermark(last_message_at=_as_utc(datetime.fromisoformat(text)), messages=None)
    except ValueError:
        return None


def _atomic_write_text(target: Path, content: str) -> tuple[int, int] | None:
    """Write *content* to *target* so no reader ever observes a partial or
    interleaved file: stage under a per-call, globally-unique temp name in
    *target*'s own directory, then ``os.replace`` onto *target*.

    A chat session's export (and its watermark sidecar) can be written from
    several places racing each other -- the periodic sweep, the kill/archive
    teardown hooks, and the on-demand admin transcript route, potentially
    from different processes and with no cross-process lock between them. A
    FIXED temp name (``<target>.tmp``) let two concurrent writers share one
    buffer and interleave into a corrupt file, and let one writer's cleanup
    delete the other's still-in-flight temp out from under it (the exact
    incident class ``src/parquet_publish.py`` documents for the
    analytics-extract writers, which this mirrors). A per-call ``uuid4``
    name means two overlapping writers never touch the same temp file at
    all: the worst case is "the writer that finishes last wins", never a
    merged or truncated result.

    On any failure the temp is removed and the exception propagates;
    *target* is left exactly as it was before the call.

    Returns the ``(st_size, st_mtime_ns)`` of the STAGED file, stat'd before
    the replace -- see :func:`_transcript_identity` for what that is for.
    ``os.replace`` is a rename, so the published file carries the temp
    file's inode and therefore exactly these values; stat'ing *target*
    afterwards instead would return whatever another writer had replaced it
    with in the meantime. ``None`` if the stat itself failed, which callers
    must treat as "no identity", never as a match.
    """
    tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        try:
            st = tmp.stat()
            identity: tuple[int, int] | None = (st.st_size, st.st_mtime_ns)
        except OSError:
            identity = None
        os.replace(tmp, target)
        return identity
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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
    ``${SESSION_DATA_DIR}/<users.id>/chat-<chat_id>.jsonl`` (atomically —
    see :func:`_atomic_write_text`) and return the path — or ``None``,
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

    target_dir = _session_data_dir() / owner["id"]
    target = target_dir / f"chat-{chat_id}.jsonl"
    content = "".join(json.dumps(turn, default=str) + "\n" for turn in turns)
    # Built from the content as well as the messages, so the sidecar is
    # pinned to THIS transcript and an overlapping writer cannot leave its
    # newer watermark vouching for our older bytes -- see `_content_digest`.
    watermark = _export_watermark(messages, content)
    # Do not publish backwards. Each os.replace is atomic and the digest
    # above makes a TORN pair detectable, but neither orders generations: a
    # slower writer that read fewer messages could still land last and
    # replace a newer transcript with a coherent, older one. The request
    # that just certified the newer file would then be serving the older
    # one. So if what is already on disk covers everything this snapshot
    # has, leave it alone -- reusing the whole staleness rule, digest check
    # included, so a torn or older pair is still healed by writing.
    #
    # This narrows the window rather than closing it (another writer can
    # publish between this check and the replace below); a loser still
    # heals, because a transcript covering fewer messages than the session
    # has is exactly what `is_chat_export_stale` reports on the next call.
    if target.is_file() and not is_chat_export_stale(target, watermark.last_message_at, watermark.messages):
        return target

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        identity = _atomic_write_text(target, content)
        _write_export_watermark(target, watermark, identity)
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


def _stale_with_watermark(
    existing_path: Path | None,
    last_message_at: datetime | None,
    message_count: int | None = None,
) -> tuple[bool, ExportWatermark | None]:
    """True when a chat session's exported jsonl is missing, unreadable, or
    older than the session's last message -- AND the watermark that answer
    was reached from, ``None`` when there was none to validate.

    The second half exists for a caller that goes on to CERTIFY the
    transcript: it must take the digest from the very read this verdict was
    computed on, never re-read the sidecar afterwards. Those are two
    different generations the moment anything republishes the file in
    between, and a digest fetched by that later read describes a generation
    nothing ever compared against the session -- which the certifying
    caller would then report as verified. See
    ``ChatTranscriptFreshness.content_sha256``. Callers that want only the
    verdict use :func:`is_chat_export_stale`, the plain-boolean face of
    this.

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
    alongside ``existing_path`` — never ``existing_path``'s own mtime. The
    file's mtime is deliberately NOT this signal: it is left at the
    wall-clock write time for ``services/session_processor_state.py``'s
    own, unrelated mtime-vs-``processed_at`` invalidation gate, and a
    message can be older than the moment it happens to get (re-)exported
    (backfilling a previously-truncated conversation, for one). A missing
    or unreadable sidecar (an export written before this mechanism existed,
    or one whose sidecar write itself failed) is unconditionally stale
    rather than falling back to mtime: that fallback would silently reopen
    the exact write race this mechanism exists to close whenever the
    sidecar write is the thing that failed, so the one-time cost of
    re-exporting a still-good legacy file is the safer trade.

    A message committed between :func:`export_chat_session_jsonl`'s read
    and its ``os.replace`` is absent from both the jsonl and the sidecar,
    so it shows up as stale here on the very next call — this is what
    actually closes the write race, the file's own mtime plays no part in
    it. Closing it takes BOTH halves of the watermark: a message that
    landed in the same instant as the newest one written is not *newer*
    than the recorded timestamp, so against the timestamp alone it would
    stay invisible forever (see :class:`ExportWatermark`). *message_count*
    — the session's own row count, moved by the same statement that moves
    ``last_message_at`` — is what catches that tie. Passing it is optional
    only because a sidecar written before the count existed cannot answer
    the question either way; every caller in this repo passes it.
    """
    if last_message_at is None:
        return False, None
    if existing_path is None or not existing_path.is_file():
        return True, None
    watermark = _read_export_watermark(existing_path)
    if watermark is None:
        return True, None
    # The pair has to belong together. Each file is replaced atomically, the
    # PAIR is not, so two overlapping writers can leave one writer's
    # transcript beside the other's watermark -- and a watermark vouching
    # for content that is not there reports the session current forever.
    # Verify rather than serialize: a mismatch is simply stale, and the next
    # export heals it. A sidecar predating the digest cannot be checked and
    # is treated exactly like a missing one, for the reason given above.
    if watermark.content_sha256 is None:
        return True, watermark
    # Hash only when the file might have changed under us. An unchanged
    # `(size, mtime_ns)` means this is byte-for-byte the transcript the
    # sidecar hashed, so the digest below cannot differ -- and re-reading
    # every transcript in full on every sweep tick, for up to 200 sessions
    # per processor, is the whole cost of the check. See
    # `_transcript_identity` for why this can only save work, never decide.
    if watermark.content_identity is None or _transcript_identity(existing_path) != watermark.content_identity:
        try:
            if _content_digest(existing_path.read_text(encoding="utf-8")) != watermark.content_sha256:
                return True, watermark
        except OSError:
            return True, watermark
    if _as_utc(last_message_at) > watermark.last_message_at:
        return True, watermark
    # Strictly MORE messages than we wrote, at a timestamp we already have:
    # the tie above. Never `!=` — a count that has drifted low (nothing in
    # this repo deletes chat messages, but a restore or a manual fix could)
    # would otherwise re-export the same session on every sweep tick.
    if message_count is not None and watermark.messages is not None and message_count > watermark.messages:
        return True, watermark
    return False, watermark


def is_chat_export_stale(
    existing_path: Path | None,
    last_message_at: datetime | None,
    message_count: int | None = None,
) -> bool:
    """The ONE staleness rule -- see :func:`_stale_with_watermark`, which
    this is the plain-boolean face of. Every caller that only needs the
    verdict uses this; a caller that then CERTIFIES the transcript must use
    the underlying helper instead, so the digest it reports comes from the
    same read as the verdict.
    """
    return _stale_with_watermark(existing_path, last_message_at, message_count)[0]


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
    ``lookup_failed=True`` means we never got to find out — the session
    store raised — and must never be reported as a missing session, which
    would tell an admin to check an id that may well be correct;
    ``raced=True`` means we DID write a file but a message landed while we
    were writing it, so ``path`` is real content that is already one
    message behind — the caller may serve it, but must not call it
    current;
    ``export_disabled=True`` means ``sessions.include_chat`` is off (the
    session may well have messages, but this instance never materializes
    them to disk); ``message_count``/``last_message_at`` describe a
    session that DOES exist but produced no file for some other reason
    (owner unresolvable, or the write itself failed).
    """

    path: Path | None
    session_found: bool
    lookup_failed: bool
    export_disabled: bool
    message_count: int
    last_message_at: datetime | None
    raced: bool = False
    #: Digest of the exact transcript this verdict covers, when there is
    #: one -- see :func:`_content_digest`. A caller that renders the file
    #: must confirm the bytes it read still hash to this before repeating
    #: the verdict: ``path`` names a file, and a file can be replaced
    #: between our check and their read (an ordinary re-export, or the
    #: narrow residue the publish-order guard in
    #: :func:`export_chat_session_jsonl` leaves open). The verdict is about
    #: a generation, not about a filename.
    content_sha256: str | None = None


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

    lookup_failed = False
    try:
        session = chat_session_repo().get_session(chat_id)
    except Exception:
        # A store that raised is not a store that answered "no such row".
        # Collapsing the two tells an admin to check an id while the real
        # problem is the database, and it is the reader who then wastes the
        # next ten minutes.
        logger.warning("chat transcript freshness: session lookup failed for %s", chat_id, exc_info=True)
        session = None
        lookup_failed = True

    if session is None:
        return ChatTranscriptFreshness(
            path=None,
            session_found=False,
            lookup_failed=lookup_failed,
            export_disabled=export_disabled,
            message_count=0,
            last_message_at=None,
        )

    freshness = ChatTranscriptFreshness(
        path=None,
        session_found=True,
        lookup_failed=False,
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
    # `session` was read before the owner lookup and this file check, so a
    # "not stale" verdict against it only means "not stale as of a snapshot
    # that is already several I/O calls old" — confirm against a current
    # read before certifying, and when the session has moved on, fall
    # through and export rather than merely labelling the file behind.
    if existing.is_file() and not is_chat_export_stale(existing, session.last_message_at, session.message_count):
        behind, validated = _is_stale_against_a_fresh_read(chat_id, existing)
        if not behind:
            freshness.path = existing
            # The digest comes from the read that produced `behind`, never
            # from a fresh look at the sidecar afterwards: a writer
            # publishing between the two would hand us the digest of a
            # generation nothing had compared against the session, and the
            # route would then repeat THIS verdict over those bytes.
            freshness.content_sha256 = validated.content_sha256 if validated else None
            return freshness

    freshness.path = export_chat_session_jsonl(chat_id)
    if freshness.path is not None:
        freshness.raced, validated = _is_stale_against_a_fresh_read(chat_id, freshness.path)
        freshness.content_sha256 = validated.content_sha256 if validated else None
    return freshness


def _is_stale_against_a_fresh_read(chat_id: str, exported: Path) -> tuple[bool, ExportWatermark | None]:
    """True when *exported* is already behind ``chat_id`` as of a session
    row read right now.

    Both callers need this for the same reason. Every value
    :func:`ensure_chat_transcript_current` decides on is a snapshot taken
    before some I/O: the session row is read before the owner lookup and
    the file check, and ``export_chat_session_jsonl`` reads the messages
    before it writes. A message committed inside either window is in
    neither the file nor its watermark — and the response that TRIGGERED
    the refresh is precisely the one that would otherwise serve that file
    as confirmed-current, which is the single thing this whole freshness
    path exists to stop. So we re-read the session (the cheap single row,
    not the messages again) and re-run the same staleness rule against the
    file we are actually about to certify.

    This narrows the window from "however long the export took" — seconds,
    on a conversation long enough to page — to the gap between this read
    and the response, and it cannot be closed entirely: a message committed
    after this line is not knowable here. That residue is why the answer is
    a claim about a check we ran, never a promise about the future.
    """
    from src.repositories import chat_session_repo

    try:
        latest = chat_session_repo().get_session(chat_id)
    except Exception:
        # We could not check. Saying "current" would be a claim we did not
        # verify; saying "raced" is the honest, conservative reading -- and
        # with no validated watermark to hand back, a caller cannot certify
        # any generation either.
        logger.warning("chat transcript freshness: post-export re-check failed for %s", chat_id, exc_info=True)
        return True, None
    if latest is None:
        return False, _read_export_watermark(exported)
    return _stale_with_watermark(exported, latest.last_message_at, latest.message_count)
