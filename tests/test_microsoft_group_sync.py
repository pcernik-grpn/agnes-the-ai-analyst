"""Unit tests for app.auth.microsoft_group_sync.

Mirrors tests/test_group_sync.py's structure (mock-env parsing, real-path
HTTP mocking) plus direct ``apply_user_groups`` coverage — the Google
sibling only exercises ``apply_user_groups`` indirectly, through the OAuth
callback in tests/test_google_group_prefix_sync.py; this file covers both
levels for Microsoft.
"""

from __future__ import annotations

import pytest

from src.db import get_system_db
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.user_groups import UserGroupsRepository
from src.repositories.users import UserRepository


# ---------------------------------------------------------------------------
# Mock env flag — bypasses the real Graph HTTP call entirely
# ---------------------------------------------------------------------------


class TestMockFlag:
    def test_returns_parsed_list(self, monkeypatch):
        monkeypatch.setenv(
            "AGNES_MICROSOFT_GRAPH_MOCK_GROUPS",
            "grp-a@example.com, grp-b@example.com , grp-c@example.com",
        )
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("any-token") == [
            "grp-a@example.com",
            "grp-b@example.com",
            "grp-c@example.com",
        ]

    def test_empty_value_returns_empty_list(self, monkeypatch):
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("any-token") == []

    def test_trailing_commas_are_skipped(self, monkeypatch):
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "a@x, , ,b@x,,")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("tok") == ["a@x", "b@x"]


# ---------------------------------------------------------------------------
# Real path — Microsoft Graph GET /me/memberOf, paginated
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


@pytest.fixture
def real_path_env(monkeypatch):
    monkeypatch.delenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", raising=False)


