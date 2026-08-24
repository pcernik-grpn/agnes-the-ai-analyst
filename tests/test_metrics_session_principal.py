"""SessionPrincipal (chat co-session) coverage for the metrics RBAC gate (#955).

`GET /api/metrics` / `GET /api/metrics/{id}` resolve the caller's accessible
table set via `get_accessible_tables(user, conn)`, which branches on
`isinstance(user, SessionPrincipal)` to return the co-session *intersection*
grant set rather than admin god-mode. This file exercises that branch
directly (mirrors the existing `test_mcp_per_table_session_principal.py`
pattern) — the dict-user path is already covered by `tests/test_api.py`'s
`TestMetricsRBAC`.

Tests:
- co-session where only the owner has the metric's table grant → metric
  hidden from list, 403 on direct get.
- co-session where both participants have the table grant (intersection
  non-empty) → metric visible, 200 on direct get.
"""

from __future__ import annotations

import pytest

from src.db import get_system_db


def _seed_metrics_co_env(conn, *, shared_table: bool):
    """Seed two users, a table_registry row + grant(s), a metric on that
    table, and a co-session. Returns (co_session_id, metric_id).

    If shared_table=True, both co-session participants have a grant on the
    metric's table (via separate groups) — the intersection is non-empty.
    If shared_table=False, only the owner has the grant — the co-session's
    intersection with the invitee's (empty) grant set is empty.
    """
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.table_registry import TableRegistryRepository
    from src.repositories import metric_repo
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    UserRepository(conn).create(id="msp1", email="msp-a@test.com", name="A")
    UserRepository(conn).create(id="msp2", email="msp-b@test.com", name="B")

    table_id = "shared_tbl" if shared_table else "solo_tbl"
    TableRegistryRepository(conn).register(id=table_id, name=table_id, source_type="keboola")

    metric_id = f"manual/{table_id}_metric"
    metric_repo().create(
        id=metric_id,
        name=f"{table_id}_metric",
        display_name=f"{table_id}_metric",
        category="test",
        sql="SELECT 1",
        table_name=table_id,
        source="manual",
    )

    groups = UserGroupsRepository(conn)
    grants = ResourceGrantsRepository(conn)
    members = UserGroupMembersRepository(conn)

    ga = groups.create(name=f"grp-a-{table_id}", description="", created_by="test")
    members.add_member("msp1", ga["id"], source="admin", added_by="test")
    grants.create(
        group_id=ga["id"],
        resource_type="table",
        resource_id=table_id,
        assigned_by="test",
        requirement="required",
    )

    if shared_table:
        gb = groups.create(name=f"grp-b-{table_id}", description="", created_by="test")
        members.add_member("msp2", gb["id"], source="admin", added_by="test")
        grants.create(
            group_id=gb["id"],
            resource_type="table",
            resource_id=table_id,
            assigned_by="test",
            requirement="required",
        )

    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="msp-a@test.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="msp-a@test.com",
        owner_user_id="msp1",
        invitee_email="msp-b@test.com",
        invitee_user_id="msp2",
    )
    return s1.id, metric_id


@pytest.fixture
def metrics_solo_app(e2e_env, shared_app):
    """Co-session where only the owner has the metric's table grant → hidden/403."""
    conn = get_system_db()
    co_id, metric_id = _seed_metrics_co_env(conn, shared_table=False)
    conn.close()
    from fastapi.testclient import TestClient

    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt

    token = mint_co_session_jwt(co_id)
    yield client, metric_id, token


@pytest.fixture
def metrics_shared_app(e2e_env, shared_app):
    """Co-session where both participants have the metric's table grant → visible/200."""
    conn = get_system_db()
    co_id, metric_id = _seed_metrics_co_env(conn, shared_table=True)
    conn.close()
    from fastapi.testclient import TestClient

    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt

    token = mint_co_session_jwt(co_id)
    yield client, metric_id, token


def test_metrics_list_hidden_when_co_session_lacks_table_grant(metrics_solo_app):
    client, metric_id, token = metrics_solo_app
    r = client.get("/api/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    ids = {m["id"] for m in r.json()["metrics"]}
    assert metric_id not in ids


def test_metrics_list_visible_when_co_session_has_shared_table_grant(metrics_shared_app):
    client, metric_id, token = metrics_shared_app
    r = client.get("/api/metrics", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    ids = {m["id"] for m in r.json()["metrics"]}
    assert metric_id in ids


def test_metric_detail_403_when_co_session_lacks_table_grant(metrics_solo_app):
    client, metric_id, token = metrics_solo_app
    r = client.get(f"/api/metrics/{metric_id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 500, f"Got 500 (crash): {r.text}"
    assert r.status_code == 403, r.text


def test_metric_detail_200_when_co_session_has_shared_table_grant(metrics_shared_app):
    client, metric_id, token = metrics_shared_app
    r = client.get(f"/api/metrics/{metric_id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == metric_id
