"""FastAPI chat REST + WebSocket endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import exc as sa_exc

from app.auth.access import require_resource_access
from app.auth.dependencies import _get_db
from app.chat.frame_seq import stamp_frame
from app.chat.manager import ChatManager, ConcurrencyCapHit, SessionNotFound
from app.chat.persistence import ChatRepository
from app.chat.profiles import get_profile
from app.chat.replay import GapReplayGate, replay_since
from app.chat.skills_catalog import (
    BUNDLED_TEMPLATE_DIR,
    marketplace_delivery,
    merged_commands,
    merged_skills,
)
from app.chat.sources import verdict as sources_verdict
from app.chat.types import Surface
from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from app.resource_types import ResourceType
from src.audit_helpers import log_safe
from src.repositories import agents_repo, user_journey_repo

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

# Cloud chat is an RBAC resource: denied to everyone by default, granted to a
# group on /admin/access. Every chat endpoint depends on this gate (the WS
# stream is covered transitively — its ticket is only mintable through the
# gated create/reissue endpoints). Admins short-circuit via god-mode. The
# resource is a singleton, so the path template is the fixed id "chat".
require_chat_access = require_resource_access(ResourceType.CHAT, "chat")


def _reject_restricted_principal(user: object, what: str) -> None:
    """403 a co-session / agent-session caller before the handler subscripts ``user``.

    ``require_resource_access`` returns whatever principal it authorized, and for
    a restricted principal that is a FROZEN DATACLASS
    (``SessionPrincipal`` / ``AgentPrincipal``), not a dict — see
    ``app/auth/access.py`` where the two branches diverge. So ``user["email"]``
    raises ``TypeError: 'SessionPrincipal' object is not subscriptable`` and the
    caller gets a 500 where it should get a 403.

    Semantically these operations have no restricted-principal meaning anyway: a
    co-session has no single identity to own a conversation or an onboarding
    journey, and an agent-session must not mutate its owner's. ``app/api/stack.py``
    added the identical guard for the identical hazard in this same change
    (``_reject_co_session``) — this is that guard for the chat routes, which were
    missed (review of #1104).
    """
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(403, f"co_session cannot {what}")


# WS auth tickets ride the coordination backend (single-use KV with TTL) —
# not a module-level dict. In single-process ``memory`` mode that's still
# just an in-process dict under the hood (see app.coordination.memory), so
# behavior is unchanged from the original in-memory store; configuring the
# ``redis`` backend makes tickets visible across replicas, which is what HA
# deployments need (see app/startup_guards.py for the multi-process gate).
_TICKET_TTL_SEC = 60
_TICKET_KEY_PREFIX = "ws-ticket:"


def _issue_ticket(chat_id: str, user_email: str) -> str:
    ticket = secrets.token_urlsafe(32)
    payload = json.dumps({"chat_id": chat_id, "user_email": user_email})
    coordination().kv_set(f"{_TICKET_KEY_PREFIX}{ticket}", payload, ttl_s=_TICKET_TTL_SEC)
    return ticket


def _consume_ticket(ticket: str) -> tuple[str, str] | None:
    raw = coordination().kv_delete(f"{_TICKET_KEY_PREFIX}{ticket}")
    if raw is None:
        return None
    try:
        rec = json.loads(raw)
        return rec["chat_id"], rec["user_email"]
    except (ValueError, KeyError, TypeError):
        return None


class PreviewSkill(BaseModel):
    """A draft skill to make invokable for one session. Both fields are
    untrusted; see the note on CreateSessionBody.preview_skill."""

    name: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=40_000)


class CreateSessionBody(BaseModel):
    surface: str = "web"
    title: str | None = None
    # Optional authoring-agent profile (see app/chat/profiles.py). Spawn-time
    # only — shapes the session persona + knowledge skill; not persisted.
    profile: str | None = None
    #: Run one of the caller's OWN named agents in this session instead of
    #: their default. The runtime for this already existed and is surface-
    #: agnostic (``ChatManager.create_session(agent_id=...)``, the same seam
    #: ``POST /api/v1/agents/{slug}/sessions`` uses); web chat was simply never
    #: wired to it, which is why agents could be configured but not used.
    agent_slug: str | None = None
    #: A draft SKILL to make invokable in this session only — what backs the
    #: /skills builder's Preview. The skills catalog reports what is on disk
    #: in the session's project scope, so previewing a skill that exists
    #: nowhere but the author's browser means writing it into that scope.
    #:
    #: Untrusted on both fields. The name becomes a DIRECTORY name and is
    #: replaced rather than sanitized (WorkdirManager.safe_skill_dirname); the
    #: body is capped here and again at the write. The write is contained to
    #: the session's own `.claude`, which is forced to a copy so a draft can
    #: never reach the author's shared workspace — see
    #: tests/test_preview_skill_containment.py.
    #:
    #: Nothing is persisted: it lives in memory for the life of the session,
    #: because a preview that outlived the tab would be a copy of unfinished
    #: work nobody asked us to keep.
    preview_skill: "PreviewSkill | None" = None


def _get_manager(request: Request) -> ChatManager:
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        raise HTTPException(
            status_code=503,
            detail={"kind": "chat_disabled", "hint": "Operator must enable chat.enabled in instance.yaml"},
        )
    return mgr


def _get_repo(request: Request) -> ChatRepository:
    return request.app.state.chat_repo


def _default_agent_id(owner_user_id: str) -> str:
    """Resolve the caller's default agent id, attributing every web session
    to it (behavior otherwise unchanged — same profile, same rails).

    ``get_or_create_default`` is SELECT-then-INSERT with no transaction
    spanning both statements, so two concurrent first-touch requests for the
    same user can both miss the SELECT and race the INSERT; the loser hits
    the unique constraint on the default-agent row. Retry once — the winner's
    row now exists, so the retried SELECT finds it.
    """
    try:
        return agents_repo().get_or_create_default(owner_user_id)["id"]
    except (duckdb.ConstraintException, sa_exc.IntegrityError):
        return agents_repo().get_or_create_default(owner_user_id)["id"]


def _resolve_agent_id(agent_slug: str | None, user: dict) -> str:
    """Which agent this session runs as — a named one, else the default.

    Access is checked here rather than left to the broker: an agent is a
    scoped identity, so naming one this caller neither owns nor was shared
    (C2.3, `ResourceType.AGENT` grant) would hand them a persona built on
    grants that are not theirs. 404 for "no such slug/id", "not yours", and
    "not shared with you" alike, so the endpoint does not confirm the
    existence of another user's private agent.

    Scope enforcement itself is NOT re-implemented — the returned id goes
    through the same ``_load_agent_row``/broker seam as the agent-as-API route,
    so a ``'selected'``-scoped agent is restricted identically whichever door
    the session came in through. ``tests/test_agent_scope_e2e.py`` pins that.
    """
    if not agent_slug:
        return _default_agent_id(user["id"])
    # `get_runnable_by_slug` resolves `agent_slug` first in the caller's own
    # slug namespace (owned), else as the target agent's id (shared via a
    # grant) — see its docstring for why slug alone cannot address someone
    # else's agent. Either way a non-owner/non-grantee gets no row back —
    # there is no window where a foreign row is fetched and then rejected.
    row = agents_repo().get_runnable_by_slug(user["id"], agent_slug)
    if not row:
        raise HTTPException(status_code=404, detail={"kind": "agent_not_found", "hint": agent_slug})
    return str(row["id"])


@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSessionBody,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    # Before anything subscripts `user`. A restricted principal reaches this
    # route as a frozen dataclass, so `user["id"]` in _resolve_agent_id below
    # would raise TypeError and return 500 where 403 is the answer. The hazard
    # predates `agent_slug`, but that field is what makes the semantics matter:
    # an agent session naming another of its owner's agents would be picking up
    # a persona built on grants it was not given.
    _reject_restricted_principal(user, "start a conversation")
    mgr = _get_manager(request)
    if body.profile is not None and get_profile(body.profile) is None:
        raise HTTPException(
            status_code=400,
            detail={"kind": "unknown_profile", "hint": body.profile},
        )
    agent_id = _resolve_agent_id(body.agent_slug, user)
    try:
        s = await mgr.create_session(
            user_email=user["email"],
            surface=Surface(body.surface),
            title=body.title,
            profile=body.profile,
            agent_id=agent_id,
            preview_skill=body.preview_skill.model_dump() if body.preview_skill else None,
        )
    except ConcurrencyCapHit as exc:
        raise HTTPException(status_code=429, detail={"kind": "concurrency_cap", "hint": str(exc)})
    ticket = _issue_ticket(s.id, user["email"])
    log_safe(
        user_id=user["id"],
        action="chat.session.create",
        resource=f"session:{s.id}",
        params={"surface": body.surface},
    )
    return {
        "id": s.id,
        "surface": s.surface.value,
        "title": s.title,
        # Which agent this session runs AS. Always set (an unnamed web session
        # is attributed to the caller's default agent, see _resolve_agent_id),
        # so a client tells "named agent" from "default" by comparing against
        # the `is_default` row in GET /api/v1/agents rather than by null-checking.
        # The composer's agent picker needs this to label a session it did not
        # itself create.
        "agent_id": s.agent_id,
        "ws_ticket": ticket,
        "ws_url": f"/api/chat/sessions/{s.id}/stream?ticket={ticket}",
    }


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: dict = Depends(require_chat_access),
):
    # Seven sibling routes on this router carry this guard; this one never did,
    # so a restricted principal got a 500 (``user["email"]`` on a frozen
    # dataclass) where 403 is the answer. Listing "your" conversations has no
    # restricted-principal meaning anyway — a co-session has no single identity
    # whose history this would be, and an agent-session must not enumerate its
    # owner's.
    _reject_restricted_principal(user, "list conversations")
    repo = _get_repo(request)
    rows = repo.list_sessions(user["email"])
    return [
        {
            "id": s.id,
            "surface": s.surface.value,
            "title": s.title,
            # Raw datetimes, not .isoformat(): DuckDB reads come back naive
            # (clock value UTC), and pre-stringifying bypasses the app's
            # datetime encoder (app/serialization.py) that labels them
            # `+00:00` — the browser then parses the offset-less string as
            # LOCAL time and every timestamp shifts by the viewer's UTC
            # offset after a reload.
            "started_at": s.started_at,
            "last_message_at": s.last_message_at,
            "message_count": s.message_count,
            "paused": s.sandbox_paused_at is not None,
            # See create_session: lets the composer's agent picker show WHO a
            # reopened conversation is with. Column has existed since v101;
            # it was simply never projected onto the wire.
            "agent_id": s.agent_id,
            # Pin state for the history panel's Pinned group. `pinned_at` is
            # also exposed so a client can order pins itself; the repo already
            # returns pinned-first, so the flag alone is enough for the rail.
            "pinned": s.pinned_at is not None,
            "pinned_at": s.pinned_at,
        }
        for s in rows
    ]


class PinSessionBody(BaseModel):
    pinned: bool


@router.put("/sessions/{chat_id}/pin")
async def set_session_pinned(
    chat_id: str,
    body: PinSessionBody,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    """Pin or unpin a conversation in the caller's history panel.

    Ownership-gated the same way as archive/messages: 404 (never 403) when the
    session doesn't exist or belongs to someone else, so the endpoint can't be
    used to probe for other users' session ids. Idempotent — pinning an already
    pinned session just re-stamps ``pinned_at``, which re-orders it to the front
    of the Pinned group.
    """
    _reject_restricted_principal(user, "pin a conversation")
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    repo.set_pinned(chat_id, body.pinned)
    return {"id": chat_id, "pinned": body.pinned}


# Long enough for a descriptive sentence, short enough that the row's own
# ellipsis stays the thing that truncates it in the UI. The auto-title path
# (Haiku) already produces titles well inside this.
_TITLE_MAX = 200


class RenameSessionBody(BaseModel):
    title: str


@router.put("/sessions/{chat_id}/title")
async def rename_session(
    chat_id: str,
    body: RenameSessionBody,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    """Rename a conversation from the history panel's row menu.

    ``set_title`` already existed for the Haiku auto-title path; this is the
    user-driven route to the same column. Ownership-gated like the sibling
    per-session routes (404, never 403).

    The title is stripped and length-capped here rather than in the repo: the
    auto-title path is a trusted internal caller, this one is user input. An
    all-whitespace title is a 400 rather than a silent no-op — the row would
    otherwise render as "Untitled chat" with no explanation.
    """
    _reject_restricted_principal(user, "rename a conversation")
    title = body.title.strip()
    if not title:
        raise HTTPException(
            status_code=400,
            detail={"kind": "invalid_title", "hint": "Title cannot be empty."},
        )
    if len(title) > _TITLE_MAX:
        raise HTTPException(
            status_code=400,
            detail={"kind": "invalid_title", "hint": f"Title cannot exceed {_TITLE_MAX} characters."},
        )
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    repo.set_title(chat_id, title)
    return {"id": chat_id, "title": title}


async def _kill_quietly(request: Request, chat_id: str, *, reason: str) -> None:
    """Stop a session's sandbox if there is a manager to ask, and never fail for
    it.

    Archiving and deleting are BOOKKEEPING on a row; stopping the runner is
    tidying up after it. So a missing manager (chat configured but no provider
    wired — the common local/dev shape) must not turn "archive this conversation"
    into a 503, and neither must a kill that throws. ``_get_manager`` is the
    right guard for the endpoints that need a live sandbox to do their job at
    all; these two do not.
    """
    mgr = getattr(request.app.state, "chat_manager", None)
    if mgr is None:
        return
    try:
        await mgr.kill(chat_id, reason=reason)
    except Exception:
        logger.exception("kill on %s failed for %s", reason, chat_id)


class ArchiveSessionBody(BaseModel):
    archived: bool


@router.put("/sessions/{chat_id}/archived")
async def set_session_archived(
    chat_id: str,
    body: ArchiveSessionBody,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    """Archive or restore a conversation from the Chats page (/chats).

    ``DELETE /sessions/{chat_id}`` has always been a soft delete (it flips
    ``archived`` and kills the sandbox), but nothing listed archived rows, so
    the state had no name in the UI and no way back. This is the explicit,
    two-directional route: ``{"archived": true}`` archives (killing the sandbox
    exactly as the DELETE path does — an archived conversation must not keep a
    sandbox warm), ``{"archived": false}`` restores.

    Ownership-gated like every sibling per-session route: 404, never 403, so the
    endpoint cannot be used to probe for other users' session ids. Idempotent in
    both directions.
    """
    _reject_restricted_principal(user, "archive a conversation")
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    if body.archived:
        await _kill_quietly(request, chat_id, reason="user_archive")
        repo.archive_session(chat_id)
    else:
        repo.restore_session(chat_id)
    log_safe(
        user_id=user["id"],
        action="chat.session.archive",
        resource=f"session:{chat_id}",
        params={"archived": body.archived},
    )
    return {"id": chat_id, "archived": body.archived}


@router.delete("/sessions/{chat_id}/permanent", status_code=204)
async def delete_session_permanently(
    chat_id: str,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    """Permanently delete a conversation and its messages.

    The counterpart to the reversible archive above: with Archive a named state
    the Chats page can list and undo, Delete has to mean the row is gone. The
    plain ``DELETE /sessions/{chat_id}`` keeps its long-standing soft-archive
    behavior — every existing caller (the chat page and rail row menus) is
    unchanged.

    Same ownership gate (404, never 403) and the same sandbox kill first: the
    row is about to go, so a runner still holding it would be orphaned.
    """
    _reject_restricted_principal(user, "delete a conversation")
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    await _kill_quietly(request, chat_id, reason="user_delete")
    repo.hard_delete_session(chat_id)
    log_safe(user_id=user["id"], action="chat.session.delete", resource=f"session:{chat_id}")


def _chat_config_for_delivery(request: Request):
    """The chat config the delivery gate must be resolved against.

    ``app.main`` always sets ``app.state.chat_config`` (chat-enabled or not), so
    the state read is the normal path. The fallback re-reads the same overlay
    file it loads from, which is also what ``app/api/kai.py`` reads when it
    builds the workspace archive — so a harness that mounts this router without
    app state still resolves the flag the way the delivering side will, instead
    of silently reporting "nothing is delivered".
    """
    cfg = getattr(request.app.state, "chat_config", None)
    if cfg is not None:
        return cfg
    from app.chat.config import load_chat_config
    from app.secrets import _state_dir

    return load_chat_config(_state_dir() / "instance.yaml")


@router.get("/skills")
async def list_skills(
    request: Request,
    user: dict = Depends(require_chat_access),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Server-normalized skills + commands catalog for the composer's slash menu.

    ``{"skills": [{name, description, source}], "commands": [{name, description}]}``.

    Two sources are merged server-side (see ``app.chat.skills_catalog`` for the
    full rationale): skills shipped in the bundled chat workspace template
    (``source="bundled"``) and the caller's RBAC-filtered marketplace/store
    plugin skills (``source="marketplace"``) — the same set that is materialized
    into the session's project scope, by ``app/chat/workdir.py`` (docker)
    or by the workspace archive ``app/api/kai.py`` serves (kai-agent). **Shadowing**: when a skill name is
    present in both sources, the marketplace entry wins (it is the more
    user-specific grant). Either source failing to list degrades non-fatally —
    a warning is logged and the other source's skills still come back.

    The marketplace source is offered only when something actually delivers it
    (``chat.bootstrap_marketplace``; ``marketplace_delivery`` resolves the
    mechanism per provider). With the flag off, those rows are omitted rather
    than advertised — a menu entry the agent has never been given resolves to
    "Unknown command" when the user picks it.

    Names are bare ``/<skill-name>`` tokens on every delivery path, never
    ``<plugin>:<skill>`` — verified against the sandboxed CLI's own command
    list, see the ``app.chat.skills_catalog`` module docstring.

    ``commands`` carries the slash commands the caller's stack plugins ship
    (``commands/*.md``). Their token, unlike a skill's, depends on how the
    plugin was delivered — ``/<plugin>:<command>`` where Agnes installed a real
    plugin (docker), ``/<command>`` where it could only flatten the plugin
    into project files (kai-agent). ``list_marketplace_commands`` documents the
    CLI handshake that table was verified against. Plugin AGENTS are delivered
    but deliberately not listed: they are dispatched by the Task tool, not by a
    slash command.
    """
    delivery = marketplace_delivery(_chat_config_for_delivery(request))
    return {
        "skills": merged_skills(BUNDLED_TEMPLATE_DIR, conn, user, delivery=delivery),
        "commands": merged_commands(conn, user, delivery=delivery),
    }


class JourneyUpdateBody(BaseModel):
    """Partial update — every field optional; only the ones present change."""

    first_asked: bool | None = None
    stack_setup_done: bool | None = None
    explored_stack: bool | None = None
    catalog_discovered: bool | None = None
    use_anywhere: bool | None = None
    agent_created: bool | None = None
    onboarded: bool | None = None
    successful_answers: int | None = None


@router.get("/journey")
async def get_journey(
    user: dict = Depends(require_chat_access),
):
    """Return the caller's own onboarding journey state (self-scoped —
    the RBAC gate is the chat-access resource; there is no cross-user
    read/write here since the repo call is always keyed off the caller's
    own ``user["id"]``)."""
    _reject_restricted_principal(user, "read an onboarding journey")
    return user_journey_repo().get(user["id"])


@router.put("/journey")
async def update_journey(
    body: JourneyUpdateBody,
    user: dict = Depends(require_chat_access),
):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    _reject_restricted_principal(user, "update an onboarding journey")
    return user_journey_repo().update(user["id"], **fields)


@router.post("/sessions/{chat_id}/ticket", status_code=201)
async def reissue_ticket(
    chat_id: str,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    """Mint a fresh WS ticket for an EXISTING session.

    POST /api/chat/sessions creates a new session every time. When the user
    clicks an old conversation in the sidebar after their WS dropped, the
    frontend needs a way to re-attach to the SAME chat_id (so history
    context continues, message threading is preserved) rather than start
    a new one. This endpoint is that path: 404 if the session doesn't
    exist or belongs to someone else, otherwise the same ticket+url shape
    that ``create_session`` returns.
    """
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    ticket = _issue_ticket(chat_id, user["email"])
    log_safe(user_id=user["id"], action="chat.session.ticket", resource=f"session:{chat_id}")
    return {
        "id": chat_id,
        "ws_ticket": ticket,
        "ws_url": f"/api/chat/sessions/{chat_id}/stream?ticket={ticket}",
    }


@router.get("/sessions/{chat_id}/messages")
async def list_messages(
    chat_id: str,
    request: Request,
    after_id: str | None = None,
    user: dict = Depends(require_chat_access),
):
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    msgs = repo.list_messages(chat_id, after_id=after_id)
    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "tool_calls": m.tool_calls,
            # The turn's ordered shape, so a reload renders prose and tool
            # cards in the sequence they actually happened (#1504). NULL for a
            # row written before schema v123 — the client falls back to the
            # positionless `tool_calls` above and renders those after the
            # answer, which is all the old row can honestly support.
            "parts": m.parts,
            # The composer reads this for two filters, both of which are dead
            # without it: the ArrowUp prompt-recall stack (a co-drive peer's
            # prompt must not surface under the owner's history) and
            # `renderMessage`'s peer-attribution badge, which otherwise
            # disappears on every reload. `/api/chat/copresence` already
            # exposes the field to participants and this route is owner-only
            # (a non-owner 404s above), so it discloses nothing new.
            "sender_email": m.sender_email,
            # Raw datetime, not .isoformat() — the app's datetime encoder
            # (app/serialization.py) labels the naive-UTC value `+00:00`;
            # pre-stringified it went out offset-less and the browser
            # parsed it as local time, shifting every reloaded bubble's
            # timestamp by the viewer's UTC offset.
            "created_at": m.created_at,
            # Recomputed on read rather than stored (see app/chat/sources.py):
            # the pair it needs is already here, so this costs no column, no
            # migration step and no DuckDB/Postgres parity surface — and a
            # better matcher improves history instead of only new answers.
            **({"sources": sources_verdict(m.content or "", m.tool_calls).to_dict()} if m.role == "assistant" else {}),
        }
        for m in msgs
    ]


