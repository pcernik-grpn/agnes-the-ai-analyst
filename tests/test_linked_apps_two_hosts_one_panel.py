"""Publishing apps is offered in two places and implemented once.

The two places are two errands, which is why both exist:

  * the MCP builder, while you connect a server that lists apps — going
    somewhere else for the obvious next step is a round trip nobody wanted;
  * ``/admin/linked-apps``, when the server was connected weeks ago and the
    job starts from the app list rather than from a connection form.

The failure this guards is them drifting. The retired standalone builder and
the section that replaced it would each have their own idea of what "read the
list" writes, and only one of them could be right — so the requests, the
state and the markup live in ``linked_apps_panel.js`` and both hosts drive it.

It also pins the two things the retired wizard got wrong, because a second
entry point is exactly where they would come back: the source is CHOSEN from
a list rather than detected by elimination, and the empty state hands off to
the builder instead of saying "register one first, then come back".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "web" / "static" / "js" / "components"
PANEL = JS / "linked_apps_panel.js"
PAGE = JS / "linked_apps_page.js"
MCP = JS / "mcp_builder.js"


@pytest.fixture(scope="module")
def panel() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_the_panel_owns_the_requests(panel):
    for call in ("/api/admin/mcp-tools", "/materialize", "/api/data-apps", "/api/admin/grants"):
        assert call in panel, f"{call} is not in the shared panel"


@pytest.mark.parametrize("host", [PAGE, MCP], ids=["standalone-page", "mcp-builder"])
def test_neither_host_reimplements_it(host):
    """A host may render and route clicks; it may not talk to the app APIs
    itself. That is how the two stay the same feature."""
    src = host.read_text(encoding="utf-8")
    assert "LinkedAppsPanel" in src, "this host does not use the shared panel at all"
    for call in ("/materialize", "'/api/data-apps"):
        assert call not in src, (
            f"{host.name} calls {call} directly — the second implementation this file exists to prevent"
        )


def test_the_standalone_page_asks_which_server(page_src=None):
    """The retired wizard DETECTED the source — "if there is exactly one, use
    that" — and printed "✓ Using" for a choice nobody made."""
    src = PAGE.read_text(encoding="utf-8")
    assert "data-la-src" in src, "there is no way to pick a server"
    body = re.search(r"function sourcesBody\(\) \{(.*?)\n  \}", src, re.S)
    assert body, "sourcesBody moved — re-point this guard"
    assert "Choose" in body.group(1)


def test_only_servers_that_can_answer_are_offered():
    """A server with no lister tool cannot answer "what apps do you have", so
    listing it would be a row whose only outcome is a failure two clicks on."""
    src = PAGE.read_text(encoding="utf-8")
    assert "LinkedAppsPanel.isLister" in src, "every connected server is offered, listers or not"


def test_the_empty_state_is_not_a_dead_end():
    """"Not registered yet? Register one first, then come back" was the
    wizard's first step. Connecting one is an action here, and the builder it
    opens carries the same panel, so the errand finishes there."""
    src = PAGE.read_text(encoding="utf-8")
    body = re.search(r"function sourcesBody\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "/admin/mcp-sources/new" in body, "the empty state offers no way to connect a server"


def test_nothing_promises_a_refresh_that_does_not_happen(panel):
    """`tool_registry.upsert` requires a schedule for materialize mode, so one
    is sent — but nothing in the scheduler reads a tool_registry row, so the
    `daily 03:00` the old wizard wrote never fired. A "keep this list current"
    switch would promise a refresh the product does not perform."""
    assert "'daily 03:00'" in panel, "the registry would refuse the mode change without it"
    assert "data-la-refresh" not in panel, "the refresh switch is back"
    assert "Read again to pick up" in panel, "the panel no longer says the catalogue is a snapshot"