class TestRealFetchPagination:
    def test_single_page_filters_to_groups_only(self, real_path_env, monkeypatch):
        """Non-group directory objects (e.g. directoryRole) in memberOf are
        skipped — only #microsoft.graph.group entries are mirrored."""
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append((url, headers, timeout))
            return _FakeResponse(
                {
                    "value": [
                        {"@odata.type": "#microsoft.graph.group", "mail": "eng@example.com", "displayName": "Eng"},
                        {"@odata.type": "#microsoft.graph.directoryRole", "displayName": "Global Admin"},
                        {"@odata.type": "#microsoft.graph.group", "mail": None, "displayName": "No Mail Group"},
                    ]
                }
            )

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", fake_get)
        from app.auth.microsoft_group_sync import GRAPH_MEMBER_OF_URL, fetch_user_groups

        groups = fetch_user_groups("tok-123")
        assert groups == ["eng@example.com", "No Mail Group"]
        assert len(calls) == 1
        url, headers, timeout = calls[0]
        assert url == GRAPH_MEMBER_OF_URL
        assert headers["Authorization"] == "Bearer tok-123"
        assert timeout

    def test_follows_odata_next_link(self, real_path_env, monkeypatch):
        pages = {
            "https://graph.microsoft.com/v1.0/me/memberOf": {
                "value": [{"@odata.type": "#microsoft.graph.group", "mail": "page1@example.com"}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/memberOf?$skiptoken=abc",
            },
            "https://graph.microsoft.com/v1.0/me/memberOf?$skiptoken=abc": {
                "value": [{"@odata.type": "#microsoft.graph.group", "mail": "page2@example.com"}],
            },
        }
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append(url)
            return _FakeResponse(pages[url])

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", fake_get)
        from app.auth.microsoft_group_sync import fetch_user_groups

        groups = fetch_user_groups("tok")
        assert groups == ["page1@example.com", "page2@example.com"]
        assert len(calls) == 2

    def test_no_access_token_fails_soft(self, real_path_env, monkeypatch):
        def fake_get(*a, **kw):
            raise AssertionError("must not call Graph with no access token")

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", fake_get)
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("") == []

    def test_http_error_fails_soft(self, real_path_env, monkeypatch):
        import requests

        def fake_get(*a, **kw):
            raise requests.RequestException("boom")

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", fake_get)
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("tok") == []

    def test_malformed_response_fails_soft(self, real_path_env, monkeypatch):
        class _BadResponse:
            def raise_for_status(self):
                pass

            def json(self):
                raise ValueError("not json")

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", lambda *a, **kw: _BadResponse())
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("tok") == []

    def test_page_cap_stops_a_runaway_next_link_loop(self, real_path_env, monkeypatch):
        """A malicious/malformed nextLink that never terminates must not
        hang the process — capped at _MAX_PAGES."""
        import app.auth.microsoft_group_sync as mgs

        call_count = 0

        def fake_get(url, headers=None, timeout=None):
            nonlocal call_count
            call_count += 1
            return _FakeResponse(
                {
                    "value": [{"@odata.type": "#microsoft.graph.group", "mail": f"g{call_count}@example.com"}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/memberOf?loop=1",
                }
            )

        monkeypatch.setattr(mgs.requests, "get", fake_get)
        groups = mgs.fetch_user_groups("tok")
        assert call_count == mgs._MAX_PAGES
        assert len(groups) == mgs._MAX_PAGES


# ---------------------------------------------------------------------------
# apply_user_groups — direct unit coverage (fresh system DB, mocked fetch)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_system_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db

    monkeypatch.setattr(db, "_system_db_conn", None, raising=False)
    monkeypatch.setattr(db, "_system_db_path", None, raising=False)
    return get_system_db()


def _enable_sync(monkeypatch):
    monkeypatch.setenv("AGNES_MICROSOFT_GROUP_SYNC_ENABLED", "true")


def _set_fetch(monkeypatch, groups):
    import app.auth.microsoft_group_sync as mgs

    monkeypatch.setattr(mgs, "fetch_user_groups", lambda token: list(groups))


class TestApplyUserGroupsDisabledByDefault:
    def test_no_graph_call_when_disabled(self, fresh_system_db, monkeypatch):
        monkeypatch.delenv("AGNES_MICROSOFT_GROUP_SYNC_ENABLED", raising=False)
        import app.auth.microsoft_group_sync as mgs

        def boom(token):
            raise AssertionError("fetch_user_groups must not be called when disabled")

        monkeypatch.setattr(mgs, "fetch_user_groups", boom)

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.applied is False
        assert result.soft_failed is False
        assert result.denied is False
        assert result.fetched == []


class TestApplyUserGroupsWritesAndSource:
    def test_groups_created_with_microsoft_sync_source(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        _set_fetch(monkeypatch, ["eng@example.com", "finance@example.com"])
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.applied is True
        assert result.soft_failed is False

        members = UserGroupMembersRepository(fresh_system_db)
        ug = UserGroupsRepository(fresh_system_db)
        rows = members.list_groups_with_meta_for_user("u1")
        by_name = {r["name"]: r for r in rows}
        assert set(by_name) == {"eng@example.com", "finance@example.com"}
        for name, row in by_name.items():
            assert row["source"] == "microsoft_sync"
            assert ug.get_by_name(name)["created_by"] == "system:microsoft-sync"

    def test_graph_failure_soft_fails_and_login_proceeds(self, fresh_system_db, monkeypatch):
        """A raised exception from fetch_user_groups never propagates —
        apply_user_groups always returns a SyncResult."""
        _enable_sync(monkeypatch)
        import app.auth.microsoft_group_sync as mgs

        def boom(token):
            raise RuntimeError("Graph is down")

        monkeypatch.setattr(mgs, "fetch_user_groups", boom)

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.soft_failed is True
        assert result.applied is False

    def test_empty_fetch_preserves_existing_snapshot(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")

        _set_fetch(monkeypatch, ["eng@example.com"])
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        _set_fetch(monkeypatch, [])
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)
        assert result.soft_failed is True

        members = UserGroupMembersRepository(fresh_system_db)
        synced = {r["name"] for r in members.list_groups_with_meta_for_user("u1") if r["source"] == "microsoft_sync"}
        assert synced == {"eng@example.com"}, "empty fetch must not wipe the previous snapshot"


class TestApplyUserGroupsPrefixFilter:
    def test_prefix_filters_relevant_groups(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        monkeypatch.setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(monkeypatch, ["agnes-eng@example.com", "agnes-finance@example.com", "other-team@example.com"])
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.applied is True
        assert sorted(result.relevant) == ["agnes-eng@example.com", "agnes-finance@example.com"]

        members = UserGroupMembersRepository(fresh_system_db)
        synced = {r["name"] for r in members.list_groups_with_meta_for_user("u1") if r["source"] == "microsoft_sync"}
        assert synced == {"agnes-eng@example.com", "agnes-finance@example.com"}

    def test_prefix_set_no_match_denies_without_clearing_existing(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")

        # First sync (no prefix) lands a group.
        _set_fetch(monkeypatch, ["eng@example.com"])
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        # Second sync: prefix configured, fetch is non-empty, nothing matches.
        monkeypatch.setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(monkeypatch, ["other-team@example.com"])
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.denied is True
        assert result.applied is False

        members = UserGroupMembersRepository(fresh_system_db)
        synced = {r["name"] for r in members.list_groups_with_meta_for_user("u1") if r["source"] == "microsoft_sync"}
        assert synced == {"eng@example.com"}, "a denied resync must not clear the prior snapshot"


class TestApplyUserGroupsAdminMembershipSurvives:
    def test_admin_added_membership_is_not_touched_by_resync(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        ug = UserGroupsRepository(fresh_system_db)
        members = UserGroupMembersRepository(fresh_system_db)
        admin_group = ug.create(name="hand-picked-admin-group", created_by="admin@example.com")
        members.add_member("u1", admin_group["id"], source="admin")

        _set_fetch(monkeypatch, ["eng@example.com"])
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)
        # Re-sync again — must not touch the admin-added row.
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        rows = members.list_groups_with_meta_for_user("u1")
        by_name = {r["name"]: r["source"] for r in rows}
        assert by_name["hand-picked-admin-group"] == "admin"
        assert by_name["eng@example.com"] == "microsoft_sync"


class TestApplyUserGroupsIdempotent:
    def test_second_sync_same_set_does_not_duplicate(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        _set_fetch(monkeypatch, ["eng@example.com"])
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        members = UserGroupMembersRepository(fresh_system_db)
        group_ids = members.list_groups_for_user("u1")
        assert len(group_ids) == 1

        count = fresh_system_db.execute(
            "SELECT COUNT(*) FROM user_groups WHERE name = ?",
            ["eng@example.com"],
        ).fetchone()[0]
        assert count == 1
