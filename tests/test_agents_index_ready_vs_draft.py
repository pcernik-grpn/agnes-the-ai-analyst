"""The /agents index sorts by what you would do with the agent, not by name.

A ready agent and a draft are different objects to their owner: one is a thing
you use, the other a thing you are still making. The list says so twice — in
two titled bands (ready first), and in what a card's click does. Clicking a
ready card starts a conversation with it; clicking a draft reopens the builder.
Neither route is lost, because each card carries the other as a footer action.

Markup-level contracts on ``agents.html``, whose list view is rendered by the
page's own inline script (as the rest of this page's suites already assume).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "agents.html"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def render_list(markup: str) -> str:
    block = re.search(r"function renderList\(\) \{(.*?)\n  \}\n", markup, re.S)
    assert block, "renderList not found"
    return block.group(1)


@pytest.fixture(scope="module")
def agent_card(markup: str) -> str:
    block = re.search(r"function agentCard\(a\) \{(.*?)\n  \}\n", markup, re.S)
    assert block, "agentCard not found"
    return block.group(1)


class TestTheListIsGroupedByState:
    def test_ready_and_drafts_are_split_into_two_bands(self, render_list):
        assert "a.status === 'ready'" in render_list
        assert "a.status !== 'ready'" in render_list

    def test_ready_comes_first(self, render_list):
        """Ready agents are the ones you came to use; drafts are housekeeping."""
        assert render_list.index("group('Ready'") < render_list.index("group('Drafts'")

    def test_an_empty_band_renders_nothing_at_all(self, markup):
        """A "Drafts" heading over no drafts reads as a rendering bug."""
        block = re.search(r"function group\(title, sub, list, lead\) \{(.*?)\n  \}", markup, re.S)
        assert block and "if (!list.length) return '';" in block.group(1)

    def test_new_agent_is_the_first_card_not_a_toolbar_button(self, render_list, markup):
        """Making an agent lands in the same builder as opening one, so it is a
        card in the same grid rather than a button floating over the collection.

        And it must lead whichever band actually RENDERS: `group()` drops an empty
        band entirely, so handing the card to Ready unconditionally would lose it
        on an instance whose every agent is still a draft."""
        # No toolbar row above the grid any more.
        assert "ag-listbar" not in render_list
        assert "'+ New agent'" not in render_list.replace('"', "'"), "the button is a card now"
        # The ZERO state keeps its own primary CTA, and should: an empty page has
        # no grid for a card to lead, so that panel is the affordance.
        empty = render_list.split("if (!agents.length)", 1)[1].split("return;", 1)[0]
        assert "cc-btn--primary" in empty
        assert "cc-btn--primary" not in render_list.replace(empty, ""), "no primary button outside the zero state"
        # The card is a real cell of the grid, inside `group()`'s own container.
        assert "newCard()" in render_list
        block = re.search(r"function group\(title, sub, list, lead\) \{(.*?)\n  \}", markup, re.S)
        assert "(lead || '')" in block.group(1), "the lead cell rides inside .ag-grid"
        # Ready leads when it renders; Drafts inherits the card when it does not.
        assert "readyHtml ? '' : newCard()" in render_list
        card = re.search(r"function newCard\(\) \{(.*?)\n  \}", markup, re.S)
        assert card, "newCard not found"
        assert "data-ag-new" in card.group(1), "it must share the create path"
        assert 'class="ag-new"' in card.group(1)


class TestTheCardsPrimaryActionFollowsItsState:
    def test_a_ready_card_opens_a_conversation(self, agent_card, markup):
        assert "data-ag-chatcard=" in agent_card
        handler = re.search(
            r"t\.hasAttribute\('data-ag-chatcard'\)\) \{(.*?)\n    \}", markup, re.S
        )
        assert handler, "no data-ag-chatcard branch in the click handler"
        assert "/chat?agent=" in handler.group(1)

    def test_a_draft_card_opens_the_builder(self, agent_card):
        assert "data-ag-open=" in agent_card

    def test_a_ready_card_still_offers_edit(self, agent_card):
        """The builder must stay reachable once an agent is marked ready —
        otherwise marking it ready is a one-way door."""
        assert re.search(r"isReady\s*\n?\s*\?\s*'<button[^']*data-ag-open=", agent_card)
        assert ">Edit<" in agent_card

    def test_a_draft_card_still_offers_chat(self, agent_card):
        """Every agent stays talk-to-able, draft included — see the comment on
        `alt` in agentCard for why this is not gated on surfaces.web."""
        assert "'<a class=\"ag-chat\" href=\"/chat?agent=" in agent_card

    def test_a_ready_agent_without_a_slug_falls_back_to_the_builder(self, agent_card):
        """There is no session to open without a slug; a dead card is worse
        than one that opens the editor."""
        assert "isReady && a.slug" in agent_card

    def test_the_card_is_keyboard_reachable(self, markup):
        assert re.search(
            r"closest\('\[data-ag-open\],\[data-ag-chatcard\],\[data-ag-toggle-sec\]'\)", markup
        )
