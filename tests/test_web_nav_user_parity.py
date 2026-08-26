"""Static guard: every USER-FACING page is reachable from the chrome.

This used to be a topnav→rail PARITY guard — "a destination that lives only in
the topnav is unreachable on rail-layout instances". Wave 0 (2026-08) deleted
the topnav chrome, which retires the parity form but makes the underlying
invariant STRONGER, not weaker: with one chrome there is no second menu a page
might still be listed in, so a live page the rail does not lead to is
unreachable, full stop.

So the guard is re-aimed at the thing itself. It reads the user-facing,
template-rendering routes out of ``app/web/router.py`` and requires each to be
reachable, where reachable means a literal href in ``_app_rail.html`` OR in
``library.html`` (the Library is a rail destination and its "+ Add" menu is the
rail's designated home for browse/install paths) OR the global search box —
restored to the rail in the same wave — OR an explicit entry in one of the two
lists below, each of which has to carry a reason.

Not hypothetical: the rail redesign shipped without /me/connections,
/corporate-memory, /apps, /marketplace and /catalog. The first three had simply
been dropped; the last two were retired deliberately *onto* in-page replacement
paths that did not exist yet. All five were found by audit, not by a user with
a working link.

The admin half of the inventory is guarded by
``tests/test_web_admin_nav.py::test_every_admin_page_route_has_a_nav_entry``,
which holds every ``require_admin`` page to an ``app/web/admin_nav.py`` entry.
This file covers what that one cannot see: the pages gated on
``get_current_user``.
"""

from __future__ import annotations

import re
from pathlib import Path

RAIL = Path("app/web/templates/_app_rail.html")
LIBRARY = Path("app/web/templates/library.html")
PALETTE = Path("app/web/templates/_app_scripts.html")
ROUTER = Path("app/web/router.py")

#: Pages the chrome reaches through a RESOLVER rather than a literal href, so
#: no static scan can see them. Each is one indirection, named here so the
#: exemption is a fact about the template rather than a hole in the sweep.
DYNAMIC_CHROME_LINKS = {
    # The rail logo's href is `{{ home_route or '/dashboard' }}`.
    "/home",
}

# Live user-facing pages deliberately absent from the chrome. Each needs a
# reason; an entry without one is drift, not a decision.
KNOWN_UNLINKED = {
    # Reached from the page whose job it is: /how-it-works owns "connect an AI
    # client", and the token flow is its fallback, not a second nav entry.
    "/mcp-connect": "cross-linked from /how-it-works; a nav row would duplicate it",
}

# Pages whose chrome answer is a REDIRECT into the Library rather than a link.
# Kept separate from KNOWN_UNLINKED because these are not "deliberately
# unreachable" — the behavioral test below proves each really redirects, so
# this set can never rot into a silent allowlist.
REDIRECTED_UNDER_RAIL = {
    "/corporate-memory": "/library?section=memory_domain",
    "/apps": "/library?section=files",
    # /stack was carried here as a KNOWN_UNLINKED entry whose own reason said
    # the open work was to RETIRE it (302 → /library), not to link it. #1088
    # did exactly that, so the exemption moves to the set that proves the
    # claim: /library already renders every kind My Stack did, off the same
    # StackResolver.browse() call, and `?stack=in_stack` is the toggle that
    # answers the question the page existed to answer.
    "/stack": "/library?stack=in_stack",
    # The Catalog and the Marketplace browse shells. Both listed rows the
    # Library already renders in full — every approved store entity, every
    # granted data package / memory domain / recipe / curated plugin — sliced
    # by kind tabs or a Browse-vs-My-Stack tab pair. That pair IS the Library's
    # Scope segment, so each redirect arrives with its half selected.
    #
    # These two are why `Install from the marketplace` and `Add shared data &
    # recipes` sat in the Library's "+ Add" menu: PR #1276 put them there to
    # stop the pages being orphans after the rail retired their nav entries. The
    # rows are gone now, and the fold is what makes their absence correct rather
    # than a stranded surface.
    "/catalog": "/library?scope=available",
    "/marketplace": "/library?scope=available",
}

_ROUTE_RE = re.compile(r'@router\.get\("(/[^"{}]*)"[^)]*response_class=HTMLResponse')


def _hrefs(text: str) -> set[str]:
    """Literal hrefs, with the fragment and query stripped.

    `href="/setup-advanced#claude-plan"` is a door to /setup-advanced; a
    pattern that stops at the first `#` or `?` sees no door at all and reports
    a linked page as an orphan.
    """
    return {h.split("?", 1)[0].split("#", 1)[0] for h in re.findall(r'href="(/[^"]*)"', text)}


