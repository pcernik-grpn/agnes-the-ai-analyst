"""Slack slash-command dispatcher — routes /agnes* commands to handlers.

Each handler delivers its answer asynchronously via the command's
response_url (30-min / 5-post limited → single-shot). /agnes help is the
only synchronous path (its body rides the 3 s ack).

This module owns its own _schedule + _run_logged (Phase 0's copies live
in events.py but are not depended upon here — verified absent at authoring
time; keeping them local makes this phase self-contained).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from services.slack_bot.binding import (
    bind_prompt,
    issue_verification_code,
    lookup_user_email,
)
from services.slack_bot.sender import open_im, send_ephemeral
from services.slack_bot.sink import EphemeralCommandSink

logger = logging.getLogger(__name__)

_BG_TASKS: set[asyncio.Task] = set()


def _schedule(coro) -> None:
    """Fire-and-forget a coroutine, keeping a strong ref so the GC can't
    cancel an in-flight dispatch."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _run_logged(coro, *, response_url: Optional[str] = None) -> None:
    """Run a dispatch coroutine, swallowing + logging any unhandled
    exception. Because the endpoint acks before dispatch, an exception
    here never triggers a Slack retry — this is the only recovery path,
    so on failure post a best-effort ephemeral to the caller's
    response_url (if one was supplied)."""
    try:
        await coro
    except Exception:
        logger.exception("unhandled exception in slash-command dispatch")
        if response_url:
            try:
                await send_ephemeral(
                    response_url,
                    ":warning: Something went wrong handling that command. Please try again.",
                )
            except Exception:
                logger.exception("failed to post error ephemeral")


def _help_body() -> str:
    return (
        "*Agnes slash commands*\n"
        "• `/agnes <question>` — ask Agnes; the answer also appears on web /chat.\n"
        "• `/agnes-new` — archive your current Agnes DM session and start fresh.\n"
        "• `/agnes-status` — show your active session count and cap.\n"
        "• `/agnes help` — show this message."
    )


def _audit_slash_command(app, cmd: dict[str, Any], command: str) -> None:
    """F2d (audit-full-coverage plan, Task 6): one row per dispatched slash
    command. Runs in a detached background task (ack-then-async, same as
    events.py) so client_kind is stamped explicitly. Identity resolves
    through the same binding lookup as the handlers below; an unbound
    caller has no email to fall back to, so a searchable
    ``slack:<user_id>`` string stands in — mirrors
    ``app.chat.audit._resolve_user_id``'s "never drop the row" convention.
    """
    from src.audit_helpers import log_safe
    from src.repositories import users_repo

    slack_user_id = cmd.get("user_id", "")
    email = lookup_user_email(app.state.chat_repo, slack_user_id)
    if email:
        row = users_repo().get_by_email(email)
        user_id = row["id"] if row else email
    else:
        user_id = f"slack:{slack_user_id}"
    log_safe(
        user_id=user_id,
        action="slack.command",
        resource=command or "unknown",
        params={"command": command},
        client_kind="slack",
    )


async def dispatch_command(app, cmd: dict[str, Any]) -> None:
    command = (cmd.get("command") or "").strip()
    handler = SLACK_COMMAND_HANDLERS.get(command)
    # Audited only for a command this dispatcher actually acts on — an
    # unrecognized command never touches `app` at all (see
    # test_dispatch_command_routes_unknown_to_noop, which passes a bare
    # `object()` for `app` on that path).
    if handler is None:
        logger.info("unknown slash command: %s", command)
        return
    _audit_slash_command(app, cmd, command)
    await handler(app, cmd)


def _is_attached(mgr, chat_id: str) -> bool:
    return any(live.chat_id == chat_id for live in mgr.list_live())


