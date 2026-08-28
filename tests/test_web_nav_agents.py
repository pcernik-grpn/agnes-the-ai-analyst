"""Inbound-link guards for two pages that shipped with zero entry points.

`/agents` (the agent-profile builder) and `/mcp-connect` (token-based editor
setup) were both reachable only by typing the URL — same bug class as #919's
`/me/connections` (see `tests/test_web_nav_me_connections.py`). Placement
contract, mirroring the AI Connector / My connections entries:

- `/agents`      → a rail destination of its own. It shipped in the topnav's
                   user dropdown as "My agents"; the rail promoted it to a
                   first-class row ("Agents") when that chrome was retired
                   (Wave 0, 2026-08). Either way the contract is the same one
                   this file exists for: it is linked from ordinary chrome,
                   not reachable only by typing the URL.
- `/mcp-connect` → contextual link from the AI Connector page, which owns the
                   "connect an AI client" job. OAuth is the happy path there;
                   the token page is the fallback, so it gets a cross-link
                   rather than a second near-duplicate nav entry.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_agents_link_in_chrome_for_non_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    body = resp.text

    assert 'href="/agents"' in body
    # A rail destination row, not a dropdown item.
    assert ">Agents<" in body


def test_agents_link_in_chrome_for_admin(seeded_app):
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert 'href="/agents"' in resp.text


def test_agents_link_hidden_when_agent_profiles_disabled(seeded_app, monkeypatch):
    """`can_agent_profiles` (get_agent_profiles_enabled()) gates the same nav
    entry point — mirrors test_web_studio.py's test_studio_nav_hidden_when_disabled."""
    monkeypatch.setattr("app.web.router.get_agent_profiles_enabled", lambda: False)
    c = seeded_app["client"]
    resp = c.get("/library", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/agents"' not in resp.text
    assert "href: '/agents'" not in resp.text  # command palette row too


def test_how_it_works_agents_links_hidden_when_agent_profiles_disabled(seeded_app, monkeypatch):
    """`/how-it-works` carries three more `/agents` links than the chrome does —
    the TOC footer, the pillars "next" row and the page-foot "Keep going" row.

    They are entry points like any other: with the flag off `GET /agents`
    redirects home, so leaving them ungated shows an opted-out instance three
    invitations that silently bounce the user back where they started (Devin
    Review on #1186). Asserted as a count, so another link added later fails
    here rather than shipping as a fresh dead end. Four, not three: the page
    also draws the chrome's own user-dropdown entry, which this PR already
    gates.
    """
    c = seeded_app["client"]

    on = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"]))
    assert on.status_code == 200
    assert on.text.count('href="/agents"') == 4, "this test's premise moved — re-count the links"

    monkeypatch.setattr("app.web.router.get_agent_profiles_enabled", lambda: False)
    off = c.get("/how-it-works", headers=_auth(seeded_app["analyst_token"]))
    assert off.status_code == 200
    assert 'href="/agents"' not in off.text


def test_mcp_connect_linked_from_ai_connector_page(seeded_app):
    """The AI Connector page is the only inbound link to /mcp-connect — if this
    fails, the token setup page is unreachable again."""
    c = seeded_app["client"]
    resp = c.get("/me/ai-connector", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    assert 'href="/mcp-connect"' in resp.text


def test_news_link_in_user_dropdown_for_non_admin(seeded_app, monkeypatch):
    """`/news`'s other two entry points are both conditional: /home's "What's
    new" strip needs a published version AND `home_route == '/home'`, and the
    command palette bails out unless `#adminMenu` is in the DOM. On the
    `/dashboard` default that left a non-admin unable to reach the page at all
    (Devin Review on #1159), so it gets a dropdown entry like the others.

    Scoped to an instance that HAS news: the surface is hidden by default since
    the admin cleanup, and the rail item is behind the same `can_news` flag as
    the route — a link to a redirect would be worse than no link. The property
    defended here is that whenever the page exists, a non-admin can reach it.
    `tests/test_retired_admin_surfaces.py` owns the hidden direction."""
    monkeypatch.setenv("AGNES_NEWS_ENABLED", "1")
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    assert 'href="/news">News</a>' in body


def test_command_palette_is_admin_only_so_it_cannot_be_the_entry_point(seeded_app, monkeypatch):
    """Pins WHY the dropdown entries above have to exist.

    The palette is a convenience for admins, not a reachability guarantee: its
    IIFE returns immediately when `#adminMenu` is absent. Asserting the palette
    rows alone would pass for a non-admin — the `<script>` body is emitted for
    everyone — while the surface never initializes, which is false assurance of
    exactly the property these tests exist to defend."""
    # Same reason as the test above — the /news row is behind `can_news`.
    monkeypatch.setenv("AGNES_NEWS_ENABLED", "1")
    c = seeded_app["client"]
    body = c.get("/dashboard", headers=_auth(seeded_app["analyst_token"])).text
    # the rows ship to everyone …
    assert "href: '/agents'" in body
    assert "href: '/mcp-connect'" in body
    assert "href: '/news'" in body
    # … behind a gate a non-admin never passes.
    assert "if (!document.getElementById('adminMenu')) return;" in body
    assert 'id="adminMenu"' not in body, "non-admin page must not carry the admin menu"
