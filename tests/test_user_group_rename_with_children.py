"""``UserGroupsRepository.update()`` — renaming a group that already has
``resource_grants``/``user_group_members`` children.

DuckDB raises a false "Violates foreign key constraint ... still referenced
... in a different table" on a plain ``UPDATE user_groups SET name = ...``
when the group has children — DuckDB implements an UPDATE that touches a
UNIQUE-constrained column (``name``) as an internal delete+reinsert, and the
transient delete trips the FK check even though ``id`` (the actual FK
target) never changes. ``update()`` works around it (see its own comment)
by moving children out, renaming against zero references, then reinserting
them — this is what these tests pin. Found while building the Microsoft
Entra ID group identity-scheme migration (`app.auth.microsoft_group_sync
._ensure_entra_group`), which needs exactly this to rename a legacy-keyed
group that already carries real admin-assigned grants.
"""

from __future__ import annotations

import pytest

from src.db import get_system_db
from src.repositories.resource_grants import ResourceGrantsRepository
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository
from src.repositories.users import UserRepository


@pytest.fixture()
def fresh_system_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db

    monkeypatch.setattr(db, "_system_db_conn", None, raising=False)
    monkeypatch.setattr(db, "_system_db_path", None, raising=False)
    return get_system_db()


class TestRenameWithGrantChildren:
    def test_rename_preserves_id_and_grants(self, fresh_system_db):
        ug = UserGroupsRepository(fresh_system_db)
        grants = ResourceGrantsRepository(fresh_system_db)

        group = ug.create(name="old-name", created_by="admin@example.com")
        grants.ensure_grant(group["id"], "collection", "col-a", assigned_by="admin@example.com")
        grants.ensure_grant(group["id"], "collection", "col-b", assigned_by="admin@example.com")

        ug.update(group["id"], name="new-name")

        renamed = ug.get(group["id"])
        assert renamed["id"] == group["id"]
        assert renamed["name"] == "new-name"

        resource_ids = {g["resource_id"] for g in grants.list_all(group_id=group["id"])}
        assert resource_ids == {"col-a", "col-b"}

    def test_rename_preserves_required_tier_grant(self, fresh_system_db):
        ug = UserGroupsRepository(fresh_system_db)
        grants = ResourceGrantsRepository(fresh_system_db)

        group = ug.create(name="old-name-2", created_by="admin@example.com")
        grants.ensure_grant(group["id"], "data_package", "pkg-1", assigned_by="admin@example.com")
        fresh_system_db.execute(
            "UPDATE resource_grants SET requirement = 'required' WHERE group_id = ?",
            [group["id"]],
        )

        ug.update(group["id"], name="new-name-2")

        rows = grants.list_all(group_id=group["id"])
        assert len(rows) == 1
        assert rows[0]["requirement"] == "required"


class TestRenameWithMembershipChildren:
    def test_rename_preserves_id_and_memberships(self, fresh_system_db):
        UserRepository(fresh_system_db).create(id="u1", email="u1@example.com", name="U1")
        ug = UserGroupsRepository(fresh_system_db)
        members = UserGroupMembersRepository(fresh_system_db)

        group = ug.create(name="old-name-3", created_by="system:microsoft-sync")
        members.add_member("u1", group["id"], source="microsoft_sync", added_by="system:microsoft-sync")

        ug.update(group["id"], name="new-name-3", description="renamed description")

        renamed = ug.get(group["id"])
        assert renamed["id"] == group["id"]
        assert renamed["description"] == "renamed description"

        member_rows = members.list_members_for_group(group["id"])
        assert {m["id"] for m in member_rows} == {"u1"}
        assert member_rows[0]["source"] == "microsoft_sync"


class TestRenameWithBothGrantsAndMemberships:
    def test_rename_preserves_both(self, fresh_system_db):
        UserRepository(fresh_system_db).create(id="u2", email="u2@example.com", name="U2")
        ug = UserGroupsRepository(fresh_system_db)
        grants = ResourceGrantsRepository(fresh_system_db)
        members = UserGroupMembersRepository(fresh_system_db)

        group = ug.create(name="old-name-4", created_by="system:microsoft-sync")
        grants.ensure_grant(group["id"], "collection", "col-c", assigned_by="admin")
        members.add_member("u2", group["id"], source="microsoft_sync", added_by="system:microsoft-sync")

        ug.update(group["id"], name="new-name-4")

        assert ug.get(group["id"])["id"] == group["id"]
        assert {g["resource_id"] for g in grants.list_all(group_id=group["id"])} == {"col-c"}
        assert {m["id"] for m in members.list_members_for_group(group["id"])} == {"u2"}


class TestRenameWithNoChildrenUsesFastPath:
    def test_no_children_still_renames(self, fresh_system_db):
        ug = UserGroupsRepository(fresh_system_db)
        group = ug.create(name="old-name-5", created_by="admin@example.com")

        ug.update(group["id"], name="new-name-5")

        assert ug.get(group["id"])["name"] == "new-name-5"


class TestDescriptionOnlyUpdateNeverTriggersWorkaround:
    def test_description_only_change_keeps_name(self, fresh_system_db):
        ug = UserGroupsRepository(fresh_system_db)
        grants = ResourceGrantsRepository(fresh_system_db)

        group = ug.create(name="stable-name", created_by="admin@example.com")
        grants.ensure_grant(group["id"], "collection", "col-d", assigned_by="admin@example.com")

        ug.update(group["id"], description="just a description edit")

        renamed = ug.get(group["id"])
        assert renamed["name"] == "stable-name"
        assert renamed["description"] == "just a description edit"
        assert {g["resource_id"] for g in grants.list_all(group_id=group["id"])} == {"col-d"}
