"""The package builder can reach a source it does not have yet.

The builder assumed the tables already existed. Its only answer to "the table
I want is not in here" was a link to /admin/tables, which addresses one of the
two reasons it can be true — the table is registered nowhere — and not the
other, that its source was never connected at all. Following the second meant
leaving, and the builder had no draft store, so the admin came back to an empty
form and usually did not come back at all.

What this suite pins, each because losing it silently returns the flow to what
it was:

  * the connect route lives in the picker's foot, as an ACTION rather than a
    link buried in a sentence — that is where you are the moment a table is
    actually missing. The panel keeps only the state that is about the whole
    page: nothing registered and nothing connected;
  * the draft is parked for the detour and restored ONLY for the detour, so an
    abandoned builder does not resurrect itself over a fresh start;
  * whatever the trip registered comes back ticked, worked out by DIFFING the
    registry — so the hand-back holds no knowledge of the connect wizard and
    cannot drift with it;
  * the draft dies the moment the package exists, or the next visit offers to
    build a duplicate;
  * /admin/data-sources says which package it took you away from, and offers
    the way back;
  * on that entry the wizard stops after Choose tables. Its Bundle and Share
    steps build a data package, which is not a thing to offer someone already
    half-way through building one.
"""

from __future__ import annotations

from tests import _ds_page_source

import re
from pathlib import Path

TEMPLATES = Path("app/web/templates")
STATIC = Path("app/web/static")

COMPONENT = STATIC / "js" / "components" / "package_drawer.js"
COMPONENT_CSS = STATIC / "css" / "package_drawer.css"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestTheConnectRouteLivesWhereTheListFails:
    def test_the_panel_speaks_up_only_when_the_package_cannot_be_filled(self) -> None:
        """A standing "Missing a table? Connect a source" line under a list the
        admin is happy with is a permanent footnote for a once-a-quarter
        errand. What survives on the panel is the one state that is about the
        whole page rather than about this list: nothing registered, nothing
        connected. Every other "I need a source" moment is answered in the
        picker, which is where you are the moment a table is actually missing.
        """
        src = COMPONENT.read_text(encoding="utf-8")
        assert "function emptyRegistryNoteHtml()" in src
        assert "emptyRegistryNoteHtml();" in src, "it must be CALLED from renderTables, not merely defined"
        # The retired standing line leaves nothing behind.
        assert "pdw-connectline" not in src
        assert "Missing a table?" not in src

    def test_all_the_offers_drive_one_handler(self) -> None:
        src = COMPONENT.read_text(encoding="utf-8")
        # Picker foot, picker empty state, panel's nothing-connected note.
        assert src.count("data-pdw-connect") >= 3
        assert "function leaveToConnect()" in src
        # The picker lives in its own click root, so it needs its own binding —
        # and it must be checked BEFORE the backdrop test, or the navigation is
        # swallowed by "clicked outside the card".
        picker_handler = src.split("if (e.target.closest('.pdw-grp__box'))", 1)[1]
        connect_at = picker_handler.index("data-pdw-connect")
        close_at = picker_handler.index("data-ag-pick-close")
        assert connect_at < close_at, "the connect route must be handled before the picker's close handlers"

    def test_one_look_for_it_rather_than_two(self) -> None:
        css = COMPONENT_CSS.read_text(encoding="utf-8")
        assert ".pdw-connect" in css
        assert ".pdw-connectline" not in css, "the retired standing line must not leave its styling behind"
        # Design-system tokens only — a literal colour would not follow the
        # theme the rest of the drawer does. Matched as a HEX COLOUR rather
        # than as a bare "#", which also caught every `#id` selector in the
        # sheet and made the guard fire on scoping that is entirely correct.
        hexes = re.findall(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})\b", css)
        assert not hexes, f"tokenise these instead of hex-coding them: {hexes}"

    def test_each_option_says_which_case_it_answers(self) -> None:
        """ "Not here" has exactly two causes needing different work — the table
        is uncatalogued, or its source was never connected. Two links at
        opposite ends of a grey bar named the destinations and explained
        neither, leaving the admin to guess from the verb."""
        src = COMPONENT.read_text(encoding="utf-8")
        assert "pdw-pickfoot__q" in src
        foot = src.split("pdw-pickfoot__q", 1)[1].split("};", 1)[0]
        assert "Register a table" in foot and "Connect a source" in foot
        # Each option carries its own one-line explanation, not just a label.
        assert foot.count("pdw-pickfoot__hint") == 2
        assert "already connected" in foot, "the register option must name its case"
        assert "not connected at all" in foot, "the connect option must name its case"
        assert "draft is kept" in foot, "the connect option must say the work is not lost"
        css = COMPONENT_CSS.read_text(encoding="utf-8")
        # Real options, not text inside the shared notice box.
        assert "#pdw-picker .ag-note {" in css
        assert ".pdw-pickfoot__opt {" in css

    def test_a_registry_browser_gets_more_room_than_a_short_list(self) -> None:
        """The shared card is 620px, which suits a handful of skills. This one
        browses a project-bucket tree of hundreds of rows, and at 620px it
        showed three of them above its own foot. Scoped to this picker's own
        mount so the other builders keep the size that fits them."""
        css = COMPONENT_CSS.read_text(encoding="utf-8")
        assert "#pdw-picker .ag-pick {" in css
        card = css.split("#pdw-picker .ag-pick {", 1)[1].split("}", 1)[0]
        assert "max-width: 880px" in card
        assert ".ag-pick { max-width" not in css, "must not widen every builder's picker"


