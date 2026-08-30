"""
Telegram Notification Bot - main entry point.

Runs two concurrent tasks:
1. Telegram polling - handles /start commands, generates verification codes
2. HTTP server on unix socket - accepts send requests from notify-runner

Usage:
    python -m telegram_bot.bot
"""

import asyncio
import contextlib
import grp
import json
import logging
import os
import sys

from aiohttp import web

from app.logging_config import setup_logging
from src.audit_helpers import log_safe

from . import config
from .dispatch import dispatch_desktop_notification
from .runner import run_user_script
from .sender import (
    answer_callback_query,
    get_updates,
    send_message,
    send_message_with_buttons,
    send_photo,
)
from .status import get_notification_status, get_script_buttons
from .storage import create_verification_code, get_chat_id, get_username_by_chat_id
from .test_report import generate_test_report

# Load instance branding for bot messages
try:
    from config.loader import load_instance_config, get_instance_value

    _bot_config = load_instance_config()
    _bot_instance_name = get_instance_value(_bot_config, "instance", "name", default="Data Analyst")
    _bot_server_hostname = get_instance_value(_bot_config, "server", "hostname", default="your-server")
    _bot_domain_suffix = get_instance_value(_bot_config, "telegram", "domain_suffix", default="")
except Exception:
    _bot_instance_name = "Data Analyst"
    _bot_server_hostname = "your-server"
    _bot_domain_suffix = ""

# Configure logging
setup_logging(__name__)
logger = logging.getLogger("notify-bot")


# --- Telegram Polling ---


def _telegram_email(username: str) -> str:
    """The Agnes email this bot username maps to — same derivation /whoami
    already used, factored out so the audit writer below can share it."""
    if _bot_domain_suffix:
        return f"{username}@{_bot_domain_suffix}"
    return username


def _audit_user_id(username: str) -> str:
    """``users.id`` for the Agnes account this bot username is derived
    from, or the computed email string when it doesn't resolve to one —
    audit only, never an authorization decision. Mirrors
    ``app.chat.audit._resolve_user_id``'s email-string-fallback convention:
    better a searchable identifier than a dropped row."""
    email = _telegram_email(username)
    try:
        from src.repositories import users_repo

        row = users_repo().get_by_email(email)
        return row["id"] if row else email
    except Exception:
        return email


async def _cmd_start(chat_id: int) -> None:
    username = get_username_by_chat_id(chat_id)
    if username:
        await send_message(
            chat_id,
            f"You are already linked as *{username}*.\nUse /help to see available commands.",
        )
        return

    code = create_verification_code(chat_id)
    await send_message(
        chat_id,
        f"Welcome to {_bot_instance_name} Notifications!\n\n"
        f"Your verification code: *{code}*\n\n"
        f"Enter this code on your dashboard at {_bot_server_hostname}\n"
        f"Code expires in 10 minutes.",
    )
    logger.info(f"Sent verification code to chat_id {chat_id}")


async def _cmd_whoami(chat_id: int) -> None:
    username = get_username_by_chat_id(chat_id)
    if username:
        # Username is derived from email
        if _bot_domain_suffix:
            email = f"{username}@{_bot_domain_suffix}"
        else:
            email = username
        await send_message(
            chat_id,
            f"*{username}*\n{email}",
        )
    else:
        await send_message(
            chat_id,
            "No linked account. Use /start to link.",
            parse_mode="",
        )


async def _cmd_status(chat_id: int) -> None:
    username = get_username_by_chat_id(chat_id)
    if not username:
        await send_message(
            chat_id,
            "Link your account first using /start and the dashboard.",
            parse_mode="",
        )
        return

    status_text = get_notification_status(username)
    buttons = get_script_buttons(username)
    if buttons:
        await send_message_with_buttons(chat_id, status_text, buttons)
    else:
        await send_message(chat_id, status_text)


