"""FIX 1: memory/bundle endpoint must be principal-aware (SessionPrincipal).

Tests:
- co-session token GET /api/memory/bundle?domain=<granted> → 200 (not 500)
- co-session token for a domain only ONE participant has → 403 (not 500)
- co-session token GET /api/memory/bundle (no domain) → 200 (not 500)
"""
from __future__ import annotations

import uuid

import pytest

from src.db import get_system_db


def _seed_co_memory_env(conn, *, grant_domain_to_both: bool = True):
    """Seed two users + a co-session + one memory domain.

    If grant_domain_to_both is True, both participants have the
    memory_domain grant (intersection includes it).  If False, only the
    owner has the grant (domain NOT in intersection → 403 for co-token).

    Returns (co_session_id, domain_slug, domain_id).
    """
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.memory_domains import MemoryDomainsRepository
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    UserRepository(conn).create(id="mu1", email="ma@x.com", name="A")
    UserRepository(conn).create(id="mu2", email="mb@x.com", name="B")

    domain_slug = "test-domain-" + uuid.uuid4().hex[:6]
    domain_id = MemoryDomainsRepository(conn).create(
        name="Test", slug=domain_slug, description="",
        icon=None, color=None, created_by="test",
    )

    # Owner group / grant
    groups = UserGroupsRepository(conn)
    ga = groups.create(name="grp-a-" + domain_slug, description="", created_by="test")
    UserGroupMembersRepository(conn).add_member("mu1", ga["id"], source="admin", added_by="test")
    ResourceGrantsRepository(conn).create(
        group_id=ga["id"], resource_type="memory_domain",
        resource_id=domain_id, assigned_by="test", requirement="required",
    )

    if grant_domain_to_both:
        gb = groups.create(name="grp-b-" + domain_slug, description="", created_by="test")
        UserGroupMembersRepository(conn).add_member("mu2", gb["id"], source="admin", added_by="test")
        ResourceGrantsRepository(conn).create(
            group_id=gb["id"], resource_type="memory_domain",
            resource_id=domain_id, assigned_by="test", requirement="required",
        )

    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="ma@x.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="ma@x.com", owner_user_id="mu1",
        invitee_email="mb@x.com", invitee_user_id="mu2",
    )
    return s1.id, domain_slug, domain_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def co_bundle_app_shared(e2e_env, shared_app):
    """Both participants have the memory_domain grant → domain in intersection."""
    conn = get_system_db()
    co_id, slug, dom_id = _seed_co_memory_env(conn, grant_domain_to_both=True)
    conn.close()

    from fastapi.testclient import TestClient
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt
    token = mint_co_session_jwt(co_id)
    yield client, slug, token


@pytest.fixture
def co_bundle_app_owner_only(e2e_env, shared_app):
    """Only owner has the memory_domain grant → domain NOT in intersection."""
    conn = get_system_db()
    co_id, slug, dom_id = _seed_co_memory_env(conn, grant_domain_to_both=False)
    conn.close()

    from fastapi.testclient import TestClient
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt
    token = mint_co_session_jwt(co_id)
    yield client, slug, token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bundle_domain_shared_returns_200_not_500(co_bundle_app_shared):
    """co-session token on a shared memory_domain → 200, not 500."""
    client, slug, token = co_bundle_app_shared
    r = client.get(f"/api/memory/bundle?domain={slug}",
                   headers={"Authorization": f"Bearer {token}"})
    # Must not be 500 (the old crash). Should be 200.
    assert r.status_code != 500, f"Got 500 (crash), body: {r.text}"
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"


def test_bundle_domain_owner_only_returns_403_not_500(co_bundle_app_owner_only):
    """co-session token on a domain only the owner has → 403, not 500."""
    client, slug, token = co_bundle_app_owner_only
    r = client.get(f"/api/memory/bundle?domain={slug}",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 500, f"Got 500 (crash), body: {r.text}"
    assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"


def test_bundle_no_domain_with_co_token_returns_200_not_500(co_bundle_app_shared):
    """co-session token on the non-domain bundle path → 200, not 500."""
    client, _slug, token = co_bundle_app_shared
    r = client.get("/api/memory/bundle",
                   headers={"Authorization": f"Bearer {token}"})
    assert r.status_code != 500, f"Got 500 (crash), body: {r.text}"
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
