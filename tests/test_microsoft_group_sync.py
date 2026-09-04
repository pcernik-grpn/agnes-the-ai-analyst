"""Unit tests for app.auth.microsoft_group_sync.

Mirrors tests/test_group_sync.py's structure (mock-env parsing, real-path
HTTP mocking) plus direct ``apply_user_groups`` coverage — the Google
sibling only exercises ``apply_user_groups`` indirectly, through the OAuth
callback in tests/test_google_group_prefix_sync.py; this file covers both
levels for Microsoft.

2026-09 identity-scheme fix: groups are now fetched as ``{"id", "mail",
"displayName"}`` dicts (was a flat list of mail/displayName strings) and
mirrored into ``user_groups`` keyed on ``entra:<id>`` — the SAME key
``connectors.sharepoint.acl_sync`` uses for the same Entra group. See
tests/test_microsoft_group_migration.py for the legacy-key rename-in-place
coverage.
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
            "g-a|grp-a@example.com|Group A, g-b|grp-b@example.com|Group B , g-c|grp-c@example.com",
        )
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("any-token") == [
            {"id": "g-a", "mail": "grp-a@example.com", "displayName": "Group A"},
            {"id": "g-b", "mail": "grp-b@example.com", "displayName": "Group B"},
            {"id": "g-c", "mail": "grp-c@example.com", "displayName": ""},
        ]

    def test_id_only_entry(self, monkeypatch):
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "g-only-id")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("any-token") == [{"id": "g-only-id", "mail": "", "displayName": ""}]

    def test_display_name_only_entry(self, monkeypatch):
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "g-x||Display Only")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("any-token") == [{"id": "g-x", "mail": "", "displayName": "Display Only"}]

    def test_empty_value_returns_empty_list(self, monkeypatch):
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("any-token") == []

    def test_trailing_commas_are_skipped(self, monkeypatch):
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "g-a|a@x, , ,g-b|b@x,,")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("tok") == [
            {"id": "g-a", "mail": "a@x", "displayName": ""},
            {"id": "g-b", "mail": "b@x", "displayName": ""},
        ]

    def test_entry_with_no_id_is_skipped(self, monkeypatch):
        """The id is what group identity now hangs on — a mock entry that
        omits it (leading ``|``) is dropped rather than mirrored under a
        blank id."""
        monkeypatch.setenv("AGNES_MICROSOFT_GRAPH_MOCK_GROUPS", "|no-id@example.com,g-ok|ok@example.com")
        from app.auth.microsoft_group_sync import fetch_user_groups

        assert fetch_user_groups("tok") == [{"id": "g-ok", "mail": "ok@example.com", "displayName": ""}]


# ---------------------------------------------------------------------------
# Real path — Microsoft Graph GET /me/transitiveMemberOf/microsoft.graph.group
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
        """Non-group directory objects (e.g. directoryRole) are skipped —
        only #microsoft.graph.group entries are mirrored. A group with no
        id is dropped (id is what identity hangs on now)."""
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append((url, headers, timeout))
            return _FakeResponse(
                {
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.group",
                            "id": "g-1",
                            "mail": "eng@example.com",
                            "displayName": "Eng",
                        },
                        {"@odata.type": "#microsoft.graph.directoryRole", "displayName": "Global Admin"},
                        {"@odata.type": "#microsoft.graph.group", "id": "g-2", "displayName": "No Mail Group"},
                        {"@odata.type": "#microsoft.graph.group", "mail": "no-id@example.com"},
                    ]
                }
            )

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", fake_get)
        from app.auth.microsoft_group_sync import GRAPH_TRANSITIVE_MEMBER_OF_GROUPS_URL, fetch_user_groups

        groups = fetch_user_groups("tok-123")
        assert groups == [
            {"id": "g-1", "mail": "eng@example.com", "displayName": "Eng"},
            {"id": "g-2", "mail": "", "displayName": "No Mail Group"},
        ]
        assert len(calls) == 1
        url, headers, timeout = calls[0]
        assert url.startswith(GRAPH_TRANSITIVE_MEMBER_OF_GROUPS_URL)
        assert "$select=" in url
        assert headers["Authorization"] == "Bearer tok-123"
        assert timeout

    def test_follows_odata_next_link(self, real_path_env, monkeypatch):
        base = (
            "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group?$select=id,mail,displayName"
        )
        next_url = "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group?$skiptoken=abc"
        pages = {
            base: {
                "value": [{"@odata.type": "#microsoft.graph.group", "id": "g-1", "mail": "page1@example.com"}],
                "@odata.nextLink": next_url,
            },
            next_url: {
                "value": [{"@odata.type": "#microsoft.graph.group", "id": "g-2", "mail": "page2@example.com"}],
            },
        }
        calls = []

        def fake_get(url, headers=None, timeout=None):
            calls.append(url)
            return _FakeResponse(pages[url])

        monkeypatch.setattr("app.auth.microsoft_group_sync.requests.get", fake_get)
        from app.auth.microsoft_group_sync import fetch_user_groups

        groups = fetch_user_groups("tok")
        assert [g["mail"] for g in groups] == ["page1@example.com", "page2@example.com"]
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
                    "value": [
                        {"@odata.type": "#microsoft.graph.group", "id": f"g-{call_count}", "mail": f"g{call_count}@x"}
                    ],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/transitiveMemberOf/microsoft.graph.group?loop=1",
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


def _group(object_id: str, mail: str = "", display_name: str = "") -> dict:
    return {"id": object_id, "mail": mail, "displayName": display_name}


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
    def test_groups_created_with_microsoft_sync_source_and_entra_key(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        _set_fetch(
            monkeypatch,
            [_group("g-eng", "eng@example.com"), _group("g-fin", "finance@example.com")],
        )
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.applied is True
        assert result.soft_failed is False
        assert sorted(result.fetched) == ["eng@example.com", "finance@example.com"]

        members = UserGroupMembersRepository(fresh_system_db)
        ug = UserGroupsRepository(fresh_system_db)
        rows = members.list_groups_with_meta_for_user("u1")
        by_name = {r["name"]: r for r in rows}
        assert set(by_name) == {"entra:g-eng", "entra:g-fin"}
        for name, row in by_name.items():
            assert row["source"] == "microsoft_sync"
            group = ug.get_by_name(name)
            assert group["created_by"] == "system:microsoft-sync"
            assert "example.com" in (group["description"] or "")

    def test_group_with_no_mail_keyed_by_id_and_display_name_in_description(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        _set_fetch(monkeypatch, [_group("g-nomail", "", "No Mail Group")])
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        ug = UserGroupsRepository(fresh_system_db)
        group = ug.get_by_name("entra:g-nomail")
        assert group is not None
        assert "No Mail Group" in (group["description"] or "")

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

        _set_fetch(monkeypatch, [_group("g-eng", "eng@example.com")])
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        _set_fetch(monkeypatch, [])
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)
        assert result.soft_failed is True

        members = UserGroupMembersRepository(fresh_system_db)
        synced = {r["name"] for r in members.list_groups_with_meta_for_user("u1") if r["source"] == "microsoft_sync"}
        assert synced == {"entra:g-eng"}, "empty fetch must not wipe the previous snapshot"


class TestApplyUserGroupsPrefixFilter:
    def test_prefix_filters_relevant_groups(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        monkeypatch.setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(
            monkeypatch,
            [
                _group("g-eng", "agnes-eng@example.com"),
                _group("g-fin", "agnes-finance@example.com"),
                _group("g-other", "other-team@example.com"),
            ],
        )
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.applied is True
        assert sorted(result.relevant) == ["agnes-eng@example.com", "agnes-finance@example.com"]

        members = UserGroupMembersRepository(fresh_system_db)
        synced = {r["name"] for r in members.list_groups_with_meta_for_user("u1") if r["source"] == "microsoft_sync"}
        assert synced == {"entra:g-eng", "entra:g-fin"}

    def test_prefix_matches_display_name_when_mail_absent(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        monkeypatch.setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(monkeypatch, [_group("g-dn", "", "agnes-displayname-only")])
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.applied is True
        assert result.relevant == ["agnes-displayname-only"]

    def test_prefix_set_no_match_denies_without_clearing_existing(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")

        # First sync (no prefix) lands a group.
        _set_fetch(monkeypatch, [_group("g-eng", "eng@example.com")])
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        # Second sync: prefix configured, fetch is non-empty, nothing matches.
        monkeypatch.setenv("AGNES_MICROSOFT_GROUP_PREFIX", "agnes-")
        _set_fetch(monkeypatch, [_group("g-other", "other-team@example.com")])
        result = mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        assert result.denied is True
        assert result.applied is False

        members = UserGroupMembersRepository(fresh_system_db)
        synced = {r["name"] for r in members.list_groups_with_meta_for_user("u1") if r["source"] == "microsoft_sync"}
        assert synced == {"entra:g-eng"}, "a denied resync must not clear the prior snapshot"


class TestApplyUserGroupsAdminMembershipSurvives:
    def test_admin_added_membership_is_not_touched_by_resync(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        ug = UserGroupsRepository(fresh_system_db)
        members = UserGroupMembersRepository(fresh_system_db)
        admin_group = ug.create(name="hand-picked-admin-group", created_by="admin@example.com")
        members.add_member("u1", admin_group["id"], source="admin")

        _set_fetch(monkeypatch, [_group("g-eng", "eng@example.com")])
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)
        # Re-sync again — must not touch the admin-added row.
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        rows = members.list_groups_with_meta_for_user("u1")
        by_name = {r["name"]: r["source"] for r in rows}
        assert by_name["hand-picked-admin-group"] == "admin"
        assert by_name["entra:g-eng"] == "microsoft_sync"


class TestApplyUserGroupsIdempotent:
    def test_second_sync_same_set_does_not_duplicate(self, fresh_system_db, monkeypatch):
        _enable_sync(monkeypatch)
        _set_fetch(monkeypatch, [_group("g-eng", "eng@example.com")])
        import app.auth.microsoft_group_sync as mgs

        UserRepository(fresh_system_db).create(id="u1", email="e@example.com", name="E")
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)
        mgs.apply_user_groups("u1", "e@example.com", "tok", fresh_system_db)

        members = UserGroupMembersRepository(fresh_system_db)
        group_ids = members.list_groups_for_user("u1")
        assert len(group_ids) == 1

        count = fresh_system_db.execute(
            "SELECT COUNT(*) FROM user_groups WHERE name = ?",
            ["entra:g-eng"],
        ).fetchone()[0]
        assert count == 1


class TestApplyUserGroupsIdentitySchemeMatchesSharePoint:
    def test_same_key_as_sharepoint_acl_sync(self):
        """The whole point of the fix: both writers must produce the exact
        same ``user_groups.name`` for the same Entra object id."""
        from connectors.sharepoint.acl_sync import entra_group_name as sharepoint_entra_group_name
        from src.entra_identity import entra_group_name as shared_entra_group_name

        assert shared_entra_group_name("abc-123") == sharepoint_entra_group_name("abc-123") == "entra:abc-123"