def _user_facing_page_routes() -> set[str]:
    """Template-rendering GET routes that a signed-in non-admin can open.

    Scoped the same way `test_web_admin_nav.py` scopes its own sweep, with the
    boundaries that matter:

    * the body window stops at the NEXT ``@router.`` decorator, so a redirect
      handler cannot inherit the following route's ``TemplateResponse``;
    * ``get_current_user`` is required, which drops the pre-auth pages
      (``/login/*``, ``/``, ``/first-time-setup``) — those are reached before
      any chrome exists;
    * ``require_admin`` is dropped (``test_web_admin_nav.py`` owns those) and
      so is any path parameter — a detail page is reached from its list.
    """
    lines = ROUTER.read_text(encoding="utf-8").split("\n")
    decorator_idx = [i for i, ln in enumerate(lines) if ln.startswith("@router.")]
    out: set[str] = set()
    for n, i in enumerate(decorator_idx):
        m = _ROUTE_RE.search(lines[i])
        if not m:
            continue
        stop = decorator_idx[n + 1] if n + 1 < len(decorator_idx) else len(lines)
        body = "\n".join(lines[i:stop])
        if "require_admin" in body:
            continue  # test_web_admin_nav.py owns these
        if "get_current_user" not in body:
            continue  # pre-auth page: no chrome to be reachable from
        if "templates.TemplateResponse(" not in body:
            continue  # a bare redirect is not a page
        out.add(m.group(1).rstrip("/") or "/")
    return out


def _reachable_links() -> set[str]:
    """Every href the product actually offers.

    The chrome doors first — these are what a caller has without already being
    somewhere:

    * ``_app_rail.html`` — the rail's own rows and account menu;
    * ``library.html`` — the Library is a rail destination and its "+ Add"
      menu is the rail's designated home for browse/install paths;
    * ``_app_scripts.html`` — the Cmd/Ctrl-K command palette, which renders on
      every authed page and is a real way in, not a shortcut for power users;
    * ``admin_nav.py`` — the admin sidebar's inventory, which carries a few
      non-``/admin/*`` rows (the API-docs group).
    """
    rail = RAIL.read_text()
    links = set(_hrefs(rail))
    links |= {h.split("?", 1)[0].split("#", 1)[0] for h in re.findall(r"'href':\s*'(/[^']*)'", rail)}
    links |= set(_hrefs(LIBRARY.read_text()))
    # Palette rows are JS object literals: `href: '/me/memory-mining'`.
    links |= {h.split("?", 1)[0].split("#", 1)[0] for h in re.findall(r"href:\s*'(/[^']*)'", PALETTE.read_text())}

    from app.web.admin_nav import ADMIN_NAV_DOCS, ADMIN_NAV_SECTIONS, _section_entries

    links |= {e["href"].split("?", 1)[0] for s_ in ADMIN_NAV_SECTIONS for e in _section_entries(s_)}
    links |= {s_["href"].split("?", 1)[0] for s_ in ADMIN_NAV_SECTIONS if s_.get("href")}
    links |= {e["href"].split("?", 1)[0] for e in ADMIN_NAV_DOCS}

    links |= DYNAMIC_CHROME_LINKS

    # ...then every other template. A guide reached from /marketplace, or
    # /setup-advanced reached from /home, is REACHED — it does not need a rail
    # row, and demanding one would push every sub-page into the chrome. What
    # this guard is really for is the page with no door AT ALL: /admin/
    # semantic-layer shipped live and linked from nowhere, and was found by an
    # operator who had been handed the URL.
    for tpl in Path("app/web/templates").rglob("*.html"):
        links |= set(_hrefs(tpl.read_text(encoding="utf-8")))
    return {href.rstrip("/") or "/" for href in links if href}


def test_every_user_facing_page_has_a_door():
    unreachable = _user_facing_page_routes() - _reachable_links() - set(KNOWN_UNLINKED) - set(REDIRECTED_UNDER_RAIL)
    assert not unreachable, (
        f"Live user-facing pages nothing links to: {sorted(unreachable)}. "
        "The rail is the only chrome, so a page with no inbound link is reachable "
        "only by typing the URL. Add a link (the rail, the Library '+ Add' menu, "
        "the command palette, or the page that owns the job), or add it to "
        "KNOWN_UNLINKED with the reason it is deliberately unlinked."
    )


