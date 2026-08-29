"""Slack event dispatcher — routes incoming events to handlers."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Coroutine, Optional

from app.chat import routing
from services.slack_bot.binding import (
    bind_prompt,
    issue_verification_code,
    is_channel_allowlisted,
    lookup_user_email,
)
from services.slack_bot.sender import add_reaction, send_ephemeral_to_user, send_thread_reply
from services.slack_bot.sink import SlackSinkBridge

logger = logging.getLogger(__name__)

# Strong references to every scheduled dispatch task. asyncio only keeps a
# weak ref to a bare create_task() result, so a fire-and-forget task can be
# GC-collected (and cancelled) mid-flight. Holding it here until the
# done-callback discards it guarantees the dispatch runs to completion.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _schedule(coro: "Coroutine[Any, Any, Any]") -> asyncio.Task:
    """Schedule a coroutine on the running loop, retaining a strong ref.

    Used at every transport's dispatch call site (HTTP endpoint + Socket
    Mode) so the slow body runs *after* the 3s Slack ack has been sent.
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _run_logged(
    coro: "Coroutine[Any, Any, Any]",
    *,
    on_failure: Optional[Callable[[BaseException], Awaitable[None]]] = None,
) -> None:
    """Wrap a scheduled dispatch coroutine — the ONLY recovery path.

    Because we ack Slack *before* processing (ack-then-async), a failure
    here does NOT trigger a Slack retry. So this wrapper must (a) never let
    the exception escape — an escaped exception surfaces as an asyncio
    "Task exception was never retrieved" and silently drops the work — and
    (b) drive the best-effort user-visible recovery notice.

    ``on_failure`` is that recovery seam: an awaitable the caller supplies
    to post a user-visible ephemeral with the failure. It is itself
    best-effort — a notifier that raises is caught and logged, never
    propagated. Phase 0 call sites pass ``on_failure=None`` (the HTTP DM
    handler emits its own binding/error replies inline, and the context-free
    dispatch here carries no channel/response_url to post to, and the
    ``send_ephemeral`` helper does not exist until Phase 2). Later phases
    (mentions/slash/interactivity), which have channel/response_url context,
    pass an ``on_failure`` that posts the ephemeral. The seam is wired and
    tested now; only the concrete ephemeral payload is deferred.
    """
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 — last line of defence for a detached task
        logger.exception("scheduled Slack dispatch failed")
        if on_failure is not None:
            try:
                await on_failure(exc)
            except Exception:  # noqa: BLE001 — recovery notice is best-effort
                logger.exception("best-effort Slack failure notice failed")


#: enforce_sender_limits raises bare RuntimeError with these reason strings —
#: on a routed thread the limits key on the AGENT OWNER, so a channel member
#: who tripped nothing themselves needs to be told what happened (the in-band
#: sink error frame covers only the locally-attached path; the cross-gateway
#: forward and the api-replica producer have no local sink).
_SENDER_LIMIT_MESSAGES = {
    "daily_budget_exhausted": "The agent's daily spend cap is reached — try again tomorrow.",
    "max_session_tokens_exhausted": "This thread hit its session token cap — start a new thread.",
    "rate_limit_exceeded": "The agent is receiving too many messages right now — try again in a few minutes.",
}

#: ConcurrencyCapHit refusal text, shared by both roles that can raise it on a
#: mention — the gateway's own create_session and the api replica's producer
#: forward through _send_or_explain_limit. One constant so the two can't drift.
_AT_CAPACITY_MESSAGE = "The agent is at capacity right now — please try again in a few minutes."


