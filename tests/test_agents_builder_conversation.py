"""The builder's left pane writes the right pane — and never replaces it.

The two-pane builder makes a conversation the primary way an agent gets
configured. That is only safe if the configuration stays the source of truth:
the panel is always hand-editable, nothing the assistant says can navigate
around the owner's own controls, and no edit — typed or spoken — reaches the
server until the owner presses Save.

These are markup-level contracts on ``agents.html`` (the page is a single
inline script, as the rest of this page's suites already assume). The
server-side half — what a turn is allowed to write — is
``tests/test_agent_builder_turns.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "agents.html"
BUILDER_CSS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "css" / "builder.css"
SHELL_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "components" / "builder_shell.js"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def builder_css() -> str:
    """The builder shell's rules, extracted out of the page's inline <style>
    so a second builder can load the same sheet. Assertions about LAYOUT read
    this; assertions about behaviour still read the page's script."""
    return BUILDER_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def shell_js() -> str:
    """The builder shell's markup, extracted so a second builder page renders
    the same thing. Assertions about what the SHELL emits read this; the
    page's own script is still `markup`."""
    return SHELL_JS.read_text(encoding="utf-8")


class TestTheConversationWritesTheConfiguration:
    def test_a_turn_posts_to_the_builder_endpoint(self, markup):
        assert "/builder/turn" in markup

    def test_a_turn_does_not_write_to_the_server(self, markup):
        """Editing is explicit: a turn returns its patch and the page merges it
        into the unsaved working copy, so "leave without saving" really does
        discard what the conversation said. Were the turn to apply, the
        conversation would be a back door around Save.

        This replaces an earlier contract ("re-render from the server's row,
        never the patch"), which existed so the panel could never show a scope
        the agent did not have. That guarantee is not lost, only moved: an
        unsaved patch grants nothing, and Save re-derives the enforced scope
        server-side — see test_saving_reflects_the_servers_row.
        """
        block = re.search(r"function sendBuilderTurn\(text\) \{(.*?)\n  \}\n", markup, re.S)
        assert block, "sendBuilderTurn not found"
        body = block.group(1)
        assert "apply: false" in body, "the turn is writing to the server again"
        assert "config: agentPayload(a)" in body, (
            "the turn is not given the unsaved working copy, so it reasons "
            "about the last-saved configuration instead of the one on screen"
        )
        assert "touch();" in body, "a conversation edit does not mark the agent dirty"

    def test_saving_reflects_the_servers_row(self, markup):
        """Save is the only writer, and what comes back is what the agent
        actually is — including the slug a rename re-derives server-side."""
        block = re.search(r"function saveAgent\(\) \{(.*?)\n  \}", markup, re.S)
        assert block, "saveAgent not found"
        body = block.group(1)
        # PUT, not PATCH: the save re-pointed to `/api/v1/agents/{id}` when
        # this page's own `/api/agents` CRUD was deleted (Task C1.2).
        assert "method: 'PUT'" in body
        assert "/api/v1/agents/" in body
        assert "if (updated && updated.slug) a.slug = updated.slug;" in body
        assert "setBaseline();" in body, "a successful save does not reset the dirty baseline"

    def test_the_history_sent_excludes_the_message_being_answered(self, markup):
        """`conv` already has the new turn pushed onto it; sending it whole
        would show the model the same message twice."""
        assert "history: conv.slice(0, -1)" in markup

    def test_capability_choices_are_confined_to_what_the_picker_offers(self, markup):
        assert "plugin_candidates:" in markup

    def test_the_owner_can_still_edit_every_field_by_hand(self, markup):
        """The panel is never disabled while the assistant is talking — the
        conversation is an accelerator, not a gate."""
        # The claim, not the sentence carrying it: the panel must advertise
        # hand-editing. Pinning the full lede made a copy edit a test failure.
        assert "editable by hand" in markup
        cfg = re.search(r"function cfgBodyHtml\(a\) \{(.*?)\n  \}", markup, re.S)
        assert cfg, "cfgBodyHtml not found"
        assert "disabled" not in cfg.group(1)


