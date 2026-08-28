"""HTTP round-trip tests for the agent-sharing approval queue (Track C6).

"A user can build their own agents freely, but SHARING an agent needs ADMIN
APPROVAL." Only ``resource_type == 'agent'`` is gated, and only when the
ACTOR (not the agent's owner) is not an admin — every other combination
stays the pre-existing instant path. PG-only (A3 ratchet): the DuckDB half
of this file asserts that the queue ENHANCEMENT is unavailable there (the
admin ``/api/admin/share-requests*`` surface fails clean with a 501) while
sharing ITSELF never regresses — a non-admin owner's share falls back to
the pre-C6 instant grant rather than a 501.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _pg_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)


def _duckdb_client(tmp_path, monkeypatch, pg_engine):
    from tests.db_pg._parity_sweep_util import build_seeded_client

    return build_seeded_client("duckdb", tmp_path, monkeypatch, pg_engine)


def _make_owner_and_group(owner_id="owner1", owner_email="owner@test.com", group_name="c6-group"):
    """A non-admin owner (a member of the group, so it's within their
    shareable set — see ``library_sharing.shareable_group_ids``) sharing to
    a group that also holds a grantee who is NOT the owner."""
    from app.auth.jwt import create_access_token
    from src.repositories import user_group_members_repo, user_groups_repo, users_repo

    users_repo().create(id=owner_id, email=owner_email, name="Owner")
    users_repo().create(id="grantee1", email="grantee@test.com", name="Grantee")
    group = user_groups_repo().create(name=group_name, created_by="admin1")
    user_group_members_repo().add_member(owner_id, group["id"], source="admin", added_by="admin1")
    user_group_members_repo().add_member("grantee1", group["id"], source="admin", added_by="admin1")
    return {
        "owner_token": create_access_token(owner_id, owner_email),
        "grantee_token": create_access_token("grantee1", "grantee@test.com"),
        "group_id": group["id"],
    }


def _create_agent(client, token, name="My Agent") -> str:
    r = client.post("/api/v1/agents", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Non-admin owner shares an agent -> queued, not granted
# ---------------------------------------------------------------------------


def test_non_admin_owner_share_queues_a_pending_request_no_grant_yet(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"])

    r = client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["group_ids"] == []
    assert body["pending_group_ids"] == [env["group_id"]]
    assert body["visibility"] == "private"

    # GET reflects the same pending state.
    got = client.get(f"/api/sharing/agent/{agent_id}", headers=_auth(env["owner_token"]))
    assert got.status_code == 200
    assert got.json()["pending_group_ids"] == [env["group_id"]]
    assert got.json()["group_ids"] == []

    # C2.3 runtime: the grantee is NOT granted yet.
    runtime = client.get(f"/api/v1/agents/{agent_id}", headers=_auth(env["grantee_token"]))
    assert runtime.status_code == 404

    # The admin queue shows it.
    listed = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token))
    assert listed.status_code == 200
    rows = listed.json()["data"]
    assert len(rows) == 1
    assert rows[0]["resource_type"] == "agent"
    assert rows[0]["resource_id"] == agent_id
    assert rows[0]["requested_group_id"] == env["group_id"]
    assert rows[0]["status"] == "pending"


def test_double_submitted_put_does_not_duplicate_the_pending_request(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"])

    for _ in range(2):
        r = client.put(
            f"/api/sharing/agent/{agent_id}",
            json={"group_ids": [env["group_id"]]},
            headers=_auth(env["owner_token"]),
        )
        assert r.status_code == 202

    listed = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token))
    assert listed.json()["total"] == 1


# ---------------------------------------------------------------------------
# Admin approves -> grant lands, C2.3 runtime honors it
# ---------------------------------------------------------------------------


def test_admin_approve_grants_access_and_marks_decided(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"])
    client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(env["owner_token"]),
    )
    request_id = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token)).json()["data"][0][
        "id"
    ]

    r = client.patch(
        f"/api/admin/share-requests/{request_id}",
        json={"decision": "approve"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "admin1"
    assert body["decided_at"] is not None

    # The grant now exists — GET reflects it, and the pending queue clears.
    got = client.get(f"/api/sharing/agent/{agent_id}", headers=_auth(env["owner_token"]))
    assert got.json()["group_ids"] == [env["group_id"]]
    assert got.json()["pending_group_ids"] == []

    # C2.3 runtime honors the ResourceType.AGENT grant end to end.
    runtime = client.get(f"/api/v1/agents/{agent_id}", headers=_auth(env["grantee_token"]))
    assert runtime.status_code == 200
    assert runtime.json()["id"] == agent_id

    # Re-approving the same (now-decided) request is a clean 404, not a
    # double grant / double audit entry.
    again = client.patch(
        f"/api/admin/share-requests/{request_id}",
        json={"decision": "approve"},
        headers=_auth(admin_token),
    )
    assert again.status_code == 404


def test_admin_reject_leaves_no_grant(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"])
    client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(env["owner_token"]),
    )
    request_id = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token)).json()["data"][0][
        "id"
    ]

    r = client.patch(
        f"/api/admin/share-requests/{request_id}",
        json={"decision": "reject"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    got = client.get(f"/api/sharing/agent/{agent_id}", headers=_auth(env["owner_token"]))
    assert got.json()["group_ids"] == []
    assert got.json()["pending_group_ids"] == []

    runtime = client.get(f"/api/v1/agents/{agent_id}", headers=_auth(env["grantee_token"]))
    assert runtime.status_code == 404


def test_admin_decide_rejects_unknown_decision_value(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"])
    client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(env["owner_token"]),
    )
    request_id = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token)).json()["data"][0][
        "id"
    ]

    r = client.patch(
        f"/api/admin/share-requests/{request_id}",
        json={"decision": "maybe"},
        headers=_auth(admin_token),
    )
    assert r.status_code == 400

    # Still pending — the bad request never touched the row.
    still_pending = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token)).json()
    assert still_pending["total"] == 1


# ---------------------------------------------------------------------------
# Admin moderation hub — the "Pending agent shares" zone (web UI)
# ---------------------------------------------------------------------------


def test_moderation_hub_lists_pending_share_and_approve_clears_it(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"], name="Hub Agent")
    client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(env["owner_token"]),
    )

    page = client.get("/admin/store", headers=_auth(admin_token))
    assert page.status_code == 200
    assert "Pending agent shares" in page.text
    assert "Hub Agent" in page.text
    assert "owner@test.com" in page.text
    assert "c6-group" in page.text

    request_id = client.get("/api/admin/share-requests?status=pending", headers=_auth(admin_token)).json()["data"][0][
        "id"
    ]
    approved = client.patch(
        f"/api/admin/share-requests/{request_id}",
        json={"decision": "approve"},
        headers=_auth(admin_token),
    )
    assert approved.status_code == 200

    page_after = client.get("/admin/store", headers=_auth(admin_token))
    assert "No agent shares waiting on approval." in page_after.text
    assert "Hub Agent" not in page_after.text


# ---------------------------------------------------------------------------
# Admin (or owner-who-is-admin) actor sharing an agent stays instant
# ---------------------------------------------------------------------------


def test_admin_actor_sharing_an_agent_is_instant_no_queue(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    # admin1 owns and shares its own agent here — the admin-actor path,
    # unaffected by the approval gate regardless of ownership.
    agent_id = _create_agent(client, admin_token, name="Admin Agent")

    r = client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["group_ids"] == [env["group_id"]]
    assert r.json()["pending_group_ids"] == []

    runtime = client.get(f"/api/v1/agents/{agent_id}", headers=_auth(env["grantee_token"]))
    assert runtime.status_code == 200

    listed = client.get("/api/admin/share-requests", headers=_auth(admin_token))
    assert listed.json()["total"] == 0


def test_unsharing_an_agent_by_a_non_admin_owner_stays_instant(tmp_path, monkeypatch, pg_engine):
    """Revoking access only narrows reach — never gated, even for a
    non-admin owner."""
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()
    agent_id = _create_agent(client, env["owner_token"])

    # Admin grants directly first (bypassing the queue, the instant path).
    client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(admin_token),
    )

    # The (non-admin) owner un-shares — instant removal, no queue involved.
    r = client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": []},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200
    assert r.json()["group_ids"] == []
    assert r.json()["pending_group_ids"] == []


# ---------------------------------------------------------------------------
# Non-agent shares are unaffected
# ---------------------------------------------------------------------------


def test_non_agent_share_stays_instant_and_unqueued(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()

    from src.repositories import file_corpora_repo

    corpus_id = file_corpora_repo().create(
        name="Owner's folder", slug="owners-folder", description=None, created_by="owner1"
    )

    r = client.put(
        f"/api/sharing/collection/{corpus_id}",
        json={"group_ids": [env["group_id"]]},
        headers=_auth(env["owner_token"]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["group_ids"] == [env["group_id"]]
    assert r.json()["pending_group_ids"] == []

    listed = client.get("/api/admin/share-requests", headers=_auth(admin_token))
    assert listed.json()["total"] == 0


# ---------------------------------------------------------------------------
# RBAC on the admin endpoints
# ---------------------------------------------------------------------------


def test_admin_endpoints_403_for_non_admin(tmp_path, monkeypatch, pg_engine):
    client, _admin_token = _pg_client(tmp_path, monkeypatch, pg_engine)
    env = _make_owner_and_group()

    assert client.get("/api/admin/share-requests", headers=_auth(env["owner_token"])).status_code == 403
    assert (
        client.patch(
            "/api/admin/share-requests/does-not-exist",
            json={"decision": "approve"},
            headers=_auth(env["owner_token"]),
        ).status_code
        == 403
    )
    assert (
        client.patch(
            "/api/admin/share-requests/does-not-exist",
            json={"decision": "reject"},
            headers=_auth(env["owner_token"]),
        ).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# DuckDB-backed instance: the approval QUEUE is unavailable (PG-only), but
# sharing itself must never regress — it falls back to the pre-C6 instant
# grant rather than failing clean with a 501. The 501 stays reserved for the
# genuinely PG-only admin queue surface below, which has no old-behavior
# equivalent to fall back to.
# ---------------------------------------------------------------------------


def test_duckdb_backend_non_admin_agent_share_falls_back_to_instant_grant(tmp_path, monkeypatch, pg_engine):
    """The approval queue doesn't exist on DuckDB (A3 ratchet), so a
    non-admin owner's share must behave exactly as it did before C6 —
    instant grant, not a 501 regression (see CI catch on PR #1710:
    tests/test_v1_builder_parity.py's DuckDB-backend agent-sharing tests
    broke against the first cut of this gate)."""
    client, admin_token = _duckdb_client(tmp_path, monkeypatch, pg_engine)

    from app.auth.jwt import create_access_token
    from src.db import get_system_db
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="owner1", email="owner@test.com", name="Owner")
    UserRepository(conn).create(id="grantee1", email="grantee@test.com", name="Grantee")
    group = UserGroupsRepository(conn).create(name="c6-duckdb-group", created_by="admin1")
    UserGroupMembersRepository(conn).add_member("owner1", group["id"], source="admin", added_by="admin1")
    UserGroupMembersRepository(conn).add_member("grantee1", group["id"], source="admin", added_by="admin1")
    owner_token = create_access_token("owner1", "owner@test.com")
    grantee_token = create_access_token("grantee1", "grantee@test.com")

    agent_id = _create_agent(client, owner_token)

    r = client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [group["id"]]},
        headers=_auth(owner_token),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["group_ids"] == [group["id"]]
    assert body["pending_group_ids"] == []

    # The grant is real and instant — no queue, no admin step, no 501.
    got = client.get(f"/api/sharing/agent/{agent_id}", headers=_auth(owner_token))
    assert got.json()["group_ids"] == [group["id"]]

    runtime = client.get(f"/api/v1/agents/{agent_id}", headers=_auth(grantee_token))
    assert runtime.status_code == 200

    # The (unreachable) admin queue never saw this share.
    queue = client.get("/api/admin/share-requests", headers=_auth(admin_token))
    assert queue.status_code == 501


def test_duckdb_backend_admin_agent_share_still_works(tmp_path, monkeypatch, pg_engine):
    """The approval gate is scoped to the non-admin-actor case — an admin
    sharing an agent must keep working on DuckDB exactly as before."""
    client, admin_token = _duckdb_client(tmp_path, monkeypatch, pg_engine)

    from src.db import get_system_db
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    group = UserGroupsRepository(conn).create(name="c6-duckdb-admin-group", created_by="admin1")

    agent_id = _create_agent(client, admin_token, name="Admin DuckDB Agent")
    r = client.put(
        f"/api/sharing/agent/{agent_id}",
        json={"group_ids": [group["id"]]},
        headers=_auth(admin_token),
    )
    assert r.status_code == 200, r.text
    assert r.json()["group_ids"] == [group["id"]]


def test_duckdb_backend_share_requests_admin_endpoint_fails_clean_501(tmp_path, monkeypatch, pg_engine):
    client, admin_token = _duckdb_client(tmp_path, monkeypatch, pg_engine)
    r = client.get("/api/admin/share-requests", headers=_auth(admin_token))
    assert r.status_code == 501
    assert r.json()["error"] == "requires_postgres_backend"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
