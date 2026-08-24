"""Agent PATs: typ=agent_pat + agent_id claim; hard-rejected off-surface.

Covers `app/auth/pat_resolver.py` (`_AGENT_PAT_ALLOWED_PREFIXES`,
`agent_id_from_request`) and the reason→detail mapping in
`app/auth/dependencies.py::get_current_user`.

`/api/catalog` in the task brief is a placeholder for "any legacy `/api/*`
route" — the real route is `/api/catalog/tables` (there is no bare
`/api/catalog` endpoint), which is what the API-level tests below hit.
"""

from __future__ import annotations

import hashlib
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_access_token


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-minimum-32-characters!!")

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="user@test.com", name="User")
    conn.close()

    return TestClient(shared_app)


@pytest.fixture
def seeded_user():
    return {"id": "u1", "email": "user@test.com"}


@pytest.fixture
def seeded_agent(client, seeded_user):
    """Depends on `client` so the system DB exists before AgentsRepository writes."""
    from src.repositories import agents_repo

    agent_id = str(uuid.uuid4())
    agents_repo().create(
        id=agent_id,
        owner_user_id=seeded_user["id"],
        name="Test Agent",
        slug="test-agent-" + agent_id[:8],
    )
    return {"id": agent_id}


@pytest.fixture
def seeded_user_pat(client, seeded_user):
    """A normal (non-agent) PAT — must be completely unaffected by this change."""
    from src.repositories import access_token_repo

    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=seeded_user["id"],
        email=seeded_user["email"],
        token_id=token_id,
        typ="pat",
    )
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=seeded_user["id"],
        name="test-pat",
        token_hash=token_hash,
        prefix=token_id.replace("-", "")[:8],
    )
    return jwt_token


def _mint_agent_pat(user, agent_id, token_id="tok-1"):
    return create_access_token(
        user_id=user["id"],
        email=user["email"],
        token_id=token_id,
        typ="agent_pat",
        extra_claims={"agent_id": agent_id},
    )


def _register_agent_pat_row(user, agent_id, token, token_id):
    """Insert the DB-backed row an agent PAT needs to pass the validity chain."""
    from src.repositories import access_token_repo

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=user["id"],
        name="agent-pat",
        token_hash=token_hash,
        prefix=token_id.replace("-", "")[:8],
        agent_id=agent_id,
    )


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    """Minimal stand-in for `fastapi.Request` — just enough surface for
    `resolve_token_to_user` (`.url.path`, `.headers.get`, `.client`, `.state`)."""

    def __init__(self, path, headers=None):
        self.url = _FakeURL(path)
        self.headers = headers or {}
        self.client = None
        self.state = types.SimpleNamespace()


# ---------------------------------------------------------------------------
# Task-brief API-level tests (adapted to a real route)
# ---------------------------------------------------------------------------