@router.delete("/sessions/{chat_id}", status_code=204)
async def archive_session(
    chat_id: str,
    request: Request,
    user: dict = Depends(require_chat_access),
):
    repo = _get_repo(request)
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(404)
    mgr = _get_manager(request)
    try:
        await mgr.kill(chat_id, reason="user_archive")
    except Exception:
        logger.exception("kill on archive failed for %s", chat_id)
    repo.archive_session(chat_id)
    log_safe(
        user_id=user["id"],
        action="chat.session.archive",
        resource=f"session:{chat_id}",
        params={"archived": True},
    )


async def _flush_gap_replay(ws: WebSocket, gate: GapReplayGate, mgr: ChatManager, chat_id: str, last_seq: int) -> None:
    """Send the gap-replay (or ``full_refresh``) for a just-reconnected WS,
    then release ``gate`` so buffered + future live frames reach the socket.

    CRITICAL fix (2026-07-18 — reconnect replay silent-gap race): this is
    called AFTER the caller has already seated ``gate`` as a live sink via
    ``mgr.attach``/``mgr.add_sink`` — never before. The previous design
    computed this replay BEFORE seating the sink; a frame broadcast in that
    window landed in neither the snapshot nor live delivery and was
    silently lost (the client only dedups by seq — it cannot detect a gap
    it was never told about). Seating first closes that window: from the
    moment ``attach()``/``add_sink()`` returns, every broadcast for this
    session is captured — either directly in ``gate``'s buffer (if it
    raced with this function) or in the replay stream this function reads
    from (or, in the overlap case, both — ``gate.release`` de-duplicates).

    ``last_seq`` comes straight off the connect query string — a client
    that has never seen a frame for this chat (first-ever open; history
    for that case comes from ``GET /sessions/{id}/messages`` instead)
    sends ``0`` or omits it, which ``replay_since`` treats as "nothing to
    replay", not a gap (wave-2F task 3 — see ``app.chat.replay``).

    Frames at or past ``mgr.turn_buffer_min_seq(chat_id)`` are excluded
    from the stream-replay side: ``attach()``'s own ``_seat_sink`` (or
    ``add_sink``) already unconditionally queued the WHOLE in-flight turn
    buffer into ``gate`` before this function ever runs — replaying those
    same frames again from the stream would double-count them ahead of
    ``gate.release``'s de-dup (which only catches an EXACT seq match, and
    the turn-buffer frames are real, seq'd entries that would exact-match).
    """
    outcome = await replay_since(chat_id, last_seq)
    if outcome.full_refresh:
        await ws.send_json(stamp_frame(chat_id, {"type": "full_refresh"}))
        await gate.release()
        return
    turn_min = mgr.turn_buffer_min_seq(chat_id)
    frames = outcome.frames if turn_min is None else [f for f in outcome.frames if f.get("seq", 0) < turn_min]
    await gate.release(extra_frames=frames)


