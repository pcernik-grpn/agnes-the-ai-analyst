"""Smoke tests for web UI pages."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def web_client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    # Reset global DuckDB singleton to pick up new DATA_DIR
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


@pytest.fixture
def admin_cookie(web_client, tmp_path, monkeypatch):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository
    from tests.helpers.auth import grant_admin

    password = "AdminPass1!"
    password_hash = PasswordHasher().hash(password)
    conn = get_system_db()
    UserRepository(conn).create(
        id="admin1",
        email="admin@test.com",
        name="Admin",
        password_hash=password_hash,
    )
    grant_admin(conn, "admin1")
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "admin@test.com", "password": password})
    assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"access_token": token}


@pytest.fixture
def analyst_cookie(web_client, tmp_path, monkeypatch):
    from argon2 import PasswordHasher
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    password = "AnalystPass1!"
    password_hash = PasswordHasher().hash(password)
    conn = get_system_db()
    UserRepository(conn).create(
        id="analyst1",
        email="analyst@test.com",
        name="Analyst",
        password_hash=password_hash,
    )
    conn.close()
    resp = web_client.post("/auth/token", json={"email": "analyst@test.com", "password": password})
    assert resp.status_code == 200, f"Analyst token failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"access_token": token}


class TestWebUISmoke:
    def test_login_page(self, web_client):
        resp = web_client.get("/login")
        assert resp.status_code == 200

    def test_login_page_has_no_logo_by_default(self, web_client):
        # Vendor-neutral default: no operator logo configured → the login card
        # renders no brand lockup, just the text heading.
        resp = web_client.get("/login")
        assert resp.status_code == 200
        assert "login-card-logo" not in resp.text

    def test_login_page_renders_configured_logo(self, web_client, monkeypatch):
        # When an operator sets AGNES_INSTANCE_LOGO_SVG, the login card renders
        # it inline above the Sign In heading (same slot the app header uses).
        monkeypatch.setenv(
            "AGNES_INSTANCE_LOGO_SVG",
            '<svg id="brand-mark" xmlns="http://www.w3.org/2000/svg"></svg>',
        )
        resp = web_client.get("/login")
        assert resp.status_code == 200
        assert "login-card-logo" in resp.text
        assert 'id="brand-mark"' in resp.text

    def test_dashboard(self, web_client, admin_cookie):
        resp = web_client.get("/dashboard", cookies=admin_cookie)
        assert resp.status_code in (200, 302)

    def test_catalog(self, web_client, admin_cookie):
        resp = web_client.get("/catalog", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_corporate_memory(self, web_client, admin_cookie):
        resp = web_client.get("/corporate-memory", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_analyst_can_access_corporate_memory(self, web_client, analyst_cookie):
        """Curated Memory is user-facing (get_current_user) — non-admins
        reach it. The admin review queue is separate at
        /admin/corporate-memory."""
        resp = web_client.get("/corporate-memory", cookies=analyst_cookie)
        assert resp.status_code == 200

    def test_activity_center(self, web_client, admin_cookie):
        resp = web_client.get("/activity-center", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        if resp.status_code == 404:
            pytest.skip("Route /admin/tables does not exist")
        assert resp.status_code == 200

    def test_admin_permissions_route_removed(self, web_client, admin_cookie):
        """v19 dropped the half-shipped /admin/permissions page (replaced by
        the unified /admin/access page). Verify the route is gone."""
        resp = web_client.get("/admin/permissions", cookies=admin_cookie)
        assert resp.status_code == 404

    def test_admin_users_renders_modern_ui(self, web_client, admin_cookie):
        resp = web_client.get("/admin/users", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        # Shared chrome — the rail, the only one there is since Wave 0 (2026-08).
        assert 'class="rail' in body
        # User-self menu post-consolidation: Profile + My activity only.
        # Auth debug folded into /me/profile troubleshooting section; the
        # /me/debug nav entry is gone.
        assert 'href="/me/profile"' in body
        assert 'href="/me/activity"' in body
        assert 'href="/me/debug"' not in body
        # Admin dropdown still carries the cross-user PAT admin entry.
        assert 'href="/admin/tokens"' in body
        assert 'href="/admin/users"' in body
        # v12 modern UI markers — Role column was replaced by Groups chips,
        # so role-pill is gone. Confirm-modal pattern is shared by both.
        assert 'class="users-page"' in body
        assert 'id="confirm-modal"' in body

    def test_nav_shows_user_self_links_for_non_admin(self, web_client, analyst_cookie):
        """Non-admins see Profile + My activity user-menu links — no admin
        Tokens entry, no Auth debug entry (folded into /me/profile).

        Rendered off /library. /dashboard is a redirect since Wave 0 (2026-08),
        and the follow-the-Location dance this used to do dropped the cookie on
        the second hop, landing on /login — which has no nav at all, so every
        `not in` assertion below would have passed for the wrong reason."""
        resp = web_client.get("/library", cookies=analyst_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert 'href="/me/profile"' in body
        assert ">Profile<" in body
        assert 'href="/me/activity"' in body
        assert ">My activity<" in body
        # Auth debug entry is gone from the nav — folded into /me/profile.
        assert 'href="/me/debug"' not in body
        assert ">Auth debug<" not in body
        # Retired entries must not surface.
        assert ">My tokens<" not in body
        assert ">My sessions<" not in body
        # Non-admins must NOT see the admin Tokens link inside the Admin dropdown.
        assert 'href="/admin/tokens"' not in body

    def test_nav_shows_admin_surfaces_for_admin(self, web_client, admin_cookie):
        """Admins see the user-self menu plus a route to the admin area, and
        the admin area still carries cross-user Tokens / Tables / Users.

        The DOM half and the inventory half are asserted separately on purpose.
        This used to read every admin href off one page, because the topnav's
        Admin mega-menu rendered the whole inventory at once. The rail carries
        a single `/admin` destination and the sidebar renders only the ACTIVE
        section's body server-side (`test_web_admin_nav.py::
        test_only_the_active_section_renders_expanded_server_side`), so no
        single page can show them all — scraping one would just re-encode which
        section happens to be open.
        """
        resp = web_client.get("/admin/users", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        # User-self menu — same as non-admin; Auth debug gone from nav.
        assert 'href="/me/activity"' in body
        assert 'href="/me/debug"' not in body
        assert ">My tokens<" not in body
        # The rail's door into the admin area, and the People tab strip.
        assert 'href="/admin"' in body
        assert 'href="/admin/tokens"' in body

        # The inventory itself — the single source both the sidebar and the
        # coverage guard read.
        from app.web.admin_nav import ADMIN_NAV_SECTIONS, _section_entries

        hrefs = {e["href"] for s in ADMIN_NAV_SECTIONS for e in _section_entries(s)}
        hrefs |= {s["href"] for s in ADMIN_NAV_SECTIONS if s.get("href")}
        for href in ("/admin/tables", "/admin/tokens", "/admin/users"):
            assert href in hrefs, f"{href} is not in the admin nav inventory"

    def test_profile_renders_account_details(self, web_client, admin_cookie):
        """/me/profile renders a real profile page with email + inline PAT section.

        v12 changes: role-pill is replaced by an Admin-pill driven by Admin
        user_group membership; ``session.google_groups`` is gone (the
        OAuth callback writes Workspace memberships into
        ``user_group_members`` instead), so the "No Google groups available"
        empty state is no longer rendered.
        Task 3: /tokens link removed; PAT management is now inline on this page.
        """
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert "admin@test.com" in body
        assert 'href="/tokens"' not in body
        # Inline PAT section is present
        assert "Personal Authentication Tokens" in body
        assert 'id="new-token-btn"' in body
        # Session & troubleshooting partial is included — a broken
        # {% include %} or missing template var would drop this string.
        assert "User record" in body

    def test_profile_requires_auth(self, web_client):
        """/me/profile requires auth (was a 302 back-compat redirect before)."""
        resp = web_client.get("/me/profile", follow_redirects=False)
        # Auth dep raises 401; some configs may redirect to /login — accept either.
        assert resp.status_code in (401, 302)


class TestProfileSensitiveLeakage:
    """The /me/profile page absorbed the former /me/debug session-diagnostics
    surface (Session & troubleshooting section). The security invariant that
    protected that surface survives the move: the raw session JWT must never
    appear in the rendered page — only its decoded claims and a short
    fingerprint. Compensating test for the deleted
    test_me_debug.TestNoSensitiveLeakage.test_raw_jwt_not_in_body."""

    def test_raw_jwt_not_in_profile_body(self, web_client, analyst_cookie):
        """The full session JWT must never appear in the rendered /me/profile
        page — only its decoded claims and a short fingerprint."""
        raw_token = analyst_cookie["access_token"]
        resp = web_client.get("/me/profile", cookies=analyst_cookie)
        assert resp.status_code == 200
        assert raw_token not in resp.text, "raw JWT leaked into page body"

    @pytest.mark.skip(
        reason=(
            "v12: /me/profile no longer renders an admin-self-management link. "
            "Admin can navigate to /admin/users/{id} from the top-nav Admin "
            "dropdown directly. Drop or rewrite this test once the profile "
            "page settles."
        )
    )
    def test_profile_shows_admin_detail_link_for_admin(self, web_client, admin_cookie):
        resp = web_client.get("/me/profile", cookies=admin_cookie)
        assert resp.status_code == 200
        assert 'href="/admin/users/admin1"' in resp.text

    @pytest.mark.skip(
        reason=(
            "v12: profile page no longer surfaces /admin/users/* link at all, "
            "so the negative-assertion is moot. Header chrome unrelated to "
            "the profile body now contains the admin dropdown."
        )
    )
    def test_profile_hides_admin_detail_link_for_non_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/me/profile", cookies=analyst_cookie)
        assert resp.status_code == 200
        assert "/admin/users/" not in resp.text

    @pytest.mark.skip(
        reason=(
            "v12: the four-level core.viewer/analyst/km_admin/admin hierarchy "
            "is gone. Profile now shows group memberships (user_group_members) "
            "and effective resource access (resource_grants), not internal "
            "role keys. Rewrite against the new sections — see "
            "templates/profile.html."
        )
    )
    def test_profile_shows_effective_roles_for_non_admin(self, web_client, analyst_cookie):
        resp = web_client.get("/me/profile", cookies=analyst_cookie)
        assert resp.status_code == 200
        body = resp.text
        assert "Effective roles" in body
        assert "core.analyst" in body
        assert "core.viewer" in body
        assert "Direct grants" in body


class TestClaudeSetupPreview:
    """/install and /dashboard render a visible, read-only preview of the
    'Setup a new Claude Code' clipboard payload. The real login token is
    delivered out-of-band (see /home's Step 4) and never appears in this
    preview at all — there's no `{token}` placeholder left to substitute
    or mask.
    """

    def test_install_preview_visible_for_signed_in_user(self, web_client, admin_cookie):
        # /setup is now a single unified flow regardless of caller's role.
        # Admin sees the same layout as everyone else; the marketplace
        # block appears iff the caller has plugin grants in
        # `resource_grants` (the seeded admin in this fixture has none).
        resp = web_client.get("/setup", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        # Preview card renders; no token placeholder to render any more.
        assert "setup-preview-pre" in body
        assert "What Claude Code will receive" in body
        assert "&lt;will be generated on click&gt;" not in body
        assert 'class="placeholder-token"' not in body
        assert "{token}" not in body
        assert "eyJ" not in body
        # Setup payload text substituted with real server URL. Step 1
        # downloads via the unversioned /cli/download endpoint (immune to a
        # mid-session server version roll), not a filename-pinned
        # /cli/wheel/<name> URL.
        assert "/cli/download" in body
        assert "/cli/wheel/" not in body
        assert "/cli/agnes.whl" not in body
        # Thin layout: step 1 install, step 2 onboard, step 3 restart,
        # step 4 confirm. Diagnose/marketplace/catalog now run inside
        # `agnes onboard`, so they are not steps on the page any more.
        assert "1) Install the CLI" in body
        assert "2) Set up the" in body
        assert "agnes onboard" in body
        assert "4) Confirm:" in body
        assert "5) Run diagnostics" not in body
        # Superseded prompt headers are gone (`agnes onboard` subsumes
        # them). `agnes auth whoami` survives as a static manual-install
        # example elsewhere on the page (not in the generated prompt).
        assert "agnes init" not in body
        assert "2) Log in" not in body
        assert "3) Verify the login" not in body

    def test_install_preview_unified_layout(self, web_client, admin_cookie):
        """The clipboard payload (SETUP_INSTRUCTIONS_TEMPLATE JS array)
        carries the same thin prompt for every caller — admin-vs-analyst
        is no longer a layout branch, and neither are plugin grants: the
        marketplace bootstrap, the diagnose run and the connector setup
        all happen inside `agnes onboard`, off the live manifest, so the
        payload has no per-caller content left."""
        import re

        resp = web_client.get("/setup", cookies=admin_cookie)
        assert resp.status_code == 200
        body = resp.text
        match = re.search(
            r"var\s+SETUP_INSTRUCTIONS_TEMPLATE\s*=\s*\[(.*?)\]\.join\(",
            body,
            re.DOTALL,
        )
        assert match, "SETUP_INSTRUCTIONS_TEMPLATE array missing"
        clipboard = match.group(1)
        assert "agnes onboard" in clipboard
        # The orchestration verbs the prompt used to spell out are the
        # CLI's business now — none of them may reappear as prompt lines.
        assert "agnes init" not in clipboard
        assert "agnes refresh-marketplace" not in clipboard
        assert "agnes diagnose" not in clipboard
        # Connector bodies were never inlined and are not even referenced
        # now (the Atlassian MCP registration lives inside the SKILL.md
        # body the user pulls up after setup).
        assert "agnes connectors show" not in clipboard
        assert "claude mcp add --transport sse atlassian" not in clipboard
        # Legacy admin-only auth verbs are gone from the generated prompt.
        assert "agnes auth import-token" not in clipboard
        assert "3) Verify the login" not in clipboard
        assert "2) Log in" not in clipboard

    # `test_dashboard_setup_cta_links_to_setup` was here. It pinned the
    # `.env-setup-cta` card on /dashboard — a "link to /setup instead of an
    # inline collapsed preview" — and both halves of the premise are gone:
    # /dashboard became an unconditional redirect in Wave 0 (2026-08) and its
    # template, the only place `env-setup-cta` was ever emitted, went with it.
    # Not re-pinned onto /home or /install: those pages deliberately DO carry
    # the inline preview (`aria-label="Preview of the clipboard payload"`), so
    # the rule this asserted does not hold there and restating it would invent
    # a contract. The clipboard payload itself stays guarded — it is
    # single-sourced from `_claude_setup_instructions.jinja` and covered by
    # `tests/test_welcome_template_api.py`.

    def test_install_mcp_card_removed(self, web_client):
        """The stale 'Use with Claude Code / MCP' card on /setup has been
        removed — there is no Agnes-as-MCP-server today. The Atlassian
        MCP server registration step (Fix C in the 2026-05-10 init-report
        response) is registered FROM the setup script, not as a /setup-
        page card; that's an unrelated wiring direction.
        """
        resp = web_client.get("/setup")
        assert resp.status_code == 200
        body = resp.text
        assert "Use with Claude Code / MCP" not in body


class TestAdminRoleGuards:
    def test_analyst_cannot_access_admin_tables(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/admin/tables", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_can_access_admin_tables(self, web_client, admin_cookie):
        resp = web_client.get("/admin/tables", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_analyst_cannot_access_admin_groups_page(self, web_client, analyst_cookie):
        """Grants moved onto the group detail page's Access tab; /admin/groups
        is the entry point. Non-admin must still be blocked."""
        resp = web_client.get("/admin/groups", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_admin_can_access_admin_groups_page(self, web_client, admin_cookie):
        resp = web_client.get("/admin/groups", cookies=admin_cookie)
        assert resp.status_code == 200

    def test_access_page_renders(self, web_client, admin_cookie):
        """/admin/access is a real page again — the cross-group Access
        workspace plus Simulate. It was a standalone matrix, was retired into
        the group detail page's Access tab (grants key on `group_id`), and
        came back as the surface that tab cannot be: one place to move between
        groups, and to answer "why can't this person see X?"."""
        resp = web_client.get("/admin/access", cookies=admin_cookie)
        assert resp.status_code == 200
        # "Simulate a person" was a section tab; the person view is the third
        # position of the page's own switch now (`?by=person`, and
        # `?lens=simulate` still lands there).
        assert 'data-by="person"' in resp.text

    def test_legacy_grants_url_redirects_to_access(self, web_client, admin_cookie):
        """The page's oldest URL 308s onto it rather than 404ing, carrying
        ?group= through so an old deep link still preselects that group."""
        resp = web_client.get("/admin/grants", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/access"

        resp = web_client.get("/admin/grants?group=grp-123", cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 308
        assert resp.headers["location"] == "/admin/access?group=grp-123"

    def test_access_urls_keep_their_admin_gate(self, web_client, analyst_cookie):
        """Both the page and the legacy redirect refuse a non-admin — the
        redirect must not answer 308 naming an internal URL where the page
        answers 403."""
        for url in ("/admin/access", "/admin/grants"):
            resp = web_client.get(url, cookies=analyst_cookie, follow_redirects=False)
            assert resp.status_code == 403, url

    def test_analyst_cannot_access_corporate_memory_admin(self, web_client, admin_cookie, analyst_cookie):
        resp = web_client.get("/admin/corporate-memory", cookies=analyst_cookie)
        assert resp.status_code == 403

    def test_corporate_memory_admin_search_and_moderation_controls(self, web_client, admin_cookie):
        """Review + All Items each carry a search box, and the All Items
        batch bar carries the moderation actions (find-and-moderate
        surface — the search/bulk-action split between tabs is closed)."""
        resp = web_client.get("/admin/corporate-memory", cookies=admin_cookie)
        assert resp.status_code == 200
        html = resp.text
        for element_id in (
            "reviewSearch",
            "allSearch",
            "batchApproveBtnAll",
            "batchRejectBtnAll",
            "batchRevokeBtnAll",
            "batchRequireBtnAll",
        ):
            assert f'id="{element_id}"' in html, element_id

    def test_admin_prompts_page_admin_only(self, web_client, admin_cookie, analyst_cookie):
        """The unified /admin/prompts page (#622) is gated by require_admin."""
        # Unauthenticated → 302 redirect to login
        r = web_client.get("/admin/prompts", follow_redirects=False)
        assert r.status_code in (302, 401, 403)
        # Non-admin → 403
        r = web_client.get("/admin/prompts", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 403
        # Admin → 200
        r = web_client.get("/admin/prompts", cookies=admin_cookie, follow_redirects=False)
        assert r.status_code == 200

    def test_legacy_prompt_pages_redirect(self, web_client, admin_cookie):
        """/admin/agent-prompt + /admin/workspace-prompt 308 → /admin/prompts
        (#622). Unconditional redirect, like the /admin/grants precedent — the
        target page enforces require_admin."""
        for old in ("/admin/agent-prompt", "/admin/workspace-prompt"):
            r = web_client.get(old, cookies=admin_cookie, follow_redirects=False)
            assert r.status_code == 308, f"{old} → {r.status_code}"
            assert r.headers["location"] == "/admin/prompts"

    def test_admin_scheduler_runs_page_admin_only(self, web_client, admin_cookie, analyst_cookie):
        """`/admin/scheduler-runs` collapsed into the unified Activity
        page as a `source=scheduler` filter. Route now 308-redirects;
        admin-only gate still applies before the redirect fires.
        """
        # Anonymous → not admin → 302 to login (require_admin runs first).
        r = web_client.get("/admin/scheduler-runs", follow_redirects=False)
        assert r.status_code in (302, 401, 403)
        # Analyst → 403 (require_admin fails before we hit the redirect).
        r = web_client.get("/admin/scheduler-runs", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 403
        # Admin → 308 to the unified page with the source filter pre-set.
        r = web_client.get("/admin/scheduler-runs", cookies=admin_cookie, follow_redirects=False)
        assert r.status_code == 308
        assert r.headers["location"] == "/admin/activity?source=scheduler"

    def test_profile_sessions_redirects_to_me_activity(self, web_client, analyst_cookie, admin_cookie):
        """/profile/sessions now 301-redirects to /me/activity?tab=sessions
        (consolidated in the /me/activity page)."""
        r = web_client.get("/profile/sessions", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 301
        assert r.headers["location"] == "/me/activity?tab=sessions"
        r = web_client.get("/profile/sessions", cookies=admin_cookie, follow_redirects=False)
        assert r.status_code == 301

    def test_profile_session_download_path_safety(self, web_client, analyst_cookie):
        """Per-session download endpoint must reject any filename that could
        escape the user's own session directory."""
        # NB: bare ".." is excluded — httpx normalises the URL to
        # /profile/sessions before sending, so it never reaches the
        # download handler. The %2F-encoded variant exercises the real
        # path-component value that does reach the handler.
        for bad in ["../etc/passwd", "subdir/file.jsonl", ".env", "session.jsonl.bak", "..%2Fetc%2Fpasswd"]:
            r = web_client.get(f"/profile/sessions/{bad}", cookies=analyst_cookie, follow_redirects=False)
            assert r.status_code == 404, f"Expected 404 for {bad!r}, got {r.status_code}"
        # Unauthenticated → never the file
        r = web_client.get("/profile/sessions/anything.jsonl", follow_redirects=False)
        assert r.status_code in (302, 401, 403)

    def test_me_activity_page_renders(self, web_client, analyst_cookie):
        """/me/activity renders for authenticated users (consolidated view)."""
        r = web_client.get("/me/activity", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 200
        assert b"My activity" in r.content

    def test_profile_session_download_returns_file_for_owner(self, web_client, analyst_cookie, tmp_path, monkeypatch):
        """Authenticated owner can fetch their own jsonl with proper Content-Disposition."""
        # The seeded analyst is "analyst1" (per conftest.seeded_app).
        user_sessions = tmp_path / "user_sessions" / "analyst1"
        user_sessions.mkdir(parents=True)
        sample = user_sessions / "abc-123.jsonl"
        sample.write_text('{"event": "test"}\n')
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        r = web_client.get("/profile/sessions/abc-123.jsonl", cookies=analyst_cookie, follow_redirects=False)
        assert r.status_code == 200
        assert r.headers.get("content-disposition", "").endswith('filename="abc-123.jsonl"')
        assert b'"event": "test"' in r.content


class TestUnauthenticatedHtmlRedirects:
    def test_dashboard_unauthenticated_redirects_to_login(self, web_client):
        resp = web_client.get("/dashboard", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")
        assert "next=%2Fdashboard" in resp.headers["location"]

    def test_catalog_unauthenticated_redirects_to_login(self, web_client):
        resp = web_client.get("/catalog", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/login")
        assert "next=%2Fcatalog" in resp.headers["location"]

    def test_api_route_still_returns_json_401(self, web_client):
        # /api/sync/manifest requires auth; must keep JSON 401 (no redirect).
        resp = web_client.get("/api/sync/manifest", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_password_login_honors_next(self, web_client, tmp_path):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        password = "TestPass1!"
        conn = get_system_db()
        UserRepository(conn).create(
            id="u1",
            email="u1@test.com",
            name="U1",
            password_hash=PasswordHasher().hash(password),
        )
        conn.close()
        resp = web_client.post(
            "/auth/password/login/web",
            data={"email": "u1@test.com", "password": password, "next": "/catalog"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/catalog"

    def test_password_login_rejects_open_redirect(self, web_client, tmp_path):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository

        password = "TestPass1!"
        conn = get_system_db()
        UserRepository(conn).create(
            id="u2",
            email="u2@test.com",
            name="U2",
            password_hash=PasswordHasher().hash(password),
        )
        conn.close()
        resp = web_client.post(
            "/auth/password/login/web",
            data={"email": "u2@test.com", "password": password, "next": "//evil.example/"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

    @pytest.mark.parametrize(
        "hostile_next,expected_location",
        [
            ("javascript:alert(1)", "/dashboard"),
            ("http://evil.example/", "/dashboard"),
            ("//evil.example/", "/dashboard"),
            ("dashboard", "/dashboard"),  # missing leading slash
            ("/foo?bar=baz", "/foo?bar=baz"),  # valid same-origin with query
        ],
    )
    def test_password_login_sanitizes_next(self, web_client, tmp_path, hostile_next, expected_location):
        from argon2 import PasswordHasher
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        import uuid

        password = "TestPass1!"
        uid = f"u-{uuid.uuid4().hex[:8]}"
        conn = get_system_db()
        UserRepository(conn).create(
            id=uid,
            email=f"{uid}@test.com",
            name=uid,
            password_hash=PasswordHasher().hash(password),
        )
        conn.close()
        resp = web_client.post(
            "/auth/password/login/web",
            data={"email": f"{uid}@test.com", "password": password, "next": hostile_next},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"] == expected_location

    def test_non_api_post_still_returns_json_401(self, web_client):
        # POST to a JSON auth endpoint that lives outside /api/ — must NOT be redirected.
        resp = web_client.post("/auth/token", json={"email": "nope@x.com", "password": "wrong"}, follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_auth_json_get_still_returns_json_401(self, web_client):
        # GET to a JSON endpoint under /auth/* (e.g. PAT CRUD) — must NOT be redirected,
        # so CLI clients calling api_get("/auth/tokens") get JSON they can parse.
        resp = web_client.get("/auth/tokens", follow_redirects=False)
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")

    def test_login_page_propagates_next_to_password_button(self, web_client):
        resp = web_client.get("/login?next=/catalog")
        assert resp.status_code == 200
        body = resp.text
        # Password button URL should carry next.
        assert "/login/password?next=%2Fcatalog" in body, (
            f"Expected /login/password?next=%2Fcatalog in login page HTML; got snippet: {body[:500]}"
        )

    def test_login_page_propagates_next_to_google_button(self, web_client, monkeypatch):
        """The Google OAuth button URL must also carry the ?next param so the
        post-login redirect honors the requested destination."""
        # Force Google provider to appear available so the button is rendered.
        monkeypatch.setattr(
            "app.auth.providers.google.is_available",
            lambda: True,
        )
        resp = web_client.get("/login?next=/catalog")
        assert resp.status_code == 200
        body = resp.text
        assert "/auth/google/login?next=%2Fcatalog" in body, (
            f"Expected google login URL with ?next in login page; snippet: {body[:800]}"
        )

    def test_login_email_page_extracts_and_renders_next(self, web_client, monkeypatch):
        """/login/email (magic link) must extract ?next from the URL and
        emit it into the hidden form field so it round-trips to the POST."""
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")  # mail transport → page renders
        # Email is opt-in-only by default (B6) — this test exercises the page
        # itself, not the default-offering policy, so name it explicitly.
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
        resp = web_client.get("/login/email?next=/catalog")
        assert resp.status_code == 200
        body = resp.text
        # The template renders <input type="hidden" name="next" value="/catalog">
        assert 'name="next" value="/catalog"' in body, f"Expected /catalog in next hidden field; snippet: {body[:800]}"

    def test_login_email_page_rejects_open_redirect_in_next(self, web_client, monkeypatch):
        """Hostile ?next values (e.g. //evil) must be sanitized away before
        the hidden field is rendered."""
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
        resp = web_client.get("/login/email?next=//evil.example/")
        assert resp.status_code == 200
        body = resp.text
        assert "evil.example" not in body
        # Empty string is the sanitized default.
        assert 'name="next" value=""' in body

    def test_login_email_page_renders_magic_link_form(self, web_client, monkeypatch):
        """/login/email must render the magic-link form, not the password
        form. The password form posts to /auth/password/*, which 404s
        under an `auth.providers: [email]` allowlist — that mismatch used
        to lock the whole web UI out (regression)."""
        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
        resp = web_client.get("/login/email")
        assert resp.status_code == 200
        body = resp.text
        assert 'action="/auth/email/send-link/web"' in body
        assert "/auth/password/login/web" not in body

    def test_login_email_page_redirects_when_no_mail_transport(self, web_client, monkeypatch):
        """Without SMTP/SendGrid (and outside dev mode) the magic-link page
        would take an email and claim a link was sent that never arrives, so
        it redirects to /login with an explanatory error instead of pretending
        (Devin review on #1288)."""
        for var in ("SMTP_HOST", "SENDGRID_API_KEY", "LOCAL_DEV_MODE"):
            monkeypatch.delenv(var, raising=False)
        # Email is allowed (reaches the mail-transport check this test is
        # about) but not available — allowed and available are independent.
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
        resp = web_client.get("/login/email", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login?error=email_not_configured"

    def test_send_link_web_post_refuses_when_no_mail_transport(self, web_client, monkeypatch):
        """The POST sibling must refuse on its own — it is reachable without a
        freshly-rendered GET (stale form, bookmark, scripted client), and
        without a transport the sent-page's "We sent a sign-in link" would be
        a lie: delivery is silently skipped (Devin Review on PR #1288)."""
        for var in ("SMTP_HOST", "SENDGRID_API_KEY", "LOCAL_DEV_MODE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
        resp = web_client.post(
            "/auth/email/send-link/web",
            data={"email": "someone@example.com"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?error=email_not_configured"

    def test_sent_page_expiry_copy_is_computed_from_the_token_ttl(self, web_client, monkeypatch):
        """The sent-page's expiry sentence must be rendered from
        MAGIC_LINK_EXPIRY, not hand-copied — the template said "15 minutes"
        while the token actually lived an hour (Devin Review on PR #1288).
        The expectation is derived from the constant so a TTL change cannot
        re-split the copy from the behavior."""
        from app.auth.providers.email import MAGIC_LINK_EXPIRY

        monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("AGNES_AUTH_PROVIDERS", "email,password")
        monkeypatch.delenv("LOCAL_DEV_MODE", raising=False)
        resp = web_client.post(
            "/auth/email/send-link/web",
            data={"email": "someone@example.com"},
        )
        assert resp.status_code == 200
        assert f"The link expires in {MAGIC_LINK_EXPIRY // 60} minutes." in resp.text

    def test_google_login_stashes_safe_next_in_session(self, web_client, monkeypatch):
        """google_login() must stash the sanitized next_path in the session.

        We can't exercise the full OAuth flow without a Google mock, but we
        can verify the helper applies the sanitizer correctly."""
        from app.auth._common import safe_next_path

        # Valid same-origin paths pass through.
        assert safe_next_path("/catalog") == "/catalog"
        assert safe_next_path("/foo?bar=baz") == "/foo?bar=baz"
        # Open-redirect shapes get defaulted.
        assert safe_next_path("//evil.example/") == "/dashboard"
        assert safe_next_path("http://evil.example/") == "/dashboard"
        assert safe_next_path("javascript:alert(1)") == "/dashboard"
        assert safe_next_path("") == "/dashboard"
        assert safe_next_path(None) == "/dashboard"
        # Empty-default variant (used when computing query string).
        assert safe_next_path(None, default="") == ""
