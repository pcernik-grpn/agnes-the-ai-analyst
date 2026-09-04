"""Tests for the Microsoft Entra ID group sync via /auth/microsoft/callback.

Mirrors tests/test_google_group_prefix_sync.py's structure and mocking
style (fake OAuth token exchange + monkeypatched fetch). Covers:

- disabled by default: no Graph call, sign-in unaffected
- group creation + source='microsoft_sync' on sign-in
- prefix filter (only matching groups survive; legacy "mirror everything"
  when unset)
- login gate when the prefix is set and nothing matches
- a Graph failure never fails login (fail-soft)
- idempotency (second login does not duplicate)
- an admin-added membership survives a resync
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def microsoft_callback_env(tmp_path, monkeypatch, shared_app):
    """TestClient for the Microsoft callback wired against monkeypatched OAuth.

    Patches `is_available`, `oauth.microsoft.authorize_access_token`
    (returning both `userinfo` and an `access_token`, since group sync needs
    the delegated token) so no real network traffic is required. The
    callback's domain check accepts `tester@example.com` because no
    `allowed_domains` is configured by default in tests.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-32chars-minimum!!!!!")

    import app.auth.providers.microsoft as ms_mod

    monkeypatch.setattr(ms_mod, "is_available", lambda: True)
    fake_oauth_microsoft = SimpleNamespace(
        authorize_access_token=AsyncMock(
            return_value={
                "access_token": "fake-graph-access-token",
                "userinfo": {
                    "email": "tester@example.com",
                    "name": "Tester",
                },
            }
        )
    )
    monkeypatch.setattr(ms_mod.oauth, "microsoft", fake_oauth_microsoft, raising=False)

    app = shared_app
    return {
        "client": TestClient(app, follow_redirects=False),
        "monkeypatch": monkeypatch,
        "ms_mod": ms_mod,
    }


def _enable_sync(monkeypatch):
    monkeypatch.setenv("AGNES_MICROSOFT_GROUP_SYNC_ENABLED", "true")


def _group(object_id: str, mail: str = "", display_name: str = "") -> dict:
    return {"id": object_id, "mail": mail, "displayName": display_name}


def _set_fetch(monkeypatch, groups):
    import app.auth.microsoft_group_sync as mgs

    monkeypatch.setattr(mgs, "fetch_user_groups", lambda token: list(groups))


def _system_db():
    from src.db import get_system_db

    return get_system_db()


class TestDisabledByDefault:
    def test_no_graph_call_and_no_synced_groups_when_disabled(self, microsoft_callback_env):
        """Without AGNES_MICROSOFT_GROUP_SYNC_ENABLED, the callback never
        calls fetch_user_groups, and sign-in is unaffected."""
        env = microsoft_callback_env
        env["monkeypatch"].delenv("AGNES_MICROSOFT_GROUP_SYNC_ENABLED", raising=False)

        import app.auth.microsoft_group_sync as mgs

        def boom(token):
            raise AssertionError("fetch_user_groups must not be called when sync is disabled")

        env["monkeypatch"].setattr(mgs, "fetch_user_groups", boom)

        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            assert user is not None
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            assert all(r["source"] != "microsoft_sync" for r in rows)
        finally:
            conn.close()


