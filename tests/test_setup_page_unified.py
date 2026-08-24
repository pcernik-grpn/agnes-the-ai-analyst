"""Tests for the unified `/setup` route.

The previous `?role=analyst|admin` query parameter is gone. The route
renders a single layout for everyone — admin-vs-analyst is no longer a
branch, and since the install prompt went thin neither are plugin grants:
the marketplace bootstrap happens inside `agnes onboard`, off the live
manifest, so the rendered prompt is caller-independent.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    """TestClient against a freshly-built FastAPI app rooted at tmp_path.

    Mirrors the `web_client` fixture in tests/test_web_ui.py — we re-create
    the app so the DuckDB singleton picks up the per-test DATA_DIR rather
    than leaking state across tests on the same xdist worker.
    """
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


def test_setup_page_renders_unified_layout(client):
    """Bare `/setup` (no query param) renders the thin flow:

    - `agnes onboard` is the one orchestration call (it subsumes the old
      init / catalog / preflight / marketplace / diagnose steps, which in
      turn subsumed the admin-only `agnes auth import-token` +
      `agnes auth whoami` pair).
    - Four steps, so Confirm = step 4 for every caller.
    """
    resp = client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    text = resp.text
    # Unified flow markers.
    assert "agnes onboard" in text
    # Superseded login/bootstrap verbs are gone from the rendered prompt.
    assert "agnes init" not in text
    assert "agnes auth import-token" not in text
    assert "4) Confirm:" in text


def test_setup_page_ignores_role_query_param(client):
    """`?role=...` is no longer accepted by the route signature. FastAPI
    ignores unknown query params silently — `/setup?role=admin` still
    serves the unified layout. No 422, no redirect, no behavior delta
    vs. bare `/setup`."""
    bare = client.get("/setup", follow_redirects=True)
    with_role = client.get("/setup?role=admin", follow_redirects=True)
    assert bare.status_code == 200
    assert with_role.status_code == 200
    # Both responses contain the unified-flow marker.
    assert "agnes onboard" in bare.text
    assert "agnes onboard" in with_role.text
    # Superseded login/bootstrap verbs are gone from both.
    assert "agnes init" not in bare.text
    assert "agnes init" not in with_role.text
    assert "agnes auth import-token" not in bare.text
    assert "agnes auth import-token" not in with_role.text


def test_setup_page_renders_marketplace_for_user_with_grants(client, monkeypatch):
    """A caller with a non-empty served stack gets the SAME prompt as
    everyone else: the thin prompt has no plugin-grant branch left, because
    `agnes onboard` installs from the LIVE marketplace manifest at run time
    — strictly fresher than a render-time snapshot, and one less way for
    the page to leak who has which grant.

    Stub `marketplace_filter.resolve_user_marketplace` to return a
    plugin so we don't have to seed the full marketplace plumbing in
    this test — we're verifying the layout, not the RBAC resolver
    itself (covered by `test_marketplace_filter`)."""
    from app.web.router import get_optional_user
    from fastapi import Request
    from src import marketplace_filter

    async def _admin_user(request: Request):  # type: ignore[no-redef]
        return {"id": "admin-1", "email": "admin@example.com", "is_admin": True, "name": "Admin", "groups": ["Admin"]}

    monkeypatch.setattr(
        marketplace_filter,
        "resolve_user_marketplace",
        lambda conn, user: [{"manifest_name": "demo-plugin"}],
    )

    client.app.dependency_overrides[get_optional_user] = _admin_user
    try:
        resp = client.get("/setup", follow_redirects=True)
    finally:
        client.app.dependency_overrides.pop(get_optional_user, None)

    assert resp.status_code == 200
    text = resp.text
    # Same four steps as for a caller with no grants at all.
    assert "agnes onboard" in text
    assert "4) Confirm:" in text
    # None of the orchestration the CLI owns leaks back into the page —
    # not the marketplace bootstrap, not the git/claude preflight, not the
    # connector wizards (incl. the Atlassian MCP registration).
    assert "agnes refresh-marketplace" not in text
    assert "Register the Agnes Claude Code marketplace" not in text
    assert "Needs git and claude on PATH" not in text
    assert "agnes connectors show" not in text
    assert "claude mcp add --transport sse atlassian" not in text


def test_install_legacy_path_redirects_to_setup(client):
    """`/install` legacy path keeps redirecting to `/setup` (302/307)."""
    resp = client.get("/install", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "/setup" in resp.headers["location"]


def test_first_time_setup_renders_all_wizard_fields(client):
    """The first-time-setup wizard (`setup.html`, served at /first-time-setup)
    still renders every field end-to-end through the real Jinja/route stack
    after the base_login.html → base_ds.html migration (#586).

    The `client` fixture builds a fresh app rooted at tmp_path with no seeded
    users, so /first-time-setup serves the wizard rather than redirecting to
    /login. We assert all four steps, the four progress dots, the key inputs,
    and that the page rides the design-system narrow shell — proving no field
    was lost when the login-card wrapper divs were removed and that the page
    is genuinely on base_ds (footer copyright + nav header), not base_login.
    """
    resp = client.get("/first-time-setup", follow_redirects=False)
    assert resp.status_code == 200
    text = resp.text
    # Every wizard field marker survives the wrapper-div removal.
    for marker in (
        'id="admin-email"',
        'id="admin-password"',
        'id="data-source"',
        'id="step-1"',
        'id="step-2"',
        'id="step-3"',
        'id="step-4"',
        'id="step-dot-1"',
        'id="step-dot-2"',
        'id="step-dot-3"',
        'id="step-dot-4"',
    ):
        assert marker in text, f"wizard field marker missing: {marker}"
    # Design-system shell marker — the page now opts into the narrow container.
    assert "container--narrow" in text
    # base_ds-only chrome the old base_login lacked: the shared page footer
    # confirms the page is genuinely on the design-system base, not just
    # textually edited. Was keyed on `&copy;` / a bare `<footer>`, both of
    # which the footer rewrite retired — the credit line is gone and the tag
    # now carries a class. `site-footer` is the stable marker.
    assert "site-footer" in text
    # The bespoke login-card chrome is gone.
    assert "max-width: 520px" not in text


def test_first_time_setup_data_source_dropdown(client):
    """The Step 2 "Data Source" `<select>` (#1055) keeps its native control —
    `toggleSourceFields()` / `configureSource()` read `#data-source` directly
    and must keep working unchanged — with a paired `ds.dropdown()` custom
    button+menu alongside it. Visibility between the two is a CSS theme
    decision (paper-skin.css), not a template one."""
    resp = client.get("/first-time-setup", follow_redirects=False)
    assert resp.status_code == 200
    text = resp.text
    # Native select: untouched wiring.
    assert 'id="data-source"' in text
    assert 'onchange="toggleSourceFields()"' in text
    assert 'class="ds-dropdown-native"' in text or "ds-dropdown-native" in text
    # Custom dropdown: paired to the native select.
    assert 'class="ds-dropdown"' in text
    assert 'data-ds-dropdown-target="data-source"' in text
    assert 'id="data-source-dd-btn"' in text
    assert 'aria-controls="data-source-dd-menu"' in text
    assert 'role="menuitemradio"' in text
    for value in ("keboola", "bigquery", "local"):
        assert f'data-value="{value}"' in text
    assert 'aria-checked="true"' in text
    assert "js/components/ds_dropdown.js" in text
