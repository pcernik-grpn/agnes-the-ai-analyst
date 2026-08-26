"""Notification-channel discovery: the in-chat long-run nudge, the checklist
sub-action, and the configuration gate.

Notifications were reachable ONLY from /me/profile — the page a user visits to
look a setting up *after* they know it exists. Nobody goes looking for
notifications before a run has finished while they were in another tab, so
discovery was effectively zero. Three changes, none of which adds a journey
step:

1. Agnes offers the channel herself, in-thread, once a turn passes
   LONG_RUN_MS — the only moment its value is legible.
2. The existing `use_anywhere` step carries a secondary "Choose where Agnes
   reaches you" link. Deliberately NOT a sixth step: a new journey column
   defaults FALSE, which would un-retire the completed rail card for every
   already-onboarded user and re-nag exactly the people who finished.
3. Both affordances — and the profile row itself — are gated on
   TELEGRAM_BOT_USERNAME. Ungated, the link instructions read "message
   @your-bot", and Agnes would be actively offering that dead end.

The JS assertions are static-source guards (no headless browser in CI), the
same contract style as test_rail_journey_chrome.py; the gate is asserted
against a real render.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")
CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
BASE_DS = Path("app/web/templates/base_ds.html")
PROFILE_HTML = Path("app/web/templates/profile.html")


def _onboarding_js() -> str:
    return ONBOARDING_JS.read_text(encoding="utf-8")


def _chat_js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


# --- 1. The in-chat nudge ---------------------------------------------------


def test_nudge_is_armed_after_the_onboarding_takeover_check():
    """A turn the gap resolver or an "add X" command took over never reaches the
    model, so it must not start a clock. Arming has to sit after that check and
    before the runner-ready wait (a slow runner IS worth being pinged about)."""
    js = _chat_js()
    takeover = js.index("if (await onboardingOnUserMessage(text, {}))")
    armed = js.index("onboardingNoteTurnStarted()")
    ready_wait = js.index("serverReadyPromise,")
    assert takeover < armed < ready_wait


def test_every_terminal_path_disarms_the_nudge():
    """done / error / cancelled are the terminal frames; the two pre-send bails
    (runner never ready, socket dropped) receive no frame at all, so they need
    their own disarm or the offer fires 45 s after a turn died at the door."""
    js = _chat_js()
    assert js.count("onboardingNoteTurnEnded()") >= 5
    for frame in ('case "done":', 'case "error":', 'case "cancelled":'):
        block = js.split(frame, 1)[1].split("break;", 1)[0]
        assert "onboardingNoteTurnEnded()" in block, f"{frame} must disarm the nudge"
    for bail in ("Runner did not become ready", "WebSocket dropped before runner"):
        block = js.split(bail, 1)[1].split("return;", 1)[0]
        assert "onboardingNoteTurnEnded()" in block, f"{bail!r} must disarm the nudge"


def test_nudge_is_gated_on_config_link_state_and_prior_answer():
    """noteTurnStarted must bail on every "nothing to ask" condition rather than
    arm a timer that renders an offer the user can't or shouldn't act on."""
    js = _onboarding_js()
    body = js.split("export function noteTurnStarted() {", 1)[1].split("\n}", 1)[0]
    # A resubmit mid-turn must not leave two timers armed.
    assert "noteTurnEnded()" in body
    assert "if (!chatMode) return" in body
    assert "nudgeOffered || nudgeDismissed()" in body
    assert "if (!telegramBot()) return" in body
    assert "if (notifyLinked === true) return" in body
    assert "setTimeout(scheduleNudge, LONG_RUN_MS)" in body


def test_nudge_waits_for_a_visible_tab():
    """Rendered into a hidden tab the card would be scrolled past unread — and
    the moment of recognition ("right, I did walk away") is exactly when the
    offer lands. So a timer that comes due on a backgrounded tab waits."""
    js = _onboarding_js()
    body = js.split("function scheduleNudge() {", 1)[1].split("\n}", 1)[0]
    assert "document.hidden" in body
    assert 'addEventListener("visibilitychange"' in body
    assert 'removeEventListener("visibilitychange"' in body


def test_a_deferred_offer_outlives_the_turn_and_changes_tense():
    """Someone who walked away and came back to a finished run is the BEST
    audience for this offer — so a deferral behind a hidden tab is not cancelled
    when the turn ends, it just has to stop saying "this is taking a while"."""
    js = _onboarding_js()
    ended = js.split("export function noteTurnEnded() {", 1)[1].split("\n}", 1)[0]
    assert "turnLive = false;" in ended
    # Only the un-fired timer is cancelled — the deferral is left standing.
    assert "nudgePending =" not in ended
    assert "removeEventListener" not in ended
    show = js.split("async function showNotifyNudge() {", 1)[1].split("\n}", 1)[0]
    # The tense is captured BEFORE the await — loadNotifyState can span the very
    # frame that ends the turn.
    assert show.index("const stillRunning = turnLive;") < show.index("await loadNotifyState()")
    assert "stillRunning" in show
    assert "That finished while you were away" in show
    assert "This one's taking a while" in show


def test_deferred_offers_do_not_stack_listeners():
    js = _onboarding_js()
    body = js.split("function scheduleNudge() {", 1)[1].split("\n}", 1)[0]
    assert "if (nudgePending) return;" in body
    assert "nudgePending = true;" in body
    assert "nudgePending = false;" in body


def test_nudge_rechecks_link_state_before_offering():
    """The page-load snapshot goes stale — the user may have linked in another
    tab since boot. Re-read, and bail if they're already reachable."""
    js = _onboarding_js()
    body = js.split("async function showNotifyNudge() {", 1)[1].split("\n}", 1)[0]
    assert "await loadNotifyState()" in body
    assert "if (notifyLinked === true) return" in body
    # One offer per page load whatever becomes of it.
    assert "if (nudgeOffered) return" in body
    assert "nudgeOffered = true" in body


