"""Web chat thumbs feedback — static checks on chat.js/chat.css (LLM
observability design §3.5).

There is no jsdom dependency in this repo, so — following the precedent in
``tests/test_chat_facts_rendering_ui.py`` — this file pins the shipped
source text rather than executing DOM-building functions: the article that
gets rated carries ``dataset.turnId`` (stamped from the live frame's
``turn_id`` before ``attachMessageActions`` runs, and from a reloaded
message's ``turn_id`` in ``renderMessage``), the feedback controls only
render for a completed assistant bubble that has one, the POST goes to the
session's own ``.../feedback`` path, and the new CSS carries only
``--ds-*``/system tokens, never a raw hex literal.
"""

from __future__ import annotations

import re
from pathlib import Path

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _slice(js: str, start_marker: str, end_marker: str) -> str:
    start = js.index(start_marker)
    end = js.index(end_marker, start)
    return js[start:end]


# ---------------------------------------------------------------------------
# chat.js
# ---------------------------------------------------------------------------


def test_finalize_stamps_turn_id_before_attaching_actions():
    """Both direct ``attachMessageActions`` call sites inside
    ``finalizeAssistantMessage`` stamp ``dataset.turnId`` from the frame
    first — thumbs need the id in place before the actions row renders."""
    js = _read(CHAT_JS)
    fn = _slice(js, "function finalizeAssistantMessage(frame)", "\n// ---------- Inline tool-call blocks")

    # The early-return (segmented, no trailing text) branch.
    early = fn[
        fn.index("_turnSealedArticles[_turnSealedArticles.length - 1]") : fn.index("_markLatestAssistant(article)")
    ]
    assert "article.dataset.turnId = frame.turn_id" in early
    assert early.index("article.dataset.turnId") < early.index("attachMessageActions(article")

    # The streamed-article branch.
    streamed = fn[
        fn.index("currentAssistantArticle.classList.remove") : fn.index("_markLatestAssistant(currentAssistantArticle)")
    ]
    assert "currentAssistantArticle.dataset.turnId = frame.turn_id" in streamed
    assert streamed.index("currentAssistantArticle.dataset.turnId") < streamed.index(
        "attachMessageActions(currentAssistantArticle"
    )

    # The no-streamed-article fallback delegates to renderMessage, which
    # does the stamping generically (also covers the history reload) — the
    # object literal just needs to carry the id through.
    fallback = fn[fn.index("} else {") :]
    assert "turn_id: frame && frame.turn_id" in fallback


def test_render_message_stamps_turn_id_from_the_history_row():
    js = _read(CHAT_JS)
    fn = _slice(js, "function renderMessage(m)", "\nfunction enhanceTables(root)")
    assert 'if (m.role === "assistant" && m.turn_id) tailArticle.dataset.turnId = m.turn_id;' in fn
    # Stamped before the actions row renders off it.
    assert fn.index("tailArticle.dataset.turnId = m.turn_id") < fn.index("attachMessageActions(tailArticle")


def test_feedback_controls_render_only_for_a_rateable_assistant_bubble():
    js = _read(CHAT_JS)
    fn = _slice(js, "function attachMessageActions(article, copyText)", "\nfunction buildFeedbackControls")
    assert 'article.classList.contains("msg-assistant") && article.dataset.turnId' in fn
    assert "buildFeedbackControls(article.dataset.turnId)" in fn
    # Placed after the copy button per the design's Interfaces block.
    assert fn.index("wrap.appendChild(copy)") < fn.index("buildFeedbackControls(article.dataset.turnId)")


def test_feedback_controls_have_the_two_verdict_buttons():
    js = _read(CHAT_JS)
    fn = _slice(js, "function buildFeedbackControls(turnId)", "\nasync function submitFeedback")
    assert 'wrap.className = "msg-feedback"' in fn
    assert 'up.className = "msg-feedback-btn"' in fn
    assert 'down.className = "msg-feedback-btn"' in fn
    assert 'up.dataset.verdict = "up"' in fn
    assert 'down.dataset.verdict = "down"' in fn
    assert 'up.setAttribute("aria-label", "Good answer")' in fn
    assert 'down.setAttribute("aria-label", "Bad answer")' in fn
    # Thumbs-down reveals the optional comment box.
    assert 'commentInput.className = "msg-feedback-comment"' in fn
    assert "commentInput.maxLength = 2000" in fn


def test_submit_feedback_posts_to_the_sessions_feedback_path():
    js = _read(CHAT_JS)
    fn = _slice(
        js, "async function submitFeedback(turnId, verdict, comment)", "\n/** Whether a persisted assistant row"
    )
    assert "`/api/chat/sessions/${currentChatId}/feedback`" in fn
    assert "turn_id: turnId" in fn
    assert 'method: "POST"' in fn
    assert '"Content-Type": "application/json"' in js  # via the shared api() helper
    # The two toasts the design calls out by exact wording.
    assert "Feedback needs the Postgres app-state backend." in fn
    assert "Couldn't send feedback." in fn
    assert "err.status === 501" in fn


def test_list_messages_response_field_is_read_as_turn_id():
    """The server field name and the client's read of it must agree — a
    silent rename on either side would leave the reload path with no
    thumbs and no test failure closer to the actual break."""
    js = _read(CHAT_JS)
    assert re.search(r"\bm\.turn_id\b", js)


# ---------------------------------------------------------------------------
# chat.css
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def test_feedback_css_block_exists_and_uses_only_tokens():
    css = _read(CHAT_CSS)
    assert ".msg-feedback " in css or ".msg-feedback {" in css
    assert ".msg-feedback-btn" in css
    assert ".msg-feedback-comment" in css

    block = css[css.index("/* Thumbs on a completed assistant turn") : css.index("/* Inline SVG is the ONE channel")]
    assert not _HEX_RE.search(block), f"raw hex literal in the new feedback CSS block: {_HEX_RE.findall(block)}"
    # --ds-* tokens only, never the legacy unprefixed --primary alias
    # (design-system.md; tests/test_design_system_contract.py pins this
    # repo-wide, this just documents the intent locally too).
    assert "var(--primary)" not in block
    for token in ("--ds-surface-dim", "--ds-text-primary", "--ds-primary", "--ds-border", "--ds-surface"):
        assert token in block


def test_a_rateable_answers_action_row_does_not_hide_behind_a_hover():
    """The thumbs are a signal the product ASKS people for, so they must be
    findable without a hover: `.msg-actions` (timestamp + copy) is
    `opacity: 0` until `.msg:hover`, a touch screen has no hover at all, and
    a rating nobody can see is a rating nobody gives.
    """
    css = _read(CHAT_CSS)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    visible_without_hover = [
        sel.strip()
        for sel, body in rules
        # `is-selected` is the sibling rule that keeps a row visible once a
        # verdict was PICKED — it cannot make the thumbs findable in the
        # first place, so it does not count here.
        if (
            ".msg-actions" in sel
            and ".msg-feedback" in sel
            and ":hover" not in sel
            and "is-selected" not in sel
            and "opacity: 1" in body
        )
    ]
    assert visible_without_hover, (
        "no rule keeps a rateable answer's actions row visible without a hover — "
        "the thumbs render but stay invisible until the pointer is over the bubble"
    )

