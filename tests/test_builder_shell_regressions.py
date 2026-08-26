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
        assert "backLabel: 'Library'" in drawer

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
        line = re.search(r".*\[data-ag-back\].*", drawer)
        assert line, "the shell's back button is not handled — it would do nothing"
        assert "close()" in line.group(0)
        assert "confirmModal" not in drawer
