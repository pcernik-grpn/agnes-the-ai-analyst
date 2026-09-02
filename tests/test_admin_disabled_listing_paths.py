"""End-to-end coverage for admin-disabling a *system* plugin (v78).

The repo-level filter (``admin_disabled = FALSE``) lives in
``list_granted_for_groups`` / ``list_with_filters`` and is pinned by the
cross-engine contract test in ``tests/db_pg``. This module covers the two
higher-level read paths that compose those repo methods plus the system-flag
fan-out queries, asserting a plugin that was marked ``is_system=TRUE`` and
then admin-disabled vanishes from:

- ``resolve_user_marketplace`` (the synthetic served marketplace + my-stack
  served content), and
- the ``my_stack`` page's "which plugins are system" probe query.

It also re-confirms that disabling clears ``is_system`` end-to-end through
the real ``set_admin_disabled`` repo method (not raw SQL).
"""

from __future__ import annotations

from pathlib import Path


def _setup_conn(tmp_path: Path):
    from src.db import _ensure_schema
    from src.duckdb_conn import _open_duckdb

    conn = _open_duckdb(str(tmp_path / "system.duckdb"))
    _ensure_schema(conn)
    return conn


def _seed_user_with_system_plugin(conn):
    """Seed a user in a group, a registered marketplace with one plugin
    marked is_system=TRUE, an RBAC grant to the group, and an explicit
    subscription (Model B: grant + subscription => served). Returns the
    user dict, the group id, and the (slug, plugin) tuple."""
    from datetime import datetime, timezone

    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    slug, plugin = "mkt-sys", "sys-plug"

    UserRepository(conn).create(id="u1", email="u1@example.com", name="User One")
    group = UserGroupsRepository(conn).create(name="grp-1", created_by="test")
    UserGroupMembersRepository(conn).add_member("u1", group["id"], source="admin")

    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [slug, slug, f"https://example.test/{slug}.git", datetime(2026, 1, 1, tzinfo=timezone.utc)],
    )
    plugins = MarketplacePluginsRepository(conn)
    plugins.replace_for_marketplace(slug, [{"name": plugin, "version": "1.0", "description": "x"}])
    conn.execute(
        "UPDATE marketplace_plugins SET is_system = TRUE WHERE marketplace_id = ? AND name = ?",
        [slug, plugin],
    )
    conn.execute(
        "INSERT INTO resource_grants "
        "(id, group_id, resource_type, resource_id, assigned_at, assigned_by) "
        "VALUES (?, ?, 'marketplace_plugin', ?, ?, 'test')",
        [f"g-{slug}-{plugin}", group["id"], f"{slug}/{plugin}", datetime.now(timezone.utc)],
    )
    UserCuratedSubscriptionsRepository(conn).subscribe("u1", slug, plugin)

    return {"id": "u1", "email": "u1@example.com", "name": "User One"}, group["id"], (slug, plugin)


