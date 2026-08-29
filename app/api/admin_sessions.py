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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.access import require_admin
from app.api.admin_user_sessions import _SESSION_FILE_RE, _session_data_dir
from services.session_pipeline.lib import parse_jsonl

from src.repositories import (
    audit_repo,
    usage_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/sessions", tags=["admin-sessions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _window_since(since_minutes: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=since_minutes)


# ---------------------------------------------------------------------------
# GET /api/admin/sessions/list
# ---------------------------------------------------------------------------


@router.get("/list")
def list_sessions(
    since_minutes: int = Query(default=10080, ge=1, le=525600),  # default 7d
    username: Optional[str] = None,
    model: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
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
    username: Optional[str] = None,
    model: Optional[str] = None,
    only_errors: bool = False,
    q: Optional[str] = None,
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

# Username constraint: same allowlist as the session-file regex (alnums + `._-`).
# Filesystem username is the local-part of an email today, so no `@` etc.
import re as _re

_USERNAME_RE = _re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def _safe_session_path(username: str, session_file: str) -> Path:
    """Resolve a session jsonl path with three layers of guards.

    1. Both segments must match the allowlist regex; reject `..`, `/`, etc.
    2. After joining, ``resolve().relative_to(root)`` confirms no symlink
       escape moved the final path outside the user-sessions root.
    3. The file must end in ``.jsonl``.
    """
    if not _USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="invalid username")
    if not _SESSION_FILE_RE.match(session_file):
        raise HTTPException(status_code=400, detail="invalid session_file")
    root = _session_data_dir().resolve()
    path = (root / username / session_file).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escape rejected")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="session not found")
    return path


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
    """
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
                        "is_error": bool(block.get("is_error", False)),
                        "text": _flatten_text_content(block.get("content")),
                        "ts": ts,
                        "uuid": uuid,
                    }
                )
    return events


@router.get("/{username}/{session_file}/download")
def download(
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
):
    """Stream a single JSONL straight from disk. Path-safety guarded the
    same way as ``/transcript``. Audit-logged."""
    from fastapi.responses import StreamingResponse

    path = _safe_session_path(username, session_file)

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
            resource=f"{username}/{session_file}",
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


def _sum_usage_from_turns(turns: list[dict]) -> Optional[dict]:
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


@router.get("/{username}/{session_file}/transcript")
def transcript(
    username: str,
    session_file: str,
    user: dict = Depends(require_admin),
):
    path = _safe_session_path(username, session_file)
    turns = parse_jsonl(path)
    events = _render_transcript(turns)
    tokens = _sum_usage_from_turns(turns)

    summary_data = usage_repo().get_session_summary(f"{username}/{session_file}")
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
            resource=f"{username}/{session_file}",
            params={"events": len(events)},
            result="success",
            # client_kind intentionally omitted (F0 audit-context autofill,
            # Task 1) — this admin read isn't necessarily browser-only.
        )
    except Exception:
        logger.exception("audit_log write failed for session.transcript_view")

    return {
        "username": username,
        "session_file": session_file,
        "summary": summary,
        "tokens": tokens,
        "events": events,
    }