class TestPaneLifecycle:
    def test_switching_agents_resets_the_conversation_and_socket(self, markup):
        """A transcript or a live socket carried into the next builder would
        show one agent's conversation under another agent's name."""
        assert "function resetBuilderPanes(" in markup
        assert re.search(r"if \(currentId !== id\) resetBuilderPanes\(\);", markup)

    def test_leaving_the_builder_closes_the_preview_socket(self, markup):
        block = re.search(r"function closeBuilder\(\) \{(.*?)\}", markup, re.S)
        assert block and "resetBuilderPanes()" in block.group(1)

    def test_a_half_typed_message_survives_a_tab_switch(self, markup):
        assert "convDraft = live.value" in markup

    def test_a_half_typed_message_survives_a_config_rerender(self, markup):
        """The config pane re-renders whenever a turn lands; a draft that only
        lived in the textarea would disappear mid-sentence."""
        assert re.search(r"data-ag-comp'\);\s*\n\s*if \(comp\) \{", markup)

    def test_only_the_changed_pane_rerenders(self, markup):
        """A full renderBuilder per streamed token would pull the caret out of
        whichever composer the owner is typing in."""
        assert "function renderConvPane(" in markup
        assert "function renderCfgPane(" in markup


class TestThePanelShowsOnlyWhatIsConnected:
    """The configuration panel states what the agent IS, not what it could be.

    It used to render the caller's whole reachable pool — every data source,
    every plugin — as a toggle list, which made the panel a form to fill in
    and buried the three things actually attached among fifty that were not.
    The pool now lives behind a picker the "+" opens, so the conversation is
    the obvious way in and the by-hand path is still one click deep.
    """

    def test_the_panel_lists_attached_items_not_the_whole_pool(self, markup):
        for fn, field in (("knowledgeBody", "a.knowledge"), ("capabilitiesBody", "a.plugins")):
            block = re.search(r"function " + fn + r"\(a\) \{(.*?)\n  \}", markup, re.S)
            assert block, f"{fn} not found"
            body = block.group(1)
            assert f"{field}.indexOf" in body, (
                f"{fn} no longer filters the pool down to what is attached — "
                "the panel is showing the whole catalogue again"
            )

    def test_neither_section_renders_the_pool_search_inline(self, markup):
        """The search box belongs to the picker. Inline, it implied the panel
        was a place to go shopping."""
        for fn in ("knowledgeBody", "capabilitiesBody"):
            block = re.search(r"function " + fn + r"\(a\) \{(.*?)\n  \}", markup, re.S)
            assert "toolbar(" not in block.group(1), f"{fn} renders the pool toolbar inline"

    def test_each_pooled_section_offers_a_manual_add(self, markup):
        for key in ("knowledge", "capabilities"):
            assert f"section('{key}'" in markup
            assert "data-ag-add=\"' + addKey + '\"" in markup or f'data-ag-add="{key}"' in markup

    def test_the_picker_is_the_shared_modal_not_a_private_overlay(self, markup, shell_js):
        """The chrome moved into the shell; the page must still reach for it
        rather than rolling a private overlay of its own."""
        block = re.search(r"function picker\(o\) \{(.*?)\n  \}", shell_js, re.S)
        assert block, "BuilderShell.picker not found"
        assert "modal-backdrop" in block.group(1) and "modal-card" in block.group(1)
        assert "BuilderShell.picker({" in markup

    def test_selecting_in_the_picker_does_not_tear_the_picker_down(self, markup):
        """A full renderBuilder would rebuild #ag-picker under the pointer,
        closing the modal after every single pick."""
        block = re.search(r"function afterIngredientChange\(\) \{(.*?)\n  \}", markup, re.S)
        assert block, "afterIngredientChange not found"
        body = block.group(1)
        assert "if (picker)" in body and "renderPickerRows()" in body
        # ...and both toggles route through it rather than re-rendering directly.
        for attr in ("data-ag-kn", "data-ag-cap"):
            handler = re.search(r"t\.hasAttribute\('" + attr + r"'\) && a\) \{(.*?)\n    \}", markup, re.S)
            assert handler and "afterIngredientChange()" in handler.group(1), (
                f"the {attr} toggle does not go through afterIngredientChange"
            )

    def test_an_attached_id_the_caller_cannot_see_is_still_shown(self, markup):
        """A source granted at build time can leave the caller's scope later.
        Dropping it silently would make the panel disagree with the agent."""
        assert "function unknownRow(" in markup
        for fn in ("knowledgeBody", "capabilitiesBody"):
            block = re.search(r"function " + fn + r"\(a\) \{(.*?)\n  \}", markup, re.S)
            assert "unknownRow(" in block.group(1), f"{fn} drops unresolvable ids"

    def test_escape_closes_the_picker(self, markup):
        assert re.search(r"e\.key === 'Escape' && picker", markup)


