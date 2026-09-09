"""Static-source guards for what the coach-mark does BETWEEN steps.

Four defects behind "Agnes is showing me around but won't let me click next
here", all invisible to the existing tour guards, which check step content and
anchors rather than what happens between and around them:

1. **A cross-page hop looked like a dead button.** Advancing from the /chat
   steps to the /library ones is a full-page navigation. The browser keeps
   painting the OUTGOING page until the destination is ready, so between the
   press and that paint the reader stared at an unchanged card with a
   live-looking "Next" — for however long /library took to load (seconds on a
   real instance; the reporter waited 5-10s before it moved). Nothing
   acknowledged the press, and a second press during the gap re-fired the hop.

2. **The in-stack step ringed a control that was off screen.** `extraSpotlight`
   was applied before `_ensureAnchorVisible` scrolled the page to the anchor,
   and that scroll routinely left the "In stack only" filter ~490px above the
   fold. The ring was real, painted, and impossible for the reader to find.

3. **The card covered the column it was describing.** Horizontal placement
   aligned the card's left edge to the anchor's and clamped it back inside the
   viewport — which for an anchor near the right edge parks the card directly
   under the anchor's own column. On /library that is a table whose last two
   columns repeat per row, so "Put it in your stack" covered 8 of 9 "Add"
   buttons while telling the reader to click Add on a row, and "Share what
   turns out to be useful" covered 6 sharing badges while telling them every
   row shows who can see it.

4. **The highlight did not read as a highlight.** Two independent cascade
   failures, both because `.tour-spotlight` is a single class (0,1,0) whose
   declarations are not `!important`:

   *The fill.* Any two-class page rule beat it (`.library-page
   .lib-vis--editable { background: transparent }`), so the anchor painted
   nothing of its own, the scrim-dimmed page showed straight through, and the
   control read greyed out inside its own bright ring. Measured 67 RGB darker
   than true colour on the Library's Add and sharing-badge anchors, 72 on
   `#connect summary`. Fixed by filling `background-color` INLINE and only when
   the computed background is fully transparent — forcing it in CSS would
   flatten `#lib-new-btn` and `[data-ag-new]`, primary blue buttons that already
   measured 0.0.

   *The ring.* The chat composer's own `:focus-within` box-shadow replaced the
   spotlight's outright, and /chat autofocuses the composer — so step 1 opened
   with no ring at all and one appeared only once the reader clicked elsewhere
   and the input blurred. Fixed by making the ring `!important`; unlike the
   fill, it must outrank whatever the target draws for itself.

The tour engine is pure client-side (no headless browser in CI), so these
assert the source contract the way test_tour_rail_spotlight.py and
test_tour_onboarding_steps.py do.
"""

from pathlib import Path

TOUR_JS = Path("app/web/static/js/tour.js")
TOUR_CSS = Path("app/web/static/css/tour.css")


def _js() -> str:
    return TOUR_JS.read_text(encoding="utf-8")


def _css() -> str:
    return TOUR_CSS.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    """The source of one top-level `function name(...)` declaration."""
    js = _js()
    start = js.index(f"\nfunction {name}(")
    rest = js[start + 1 :]
    end = rest.index("\n}\n")
    return rest[: end + 2]


# --- 1. A cross-page hop acknowledges the press ---------------------------


def test_the_hop_marks_the_card_pending_before_it_navigates():
    """Order matters: the class has to land on a card the browser is still
    painting. Setting it after `location.href` would be a no-op the reader
    never sees."""
    goto = _fn("_gotoStep")
    assert "_markPopoverPending()" in goto
    assert goto.index("_markPopoverPending()") < goto.index("window.location.href")


def test_pending_kills_the_actions_and_says_so_to_assistive_tech():
    pending = _fn("_markPopoverPending")
    assert "classList.add('tour-popover--pending')" in pending
    assert "aria-busy" in pending
    # Every button, not just Next — "Back" and "I'll explore on my own" are
    # equally dead once the document is being replaced.
    assert "querySelectorAll('button')" in pending
    assert "disabled = true" in pending


def test_a_second_press_during_the_hop_is_ignored():
    """Three impatient presses used to fire two navigations and walk the reader
    from step 2 to step 5, skipping the two in between."""
    goto = _fn("_gotoStep")
    assert "if (_active.navigating) return;" in goto
    assert "_active.navigating = true;" in goto
    # The guard must be read before anything else acts on the press.
    assert goto.index("_active.navigating) return") < goto.index("_endTour(true)")


def test_each_run_starts_with_the_navigating_flag_clear():
    """`_startTour` builds `_active` fresh; a stale `navigating` would leave the
    replayed tour permanently unable to advance."""
    js = _js()
    start = js.index("  _active = {")
    assert "navigating: false," in js[start : js.index("  };", start)]


