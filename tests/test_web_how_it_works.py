"""``/how-it-works`` — the consolidated orientation page.

One page, one scroll, one sticky table of contents. It absorbed:

  * /home's product story ("Four places, one workspace", the first-session
    narrative, the explore grid) — /home stays as the CLI install wizard it
    actually is;
  * the whole standalone AI Connector page (`me_cowork.html`, now
    `how_it_works.html`) — connector URL, six per-tool tabs, the CLI install,
    troubleshooting, and the personalized tools/skills/packages inventories.

The connector content is not *summarized* here, it LIVES here. That is the
point: two pages both explaining "how do I connect" had already drifted
("Four places" vs. six tool tabs), and a summary drifts by construction.

The eight section anchors are a PUBLIC CONTRACT — the chat landing's "Take
Agnes to your tools" door, the rail's "Use Agnes elsewhere" row, the onboarding
checklist's "Use Agnes from other AI tools" step, the tour's "Connect my AI
tools" button, and every redirected /me/ai-connector bookmark all point into
them.
"""

from __future__ import annotations

import pytest

# One per TOC entry, in page order. Renaming any of these breaks an inbound
# link, so they are asserted individually rather than as a loose count.
SECTION_IDS = [
    "overview",
    "knowledge",
    "surfaces",
    "connect",
    "cli",
    "first-run",
    "privacy",
    "reference",
]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def page(seeded_app):
    """The rendered page for a plain (non-admin) analyst."""
    resp = seeded_app["client"].get("/how-it-works", headers=_auth(seeded_app["analyst_token"]))
    assert resp.status_code == 200
    return resp.text


# ── Route + structure ────────────────────────────────────────────────


def test_page_loads_for_non_admin(page):
    """Nothing on the page is admin-gated — it is the orientation surface."""
    assert "One knowledge layer" in page


def test_page_requires_auth(seeded_app):
    resp = seeded_app["client"].get("/how-it-works", follow_redirects=False)
    assert resp.status_code in (302, 401, 403)


@pytest.mark.parametrize("section_id", SECTION_IDS)
def test_section_anchor_present(page, section_id):
    """Every TOC target exists as a real element id."""
    assert f'id="{section_id}"' in page


@pytest.mark.parametrize("section_id", SECTION_IDS)
def test_toc_links_to_section(page, section_id):
    """…and the TOC actually links to it."""
    assert f'href="#{section_id}"' in page


def test_toc_is_present_and_ordered(page):
    """The TOC exists and its entries appear in page order.

    A TOC whose order diverges from the document is worse than none — it
    breaks the reader's model of where they are.
    """
    assert 'id="hiw-toc"' in page
    positions = [page.index(f'href="#{sid}"') for sid in SECTION_IDS]
    assert positions == sorted(positions)


# ── No information was lost in the consolidation ─────────────────────


def test_connector_url_box_migrated(page):
    assert "/api/mcp/http" in page
    assert "Connector URL" in page


@pytest.mark.parametrize(
    "tool",
    ["claude-code", "claude-desktop", "claude-web", "cursor", "vscode", "chatgpt"],
)
def test_all_six_tool_tabs_migrated(page, tool):
    """All six per-tool panels survive the move.

    Six was a deliberate call over "top two + More tools": measured against
    the real `.aic-tab` metrics the strip needs ~640px inside an 860px column,
    so every tool stays one click away with no overflow.
    """
    assert f'data-panel="{tool}"' in page
    assert f'data-tab="{tool}"' in page


def test_claude_code_instructions_intact(page):
    assert "claude mcp add --scope user --transport http agnes" in page
    assert "--transport sse agnes" in page  # SSE fallback recipe
    assert "only appears after restarting Claude Code" in page


def test_cli_section_carries_the_install_flow(page):
    """The CLI mode was a hidden tab panel; it is now a linkable section."""
    assert 'id="setupClaudeBtn"' in page
    assert "agnes catalog" in page


def test_plugin_package_section_migrated(page):
    assert 'id="cowork-plugin-list"' in page
    assert "/marketplace/cowork/" in page
    assert "Plugin packages" in page


def test_troubleshooting_and_reference_migrated(page):
    assert "Having trouble?" in page
    assert "Advanced &amp; reference" in page


def test_connect_and_cli_are_two_tabs_of_one_section(page):
    """The two setup routes are alternatives, so they are two large tabs of
    one section rather than two stacked sections.

    Stacked, the CLI install read as a second step every reader owed after
    the connector; it is a different audience, not a later one. The section
    keeps both anchors: `#connect` on the section, `#cli` on the CLI panel.
    """
    assert 'data-mode="mcp"' in page
    assert 'data-mode="cli"' in page
    assert 'data-mode-panel="mcp"' in page
    assert 'data-mode-panel="cli"' in page
    # The CLI anchor moved onto the panel — it is no longer a section.
    assert 'id="cli" role="tabpanel"' in page
    assert '<section class="hiw-sec" id="cli"' not in page


