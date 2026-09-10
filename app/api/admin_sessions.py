"""Admin endpoints for browsing sessions across all users.

Per-user endpoints (`/api/admin/users/{user_id}/sessions/*`) live in
``admin_user_sessions.py``. This module adds:

- ``GET /api/admin/sessions/list``     — cross-user list, filterable
- ``GET /api/admin/sessions/kpis``     — top-bar numbers for the list page
- ``GET /api/admin/sessions/{username}/{session_file}/transcript``
                                        — parsed JSONL events for the viewer

Both backend paths reuse ``_session_data_dir`` + the filename regex from
``admin_user_sessions``; the goal is one source of truth for path safety.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.admin_user_sessions import _SESSION_FILE_RE, _session_data_dir
from app.auth.access import require_admin
from services.session_pipeline.lib import parse_jsonl
from src.repositories import (
    audit_repo,
    usage_repo,
    users_repo,
)

if TYPE_CHECKING:
    from app.chat.session_export import ChatTranscriptFreshness

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sessions", tags=["admin-sessions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _window_since(since_minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=since_minutes)


# ---------------------------------------------------------------------------
# GET /api/admin/sessions/list
# ---------------------------------------------------------------------------


@router.get("/list")
def list_sessions(
    since_minutes: int = Query(default=10080, ge=1, le=525600),  # default 7d
    username: str | None = None,
    model: str | None = None,
    only_errors: bool = False,
    q: str | None = None,
    anchor: str = Query(default="uploaded", pattern="^(started|uploaded)$"),
    sort: str = Query(default="started_at:desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=50000),
    _user: dict = Depends(require_admin),
):
    since = _window_since(since_minutes)
    # anchor=uploaded (default) windows on ARRIVAL, so late queue catch-ups
    # stay visible in recent windows; anchor=started restores the old view.
    filters = {
        "since": since,
        "username": username,
        "model": model,
        "only_errors": only_errors,
        "q": q,
        "anchor": anchor,
    }
    sort_col, _, sort_dir = sort.partition(":")
    direction = "ASC" if (sort_dir or "desc").lower() == "asc" else "DESC"

    repo = usage_repo()
    total = repo.sessions_count(filters)
    rows = repo.sessions_list(
        filters,
        sort_col=sort_col,
        direction=direction,
        limit=limit,
        offset=offset,
    )
    out = []
    for d in rows:
        for k in ("started_at", "ended_at"):
            v = d.get(k)
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        # `session_dir` is the on-disk directory name (UUID for upload-API
        # path, OS-username for the legacy collector). The UI uses this for
        # URL building so the transcript / download endpoints find the file
        # — `username` is now the display email (v60), which is NOT a valid
        # filesystem segment. Derived here rather than stored so older rows
        # don't need a separate backfill. Empty string for rows missing the
        # `<dir>/<file>` shape so the UI defaults to "_" instead of crashing.
        sf = d.get("session_file") or ""
        d["session_dir"] = sf.split("/", 1)[0] if "/" in sf else ""
        out.append(d)
    return {
        "rows": out,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if (offset + limit) < (total or 0) else None,
    }


# ---------------------------------------------------------------------------
# GET /api/admin/sessions/kpis  +  /facets
# ---------------------------------------------------------------------------


@router.get("/kpis")
def kpis(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    username: str | None = None,
    model: str | None = None,
    only_errors: bool = False,
    q: str | None = None,
    anchor: str = Query(default="uploaded", pattern="^(started|uploaded)$"),
    _user: dict = Depends(require_admin),
):
    since = _window_since(since_minutes)
    k = usage_repo().sessions_kpis(
        {
            "since": since,
            "username": username,
            "model": model,
            "only_errors": only_errors,
            "q": q,
            "anchor": anchor,
        }
    )
    tool_calls_total = k["tool_calls_total"]
    error_rate = (k["tool_errors_total"] / tool_calls_total) if tool_calls_total else 0.0
    return {
        "sessions_total": k["sessions_total"],
        "distinct_users": k["distinct_users"],
        "error_sessions": k["error_sessions"],
        "tool_calls_total": tool_calls_total,
        "tool_errors_total": k["tool_errors_total"],
        "tool_error_rate": round(error_rate, 4),
    }


@router.get("/facets")
def facets(
    since_minutes: int = Query(default=10080, ge=1, le=525600),
    _user: dict = Depends(require_admin),
):
    since = _window_since(since_minutes)
    return usage_repo().sessions_facets(since)


# ---------------------------------------------------------------------------
# Transcript viewer
# ---------------------------------------------------------------------------

# Username constraint. The segment is normally the on-disk session DIRECTORY
# name, but `/list` reports `username` as the display e-mail (v60) and that is
# the string an operator copies out of `agnes admin sessions list` — so `@` and
# `+` are in the class and `_resolve_dir_candidates` maps an e-mail onto the
# real directory. Neither character is a path separator on any platform, and
# neither is what keeps the path contained: `..` matched this class before the
# widening too, and is refused by the ``resolve()``/``relative_to(root)`` guard
# in `_resolve_session_target` below. `/`, `\` and NUL stay out of the class.
import re as _re

_USERNAME_RE = _re.compile(r"^[A-Za-z0-9._@+-]{1,200}$")

# A web-chat session's exported jsonl is always named this way
# (app/chat/session_export.py::export_chat_session_jsonl) — matching the
# filename here is what lets the transcript route tell "not exported yet"
# apart from "never had a transcript at all" (see `_chat_id_from_session_file`
# and its one caller below). A legacy CLI-collector filename never matches,
# so it is entirely unaffected — no chat lookaside, no on-demand export.
_CHAT_SESSION_FILE_RE = _re.compile(r"^chat-(?P<chat_id>.+)\.jsonl$")


def _chat_id_from_session_file(session_file: str) -> str | None:
    m = _CHAT_SESSION_FILE_RE.match(session_file)
    return m.group("chat_id") if m else None


def _resolve_dir_candidates(username: str) -> list[str]:
    """URL ``{username}`` segment → on-disk directory names to try, in order.

    The two ingestion paths name their directory differently — ``users.id``
    (upload API, chat export) or the e-mail local-part (legacy collector) —
    and neither is the display e-mail the listing shows. Same pair
    ``admin_user_sessions._user_session_dirs`` scans; resolved here so the
    only identity a caller can actually see is a usable path segment.

    The segment itself is always tried first (that is what the web UI sends
    as ``session_dir``); the derived names are appended, never substituted.
    """
    names = [username]
    if "@" in username:
        local_part = username.split("@", 1)[0]
        if local_part:
            names.append(local_part)
        try:
            row = users_repo().get_by_email_ci(username)
        except Exception:
            logger.debug("session path: user lookup failed for %r", username, exc_info=True)
            row = None
        if row and row.get("id"):
            names.append(str(row["id"]))
    return list(dict.fromkeys(names))


def _resolve_session_target(username: str, session_file: str) -> tuple[str, Path]:
    """Resolve a session jsonl to ``(on-disk dir name, path)``, guarded.

    1. Both segments must match the allowlist regex; rejects `/`, NUL, etc.
    2. Every candidate directory — including the ones derived from an e-mail,
       which are re-validated against the same regex — is joined and then
       ``resolve().relative_to(root)``-checked, so no `..` and no symlink can
       move the final path outside the user-sessions root.
    3. The file must end in ``.jsonl`` (``_SESSION_FILE_RE``).

    The directory name is returned alongside the path because rows in
    ``usage_session_summary`` are keyed on ``<dir>/<file>``: a caller who
    passed the e-mail must still get the right summary row.
    """
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="invalid username")
    if not _SESSION_FILE_RE.match(session_file):
        raise HTTPException(status_code=400, detail="invalid session_file")
    root = _session_data_dir().resolve()
    for idx, name in enumerate(_resolve_dir_candidates(username)):
        if not _USERNAME_RE.match(name):
            # Derived candidates are DB-shaped, not caller-shaped, but they
            # pass the same gate before they may become a path segment.
            continue
        path = (root / name / session_file).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            if idx == 0:
                # The segment the caller actually typed escaped the root —
                # name it, rather than hiding it behind a 404.
                raise HTTPException(status_code=400, detail="path escape rejected")
            continue
        if path.is_file():
            return name, path
    raise HTTPException(status_code=404, detail="session not found")


def _safe_session_path(username: str, session_file: str) -> Path:
    """``_resolve_session_target`` without the directory name."""
    return _resolve_session_target(username, session_file)[1]


def _flatten_text_content(content: Any) -> str:
    """Tool result `content` is often `list[{type:'text', text:'…'}]`. Flatten
    to a string preserving newlines for readable rendering."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


