"""Admin-disabled plugins must be invisible on every user-facing curated surface.

``docs/marketplace.md`` promises that ``marketplace_plugins.admin_disabled``
removes a plugin instance-wide "regardless of grants" — and the served feed,
browse listing, my-stack and v2 ``/skills`` already honor that (pinned by
``tests/test_admin_disabled_listing_paths.py``). This module pins the surfaces
that did NOT: the curated plugin detail endpoint, its inner skill/agent
detail, the served asset/doc/mirrored files, the install (subscribe) action,
and the ``/library`` page — each kept working for any caller who still held a
live ``resource_grants`` row for the disabled plugin.

Every test seeds a non-admin user whose group holds a grant (the exact live
repro), asserts the surface works while enabled, disables through the real
admin endpoint (``POST /api/marketplaces/{id}/plugins/{name}/disable``), and
asserts the surface then treats the plugin as nonexistent. The admin Details
modal path (``GET /api/marketplaces/{id}/plugins``) must keep returning the
row — it is the one surface that may show a disabled plugin (re-enable
control).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)

SLUG = "probe-mp"
PLUGIN = "disabled-probe-plugin"
SKILL = "probe-skill"
AGENT = "probe-agent"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_granted_plugin(seeded_app) -> None:
    """Register a marketplace + one plugin on disk and in the DB, and grant it
    to a group the non-admin ``analyst1`` user belongs to."""
    from src.db import get_system_db
    from src.repositories.marketplace_registry import MarketplaceRegistryRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    data_dir = Path(seeded_app["env"]["data_dir"])
    plugin_root = data_dir / "marketplaces" / SLUG / "plugins" / PLUGIN
    (plugin_root / "skills" / SKILL).mkdir(parents=True, exist_ok=True)
    (plugin_root / "skills" / SKILL / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: probe\n---\nbody\n",
        encoding="utf-8",
    )
    (plugin_root / "agents").mkdir(parents=True, exist_ok=True)
    (plugin_root / "agents" / f"{AGENT}.md").write_text(
        f"---\nname: {AGENT}\ndescription: probe\n---\nbody\n",
        encoding="utf-8",
    )
    (plugin_root / "cover.png").write_bytes(_PNG_1x1)
    (plugin_root / "docs").mkdir(parents=True, exist_ok=True)
    (plugin_root / "docs" / "guide.md").write_text("guide", encoding="utf-8")
    # Mirrored external-asset cache (separate root from the git working tree).
    mirror = data_dir / "marketplace-cache" / SLUG / PLUGIN
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "cover.png").write_bytes(_PNG_1x1)

    conn = get_system_db()
    try:
        MarketplaceRegistryRepository(conn).register(
            id=SLUG,
            name=SLUG,
            url=f"https://example.com/{SLUG}.git",
            curator_name="C",
            curator_email="c@example.com",
        )
        conn.execute(
            "INSERT OR REPLACE INTO marketplace_plugins (marketplace_id, name, description) VALUES (?, ?, ?)",
            [SLUG, PLUGIN, "probe plugin"],
        )
        group = UserGroupsRepository(conn).create(name="probe-group", created_by="test")
        UserGroupMembersRepository(conn).add_member("analyst1", group["id"], source="admin")
        ResourceGrantsRepository(conn).create(
            group_id=group["id"],
            resource_type="marketplace_plugin",
            resource_id=f"{SLUG}/{PLUGIN}",
            assigned_by="test",
        )
    finally:
        conn.close()


def _disable(seeded_app) -> None:
    r = seeded_app["client"].post(
        f"/api/marketplaces/{SLUG}/plugins/{PLUGIN}/disable",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 200, r.text


@pytest.fixture
def granted_plugin(seeded_app):
    _seed_granted_plugin(seeded_app)
    return seeded_app


def test_curated_detail_404_when_disabled(granted_plugin):
    client = granted_plugin["client"]
    headers = _auth(granted_plugin["analyst_token"])
    url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}"

    assert client.get(url, headers=headers).status_code == 200

    _disable(granted_plugin)
    r = client.get(url, headers=headers)
    assert r.status_code == 404, r.text
    # Admins get the same 404 — their surface for disabled plugins is the
    # /admin/marketplaces Details modal, not the user-facing detail page.
    r_admin = client.get(url, headers=_auth(granted_plugin["admin_token"]))
    assert r_admin.status_code == 404, r_admin.text


def test_curated_install_404_when_disabled_and_writes_no_subscription(granted_plugin):
    from src.db import get_system_db
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    client = granted_plugin["client"]
    headers = _auth(granted_plugin["analyst_token"])
    url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}/install"

    _disable(granted_plugin)
    r = client.post(url, headers=headers)
    assert r.status_code == 404, r.text

    conn = get_system_db()
    try:
        subs = UserCuratedSubscriptionsRepository(conn).subscribed_set("analyst1")
    finally:
        conn.close()
    assert (SLUG, PLUGIN) not in subs


def test_curated_inner_skill_and_agent_404_when_disabled(granted_plugin):
    client = granted_plugin["client"]
    headers = _auth(granted_plugin["analyst_token"])
    skill_url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}/skill/{SKILL}"
    agent_url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}/agent/{AGENT}"

    assert client.get(skill_url, headers=headers).status_code == 200
    assert client.get(agent_url, headers=headers).status_code == 200

    _disable(granted_plugin)
    assert client.get(skill_url, headers=headers).status_code == 404
    assert client.get(agent_url, headers=headers).status_code == 404


def test_curated_doc_404s_when_disabled(granted_plugin):
    client = granted_plugin["client"]
    headers = _auth(granted_plugin["analyst_token"])
    doc_url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}/doc/plugins/{PLUGIN}/docs/guide.md"

    assert client.get(doc_url, headers=headers).status_code == 200
    _disable(granted_plugin)
    assert client.get(doc_url, headers=headers).status_code == 404


def test_cover_art_endpoints_stay_open_after_a_disable(granted_plugin):
    """The two image paths are deliberately NOT gated on ``admin_disabled``.

    Both carry login-only auth with no per-plugin RBAC (see their docstrings):
    any authenticated caller can already fetch any plugin's cover art without
    a grant, because it is curator marketing visuals with no PII / source /
    secrets. Gating them on the disable flag would hide nothing an
    unauthorized viewer could not already read, and would put one serialized
    DuckDB round-trip per image back on a render-blocking path (12-20 covers
    per /marketplace grid) that this endpoint was explicitly stripped of DB
    work for. Disabled plugins appear on no listing, so the URL is reachable
    only by a caller who already knows it.

    This test exists so that reasoning is a decision on the record rather than
    an oversight someone "fixes" back into a per-request query.
    """
    client = granted_plugin["client"]
    headers = _auth(granted_plugin["analyst_token"])
    asset_url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}/asset/plugins/{PLUGIN}/cover.png"
    mirrored_url = f"/api/marketplace/curated/{SLUG}/{PLUGIN}/mirrored/cover.png"

    assert client.get(asset_url, headers=headers).status_code == 200
    assert client.get(mirrored_url, headers=headers).status_code == 200

    _disable(granted_plugin)
    assert client.get(asset_url, headers=headers).status_code == 200
    assert client.get(mirrored_url, headers=headers).status_code == 200


def test_library_page_hides_disabled_plugin(granted_plugin):
    client = granted_plugin["client"]
    headers = _auth(granted_plugin["analyst_token"])
    marker = f"/marketplace/curated/{SLUG}/{PLUGIN}"

    r = client.get("/library", headers=headers)
    assert r.status_code == 200
    assert marker in r.text

    _disable(granted_plugin)
    r = client.get("/library", headers=headers)
    assert r.status_code == 200
    assert marker not in r.text


def test_admin_details_modal_listing_still_shows_disabled_plugin(granted_plugin):
    """The one surface that must keep showing a disabled plugin: the admin
    plugin listing behind the /admin/marketplaces Details modal, which carries
    the re-enable control."""
    client = granted_plugin["client"]
    admin = _auth(granted_plugin["admin_token"])

    _disable(granted_plugin)
    r = client.get(f"/api/marketplaces/{SLUG}/plugins", headers=admin)
    assert r.status_code == 200, r.text
    rows = {p["name"]: p for p in r.json()}
    assert PLUGIN in rows
    assert rows[PLUGIN]["admin_disabled"] is True

    # And re-enabling restores the user-facing detail page.
    r = client.post(
        f"/api/marketplaces/{SLUG}/plugins/{PLUGIN}/enable",
        headers=admin,
    )
    assert r.status_code == 200, r.text
    r = client.get(
        f"/api/marketplace/curated/{SLUG}/{PLUGIN}",
        headers=_auth(granted_plugin["analyst_token"]),
    )
    assert r.status_code == 200, r.text