def test_cli_tab_deep_link_opens_its_tab(page):
    """`#cli` starts hidden, so the anchor only works if the script opens
    the tab for it — inbound bookmarks and the #surfaces card both land
    there."""
    assert "function modeForHash" in page
    assert "window.addEventListener('hashchange', syncModeToHash)" in page


# ── The new orientation content ──────────────────────────────────────


def test_surfaces_section_includes_agnes_itself(page):
    """The rewritten "Four places" names the surface the reader is ON.

    The /home original listed VS Code · Terminal · Claude Code Desktop ·
    Cowork — four flavours of Claude Code — and omitted the web app entirely,
    which inverts the product model the rail redesign is built on.
    """
    assert "Four ways in, one knowledge layer" in page
    assert "you're here" in page
    assert 'href="#connect"' in page
    assert 'href="#cli"' in page


def test_knowledge_section_hands_off_to_the_library(page):
    """The four pillars are described here and browsed in the Library.

    They briefly deep-linked to /catalog, /marketplace and /corporate-memory —
    but the redesign folded all three into /library's own sections, so linking
    them from here resurrected surfaces the new UI retired.
    """
    assert 'href="/library"' in page
    assert 'href="/agents"' in page


#: Pages with no nav entry under the rail chrome — superseded by /library, or
#: legacy CLI/VS-Code guides. This page was the last thing in the product
#: linking several of them, which is exactly how a retired surface comes back
#: from the dead: one "helpful" cross-link at a time.
RETIRED_PAGES = [
    "/catalog",
    "/marketplace",
    "/corporate-memory",
    "/news",
    "/setup-advanced",
    "/workspace",
]


def _content(page: str) -> str:
    """The page's OWN markup, without the surrounding chrome.

    Scoped deliberately: the default chrome is the legacy topnav, whose nav and
    account menu still link /catalog, /marketplace and /corporate-memory. A
    whole-document scan would therefore fail on markup this page doesn't own —
    retiring those links from the topnav is a separate decision, not this
    change's job.
    """
    body = page.split('class="hiw-shell"', 1)[1]
    return body.split('id="aic-admin-msg"', 1)[0]


@pytest.mark.parametrize("path", RETIRED_PAGES)
def test_no_links_to_retired_surfaces(page, path):
    """No `href` in this page's own content may point at a retired surface.

    Matches the path as a whole segment so genuine API endpoints that merely
    share a prefix — `/marketplace/cowork/<plugin>.zip`, the real per-plugin
    download the Reference section builds — are not caught.
    """
    import re

    hrefs = re.findall(r'href="([^"]+)"', _content(page))
    offenders = [h for h in hrefs if re.match(rf"^{re.escape(path)}(?:[#?]|$)", h)]
    assert not offenders, f"links to retired {path}: {offenders}"


def test_privacy_is_its_own_section(page):
    """Promoted out of a `<p class="annotation">` buried in beat 5 of /home's
    terminal story — the highest-anxiety question on the page, previously
    placed where almost nobody scrolled."""
    assert 'id="privacy"' in page
    assert "Where your data goes" in page
    assert "/agnes-private" in page


def test_terminal_first_session_is_demoted_not_deleted(page):
    """The five-beat CLI narrative is accurate for CLI users and misleading
    for everyone else: kept, but as one collapsed row rather than the page's
    biggest block."""
    assert "five-beat CLI session" in page
    assert "What your first session looks like" in page


# ── First-read consumability ─────────────────────────────────────────
# Consolidating everything onto one page made it complete and also made it
# ~4,300px of uniform density: eight sections all shaped alike, so nothing
# distinguished essential from reference and "what do I do now?" was two
# screens down. The assertions below pin the three fixes — they are the kind
# of decision a well-meaning later edit reverses without noticing.


def test_hero_carries_no_action_buttons(page):
    """The hero states the model; it does not sell.

    This page is the *read* half of onboarding — the rail's "Continue setup"
    card is the *do* half — and the two actions a reader could want are already
    reachable twice over without a CTA pair in the hero (below), so a pair here
    only made an orientation page look like a landing page.
    """
    hero = page.split('id="overview"', 1)[1].split("</section>", 1)[0]
    assert "hiw-cta-primary" not in hero
    assert "hiw-cta-secondary" not in hero
    assert "hiw-hero-cta" not in hero


