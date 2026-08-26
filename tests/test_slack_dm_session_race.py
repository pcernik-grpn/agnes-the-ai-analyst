"""Slack DM session-start hardening — two bugs surfaced by live E2E testing.

Bug B — ``SessionNotFound`` race: ``_handle_dm`` / ``_handle_mention`` / the
``/agnes`` slash command schedule ``ChatManager.attach`` fire-and-forget (it
spawns the sandbox, takes seconds, and never returns) and then used a fixed
``asyncio.sleep(0.1)`` before calling ``send_user_message``. The sleep raced
attach() registering the live session, so the user's first message after
binding was dropped with ``SessionNotFound``. Fix: ``mgr.wait_until_live`` polls
the registry with a real timeout.

Bug C — NULL ``slack_user_id`` crash: a "message" event without a ``user``
(edits/deletions and other subtypes) fell through to
``issue_verification_code(slack_user_id=None)``, tripping the
``slack_binding_codes.slack_user_id`` NOT NULL constraint. Fix: ``_handle_dm``
now early-returns when the event carries no user, mirroring ``_handle_mention``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace


def _make_manager():
    from app.chat.manager import ChatManager

    # wait_until_live only touches self._live + SessionState; the other deps
    # are never exercised, so None is fine for this unit.
    return ChatManager(provider=None, workdir_mgr=None, repo=None, config=None)


def _live_session(chat_id: str):
    from app.chat.manager import LiveSession
    from app.chat.types import SessionState

    return LiveSession(
        chat_id=chat_id,
        user_email="u@example.com",
        state=SessionState.ACTIVE,
        handle=object(),  # non-None — send_user_message requires it
        started_at=datetime.now(timezone.utc),
        last_activity=datetime.now(timezone.utc),
    )


def test_wait_until_live_returns_true_once_session_registers():
    """attach() registers the live session after a delay (it spawns a sandbox
    first); wait_until_live must block until then, not return early."""
    mgr = _make_manager()

    async def _run():
        async def _register_late():
            await asyncio.sleep(0.25)
            mgr._live["c1"] = _live_session("c1")

        asyncio.create_task(_register_late())
        return await mgr.wait_until_live("c1", timeout=5.0)

    assert asyncio.run(_run()) is True


def test_wait_until_live_returns_false_on_timeout():
    """If the session never registers (sandbox never comes up), the wait must
    give up after the timeout rather than hang forever."""
    mgr = _make_manager()
    assert asyncio.run(mgr.wait_until_live("never", timeout=0.3)) is False


def test_wait_until_live_ignores_dead_session():
    """A DEAD live entry must not count as ready — send_user_message would
    reject it, so wait_until_live should keep waiting (and here, time out)."""
    from app.chat.types import SessionState

    mgr = _make_manager()
    dead = _live_session("c1")
    dead.state = SessionState.DEAD
    mgr._live["c1"] = dead
    assert asyncio.run(mgr.wait_until_live("c1", timeout=0.3)) is False


def test_handle_dm_ignores_event_without_user(monkeypatch):
    """Bug C: a DM-shaped event with no ``user`` must be dropped before it
    reaches issue_verification_code — otherwise the NOT NULL constraint on
    slack_binding_codes.slack_user_id crashes the dispatch."""
    import services.slack_bot.events as ev

    issued: list = []
    monkeypatch.setattr(
        ev,
        "issue_verification_code",
        lambda *a, **k: issued.append(1) or "000000",
    )
    looked_up: list = []
    monkeypatch.setattr(
        ev,
        "lookup_user_email",
        lambda *a, **k: looked_up.append(1) or None,
    )

    app = SimpleNamespace(state=SimpleNamespace(chat_repo=SimpleNamespace(_conn=None)))
    # channel_type=im, no "user" key — e.g. a message edit/deletion subtype.
    event = {"channel_type": "im", "channel": "D1", "ts": "1.0"}

    asyncio.run(ev._handle_dm(app, event))

    assert issued == [], "issue_verification_code must not run for a user-less event"
    assert looked_up == [], "guard should return before the identity lookup"
