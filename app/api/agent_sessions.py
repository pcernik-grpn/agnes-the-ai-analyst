"""Agent-as-API multi-turn sessions — `POST /api/v1/agents/{slug}/sessions`
+ `POST/GET/DELETE /api/v1/sessions/{id}` + `POST /api/v1/sessions/{id}/cancel`
(V1b Task 4, `docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api
-design.md` §3). Split out of `app/api/agent_runtime.py` (which owns the
one-shot `/responses` + `/jobs` runtime) to keep each router focused.

Where the one-shot runtime spawns a fresh headless session per call, this
router exposes the underlying multi-turn chat primitive directly: create a
session bound to an agent, stream one turn at a time as Server-Sent Events
in the AG-UI vocabulary (`app.api.agent_sse`), read history, cancel an
in-flight turn, or archive the session.

Auth: session routes have no `{slug}` in their path, so
`require_agent_runtime_principal` (agent_runtime.py) cannot guard them, and
`require_session_token` (app/auth/dependencies.py) rejects every PAT flavor
outright — it would kill the agent-PAT flow this surface must support (per
`app.auth.pat_resolver`'s `_AGENT_PAT_ALLOWED_PREFIXES`, agent PATs ARE
allowed against `/api/v1/sessions/*`). `require_session_principal` below is
the session-scoped auth dependency built for this router: resolve the
session's owning agent, then allow either the agent's owner (interactive
session token) or an agent PAT bound to that exact agent — anything else is
`404`, never `403`, so a non-owner can't distinguish "session exists,
not yours" from "session doesn't exist".

The exact AG-UI wire format `_event_stream` below produces (event order,
lifecycle balance, gap-free `id:` sequencing) is a contract — see
`tests/test_agent_sse_contract.py` for the canned-frame golden test that
locks it down independent of this router's own FastAPI/manager plumbing.

SSE stream lifecycle: one `StreamingSink` (`app.chat.streaming_sink`) is
attached per `POST .../messages` call — a fresh attach/detach pair per
turn, which is also what makes `RUN_STARTED` (emitted once per `attach()`,
see `ChatManager._seat_sink`) appear exactly once per turn rather than once
per reconnect: there is no persistent WS connection here to reconnect. A
client that disconnects mid-stream does NOT cancel the turn — `finally:
detach_sink()` only removes this one sink; the runner keeps running and
burns budget until it finishes, crashes, or `POST .../cancel` is called
explicitly. A turn that never emits a terminal frame (`RUN_FINISHED`/
`RUN_ERROR`) would otherwise hang the response forever; `_IDLE_TIMEOUT_S`
bounds the wait between frames and forces a terminal `RUN_ERROR` instead.

Concurrency: a per-session turn lock (`coordination().lease_acquire`, the
same primitive `app.api.chat`'s WS ticket store and `app.chat.routing`'s
routing leases use) rejects a second concurrent `POST .../messages` for the
same session with `409 {"code": "turn_in_flight"}` rather than racing two
turns onto the same runner.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from app.api.agent_runtime import AgentRuntimePrincipal, require_agent_runtime_principal
from app.api.agent_sse import SSE_TERMINAL_TYPES, frame_to_agui, sse_bytes
from app.auth.access import can_access, require_agent_profiles_enabled
from app.auth.dependencies import _get_db, get_current_user
from app.auth.pat_resolver import agent_id_from_request
from app.auth.session_principal import PRINCIPAL_TYPES
from app.chat.manager import ConcurrencyCapHit, SessionNotFound, get_current_chat_manager
from app.chat.streaming_sink import StreamingSink
from app.chat.structured_output import schema_directive
from app.chat.types import Surface
from app.coordination.factory import coordination
from app.logging_config import request_id_var
from app.resource_types import ResourceType
from src.object_store import object_store
from src.repositories import agent_artifacts_repo, agents_repo, chat_message_repo, chat_session_repo

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["agent-sessions"],
    dependencies=[Depends(require_agent_profiles_enabled)],
)

#: Per-session in-flight-turn lease. TTL is a ceiling only — the lease is
#: released explicitly in the streaming generator's `finally` on every exit
#: path (success, error, idle-timeout, client disconnect); the TTL just
#: bounds how long a crashed request (never reaching `finally`, e.g. the
#: worker process itself dying) can wedge the session.
_TURN_LOCK_PREFIX = "agent-session-turn:"
_TURN_LOCK_TTL_S = 600

#: Bound on the wait between successive frames from the sink. A turn that
#: never emits a terminal frame (runner wedged, crashed without a `done`/
#: `error`/`cancelled` broadcast) would otherwise hang the HTTP response
#: forever — this forces a terminal RUN_ERROR instead.
_IDLE_TIMEOUT_S = 300.0


class CreateAgentSessionBody(BaseModel):
    """Empty today — no prompt is sent at creation time (that's the first
    `POST .../messages` call). A distinct model (rather than accepting no
    body) leaves room to grow (e.g. an initial `title`) without a breaking
    change to the request shape."""


class SendSessionMessageBody(BaseModel):
    input: str
    #: Structured-output request — `{"type": "json_schema", "schema": {...}}`.
    #: V1b Task 7 wires PROMPT-STEERING here (`schema_directive()`'s text is
    #: appended to `input` below, same as the one-shot `/responses` route —
    #: see `app.chat.structured_output`'s module docstring), but deliberately
    #: NOT post-hoc validation of the streamed answer.
    #:
    #: Task 7 scope decision: `/responses` validates synchronously because it
    #: already holds the final `answer` string before building its one JSON
    #: response body, so a schema violation can become a clean `422` in the
    #: same request/response cycle (C13). This route is fundamentally
    #: different — `_event_stream` below has already sent a `200` and started
    #: streaming AG-UI events by the time a terminal frame (and thus the full
    #: answer) exists; there is no HTTP status left to change to `422`, and
    #: inventing a new terminal SSE event type (or repurposing `RUN_ERROR`)
    #: for "the run succeeded but its output didn't match your schema" is a
    #: real design question (does the client still get the raw answer? does
    #: `GET /sessions/{id}` need a `schema_valid` flag on the message row?)
    #: that deserves its own task rather than a rushed fit here. Deferred,
    #: not forgotten — filed as a natural Task 7 follow-up.
    response_format: Optional[Dict[str, Any]] = None

    @field_validator("input")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("input must be non-empty")
        return v


class SessionRuntimePrincipal:
    """Resolved (user, agent, session) triple for one `/api/v1/sessions/{id}/...` call."""

    def __init__(self, user: dict, agent: dict, session: Any) -> None:
        self.user = user
        self.agent = agent
        self.session = session


def require_session_principal(
    session_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> SessionRuntimePrincipal:
    """Auth dependency for every `/api/v1/sessions/{id}/...` route.

    A co-session (`SessionPrincipal`) credential carries no single owner
    identity — hard-denied, same posture as
    `require_agent_runtime_principal`. Every other failure (session
    missing, agent missing/deleted, wrong owner, agent PAT bound to a
    DIFFERENT agent than this session's) collapses to the SAME `404` —
    never `403` — so a non-owner request can't distinguish "no such
    session" from "not yours".

    Once ownership is established, re-check the `ResourceType.CHAT` grant —
    the same check `require_agent_runtime_principal` applies for
    `/agents/{slug}/sessions` and `/responses` (`can_access(..., "chat")`).
    Sessions can outlive the grant that let their owner create them (a
    revoked group membership doesn't retroactively delete existing
    sessions), so without this re-check a caller whose CHAT grant was
    pulled after session creation could keep driving `/messages` forever.
    `403 chat_access_denied` here matches what `/responses` returns for the
    same condition.
    """
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})

    session = chat_session_repo().get_session(session_id)
    if session is None or session.agent_id is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})

    agent = agents_repo().get_by_id(session.agent_id)
    if agent is None or agent.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})

    pat_agent_id = agent_id_from_request(request)
    if pat_agent_id is not None:
        if pat_agent_id != agent["id"]:
            raise HTTPException(status_code=404, detail={"code": "session_not_found"})
    elif agent["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})

    if not can_access(user["id"], ResourceType.CHAT.value, "chat", conn):
        raise HTTPException(status_code=403, detail={"code": "chat_access_denied"})

    return SessionRuntimePrincipal(user=user, agent=agent, session=session)


@router.post("/agents/{slug}/sessions", status_code=201)
async def create_agent_session(
    slug: str,
    body: CreateAgentSessionBody,
    principal: AgentRuntimePrincipal = Depends(require_agent_runtime_principal),
) -> dict:
    user, agent = principal.user, principal.agent
    manager = get_current_chat_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail={"code": "chat_disabled"})
    # An agent's persona and memory notebook are delivered by materializing
    # them into the session workspace (`ChatManager.create_session` ->
    # `agent_profile.materialize_memories`). A provider that brings its own
    # runtime never reads that directory, so the agent's system prompt, tone,
    # greeting and memories simply do not reach the turn — the caller would
    # get the instance template persona under the agent's name, and a
    # narrowed-scope agent would additionally 403 on every tool call.
    # Refuse rather than answer wrongly: this endpoint's whole contract is
    # "run as THIS agent".
    if getattr(getattr(manager, "_provider", None), "provides_own_credentials", False) is True:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "agent_sessions_unavailable_on_provider",
                "hint": (
                    "This instance runs chat on an embedded turn engine that does not read the "
                    "agent workspace, so an agent's persona and memories cannot be applied. Use "
                    "the native chat provider to serve the agent API."
                ),
            },
        )
    try:
        session = await manager.create_session(
            user_email=user["email"],
            surface=Surface.API,
            agent_id=agent["id"],
        )
    except ConcurrencyCapHit as exc:
        raise HTTPException(status_code=429, detail={"code": "concurrency_cap", "hint": str(exc)}) from exc
    except RuntimeError as exc:
        # `ChatManager.create_session` raises this specific message when
        # `chat.enabled` is false at the config level — normally unreachable
        # since `get_current_chat_manager()` is already None in that case
        # (app/main.py only starts the manager when chat.enabled=true), but
        # guard it defensively rather than let it fall through to the
        # catch-all 500 handler. Same `503 chat_disabled` the `manager is
        # None` branch above (and app/api/chat.py's `_get_manager`) returns.
        if str(exc) != "chat.enabled is false":
            raise
        raise HTTPException(status_code=503, detail={"code": "chat_disabled"}) from exc
    return {"session_id": session.id}


async def _event_stream(
    manager: Any,
    session_id: str,
    sink: StreamingSink,
    lock_key: str,
    lock_holder: str,
) -> AsyncIterator[bytes]:
    """SSE body generator: drain `sink`, mapping each frame to an AG-UI
    event, until a terminal event or an idle timeout. Always detaches the
    sink and releases the turn lock on the way out — success, error, or a
    client disconnect (Starlette cancels this generator's task when the
    connection drops, which surfaces here as `GeneratorExit`/cancellation
    at whichever `await` is in flight; the `finally` still runs).
    """
    aiter = sink.__aiter__()
    try:
        while True:
            try:
                frame = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT_S)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                yield sse_bytes(
                    {"type": "RUN_ERROR", "message": "stream idle timeout", "code": "idle_timeout"},
                    None,
                )
                break
            event = frame_to_agui(frame)
            if event is None:
                continue
            yield sse_bytes(event, frame.get("id"))
            if event["type"] in SSE_TERMINAL_TYPES:
                break
    finally:
        try:
            await manager.detach_sink(session_id, sink)
        finally:
            coordination().lease_release(lock_key, lock_holder)


@router.post("/sessions/{session_id}/messages")
async def post_session_message(
    session_id: str,
    body: SendSessionMessageBody,
    request: Request,
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> StreamingResponse:
    manager = get_current_chat_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail={"code": "chat_disabled"})

    lock_key = f"{_TURN_LOCK_PREFIX}{session_id}"
    lock_holder = uuid.uuid4().hex
    if not coordination().lease_acquire(lock_key, lock_holder, ttl_s=_TURN_LOCK_TTL_S):
        raise HTTPException(status_code=409, detail={"code": "turn_in_flight"})

    sink = StreamingSink()
    try:
        await manager.attach(session_id, sink)
    except SessionNotFound:
        # attach() raised before seating the sink (see its docstring: every
        # branch that seats the sink returns immediately after) — nothing to
        # detach.
        coordination().lease_release(lock_key, lock_holder)
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})
    except Exception:
        coordination().lease_release(lock_key, lock_holder)
        raise

    # Prompt-steering only (see `SendSessionMessageBody.response_format`'s
    # docstring for the scope decision): append the schema directive to the
    # TEXT sent to the model — there is no `send_user_message(response_format
    # =...)` param, and post-hoc validation of the streamed answer is
    # deferred.
    effective_input = body.input
    if body.response_format is not None:
        effective_input = f"{body.input}\n\n{schema_directive(body.response_format)}"

    try:
        await manager.send_user_message(session_id, effective_input, sender_email=principal.user["email"])
    except Exception:
        # Unlike attach() above, the sink IS seated by this point (attach()
        # already returned successfully) — leaving it attached would leak it
        # in `live.sinks` forever (undrained queue, skewed linger/pause
        # lifecycle). Detach before releasing the lock and re-raising.
        try:
            await manager.detach_sink(session_id, sink)
        finally:
            coordination().lease_release(lock_key, lock_holder)
        raise

    request_id = request_id_var.get() or uuid.uuid4().hex
    return StreamingResponse(
        _event_stream(manager, session_id, sink, lock_key, lock_holder),
        media_type="text/event-stream",
        headers={"x-request-id": request_id},
    )


@router.get("/sessions/{session_id}")
async def get_session(
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> dict:
    session = principal.session
    msgs = chat_message_repo().list_messages(session.id)
    return {
        "session_id": session.id,
        "agent_id": session.agent_id,
        "state": "archived" if session.archived else "active",
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tool_calls": m.tool_calls,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    }


@router.post("/sessions/{session_id}/cancel", status_code=202)
async def cancel_session(
    session_id: str,
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> dict:
    manager = get_current_chat_manager()
    if manager is None:
        raise HTTPException(status_code=503, detail={"code": "chat_disabled"})
    await manager.cancel(session_id)
    return {}


async def _harvest_before_kill(manager: Any, session_id: str, agent: dict) -> None:
    """Best-effort artifact harvest on `DELETE /api/v1/sessions/{id}` (V1b
    Task 5, C4) — runs BEFORE `manager.kill()` tears the sandbox down,
    since the handle is only reachable while the session is still live.

    No-ops (never raises) when the manager has no live handle for this
    session — already torn down, paused with no active handle, or the
    session never actually spawned a sandbox (e.g. a session created but
    never sent a message).
    """
    list_live = getattr(manager, "list_live", None)
    if list_live is None:
        return
    try:
        live = next((entry for entry in list_live() if entry.chat_id == session_id), None)
        if live is None or live.handle is None:
            return
        from app.chat.artifact_harvest import caps_from_manager, harvest_session_artifacts

        await harvest_session_artifacts(
            session_id,
            agent["id"],
            agent["owner_user_id"],
            live.handle,
            **caps_from_manager(manager),
        )
    except Exception:
        logger.exception("delete_session: artifact harvest failed for %s", session_id)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> None:
    manager = get_current_chat_manager()
    if manager is not None:
        await _harvest_before_kill(manager, session_id, principal.agent)
        try:
            await manager.kill(session_id, reason="agent_api_delete")
        except Exception:
            logger.exception("kill on agent-session delete failed for %s", session_id)
    chat_session_repo().archive_session(session_id)


#: Presigned-GET TTL cap for the opt-in `?redirect=1` download path (C5) —
#: short-lived by design (the tradeoff: a leaked URL is usable for this
#: window without re-authenticating against Agnes at all). The default
#: (non-redirect) download path streams through this authenticated endpoint
#: instead and carries no such exposure.
_ARTIFACT_PRESIGN_TTL_S = 120


@router.get("/sessions/{session_id}/artifacts")
async def list_session_artifacts(
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> dict:
    """Cursor-envelope listing of every artifact harvested for this session.

    Not actually paginated today — a session's artifact count is bounded by
    `agent_api_artifact_max_files` (20 by default, C3) per harvest call, so
    a single page always covers it — but the response shape matches the
    project's standard cursor envelope (`command-ux.md`) so a future caller
    doesn't have to special-case this endpoint if/when true pagination is
    added.
    """
    rows = agent_artifacts_repo().list_for_session(principal.session.id)
    return {
        "data": [
            {
                "id": r["id"],
                "filename": r["filename"],
                "size_bytes": r["size_bytes"],
                "content_type": r["content_type"],
                "created_at": r["created_at"].isoformat() if hasattr(r["created_at"], "isoformat") else r["created_at"],
            }
            for r in rows
        ],
        "has_more": False,
        "next_cursor": None,
    }


@router.get("/sessions/{session_id}/artifacts/{artifact_id}")
async def download_session_artifact(
    artifact_id: str,
    redirect: bool = Query(
        False,
        description=(
            "Opt-in: 307-redirect to a short-TTL presigned object-store URL instead of "
            "streaming through this authenticated endpoint. Tradeoff: the presigned URL "
            "is usable by anyone who obtains it (e.g. via a proxy log or browser history) "
            "for up to the TTL, with no further Agnes auth check. Default (false) streams "
            "the bytes through this endpoint's own auth instead."
        ),
    ),
    principal: SessionRuntimePrincipal = Depends(require_session_principal),
) -> Response:
    """Download one artifact. `404` for both an unknown id and an id that
    belongs to a different session — same non-leaking posture
    `require_session_principal` already applies at the session level (a
    cross-agent PAT never even resolves a `principal` to get this far)."""
    # Local import: app.chat.artifact_harvest -> app.chat.e2b_provider pulls
    # in the e2b SDK (~250ms) at module top — heavy for this one filename
    # sanitizer, only worth paying when an artifact is actually downloaded.
    from app.chat.artifact_harvest import sanitize_filename

    row = agent_artifacts_repo().get(artifact_id)
    if row is None or row.get("session_id") != principal.session.id:
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found"})

    store = object_store()
    if store is None:
        raise HTTPException(status_code=503, detail={"code": "object_store_unavailable"})

    safe_filename = sanitize_filename(row["filename"] or "")
    content_type = row.get("content_type") or "application/octet-stream"

    if redirect and hasattr(store, "presign_get"):
        try:
            url = store.presign_get(row["object_key"], ttl_s=_ARTIFACT_PRESIGN_TTL_S)
        except Exception:
            logger.exception(
                "download_session_artifact: presign_get failed for %s — falling back to stream", artifact_id
            )
        else:
            return RedirectResponse(url=url, status_code=307)

    data = store.get_bytes(row["object_key"])
    if data is None:
        raise HTTPException(status_code=404, detail={"code": "artifact_not_found"})

    return Response(
        content=data,
        media_type=content_type,
        headers={"content-disposition": f'attachment; filename="{safe_filename}"'},
    )
