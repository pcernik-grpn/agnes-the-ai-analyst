"""V1d Task 3 — the broker mints an agent-session token and the PAT resolver
returns an ``AgentPrincipal`` for it.

This is the wiring that makes the (already-computed, Task 1/2) owner-grants
intersection-agent-scope actually reach the authorization seams. Covers:

- ``mint_agent_session_jwt`` shape (no baked-in grants — SR-4 style).
- ``_mint_identity_jwt`` branch order: co-session (untouched) -> solo session
  whose agent narrows -> agent-session JWT; a solo session with a plain
  all-'all' default agent, or no agent at all, must keep minting the
  ordinary owner JWT (web-chat regression guard).
- The resolver's ``typ="agent_session"`` branch: session -> agent_id -> agent
  row -> owner -> ``AgentPrincipal`` whose intersection matches
  ``resolve_agent_authority`` computed independently.
- Every fail-closed path, individually: missing session, session with no
  agent_id, missing agent row, soft-deleted agent, missing owner.
"""

from __future__ import annotations

import uuid

from app.auth.jwt import verify_token
from app.chat.types import Surface
from src.db import get_system_db
from src.repositories import agents_repo, chat_session_repo
from src.repositories.users import UserRepository


def _make_user():
    tag = uuid.uuid4().hex[:8]
    email = f"agent_session_{tag}@test.com"
    user_id = f"agent_session_user_{tag}"
    conn = get_system_db()
    UserRepository(conn).create(id=user_id, email=email, name="Agent Session Test User")
    conn.close()
    return user_id, email


def _make_agent(owner_user_id, **mode_overrides):
    tag = uuid.uuid4().hex[:8]
    agent_id = str(uuid.uuid4())
    kwargs = dict(
        id=agent_id,
        owner_user_id=owner_user_id,
        name="Agent Session Test Agent",
        slug=f"agent-session-test-{tag}",
        plugins_mode="all",
        connections_mode="all",
        tables_mode="all",
        memory_mode="all",
    )
    kwargs.update(mode_overrides)
    agents_repo().create(**kwargs)
    return agent_id


def _make_session(user_email, agent_id=None):
    session = chat_session_repo().create_session(user_email=user_email, surface=Surface.WEB, agent_id=agent_id)
    return session.id


# ---------------------------------------------------------------------------
# mint_agent_session_jwt — token shape, no baked-in grants
# ---------------------------------------------------------------------------


def test_mint_agent_session_jwt_shape(e2e_env):
    from app.auth.access import mint_agent_session_jwt

    token = mint_agent_session_jwt("chat_abc123")
    payload = verify_token(token)

    assert payload["typ"] == "agent_session"
    assert payload["sub"] == "agent-session:chat_abc123"
    assert payload["email"] == ""
    assert payload["scope"] == "chat"
    assert payload["chat_session_id"] == "chat_abc123"

    # No grants/intersection/agent identity baked into the token — same
    # no-stale-replay-window contract as mint_co_session_jwt. The resolver
    # rebuilds the intersection live, per request.
    forbidden_keys = {
        "intersection",
        "grants",
        "scope_list",
        "resource_grants",
        "agent_id",
        "owner_user_id",
        "tables",
        "plugins",
    }
    assert not (forbidden_keys & set(payload.keys())), payload.keys()


# ---------------------------------------------------------------------------
# _mint_identity_jwt branch order
# ---------------------------------------------------------------------------


def test_broker_mints_agent_session_jwt_for_narrowing_agent(e2e_env):
    from app.api.broker import _mint_identity_jwt

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id, tables_mode="selected")
    session_id = _make_session(owner_email, agent_id=agent_id)

    payload = verify_token(_mint_identity_jwt(session_id))

    assert payload["typ"] == "agent_session"
    assert payload["chat_session_id"] == session_id
    assert payload["sub"] == f"agent-session:{session_id}"


def test_broker_default_all_agent_mints_plain_owner_jwt(e2e_env):
    """Regression guard: an all-'all' agent (every user's lazily-seeded
    default) must NOT change web chat's JWT shape — same authority as
    today, so the broker takes the cheap owner-identity path."""
    from app.api.broker import _mint_identity_jwt

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id)  # every mode left at 'all'
    session_id = _make_session(owner_email, agent_id=agent_id)

    payload = verify_token(_mint_identity_jwt(session_id))

    assert payload["typ"] == "session"
    assert payload["sub"] == owner_id
    assert payload["email"] == owner_email