def test_agent_pat_rejected_on_legacy_api(client, seeded_user, seeded_agent):
    token = _mint_agent_pat(seeded_user, seeded_agent["id"])
    r = client.get("/api/catalog/tables", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Agent token not valid on this surface"


def test_agent_pat_rejected_on_marketplace_zip(client, seeded_user, seeded_agent):
    token = _mint_agent_pat(seeded_user, seeded_agent["id"])
    r = client.get("/marketplace.zip", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (401, 403)


def test_user_pat_unaffected(client, seeded_user_pat):
    r = client.get("/api/catalog/tables", headers={"Authorization": f"Bearer {seeded_user_pat}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Resolver-level tests (no real /api/v1/agents|sessions|jobs routes exist
# yet in this branch — those land in later tasks — so the "allowed surface"
# and DB-validity-chain paths are exercised directly against
# `resolve_token_to_user`).
# ---------------------------------------------------------------------------


def test_agent_pat_wrong_surface_reason(client, seeded_user, seeded_agent):
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_agent_pat(seeded_user, seeded_agent["id"])
    req = _FakeRequest("/api/legacy/whatever")
    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "agent_pat_wrong_surface"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/agents/",
        "/api/v1/agents/abc123/chat",
        "/api/v1/sessions/xyz",
        "/api/v1/jobs/job-1",
    ],
)
def test_agent_pat_allowed_surface_resolves(client, seeded_user, seeded_agent, path):
    from app.auth.pat_resolver import agent_id_from_request, resolve_token_to_user

    token_id = "tok-allow-" + path.replace("/", "_")
    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id=token_id)
    _register_agent_pat_row(seeded_user, seeded_agent["id"], token, token_id)

    req = _FakeRequest(path)
    user, reason = resolve_token_to_user(None, token, req)
    assert reason is None
    assert user is not None
    assert user["id"] == seeded_user["id"]
    # helper reads the stashed payload, no re-verification
    assert agent_id_from_request(req) == seeded_agent["id"]


def test_agent_pat_prefix_boundary_not_matched(client, seeded_user, seeded_agent):
    """`startswith` on the allow-tuple must not match a path that merely
    starts with the same characters but lacks the trailing slash — e.g. a
    hypothetical `/api/v1/agentsevil/` sibling route must NOT be treated as
    in-scope for `/api/v1/agents/`."""
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_agent_pat(seeded_user, seeded_agent["id"])
    req = _FakeRequest("/api/v1/agentsevil/whatever")
    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "agent_pat_wrong_surface"


def test_agent_pat_still_runs_db_validity_chain(client, seeded_user, seeded_agent):
    """Agent PATs live in `personal_access_tokens` with `agent_id` set — the
    same revoked/expired/unknown/hash-mismatch chain that guards `typ=pat`
    must guard them too, even on an allowed surface."""
    from src.repositories import access_token_repo
    from app.auth.pat_resolver import resolve_token_to_user

    token_id = "tok-revoke-1"
    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id=token_id)
    _register_agent_pat_row(seeded_user, seeded_agent["id"], token, token_id)

    req = _FakeRequest("/api/v1/agents/abc/chat")
    user, reason = resolve_token_to_user(None, token, req)
    assert reason is None  # sanity: valid before revocation

    access_token_repo().revoke(token_id)
    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "pat_revoked"


def test_agent_pat_unknown_row_rejected_even_on_allowed_surface(client, seeded_user, seeded_agent):
    """No DB row for the jti at all (never minted through the repo) → pat_unknown,
    not a silent pass — an agent_pat JWT alone (forged path aside) must not be
    sufficient without a live DB row."""
    from app.auth.pat_resolver import resolve_token_to_user

    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id="tok-never-registered")
    req = _FakeRequest("/api/v1/agents/abc/chat")
    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "pat_unknown"


def test_agent_id_from_request_none_for_non_agent_tokens(client, seeded_user_pat):
    """Helper must return None for a plain user PAT (or any non-agent_pat typ)."""
    from app.auth.pat_resolver import agent_id_from_request, resolve_token_to_user

    req = _FakeRequest("/api/catalog/tables")
    user, reason = resolve_token_to_user(None, seeded_user_pat, req)
    assert reason is None
    assert agent_id_from_request(req) is None


def test_agent_id_from_request_none_without_prior_resolve():
    """No `request.state.token_payload` stashed → None, never raises."""
    from app.auth.pat_resolver import agent_id_from_request

    req = _FakeRequest("/api/v1/agents/abc")
    assert agent_id_from_request(req) is None


