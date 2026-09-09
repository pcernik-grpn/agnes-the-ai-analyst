"""Behavioural tests for the coach-mark engine — the parts shape cannot cover.

`tests/test_tour_step_transitions.py` next door is a source-shape guard, which
is the house style for this engine (no headless browser in CI). Shape is the
wrong instrument for arithmetic though: a sign error, a swapped constant or an
inverted comparison all keep the same source text. Both functions here decide
something numeric and both have already been wrong once —

  • `_isFullyTransparent` first matched the string TAIL, which made
    `rgb(0, 0, 0)` (opaque black, ends in ", 0)") read as transparent and get
    repainted surface-white.
  • the horizontal half of `_positionPopover` clamped a right-edge anchor's
    card back under that anchor's own column, hiding the per-row controls the
    step was describing.

The third section does the same for the one LIFECYCLE hole worth executing:
whether a cross-page hop still arms its recovery when there is no card to
decorate. That is a control-flow question between two functions, and a
substring assertion cannot answer it.

So run the shipped source through node, the way
tests/test_chat_facts_rendering_ui.py already does for chat.js. Skipped when
node is absent rather than silently passing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

TOUR_JS = Path("app/web/static/js/tour.js")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _fn_source(name: str) -> str:
    """The shipped source of one top-level function — never a copy."""
    js = TOUR_JS.read_text(encoding="utf-8")
    start = js.index(f"\nfunction {name}(")
    rest = js[start + 1 :]
    return rest[: rest.index("\n}\n") + 2]


# --- _isFullyTransparent ---------------------------------------------------

# Every serialization a browser can hand back from
# `getComputedStyle(el).backgroundColor`, plus the traps.
TRANSPARENCY_CASES = [
    ("transparent", True),
    ("rgba(0, 0, 0, 0)", True),
    ("rgba(255, 255, 255, 0)", True),
    ("hsla(0, 0%, 0%, 0)", True),
    ("color(srgb 0 0 0 / 0)", True),
    ("color(srgb 0.2 0.4 0.6 / 0%)", True),
    # The tail trap: opaque black also ends in ", 0)".
    ("rgb(0, 0, 0)", False),
    ("rgb(10, 20, 0)", False),
    ("rgb(255, 255, 255)", False),
    ("hsl(0, 0%, 0%)", False),
    ("rgba(0, 0, 0, 1)", False),
    ("rgba(12, 34, 56, 0.5)", False),
    ("color(srgb 0.5 0.2 0.1 / 0.34)", False),
    ("color(srgb 0 0 0)", False),
    # Unreadable: a node detached between the two backing passes. Must fail
    # CLOSED — never repaint a background we could not read.
    ("", False),
]


def test_transparency_probe_reads_the_alpha_channel():
    script = (
        _fn_source("_isFullyTransparent")
        + "\nconst cases = "
        + json.dumps(TRANSPARENCY_CASES)
        + ";\n"
        + "console.log(JSON.stringify(cases.map(([c, want]) => "
        + "({c, got: _isFullyTransparent(c), want}))));"
    )
    results = json.loads(_node_run(script))
    wrong = [r for r in results if r["got"] != r["want"]]
    assert not wrong, f"wrong transparency verdict for: {wrong}"


# --- _positionPopover, horizontal half -------------------------------------

# Mirrors the constants in tour.js; the test asserts they are still these.
_GAP = 14
_PAD = 12


def _horizontal_source() -> str:
    """The horizontal placement block, lifted verbatim from _positionPopover
    and wrapped in a callable. Lifting rather than re-implementing is the whole
    point — a re-implementation would pass while the shipped code was wrong."""
    src = _fn_source("_positionPopover")
    start = src.index("let left = rect.left;")
    # Search AFTER `start`: the centered branch higher up writes
    # `popover.style.top` too, and anchoring on its first occurrence silently
    # yields an empty block — a test that then proves nothing.
    end = src.index("popover.style.top", start)
    block = src[start:end]
    assert "rect.left" in block and "vw" in block, "lifted an empty placement block"
    return block


def _place(cases: list[dict]) -> list[float]:
    script = (
        "const POPOVER_GAP = %d, VIEWPORT_PAD = %d;\n" % (_GAP, _PAD)
        + "function place(rect, popW, vw) {\n"
        + _horizontal_source()
        + "  return left;\n}\n"
        + "const cases = "
        + json.dumps(cases)
        + ";\n"
        + "console.log(JSON.stringify(cases.map(c => "
        + "place({left: c.left}, c.popW, c.vw))));"
    )
    return json.loads(_node_run(script))


def test_constants_the_placement_maths_is_pinned_against():
    js = TOUR_JS.read_text(encoding="utf-8")
    assert re.search(rf"const POPOVER_GAP = {_GAP};", js)
    assert re.search(rf"const VIEWPORT_PAD = {_PAD};", js)


def test_an_anchor_with_room_to_its_right_keeps_the_original_left_align():
    """The composer (x=480) and the rail rows (x=12) must not move."""
    got = _place(
        [
            {"left": 480, "popW": 380, "vw": 1512},
            {"left": 12, "popW": 380, "vw": 1512},
            {"left": 0, "popW": 380, "vw": 1280},
        ]
    )
    assert got == [480, 12, _PAD]


def test_a_right_edge_anchor_is_placed_clear_of_its_own_column():
    """The Library's Add button at x=1321 in a 1512 viewport. Old behaviour
    clamped to 1120, parking the card under the Add column; it must now land
    a gap to the LEFT of the anchor so the column stays readable."""
    (got,) = _place([{"left": 1321, "popW": 380, "vw": 1512}])
    assert got == 1321 - _GAP - 380 == 927
    assert got + 380 < 1321, "the card must end before the anchor starts"


def test_the_flip_never_pushes_the_card_off_the_left_edge():
    """A narrow viewport has no room beside the anchor; the old clamp is the
    fallback, and it must still land inside the viewport."""
    got = _place(
        [
            {"left": 300, "popW": 380, "vw": 700},
            {"left": 20, "popW": 380, "vw": 420},
            {"left": 360, "popW": 351, "vw": 375},  # phone: popW is clamped by CSS
        ]
    )
    for left, case in zip(got, [(380, 700), (380, 420), (351, 375)]):
        popW, vw = case
        assert left >= _PAD, f"card starts off-screen at {left}"
        assert left <= max(_PAD, vw - popW - _PAD), f"card overflows right at {left}"


def test_the_card_never_overlaps_the_anchor_it_points_at():
    """Sweep the anchor across a desktop viewport: wherever the card lands, it
    must not sit on top of the thing it is describing."""
    anchor_w = 142
    cases = [{"left": x, "popW": 380, "vw": 1512} for x in range(0, 1370, 10)]
    for left, c in zip(_place(cases), cases):
        a0, a1 = c["left"], c["left"] + anchor_w
        p0, p1 = left, left + c["popW"]
        overlap = min(p1, a1) - max(p0, a0)
        # Only the below/above placement may share the anchor's x-range; the
        # flip branch must clear it entirely.
        if p0 < a0:
            assert overlap <= 0, f"anchor at {a0} overlapped by card at {p0}"


# --- the cross-page hop's recovery arming ----------------------------------
#
# Not a pure function, but the one lifecycle hole worth executing rather than
# pinning by shape: `_gotoStep` arms the recovery timer, `_markPopoverPending`
# bails when there is no card, and an anchor-miss reaches the hop with the card
# already gone. Whether the flag still becomes recoverable on THAT path is a
# question about control flow, which a substring assertion cannot answer.
#
# The collaborators stubbed below (`_endTour`, `_showStep`, `stashPending`) are
# not what is under test — the arming order is — and `_markPopoverPending` is
# the real shipped source, so its early return is the real one.

_HOP_HARNESS = """
let navigated = null, shown = null, ended = false, stashed = null;
const window = { location: { pathname: '/chat', set href(v) { navigated = v; } } };
function _endTour() { ended = true; }
function _showStep(i) { shown = i; }
function stashPending(id, i) { stashed = i; }
const TOURS = { welcome: [{page: '/chat'}, {page: '/chat'}, {page: '/library'}] };
"""


def _run_hop(active_extra: str, assertions: str) -> dict:
    src = "\n".join(
        [
            _HOP_HARNESS,
            "let _active = { id: 'welcome', steps: TOURS.welcome, index: 1,"
            " navigating: false, navStuckTimer: null, popover: null, "
            + active_extra
            + " };",
            _fn_source("pathMatches"),
            _fn_source("_gotoStep"),
            _fn_source("_markPopoverPending"),
            _fn_source("_unmarkPopoverPending"),
            "const NAV_STUCK_MS = 30000;",
            assertions,
            # A pending 30s timer keeps node alive until it fires; exit as soon
            # as the assertions have printed.
            "process.exit(0);",
        ]
    )
    return json.loads(_node_run(src))


def test_an_anchor_miss_hop_still_arms_recovery_with_no_card_on_screen():
    """_showStep removes the popover before resolving the next anchor, and an
    anchor-miss routes straight into the cross-page branch. Arming the timer
    inside _markPopoverPending left this path with `navigating` stuck on for
    good — the tour silently refusing every press from then on."""
    got = _run_hop(
        "",  # popover: null — the anchor-miss case
        """
        _gotoStep(2);
        const armed = _active.navStuckTimer !== null;
        // Fire whatever was scheduled, the way a cancelled navigation would.
        clearTimeout(_active.navStuckTimer);
        _unmarkPopoverPending();
        console.log(JSON.stringify({
          navigatedTo: navigated, armed, recovered: _active.navigating === false,
        }));
        """,
    )
    assert got["navigatedTo"] == "/library", "the hop must still navigate"
    assert got["armed"], "no recovery armed: a cancelled navigation would freeze the tour"
    assert got["recovered"], "`navigating` must be clearable without a card"


def test_a_second_press_during_the_hop_does_not_navigate_again():
    got = _run_hop(
        "",
        """
        _gotoStep(2);
        const first = navigated;
        navigated = null;
        _gotoStep(2);
        _gotoStep(2);
        console.log(JSON.stringify({first, afterRepeats: navigated}));
        """,
    )
    assert got["first"] == "/library"
    assert got["afterRepeats"] is None, "a repeat press re-fired the navigation"


def test_a_same_page_step_neither_navigates_nor_arms_recovery():
    """The guard and the timer are for cross-page hops only; an in-page step
    must stay instant."""
    got = _run_hop(
        "",
        """
        _gotoStep(0);
        console.log(JSON.stringify({
          navigatedTo: navigated, shownStep: shown,
          armed: _active.navStuckTimer !== null, navigating: _active.navigating,
        }));
        """,
    )
    assert got["navigatedTo"] is None
    assert got["shownStep"] == 0
    assert got["armed"] is False
    assert got["navigating"] is False