class TestThePickerTreeIsLegible:
    def test_the_rows_do_not_inherit_their_box_from_a_parent_they_no_longer_have(self) -> None:
        """A legacy global in the style-custom.css import tree forces
        `display:block` onto these <label> rows — disable that sheet and they
        snap back to flex. The component's own `.pdw-tables__row` lost the
        cascade to it, so a row rendered as a checkbox on one line and its
        name on the next. Scoping to `.ag-pick` wins, and every box property
        is stated rather than inherited from the `.pdw-tables` parent the
        picker copy does not have."""
        css = COMPONENT_CSS.read_text(encoding="utf-8")
        block = css.split(".ag-pick .pdw-tables__row {", 1)[1].split("}", 1)[0]
        for prop in ("display: flex", "align-items: center", "padding:", "margin: 0"):
            assert prop in block, f"the picker row must state {prop} itself"
        # `flex: 1` on the label column is load-bearing: with `min-width: 0`
        # and no basis it shrinks to nothing and the name wraps onto three lines.
        label_col = css.split(".ag-pick .pdw-tables__g {", 1)[1].split("}", 1)[0]
        assert "flex: 1" in label_col

    def test_a_subtitle_does_not_say_the_same_word_twice(self) -> None:
        """For an internal table the source type and the query mode are the
        same word, and "internal · internal" reads as a rendering bug. The
        panel deduped this; the picker kept showing it."""
        src = COMPONENT.read_text(encoding="utf-8")
        rows = src.split("function pickerRowsHtml()", 1)[1].split("function pickerControlsHtml", 1)[0]
        assert "seenSub" in rows, "the picker's row subtitle must dedupe like the panel's does"

    def test_the_bar_is_the_products_own_toolbar(self) -> None:
        """Three private versions of this control preceded it, each wrong in
        its own way — a bare substring box, a strip that vanished on a uniform
        registry, and a lone right-aligned dropdown over three rows. The
        product already has one filter toolbar, driving /library, /chats and
        three admin pages, so a reader who has filtered the Library knows this
        one: search, a Filter button whose menu groups the categories, one
        chip per applied category, sort inline."""
        src = COMPONENT.read_text(encoding="utf-8")
        assert "function pickerBarHtml()" in src
        bar = src.split("function pickerBarHtml()", 1)[1].split("function pickerHtml", 1)[0]
        for cls in ("fbar__search", "fbar-filter__btn", "fbar-menu--cats", "fbar-chips"):
            assert cls in bar, f"the bar must use the shared {cls}"
        # The private strip and its stylesheet are gone together.
        assert "pdw-pickctl__k--sort" not in src
        assert "function pickerControlsHtml" not in src
        # With nothing to slice the button is disabled, not absent — the same
        # rule /library follows, so the bar keeps its shape either way.
        assert "disabled aria-disabled=" in bar

    def test_one_filter_narrows_on_every_instance(self) -> None:
        """Source and query mode only narrow when an instance happens to have
        two of them, and "in no package" only when some are packaged. "What I
        have already ticked" narrows whenever anything is ticked — the case
        that matters in a long tree where your picks sit in collapsed groups.
        It rides the bar as its own pressed-state button, the design system's
        pattern for the one refinement worth seeing at rest."""
        src = COMPONENT.read_text(encoding="utf-8")
        cfg = src.split("window.FilterToolbar.init(", 1)[1].split("});", 1)[0]
        assert "key: 'selected'" in cfg and "attr: 'data-selected'" in cfg
        assert "control: '#pdw-picked-toggle'" in cfg, "it belongs on the bar, not in the menu"
        assert "toggle: true" in cfg
        # Rendered from the start rather than built by the first tick — see
        # the test below for why that matters.
        assert "id=\"pdw-picked-toggle\"" in src
        assert "data-selected=\"' + (on ? 'yes' : 'no') + '\"" in src

    def test_a_tick_re_renders_nothing(self) -> None:
        """This is what keeps the picker usable while filtered. Replacing the
        rows leaves the engine holding detached nodes, so its next filter does
        nothing; rebuilding the bar re-mounts the engine, which starts with no
        facets selected. Either one silently drops the reader's filter and
        search the moment they tick a box — measured: filter to two of nine,
        tick one, and all nine come back with the badge cleared."""
        src = COMPONENT.read_text(encoding="utf-8")
        assert "function absorbTick()" in src
        change = src.split("els.picker.addEventListener('change'", 1)[1].split("els.tables.addEventListener", 1)[0]
        assert change.count("absorbTick();") >= 2, (
            "both the single tick and the bulk group tick must absorb in place"
        )
        assert "renderPickerRows()" not in change, "a tick must not re-render the rows"
        assert "renderPickerBar" not in src, "nor rebuild the bar"
        # The toggle exists from the start, so no tick has to create it.
        assert "(pickedN ? '' : ' hidden')" in src

    def test_a_group_tick_only_takes_what_is_visible(self) -> None:
        """Under a filter the group box means "the rows I can see". Ticking a
        Keboola group while filtered to live-query tables must not silently
        add the synced ones you had just filtered away."""
        src = COMPONENT.read_text(encoding="utf-8")
        change = src.split("els.picker.addEventListener('change'", 1)[1].split("els.tables.addEventListener", 1)[0]
        assert "return !row || !row.hidden;" in change

    def test_a_bucket_tier_that_only_repeats_its_project_is_collapsed(self) -> None:
        """The internal source rendered as "Agnes internal › Agnes Internal ›
        3 tables" — a tier costing a click and a line and carrying nothing.
        Only when the labels are the same word: a single bucket with a name of
        its own (project "crm", bucket "in.c-crm") is real information."""
        src = COMPONENT.read_text(encoding="utf-8")
        rows = src.split("function pickerRowsHtml()", 1)[1].split("function pickerControlsHtml", 1)[0]
        assert "var flat = p.order.length === 1 &&" in rows
        assert "if (flat) return rows;" in rows


