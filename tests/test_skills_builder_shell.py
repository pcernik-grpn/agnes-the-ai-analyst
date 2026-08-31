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


class TestTheCreateConversation:
    """The Create tab writes the form beside it — and never replaces it."""

    def test_a_turn_posts_to_the_entity_endpoint(self, markup):
        assert "/api/store/entities/builder/turn" in markup

    def test_a_turn_writes_nothing_server_side(self, markup):
        """A Library entity has no row until Save to Library. The patch is
        merged into the local draft; there is no apply flag because there is
        nothing to apply to."""
        block = re.search(r"function sendBuilderTurn\(text\) \{(.*?)\n  \}\n", markup, re.S)
        assert block, "sendBuilderTurn not found"
        body = block.group(1)
        assert "draft[k] = patch[k]" in body, "the patch is not merged into the local draft"
        assert "persist()" in body, "a merged patch is not written to the draft store"

    def test_the_turn_sees_the_draft_on_screen(self, markup):
        block = re.search(r"function sendBuilderTurn\(text\) \{(.*?)\n  \}\n", markup, re.S)
        assert "draft: {" in block.group(1)

    def test_the_history_sent_excludes_the_message_being_answered(self, markup):
        """`conv()` already has the new turn pushed onto it; sending it whole
        would show the model the same message twice. The opening turn is the
        other case: it pushes nothing, and an empty transcript is what tells
        the server it is being asked to speak first."""
        assert "conv().slice(0, -1)" in markup
        assert re.search(r"history:\s*opening \? \[\] : conv\(\)\.slice\(0, -1\)", markup)

    def test_the_transcript_is_per_type(self, markup):
        """Switching type must not carry a skill's conversation into a
        plugin — the drafts are already per-type for the same reason."""
        assert re.search(r"var convs = \{ skill: \[\], plugin: \[\], agent: \[\] \}", markup)

    def test_the_form_stays_hand_editable_while_the_assistant_talks(self, markup):
        """The conversation is an accelerator, not a gate."""
        block = re.search(r"function stepsHtml\(c, ns\) \{(.*?)\n  \}\n", markup, re.S)
        assert block, "stepsHtml not found"
        assert "disabled" not in block.group(1)

    def test_only_the_changed_pane_rerenders(self, markup):
        """A full render() per turn would pull the caret out of whichever
        field or composer the author is typing in."""
        assert "function renderLeftPane(" in markup
        assert "function renderConfigPane(" in markup

    def test_a_half_typed_message_survives_a_tab_switch(self, markup):
        assert "convDraft = live.value" in markup

    def test_the_composer_is_state_not_just_dom(self, markup):
        """The left pane re-renders when a turn lands; a draft that only lived
        in the textarea would vanish mid-sentence."""
        block = re.search(r"function handleFieldInput\(e\) \{(.*?)\n    var f =", markup, re.S)
        assert block and "convDraft = e.target.value" in block.group(1)

    def test_every_conversation_hook_is_wired_to_the_delegated_handler(self, markup):
        """A branch the selector never matches is dead code — how Save shipped
        broken on /agents."""
        sel = re.search(r"var t = e\.target\.closest\((.*?)\);", markup, re.S)
        assert sel
        for hook in ("[data-ag-tab]", "[data-ag-send]", "[data-ag-chip]"):
            assert hook in sel.group(1), f"{hook} is not matched by the click handler"

    def test_each_type_brings_its_own_opening_and_starters(self, markup):
        """The sentence that tells a first-time author what this box wants
        differs per type — a skill, a bundle and a role are not described the
        same way."""
        for key in ("convOpening", "convPlaceholder", "convStarters"):
            assert markup.count(key + ":") == 3, f"{key} is not declared for all three types"

    def test_the_starters_belong_to_the_empty_state_alone(self, markup):
        """They shipped invisible, and the first fix was too narrow.

        The builder opens the conversation itself, and that turn's model
        suggestions were assigned over the curated starters — so a first-time
        author saw three invented scenarios ("Generate a monthly budget
        variance report") instead of three kinds of skill. Suppressing them on
        the OPENING turn fixed that screen and left the mirror image: any
        later turn yielding no usable suggestion put the starters back, so a
        nearly-finished draft could be offered "How to check a pipeline is
        healthy" — one click from a fresh brief landing on top of real work.

        The condition is the author, not the turn: starters until they have
        said something, never after.
        """
        chips = re.search(r"chips: (.*?),\n", markup, re.S)
        assert chips, "the chip selection moved — re-point this guard"
        assert "startersApply()" in chips.group(1), (
            "starters must be keyed on whether this is still a blank page"
        )
        body = markup
        assert "function startersApply() { return !authorHasSpoken() && isBlank(); }" in body, (
            "a RESUMED draft is not a blank page — the transcript is memory-only, so a reload "
            "makes work-in-progress look like a first visit to anything that asks the conversation"
        )
        assert "convStarters" in chips.group(1), "the curated starters are no longer reachable at all"


class TestTheLivePreview:
    """Preview means a real session with the thing you are building — for the
    one type where that is possible."""

    def test_the_preview_service_is_shared_not_forked(self, markup):
        """A session, a socket and a stream of tokens is genuinely stateful,
        so it is a service beside the (pure) shell rather than part of it —
        but it is still ONE implementation. Both builders load it."""
        agents = AGENTS.read_text(encoding="utf-8")
        for page, text in (("agents.html", agents), ("skills.html", markup)):
            assert "js/components/builder_preview.js" in text, f"{page} does not load the shared service"
            assert "BuilderPreview({" in text, f"{page} does not use it"

    def test_neither_page_keeps_its_own_socket_plumbing(self, markup):
        """The point of extracting it. A page re-growing its own WebSocket
        handling is the fork this guards against."""
        agents = AGENTS.read_text(encoding="utf-8")
        for page, text in (("agents.html", agents), ("skills.html", markup)):
            assert "new WebSocket(" not in text, f"{page} has grown its own socket again"

    def test_a_template_and_a_skill_can_be_tried_live_but_not_a_plugin(self, markup):
        """Two types, two routes: a template BECOMES an agent (a scratch one
        pointed at the draft), a skill is added TO one (materialized into the
        session's own workspace).

        A plugin is neither, and that is the line: its contents are an
        uploaded archive, and unpacking an untrusted zip into a session
        workspace is a bigger question than a preview tab. Its Preview stays
        the card — which is a deliberate limit, not an oversight, so it is
        pinned rather than left to be "fixed" by someone who has not thought
        about the archive.
        """
        assert re.search(r"return type === 'agent' \|\| type === 'skill';", markup)
        block = re.search(r"sessionExtras: function \(\) \{(.*?)\n    \},", markup, re.S)
        assert block, "the draft is not handed to the session"
        assert "type !== 'skill'" in block.group(1), "a non-skill draft is being sent as a preview_skill"

    def test_a_bodyless_template_says_what_to_do_rather_than_failing(self, markup):
        """There is nothing to run yet; the composer is disabled with the
        reason as its placeholder rather than accepting a message that would
        go nowhere."""
        assert "Write the role on the right, then talk to it here." in markup

    def test_switching_type_drops_the_session(self, markup):
        """Carrying the socket across would leave the author talking to the
        previous template under the new one's name."""
        block = re.search(r"function chooseType\(k\) \{(.*?)\n  \}", markup, re.S)
        assert block and "preview.reset()" in block.group(1)

    def test_the_pane_says_the_template_has_no_data_access(self, markup):
        """The most important thing to know about a template preview: it runs
        with nothing, and so will whoever installs it. A preview that implied
        otherwise would flatter the template."""
        assert "no data access of its own" in markup