@router.websocket("/sessions/{chat_id}/stream")
async def ws_stream(ws: WebSocket, chat_id: str, ticket: str, last_seq: int = 0):
    try:
        consumed = _consume_ticket(ticket)
    except CoordinationUnavailable:
        await ws.close(code=4503, reason="coordination_unavailable")
        return
    if consumed is None or consumed[0] != chat_id:
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    chat_id_v, user_email = consumed

    await ws.accept()
    mgr: ChatManager = ws.app.state.chat_manager
    # CRITICAL fix (2026-07-18): wrap ws in a GapReplayGate and seat the
    # GATE as the live sink (via attach() below) BEFORE computing the
    # gap-replay/full_refresh decision — see _flush_gap_replay's docstring
    # for why the seat must happen first.
    gate = GapReplayGate(ws)

    async def reader_loop() -> None:
        try:
            while True:
                frame = await ws.receive_json()
                kind = frame.get("type")
                if kind == "user_msg":
                    # The client may send ``user_msg`` as soon as the WS is
                    # TCP-open, but ``attach()`` hasn't necessarily finished
                    # ``_spawn_runner`` (sandbox creation can take ~5 s),
                    # so ``live[chat_id]`` may not exist yet. Wait briefly
                    # for ``attach`` to populate it before raising — without
                    # this, an early ``user_msg`` triggers SessionNotFound,
                    # ws_stream closes the WS with 4404, and the user sees
                    # "Disconnected" before the runner has a chance to boot.
                    text = frame.get("text", "")
                    for _ in range(60):  # up to 30 s total at 0.5 s ticks
                        try:
                            # Thread sender_email so per-sender budgets (SR-10)
                            # and departed-participant replay-skip (SR-11) work.
                            await mgr.send_user_message(chat_id_v, text, sender_email=user_email)
                            break
                        except SessionNotFound:
                            await asyncio.sleep(0.5)
                    else:
                        # Sent directly on the WS before any LiveSession
                        # exists (so it can't go through
                        # ChatManager._broadcast) — stamp it here (wave-2F
                        # task 2).
                        await ws.send_json(
                            stamp_frame(
                                chat_id_v,
                                {
                                    "type": "error",
                                    "kind": "runner_not_ready",
                                    "message": "Runner did not become ready within 30 s.",
                                },
                            )
                        )
                elif kind == "cancel":
                    await mgr.cancel(chat_id_v)
                elif kind == "approval_decision":
                    rid = frame.get("request_id")
                    dec = frame.get("decision")
                    if isinstance(rid, str) and rid and dec in ("allow", "allow_session", "deny"):
                        try:
                            await mgr.deliver_approval_decision(chat_id_v, rid, dec, sender_email=user_email)
                        except SessionNotFound:
                            pass
                elif kind == "question_answer":
                    # AskUserQuestion card answer. Only answers/dismissed are
                    # accepted from a client — the manager hardens the dict
                    # and the "unattended" resolution is manager-originated
                    # only, so it cannot be forged over a WebSocket.
                    rid = frame.get("request_id")
                    answers = frame.get("answers")
                    dismissed = bool(frame.get("dismissed"))
                    if isinstance(rid, str) and rid and (dismissed or isinstance(answers, dict)):
                        try:
                            await mgr.deliver_question_answer(
                                chat_id_v,
                                rid,
                                answers=answers if isinstance(answers, dict) else None,
                                dismissed=dismissed,
                                sender_email=user_email,
                            )
                        except SessionNotFound:
                            pass
        except WebSocketDisconnect:
            return

    try:
        await mgr.attach(chat_id_v, gate)
        await _flush_gap_replay(ws, gate, mgr, chat_id_v, last_seq)
        await reader_loop()
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
    finally:
        await mgr.detach_sink(chat_id_v, gate)


