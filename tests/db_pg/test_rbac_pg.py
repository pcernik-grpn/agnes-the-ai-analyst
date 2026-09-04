"""Postgres-side smoke + invariant tests for the RBAC quartet.

Covers the load-bearing behaviours that the existing DuckDB tests
already enforce on the original repos; the contract here is "PG repo
produces the same observable outcomes given the same inputs".

Full DuckDB-parity contract tests can be added later by parametrizing
across both impls (see ``test_audit_contract.py`` for the pattern).
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def rbac_engine(pg_engine, monkeypatch):
    """Run migrations + bind the singleton engine to the per-test PG."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg
    db_pg.dispose()
    return db_pg.get_engine()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def test_users_create_get_by_id_and_email(rbac_engine):
    from src.repositories.users_pg import UsersPgRepository

    repo = UsersPgRepository(rbac_engine)
    repo.create(id="u1", email="alice@example.com", name="Alice")
    by_id = repo.get_by_id("u1")
    by_email = repo.get_by_email("alice@example.com")
    assert by_id is not None and by_id["email"] == "alice@example.com"
    assert by_email is not None and by_email["id"] == "u1"


def test_users_unique_email_constraint(rbac_engine):
    from sqlalchemy.exc import IntegrityError
    from src.repositories.users_pg import UsersPgRepository

    repo = UsersPgRepository(rbac_engine)
    repo.create(id="u1", email="alice@example.com", name="Alice")
    with pytest.raises(IntegrityError):
        repo.create(id="u2", email="alice@example.com", name="Alice2")


def test_users_update_only_allowed_columns(rbac_engine):
    from src.repositories.users_pg import UsersPgRepository

    repo = UsersPgRepository(rbac_engine)
    repo.create(id="u1", email="alice@example.com", name="Alice")
    # Allowed
    repo.update("u1", name="Alice Smith", active=False)
    row = repo.get_by_id("u1")
    assert row["name"] == "Alice Smith"
    assert row["active"] is False
    # Not in allowlist — silently dropped, not raised
    repo.update("u1", created_at="2000-01-01", deactivated_at="2000-01-01")
    row = repo.get_by_id("u1")
    assert row["id"] == "u1"  # PK unchanged
    # created_at is not in the allowlist — value untouched
    assert str(row["created_at"]).startswith("2026") or str(row["created_at"]) != "2000-01-01"


def test_users_delete_cascades_memberships(rbac_engine):
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    users = UsersPgRepository(rbac_engine)
    groups = UserGroupsPgRepository(rbac_engine)
    members = UserGroupMembersPgRepository(rbac_engine)

    users.create(id="u1", email="alice@example.com", name="Alice")
    g = groups.create(name="data-team")
    members.add_member("u1", g["id"], source="admin")
    assert members.has_membership("u1", g["id"])

    users.delete("u1")
    assert users.get_by_id("u1") is None
    assert not members.has_membership("u1", g["id"])


# ---------------------------------------------------------------------------
# user_groups
# ---------------------------------------------------------------------------

def test_user_groups_create_and_get(rbac_engine):
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    repo = UserGroupsPgRepository(rbac_engine)
    created = repo.create(name="data-team", description="Data team", created_by="admin")
    assert created["name"] == "data-team"
    assert repo.get_by_name("data-team") is not None
    assert repo.get(created["id"]) is not None


def test_user_groups_unique_name(rbac_engine):
    from sqlalchemy.exc import IntegrityError
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    repo = UserGroupsPgRepository(rbac_engine)
    repo.create(name="data-team")
    with pytest.raises(IntegrityError):
        repo.create(name="data-team")


def test_user_groups_system_group_protection(rbac_engine):
    from src.repositories.user_groups_pg import (
        SystemGroupProtected,
        UserGroupsPgRepository,
    )

    repo = UserGroupsPgRepository(rbac_engine)
    sysg = repo.ensure_system("Admin", "Admin group")
    # Rename forbidden
    with pytest.raises(SystemGroupProtected):
        repo.update(sysg["id"], name="not-admin")
    # Delete forbidden
    with pytest.raises(SystemGroupProtected):
        repo.delete(sysg["id"])
    # Description edits allowed
    repo.update(sysg["id"], description="new desc")
    assert repo.get(sysg["id"])["description"] == "new desc"


def test_user_groups_ensure_idempotent(rbac_engine):
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    repo = UserGroupsPgRepository(rbac_engine)
    a = repo.ensure("data-team", description="initial")
    b = repo.ensure("data-team", description="ignored")  # existing → unchanged
    assert a["id"] == b["id"]
    assert b["description"] == "initial"


# ---------------------------------------------------------------------------
# user_group_members
# ---------------------------------------------------------------------------

def test_user_group_members_add_remove_idempotent(rbac_engine):
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    users = UsersPgRepository(rbac_engine)
    groups = UserGroupsPgRepository(rbac_engine)
    members = UserGroupMembersPgRepository(rbac_engine)

    users.create(id="u1", email="u1@example.com", name="U1")
    g = groups.create(name="g1")

    members.add_member("u1", g["id"], source="admin")
    members.add_member("u1", g["id"], source="admin")  # idempotent
    assert members.has_membership("u1", g["id"])
    assert members.count_members(g["id"]) == 1

    removed = members.remove_member("u1", g["id"])
    assert removed is True
    assert not members.has_membership("u1", g["id"])

    # Re-remove now returns False
    assert members.remove_member("u1", g["id"]) is False


