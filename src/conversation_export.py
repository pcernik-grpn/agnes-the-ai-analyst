"""The conversation-corpus export — one record per chat session, built for
evaluation rather than for the LLM-call telemetry (design 2026-09-08 §3.12).

Telemetry (spans, ``llm_calls`` rows) is one row per LLM call, content
capped, aimed at "what did this cost and where does it burn". This export
is the opposite shape: one COMPLETE record per conversation, assembled
on-instance from what the instance already keeps (``chat_sessions``,
``chat_messages``, ``llm_calls``, ``chat_message_feedback``,
``agent_memories``), for "why are the answers bad, at scale".

Four functions, kept pure of any repository/HTTP concern so they are
testable without a database:

- :func:`build_conversation_record` — one session's data in, the spec 3.12
  record shape out. Never receives an email: the caller resolves
  ``user_id`` before this is called, so there is nothing here to leak.
- :func:`iter_conversations` — one page of sessions (keyset on
  ``(last_message_at, id)``), the bulk-by-session-id repo reads, and the
  per-session record assembly. Takes a :class:`ConversationExportRepoBundle`
  so the caller (the API route) supplies real repos and the tests supply
  fakes.
- :func:`records_for_session_ids` — the same bulk reads and per-session
  assembly as :func:`iter_conversations`, but for an explicit list of
  session ids rather than a ``(since, until)`` keyset page — the push
  sink's "late feedback" aux sweep uses this to re-export a conversation
  that already scrolled past the main walk once its feedback changes.
- :func:`serialize_jsonl` — records to newline-delimited JSON bytes, for
  ``StreamingResponse``.

**Content policy.** ``content_mode`` is ``"full"`` or ``"pseudonymized"`` —
never ``"off"``; the caller (the route) refuses the whole request under
``off`` before any of this runs (spec 3.12, "under the content policy").
Under ``pseudonymized`` every text leaf in ``messages_json``,
``tool_calls_json``, ``first_user_message`` and ``feedback_json[].comment``
goes through ``anonymizer`` exactly once (never per span, never twice for
the same leaf shared between the two JSON blobs) — see
:func:`_pseudonymize_parts`. Ids and timestamps are never touched.
``memory_writes_json`` never carries free text at all — only
``content_length`` — so there is nothing to scrub there.

**``conversation_end`` vs. the pull endpoint's keyset cursor.** The pull
route pages sessions on ``chat_sessions.last_message_at``, while
``conversation_end`` here is the last message's own ``created_at``
(``ordered[-1]``). The two are the same value by construction, not merely
close: on Postgres ``ChatMessagePgRepository.append_message`` writes both
columns from the SAME ``now`` variable inside one transaction
(``src/repositories/chat_messages_pg.py``); on the frozen DuckDB backend
``last_message_at`` is never stored at all — it is derived at READ time as
``MAX(m.created_at)`` over exactly this table (``_SESSION_SELECT`` in
``app/chat/persistence.py``). Paging on one and reporting the other is
therefore safe without re-deriving anything here.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.llm_pricing import cost_usd

#: What ``content_mode`` may be on a built record — "off" never reaches this
#: module (the route refuses the request before calling in).
CONTENT_MODES = ("full", "pseudonymized")


# ---------------------------------------------------------------------------
# Cursor codec — same shape as ``corpus_file_events_pg.py``'s
# encode_cursor/decode_cursor: an opaque, URL-safe token for one
# (last_message_at, session_id) keyset position.
# ---------------------------------------------------------------------------


def encode_cursor(last_message_at: datetime, session_id: str) -> str:
    """Opaque, URL-safe pagination token for one keyset position."""
    raw = f"{last_message_at.isoformat()}|{session_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Inverse of :func:`encode_cursor`. Raises ``ValueError`` on a malformed
    token — the caller (the API route) turns that into a typed 400, never a
    500 from a bad comparison downstream."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts_str, session_id = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), session_id
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError(f"malformed cursor: {cursor!r}") from exc


# ---------------------------------------------------------------------------
# Repo bundle — everything iter_conversations needs to build one page.
# ---------------------------------------------------------------------------


@dataclass
class ConversationExportRepoBundle:
    """The repos (and the resolved policy) one export page needs.

    ``sessions``/``messages``/``calls``/``feedback``/``memories`` are the
    PG repositories this feature reads (``chat_session_repo()``,
    ``chat_message_repo()``, ``llm_calls_repo()``,
    ``chat_message_feedback_repo()``, ``agent_memories_repo()``); ``users``
    resolves an owner's ``user_id`` from their ``user_email`` (only
    ``get_by_email`` is called). Kept as a plain bundle rather than five
    separate keyword arguments so a test can hand in five small fakes and
    the route can hand in five real repos, with the same call shape either
    way.
    """

    sessions: Any
    messages: Any
    calls: Any
    feedback: Any
    memories: Any
    users: Any
    content_mode: str
    anonymizer: Callable[[str], str] | None = None
    #: Cache of email -> resolved user_id, filled in as sessions are
    #: processed (a page rarely spans more than a handful of distinct
    #: owners, so this reduces the resolution from O(sessions) calls to
    #: O(distinct owners)). Exposed for tests; callers never set it.
    _user_id_cache: dict[str, str | None] = field(default_factory=dict)


def _resolve_user_id(bundle: ConversationExportRepoBundle, email: str | None) -> str | None:
    if not email:
        return None
    if email in bundle._user_id_cache:
        return bundle._user_id_cache[email]
    resolved: str | None = None
    try:
        user = bundle.users.get_by_email(email)
        resolved = (user or {}).get("id")
    except Exception:  # noqa: BLE001 — a lookup failure never breaks the export
        resolved = None
    bundle._user_id_cache[email] = resolved
    return resolved


# ---------------------------------------------------------------------------
# Content transforms — applied once per leaf, shared by messages_json and
# tool_calls_json (they read the SAME transformed parts).
# ---------------------------------------------------------------------------


def _walk_strings(value: Any, fn: Callable[[str], str]) -> Any:
    """Apply ``fn`` to every string leaf in ``value``, preserving shape.
    Non-string scalars (ints, bools, None) and container structure pass
    through untouched — only text is ever pseudonymized."""
    if isinstance(value, str):
        return fn(value) if value else value
    if isinstance(value, dict):
        return {k: _walk_strings(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk_strings(v, fn) for v in value]
    return value


def _pseudonymize_parts(parts: list[dict] | None, fn: Callable[[str], str]) -> list[dict] | None:
    """A message's ``parts`` array with every text/args/result leaf run
    through ``fn`` — never ``tool_use_id``, ``type``, ``state``, ``is_error``
    or ``approval``, which are structure, not content."""
    if not parts:
        return parts
    out: list[dict] = []
    for part in parts:
        p = dict(part)
        if p.get("type") == "text" and isinstance(p.get("text"), str):
            p["text"] = fn(p["text"]) if p["text"] else p["text"]
        elif p.get("type") == "tool":
            if "args" in p:
                p["args"] = _walk_strings(p["args"], fn)
            if "result" in p:
                p["result"] = _walk_strings(p["result"], fn)
        out.append(p)
    return out


def _tool_calls_from_parts(turn_id: Any, parts: list[dict] | None, started_at: str | None) -> list[dict]:
    calls: list[dict] = []
    for part in parts or []:
        if part.get("type") != "tool":
            continue
        calls.append(
            {
                "turn_id": turn_id,
                "tool_name": part.get("tool"),
                "input": part.get("args"),
                "output": part.get("result"),
                "is_error": bool(part.get("is_error", False)),
                "started_at": started_at,
            }
        )
    return calls


def _tool_calls_from_legacy_column(
    turn_id: Any,
    tool_calls: list[dict] | None,
    started_at: str | None,
    text_fn: Callable[[str], str],
) -> list[dict]:
    """Fallback for a message with no ``parts`` at all (written before
    schema v123 -- ``app/chat/message_parts.py`` -- so its calls survive
    only in the legacy positionless ``tool_calls`` column). Mirrors the
    same compatibility path the chat-session-jsonl export already has
    (``app/chat/session_export.py::_assistant_blocks``'s legacy fallback).

    That legacy shape is ``[{"tool": ..., "args": ...}]`` only
    (``parts_to_tool_calls``) -- no result, no error flag, no
    ``tool_use_id`` was ever recorded alongside it, so ``output`` stays
    ``None`` and ``is_error`` stays ``False`` here rather than guessing at
    an outcome the row never stored. A killed/cancelled-turn marker
    (``_partial_save``'s ``{"interrupted": True, "reason": ...}``) has no
    ``tool`` key and is skipped, not read as a bogus call.
    """
    calls: list[dict] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        tool_name = call.get("tool")
        if not isinstance(tool_name, str):
            continue
        args = call.get("args")
        if args is not None:
            args = _walk_strings(args, text_fn)
        calls.append(
            {
                "turn_id": turn_id,
                "tool_name": tool_name,
                "input": args,
                "output": None,
                "is_error": False,
                "started_at": started_at,
            }
        )
    return calls


def _iso(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _deployment_environment() -> str:
    """Mirror of ``src.observability.otel._deployment_env`` /
    ``app.logging_config._deployment_env`` — kept in step so the export's
    ``deployment_environment`` field agrees with the label the logs and
    spans carry for this same instance."""
    for var in ("AGNES_DEPLOYMENT_ENV", "RELEASE_CHANNEL"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return "unknown"


def _interrupted_reason(message: Mapping[str, Any] | None) -> str | None:
    """The ``reason`` off an interrupted-turn marker on ``message``'s
    ``tool_calls`` (``app/chat/manager.py``'s ``_partial_save``:
    ``tool_calls=[{"interrupted": True, "reason": ...}, ...]``), or
    ``None``. A killed/cancelled turn still persists a real assistant row
    so the session never dead-ends — this is what tells the corpus that
    row is not a genuine complete answer.
    """
    if not isinstance(message, Mapping):
        return None
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return None
    for call in tool_calls:
        if isinstance(call, Mapping) and call.get("interrupted"):
            reason = call.get("reason")
            return str(reason) if reason is not None else "unknown"
    return None


def _was_cancelled(message: Mapping[str, Any] | None) -> bool:
    """Whether ``message`` is the assistant row a CANCEL left behind.

    ``ChatManager.cancel`` persists ``tool_calls=[{"cancelled": True}]`` on a
    real assistant row so the agent's history reflects the stop. It is not
    the ``interrupted`` marker ``_partial_save`` writes, and without this the
    corpus would report a cancelled answer as a complete one (#2365 review).
    """
    if not isinstance(message, Mapping):
        return False
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False
    return any(isinstance(call, Mapping) and call.get("cancelled") for call in tool_calls)


def _fold_cancelled_marker(totals: dict[str, Any], cancelled: bool) -> dict[str, Any]:
    """Say that the conversation ended in a cancel.

    Its own status, not folded into ``interrupted``: a person pressing stop
    is a different fact from a turn the system lost, and an evaluation
    pipeline reading "the answer was incomplete" needs to know which. Unlike
    an interruption it is NOT an error -- nothing went wrong.

    Which is also why ``has_error`` has to be recomputed rather than left
    alone: cancelling an ANSWER cuts the stream, so the broker records that
    call as ``incomplete``, and the session-wide ``has_error`` would then
    report the cancel itself as a failure.

    But a cancel does not always cut a stream. ``ChatManager.cancel`` can
    land while a TOOL is running, after the completion that asked for the
    tool already finished cleanly — the transcript gets its cancelled
    marker and the ledger gets no incomplete row at all. Forgiving one
    incomplete row regardless would then erase a genuine cut stream from an
    earlier turn (#2365 review). So the row is forgiven only when the
    session's NEWEST call really is the incomplete one, which is the row
    the cancel produced; everything else — an ``error`` row, an earlier
    incomplete one — still counts.
    """
    if not cancelled:
        return totals
    folded = dict(totals)
    # Read BEFORE the status is overwritten below: this is the newest call's
    # own status, and only an `incomplete` one can be the cancel's doing.
    cancel_cut_the_stream = folded.get("last_run_status") == "incomplete"
    folded["last_run_status"] = "cancelled"
    error_count = int(folded.get("error_count") or 0)
    incomplete_count = int(folded.get("incomplete_count") or 0)
    unexplained_incomplete = incomplete_count - (1 if cancel_cut_the_stream else 0)
    folded["has_error"] = error_count > 0 or unexplained_incomplete > 0
    if not folded["has_error"]:
        folded["error_types"] = [t for t in (folded.get("error_types") or []) if t != "stream_incomplete"]
    return folded


def _fold_interrupted_marker(totals: dict[str, Any], reason: str | None) -> dict[str, Any]:
    """Fold the transcript-level interrupted marker into the ``llm_calls``-
    sourced error figures. The killed turn's own completion may never have
    reached ``llm_calls`` at all (the call was still running when the kill
    landed), so without this the record's ``last_run_status`` would report
    an EARLIER, unrelated turn as the session's last word — the marker is
    the more accurate signal of how the conversation actually ended.
    """
    if reason is None:
        return totals
    folded = dict(totals)
    folded["has_error"] = True
    folded["last_run_status"] = "interrupted"
    marker = f"interrupted:{reason}"
    error_types = list(folded.get("error_types") or [])
    if marker not in error_types:
        error_types.append(marker)
    folded["error_types"] = sorted(error_types)
    return folded


def _totals(calls: Mapping[str, Any] | None, messages: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
    """``(totals, cost_status)`` — ``ledger`` when ``calls`` (an
    ``llm_calls``-backed summary) is given, else ``transcript`` when at
    least one message carries token columns, else ``unavailable`` with
    every figure zeroed. Never a silent zero: the caller always has
    ``cost_status`` to tell "measured" from "nothing to measure" apart.
    """
    if calls is not None:
        return (
            {
                "llm_run_count": int(calls.get("llm_run_count") or 0),
                "total_prompt_tokens": int(calls.get("total_prompt_tokens") or 0),
                "total_completion_tokens": int(calls.get("total_completion_tokens") or 0),
                "llm_cache_read_tokens": int(calls.get("llm_cache_read_tokens") or 0),
                "llm_cache_creation_tokens": int(calls.get("llm_cache_creation_tokens") or 0),
                "total_cost": float(calls.get("total_cost") or 0.0),
                "primary_model": calls.get("primary_model"),
                "provider": calls.get("provider"),
                "last_run_status": calls.get("last_run_status"),
                "has_error": bool(calls.get("has_error")),
                "error_types": list(calls.get("error_types") or []),
                # Carried for the cancel fold below, which has to tell the
                # cancelled turn's own cut stream apart from a real failure.
                # Not exported: they are working figures, not record fields.
                "error_count": int(calls.get("error_count") or 0),
                "incomplete_count": int(calls.get("incomplete_count") or 0),
            },
            "ledger",
        )

    token_bearing = [m for m in messages if m.get("tokens_in") is not None or m.get("tokens_out") is not None]
    empty = {
        "llm_run_count": 0,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "llm_cache_read_tokens": 0,
        "llm_cache_creation_tokens": 0,
        "total_cost": 0.0,
        "primary_model": None,
        "provider": None,
        "last_run_status": None,
        "has_error": False,
        "error_types": [],
    }
    if not token_bearing:
        return dict(empty), "unavailable"

    total_cost = round(
        sum(
            cost_usd(
                model=m.get("model"),
                input_tokens=int(m.get("tokens_in") or 0),
                output_tokens=int(m.get("tokens_out") or 0),
                cache_read_tokens=int(m.get("cache_read_tokens") or 0),
                cache_creation_tokens=int(m.get("cache_creation_tokens") or 0),
            )
            for m in token_bearing
        ),
        6,
    )
    models = [m["model"] for m in token_bearing if m.get("model")]
    primary_model = Counter(models).most_common(1)[0][0] if models else None
    return (
        {
            **empty,
            "llm_run_count": len(token_bearing),
            "total_prompt_tokens": sum(int(m.get("tokens_in") or 0) for m in token_bearing),
            "total_completion_tokens": sum(int(m.get("tokens_out") or 0) for m in token_bearing),
            "llm_cache_read_tokens": sum(int(m.get("cache_read_tokens") or 0) for m in token_bearing),
            "llm_cache_creation_tokens": sum(int(m.get("cache_creation_tokens") or 0) for m in token_bearing),
            "total_cost": total_cost,
            "primary_model": primary_model,
            # provider is unknown from chat_messages alone (no column for
            # it) — reporting a guess here would be the exact silent-zero
            # class this function exists to avoid, just for a string
            # instead of a number.
            "provider": None,
        },
        "transcript",
    )


def _feedback_entry(row: Mapping[str, Any], scrub: Callable[[Any], Any]) -> dict[str, Any]:
    """``comment`` is free text (spec 3.12) and goes through the SAME scrub
    the caller applies to messages/tool calls/``first_user_message`` — a
    record's ``content_mode`` describes the whole record, not a subset of it.
    """
    return {
        "turn_id": row.get("turn_id"),
        "user_id": row.get("user_id"),
        "verdict": row.get("verdict"),
        "comment": scrub(row.get("comment")),
        "created_at": _iso(row.get("created_at")),
    }


def _memory_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    # No scrub needed: only `content_length` (an int) leaves here, never the
    # memory's own text.
    content = row.get("content") or ""
    return {
        "memory_id": row.get("id"),
        "turn_id": row.get("source_turn_id"),
        "status": row.get("status"),
        "content_length": len(content),
    }


def build_conversation_record(
    session: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    calls: Mapping[str, Any] | None,
    feedback: Sequence[Mapping[str, Any]],
    memories: Sequence[Mapping[str, Any]],
    *,
    content_mode: str,
    anonymizer: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """One conversation-corpus record (spec 3.12's field table).

    ``session`` carries ``id``, ``surface``, ``agent_id`` and ``user_id`` —
    already resolved by the caller, never an email; this function has no
    way to leak one because it never receives one. ``messages`` is oldest
    first (``chat_messages`` shape: ``role``, ``content``, ``parts``,
    ``turn_id``, ``created_at``, plus the four token columns and ``model``
    for the ``cost_status='transcript'`` fallback). ``calls`` is the
    session's merged ``llm_calls`` totals+statuses, or ``None`` when no
    ledger row exists for it. ``feedback``/``memories`` are the raw
    ``chat_message_feedback``/``agent_memories`` rows for this session.
    """
    if content_mode not in CONTENT_MODES:
        raise ValueError(f"content_mode must be one of {CONTENT_MODES}, got {content_mode!r}")
    if content_mode == "pseudonymized" and anonymizer is None:
        raise ValueError("anonymizer is required when content_mode='pseudonymized'")

    def _text(value: Any) -> Any:
        if content_mode == "pseudonymized" and isinstance(value, str) and value:
            return anonymizer(value)  # type: ignore[misc]
        return value

    ordered = list(messages)
    messages_json: list[dict[str, Any]] = []
    tool_calls_json: list[dict[str, Any]] = []
    for m in ordered:
        created_at_iso = _iso(m.get("created_at"))
        raw_parts = m.get("parts")
        transformed_parts = _pseudonymize_parts(raw_parts, _text) if content_mode == "pseudonymized" else raw_parts
        messages_json.append(
            {
                "role": m.get("role"),
                "content": _text(m.get("content")),
                "turn_id": m.get("turn_id"),
                "created_at": created_at_iso,
                "parts": transformed_parts,
            }
        )
        if raw_parts:
            tool_calls_json.extend(_tool_calls_from_parts(m.get("turn_id"), transformed_parts, created_at_iso))
        else:
            # A row written before `parts` existed (schema v123) keeps its
            # calls only in the legacy `tool_calls` column -- without this
            # fallback a historical conversation exports as if the model
            # used no tools at all (#2365 review).
            tool_calls_json.extend(
                _tool_calls_from_legacy_column(m.get("turn_id"), m.get("tool_calls"), created_at_iso, _text)
            )

    turn_ids = {m.get("turn_id") for m in ordered if m.get("turn_id")}
    # A conversation written before `turn_id` existed carries none at all,
    # and counting only the ids would report a long multi-turn transcript as
    # ZERO turns -- false metadata for an evaluation pipeline, which is
    # worse than a derived figure (#2365 review). Each USER message opens a
    # turn, so the transcript itself answers for those rows: count the
    # distinct ids, plus every user message that carries none.
    turn_count = len(turn_ids) + sum(1 for m in ordered if m.get("role") == "user" and not m.get("turn_id"))
    first_user = next((m for m in ordered if m.get("role") == "user"), None)
    first_user_message = _text(first_user.get("content")) if first_user is not None else None
    last_message = ordered[-1] if ordered else None
    interrupted_reason = _interrupted_reason(last_message)
    cancelled = _was_cancelled(last_message)

    conversation_start = _iso(ordered[0].get("created_at")) if ordered else None
    conversation_end = _iso(ordered[-1].get("created_at")) if ordered else None
    duration_seconds = None
    start_dt, end_dt = (ordered[0].get("created_at"), ordered[-1].get("created_at")) if ordered else (None, None)
    if isinstance(start_dt, datetime) and isinstance(end_dt, datetime):
        duration_seconds = (end_dt - start_dt).total_seconds()

    totals, cost_status = _totals(calls, ordered)
    totals = _fold_interrupted_marker(totals, interrupted_reason)
    totals = _fold_cancelled_marker(totals, cancelled and interrupted_reason is None)

    return {
        "thread_id": session.get("id"),
        "source": "agnes",
        "surface": session.get("surface"),
        "agent_id": session.get("agent_id"),
        "user_id": session.get("user_id"),
        "deployment_environment": _deployment_environment(),
        "conversation_start": conversation_start,
        "conversation_end": conversation_end,
        "duration_seconds": duration_seconds,
        "turn_count": turn_count,
        "message_count": len(ordered),
        "tool_call_count": len(tool_calls_json),
        "tool_calls_sequence": [c["tool_name"] for c in tool_calls_json],
        "llm_run_count": totals["llm_run_count"],
        "total_prompt_tokens": totals["total_prompt_tokens"],
        "total_completion_tokens": totals["total_completion_tokens"],
        "llm_cache_read_tokens": totals["llm_cache_read_tokens"],
        "llm_cache_creation_tokens": totals["llm_cache_creation_tokens"],
        "total_cost": totals["total_cost"],
        "primary_model": totals["primary_model"],
        "provider": totals["provider"],
        "cost_status": cost_status,
        "messages_json": messages_json,
        "tool_calls_json": tool_calls_json,
        "first_user_message": first_user_message,
        "last_message_role": last_message.get("role") if last_message is not None else None,
        "final_assistant_message_complete": bool(
            last_message is not None
            and last_message.get("role") == "assistant"
            and interrupted_reason is None
            and not cancelled
        ),
        "last_run_status": totals["last_run_status"],
        "has_error": totals["has_error"],
        "error_types": totals["error_types"],
        "feedback_json": [_feedback_entry(f, _text) for f in feedback],
        "memory_writes_json": [_memory_entry(m) for m in memories],
        "content_mode": content_mode,
        "exported_at": _iso(datetime.now(UTC)),
    }


#: How long a conversation must have been quiet before it is exported.
#: A session whose newest message is seconds old may be mid-turn (a user
#: message whose answer has not been written yet), and half a turn is worse
#: than a turn that arrives one tick later. Both delivery paths read this:
#: the push sink bounds its walk with it, and the pull endpoint's DEFAULT
#: upper bound lags by it (an explicit `until` is honoured as given).
SETTLE_WINDOW = timedelta(minutes=5)


def _messages_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """``ChatMessage`` dataclasses (the repo's return shape) or plain
    dicts (a test's fakes) -> plain dicts, uniformly."""
    out = []
    for r in rows:
        if is_dataclass(r) and not isinstance(r, type):
            d = dict(r.__dict__)
        else:
            d = dict(r)
        out.append(d)
    return out


def iter_conversations(
    repo_bundle: ConversationExportRepoBundle,
    *,
    since: datetime,
    until: datetime,
    surface: str | None = None,
    surfaces: tuple[str, ...] | None = None,
    agent_id: str | None = None,
    limit: int = 200,
    cursor: str | None = None,
) -> tuple[list[dict[str, Any]], str | None, list[tuple[datetime, str]]]:
    """One page of conversation-corpus records, newest-cursor-forward.

    Fetches ``limit + 1`` rows from ``repo_bundle.sessions.
    list_completed_between`` to detect "more pages exist" without a second
    COUNT query, then bulk-reads messages/calls/feedback/memories for
    exactly the sessions on this page (no per-session queries) before
    building each record. Raises ``ValueError`` for a malformed ``cursor``
    (the route turns that into a typed 400).

    ``surface`` (single value — the pull endpoint's one-surface query
    filter) and ``surfaces`` (a tuple — the push sink's config-driven
    allowlist) are both pushed straight into ``list_completed_between``'s
    own query rather than filtered on the returned rows: a client-side
    filter would let a page come back empty of matches while still
    consuming a keyset position, which is harmless for a one-shot pull but
    wrong for a resumable walk (design 2026-09-08 §3.12, push-sink defect
    fix). ``surfaces`` wins when both are given.

    Returns ``(records, next_cursor, keys)`` — ``keys[i]`` is the
    underlying ``(last_message_at, id)`` keyset position ``records[i]`` was
    built from, straight off the ``chat_sessions`` row, NEVER derived from
    the record's own ``conversation_end`` (the last MESSAGE's timestamp,
    which can diverge from the session's own ``last_message_at`` — e.g. a
    forked session). A caller that only paginates (the pull endpoint)
    ignores ``keys``; a caller persisting how far it has delivered (the
    push sink) uses them instead of anything inside the record body.
    """
    after = decode_cursor(cursor) if cursor else None
    surface_filter = surfaces if surfaces else ((surface,) if surface else None)
    rows = repo_bundle.sessions.list_completed_between(
        since, until, surfaces=surface_filter, agent_id=agent_id, limit=limit + 1, after=after
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return [], None, []

    next_cursor = encode_cursor(rows[-1]["last_message_at"], rows[-1]["id"]) if has_more else None
    keys = [(row["last_message_at"], row["id"]) for row in rows]

    session_ids = [r["id"] for r in rows]
    messages_by_session = repo_bundle.messages.list_for_sessions(session_ids)
    totals_by_session = repo_bundle.calls.totals_for_sessions(session_ids)
    statuses_by_session = repo_bundle.calls.statuses_for_sessions(session_ids)
    feedback_by_session = repo_bundle.feedback.list_for_sessions(session_ids)
    memories_by_session = repo_bundle.memories.list_for_sessions(session_ids)

    records: list[dict[str, Any]] = []
    for row in rows:
        sid = row["id"]
        session_view = {
            "id": sid,
            "surface": row.get("surface"),
            "agent_id": row.get("agent_id"),
            "user_id": _resolve_user_id(repo_bundle, row.get("user_email")),
        }
        messages = _messages_to_dicts(messages_by_session.get(sid, []))
        calls = None
        if sid in totals_by_session:
            calls = {**totals_by_session[sid], **statuses_by_session.get(sid, {})}
        record = build_conversation_record(
            session_view,
            messages,
            calls,
            feedback_by_session.get(sid, []),
            memories_by_session.get(sid, []),
            content_mode=repo_bundle.content_mode,
            anonymizer=repo_bundle.anonymizer,
        )
        records.append(record)
    return records, next_cursor, keys


def records_for_session_ids(
    repo_bundle: ConversationExportRepoBundle,
    session_ids: Sequence[str],
) -> list[dict[str, Any]]:
    """Records for an explicit list of session ids, built through the exact
    same bulk reads + :func:`build_conversation_record` call as
    :func:`iter_conversations`'s per-page loop -- the push sink's "late
    feedback" aux sweep's read path (design 2026-09-08 §3.12, push-sink
    defect fix). A session that already scrolled past the main keyset walk
    still needs to be re-exported when its feedback changes long after it
    settled, and that has nothing to do with ``chat_sessions.last_message_at``
    or any keyset position -- so this takes ids directly rather than a
    ``(since, until)`` window.

    A session id no longer resolvable (e.g. hard-deleted between the aux
    sweep's query and this read) is skipped rather than raising -- the
    delivered corpus simply does not carry it, same as it never carried a
    session deleted before its first export.
    """
    ids = list(dict.fromkeys(session_ids))  # de-dup, keep first-seen order
    if not ids:
        return []

    sessions_by_id: dict[str, Any] = {}
    for sid in ids:
        session = repo_bundle.sessions.get_session(sid)
        if session is not None:
            sessions_by_id[sid] = session
    resolved_ids = list(sessions_by_id.keys())
    if not resolved_ids:
        return []

    messages_by_session = repo_bundle.messages.list_for_sessions(resolved_ids)
    totals_by_session = repo_bundle.calls.totals_for_sessions(resolved_ids)
    statuses_by_session = repo_bundle.calls.statuses_for_sessions(resolved_ids)
    feedback_by_session = repo_bundle.feedback.list_for_sessions(resolved_ids)
    memories_by_session = repo_bundle.memories.list_for_sessions(resolved_ids)

    records: list[dict[str, Any]] = []
    for sid in resolved_ids:
        session = sessions_by_id[sid]
        surface = getattr(session, "surface", None)
        session_view = {
            "id": sid,
            # `.value`: `get_session` returns the `ChatSession` dataclass, whose
            # `surface` is an `app.chat.types.Surface` (str+Enum) -- `iter_conversations`
            # instead reads a plain string off a raw SQL row. Both serialize to the
            # same JSON text either way (Surface IS a str), but normalizing here keeps
            # this function's output shape identical to that one's, not merely
            # equal under `==`.
            "surface": getattr(surface, "value", surface),
            "agent_id": getattr(session, "agent_id", None),
            "user_id": _resolve_user_id(repo_bundle, getattr(session, "user_email", None)),
        }
        messages = _messages_to_dicts(messages_by_session.get(sid, []))
        calls = None
        if sid in totals_by_session:
            calls = {**totals_by_session[sid], **statuses_by_session.get(sid, {})}
        record = build_conversation_record(
            session_view,
            messages,
            calls,
            feedback_by_session.get(sid, []),
            memories_by_session.get(sid, []),
            content_mode=repo_bundle.content_mode,
            anonymizer=repo_bundle.anonymizer,
        )
        records.append(record)
    return records


def serialize_jsonl(records: Iterable[Mapping[str, Any]]) -> Iterator[bytes]:
    """Records -> newline-delimited JSON bytes, one line per record — the
    shape ``StreamingResponse`` streams for the default ``format=jsonl``."""
    for record in records:
        yield (json.dumps(record, default=str) + "\n").encode("utf-8")


__all__ = [
    "CONTENT_MODES",
    "ConversationExportRepoBundle",
    "build_conversation_record",
    "decode_cursor",
    "encode_cursor",
    "iter_conversations",
    "records_for_session_ids",
    "serialize_jsonl",
]
