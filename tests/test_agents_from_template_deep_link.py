"""`/agents?from_template=1` opens a modal OVER the list, not over a blank page.

The Library's Agent-templates band links here (`router.py`'s `band_link`), and
the page's `routeFromQuery` handled it alongside `?new=1` and `?open=<id>` —
both of which legitimately return without rendering, because both end in the
builder. The picker does not: it is an overlay, so returning early left it
floating over an empty page, and Cancel dismissed it onto that same empty page.

The second failure was a race. `plugins` (which is where the templates come
from) is fetched in parallel with the agents list, and the picker was opened
synchronously from the agents fetch's `finally` — one request against two, so
the agents list usually won and the caller got a "Start from a template" modal
listing no templates at all. The `data-ag-new` click handler already waits for
`loadPlugins()` for exactly this reason; the deep link now does too.

Markup-level contracts on ``agents.html``, as the rest of this page's suites are.
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
def route_from_query(markup: str) -> str:
    block = re.search(r"function routeFromQuery\(\) \{(.*?)\n  \}\n", markup, re.S)
    assert block, "routeFromQuery not found"
    return block.group(1)


@pytest.fixture(scope="module")
def picker_branch(route_from_query: str) -> str:
    """The `?from_template=1` branch — from its `if` to the `return true`."""
    block = re.search(r"if \(wantPicker\) \{(.*?)\n      return true;", route_from_query, re.S)
    assert block, (
        "the ?from_template=1 branch is a one-liner again: the picker has to "
        "render the list under itself and await loadPlugins() first"
    )
    return block.group(1)


def test_the_library_band_still_links_here() -> None:
    """The other half of the contract: the link this deep link exists for."""
    router = (Path(__file__).resolve().parents[1] / "app" / "web" / "router.py").read_text(
        encoding="utf-8"
    )
    assert '"/agents?from_template=1"' in router


def test_the_deep_link_is_recognised(route_from_query: str) -> None:
    assert "params.get('from_template')" in route_from_query
    # One-shot, like every other param here: a reload must not re-open it.
    assert "params.delete('from_template')" in route_from_query


def test_the_list_renders_under_the_picker(picker_branch: str) -> None:
    """The bug: a modal over a blank page, and Cancel onto a blank page."""
    assert "renderList();" in picker_branch


def test_the_picker_waits_for_the_templates_to_load(picker_branch: str) -> None:
    """The other bug: a template picker listing no templates."""
    assert "loadPlugins()" in picker_branch
    assert re.search(r"loadPlugins\(\)\s*\.then\(", picker_branch), (
        "openTemplatePicker must be inside loadPlugins().then — opening it "
        "synchronously races the marketplace fetch that populates `plugins`"
    )
    opener = picker_branch.index("openTemplatePicker()")
    assert opener > picker_branch.index("loadPlugins()")


def test_no_templates_says_so_instead_of_showing_an_empty_modal(picker_branch: str) -> None:
    assert "installedTemplates().length" in picker_branch
    assert "appToast" in picker_branch


def test_the_builder_bound_params_still_return_without_rendering(route_from_query: str) -> None:
    """`?new=1` and `?template=<id>` end in `openBuilder`, which owns the view —
    rendering the list first would be a visible flash of the wrong screen."""
    assert "if (wantTemplate) { createAgent(null, wantTemplate); return true; }" in route_from_query
    assert "if (wantNew) { createAgent(null); return true; }" in route_from_query


def test_a_deep_link_arrival_skips_the_coach_mark(markup: str) -> None:
    """`TOURS.agents` anchors on `[data-ag-new]` and RAISES that anchor above
    the page — with the list rendering under the picker, the spotlight drew the
    "+ Build an agent" card straight through the template list. The tour has no
    notion of an open modal, so the page decides: an arrival that carries an
    intent doesn't get narrated at.

    The flag is read SYNCHRONOUSLY, because `routeFromQuery` strips the params
    and the coach-mark's module script runs after that.
    """
    assert "window.__agnesAgentsDeepLink = /[?&](open|new|template|from_template)=/" in markup
    assert "if (!window.__agnesAgentsDeepLink) {" in markup
    # …and it is set OUTSIDE routeFromQuery — inside, it would be set only
    # after the agents fetch resolves, which is the race it exists to avoid.
    block = re.search(r"function routeFromQuery\(\) \{(.*?)\n  \}\n", markup, re.S)
    assert block and "__agnesAgentsDeepLink" not in block.group(1)


def test_the_coach_mark_still_fires_on_a_plain_visit(markup: str) -> None:
    """Only the deep link is suppressed. `autoLaunchTour` marks a tour seen
    when it LAUNCHES, so a skipped card is not a lost one — it arrives on the
    next plain /agents visit."""
    assert 'mod.autoLaunchTour("agents")' in markup