def test_the_two_actions_survive_below_the_hero(page):
    """Removing the hero CTAs must not strand the actions it offered.

    Both live in the "Keep going" row at the page foot, and chat again in the
    TOC's foot links — so this stays a deletion of duplication, not of a route.
    """
    nxt = page.split('id="next"', 1)[1].split("</section>", 1)[0]
    assert 'href="/chat"' in nxt
    assert 'href="#connect"' in nxt
    toc = page.split('class="hiw-toc-foot"', 1)[1].split("</div>", 1)[0]
    assert 'href="/chat"' in toc


#: Every section that carries a body — one collapsed `<details class="hiw-fold">`
#: each, so the page opens as a stack of readable rows rather than ~4,000px of
#: uniform density. The hero (`#overview`) and the "Keep going" pill row
#: (`#next`) are deliberately NOT folds: the hero is the page's first
#: impression, and a three-pill hand-off row has nothing to hide.
COLLAPSED_SECTIONS = ["knowledge", "surfaces", "connect", "first-run", "privacy", "reference"]


@pytest.mark.parametrize("section_id", COLLAPSED_SECTIONS)
def test_every_section_is_collapsible(page, section_id):
    """One disclosure per section, closed on arrival.

    The reader picks what to read; nothing sits expanded in the path of someone
    who came for one of the other five.
    """
    section = page.split(f'id="{section_id}"', 1)[1].split("</section>", 1)[0]
    assert 'class="hiw-fold"' in section, f"#{section_id} should collapse its body"
    # Collapsed, not summarised — and never collapsed-but-open, which would make
    # the disclosure decorative.
    assert 'class="hiw-fold" open' not in section


@pytest.mark.parametrize("section_id", COLLAPSED_SECTIONS)
def test_fold_summary_keeps_the_heading_and_lede_in_the_flow(page, section_id):
    """A closed section still SAYS what it covers.

    Eyebrow, `<h2>` and the one-line lede live in the `<summary>`, so scanning
    the page top to bottom answers "which of these do I need?" without opening
    anything.
    """
    section = page.split(f'id="{section_id}"', 1)[1].split("</section>", 1)[0]
    summary = section.split("<summary>", 1)[1].split("</summary>", 1)[0]
    assert 'class="aic-eyebrow"' in summary
    assert "<h2>" in summary
    assert 'class="hiw-lede"' in summary


def test_the_id_stays_on_the_section_not_the_details(page):
    """The eight anchors are an inbound-link contract, so they may not move onto
    the `<details>`: `#connect` has to keep resolving whether the fold is open
    or closed, and the script opens the fold that owns the target."""
    for section_id in COLLAPSED_SECTIONS:
        assert f'<section class="hiw-sec" id="{section_id}">' in page


def test_hash_opens_the_owning_fold(page):
    """A closed `<details>` cannot be scrolled into, so arriving at
    `/how-it-works#connect` — the chat landing's tools door, the rail row, the
    onboarding checklist, the tour, every redirected `/me/ai-connector`
    bookmark — has to open that section before the jump."""
    assert "function foldForHash" in page
    assert "function syncFoldToHash" in page
    assert "window.addEventListener('hashchange', syncFoldToHash)" in page
    # Resolved through the element, so `#cli` (a tab panel deep inside #connect)
    # finds the #connect fold without anything hard-coding the relationship.
    assert "closest('.hiw-fold')" in page


def test_collapsed_sections_keep_their_bodies_verbatim(page):
    """`.hiw-fold` hides content, it does not replace it with a summary.

    A summary drifts from what it summarises; the whole reason this page exists
    is that two pages explaining "how do I connect" had already drifted.
    """
    # The four pillars, the three beats and the four privacy rows, all still here.
    assert "hiw-pillar-t" in page
    assert "hiw-beat-n" in page
    assert "hiw-priv-row" in page
    # …and so are the four surface cards and the whole connector flow.
    assert "hiw-surface-ey" in page
    assert 'class="aic-modecard"' in page


def test_troubleshooting_is_not_open_by_default(page):
    """Eight failure modes expanded in front of a reader who has not yet tried
    anything — and it was the one block whose default state disagreed with its
    neighbour, which made "Advanced & reference" read as the lesser of the two."""
    assert 'class="aic-card aic-help" open' not in page
    assert 'class="aic-card aic-help"' in page


def test_the_setup_offer_is_not_restated_a_third_time(page):
    """The hero, the #surfaces cards (whose CTAs link down into #connect) and
    the mode tabs themselves already make the connector-or-CLI offer. #connect's
    lede used to make it again in prose, immediately above the tabs that make it
    a fourth time as their own labels — and working out whether a re-statement
    is a new choice or the same one is most of what made this page feel heavy.
    """
    assert "Two ways in, one knowledge layer behind both" not in page
    # The lede now carries only what is new: that setup is optional…
    assert "Optional &mdash; chat above already works" in page or "Optional — chat above already works" in page