async def _cmd_test(chat_id: int) -> None:
    username = get_username_by_chat_id(chat_id)
    if not username:
        await send_message(
            chat_id,
            "Link your account first using /start and the dashboard.",
            parse_mode="",
        )
        return

    await send_message(chat_id, "Generating test report...", parse_mode="")
    try:
        image_path, caption = generate_test_report(username)
        await send_photo(chat_id, image_path, caption)
        # Cleanup temp file
        os.unlink(image_path)
        logger.info(f"Sent test report to {username}")
    except Exception:
        logger.exception(f"Failed to generate test report for {username}")
        await send_message(chat_id, "Failed to generate report. Check server logs.", parse_mode="")


async def _cmd_help(chat_id: int) -> None:
    await send_message(
        chat_id,
        f"*{_bot_instance_name} Bot*\n\n"
        "/start - Link your Telegram account\n"
        "/whoami - Show your username and chat ID\n"
        "/status - List your notification scripts\n"
        "/test - Send a demo report\n"
        "/help - Show this help",
    )


#: The command -> handler registry `handle_message` actually routes
#: through — a plain if/elif chain used to hand-duplicate these five
#: strings, which is exactly the kind of registry-vs-reality drift the
#: non-HTTP audit-posture ratchet (`src.audit_posture.BOT_COMMAND_POSTURE`)
#: is designed to catch elsewhere. Deriving `TELEGRAM_COMMANDS` FROM this
#: dict (rather than listing the strings twice) means a new command added
#: here is automatically picked up by both the dispatcher and the ratchet.
TELEGRAM_COMMAND_HANDLERS = {
    "/start": _cmd_start,
    "/whoami": _cmd_whoami,
    "/status": _cmd_status,
    "/test": _cmd_test,
    "/help": _cmd_help,
}

#: Every text command this bot recognizes — used by
#: `tests/test_audit_nonhttp_posture.py` to ratchet
#: `src.audit_posture.BOT_COMMAND_POSTURE` against reality.
TELEGRAM_COMMANDS: frozenset[str] = frozenset(TELEGRAM_COMMAND_HANDLERS)


async def handle_message(message: dict) -> None:
    """Handle an incoming Telegram message."""
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id:
        return

    # F2d (audit-full-coverage plan, Task 6): a read event — the bot received
    # and processed a message. Only for a linked account (nothing to attribute
    # otherwise); the command name only, never any free-text the user typed.
    linked_username = get_username_by_chat_id(chat_id)
    if linked_username:
        log_safe(
            user_id=_audit_user_id(linked_username),
            action="telegram.message",
            resource=f"chat:{chat_id}",
            params={"command": text.split(None, 1)[0] if text else ""},
            client_kind="telegram",
        )

    handler = TELEGRAM_COMMAND_HANDLERS.get(text)
    if handler is None:
        await send_message(
            chat_id,
            "Unknown command. Type /help for available commands.",
            parse_mode="",
        )
        return
    await handler(chat_id)


