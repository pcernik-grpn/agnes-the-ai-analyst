"""FIX 3: _run_internal_query must handle SessionPrincipal.

Tests:
- co-session token SELECT * FROM agnes_sessions → 200 with 0 rows (not 500)
"""
from __future__ import annotations

import pytest

from src.db import get_system_db


def _seed_query_co_env(conn):
    """Seed two users + a co-session, return co_session_id."""
    from src.repositories.users import UserRepository
    from app.chat.persistence import ChatRepository
    from app.chat.types import Surface

    UserRepository(conn).create(id="qu1", email="qa@q.com", name="A")
    UserRepository(conn).create(id="qu2", email="qb@q.com", name="B")

    repo = ChatRepository(conn)
    s0 = repo.create_session(user_email="qa@q.com", surface=Surface.WEB)
    s1 = repo.fork_session_as_co_session(
        s0.id,
        owner_email="qa@q.com", owner_user_id="qu1",
        invitee_email="qb@q.com", invitee_user_id="qu2",
    )
    return s1.id


@pytest.fixture
def query_co_app(e2e_env, shared_app):
    conn = get_system_db()
    co_id = _seed_query_co_env(conn)
    conn.close()
    from fastapi.testclient import TestClient
    app = shared_app
    client = TestClient(app, raise_server_exceptions=False)
    from app.auth.access import mint_co_session_jwt
    token = mint_co_session_jwt(co_id)
    yield client, token


def test_query_internal_co_token_returns_200_zero_rows(query_co_app):
    """co-session token SELECT * FROM agnes_sessions → 200 with 0 rows, not 500."""
    client, token = query_co_app
    r = client.post(
        "/api/query",
        json={"sql": "SELECT * FROM agnes_sessions", "limit": 10},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 500, f"Got 500 (crash): {r.text}"
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    # Must return 0 rows — empty identity shim → no rows match the filter
    assert data["row_count"] == 0, f"Expected 0 rows, got {data['row_count']}"