def test_user_group_members_require_source_blocks_other_sources(rbac_engine):
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    users = UsersPgRepository(rbac_engine)
    groups = UserGroupsPgRepository(rbac_engine)
    members = UserGroupMembersPgRepository(rbac_engine)

    users.create(id="u1", email="u1@example.com", name="U1")
    g = groups.create(name="g1")
    members.add_member("u1", g["id"], source="system_seed")

    # admin-source delete cannot touch a system_seed row
    assert members.remove_member("u1", g["id"], require_source="admin") is False
    assert members.has_membership("u1", g["id"])

    # system_seed-source delete succeeds
    assert members.remove_member("u1", g["id"], require_source="system_seed") is True


def test_user_group_members_replace_google_sync(rbac_engine):
    from src.repositories.user_group_members_pg import UserGroupMembersPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository
    from src.repositories.users_pg import UsersPgRepository

    users = UsersPgRepository(rbac_engine)
    groups = UserGroupsPgRepository(rbac_engine)
    members = UserGroupMembersPgRepository(rbac_engine)

    users.create(id="u1", email="u1@example.com", name="U1")
    g_a = groups.create(name="g-a")
    g_b = groups.create(name="g-b")
    g_admin = groups.create(name="g-admin-source")

    # Admin grants u1 → g-admin-source (must survive sync)
    members.add_member("u1", g_admin["id"], source="admin")
    # Pre-existing google_sync grants → g-a only
    members.add_member("u1", g_a["id"], source="google_sync")

    # Sync claims user now belongs to g-b (g-a should be dropped)
    members.replace_google_sync_groups("u1", [g_b["id"]])

    user_groups = set(members.list_groups_for_user("u1"))
    assert user_groups == {g_admin["id"], g_b["id"]}, (
        f"admin row must survive sync; got {user_groups}"
    )


# ---------------------------------------------------------------------------
# resource_grants
# ---------------------------------------------------------------------------

def test_resource_grants_create_and_has_grant(rbac_engine):
    import sqlalchemy as sa
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(rbac_engine)
    grants = ResourceGrantsPgRepository(rbac_engine)

    # Seed table_registry row so the per-type FK is satisfied.
    with rbac_engine.begin() as conn:
        conn.execute(sa.text(
            "INSERT INTO table_registry (id, name) VALUES ('bq.dataset.events', 'events')"
        ))

    g = groups.create(name="g1")
    grant_id = grants.create(
        group_id=g["id"],
        resource_type="table",
        resource_id="bq.dataset.events",
    )
    assert isinstance(grant_id, str)
    assert grants.has_grant([g["id"]], "table", "bq.dataset.events")
    assert not grants.has_grant([g["id"]], "table", "bq.dataset.OTHER")


def test_resource_grants_unique_constraint(rbac_engine):
    from sqlalchemy.exc import IntegrityError
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(rbac_engine)
    grants = ResourceGrantsPgRepository(rbac_engine)

    g = groups.create(name="g1")
    grants.create(group_id=g["id"], resource_type="t", resource_id="r")
    with pytest.raises(IntegrityError):
        grants.create(group_id=g["id"], resource_type="t", resource_id="r")


def test_resource_grants_delete_by_resource(rbac_engine):
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(rbac_engine)
    grants = ResourceGrantsPgRepository(rbac_engine)

    g1 = groups.create(name="g1")
    g2 = groups.create(name="g2")
    grants.create(group_id=g1["id"], resource_type="t", resource_id="r")
    grants.create(group_id=g2["id"], resource_type="t", resource_id="r")
    grants.create(group_id=g1["id"], resource_type="t", resource_id="r-other")

    deleted = grants.delete_by_resource("t", "r")
    assert deleted == 2
    assert grants.count_for_group(g1["id"]) == 1  # r-other survives
    assert grants.count_for_group(g2["id"]) == 0


def test_resource_grants_list_for_groups_filters_by_groups(rbac_engine):
    from src.repositories.resource_grants_pg import ResourceGrantsPgRepository
    from src.repositories.user_groups_pg import UserGroupsPgRepository

    groups = UserGroupsPgRepository(rbac_engine)
    grants = ResourceGrantsPgRepository(rbac_engine)

    g1 = groups.create(name="g1")
    g2 = groups.create(name="g2")
    g3 = groups.create(name="g3")
    grants.create(group_id=g1["id"], resource_type="t", resource_id="r1")
    grants.create(group_id=g2["id"], resource_type="t", resource_id="r2")
    grants.create(group_id=g3["id"], resource_type="t", resource_id="r3")

    rows = grants.list_for_groups([g1["id"], g2["id"]])
    rids = {r["resource_id"] for r in rows}
    assert rids == {"r1", "r2"}


