"""Three bugs the builders shipped with, and the shape of each.

All three are the same class of mistake in different clothes: something that
is rendered once and then partially updated, or sized against a number that
was only ever right for one page.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "app" / "web" / "templates" / "skills.html"
DRAWER = ROOT / "app" / "web" / "static" / "js" / "components" / "package_drawer.js"
PAGE = ROOT / "app" / "web" / "templates" / "admin_package_builder.html"
LIBRARY = ROOT / "app" / "web" / "templates" / "library.html"
CSS = ROOT / "app" / "web" / "static" / "css" / "builder.css"


@pytest.fixture(scope="module")
def skills() -> str:
    return SKILLS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def drawer() -> str:
    return DRAWER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


class TestTheTabStripTracksTheOpenTab:
    def test_the_left_pane_rebuilds_its_tabs(self, skills):
        """The strip is part of the pane, not chrome above it. Keeping it and
        replacing only what follows left `aria-selected` on whichever tab was
        active at first paint — Preview's pane rendered under a Create tab
        still styled as selected."""
        block = re.search(r"function renderLeftPane\(\) \{(.*?)\n  \}", skills, re.S)
        assert block, "renderLeftPane not found"
        body = block.group(1)
        assert "tabsHtml()" in body, "the tab strip is not re-rendered with the pane"
        assert "tabs.nextSibling" not in body, "the old keep-the-strip approach is back"

    def test_there_is_one_definition_of_the_tabs(self, skills):
        """Two copies is how the strip and the pane disagree in the first
        place."""
        assert skills.count("id: 'create', label: 'Create'") == 1


class TestTheWorkspaceFillsTheWindow:
    def test_it_does_not_subtract_a_guessed_chrome_height(self, css):
        """`height: calc(100vh - 150px)` was tuned to ONE page's header. On
        the other builder the header is 70px, so the workspace stopped short
        and left a band of dead white under it — and either header rewrapping
        would have broken both."""
        rule = re.search(r"\.ag-work \{([^}]*)\}", css)
        assert rule, ".ag-work rule not found"
        body = rule.group(1)
        assert "100vh" not in body, "the workspace is sized against a guessed chrome height again"
        assert "flex: 1" in body

    def test_the_column_above_it_flexes_all_the_way_down(self, css):
        """Flexing the workspace only works if every ancestor does too."""
        for sel in (
            "body.ag-building .idx ",
            "body.ag-building .idx-band ",
            "body.ag-building .idx-band-inner ",
        ):
            assert sel in css, f"{sel.strip()} is not part of the flex column"
        assert "#sk-builder-view" in css and "#ag-builder-view" in css

    def test_the_stacked_layout_can_still_scroll(self, css):
        """Pinning the shell to the viewport is right only while the panes sit
        side by side; stacked, it would trap the lower one."""
        assert re.search(r"@media \(max-width: 900px\) \{\s*[^}]*body\.ag-building \.idx \{[^}]*height: auto", css, re.S)


class TestTheDrawerWearsOneHeaderAtATime:
    def test_the_builder_uses_the_shell_header(self, drawer):
        """Back on the left, the verb that commits on the right — the same
        header every other builder has. A workspace whose primary action sits
        in a footer under a scrolling form reads as a dialog, and the action
        leaves the screen as soon as the form is long enough to scroll."""
        assert "BuilderShell.head({" in drawer
        # The label is configurable now (the page and the drawer can name
        # different destinations), but it still defaults to Library.
        assert "backLabel: (st && st.backLabel) || 'Library'" in drawer

    def test_the_commit_button_is_moved_on_every_open_not_once(self, drawer):
        """One node, one DOM, two sizes. Placed once at build, the button
        stayed in the shell header — and the next compact open showed a footer
        with nothing in it but Cancel."""
        block = re.search(r"function placeSubmit\(\) \{(.*?)\n  \}", drawer, re.S)
        assert block, "placeSubmit not found"
        body = block.group(1)
        assert "st.builder" in body and "els.foot" in body
        # ...and it is actually called from open(), not just defined.
        assert re.search(r"placeSubmit\(\);", drawer)

    def test_each_header_is_hidden_in_the_other_mode(self, css):
        assert ".ds-drawer--builder .ds-drawer__head" in css
        assert ".ds-drawer--builder .ds-drawer__foot" in css
        assert ".ds-drawer:not(.ds-drawer--builder) .ag-build-head" in css

    def test_back_closes_without_pretending_there_is_something_to_lose(self, drawer):
        """The package does not exist until Create and there is no draft store
        here, so leaving costs nothing — a confirmation would be theatre."""
        block = re.search(r"\[data-ag-back\]'\)\) \{(.*?)\n      \}", drawer, re.S)
        assert block, "the shell's back button is not handled — it would do nothing"
        body = block.group(1)
        # As a page it navigates; as an overlay it closes. Neither confirms.
        assert "st.backHref" in body and "close()" in body
        assert "confirmModal" not in drawer


class TestThePackageBuilderIsAPage:
    """A grown drawer is still an overlay. Authoring a package is not a detour
    from another page — it is the thing you came to do — so it looks like the
    other two places you come to do the thing: rail, shell header, two panes.

    The drawer itself is unchanged and stays where it belongs: /admin/tables,
    opened mid-sentence while assigning a table to a package that does not
    exist yet. One component, two mountings, no second package form.
    """

    @pytest.fixture(scope="class")
    def page(self) -> str:
        return PAGE.read_text(encoding="utf-8")

    def test_the_page_reuses_the_drawer_component(self, page):
        """Not a second implementation of a package's fields and grants."""
        assert "package_drawer.js" in page
        assert "AgnesPackageDrawer.open(" in page
        assert "mount:" in page

    def test_the_page_loads_the_shared_shell(self, page):
        assert "builder_shell.js" in page and "css/builder.css" in page

    def test_the_page_head_is_empty_so_the_shell_header_is_the_only_title(self, page):
        block = re.search(r"\{% block page_head %\}(.*?)\{% endblock %\}", page, re.S)
        assert block, "page_head block missing"
        assert "<h1" not in block.group(1) and "<h2" not in block.group(1)

    def test_mounting_moves_the_root_not_just_the_panel(self, drawer):
        """Every rule that dresses this thing is scoped from the root
        (`.ds-drawer--builder .ds-drawer__head`). Relocating the panel alone
        left those selectors matching nothing, and the drawer arrived on the
        page wearing its overlay chrome and none of its builder chrome."""
        assert "st.mount.appendChild(els.root)" in drawer

    def test_a_page_cannot_be_closed_like_an_overlay(self, drawer):
        """There is no backdrop and no Escape to dismiss — leaving is a
        navigation, and close() would leave a blank page behind."""
        block = re.search(r"function close\(\) \{(.*?)\n  \}", drawer, re.S)
        assert block and "st.mount" in block.group(1)

    def test_the_back_label_and_destination_are_taken_together(self, drawer):
        """They are one promise. Split, the header says Library while the
        button goes somewhere else."""
        assert "backLabel: opts.backLabel" in drawer and "backHref: opts.backHref" in drawer

    def test_the_library_navigates_rather_than_opening_a_drawer(self):
        lib = LIBRARY.read_text(encoding="utf-8")
        block = re.search(r"data-new-package\][^;]*?\{(.*?)\}\)\);", lib, re.S)
        assert block, "the + New package handler moved"
        body = block.group(1)
        assert "/admin/data-packages/new" in body
        # A navigation gated on a script being loaded silently does nothing.
        assert "AgnesPackageDrawer)" not in body

    def test_the_head_band_is_fully_collapsed_while_building(self, css):
        """/agents hides the band outright, so only its bottom padding ever
        showed. A builder PAGE renders it empty, and its 32px top padding
        pushed the whole workspace down by exactly that much."""
        assert re.search(r"body\.ag-building \.idx-head \{ padding: 0; \}", css)


class TestThePageBuilderDoesNotPaintOverTheRail:
    def test_page_mode_gives_up_the_overlay_z_index(self, css):
        """An overlay sits above everything (drawer.css: z-index 1200). A page
        must not: the app rail is a fixed element at z-index 40, so a
        page-mounted drawer that kept 1200 paints OVER the sidebar wherever the
        two meet — during a rail expand/collapse transition, for instance,
        when the content offset and the rail width are briefly inconsistent.

        `position: static` does NOT neutralise this on its own: the root is a
        flex item, and z-index applies to flex items whether or not they are
        positioned. That is the trap this pins.
        """
        rule = re.search(r"\.ds-drawer\.is-page \{([^}]*)\}", css, re.S)
        assert rule, ".ds-drawer.is-page rule not found"
        body = rule.group(1)
        assert "z-index: auto" in body, "page mode is keeping the overlay stacking order"
        assert "position: static" in body
