"""Unit tests for app.auth.group_sync.fetch_user_groups."""

from __future__ import annotations

from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Mock env flag
# ---------------------------------------------------------------------------


class TestMockFlag:
    def test_returns_parsed_list(self, monkeypatch):
        monkeypatch.setenv(
            "GOOGLE_ADMIN_SDK_MOCK_GROUPS",
            "grp_a@groupon.com, grp_b@groupon.com , grp_c@groupon.com",
        )
        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("any@x") == [
            "grp_a@groupon.com",
            "grp_b@groupon.com",
            "grp_c@groupon.com",
        ]

    def test_empty_value_returns_empty_list(self, monkeypatch):
        """Setting the flag to the empty string returns [] — explicit 'no groups'."""
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "")
        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("any@x") == []

    def test_single_value_no_comma(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "solo@groupon.com")
        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("any@x") == ["solo@groupon.com"]

    def test_trailing_commas_are_skipped(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", "a@x, , ,b@x,,")
        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("u@x") == ["a@x", "b@x"]


# ---------------------------------------------------------------------------
# Real path (monkeypatched Google client) — keyless-DWD + Admin SDK shape
# ---------------------------------------------------------------------------


def _make_admin_service_mock(pages: list[dict]):
    """Mock for ``service.groups().list(...).execute()`` that yields ``pages``
    in order. Returns ``(service, list_call)`` so tests can assert on the
    call kwargs."""
    page_iter = iter(pages)

    def execute_side_effect(*_a, **_kw):
        return next(page_iter)

    list_call = mock.Mock()
    list_call.return_value.execute.side_effect = execute_side_effect
    groups = mock.Mock()
    groups.return_value.list = list_call
    service = mock.Mock()
    service.groups = groups
    return service, list_call


@pytest.fixture
def real_path_env(monkeypatch):
    """Common setup: ensure mock-env is unset, subject + SA explicit (no
    metadata-server call), and stub `google.auth.default` + `iam.Signer` +
    `service_account.Credentials` so the SDK init never reaches Google."""
    monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
    monkeypatch.setenv("GOOGLE_ADMIN_SDK_SUBJECT", "admin@example.com")
    monkeypatch.setenv("GOOGLE_ADMIN_SDK_SA_EMAIL", "sa@example.iam.gserviceaccount.com")
    monkeypatch.setattr("google.auth.default", lambda *a, **kw: (mock.Mock(), "test-project"))
    monkeypatch.setattr("google.auth.iam.Signer", lambda *a, **kw: mock.Mock())
    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials",
        lambda **kw: mock.Mock(),
    )


class TestRealPath:
    def test_success_single_page(self, monkeypatch, real_path_env):
        service, list_call = _make_admin_service_mock(
            [
                {
                    "groups": [
                        {"email": "grp_a@groupon.com", "name": "A"},
                        {"email": "grp_b@groupon.com", "name": "B"},
                    ]
                    # no nextPageToken
                }
            ]
        )
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

        from app.auth.group_sync import fetch_user_groups

        result = fetch_user_groups("user@groupon.com")
        assert result == ["grp_a@groupon.com", "grp_b@groupon.com"]

        call_kwargs = list_call.call_args.kwargs
        assert call_kwargs["userKey"] == "user@groupon.com"
        assert call_kwargs["pageToken"] is None

    def test_success_paginated(self, monkeypatch, real_path_env):
        service, list_call = _make_admin_service_mock(
            [
                {
                    "groups": [{"email": "page1@x"}],
                    "nextPageToken": "tok1",
                },
                {
                    "groups": [{"email": "page2@x"}],
                    # terminal
                },
            ]
        )
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

        from app.auth.group_sync import fetch_user_groups

        result = fetch_user_groups("u@x")
        assert result == ["page1@x", "page2@x"]
        assert list_call.call_args_list[1].kwargs["pageToken"] == "tok1"

    def test_api_exception_returns_empty(self, monkeypatch, real_path_env):
        service = mock.Mock()
        service.groups.return_value.list.return_value.execute.side_effect = RuntimeError("boom")
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("user@x") == []

    def test_client_init_exception_returns_empty(self, monkeypatch, real_path_env):
        """Errors before the API call (ADC, signer, build) also fail-soft."""

        def boom(*a, **kw):
            raise RuntimeError("adc unavailable")

        monkeypatch.setattr("google.auth.default", boom)

        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("user@x") == []

    def test_groups_without_email_are_skipped(self, monkeypatch, real_path_env):
        """Defensive: a malformed group entry missing 'email' must not crash."""
        service, _ = _make_admin_service_mock(
            [
                {
                    "groups": [
                        {"email": "good@x", "name": "Good"},
                        {"name": "no email"},
                        {},
                    ]
                }
            ]
        )
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("u@x") == ["good@x"]


# ---------------------------------------------------------------------------
# Pre-flight env / metadata checks (fail-soft when config is missing)
# ---------------------------------------------------------------------------


