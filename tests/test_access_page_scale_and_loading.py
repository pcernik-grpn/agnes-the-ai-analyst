"""What /admin/access does at size, and while it is still loading.

Two live reports, both against v0.96.0:

  * "if I click by resource the whole thing freezes and crashes because it's
    probably loading all 211k resources" — it was. `corpus_file` is one
    grantable item per crawled file, and the By-resource renderer built the
    HTML for every ungranted row and handed it to `innerHTML` inside a
    CLOSED `<details>`. A closed `<details>` still parses its children, so
    "collapsed by default" bought nothing. Reproduced locally at 30,000
    seeded files: 4.25 MB of payload became 751,665 DOM nodes and wedged the
    tab for over 45 seconds. After the fix, the same seed renders 1,415.

  * #2140 item 1 — an expanded group showed "Nobody is in this group yet"
    while its own header counted six people, because a failed members read
    and an empty group were the same value. Item 2 — the loading state was
    a line of small type over an already-drawn "Pick a group" card.

These are source guards, not renders: the behaviours are in the browser and
CI has no headless one. Each pins the specific mechanism that failed, so a
regression names itself rather than showing up as "the page is slow".
"""

from __future__ import annotations

import re

import pytest

from tests.helpers.access_page import access_js, access_template


@pytest.fixture(scope="module")
def js() -> str:
    return access_js()


class TestACollapsedSectionIsNotRendered:
    """The crash. `innerHTML` is what costs; a closed `<details>` does not
    save you from it."""

    def test_the_nobody_drawer_builds_rows_only_while_open(self, js):
        line = next(ln for ln in js.splitlines() if "cappedRuns(nobody" in ln)
        assert "nobodyOpen ?" in line, (
            "the ungranted rows must be built only when the drawer is open — "
            "this is the line that took the tab down at 211k files"
        )

    def test_opening_the_drawer_asks_for_a_repaint(self, js):
        """Corollary of the above: rows built lazily do not appear on a
        native `<details>` toggle, so the handler has to repaint."""
        handler = js[js.index('const nobodySum = e.target.closest("[data-nobody] > summary")') :]
        handler = handler[: handler.index("const kindRun")]
        assert "renderBundles()" in handler

    def test_a_run_decides_its_default_from_its_own_size(self, js):
        assert "const RUN_OPEN_MAX" in js
        assert "size > RUN_OPEN_MAX" in js, (
            "a big run must start collapsed; a small one must not, or every "
            "ordinary instance pays for the pathological one"
        )

    def test_an_open_run_renders_a_bounded_number_of_rows(self, js):
        assert "const RUN_RENDER_CAP" in js
        assert "list.slice(0, RUN_RENDER_CAP)" in js

    def test_a_truncated_run_says_so_and_says_how_to_narrow(self, js):
        """Silent truncation is the worse half of the bug it replaces: a
        reader who cannot find a file would conclude it is not granted."""
        body = js[js.index("const cappedRuns = (") :]
        body = body[: body.index("\n    };")]
        assert "Showing ${RUN_RENDER_CAP}" in body
        assert "list.length.toLocaleString()" in body, "the real total, grouped"
        assert "search by name" in body and "Filter" in body

    def test_the_search_is_not_bounded_by_the_render_cap(self, js):
        """What makes the cap honest: the search runs over every item, and
        only the RENDER is bounded — so a named file is always reachable
        however many are hidden. Verified live at 30,000 seeded files: a
        search for #29,999 returns it, from a list showing 200."""
        fn = js[js.index("function renderBundles()") :]
        collect = fn[fn.index("const rows = [];") : fn.index("if (!rows.length)")]
        assert "hay.includes(q)" in collect, "the search is what builds the row set"
        assert "RUN_RENDER_CAP" not in collect, "the cap must not narrow what is searched"

    def test_grants_are_indexed_once_rather_than_scanned_per_item(self, js):
        """`.filter()` over every grant inside the item loop is quadratic at
        the size corpus_file reaches, and it ran before anything collapsed."""
        fn = js[js.index("function renderBundles()") :]
        fn = fn[: fn.index("if (!rows.length)")]
        assert "const heldBy = new Map();" in fn
        assert "(overview.grants || []).filter(" not in fn

    def test_the_toggle_reads_the_state_the_reader_can_see(self, js):
        """With a size default, "is it in shutKinds" is not the same question
        as "is it open" — one click would appear to do nothing."""
        h = js[js.index('const kindRun = e.target.closest("[data-kindrun]")') :]
        h = h[: h.index("const bbSum")]
        assert 'getAttribute("aria-expanded")' in h


