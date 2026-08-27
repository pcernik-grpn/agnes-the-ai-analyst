"""The /agents index sorts by what you would do with the agent, not by name.

A ready agent and a draft are different objects to their owner: one is a thing
you use, the other a thing you are still making. The list says so in two titled
bands, ready first.

What a CARD does is the same in both bands: it opens the builder. This page is
where an agent is configured, so the one obvious gesture on a card has to reach
that; a ready card used to start a conversation instead, which left editing to a
small footer button on the page whose whole subject is the configuration.
Chatting keeps a footer action on every card, so neither route is lost.

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


class TestEveryCardOpensTheBuilder:
    def test_the_card_click_opens_the_builder_in_both_states(self, agent_card):
        """Not a conversation: the card stands for the CONFIGURATION on the page
        that configures it. Unconditional, so a ready card and a draft behave
        the same."""
        assert "var open = 'data-ag-open=\"' + esc(a.id) + '\"';" in agent_card
        assert "data-ag-chatcard" not in agent_card, "a card still opens a chat"

    def test_no_chatcard_route_is_left_anywhere_on_the_page(self, markup):
        """Including the click handler and the keyboard-activation selector — a
        branch nothing emits is a trap for the next reader."""
        assert "data-ag-chatcard" not in markup

    def test_every_card_offers_chat_in_its_footer(self, agent_card):
        """The route the card click used to be. Every agent stays talk-to-able,
        draft included — see the comment on `alt` in agentCard for why this is
        not gated on surfaces.web."""
        assert "'<a class=\"ag-chat\" href=\"/chat?agent=" in agent_card
        assert ">Edit<" not in agent_card, "the card itself is the edit route now"

    def test_an_agent_without_a_slug_offers_no_chat_action(self, agent_card):
        """There is no session to open without a slug, and a dead link is worse
        than no link. The card still opens the builder."""
        assert re.search(r"var alt = a\.slug\s*\n?\s*\?", agent_card)

    def test_the_card_is_keyboard_reachable(self, markup):
        assert re.search(r"closest\('\[data-ag-open\],\[data-ag-toggle-sec\]'\)", markup)