def test_broker_all_agent_on_foreign_session_takes_enforced_path(e2e_env):
    """An all-'all' agent on a session whose user is NOT the agent's owner
    (a Slack channel binding routes the MENTIONER's session to the owner's
    agent) must mint an agent-session JWT: the plain-identity fall-through
    would run the turn with the mentioning user's own authority — admin
    short-circuit included."""
    from app.api.broker import _mint_identity_jwt

    owner_id, _owner_email = _make_user()
    _mentioner_id, mentioner_email = _make_user()
    agent_id = _make_agent(owner_id)  # every mode 'all' — passthrough shape
    session_id = _make_session(mentioner_email, agent_id=agent_id)

    payload = verify_token(_mint_identity_jwt(session_id))

    assert payload["typ"] == "agent_session"
    assert payload["chat_session_id"] == session_id
    assert payload["sub"] == f"agent-session:{session_id}"


def test_broker_no_agent_session_mints_plain_owner_jwt(e2e_env):
    """A session with no agent_id at all (legacy / Slack path) is unchanged."""
    from app.api.broker import _mint_identity_jwt

    owner_id, owner_email = _make_user()
    session_id = _make_session(owner_email)  # no agent bound

    payload = verify_token(_mint_identity_jwt(session_id))

    assert payload["typ"] == "session"
    assert payload["sub"] == owner_id
    assert payload["email"] == owner_email


# ---------------------------------------------------------------------------
# Resolver: typ="agent_session" -> AgentPrincipal
# ---------------------------------------------------------------------------


def test_resolver_returns_agent_principal_matching_intersection(e2e_env, monkeypatch):
    import src.agent_scope_intersection as intersection_mod
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import AgentPrincipal

    monkeypatch.setattr(
        intersection_mod,
        "_allowed_ids_for_user",
        lambda uid, rt, conn=None: frozenset({"t1", "t2"}) if rt == "table" else frozenset(),
    )
    monkeypatch.setattr(
        intersection_mod,
        "_agent_scope_rows",
        lambda aid, it, conn=None: [{"item_id": "t1", "granted_by": None}] if it == "table" else [],
    )

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id, tables_mode="selected")
    session_id = _make_session(owner_email, agent_id=agent_id)

    token = mint_agent_session_jwt(session_id)
    principal, reason = resolve_token_to_user(None, token)

    assert reason is None
    assert isinstance(principal, AgentPrincipal)
    assert principal.session_id == session_id
    assert principal.agent_id == agent_id
    assert principal.owner_user_id == owner_id
    assert principal.owner_email == owner_email

    expected = intersection_mod.resolve_agent_authority(agent_id)
    assert principal.intersection == expected
    assert principal.intersection["table"] == frozenset({"t1"})


def test_resolver_agent_session_stashes_payload(e2e_env):
    """Mirrors the co-session branch's `_stash_payload` call, matched by
    convention (`agent_id_from_request`-style downstream readers)."""
    from unittest.mock import MagicMock

    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id, tables_mode="selected")
    session_id = _make_session(owner_email, agent_id=agent_id)

    token = mint_agent_session_jwt(session_id)
    fake_request = MagicMock()
    fake_request.state = MagicMock()

    principal, reason = resolve_token_to_user(None, token, request=fake_request)

    assert reason is None
    assert fake_request.state.token_payload.get("typ") == "agent_session"


# ---------------------------------------------------------------------------
# Fail-closed paths, each verified separately
# ---------------------------------------------------------------------------


def test_resolver_agent_session_missing_session_fails_closed(e2e_env):
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user

    token = mint_agent_session_jwt("nonexistent-session-id")
    principal, reason = resolve_token_to_user(None, token)

    assert principal is None
    assert reason == "invalid_token"


def test_resolver_agent_session_no_agent_id_fails_closed(e2e_env):
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user

    _, owner_email = _make_user()
    session_id = _make_session(owner_email)  # no agent bound

    token = mint_agent_session_jwt(session_id)
    principal, reason = resolve_token_to_user(None, token)

    assert principal is None
    assert reason == "invalid_token"


def test_resolver_agent_session_missing_agent_row_fails_closed(e2e_env):
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user

    _, owner_email = _make_user()
    # session names an agent_id that was never created
    session_id = _make_session(owner_email, agent_id=str(uuid.uuid4()))

    token = mint_agent_session_jwt(session_id)
    principal, reason = resolve_token_to_user(None, token)

    assert principal is None
    assert reason == "invalid_token"


def test_resolver_agent_session_soft_deleted_agent_fails_closed(e2e_env):
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id, tables_mode="selected")
    session_id = _make_session(owner_email, agent_id=agent_id)
    agents_repo().soft_delete(agent_id)

    token = mint_agent_session_jwt(session_id)
    principal, reason = resolve_token_to_user(None, token)

    assert principal is None
    assert reason == "invalid_token"