async def _cmd_agnes(app, cmd: dict) -> None:
    from app.auth.access import can_access
    from app.chat.manager import ConcurrencyCapHit
    from app.chat.types import Surface
    from app.resource_types import ResourceType
    from src.repositories import users_repo

    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    slack_user_id = cmd.get("user_id", "")
    text = (cmd.get("text") or "").strip()
    response_url = cmd.get("response_url", "")

    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        await send_ephemeral(response_url, bind_prompt(public_url, code))
        return

    _u = users_repo().get_by_email(user_email)
    if not _u or not can_access(_u["id"], ResourceType.CHAT.value, "chat", repo._conn):
        await send_ephemeral(
            response_url,
            "You don't have access to Agnes chat yet — ask an admin to grant your group access on /admin/access.",
        )
        return

    im_channel = await open_im(slack_user_id)
    if im_channel is None:
        await send_ephemeral(response_url, ":warning: Couldn't open a DM channel. Try again.")
        return

    if mgr is None:
        # api-role replica: no ChatManager runs in this process (only
        # Role.GATEWAY builds one) — thin-producer forward (wave-2F final
        # review F1): resolve/create the DM session row and publish over
        # the chat-in:{chat_id} stream; the reply lands in the DM via the
        # owning gateway's sink (slack_origin lets it re-seat one). Same
        # posture as services.slack_bot.events._produce_slack_message.
        from app.chat.manager import (
            ConcurrencyCapHit as _CapHit,
        )
        from app.chat.manager import (
            produce_inbound_user_message,
            resolve_or_create_slack_session,
        )

        config = getattr(app.state, "chat_config", None)
        if config is None:
            logger.warning("/agnes thin-producer skipped: app.state.chat_config missing (chat init failed?)")
            await send_ephemeral(response_url, ":warning: Chat is not available right now. Try again later.")
            return
        try:
            session = resolve_or_create_slack_session(
                repo,
                config,
                user_email=user_email,
                surface=Surface.SLACK_DM,
                slack_channel_id=im_channel,
            )
        except _CapHit:
            await send_ephemeral(
                response_url,
                f"You're at your session limit ({config.concurrency_per_user}); run `/agnes-new` to free one.",
            )
            return
        await produce_inbound_user_message(
            repo,
            config,
            session.id,
            text,
            slack_origin={"channel": im_channel, "thread_ts": ""},
        )
        await send_ephemeral(response_url, "On it — Agnes will reply in your DM.")
        return

    try:
        session = await mgr.create_session(
            user_email=user_email,
            surface=Surface.SLACK_DM,
            slack_channel_id=im_channel,
        )
    except ConcurrencyCapHit:
        cap = mgr._config.concurrency_per_user
        await send_ephemeral(
            response_url,
            f"You're at your session limit ({cap}); run `/agnes-new` to free one.",
        )
        return

    # Multi-replica gate lift: this slash-command webhook can land on ANY
    # gateway replica. `_is_attached` below is process-local — when a
    # DIFFERENT live gateway owns this session, it reads False here and
    # the mgr.attach() call would fire ChatManager.attach's cross-gateway
    # TAKEOVER (destroy the owner's sandbox + respawn locally) for a plain
    # slash command. Same fix as services.slack_bot.events (wave-2F task
    # 7): forward the message via send_user_message (routes over the
    # chat-in:{chat_id} stream; slack_origin lets the owner re-establish
    # its SlackSinkBridge) and ack the response_url — the reply lands in
    # the DM via the owner's sink, not this single-shot response_url.
    from services.slack_bot.events import _owned_by_other_gateway

    if await _owned_by_other_gateway(session.id):
        await mgr.send_user_message(
            session.id,
            text,
            slack_origin={"channel": im_channel, "thread_ts": ""},
        )
        await send_ephemeral(response_url, "On it — Agnes will reply in your DM.")
        return

    # Attach a one-shot ephemeral sink only if no permanent sink (web/DM)
    # is already pumping — response_url is single-shot and the persistent
    # sink keeps streaming on web/DM.
    if not _is_attached(mgr, session.id):
        sink = EphemeralCommandSink(response_url=response_url)
        # _schedule (not bare create_task) retains a strong ref so the GC can't
        # cancel this in-flight task — it must survive the up-to-30s
        # wait_until_live poll below, far longer than the old 0.1s sleep.
        _schedule(mgr.attach(session.id, sink))
        # attach() spawns the sandbox (seconds) before registering the live
        # session and never returns, so wait (bounded) for it instead of a
        # fixed sleep that raced it and dropped the turn with SessionNotFound.
        if not await mgr.wait_until_live(session.id):
            await send_ephemeral(
                response_url,
                "Agnes is still starting up — please rerun `/agnes` in a few seconds.",
            )
            return
    await mgr.send_user_message(session.id, text)


async def _kill_locally_or_forward(app, chat_id: str, *, reason: str) -> None:
    """Kill via the local ChatManager when this process runs one; on an
    api-role replica (``app.state.chat_manager is None`` — only
    ``Role.GATEWAY`` builds a ChatManager) mirror ``ChatManager.kill``'s
    non-owner branch instead: revoke broker tickets locally and publish a
    ``control:kill`` for the owning gateway's consumer to execute (wave-2F
    final review F1). The caller archives the row either way; with no
    owner there is nothing live to tear down and the owner's reaper is the
    backstop, same as kill()'s own posture."""
    mgr = app.state.chat_manager
    if mgr is not None:
        try:
            await mgr.kill(chat_id, reason=reason)
        except Exception:
            logger.exception("kill failed for %s during %s", chat_id, reason)
        return
    from app.chat import inbound, routing
    from src.repositories import ticket_repo

    try:
        ticket_repo().revoke_session(chat_id)
    except Exception:
        logger.warning("broker ticket revocation failed for %s on producer-side kill (non-fatal)", chat_id)
    owner = await asyncio.to_thread(routing.owner_of, chat_id)
    if owner is not None and owner != routing.this_gateway_id():
        try:
            await inbound.publish_control(chat_id, "kill", reason=reason)
        except inbound.InboundPublishFailed:
            logger.warning(
                "cross-gateway kill for %s could not be published (owner %s); "
                "the owner's sandbox keeps running until its own reaper acts",
                chat_id,
                owner,
            )


