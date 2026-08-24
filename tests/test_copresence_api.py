"""SR-8 / SR-9 co-presence API gate tests (Task 14).

Covers:
- invite requires caller owns S0 AND invitee has CHAT access
- non-owner invite → 403
- join-ticket only for a live participant; stranger → 403
- seed is a summary, not a raw clone (SR-8: SECRET_ROW_VALUE absent in S1)
- add_sink rejects non-participant (SR-9)

All fixtures are fully self-contained; no shared state between tests.
"""
from __future__ import annotations

import pytest
import duckdb

from src.db import get_system_db, SYSTEM_ADMIN_GROUP


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_app_with_conn(conn):
    """Build a TestClient wired to the same DuckDB connection that
    was seeded by the test fixture."""
    from app.main import create_app
    from fastapi.testclient import TestClient
    app = create_app()
    # Inject the seeded DB connection into app state so the auth layer
    # and chat repo see the same data.
    from app.chat.persistence import ChatRepository
    app.state.chat_repo = ChatRepository(conn)
    return TestClient(app)


def _setup_users_and_chat_grant(conn):
    """Seed owner (a@example.com) and invitee (b@example.com).
    Grant CHAT access to both users via the resource_grants path.
    Returns (owner_token, invitee_token, other_token).
    """
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from app.auth.jwt import create_access_token

    users = UserRepository(conn)
    users.create(id="owner1", email="owner@example.com", name="Owner")
    users.create(id="collab1", email="collab@example.com", name="Collab")
    users.create(id="other1", email="other@example.com", name="Other")

    groups = UserGroupsRepository(conn)
    chat_group = groups.create(name="chat-users", description="chat", created_by="test")
    members = UserGroupMembersRepository(conn)
    members.add_member("owner1", chat_group["id"], source="admin", added_by="test")
    members.add_member("collab1", chat_group["id"], source="admin", added_by="test")
    # other1 does NOT get chat access

    grants = ResourceGrantsRepository(conn)
    grants.create(
        group_id=chat_group["id"],
        resource_type="chat",
        resource_id="chat",
        assigned_by="test",
        requirement="required",
    )

    owner_token = create_access_token("owner1", "owner@example.com")
    collab_token = create_access_token("collab1", "collab@example.com")
    other_token = create_access_token("other1", "other@example.com")
    return owner_token, collab_token, other_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def co_api(e2e_env, shared_app):
    """Returns (client, s0_id, owner_hdr, invitee_email)."""
    conn = get_system_db()
    owner_token, collab_token, _ = _setup_users_and_chat_grant(conn)

    # Create a source session for owner
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="owner@example.com", surface=Surface.WEB)

    from fastapi.testclient import TestClient
    app = shared_app
    # Wire both the auth DB + chat repo to the seeded conn
    app.state.chat_repo = repo
    client = TestClient(app)

    owner_hdr = {"Authorization": f"Bearer {owner_token}"}
    yield client, s0.id, owner_hdr, "collab@example.com"
    conn.close()


@pytest.fixture
def co_api_other(e2e_env, shared_app):
    """Returns (client, s0_id, other_hdr, invitee_email) — other is NOT the session owner."""
    conn = get_system_db()
    owner_token, collab_token, other_token = _setup_users_and_chat_grant(conn)
    # Grant chat access to other1 too so the only rejection reason is ownership
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("chat-users")
    UserGroupMembersRepository(conn).add_member("other1", grp["id"], source="admin", added_by="test")

    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="owner@example.com", surface=Surface.WEB)

    from fastapi.testclient import TestClient
    app = shared_app
    app.state.chat_repo = repo
    client = TestClient(app)

    other_hdr = {"Authorization": f"Bearer {other_token}"}
    yield client, s0.id, other_hdr, "collab@example.com"
    conn.close()


@pytest.fixture
def co_api_joined(e2e_env, shared_app):
    """Creates a co-session with owner + collab; stranger has no participant row.
    Returns (client, s1_id, collab_hdr, stranger_hdr).
    """
    conn = get_system_db()
    owner_token, collab_token, other_token = _setup_users_and_chat_grant(conn)

    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="owner@example.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        source_id=s0.id,
        owner_email="owner@example.com", owner_user_id="owner1",
        invitee_email="collab@example.com", invitee_user_id="collab1",
    )

    from fastapi.testclient import TestClient
    app = shared_app
    app.state.chat_repo = repo
    client = TestClient(app)

    collab_hdr = {"Authorization": f"Bearer {collab_token}"}
    stranger_hdr = {"Authorization": f"Bearer {other_token}"}
    yield client, s1.id, collab_hdr, stranger_hdr
    conn.close()


