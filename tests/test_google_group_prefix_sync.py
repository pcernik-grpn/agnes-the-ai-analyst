"""Tests for the Google-prefix mapping + system-group routing.

Covers:
- prefix filter (only `grp_acme_*` rows survive into user_group_members)
- login gate (302 when prefix is set and no Workspace group matches)
- system-group mapping (admin/everyone Workspace email → seeded
  Admin/Everyone row instead of a fresh user_groups insert)
- idempotency (second login produces the same memberships)
- API guard `_is_google_managed` + 409 google_managed_readonly
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def google_callback_env(tmp_path, monkeypatch, shared_app):
    """TestClient for the Google callback wired against monkeypatched OAuth.

    Patches `is_available`, `oauth.google.authorize_access_token`, and
    `app.auth.group_sync.fetch_user_groups` so no real network traffic is
    required. The callback's domain check accepts `tester@example.com`
    because no `allowed_domains` is configured by default in tests.

    Per-test setup: monkeypatch the prefix/admin/everyone env vars and the
    `fetch_user_groups` return value before issuing the callback request.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

    import app.auth.providers.google as g_mod

    monkeypatch.setattr(g_mod, "is_available", lambda: True)
    fake_oauth_google = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={
                "userinfo": {
                    "email": "tester@example.com",
                    "name": "Tester",
                }
            }
        )
    )
    monkeypatch.setattr(g_mod.oauth, "google", fake_oauth_google, raising=False)

    app = shared_app
    return {
        "client": TestClient(app, follow_redirects=False),
        "monkeypatch": monkeypatch,
        "g_mod": g_mod,
    }


def _set_fetch(monkeypatch, groups):
    import app.auth.group_sync as gs_mod

    monkeypatch.setattr(gs_mod, "fetch_user_groups", lambda email: list(groups))


def _system_db():
    from src.db import get_system_db

    return get_system_db()