async def _soft_archive_dm(app, slack_user_id: str) -> bool:
    """Resolve the caller's IM channel, kill + archive any live DM session.

    Returns True if a session was archived, False if none existed. Shared
    by /agnes-new and (Phase 3) the New-session button.
    """
    repo = app.state.chat_repo
    im_channel = await open_im(slack_user_id)
    if im_channel is None:
        return False
    existing = repo.get_slack_dm_session(im_channel)
    if existing is None:
        return False
    await _kill_locally_or_forward(app, existing.id, reason="agnes_new")
    repo.archive_session(existing.id)
    return True


async def _soft_archive_dm_for_button(app, owner_email: str, channel_id: str) -> None:
    """Soft-archive an owner's live DM session by email + channel_id.

    Used by the New-session button (interactivity phase) which already has
    the owner email resolved; unlike _soft_archive_dm it does not need to
    call open_im because the channel_id comes directly from the button value.
    No-op when there is no live/active session for that channel.
    """
    repo = app.state.chat_repo
    existing = repo.get_slack_dm_session(channel_id)
    # Defense-in-depth: never archive a session this owner doesn't own, even
    # if the caller already owner-gated. The button value is signature-verified
    # but the resolved session is the source of truth for ownership.
    if existing is None or existing.user_email != owner_email:
        return
    await _kill_locally_or_forward(app, existing.id, reason="new_session_button")
    repo.archive_session(existing.id)


async def _cmd_new(app, cmd: dict) -> None:
    slack_user_id = cmd.get("user_id", "")
    response_url = cmd.get("response_url", "")
    # Binding/grant are enforced on the next /agnes; /agnes-new is a no-op
    # for unbound users (no DM session can exist), so we skip the gate here.
    archived = await _soft_archive_dm(app, slack_user_id)
    if archived:
        await send_ephemeral(response_url, "Archived your Agnes session — your next `/agnes` starts fresh.")
    else:
        await send_ephemeral(response_url, "No active Agnes session to archive — your next `/agnes` starts fresh.")


async def _cmd_status(app, cmd: dict) -> None:
    repo = app.state.chat_repo
    mgr = app.state.chat_manager
    slack_user_id = cmd.get("user_id", "")
    response_url = cmd.get("response_url", "")

    user_email = lookup_user_email(repo, slack_user_id)
    if user_email is None:
        code = issue_verification_code(repo._conn, slack_user_id=slack_user_id)
        public_url = getattr(app.state, "public_url", "")
        await send_ephemeral(response_url, bind_prompt(public_url, code))
        return

    if mgr is not None:
        active = mgr.active_count_for_user(user_email)
        cap = mgr._config.concurrency_per_user
    else:
        # api-role replica: every live session of this user is hosted on
        # SOME gateway and holds a chat:{id} routing lease this process
        # can count — the same lease-derived predicate ChatManager's own
        # cap uses, minus the (empty-here) local-live term (wave-2F final
        # review F1).
        from app.chat.config import ChatConfig
        from app.chat.manager import count_foreign_lease_sessions

        config = getattr(app.state, "chat_config", None) or ChatConfig()
        try:
            active = count_foreign_lease_sessions(repo, user_email)
        except Exception:
            logger.warning("producer-side session count failed for %s", user_email, exc_info=True)
            active = 0
        cap = config.concurrency_per_user
    public_url = getattr(app.state, "public_url", "")
    chat_link = f"{public_url}/chat" if public_url else "/chat"
    await send_ephemeral(
        response_url,
        f"*Agnes status* — active sessions: *{active}* / {cap}\nOpen the full chat UI: {chat_link}",
    )


#: The command -> handler registry `dispatch_command` actually routes
#: through — a plain if/elif chain used to hand-duplicate these three
#: strings, which is exactly the kind of registry-vs-reality drift the
#: non-HTTP audit-posture ratchet (`src.audit_posture.BOT_COMMAND_POSTURE`)
#: is designed to catch elsewhere. Deriving `SLACK_COMMANDS` FROM this dict
#: (rather than listing the strings twice) means a new command added here
#: is automatically picked up by both the dispatcher and the ratchet.
SLACK_COMMAND_HANDLERS: dict[str, Any] = {
    "/agnes": _cmd_agnes,
    "/agnes-new": _cmd_new,
    "/agnes-status": _cmd_status,
}

#: Every slash command this bot recognizes — used by
#: `tests/test_audit_nonhttp_posture.py` to ratchet
#: `src.audit_posture.BOT_COMMAND_POSTURE` against reality.
SLACK_COMMANDS: frozenset[str] = frozenset(SLACK_COMMAND_HANDLERS)