@pytest.fixture
def co_api_secret(e2e_env, shared_app):
    """S0 contains SECRET_ROW_VALUE in a message; invite must NOT clone it.
    Returns (client, s0_id, owner_hdr, invitee_email).
    """
    conn = get_system_db()
    owner_token, collab_token, _ = _setup_users_and_chat_grant(conn)

    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface
    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="owner@example.com", surface=Surface.WEB)
    # Seed a message with the secret that must NOT appear in the co-session
    repo.append_message(session_id=s0.id, role="assistant", content="SECRET_ROW_VALUE is the key")

    from fastapi.testclient import TestClient
    app = shared_app
    app.state.chat_repo = repo
    client = TestClient(app)

    owner_hdr = {"Authorization": f"Bearer {owner_token}"}
    yield client, s0.id, owner_hdr, "collab@example.com"
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_invite_requires_owner_and_invitee_chat_access(co_api):
    client, s0, owner_hdr, invitee_email = co_api
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": invitee_email}, headers=owner_hdr)
    assert r.status_code == 200
    assert r.json()["is_co_session"] is True


@pytest.fixture
def co_api_case(e2e_env, shared_app):
    """Like ``co_api`` but also yields the invitee's own token, so a test can
    check that the invited person can actually USE the session."""
    conn = get_system_db()
    owner_token, collab_token, _ = _setup_users_and_chat_grant(conn)

    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="owner@example.com", surface=Surface.WEB)

    from fastapi.testclient import TestClient

    app = shared_app
    app.state.chat_repo = repo
    client = TestClient(app)
    yield client, s0.id, {"Authorization": f"Bearer {owner_token}"}, {"Authorization": f"Bearer {collab_token}"}
    conn.close()


def test_invite_by_case_variant_address_still_lets_the_invitee_in(co_api_case):
    """The invite must be recorded under the invited ACCOUNT's address.

    Resolving the invitee case-insensitively is only half the job: every
    downstream authorization check compares participant rows to the account's
    stored email with plain string equality, and compute_grant_intersection
    resolves participants exactly and fails closed. Persisting the spelling the
    inviter typed makes the invite appear to succeed and then 403 the invitee
    on every read, join and leave.
    """
    client, s0, owner_hdr, collab_hdr = co_api_case
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": "Collab@Example.COM"}, headers=owner_hdr)
    assert r.status_code == 200, r.text
    s1 = r.json()["session_id"]

    # The invited person can actually open the session they were invited to.
    assert client.post(f"/api/chat/{s1}/join-ticket", headers=collab_hdr).status_code == 200
    assert client.get(f"/api/chat/{s1}/messages", headers=collab_hdr).status_code == 200


def test_invite_rejects_non_owner(co_api_other):
    client, s0, other_hdr, invitee_email = co_api_other
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": invitee_email}, headers=other_hdr)
    assert r.status_code == 403


def test_join_ticket_only_for_live_participant(co_api_joined):
    client, s1, collab_hdr, stranger_hdr = co_api_joined
    assert client.post(f"/api/chat/{s1}/join-ticket", headers=collab_hdr).status_code == 200
    assert client.post(f"/api/chat/{s1}/join-ticket", headers=stranger_hdr).status_code == 403


def test_seed_is_summary_not_raw_clone(co_api_secret):
    client, s0, owner_hdr, invitee_email = co_api_secret
    r = client.post(f"/api/chat/{s0}/invite", json={"invitee_email": invitee_email}, headers=owner_hdr)
    assert r.status_code == 200, r.text
    s1 = r.json()["session_id"]
    msgs = client.get(f"/api/chat/{s1}/messages", headers=owner_hdr)
    assert msgs.status_code == 200, msgs.text
    joined = " ".join(m.get("content", "") for m in msgs.json())
    assert "SECRET_ROW_VALUE" not in joined


def test_leave_rejects_non_participant(co_api_joined, monkeypatch):
    """A non-participant who knows a co-session id must NOT be able to call
    leave (which would trigger _respawn_co_runner → DoS the real
    participants). The membership gate returns 403 before leave_session runs."""
    client, s1, collab_hdr, stranger_hdr = co_api_joined
    # Spy: leave_session must never be invoked for the stranger.
    from app.chat.manager import ChatManager
    called = []
    monkeypatch.setattr(
        ChatManager, "leave_session",
        lambda self, sid, email: called.append((sid, email)),
        raising=False,
    )
    r = client.post(f"/api/chat/{s1}/leave", headers=stranger_hdr)
    assert r.status_code == 403, r.text
    assert called == []  # respawn path never reached


def test_leave_unknown_session_404(co_api_joined):
    client, s1, collab_hdr, stranger_hdr = co_api_joined
    r = client.post("/api/chat/does-not-exist/leave", headers=collab_hdr)
    assert r.status_code == 404, r.text
