"""A `next_actions` chip with an unfilled placeholder must not one-click-send.

Measured on a live production instance (2026-09-09): the model wrote a
`next_actions` suggestion with fill-in-the-blank brackets left in —
`Client is [name], [industry], <engagement type> ...` — and the web chat's
one-click chip sent that text verbatim as the user's next message, because
`renderNextActions`'s click handler always sets the composer value and
submits immediately (see `app/web/static/js/chat.js`). Three round-trips
were lost this way in one session, in front of a customer.

`extractNextActions` is pure (no DOM) and is executed for real under node,
same pattern as `tests/test_chat_turn_tail_state_ui.py`. `renderNextActions`
builds DOM and this repo carries no jsdom dependency, so its click-handler
wiring is pinned structurally — same trade-off
`tests/test_chat_facts_rendering_ui.py` documents for other DOM-building
functions.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _guard_src() -> str:
    """The shipped placeholder-detection regex plus the shipped predicate."""
    js = _read(CHAT_JS)
    i = js.index("const _PLACEHOLDER_RE")
    # To the terminating `;`, not to the end of the line: a wrapped
    # declaration would otherwise be sliced in half and every case here
    # would fail with a node syntax error rather than a real verdict.
    src = js[i : js.index(";", i) + 1] + "\n"
    i = js.index("function _hasUnfilledPlaceholder")
    src += js[i : js.index("\n}", i) + 2]
    return src


def _check(cases: list[str]) -> list[bool]:
    script = _guard_src() + (
        f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(_hasUnfilledPlaceholder)));\n"
    )
    return json.loads(_node_run(script))


# ── the pure predicate, run for real under node ─────────────────────────────


def test_square_bracket_placeholder_is_detected():
    (r,) = _check(["Client is [name], [industry], standalone, SOW and deck"])
    assert r is True


def test_angle_bracket_placeholder_is_detected():
    (r,) = _check(["The signer is [real name], <title> — re-render the SOW"])
    assert r is True


def test_handlebars_placeholder_is_detected():
    (r,) = _check(["Invoice {{amount}} to the client"])
    assert r is True


def test_bare_tbd_marker_is_detected():
    (r,) = _check(["Fee is TBD — confirm before rendering"])
    assert r is True


def test_ordinary_follow_up_prompts_are_not_flagged():
    results = _check(
        [
            "Break daily revenue down by country",
            "Chart the last 90 days as a trend line",
            "Compare Q2 to Q3 margins",
        ]
    )
    assert results == [False, False, False]


def test_empty_and_missing_text_are_not_flagged():
    assert _check([""]) == [False]


def test_comparison_syntax_is_not_flagged():
    """Bracket/angle syntax alone is not proof of an unfilled placeholder —
    this product is an analytics chat, and comparison phrasing in a
    suggested action is entirely ordinary. The old `<[^<>]+>` pattern
    matched clear across `<10 and >10` (everything between the first `<`
    and the first `>`), which would have taken one-click submission away
    from a perfectly answerable follow-up."""
    results = _check(
        [
            "Compare customers with revenue <10 and >10",
            "Filter for score <5 or >95",
        ]
    )
    assert results == [False, False]


def test_bracketed_citation_marker_is_not_flagged():
    """`[1]` is a citation/footnote marker, not a fill-in-the-blank slot —
    digits inside brackets read as a reference, never as an unfilled
    template."""
    (r,) = _check(["See the finding in [1] for details"])
    assert r is False


def test_capitalised_slot_labels_are_detected():
    """A slot is as likely to be written `[Client name]` or `<Start date>`
    as lowercase, so case is not part of the test. Keying on capitalisation
    would leave exactly this hole: the chip would send the template."""
    results = _check(
        [
            "Draft the proposal for [Client name]",
            "Schedule the kickoff for <Start date>",
            "Rename the engagement to [Project_Name]",
        ]
    )
    assert results == [True, True, True]


def test_a_filled_bracketed_name_pre_fills_rather_than_sends():
    """The accepted cost of ignoring case: a literal, already-filled name in
    brackets reads as a slot, so the chip pre-fills instead of sending. That
    is one keystroke, against a wasted turn and a refused render if we
    guessed the other way — the asymmetry is deliberate, so pin it rather
    than let a future change quietly flip it."""
    (r,) = _check(["Draft a renewal quote for [Acme Corp]"])
    assert r is True


# ── structural pin: the click handler must consult the guard ───────────────


def test_render_next_actions_click_handler_consults_the_guard():
    """The click handler must check `_hasUnfilledPlaceholder(action)` and
    stop short of submitting when it is true — pre-filling the composer
    for the user to complete instead of sending the template verbatim.
    Pinned structurally: `renderNextActions` builds DOM and this repo
    carries no jsdom dependency (see module docstring)."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert "_hasUnfilledPlaceholder(action)" in fn
    # The guard must run BEFORE the submit dispatch, not after — a check
    # that only fires post-submit is not a guard at all.
    guard_pos = fn.index("_hasUnfilledPlaceholder(action)")
    submit_pos = fn.index("new SubmitEvent")
    assert guard_pos < submit_pos
    # The composer must still receive the text (pre-fill, not drop) — the
    # user still benefits from the chip, they just have to complete it.
    assert "ta.value = action" in fn


def test_guarded_branch_dispatches_input_event_before_returning():
    """A programmatic `ta.value = action` does not fire the textarea's own
    `input` listener — the one that owns autosizing, prompt-history reset,
    and slash-menu sync. Reproduced live: open the slash menu with `/`,
    then click a placeholder chip — the text is replaced but the menu stays
    open and the textarea keeps its old height. The guarded branch must
    dispatch that event itself, after the assignment and before its own
    early return, so the existing synchronisation path runs instead of a
    second, duplicated update here."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    guard_pos = fn.index("_hasUnfilledPlaceholder(action)")
    # The guarded branch's own `return;` — the first one after the guard.
    guard_return_pos = fn.index("return;", guard_pos)
    dispatch_pos = fn.index('dispatchEvent(new Event("input"', guard_pos)
    assert guard_pos < dispatch_pos < guard_return_pos


def test_extract_next_actions_does_not_drop_placeholder_actions():
    """The offer is not silently dropped — a chip with a placeholder still
    renders (guarded at click time), it just cannot auto-send. Dropping it
    would remove a genuinely useful suggestion (e.g. only 1 of 3 candidate
    actions had a placeholder)."""
    js = _read(CHAT_JS)
    fn = js[js.index("function extractNextActions") : js.index("/** True while the stream sits")]
    assert "_hasUnfilledPlaceholder" not in fn, "extraction must not filter — the click handler is the guard"