class TestTheBuilderIsFullBleed:
    def test_the_builder_breaks_out_of_the_index_column(self, markup, builder_css):
        """The workspace is not a document: a card inside the shell's centred
        column spent most of a wide screen on gutters."""
        assert "body.ag-building .idx-band-inner" in builder_css
        # The rule is inert without the page toggling the class.
        assert re.search(r"classList\.toggle\('ag-building'", markup)

    def test_the_conversation_keeps_a_readable_measure(self, builder_css):
        """Full-bleed panes must not turn prose into 1200px-wide lines."""
        assert ".ag-conv-in" in builder_css and "max-width: 780px" in builder_css

    def test_the_two_panes_split_the_width_evenly(self, builder_css):
        """Conversation and configuration are equal partners — the panel is
        the source of truth, not a sidebar summarising the chat."""
        rule = re.search(r"\.ag-work \{([^}]*)\}", builder_css)
        assert rule, ".ag-work rule not found"
        assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in rule.group(1)


class TestCollapsingASectionKeepsYourPlace:
    def test_the_toggle_does_not_rebuild_the_panel(self, markup):
        """Every section's body is always in the DOM — `.ag-sec.collapsed`
        only hides it — so opening one never needed a re-render. Rebuilding
        threw away ag-cfg-body's scrollTop, snapping the panel back to the top
        every time you opened section 5.
        """
        branch = re.search(r"t\.hasAttribute\('data-ag-toggle-sec'\)\) \{(.*?)\n    \} else if", markup, re.S)
        assert branch, "the data-ag-toggle-sec branch moved"
        body = branch.group(1)
        assert "classList.toggle('collapsed'" in body, "the toggle no longer flips in place"
        # A renderBuilder() is allowed ONLY as the not-found fallback.
        assert re.search(r"\} else \{\s*\n\s*renderBuilder\(\);", body), (
            "renderBuilder is called outside the section-not-found fallback — "
            "the scroll position is being thrown away again"
        )

    def test_the_body_is_rendered_even_when_collapsed(self, builder_css, shell_js):
        """The in-place toggle is only correct because of this rule; if the
        body were conditionally rendered, opening a section would show nothing.
        """
        assert ".ag-sec.collapsed .ag-sec-body { display: none; }" in builder_css
        sec = re.search(r"function section\(o\) \{(.*?)\n  \}", shell_js, re.S)
        assert sec, "BuilderShell.section not found"
        assert "ag-sec-body" in sec.group(1), "the shell no longer always emits the body"


class TestEditingIsExplicit:
    """Nothing reaches the server until the owner says so.

    The builder used to debounce-PATCH every keystroke. That made "leave
    without saving" impossible to offer honestly — there was nothing unsaved
    to leave — and never gave the owner a moment at which they had decided the
    agent was as they wanted it.
    """

    def test_there_is_no_autosave(self, markup):
        assert "saveTimers" not in markup, "the debounced autosave is back"
        assert "function persist(" not in markup

    def test_a_field_edit_only_marks_dirty(self, markup):
        """Typing must not write, and must not re-render the panel it is being
        typed into — only the header's Save state changes."""
        block = re.search(r"function touch\(\) \{(.*?)\n  \}", markup, re.S)
        assert block, "touch not found"
        body = block.group(1)
        assert "fetch(" not in body, "a keystroke is hitting the API"
        assert "syncHeaderState()" in body

    def test_dirtiness_is_measured_against_the_last_agreement_with_the_server(self, markup):
        assert "function setBaseline()" in markup
        assert re.search(r"function isDirty\(\) \{.*?snapshot\(a\) !== baseline", markup, re.S)
        # Opening an agent takes the snapshot everything is compared against.
        opened = re.search(r"function openBuilder\(id, opts\) \{(.*?)\n  \}", markup, re.S)
        assert opened and "setBaseline();" in opened.group(1)