def test_nudge_links_inline_and_points_at_the_canonical_home():
    """The card completes the link without leaving the thread (the profile is
    two navigations away mid-run), and still names the settings page as the
    place this lives — the nudge is a signpost, not a second home."""
    js = _onboarding_js()
    assert '"/api/telegram/verify"' in js
    assert "/me/profile#notifications" in js


def test_dismissal_is_remembered_and_survives_disabled_storage():
    js = _onboarding_js()
    assert 'NUDGE_DISMISSED_KEY = "agnes.notify.nudge.dismissed"' in js
    dismiss = js.split("function nudgeDismissed() {", 1)[1].split("\n}", 1)[0]
    # Private mode throws on localStorage access — being asked again next visit
    # beats an exception in the turn-start path.
    assert "try {" in dismiss and "catch" in dismiss
    remember = js.split("function rememberNudgeDismissed() {", 1)[1].split("\n}", 1)[0]
    assert 'setItem(NUDGE_DISMISSED_KEY, "1")' in remember


def test_bot_handle_is_escaped_into_the_card():
    """`window._agTelegramBot` is operator-controlled config interpolated into
    innerHTML — it goes through escapeHtml like every other value in this file."""
    js = _onboarding_js()
    assert "@${escapeHtml(bot)}" in js


def test_nudge_card_styles_use_tokens_only():
    css = CHAT_CSS.read_text(encoding="utf-8")
    assert ".cloud-chat-notifycard {" in css
    block = css.split("/* Long-run notification nudge", 1)[1].split("/* Journey step secondary destination", 1)[0]
    assert "#" not in block, "raw hex in the nudge card CSS — use --ds-* tokens"


# --- 2. The checklist sub-action -------------------------------------------


def test_journey_has_six_steps():
    """v116 added the sixth step, "Create your first agent".

    This guard used to say the opposite — no sixth step — for a specific
    reason: a new journey column defaults FALSE, so shipping one would drop
    every already-onboarded user from Complete ✓ back to 5/6 and bring the
    retired rail card back. That objection is answered rather than overruled,
    by the v116 backfill (see the next test), so the count moved instead.
    """
    js = _onboarding_js()
    keys = js.split("const STEP_KEYS = [", 1)[1].split("]", 1)[0]
    assert keys.count('"') == 12, "STEP_KEYS must hold six entries"
    assert "agent_created" in keys


def test_the_sixth_step_is_backfilled_so_nobody_regresses():
    """The reason the step count was allowed to move.

    Both ladders must mark the new flag TRUE for anyone whose journey is
    already closed, and for anyone who already owns an agent — otherwise the
    step is retroactively unearned and the checklist reopens for people who
    finished it. Guarded on both engines because a backfill present on one
    and missing on the other is the worse failure: the same user sees a
    different checklist depending on which backend their instance runs.
    """
    duck = Path("src/db.py").read_text(encoding="utf-8")
    step = duck.split("def _v117_to_v118", 1)[1].split("\ndef ", 1)[0]
    assert "SET agent_created = TRUE WHERE onboarded = TRUE" in step
    assert "owner_user_id FROM agents" in step

    alembic = Path("migrations/versions/0065_journey_agent_created_v118.py").read_text(encoding="utf-8")
    assert "SET agent_created = TRUE WHERE onboarded = TRUE" in alembic
    assert "owner_user_id FROM agents" in alembic


def test_use_anywhere_carries_the_notifications_sub_action():
    js = _onboarding_js()
    meta = js.split("  use_anywhere: {", 1)[1].split("\n  },", 1)[0]
    assert 'href: "/how-it-works#connect"' in meta, "primary destination unchanged"
    assert "sub: {" in meta
    assert '"/me/profile#notifications"' in meta
    assert "doneLabel:" in meta