def _render_transcript(turns: list[dict]) -> list[dict]:
    """Flatten a Claude Code session jsonl into a chronological list of
    render-ready event dicts. Each event carries enough context for the UI
    to show role / kind / text / tool-call payload / error flag.

    Three event kinds:
      - ``text``         (role=user|assistant)
      - ``tool_use``     (assistant requested a tool)
      - ``tool_result``  (user-role echo from Claude Code carrying tool output)
    Non-conversational turns (system, summary, file-history-snapshot…) are
    skipped; they're noise for an operator investigating a failure.

    A ``tool_result`` event also carries the ``tool_name`` of the call it
    answers (resolved via ``tool_use_id``) — the raw block only has the id,
    and a result card labeled ``toolu_01Xq…`` gives an operator no way to
    tie the output back to its input.
    """
    # First pass: tool_use_id → tool name, so result events can be labeled.
    tool_names: dict[str, str] = {}
    for turn in turns:
        if turn.get("type") != "assistant":
            continue
        content = (turn.get("message", {}) or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                tool_names[block["id"]] = block.get("name") or ""

    events: list[dict] = []
    for turn in turns:
        ttype = turn.get("type")
        if ttype not in ("user", "assistant"):
            continue
        ts = turn.get("timestamp")
        uuid = turn.get("uuid")
        msg = turn.get("message", {}) or {}
        role = msg.get("role") or ttype
        content = msg.get("content")

        if isinstance(content, str):
            events.append(
                {
                    "kind": "text",
                    "role": role,
                    "text": content,
                    "ts": ts,
                    "uuid": uuid,
                }
            )
            continue
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                events.append(
                    {
                        "kind": "text",
                        "role": role,
                        "text": block.get("text") or "",
                        "ts": ts,
                        "uuid": uuid,
                    }
                )
            elif btype == "tool_use":
                events.append(
                    {
                        "kind": "tool_use",
                        "tool_name": block.get("name"),
                        "input": block.get("input"),
                        "tool_use_id": block.get("id"),
                        "ts": ts,
                        "uuid": uuid,
                    }
                )
            elif btype == "tool_result":
                events.append(
                    {
                        "kind": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "tool_name": tool_names.get(block.get("tool_use_id") or "") or None,
                        "is_error": bool(block.get("is_error", False)),
                        "text": _flatten_text_content(block.get("content")),
                        "ts": ts,
                        "uuid": uuid,
                    }
                )
    return events


def _count_tools_from_events(events: list[dict]) -> dict:
    """Exact tool-call counters for the transcript being viewed.

    The summary row's ``tool_calls`` comes from the UsageProcessor and can
    lag (fresh upload, pre-v10 semantics that excluded MCP/subagent calls) —
    a header number that disagrees with the tool cards below it reads as a
    bug. Same principle as ``_sum_usage_from_turns`` (TCRD-222): compute it
    inline from the file this request already parsed, so the detail view is
    exact regardless of the processor's tick.

    ``tool_errors`` counts distinct failed calls (by ``tool_use_id``), the
    same one-error-per-call correlation the processor applies.

    ``mcp_calls`` is the MCP slice of ``tool_calls`` (a breakdown, not a
    sibling) — on an MCP-heavy session it explains at a glance why the
    total is what it is.
    """
    tool_calls = sum(1 for e in events if e["kind"] == "tool_use")
    mcp_calls = sum(1 for e in events if e["kind"] == "tool_use" and str(e.get("tool_name") or "").startswith("mcp__"))
    error_ids = {e.get("tool_use_id") for e in events if e["kind"] == "tool_result" and e.get("is_error")}
    return {"tool_calls": tool_calls, "tool_errors": len(error_ids), "mcp_calls": mcp_calls}


@router.get("/{username}/{session_file}/download")
def download(
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
):
    """Stream a single JSONL straight from disk. Path-safety guarded the
    same way as ``/transcript``. Audit-logged."""
    from fastapi.responses import StreamingResponse

    session_dir, path = _resolve_session_target(username, session_file)

    def _iter():
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                yield chunk

    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="session_download",
            # Resolved directory, not the segment the caller typed: two
            # aliases for one file must audit as the same resource.
            resource=f"{session_dir}/{session_file}",
            params={"bytes": path.stat().st_size},
            result="success",
            # client_kind intentionally omitted (F0 audit-context autofill,
            # Task 1) — this admin read isn't necessarily browser-only.
        )
    except Exception:
        logger.exception("audit_log write failed for session_download")

    return StreamingResponse(
        _iter(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{session_file}"'},
    )


def _sum_usage_from_turns(turns: list[dict]) -> dict | None:
    """Sum ``message.usage`` across assistant turns (TCRD-222).

    Same field mapping the UsageProcessor uses (``usage_lib``), computed
    inline from the file this request already parsed — so the detail view is
    exact for the transcript being read, independent of whether the
    processor has ticked yet (a fresh upload's summary row is missing or
    zeroed until it does).

    Returns None when no turn carried a usage block at all: an old-format
    JSONL predates the field, and "we don't know" must not be spelled
    "0 tokens" — a zero reads as a measurement.
    """
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    saw_usage = False
    for t in turns:
        if t.get("type") != "assistant":
            continue
        usage = (t.get("message", {}) or {}).get("usage") or {}
        if not isinstance(usage, dict) or not usage:
            continue
        saw_usage = True
        for src_key, out_key in (
            ("input_tokens", "input"),
            ("output_tokens", "output"),
            ("cache_read_input_tokens", "cache_read"),
            ("cache_creation_input_tokens", "cache_creation"),
        ):
            v = usage.get(src_key, 0)
            if isinstance(v, int):
                totals[out_key] += v
    if not saw_usage:
        return None
    totals["total"] = sum(totals.values())
    return totals


def _chat_transcript_not_found_detail(freshness: ChatTranscriptFreshness) -> dict:
    """Turn a failed on-demand refresh into an honest, actionable 404 body —
    never a bare "session not found" once we know a chat session by this id
    actually exists. See ``ensure_chat_transcript_current``'s docstring for
    what each ``freshness`` field means.
    """
    if freshness.lookup_failed:
        # We never established whether this session exists, so we must not
        # say it does not — the id may be perfectly good and the store down.
        return {
            "error": "session_lookup_failed",
            "hint": (
                "The session store did not answer, so this is not a statement "
                "about the id. Retry; if it persists, check the server logs "
                "for the lookup failure."
            ),
        }
    if not freshness.session_found:
        return {
            "error": "session_not_found",
            "hint": "No such session. Check the id with `agnes admin sessions list`.",
        }
    if freshness.export_disabled:
        return {
            "error": "chat_transcript_export_disabled",
            "hint": (
                "This instance has `sessions.include_chat` turned off, so chat "
                "sessions are never materialized into a browsable transcript. "
                "An admin can turn it on in instance.yaml."
            ),
        }
    if not freshness.message_count:
        return {
            "error": "session_has_no_messages",
            "hint": "This session exists but has no messages yet — there is nothing to show.",
        }
    since = freshness.last_message_at.isoformat() if freshness.last_message_at else "an unknown time"
    return {
        "error": "chat_transcript_not_exported_yet",
        "hint": (
            f"Not exported yet — {freshness.message_count} message(s) pending "
            f"since {since}. Retry shortly, or check server logs if this persists."
        ),
    }


def _chat_transcript_freshness_note(freshness: ChatTranscriptFreshness | None) -> dict:
    """Say, on a transcript we ARE serving, whether we could confirm it is
    the current one.

    ``ensure_chat_transcript_current`` sets ``path`` only when the export is
    known current — it either already was, or was just rewritten. Every
    other outcome can still resolve to a real file on disk: the periodic
    sweep wrote one minutes ago, and messages since then are simply not in
    what the admin is now reading. Serving it is still the right call — in
    the middle of an incident a partial transcript is often the only
    evidence there is — but serving it silently lets a partial record read
    as the whole one, which is the failure this endpoint exists to end.

    ``verified`` is therefore a claim we only make when we checked, and
    ``reason`` names what stopped us when we could not. ``freshness=None``
    means the refresh call itself raised; ``raced`` means we wrote the file
    but a message landed while we were writing it, so a real ``path`` is
    still not a current one.
    """
    if freshness is not None and freshness.path is not None and not freshness.raced:
        return {"verified": True}
    if freshness is not None and freshness.raced:
        return {
            "verified": False,
            "reason": "export_raced_a_new_message",
            "hint": (
                "A message landed while this transcript was being exported, so "
                "what is shown is already one or more messages behind. Reload "
                "for a current copy."
            ),
        }
    if freshness is None:
        return {
            "verified": False,
            "reason": "refresh_failed",
            "hint": (
                "Could not check whether this export is current — the refresh "
                "itself failed (see server logs). Newer messages may be missing "
                "from what is shown."
            ),
        }
    if freshness.lookup_failed:
        return {
            "verified": False,
            "reason": "session_lookup_failed",
            "hint": (
                "Could not check whether this export is current — the session "
                "store did not answer. Newer messages may be missing from what "
                "is shown; retry for a verified copy."
            ),
        }
    if not freshness.session_found:
        return {
            "verified": False,
            "reason": "session_not_found",
            "hint": (
                "This file is a historical export — the chat session it came "
                "from no longer exists, so there is nothing left to check it "
                "against."
            ),
        }
    if freshness.export_disabled:
        return {
            "verified": False,
            "reason": "chat_transcript_export_disabled",
            "hint": (
                "This instance has `sessions.include_chat` turned off, so this "
                "file is frozen at whatever was exported before it was turned "
                "off. Newer messages are not in it."
            ),
        }
    return {
        "verified": False,
        "reason": "export_not_written",
        "hint": (
            "Could not bring this export current (see server logs). Newer messages may be missing from what is shown."
        ),
    }


@router.get("/{username}/{session_file}/transcript")
def transcript(
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
):
    # F4 freshness follow-up (measured on a production instance, 2026-09-09):
    # a chat session's jsonl is otherwise only (re-)written by the periodic
    # sweep, which can lag the session's own activity by several minutes. A
    # chat-shaped filename gets brought current here, on demand, before we
    # even try to resolve it on disk — the same idempotent, mtime-guarded
    # work the sweep would do a few minutes later
    # (app/chat/session_export.py::ensure_chat_transcript_current). A
    # non-chat (legacy CLI-collector) filename never matches and is
    # completely unaffected.
    chat_id = _chat_id_from_session_file(session_file)
    chat_freshness = None
    if chat_id is not None:
        from app.chat.session_export import ensure_chat_transcript_current

        try:
            chat_freshness = ensure_chat_transcript_current(chat_id)
        except Exception:
            logger.warning("on-demand chat transcript refresh failed for %s", chat_id, exc_info=True)

    try:
        session_dir, path = _resolve_session_target(username, session_file)
    except HTTPException as exc:
        if exc.status_code == 404 and chat_freshness is not None:
            # 404 says "this does not exist". When the session store never
            # answered we cannot say that, so a lookup failure goes out as a
            # retryable 503 instead — the caller should come back, not go
            # hunting for a typo in a correct id.
            status = 503 if chat_freshness.lookup_failed else 404
            raise HTTPException(
                status_code=status,
                detail=_chat_transcript_not_found_detail(chat_freshness),
            ) from exc
        raise
    # A file resolved. Whether it is the CURRENT one is a separate question,
    # and one we can only have answered for a chat-shaped filename: a legacy
    # CLI-collector file is not ours to refresh, so we make no claim about it.
    freshness_note = _chat_transcript_freshness_note(chat_freshness) if chat_id is not None else None

    turns = parse_jsonl(path)
    events = _render_transcript(turns)
    tokens = _sum_usage_from_turns(turns)
    counts = _count_tools_from_events(events)

    # Summary rows are keyed on `<on-disk dir>/<file>`, so look them up with
    # the RESOLVED directory — a caller who passed the display e-mail would
    # otherwise get a transcript with a silently empty header.
    summary_data = usage_repo().get_session_summary(f"{session_dir}/{session_file}")
    summary: dict[str, Any] = {}
    if summary_data:
        summary = summary_data
        for k in ("started_at", "ended_at"):
            v = summary.get(k)
            if isinstance(v, datetime):
                summary[k] = v.isoformat()

    # Audit: looking at someone else's transcript is a privacy-sensitive
    # operation; record actor + target + bytes scanned for traceability.
    try:
        audit_repo().log(
            user_id=user.get("id"),
            action="session.transcript_view",
            resource=f"{session_dir}/{session_file}",
            params={"events": len(events)},
            result="success",
            # client_kind intentionally omitted (F0 audit-context autofill,
            # Task 1) — this admin read isn't necessarily browser-only.
        )
    except Exception:
        logger.exception("audit_log write failed for session.transcript_view")

    body: dict[str, Any] = {
        "username": username,
        "session_file": session_file,
        "summary": summary,
        "tokens": tokens,
        "counts": counts,
        "events": events,
    }
    if freshness_note is not None:
        body["freshness"] = freshness_note
    return body
