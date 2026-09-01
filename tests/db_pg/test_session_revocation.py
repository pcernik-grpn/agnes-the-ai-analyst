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


# ---------------------------------------------------------------------------
# Password change / reset also bump the floor (issue #1676 remainder) —
# exercised end-to-end through the real HTTP endpoints (CSRF + cookie
# exchange included), unlike the lower-level tests above.
# ---------------------------------------------------------------------------


def _client():
    """Fresh TestClient over the app the ``_env``/``state_backend`` fixture
    just configured. Mirrors the app-construction tail of
    ``tests/db_pg/_parity_sweep_util.build_seeded_client`` without its
    admin/analyst seeding, which these tests don't need."""
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests._backend_pin import reregister_requires_pg_handler

    app = create_app()
    reregister_requires_pg_handler(app)
    return TestClient(app)


def _seed_password_user(user_id: str, email: str, password: str) -> None:
    from argon2 import PasswordHasher
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=email, name="U", password_hash=PasswordHasher().hash(password))


def _csrf_token(client, session_token: str) -> str:
    import re

    resp = client.get("/auth/password/change", headers={"Authorization": f"Bearer {session_token}"})
    assert resp.status_code == 200, resp.text
    m = re.search(r'data-csrf="([^"]+)"', resp.text)
    assert m, resp.text
    return m.group(1)


def test_password_change_revokes_other_sessions_but_mints_a_working_one(_env):
    """``POST /auth/password/change`` bumps the same floor ``POST
    /auth/logout`` does, so an OLD copy of the caller's session token (a
    second device, a synced browser profile) stops working on PG — but the
    CURRENT caller must not be locked out by their own request: the response
    mints a fresh session cookie, created after the floor, that keeps
    working on both backends."""
    from app.auth.jwt import create_access_token

    client = _client()
    _seed_password_user("pw-change-1", "pwchange@example.com", "orig-password-123")

    # Stands in for a different device/tab holding a copy of the
    # about-to-be-superseded session. `iat` is whole-second floored, so it
    # must land in an earlier second than the revoke call below — same
    # reasoning as test_a_fresh_login_after_revoke_still_works above.
    old_token = create_access_token("pw-change-1", "pwchange@example.com")
    time.sleep(1.1)

    csrf = _csrf_token(client, old_token)
    resp = client.post(
        "/auth/password/change",
        json={"current_password": "orig-password-123", "new_password": "brand-new-password-456"},
        headers={"Authorization": f"Bearer {old_token}", "X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.text

    # The response mints a fresh `access_token` cookie for the SAME caller —
    # the TestClient persists it, so the next call authenticates with no
    # header at all, on EITHER backend.
    assert client.cookies.get("access_token"), "password change must mint a fresh session cookie"
    cookie_auth = client.get("/auth/password/change")
    assert cookie_auth.status_code == 200, cookie_auth.text

    # The OLD token is a different story per backend.
    old_reused = client.get("/auth/password/change", headers={"Authorization": f"Bearer {old_token}"})
    if _env == "pg":
        assert old_reused.status_code == 401, old_reused.text
    else:
        assert old_reused.status_code == 200, "DuckDB has no revocation column — must stay a no-op"


def test_reset_confirm_revokes_prior_sessions(_env):
    """Completing a password RESET also bumps the floor. The account was
    not "logged in" through this flow, so there is no current session to
    preserve — the resetting browser just gets an ordinary fresh login
    cookie, same as before this change — but every OTHER copy of an old
    session token for the account is cut on PG."""
    from datetime import datetime, timezone

    from app.auth.jwt import create_access_token
    from app.auth.token_hash import hash_token
    from src.repositories import users_repo

    client = _client()
    _seed_password_user("pw-reset-1", "pwreset@example.com", "orig-password-123")

    old_token = create_access_token("pw-reset-1", "pwreset@example.com")
    time.sleep(1.1)

    users_repo().update(
        id="pw-reset-1",
        reset_token=hash_token("reset-tok-plain"),
        reset_token_created=datetime.now(timezone.utc),
    )

    resp = client.post(
        "/auth/password/reset/confirm",
        data={
            "email": "pwreset@example.com",
            "token": "reset-tok-plain",
            "password": "reset-new-password-1",
            "confirm_password": "reset-new-password-1",
        },
    )
    assert resp.status_code == 200, resp.text  # followed the redirect to /login/password
    assert client.cookies.get("access_token"), "a completed reset must sign the user in"

    old_reused = client.get("/auth/password/change", headers={"Authorization": f"Bearer {old_token}"})
    if _env == "pg":
        assert old_reused.status_code == 401, old_reused.text
    else:
        assert old_reused.status_code == 200, "DuckDB has no revocation column — must stay a no-op"
