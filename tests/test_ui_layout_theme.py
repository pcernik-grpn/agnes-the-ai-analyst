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
        head = text.split('class="lib-head"', 1)[1].split('class="fbar-dock"', 1)[0]
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
        # The segment is gone, not merely hidden — a filter is not a tab.
        assert 'id="lib-scope"' not in text
        assert "segments: {" not in text

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
        for label in ("Build a skill", "Build a plugin", "Upload a file"):
            assert f"<span>{label}</span>" in text
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
        assert "In your stack" in text
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
        its DELETE /api/agents/{id} handler, and — unlike the list card —
        confirms first, because here it is one button away from a primary
        action on the config the caller is looking at."""
        resp = web_client.get("/agents", cookies=admin_cookie)
        assert resp.status_code == 200
        text = resp.text
        assert 'class="cc-btn ag-del-btn" data-ag-del=' in text
        # Left of the status button, inside the builder's action group.
        actions = text.index('<div class="ag-build-actions">')
        assert actions < text.index('class="cc-btn ag-del-btn"') < text.index("data-ag-status")
        # Confirms only for the builder button; the list card is unchanged.
        assert "window.confirm(" in text
        assert "t.classList.contains('ag-del-btn')" in text
        # A pending debounced PATCH must not outlive the row it would write to.
        assert "clearTimeout(saveTimers[id]);" in text

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
        # The row CLOSES the bottom zone: under Admin, above the profile.
        row_pos = text.find('class="rail-getstarted"')
        assert text.find('class="rail-admin"') < row_pos < text.find('class="rail-foot"'), (
            "the onboarding row belongs under Admin, above the profile"
        )
        # ...and the foot below it is the profile alone.
        foot = text.split('class="rail-foot"', 1)[1]
        assert "rail-getstarted" not in foot
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

    def test_pinned_and_chats_are_two_collapsible_sections(self, web_client, admin_cookie, monkeypatch):
        """Pinned is its OWN section, above Chats, and either can be put away.

        The two lists answer different questions — a curated shelf vs. a
        chronological feed — and as sibling groups in one list they could only be
        told apart by a header that scrolled away, with neither one closable. So:
        a section each, Pinned first, each with a disclosure and its own list."""
        self._enable_chat(web_client, monkeypatch)
        text = web_client.get("/library", cookies=admin_cookie).text
        rail = text.split('<nav class="rail"', 1)[1].split("</nav>", 1)[0]

        # Anatomy of each section: a labelled disclosure button wired to its own
        # body, and a list of its own.
        # "Recent", not "Chats": the feed is a capped slice with "View all chats"
        # under it, and a section labelled "Chats" over a slice would make that
        # link read as a second route to the same list.
        for sec, toggle, body, label in (
            ("rail-pinned", "rail-pinned-toggle", "rail-pinned-body", "Pinned"),
            ("rail-chats", "rail-chats-toggle", "rail-chats-body", "Recent"),
        ):
            assert f'id="{sec}"' in rail
            assert f'id="{toggle}"' in rail
            assert f'aria-controls="{body}"' in rail, f"{toggle} must point at its own body"
            assert f'id="{body}"' in rail
            assert f'<span class="rail-chatsec-txt">{label}</span>' in rail
        # Both start expanded server-side; rail_history.js applies the persisted
        # state before any rows arrive.
        assert rail.count('aria-expanded="true"') >= 2
        # Two lists, and the chat page's ids are still the ones the renderers
        # bind to (chat.js owns #chat-list on /chat unchanged).
        assert 'id="pinned-chat-list"' in rail
        assert 'id="chat-list"' in rail
        # Pinned above Chats, and the empty state lives in Chats (the section
        # that survives a first run) rather than between them.
        assert rail.find('id="rail-pinned"') < rail.find('id="rail-chats"') < rail.find('id="cloud-chat-empty-state"')
        # Pinned starts hidden — a "Pinned" header over nothing is dead chrome,
        # so a renderer unhides it only once it has rows.
        pinned_open = rail[rail.find('<section class="rail-chatsec" id="rail-pinned"') :][:120]
        assert " hidden" in pinned_open, "the Pinned section must start hidden"

    def test_chat_section_headers_are_labels_not_rows(self, web_client, admin_cookie, monkeypatch):
        """The headers keep the group-label voice they were promoted from —
        10.5px/700 uppercase muted ink — and stay LABELS: no background in any
        state, no border, no divider.

        This is the rail's colour rule, not taste (see
        test_accent_marks_where_you_are_and_nothing_else): the accent marks the
        active conversation and the neutral wash marks hovering an actual row, so
        a header that filled on hover would read as a selectable row. Ink is the
        one channel still free, so hover spends that."""
        css = web_client.get("/static/css/rail.css").text

        def block_for(selector):
            return css.split(selector, 1)[1].split("}", 1)[0]

        hd = block_for('html[data-ui-layout="rail"] .rail-chatsec-hd {')
        assert "background: none" in hd
        assert "border: none" in hd
        assert "border-top" not in hd and "border-bottom" not in hd
        assert "color: var(--ds-text-muted)" in hd
        # Left edge shared with the conversation titles below (rows: `padding:
        # 4px 9px`) — a section label indented differently from its own list
        # reads as a misalignment in a 240px column.
        assert "9px" in hd
        hover = block_for('html[data-ui-layout="rail"] .rail-chatsec-hd:hover {')
        assert "color: var(--ds-text-secondary)" in hover
        assert "background" not in hover, "a filled header would read as a row"
        txt = block_for('html[data-ui-layout="rail"] .rail-chatsec-txt {')
        assert "font-size: 10.5px" in txt
        assert "font-weight: 700" in txt
        assert "text-transform: uppercase" in txt
        # The caret annotates the label, so it sits against it — not flushed to
        # the rail's right edge, where it read as a second column of controls
        # beside the conversation rows' own "⋮". No count rides out there either.
        caret = block_for('html[data-ui-layout="rail"] .rail-chatsec-caret {')
        assert "margin-left: auto" not in caret
        assert "rail-chatsec-count" not in css
        # Keyboard reachability — it is a real <button>, so it must show focus.
        assert "outline: var(--ds-focus-outline)" in block_for(
            'html[data-ui-layout="rail"] .rail-chatsec-hd:focus-visible {'
        )
        # Whitespace separates the sections; the rail's only nav divider is
        # Admin's (test_admin_is_the_only_divided_group).
        gap = block_for('html[data-ui-layout="rail"] .rail-chatsec + .rail-chatsec {')
        assert "margin-top" in gap
        assert "border" not in gap

    def test_any_date_boundary_stays_subordinate_to_the_section_label(self, web_client, admin_cookie, monkeypatch):
        """A date boundary ("Older") lives INSIDE a section, so it cannot wear the
        same uppercase-700 label voice as that section's own header — two labels
        of equal weight nested one inside the other read as siblings, i.e. as two
        sections with the rows above the second one orphaned.

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

    def test_section_collapse_is_persisted(self, web_client, admin_cookie, monkeypatch):
        """One owner for the section chrome — rail_history.js, loaded on every
        rail page INCLUDING /chat (where chat.js owns only the rows, and calls in
        through window.railChatSections).

        The open state survives navigation: a disclosure that forgets what you
        did to it on every page load reads as broken, and the choice ("I live in
        my pins") is about how the caller works, not about the page they're on."""
        js = web_client.get("/static/js/rail_history.js").text
        assert "agnes.rail.chatsec." in js, "the open state must be persisted per section"
        assert "localStorage.setItem" in js and "localStorage.getItem" in js
        # Default OPEN: only an explicit "0" closes a section, so a first-time
        # caller never has to discover a disclosure to see their chats.
        assert '!== "0"' in js
        assert 'setAttribute("aria-expanded"' in js
        assert "window.railChatSections" in js, "chat.js needs a seam to re-sync after a render"
        chat_js = web_client.get("/static/js/chat.js").text
        assert "window.railChatSections" in chat_js, "/chat must re-sync the sections it re-renders"
        css = web_client.get("/static/css/rail.css").text
        assert 'html[data-ui-layout="rail"] .rail-chatsec.is-collapsed .rail-chatsec-caret {' in css

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
            'class="rail-getstarted"',  # ...then the onboarding card
            'class="rail-foot"',
            'id="userMenu"',  # profile, at the very bottom
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

        The destination used to be `.rail-history-all`, a link at the foot of the
        region. It is the Chats row above the lists now, and the old rules must
        not come back: a way to a page cannot live inside the one part of the rail
        that collapse hides."""
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
        # The foot-of-the-list link is gone from BOTH sheets and the renderer —
        # the Chats row (a `.rail-i`) is the destination now.
        assert 'html[data-ui-layout="rail"] .rail-history-all {' not in css
        assert "rail-view-all-chats" not in js


class TestRailChatsDestination:
    """Chats is a DESTINATION ROW in the rail, and every rail row has an icon.

    These two facts are one contract. The rail collapses to a 56px glyph strip —
    by default on /admin — so a row with no icon is a row that DISAPPEARS when it
    collapses. That is exactly how the way to /chats went missing: the whole
    conversation region is text (two section labels plus titles), its only door
    out was a "View all chats" link inside it, and that link was itself hidden
    until the caller had a conversation. On an admin page the product therefore
    had NO path to the chat list, and on a first run it had none anywhere.

    So: /chats is a `.rail-i` with a glyph, in the nav zone beside New chat; the
    conversation lists are content underneath it; and the icon rule is asserted
    directly, because the next text-only row would reintroduce the same bug in a
    different place."""

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

    def test_the_foot_of_the_list_link_is_gone_everywhere(self, web_client, admin_cookie, monkeypatch):
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        assert "rail-view-all-chats" not in self._rail(web_client, admin_cookie)
        css = web_client.get("/static/css/rail.css").text
        assert ".rail-history-all {" not in css
        js = web_client.get("/static/js/rail_history.js").text
        assert "rail-view-all-chats" not in js

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

    def test_the_onboarding_card_is_not_on_admin_pages(self, web_client, admin_cookie, monkeypatch):
        """It measures the ANALYST's journey — connect your tools, ask your first
        question — and it is the only element in the rail with a coloured progress
        arc, so it pulls hardest of anything on screen while you are registering a
        table. Nothing is lost: the checklist is still reachable from the account
        menu and from the chat dashboard's own hero."""
        monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
        self._enable_chat(web_client, monkeypatch)
        admin_rail = self._rail(web_client, admin_cookie, "/admin/users")
        assert 'id="railGetStarted"' not in admin_rail
        assert "rail-getstarted" not in admin_rail
        # …and the module that writes into it is not loaded there either.
        assert "js/chat_onboarding.js" not in web_client.get("/admin/users", cookies=admin_cookie).text
        # Still there on an app page — this is a scoping change, not a removal.
        assert 'id="railGetStarted"' in self._rail(web_client, admin_cookie)


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

    def test_rail_chat_renders_dashboard_empty_state(self, web_client, admin_cookie, monkeypatch):
        self._enable_chat(web_client, monkeypatch)
        resp = web_client.get("/chat", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 200
        text = resp.text
        assert 'data-ui-layout="rail"' in text
        for anchor in (
            # The Knowledge Layer hero — one premium banner: text lead + green
            # CTA on the left, orb in the centre, floating integration chips on
            # the right (no inner "Ask Agnes" / tools cards).
            'class="klb"',  # self-contained hero banner
            '<div class="klb-lead">',  # left text lead block
            "Agnes is your knowledge layer.",  # hero headline line 1
            "Use it here or connect your tools.",  # hero headline line 2 (gradient)
            '<p class="klb-sub">',  # the supporting sentence renders
            'href="/how-it-works#connect"',  # the green "Connect your tools" CTA target
            'class="klb-chips"',  # floating integration chips (no card)
            "Claude Code",  # a chip label
            "CLI and more",  # a chip label
            # Below the banner: the trust caption + "Ask Agnes anything" heading.
            "klb-hub-label--lead",  # the trust-caption wrapper
            "Secure. Private. Always in sync.",  # caption text, below the banner
            'class="rdb-ask-heading"',  # the usage heading above the composer
            "Ask Agnes anything",
            'id="rdb-actions"',  # the one personalized section
            'id="rdb-actions-list"',  # suggested-actions list
            "css/chat_dashboard.css",  # dashboard styles
            'id="chat-input"',  # the REAL composer serves the dashboard
        ):
            assert anchor in text, f"rail chat dashboard is missing {anchor}"
        # The retired three-panel layout is gone (one actions list instead).
        # `rdb-semantic-links` joins it: the dashboard carried a "Browse metrics
        # & glossary" button (#1108) directly above the composer, and this is
        # the moment of INTENT — the reader came here to ask something. A
        # control whose only function is to navigate away from the composer,
        # offered before they have an answer to check, is a detour.
        #
        # Asserted on the BUTTON'S wrapper class, not on the bare
        # `/catalog/semantics` URL: the rail chrome now carries a Definitions
        # nav row, so that URL is legitimately present on every rail page
        # including this one. What must not come back is the in-page button.
        for retired in (
            'id="rdb-continue-list"',
            'id="rdb-tasks"',
            "Recent updates",
            "rdb-semantic-links",
        ):
            assert retired not in text, f"retired dashboard panel leaked back: {retired}"
        # The banner's old two-button CTA row is retired: "Connect your tools"
        # moved into the tools card (klb-card-cta), "Learn how it works" into
        # the user menu. Neither the CTA row nor the outline secondary remain.
        assert 'class="klb-ctas"' not in text
        assert "klb-cta-secondary" not in text
        # One composer only — the retired standalone dashboard's look-alike
        # input and its prompt-handoff module must be gone.
        assert 'id="rdb-composer"' not in text
        assert "dashboard_rail" not in text
        # The retired ask-hero brand block is gone too.
        assert "Ask anything." not in text
        # The "Agnes Knowledge Layer" hub title was dropped as redundant with the
        # "Agnes is your knowledge layer." headline — it must not render anywhere.
        assert "Agnes Knowledge Layer" not in text

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

    def test_chat_keeps_upload_menu_journey_and_row_menu(self, web_client, admin_cookie, monkeypatch):
        """The composer "+" menu and the row menu render on the page; the
        journey checklist is the rail's own ``railGetStarted`` card
        (chat.html's ``#chat-journey`` div is the retired topnav sidebar's
        slot — the live page never renders it)."""
        resp = self._chat(web_client, admin_cookie)
        assert resp.status_code == 200
        assert 'id="chat-plus-menu"' in resp.text
        assert 'id="railGetStarted"' in resp.text
        assert "chat_row_menu.js" in resp.text
        assert 'id="chat-copy-transcript"' in resp.text

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
