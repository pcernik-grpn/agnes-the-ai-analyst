"""C2.1 — the D-C2 staged write-gate on `agent_scope` writes.

`docs/superpowers/plans/2026-08-26-one-agent-model.md` -> "Decision point
D-C2": a non-admin writer may only grant a DATA-authority item
(`table`/`data_package`/`collection`/`connection`) they currently hold
themselves; an admin writer is unconditioned. `plugin`/`memory_domain`/
`slack_channel` keep today's rules regardless of writer.

Unit tests drive `src.agent_scope_intersection.writer_can_access_item` /
`first_inaccessible_data_item` directly (all four DATA_AUTHORITY_ITEM_TYPES
+ the admin bypass); the HTTP tests drive both write paths the plan names —
`PUT /api/v1/agents/{id}/scope` (`app/api/agents_admin.py`) and the
builder-shape `POST`/`PUT /api/v1/agents` (`_sync_builder_scope`,
`app/api/agents_builder_shared.py`) — end to end.

Failing-first: before this task, `first_inaccessible_data_item` did not
exist and neither write path checked reachability at all — a non-admin
writer could declare ANY table/data_package/collection/connection id, valid
or not, and it landed in `agent_scope` with 200/201. Reverting
`app/api/agents_admin.py`'s scope-PUT gate call and
`app/api/agents_builder_shared.py::_sync_builder_scope`'s gate call
reproduces that: `test_scope_put_rejects_an_inaccessible_table` and
`test_builder_create_rejects_an_inaccessible_data_package` below both flip
from 403 to 200/201.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant(conn, user_id: str, resource_type: str, resource_id: str, *, group_name: str) -> None:
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name(group_name) or groups.create(name=group_name, description="test", created_by="test")
    members = UserGroupMembersRepository(conn)
    if not members.has_membership(user_id, grp["id"]):
        members.add_member(user_id, grp["id"], source="admin", added_by="test")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], resource_type, resource_id):
        grants.create(
            group_id=grp["id"],
            resource_type=resource_type,
            resource_id=resource_id,
            assigned_by="test",
            requirement="required",
        )


@pytest.fixture
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="admin1", email="admin@test.com", name="Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    return {
        "client": TestClient(shared_app),
        "owner": {"id": "owner1", "token": create_access_token("owner1", "owner@test.com")},
        "admin": {"id": "admin1", "token": create_access_token("admin1", "admin@test.com")},
    }


def _create_agent(client, token, slug) -> str:
    r = client.post("/api/v1/agents", json={"name": slug, "slug": slug}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Unit level — writer_can_access_item / first_inaccessible_data_item
# ---------------------------------------------------------------------------


def test_writer_can_access_item_table(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import writer_can_access_item
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@test.com", name="U1")
    assert writer_can_access_item("u1", "table", "t1") is False
    _grant(conn, "u1", "table", "t1", group_name="wg-table")
    conn.close()
    assert writer_can_access_item("u1", "table", "t1") is True


def test_writer_can_access_item_data_package(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import writer_can_access_item
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@test.com", name="U1")
    assert writer_can_access_item("u1", "data_package", "pkg1") is False
    _grant(conn, "u1", "data_package", "pkg1", group_name="wg-pkg")
    conn.close()
    assert writer_can_access_item("u1", "data_package", "pkg1") is True


def test_writer_can_access_item_collection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import writer_can_access_item
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@test.com", name="U1")
    assert writer_can_access_item("u1", "collection", "col1") is False
    _grant(conn, "u1", "collection", "col1", group_name="wg-col")
    conn.close()
    assert writer_can_access_item("u1", "collection", "col1") is True


def test_writer_can_access_item_collection_owned_without_a_grant(tmp_path, monkeypatch):
    """Ownership alone grants collection access (mirrors
    `app.auth.access.accessible_collection_ids`) — a creator with no group
    grant on their own upload must still pass the write-gate."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import writer_can_access_item
    from src.db import get_system_db
    from src.repositories.file_corpora import FileCorporaRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@test.com", name="U1")
    col_id = FileCorporaRepository(conn).create(name="mine", slug="mine", description=None, created_by="u1")
    conn.close()
    assert writer_can_access_item("u1", "collection", col_id) is True


def test_writer_can_access_item_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import writer_can_access_item
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository
    from src.repositories.tool_registry import PASSTHROUGH, ToolRegistryRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@test.com", name="U1")
    MCPSourceRepository(conn).upsert(id="src1", name="src1", transport="stdio", command="/bin/true", args=[])
    ToolRegistryRepository(conn).upsert(
        tool_id="src1.lookup",
        source_id="src1",
        original_name="lookup",
        exposed_name="lookup",
        mode=PASSTHROUGH,
        description="test",
    )
    assert writer_can_access_item("u1", "connection", "src1") is False
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    grp = UserGroupsRepository(conn).create(name="wg-conn", description="test")
    ToolRegistryRepository(conn).add_grant("src1.lookup", grp["id"])
    UserGroupMembersRepository(conn).add_member("u1", grp["id"], source="admin", added_by="test")
    conn.close()
    assert writer_can_access_item("u1", "connection", "src1") is True


def test_writer_can_access_item_non_data_types_always_true():
    """plugin/memory_domain/slack_channel are never checked — always True
    regardless of any grant."""
    from src.agent_scope_intersection import writer_can_access_item

    assert writer_can_access_item("nobody", "plugin", "p1") is True
    assert writer_can_access_item("nobody", "memory_domain", "d1") is True
    assert writer_can_access_item("nobody", "slack_channel", "C1") is True