class TestLeavingTheBuilderAsksFirst:
    def test_a_new_agent_is_discarded_and_deleted(self, markup):
        """The placeholder row exists server-side because the conversation and
        the preview both address the agent by id — so abandoning the attempt
        has to remove it, or every false start litters the list."""
        block = re.search(r"function leaveBuilder\(\) \{(.*?)\n  \}", markup, re.S)
        assert block, "leaveBuilder not found"
        body = block.group(1)
        assert "if (isNewAgent)" in body and "discardNewAgent(a)" in body
        disc = re.search(r"function discardNewAgent\(a\) \{(.*?)\n  \}", markup, re.S)
        assert disc and "method: 'DELETE'" in disc.group(1)

    def test_a_failed_discard_puts_the_row_back(self, markup):
        """Otherwise the list denies the existence of an agent the server
        still has."""
        disc = re.search(r"function discardNewAgent\(a\) \{(.*?)\n  \}", markup, re.S)
        assert "agents.push(a); renderList();" in disc.group(1)

    def test_an_edited_existing_agent_is_rolled_back(self, markup):
        block = re.search(r"function leaveBuilder\(\) \{(.*?)\n  \}", markup, re.S)
        body = block.group(1)
        assert "revertToBaseline(a)" in body
        rev = re.search(r"function revertToBaseline\(a\) \{(.*?)\n  \}", markup, re.S)
        assert rev and "JSON.parse(baseline)" in rev.group(1)

    def test_a_clean_agent_leaves_without_a_dialog(self, markup):
        """Confirming when there is nothing to lose trains people to click
        through the dialog that matters."""
        block = re.search(r"function leaveBuilder\(\) \{(.*?)\n  \}", markup, re.S)
        assert "if (!isDirty()) { closeBuilder(); return; }" in block.group(1)

    def test_the_dialog_is_the_shared_modal_not_a_native_confirm(self, markup):
        block = re.search(r"function leaveBuilder\(\) \{(.*?)\n  \}", markup, re.S)
        body = block.group(1)
        assert body.count("confirmModal(") == 2
        assert "window.confirm" not in body

    def test_closing_a_tab_mid_edit_still_warns(self, markup):
        """Our own dialog cannot intercept a nav click, a reload, or a closed
        tab; the browser's generic prompt is the only thing available there."""
        assert re.search(r"addEventListener\('beforeunload'", markup)
        assert "function hasPendingWork()" in markup
        # A never-saved agent counts as pending even before it is touched:
        # leaving throws the whole row away.
        assert re.search(r"function hasPendingWork\(\).*?isNewAgent \|\| isDirty\(\)", markup, re.S)


class TestTheHeaderVerbsFollowTheAgentsLife:
    def test_a_new_agent_has_no_delete(self, markup):
        """There is nothing yet to delete, and "← All agents" already
        discards. Two buttons for one outcome, one sounding permanent and the
        other harmless, is worse than one."""
        fn = re.search(r"function headActionsHtml\(a\) \{(.*?)\n  \}", markup, re.S)
        assert fn, "headActionsHtml not found"
        new_branch = re.search(r"if \(isNewAgent\) \{(.*?)\n    \}", fn.group(1), re.S)
        assert new_branch, "no isNewAgent branch"
        assert "data-ag-del" not in new_branch.group(1)
        assert "Save as draft" in new_branch.group(1)

    def test_an_existing_agent_gets_save_status_and_delete(self, markup):
        fn = re.search(r"function headActionsHtml\(a\) \{(.*?)\n  \}", markup, re.S)
        existing = fn.group(1).rsplit("return statusPill(a) +", 1)[-1]
        for hook in ("data-ag-del", "data-ag-status", "data-ag-save"):
            assert hook in existing, f"{hook} missing from the existing-agent header"
        assert "Revert to draft" in existing

    def test_save_is_dead_until_there_is_something_to_save(self, markup):
        fn = re.search(r"function headActionsHtml\(a\) \{(.*?)\n  \}", markup, re.S)
        existing = fn.group(1).rsplit("return statusPill(a) +", 1)[-1]
        assert "saving || !dirty ? ' disabled' : ''" in existing

    def test_the_save_button_is_actually_wired_up(self, markup):
        """A branch in the click handler is useless if the delegated selector
        does not match the button — which is exactly how this shipped broken
        the first time."""
        sel = re.search(r"var t = e\.target\.closest\('([^']*)'\)", markup)
        assert sel and "[data-ag-save]" in sel.group(1)
