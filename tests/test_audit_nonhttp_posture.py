"""Non-HTTP surfaces join the declared-action audit ratchet (Wave 2 —
audit-coverage plan, non-HTTP posture task).

``POSTURE``/``READ_POSTURE``/``WS_POSTURE`` (see ``tests/test_audit_route_
posture.py`` and ``tests/test_audit_read_posture.py``) only ever inspect
``app.routes`` — a new worker job kind, MCP foundation tool, or bot slash-
command/callback can ship with zero audit posture and nothing fails, because
none of those three surfaces is an ASGI route. ``JOB_POSTURE``,
``MCP_TOOL_POSTURE``, and ``BOT_COMMAND_POSTURE`` close that gap: every
entry in the surface's own enumerable registry must declare a cataloged
action or ``"exempt:<reason>"`` drawn from the same closed ``EXEMPT_REASONS``
vocabulary, and vice versa (a stale entry fails too).
"""

from __future__ import annotations

import pytest

from src.audit_events import is_cataloged
from src.audit_posture import (
    BOT_COMMAND_POSTURE,
    EXEMPT_REASONS,
    JOB_POSTURE,
    MCP_TOOL_POSTURE,
    declared_nonhttp_action,
)


# ---------------------------------------------------------------------------
# Worker job kinds
# ---------------------------------------------------------------------------


@pytest.fixture
def all_job_kinds():
    """The full universe of registerable job kinds, including the
    conditionally-registered ``agent_response`` — mirrors
    ``tests/test_worker_kinds.py``'s ``clean_job_kinds_registry`` fixture:
    the registry is a process-wide module dict, so isolate + reset it, and
    force a live chat-manager sentinel so ``register_all_kinds()`` registers
    ``agent_response`` too (see that function's docstring)."""
    from app.chat.manager import set_current_chat_manager
    from app.worker.kinds import register_all_kinds
    from app.worker.registry import JOB_KINDS

    JOB_KINDS.clear()
    set_current_chat_manager(object())  # any non-None sentinel — only identity matters
    try:
        register_all_kinds()
        yield dict(JOB_KINDS)
    finally:
        JOB_KINDS.clear()
        set_current_chat_manager(None)


def test_every_job_kind_declares_posture(all_job_kinds):
    kinds = set(all_job_kinds)
    undeclared = sorted(kinds - JOB_POSTURE.keys())
    stale = sorted(JOB_POSTURE.keys() - kinds)
    assert not undeclared, (
        "New worker job kinds must declare audit posture in src/audit_posture.py "
        f"(JOB_POSTURE — a cataloged action, or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune JOB_POSTURE, kinds gone from app.worker.registry.JOB_KINDS: {stale}"


# ---------------------------------------------------------------------------
# MCP foundation tools
# ---------------------------------------------------------------------------


def test_every_foundation_tool_declares_posture():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    tools = set(FOUNDATION_TOOL_NAMES)
    undeclared = sorted(tools - MCP_TOOL_POSTURE.keys())
    stale = sorted(MCP_TOOL_POSTURE.keys() - tools)
    assert not undeclared, (
        "New MCP foundation tools must declare audit posture in src/audit_posture.py "
        f"(MCP_TOOL_POSTURE — a cataloged action, or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune MCP_TOOL_POSTURE, tools gone from FOUNDATION_TOOL_NAMES: {stale}"


# ---------------------------------------------------------------------------
# Bot commands (Slack + Telegram)
# ---------------------------------------------------------------------------


def _bot_registry_keys() -> set[str]:
    from services.slack_bot.commands import SLACK_COMMANDS
    from services.telegram_bot.bot import TELEGRAM_COMMANDS

    keys = {f"slack:{c}" for c in SLACK_COMMANDS}
    keys |= {f"telegram:{c}" for c in TELEGRAM_COMMANDS}
    # The Telegram inline "run script" button callback has no text-command
    # string of its own — its own module has nothing to enumerate it against
    # (there is only the one callback kind, `run:{script_name}`), so it is
    # named directly here rather than via a registry.
    keys.add("telegram:callback:run_script")
    return keys


def test_every_bot_command_declares_posture():
    known = _bot_registry_keys()
    undeclared = sorted(known - BOT_COMMAND_POSTURE.keys())
    stale = sorted(BOT_COMMAND_POSTURE.keys() - known)
    assert not undeclared, (
        "New Slack/Telegram bot commands must declare audit posture in "
        f"src/audit_posture.py (BOT_COMMAND_POSTURE — a cataloged action, "
        f"or 'exempt:<reason>'): {undeclared}"
    )
    assert not stale, f"Prune BOT_COMMAND_POSTURE, commands gone from the bot modules: {stale}"


# ---------------------------------------------------------------------------
# Shared vocabulary + honesty checks
# ---------------------------------------------------------------------------


def test_exempt_reasons_come_from_the_closed_vocabulary():
    for source in (JOB_POSTURE, MCP_TOOL_POSTURE, BOT_COMMAND_POSTURE):
        for key, v in source.items():
            if v.startswith("exempt:"):
                assert v.split(":", 1)[1] in EXEMPT_REASONS, (key, v)
            else:
                assert is_cataloged(v), (key, v)


def test_declared_nonhttp_action_resolves_and_skips_exempt():
    key = next(k for k, v in MCP_TOOL_POSTURE.items() if not v.startswith("exempt:"))
    assert declared_nonhttp_action(MCP_TOOL_POSTURE, key) == MCP_TOOL_POSTURE[key]

    ex = next(k for k, v in MCP_TOOL_POSTURE.items() if v.startswith("exempt:"))
    assert declared_nonhttp_action(MCP_TOOL_POSTURE, ex) is None

    assert declared_nonhttp_action(MCP_TOOL_POSTURE, "no-such-tool") is None


def test_telegram_sudo_script_run_callback_is_not_exempt():
    """The one genuinely sensitive row on this whole surface — a Telegram
    button press runs a script as an arbitrary OS user via `sudo -u`
    (services/telegram_bot/runner.py). Pinned the way
    `test_sensitive_reads_are_not_exempt` pins its own category in
    tests/test_audit_read_posture.py: this key must always resolve to a
    real, cataloged action, never `exempt:<reason>`."""
    value = BOT_COMMAND_POSTURE["telegram:callback:run_script"]
    assert not value.startswith("exempt:"), value
    assert value == "telegram.script_run"
    assert is_cataloged(value)