@router.websocket("/sessions/{session_id}/join")
async def ws_join(ws: WebSocket, session_id: str, ticket: str, last_seq: int = 0):
    """WebSocket join route for co-drive participants.

    A participant who obtained a ticket via POST /api/chat/{id}/join-ticket
    connects here to join a live co-session.  The route:

      1. Consumes the short-lived opaque ticket (same coordination-backed
         ticket mechanism as ws_stream) to recover (session_id, participant_email).
      2. Re-verifies that the email is a live (left_at IS NULL) participant
         of the session (SR-9: membership re-verified at WS connect time,
         not just at ticket issuance).
      3. Calls mgr.add_sink(session_id, gate, participant_email), which
         replays persisted history to the joiner and then fans out new
         frames to them alongside the primary sink.

    This is the ONLY path that calls add_sink for web co-drive joiners.
    The primary owner always connects via ws_stream (which calls attach).
    """
    try:
        consumed = _consume_ticket(ticket)
    except CoordinationUnavailable:
        await ws.close(code=4503, reason="coordination_unavailable")
        return
    if consumed is None or consumed[0] != session_id:
        await ws.close(code=4401, reason="invalid_or_expired_ticket")
        return
    _session_id_v, participant_email = consumed

    mgr: ChatManager = ws.app.state.chat_manager
    repo = ws.app.state.chat_repo

    # SR-9: re-verify live participant membership at WS connect time.
    # The ticket was issued at join-ticket time (SR-9 verified there too),
    # but the participant may have left between ticket issuance and WS connect.
    parts = repo.get_session_participants(session_id)
    if not any(p.user_email == participant_email and p.left_at is None for p in parts):
        await ws.close(code=4403, reason="not_a_live_participant")
        return

    await ws.accept()
    # CRITICAL fix (2026-07-18): same seat-before-replay gate as ws_stream
    # (see _flush_gap_replay's docstring), using the verified session_id
    # (consumed[0] already checked == session_id above).
    gate = GapReplayGate(ws)

    async def joiner_reader_loop() -> None:
        try:
            while True:
                frame = await ws.receive_json()
                kind = frame.get("type")
                if kind == "user_msg":
                    text = frame.get("text", "")
                    for _ in range(60):
                        try:
                            # Thread sender_email so per-sender budgets (SR-10)
                            # and departed-participant replay-skip (SR-11) work.
                            await mgr.send_user_message(session_id, text, sender_email=participant_email)
                            break
                        except SessionNotFound:
                            await asyncio.sleep(0.5)
                    else:
                        # See ws_stream's identical branch above — stamp for
                        # the same reason (wave-2F task 2).
                        await ws.send_json(
                            stamp_frame(
                                session_id,
                                {
                                    "type": "error",
                                    "kind": "runner_not_ready",
                                    "message": "Runner did not become ready within 30 s.",
                                },
                            )
                        )
                elif kind == "cancel":
                    await mgr.cancel(session_id)
                elif kind == "approval_decision":
                    # Co-drive participants may approve: they can already
                    # steer the session (arbitrary user messages), so this
                    # is no escalation. Same validation as ws_stream.
                    rid = frame.get("request_id")
                    dec = frame.get("decision")
                    if isinstance(rid, str) and rid and dec in ("allow", "allow_session", "deny"):
                        try:
                            await mgr.deliver_approval_decision(session_id, rid, dec, sender_email=participant_email)
                        except SessionNotFound:
                            pass
                elif kind == "question_answer":
                    # Co-drive participants may answer questions for the same
                    # reason they may approve. Same validation as ws_stream.
                    rid = frame.get("request_id")
                    answers = frame.get("answers")
                    dismissed = bool(frame.get("dismissed"))
                    if isinstance(rid, str) and rid and (dismissed or isinstance(answers, dict)):
                        try:
                            await mgr.deliver_question_answer(
                                session_id,
                                rid,
                                answers=answers if isinstance(answers, dict) else None,
                                dismissed=dismissed,
                                sender_email=participant_email,
                            )
                        except SessionNotFound:
                            pass
        except WebSocketDisconnect:
            return

    try:
        # add_sink replays history and appends the gate to live.sinks.
        # SR-9: raises PermissionError if participant left between accept()
        # and add_sink(); close with 4403 in that case.
        await mgr.add_sink(session_id, gate, participant_email)
        await _flush_gap_replay(ws, gate, mgr, session_id, last_seq)
        await joiner_reader_loop()
    except PermissionError:
        await ws.close(code=4403, reason="not_a_live_participant")
    except SessionNotFound:
        await ws.close(code=4404, reason="session_not_found")
    finally:
        # Mirror ws_stream: a departed joiner must not leave a dead sink in
        # live.sinks — it would block the last-sink detach (linger→pause)
        # policy until the idle reaper. No-op if add_sink never seated it.
        await mgr.detach_sink(session_id, gate)