def test_resolver_agent_session_missing_owner_fails_closed(e2e_env):
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user

    ghost_owner_id = f"ghost_owner_{uuid.uuid4().hex[:8]}"
    agent_id = _make_agent(ghost_owner_id, tables_mode="selected")
    _, some_email = _make_user()
    session_id = _make_session(some_email, agent_id=agent_id)

    token = mint_agent_session_jwt(session_id)
    principal, reason = resolve_token_to_user(None, token)

    assert principal is None
    assert reason == "invalid_token"


# ---------------------------------------------------------------------------
# Cross-agent replay: a token minted for session A must never resolve to
# session B's agent/owner (the intersection is keyed off the *session*, not
# any claim the caller could forge — the token carries no agent_id at all).
# ---------------------------------------------------------------------------


def test_agent_session_token_cannot_be_replayed_against_a_different_session(e2e_env):
    from app.auth.access import mint_agent_session_jwt
    from app.auth.pat_resolver import resolve_token_to_user
    from app.auth.session_principal import AgentPrincipal

    owner_a, email_a = _make_user()
    agent_a = _make_agent(owner_a, tables_mode="selected")
    session_a = _make_session(email_a, agent_id=agent_a)

    owner_b, email_b = _make_user()
    agent_b = _make_agent(owner_b, tables_mode="selected")
    session_b = _make_session(email_b, agent_id=agent_b)

    token_for_a = mint_agent_session_jwt(session_a)
    # Sanity: resolving the real token for A resolves to A's agent/owner.
    principal, reason = resolve_token_to_user(None, token_for_a)
    assert reason is None
    assert isinstance(principal, AgentPrincipal)
    assert principal.agent_id == agent_a
    assert principal.owner_user_id == owner_a

    # A token minted for B's session must resolve to B, never leak into A.
    token_for_b = mint_agent_session_jwt(session_b)
    principal_b, reason_b = resolve_token_to_user(None, token_for_b)
    assert reason_b is None
    assert principal_b.agent_id == agent_b
    assert principal_b.owner_user_id == owner_b
    assert principal_b.agent_id != principal.agent_id
    assert principal_b.owner_user_id != principal.owner_user_id


# ---------------------------------------------------------------------------
# get_current_user — principal path stashes the chat-session claim
# ---------------------------------------------------------------------------


def test_get_current_user_stashes_chat_session_id_for_principal(e2e_env):
    """Regression (Devin review): the principal early-return must stash the
    chat-session claim FIRST — app/api/query.py's per-session BQ scan
    accumulator reads ``request.state.chat_session_id``, and without the
    stash a scoped agent's brokered queries would escape the per-session
    scan cap that ``scope="chat"`` promises to keep."""
    from types import SimpleNamespace

    from app.auth.access import mint_agent_session_jwt
    from app.auth.dependencies import get_current_user
    from app.auth.session_principal import PRINCIPAL_TYPES
    from src.db import get_system_db

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id, tables_mode="selected")
    session_id = _make_session(owner_email, agent_id=agent_id)
    token = mint_agent_session_jwt(session_id)

    req = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})
    conn = get_system_db()
    try:
        user = get_current_user(request=req, authorization=f"Bearer {token}", conn=conn)
    finally:
        conn.close()

    assert isinstance(user, PRINCIPAL_TYPES)
    assert getattr(req.state, "chat_session_id", None) == session_id


def test_broker_deleted_agent_fails_closed(e2e_env):
    """A session attributed to a deleted agent must NOT fall through to the
    owner's full-authority identity JWT — 401 instead (fail closed,
    mirroring pat_resolver)."""
    import pytest
    from fastapi import HTTPException

    from app.api.broker import _mint_identity_jwt
    from src.repositories import agents_repo

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id, tables_mode="selected")
    session_id = _make_session(owner_email, agent_id=agent_id)
    agents_repo().soft_delete(agent_id)

    with pytest.raises(HTTPException) as exc:
        _mint_identity_jwt(session_id)
    assert exc.value.status_code == 401
    assert exc.value.detail == "ticket_agent_not_found"


def test_broker_misconfigured_agent_mode_takes_enforced_path(e2e_env):
    """An unknown mode value is NOT a passthrough — it must mint the
    enforced agent-session JWT, never the owner's plain identity."""
    from app.api.broker import _mint_identity_jwt
    from src.repositories import agents_repo

    owner_id, owner_email = _make_user()
    agent_id = _make_agent(owner_id)
    conn_agent = agents_repo().get_by_id(agent_id)
    assert conn_agent is not None
    agents_repo().update(agent_id, tables_mode="restricted")
    session_id = _make_session(owner_email, agent_id=agent_id)

    payload = verify_token(_mint_identity_jwt(session_id))
    assert payload["typ"] == "agent_session"
