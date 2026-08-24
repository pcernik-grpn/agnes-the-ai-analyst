"""FIX 2: mcp_per_table endpoint must be principal-aware (SessionPrincipal).

Tests:
- co-session token on a single-participant table → 403 (not 500, not allow)
- co-session token on a shared table → 200
"""
from __future__ import annotations

import pytest

from src.db import get_system_db


def _seed_mcp_co_env(conn, *, shared_table: bool):
    """Seed two users, table_registry rows, data_package grants, a co-session.

    If shared_table=True, both participants have access to 'shared_tbl' via
    data packages. If shared_table=False, only owner has 'solo_tbl'.

    Returns (co_session_id, table_id).
    """
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.table_registry import TableRegistryRepository
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    UserRepository(conn).create(id="mc1", email="ma@mcp.com", name="A")
    UserRepository(conn).create(id="mc2", email="mb@mcp.com", name="B")

    reg = TableRegistryRepository(conn)
    if shared_table:
        table_id = "shared_tbl"
        reg.register(id=table_id, name=table_id, source_type="keboola")
    else:
        table_id = "solo_tbl"
        reg.register(id=table_id, name=table_id, source_type="keboola")

    groups = UserGroupsRepository(conn)
    grants = ResourceGrantsRepository(conn)
    members = UserGroupMembersRepository(conn)

    # Always grant to owner
    ga = groups.create(name=f"grp-a-{table_id}", description="", created_by="test")
    members.add_member("mc1", ga["id"], source="admin", added_by="test")
    grants.create(
        group_id=ga["id"], resource_type="table",
        resource_id=table_id, assigned_by="test", requirement="required",
    )

    if shared_table:
        gb = groups.create(name=f"grp-b-{table_id}", description="", created_by="test")
        members.add_member("mc2", gb["id"], source="admin", added_by="test")
        grants.create(
            group_id=gb["id"], resource_type="table",
            resource_id=table_id, assigned_by="test", requirement="required",
        )

    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="ma@mcp.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="ma@mcp.com", owner_user_id="mc1",
        invitee_email="mb@mcp.com", invitee_user_id="mc2",
    )
    return s1.id, table_id


@pytest.fixture
def mcp_solo_app(e2e_env, shared_app):
    """Co-session where only the owner has the table grant → 403."""
    conn = get_system_db()
    co_id, table_id = _seed_mcp_co_env(conn, shared_table=False)
    conn.close()
    from fastapi.testclient import TestClient
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt
    token = mint_co_session_jwt(co_id)
    yield client, table_id, token


@pytest.fixture
def mcp_shared_app(e2e_env, shared_app):
    """Co-session where both participants have the table grant → 200."""
    conn = get_system_db()
    co_id, table_id = _seed_mcp_co_env(conn, shared_table=True)
    conn.close()
    from fastapi.testclient import TestClient
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt
    token = mint_co_session_jwt(co_id)
    yield client, table_id, token


def test_mcp_per_table_co_token_solo_table_403(mcp_solo_app):
    """co-token on a single-participant table → 403 (not 500)."""
    client, table_id, token = mcp_solo_app
    r = client.post(
        f"/api/mcp/query-table/{table_id}",
        json={"filter": {}, "limit": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 500, f"Got 500 (crash): {r.text}"
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


def test_mcp_per_table_co_token_shared_table_not_500(mcp_shared_app):
    """co-token on a shared table → not 500 (may be 200 or 409 if view absent)."""
    client, table_id, token = mcp_shared_app
    r = client.post(
        f"/api/mcp/query-table/{table_id}",
        json={"filter": {}, "limit": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 500, f"Got 500 (crash): {r.text}"
    # 409 = view not in analytics.duckdb (sync not run in test); 200 = view present
    assert r.status_code in (200, 409), f"Expected 200 or 409, got {r.status_code}: {r.text}"