def test_agent_id_from_request_none_after_failed_resolution(client, seeded_user, seeded_agent):
    """Task 4 review carry-over: a token that decodes fine but then fails a
    later DB-backed check (revoked here) must leave NO stashed payload on
    `request.state` — `agent_id_from_request` must return None, not the
    revoked token's `agent_id` claim.

    Before the fix, `resolve_token_to_user` stashed `request.state.token_payload`
    immediately after `verify_token` succeeded, before any of the
    revoked/expired/mismatched/wrong-surface/deleted-agent checks ran — so a
    failed resolution still left a stale, readable payload behind.
    """
    from app.auth.pat_resolver import agent_id_from_request, resolve_token_to_user
    from src.repositories import access_token_repo

    token_id = "tok-stale-payload"
    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id=token_id)
    _register_agent_pat_row(seeded_user, seeded_agent["id"], token, token_id)
    access_token_repo().revoke(token_id)

    req = _FakeRequest("/api/v1/agents/abc/chat")
    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "pat_revoked"
    assert agent_id_from_request(req) is None


# ---------------------------------------------------------------------------
# Repository-level tests
# ---------------------------------------------------------------------------


def test_access_token_repo_create_stores_agent_id(client, seeded_user, seeded_agent):
    from src.repositories import access_token_repo

    repo = access_token_repo()
    token_id = str(uuid.uuid4())
    repo.create(
        id=token_id,
        user_id=seeded_user["id"],
        name="agent-tok",
        token_hash="deadbeef",
        prefix=token_id[:8],
        agent_id=seeded_agent["id"],
    )
    row = repo.get_by_id(token_id)
    assert row["agent_id"] == seeded_agent["id"]


def test_access_token_repo_create_agent_id_defaults_none(client, seeded_user):
    """Existing callers that never pass agent_id must stay source-compatible."""
    from src.repositories import access_token_repo

    repo = access_token_repo()
    token_id = str(uuid.uuid4())
    repo.create(
        id=token_id,
        user_id=seeded_user["id"],
        name="user-tok",
        token_hash="deadbeef",
        prefix=token_id[:8],
    )
    row = repo.get_by_id(token_id)
    assert row.get("agent_id") is None


def test_access_token_repo_revoke_for_agent(client, seeded_user, seeded_agent):
    from src.repositories import access_token_repo

    repo = access_token_repo()
    ids = [str(uuid.uuid4()) for _ in range(2)]
    for tid in ids:
        repo.create(
            id=tid,
            user_id=seeded_user["id"],
            name="agent-tok",
            token_hash="deadbeef",
            prefix=tid[:8],
            agent_id=seeded_agent["id"],
        )
    other_id = str(uuid.uuid4())
    repo.create(
        id=other_id,
        user_id=seeded_user["id"],
        name="user-tok",
        token_hash="deadbeef",
        prefix=other_id[:8],
    )

    repo.revoke_for_agent(seeded_agent["id"])

    for tid in ids:
        assert repo.get_by_id(tid)["revoked_at"] is not None
    assert repo.get_by_id(other_id)["revoked_at"] is None


def test_agent_pat_rejected_by_require_session_token(client, seeded_user, seeded_agent):
    """`require_session_token` must reject agent PATs, same as plain PATs.

    Mirrors `test_require_session_token_rejects_scheduler_secret`
    (tests/test_auth_scheduler_token.py) — calls the dependency directly
    (bypassing `Depends(get_current_user)`) rather than round-tripping
    through a real endpoint. A round trip through e.g. `/auth/tokens` would
    already 401 an agent PAT via the surface allowlist in
    `resolve_token_to_user` before `require_session_token`'s own body ever
    ran, which would mask whether *this* check independently catches
    `typ="agent_pat"` too.
    """
    import asyncio
    from unittest.mock import MagicMock

    from fastapi import HTTPException

    from app.auth.dependencies import require_session_token

    token = _mint_agent_pat(seeded_user, seeded_agent["id"])

    request = MagicMock()
    request.headers = {"authorization": f"Bearer {token}"}
    request.cookies = {}

    try:
        asyncio.run(require_session_token(request=request, user=seeded_user))
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "interactive" in exc.detail.lower()
    else:
        raise AssertionError("require_session_token must reject an agent PAT")


# ---------------------------------------------------------------------------
# No-`request` callers (git smart-HTTP, MCP HTTP) must fail closed
# ---------------------------------------------------------------------------


