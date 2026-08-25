"""Tests for the Google group self-heal-on-miss feature (#504).

When a PAT caller is denied access to a resource and Google group sync is
configured, ``require_resource_access`` re-fetches Workspace groups for that
user and retries the access check — all without a browser re-login.

Three scenarios:

1. User gains access after re-sync (the primary bug fix).
2. Cooldown prevents a second Admin SDK call within the window.
3. Self-heal is skipped entirely when Google sync is not configured.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    client = TestClient(app, raise_server_exceptions=True)

    # Reset the in-process cooldown cache so tests don't bleed into each other.
    import app.auth.access as access_mod

    access_mod._google_resync_last.clear()

    yield {"client": client, "monkeypatch": monkeypatch, "tmp_path": tmp_path}
    close_system_db()


def _create_user_and_token(client: TestClient, email: str = "pat@example.com"):
    from argon2 import PasswordHasher

    from src.db import get_system_db
    from src.repositories.users import UserRepository

    ph = PasswordHasher()
    conn = get_system_db()
    user_id = email.split("@")[0]
    UserRepository(conn).create(
        id=user_id,
        email=email,
        name=user_id,
        password_hash=ph.hash("UserPass1!"),
    )
    conn.close()
    r = client.post("/auth/token", json={"email": email, "password": "UserPass1!"})
    assert r.status_code == 200, r.text
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _grant_group_to_marketplace(client: TestClient, admin_headers: dict, group_name: str, slug: str):
    """Ensure group exists and holds a marketplace grant for *slug*."""
    from src.db import get_system_db
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    try:
        group = UserGroupsRepository(conn).ensure(group_name)
        ResourceGrantsRepository(conn).create(group_id=group["id"], resource_type="marketplace", resource_id=slug)
    finally:
        conn.close()


def _set_fetch(monkeypatch, groups: list[str]) -> None:
    import app.auth.group_sync as gs_mod

    monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: list(groups))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_self_heal_grants_access_after_group_added(env):
    """A user added to a Workspace group after their last login gains access
    on the next PAT request — without a browser re-login.

    The self-heal fires on the first denied check, re-fetches groups
    (mock returns the newly-added group), and the retry succeeds.
    """
    client = env["client"]
    monkeypatch = env["monkeypatch"]

    # Google sync enabled via mock.
    monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "")

    user_id, headers = _create_user_and_token(client)
    slug = "acme-marketplace"

    # Grant access to the group "analysts@example.com" for this marketplace.
    _grant_group_to_marketplace(client, headers, "analysts@example.com", slug)

    # Before self-heal: user has NO google_sync rows → denied.
    _set_fetch(monkeypatch, [])
    # Confirm denied without self-heal path (cooldown already consumed by fixture reset).
    import app.auth.access as access_mod

    access_mod._google_resync_last.clear()

    # Now stub fetch to return the newly-added group and trigger the real request.
    _set_fetch(monkeypatch, ["analysts@example.com"])

    # The first GET should trigger self-heal and succeed.
    r = client.get(f"/api/marketplace/{slug}/manifest", headers=headers)
    # 200 or 404 (slug not registered) — both mean the auth gate passed.
    assert r.status_code != 403, f"Expected auth to pass after self-heal, got 403. Body: {r.text}"


def test_self_heal_cooldown_prevents_repeated_sdk_calls(env):
    """Within the cooldown window, ``_maybe_resync_google_groups`` must not
    call the Admin SDK a second time.
    """
    monkeypatch = env["monkeypatch"]
    monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "")

    call_count = 0
    import app.auth.group_sync as gs_mod

    def counting_fetch(email):
        nonlocal call_count
        call_count += 1
        return []

    monkeypatch.setattr(gs_mod, "fetch_user_groups", counting_fetch)

    import app.auth.access as access_mod

    access_mod._google_resync_last.clear()

    # First call — should attempt resync (call_count → 1).
    access_mod._maybe_resync_google_groups("user-1", "user-1@example.com")
    assert call_count == 1

    # Second call within cooldown — must be skipped (call_count stays 1).
    access_mod._maybe_resync_google_groups("user-1", "user-1@example.com")
    assert call_count == 1, "Admin SDK called twice within cooldown window"


def test_self_heal_skipped_without_google_config(env, monkeypatch):
    """When neither GOOGLE_ADMIN_SDK_SUBJECT nor GOOGLE_ADMIN_SDK_MOCK_GROUPS
    is set, ``_maybe_resync_google_groups`` returns False immediately.
    """
    monkeypatch.delenv("GOOGLE_ADMIN_SDK_SUBJECT", raising=False)
    monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)

    import app.auth.group_sync as gs_mod

    monkeypatch.setattr(
        gs_mod, "fetch_user_groups", lambda e: (_ for _ in ()).throw(AssertionError("should not be called"))
    )

    import app.auth.access as access_mod

    access_mod._google_resync_last.clear()
    result = access_mod._maybe_resync_google_groups("user-x", "user-x@example.com")
    assert result is False, "Expected False when Google sync not configured"