async def _send_or_explain_limit(mgr_send, channel: str, slack_user_id: str) -> None:
    """Run one send coroutine; answer a known sender-limit refusal with an
    ephemeral instead of letting it vanish into the background-task log.

    ``ConcurrencyCapHit`` is caught alongside the RuntimeError reasons
    because it is a plain ``Exception``, not a RuntimeError, and the
    api-replica producer path reaches ``resolve_or_create_slack_session``
    through here — an uncaught one unwinds into ``_run_logged``, which logs
    and swallows, leaving the mentioner with the 👀 ack and then silence.
    Routed threads pool on the AGENT OWNER's cap, so hitting it is routine.
    Wording is kept identical to the gateway branch's own cap refusal in
    ``_handle_mention`` — the same refusal must read the same on both roles.
    """
    from app.chat.manager import ConcurrencyCapHit

    try:
        await mgr_send
    except ConcurrencyCapHit:
        await send_ephemeral_to_user(channel, slack_user_id, _AT_CAPACITY_MESSAGE)
    except RuntimeError as exc:
        msg = _SENDER_LIMIT_MESSAGES.get(str(exc))
        if msg is None:
            raise
        await send_ephemeral_to_user(channel, slack_user_id, msg)


async def dispatch_event(app, event: dict[str, Any]) -> None:
    etype = event.get("type")
    if etype == "message":
        await _handle_dm(app, event)
    elif etype == "app_mention":
        await _handle_mention(app, event)


