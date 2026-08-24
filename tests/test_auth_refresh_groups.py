"""Tests for the ``POST /auth/refresh-groups`` endpoint and the underlying
``app.auth.group_sync.apply_user_groups`` extraction.

Covers the post-login refresh path: a CLI / PAT-authenticated caller
re-syncs their Workspace group memberships without a browser round-trip.
Mirrors the OAuth callback's write path so post-OAuth-callback refreshes
are byte-identical to a fresh sign-in.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch, shared_app):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db

    close_system_db()

    app = shared_app
    yield TestClient(app)
    close_system_db()


def _create_user(client: TestClient, email: str = "alice@example.com") -> tuple[str, dict]:
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


def _set_fetch(monkeypatch, groups: list[str]) -> None:
    """Stub out the Admin SDK fetch with a fixed group list."""
    import app.auth.group_sync as gs_mod

    monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: list(groups))


def _synced_names(user_id: str) -> set[str]:
    from src.db import get_system_db

    conn = get_system_db()
    try:
        rows = conn.execute(
            "SELECT g.name FROM user_group_members m "
            "JOIN user_groups g ON g.id = m.group_id "
            "WHERE m.user_id = ? AND m.source = 'google_sync'",
            [user_id],
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


class TestEndpointGuard:
    def test_unauthenticated_returns_401(self, client):
        r = client.post("/auth/refresh-groups")
        assert r.status_code == 401


class TestApplyUserGroups:
    """Direct tests against the extracted ``apply_user_groups`` function —
    confirms the OAuth callback's policy logic (prefix filter, denied
    semantics, fail-soft on empty fetch) survives the extraction."""

    def test_applies_when_groups_match(self, client, monkeypatch):
        user_id, _ = _create_user(client)
        _set_fetch(monkeypatch, ["team@example.com", "eng@example.com"])

        from app.auth.group_sync import apply_user_groups
        from src.db import get_system_db

        conn = get_system_db()
        try:
            result = apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()

        assert result.applied is True
        assert result.denied is False
        assert result.soft_failed is False
        assert set(result.fetched) == {"team@example.com", "eng@example.com"}
        assert _synced_names(user_id) == {"team@example.com", "eng@example.com"}

    def test_soft_failed_on_empty_fetch_preserves_snapshot(self, client, monkeypatch):
        user_id, _ = _create_user(client)
        # Seed an existing google_sync row so we can assert it survives.
        _set_fetch(monkeypatch, ["existing@example.com"])
        from app.auth.group_sync import apply_user_groups
        from src.db import get_system_db

        conn = get_system_db()
        try:
            apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()
        assert _synced_names(user_id) == {"existing@example.com"}

        _set_fetch(monkeypatch, [])
        conn = get_system_db()
        try:
            result = apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()
        assert result.soft_failed is True
        assert result.applied is False
        assert _synced_names(user_id) == {"existing@example.com"}

    def test_denied_when_prefix_excludes_all_groups(self, client, monkeypatch):
        user_id, _ = _create_user(client)
        monkeypatch.setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(monkeypatch, ["other@example.com", "drinks@example.com"])

        from app.auth.group_sync import apply_user_groups
        from src.db import get_system_db

        conn = get_system_db()
        try:
            result = apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()
        assert result.denied is True
        assert result.applied is False
        assert _synced_names(user_id) == set()

    def test_prefix_filter_drops_non_matching(self, client, monkeypatch):
        user_id, _ = _create_user(client)
        monkeypatch.setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(
            monkeypatch,
            [
                "grp_acme_eng@example.com",
                "grp_acme_finance@example.com",
                "drinks@example.com",
            ],
        )

        from app.auth.group_sync import apply_user_groups
        from src.db import get_system_db

        conn = get_system_db()
        try:
            result = apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()
        assert result.applied is True
        assert _synced_names(user_id) == {
            "grp_acme_eng@example.com",
            "grp_acme_finance@example.com",
        }

    def test_resync_preserves_system_seed_everyone_row(self, client, monkeypatch):
        """Issue #748: the creation-time Everyone grant (source='system_seed')
        must survive an ``apply_user_groups`` re-sync — ``replace_google_sync_groups``
        only touches ``source='google_sync'`` rows for this user."""
        from app.auth.group_sync import ensure_everyone_membership
        from src.db import SYSTEM_EVERYONE_GROUP

        user_id, _ = _create_user(client)
        ensure_everyone_membership(user_id, added_by="test:seed")

        _set_fetch(monkeypatch, ["team@example.com"])
        from app.auth.group_sync import apply_user_groups
        from src.db import get_system_db

        conn = get_system_db()
        try:
            result = apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()

        assert result.applied is True
        from src.repositories import user_group_members_repo

        rows = user_group_members_repo().list_groups_with_meta_for_user(user_id)
        everyone_rows = [r for r in rows if r["name"] == SYSTEM_EVERYONE_GROUP]
        assert len(everyone_rows) == 1
        assert everyone_rows[0]["source"] == "system_seed"

    def test_opt_out_from_everyone_not_reasserted_by_resync(self, client, monkeypatch):
        """Issue #748: if an admin removes a user's Everyone membership,
        a subsequent Google re-sync must NOT re-add it — the creation-time
        grant is not re-asserted at login."""
        from app.auth.group_sync import ensure_everyone_membership
        from src.db import SYSTEM_EVERYONE_GROUP
        from src.repositories import user_group_members_repo, user_groups_repo

        user_id, _ = _create_user(client)
        ensure_everyone_membership(user_id, added_by="test:seed")

        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        user_group_members_repo().remove_member(user_id, everyone["id"])

        _set_fetch(monkeypatch, ["team@example.com"])
        from app.auth.group_sync import apply_user_groups
        from src.db import get_system_db

        conn = get_system_db()
        try:
            apply_user_groups(user_id, "alice@example.com", conn)
        finally:
            conn.close()

        rows = user_group_members_repo().list_groups_with_meta_for_user(user_id)
        assert not any(r["name"] == SYSTEM_EVERYONE_GROUP for r in rows), (
            "Everyone must NOT be re-added by a Google sync re-run after "
            "an admin removed it — the creation-time grant is not re-asserted"
        )


class TestRefreshGroupsEndpoint:
    def test_applies_and_reports_added(self, client, monkeypatch):
        user_id, headers = _create_user(client)
        _set_fetch(monkeypatch, ["team@example.com", "eng@example.com"])

        r = client.post("/auth/refresh-groups", headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["applied"] is True
        assert data["denied"] is False
        assert data["soft_failed"] is False
        assert set(data["added"]) == {"team@example.com", "eng@example.com"}
        assert data["removed"] == []
        assert set(data["current"]) >= {"team@example.com", "eng@example.com"}

    def test_reports_removed_when_group_dropped_upstream(self, client, monkeypatch):
        user_id, headers = _create_user(client)
        _set_fetch(monkeypatch, ["team@example.com", "eng@example.com"])
        client.post("/auth/refresh-groups", headers=headers)

        # Upstream now reports only one of them; the other should be removed.
        _set_fetch(monkeypatch, ["team@example.com"])
        r = client.post("/auth/refresh-groups", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["applied"] is True
        assert data["added"] == []
        assert data["removed"] == ["eng@example.com"]

    def test_idempotent_when_no_change(self, client, monkeypatch):
        user_id, headers = _create_user(client)
        _set_fetch(monkeypatch, ["team@example.com"])
        client.post("/auth/refresh-groups", headers=headers)

        r = client.post("/auth/refresh-groups", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["applied"] is True
        assert data["added"] == []
        assert data["removed"] == []

    def test_denied_when_prefix_filter_excludes_everything(self, client, monkeypatch):
        user_id, headers = _create_user(client)
        monkeypatch.setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(monkeypatch, ["other@example.com"])

        r = client.post("/auth/refresh-groups", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["denied"] is True
        assert data["applied"] is False
        assert data["added"] == []
        assert data["removed"] == []

    def test_soft_failed_when_fetch_empty(self, client, monkeypatch):
        user_id, headers = _create_user(client)
        _set_fetch(monkeypatch, [])

        r = client.post("/auth/refresh-groups", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["soft_failed"] is True
        assert data["applied"] is False
