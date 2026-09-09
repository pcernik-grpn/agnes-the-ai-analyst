"""Conversation corpus export — ``GET /api/admin/conversations/corpus``
(design 2026-09-08 §3.12).

An evaluation-corpus pull, not telemetry: one COMPLETE record per chat
session (see ``src/conversation_export.py`` for the shape), built
on-instance from ``chat_sessions``/``chat_messages``/``llm_calls``/
``chat_message_feedback``/``agent_memories`` — data the instance already
keeps, never truncated the way a span's content is.

**Postgres-only** (A3 PG-first ratchet): the export reads ``llm_calls`` and
``chat_message_feedback``, both PG-only tables with no DuckDB sibling, so
``_export_repo_bundle`` below resolves them FIRST — before the
``chat_session``/``chat_message`` repos, which DO have a DuckDB sibling but
none of the bulk export methods this route calls. Resolving the PG-only
repos first is what makes a DuckDB-backed instance answer a clean, typed
``501 requires_postgres_backend`` (translated by ``app/main.py``) instead of
an ``AttributeError`` crash reaching for a method the DuckDB side never
grew.

**Under the content policy** (spec 3.6/3.12): refuses with
``403 content_export_disabled`` when ``observability.content_export.mode``
is off, has no recorded basis, or excludes workload ``chat``. When the mode
is ``pseudonymized`` every exported text leaf goes through the same
instance anonymizer the OTel content-export path uses
(``src.observability.content_policy.make_export_scrubber`` — one policy read
for the whole pass, fails closed, WITHHELD
rather than raw text on an anonymizer failure).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from app.auth.access import require_admin
from src.audit_helpers import log_safe
from src.conversation_export import ConversationExportRepoBundle, iter_conversations, serialize_jsonl
from src.observability.content_policy import (
    NO_BASIS_WARNING,
    content_export_mode,
    make_export_scrubber,
    load_content_export_policy,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin-conversations-export"])

_MAX_LIMIT = 500
_DEFAULT_LIMIT = 200
_FORMATS = ("jsonl", "json")


def _export_repo_bundle_deps() -> dict[str, Any]:
    """Resolve every repo the export needs AS A DEPENDENCY, PG-only repos
    first. Not inside the handler body: FastAPI solves dependencies before
    the handler runs, so raising here — instead of three lines into the
    handler, past a check the DuckDB backend could otherwise fail on its
    own terms — is what makes a DuckDB-backed instance answer the typed
    ``501`` rather than an unrelated crash or a misleadingly-successful
    empty page.
    """
    from src.repositories import (
        agent_memories_repo,
        chat_message_feedback_repo,
        chat_message_repo,
        chat_session_repo,
        llm_calls_repo,
        users_repo,
    )

    calls = llm_calls_repo()  # PG-only (A3 ratchet) -> raises RequiresPostgresBackend on DuckDB
    feedback = chat_message_feedback_repo()  # PG-only (A3 ratchet) -> same
    return {
        "sessions": chat_session_repo(),
        "messages": chat_message_repo(),
        "calls": calls,
        "feedback": feedback,
        "memories": agent_memories_repo(),
        "users": users_repo(),
    }


def _parse_dt(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_datetime", "field": field, "value": value},
        ) from None


def _export_denial_reason(workload: str) -> str:
    """``mode_off`` | ``no_basis`` | ``workload_excluded`` — the reason the
    caller gets back alongside the 403, so an admin (or the CLI, or an
    analyst reading the error) can tell "nobody turned this on" from "chat
    specifically is excluded" without reading ``instance.yaml`` first."""
    policy = load_content_export_policy()
    if policy.mode == "off":
        return "no_basis" if NO_BASIS_WARNING in policy.warnings else "mode_off"
    if policy.workloads and workload not in policy.workloads:
        return "workload_excluded"
    return "mode_off"


@router.get("/api/admin/conversations/corpus")
def export_conversations(
    since: str | None = Query(None, description="ISO date/datetime, inclusive lower bound. Required."),
    until: str | None = Query(None, description="ISO date/datetime, exclusive upper bound. Default: now."),
    surface: str | None = Query(None, description="Filter to one chat surface (web, slack_dm, api, ...)."),
    agent_id: str | None = Query(None, description="Filter to one shared agent's sessions."),
    format: str = Query("jsonl", description="jsonl (default, streamed) or json (one array)."),
    limit: int = Query(_DEFAULT_LIMIT, description=f"Records per page, at most {_MAX_LIMIT}."),
    cursor: str | None = Query(None, description="Opaque token from a prior page's next_cursor / X-Next-Cursor."),
    admin: dict = Depends(require_admin),
    repos: dict = Depends(_export_repo_bundle_deps),
):
    """The evaluation-corpus pull (spec 3.12). Admin-only, Postgres-only,
    content-export-policy-gated. ``since`` is required — a caller that omits
    it gets a typed 400, not an unbounded scan of every conversation ever
    held."""
    if since is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "since_required", "message": "`since` is required (ISO date or datetime)."},
        )
    if format not in _FORMATS:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_format", "message": f"format must be one of {_FORMATS}, got {format!r}."},
        )
    if limit < 1 or limit > _MAX_LIMIT:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_limit", "message": f"limit must be between 1 and {_MAX_LIMIT}."},
        )

    since_dt = _parse_dt(since, "since")
    until_dt = _parse_dt(until, "until") if until else datetime.now(UTC)

    mode = content_export_mode(workload="chat")
    if mode == "off":
        raise HTTPException(
            status_code=403,
            detail={"error": "content_export_disabled", "reason": _export_denial_reason("chat")},
        )

    anonymizer = make_export_scrubber() if mode == "pseudonymized" else None
    bundle = ConversationExportRepoBundle(
        sessions=repos["sessions"],
        messages=repos["messages"],
        calls=repos["calls"],
        feedback=repos["feedback"],
        memories=repos["memories"],
        users=repos["users"],
        content_mode=mode,
        anonymizer=anonymizer,
    )

    try:
        records, next_cursor, _keys = iter_conversations(
            bundle,
            since=since_dt,
            until=until_dt,
            surface=surface,
            agent_id=agent_id,
            limit=limit,
            cursor=cursor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_cursor", "message": str(exc)}) from exc

    log_safe(
        user_id=admin.get("id"),
        action="conversations.export",
        resource="conversations:export",
        params={
            "since": since,
            "until": until,
            "surface": surface,
            "agent_id": agent_id,
            "count": len(records),
            "content_mode": mode,
            "placement": load_content_export_policy().placement,
            "delivery": "pull",
        },
        result="success",
    )

    headers = {"X-Next-Cursor": next_cursor} if next_cursor else {}
    if format == "json":
        return JSONResponse(
            content={"data": records, "count": len(records), "next_cursor": next_cursor}, headers=headers
        )

    return StreamingResponse(serialize_jsonl(records), media_type="application/x-ndjson", headers=headers)


__all__ = ["router"]