def test_setup_pre_resolves_the_choices_it_can(page):
    """Setup asked up to three questions (mode → tool → OS) before showing one
    instruction. Two of them the page can answer itself.

    The OS default mattered most: it was `unix`, and the failure is silent —
    a `curl … | bash` line pasted into PowerShell simply does not work.
    """
    assert "function detectOs" in page
    assert "navigator.userAgentData" in page
    # The six-way tool choice survives a reload rather than being re-picked.
    assert "agnes.hiw." in page
    assert "function showTool" in page


# ── Rail nav placement ───────────────────────────────────────────────


def test_rail_carries_the_orientation_entry(seeded_app, monkeypatch):
    """The rail chrome links to the page with brand-templated wording, OUTSIDE
    the `can_chat` gate — a caller with no chat grant needs the orientation more
    than anyone, not less.

    Asserted against the rail's own markup, not just the href: the topnav
    account menu links to the same page, so `href="/how-it-works"` alone would
    pass even if the layout switch silently failed to apply.
    """
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    body = seeded_app["client"].get("/how-it-works", headers=_auth(seeded_app["analyst_token"])).text
    rail = body.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

    assert 'href="/how-it-works"' in rail
    assert ">How Agnes works</a>" in rail
    # The stale wording from when this pointed at /home must not come back.
    assert "Learn how it works" not in rail


def test_rail_orientation_entry_takes_no_row_and_is_not_on_the_brand(seeded_app, monkeypatch):
    """It is an account-menu item, NOT a nav row and NOT an icon on the wordmark.

    Three placements were tried and rejected, and each retirement is guarded here
    so it cannot quietly return:
      * `.rail-i` — a full destination row (34px ladder, 18px icon, accent tint)
        for a page you open once or twice, reading as a fourth
        Library/Agents/Admin;
      * `.rail-meta` — a quieter row in the foot; still a row, and orphaned
        between the onboarding card and the account;
      * `.rail-help` — a `?` beside the wordmark; the lockup is an identity
        element, not a control surface.

    The menu is the right shelf because the precedent was already in it: "Start
    over onboarding" is an orientation action too, not a fact about the caller.
    The two are grouped behind their own separator.
    """
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    body = seeded_app["client"].get("/how-it-works", headers=_auth(seeded_app["analyst_token"])).text
    rail = body.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

    # None of the three retired treatments.
    assert "rail-meta" not in rail
    assert "rail-help" not in rail
    assert "rail-head" not in rail

    pos = rail.find('href="/how-it-works"')
    assert pos > -1
    tag = rail[rail.rfind("<a", 0, pos) : pos]
    assert "app-user-menu-item" in tag
    assert "rail-i" not in tag

    # Inside the account menu panel, which lives in the foot — outside
    # `.rail-collapsible`, so it survives the ≤1024px bar with the nav closed.
    assert -1 < rail.find('id="userMenuPanel"') < pos
    assert rail.find('id="rail-collapsible"') < rail.find('class="rail-foot"') < pos


def test_rail_orientation_entry_is_grouped_with_restart_onboarding(seeded_app, monkeypatch):
    """The two bearings items form their own group: personal items first
    (Profile, My activity), separator, then How Agnes works + Start over
    onboarding, separator, then Logout.

    Uses the admin fixture because "Start over onboarding" is `can_chat`-gated
    while the orientation link deliberately is not.
    """
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    body = seeded_app["client"].get("/how-it-works", headers=_auth(seeded_app["admin_token"])).text
    rail = body.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
    panel = rail[rail.index('id="userMenuPanel"') :]

    profile = panel.index('href="/me/profile"')
    activity = panel.index('href="/me/activity"')
    hiw = panel.index('href="/how-it-works"')
    logout = panel.index("Logout")

    # Personal pair, then the bearings pair, then Logout last.
    assert profile < activity < hiw < logout
    # A separator opens the bearings group — i.e. one falls between the personal
    # items and the orientation link.
    assert activity < panel.index("app-user-menu-sep") < hiw


# The chat dashboard's own first-run orientation link ("See how Agnes works",
# in the trust line at the foot of the empty state) is guarded in
# tests/test_web_chat_empty_state.py, which has the rail + chat-backend +
# chat-grant fixture that rendering /chat needs.


def test_page_uses_brand_short_not_a_hardcoded_name(page):
    """Vendor-agnostic: the repo is the public distribution, so no instance's
    name may be baked into the template. `instance_brand[_short]` is
    operator-configured, and the seeded default is "Agnes"."""
    assert "{{ instance_brand" not in page  # rendered, not leaked as source