class TestTheEmptyStatesStoppedLying:
    def test_a_search_miss_is_not_the_same_as_an_empty_registry(self) -> None:
        """ "No table matches that" over a registry holding nothing is a
        broken-search message for a search nobody ran — it sends the admin
        hunting for a filter to clear that does not exist."""
        src = COMPONENT.read_text(encoding="utf-8")
        rows = src.split("function pickerRowsHtml()", 1)[1].split("function pickerControlsHtml", 1)[0]
        assert "No table matches that." in rows
        assert "st.registry && st.registry.length" in rows, (
            "the miss message must be gated on the registry actually holding something"
        )
        assert "No data source is connected" in rows

    def test_the_state_is_read_off_the_registry_not_the_connection_count(self) -> None:
        """An instance with no source connected still has its internal tables
        registered and perfectly packageable, so "nothing a package can carry"
        keyed on connections alone was false on every fresh instance."""
        src = COMPONENT.read_text(encoding="utf-8")
        note = src.split("function emptyRegistryNoteHtml()", 1)[1].split("function renderTables()", 1)[0]
        assert "(st.registry || []).length || (st.connections || []).length" in note, (
            "the note needs an empty registry AND no connections before it claims there is nothing to carry"
        )

    def test_the_propose_invitation_needs_something_to_propose_from(self) -> None:
        src = COMPONENT.read_text(encoding="utf-8")
        tables = src.split("function renderTables()", 1)[1].split("function reachCount", 1)[0]
        assert "body = (st.registry || []).length" in tables
        assert "No source to draw from yet." in tables