class TestAFailedMemberReadIsNotAnEmptyGroup:
    """#2140 item 1. The empty state is a policy assertion — "anything
    granted under Access reaches no one" — and may only be made from an
    answer, never from a failure."""

    @pytest.fixture(scope="class")
    def render_members(self, js) -> str:
        body = js[js.index("async function renderMembers()") :]
        return body[: body.index("\n  function initialsOf(")]

    def test_a_failure_is_distinguishable_from_an_empty_group(self, render_members):
        assert "membersFailed" in render_members

    def test_a_failure_is_never_cached(self, render_members):
        """The half that made it stick: caching `[]` meant collapsing and
        reopening could not recover — one bad response poisoned the group
        for the rest of the sitting."""
        for line in render_members.splitlines():
            if "membersCache.set" in line:
                assert line.strip().startswith("membersCache.set"), line
        cache_at = render_members.index("membersCache.set(selectedGroup, members)")
        ok_at = render_members.index("if (r.ok) {")
        else_at = render_members.index("membersFailed = true;")
        assert ok_at < cache_at < else_at, "the cache write must sit on the ok branch only"

    def test_the_confident_empty_state_is_unreachable_after_a_failure(self, render_members):
        fail_at = render_members.index("if (membersFailed) {")
        empty_at = render_members.index("Nobody is in this group yet")
        assert fail_at < empty_at, "the failure branch must return before the empty state"

    def test_the_error_state_says_grants_still_apply_and_offers_a_retry(self, render_members):
        flat = " ".join(render_members.split())
        assert "Couldn’t load who is in this group" in flat
        assert "the member list failing, not the group emptying" in flat
        assert "data-retry-members" in flat

    def test_the_header_does_not_report_nobody_when_the_read_failed(self, render_members):
        """The reported symptom was two claims an inch apart on one screen."""
        head = render_members[render_members.index("setPeopleHead(") :]
        head = head[: head.index("// The find box")]
        assert 'membersFailed ? "Unknown"' in head

    def test_the_retry_is_wired(self, js):
        assert 'e.target.closest("[data-retry-members]")' in js


class TestTheLoadingStateLooksLikeLoading:
    """#2140 item 2: the page rendered its whole chrome, one line of small
    type, and an already-drawn "Pick a group" card — which reads as a broken
    render rather than one that has not arrived."""

    @pytest.fixture(scope="module")
    def tpl(self) -> str:
        return access_template()

    def test_the_bare_loading_line_is_gone(self, tpl):
        assert '<div class="ax-empty">Loading groups…</div>' not in tpl

    def test_the_list_region_holds_skeleton_rows(self, tpl):
        assert 'id="ax-groups" aria-busy="true"' in tpl
        assert len(re.findall(r"ax-skel__row", tpl)) >= 3

    def test_the_skeleton_still_says_something_to_a_screen_reader(self, tpl):
        """The bars carry no text; a status line does."""
        assert 'role="status"' in tpl
        assert "ax-skel__say" in tpl

    def test_the_placeholder_card_waits_for_data(self, tpl):
        assert '<div class="ax-work" id="ax-work" hidden>' in tpl

    def test_the_pulse_stops_under_reduced_motion(self, tpl):
        """A moving page is the wrong thing to hand someone who asked for
        stillness, and a static bar still reads as "not yet"."""
        skel = tpl[tpl.index("/* The loading state (#2140 item 2)") :]
        skel = skel[: skel.index("</style>")] if "</style>" in skel else skel
        block = skel[skel.index("prefers-reduced-motion") :][:200]
        assert "ax-skel__b" in block and "animation: none" in block

    def test_both_exits_from_the_fetch_clear_the_loading_state(self, js):
        """A page stuck mid-skeleton after a failure is the reported state,
        only worse."""
        boot = js[js.index("bootWhenReady(async function boot()") :]
        boot = boot[: boot.index("const params = new URLSearchParams")]
        assert boot.count("settled();") == 2, "the catch and the success path both"

    def test_settled_clears_the_busy_flag_and_reveals_the_panel(self, js):
        fn = js[js.index("const settled = () => {") :]
        fn = fn[: fn.index("\n  };")]
        assert 'removeAttribute("aria-busy")' in fn
        assert "work.hidden = false" in fn