def test_every_exception_names_a_live_route():
    """An allowlist that outlives its route is worse than no allowlist: it
    keeps a deleted page's name alive and hides the next real gap behind it."""
    live = _user_facing_page_routes() | set(REDIRECTED_UNDER_RAIL)
    stale = {p for p in KNOWN_UNLINKED if p not in live}
    assert not stale, f"KNOWN_UNLINKED names routes that no longer render a page: {sorted(stale)}"


def test_every_exception_carries_a_reason():
    """The list is a record of decisions, not a mute allowlist."""
    for path, reason in KNOWN_UNLINKED.items():
        assert reason and len(reason) > 20, f"{path} needs a real reason, got {reason!r}"


def test_redirected_entries_really_redirect(seeded_app, monkeypatch):
    """REDIRECTED_UNDER_RAIL is a claim, not an allowlist: every entry must
    actually 302 into its Library section."""
    monkeypatch.setenv("AGNES_DATA_APPS_ENABLED", "1")
    # Both redirects fire only when the Library will actually show the caller
    # a row in the target band — seed an app owned by the analyst and a
    # required memory-domain grant so each claim is testable.
    import uuid

    from src.db import get_system_db
    from src.repositories.data_apps import DataAppsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    try:
        DataAppsRepository(conn).create(slug="parity-app", name="parity-app", owner_user_id="analyst1", description="")
        group_id = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone'").fetchone()[0]
        UserGroupMembersRepository(conn).add_member("analyst1", group_id, source="test")
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'memory_domain', 'md_data', 'required', CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), group_id],
        )
    finally:
        conn.close()
    c = seeded_app["client"]
    headers = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    for src, target in REDIRECTED_UNDER_RAIL.items():
        resp = c.get(src, headers=headers, follow_redirects=False)
        assert resp.status_code == 302, f"{src} did not redirect"
        assert resp.headers["location"] == target


def test_a_retired_browse_page_is_folded_not_stranded():
    """A page the rail stopped linking needs a destination, not a menu row.

    This used to require `library.html` to carry `href="/marketplace"` and
    `href="/catalog"`, because the "+ Add" menu was the only thing linking
    them: the rail had retired both nav entries and PR #1276 put the links in
    that menu to stop two live pages being orphans.

    Both are now folded INTO the Library (`REDIRECTED_UNDER_RAIL`), which is
    the durable answer the merge spec (2026-08-12) already gave for
    /corporate-memory, /apps and /stack — so an inbound link from the very
    page they redirect to would be a loop. What must hold is the weaker, truer
    thing: every retired browse page either redirects somewhere real, or is
    still linked. `test_redirected_entries_really_redirect` above proves the
    redirect end; this proves nothing is left in neither state.
    """
    library = LIBRARY.read_text()
    for href in ("/marketplace", "/catalog"):
        folded = href in REDIRECTED_UNDER_RAIL
        linked = f'href="{href}"' in library
        assert folded or linked, (
            f"{href} is neither folded into the Library nor linked from it — "
            "it is reachable only by typing the URL. Add it to "
            "REDIRECTED_UNDER_RAIL with its target, or link it."
        )
        assert not (folded and linked), (
            f"{href} both redirects to the Library and is linked from it — the link is a loop through a 302. Drop one."
        )


#: The rail's own destination rows. The broad sweep above cannot see one of
#: these disappear — /library is linked from a dozen page bodies, so dropping
#: its rail row still leaves a "door" by that definition while costing the
#: product its primary navigation. Pinned separately and literally.
RAIL_DESTINATIONS = ("/chat", "/chats", "/library", "/agents", "/admin")


def test_rail_carries_its_core_destinations():
    rail = RAIL.read_text()
    missing = [h for h in RAIL_DESTINATIONS if f'href="{h}"' not in rail]
    assert not missing, (
        f"_app_rail.html no longer links {missing}. These are the rail's own rows — "
        "losing one leaves the surface reachable only from page bodies, which is how "
        "the first rail generation stranded five destinations."
    )


def test_global_search_is_a_reachability_path():
    """Search is the catch-all door the rail deliberately leans on — the IA
    note in _app_rail.html cites it when explaining why a surface can lose its
    row. It only counts while the box is actually there (it was missing for
    the whole first rail generation, which is what made those five pages
    unreachable rather than merely unlisted)."""
    rail = RAIL.read_text()
    assert 'id="global-search"' in rail
    assert 'id="globalSearchResults"' in rail