class TestAutoEveryoneAtFirstSignIn:
    """Issue #748: a brand-new Google sign-in must land the user in the
    seeded Everyone group (source='system_seed') when
    AGNES_GROUP_EVERYONE_EMAIL is unset — restoring the pre-PR#131 default
    for the common (non-Workspace-mapped-Everyone) deployment shape.
    """

    def test_new_user_gets_everyone_system_seed_row(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        env["monkeypatch"].delenv("AGNES_GOOGLE_GROUP_PREFIX", raising=False)
        _set_fetch(env["monkeypatch"], [])

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            assert user is not None

            everyone = UserGroupsRepository(conn).get_by_name("Everyone")
            assert everyone is not None

            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            matching = [r for r in rows if r["group_id"] == everyone["id"]]
            assert len(matching) == 1, f"expected exactly one Everyone row, got {matching}"
            assert matching[0]["source"] == "system_seed"
        finally:
            conn.close()

    def test_existing_user_relogin_does_not_gain_everyone_via_this_path(self, google_callback_env):
        """The helper only fires in the new-user branch — a second login
        for the SAME user must not re-trigger the grant call (idempotent
        either way, but this asserts the call-site placement: only one
        Everyone row, not one per login)."""
        env = google_callback_env
        env["monkeypatch"].delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        _set_fetch(env["monkeypatch"], [])

        env["client"].get("/auth/google/callback?code=x&state=y")
        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            everyone_rows = [r for r in rows if r["name"] == "Everyone"]
            assert len(everyone_rows) == 1
        finally:
            conn.close()


class TestPrefixFilter:
    def test_prefix_filter_keeps_only_matching_groups(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(
            env["monkeypatch"],
            [
                "grp_acme_finance@example.com",
                "grp_acme_eng@example.com",
                "grp_other@example.com",
                "acme-everyone@example.com",
                "drinks@example.com",
            ],
        )

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            assert user is not None

            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            ug = UserGroupsRepository(conn)
            names = sorted(ug.get(gid)["name"] for gid in group_ids)
            # Everyone (system_seed, issue #748 auto-grant-at-creation)
            # plus the two prefix-matched google_sync groups.
            assert names == [
                "Everyone",
                "grp_acme_eng@example.com",
                "grp_acme_finance@example.com",
            ]
            for n in names:
                if n == "Everyone":
                    continue
                assert ug.get_by_name(n)["created_by"] == "system:google-sync"
        finally:
            conn.close()

    def test_prefix_set_no_match_redirects_to_login_error(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(
            env["monkeypatch"],
            [
                "drinks@example.com",
                "acme-everyone@example.com",
            ],
        )

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        # Bare RedirectResponse defaults to 307 (matches the other error
        # redirects in google.py — domain_not_allowed, oauth_failed, etc.).
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/login?error=not_in_allowed_group"

        # No google_sync group memberships were written (the gate fired
        # before replace_google_sync_groups). The user row may exist
        # because user creation happens before the gate — that's the
        # documented behavior; admins can mark the row inactive if they
        # want a hard block. The Everyone system_seed grant (issue #748)
        # is part of that same creation-time step and is likewise
        # unaffected by the later deny gate.
        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            if user:
                groups = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
                assert [g["name"] for g in groups] == ["Everyone"]
                assert groups[0]["source"] == "system_seed"
        finally:
            conn.close()

    def test_no_prefix_means_legacy_behavior(self, google_callback_env):
        """Without AGNES_GOOGLE_GROUP_PREFIX, every fetched group is mirrored."""
        env = google_callback_env
        env["monkeypatch"].delenv("AGNES_GOOGLE_GROUP_PREFIX", raising=False)
        _set_fetch(
            env["monkeypatch"],
            [
                "grp_a@example.com",
                "grp_b@example.com",
            ],
        )

        resp = env["client"].get("/auth/google/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            names = sorted(UserGroupsRepository(conn).get(gid)["name"] for gid in group_ids)
            # Everyone (system_seed, issue #748) plus the two mirrored groups.
            assert names == ["Everyone", "grp_a@example.com", "grp_b@example.com"]
        finally:
            conn.close()


class TestSystemMapping:
    def test_admin_email_routes_to_seeded_admin_row(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        env["monkeypatch"].setenv("AGNES_GROUP_ADMIN_EMAIL", "grp_acme_admin@example.com")
        _set_fetch(
            env["monkeypatch"],
            [
                "grp_acme_admin@example.com",
                "grp_acme_finance@example.com",
            ],
        )

        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            ug = UserGroupsRepository(conn)
            # Crucially: no separate user_groups row was created with the
            # full admin email as `name`. Membership lands in the seeded
            # Admin row instead.
            assert ug.get_by_name("grp_acme_admin@example.com") is None

            admin_row = ug.get_by_name("Admin")
            assert admin_row is not None and admin_row["is_system"] is True

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            assert admin_row["id"] in group_ids

            # Finance group still goes through ensure() and creates a fresh row.
            finance = ug.get_by_name("grp_acme_finance@example.com")
            assert finance is not None
            assert finance["is_system"] is False
            assert finance["created_by"] == "system:google-sync"
            assert finance["id"] in group_ids
        finally:
            conn.close()

    def test_everyone_email_routes_to_seeded_everyone_row(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        env["monkeypatch"].setenv("AGNES_GROUP_EVERYONE_EMAIL", "grp_acme_everyone@example.com")
        _set_fetch(
            env["monkeypatch"],
            [
                "grp_acme_everyone@example.com",
            ],
        )

        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            ug = UserGroupsRepository(conn)
            assert ug.get_by_name("grp_acme_everyone@example.com") is None

            everyone_row = ug.get_by_name("Everyone")
            assert everyone_row is not None
            assert everyone_row["is_system"] is True

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            assert everyone_row["id"] in group_ids
        finally:
            conn.close()

    def test_everyone_email_set_first_signin_has_no_system_seed_row(self, google_callback_env):
        """Issue #748 dual-mode, Workspace-controlled half: when
        AGNES_GROUP_EVERYONE_EMAIL is set, a brand-new user's Everyone
        membership must come ONLY from google_sync (the row asserted in
        test_everyone_email_routes_to_seeded_everyone_row above) — the
        auto-grant-at-creation helper (app.auth.group_sync.ensure_everyone_membership)
        must no-op and NOT add a competing system_seed row.
        """
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GROUP_EVERYONE_EMAIL", "grp_acme_everyone@example.com")
        # No prefix and no matching group in the fetch — isolates the
        # creation-time helper from the google_sync write path entirely,
        # so any Everyone row found afterward must have come from the
        # (should-be-skipped) auto-grant helper.
        _set_fetch(env["monkeypatch"], ["unrelated@example.com"])

        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            assert user is not None
            everyone_row = UserGroupsRepository(conn).get_by_name("Everyone")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            assert everyone_row["id"] not in group_ids, (
                "ensure_everyone_membership must no-op when AGNES_GROUP_EVERYONE_EMAIL is set"
            )
        finally:
            conn.close()


class TestIdempotency:
    def test_second_login_does_not_duplicate_groups(self, google_callback_env):
        env = google_callback_env
        env["monkeypatch"].setenv("AGNES_GOOGLE_GROUP_PREFIX", "grp_acme_")
        _set_fetch(
            env["monkeypatch"],
            [
                "grp_acme_finance@example.com",
            ],
        )

        env["client"].get("/auth/google/callback?code=x&state=y")
        env["client"].get("/auth/google/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.users import UserRepository
            from src.repositories.user_group_members import (
                UserGroupMembersRepository,
            )

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            # Exactly two memberships: the prefix-matched google_sync group
            # (deduplicated by the (user_id, group_id) PK in
            # user_group_members) plus the Everyone system_seed row granted
            # once at creation (issue #748) — the second login does not
            # re-invoke the creation-time grant.
            assert len(group_ids) == 2

            # Exactly one user_groups row for that name (ensure() is
            # get-or-create, the second login picks up the existing row).
            count = conn.execute(
                "SELECT COUNT(*) FROM user_groups WHERE name = ?",
                ["grp_acme_finance@example.com"],
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestIsGoogleManagedFlag:
    """Exercises the `_is_google_managed` rule used by GroupResponse +
    the API guard."""

    def test_google_sync_row_is_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from app.api.access import _is_google_managed

        g = {
            "name": "grp_acme_x@example.com",
            "is_system": False,
            "created_by": "system:google-sync",
        }
        assert _is_google_managed(g) is True

    def test_system_admin_with_env_mapping_is_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "grp_acme_admin@example.com")
        from app.api.access import _is_google_managed

        g = {"name": "Admin", "is_system": True, "created_by": "system:seed"}
        assert _is_google_managed(g) is True

    def test_system_admin_without_env_mapping_is_not_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("AGNES_GROUP_ADMIN_EMAIL", raising=False)
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        from app.api.access import _is_google_managed

        g = {"name": "Admin", "is_system": True, "created_by": "system:seed"}
        assert _is_google_managed(g) is False

    def test_manual_custom_group_is_not_managed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        from app.api.access import _is_google_managed

        g = {
            "name": "data-team",
            "is_system": False,
            "created_by": "alice@example.com",
        }
        assert _is_google_managed(g) is False


class TestApiGuard:
    """API endpoints reject mutations on Google-managed groups with 409."""

    @pytest.fixture
    def admin_client(self, tmp_path, monkeypatch, shared_app):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")
        monkeypatch.setenv("AGNES_GROUP_ADMIN_EMAIL", "grp_acme_admin@example.com")

        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.user_group_members import (
            UserGroupMembersRepository,
        )
        from app.auth.jwt import create_access_token

        conn = get_system_db()
        try:
            ur = UserRepository(conn)
            ur.create(id="admin1", email="admin@x", name="Admin1")
            ur.create(id="u1", email="u1@x", name="U1")
            ug = UserGroupsRepository(conn)
            admin_id = ug.get_by_name("Admin")["id"]
            UserGroupMembersRepository(conn).add_member(
                "admin1",
                admin_id,
                source="system_seed",
            )
            # A google-sync group to act on.
            ug.ensure("grp_acme_finance@example.com")
        finally:
            conn.close()

        app = shared_app
        client = TestClient(app, follow_redirects=False)
        token = create_access_token("admin1", "admin@x")
        client.cookies.set("access_token", token)
        return client

    def _gid(self, name):
        from src.db import get_system_db
        from src.repositories.user_groups import UserGroupsRepository

        conn = get_system_db()
        try:
            return UserGroupsRepository(conn).get_by_name(name)["id"]
        finally:
            conn.close()

    def test_patch_google_managed_returns_409(self, admin_client):
        gid = self._gid("grp_acme_finance@example.com")
        r = admin_client.patch(
            f"/api/admin/groups/{gid}",
            json={"name": "renamed"},
        )
        assert r.status_code == 409
        body = r.json()
        # FastAPI wraps the dict detail under "detail"; assert the code is
        # surfaced for the UI's machine-readable branch.
        assert body["detail"]["code"] == "google_managed_readonly"

    def test_delete_google_managed_returns_409(self, admin_client):
        gid = self._gid("grp_acme_finance@example.com")
        r = admin_client.delete(f"/api/admin/groups/{gid}")
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "google_managed_readonly"

    def test_add_member_to_google_managed_returns_409(self, admin_client):
        gid = self._gid("grp_acme_finance@example.com")
        r = admin_client.post(
            f"/api/admin/groups/{gid}/members",
            json={"email": "u1@x"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "google_managed_readonly"

    def test_patch_admin_with_env_mapping_returns_409(self, admin_client):
        # AGNES_GROUP_ADMIN_EMAIL is set in the fixture → seeded Admin row
        # is treated as Google-managed and rejects renames here too.
        gid = self._gid("Admin")
        r = admin_client.patch(
            f"/api/admin/groups/{gid}",
            json={"description": "updated"},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "google_managed_readonly"