class TestTheDraftSurvivesTheDetourAndNothingElse:
    def test_it_is_parked_before_the_navigation_not_after(self) -> None:
        src = COMPONENT.read_text(encoding="utf-8")
        leave = src.split("function leaveToConnect()", 1)[1].split("\n  }", 1)[0]
        assert leave.index("persistDraft('connect')") < leave.index("window.location.href"), (
            "the draft is the only thing holding what was typed — park it before leaving"
        )
        assert "from=package-builder" in leave

    def test_only_a_detour_draft_comes_back(self) -> None:
        src = COMPONENT.read_text(encoding="utf-8")
        assert "var DRAFT_KEY = 'agnes_pkg_builder_draft_v1';" in src
        read = src.split("function readDraft()", 1)[1].split("function discardDraft", 1)[0]
        assert "d.reason === 'connect'" in read, (
            "a builder abandoned last week must not resurrect itself over a fresh start"
        )

    def test_an_edit_never_parks_one(self) -> None:
        src = COMPONENT.read_text(encoding="utf-8")
        persist = src.split("function persistDraft(reason)", 1)[1].split("function readDraft", 1)[0]
        assert "st.mode !== 'create'" in persist
        # …and only a create even looks for one.
        assert "pendingDraft: mode === 'create' ? readDraft() : null," in src

    def test_the_hand_back_diffs_the_registry_rather_than_trusting_the_wizard(self) -> None:
        """The wizard hands nothing over. The builder works out what is new by
        comparing the live registry against the ids it saw before it left, so a
        connector added later is carried for free and the two surfaces cannot
        fall out of step."""
        src = COMPONENT.read_text(encoding="utf-8")
        assert "knownTables:" in src
        apply = src.split("function applyDraft(d)", 1)[1].split("function leaveToConnect", 1)[0]
        assert "d.knownTables" in apply
        assert "fresh.forEach(function (t) { st.tablesSelected.add(t.id); });" in apply
        # The trip is reported, so a silently-changed table set is not a surprise.
        assert "st.returnNote" in apply

    def test_it_dies_when_the_package_becomes_real(self) -> None:
        src = COMPONENT.read_text(encoding="utf-8")
        assert "discardDraft();" in src.split("Promise.allSettled(tableCalls),", 1)[1], (
            "a draft outliving its own create offers to build a duplicate"
        )
        # Applying one consumes it too, so a reload does not re-apply it.
        assert "st.pendingDraft = null;\n        discardDraft();" in src

    def test_cancel_still_means_cancel(self) -> None:
        """Back is the admin saying they are done with this package. Restoring
        a draft over that is a builder arguing with the person using it."""
        src = COMPONENT.read_text(encoding="utf-8")
        back = src.split("if (e.target.closest('[data-ag-back]'))", 1)[1].split("return;", 1)[0]
        assert "readDraft" not in back and "persistDraft" not in back


class TestTheSourcesPageHoldsTheOtherEndOfTheTrip:
    def test_it_offers_the_way_back(self, seeded_app) -> None:
        c = seeded_app["client"]
        html = c.get("/admin/data-sources", headers=_auth(seeded_app["admin_token"])).text
        assert 'id="ds-from-package"' in html
        assert 'href="/admin/data-packages/new"' in html
        # Hidden until the query string says you came from there.
        strip = html.split('id="ds-from-package"', 1)[1].split("</div>", 1)[0]
        assert "hidden" in strip

    def test_the_param_only_toggles_a_constant_href(self) -> None:
        """`from` is untrusted request input. It is compared against a literal
        and the destination is hard-coded, so it cannot steer a navigation the
        way an interpolated value could."""
        page = _ds_page_source.page_source()
        assert '_params.get("from") === "package-builder"' in page
        assert '"/admin/data-packages/new"' in page

    def test_the_wizard_stops_after_choosing_tables_on_that_entry(self) -> None:
        page = _ds_page_source.page_source()
        assert "let _fromPackageBuilder = false;" in page
        assert "_fromPackageBuilder = true;" in page
        # Bundle and Share BUILD a package; they are not offered to someone
        # already building one.
        assert "if (Number(el.dataset.wstep) > 2) el.hidden = true;" in page
        # …and the primary names where the tables are actually going.
        assert '"Add selected tables to your package"' in page
        # The step-2 handler returns instead of walking on to the board.
        register = page.split('getElementById("ds-wizard-register-btn").addEventListener', 1)[1]
        hand_back = register.index("if (_fromPackageBuilder)")
        seeds_board = register.index("_seedBoard(")
        assert hand_back < seeds_board, "the hand-back must come before the bundling path"

    def test_the_builder_opens_the_wizard_over_itself(self) -> None:
        """The whole point of lifting the wizard out of admin_data_sources:
        "Connect a source" answers in place instead of navigating. The compact
        drawer on /admin/tables cannot stack another overlay, so it passes no
        hook and keeps the navigation — the host decides."""
        builder = (TEMPLATES / "admin_package_builder.html").read_text(encoding="utf-8")
        assert '{% include "_add_data_wizard.html" %}' in builder
        for script in (
            "js/admin/data_sources_page.js",
            "js/admin/data_sources_sharepoint_wizard.js",
            "js/admin/data_sources_extraction_observability.js",
            "js/admin/data_sources_anon_preview.js",
        ):
            assert script in builder, script
        assert "css/ds_page.css" in builder
        assert "window.DS_BOOT" in builder, "it must hand over the values that page renders"
        assert "openConnectWizard: function (onRegistered)" in builder
        src = COMPONENT.read_text(encoding="utf-8")
        leave = src.split("function leaveToConnect()", 1)[1].split("\n  }", 1)[0]
        assert "st.openConnectWizard" in leave
        assert "persistDraft('connect')" in leave, "the navigation stays as the fallback"