def test_writer_can_access_item_fails_closed_for_unhandled_data_authority_type(monkeypatch):
    """Defense in depth: if ``DATA_AUTHORITY_ITEM_TYPES`` ever grows a value
    with no matching branch inside ``writer_can_access_item``, the write-gate
    must deny it rather than silently fall through to the non-data-type
    pass-through — that catch-all is for types genuinely OUTSIDE the DATA
    axis (plugin/memory_domain/slack_channel), never for one inside it that
    this function simply hasn't caught up to yet."""
    import src.agent_scope_intersection as asi

    monkeypatch.setattr(asi, "DATA_AUTHORITY_ITEM_TYPES", asi.DATA_AUTHORITY_ITEM_TYPES | {"widget"})
    assert asi.writer_can_access_item("nobody", "widget", "w1") is False


def test_first_inaccessible_data_item_admin_bypasses_entirely(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import first_inaccessible_data_item
    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="admin1", email="a@test.com", name="A")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()[0]
    UserGroupMembersRepository(conn).add_member("admin1", admin_gid, source="system_seed")
    conn.close()

    # No grant of any kind — an admin is unconditioned regardless.
    assert first_inaccessible_data_item("admin1", [("table", "t-nobody-has")]) is None


def test_first_inaccessible_data_item_returns_the_offending_pair(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.agent_scope_intersection import first_inaccessible_data_item
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@test.com", name="U1")
    conn.close()

    assert first_inaccessible_data_item("u1", [("plugin", "p1"), ("table", "t-secret")]) == ("table", "t-secret")


# ---------------------------------------------------------------------------
# HTTP level — PUT /api/v1/agents/{id}/scope (governance surface)
# ---------------------------------------------------------------------------


def test_scope_put_admin_grants_a_table_it_cannot_itself_access(env):
    """Admin unconditioned — D-C2's 'admin-granted = unconditioned' half.
    Uses the admin's OWN agent (mutations on a foreign agent stay 403,
    unaffected by C2.1 — see `_load_agent`)."""
    agent_id = _create_agent(env["client"], env["admin"]["token"], "admin-agent")
    r = env["client"].put(
        f"/api/v1/agents/{agent_id}/scope",
        json={"items": [{"item_type": "table", "item_id": "t-nobody-granted"}]},
        headers=_auth(env["admin"]["token"]),
    )
    assert r.status_code == 200, r.text


def test_scope_put_non_admin_grants_an_accessible_table(env):
    from src.db import get_system_db

    conn = get_system_db()
    _grant(conn, "owner1", "table", "t1", group_name="wg-scope-put-ok")
    conn.close()

    agent_id = _create_agent(env["client"], env["owner"]["token"], "owner-agent-ok")
    r = env["client"].put(
        f"/api/v1/agents/{agent_id}/scope",
        json={"items": [{"item_type": "table", "item_id": "t1"}]},
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 200, r.text


def test_scope_put_rejects_an_inaccessible_table(env):
    agent_id = _create_agent(env["client"], env["owner"]["token"], "owner-agent-bad")
    r = env["client"].put(
        f"/api/v1/agents/{agent_id}/scope",
        json={"items": [{"item_type": "table", "item_id": "t-secret"}]},
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "scope_item_not_accessible"


def test_scope_put_plugin_memory_domain_slack_channel_unaffected(env):
    """No grant exists for any of these three ids, yet all three succeed —
    they are not DATA_AUTHORITY_ITEM_TYPES."""
    agent_id = _create_agent(env["client"], env["owner"]["token"], "owner-agent-nondata")
    r = env["client"].put(
        f"/api/v1/agents/{agent_id}/scope",
        json={
            "items": [
                {"item_type": "plugin", "item_id": "p1"},
                {"item_type": "memory_domain", "item_id": "d1"},
                {"item_type": "slack_channel", "item_id": "C1"},
            ]
        },
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# HTTP level — builder-shape create/update (`_sync_builder_scope`)
# ---------------------------------------------------------------------------


def test_builder_create_rejects_an_inaccessible_data_package(env):
    from src.repositories import data_packages_repo

    pkg_id = data_packages_repo().create(
        name="Secret", slug="write-gate-secret-pkg", description=None, icon=None, color=None, created_by="test"
    )
    r = env["client"].post(
        "/api/v1/agents",
        json={"name": "Bad Builder Agent", "knowledge": [pkg_id]},
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "scope_item_not_accessible"


def test_builder_create_accepts_an_accessible_data_package(env):
    from src.db import get_system_db
    from src.repositories import data_packages_repo

    pkg_id = data_packages_repo().create(
        name="Held", slug="write-gate-held-pkg", description=None, icon=None, color=None, created_by="test"
    )
    conn = get_system_db()
    _grant(conn, "owner1", "data_package", pkg_id, group_name="wg-builder-ok")
    conn.close()

    r = env["client"].post(
        "/api/v1/agents",
        json={"name": "Good Builder Agent", "knowledge": [pkg_id]},
        headers=_auth(env["owner"]["token"]),
    )
    assert r.status_code == 201, r.text
    assert r.json()["knowledge"] == [pkg_id]


def test_builder_create_admin_grants_any_data_package(env):
    from src.repositories import data_packages_repo

    pkg_id = data_packages_repo().create(
        name="Ungranted", slug="write-gate-admin-pkg", description=None, icon=None, color=None, created_by="test"
    )
    r = env["client"].post(
        "/api/v1/agents",
        json={"name": "Admin Builder Agent", "knowledge": [pkg_id]},
        headers=_auth(env["admin"]["token"]),
    )
    assert r.status_code == 201, r.text