def test_agent_pat_no_request_fails_closed(client, seeded_user, seeded_agent):
    """Callers that omit `request` (git_router.py, mcp_http.py) must fail
    closed for agent PATs — a missing request resolves to path="", which
    never matches `_AGENT_PAT_ALLOWED_PREFIXES`."""
    from app.auth.pat_resolver import resolve_token_to_user

    token_id = "tok-no-request"
    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id=token_id)
    _register_agent_pat_row(seeded_user, seeded_agent["id"], token, token_id)

    user, reason = resolve_token_to_user(None, token, request=None)
    assert user is None
    assert reason == "agent_pat_wrong_surface"


def test_agent_pat_marketplace_zip_path_fails_closed(client, seeded_user, seeded_agent):
    """Same fail-closed outcome when a request IS present but its path is
    `/marketplace.zip` — the git/marketplace channel's real surface."""
    from app.auth.pat_resolver import resolve_token_to_user

    token_id = "tok-marketplace-zip"
    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id=token_id)
    _register_agent_pat_row(seeded_user, seeded_agent["id"], token, token_id)

    req = _FakeRequest("/marketplace.zip")
    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "agent_pat_wrong_surface"


def test_user_pat_no_request_still_resolves(client, seeded_user_pat):
    """Regression guard for the git channel: a plain user PAT with no
    `request` (git_router.py's / mcp_http.py's call pattern) must keep
    resolving normally — only agent PATs are surface-gated."""
    from app.auth.pat_resolver import resolve_token_to_user

    user, reason = resolve_token_to_user(None, seeded_user_pat, request=None)
    assert reason is None
    assert user is not None
    assert user["id"] == "u1"


def test_agent_pat_agent_deleted_defense_in_depth(client, seeded_user, seeded_agent):
    """Delete-cascade-bypass: soft-delete the agent row directly (skipping
    `access_token_repo().revoke_for_agent`, which is what a failed cascade
    call would leave in place) and confirm the resolver's independent
    agent-liveness check still kills the token."""
    from src.repositories import agents_repo
    from app.auth.pat_resolver import resolve_token_to_user

    token_id = "tok-agent-deleted"
    token = _mint_agent_pat(seeded_user, seeded_agent["id"], token_id=token_id)
    _register_agent_pat_row(seeded_user, seeded_agent["id"], token, token_id)

    req = _FakeRequest("/api/v1/agents/abc/chat")
    user, reason = resolve_token_to_user(None, token, req)
    assert reason is None  # sanity: valid before the agent is deleted

    agents_repo().soft_delete(seeded_agent["id"])  # cascade "failed": no revoke_for_agent call

    user, reason = resolve_token_to_user(None, token, req)
    assert user is None
    assert reason == "agent_pat_agent_deleted"


def test_user_pat_unaffected_by_agent_liveness_check(client, seeded_user_pat):
    """A plain user PAT (typ=pat, no agent_id) must never run the
    agent-liveness check — it has no agent to look up."""
    from app.auth.pat_resolver import resolve_token_to_user

    req = _FakeRequest("/api/catalog/tables")
    user, reason = resolve_token_to_user(None, seeded_user_pat, req)
    assert reason is None
    assert user is not None
    assert user["id"] == "u1"


def test_access_token_repo_list_for_agent(client, seeded_user, seeded_agent):
    from src.repositories import access_token_repo

    repo = access_token_repo()
    tid = str(uuid.uuid4())
    repo.create(
        id=tid,
        user_id=seeded_user["id"],
        name="agent-tok",
        token_hash="deadbeef",
        prefix=tid[:8],
        agent_id=seeded_agent["id"],
    )
    other_id = str(uuid.uuid4())
    repo.create(
        id=other_id,
        user_id=seeded_user["id"],
        name="user-tok",
        token_hash="deadbeef",
        prefix=other_id[:8],
    )

    rows = repo.list_for_agent(seeded_agent["id"])
    assert {r["id"] for r in rows} == {tid}
