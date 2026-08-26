"""The builder's left pane writes the right pane — and never replaces it.

The two-pane builder makes a conversation the primary way an agent gets
configured. That is only safe if the configuration stays the source of truth:
the panel is always hand-editable, the server's returned row is what the page
re-renders from, and nothing the assistant says can navigate around the
owner's own controls.

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


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TestTheConversationWritesTheConfiguration:
    def test_a_turn_posts_to_the_builder_endpoint(self, markup):
        assert "/builder/turn" in markup

    def test_the_panel_rerenders_from_the_servers_row_not_the_patch(self, markup):
        """The server applies the patch AND re-derives the enforced scope, so
        its row is the truth. Merging the patch client-side would show a scope
        the agent does not actually have."""
        block = re.search(r"function sendBuilderTurn\(text\) \{(.*?)\n  \}\n", markup, re.S)
        assert block, "sendBuilderTurn not found"
        body = block.group(1)
        assert "agents[idx] = body.agent" in body, "the working copy is not replaced wholesale"

    def test_the_history_sent_excludes_the_message_being_answered(self, markup):
        """`conv` already has the new turn pushed onto it; sending it whole
        would show the model the same message twice."""
        assert "history: conv.slice(0, -1)" in markup

    def test_capability_choices_are_confined_to_what_the_picker_offers(self, markup):
        assert "plugin_candidates:" in markup

    def test_the_owner_can_still_edit_every_field_by_hand(self, markup):
        """The panel is never disabled while the assistant is talking — the
        conversation is an accelerator, not a gate."""
        assert "everything the assistant set, editable by hand" in markup
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
