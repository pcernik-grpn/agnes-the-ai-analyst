"""Tests for src/rbac.py — table access via resource_grants (v19+).

``can_access_table`` and ``get_accessible_tables`` are thin wrappers over
``app.auth.access.can_access`` / ``is_user_admin``. Admin group members see
everything; non-admin users see only tables with a matching
``resource_grants(group, "table", id)`` row via any of their groups.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def setup_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    UserRepository(conn).create(id="user1", email="user@test.com", name="User")

    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")

    # Custom group + grant: user1 ∈ analysts, analysts can see "orders"
    analysts = UserGroupsRepository(conn).create(
        name="analysts",
        description="test group",
        created_by="test",
    )
    UserGroupMembersRepository(conn).add_member(
        "user1",
        analysts["id"],
        source="admin",
        added_by="test",
    )
    conn.execute(
        "INSERT INTO table_registry (id, name) VALUES (?, ?)",
        ["orders", "orders"],
    )
    conn.execute(
        "INSERT INTO table_registry (id, name) VALUES (?, ?)",
        ["salaries", "salaries"],
    )
    # Stack-gated RBAC: wrap 'orders' in an auto data_package and grant the
    # package to the analysts group with required=true so it lands in the
    # user's stack automatically. Per-table grants on resource_grants are
    # no longer consulted for analyst visibility.
    from src.repositories.data_packages import DataPackagesRepository

    pkgs = DataPackagesRepository(conn)
    pkg_id = pkgs.create(
        name="orders-pkg",
        slug="orders-pkg",
        description=None,
        icon=None,
        color=None,
        created_by="test",
    )
    pkgs.add_table(pkg_id, "orders", added_by="test")
    conn.execute(
        """INSERT INTO resource_grants
           (id, group_id, resource_type, resource_id, requirement)
           VALUES (?, ?, 'data_package', ?, 'required')""",
        [str(uuid.uuid4()), analysts["id"], pkg_id],
    )

    conn.close()
    yield


class TestCanAccessTable:
    """Admin shortcut + per-(group, table) grants. No is_public, no
    dataset_permissions, no bucket wildcards — explicit grants only."""

    def test_admin_sees_every_table(self, setup_db):
        from src.db import get_system_db
        from src.rbac import can_access_table

        conn = get_system_db()
        try:
            admin = {"id": "admin1"}
            assert can_access_table(admin, "orders", conn) is True
            assert can_access_table(admin, "salaries", conn) is True
            # Admin can even ask about tables that don't exist.
            assert can_access_table(admin, "nonexistent", conn) is True
        finally:
            conn.close()

    def test_non_admin_sees_only_granted_tables(self, setup_db):
        from src.db import get_system_db
        from src.rbac import can_access_table

        conn = get_system_db()
        try:
            user = {"id": "user1"}
            # user1's group "analysts" was granted resource_id='orders'
            assert can_access_table(user, "orders", conn) is True
            # No grant for 'salaries' → denied
            assert can_access_table(user, "salaries", conn) is False
        finally:
            conn.close()

    def test_no_implicit_public_access(self, setup_db):
        """Pre-v19 a freshly registered table was implicitly public via
        ``is_public DEFAULT true``. v19 removes the column — every
        non-admin access requires an explicit grant. Verify by
        registering a fresh table and asserting denial."""
        from src.db import get_system_db
        from src.rbac import can_access_table

        conn = get_system_db()
        try:
            conn.execute(
                "INSERT INTO table_registry (id, name) VALUES (?, ?)",
                ["fresh_table", "fresh_table"],
            )
            user = {"id": "user1"}
            assert can_access_table(user, "fresh_table", conn) is False
        finally:
            conn.close()

    def test_unknown_user_id_denied(self, setup_db):
        from src.db import get_system_db
        from src.rbac import can_access_table

        conn = get_system_db()
        try:
            assert can_access_table({"id": "ghost"}, "orders", conn) is False
            # No id at all → denied (defensive default).
            assert can_access_table({}, "orders", conn) is False
        finally:
            conn.close()


class TestGetAccessibleTables:
    """Admin returns None (= "all"); non-admin returns the grant list."""

    def test_admin_returns_none(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_tables

        conn = get_system_db()
        try:
            assert get_accessible_tables({"id": "admin1"}, conn) is None
        finally:
            conn.close()

    def test_non_admin_returns_grant_list(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_tables
        from connectors.internal.access import INTERNAL_TABLES

        conn = get_system_db()
        try:
            tables = get_accessible_tables({"id": "user1"}, conn)
            internal_ids = {t.registry_id for t in INTERNAL_TABLES}
            # Package-derived tables ONLY. The agnes_* internal tables used
            # to be appended unconditionally here; since the seeded
            # `agnes-usage` package they arrive through package membership
            # like any other table, so a caller without that grant does not
            # see them (docs/superpowers/plans/2026-08-31-usage-package-
            # per-turn-tokens.md Task 8, **BREAKING**).
            assert "orders" in tables
            assert not (internal_ids & set(tables))
            assert set(tables) == {"orders"}
        finally:
            conn.close()

    def test_user_with_no_grants_returns_empty(self, setup_db):
        from src.db import SYSTEM_EVERYONE_GROUP, get_system_db
        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.users import UserRepository
        from src.rbac import get_accessible_tables
        from connectors.internal.access import INTERNAL_TABLES

        conn = get_system_db()
        try:
            UserRepository(conn).create(id="loner", email="loner@test.com", name="L")
            # Membership in Everyone alone (no grants on it) → nothing at
            # all, not even the agnes_* internal tables: those are members
            # of the seeded `agnes-usage` package now and need its grant
            # like any other table (Task 8 of the usage-package plan,
            # **BREAKING**).
            everyone = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_EVERYONE_GROUP]).fetchone()
            if everyone:
                UserGroupMembersRepository(conn).add_member(
                    "loner",
                    everyone[0],
                    source="system_seed",
                )
            internal_ids = {t.registry_id for t in INTERNAL_TABLES}
            accessible = set(get_accessible_tables({"id": "loner"}, conn))
            assert accessible == set()
            assert not (accessible & internal_ids)
        finally:
            conn.close()


class TestGetAccessibleIds:
    """``get_accessible_ids`` — generic grant-based id set for any
    resource_type. Admin → None; non-admin → frozenset of granted ids;
    SessionPrincipal → intersection membership (never None)."""

    def test_admin_returns_none(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_ids

        conn = get_system_db()
        try:
            assert get_accessible_ids({"id": "admin1"}, "recipe", conn) is None
        finally:
            conn.close()

    def test_non_admin_returns_granted_ids(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_ids

        conn = get_system_db()
        try:
            analysts_id = conn.execute("SELECT id FROM user_groups WHERE name = 'analysts'").fetchone()[0]
            conn.execute(
                """INSERT INTO resource_grants
                   (id, group_id, resource_type, resource_id)
                   VALUES (?, ?, 'recipe', 'recipe-1')""",
                [str(uuid.uuid4()), analysts_id],
            )
            ids = get_accessible_ids({"id": "user1"}, "recipe", conn)
            assert ids == frozenset({"recipe-1"})
        finally:
            conn.close()

    def test_no_grant_returns_empty_frozenset(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_ids

        conn = get_system_db()
        try:
            ids = get_accessible_ids({"id": "user1"}, "recipe", conn)
            assert ids == frozenset()
        finally:
            conn.close()

    def test_unknown_user_id_returns_empty_frozenset(self, setup_db):
        from src.db import get_system_db
        from src.rbac import get_accessible_ids

        conn = get_system_db()
        try:
            assert get_accessible_ids({"id": "ghost"}, "recipe", conn) == frozenset()
            assert get_accessible_ids({}, "recipe", conn) == frozenset()
        finally:
            conn.close()

    def test_session_principal_returns_intersection(self, setup_db):
        from src.rbac import get_accessible_ids
        from app.auth.session_principal import SessionPrincipal

        principal = SessionPrincipal(
            session_id="s1",
            participant_user_ids=["analyst1"],
            participant_emails=["analyst@test.com"],
            intersection={"recipe": frozenset({"recipe-a", "recipe-b"})},
        )
        assert get_accessible_ids(principal, "recipe") == frozenset({"recipe-a", "recipe-b"})
        # No admin god-mode, no personal stack — resource_type absent from
        # the intersection yields empty, never None.
        assert get_accessible_ids(principal, "collection") == frozenset()


class TestHasExplicitGrant:
    """``has_explicit_grant`` reports only what is explicitly granted to a
    group the user belongs to — no Admin god-mode short-circuit, no implicit
    internal-table grants. Used for UI affordances (the cloud-chat nav link)
    that must track rollout state, not effective access. Contrast with
    ``can_access``, which DOES short-circuit for admins."""

    def test_admin_without_grant_is_false_while_can_access_is_true(self, setup_db):
        from src.db import get_system_db
        from app.auth.access import can_access, has_explicit_grant

        conn = get_system_db()
        try:
            # No chat grant exists anywhere in the seeded DB.
            assert has_explicit_grant("admin1", "chat", "chat", conn) is False
            # Same answer through the backend-aware default path (no conn) —
            # this is what the nav-link caller now uses post-fix.
            assert has_explicit_grant("admin1", "chat", "chat") is False
            # ...yet god-mode still grants the admin *effective* access.
            assert can_access("admin1", "chat", "chat", conn) is True
        finally:
            conn.close()

    def test_member_of_granted_group_is_true(self, setup_db):
        from src.db import get_system_db
        from app.auth.access import has_explicit_grant

        conn = get_system_db()
        try:
            analysts_id = conn.execute("SELECT id FROM user_groups WHERE name = 'analysts'").fetchone()[0]
            conn.execute(
                """INSERT INTO resource_grants
                   (id, group_id, resource_type, resource_id)
                   VALUES (?, ?, 'chat', 'chat')""",
                [str(uuid.uuid4()), analysts_id],
            )
            # user1 ∈ analysts → the explicit grant is visible.
            assert has_explicit_grant("user1", "chat", "chat", conn) is True
            # Backend-aware default path (no conn) sees the same grant.
            assert has_explicit_grant("user1", "chat", "chat") is True
            # admin1 ∉ analysts and god-mode is NOT applied here → still False.
            assert has_explicit_grant("admin1", "chat", "chat", conn) is False
        finally:
            conn.close()
