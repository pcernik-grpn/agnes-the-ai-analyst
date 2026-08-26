"""/skills hosts the same builder shell as /agents.

Two builders that look and behave differently are two products. This page was
a step-wise form with a card preview beside it; it now renders the shared
shell — full-bleed two panes, a Preview tab on the left, the numbered
configuration on the right — from `builder_shell.js` and `builder.css`, the
same artifacts `/agents` renders from.

What this file guards is that it keeps DOING that: the value of a shared
shell is lost the moment one page quietly forks a copy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "app" / "web" / "templates" / "skills.html"
AGENTS = ROOT / "app" / "web" / "templates" / "agents.html"
SHELL_JS = ROOT / "app" / "web" / "static" / "js" / "components" / "builder_shell.js"


@pytest.fixture(scope="module")
def markup() -> str:
    return SKILLS.read_text(encoding="utf-8")


class TestItRendersTheSharedShell:
    def test_it_loads_the_shared_sheet_and_module(self, markup):
        assert "css/builder.css" in markup
        assert "js/components/builder_shell.js" in markup

    def test_the_module_is_not_deferred(self, markup):
        """The page renders on boot; a deferred BuilderShell is undefined at
        first paint."""
        tag = re.search(r"<script src=\"\{\{ static_url\('js/components/builder_shell\.js'\) \}\}\"[^>]*>", markup)
        assert tag, "builder_shell.js is not loaded"
        assert "defer" not in tag.group(0)

    @pytest.mark.parametrize("fn", ["head", "workspace", "tabs", "section"])
    def test_the_chrome_comes_from_the_shell(self, markup, fn):
        assert f"BuilderShell.{fn}(" in markup, f"{fn} is being hand-rolled instead of shared"

    def test_the_page_does_not_keep_its_own_escaper(self, markup):
        """One escaper across both builder pages — see builder_shell.js."""
        assert "var esc = BuilderShell.esc;" in markup
        assert "function esc(s)" not in markup

    def test_only_the_type_step_keeps_bespoke_section_markup(self, markup):
        """`sk-sec-head` was this page's private copy of the section
        component. The three real configuration sections use the shared one
        now; Type is deliberately left alone, because it is not an editable
        section — it is a decision already made, showing a ✓ and a Change
        button, and it has no collapsed/expanded pair to model.

        Scoped to `typeSectionHtml` rather than deleted outright so the guard
        still fires if the fork spreads back into the other sections.
        """
        block = re.search(r"function typeSectionHtml\(\) \{(.*?)\n  \}", markup, re.S)
        assert block, "typeSectionHtml not found"
        outside = markup.replace(block.group(1), "")
        for dead in ('class="sk-sec-head"', 'class="sk-sec-no"'):
            assert dead not in outside, (
                f"{dead} is back outside the Type step — the section component has been re-forked"
            )


class TestItBehavesLikeTheOtherBuilder:
    def test_it_breaks_out_of_the_index_column_only_while_building(self, markup):
        """The type picker is a document and keeps the centred column; once a
        type is chosen the page is a workspace."""
        assert re.search(r"classList\.toggle\('ag-building', !!type\)", markup)

    def test_collapsing_a_section_does_not_rebuild_the_panel(self, markup):
        """Same bug /agents had: a rebuild throws away the panel's scroll
        position and the caret in whatever field has focus."""
        branch = re.search(r"t\.hasAttribute\('data-ag-toggle-sec'\)\) \{(.*?)\n    \}", markup, re.S)
        assert branch, "no data-ag-toggle-sec branch"
        assert "classList.toggle('collapsed'" in branch.group(1)

    def test_the_toggle_is_actually_wired_to_the_delegated_handler(self, markup):
        """A branch the selector never matches is dead code — exactly how the
        Save button shipped broken on /agents."""
        sel = re.search(r"var t = e\.target\.closest\((.*?)\);", markup, re.S)
        assert sel and "[data-ag-toggle-sec]" in sel.group(1)
        assert "[data-ag-back]" in sel.group(1)

    def test_collapsed_summaries_track_the_fields_under_them(self, markup):
        """A folded section shows only its summary; one that still reads
        "Unnamed" after you typed a name is worse than no summary."""
        block = re.search(r"function syncPreview\(\) \{(.*?)\n  \}", markup, re.S)
        assert block, "syncPreview not found"
        assert '[data-sec="identity"] .ag-sec-sum' in block.group(1)

    def test_leaving_does_not_claim_a_loss_that_does_not_happen(self, markup):
        """/agents confirms on leave because it holds the only copy. This page
        writes every keystroke to localStorage, so the draft survives — a
        "changes will be discarded" dialog here would be false, and false
        dialogs are how people learn to click through the real ones.

        The deliberate ASYMMETRY is the thing under test: if someone later
        adds a confirm here, they should have to change this test and read
        why first.
        """
        branch = re.search(r"t\.hasAttribute\('data-ag-back'\)\) \{(.*?)\n    \}", markup, re.S)
        assert branch, "no data-ag-back branch — the shell's back button does nothing"
        body = branch.group(1)
        assert "/library" in body
        assert "confirmModal" not in body and "window.confirm" not in body


class TestTheTwoBuildersStayOneProduct:
    def test_both_pages_render_from_the_same_shell(self):
        agents = AGENTS.read_text(encoding="utf-8")
        skills = SKILLS.read_text(encoding="utf-8")
        for page, text in (("agents.html", agents), ("skills.html", skills)):
            assert "BuilderShell.workspace({" in text, f"{page} no longer uses the shared workspace"
            assert "css/builder.css" in text, f"{page} no longer loads the shared sheet"

    def test_the_shell_exports_everything_both_pages_call(self):
        """A page calling a helper the module does not export is a TypeError at
        first paint, and neither page's markup tests would catch it."""
        shell = SHELL_JS.read_text(encoding="utf-8")
        exported = set(re.findall(r"^    (\w+): \w+,$", shell, re.M))
        assert exported, "could not read BuilderShell's export table"
        called = set()
        for text in (AGENTS.read_text(encoding="utf-8"), SKILLS.read_text(encoding="utf-8")):
            called |= set(re.findall(r"BuilderShell\.(\w+)\b", text))
        missing = called - exported
        assert not missing, f"called but not exported by BuilderShell: {sorted(missing)}"