def test_pending_is_styled_and_survives_reduced_motion():
    css = _css()
    assert ".tour-popover--pending" in css
    # An indeterminate bar, not swapped copy: the card is about to be replaced.
    assert "tour-pending-sweep" in css
    assert "@keyframes tour-pending-sweep" in css
    # Reduced motion still gets a "working" state, just a still one.
    reduced = [
        block
        for block in css.split("@media (prefers-reduced-motion: reduce)")[1:]
        if "tour-popover--pending" in block.split("}\n\n")[0] or "tour-popover--pending" in block[:400]
    ]
    assert reduced, "the pending bar must be quieted under prefers-reduced-motion"


# --- 2. No ring the reader cannot see -------------------------------------


def test_extra_rings_are_applied_after_the_anchor_scroll_lands():
    render = _fn("_renderStep")
    assert "_applyExtraSpotlights(step, anchor);" in render
    assert "_ensureAnchorVisible(anchor);" in render
    # Inside the settle callback, and after the scroll correction — measuring
    # before it is exactly what put the ring off screen.
    assert render.index("_ensureAnchorVisible(anchor);") < render.index("_applyExtraSpotlights(step, anchor);")
    assert render.index("_waitForScrollSettle(anchor") < render.index("_applyExtraSpotlights(step, anchor);")


def test_an_off_screen_extra_target_is_not_ringed():
    """`_visibleMatch` only proves the element is laid out. In the viewport is a
    separate question, and the one this step got wrong."""
    apply_fn = _fn("_applyExtraSpotlights")
    assert "getBoundingClientRect()" in apply_fn
    assert "window.innerHeight" in apply_fn
    assert "window.innerWidth" in apply_fn
    # It must skip, not ring-and-hope.
    tail = apply_fn[apply_fn.index("getBoundingClientRect()") :]
    assert "continue;" in tail[: tail.index("classList.add('tour-spotlight')")]


# --- 3. The card does not sit on the column it is describing --------------


def test_a_right_edge_anchor_puts_the_card_beside_it_not_under_its_column():
    """The clamp is the fallback, not the plan. When the card cannot start at
    the anchor's left edge it must try the space to the anchor's LEFT first —
    that is what keeps a per-row control column readable underneath."""
    pos = _fn("_positionPopover")
    horizontal = pos[pos.index("let left = rect.left;") :]
    assert "rect.left - POPOVER_GAP - popW" in horizontal
    # The "is the anchor near the right edge" test, and the beside-first order.
    assert "left + popW + VIEWPORT_PAD > vw" in horizontal
    assert horizontal.index("rect.left - POPOVER_GAP - popW") < horizontal.index("vw - popW - VIEWPORT_PAD")


def test_placement_beside_still_ends_inside_the_viewport():
    """Whatever branch wins, the final value is clamped — a `beside` that
    underflows must not push the card off the left edge."""
    pos = _fn("_positionPopover")
    tail = pos[pos.index("rect.left - POPOVER_GAP - popW") :]
    assert "Math.max(VIEWPORT_PAD, Math.min(left, vw - popW - VIEWPORT_PAD))" in tail
    assert tail.index("Math.max(VIEWPORT_PAD, Math.min(left") < tail.index("popover.style.left")


def test_an_anchor_with_room_to_its_right_is_left_aligned_as_before():
    """The beside-branch is gated, not unconditional: the composer and the rail
    nav rows have room to their right and must keep the original placement."""
    pos = _fn("_positionPopover")
    horizontal = pos[pos.index("let left = rect.left;") :]
    # The flip lives behind the near-the-right-edge condition, so the default
    # path is still a plain left-align.
    assert horizontal.index("let left = rect.left;") < horizontal.index("if (left + popW + VIEWPORT_PAD > vw) {")


# --- 4. The highlight actually reads as a highlight ------------------------


def test_a_transparent_target_gets_its_background_filled_inline():
    """`z-index: 9010` lifts only what the anchor ITSELF paints. `.tour-spotlight`
    is one class (0,1,0) and its `background` is not `!important`, so
    `.library-page .lib-vis--editable { background: transparent }` wins — the
    anchor paints nothing, the dimmed page shows through, and the control reads
    greyed out inside a bright ring. An inline style outranks any non-important
    stylesheet rule."""
    fn = _fn("_applySpotlightBacking")
    assert "el.style.backgroundColor = 'var(--ds-surface)'" in fn
    assert "_isFullyTransparent(getComputedStyle(el).backgroundColor)" in fn
    # Anchor and extra rings alike.
    assert "_active.spotlight" in fn
    assert "extraSpotlights" in fn