async def handle_callback_query(callback_query: dict) -> None:
    """Handle inline keyboard button press."""
    callback_id = callback_query.get("id")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    data = callback_query.get("data", "")

    if not chat_id or not data:
        return

    # Parse callback data: "run:{script_name}"
    if not data.startswith("run:"):
        await answer_callback_query(callback_id, "Unknown action")
        return

    script_name = data[4:]  # strip "run:"
    username = get_username_by_chat_id(chat_id)
    if not username:
        await answer_callback_query(callback_id, "Account not linked")
        return

    await answer_callback_query(callback_id, f"Running {script_name}...")
    await send_message(chat_id, f"Running `{script_name}`...", parse_mode="Markdown")

    logger.info(f"On-demand run: {script_name} for {username}")
    output = await asyncio.to_thread(run_user_script, username, script_name)

    # F2d (audit-full-coverage plan, Task 6): the sudo path — a Telegram
    # button press runs a script as an arbitrary OS user via `sudo -u`
    # (services/telegram_bot/runner.py). Highest-value audit row in this
    # task: who triggered it, which script, which OS user, and whether the
    # subprocess actually succeeded. This is the real action
    # `src.audit_posture.BOT_COMMAND_POSTURE["telegram:callback:run_script"]`
    # declares — must never be exempt.
    log_safe(
        user_id=_audit_user_id(username),
        action="telegram.script_run",
        resource=f"script:{script_name}",
        params={"script": script_name, "os_user": username},
        result="success" if output is not None else "error",
        client_kind="telegram",
    )

    if output is None:
        await send_message(chat_id, f"`{script_name}` failed. Check server logs.", parse_mode="Markdown")
        return

    if not output.get("notify", False):
        await send_message(chat_id, f"`{script_name}` returned notify=false (no alert).", parse_mode="Markdown")
        return

    # Format and send the notification
    parts = []
    title = output.get("title", "")
    message_text = output.get("message", "")
    if title:
        parts.append(f"*{title}*")
    if message_text:
        parts.append(message_text)
    text = "\n".join(parts)

    image_path = output.get("image_path", "")
    if image_path and os.path.isfile(image_path):
        await send_photo(chat_id, image_path, caption=text)
    elif text:
        await send_message(chat_id, text)
    else:
        await send_message(chat_id, f"`{script_name}` produced no output.", parse_mode="Markdown")

    # Also publish a desktop-app notification (coordination pub/sub — wave-2F task 6)
    await asyncio.to_thread(dispatch_desktop_notification, username, output, script_name)


async def polling_loop() -> None:
    """Long-poll Telegram for updates."""
    logger.info("Starting Telegram polling loop")
    offset = 0

    while True:
        try:
            updates, offset = await get_updates(offset)
            for update in updates:
                message = update.get("message")
                if message:
                    await handle_message(message)
                callback_query = update.get("callback_query")
                if callback_query:
                    await handle_callback_query(callback_query)
        except Exception:
            logger.exception("Polling loop error")
            await asyncio.sleep(config.POLL_ERROR_RETRY_SECONDS)


# --- Leader lease around polling (wave-2C task 3) -------------------------
#
# This service is a standalone process (its own systemd unit / container),
# potentially run with more than one replica for HA. Telegram's getUpdates
# long-poll advances a single global `offset` cursor server-side — two
# replicas polling concurrently would race on acking/dropping each other's
# updates, so at most one replica may run `polling_loop` at a time.
#
# `app.coordination.leases.run_with_lease` needs `app.coordination`'s config
# plumbing (instance.yaml / env) to resolve the coordination backend. This
# module already imports `from app.logging_config import setup_logging`
# above, confirming the `app` package is importable in this process's
# context; `app.coordination.factory.resolve_backend_name()` reads
# `AGNES_COORDINATION_BACKEND` (falling back to `instance.yaml` via
# `app.instance_config.get_value`, which fails soft to defaults if no
# instance.yaml is reachable from this process) — so this works whether or
# not this standalone service has its own instance.yaml wired up, via the
# already-documented env override.
_poll_task: "asyncio.Task | None" = None


async def _start_polling() -> None:
    """run_with_lease `start` callback: launch `polling_loop` as a
    background task. It must be a cancellable task (not just a cooperative
    stop-flag check) because `get_updates` long-polls Telegram for up to
    `POLL_TIMEOUT_SECONDS`; only cancellation interrupts that promptly."""
    global _poll_task
    _poll_task = asyncio.create_task(polling_loop(), name="telegram-polling")