def _strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove the bot's own ``<@ID>`` / ``<@ID|label>`` mention token(s) from
    an app_mention text body and return the trimmed remainder.

    ``bot_user_id`` None (not yet resolved) → just trim — never echo the raw
    ``<@…>`` token into the runner.
    """
    if not text:
        return ""
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]*)?>", "", text)
    return text.strip()


def _is_attached(mgr, chat_id: str) -> bool:
    """True iff `chat_id` already has a live attach (sink pumping)."""
    return any(live.chat_id == chat_id for live in mgr.list_live())


def _has_slack_sink(mgr, chat_id: str, channel: str) -> bool:
    """True iff ``chat_id``'s live session already has a SlackSinkBridge
    for ``channel``.

    ``_is_attached`` alone is not enough on the OWNER after a cross-gateway
    takeover: ``ChatManager._takeover_foreign_session`` builds its fresh
    LiveSession with ``sinks=[]``, so the session is "attached" (live) but
    has nowhere to deliver Slack replies — every webhook handler that used
    to skip sink creation whenever a live entry existed silently dropped
    all subsequent replies for Slack-surfaced sessions. Handlers use this
    to re-seat a bridge in that live-but-sinkless window."""
    for live in mgr.list_live():
        if getattr(live, "chat_id", None) != chat_id:
            continue
        for entry in getattr(live, "sinks", []):
            sink = getattr(entry, "sink", None)
            if isinstance(sink, SlackSinkBridge) and sink._channel == channel:
                return True
    return False


async def _produce_slack_message(
    app, *, user_email: str, surface, channel: str, thread_ts: str, text: str, agent_id: Optional[str] = None
) -> None:
    """Thin-producer forward for a Slack message handled on a process with
    NO ChatManager — i.e. an api-role replica, where ``app.state.
    chat_manager`` is ``None`` because only ``Role.GATEWAY`` processes
    construct one (wave-2F final review F1).

    An api replica is never an owner, so this ALWAYS forwards: resolve or
    create the session row, run the same sender-limit gate as the owner
    path, persist the user message, and publish it over the
    ``chat-in:{chat_id}`` stream with its Slack origin so the owning
    gateway's consumer (re-)establishes the reply sink before delivering.
    All via ``app.chat.manager``'s module-level thin-producer helpers (the
    ChatManager methods delegate to the same implementations, so the two
    paths cannot drift). The reference m-tier LB rule routes
    ``/api/slack/*`` to gateway-role upstreams (docs/DEPLOYMENT.md -> chat
    HA -> LB routing rule), so this is the graceful-degradation fallback,
    not the primary route — note a session no gateway ever owns has no
    consumer to deliver to (see the thin-producer section's reach
    disclosure in app/chat/manager.py).
    """
    from app.chat import manager as chat_manager_mod
    from app.chat.types import Surface

    repo = app.state.chat_repo
    config = getattr(app.state, "chat_config", None)
    if config is None:
        logger.warning("thin-producer forward skipped: app.state.chat_config missing (chat init failed?)")
        return
    session = chat_manager_mod.resolve_or_create_slack_session(
        repo,
        config,
        user_email=user_email,
        surface=surface,
        slack_channel_id=channel,
        slack_thread_ts=thread_ts if surface == Surface.SLACK_THREAD else None,
        agent_id=agent_id,
    )
    await chat_manager_mod.produce_inbound_user_message(
        repo,
        config,
        session.id,
        text,
        slack_origin={"channel": channel, "thread_ts": thread_ts},
    )


async def _owned_by_other_gateway(chat_id: str) -> bool:
    """True iff ``chat_id``'s routing lease is currently held by a
    DIFFERENT, presumably-still-live gateway replica than this process
    (wave-2F task 7 — thin Slack webhook producers).

    A Slack HTTP webhook can land on ANY gateway replica behind the load
    balancer, regardless of which replica actually owns (spawned/attached)
    the session. Blindly calling ``ChatManager.attach`` here would hit its
    "no local LiveSession, but the routing lease is held elsewhere" branch,
    which is a cross-gateway TAKEOVER — it destroys the session's runner on
    its current owner and respawns a fresh one on THIS replica (see
    ``ChatManager.attach`` / ``_takeover_foreign_session`` docstrings). That
    behavior exists for a reconnecting web WS, which really does need to be
    local to whichever gateway now holds the socket — it is the wrong
    behavior for a webhook that has no such requirement and would otherwise
    silently steal (and interrupt) every session on every load-balanced
    request.

    Checking ownership first lets the handler skip attach/wait_until_live
    entirely when a foreign owner is live and fall straight through to
    ``ChatManager.send_user_message``, which already forwards the message
    over the ``chat-in:{chat_id}`` coordination stream to whichever gateway
    owns the session (``ChatManager._forward_inbound_message``) — no local
    spawn, no takeover, no assumption that this process hosts the session.

    Returns False (safe to attach locally) when the lease is unclaimed/
    expired, held by this same gateway, or the coordination backend is
    unavailable (``routing.owner_of`` already degrades to ``None`` in that
    case) — all of those fall through to the existing local resume/spawn
    path, unchanged.
    """
    this_gw = routing.this_gateway_id()
    owner = await asyncio.to_thread(routing.owner_of, chat_id)
    return owner is not None and owner != this_gw


async def _handle_dm(app, event: dict) -> None:
    if event.get("channel_type") != "im" or event.get("bot_id"):
        return
    slack_user_id = event.get("user")
    # Some "message" events carry no user — message edits/deletions and other
    # subtypes, unfurl side-effects. Without this guard such an event falls
    # through to issue_verification_code(slack_user_id=None) below and trips the
    # slack_binding_codes.slack_user_id NOT NULL constraint, crashing dispatch.
    # Mirrors the guard _handle_mention already applies.
    if not slack_user_id:
        return
    text = event.get("text", "")
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    repo = app.state.chat_repo
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        # First DM from an unbound user: mint a 6-digit code and reply with
        # a one-click /slack/bind?code= magic link (bind_prompt). Opening it
        # while signed in to Agnes redeems the code — no copy-paste.
        code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        await send_thread_reply(channel, thread_ts, bind_prompt(public_url, code))
        return
    # Cloud chat is an RBAC resource (default-deny). A bound Slack user still
    # needs the grant on their group, same as the web surface — check before
    # spawning a session so Slack can't bypass the gate.
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from src.repositories import users_repo

    _u = users_repo().get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", repo._conn):
        await send_thread_reply(
            channel,
            thread_ts,
            "You don't have access to Agnes chat yet — ask an admin to grant your group access on /admin/access.",
        )
        return
    # F2d (audit-full-coverage plan, Task 6): a read event — the bot accepted
    # an inbound DM from a bound, granted user. client_kind is explicit since
    # this dispatch coroutine runs detached from any HTTP request context
    # (ack-then-async — see the module docstring in app/api/slack.py).
    from src.audit_helpers import log_safe

    log_safe(
        user_id=_u["id"],
        action="slack.message",
        resource=f"channel:{channel}",
        params={"surface": "dm"},
        client_kind="slack",
    )
    mgr = app.state.chat_manager
    from app.chat.types import Surface

    if mgr is None:
        # api-role replica (no ChatManager in this process): thin-producer
        # forward — never attach/spawn here, an api replica is never an
        # owner. See _produce_slack_message.
        await _produce_slack_message(
            app,
            user_email=user_email,
            surface=Surface.SLACK_DM,
            channel=channel,
            thread_ts=thread_ts,
            text=text,
        )
        return
    session = await mgr.create_session(
        user_email=user_email,
        surface=Surface.SLACK_DM,
        slack_channel_id=channel,
    )
    # wave-2F task 7: this HTTP webhook can land on ANY gateway replica, not
    # necessarily the one that owns this session. Only attach/spawn locally
    # when THIS replica would actually become (or already is) the owner —
    # if a different, still-live gateway already owns it, skip straight to
    # send_user_message (which forwards over the inbound coordination
    # stream) instead of triggering attach()'s cross-gateway takeover. See
    # _owned_by_other_gateway's docstring. slack_origin rides the forwarded
    # envelope so the OWNER's consumer can (re-)establish the Slack sink.
    if await _owned_by_other_gateway(session.id):
        await mgr.send_user_message(
            session.id,
            text,
            slack_origin={"channel": channel, "thread_ts": thread_ts},
        )
        return
    # Attach a SlackSinkBridge if no pump is running for this session yet.
    # The bridge forwards assistant_message frames to send_thread_reply so
    # the user actually sees the answer in Slack.
    if not _is_attached(mgr, session.id):
        web_base = getattr(app.state, "public_url", "")
        sink = SlackSinkBridge(
            channel=channel,
            thread_ts=thread_ts,
            chat_id=session.id,
            owner=user_email,
            web_base=web_base,
        )
        _schedule(mgr.attach(session.id, sink))
        # attach() never returns during a session's lifetime (it awaits the
        # pump), so we can't await it — but it spawns the sandbox first, which
        # takes several seconds. Wait (bounded) for the live session to register
        # before injecting the turn; a fixed sleep raced attach() and dropped
        # the user's first message with SessionNotFound.
        if not await mgr.wait_until_live(session.id):
            await send_thread_reply(
                channel,
                thread_ts,
                "Agnes is still starting up — please resend your message in a few seconds.",
            )
            return
    elif not _has_slack_sink(mgr, session.id, channel):
        # Live session with no Slack sink for this channel — the
        # post-takeover window (_takeover_foreign_session seats sinks=[]).
        # Re-establish the bridge or replies silently stop reaching Slack.
        sink = SlackSinkBridge(
            channel=channel,
            thread_ts=thread_ts,
            chat_id=session.id,
            owner=user_email,
            web_base=getattr(app.state, "public_url", ""),
        )
        _schedule(mgr.attach(session.id, sink))
    await mgr.send_user_message(session.id, text)


async def _handle_mention(app, event: dict) -> None:
    """Channel @agnes mention → public in-thread reply on a persistent
    SLACK_THREAD session owned by the mention starter. Gated by the
    per-channel allowlist (default-deny). All denials are ephemeral.
    """
    # 2. Bot loop-guard: ignore our own / any bot's posts.
    bot_user_id = getattr(app.state, "slack_bot_user_id", None)
    if event.get("bot_id") or (bot_user_id and event.get("user") == bot_user_id):
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    slack_user_id = event.get("user")
    text = event.get("text", "")
    if not slack_user_id:
        return
    repo = app.state.chat_repo
    conn = repo._conn

    # 3. Allowlist (direct Everyone grant — never can_access).
    if not is_channel_allowlisted(conn, channel):
        await send_ephemeral_to_user(channel, slack_user_id, "Agnes isn't enabled in this channel.")
        return

    # 4. Identity binding.
    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        await send_ephemeral_to_user(channel, slack_user_id, bind_prompt(public_url, code))
        return

    # 5. CHAT grant.
    from app.auth.access import can_access
    from app.resource_types import ResourceType
    from src.repositories import users_repo

    _u = users_repo().get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", conn):
        await send_ephemeral_to_user(
            channel,
            slack_user_id,
            "You don't have access to Agnes chat yet — ask an admin to grant your group access on /admin/access.",
        )
        return
    # F2d (audit-full-coverage plan, Task 6): see _handle_dm's twin call for
    # why client_kind is explicit here.
    from src.audit_helpers import log_safe

    log_safe(
        user_id=_u["id"],
        action="slack.message",
        resource=f"channel:{channel}",
        params={"surface": "mention"},
        client_kind="slack",
    )

    # 5b. Channel→agent binding: an agent holding scope item
    # ('slack_channel', <channel_id>) owns mentions in this channel — the
    # session runs with that agent's persona, scope, and budget. Unbound
    # channels keep the agent-less behavior bit-for-bit. Best-effort: a
    # routing lookup failure degrades to unrouted rather than dropping the
    # mention.
    from src.repositories import agents_repo

    bound_agent = None
    routed_session_user: Optional[str] = None
    try:
        bound_agent = agents_repo().agent_for_scope_item("slack_channel", channel)
    except Exception:
        logger.exception("slack_channel binding lookup failed for %s — continuing unrouted", channel)
    if bound_agent is not None:
        # A routed session runs AS THE AGENT'S OWNER end to end — session
        # row, sandbox workspace, rails, personal override, and brokered
        # authority all resolve from one identity, exactly like the agent's
        # API/scheduled runs. The mentioner's identity gates participation
        # (allowlist + binding + CHAT above) and rides along as sender
        # attribution; it must NOT shape the workspace, or the same bound
        # agent would present different rails per invoker and pick up an
        # arbitrary channel member's personal CLAUDE.local.md as standing
        # instructions.
        from src.agent_scope_intersection import agent_is_passthrough

        if agent_is_passthrough(bound_agent):
            # Defense in depth for rows predating the API-side guard: an
            # all-'all' agent's routed turns would ride the owner's PLAIN
            # identity via the broker's passthrough optimization — never
            # route to one.
            logger.warning(
                "slack_channel binding for %s skipped: agent %s has every scope mode 'all'",
                channel,
                bound_agent.get("id"),
            )
            bound_agent = None
        owner_row = None if bound_agent is None else users_repo().get_by_id(str(bound_agent["owner_user_id"]))
        if bound_agent is None:
            pass
        elif owner_row is None or not owner_row.get("email"):
            logger.warning(
                "slack_channel binding for %s points at agent %s with no resolvable owner — continuing unrouted",
                channel,
                bound_agent.get("id"),
            )
            bound_agent = None
        elif not can_access(owner_row["id"], ResourceType.CHAT.value, "chat", conn):
            # The routed session runs AS the owner, so the owner's CHAT grant
            # is the authority that matters here — revoking a user's chat
            # access must also stop their agent's channel turns, or the
            # binding would keep spawning sessions under a revoked identity.
            logger.warning(
                "slack_channel binding for %s skipped: agent %s's owner no longer holds the CHAT grant",
                channel,
                bound_agent.get("id"),
            )
            bound_agent = None
        else:
            routed_session_user = owner_row["email"]

    # 6. Thread session: reuse or create. The dedupe is per (channel,
    # thread_ts) regardless of user, so which identity a mention runs as is
    # decided by the EXISTING row first and the binding second — a binding
    # governs NEW threads only:
    #   - existing SERVICE thread (agent_id set — only routing creates these
    #     on the Slack surface): shared; any gated member continues it, even
    #     after the channel is unbound (the session keeps its stored agent).
    #   - existing HUMAN thread (no agent_id — e.g. started before the
    #     channel was bound): belongs to its starter, exactly as pre-binding;
    #     the mention runs unrouted so the starter's thread keeps working.
    #   - no existing thread: the binding routes it (owner-owned session).
    mgr = app.state.chat_manager
    from app.chat.types import Surface

    existing = repo.get_slack_thread_session(channel, thread_ts)
    existing_agent_id = getattr(existing, "agent_id", None) if existing is not None else None
    service_thread = existing_agent_id is not None
    if existing is not None and bound_agent is not None and existing_agent_id != bound_agent["id"]:
        # Pre-binding human thread, or a thread routed to a previously-bound
        # agent: this mention does not run the currently-bound agent.
        bound_agent = None
        routed_session_user = None

    if service_thread and bound_agent is None:
        # The stored agent_id outlives its binding, so an unbound (or
        # re-bound) channel reaches here with service_thread True and none
        # of the 5b checks run — they all sit under `if bound_agent is not
        # None`. Re-assert them against the STORED agent, because the turn
        # is still delivered into an agent-carrying, owner-owned session:
        # without this an agent widened to all-'all' after unbinding takes
        # the broker's passthrough optimization (session user == owner, so
        # `_mint_identity_jwt` never diverts to mint_agent_session_jwt) and
        # runs on the owner's PLAIN identity JWT, admin short-circuit and
        # all. A missing/deleted agent is refused here too — the broker
        # already fails closed on it (401 ticket_agent_not_found), so
        # delivering would buy an opaque failure instead of a plain answer.
        from src.agent_scope_intersection import agent_is_passthrough

        stored_agent = None
        try:
            stored_agent = agents_repo().get_by_id(str(existing_agent_id))
        except Exception:
            logger.exception("stored agent lookup failed for service thread %s", existing_agent_id)
        stored_owner = (
            None
            if stored_agent is None or stored_agent.get("deleted_at") is not None
            else users_repo().get_by_id(str(stored_agent["owner_user_id"]))
        )
        if (
            stored_agent is None
            or stored_agent.get("deleted_at") is not None
            or agent_is_passthrough(stored_agent)
            or stored_owner is None
            or not stored_owner.get("email")
            or not can_access(stored_owner["id"], ResourceType.CHAT.value, "chat", conn)
        ):
            logger.warning(
                "service thread %s refused: stored agent %s is missing, deleted, all-'all', "
                "or its owner no longer holds the CHAT grant",
                getattr(existing, "id", None),
                existing_agent_id,
            )
            await send_ephemeral_to_user(
                channel,
                slack_user_id,
                "This thread's agent is no longer available — start a new thread.",
            )
            return

    # The identity the session row (and everything keyed off it) belongs to.
    # (routed_session_user is non-None exactly when bound_agent survived all
    # the routing checks above — the two are set and cleared together.)
    session_user = routed_session_user if routed_session_user is not None else user_email

    if existing is not None and not service_thread and existing.user_email != user_email:
        # Human thread owned by someone else. Resolved through the factory
        # (not a raw query on the DuckDB-typed conn) so the owner's
        # slack_user_id is read from whichever backend is active.
        owner_row = users_repo().get_by_email(existing.user_email)
        owner_slack_id = owner_row.get("slack_user_id") if owner_row else None
        owner_ref = f"<@{owner_slack_id}>" if owner_slack_id else "another user"
        await send_ephemeral_to_user(channel, slack_user_id, f"This thread belongs to {owner_ref}.")
        return

    if bound_agent is not None or service_thread:
        # Instant acknowledgement on the mentioning message, before the
        # (seconds-long) session spawn — but AFTER every gate that can still
        # refuse the mention (allowlist, identity, CHAT grant, thread
        # ownership above): an ack on a mention we then reject would promise
        # an answer that never comes. Fire-and-forget; add_reaction swallows
        # its own failures.
        _schedule(add_reaction(channel, event["ts"], "eyes"))

    # 7. Strip our own mention token. (Before session creation so the
    # api-role thin-producer branch below can forward the cleaned text.)
    clean = _strip_bot_mention(text, bot_user_id)

    # 7b. Routed sessions get a one-time context header on their FIRST turn
    # so an agent granted Slack tools can operate on the correct thread —
    # the sandbox otherwise never learns channel/ts identifiers. Keyed on
    # "no message ever delivered", not on row existence: create_session
    # persists the row BEFORE the liveness wait, so a first mention that
    # times out on startup leaves a zero-message session behind and the
    # retry must still carry the header. A pre-binding session with real
    # messages gets none (dedupe wins over the binding).
    if (bound_agent is not None or service_thread) and (existing is None or (existing.message_count or 0) == 0):
        clean = (
            f"[slack context: channel={channel} thread_ts={thread_ts} "
            f"message_ts={event['ts']} sender=<@{slack_user_id}>]\n{clean}"
        )
    elif bound_agent is not None or service_thread:
        # Follow-up turn on a shared routed thread: the session belongs to
        # the agent's owner, so without attribution the agent cannot tell
        # WHO is asking (the reviewer requesting a revision vs. the author).
        clean = f"[slack sender=<@{slack_user_id}>]\n{clean}"

    if mgr is None:
        # api-role replica (no ChatManager in this process): thin-producer
        # forward — see _handle_dm's twin branch and _produce_slack_message.
        await _send_or_explain_limit(
            _produce_slack_message(
                app,
                user_email=session_user,
                surface=Surface.SLACK_THREAD,
                channel=channel,
                thread_ts=thread_ts,
                text=clean,
                agent_id=bound_agent["id"] if bound_agent else None,
            ),
            channel,
            slack_user_id,
        )
        return
    from app.chat.manager import ConcurrencyCapHit

    try:
        session = await mgr.create_session(
            user_email=session_user,
            surface=Surface.SLACK_THREAD,
            slack_channel_id=channel,
            slack_thread_ts=thread_ts,
            agent_id=bound_agent["id"] if bound_agent else None,
        )
    except ConcurrencyCapHit:
        # Routed sessions pool on the AGENT OWNER's concurrency cap, so a
        # busy bound channel can hit it through no fault of the mentioner —
        # a silent drop (background log only) reads as the bot ignoring
        # people. Say so instead.
        await send_ephemeral_to_user(channel, slack_user_id, _AT_CAPACITY_MESSAGE)
        return

    # 8. Attach (NOT awaited — keep the 3s ack budget). wave-2F task 7: skip
    # entirely when a different, still-live gateway already owns this
    # session — see _owned_by_other_gateway's docstring for why attaching
    # here would otherwise trigger a cross-gateway takeover; slack_origin
    # rides the forwarded envelope so the OWNER re-establishes the sink.
    if await _owned_by_other_gateway(session.id):
        await _send_or_explain_limit(
            mgr.send_user_message(
                session.id,
                clean,
                slack_origin={"channel": channel, "thread_ts": thread_ts},
            ),
            channel,
            slack_user_id,
        )
        return
    if not _is_attached(mgr, session.id):
        sink = SlackSinkBridge(
            channel=channel,
            thread_ts=thread_ts,
            chat_id=session.id,
            owner=session.user_email,
            web_base=getattr(app.state, "public_url", ""),
        )
        _schedule(mgr.attach(session.id, sink))
        # Bounded wait for the live session — attach() spawns the sandbox
        # (seconds) before registering, so a fixed sleep raced it and dropped
        # the first turn with SessionNotFound. attach() itself never returns.
        if not await mgr.wait_until_live(session.id):
            await send_ephemeral_to_user(
                channel,
                slack_user_id,
                "Agnes is still starting up — please resend in a few seconds.",
            )
            return
    elif not _has_slack_sink(mgr, session.id, channel):
        # Live-but-sinkless (post-takeover) window — see _has_slack_sink.
        sink = SlackSinkBridge(
            channel=channel,
            thread_ts=thread_ts,
            chat_id=session.id,
            owner=session.user_email,
            web_base=getattr(app.state, "public_url", ""),
        )
        _schedule(mgr.attach(session.id, sink))

    # 9. Inject the user turn. send_user_message(chat_id, text) — no sender_email
    #    (per-sender attribution arrives with Phase 5a's multi-sink refactor).
    #    A sender-limit refusal (keyed on the session owner — the AGENT owner
    #    for routed threads) is answered with an ephemeral; the attached sink
    #    also posts the in-band error frame to the thread.
    await _send_or_explain_limit(mgr.send_user_message(session.id, clean), channel, slack_user_id)
