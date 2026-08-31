"""Chrome-layout switch (topnav/rail) + paper theme contract.

Three guarantees:

1. **Default-chrome regression guard** — with no theme/layout config,
   pages render the horizontal ``_app_header.html`` chrome and the
   ``blue`` palette exactly as before the paper redesign. Existing
   instances must see zero change without opting in.
2. **Opt-in rail layout** — ``AGNES_UI_LAYOUT=rail`` swaps the chrome
   for ``_app_rail.html`` (and only then).
3. **Paper theme registration** — ``AGNES_INSTANCE_THEME=paper`` stamps
   ``data-theme="paper"`` and the token sheet actually defines the
   palette block, so the value can't silently no-op.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.instance_config import get_instance_theme, get_ui_layout


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=PasswordHasher().hash(password),
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    return {"access_token": resp.json()["access_token"]}


class TestResolvers:
    @pytest.fixture(autouse=True)
    def _reset_ui_layout_warn_once(self):
        """`get_ui_layout()`'s retired-value warning is gated by a
        module-global, once-per-process guard (`_warned_once_keys`) — reset
        it before every test in this class so one test setting a retired
        value (e.g. `test_ui_layout_topnav_value_is_inert`) can't silently
        suppress the warning another test asserts on."""
        import app.instance_config as ic

        ic._warned_once_keys = set()
        yield

    def test_ui_layout_defaults_to_rail(self, monkeypatch):
        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        assert get_ui_layout() == "rail"

    def test_configured_rail_value_still_resolves_to_rail(self, monkeypatch):
        """Explicitly configuring the current value is a harmless no-op —
        the symmetric case to `test_ui_layout_topnav_value_is_inert` below
        (the OLD value is also inert): ANY configured value, current or
        retired, resolves to "rail"."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        assert get_ui_layout() == "rail"

    def test_ui_layout_typo_falls_back(self, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "sidebar")
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        assert get_ui_layout() == "rail"

    def test_ui_layout_topnav_value_is_inert(self, monkeypatch):
        """`topnav` used to be a valid value; the chrome switch is retired
        (Wave 0, 2026-08), so it no longer has any effect — rail is
        unconditional regardless of what's configured."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "topnav")
        assert get_ui_layout() == "rail"

    def test_ui_layout_retired_value_warns_once(self, monkeypatch, caplog):
        """A configured (retired) `instance.ui_layout`/`AGNES_UI_LAYOUT` value
        logs a single warning per process, not once per resolver call — the
        resolver runs on every page render, so a per-call warning would flood
        the log for the lifetime of a misconfigured instance."""
        import logging

        monkeypatch.setenv("AGNES_UI_LAYOUT", "topnav")
        with caplog.at_level(logging.WARNING, logger="app.instance_config"):
            assert get_ui_layout() == "rail"
            assert get_ui_layout() == "rail"
        assert caplog.text.count("is retired") == 1

    def test_theme_defaults_to_paper(self, monkeypatch):
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        assert get_instance_theme() == "paper"

    def test_theme_accepts_paper(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        assert get_instance_theme() == "paper"

    def test_theme_typo_falls_back_to_paper(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "papier")
        monkeypatch.delenv("AGNES_INSTANCE_EXPERIENCE", raising=False)
        assert get_instance_theme() == "paper"

    def test_explicit_blue_theme_still_wins(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "blue")
        assert get_instance_theme() == "blue"

    def test_classic_experience_falls_back_to_redesign(self, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_EXPERIENCE", "classic")
        from app.instance_config import get_experience

        assert get_experience() == "redesign"

    def test_default_footer_is_config_driven_not_keboola(self, web_client, admin_cookie, monkeypatch):
        """Default (blue/topnav) instances render the shared config-driven
        footer and no vendor credit. Regression guard for the #896 footer leak.

        The assertion used to be `"AI Harness" in resp.text`, which passed for
        the wrong reason: that string is the *fallback* the footer printed
        because INSTANCE_COPYRIGHT was hardcoded empty in the context builder,
        and it also appears in every <title>. Pin the structure instead."""
        import app.instance_config as ic

        monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        monkeypatch.delenv("AGNES_INSTANCE_COPYRIGHT", raising=False)
        # The "no credit" half needs the YAML layer isolated too, or this fails
        # only for developers whose own config/instance.yaml sets one.
        monkeypatch.setattr(ic, "_instance_config", {})
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="site-footer' in resp.text
        assert "<b>Keboola</b>" not in resp.text
        # No credit configured → no attribution line invented.
        assert "Deployed by" not in resp.text


class TestRedesignIsTheOnlyExperience:
    """The redesign contract this wave installs: rail is unconditional, and
    a configured ``topnav``/``classic`` value is tolerated but inert — no
    default-parity guard survives it (Wave 0, 2026-08 legacy retirement).
    Replaces ``TestDefaultChromeUnchanged`` and the topnav-vs-rail pairs
    that used to live in ``TestRedesignedPageContracts`` (formerly
    ``TestDefaultContentParity``) and ``TestDetailPageParity``, whose
    entire premise (a second, classic chrome existing to keep parity with)
    this wave retires."""

    def test_default_renders_rail_chrome(self, web_client, admin_cookie):
        html = web_client.get("/library", cookies=admin_cookie).text
        assert 'data-ui-layout="rail"' in html
        assert 'class="rail"' in html  # _app_rail.html rendered
        # NOT `"_app_header" not in html`: the template is deleted, so a
        # stale include would raise TemplateNotFound at render time rather
        # than emit that literal — no rendered page could ever contain it,
        # so that assertion could never fail. Assert on what the header
        # actually rendered instead (the marker `TestRailOptIn` still uses
        # to prove the rail, not the header, is what's on screen).
        assert 'class="app-header"' not in html

    def test_topnav_value_is_inert(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "topnav")
        html = web_client.get("/library", cookies=admin_cookie).text
        assert 'data-ui-layout="rail"' in html


class TestRailBodyClearance:
    """The 240px body padding must be tied to the rail actually rendering.

    `data-ui-layout="rail"` is stamped on <html> from instance config, but the
    rail nav renders only for a signed-in user — so an unconditional padding
    survived onto pre-auth pages that have no rail, and `/login/password`
    centred its card inside a box shifted 240px right of the viewport (#1170).
    """

    def test_pre_auth_page_carries_no_rail(self, web_client, monkeypatch):
        """The premise of the fix: on a logged-out page the rail is absent
        while the layout attribute is still stamped."""
        resp = web_client.get("/login/password")
        assert resp.status_code == 200
        assert 'data-ui-layout="rail"' in resp.text, "layout attribute should still be stamped"
        assert 'class="rail"' not in resp.text, "the rail nav must not render pre-auth"

    def test_body_clearance_is_conditional_on_the_rail(self, web_client):
        css = web_client.get("/static/css/rail.css").text
        assert 'html[data-ui-layout="rail"] body {' not in css, (
            "unconditional body padding is back — it applies on pre-auth pages that render no rail (#1170)"
        )
        assert 'html[data-ui-layout="rail"] body:has(.rail) {' in css

    def test_narrow_override_matches_the_desktop_selector(self, web_client):
        """`:has()` takes its argument's specificity, so the ≤1024px override
        must carry `:has(.rail)` too. A plain `body` there would lose to the
        desktop rule and keep reserving 240px in the top-bar layout — where the
        rail is a static block and the reservation is pure dead margin."""
        css = web_client.get("/static/css/rail.css").text
        narrow = css[css.index("@media (max-width: 1024px)") :]
        assert 'html[data-ui-layout="rail"] body:has(.rail) {\n        padding-left: 0;' in narrow, (
            "the narrow-screen override no longer matches the desktop rule's specificity"
        )

    def test_clearance_is_published_beside_every_padding_that_encodes_it(self, web_client):
        """`position: fixed` chrome cannot inherit the body padding — it is laid
        out against the viewport — so it reads the same edge from
        `--rail-clearance`. Every rule that sets the padding must set the
        variable to the SAME value in the same block, or the two drift and the
        fixed chrome ends up somewhere the content is not."""
        css = web_client.get("/static/css/rail.css").text
        # Only the blocks that actually declare the padding — a `body:has(.rail)`
        # rule carrying just the transition has no edge of its own to publish.
        blocks = [body for body in re.findall(r"body:has\(\.rail[^)]*\)\s*\{([^}]*)\}", css) if "padding-left:" in body]
        assert len(blocks) >= 3, blocks
        for body in blocks:
            pad = re.search(r"padding-left:\s*([^;]+);", body)
            var = re.search(r"--rail-clearance:\s*([^;]+);", body)
            assert pad and var, f"a rail clearance rule is missing one of the pair: {body!r}"
            # 0 and 0px are the same edge; compare numerically.
            assert float(pad.group(1).rstrip("px")) == float(var.group(1).rstrip("px")), body

    def test_no_fixed_chrome_hardcodes_the_expanded_rail_width(self):
        """The bug this guards: `.fbar-dock`, `.fbar-dock__veil` and `.ch-bulk`
        each pinned `left: 240px` under `html[data-ui-layout="rail"]`. 240px is
        the EXPANDED rail's width and nothing more, so once the rail could be
        collapsed to 56px the docked toolbar centred 184px right of its content
        and the veil's blur started 184px in — sliced off down its left edge.
        They read `--rail-clearance` now; a new literal is the same bug.

        rail.css is exempt because it OWNS the number — it is the one file that
        may say 240px, and the test above is what keeps the variable it
        publishes in step with the padding it declares."""
        offenders = []
        for path in sorted(Path("app/web/static/css").glob("*.css")):
            if path.name == "rail.css":
                continue
            # Declarations only — a comment recounting the old bug is not one.
            # Blanked in place (newlines kept) so line numbers stay reportable.
            code = re.sub(
                r"/\*.*?\*/",
                lambda m: "\n" * m.group(0).count("\n"),
                path.read_text(encoding="utf-8"),
                flags=re.S,
            )
            for lineno, line in enumerate(code.splitlines(), 1):
                if re.search(r"(?<![-\w])(left|right)\s*:\s*240px", line):
                    offenders.append(f"{path}:{lineno}: {line.strip()}")
        assert not offenders, "hardcoded rail offset — use var(--rail-clearance, 0px):\n" + "\n".join(offenders)


class TestRailOptIn:
    def test_rail_layout_swaps_chrome(self, web_client, admin_cookie, monkeypatch):
        # Probe a real rail landing surface (/library). /dashboard is no longer
        # a rail render target — it 302s to /chat or /library (see
        # TestDashboardLandingRedirect); /stack 302s to /library too (#1088).
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="rail"' in resp.text
        assert 'class="app-header"' not in resp.text
        assert 'data-ui-layout="rail"' in resp.text

    def test_rail_keeps_nav_contract(self, web_client, admin_cookie, monkeypatch):
        """Rail must carry the two-zone IA (Library + Agents as the bottom
        zone's flat destinations) and the same JS/id contract as the header:
        user menu, theme toggle."""
        resp = web_client.get("/library", cookies=admin_cookie)
        text = resp.text
        for anchor in (
            'id="userMenu"',
            'id="themeToggle"',
            # Library — private uploads (moved off My Stack) + future data apps.
            'href="/library"',
            # Agents — build an assistant out of the caller's stack. A primary
            # destination directly under Library (it used to sit one hover deep
            # inside the Studio dropdown).
            'href="/agents"',
            # brand lockup: the Agnes orb mark + the Agnes wordmark beside it.
            'class="rail-orb"',
            'class="rail-logo-txt"',
        ):
            assert anchor in text, f"rail chrome is missing {anchor}"
        # Rail nav items carry no WIP badge.
        assert 'class="rail-badge"' not in text
        # Library · Agents are the bottom zone, in that order, with no divider
        # between them (Admin is the only divided group — see
        # TestRailTwoZones). New chat renders only for chat-granted callers, so
        # it's not pinned here.
        assert 'class="rail-nav-sep"' not in text
        positions = [
            text.index('class="rail-nav rail-nav-bottom"'),
            text.index('href="/library"'),
            text.index('href="/agents"'),
        ]
        assert positions == sorted(positions), "rail nav items are out of order"
        # Catalog is a single flat destination — no nested subcategory tree.
        assert 'class="rail-sub"' not in text

    def test_rail_has_no_my_stack_entry(self, web_client, admin_cookie, monkeypatch):
        """My Stack is retired out of the rail, not merely demoted (#1088) —
        /stack is no longer a rendering route at all, it 302s into the
        Library. Asserted against the rail chrome slice, not the whole
        document: the Library's own body legitimately mentions the stack
        throughout (the "In stack only" toggle)."""
        text = web_client.get("/library", cookies=admin_cookie).text
        nav = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/stack"' not in nav
        assert "My Stack" not in nav
        # ...and the route it used to point at now redirects rather than
        # rendering, 404ing, or requiring a fresh bookmark.
        resp = web_client.get("/stack", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library?stack=in_stack"

    def test_library_answers_the_stack_question_in_its_toolbar(self, web_client, admin_cookie, monkeypatch):
        """No cross-link to /stack in the Library header — the toolbar's "In
        stack only" toggle answers "what can the default agent use?" against
        this same list, so a header link would point at a narrower view of the
        rows already on screen.

        This toggle is what makes the removal safe, so it is the thing worth
        pinning: if it stops answering the Stack question, My Stack needs an
        entry point again (#1088).

        The toggle is deliberately conditional — it renders only when flipping
        it would change the page (`0 < in_stack < total`), so it is absent when
        everything is in the Stack or nothing is, both cases where filtering is
        a no-op. Asserting its presence outright would just pin the fixture's
        membership mix, so this asserts the equivalence instead."""
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        # Bounded by the browsing block, which is the next landmark after the
        # header. It used to slice to `class="fbar-dock"`, which /library has not
        # rendered since #1751 — so the slice silently ran to the end of the
        # document and the assertion below covered the whole page instead of the
        # header.
        head = text.split('class="lib-head"', 1)[1].split('class="lib-browse"', 1)[0]
        assert 'href="/stack"' not in head

        rows = re.findall(r"<tr[^>]*\bdata-item-id=[^>]*>", text)
        top_level = [r for r in rows if "data-parent-id=" not in r]
        in_stack = [r for r in top_level if 'data-stack="in_stack"' in r]
        rendered = 'id="lib-stack-toggle"' in text
        assert rendered == (0 < len(in_stack) < len(top_level)), (
            f"stack toggle rendered={rendered} with {len(in_stack)}/{len(top_level)} rows in stack — "
            "it must render exactly when flipping it would change the list"
        )
        if not rendered:
            return

        # It is a toolbar button, NOT a row inside the Filter menu: the condition
        # is consequential enough to be visible at rest.
        assert "In stack only" in text
        assert 'data-facet="stack" data-facet-value="in_stack"' in text
        assert 'aria-pressed="false"' in text
        menu = text.split('id="lib-filter-menu"', 1)[1].split("</div>", 1)[0]
        assert "In stack only" not in menu, "the stack toggle must not also sit in the Filter menu"
        assert "fbar-menu__toggle" not in text, "retired in-menu toggle markup"

        # Order on the bar: Search · Filter · the "In stack only" toggle ·
        # Sort — search is the way into a library of any size, so it leads;
        # the refinements follow it.
        positions = [
            text.index('id="lib-search"'),
            text.index('id="lib-filter-btn"'),
            text.index('id="lib-sort"'),
        ]
        assert positions == sorted(positions), "search must lead the bar, before Filter and Sort"

    def test_stack_filter_is_a_pressed_toggle_not_a_segment(self, web_client, admin_cookie, monkeypatch):
        """The stack filter is the `.fbar-toggle` button, engine-owned; the
        acquisition question lives in the Filter menu.

        The fold first shipped a three-way Scope segment (All / Yours /
        Available to add): "Available to add" framed the Library as a shop,
        and a segment gave one FILTER tab-rank. Both narrowings are ordinary
        refinements now — "In stack only" as the bar's pressed-state button
        (the design system's pattern for a binary condition worth seeing at
        rest, count riding the button), "Not in stack yet" one level deep in
        the Filter menu (see REDIRECTED_UNDER_RAIL — the retired browse
        pages' `?scope=available` links arrive with that one applied).
        """
        text = web_client.get("/library", cookies=admin_cookie).text
        # Engine-owned via `control` — so Clear all and reset keep working.
        # Asserted on the config string because the button itself renders
        # only when flipping it would change the list.
        assert "control: '#lib-stack-toggle'" in text
        assert 'data-facet="availability"' in text, "the demoted acquisition filter must exist"
        # The SCOPE segment is gone, not merely hidden — a filter is not a tab.
        # A segmented control as such is fine again, and there is one: the
        # Knowledge / Capabilities tabs. That is the distinction this test was
        # always drawing — those ARE tabs, and they change which half of the
        # Library you are in rather than narrowing the list you are looking at.
        assert 'id="lib-scope"' not in text
        assert "container: '#lib-scope'" not in text

    def test_rail_has_no_studio_or_marketplace_entry(self, web_client, admin_cookie, monkeypatch):
        """Studio is retired from the rail and Marketplace is no longer a rail
        entry. Studio was a hover dropdown holding Agents (now its own top-level
        item), the Skill and Plugin builders (now reached from the Library
        header's "+ Add" menu) and a non-interactive "Corporate Memory builder"
        concept label. Both the trigger markup and the dead .rail-studio-*
        styling must be gone, not merely hidden."""
        text = web_client.get("/library", cookies=admin_cookie).text
        assert "rail-studio" not in text
        assert ">Studio<" not in text
        assert "Corporate Memory builder" not in text
        assert ">Marketplace<" not in text
        assert 'id="nav-catalog"' not in text
        # /catalog and /skills stay live routes — they are simply not rail
        # entries any more, so nothing in the rail should link to them.
        assert 'class="rail-i" href="/catalog"' not in text
        assert 'href="/skills"' not in text
        # The stylesheet carries no orphaned rules for the retired chrome —
        # the Studio dropdown, its "Maybe?" badge, or the group dividers.
        css = web_client.get("/static/css/rail.css").text
        assert "rail-studio" not in css
        assert "rail-badge--maybe" not in css
        assert "rail-nav-sep" not in css
        # Admin is live chrome, so its rule must be present rather than
        # absent — but it is now ONE plain link to /admin, so what survives is
        # the `.rail-admin` wrapper that carries the divider above it. The
        # flyout machinery it used to open is retired chrome like the rest,
        # and its rules must be gone (see TestRailAdminIsOnePlainLink).
        assert "rail-admin {" in css
        assert "rail-admin-summary" not in css
        assert "rail-admin-groups" not in css
        assert "rail-admin-flyout" not in css
        assert "rail-admin-sub" not in css
        # The retired /ask hero (#896) is gone: no rail nav item points at it,
        # and the Chat slot renders only when cloud-chat is actually reachable.
        assert 'href="/ask"' not in text
        # Global search IS in the rail. It shipped only in the topnav chrome
        # and was left out when the rail was first built, on the reasoning
        # that page-local boxes would cover it; retiring the topnav turned
        # that into a hole (nothing crossed tables / knowledge / documents),
        # so Wave 0 (2026-08) restored it above Zone 1 with the same two ids
        # global_search.js binds on.
        assert 'id="global-search"' in text
        assert 'id="globalSearchResults"' in text

    def test_rail_catalog_folds_into_the_library(self, web_client, admin_cookie, monkeypatch):
        """/catalog is no longer a browse surface.

        It rendered kind tabs (Data · Plugins · Memory · Recipes) over one
        grid of rows the Library already lists in full, off the same
        `StackResolver.browse()` — two destinations for one list. It now 302s
        into the Library with the Scope segment set to `available`, which is
        the question its tabs were really asking. Every detail route beneath
        it is untouched; this folded the shell only.
        """
        resp = web_client.get("/catalog", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library?scope=available"

    def test_library_page_hosts_uploads(self, web_client, admin_cookie, monkeypatch):
        """The caller's things live on /library — the renamed, widened former
        /artefacts. It carries the item count, the "+ Upload" affordance, the
        share dialog, and a "Data apps coming soon" badge on the Files band for
        the not-yet-built kind that will ship into that section."""
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "Library" in text
        # Item count (no section heading — the count stands alone) + the
        # create-upload modal trigger.
        assert ">Uploads<" not in text
        assert 'id="lib-item-count"' in text
        assert "data-new-upload" in text
        assert 'id="uploadModal"' in text
        # Owner-initiated sharing: the dialog a grant-backed row's Sharing badge
        # opens ships with the page — as the shared component every detail page's
        # badge opens, not the page-local copy this page used to carry.
        assert "js/components/share_dialog.js" in text
        assert "css/share_dialog.css" in text
        assert 'id="shareModal"' not in text, "the share dialog is a component, not page markup"
        # Every "add something" path sits behind ONE chevron button.
        assert 'id="lib-new-btn"' in text
        assert 'id="lib-new-menu"' in text
        # Matched on the closing bracket, not the closing tag: every item now
        # carries a `<small>` description, so the label is no longer the whole
        # span. Same trick the agent-template check below uses.
        for label in ("Build a skill", "Build a plugin", "Upload a file"):
            assert f">{label}<" in text
        # The page-head `.pnote` caveats are retired, and so are the two tinted
        # panels that replaced them. What is left above the inventory is ONE thin
        # row (the caveat about the list), and Data apps states its schedule as a
        # badge on the band it will ship into.
        assert 'class="pnote"' not in text
        assert "Content being prepared" not in text
        assert "lib-status" not in text
        assert 'class="lib-count-note"' in text
        assert ">More coming soon<" in text
        assert "lib-strip" not in text
        # The Data apps badge is NOT asserted here, and its absence is correct:
        # this instance has no files, so there is no Files band to carry it. The
        # schedule now lives on the section the kind will ship into, which means
        # an empty Library states it nowhere — a deliberate consequence of moving
        # it out of the page head. See
        # test_web_library_sharing.test_data_apps_schedule_is_a_badge_on_the_files_band,
        # which seeds content and asserts the badge for real.
        assert 'class="fbar-group__soon"' not in text
        # The banners these replaced are gone, class and all.
        assert "lib-soon" not in text
        assert "lib-apps" not in text
        # The "same knowledge, everywhere" connect banner closes the Library
        # header (it moved here from the My Stack header).
        assert 'class="cbn cbn--bar"' in text
        assert "Connect your AI tools to give them access to the same knowledge." in text
        # Agents are NOT a Library kind — they live on /agents. An agent
        # TEMPLATE is, though (store `type='agent'`, renamed in AGT-4), and its
        # menu item legitimately reads "Build an agent template" — so the guard
        # matches the closing tag rather than a prefix both strings share.
        assert 'data-kind="agent"' not in text
        assert ">Build an agent<" not in text
        assert ">Build an agent template<" in text

    def test_artefacts_redirects_to_library(self, web_client, admin_cookie, monkeypatch):
        """/artefacts was renamed to /library and redirects there, so old links,
        bookmarks and the onboarding tour keep working."""
        resp = web_client.get("/artefacts", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/library"

    def test_agents_page_renders_builder(self, web_client, admin_cookie, monkeypatch):
        """/agents hosts the agent builder (WIP): list + builder views, the
        server-rendered RBAC-scoped knowledge ingredients, and the
        capabilities hydration off everything available to the caller. Agent
        definitions persist SERVER-SIDE in the v103 agents registry, so they
        follow the user across devices and can be shared from the Library."""
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "Agents" in text
        # Both in-page views + the create affordance.
        assert 'id="ag-list-view"' in text
        assert 'id="ag-builder-view"' in text
        # The create affordance moved into the JS-rendered list (the old
        # server-rendered id="ag-new-btn" header button is gone), so assert on
        # the delegated hook the list renders instead.
        assert "data-ag-new" in text
        # Real ingredients: server-embedded knowledge JSON + client-side
        # capabilities hydrated from EVERYTHING available to the caller
        # (curated ∪ community store), not just what's already in their stack.
        assert 'id="ag-knowledge-data"' in text
        assert "pull('curated')" in text and "pull('flea')" in text
        assert "tab=my" not in text  # subscribed-only pool is retired
        # Knowledge now spans a third kind — the caller's files/artefacts —
        # alongside governed data + memory.
        assert "data, memory & files" in text
        # Available-but-not-yet-added marketplace items are surfaced and the
        # ones already in the stack are marked (not filtered out).
        assert "Agents can use this" in text
        assert "any plugin or skill available to you" in text.lower()
        # The "work in progress" notice is GONE (AGT-6). It said agents were
        # saved but that running them was still to come — and running them is
        # what the Chat button on each card now does, so the notice was
        # apologising for the one thing that had just been fixed. Its two
        # copies (server-rendered head + the builder's JS twin) went with it.
        assert "saved to your workspace" not in text
        assert "Work in progress" not in text
        assert "ag-localnote" not in text
        # Nothing regressed into the older, wronger phrasings either.
        assert "saved in this browser" not in text
        assert "where you can share them" not in text
        # ...and the card carries the action that replaced the apology.
        assert "/chat?agent=" in text

    def test_agents_page_opens_builder_from_query(self, web_client, admin_cookie, monkeypatch):
        """The builder is an in-page view, so the Library reaches it through
        query params: `?new=1` (the "Build an agent" CTA) lands straight in the
        builder on a fresh agent, and `?open=<id>` (the Library's agent cards)
        opens that agent. Without this the page always rendered the LIST and
        both deep links silently dead-ended one click short of the builder."""
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "routeFromQuery" in text
        assert "params.get('open')" in text
        assert "params.get('new')" in text
        # Boot routes through the query BEFORE falling back to the list.
        assert "if (!routeFromQuery()) renderList();" in text
        # One-shot — the param is stripped so a reload can't mint a second agent.
        assert "params.delete('new')" in text
        # `?new=1` shares the create path with the "+ New agent" button rather
        # than rendering an unsaved shell (the server owns the id).
        assert "createAgent(null)" in text

    def test_agent_builder_has_delete_action(self, web_client, admin_cookie, monkeypatch):
        """The builder can delete the agent it is configuring — previously the
        only Delete lived on the list card, so the detail view was a dead end
        for the one destructive action. It sits LEFT of the status button
        (Mark ready / Back to draft), reuses the list's `data-ag-del` hook and
        its DELETE /api/v1/agents/{id} handler, and — unlike the list card —
        confirms first, because here it is one button away from a primary
        action on the config the caller is looking at."""
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'class="cc-btn ag-del-btn" data-ag-del=' in text
        # Left of the status button, in the header branch that renders an
        # EXISTING agent's actions. Scoped to that branch on purpose: the
        # new-agent branch above it also mentions data-ag-status, so a
        # whole-file index comparison measures source order, not screen order.
        fn = re.search(r"function headActionsHtml\(a\) \{(.*?)\n  \}", text, re.S)
        assert fn, "headActionsHtml not found"
        existing = fn.group(1).rsplit("return statusPill(a) +", 1)[-1]
        assert existing.index('class="cc-btn ag-del-btn"') < existing.index("data-ag-status")
        # Confirms only for the builder button; the list card is unchanged.
        assert "window.confirm(" in text
        assert "t.classList.contains('ag-del-btn')" in text
        # Delete is for an agent that EXISTS. A never-saved placeholder shows
        # "Save as draft" instead and is discarded by leaving — offering both
        # would be two buttons for one outcome.
        assert "if (isNewAgent) {" in text
        # Deleting the open agent must clear the builder's editing state, or
        # the next open compares against a dead row's baseline.
        assert "baseline = null; isNewAgent = false;" in text

    def test_agents_page_has_no_default_agent_card(self, web_client, admin_cookie, monkeypatch):
        """/agents lists the caller's OWN agents only — the always-on baseline
        assistant is not a card here (it is configured from the caller's
        Stack — /library?stack=in_stack, #1088)."""
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert "ag-card--default" not in text
        assert "Default agent" not in text
        assert "ag-badge--system" not in text
        # With no agents yet the grid falls back to the build-your-first empty
        # state rather than a stack-derived card.
        assert "No agents yet" in text

    def test_agents_page_requires_auth(self, web_client, monkeypatch):
        resp = web_client.get("/agents", follow_redirects=False)
        assert resp.status_code in (302, 303, 307, 401, 403)

    def test_paper_theme_stamped(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'data-theme="paper"' in resp.text

    def test_paper_footer_is_config_driven_and_keeps_the_orb(self, web_client, admin_cookie, monkeypatch):
        """The redesign carries no vendor branding: the paper footer renders the
        same config-driven credit as every other chrome (an instance puts its
        own name there via INSTANCE_COPYRIGHT), while the orb favicon — a
        neutral product mark — stays redesign-only."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        monkeypatch.setenv("AGNES_INSTANCE_COPYRIGHT", "Acme Corp")
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Deployed by Acme Corp" in resp.text
        assert "<b>Keboola</b>" not in resp.text
        assert "img/agnes-orb.png" in resp.text


class TestRailChatHistory:
    """Rail chat-history migration (#896): the conversation history lives in the
    left rail, directly under New chat and present on every page (not just
    /chat). It is TWO collapsible sections — Pinned, then Chats — with no
    truncation and no "View all chats" control. The standalone "+ New chat"
    button is retired and the chat entry is renamed "New chat" (id="new-chat", so
    chat.js resets in place on /chat). All gated on can_chat. Topnav is
    unaffected — its in-page chat sidebar is unchanged."""

    def _enable_chat(self, web_client, monkeypatch):
        """Make can_chat true: chat enabled AND an explicit CHAT grant (admin
        god-mode does NOT short-circuit has_explicit_grant, so patch it)."""
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def test_rail_renders_history_section(self, web_client, admin_cookie, monkeypatch):
        self._enable_chat(web_client, monkeypatch)
        # Probe a NON-chat rail page — the history must render everywhere.
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        # History section + the reused chat list ids live in the rail.
        assert 'class="rail-history"' in text
        assert 'id="chat-list"' in text
        assert 'id="cloud-chat-empty-state"' in text
        # The chat entry is renamed and carries id="new-chat" (chat.js hook).
        assert 'id="new-chat"' in text
        assert "New chat" in text
        # The standalone +New chat button above the nav (old markup) is retired.
        assert 'class="rail-newchat"' not in text
        # The conversations are TWO collapsible sections (see
        # TestRailChatSections for the full contract).
        assert 'id="rail-pinned"' in text
        assert 'id="rail-chats"' in text
        # The heading is a quiet section LABEL, not the `.rail-i`-styled summary
        # row with a clock glyph that #896 shipped and retired: styled as a nav
        # destination, it read as a fourth Library/Agents/Admin.
        assert 'class="rail-i rail-history-summary"' not in text
        assert "rail-history-summary-txt" not in text
        assert "rail-history-caret" not in text
        # The recent feed is capped, and the way to the rest of it is the Chats
        # DESTINATION row above the lists — not an in-place expander (the retired
        # "Show more"), and no longer a link at the foot of the region either
        # (see TestRailChatsDestination for why that link had to go).
        assert "rail-history-more" not in text
        assert "Show less" not in text
        assert 'id="rail-view-all-chats"' not in text
        assert 'id="nav-chats"' in text
        assert 'href="/chats"' in text
        # The loader that fills the list off /chat is wired in.
        assert "js/rail_history.js" in text

    def test_rail_onboarding_row_hosts_the_panel(self, web_client, admin_cookie, monkeypatch):
        """Onboarding's rail presence is ONE row — the same `.rail-i` anatomy
        as every other destination, icon + label — at the head of the bottom
        zone, opening the checklist as a popover. It replaces the
        "Finish setup · N/5" text row (and, before that, the "Your journey"
        checklist inline at the bottom of the chat list): a row in a column of
        rows is easy to read past. Its own icon is a circular progress ring
        rather than a fixed glyph — no separate bar, no chevron (sibling rows
        don't carry one either, and it navigates rather than expands)."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        # This slot holds one of two rows, and it is the ANALYST one under
        # test here. An admin mid-chain gets the setup chain instead, so the
        # chain is stood down for this test — the state every member is always
        # in, and the state an admin reaches once their instance is set up.
        import app.web.router as _router

        monkeypatch.setattr(_router, "_admin_setup_rail", lambda: None)
        _router.templates.env.globals["admin_setup_rail"] = lambda: None
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        # Asserted against the rail chrome slice, not the whole document — the
        # page body is free to say "Get started" in its own copy.
        text = resp.text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        # The row + its popover. The ids are the JS contract chat_onboarding.js
        # and rail_history.js bind to, so they outlive the relabelling.
        assert 'id="rail-getstarted-toggle"' in text
        assert 'id="rail-getstarted-panel"' in text
        # Row anatomy: the ring (icon) + title + progress sentence.
        assert 'id="rail-getstarted-ring-fill"' in text
        assert 'id="rail-getstarted-title"' in text
        assert ">Set up Agnes<" in text
        assert 'id="rail-getstarted-count"' in text
        # The progress line renders EMPTY — a static "0 of 5" would flash the
        # wrong number at anyone mid-way through.
        assert '<span class="rail-getstarted-sub" id="rail-getstarted-count"></span>' in text
        # Retired anatomy: the horizontal bar and the chevron are gone — the
        # ring carries progress now, and this row navigates via the same
        # click as always rather than "expanding".
        assert "rail-getstarted-bar" not in text
        assert "rail-getstarted-chev" not in text
        # Retired labels.
        assert "Your Journey" not in text
        assert "Get started" not in text
        assert "rail-getstarted-check" not in text
        # The row lives in the FOOT — the zone that survives collapse — under
        # "Take Agnes to your tools" and above the profile. It is not part of the
        # nav proper: setup is a thing you finish and stop seeing, so it sits
        # with the other always-reachable rows rather than among destinations.
        foot = text.split('class="rail-foot"', 1)[1]
        row_pos = foot.find('class="rail-getstarted"')
        assert row_pos != -1, "the onboarding row belongs in the rail foot"
        assert foot.find('href="/how-it-works#connect"') < row_pos < foot.find('id="userMenu"'), (
            "the onboarding row belongs under 'Take Agnes to your tools', above the profile"
        )
        # …and nothing left it behind in the collapsible nav above.
        assert "rail-getstarted" not in text.split('class="rail-foot"', 1)[0]
        # The journey render target moved into the popover — and out of the list.
        journey_pos = text.find('id="chat-journey"')
        panel_pos = text.find('id="rail-getstarted-panel"')
        assert journey_pos != -1 and panel_pos != -1
        assert journey_pos > panel_pos, "#chat-journey must render inside the row's popover"
        assert text.find('id="chat-journey"', text.find('class="rail-history"'), panel_pos) == -1, (
            "#chat-journey must no longer sit in the chat-history section"
        )
        # Off /chat, the standalone mount fills the popover (a script AFTER the
        # rail chrome, so this one is checked against the whole document).
        assert "mountJourneyPanel" in resp.text

    def test_onboarding_row_styling_contract(self, web_client, admin_cookie, monkeypatch):
        """Same `.rail-i` treatment as every other row (hover wash, no
        standing tint) — the progress ring carries the DS's brand accent for
        its filled arc, and the row retires itself at 5/5."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        # No card chrome left on the button — it relies on `.rail-i` (added
        # directly in the markup) for its background/padding/hover treatment.
        btn = css.split('html[data-ui-layout="rail"] .rail-getstarted-btn {', 1)[1].split("}", 1)[0]
        assert "background" not in btn
        assert "--ds-accent-info-bg" not in btn
        fill = css.split('html[data-ui-layout="rail"] .rail-getstarted-ring-fill {', 1)[1].split("}", 1)[0]
        assert "stroke: var(--ds-primary)" in fill
        track = css.split('html[data-ui-layout="rail"] .rail-getstarted-ring-track {', 1)[1].split("}", 1)[0]
        assert "stroke: var(--ds-border)" in track
        # ...and it retires itself at 5/5 — nothing left to continue.
        assert ".rail-getstarted.is-complete {" in css
        assert "display: none" in css.split(".rail-getstarted.is-complete {", 1)[1].split("}", 1)[0]

    def test_onboarding_row_title_and_progress_are_js_driven(self, web_client, admin_cookie, monkeypatch):
        """ "Set up {brand}" until the first step lands, "Continue setup" after —
        and the ring's arc follows the same count.

        The product half of that title reads the brand seam (`brandShort()`,
        fed by `data-brand-short` on `#railGetStarted`) rather than a literal:
        the card is branded server-side and this function rewrites it, so a
        literal here would replace an operator's brand with "Agnes" one frame
        into the page. `tests/test_brand_prose_sweep.py` owns that invariant;
        this line only pins that the flip itself is still JS-driven."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        js = web_client.get("/static/js/chat_onboarding.js").text
        body = js.split("function updateGetStartedIndicator(", 1)[1].split("\n}", 1)[0]
        assert 'done > 0 ? "Continue setup" : `Set up ${brandShort()}`' in body
        assert "${done} of ${total} steps complete" in body
        assert "(done / total) * 100" in body
        assert "strokeDasharray" in body
        assert 'classList.toggle("is-complete"' in body

    def test_new_token_button_cancels_the_summary_toggle(self, web_client, admin_cookie, monkeypatch):
        """`+ New token` lives inside a <summary>, so it must cancel the disclosure.

        stopPropagation() alone is NOT enough and was the original bug: it keeps
        the click off ancestor listeners, but a <details> toggle is the summary's
        default ACTIVATION BEHAVIOUR, which only preventDefault() cancels. With
        just the former, minting a token also collapsed the section it was
        launched from.

        Rail-pinned: the <summary>-hosted token panel is the REDESIGNED
        profile's; the default chrome serves the frozen pre-redesign page
        (spec 2026-08-07 wave 2), whose classic panel has no disclosure.
        """
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        marker = 'id="new-token-btn"'
        assert marker in resp.text
        handler = resp.text[resp.text.index(marker) - 400 : resp.text.index(marker) + 400]
        assert "preventDefault()" in handler, "New token must cancel the <summary> default action, not only bubbling"
        assert "stopPropagation()" in handler

    def test_profile_menu_can_restart_onboarding(self, web_client, admin_cookie, monkeypatch):
        """The way back once the Finish setup row has retired itself at 5/5: the
        row's own "Start over" goes with it, so the profile menu — the one thing
        pinned to the rail in every state — carries the entry."""
        self._enable_chat(web_client, monkeypatch)
        rail = (
            web_client.get("/library", cookies=admin_cookie).text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]
        )
        assert 'id="rail-restart-onboarding"' in rail
        assert "Start over onboarding" in rail
        # Inside the profile menu panel, after Profile. The landmark used to be
        # "Learn how it works" (href="/home"), which no longer lives in this
        # menu — it became a rail row of its own pointing at /how-it-works, so
        # Profile is now the first item the entry must follow.
        panel_pos = rail.find('id="userMenuPanel"')
        profile_pos = rail.find('href="/me/profile"')
        entry_pos = rail.find('id="rail-restart-onboarding"')
        assert -1 < panel_pos < profile_pos < entry_pos
        # A <button>, not a link — it has no page to navigate to; the handler
        # lives in chat_onboarding.js.
        assert '<button type="button" class="app-user-menu-item app-user-menu-btn"' in rail

    def test_restart_onboarding_entry_is_chat_gated(self, web_client, admin_cookie, monkeypatch):
        """No chat grant → no onboarding row and nothing to restart."""
        resp = web_client.get("/library", cookies=admin_cookie)
        assert 'id="rail-restart-onboarding"' not in resp.text

    def test_the_conversation_zone_is_one_unlabelled_list(self, web_client, admin_cookie, monkeypatch):
        """ONE list: pinned rows first, then the feed, then the row that closes
        it. No section labels, no disclosures, no seam.

        Two <section>s survive because there are two RENDER TARGETS — chat.js and
        rail_history.js both route pinned rows to their own <ul> — but nothing may
        make them read as two lists. "Pinned" and "Recent" were a <button> with a
        caret, then an inert <h2>, and are now gone entirely: a pinned row is
        marked by its pin glyph, and that the feed is a slice is said by the
        "View all chats" row at the bottom, where a caller who has run out of rows
        is already looking."""
        self._enable_chat(web_client, monkeypatch)
        text = web_client.get("/library", cookies=admin_cookie).text
        rail = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

        # Both render targets are present…
        for sec, body in (("rail-pinned", "rail-pinned-body"), ("rail-chats", "rail-chats-body")):
            assert f'id="{sec}"' in rail
            assert f'id="{body}"' in rail
        assert 'id="pinned-chat-list"' in rail
        assert 'id="chat-list"' in rail
        # …and nothing labels or separates them.
        for gone in (
            "rail-chatsec-txt",
            "rail-chatsec-hd",
            ">Pinned<",
            ">Recent<",
            'id="rail-pinned-toggle"',
            'id="rail-chats-toggle"',
            "rail-chatsec-caret",
            'aria-controls="rail-pinned-body"',
            'aria-controls="rail-chats-body"',
        ):
            assert gone not in rail, f"the zone must read as one plain list ({gone})"
        # No gap either — a margin between the two would seam one list in half.
        css = web_client.get("/static/css/rail.css").text
        gap = css.split('html[data-ui-layout="rail"] .rail-chatsec + .rail-chatsec {', 1)[1].split("}", 1)[0]
        assert "margin-top: 0" in gap
        # Pinned leads, the empty state lives in the feed (the section that
        # survives a first run), and the "more" row closes the whole thing.
        assert (
            rail.find('id="rail-pinned"')
            < rail.find('id="rail-chats"')
            < rail.find('id="cloud-chat-empty-state"')
            < rail.find('class="rail-history-all"')
        )
        # Pinned starts hidden — with no label to head it, an empty section would
        # still contribute stray padding. Emptiness is the ONLY conditional left.
        pinned_open = rail[rail.find('<section class="rail-chatsec" id="rail-pinned"') :][:120]
        assert " hidden" in pinned_open, "the Pinned section must start hidden"

    def test_the_zone_has_no_section_headers_at_all(self, web_client, admin_cookie, monkeypatch):
        """There is no section label left to style, so there must be no rules for
        one — and the "more" row must be a ROW, sharing the conversation row's
        box rather than sitting under the list as a footer.

        This is the rail's colour rule doing the work (see
        test_accent_marks_where_you_are_and_nothing_else): the accent marks the
        active conversation, the neutral wash marks hovering a row. The closing
        row takes the wash, because it IS a row you hover and click; what
        separates it from a conversation is ink and weight, not a treatment of
        its own."""
        css = web_client.get("/static/css/rail.css").text
        # Every trace of the retired labels and their disclosure.
        for gone in (
            'html[data-ui-layout="rail"] .rail-chatsec-hd {',
            'html[data-ui-layout="rail"] .rail-chatsec-hd:hover {',
            'html[data-ui-layout="rail"] .rail-chatsec-hd:focus-visible {',
            'html[data-ui-layout="rail"] .rail-chatsec-txt {',
            'html[data-ui-layout="rail"] .rail-chatsec-caret {',
            "rail-chatsec.is-collapsed",
            "rail-chatsec-hd-wrap",
            "rail-chatsec-count",
        ):
            assert gone not in css, f"there are no section labels ({gone})"

        row = css.split('html[data-ui-layout="rail"] .rail-history-all {', 1)[1].split("}", 1)[0]
        # The conversation row's own metrics — same height ladder, left edge and
        # radius, so the list has no seam before its last entry.
        assert "min-height: var(--rail-row-h)" in row
        assert "padding: 4px 9px" in row
        assert "border-radius: 8px" in row
        # Quieter than a conversation, which is the only thing marking it as the
        # end of the list rather than another entry in it.
        assert "color: var(--ds-text-muted)" in row
        hover = css.split('html[data-ui-layout="rail"] .rail-history-all:hover {', 1)[1].split("}", 1)[0]
        assert "background: var(--rail-hover-bg)" in hover, "it is a row, so it takes the row wash"

    def test_any_date_boundary_stays_quieter_than_a_conversation(self, web_client, admin_cookie, monkeypatch):
        """A date boundary ("Older") is the only label this zone can still grow,
        now that the section headers are gone. It must not arrive wearing the
        uppercase-700 voice they used: one list with a single loud label in the
        middle of it reads as two lists, which is the seam this zone just removed.

        Conditional on the rail styling date headers at all: whether a capped
        feed is short enough to need no date labels is a separate call, and this
        guard is about the hierarchy that applies whenever they DO render."""
        css = web_client.get("/static/css/rail.css").text
        selector = 'html[data-ui-layout="rail"] .rail-history .cloud-chat-list-group-header {'
        if selector not in css:
            return  # the rail renders no date headers — nothing to keep in line
        block = css.split(selector, 1)[1].split("}", 1)[0]
        assert "text-transform: none" in block
        assert "letter-spacing: 0" in block
        assert "font-weight: 600" in block

    def test_sections_have_no_collapse_state_to_persist(self, web_client, admin_cookie, monkeypatch):
        """There is ONE owner for the section chrome — rail_history.js, loaded on
        every rail page INCLUDING /chat (where chat.js owns only the rows, and
        calls in through window.railChatSections) — and exactly one thing left for
        it to decide: which of the two sections renders, from how many rows each
        holds.

        The disclosure is gone, so the per-section open flag it persisted is gone
        with it. Nothing about this region belongs in localStorage any more, and a
        `is-collapsed` class or an `aria-expanded` on a heading would be state
        with no control to set it."""
        js = web_client.get("/static/js/rail_history.js").text
        assert "window.railChatSections" in js, "chat.js needs a seam to re-sync after a render"
        chat_js = web_client.get("/static/js/chat.js").text
        assert "window.railChatSections" in chat_js, "/chat must re-sync the sections it re-renders"
        # The emptiness rule is the survivor: Pinned only with rows, Recent
        # standing down only when pins exist and the feed is empty.
        assert "pinned-chat-list" in js and "sec.hidden" in js
        # No persisted open/closed state, and no disclosure wiring.
        assert "agnes.rail.chatsec." not in js, "there is no collapse state to persist"
        for gone in ("isSecOpen", "rail-pinned-toggle", "rail-chats-toggle", "is-collapsed"):
            assert gone not in js, f"the disclosure is retired ({gone})"
        css = web_client.get("/static/css/rail.css").text
        assert "rail-chatsec.is-collapsed" not in css

    def test_rail_history_absent_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """No chat reachability → no history section, no New chat item, no
        Finish setup row, no loader (matches the "Chat slot only when
        reachable" contract)."""
        # Chat is disabled by default in tests, so can_chat is False.
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="rail-history"' not in resp.text
        assert 'id="new-chat"' not in resp.text
        assert 'id="rail-getstarted-toggle"' not in resp.text
        assert "js/rail_history.js" not in resp.text


class TestRailTwoZones:
    """Rail IA: two fixed zones with the conversation list between them.

    Top zone   — New chat, then the Pinned + Chats sections.
    (scroll)   — the rest of both lists, inside .rail-history-body.
    Bottom     — Library · Agents, then Admin behind a divider, then the
                 onboarding card, then the profile pinned to the very bottom.

    The order is the whole point of the layout, so it is asserted as one
    top-to-bottom sequence rather than per-item.
    """

    def _enable_chat(self, web_client, monkeypatch):
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def _rail(self, web_client, admin_cookie):
        text = web_client.get("/library", cookies=admin_cookie).text
        return text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

    def test_zone_order(self, web_client, admin_cookie, monkeypatch):
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        sequence = [
            'class="rail-nav rail-nav-top"',  # zone 1
            'id="new-chat"',
            'id="nav-chats"',  # …the destination the lists belong to,
            'class="rail-history"',  # the conversations region…
            'id="rail-pinned"',  # …Pinned first,
            'id="chat-list"',  # …then Chats
            'class="rail-nav rail-nav-bottom"',  # zone 2
            'href="/library"',
            'href="/agents"',
            'class="rail-admin"',  # ...Admin behind a divider
            'class="rail-foot"',  # zone 3 — always reachable, never collapsed
            'href="/how-it-works#connect"',  # …take Agnes elsewhere,
            # Either row — the analyst journey or the admin setup chain, which
            # share this slot and this class. Anchored without the closing
            # quote so the chain's `class="rail-getstarted rail-setupchain"`
            # matches too; the assertion is about WHERE the row sits.
            'class="rail-getstarted',  # …then the onboarding row,
            'id="userMenu"',  # …then profile, at the very bottom
        ]
        positions = [rail.find(anchor) for anchor in sequence]
        assert -1 not in positions, [a for a, p in zip(sequence, positions) if p == -1]
        assert positions == sorted(positions), "rail zones are out of order"

    def test_admin_is_the_only_divided_group(self, web_client, admin_cookie, monkeypatch):
        """Admin carries the divider; Library/Agents and the recents do not —
        the two zones are separated by the scroll region between them, not by
        rule lines. Pinned in CSS because that is where the dividers live."""
        css = web_client.get("/static/css/rail.css").text

        def block_for(selector):
            body = css.split(selector, 1)[1].split("}", 1)[0]
            return body

        assert "border-top" in block_for('html[data-ui-layout="rail"] .rail-admin {')
        # The profile keeps its own divider (it is the very bottom row).
        assert "border-top" in block_for('html[data-ui-layout="rail"] .rail-user-menu {')
        # The conversations region draws none — the Pinned/Chats section labels
        # are separated by whitespace, not rules (see
        # test_chat_section_headers_are_labels_not_rows).
        assert "border-top" not in block_for('html[data-ui-layout="rail"] .rail-history {')
        assert "border-top" not in block_for('html[data-ui-layout="rail"] .rail-nav-bottom {')

    def test_accent_marks_where_you_are_and_nothing_else(self, web_client, admin_cookie, monkeypatch):
        """The rail's colour rule: the accent tint marks the ACTIVE row, hover is
        a neutral wash, and nothing carries a resting tint of its own.

        The rail has held every arrangement of this, so what the guard protects
        is the invariant rather than the values: exactly ONE thing in the column
        may own the accent. It broke when New chat took a standing tint while
        active rows were tinted too (one hue, four meanings), and again when
        hover took the accent while New chat still had it (a hovered row looked
        selected). Active wins the accent because it is persistent wayfinding;
        hover only has to be perceptible, since the pointer is already there."""
        css = web_client.get("/static/css/rail.css").text

        def block_for(selector):
            return css.split(selector, 1)[1].split("}", 1)[0]

        # One shared token per state, so the three rules of each cannot drift.
        assert "--rail-active-bg:" in css
        assert "--rail-hover-bg:" in css
        for selector in (
            'html[data-ui-layout="rail"] .rail-i.on {',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li.active,',
        ):
            assert "background: var(--rail-active-bg)" in block_for(selector), selector
        for selector in (
            'html[data-ui-layout="rail"] .rail-i:hover {',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id]:hover {',
        ):
            assert "background: var(--rail-hover-bg)" in block_for(selector), selector

        # Active owns the accent; hover is neutral. Exactly one owner.
        active_token = css.split("--rail-active-bg:", 1)[1].split(";", 1)[0]
        hover_token = css.split("--rail-hover-bg:", 1)[1].split(";", 1)[0]
        assert "--ds-primary" in active_token, "the active row must own the accent"
        assert "--ds-primary" not in hover_token, "hover must not spend the accent a second time"
        # If both states ever go neutral again, the stronger must be an ink-mix:
        # the surface ramp is not monotonic across themes (in dark, `sunken`
        # lands darker than `dim`), so the two would swap places.
        assert "--ds-surface-sunken" not in active_token

    def test_active_row_keeps_its_tint_on_hover(self, web_client, admin_cookie, monkeypatch):
        """Pointing at the conversation you are reading must not un-highlight it.

        This is a specificity trap, not a style choice. The conversation hover
        rule is `… .cloud-chat-list li[data-id]:hover` (five units) and the
        active rule is `… .cloud-chat-list li.is-active` (four), so the hover
        wash wins on the active row unless the active rule ALSO carries
        `:hover` selectors — meaning the open chat would visibly lose its accent
        exactly when you reached for it. The nav rows and Admin links tie on
        specificity, so source order already protects them."""
        css = web_client.get("/static/css/rail.css").text
        for selector in (
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id].is-active:hover',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id].active:hover',
        ):
            assert selector in css, f"missing {selector} — the open chat loses its tint on hover"
        # ...and they must resolve to the ACTIVE fill, not the hover one.
        block = css.split('html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id].active:hover', 1)[
            1
        ].split("}", 1)[0]
        assert "background: var(--rail-active-bg)" in block
        # Source order still has to keep `.rail-i.on` after `.rail-i:hover`,
        # since those two tie and nothing else separates them.
        assert css.index('html[data-ui-layout="rail"] .rail-i.on {') > css.index(
            'html[data-ui-layout="rail"] .rail-i:hover {'
        ), "`.rail-i.on` must come after `.rail-i:hover` — they tie on specificity"

        # The retired CTA treatments, in markup and CSS.
        assert "rail-newchat-item" not in css
        rail = self._rail(web_client, admin_cookie)
        assert "rail-newchat-item" not in rail

    def test_new_chat_is_an_ordinary_row(self, web_client, admin_cookie, monkeypatch):
        """New chat is a plain `.rail-i`, styled identically to Library / Agents /
        Admin, with NO treatment of its own.

        It held a dedicated `.rail-compose` control look in three variants —
        grey fill + border + tinted icon chip, solid `--ds-primary`, and pale
        `--ds-primary-light` — and all three are retired. The reason is the
        accent budget, not taste: a standing tint on one row meant hover could
        not use the accent (a hovered Library row became identical to New chat at
        rest) and `.on` needed an inset ring purely to separate two pale-blue
        things. Feedback that fires on every row beats decoration on one.

        Asserted as an absence, so a fourth variant cannot land without
        confronting `test_accent_marks_where_you_are_and_nothing_else`."""
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        # It carries the ORDINARY row class and its bare glyph, like every
        # other nav row — no wrapper span, no icon chip.
        assert re.search(r'class="rail-i[^"]*"\s+id="new-chat"', rail)
        assert "rail-compose" not in rail

        css = web_client.get("/static/css/rail.css").text
        # Selector forms, not the bare word: the stylesheet's New-chat section
        # names `.rail-compose` to record the three retired variants, and a guard
        # that bans the name would force that history to be deleted.
        for selector in (".rail-compose {", ".rail-compose:", ".rail-compose.", ".rail-compose-icon"):
            assert selector not in css, f"New chat must not have rules of its own ({selector})"
        # Whitespace, not a heading and not a treatment, separates it from the
        # list — the one thing that still marks the boundary.
        top = css.split('html[data-ui-layout="rail"] .rail-nav-top {', 1)[1].split("}", 1)[0]
        assert "margin-bottom" in top
        narrow = css.split("@media (max-width: 1024px)", 1)[1]
        # In the bar the column gap reads as a stray gap, so it is dropped.
        assert ".rail-nav-top {\n        margin-bottom: 0;" in narrow

    def test_rows_share_one_height(self, web_client, admin_cookie, monkeypatch):
        """Consistent row heights across the ladder: nav rows, conversation
        rows and the profile row all size off `--rail-row-h`."""
        css = web_client.get("/static/css/rail.css").text
        assert "--rail-row-h:" in css
        for selector in (
            'html[data-ui-layout="rail"] .rail-i {',
            'html[data-ui-layout="rail"] .rail-history .cloud-chat-list li[data-id] {',
            'html[data-ui-layout="rail"] .rail-user {',
        ):
            body = css.split(selector, 1)[1].split("}", 1)[0]
            assert "min-height: var(--rail-row-h)" in body, selector

    def test_recents_are_capped_by_a_destination_not_by_an_expander(self, web_client, admin_cookie, monkeypatch):
        """The region still fills the free space between the two zones and scrolls
        inside its own box, in ONE state — so the bottom zone never moves.

        What changed with /chats: the recent feed is capped again. The cap that
        was removed had nowhere to go (five rows on a screen with room for nine,
        plus a "Show more" whose only job was to undo a limit we imposed
        ourselves); this one hands the long tail to a page built for it. So the
        contract is: a cap, and a DESTINATION — never a second state of this
        list. The two-state machinery stays retired in CSS and in JS.

        The destination is `.rail-history-all`, a link at the foot of the region,
        with the Chats row in the nav zone as its COLLAPSED form — the link is
        inside the part of the rail that collapse hides, so the icon row stands in
        there and neither is ever conditional on having used it."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        css = web_client.get("/static/css/rail.css").text
        body = css.split('html[data-ui-layout="rail"] .rail-history-body {', 1)[1].split("}", 1)[0]
        assert "flex: 1 1 0" in body
        assert "overflow-y: auto" in body
        # min-height:0 (and no floor) so a short viewport shrinks the list rather
        # than pushing the bottom zone past the fold — `.rail` cannot scroll.
        assert "min-height: 0" in body
        # No collapsed/expanded pair, and no in-place expander.
        assert ".rail-history.is-expanded" not in css
        assert "rail-history-more" not in css
        js = web_client.get("/static/js/rail_history.js").text
        assert "recentsExpanded" not in js
        # The constructor CALL, not the bare word: the comment that records why
        # the observer was removed names it, and asserting on the name alone
        # would forbid explaining the removal.
        assert "new MutationObserver" not in js
        assert "applyTruncation()" not in js
        # The cap itself, and its destination.
        assert "slice(0, RAIL_RECENT_LIMIT)" in js
        # The destination is the foot-of-the-list link, and it is STATIC markup:
        # the renderer has no say in whether it shows, which is what made the
        # first version of it unreachable on a first run.
        assert 'html[data-ui-layout="rail"] .rail-history-all {' in css
        assert "rail-history-all" not in js, "the link must not be conditional on a render"


class TestRailChatsDestination:
    """/chats is reachable at BOTH rail widths, and every rail row has an icon.

    These two facts are one contract. The rail collapses to a 56px glyph strip —
    by default on /admin — so a row with no icon is a row that DISAPPEARS when it
    collapses. That is exactly how the way to /chats went missing once: the whole
    conversation region is text (two section labels plus titles), its only door
    out was a "View all chats" link inside it, and that link was itself hidden
    until the caller had a conversation. On an admin page the product therefore
    had NO path to the chat list, and on a first run it had none anywhere.

    Both halves are answered, and by different things:

      • EXPANDED — the lists are on screen and "View all chats" closes them. It
        is static markup now, so a first run gets the link with an empty list.
      • COLLAPSED — the lists cannot render in a glyph strip, so `#nav-chats`
        stands in for the whole zone: a `.rail-i` with a speech bubble, folded
        away again (`.rail-i--collapsed-only`) the moment the rail opens, so the
        two are never on screen together.

    On an ADMIN page the lists are not rendered at all, so the row keeps its
    place at every width — there is nothing there for it to hand off to.

    The icon rule is asserted directly, because the next text-only row would
    reintroduce the same bug in a different place."""

    def _enable_chat(self, web_client, monkeypatch) -> None:
        """Same recipe as TestRailHistory's — chat enabled AND an explicit grant
        (admin god-mode does not short-circuit has_explicit_grant)."""
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def _rail(self, web_client, admin_cookie, path: str = "/library") -> str:
        text = web_client.get(path, cookies=admin_cookie).text
        # NOT `class="rail"` — on an admin page the element carries the
        # server-rendered collapsed default too (`class="rail rail-icon-mode"`).
        return text.split('<nav class="rail', 1)[1].split("</nav>", 1)[0]

    def test_every_rail_row_carries_an_icon(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        # Whole elements, so an icon is only credited to the row it is inside.
        # `.rail-i` is applied to both <a> (destinations) and <button> (the
        # onboarding row), and the class list may carry a modifier after it.
        rows = re.findall(
            r'<(a|button)\b[^>]*class="rail-i[^"]*"[^>]*>(.*?)</\1>',
            rail,
            re.DOTALL,
        )
        assert rows, "no rail rows found — the slice above must be wrong"
        for _tag, inner in rows:
            label = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
            assert "<svg" in inner, f"rail row {label!r} has no icon — it would vanish when collapsed"

    def test_chats_is_a_destination_row_in_the_nav_zone(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        zone = rail[rail.index('class="rail-nav rail-nav-top"') : rail.index('class="rail-history"')]
        assert 'id="nav-chats"' in zone
        assert 'href="/chats"' in zone
        assert ">Chats<" in zone
        # Peer of New chat, Library and Agents — an ordinary row, no treatment of
        # its own (the retired link was deliberately quieter; a destination is not).
        row = zone[zone.index('id="nav-chats"') - 200 : zone.index('id="nav-chats"')]
        assert "rail-i " in row or 'rail-i"' in row

    def test_the_two_forms_of_the_destination_are_exact_complements(self, web_client, admin_cookie, monkeypatch):
        """Expanded shows the link; collapsed shows the row; never both, never
        neither. The row's fold is what enforces "never both", and its DEFAULT
        must be folded — so it has to be declared outside the icon-strip media
        query, or the row would stand above the lists below 1025px, where there
        is no icon mode to fold it."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie)
        # Both forms are in the markup; CSS decides which one is on screen.
        assert '<a class="rail-history-all" href="/chats">View all chats</a>' in rail
        assert 'id="nav-chats"' in rail
        assert "rail-i--collapsed-only" in rail

        css = web_client.get("/static/css/rail.css").text
        assert 'html[data-ui-layout="rail"] .rail-history-all {' in css
        # The folded default, OUTSIDE the min-width media query.
        base_sel = 'html[data-ui-layout="rail"] .rail .rail-i--collapsed-only {'
        assert base_sel in css
        strip = css.split("@media (min-width: 1025px)", 1)
        assert base_sel in strip[0], "the folded default must hold below 1025px too"
        base = css.split(base_sel, 1)[1].split("}", 1)[0]
        assert "height: 0" in base
        assert "visibility: hidden" in base
        # …and the icon strip is the one exception that unfolds it.
        unfold = css.split('html[data-ui-layout="rail"] .rail.rail-icon-mode .rail-i--collapsed-only {', 1)[1].split(
            "}", 1
        )[0]
        assert "height: var(--rail-row-h)" in unfold
        assert "visibility: visible" in unfold
        # A peeked rail is showing the lists, so the stand-in folds again.
        peek = css.split(
            'html[data-ui-layout="rail"] .rail.rail-icon-mode:not(.rail-no-peek)'
            ":is(:hover, :focus-within) .rail-i--collapsed-only {",
            1,
        )[1].split("}", 1)[0]
        assert "height: 0" in peek
        assert "visibility: hidden" in peek

    def test_admin_pages_get_the_destination_but_not_the_lists(self, web_client, admin_cookie, monkeypatch):
        """The one place the rail's item set differs by context, and it differs in
        the safe direction: the Chats row (icon, survives collapse) is on every
        page; the text-only lists are only where they can be seen.

        Per PAGE, collapsed and expanded still hold the same set of destinations —
        that is the invariant. Two pages differing is a full repaint, not a shift."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        rail = self._rail(web_client, admin_cookie, "/admin/users")
        assert 'id="nav-chats"' in rail, "an admin must be able to reach their chats"
        assert 'id="new-chat"' in rail
        assert 'class="rail-history"' not in rail
        assert 'id="rail-pinned"' not in rail

    def test_the_analyst_onboarding_card_is_not_on_admin_pages(self, web_client, admin_cookie, monkeypatch):
        """It measures the ANALYST's journey — take Agnes to your tools, ask your
        first question — and it is the only element in the rail with a coloured
        progress arc, so it pulls hardest of anything on screen while you are
        registering a table. Nothing is lost: the checklist is still reachable from
        the account menu and from the chat dashboard's "Set up Agnes" door.

        The exclusion is of that journey, not of the slot. The admin's OWN
        chain now uses it (sibling test below), and the reason above is why
        that is the right way round rather than an exception to this rule: on
        an admin page the chain names the exact job you are there to do.
        """
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        import app.web.router as _router

        monkeypatch.setattr(_router, "_admin_setup_rail", lambda: None)
        _router.templates.env.globals["admin_setup_rail"] = lambda: None
        admin_rail = self._rail(web_client, admin_cookie, "/admin/users")
        assert 'id="railGetStarted"' not in admin_rail
        assert "rail-getstarted" not in admin_rail
        # …and the module that writes into it is not loaded there either.
        assert "js/chat_onboarding.js" not in web_client.get("/admin/users", cookies=admin_cookie).text
        # Still there on an app page — this is a scoping change, not a removal.
        assert 'id="railGetStarted"' in self._rail(web_client, admin_cookie)

    def test_the_admin_chain_takes_the_slot_while_it_is_unfinished(self, web_client, admin_cookie, monkeypatch):
        """An admin mid-chain had no view of their own progress anywhere but
        /chat, and on app pages the rail showed them the ANALYST checklist —
        six steps, none of them their job.

        The row switches on whose steps can currently succeed: until a source
        is connected and shared, "ask your first question" has nothing to
        answer from. It is server-rendered with its own ids, so
        chat_onboarding.js — whose every lookup is guarded — finds nothing and
        cannot write analyst numbers over admin ones.
        """
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        import app.web.router as _router

        chain = {
            "done": 1,
            "total": 6,
            "complete": False,
            "steps": [
                {"label": "Connect a source", "done": True, "failed": False, "href": "/admin/data-sources"},
                {"label": "Choose tables", "done": False, "failed": False, "href": "/admin/tables"},
                {"label": "Invite people", "done": False, "failed": True, "href": "/admin/users"},
            ],
        }
        _router.templates.env.globals["admin_setup_rail"] = lambda: chain
        try:
            for path in ("/library", "/admin/users"):
                rail = self._rail(web_client, admin_cookie, path)
                assert 'id="railSetupChain"' in rail, f"the chain is missing on {path}"
                assert "1 of 6 steps complete" in rail
                assert "Connect a source" in rail and "Choose tables" in rail
                # The analyst row stands down — one journey VISIBLE in the
                # slot, which is the whole reason this row exists. It is
                # present in the document and marked as the standby occupant
                # (rail.css hides it until the admin asks for it), because a
                # row that is absent is a row the profile menu's "Start over
                # onboarding" cannot reveal — it reset journey state into an
                # empty rail, which is how that control came to do nothing.
                if path == "/library":
                    assert 'id="railGetStarted"' in rail
                    assert "data-chain-alternate" in rail, (
                        "the analyst row is not marked standby — it would render "
                        "alongside the chain as a second progress ring"
                    )
                else:
                    # Admin pages never carry the analyst row at all, chain or
                    # no chain — a half-finished analyst checklist is not
                    # relevant while you are registering a table.
                    assert 'id="railGetStarted"' not in rail
                # A step whose own check raised says so instead of reading as
                # an ordinary open step on a healthy chain.
                assert "could not check" in rail
        finally:
            _router.templates.env.globals["admin_setup_rail"] = _router._admin_setup_rail

    def test_the_chain_is_wired_end_to_end_without_a_stub(self, web_client, admin_cookie, monkeypatch):
        """The two tests around this one inject the chain through the Jinja
        global, which proves the ROW renders and proves nothing about how the
        row gets its data.

        That gap is not hypothetical: it let a build pass every test while the
        row appeared on no page at all, and the twenty minutes that followed
        went on debugging a template that was fine. So this one calls the real
        registered global — resolver, memo and registration included — and
        only asserts the shape, because the numbers depend on the fixture's
        instance and pinning them would make this a test of the seed data.
        """
        import app.web.router as _router

        chain = _router.templates.env.globals["admin_setup_rail"]()
        assert chain is not None, "the chain resolver returned nothing on a seeded instance"
        assert chain["total"] > 0
        assert isinstance(chain["steps"], list) and chain["steps"]
        for st in chain["steps"]:
            assert st["label"], "a step with no label renders as an empty row"
            assert st["href"], "every step is a door — one with no href is a dead line of text"

    def test_the_chain_can_be_hidden_and_brought_back(self, web_client, admin_cookie, monkeypatch):
        """The chain gets the analyst card's PAIR of controls: a quiet way out
        in its own panel, and a way back in the profile menu once it is gone.

        Everything asserted here is markup + wiring, because that is where the
        pair can silently come apart. The dismissal itself is a per-browser
        localStorage flag — the chain's six steps are readings of instance
        state, so "skip" cannot mean "write them done" the way the analyst
        journey's can, and there is no per-user preference table to hold it.

        The no-flash half is the point of the inline script: `defer` would hide
        the card only after first paint, so a dismissed card would flash back
        on every single page load.
        """
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        import app.web.router as _router

        _router.templates.env.globals["admin_setup_rail"] = lambda: {
            "done": 1,
            "total": 6,
            "complete": False,
            "steps": [{"label": "Connect a source", "done": False, "failed": False, "href": "/admin"}],
        }
        try:
            for path in ("/library", "/admin/users"):
                page = web_client.get(path, cookies=admin_cookie).text
                rail = self._rail(web_client, admin_cookie, path)
                # The panel's own control, and the JS seam it hangs on.
                assert "data-setupchain-skip>" in rail, f"no way to skip the chain on {path}"
                assert "Skip setup" in rail
                # The way back. Rendered under the same gate as the card, so
                # the two never ship apart; rail.css keeps it out of sight
                # until the dismissal is actually stored.
                assert 'id="rail-restart-setupchain"' in rail, f"no way back on {path}"
                assert "Show setup checklist" in rail
                # …and on an app page it sits with the analyst equivalent, not
                # somewhere else in the menu: the two are one kind of action,
                # and together they are the switch between the slot's two
                # cards. On an admin page the analyst entry is gone (the row it
                # re-arms is not rendered there), so there is no pair to order.
                if path == "/library":
                    assert rail.index("rail-restart-onboarding") < rail.index(
                        "rail-restart-setupchain"
                    )
                else:
                    assert "rail-restart-onboarding" not in rail
                # The module that wires both. Asserted on the whole PAGE, not
                # the rail slice: the rail's scripts sit after `</nav>`, which
                # is exactly where `_rail()` cuts.
                assert "rail_setupchain.js" in page, f"neither control is wired on {path}"

                # The pre-paint guard, and the pair of ids it governs.
                assert "agnes.setupchain.skipped" in rail
                # Pre-paint suppression must be INLINE — a `src=` script is
                # `defer`red and so runs only AFTER first paint, which is the
                # flash this guard exists to prevent.
                #
                # Asserted on the LAST script element before the card, and on
                # its BODY, because the two obvious weaker forms both pass the
                # bug: the rail opens with an inline `<script>` of its own
                # ~600 lines above, so `"<script>" in head` is true whatever
                # this guard is, and the key string survives being moved onto
                # an attribute of an external tag (`data-key="…"`), so the
                # assertion above does not pin it either. Confirmed by
                # mutation: rewriting the guard as
                # `<script src=… data-key="agnes.setupchain.skipped" defer>`
                # left both of those green.
                head = rail.split('id="railSetupChain"', 1)[0]
                guard = head[head.rindex("<script") :]
                assert "src=" not in guard, (
                    f"the pre-paint guard is an external script, so it cannot beat "
                    f"first paint: {guard[:120]}"
                )
                assert 'localStorage.getItem("agnes.setupchain.skipped")' in guard, (
                    "the script before the card does not read the dismissal — the "
                    f"card will flash back on every load: {guard[:120]}"
                )
        finally:
            _router.templates.env.globals["admin_setup_rail"] = _router._admin_setup_rail

    def test_the_two_cards_are_a_switch_not_a_stack(self, web_client, admin_cookie, monkeypatch):
        """One card VISIBLE in the slot, and the stored flag picks which.

        Two six-step journeys with different numerators side by side is the bug
        the one-slot design exists to prevent, and it still cannot happen — but
        the mechanism moved. It used to be "render only one", which made the
        profile menu's "Start over onboarding" a dead control for an admin
        mid-chain: it reset the journey server-side and there was no card in the
        document for the result to appear in. Now both render and CSS picks,
        which is what gives that entry something to reveal.

        The complementary pair is the whole invariant, so both halves are
        asserted here: miss the second rule and the two cards render stacked.
        """
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        import app.web.router as _router

        _router.templates.env.globals["admin_setup_rail"] = lambda: {
            "done": 1,
            "total": 6,
            "complete": False,
            "steps": [{"label": "Connect a source", "done": False, "failed": False, "href": "/admin"}],
        }
        try:
            rail = self._rail(web_client, admin_cookie)
            # Both in the document…
            assert 'id="railSetupChain"' in rail
            assert 'id="railGetStarted"' in rail
            # …and the analyst one marked as the standby.
            standby = rail.split('id="railGetStarted"', 1)[1].split(">", 1)[0]
            assert "data-chain-alternate" in standby
        finally:
            _router.templates.env.globals["admin_setup_rail"] = _router._admin_setup_rail

        css = (Path(__file__).resolve().parents[1] / "app/web/static/css/rail.css").read_text()
        rail_scope = 'html[data-ui-layout="rail"]'
        # No flag → the standby is hidden, so the chain is alone on screen.
        assert (
            f'{rail_scope}:not([data-setupchain-skipped="1"]) '
            ".rail-getstarted[data-chain-alternate] {" in css
        ), "nothing hides the standby card — both journeys would render stacked"
        # Flag → the chain is hidden, so the standby is alone on screen.
        assert f'{rail_scope}[data-setupchain-skipped="1"] .rail-setupchain {{' in css

    def test_the_restart_entry_is_absent_where_it_could_not_work(
        self, web_client, admin_cookie, monkeypatch
    ):
        """"Start over onboarding" is gated on the card it re-arms.

        On an admin page the analyst row is deliberately absent and
        chat_onboarding.js — which owns the click — is not loaded, so the entry
        was a menu item that reset journey state and showed nothing. The switch
        fixes that everywhere the row can render; here the honest fix is not to
        offer it.
        """
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        admin_page = self._rail(web_client, admin_cookie, "/admin/users")
        assert 'id="rail-restart-onboarding"' not in admin_page
        # …and present on an app page, where the row it re-arms exists.
        assert 'id="rail-restart-onboarding"' in self._rail(web_client, admin_cookie)

    def test_both_launcher_cards_share_one_popover_implementation(self):
        """The chain card must be openable and collapsible, like its twin.

        It reuses `.rail-getstarted` for its LOOK but carries its own ids so
        chat_onboarding.js cannot write analyst numbers into it — and those
        distinct ids also took it out of the only wiring that made the panel
        clickable, which lived inline in rail_history.js bound to
        `rail-getstarted-*`. The chain could be previewed on hover and never
        opened, pinned or collapsed: two cards that are the same object
        visually, behaving differently, the second one worse.

        Asserted structurally rather than by driving a browser: that the
        behaviour has ONE definition and both cards call it. A copy in the
        second file would pass any behavioural test and drift on the next fix.
        """
        js = Path(__file__).resolve().parents[1] / "app/web/static/js"
        helper = (js / "rail_popover.js").read_text()
        history = (js / "rail_history.js").read_text()
        chain = (js / "rail_setupchain.js").read_text()

        # The behaviour lives in the helper…
        for behaviour in ("is-open", "is-closed", "Escape", "mouseleave", "aria-expanded"):
            assert behaviour in helper, f"the shared popover lost its {behaviour} handling"

        # …and BOTH cards reach it through the same call.
        assert "railPopover.wire" in history, "the analyst card no longer uses the shared popover"
        assert "railPopover.wire" in chain, "the chain card is not wired for click/collapse"

        # No second copy: `.is-closed` is the tell — it is the one class only
        # this behaviour touches, so a file setting it is a file that
        # reimplemented the behaviour rather than calling it. Matched as a
        # string LITERAL, not as prose: both files discuss the class in
        # comments (rail_setupchain.js's explains why it stopped setting it by
        # hand), and a guard that cannot tell code from a comment about code
        # fires on the very note that documents the fix.
        for name, src in (("rail_history.js", history), ("rail_setupchain.js", chain)):
            assert '"is-closed"' not in src and "'is-closed'" not in src, (
                f"{name} manipulates `.is-closed` directly — that is the shared "
                "popover's business, and a second copy of it will drift"
            )

    def test_both_panels_advertise_the_collapse(self):
        """A chevron, top right, on BOTH launcher panels.

        The panel closed four ways before this — a second click on the
        launcher, click-away, Escape, mouse-leave — and advertised none of
        them, so the card read as something that opens and then stays. The
        chevron is the only visible affordance for putting it away, which is
        why both panels must carry it and not just the one that happened to
        get looked at.

        The analyst panel's goes LAST in the actions row, past the ↻ replay
        button: the chevron acts on the panel, ↻ acts on the panel's contents,
        so the outermost control sits furthest right. Asserted by index rather
        than by eye, because the two are adjacent in one template string and a
        later edit reorders them without looking wrong.
        """
        js = Path(__file__).resolve().parents[1] / "app/web/static/js"
        rail_tpl = (
            Path(__file__).resolve().parents[1] / "app/web/templates/_app_rail.html"
        ).read_text()
        onboarding = (js / "chat_onboarding.js").read_text()

        # The admin chain's, in its own head row above the steps.
        head = rail_tpl.split('id="rail-setupchain-panel"', 1)[1].split("<ul", 1)[0]
        assert "data-rail-popover-collapse" in head, (
            "the chain panel has no collapse chevron — it opens and cannot be told to close"
        )

        # The analyst panel's, after the replay button.
        actions = onboarding.split("cloud-chat-journey-actions", 1)[1].split("</div>", 1)[0]
        assert "data-rail-popover-collapse" in actions
        assert actions.index("data-journey-replay") < actions.index(
            "data-rail-popover-collapse"
        ), "the chevron must sit to the RIGHT of the refresh button"

        # Rail only: the inline /chat panel is part of the page, not a popover
        # hanging off a launcher, so it keeps "×" (a different action) instead.
        assert "data-journey-close" in actions

    def test_the_collapse_chevron_is_delegated_not_bound(self):
        """chat_onboarding.js rebuilds the analyst panel's innerHTML on every
        journey update, so a handler attached to the chevron would be discarded
        with the button the first time a step completed — the control would work
        until the user did something, then quietly stop. The listener lives on
        the document instead.

        Asserted because the failure is invisible: a per-button handler passes
        every "does the chevron close the panel" check on a freshly loaded page.
        """
        js = Path(__file__).resolve().parents[1] / "app/web/static/js"
        popover = (js / "rail_popover.js").read_text()
        onboarding = (js / "chat_onboarding.js").read_text()

        assert 'document.addEventListener("click"' in popover
        assert "data-rail-popover-collapse" in popover
        # The renderer supplies the attribute and nothing else — no
        # addEventListener on the chevron it just built.
        rendered = onboarding.split("data-rail-popover-collapse", 1)[1].split("</div>", 1)[0]
        assert "addEventListener" not in rendered

    def test_skip_and_collapse_are_different_words_for_different_things(self):
        """The chain's footer button says "Skip setup", matching the analyst
        card's "Skip onboarding", because it is the same outcome and the same
        gesture: the card goes for good, recoverable only from the profile menu.

        It read "Hide this checklist" first, on the argument that nothing is
        truly skipped — the work the chain names still has to happen. But that
        reasoning describes the instance and the button acts on the card, and
        to a reader "hide" promises exactly the collapse the chevron now
        provides. Two controls that do different things cannot both be called
        hiding.
        """
        rail_tpl = (
            Path(__file__).resolve().parents[1] / "app/web/templates/_app_rail.html"
        ).read_text()
        # `data-setupchain-skip>` with the closing bracket: the bare attribute
        # name is a PREFIX of the pre-paint flag `data-setupchain-skipped`, so
        # splitting on it lands in the inline script instead of on the button.
        skip = rail_tpl.split("data-setupchain-skip>", 1)[1].split("</button>", 1)[0]
        assert "Skip setup" in skip
        assert "Hide" not in skip, "the skip button reads as the collapse affordance"

    def test_the_hidden_state_is_styled_not_guessed(self):
        """Both halves of the flag live in rail.css: the card goes when it is
        set, and the menu entry arrives. Miss the second rule and the way back
        is permanently invisible — the card would be unrecoverable."""
        css = (Path(__file__).resolve().parents[1] / "app/web/static/css/rail.css").read_text()
        skipped = 'html[data-ui-layout="rail"][data-setupchain-skipped="1"]'
        assert f"{skipped} .rail-setupchain {{" in css
        assert f"{skipped} .rail-restart-setupchain {{" in css
        # Hidden by DEFAULT, or it offers to restore a card already on screen.
        assert 'html[data-ui-layout="rail"] .rail-restart-setupchain {\n    display: none;' in css

    def test_a_finished_chain_hands_the_slot_back(self, web_client, admin_cookie, monkeypatch):
        """Once the instance is set up the admin is also a user, and the
        analyst steps become things that can actually succeed. The row is not
        retired — it is handed back."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        import app.web.router as _router

        _router.templates.env.globals["admin_setup_rail"] = lambda: {
            "done": 6, "total": 6, "complete": True, "steps": [],
        }
        try:
            rail = self._rail(web_client, admin_cookie)
            assert 'id="railSetupChain"' not in rail
            assert 'id="railGetStarted"' in rail
        finally:
            _router.templates.env.globals["admin_setup_rail"] = _router._admin_setup_rail


class TestRailAdminIsOnePlainLink:
    """Admin in the rail is ONE plain link to /admin, on every page.

    It used to open a hover/focus flyout listing seven "areas", each with its
    own panel of links beside the column. That was a second, hand-written copy
    of the admin inventory in `app/web/admin_nav.py`, and it had already
    drifted from it: different labels, different grouping, and three
    `/documentation` links that are not admin pages at all (that route is
    gated by `get_current_user`, not `require_admin`). Two IAs for one section
    is a maintenance trap, and the flyout was the wrong half to keep — it could
    only ever be a menu, where `/admin` is a page that explains itself and
    carries those Documentation links on its own card grid.

    These tests are the replacement for four that pinned the flyout's
    anatomy (subitem rows, button-not-nested-details, positioned-not-inline,
    and the active/traced-area marking)."""

    def test_no_flyout_markup_on_a_non_admin_rail_page(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        rail = resp.text.split('<nav class="rail', 1)[1].split("</nav>", 1)[0]
        for gone in (
            "rail-admin-summary",
            "rail-admin-groups",
            "rail-admin-sub",
            "rail-admin-flyout",
            "rail-admin-caret",
            "<details",
        ):
            assert gone not in rail, gone

    def test_admin_row_links_to_the_hub_and_is_active_across_the_subtree(self, web_client, admin_cookie, monkeypatch):
        """Every /admin/* page IS this destination, so the row reads active
        across the whole subtree — not only on the hub itself."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        for path in ("/admin", "/admin/users"):
            resp = web_client.get(path, cookies=admin_cookie)
            assert resp.status_code == 200, path
            rail = resp.text.split('<nav class="rail', 1)[1].split("</nav>", 1)[0]
            admin_row = rail.split('href="/admin"', 1)[0].rsplit("<a ", 1)[1]
            assert "rail-i" in admin_row, path
            assert "on" in admin_row, path

        # ...and NOT active on a page outside it.
        resp = web_client.get("/library", cookies=admin_cookie)
        rail = resp.text.split('<nav class="rail', 1)[1].split("</nav>", 1)[0]
        admin_row = rail.split('href="/admin"', 1)[0].rsplit("<a ", 1)[1]
        assert " on" not in admin_row

    def test_documentation_is_still_reachable_from_the_hub(self, web_client, admin_cookie, monkeypatch):
        """The flyout was the only rail path to the API Guide. Retiring it is
        only safe because /admin carries those links — assert that, rather
        than trusting it."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        resp = web_client.get("/admin", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'href="/documentation/api"' in resp.text


class TestDashboardLandingRedirect:
    """The Dashboard IS Chat's pre-conversation state (chat.html's rail empty
    state, see TestRailDashboard), so /dashboard 302s to /chat for
    chat-granted users; grant-less users get the 302 to the Library (not My
    Stack — /stack is retired, #1088; the page exists to start Agnes
    conversations, so without a grant it would be a dead shell)."""

    def test_rail_dashboard_redirects_to_chat_with_grant(self, web_client, admin_cookie, monkeypatch):
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/chat"

    def test_rail_dashboard_redirects_to_library_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """The grant-less landing is the Library, not My Stack: /stack is no
        longer a rail destination (#1088), so landing there would strand the
        caller on a page the rail neither links to nor highlights."""
        # Chat is disabled by default in tests, so can_chat is False.
        resp = web_client.get("/dashboard", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library"

    def test_ask_is_retired(self, web_client, admin_cookie, monkeypatch):
        """The /ask hero is retired — it 302s to / rather than rendering."""
        resp = web_client.get("/ask", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


class TestRailDashboard:
    """The rail Dashboard = Chat's pre-conversation state: /chat with no
    active conversation renders the Agnes-centric dashboard (greeting, the
    REAL composer, activity panels, guided task starters) and hides it the
    moment a conversation starts. One composer, one conversation flow —
    there is no separate dashboard page or second chat input."""

    def _enable_chat(self, web_client, monkeypatch):
        """Make can_chat true — same recipe as TestRailChatHistory."""
        import app.auth.access as access

        monkeypatch.setattr(access, "has_explicit_grant", lambda *a, **k: True)
        web_client.app.state.chat_config = SimpleNamespace(enabled=True)

    def test_dashboard_caption_block_is_not_a_scroller(self, web_client):
        """`#chat-capabilities` must not keep the base rule's `overflow-y: auto`
        under the rail.

        The base `.cloud-chat-capabilities` rule was written for the retired
        topnav empty state, where the panel held the capability cards inside a
        bounded (`flex: 0 1 auto`) column and legitimately scrolled. The rail
        dashboard reuses the same element for two lines at natural height —
        the trust caption and the "Ask … anything" heading — where the
        inherited value can only do harm: the block's height lands on a
        fraction, Chrome resolves that to ~0.5px of scrollable overflow, and
        `auto` paints a scrollbar for it. It rendered as a ~59px grey thumb
        floating inside the page (the panel is the 1280px --rdb-col, not the
        full width), appearing and disappearing with the viewport width as
        the heading's clamp() moved the fraction around.

        Sub-pixel overflow is invisible to `scrollHeight - clientHeight` (it
        rounds to 0), so nothing downstream would catch a regression here.
        """
        css = web_client.get("/static/css/chat.css").text

        def block(selector: str) -> str:
            start = css.index(selector + " {")
            return css[start : css.index("}", start)]

        rail = block('html[data-ui-layout="rail"] .cloud-chat-capabilities')
        assert "overflow: visible" in rail, (
            "the rail dashboard rule no longer cancels the base overflow — the "
            "sub-pixel scrollbar on the caption/heading block is back"
        )

    @staticmethod
    def _tables_registered(monkeypatch):
        """Report the instance as having registered tables.

        This class signs in with `admin_cookie` against a `tmp_path` instance,
        which has none — the state in which /chat shows the "no data is
        registered yet" notice. Tests about the ordinary landing page say so
        with this; the notice has its own tests below.
        """
        import app.services.admin_dashboard as admin_dashboard

        monkeypatch.setattr(
            admin_dashboard,
            "resolve_journey",
            lambda: {
                "setup": {
                    "steps": [{"key": "tables", "done": True, "failed": False, "cta": "x", "href": "/x"}],
                    "complete": False,
                    "done_count": 1,
                    "total": 6,
                    "summary": "",
                }
            },
        )

    @staticmethod
    def _dev_mode(monkeypatch):
        """Turn the local-dev preview gate on.

        Patches the FUNCTION rather than setting LOCAL_DEV_MODE=1: the env var
        also switches the whole auth layer to auto-login as a seeded dev user
        that does not exist in this fixture's DB, which would change who is
        making the request — the one thing these tests must hold still.
        """
        import app.auth.dependencies as deps

        monkeypatch.setattr(deps, "is_local_dev_mode", lambda: True)

    def test_admin_notice_states_the_instance_has_no_data(self, web_client, admin_cookie, monkeypatch):
        """One line, not a checklist.

        A six-step panel with a "0 of 6 done" meter lived in the hero slot and
        was removed as misleading: it read as onboarding, existed only before
        the first message, and disappeared for good at 6 of 6 — exactly when an
        admin might still want another source. What survives is the single
        claim that is a FACT rather than a milestone: with nothing registered, no
        answer can be GROUNDED in company data.

        Note the scope, because the copy got this wrong once. It read "it knows
        nothing about your company yet, so it can answer nothing" — and that
        second clause was false: this state's own placeholder ("Ask how to get
        Agnes set up…") and its two suggested questions all get real answers from
        general knowledge. A reader who typed one would catch the page lying on
        their first attempt. What is missing is the grounding, not the answering,
        so that is the line the copy draws.
        """
        self._enable_chat(web_client, monkeypatch)
        # NOT patched: the tmp_path instance genuinely has no registered table.
        text = web_client.get("/chat", cookies=admin_cookie, follow_redirects=False).text

        # The message is the page's own heading and lede now, not a bar above
        # them: a notice repeating the heading directly underneath said it twice.
        assert "Set up Agnes for your team" in text, "zero-state heading missing on an instance with no data"
        # The CONSEQUENCE, scoped to what is actually true: answers are ungrounded,
        # not absent.
        #
        # Read off the lede with whitespace collapsed — the sentence wraps across
        # source lines, so a raw substring search can fail on a phrase that is
        # plainly present on the page. (It did: "answer from your own numbers"
        # straddles a newline in the template.)
        lede = re.sub(r"\s+", " ", text.split('class="cld-lede">', 1)[1].split("</p>", 1)[0]).strip()
        assert "answers from general knowledge" in lede, "the consequence is what makes the fact mean something"
        assert "answer from your own numbers" in lede, "…and what changes when you connect a source"
        # The overclaim must not come back in either of its old forms.
        for lie in ("so it can answer nothing", "nobody can ask anything"):
            assert lie not in text, f"the page must not claim it cannot answer: {lie!r}"
        # "your company", never the org name — `instance.name` is routinely
        # product-shaped and turns the sentence into a bug.
        assert "your company's data" in text
        assert "AI Data Analyst answers" not in text
        # ONE action, in the setup card. The hero used to carry a chip and a
        # "Full checklist" link as well; three routes into the same job meant
        # choosing between them before starting any of it.
        assert "cld-door--setup" in text, "the setup card is the admin's action now"
        assert 'class="cld-door-btn"' in text, "…and it carries a primary button"
        # That button is the CONNECT action, never the journey's current step:
        # `admin_notice.cta` can read "Invite people" on a partly-set-up
        # instance, which is incoherent beside "nothing is registered yet".
        # Scoped to the hero, not the document: the rail's setup-chain row
        # lists every step by name — "Invite people" among them — which is
        # correct there and says nothing about this button. Before that row
        # existed the whole page was a safe proxy for the hero; it is not any
        # more, and a document-wide assertion would have started failing for a
        # reason unrelated to what it is checking.
        _hero = text.split('cld-door--setup', 1)[1].split("</section>", 1)[0]
        assert "Invite people" not in _hero
        assert "/admin/data-sources?add=" in text
        # The composer must not advertise the one thing that cannot work here.
        assert "summarize revenue trends" not in text, "the zero state still offers a data example"
        # The retired panel's framing must not come back with it.
        for retired in ('class="cset"', "of 6 done", "Do this next", 'class="cset-steps"'):
            assert retired not in text, f"the retired setup-panel framing is back: {retired}"
        # The composer is still the page's point.
        assert 'id="chat-input"' in text

    def test_zero_state_offers_only_answerable_starters(self, web_client, admin_cookie, monkeypatch):
        """The four data starters must not be offered where they cannot work.

        "Compare revenue trends" against an instance with no reachable tables
        can only apologise. The renderer picks its set from the server-rendered
        capability snapshot, so the page has to ship that snapshot with a
        truthful `tables_total` for the swap to happen at all — this asserts the
        SIGNAL, since the swap itself is in chat_dashboard.js.
        """
        self._enable_chat(web_client, monkeypatch)
        text = web_client.get("/chat", cookies=admin_cookie, follow_redirects=False).text
        assert '"tables_total": 0' in text, "the snapshot must tell the dashboard there is no data"
        assert '"is_admin": true' in text, "…and who is looking, so it offers connect vs ask-for-access"

    def test_admin_notice_is_silent_once_tables_are_registered(self, web_client, admin_cookie, monkeypatch):
        """Gated on the FACT, not on chain completion — so it goes quiet as soon
        as there is data, without ever implying setup is finished."""
        self._enable_chat(web_client, monkeypatch)
        self._tables_registered(monkeypatch)
        text = web_client.get("/chat", cookies=admin_cookie, follow_redirects=False).text
        assert "Set up Agnes for your team" not in text
        # …and the page itself is unchanged: it is the same landing page for
        # everyone, which is the point of retiring the two-hero split.
        assert 'class="cld-doors"' in text
        assert 'class="rdb-ask-heading"' in text

    def test_non_admin_never_sees_the_admin_notice(self, web_client, monkeypatch):
        """A member cannot act on it, so it would be a dead end."""
        from argon2 import PasswordHasher

        from src.db import get_system_db
        from src.repositories.users import UserRepository

        conn = get_system_db()
        UserRepository(conn).create(
            id="member1",
            email="member@test.com",
            name="Member",
            password_hash=PasswordHasher().hash("MemberPass1!"),
        )
        conn.close()
        self._enable_chat(web_client, monkeypatch)
        # A member has no CHAT grant in this fixture, so /chat would 302 to home
        # and every "not in text" assertion below would pass against an empty
        # body — proving nothing. Let them through the gate so the test actually
        # exercises the render.
        import app.auth.access as access

        monkeypatch.setattr(access, "can_access", lambda *a, **k: True)

        resp = web_client.post("/auth/token", json={"email": "member@test.com", "password": "MemberPass1!"})
        assert resp.status_code == 200, f"member login failed: {resp.text}"
        cookie = {"access_token": resp.json()["access_token"]}
        page = web_client.get("/chat", cookies=cookie, follow_redirects=False)
        assert page.status_code == 200, f"member did not reach /chat: {page.status_code}"
        text = page.text
        assert "Set up Agnes for your team" not in text, "a non-admin was shown the admin notice"
        assert "cset-devsw" not in text, "a non-admin was shown the dev audience switch"
        # They get the same landing page as everyone else.
        assert 'class="cld-doors"' in text

    def test_dev_preview_member_hides_the_admin_notice(self, web_client, admin_cookie, monkeypatch):
        """`?preview=member` under LOCAL_DEV_MODE lets one account look at the
        other audience's landing page — the reason the switch exists."""
        self._enable_chat(web_client, monkeypatch)
        self._dev_mode(monkeypatch)
        text = web_client.get("/chat?preview=member", cookies=admin_cookie, follow_redirects=False).text
        assert "Set up Agnes for your team" not in text, "?preview=member did not suppress the admin notice"
        # The switch stays on screen so the view is escapable and labelled. It
        # moved out of this page's markup and into the base layout — inline it
        # read as product chrome, and its links could only point back at /chat.
        assert "data-devsw" in text
        assert "your data and permissions are unchanged" in text

    def test_dev_preview_empty_forces_the_notice_without_touching_data(self, web_client, admin_cookie, monkeypatch):
        """`?preview=empty` renders the no-data notice on an instance that has
        data, so the state can be reviewed without registering or deleting real
        tables to reach it. It fakes the RENDER only."""
        self._enable_chat(web_client, monkeypatch)
        self._dev_mode(monkeypatch)
        self._tables_registered(monkeypatch)  # …so the notice would normally be silent
        text = web_client.get("/chat?preview=empty", cookies=admin_cookie, follow_redirects=False).text
        assert "Set up Agnes for your team" in text
        assert "answers from general knowledge" in text
        # The forced state must force the DATA signal too, or the heading sits
        # above four suggestions that need data — a state no instance can be in.
        assert '"tables_total": 0' in text

    def test_dev_preview_is_inert_without_local_dev_mode(self, web_client, admin_cookie, monkeypatch):
        """The switch must not be a production surface. Off the dev gate every
        value is ignored outright — not half-honoured — and the toggle is not
        rendered at all, so nothing advertises a view it won't give."""
        self._enable_chat(web_client, monkeypatch)
        self._tables_registered(monkeypatch)
        # No _dev_mode() call: this is the default, production-shaped path.
        for value in ("member", "empty"):
            text = web_client.get(f"/chat?preview={value}", cookies=admin_cookie, follow_redirects=False).text
            assert "cset-devsw" not in text, f"the dev switch rendered outside LOCAL_DEV_MODE (?preview={value})"
            assert "Set up Agnes for your team" not in text, f"?preview={value} was honoured outside LOCAL_DEV_MODE"

    def test_dev_preview_rejects_an_unknown_value(self, web_client, admin_cookie, monkeypatch):
        """Anything but the known values falls back to the real view rather than
        to an arbitrary branch."""
        self._enable_chat(web_client, monkeypatch)
        self._dev_mode(monkeypatch)
        self._tables_registered(monkeypatch)
        text = web_client.get("/chat?preview=wat", cookies=admin_cookie, follow_redirects=False).text
        assert "Set up Agnes for your team" not in text

    def test_rail_dashboard_actions_section(self, web_client, admin_cookie, monkeypatch):
        """One Suggested-next-actions section below the composer: list +
        loading + empty-state elements are all server-rendered (js toggles
        them), and there are no department/role tabs."""
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/chat", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'id="rdb-actions-loading"' in text
        assert 'id="rdb-actions-empty"' in text
        assert "No suggested actions yet" in text
        # js/chat_dashboard.js drives the list through chat.js's one flow.
        assert "js/chat_dashboard.js" not in text  # loaded via chat.js import, not a script tag

    def test_rail_nav_new_chat_is_the_single_chat_entry(self, web_client, admin_cookie, monkeypatch):
        """There is no separate Dashboard nav item — /dashboard is just Chat's
        pre-conversation state, so it and New chat pointed at the same surface.
        New chat is the single chat entry point; the only /dashboard href left
        is the rail logo (href = home_route, default /dashboard)."""
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'id="new-chat"' in text
        assert "New chat" in text
        # The retired Dashboard nav item is gone: the only /dashboard href is
        # the logo (even with a chat grant), never a second nav-item occurrence.
        assert text.count('href="/dashboard"') == 1
        assert 'class="rail-logo" href="/dashboard"' in text

    def test_rail_nav_new_chat_active_on_empty_chat(self, web_client, admin_cookie, monkeypatch):
        """New chat carries the `.on` active state (folded over from the retired
        Dashboard item) exactly while the pre-conversation state is showing —
        /chat with no session deep link."""
        self._enable_chat(web_client, monkeypatch)
        # Empty /chat → New chat is active.
        resp = web_client.get("/chat", cookies=admin_cookie)
        assert resp.status_code == 200
        assert re.search(r'class="rail-i[^"]*\bon\b[^"]*"\s+id="new-chat"', resp.text)
        # Deep-linked into a conversation → New chat is not active, and carries
        # no standing tint of its own either (test_new_chat_is_an_ordinary_row),
        # so the row is genuinely plain.
        resp = web_client.get("/chat?session=abc", cookies=admin_cookie)
        assert resp.status_code == 200
        assert not re.search(r'class="rail-i[^"]*\bon\b[^"]*"\s+id="new-chat"', resp.text)

    def test_rail_nav_hides_new_chat_without_chat_grant(self, web_client, admin_cookie, monkeypatch):
        """Without a chat grant the chat slot renders nothing; the only
        /dashboard href left is the logo (whose route bounces grant-less
        callers to /library)."""
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'id="new-chat"' not in resp.text
        assert resp.text.count('href="/dashboard"') == 1


class TestProfileNotifications:
    """The Notifications channels moved off the retired /dashboard onto the
    account page (/me/profile), where they belong. Rendered on both layouts."""

    def test_profile_renders_notifications_section(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Notifications" in resp.text
        assert 'class="pf-notif-list"' in resp.text
        # Telegram link affordance is present (unlinked state → Link button).
        assert "showTelegramVerify()" in resp.text


class TestStackWorkspace:
    """My Stack the PAGE is retired (#1088) — folded into the Library, which
    already renders every kind it did off the same StackResolver.browse()
    call. This class used to pin the page's own DOM (a two-group `stk-*`
    table: Required vs. Added by you); that markup no longer exists, so the
    business semantics it guarded — a required-tier grant reads "In stack"
    but LOCKED with no remove affordance, an optional self-subscription is
    removable — are folded into the Library's own suite instead:
    ``tests/test_web_library.py::test_library_required_grant_is_locked_in_stack``
    and ``::test_library_available_grant_classic_is_not_claimed_in_stack``.
    What remains here is the redirect contract itself."""

    @pytest.mark.parametrize("layout", ["topnav", "rail"])
    def test_stack_page_redirects_to_library_in_stack_view(self, web_client, admin_cookie, monkeypatch, layout):
        """Unconditional — unlike /corporate-memory and /apps (#1278), there
        is no legacy My-Stack template kept alive for topnav, so the redirect
        must fire under every layout, not just rail."""
        if layout == "topnav":
            monkeypatch.delenv("AGNES_UI_LAYOUT", raising=False)
        else:
            monkeypatch.setenv("AGNES_UI_LAYOUT", layout)
        resp = web_client.get("/stack", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library?stack=in_stack"


class TestCatalogRecommendations:
    """Catalog reshape: the Catalog surfaces ONLY resources the caller does
    not already have. Auto-membership puts every granted package in the
    caller's stack the moment it's granted, so a granted package appears
    ONLY on My Stack — never on /catalog (not in the addable grids, and not
    in the "Recommended for you" row, which stays empty for granted
    content). The download-a-local-copy action for a granted-but-not-yet-
    materialized package lives on My Stack, not here."""

    @pytest.fixture(autouse=True)
    def _auto_membership_mode(self, monkeypatch):
        """The reshape is auto-membership behavior, opt-in since the classic
        subscribe model became the default again (spec
        2026-08-07-default-chrome-ux-parity)."""
        monkeypatch.setenv("AGNES_STACK_AUTO_MEMBERSHIP", "1")

    def test_granted_package_absent_from_catalog_present_on_my_stack(self, web_client, admin_cookie, monkeypatch):
        """A granted-but-not-yet-downloaded package must not appear anywhere
        on /catalog. It lives on My Stack — /library?stack=in_stack, since
        the standalone /stack page is retired (#1088). Materializing
        (subscribing) it must not pull it back into the Catalog — it still
        shows only on My Stack."""
        import uuid

        from src.db import get_system_db
        from src.repositories.data_packages import DataPackagesRepository

        conn = get_system_db()
        pkg_id = DataPackagesRepository(conn).create(
            name="Unstacked Package XYZ",
            slug="unstacked-xyz",
            description="d",
            icon=None,
            color=None,
            created_by="test",
        )
        admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, 'available', CURRENT_TIMESTAMP, 'test')",
            [str(uuid.uuid4()), admin_gid, pkg_id],
        )
        conn.close()

        # Granted → auto-membership in_stack=True → absent from the entire
        # Catalog page (Recommended row + addable grids alike).
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Unstacked Package XYZ" not in resp.text, "granted package must not appear anywhere on the Catalog"

        # ...but it IS on My Stack, where the caller's holdings live.
        resp = web_client.get("/library?stack=in_stack", cookies=admin_cookie)
        assert "Unstacked Package XYZ" in resp.text

        # Materializing (subscribing) it must not pull it back into the Catalog.
        from src.repositories.user_stack_subscriptions import UserStackSubscriptionsRepository

        conn = get_system_db()
        UserStackSubscriptionsRepository(conn).subscribe("admin1", "data_package", pkg_id)
        conn.close()

        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert "Unstacked Package XYZ" not in resp.text
        resp = web_client.get("/library?stack=in_stack", cookies=admin_cookie)
        assert "Unstacked Package XYZ" in resp.text


class TestPaperThemeAssets:
    """The paper value must resolve to real CSS, not a silent no-op."""

    def test_design_tokens_define_paper_block(self):
        css = open("app/web/static/css/design-tokens.css").read()
        assert ':root[data-theme="paper"]' in css

    def test_paper_block_covers_core_ds_tokens(self):
        css = open("app/web/static/css/design-tokens.css").read()
        block = re.search(r':root\[data-theme="paper"\]\s*\{(.*?)\n\}', css, re.DOTALL)
        assert block, "paper block missing"
        body = block.group(1)
        for token in (
            "--ds-primary:",
            "--ds-bg:",
            "--ds-surface:",
            "--ds-border:",
            "--ds-text-primary:",
            "--primary:",  # legacy compat shim
            "--background:",  # legacy compat shim
        ):
            assert token in body, f"paper theme must override {token}"

    def test_bases_load_rail_and_paper_sheets(self):
        for base in ("app/web/templates/base_ds.html", "app/web/templates/base.html"):
            html = open(base).read()
            assert "css/rail.css" in html, f"{base} must load rail.css"
            assert "css/paper-skin.css" in html, f"{base} must load paper-skin.css"
            assert "css/detail-page.css" in html, f"{base} must load detail-page.css"

    def test_detail_page_sheet_loads_before_head_extra(self):
        """The detail pages emit `detail.styles()` from `head_extra`, so the
        shared sheet must be linked ABOVE that block — otherwise every rule
        it overrides would lose the cascade to a per-page <style>."""
        for base in ("app/web/templates/base_ds.html", "app/web/templates/base.html"):
            html = open(base).read()
            assert html.index("css/detail-page.css") < html.index("{% block head_extra %}"), (
                f"{base} must link detail-page.css before the head_extra block"
            )

    @staticmethod
    def _selectors(path: str) -> list[str]:
        """Rule selectors from a flat CSS sheet — comments stripped,
        at-rules (@media/@supports wrappers) skipped; rules nested in
        at-rule bodies still surface as ordinary selectors.

        `@keyframes` bodies are dropped whole: their `from` / `to` / `50%`
        stops are parsed as selectors by the scan below, and a keyframe stop
        can no more carry a theme scope than it can carry a class."""
        css = re.sub(r"/\*.*?\*/", "", open(path).read(), flags=re.DOTALL)
        css = re.sub(r"@(?:-\w+-)?keyframes\s+[\w-]+\s*\{(?:[^{}]|\{[^{}]*\})*\}", "", css)
        raw = re.findall(r"(?:^|[{}])\s*([^{}]+?)\s*\{", css)
        return [s.strip() for s in raw if s.strip() and not s.strip().startswith("@")]

    def test_rail_css_rules_are_scoped_to_activation(self):
        """Every rule in rail.css must be scoped to the rail layout
        attribute so the sheet is inert under topnav."""
        for sel in self._selectors("app/web/static/css/rail.css"):
            assert 'html[data-ui-layout="rail"]' in sel, f"rail.css selector not scoped to rail layout: {sel!r}"

    def test_paper_skin_rules_are_scoped_to_theme(self):
        for sel in self._selectors("app/web/static/css/paper-skin.css"):
            assert '[data-theme="paper"]' in sel, f"paper-skin.css selector not scoped to paper theme: {sel!r}"

    def test_detail_page_rules_are_scoped_to_theme(self):
        """The shared resource-detail layout is opt-in like every other
        redesign sheet: default blue/topnav instances load it inert."""
        for sel in self._selectors("app/web/static/css/detail-page.css"):
            assert '[data-theme="paper"]' in sel, f"detail-page.css selector not scoped to paper theme: {sel!r}"

    def test_trustmark_css_rules_are_scoped_to_theme(self):
        """The trust markers are opt-in like every other redesign sheet.

        This started life as its mirror image — a test asserting the sheet was
        deliberately GLOBAL, on the reading that the markers were a documented
        default-look change. That was wrong: the CHANGELOG documents the two
        FLAGS defaulting on (markers appearing where a flag had hidden them),
        not the theme scoping, which was never a decision. A default blue
        instance must render its own spelling — `.cc-trust` on catalog cards,
        the amber `Curated` badge on package cards and detail heroes, nothing on
        Library rows — so `.ds-trust` has to stay inert there.

        Markup gating is the other half and cannot be seen from here: `mark()`
        in macros/_trustmark.html takes `paper=False`, so an ungated callsite
        emits nothing rather than an unstyled marker.
        """
        for sel in self._selectors("app/web/static/css/trustmark.css"):
            assert '[data-theme="paper"]' in sel, f"trustmark.css selector not scoped to paper theme: {sel!r}"

    def test_keyframe_stops_are_not_mistaken_for_selectors(self):
        """Guard on the guard: without the @keyframes strip, a stop like
        `from {` parses as an unscoped selector, which would fail the scoping
        test above for a reason that has nothing to do with scoping."""
        selectors = self._selectors("app/web/static/css/detail-page.css")
        assert selectors, "selector scan returned nothing — the regex broke"
        assert "from" not in selectors and "to" not in selectors


class TestSharedDetailLayout:
    """One editorial layout for every resource type.

    A detail page is: a header on the page ground (no gradient slab, no
    nested frosted panel), a resource-type badge beside the title, and a
    two-column shell with a sticky right rail. (Wave 0, 2026-08 legacy
    retirement, deleted the frozen pre-redesign ``*_legacy.html`` copies this
    class used to guard against leaking onto a default instance — there is
    only this layout now.)"""

    @staticmethod
    def _package(slug: str = "detail-layout-pkg") -> str:
        from src.repositories import data_packages_repo

        return data_packages_repo().create(
            name="Detail Layout Package",
            slug=slug,
            description="A package used to assert the shared detail layout.",
            icon=None,
            color=None,
            created_by="admin1",
        )

    def test_redesign_renders_the_two_column_shell_and_type_badge(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        self._package("paper-detail-pkg")
        text = web_client.get("/catalog/p/paper-detail-pkg", cookies=admin_cookie).text
        assert "detail-cols" in text, "the editorial layout must open the two-column shell"
        assert "detail-main" in text
        assert "detail-aside" in text, "the sticky right rail must render"
        # The resource-type badge names the type in words, next to the title.
        assert 'class="detail-type"' in text
        assert ">Data package<" in text
        # The rail answers "is this on my laptop?".
        assert "Availability" in text
        assert 'class="detail-side__rows"' in text

    def test_default_instance_renders_no_ds_trust_marker(self, web_client, admin_cookie, monkeypatch):
        """A default instance shows its OWN trust spelling, never `.ds-trust`.

        This assertion belongs next to the one below and its absence is exactly
        how the leak shipped: that test pins the absence of `detail-cols`,
        `detail-aside` and `detail-type`, so the trust pill sailed through the
        one test written to protect the default hero.
        """
        monkeypatch.delenv("AGNES_INSTANCE_THEME", raising=False)
        for path in ("/library", "/catalog"):
            resp = web_client.get(path, cookies=admin_cookie)
            assert resp.status_code == 200, path
            # Match the EMITTED markup, not the string: the page's own CSS
            # comments legitimately name the class while explaining where the
            # markers went.
            assert 'class="ds-trust' not in resp.text, (
                f"{path} leaked the paper-only trust marker into the default theme"
            )

    def test_the_admin_errand_lives_in_the_rail_not_the_header(self, web_client, admin_cookie, monkeypatch):
        """One prominent action per header, and the admin errand offered ONCE.

        This used to pin the errand inside the reader's overflow menu, which
        made "manage this package" a menu item that navigated to
        `/admin/tables?edit_package=` — the Tables lens, a page about something
        else. Managing the thing you are standing on now has its own labelled
        home in the rail (`detail.manage`), and it edits in place through the
        shared drawer. The overflow menu keeps only what it always promised:
        the actions a READER has that are not what they came to do.

        The invariant the old test was really protecting is unchanged and still
        asserted here — the action is offered in exactly one place, and the
        header is not it.
        """
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        self._package("menu-detail-pkg")
        text = web_client.get("/catalog/p/menu-detail-pkg", cookies=admin_cookie).text
        assert "data-manage" in text, "the rail must carry the governance cluster"
        assert 'id="pkg-edit-btn"' in text, "and its in-place editor"
        # Neither of the two older spellings survives, so it is offered once.
        assert 'class="detail-edit-icon"' not in text
        assert "Edit package metadata" not in text
        # And it no longer answers "edit this" by leaving for the Tables lens.
        assert "/admin/tables?edit_package=" not in text


class TestResourceColourTokens:
    """One semantic accent per resource type, consumed product-wide through
    the `--ds-kind-*` aliases rather than by any page directly."""

    RESOURCES = ("data", "skill", "plugin", "file", "collection", "agent", "memory", "recipe")

    @staticmethod
    def _tokens_css() -> str:
        return open("app/web/static/css/design-tokens.css").read()

    def test_every_resource_has_the_full_three_role_set(self):
        css = self._tokens_css()
        for resource in self.RESOURCES:
            for role in ("ink", "soft", "line"):
                token = f"--ds-resource-{resource}-{role}:"
                assert token in css, f"resource colour system is missing {token}"

    def test_dark_theme_flips_both_halves_of_every_pair(self):
        """A resource tint is a near-white fill in light mode. Under dark it
        has to be re-derived, or the accent ink lands on a light fill while
        the page around it went dark."""
        css = self._tokens_css()
        # More than one dark block exists (the brand-variant one included), so
        # scan them all rather than assuming which of them declares these.
        joined = "\n".join(
            m.group(1) for m in re.finditer(r':root\[data-theme="dark"\][^{]*\{(.*?)\n\}', css, re.DOTALL)
        )
        assert joined, "no dark theme block found"
        for resource in self.RESOURCES:
            assert f"--ds-resource-{resource}-ink:" in joined, f"{resource} ink not re-derived under dark"
            assert f"--ds-resource-{resource}-soft:" in joined, f"{resource} tint not re-derived under dark"

    def test_paper_routes_kind_aliases_onto_the_resource_family(self):
        """The remap is the whole distribution mechanism: every existing
        consumer reads `--ds-kind-*`, so pointing those at `--ds-resource-*`
        under paper is what carries the palette to the Library table, the
        cards, the detail pages, search results and the Stack at once."""
        css = self._tokens_css()
        block = re.search(r':root\[data-theme="paper"\]\s*\{(.*?)\n\}', css, re.DOTALL)
        assert block, "paper block missing"
        body = block.group(1)
        for kind, resource in (
            ("data", "data"),
            ("skill", "skill"),
            ("plugin", "plugin"),
            ("file", "file"),
            ("library", "collection"),  # `library` is the scaffold's name for a collection
            ("agent", "agent"),
            ("memory", "memory"),
            ("recipe", "recipe"),
        ):
            assert f"--ds-kind-{kind}: var(--ds-resource-{resource}-ink);" in body, (
                f"paper must alias --ds-kind-{kind} onto the {resource} resource colour"
            )

    def test_default_theme_keeps_its_original_kind_hues(self):
        """The palette is opt-in. A default (blue) instance must still resolve
        the pre-redesign hues, so the remap may only live in the paper block."""
        css = self._tokens_css()
        # The unscoped `:root` is split across several append-only blocks.
        globals_ = "\n".join(m.group(1) for m in re.finditer(r"^:root\s*\{(.*?)\n\}", css, re.DOTALL | re.MULTILINE))
        assert "--ds-kind-data: #185a57;" in globals_, "default data hue changed"
        assert "--ds-kind-plugin: #391c57;" in globals_, "default plugin hue changed"
        assert "--ds-kind-library: #0a5aa8;" in globals_, "default collection hue changed"
        assert "--ds-kind-skill: #0e7c57;" in globals_, "default skill hue changed"
        assert "--ds-kind-memory: #523410;" in globals_, "default memory hue changed"


class TestDetailPageTemplateIsShared:
    """Every resource detail page is the SAME template, not nine lookalikes.

    The point of `macros/_detail.html` is that a reader meets one page shape
    on a data package, a plugin, a skill, an agent, a file, a collection and a
    memory domain — same header, same container language, same rail, same
    place for every shared concept. Two things drift without a guard:

      1. a page keeps hand-writing its own header (the marketplace pages did
         exactly this, for the sake of four hydration hooks, and lost the
         type badge, the rail and the overflow menu in the process), and
      2. a page opts out of the panels container language, so it renders
         borderless sections beside another page's panels.

    (Wave 0, 2026-08 legacy retirement, deleted the frozen pre-redesign
    ``*_legacy.html`` copies this class used to guard against leaking onto a
    default instance — there is only this template now.)
    """

    # (path-builder key, the type badge the header must print)
    PAPER_PAGES = (
        ("/catalog/p/{pkg}", "Data package"),
        ("/marketplace/curated/agnes-builtin/agnes-analyst", "Plugin"),
    )

    @staticmethod
    def _package(slug: str) -> str:
        from src.repositories import data_packages_repo

        return data_packages_repo().create(
            name="Shared Template Package",
            slug=slug,
            description="A package used to assert the shared detail template.",
            icon=None,
            color=None,
            created_by="admin1",
        )

    def test_every_detail_page_speaks_the_panels_language(self, web_client, admin_cookie, monkeypatch):
        """`detail--panels` is the scaffold's shared default, not a per-page
        opt-in — a default each page has to remember to ask for is a default
        that drifts."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        self._package("panels-detail-pkg")
        for path in ("/catalog/p/panels-detail-pkg", "/marketplace/curated/agnes-builtin/agnes-analyst"):
            resp = web_client.get(path, cookies=admin_cookie)
            assert resp.status_code == 200, path
            assert "detail--panels" in resp.text, f"{path} is not on the shared container language"
            assert "detail-cols" in resp.text, f"{path} is missing the two-column shell"
            assert "detail-aside" in resp.text, f"{path} is missing the rail"

    def test_the_marketplace_pages_render_through_the_shared_hero(self, web_client, admin_cookie, monkeypatch):
        """They used to hand-write the header. The tell is the type badge and
        the overflow menu, which only the shared hero emits."""
        monkeypatch.setenv("AGNES_INSTANCE_THEME", "paper")
        text = web_client.get("/marketplace/curated/agnes-builtin/agnes-analyst", cookies=admin_cookie).text
        assert 'class="detail-type"' in text, "the plugin header must name the resource type"
        assert ">Plugin<" in text
        assert '<details class="detail-menu">' in text, "secondary actions belong in the overflow menu"
        # The hydration hooks survived the move onto the shared hero.
        assert 'id="hero-name"' in text
        assert 'id="hero-icon"' in text
        assert 'id="details-list"' in text

    def test_shared_concepts_use_one_component_each(self):
        """Sharing, versions, the admin ladder and 'what is inside this' are
        defined once in the scaffold. A page that re-specifies one of them
        locally is how two surfaces come to disagree about the same fact."""
        scaffold = open("app/web/templates/macros/_detail.html").read()
        for macro in (
            "macro visibility_chip(",
            "macro side_sharing(",
            "macro version_timeline(",
            "macro store_menu(",
            "macro objects(",
        ):
            assert macro in scaffold, f"the shared scaffold is missing `{macro}`"

        # The store-entity action ladder existed twice, byte-similar, on the
        # plugin and the skill/agent pages. Neither may rebuild it.
        for page in (
            "app/web/templates/marketplace_plugin_detail.html",
            "app/web/templates/marketplace_item_detail.html",
        ):
            assert "detail.store_menu(" in open(page).read(), (
                f"{page} must reach the Edit/Archive/Hard-delete ladder through the shared macro"
            )


class TestRedesignedPageContracts:
    """The redesigned surfaces the topnav/classic chrome used to keep a
    parity twin for (Wave 0, 2026-08 legacy retirement, deleted that twin —
    ``library_legacy.html``, ``marketplace_legacy.html``, the classic /chat
    composer/sidebar/tour, ``profile_legacy.html``, ``me_activity_legacy.html``,
    ``agents_legacy.html``, ``me_cowork_legacy.html`` are all gone, and
    ``/catalog`` collapsed onto ``catalog_unified.html`` unconditionally).
    What remains is what every instance renders now:

    - ``/library``: the unified Library (``id="lib-search"``)
    - ``/marketplace``: one Browse shelf
    - ``/chat``: the composer "+" upload menu, the rail's own onboarding
      card (NOT the retired topnav sidebar's ``#chat-journey`` slot), the
      conversation row menu, and no legacy tour overlay
    """

    def test_library_is_the_unified_library(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/library", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'id="lib-search"' in resp.text
        assert "Your collections" not in resp.text

    def test_marketplace_browse_folds_into_the_library(self, web_client, admin_cookie, monkeypatch):
        """The Marketplace browse shell folds into the Library.

        Its Browse and My Stack tabs were "everything there is" and "what I
        have" over store entities and curated plugins the Library already
        lists — the same pair the Library now asks of one list: `?tab=my`
        maps to `scope=mine` (the In stack segment) and the browse tab to
        `scope=available` (the "Not in stack yet" filter), so an old link
        lands where it meant to. The store itself is untouched: every
        detail, edit and submission route under /marketplace still renders.
        """
        resp = web_client.get("/marketplace", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/library?scope=available"

        mine = web_client.get("/marketplace?tab=my", cookies=admin_cookie, follow_redirects=False)
        assert mine.status_code == 302
        assert mine.headers["location"] == "/library?scope=mine"

    def _chat(self, web_client, admin_cookie):
        """GET /chat with chat enabled AND explicitly granted to the Admin
        group — ``can_chat`` (the rail card's gate) deliberately reads the
        explicit grant, not god-mode."""
        import uuid

        from src.db import get_system_db

        web_client.app.state.chat_config = SimpleNamespace(enabled=True)
        conn = get_system_db()
        try:
            gid = conn.execute("SELECT id FROM user_groups WHERE name = 'Admin'").fetchone()[0]
            already = conn.execute(
                "SELECT 1 FROM resource_grants WHERE group_id = ? AND resource_type = 'chat'", [gid]
            ).fetchone()
            if not already:
                conn.execute(
                    "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
                    "requirement, assigned_at, assigned_by) "
                    "VALUES (?, ?, 'chat', 'chat', 'available', CURRENT_TIMESTAMP, 'test')",
                    [str(uuid.uuid4()), gid],
                )
        finally:
            conn.close()
        return web_client.get("/chat", cookies=admin_cookie)

    def test_chat_keeps_upload_menu_and_row_menu(self, web_client, admin_cookie, monkeypatch):
        """The composer "+" menu and the row menu render on the page."""
        resp = self._chat(web_client, admin_cookie)
        assert resp.status_code == 200
        assert 'id="chat-plus-menu"' in resp.text
        assert "chat_row_menu.js" in resp.text
        assert 'id="chat-copy-transcript"' in resp.text

    def test_the_analyst_journey_yields_to_an_unfinished_admin_chain(
        self, web_client, admin_cookie, monkeypatch
    ):
        """Two six-step "setup" journeys must never render together.

        The rail's ``railGetStarted`` card is the ANALYST journey (ask a
        question, explore your Library, put knowledge in your stack…). The
        hero card on the same page is the ADMIN chain (connect a source,
        choose tables, bundle, invite, share, verify). They share no steps,
        and rendering both put two "of 6" counters with different numerators
        on one screen — the one the front door drove being the one that never
        mentions the admin's actual job.

        So for an admin whose chain is unfinished the analyst journey yields.
        This asserts the narrowing in BOTH directions: it is not "the journey
        is gone", it is "the journey is not shown to the wrong person at the
        wrong time". A non-admin, and an admin who has finished, still get it.
        """
        resp = self._chat(web_client, admin_cookie)
        assert resp.status_code == 200
        # This fixture's instance has no source and no tables, so the admin
        # chain is unfinished and the analyst journey must stand down — as the
        # slot's STANDBY occupant, not by being absent. Absent is what made
        # "Start over onboarding" a control that reset state and showed
        # nothing; `data-chain-alternate` keeps it hidden until the admin asks
        # for it, so only one of the two counters is ever on screen.
        assert 'id="railGetStarted"' in resp.text
        standby = resp.text.split('id="railGetStarted"', 1)[1].split(">", 1)[0]
        assert "data-chain-alternate" in standby
        assert "window._agAdminSetupPending = true" in resp.text

    def test_the_analyst_journey_is_untouched_for_a_non_admin(self):
        """The narrowing keys off `admin_setup`, which the chat route computes
        only for an admin — so a member's render is bit-for-bit unchanged.

        Asserted on the template condition itself rather than through a second
        seeded user: the condition IS the contract, and a member fixture would
        test the seeding as much as the rule.
        """
        from jinja2 import Environment

        env = Environment()
        pill = env.from_string(
            "{% if not (admin_setup and not admin_setup.complete) %}PILL{% else %}NONE{% endif %}"
        )
        flag = env.from_string(
            "{% if admin_setup and not admin_setup.complete %}FLAG{% else %}NONE{% endif %}"
        )
        # A non-admin never gets `admin_setup` at all.
        assert pill.render() == "PILL"
        assert flag.render() == "NONE"
        assert pill.render(admin_setup=None) == "PILL"
        # An admin who has finished is, for this purpose, just a user again.
        done = {"done": 6, "total": 6, "complete": True}
        assert pill.render(admin_setup=done) == "PILL"
        assert flag.render(admin_setup=done) == "NONE"
        # Only the unfinished admin yields.
        mid = {"done": 2, "total": 6, "complete": False}
        assert pill.render(admin_setup=mid) == "NONE"
        assert flag.render(admin_setup=mid) == "FLAG"

    # ── Wave 2 (spec 2026-08-07-default-chrome-ux-parity): the page rewrites. ──

    def test_profile_is_the_redesigned_page(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'id="pf-name-edit"' in resp.text

    def test_activity_is_the_redesigned_page(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/me/activity", cookies=admin_cookie)
        assert resp.status_code == 200
        assert "Sessions, token usage, data access, and sync activity" not in resp.text

    def test_agents_is_the_builder(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'id="ag-builder-view"' in resp.text

    def test_ai_connector_stays_consolidated(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/me/ai-connector", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/how-it-works#connect"

    def test_does_not_ship_the_legacy_tour(self, web_client, admin_cookie, monkeypatch):
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert 'id="agnesTour"' not in resp.text
        assert "tour_legacy.js" not in resp.text


class TestDetailPageParity:
    """The redesign restructured seven DETAIL templates in place (the
    kind-coloured hero + columns anatomy from ``macros/_detail.html``).

    The ``_detail_template()`` switch and the frozen ``<name>_legacy.html``
    copies it served on a default (topnav) instance were removed in Wave 0
    legacy retirement (2026-08) — every render site now serves the
    redesigned template unconditionally. The unit test on the old switch and
    the closed-set static sweep over the seven frozen copies went with them.
    What remains: for the two cheaply-seedable pages (collection + catalog
    table), a live-render check that the shared ``detail-page`` template is
    what actually renders and the retired legacy layout does not leak back
    in — no "topnav" branch to compare against any more, just the one
    template every instance serves — and
    ``test_the_live_detail_page_keeps_the_invariant`` below, which is NOT
    legacy-chrome leftover: rail-only chrome means these live templates are
    what every instance renders now, so the three prior production
    regressions it guards (#1177, #1178, the per-file entry point) matter
    more than before, not less.
    """

    #: Behaviours that must hold on the live (redesigned) detail templates —
    #: each one a prior production regression, restated as the token that
    #: implements the fix. Add a row whenever a fix has to reach one of these
    #: pages, so a later edit can't silently drop it again.
    FORKED_PAIR_INVARIANTS = (
        (
            "marketplace_plugin_detail",
            "own_private",
            "#1177 — the author's own Private row sits at 'hidden' and must stay deletable",
        ),
        ("marketplace_item_detail", "own_private", "#1177 — same gate on the skill/agent page"),
        (
            "marketplace_plugin_detail",
            "d.installable !== true",
            "#1178 — install is gated on the server-resolved flag, not on the status alone",
        ),
        ("marketplace_item_detail", "d.installable !== true", "#1178 — same gate on the skill/agent page"),
        (
            "library_detail",
            "/f/",
            "the per-file page's only entry point — without it, "
            "`/library/<slug>/f/<id>` is reachable only by typing the URL",
        ),
        (
            "data_app_detail",
            "app.state_detail",
            "a failed deploy records WHY in state_detail, and the page is where an operator "
            "looks next — a bare `error` badge sent one investigation at a healthy sidecar",
        ),
    )

    @pytest.mark.parametrize("base,token,why", FORKED_PAIR_INVARIANTS)
    def test_the_live_detail_page_keeps_the_invariant(self, base, token, why):
        from pathlib import Path

        live = Path(f"app/web/templates/{base}.html").read_text()
        assert token in live, f"regression — {token!r} is no longer in {base}.html ({why})"

    def _seed_collection(self, web_client, admin_cookie, name):
        r = web_client.post("/api/collections", json={"name": name}, cookies=admin_cookie)
        assert r.status_code == 201, r.text
        return r.json()

    def test_library_detail_renders_the_shared_detail_page(self, web_client, admin_cookie):
        """No layout knob left to flip — rail is the only chrome (Wave 0,
        2026-08 legacy retirement) — so every collection detail page renders
        through the shared ``detail-page`` anatomy (``macros/_detail.html``),
        never the retired ``lib-sec`` layout ``library_detail_legacy.html``
        used to serve."""
        col = self._seed_collection(web_client, admin_cookie, "Parity Files")
        resp = web_client.get(f"/library/{col['slug']}", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="detail-page"' in resp.text, "the shared detail template must render"
        assert 'class="lib-sec"' not in resp.text, "the retired legacy collection layout must not leak back in"

    def _seed_table(self, name):
        from src.repositories import table_registry_repo

        table_registry_repo().register(
            id=name,
            name=name,
            source_type="keboola",
            bucket="in.c-test",
            source_table=name,
            query_mode="local",
        )

    def test_catalog_table_detail_renders_the_shared_detail_page(self, web_client, admin_cookie):
        """Same guard as above, for the catalog table detail page — the
        retired legacy layout keyed its back-link off ``td-back``;
        ``catalog_table_detail_legacy.html`` is gone, so it must not
        reappear."""
        self._seed_table("parity_table")
        resp = web_client.get("/catalog/t/parity_table", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'class="detail-page"' in resp.text, "the shared detail template must render"
        assert "td-back" not in resp.text, "the retired legacy table-detail layout must not leak back in"