async def _stop_polling() -> None:
    """run_with_lease `stop` callback: cancel the polling task and wait for
    it to unwind.

    FLUSHALL story: if the coordination backend loses its state (Redis
    FLUSHALL/restart, or an outage outliving one ttl_s), this replica's
    lease renew fails -> `_stop_polling` cancels `polling_loop` -> the lease
    loop re-enters acquire-polling -> some replica (maybe this one)
    re-acquires and resumes polling within one `ttl_s`. Under the default
    `memory` backend (single-replica deployment) this never fires — the
    lease is process-local and always immediately acquired.
    """
    global _poll_task
    task = _poll_task
    _poll_task = None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def run_polling_with_lease() -> None:
    """Entry point used by `main()` in place of a bare `await polling_loop()`
    — wraps the long-poll loop in the `telegram-poll` leader lease so only
    one replica of this service polls Telegram at a time. Never returns
    (mirrors `polling_loop`'s own never-returns contract)."""
    from app.coordination.leases import default_holder_id, run_with_lease

    await run_with_lease(
        "telegram-poll",
        default_holder_id(),
        ttl_s=15,
        start=_start_polling,
        stop=_stop_polling,
    )


# --- HTTP Send API (unix socket) ---


async def handle_send(request: web.Request) -> web.Response:
    """Handle POST /send - send text message."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("user")
    text = data.get("text")
    parse_mode = data.get("parse_mode", "Markdown")

    if not username or not text:
        return web.json_response({"error": "Missing required fields: user, text"}, status=400)

    chat_id = get_chat_id(username)
    if not chat_id:
        return web.json_response({"error": f"User '{username}' has no linked Telegram"}, status=404)

    success = await send_message(chat_id, text, parse_mode)
    if success:
        return web.json_response({"ok": True})
    return web.json_response({"error": "Failed to send message"}, status=502)


async def handle_send_photo(request: web.Request) -> web.Response:
    """Handle POST /send_photo - send image with optional caption."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("user")
    photo_path = data.get("photo_path")
    caption = data.get("caption", "")
    parse_mode = data.get("parse_mode", "Markdown")

    if not username or not photo_path:
        return web.json_response({"error": "Missing required fields: user, photo_path"}, status=400)

    # Security audit F15: constrain photo_path to the allowlisted base dirs via
    # realpath containment so a caller on the socket cannot exfiltrate arbitrary
    # files the bot process can read (e.g. /etc/passwd, other users' data).
    # Realpath resolves symlinks first, so a symlink inside an allowed dir that
    # points elsewhere is still rejected.
    resolved = os.path.realpath(photo_path)
    allowed = False
    for base_dir in config.PHOTO_BASE_DIRS:
        base = os.path.realpath(base_dir)
        if os.path.commonpath([base, resolved]) == base:
            allowed = True
            break
    if not allowed:
        logger.warning("send_photo rejected out-of-bounds path: %r", photo_path)
        return web.json_response({"error": "Photo path not permitted"}, status=403)

    if not os.path.isfile(resolved):
        return web.json_response({"error": f"Photo file not found: {photo_path}"}, status=400)
    photo_path = resolved

    chat_id = get_chat_id(username)
    if not chat_id:
        return web.json_response({"error": f"User '{username}' has no linked Telegram"}, status=404)

    success = await send_photo(chat_id, photo_path, caption, parse_mode)
    if success:
        return web.json_response({"ok": True})
    return web.json_response({"error": "Failed to send photo"}, status=502)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app.router.add_post("/send", handle_send)
    app.router.add_post("/send_photo", handle_send_photo)
    app.router.add_get("/health", handle_health)
    return app


async def start_http_server() -> None:
    """Start HTTP server on unix socket."""
    # Remove stale socket
    socket_path = config.SOCKET_PATH
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()

    # Set socket permissions: group-writable for dataread group (analysts send via notify-runner)
    # Socket lives in /run/notify-bot/ (systemd RuntimeDirectory, mode 0755)
    os.chmod(socket_path, 0o660)
    # Change group ownership to dataread (deploy user is member of dataread group)
    os.chown(socket_path, -1, grp.getgrnam("dataread").gr_gid)

    logger.info(f"HTTP server listening on {socket_path}")


# --- Main ---


async def main() -> None:
    """Run bot polling and HTTP server concurrently."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    # Ensure notifications directory exists
    os.makedirs(config.NOTIFICATIONS_DIR, exist_ok=True)

    await start_http_server()
    await run_polling_with_lease()


if __name__ == "__main__":
    asyncio.run(main())
