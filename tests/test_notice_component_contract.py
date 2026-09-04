"""The `.notice` contract: one component, one builder, one tone decision.

A transcript note, an upload dialog's error slot and a toast were three
visual languages for the same kind of message, and every one of them decided
its own tone — almost always by passing ``"error"`` literally. That is how a
rate limit that clears itself, a file that is too large, and "Up to 4
attachments per message." all arrived in the same red as a crashed runner.

The fix was structural, so the guard is too. What these tests protect is not a
particular shade: it is that the three surfaces cannot drift apart again,
because they share one builder (``window.agnesNotice``), one copy module
(``chat_errors.js``) and one classifier (``chatErrorTone`` /
``requestErrorTone``) — and that ``/_debug/error-surfaces`` renders from those
same three, so what a designer reviews there is what a user gets.

Source-level on purpose: these are wiring facts, and wiring is exactly what
regressed. The behaviour of the copy and the classifier is covered by
tests/test_chat_upstream_rate_limit_copy.py, which runs the real module.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
CHAT_JS = STATIC / "js" / "chat.js"
CHAT_ERRORS_JS = STATIC / "js" / "chat_errors.js"
APP_JS = STATIC / "app.js"
STYLE_CSS = STATIC / "style-custom.css"
CHAT_CSS = STATIC / "css" / "chat.css"
PAPER_CSS = STATIC / "css" / "paper-skin.css"
GALLERY = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "debug_error_surfaces.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# one builder
# ---------------------------------------------------------------------------


def test_the_builder_is_global_and_takes_a_placement():
    """`window.agnesNotice` lives in app.js because app.js loads on every page
    as a classic script — a module could not be reached from inline page
    scripts, and the toast surface is not chat-only."""
    app = _read(APP_JS)
    assert "window.agnesNotice = agnesNotice;" in app
    assert "function agnesNotice(text, kind, opts)" in app
    assert 'opts.placement === "floating" ? "floating" : "inline"' in app


def test_the_glyph_markup_is_author_controlled_never_caller_text():
    """The icon is set with innerHTML, so it must come from the table in app.js
    and never from a caller's string — the message itself goes through
    textContent."""
    app = _read(APP_JS)
    builder = app[app.index("function agnesNotice(text, kind, opts)") :][:2000]
    assert "icon.innerHTML = glyph;" in builder
    assert "msg.textContent = String(text" in builder
    assert ".innerHTML = text" not in builder and ".innerHTML = msg" not in builder


@pytest.mark.parametrize(
    "surface,needle",
    [
        (
            "transcript note",
            'window.agnesNotice(text, kind, { placement: "inline", extraClass: "cloud-chat-system-note" })',
        ),
        ("chat toast", 'window.agnesNotice(text, kind, { placement: "floating", extraClass: "cloud-chat-toast" })'),
        ("dialog error slot", 'window.agnesNotice(msg, tone, { placement: "inline" })'),
    ],
)
def test_every_chat_surface_builds_through_the_shared_builder(surface, needle):
    assert needle in _read(CHAT_JS), f"{surface} no longer uses window.agnesNotice"


def test_the_global_toast_composes_the_shared_notice():
    app = _read(APP_JS)
    assert 'agnesNotice(msg, kind, { placement: "floating", extraClass: "toast" })' in app


# ---------------------------------------------------------------------------
# one tone decision
# ---------------------------------------------------------------------------


def test_the_transcript_derives_its_tone_from_the_cause():
    """`renderSystemNote(copy, "error")` was the whole bug on this surface."""
    chat = _read(CHAT_JS)
    assert (
        "renderSystemNote(chatErrorCopy(frame.message, frame.kind), chatErrorTone(frame.message, frame.kind))" in chat
    )


def test_the_dialogs_derive_their_tone_from_the_same_response_as_their_sentence():
    """Both come from the response body, which can only be consumed once —
    hence the clone. A dialog that reads the body for its sentence and then
    hard-codes red is the exact regression this pins."""
    chat = _read(CHAT_JS)
    assert "async function _responseTone(res)" in chat
    assert chat.count("await _responseTone(res)") >= 3, "an upload dialog stopped deriving its tone"
    assert "res.clone()" in chat


def test_no_chat_surface_hard_codes_a_tone_where_it_knows_the_cause():
    """A literal tone is fine where we authored the sentence and know what it
    is (a size cap is a caution, a precondition is info). It is NOT fine on a
    branch that has a status or a server code in hand and ignores it."""
    chat = _read(CHAT_JS)
    offenders = []
    for m in re.finditer(r'showToast\(\s*`([^`]*)`\s*,\s*"error"', chat):
        text = m.group(1)
        if "err.message" in text or "${msg}" in text:
            offenders.append(text)
    assert not offenders, f"these toasts have a cause available but pass 'error' literally: {offenders}"


def test_the_classifier_is_the_only_place_tone_is_decided():
    """Both entry points delegate to one internal function, so the transcript
    and a dialog can never disagree about the same condition."""
    mod = _read(CHAT_ERRORS_JS)
    assert "function _tone(text, status)" in mod
    assert "export function chatErrorTone(raw, kind)" in mod
    assert "export function requestErrorTone(status, code)" in mod
    for fn in ("chatErrorTone", "requestErrorTone"):
        body = mod[mod.index(f"export function {fn}(") :]
        body = body[: body.index("\n}\n")]
        assert "_tone(" in body, f"{fn} decides a tone itself instead of delegating"


# ---------------------------------------------------------------------------
# one look
# ---------------------------------------------------------------------------


def test_tone_never_repaints_the_text_or_the_surrounding_hairline():
    """Coloured text on a coloured ground is what made these read as alarms and
    what cost them legibility. And the hairline stays neutral because the
    accent `-line` tokens invert between the light and dark palettes — painting
    all four edges with one drew a bright outline around the whole element on
    dark."""
    css = _read(STYLE_CSS)
    block = css[css.index("/* =====================================================\n   .notice") :]
    block = block[: block.index("/* =====================================================\n   Error pages")]
    for tone in ("is-warn", "is-error", "is-ok", "is-info"):
        rule = re.search(rf"\.notice\.{tone}[^{{]*\{{([^}}]*)\}}", block)
        assert rule, f"no rule for .notice.{tone}"
        body = rule.group(1)
        assert "color:" not in body.replace("border-left-color:", ""), (
            f".notice.{tone} sets a text colour: {body.strip()}"
        )
        assert "border-color:" not in body, f".notice.{tone} repaints the whole hairline: {body.strip()}"


def test_the_toast_is_not_a_pill():
    """`--ds-radius-pill` is the BADGE language. It suited the one-line
    confirmation the prototype showed and became a lozenge with the text
    crammed in the moment a toast carried a sentence."""
    paper = _read(PAPER_CSS)
    rule = re.search(r'\[data-theme="paper"\]\s+\.toast\s*\{([^}]*)\}', paper)
    assert rule, "the paper toast rule moved — re-point this guard"
    assert "--ds-radius-pill" not in rule.group(1), rule.group(1)


@pytest.mark.parametrize(
    "cls,owner",
    [("cloud-chat-system-note", CHAT_CSS), ("cloud-chat-toast", CHAT_CSS)],
)
def test_the_legacy_classes_carry_only_placement_not_a_look(cls, owner):
    """They stay as hooks (chat.css sizes them, guards elsewhere name them),
    but the surface, border and tone belong to `.notice` — two owners for one
    look is how the three surfaces drifted apart in the first place."""
    css = _read(owner)
    rule = re.search(rf"^\.{cls} \{{([^}}]*)\}}", css, re.MULTILINE)
    assert rule, f".{cls} rule not found"
    body = rule.group(1)
    for prop in ("background:", "border:", "border-radius:", "box-shadow:", "color:"):
        assert prop not in body, f".{cls} still owns `{prop}` — that belongs to .notice"


# ---------------------------------------------------------------------------
# the preview cannot lie
# ---------------------------------------------------------------------------


def test_the_gallery_renders_from_the_shipped_builder_and_copy():
    """A gallery that transcribes its subject goes stale silently and then
    lies. This one calls the same two things the app calls."""
    html = _read(GALLERY)
    assert "window.agnesNotice(" in html
    assert "from \"{{ static_url('js/chat_errors.js') }}\"" in html
    assert "chatErrorCopy" in html and "chatErrorTone" in html and "requestErrorTone" in html
    # No hand-written sentence: every message on the page comes from a call.
    assert "rate-limited" not in html and "Try again in a moment" not in html


def test_the_gallery_is_dev_only():
    """It renders whatever the copy module says, including states an operator
    should not be able to conjure on a live instance."""
    router = (Path(__file__).resolve().parents[1] / "app" / "web" / "router.py").read_text(encoding="utf-8")
    body = router[router.index('@router.get("/_debug/error-surfaces"') :][:2000]
    assert "if not _is_debug():" in body
    assert "raise HTTPException(status_code=404" in body