def test_only_a_fully_transparent_target_is_filled():
    """`#lib-new-btn` and `[data-ag-new]` are primary blue buttons that already
    measured undimmed; repainting them surface-white is the regression a blanket
    `!important` fill would have shipped."""
    assert "if (!_isFullyTransparent" in _fn("_applySpotlightBacking")
    probe = _fn("_isFullyTransparent")
    assert "'transparent'" in probe
    # How it decides is pinned by
    # test_transparency_is_read_from_the_alpha_channel_not_the_string_tail.


def test_the_fill_is_reverted_to_whatever_was_there_before():
    """The author's own inline value (usually '') goes back on, so the tour
    leaves the element exactly as it found it."""
    apply_fn = _fn("_applySpotlightBacking")
    assert "prev: el.style.backgroundColor" in apply_fn
    clear = _fn("_clearSpotlightBacking")
    assert "el.style.backgroundColor = prev" in clear


def test_the_fill_is_not_re_saved_on_the_second_pass():
    """_renderStep backs the target twice (see below). Without the guard the
    second pass would record the surface colour as the 'previous' value and the
    fill would never be reverted."""
    fn = _fn("_applySpotlightBacking")
    assert "_active.backed.some((entry) => entry.el === el)" in fn


def test_backing_is_dropped_on_step_change_and_on_end():
    assert "_clearSpotlightBacking()" in _fn("_showStep")
    assert "_clearSpotlightBacking()" in _fn("_endTour")


def test_backing_is_applied_twice_early_then_after_the_scroll_settles():
    """Twice on purpose. The settle pass runs on requestAnimationFrame, which a
    browser pauses in a hidden or background tab — waiting only for it leaves
    the target dimmed for as long as the tab stays hidden."""
    render = _fn("_renderStep")
    assert render.count("_applySpotlightBacking();") == 2
    late = render.rindex("_applySpotlightBacking();")
    assert render.index("_applySpotlightBacking();") < render.index("_buildPopover")
    assert render.index("_applyExtraSpotlights(step, anchor);") < late


def test_the_spotlight_ring_outranks_the_targets_own_focus_ring():
    """/chat autofocuses the composer, whose `:focus-within` box-shadow replaced
    the spotlight's outright — step 1 opened with NO ring, and one appeared only
    after the reader clicked elsewhere and the input blurred. The ring is the
    spotlight's entire job, so it has to win."""
    css = _css()
    block = css[css.index(".tour-spotlight {"):]
    block = block[: block.index("\n}")]
    assert "box-shadow:" in block
    shadow = block[block.index("box-shadow:"):]
    assert "!important" in shadow, "the spotlight ring must outrank the target's own"


def test_no_separate_plate_element_survives_in_the_dom():
    """An earlier fix backed the target with a sibling <div>. It OUTLIVED its
    target: clicking "Add to my agents" — which the step invites — re-rendered
    the row and left the plate behind as an empty white pill floating over the
    page. Nothing may re-introduce a detached backing layer."""
    js = _js()
    assert "tour-spotlight-plate" not in js
    assert "tour-spotlight-plate" not in _css()


def test_transparency_is_read_from_the_alpha_channel_not_the_string_tail():
    """`rgb(0, 0, 0)` is opaque black and ends in ", 0)". A tail match calls it
    transparent and repaints a legitimately black control surface-white."""
    fn = _fn("_isFullyTransparent")
    # rgb()/hsl() carry no alpha at all and must short-circuit to opaque.
    assert "/^(rgba|hsla)\\(/i.test(color)" in fn
    # rgba()/hsla(): the FOURTH component, and only when there are four.
    assert "parts.length === 4" in fn
    assert "parseFloat(parts[3]) === 0" in fn
    # color(srgb r g b / a) and friends.
    assert "parseFloat(slash[1]) === 0" in fn


def test_coming_back_from_the_bfcache_unfreezes_the_card():
    """A cross-page hop leaves `navigating` set and the card pending. Press Back
    and the browser can restore that page whole from the back/forward cache —
    same DOM, same module state — so the tour comes back frozen: greyed-out
    actions and a `_gotoStep` that refuses every press."""
    fn = _fn("_onPageShow")
    assert "e.persisted" in fn
    assert "_active.navigating = false" in fn
    assert "classList.remove('tour-popover--pending')" in fn
    assert "btn.disabled = false" in fn
    # Wired up and torn down with the rest.
    assert "window.addEventListener('pageshow', _onPageShow)" in _fn("_attachListeners")
    assert "window.removeEventListener('pageshow', _onPageShow)" in _fn("_removeListeners")


def test_the_first_steps_back_button_stays_disabled_after_a_bfcache_restore():
    """Re-enabling every button wholesale would hand step 1 a live "Back" that
    _buildPopover had deliberately disabled."""
    fn = _fn("_onPageShow")
    assert "_active.index === 0" in fn
    assert "back.disabled = true" in fn
