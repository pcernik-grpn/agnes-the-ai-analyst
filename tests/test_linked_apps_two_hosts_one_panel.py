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


def test_only_servers_that_can_answer_are_offered_as_a_choice():
    """A server with no lister tool cannot answer "what apps do you have", so
    CHOOSING it would be a row whose only outcome is a failure two clicks on."""
    src = PAGE.read_text(encoding="utf-8")
    assert "LinkedAppsPanel.isLister" in src, "every connected server is offered, listers or not"
    # Choosable rows carry `data-la-src` (the click handler's only hook); the
    # reference rows must not, or "not offered as a choice" is only a visual
    # claim.
    off = re.search(r"function offRowsHtml\(\) \{(.*?)\n  \}", src, re.S)
    assert off, "offRowsHtml moved — re-point this guard"
    assert "data-la-src" not in off.group(1), "a server that cannot list apps is still clickable"
    assert "disabled" in off.group(1), "its Choose button is not disabled"


def test_a_server_that_cannot_list_apps_is_shown_not_hidden():
    """Dropping those servers from the response made the empty state ambiguous
    in the one way that matters: "none can list apps" read identically to
    "none are connected", and the two want opposite next moves — add a lister
    tool to a server you have, vs. connect a server at all."""
    src = PAGE.read_text(encoding="utf-8")
    assert "otherSources" in src, "the non-lister servers are not kept at all"
    body = re.search(r"function sourcesBody\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert "offRowsHtml()" in body, "sourcesBody never renders them"
    # The two empty states must be distinguishable, which means two heads.
    assert "None of your connected servers can list apps." in body
    assert "No server is connected yet." in body


def test_the_empty_state_is_not_a_dead_end():
    """ "Not registered yet? Register one first, then come back" was the
    wizard's first step. Connecting one is an action here, and the builder it
    opens carries the same panel, so the errand finishes there.

    The markup lives in `connectRow()` — both empty states and the populated
    list end with it, so it could not stay inline in one branch."""
    src = PAGE.read_text(encoding="utf-8")
    connect = re.search(r"function connectRow\(\) \{(.*?)\n  \}", src, re.S)
    assert connect, "connectRow moved — re-point this guard"
    assert "/admin/mcp-sources/new" in connect.group(1), "no way to connect a server"
    body = re.search(r"function sourcesBody\(\) \{(.*?)\n  \}", src, re.S).group(1)
    assert body.count("connectRow()") == 2, "an empty state or the populated list has no connect row"


def test_leaving_the_page_is_stated_before_the_click():
    """Connecting a server navigates away from a half-filled configuration.
    Saying so is the difference between a handoff and losing your place."""
    connect = re.search(r"function connectRow\(\) \{(.*?)\n  \}", PAGE.read_text(encoding="utf-8"), re.S).group(1)
    assert "leave this page" in connect, "the round trip is not stated"
    # Both endings, because both are real: finish there, or come back here.
    assert "come back here" in connect


def test_the_chosen_server_can_be_un_chosen():
    """`pickSource` was set-only, on a control that is the same `.ag-tglbtn` as
    the panel's own `✓ On` / `Off` app rows and carries the same `on` state — so
    it read as a toggle and behaved as a latch, and a caller who picked the
    wrong server had no way back to "none chosen".

    Clearing has to reset the PANEL too: an app list belongs to the server it
    was read from, so a cleared pick that left the list on screen would be
    showing rows nothing accounts for."""
    src = PAGE.read_text(encoding="utf-8")
    body = re.search(r"function pickSource\(id\) \{(.*?)\n  \}", src, re.S)
    assert body, "pickSource moved — re-point this guard"
    body = body.group(1)
    assert "picked && picked.id === id" in body, "re-clicking the chosen server is still a no-op"
    assert "picked = null" in body, "the pick is never cleared"
    assert "setSource(null, null)" in body, "clearing the pick leaves the panel's app list behind"
    # Single-select survives: a DIFFERENT id must still switch rather than
    # being swallowed by the toggle branch.
    assert "sources.filter(" in body, "choosing another server no longer resolves it"


def test_configuring_something_does_not_scroll_you_back_to_the_top():
    """Every interaction repaints the whole mount, which destroys the scrolling
    column and rebuilds it at offset 0 — so choosing a group in section 3 threw
    the reader up to section 1, and picking the second of a long list of groups
    reset the picker each time.

    Asserted as ORDER, because that is the part that can silently break: the
    offsets have to be read BEFORE the innerHTML write and re-applied AFTER
    it. A restore that runs first is a no-op that still reads as a fix."""
    src = PAGE.read_text(encoding="utf-8")
    body = re.search(r"function render\(\) \{(.*?)\n  \}", src, re.S)
    assert body, "render moved — re-point this guard"
    body = body.group(1)
    capture, write, restore = (
        body.find("scrollState()"),
        body.find("mount.innerHTML"),
        body.find("restoreScroll("),
    )
    assert capture != -1, "render never reads the scroll offsets"
    assert restore != -1, "render never restores them"
    assert capture < write < restore, "the offsets are not captured before the repaint and applied after"

    # Every scroller on the page, because which one moves depends on the
    # viewport and on what is open: the configuration column, the Library
    # preview in the left pane, the picker's list, and — below the two-pane
    # breakpoint, where builder.css hands scrolling back to the page — the
    # window. A new scroller that skips the list jumps silently.
    for scroller in (".ag-cfg-body", ".la-prev-body", ".ag-pick-rows", "window.scrollTo"):
        assert scroller in src, f"{scroller} is not preserved across a repaint"
    # The selectors are registered in ONE list, so the capture and the restore
    # cannot drift apart — the bug where a scroller is saved but never applied.
    assert "SCROLLERS" in src, "the scroller list was inlined into both halves again"


def test_the_left_pane_shows_what_publishing_will_do():
    """The shell's left pane holds an assistant transcript in both other
    callers. This page has no assistant and should not grow one — every step is
    a pick from a short enumerated list, so there is nothing to draft — which
    left the pane holding three paragraphs of `.ag-cfg-blurb`, a 12.5px CENTRED
    CAPTION component, in a column sized for a chat.

    It now previews the outcome: the rows that will land in the Library, for
    the groups granted. Read-only is the load-bearing part — the configuration
    stays the single place anything is changed."""
    src = PAGE.read_text(encoding="utf-8")
    assert "previewBody()" in src, "the left pane no longer renders the preview"
    body = re.search(r"function previewBody\(\) \{(.*?)\n  \}", src, re.S)
    assert body, "previewBody moved — re-point this guard"
    body = body.group(1)

    # The caption-as-body-prose is gone from this page. Matched on the RENDERED
    # class, not the bare name — the comment above `previewBody` names it, which
    # a substring check on the whole file would hit.
    assert 'class="ag-cfg-blurb"' not in src, "the centred-caption blurb is back in the left pane"

    # Read-only: no hook the page's click handler can match. `data-la-src`,
    # `data-la-app`, `data-la-openpick` etc. are all configuration-side.
    preview_src = src[src.index("var APP_GLYPH") : src.index("function accessBody")]
    for hook in ("data-la-src", "data-la-app", "data-la-read", "data-la-openpick", "data-la-pick"):
        assert hook not in preview_src, f"the preview renders {hook} — it is meant to be read-only"

    # Every state the panel can be in has to say something different, or the
    # pane goes back to being dead space at the moment it matters.
    for state in ("Nothing to show yet", "Not read yet", "lists no apps", "switched off"):
        assert state in src, f"the preview has no {state!r} state"

    # Untrusted: names and descriptions come from an external tool server.
    assert "esc(a.name)" in body or "esc(a.name)" in preview_src, "app names are not escaped"
    assert "esc(g.name)" in preview_src, "group names are not escaped"


def test_the_panel_exposes_progress_without_the_host_reaching_inside(panel):
    """The preview needs to tell "not read yet" from "read, and everything
    switched off" — `chosen()` returns an empty array for both. That is a
    read-only accessor on the panel, not the host poking at internals or
    parsing the display string `summary()` returns."""
    assert "state: function" in panel, "the panel exposes no progress accessor"
    src = PAGE.read_text(encoding="utf-8")
    assert "thePanel().state()" in src, "the page does not use it"
    # Not by scraping the human-facing summary.
    assert "summary().indexOf" not in src and "'not read yet'" not in src, (
        "the page is parsing the panel's display string instead of its state"
    )


def test_publishing_keys_the_grant_by_slug_not_row_id(panel):
    """A `data_app` grant is keyed by SLUG. `_can_view` calls
    `can_access(…, row["slug"])`, the Library's apps band tests
    `da["slug"] in granted_ids`, and the ResourceTypeSpec declares
    `id_format="<slug>"` — every test in the suite that seeds one uses a slug.

    Publish sent `a.id`, the row id (`app_<hex>`), so it wrote grant rows that
    nothing ever reads: it reported success, redirected to the Library, and the
    granted group still could not see the apps. Verified against the real API
    before and after — an id-keyed grant left `_can_view` False for a
    non-admin member of the granted group; the slug-keyed one makes it True.

    Both hosts publish through this function, so the MCP builder's apps
    section had the same hole."""
    assert "resource_type: 'data_app', resource_id: a.slug" in panel, (
        "publish is not keying the grant by slug — the grant would be unreadable"
    )
    assert "resource_id: a.id" not in panel, "the row id is back as the grant key"
    # The slug has to survive the panel's own app mapping to be sendable, and
    # must not replace `id`, which is the key `st.chosen` is built on.
    apps_map = re.search(r"st\.apps = rows\.map\((.*?)\}\);", panel, re.S)
    assert apps_map, "the app mapping moved — re-point this guard"
    assert "slug: String(a.slug" in apps_map.group(1), "the mapping drops the slug"
    assert "id: String(a.id)" in apps_map.group(1), "the panel's own per-app key is gone"


def test_nothing_promises_a_refresh_that_does_not_happen(panel):
    """`tool_registry.upsert` requires a schedule for materialize mode, so one
    is sent — but nothing in the scheduler reads a tool_registry row, so the
    `daily 03:00` the old wizard wrote never fired. A "keep this list current"
    switch would promise a refresh the product does not perform."""
    assert "'daily 03:00'" in panel, "the registry would refuse the mode change without it"
    assert "data-la-refresh" not in panel, "the refresh switch is back"
    assert "Read again to pick up" in panel, "the panel no longer says the catalogue is a snapshot"
