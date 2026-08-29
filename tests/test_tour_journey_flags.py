"""Static-source guards for the coach-mark tour's journey bookkeeping.

The tour engine (`tour.js`) is pure client-side, so there is no headless
browser in CI — we assert the source contract the way
test_chat_surface_badge.py and test_design_system_contract.py do.

Two invariants:

1. **The tour may not assert activity-driven journey flags.** `first_asked`
   completes when a question is actually asked and `stack_setup_done` when a
   package is actually subscribed. `stack_setup_done` additionally gates the
   in-chat gap resolver, so asserting it from the tour permanently suppresses
   the "your Stack is empty, here's what I'd add" card for anyone who takes
   the tour before asking anything.
2. **Progress dots count only steps that render.** A step whose anchor is
   absent (permission-gated CTA, layout-dependent nav item) is skipped
   silently; it must not still be counted, or the indicator promises a step
   the user never sees.
"""

import re
from pathlib import Path

TOUR_JS = Path("app/web/static/js/tour.js")
ONBOARDING_JS = Path("app/web/static/js/chat_onboarding.js")


def _js() -> str:
    return TOUR_JS.read_text(encoding="utf-8")


def _code() -> str:
    """`tour.js` with comments stripped.

    The flag assertions below are about what the engine *does*, and the
    rationale for not touching those flags is itself written in a comment that
    names them — so match against code only.
    """
    src = _js()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.MULTILINE)
    return re.sub(r"\s//.*$", "", src, flags=re.MULTILINE)


# --- Journey flags -------------------------------------------------------


def test_tour_does_not_assert_activity_driven_flags():
    """The tour never PUTs first_asked / stack_setup_done.

    Regression guard: the old `markAllJourneyDone()` set all five flags,
    which silently disabled the gap resolver for tour-first users.
    """
    js = _code()
    assert "first_asked" not in js, (
        "tour.js must not touch first_asked — it completes when the user "
        "actually asks a question (see chat_onboarding.js)"
    )
    assert "stack_setup_done" not in js, (
        "tour.js must not touch stack_setup_done — it gates the in-chat gap "
        "resolver and completes on a real subscription"
    )


def test_tour_marks_only_the_step_it_walks():
    """Finishing the tour asserts exactly ONE step: "Explore your Library",
    which is the thing the tour actually does for you.

    `catalog_discovered` used to ride along, from when that step meant "saw the
    Catalog" and the tour walked it. Under its current label ("Add or share
    something") it is an action the user has to take, and ticking it on finish
    left the checklist claiming credit for work nobody had done."""
    js = _js()
    assert "markTourStepsDone" in js
    body = js.split("async function markTourStepsDone()", 1)[1].split("}\n", 1)[0]
    assert "explored_stack: true" in body
    assert "catalog_discovered" not in body


def test_connect_button_marks_use_anywhere():
    """ "Take Agnes to my tools" navigates to #connect, which is what the
    journey panel's step of the same name does — so it marks the same flag."""
    js = _js()
    assert "markUseAnywhereDone" in js
    assert "use_anywhere: true" in js


def test_gap_resolver_still_gated_on_stack_setup_done():
    """The invariant the first test protects is real: chat_onboarding.js does
    gate its gap resolver on stack_setup_done. If that ever changes, the
    reasoning above needs revisiting rather than silently rotting."""
    onboarding = ONBOARDING_JS.read_text(encoding="utf-8")
    assert "stack_setup_done" in onboarding


# --- Progress dots -------------------------------------------------------


def test_missing_anchor_is_recorded_as_skipped():
    js = _js()
    assert "_active.skipped.add(index)" in js


def test_dots_are_built_from_rendering_steps_only():
    js = _js()
    # The label counts the shown steps, not the raw step list.
    assert "Step ${position + 1} of ${shown.length}" in js
    assert "skipped.has(i)" in js


def test_skipped_steps_survive_cross_page_hops():
    """The tour navigates /stack -> /catalog mid-run; a skip discovered before
    the hop must not be re-counted after it."""
    js = _js()
    assert "rec.skipped = Array.from(skipped)" in js
    assert "_startTour(id, steps, index, skipped)" in js
