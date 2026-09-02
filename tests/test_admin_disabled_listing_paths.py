"""End-to-end coverage for admin-disabling a plugin everyone has (v78).

The repo-level filter (``admin_disabled = FALSE``) lives in
``list_granted_for_groups`` / ``list_with_filters`` and is pinned by the
cross-engine contract test in ``tests/db_pg``. This module covers the
higher-level read paths that compose those repo methods, asserting that a
plugin reaching every account at the required tier still vanishes from:

- ``resolve_user_marketplace`` (the synthetic served marketplace + my-stack
  served content), and
- the ``/admin/access`` grantable-resource projection.

Off beats any grant — that is what makes availability a different question
from distribution, and the reason the two live on different pages. "Reaching
every account" was ``marketplace_plugins.is_system`` until migration 0098;
it is now a required grant at ``scope='everyone'``, which on this frozen
DuckDB ladder is a required grant held by the carrier group.
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
    """Seed a user in a group, a registered marketplace with one plugin, a
    required grant reaching EVERY account, and an explicit subscription
    (Model B: grant + subscription => served). Returns the user dict, the
    group id, and the (slug, plugin) tuple."""
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
    # The required grant that reaches everyone, held by the carrier group —
    # and the user joins that group, which every real creation path does.
    UserGroupMembersRepository(conn).add_member(
        "u1", _carrier_group_id(conn), source="system_seed"
    )
    conn.execute(
        "INSERT INTO resource_grants "
        "(id, group_id, resource_type, resource_id, requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'marketplace_plugin', ?, 'required', ?, 'test')",
        [
            f"g-everyone-{slug}-{plugin}",
            _carrier_group_id(conn),
            f"{slug}/{plugin}",
            datetime.now(timezone.utc),
        ],
    )
    UserCuratedSubscriptionsRepository(conn).subscribe("u1", slug, plugin)

    return {"id": "u1", "email": "u1@example.com", "name": "User One"}, group["id"], (slug, plugin)


def _carrier_group_id(conn) -> str:
    """The seeded ``Everyone`` group — where an everyone-scoped grant lives."""
    row = conn.execute("SELECT id FROM user_groups WHERE name = 'Everyone' AND is_system").fetchone()
    assert row, "the seeded Everyone group is missing"
    return row[0]


def _my_stack_locked_set(conn, user_id: str) -> set[tuple[str, str]]:
    """What the my-stack page locks the toggle on.

    Was a probe of ``is_system AND NOT admin_disabled``; the page now asks
    the same question per user through ``required_plugin_keys``, which is
    strictly better — it locks exactly what the uninstall API refuses. The
    ``admin_disabled`` half is not in that read, so it is asserted through
    the served set instead.
    """
    from src.marketplace_filter import required_plugin_keys

    return required_plugin_keys(conn, user_id)


def test_disabled_system_plugin_drops_from_resolver_and_my_stack(tmp_path, monkeypatch):
    conn = _setup_conn(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Route every factory-resolved repo to this one DuckDB connection.
    monkeypatch.setattr("src.repositories.get_system_db", lambda: conn)

    from src.marketplace_filter import resolve_user_marketplace
    from src.repositories.marketplace_plugins import MarketplacePluginsRepository

    user, _group_id, (slug, plugin) = _seed_user_with_system_plugin(conn)

    # Baseline: served, and locked in my-stack because it is required.
    served = {(p["marketplace_id"], p["original_name"]) for p in resolve_user_marketplace(conn, user)}
    assert (slug, plugin) in served
    assert (slug, plugin) in _my_stack_locked_set(conn, "u1")

    found = MarketplacePluginsRepository(conn).set_admin_disabled(slug, plugin, True)
    assert found is True

    # Synthetic served marketplace / my-stack served content: gone.
    served_after = {(p["marketplace_id"], p["original_name"]) for p in resolve_user_marketplace(conn, user)}
    assert (slug, plugin) not in served_after

    # The GRANT is untouched — disabling hides the plugin, it does not
    # un-grant it, which is why re-enabling needs no re-granting. The
    # deliberate reversal of the old contract, where disabling cleared
    # `is_system` and re-enabling did NOT restore it.
    row = MarketplacePluginsRepository(conn).get(slug, plugin)
    assert row is not None
    assert bool(row.get("admin_disabled")) is True
    assert (slug, plugin) in _my_stack_locked_set(conn, "u1"), (
        "the grant survives a disable; it is the served set that must not"
    )

    assert MarketplacePluginsRepository(conn).set_admin_disabled(slug, plugin, False) is True
    served_again = {(p["marketplace_id"], p["original_name"]) for p in resolve_user_marketplace(conn, user)}
    assert (slug, plugin) in served_again, "re-enabling did not restore the grant's reach"

    conn.close()


def test_disabled_plugin_is_not_served_even_to_an_everyone_grantee(tmp_path, monkeypatch):
    """A disabled plugin reaches nobody, whatever its grants say.

    The invariant used to be enforced inside the two fan-out queries (a new
    user's subscription sweep and a new group's grant sweep), each carrying
    its own ``AND admin_disabled = FALSE``. Those sweeps are long gone, so it
    is asserted where it actually lives: the visibility read, which is the
    only place it can be got wrong.

    Note what is NOT asserted: the required-tier read. That answers "what
    must this person carry" from the grants alone and does not consult
    ``admin_disabled`` — correctly, because visibility is the chokepoint and
    a tier is not reach.
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
    conn.execute(
        "UPDATE marketplace_plugins SET admin_disabled = TRUE "
        "WHERE marketplace_id = ? AND name = ?",
        [slug, plugin],
    )

    UserRepository(conn).create(id="u2", email="u2@example.com", name="User Two")
    group = UserGroupsRepository(conn).create(name="grp-fan", created_by="test")
    from src.repositories.user_group_members import UserGroupMembersRepository

    UserGroupMembersRepository(conn).add_member("u2", _carrier_group_id(conn), source="system_seed")
    conn.execute(
        "INSERT INTO resource_grants "
        "(id, group_id, resource_type, resource_id, requirement, assigned_at, assigned_by) "
        "VALUES (?, ?, 'marketplace_plugin', ?, 'required', ?, 'test')",
        [
            f"g-everyone-{slug}-{plugin}",
            _carrier_group_id(conn),
            f"{slug}/{plugin}",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        ],
    )

    plugins = MarketplacePluginsRepository(conn)
    # Not served to the caller's own group, and not served through the
    # everyone-reaching grant either.
    served = {(r["marketplace_id"], r["name"]) for r in plugins.list_granted_for_groups([group["id"]])}
    assert (slug, plugin) not in served
    served_via_carrier = {
        (r["marketplace_id"], r["name"])
        for r in plugins.list_granted_for_groups([_carrier_group_id(conn)])
    }
    assert (slug, plugin) not in served_via_carrier

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