class TestPreflightFailSoft:
    def test_missing_subject_returns_empty(self, monkeypatch):
        """Without GOOGLE_ADMIN_SDK_SUBJECT we cannot impersonate — bail."""
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_SUBJECT", raising=False)
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_SA_EMAIL", "sa@x.iam")

        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("u@x") == []

    def test_missing_sa_and_no_metadata_returns_empty(self, monkeypatch):
        """No explicit SA + metadata server unreachable → bail."""
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_SA_EMAIL", raising=False)
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_SUBJECT", "admin@example.com")

        # Force the metadata fetch to fail.
        def boom(*a, **kw):
            raise OSError("no route to metadata")

        monkeypatch.setattr("urllib.request.urlopen", boom)

        from app.auth.group_sync import fetch_user_groups

        assert fetch_user_groups("u@x") == []

    def test_explicit_sa_email_used(self, monkeypatch):
        """When GOOGLE_ADMIN_SDK_SA_EMAIL is set, metadata server is bypassed."""
        monkeypatch.delenv("GOOGLE_ADMIN_SDK_MOCK_GROUPS", raising=False)
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_SUBJECT", "admin@example.com")
        monkeypatch.setenv("GOOGLE_ADMIN_SDK_SA_EMAIL", "explicit@x.iam.gserviceaccount.com")

        # Capture what email Signer is called with.
        captured: dict[str, str] = {}

        def fake_signer(_request, _source, sa_email):
            captured["sa"] = sa_email
            return mock.Mock()

        monkeypatch.setattr("google.auth.default", lambda *a, **kw: (mock.Mock(), "p"))
        monkeypatch.setattr("google.auth.iam.Signer", fake_signer)
        monkeypatch.setattr(
            "google.oauth2.service_account.Credentials",
            lambda **kw: mock.Mock(),
        )

        service, _ = _make_admin_service_mock([{"groups": []}])
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **kw: service)

        from app.auth.group_sync import fetch_user_groups

        fetch_user_groups("u@x")
        assert captured["sa"] == "explicit@x.iam.gserviceaccount.com"


# ---------------------------------------------------------------------------
# ensure_everyone_membership — issue #748 auto-Everyone-at-creation helper
# ---------------------------------------------------------------------------


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """A fresh system DB (Admin/Everyone seeded) + one plain user row."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key-min-32-characters!!")
    (tmp_path / "state").mkdir()
    (tmp_path / "analytics").mkdir()
    (tmp_path / "extracts").mkdir()
    from src.db import close_system_db, get_system_db
    from src.repositories.users import UserRepository

    close_system_db()
    conn = get_system_db()
    UserRepository(conn).create(id="u1", email="u1@example.com", name="U1")
    conn.close()
    yield
    close_system_db()


class TestEnsureEveryoneMembership:
    def test_env_unset_adds_system_seed_row(self, db_env, monkeypatch):
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        from app.auth.group_sync import ensure_everyone_membership
        from src.db import SYSTEM_EVERYONE_GROUP
        from src.repositories import user_group_members_repo, user_groups_repo

        result = ensure_everyone_membership("u1", added_by="test:caller")
        assert result is True

        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        rows = user_group_members_repo().list_groups_with_meta_for_user("u1")
        matching = [r for r in rows if r["group_id"] == everyone["id"]]
        assert len(matching) == 1
        assert matching[0]["source"] == "system_seed"

    def test_the_legacy_env_var_no_longer_suppresses_it(self, db_env, monkeypatch):
        """It used to no-op whenever ``AGNES_GROUP_EVERYONE_EMAIL`` was set,
        because on those instances the seeded row was mirrored from a
        Workspace group and a local membership would have fought the
        Workspace-authoritative set.

        0098 converted that mapping into an ordinary synced group, so nothing
        mirrors ``Everyone`` any more and it goes back to meaning what it
        says — on every instance, whether or not the (now inert) variable is
        still set in someone's env.
        """
        monkeypatch.setenv("AGNES_GROUP_EVERYONE_EMAIL", "everyone@workspace.test")
        from app.auth.group_sync import ensure_everyone_membership
        from src.repositories import user_group_members_repo

        assert ensure_everyone_membership("u1", added_by="test:caller") is True
        rows = user_group_members_repo().list_groups_with_meta_for_user("u1")
        assert [r["name"] for r in rows] == ["Everyone"]
        assert rows[0]["source"] == "system_seed"

    def test_idempotent_on_second_call(self, db_env, monkeypatch):
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        from app.auth.group_sync import ensure_everyone_membership
        from src.repositories import user_group_members_repo

        ensure_everyone_membership("u1", added_by="test:caller")
        ensure_everyone_membership("u1", added_by="test:caller")

        rows = user_group_members_repo().list_groups_with_meta_for_user("u1")
        everyone_rows = [r for r in rows if r["name"] == "Everyone"]
        assert len(everyone_rows) == 1

    def test_missing_group_returns_false_without_raising(self, db_env, monkeypatch):
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        from src.db import get_system_db

        conn = get_system_db()
        conn.execute("DELETE FROM user_groups WHERE name = 'Everyone'")
        conn.close()

        from app.auth.group_sync import ensure_everyone_membership

        result = ensure_everyone_membership("u1", added_by="test:caller")
        assert result is False

    def test_opt_out_preserved_helper_does_not_reassert(self, db_env, monkeypatch):
        """Once granted, removing the row and calling the helper again does
        NOT re-add it — callers only invoke this at creation time, not on
        every login/boot, so admin opt-out sticks."""
        monkeypatch.delenv("AGNES_GROUP_EVERYONE_EMAIL", raising=False)
        from src.db import get_system_db
        from src.repositories import user_group_members_repo, user_groups_repo
        from src.db import SYSTEM_EVERYONE_GROUP

        from app.auth.group_sync import ensure_everyone_membership

        ensure_everyone_membership("u1", added_by="test:caller")
        everyone = user_groups_repo().get_by_name(SYSTEM_EVERYONE_GROUP)
        user_group_members_repo().remove_member("u1", everyone["id"])

        conn = get_system_db()
        conn.close()

        # The helper itself is not re-invoked by any login/boot path — this
        # asserts the removal sticks in the DB, not that the helper enforces
        # it (there is no re-assertion call site by design).
        rows = user_group_members_repo().list_groups_with_meta_for_user("u1")
        assert not any(r["name"] == SYSTEM_EVERYONE_GROUP for r in rows)
