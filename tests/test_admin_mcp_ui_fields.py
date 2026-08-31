"""Template-content assertions for the MCP-source admin UI (Phase 2).

Cheap, deterministic checks that the create/edit forms surface the new
``env`` + ``scope`` fields, relabel the legacy ``auth_secret_env`` path,
drop the misleading help text, and that the detail page carries the
write-only vault-secret control + a list secret-status badge.
"""

from pathlib import Path

TPL = Path("app/web/templates")


def _read(name):
    return (TPL / name).read_text()


def test_the_listing_page_no_longer_carries_a_create_form():
    """It used to hold a modal with the same eleven fields as the builder, and
    no connection check — so the route the admin nav leads to was the one that
    could register a server nothing had ever reached. The fields below are the
    detail page's job now (see the test underneath); creation is
    /admin/mcp-sources/new."""
    html = _read("admin_mcp_sources.html")
    for dead in ('id="new-env"', 'id="new-scope"', 'id="new-transport"', 'id="confirm-create-btn"'):
        assert dead not in html, f"the create modal is back on the listing page: {dead}"
    assert "/admin/mcp-sources/new" in html, "the listing page links to no creation path at all"


def test_detail_form_has_env_scope_and_vault_secret_controls():
    html = _read("admin_mcp_source_detail.html")
    assert 'id="edit-env"' in html
    assert 'id="edit-scope"' in html
    assert 'id="set-vault-secret"' in html  # secret value input
    assert "/secret" in html  # PUT/DELETE vault secret endpoint used by JS
    assert "legacy" in html.lower()


def test_materialize_toast_reports_the_run_outcome():
    """A 200 from /materialize does not mean the tool produced rows: an emptied
    table and a per-tool failure both come back inside the body. The toast used
    to say "Materialize triggered" for all three."""
    html = _read("admin_mcp_source_detail.html")
    assert "empty_upstream" in html  # branches on the extractor's error/table code
    assert "reset to 0 rows" in html  # upstream went empty
    assert "rows materialized" in html  # normal run reports the row count


def test_list_shows_secret_status():
    html = _read("admin_mcp_sources.html")
    assert "has_vault_secret" in html  # list JS reads the flag to render a badge


def test_detail_has_inline_my_connection_panel():
    """per_user sources: the admin can connect + test their OWN credential
    right on the detail page instead of hopping to /me/connections first."""
    html = _read("admin_mcp_source_detail.html")
    assert 'id="my-connection-card"' in html
    assert 'id="myconn-token"' in html
    assert "myconn-save-btn" in html  # ds.button ids appear as macro args in raw template
    assert "myconn-test-btn" in html
    assert "myconn-clear-btn" in html
    assert "/my-secret" in html  # per-user secret API used by JS


def test_admin_connection_card_handles_expired_stored_connection():
    """The admin "Your connection" JS must have a third branch: stored but
    unusable (expired, no refresh path) still offers Disconnect (Devin
    Review on #1130)."""
    html = _read("admin_mcp_source_detail.html")
    assert "Connection expired — reconnect or disconnect" in html


def test_vault_secret_card_renders_last_rotated_timestamp():
    """The shared-vault card must show WHEN the secret was last rotated, not
    just that one is set — same '(since YYYY-MM-DD)' style the per-user
    'Your connection' card on this same page already uses, so the two don't
    invent two date formats."""
    html = _read("admin_mcp_source_detail.html")
    assert "vault_secret_updated_at" in html
    # Reuses the exact slice(0, 10) + "(since …)" convention renderMyConnection()
    # already established on this page (Devin Review: don't invent a new format).
    assert "vault_secret_updated_at.slice(0, 10)" in html
    assert "(since $" in html


def test_detail_has_per_user_secret_coverage_table():
    """Admin-only 'who has connected their own secret, and when' table —
    identity + timestamp only, never a secret value."""
    html = _read("admin_mcp_source_detail.html")
    assert 'id="peruser-table"' in html
    assert 'id="peruser-tbody"' in html
    assert "per_user_secrets" in html  # reads the GET detail payload's new key


def test_auth_method_selects_offer_oauth():
    """Both the create and edit forms must offer auth_method='oauth' — a
    select without the option silently coerces an oauth source to '' on
    save, flipping auth away from oauth and (by design) purging everyone's
    tokens (Devin Review on #1130)."""
    # The listing page's own create form is gone (see above), so the two forms
    # that can still write auth_method are the builder and the detail page.
    assert 'value="oauth"' in _read("admin_mcp_source_detail.html")
    builder = Path("app/web/static/js/components/mcp_builder.js").read_text(encoding="utf-8")
    assert "{ id: 'oauth', label: 'OAuth' }" in builder