def test_sub_action_is_a_sibling_anchor_not_a_nested_button():
    """A <button> may not contain a link, so the secondary destination is a
    sibling <a> — which also means it navigates natively, no handler needed."""
    js = _onboarding_js()
    body = js.split("function subActionHtml(step) {", 1)[1].split("\n}", 1)[0]
    assert "<a class=" in body
    assert "<button" not in body
    # A step with no sub-action renders nothing, and an instance with no bot
    # configured must not advertise the channel. Note there is deliberately NO
    # lock gate: a sub-action is a secondary destination, not a shortcut past
    # the step, so it renders unconditionally (see the note above the telegram
    # gate in chat_onboarding.js).
    assert 'if (!sub) return ""' in body
    assert "if (!telegramBot()) return" in body


def test_sub_action_state_comes_from_a_real_status_read():
    """`notifyLinked` starts null (unknown) and only claims "connected" on a
    true — an unknown state renders the invitation, never a wrong success."""
    js = _onboarding_js()
    assert "let notifyLinked = null;" in js
    assert '"/api/telegram/status"' in js
    assert "const linked = notifyLinked === true;" in js
    # Both boot paths hydrate it, so the sub-action is correct on /chat and on
    # every other rail page that mounts the panel standalone.
    for entry in ("export async function initChatOnboarding", "export async function mountJourneyPanel"):
        body = js.split(entry, 1)[1].split("\n}", 1)[0]
        assert "await loadNotifyState()" in body, f"{entry} must hydrate notify state"


def test_verifying_from_the_chat_card_repaints_the_checklist():
    """Two surfaces read one flag — they must not disagree after a link."""
    js = _onboarding_js()
    verify = js.split('card.querySelector("[data-notify-verify]")', 1)[1]
    assert "notifyLinked = true;" in verify
    assert "renderJourneyPanel();" in verify


# --- 3. The configuration gate --------------------------------------------


def test_base_stamps_the_bot_handle_defensively():
    """`| default('', true)` keeps pages whose context builder skips `config`
    from raising inside tojson — the studio pages have regressed on exactly
    that before."""
    html = BASE_DS.read_text(encoding="utf-8")
    assert "window._agTelegramBot =" in html
    assert "config.TELEGRAM_BOT_USERNAME | default('', true) | tojson" in html


def test_profile_no_longer_falls_back_to_a_placeholder_handle():
    """The `or 'your-bot'` fallback WAS the dead end — it rendered instructions to
    message an account that doesn't exist. The row is gated on real config now,
    so nothing needs a placeholder."""
    html = PROFILE_HTML.read_text(encoding="utf-8")
    assert "or 'your-bot'" not in html
    assert 'or "your-bot"' not in html


def test_notifications_panel_keeps_its_public_anchor():
    """Both new entry points deep-link /me/profile#notifications."""
    assert 'id="notifications"' in PROFILE_HTML.read_text(encoding="utf-8")


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def user_cookie(web_client):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    password = "NotifPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="notif1",
        email="notif@test.com",
        name="Notif",
        password_hash=PasswordHasher().hash(password),
    )
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "notif@test.com", "password": password})
    assert resp.status_code == 200, f"token failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


class TestProfileGate:
    @pytest.fixture(autouse=True)
    def _rail_profile(self, monkeypatch):
        """The notification-channels panel lives on the REDESIGNED profile
        (#896 moved it there off the retired rail dashboard); the default
        chrome serves the frozen pre-redesign profile, where Telegram linking
        stays on /dashboard exactly as before the redesign (spec 2026-08-07
        wave 2). The deep-link source (chat_onboarding.js) is rail-gated, so
        the anchor is only ever linked where it exists."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")

    def test_unconfigured_instance_offers_no_telegram_link(self, web_client, user_cookie, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
        resp = web_client.get("/me/profile", cookies=user_cookie)
        assert resp.status_code == 200
        body = resp.text
        # The panel stays (the macOS row lives there) and says why it's empty,
        # but there is no Link button and no verify block to dead-end into.
        assert 'id="notifications"' in body
        # The handler function stays in the page's inline script (harmless, and
        # shared with the linked case) — what must be gone is anything that can
        # CALL it, plus the verify block it would reveal.
        assert 'onclick="showTelegramVerify()"' not in body
        assert 'id="telegramVerify"' not in body
        assert "Telegram isn't configured on this instance yet" in body

    def test_configured_instance_offers_the_link_with_the_real_handle(self, web_client, user_cookie, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "acme_agnes_bot")
        resp = web_client.get("/me/profile", cookies=user_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert 'onclick="showTelegramVerify()"' in body
        assert 'id="telegramVerify"' in body
        assert "@acme_agnes_bot" in body
        assert "Telegram isn't configured" not in body

    def test_stamped_handle_follows_the_same_config(self, web_client, user_cookie, monkeypatch):
        """The chat nudge reads the stamp, so it has to track the env var — an
        empty stamp is what makes the nudge stay silent."""
        monkeypatch.delenv("TELEGRAM_BOT_USERNAME", raising=False)
        body = web_client.get("/me/profile", cookies=user_cookie).text
        assert 'window._agTelegramBot = ""' in body

        monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "acme_agnes_bot")
        body = web_client.get("/me/profile", cookies=user_cookie).text
        assert 'window._agTelegramBot = "acme_agnes_bot"' in body