class TestGroupCreationAndSource:
    def test_groups_created_with_microsoft_sync_source(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        _set_fetch(
            env["monkeypatch"],
            [_group("g-eng", "eng@example.com"), _group("g-fin", "finance@example.com")],
        )

        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            assert user is not None

            ug = UserGroupsRepository(conn)
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            by_name = {r["name"]: r for r in rows}
            assert by_name["entra:g-eng"]["source"] == "microsoft_sync"
            assert by_name["entra:g-fin"]["source"] == "microsoft_sync"
            assert ug.get_by_name("entra:g-eng")["created_by"] == "system:microsoft-sync"
            # Everyone (system_seed, issue #748 auto-grant-at-creation) is
            # untouched by group sync — same shared ensure_user path Google
            # uses.
            assert by_name["Everyone"]["source"] == "system_seed"
        finally:
            conn.close()


class TestPrefixFilter:
    def test_prefix_filter_keeps_only_matching_groups(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        env["monkeypatch"].setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(
            env["monkeypatch"],
            [
                _group("g-fin", "agnes-finance@example.com"),
                _group("g-eng", "agnes-eng@example.com"),
                _group("g-other", "other-team@example.com"),
            ],
        )

        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            ug = UserGroupsRepository(conn)
            names = sorted(ug.get(gid)["name"] for gid in group_ids)
            assert names == [
                "Everyone",
                "entra:g-eng",
                "entra:g-fin",
            ]
        finally:
            conn.close()

    def test_no_prefix_means_every_fetched_group_mirrored(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        env["monkeypatch"].delenv("AGNES_MICROSOFT_GROUP_PREFIX", raising=False)
        _set_fetch(env["monkeypatch"], [_group("g-a", "grp-a@example.com"), _group("g-b", "grp-b@example.com")])

        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            names = sorted(UserGroupsRepository(conn).get(gid)["name"] for gid in group_ids)
            assert names == ["Everyone", "entra:g-a", "entra:g-b"]
        finally:
            conn.close()

    def test_prefix_set_no_match_redirects_to_login_error(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        env["monkeypatch"].setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(env["monkeypatch"], [_group("g-other", "other-team@example.com")])

        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/login?error=microsoft_not_in_allowed_group"

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            if user:
                rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
                # User creation + Everyone auto-grant happens before the
                # deny gate — same documented behavior as the Google
                # callback — but no microsoft_sync row is written.
                assert [r["name"] for r in rows] == ["Everyone"]
        finally:
            conn.close()


class TestGraphFailureNeverFailsLogin:
    def test_graph_exception_still_logs_in(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        import app.auth.microsoft_group_sync as mgs

        def boom(token):
            raise RuntimeError("Graph is unreachable")

        env["monkeypatch"].setattr(mgs, "fetch_user_groups", boom)

        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302
        assert resp.headers["location"] == "/dashboard"
        assert "access_token" in resp.cookies


class TestIdempotency:
    def test_second_login_does_not_duplicate_groups(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        env["monkeypatch"].setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(env["monkeypatch"], [_group("g-fin", "agnes-finance@example.com")])

        env["client"].get("/auth/microsoft/callback?code=x&state=y")
        env["client"].get("/auth/microsoft/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            group_ids = UserGroupMembersRepository(conn).list_groups_for_user(user["id"])
            # Everyone (system_seed) + one microsoft_sync group, not
            # duplicated by the second login.
            assert len(group_ids) == 2

            count = conn.execute(
                "SELECT COUNT(*) FROM user_groups WHERE name = ?",
                ["entra:g-fin"],
            ).fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestAdminMembershipSurvivesResync:
    def test_admin_added_group_untouched_by_microsoft_sync(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])
        _set_fetch(env["monkeypatch"], [_group("g-eng", "eng@example.com")])

        # First sign-in provisions the user + entra:g-eng via sync.
        env["client"].get("/auth/microsoft/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.user_groups import UserGroupsRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            ug = UserGroupsRepository(conn)
            members = UserGroupMembersRepository(conn)
            hand_added = ug.create(name="hand-picked-group", created_by="admin@example.com")
            members.add_member(user["id"], hand_added["id"], source="admin")
        finally:
            conn.close()

        # Second sign-in re-syncs Microsoft groups — must not touch the
        # admin-added row.
        env["client"].get("/auth/microsoft/callback?code=x&state=y")

        conn = _system_db()
        try:
            from src.repositories.user_group_members import UserGroupMembersRepository
            from src.repositories.users import UserRepository

            user = UserRepository(conn).get_by_email("tester@example.com")
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            by_name = {r["name"]: r["source"] for r in rows}
            assert by_name["hand-picked-group"] == "admin"
            assert by_name["entra:g-eng"] == "microsoft_sync"
        finally:
            conn.close()


class TestLegacyKeyMigration:
    """2026-09 identity-scheme fix: a pre-fix install has groups keyed on
    lower-cased mail/displayName. The first post-fix sync must rename that
    row in place to ``entra:<id>`` — preserving its members and grants —
    rather than creating a duplicate row."""

    def test_legacy_mail_keyed_group_is_renamed_in_place(self, microsoft_callback_env):
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])

        from src.repositories.user_group_members import UserGroupMembersRepository
        from src.repositories.user_groups import UserGroupsRepository
        from src.repositories.users import UserRepository

        conn = _system_db()
        try:
            # Seed the OLD keying this module used pre-fix: the group's
            # lower-cased mail, created by the SAME sentinel this module
            # still stamps.
            ug = UserGroupsRepository(conn)
            legacy_group = ug.create(name="eng@example.com", created_by="system:microsoft-sync")
            legacy_id = legacy_group["id"]
            # A grant a group being renamed in place must survive (proves
            # the rename, not a delete-and-recreate).
            from app.resource_types import ResourceType
            from src.repositories import file_corpora_repo, resource_grants_repo

            collection_id = file_corpora_repo().create(
                name="Legacy Grant Col", slug="legacy-grant-col", description=None, created_by="admin"
            )
            resource_grants_repo().ensure_grant(
                legacy_id, ResourceType.COLLECTION.value, collection_id, assigned_by="admin"
            )
        finally:
            conn.close()

        _set_fetch(env["monkeypatch"], [_group("g-eng", "eng@example.com")])
        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302

        conn = _system_db()
        try:
            ug = UserGroupsRepository(conn)
            renamed = ug.get_by_name("entra:g-eng")
            assert renamed is not None
            assert renamed["id"] == legacy_id, "the rename must preserve the row's id (and thus its grants)"
            assert ug.get_by_name("eng@example.com") is None, "the legacy key must no longer resolve"

            from src.repositories import resource_grants_repo

            grants = [
                g
                for g in resource_grants_repo().list_all(resource_type="collection")
                if g.get("resource_id") == collection_id
            ]
            assert any(g["group_id"] == legacy_id for g in grants), "the pre-existing grant must survive the rename"

            user = UserRepository(conn).get_by_email("tester@example.com")
            rows = UserGroupMembersRepository(conn).list_groups_with_meta_for_user(user["id"])
            assert any(r["name"] == "entra:g-eng" and r["source"] == "microsoft_sync" for r in rows)
        finally:
            conn.close()

    def test_admin_created_group_with_the_same_name_is_never_seized(self, microsoft_callback_env):
        """A group named ``eng@example.com`` an ADMIN created by hand (not
        this sync's sentinel) must never be renamed — only a row this
        module itself created is eligible for the migration rename."""
        env = microsoft_callback_env
        _enable_sync(env["monkeypatch"])

        from src.repositories.user_groups import UserGroupsRepository

        conn = _system_db()
        try:
            ug = UserGroupsRepository(conn)
            admin_group = ug.create(name="eng@example.com", created_by="admin@example.com")
            admin_group_id = admin_group["id"]
        finally:
            conn.close()

        _set_fetch(env["monkeypatch"], [_group("g-eng", "eng@example.com")])
        resp = env["client"].get("/auth/microsoft/callback?code=x&state=y")
        assert resp.status_code == 302

        conn = _system_db()
        try:
            ug = UserGroupsRepository(conn)
            untouched = ug.get_by_name("eng@example.com")
            assert untouched is not None
            assert untouched["id"] == admin_group_id
            assert untouched["created_by"] == "admin@example.com"

            synced = ug.get_by_name("entra:g-eng")
            assert synced is not None
            assert synced["id"] != admin_group_id, "sync must create its OWN row, never seize the admin's"
        finally:
            conn.close()