def _my_stack_system_set(conn) -> set[tuple[str, str]]:
    """Replicate the system-plugin probe SQL behind the my_stack page (now
    ``marketplace_plugins.list_system_keys``, called from app/api/my_stack.py)
    so a regression in the WHERE clause fails here too."""
    rows = conn.execute(
        "SELECT marketplace_id, name FROM marketplace_plugins "
        "WHERE is_system = TRUE AND admin_disabled = FALSE",
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def test_disabled_system_plugin_drops_from_resolver_and_my_stack(tmp_path, monkeypatch):
    conn = _setup_conn(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Route every factory-resolved repo to this one DuckDB connection.
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace_filter import resolve_user_marketplace
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository

    user, _group_id, (slug, plugin) = _seed_user_with_system_plugin(conn)

    # Baseline: the system plugin is served and flagged system in my-stack.
    served = {(p["marketplace_id"], p["original_name"]) for p in resolve_user_marketplace(conn, user)}
    assert (slug, plugin) in served
    assert (slug, plugin) in _my_stack_system_set(conn)

    # Disable it through the real repo method (clears is_system as a side-effect).
    found = MarketplacePluginsRepository(conn).set_admin_disabled(slug, plugin, True)
    assert found is True

    # Synthetic served marketplace / my-stack served content: gone.
    served_after = {(p["marketplace_id"], p["original_name"]) for p in resolve_user_marketplace(conn, user)}
    assert (slug, plugin) not in served_after

    # my_stack system-set probe: gone (filtered by admin_disabled, and is_system cleared).
    assert (slug, plugin) not in _my_stack_system_set(conn)

    # is_system was cleared by disabling.
    row = MarketplacePluginsRepository(conn).get(slug, plugin)
    assert row is not None
    assert bool(row.get("is_system")) is False
    assert bool(row.get("admin_disabled")) is True

    conn.close()


def test_disabled_plugin_is_not_automatic_for_anyone(tmp_path, monkeypatch):
    """A plugin that is BOTH is_system and admin_disabled must reach nobody.

    This invariant used to be enforced inside the two fan-out queries (a new
    user's subscription sweep and a new group's grant sweep), each carrying
    its own ``AND admin_disabled = FALSE``. Those sweeps are gone — the flag
    is resolved at read time now — so the same invariant is asserted where it
    actually lives: the visibility read and the Automatic-tier read, which
    are also the only two places it can be got wrong.
    """
    conn = _setup_conn(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from datetime import datetime, timezone

    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    slug, plugin = "mkt-fan", "fan-plug"
    conn.execute(
        "INSERT INTO marketplace_registry (id, name, url, registered_at) VALUES (?, ?, ?, ?)",
        [slug, slug, f"https://example.test/{slug}.git", datetime(2026, 1, 1, tzinfo=timezone.utc)],
    )
    MarketplacePluginsRepository(conn).replace_for_marketplace(
        slug, [{"name": plugin, "version": "1.0", "description": "x"}]
    )
    # Force both flags TRUE so the `admin_disabled = FALSE` filter is proven to
    # bite on its own, independently of set_admin_disabled clearing is_system.
    conn.execute(
        "UPDATE marketplace_plugins SET is_system = TRUE, admin_disabled = TRUE "
        "WHERE marketplace_id = ? AND name = ?",
        [slug, plugin],
    )

    UserRepository(conn).create(id="u2", email="u2@example.com", name="User Two")
    group = UserGroupsRepository(conn).create(name="grp-fan", created_by="test")

    plugins = MarketplacePluginsRepository(conn)
    # Visibility: not served to a group, and not served to "everyone" either.
    served = {(r["marketplace_id"], r["name"])
              for r in plugins.list_granted_for_groups([group["id"]])}
    assert (slug, plugin) not in served
    assert (slug, plugin) not in set(plugins.list_system_keys())

    # Automatic tier: not required of anyone.
    from src.marketplace_filter import required_plugin_keys
    assert (slug, plugin) not in required_plugin_keys(conn, "u2")

    conn.close()






def test_disabled_plugin_drops_from_rbac_projection(tmp_path, monkeypatch):
    """The /admin/access grant UI projects plugins via
    ``app.resource_types._marketplace_plugin_blocks``. A disabled plugin must
    vanish from that projection too — it used ``list_all()`` without an
    ``admin_disabled`` filter, so the disabled plugin stayed listed as a
    grantable resource on the RBAC page while every served surface hid it.
    """
    conn = _setup_conn(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from app.resource_types import _marketplace_plugin_blocks
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository

    _user, _group_id, (slug, plugin) = _seed_user_with_system_plugin(conn)

    def _projected_ids() -> set[str]:
        return {
            item["resource_id"]
            for block in _marketplace_plugin_blocks()
            for item in block["items"]
        }

    # Baseline: the plugin is a grantable resource on /admin/access.
    assert f"{slug}/{plugin}" in _projected_ids()

    # Disable it through the real repo method → gone from the RBAC projection.
    MarketplacePluginsRepository(conn).set_admin_disabled(slug, plugin, True)
    assert f"{slug}/{plugin}" not in _projected_ids()

    conn.close()


def test_disabled_plugin_drops_from_v2_skills_admin(tmp_path, monkeypatch):
    """The v2 ``/skills`` endpoint's admin branch lists plugins via
    ``list_all()`` (RBAC bypass). Admin-disabled plugins must not surface
    there either — their skills must not be served into Claude's context."""
    conn = _setup_conn(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from app.api.v2_marketplace import _accessible_plugins
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    user, _group_id, (slug, plugin) = _seed_user_with_system_plugin(conn)

    # Make the user an admin (member of the seeded Admin system group) so the
    # admin branch (the unfiltered list_all path we hardened) is exercised.
    admin_group = UserGroupsRepository(conn).get_by_name("Admin")
    assert admin_group is not None
    UserGroupMembersRepository(conn).add_member(user["id"], admin_group["id"], source="admin")

    def _names() -> set[tuple[str, str]]:
        return {(p["marketplace_id"], p["name"]) for p in _accessible_plugins(user)}

    assert (slug, plugin) in _names()

    MarketplacePluginsRepository(conn).set_admin_disabled(slug, plugin, True)
    assert (slug, plugin) not in _names()

    conn.close()


