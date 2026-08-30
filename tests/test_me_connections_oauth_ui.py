"""``/me/connections`` and the admin inline "Your connection" panel —
oauth-source UI (2026-07-30 outbound MCP OAuth sources spec §3).

Two layers, mirroring ``tests/test_admin_mcp_ui_fields.py``'s style:

* Template-content assertions — cheap, deterministic checks that the
  templates carry the Connect/Disconnect markup and the connected/error
  banner hooks.
* A route-level integration test that a granted analyst sees the oauth
  branch rendered for an ``auth_method='oauth'`` source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from cryptography.fernet import Fernet

from app.secrets_vault import _reset_ephemeral_key_for_tests


@pytest.fixture(autouse=True)
def _stable_vault_key(monkeypatch):
    """Token upserts in these tests encrypt through the vault — same
    autouse key fixture as tests/test_mcp_user_secrets.py."""
    monkeypatch.setenv("AGNES_VAULT_KEY", Fernet.generate_key().decode())
    _reset_ephemeral_key_for_tests()
    yield
    _reset_ephemeral_key_for_tests()


TPL = Path("app/web/templates")


def _read(name: str) -> str:
    return (TPL / name).read_text()


# ---------------------------------------------------------------------------
# Template content
# ---------------------------------------------------------------------------


def test_me_connections_has_oauth_connect_and_disconnect_markup():
    html = _read("me_connections.html")
    assert 'data-auth-kind="{{ s.auth_kind }}"' in html
    assert "oauth/authorize" in html
    assert 'data-action="oauth-disconnect"' in html
    assert "oauth/connection" in html  # DELETE endpoint used by JS


def test_me_connections_has_connected_and_error_banners():
    html = _read("me_connections.html")
    assert "connected_source" in html
    assert "connect_error" in html


def test_admin_detail_has_oauth_connect_and_disconnect_controls():
    html = _read("admin_mcp_source_detail.html")
    assert 'id="myconn-oauth-controls"' in html
    assert 'id="myconn-oauth-connect-link"' in html
    assert "myconn-oauth-disconnect-btn" in html
    assert "myconn-oauth-test-btn" in html
    assert "oauth/authorize" in html
    assert "oauth/connection" in html


# ---------------------------------------------------------------------------
# Route: GET /me/connections
# ---------------------------------------------------------------------------


def _seed_oauth_source(source_id: str = "src_oauth_ui", grant_to: str = "analyst1") -> None:
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository
    from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name=source_id,
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    tools = ToolRegistryRepository(conn)
    tools.upsert(
        tool_id=f"{source_id}.lookup",
        source_id=source_id,
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="grant target",
    )
    grp = UserGroupsRepository(conn).create(name=f"grant-{source_id}", description=None)
    tools.add_grant(f"{source_id}.lookup", grp["id"])
    UserGroupMembersRepository(conn).add_member(grant_to, grp["id"], source="system_seed")
    conn.close()


def test_me_connections_page_renders_oauth_source(seeded_app):
    _seed_oauth_source()
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200, r.text
    assert 'data-auth-kind="oauth"' in r.text
    assert "/api/mcp/sources/src_oauth_ui/oauth/authorize" in r.text


def test_me_connections_rail_redesign_keeps_the_card_contracts(seeded_app, monkeypatch):
    """The rail chrome serves the REDESIGNED template
    (me_connections.html; topnav keeps the frozen legacy copy), so the card
    contracts every test above pins — auth-kind branches, OAuth authorize
    href, the action buttons the page script drives — must hold in that
    template too, or they are guarded on only half the pair."""
    monkeypatch.setenv("AGNES_UI_LAYOUT", "rail")
    _seed_oauth_source(source_id="src_oauth_rail")
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert 'class="connx-head"' in body  # the redesigned page, not legacy
    assert 'data-auth-kind="oauth"' in body
    assert "/api/mcp/sources/src_oauth_rail/oauth/authorize" in body
    assert 'data-action="oauth-connect"' in body
    # The script's contracts survive the restyle.
    assert "data-status" in body
    assert "data-highlight=" in body


def test_me_connections_page_shows_connected_banner(seeded_app):
    from src.repositories import mcp_user_oauth_tokens_repo

    _seed_oauth_source()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_ui", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "src_oauth_ui"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "Connected <strong>src_oauth_ui</strong>" in r.text


def test_me_connections_admin_sees_ungranted_source_with_bootstrap_note(seeded_app):
    """The register → connect → introspect bootstrap: an admin must see a
    freshly registered per_user source on /me/connections even before any
    tools exist, with a note linking to the admin page (UX round on #1130)."""
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_boot",
        name="src_oauth_boot",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200
    assert 'id="source-src_oauth_boot"' in r.text
    assert "No tools are enabled for this source yet" in r.text
    assert "/admin/mcp-sources/src_oauth_boot" in r.text


def test_me_connections_analyst_sees_own_connection_even_without_grants(seeded_app):
    """A connection you made must never be invisible — the card (with
    Disconnect) shows even when no tools are granted to you."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_mine",
        name="src_oauth_mine",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_mine", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert 'id="source-src_oauth_mine"' in r.text
    assert "No tools are enabled for this source yet" in r.text


def test_disconnect_works_without_a_live_grant(seeded_app):
    """Deleting YOUR OWN stored credential needs no tool grant — a revoked
    user's card must not dead-end on Disconnect (Devin Review on #1167)."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_revoked",
        name="src_oauth_revoked",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_revoked", "analyst1", "atok")
    hdr = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    r = seeded_app["client"].delete("/api/mcp/sources/src_oauth_revoked/oauth/connection", headers=hdr)
    assert r.status_code == 204, r.text
    assert mcp_user_oauth_tokens_repo().get("src_oauth_revoked", "analyst1") is None
    # …but with no stored row either, the grant gate still applies.
    r2 = seeded_app["client"].delete("/api/mcp/sources/src_oauth_revoked/oauth/connection", headers=hdr)
    assert r2.status_code == 403


def test_revoked_card_hides_test_but_keeps_disconnect(seeded_app):
    """A stored-but-ungranted card renders Disconnect (works grant-free)
    but not Test (grant-gated upstream call that could only 403)."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_norights",
        name="src_oauth_norights",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_norights", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    section = r.text.split('id="source-src_oauth_norights"')[1].split('class="conn-card"')[0]
    assert 'data-action="oauth-disconnect"' in section
    assert 'data-action="test"' not in section


def test_me_connections_analyst_does_not_see_ungranted_unconnected_source(seeded_app):
    """Without granted tools AND without an own connection, a non-admin
    still doesn't see the source — nothing they could do with it."""
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_hidden",
        name="src_oauth_hidden",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "src_oauth_hidden" not in r.text


def test_me_connections_connected_banner_uses_source_name(seeded_app):
    """The success banner names the source, not its UUID (UX round on #1130)."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="11111111-2222-3333-4444-555555555555",
        name="Keboola (EU)",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("11111111-2222-3333-4444-555555555555", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "11111111-2222-3333-4444-555555555555"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "Connected <strong>Keboola (EU)</strong>" in r.text


def test_me_connections_error_banner_offers_retry_link(seeded_app):
    """?connect_error with a valid &retry= renders a one-click Try again
    link to the authorize endpoint; an id the caller cannot see renders no
    link (crafted-link safety)."""
    from src.repositories import mcp_user_oauth_tokens_repo

    _seed_oauth_source(source_id="src_oauth_retry")
    mcp_user_oauth_tokens_repo().upsert("src_oauth_retry", "analyst1", "atok")
    hdr = {"Authorization": f"Bearer {seeded_app['analyst_token']}"}
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": "token_exchange_failed", "retry": "src_oauth_retry"},
        headers=hdr,
    )
    assert r.status_code == 200
    assert "/api/mcp/sources/src_oauth_retry/oauth/authorize" in r.text
    assert "Try again" in r.text

    r2 = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": "token_exchange_failed", "retry": "not-my-source"},
        headers=hdr,
    )
    assert r2.status_code == 200
    assert "not-my-source" not in r2.text


def test_me_connections_connected_banner_shows_for_source_without_tools(seeded_app):
    """The banner check keys off the caller's stored token row, not the
    page's tool-derived source list — a freshly registered source has no
    tools yet, and the admin's post-connect banner must still show (Devin
    Review on #1130)."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_fresh",
        name="src_oauth_fresh",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_fresh", "analyst1", "atok")
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "src_oauth_fresh"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "Connected <strong>src_oauth_fresh</strong>" in r.text


def test_me_connections_lapsed_connection_still_offers_disconnect(seeded_app):
    """An expired token with no refresh path is not usable (no green pill),
    but the stored row must still be removable from the page (Devin Review
    on #1130)."""
    from datetime import datetime, timedelta, timezone

    from src.repositories import mcp_user_oauth_tokens_repo

    _seed_oauth_source(source_id="src_oauth_lapsed")
    mcp_user_oauth_tokens_repo().upsert(
        "src_oauth_lapsed",
        "analyst1",
        "atok",
        refresh_token=None,
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    section = r.text.split('id="source-src_oauth_lapsed"')[1]
    section = section.split('class="conn-card"')[0]  # just this card
    # The pill states the state only (TCRD-208) — it sits beside "Connected"
    # and "Not connected", and the remedy lives in the detail page.
    assert "Expired" in section
    assert 'data-action="oauth-disconnect"' in section
    assert ">Reconnect<" in section


def test_me_connections_page_shows_connect_error_banner(seeded_app):
    from app.api.mcp_oauth_connect import CONNECT_ERROR_MESSAGES

    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": "token_exchange_failed"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert CONNECT_ERROR_MESSAGES["token_exchange_failed"] in r.text


def test_me_connections_error_banner_never_echoes_crafted_text(seeded_app):
    """?connect_error= is reachable via a hand-crafted link, so anything not
    in the fixed code→message map must render the generic fallback — never
    the link's own words inside an Agnes-branded banner (Devin Review on
    #1130)."""
    from app.api.mcp_oauth_connect import CONNECT_ERROR_FALLBACK

    crafted = "your account was suspended, call +1-555-0100"
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": crafted},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert crafted not in r.text
    assert "suspended" not in r.text
    assert CONNECT_ERROR_FALLBACK in r.text


def test_me_connections_connected_banner_only_names_visible_sources(seeded_app):
    """Same crafted-link channel as connect_error: ?connected= must only
    ever name a source the caller can actually see."""
    r = seeded_app["client"].get(
        "/me/connections",
        params={"connected": "definitely-not-a-source"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )
    assert r.status_code == 200
    assert "definitely-not-a-source" not in r.text


def test_revoked_analyst_is_told_they_lost_access_not_that_tools_are_missing(seeded_app):
    """ "This source has no tools yet" and "no tools are granted to me" are
    different facts. They coincide for an admin, which hid the bug: a revoked
    analyst — whose card this page deliberately keeps visible — was told the
    source was unfinished and sent to chase an admin about tools that were
    already enabled (Devin Review on #1167).
    """
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository
    from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_lost",
        name="src_oauth_lost",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    tools = ToolRegistryRepository(conn)
    tools.upsert(
        tool_id="src_oauth_lost.lookup",
        source_id="src_oauth_lost",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="enabled, but granted to a group the analyst is not in",
    )
    # Granted to a group with no members — the source HAS enabled tools, the
    # analyst just has none of them.
    grp = UserGroupsRepository(conn).create(name="grant-src_oauth_lost", description=None)
    tools.add_grant("src_oauth_lost.lookup", grp["id"])
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_lost", "analyst1", "atok")

    r = seeded_app["client"].get(
        "/me/connections",
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )

    assert r.status_code == 200
    card = r.text[r.text.index('id="source-src_oauth_lost"') :]
    card = card[: card.find('id="source-', 1)] if 'id="source-' in card[1:] else card
    assert "You no longer have access to this source's tools" in card
    assert "No tools are enabled for this source yet" not in card
    # ...and no control that could only 403 (PUT my-secret is grant-gated).
    assert 'data-action="save"' not in card
    # Every control whose endpoint calls _require_source_grant unconditionally
    # is gone; the own-credential carve-out (Disconnect) is what keeps the card
    # useful. This same dead-end was reported three times on this PR — once per
    # control — so assert the whole set, not the one last pointed at.
    assert 'data-action="oauth-connect"' not in card
    assert 'data-action="test"' not in card
    assert 'data-action="oauth-disconnect"' in card


def test_try_again_link_is_withheld_when_the_retry_would_fail_again(seeded_app):
    """`retry` was validated against the listed cards on the reasoning that
    listed implies connectable. This PR broke that premise by listing
    stored-but-ungranted sources, so a not_granted failure offered a "Try
    again" that could only fail again."""
    from src.db import get_system_db
    from src.repositories import mcp_user_oauth_tokens_repo
    from src.repositories.mcp_sources import MCPSourceRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id="src_oauth_retry",
        name="src_oauth_retry",
        transport="http",
        url="https://upstream.example/mcp",
        auth_method="oauth",
        scope="per_user",
    )
    conn.close()
    mcp_user_oauth_tokens_repo().upsert("src_oauth_retry", "analyst1", "atok")

    r = seeded_app["client"].get(
        "/me/connections",
        params={"connect_error": "not_granted", "retry": "src_oauth_retry"},
        headers={"Authorization": f"Bearer {seeded_app['analyst_token']}"},
    )

    assert r.status_code == 200
    assert "Connect failed" in r.text
    assert "/api/mcp/sources/src_oauth_retry/oauth/authorize" not in r.text
