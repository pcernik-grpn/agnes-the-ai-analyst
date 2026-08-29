"""Server-side session revocation — issue #1676.

``app/auth/pat_resolver.resolve_token_to_user`` used to trust a
``typ="session"`` JWT off signature + ``exp`` alone: no DB-backed check ever
ran for it, so nothing short of deactivating the whole account could end an
individual session early. ``users.session_revoked_before`` (PG-only column,
A3 ratchet) is the fix — a per-user timestamp floor compared against the
token's ``iat``, bumped by ``users_repo().revoke_sessions(...)`` (what
``POST /auth/logout`` calls).

Runs on both backends via ``state_backend`` (mirrors
``test_parity_co_session_resolution.py``) so the DUCKDB SIDE OF THE
ASYMMETRY IS A CHECKED FACT, not an assumption: DuckDB has no such column
(frozen post-A3 schema) and ``revoke_sessions()`` is a documented no-op
there, so a revoked user's OLD token keeps resolving on that backend — the
tests assert both outcomes explicitly rather than skipping the DuckDB half.
"""

from __future__ import annotations

import time

import pytest

_SECRET = "test-secret-key-minimum-32-characters!!"


@pytest.fixture
def _env(state_backend, tmp_path, monkeypatch):
    """DATA_DIR + JWT secret + (DuckDB) fresh system DB, for either backend."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", _SECRET)
    for sub in ("extracts", "analytics", "state", "notifications"):
        (tmp_path / sub).mkdir(exist_ok=True)
    if state_backend == "duckdb":
        from src.db import close_system_db, get_system_db

        close_system_db()
        get_system_db()  # triggers _ensure_schema + _seed_system_groups
    return state_backend


def _mint_and_seed_user(user_id: str = "u1", email: str = "u1@example.com") -> str:
    from app.auth.jwt import create_access_token
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=email, name="U")
    return create_access_token(user_id, email)


def test_live_session_token_resolves_on_both_backends(_env):
    from app.auth.pat_resolver import resolve_token_to_user

    tok = _mint_and_seed_user()
    user, reason = resolve_token_to_user(None, tok)
    assert reason is None, f"unexpected reject on {_env}: {reason}"
    assert user is not None
    assert user["id"] == "u1"


def test_revoke_sessions_effect_differs_by_backend(_env):
    """The whole point of the A3-ratchet trade-off, pinned as a fact: PG
    enforces the floor and refuses the old token; DuckDB has no revocation
    column and keeps accepting it (documented no-op, not silently wrong —
    see CHANGELOG.md and src/repositories/users.py::revoke_sessions)."""
    from app.auth.pat_resolver import resolve_token_to_user
    from src.repositories import users_repo

    tok = _mint_and_seed_user()
    user, reason = resolve_token_to_user(None, tok)
    assert reason is None and user is not None

    # The comparison floors both sides to whole seconds (see pat_resolver's
    # `session_revoked_before` check) so a same-SECOND re-login is not
    # spuriously rejected — realistic here too: a real session's `iat` is
    # from whenever the user originally logged in, essentially never the
    # same wall-clock second as the later logout click.
    time.sleep(1.1)
    users_repo().revoke_sessions("u1")

    user2, reason2 = resolve_token_to_user(None, tok)
    if _env == "pg":
        assert user2 is None
        assert reason2 == "session_revoked"
    else:
        assert reason2 is None, "DuckDB has no revocation column — must stay a no-op, not error"
        assert user2 is not None


def test_a_fresh_login_after_revoke_still_works(_env):
    """The floor only blocks tokens minted BEFORE the revoke — a legitimate
    re-login (e.g. right after clicking Logout) must not be locked out."""
    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    from src.repositories import users_repo

    old_tok = _mint_and_seed_user()
    # The comparison floors both sides to whole seconds, so the OLD token's
    # `iat` must land in an earlier second than the revoke call — realistic
    # (a real session was minted well before the later logout click).
    time.sleep(1.1)
    users_repo().revoke_sessions("u1")

    # Ensure the new token's floored `iat` (whole-second precision) lands in
    # a later second than the revoke call.
    time.sleep(1.1)
    new_tok = create_access_token("u1", "u1@example.com")

    old_user, old_reason = resolve_token_to_user(None, old_tok)
    new_user, new_reason = resolve_token_to_user(None, new_tok)

    if _env == "pg":
        assert old_reason == "session_revoked"
        assert old_user is None
    else:
        assert old_reason is None  # DuckDB no-op

    assert new_reason is None, f"a fresh post-revoke login must still work on {_env}: {new_reason}"
    assert new_user is not None


def test_non_session_typ_tokens_are_unaffected_by_the_floor(_env):
    """PAT / agent_pat run their OWN DB-backed validity chain
    (`personal_access_tokens.revoked_at`); the session floor must not touch
    them — `resolve_token_to_user` should not even read
    ``session_revoked_before`` for a `typ="pat"` token."""
    import hashlib
    import uuid

    from app.auth.jwt import create_access_token
    from app.auth.pat_resolver import resolve_token_to_user
    from src.repositories import access_token_repo, users_repo

    users_repo().create(id="u2", email="u2@example.com", name="U2")
    users_repo().revoke_sessions("u2")  # bump the floor to "now" first

    tid = str(uuid.uuid4())
    pat = create_access_token("u2", "u2@example.com", token_id=tid, typ="pat", omit_exp=True)
    access_token_repo().create(
        id=tid,
        user_id="u2",
        name="t",
        token_hash=hashlib.sha256(pat.encode()).hexdigest(),
        prefix=tid[:8],
    )

    user, reason = resolve_token_to_user(None, pat)
    assert reason is None, f"a PAT minted after the session floor must not be caught by it on {_env}: {reason}"
    assert user is not None
