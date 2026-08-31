"""``?session=<id>`` stays in sync with the open conversation (issue #1914).

The restore path itself already existed: the server threads
``?session=<id>`` into ``data-initial-session`` (chat.html) and
``_maybeOpenInitialSession`` opens it on boot. What was missing is the other
direction — the in-page chat never wrote the id back into the URL, so a
refresh always arrived at a bare ``/chat`` and opened a new "Untitled chat".

No headless browser in CI, so — same contract style as
test_chat_pin_conversations.py / test_chat_input_history_recall.py — these
are static-source guards against app/web/static/js/chat.js rather than a
real browser-driven test.
"""

from __future__ import annotations

from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")


def _chat_js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def _slice(js: str, start_marker: str, end_marker: str) -> str:
    start = js.index(start_marker)
    end = js.index(end_marker, start)
    return js[start:end]


def test_sync_helper_exists_and_uses_replace_state_not_push_state():
    js = _chat_js()
    body = _slice(js, "function _syncSessionUrl(chatId) {", "// --- Composer agent picker")
    assert "new URL(window.location.href)" in body
    assert "u.searchParams.set(" in body and '"session"' in body
    assert "u.searchParams.delete(" in body
    assert "window.history.replaceState(" in body
    assert "pushState" not in body, "must not push a history entry per session open"


def test_sync_helper_clears_the_param_on_a_falsy_id():
    """A falsy chatId must delete the param, not set it to a stringified
    'null'/'undefined' — the bug this whole feature exists to avoid on the
    reverse path (a param that lingers past its session)."""
    js = _chat_js()
    body = _slice(js, "function _syncSessionUrl(chatId) {", "// --- Composer agent picker")
    assert "if (chatId) {" in body
    delete_idx = body.index("u.searchParams.delete(")
    set_idx = body.index("u.searchParams.set(")
    else_idx = body.index("} else {")
    # the delete branch is the else of the set branch
    assert set_idx < else_idx < delete_idx


def test_mark_conversation_started_syncs_the_url():
    """The first turn of a brand-new chat, and opening an existing
    conversation that already has history (loadAndRenderHistory calls this
    too — see test below), both funnel through here."""
    js = _chat_js()
    body = _slice(js, "function _markConversationStarted() {", "function _markConversationNotStarted() {")
    assert "_sessionHasTurns = true;" in body
    assert "_syncSessionUrl(currentChatId)" in body


def test_load_and_render_history_reaching_turns_goes_through_mark_started():
    """An existing conversation opened with persisted history (sidebar,
    history list, palette, or a deep link) must end up with its id in the
    URL — this file doesn't re-derive that; it pins that the history>0
    branch still calls `_markConversationStarted`, whose own URL sync this
    test file covers directly above."""
    js = _chat_js()
    body = _slice(
        js, "async function loadAndRenderHistory(chatId) {", "async function openSession(chatId, wsUrlOverride) {"
    )
    assert "_markConversationStarted();" in body


def test_open_session_writes_the_url_from_session_has_turns():
    js = _chat_js()
    body = _slice(js, "async function openSession(chatId, wsUrlOverride) {", "function chatErrorCopy(raw, kind) {")
    assert "if (_switchingSession) _sessionHasTurns = false;" in body
    sync_call = "_syncSessionUrl(_sessionHasTurns ? chatId : null);"
    assert sync_call in body
    # Must run AFTER the switch reset (so a switch to an unproven session
    # starts cleared) and BEFORE the history fetch settles (so an
    # already-known-started session gets its URL immediately, without
    # waiting on the network).
    reset_idx = body.index("if (_switchingSession) _sessionHasTurns = false;")
    sync_idx = body.index(sync_call)
    fetch_idx = body.index("await loadAndRenderHistory(chatId);")
    assert reset_idx < sync_idx < fetch_idx


def test_delete_session_clears_the_url_when_deleting_the_open_conversation():
    js = _chat_js()
    body = _slice(js, "async function deleteSession(chatId) {", "function markActiveSidebar(chatId) {")
    assert "currentChatId === chatId" in body
    reset = body[body.index("currentChatId === chatId") :]
    assert "currentChatId = null;" in reset
    assert "_syncSessionUrl(null);" in reset


def test_new_chat_failure_path_clears_the_url():
    """``#new-chat``'s click handler resets every session pointer on a failed
    ``newChat()`` — the URL is one of them, or a refresh after a failed "New
    chat" click could re-open a stale prior session id."""
    js = _chat_js()
    body = _slice(js, '$("new-chat")?.addEventListener("click"', '$("chat-form").onsubmit')
    assert "currentChatId = null;" in body
    assert "_syncSessionUrl(null);" in body


def test_no_popstate_handling_was_added():
    """replaceState-only means no popstate listener is needed; adding one would
    silently imply pushState is happening somewhere, which contradicts the
    'no back-button history entries' design."""
    js = _chat_js()
    assert 'addEventListener("popstate"' not in js
