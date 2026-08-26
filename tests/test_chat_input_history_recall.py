"""ArrowUp/ArrowDown recall of this chat's own sent messages in the composer.

No headless browser in CI, so — same contract style as
test_chat_notify_nudge.py / test_rail_journey_chrome.py — these are
static-source guards against app/web/static/js/chat.js rather than a real
keyboard-driven DOM test.
"""

from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")


def _chat_js() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def test_prompt_history_state_declared():
    js = _chat_js()
    assert "let _promptHistory = [];" in js
    assert "let _historyPos = 0;" in js
    assert 'let _historyDraft = "";' in js
    assert "let _historyBrowsing = false;" in js


def test_load_and_render_history_seeds_prompt_history_from_persisted_messages():
    """Recall must cover messages from before this page load too — a
    reloaded or reconnected (full_refresh) session should still let ArrowUp
    reach what was typed earlier, not just what was sent this render."""
    js = _chat_js()
    start = js.index("async function loadAndRenderHistory")
    end = js.index("async function openSession")
    body = js[start:end]
    assert "_promptHistory = [];" in body
    assert "_promptHistory.push(lastUserText);" in body
    assert "_historyPos = _promptHistory.length;" in body
    assert "_historyDraft = " in body
    assert "_historyBrowsing = false;" in body


def test_history_reset_happens_before_the_fetch_that_can_fail():
    """A failed history fetch (network hiccup while switching chats) must not
    leave ArrowUp/ArrowDown browsing the PREVIOUS conversation's prompts under
    the new chatId — the reset has to happen before the `await api(...)` call
    that the `catch` block can bail out of, not after it succeeds."""
    js = _chat_js()
    start = js.index("async function loadAndRenderHistory")
    end = js.index("async function openSession")
    body = js[start:end]
    reset = body.index("_promptHistory = [];")
    fetch = body.index("await api(`/api/chat/sessions/${chatId}/messages`)")
    catch_return = body.index("setStatus(`Could not load history")
    assert reset < fetch < catch_return


def test_history_seeding_skips_co_drive_peers_own_messages_only():
    """Recall is 'this conversation's OWN sent messages' — a co-drive peer's
    prompt (m.sender_email set and not ours) must not surface under my
    ArrowUp, matching submitUserMessage's live-send path, which only ever
    appends the local sender's own text."""
    js = _chat_js()
    start = js.index("async function loadAndRenderHistory")
    end = js.index("async function openSession")
    body = js[start:end]
    guard = body.index("!m.sender_email || m.sender_email === currentUserEmail")
    push = body.index("_promptHistory.push(lastUserText);")
    assert guard < push


def test_history_seeding_dedupes_consecutive_identical_prompts():
    """The live-send path collapses a repeat of the immediately preceding
    prompt (see test_submit_user_message_appends_sent_prompt_to_history).
    Seeding from persisted history must apply the same dedup, or a
    reload/full_refresh re-materializes duplicates the live session had
    already collapsed, producing a different recall stack than before."""
    js = _chat_js()
    start = js.index("async function loadAndRenderHistory")
    end = js.index("async function openSession")
    body = js[start:end]
    assert "_promptHistory[_promptHistory.length - 1] !== lastUserText" in body


def test_submit_user_message_appends_sent_prompt_to_history():
    js = _chat_js()
    start = js.index("async function submitUserMessage")
    end = js.index("/** Resize the composer textarea")
    body = js[start:end]
    assert "lastUserText = text;" in body
    assert "_promptHistory.push(text);" in body
    assert "_historyPos = _promptHistory.length;" in body
    assert "_historyBrowsing = false;" in body
    # Back-to-back identical sends shouldn't create duplicate history entries.
    assert "_promptHistory[_promptHistory.length - 1] !== text" in body


def test_composer_keydown_recall_sits_between_slash_menu_and_enter_submit():
    """The slash-command menu still claims Up/Down first when open; Enter-to-
    submit stays the last branch. Recall must sit in between so it only fires
    once the slash-menu block has already returned."""
    js = _chat_js()
    start = js.index('$("chat-input").addEventListener("keydown"')
    end = js.index('$("chat-input").addEventListener("input"')
    handler = js[start:end]
    slash_end = handler.index("_slashMenu_selectCurrent();")
    recall_up = handler.index('e.key === "ArrowUp" && (_historyBrowsing || atStart)')
    recall_down = handler.index('e.key === "ArrowDown" && _historyBrowsing')
    enter_submit = handler.index('e.key === "Enter" && !e.shiftKey && !e.isComposing')
    assert slash_end < recall_up < recall_down < enter_submit


def test_arrow_up_only_starts_browsing_from_caret_start():
    """First ArrowUp on a multi-line draft should move the caret up a line
    like normal editing; only once already at position 0 (or already
    browsing) does it start recalling history."""
    js = _chat_js()
    assert "const atStart = ta.selectionStart === 0 && ta.selectionEnd === 0;" in js
    assert "(_historyBrowsing || atStart) && _historyPos > 0" in js


def test_manual_edit_resets_history_browsing():
    """Typing over a recalled entry must exit browsing mode — otherwise the
    next ArrowUp would resume mid-history against a value that no longer
    matches what's stored there."""
    js = _chat_js()
    start = js.index('$("chat-input").addEventListener("input"')
    end = js.index("_onSlashInputChanged();", start) + len("_onSlashInputChanged();")
    block = js[start:end]
    assert "_historyPos = _promptHistory.length;" in block
    assert "_historyBrowsing = false;" in block
    assert "autosizeComposer();" in block


def test_the_recall_filter_has_a_payload_field_to_read():
    """The two halves of the co-drive filter, pinned together.

    `chat.js` filters the recall stack on `m.sender_email`, over rows from
    `GET /api/chat/sessions/{id}/messages`. That endpoint did not serialize the
    field, so `!m.sender_email` was unconditionally true and the filter was
    dead code — a peer's prompts still surfaced under the owner's ArrowUp after
    any reload or `full_refresh`, and the review round that asked for the
    filter was resolved believing it worked.

    A source-grep test cannot tell "this code exists" from "this code does
    anything", which is exactly how that shipped green. This pins the server
    half so the client half cannot go back to reading a field nobody sends;
    `tests/test_chat_api.py::test_get_messages_exposes_sender_email` asserts
    the payload itself.
    """
    from pathlib import Path

    js = Path("app/web/static/js/chat.js").read_text()
    api = Path("app/api/chat.py").read_text()
    assert "m.sender_email" in js, "the client filter is gone -- drop this guard with it"
    assert '"sender_email": m.sender_email' in api, (
        "chat.js filters on m.sender_email; the messages endpoint must send it"
    )


def test_arrow_up_leaves_the_caret_at_the_end():
    """Shell history — the model named in this feature's own comments — puts
    the caret at the end in both directions. ArrowUp used to leave it at 0,
    the one position a reader recalling a prompt to edit its tail has to move
    away from, while ArrowDown already used the end."""
    from pathlib import Path

    js = Path("app/web/static/js/chat.js").read_text()
    block = js[js.index('if (e.key === "ArrowUp" && (_historyBrowsing || atStart)') :]
    block = block[: block.index("else if")]
    assert "setSelectionRange(0, 0)" not in block, "ArrowUp must not park the caret at position 0"
    assert "ta.value.length" in block
