"""Tests for the ``sharepoint-acl-sync`` worker job body (2026-08-30 plan,
Task 4 — ``connectors/sharepoint/acl_sync.py::run_acl_sync``).

Builds connection/scope/user/group rows directly via the repos under a
fresh ``DATA_DIR`` (never through the wizard's HTTP endpoint — that endpoint
does not persist ``drive_id``/``access_mode`` yet, see the module's "Known
gap" docstring note). Graph is never called: ``graph_client.get_app_token``,
``graph_client.list_item_permissions`` and
``graph_client.list_group_transitive_members`` are monkeypatched with async
fakes on the ``graph_client`` module itself (the sync body calls them
module-qualified — ``graph_client.xxx(...)`` — specifically so this
monkeypatch idiom works, mirroring ``tests/test_sharepoint_graph_client_acl
.py``'s own transport-level fakes one layer up).
"""

from __future__ import annotations

import pytest

from connectors.sharepoint import acl_sync, graph_client
from connectors.sharepoint.acl_sync import ACL_SYNC_SENTINEL, ACL_SYNC_SOURCE
from connectors.sharepoint.graph_client import SharePointGraphError

CONN_ID = "conn-acl-1"


@pytest.fixture
def acl_env(tmp_path, monkeypatch):
    """Fresh system.duckdb under a tmp DATA_DIR, feature flag on, and a
    resolvable (but never actually parsed — get_app_token is faked)
    certificate env var so ``resolve_sharepoint_settings`` doesn't raise."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AGNES_DB_URL", raising=False)
    monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "true")
    monkeypatch.setenv("SHAREPOINT_CERT_PRIVATE_KEY", "unused-because-get-app-token-is-faked")

    from src.db import close_system_db, get_system_db

    get_system_db()
    yield
    close_system_db()


# ---------------------------------------------------------------------------
# fixture builders — direct repo writes, no HTTP
# ---------------------------------------------------------------------------


def _make_connection(connection_id: str = CONN_ID) -> str:
    from src.repositories import source_connections_repo

    source_connections_repo().create(
        id=connection_id,
        name="Test SharePoint",
        source_type="sharepoint",
        config={"tenant_id": "tenant-1", "client_id": "client-1", "scopes": []},
    )
    return connection_id


def _add_scope(
    connection_id: str,
    *,
    source_scope_id: str,
    collection_id: str,
    drive_id: "str | None" = "drive-1",
    access_mode: str = "mirrored",
) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = dict(row.get("config") or {})
    scopes = list(config.get("scopes") or [])
    scopes.append(
        {
            "source_scope_id": source_scope_id,
            "display_path": source_scope_id,
            "anonymize": False,
            "collection_id": collection_id,
            "drive_id": drive_id,
            "access_mode": access_mode,
        }
    )
    config["scopes"] = scopes
    repo.update(connection_id, config=config)


def _make_collection(name: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(
        name=name, slug=name.lower().replace(" ", "-"), description=None, created_by="test"
    )


def _make_user(user_id: str, email: str, name: str = "Test User") -> None:
    from src.repositories import users_repo

    users_repo().create(id=user_id, email=email, name=name)


def _group_by_name(name: str):
    from src.repositories import user_groups_repo

    for g in user_groups_repo().list_all():
        if g["name"] == name:
            return g
    return None


def _grants_for_collection(collection_id: str) -> list:
    from app.resource_types import ResourceType
    from src.repositories import resource_grants_repo

    return [
        g
        for g in resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
        if g.get("resource_id") == collection_id
    ]


def _last_run(connection_id: str = CONN_ID) -> dict:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(connection_id)
    return (row.get("config") or {}).get("acl_sync_last_run") or {}


def _audit_count(action_prefix: str = None, action: str = None) -> int:
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action_prefix=action_prefix, action=action, limit=1000)
    return len(rows)


# ---------------------------------------------------------------------------
# graph fakes
# ---------------------------------------------------------------------------


async def _fake_get_app_token(tenant_id, client_id, private_key, *, client_secret=""):
    return "fake-token"


def _perms_fake(mapping: dict):
    """``mapping``: ``{(drive_id, item_id): [perm, ...]}``."""

    async def fake(token, drive_id, item_id):
        return mapping.get((drive_id, item_id), [])

    return fake


def _members_fake(mapping: dict):
    """``mapping``: ``{oid: [{"mail":..., "userPrincipalName":...}, ...]}``."""

    async def fake(token, group_id):
        return mapping.get(group_id, [])

    return fake


def _group_perm(oid: str) -> dict:
    return {"id": f"perm-{oid}", "roles": ["read"], "grantedToV2": {"group": {"id": oid}}}


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestMirroredVsManualScope:
    def test_mirrored_scope_syncs_group_membership_and_grant_manual_untouched(self, acl_env, monkeypatch):
        conn_id = _make_connection()
        col_mirrored = _make_collection("Mirrored Col")
        col_manual = _make_collection("Manual Col")
        _add_scope(conn_id, source_scope_id="mirrored-1", collection_id=col_mirrored, access_mode="mirrored")
        _add_scope(conn_id, source_scope_id="manual-1", collection_id=col_manual, access_mode="manual")
        _make_user("u-alice", "alice@example.com")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "mirrored-1"): [_group_perm("g-123")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-123": [{"mail": "alice@example.com", "userPrincipalName": "alice@example.com"}]}),
        )

        result = acl_sync.run_acl_sync({"connection_id": conn_id})

        assert result["connections"] == 1
        assert result["scopes"] == 1  # only the mirrored scope is processed
        assert result["matched"] == 1
        assert result["unmatched"] == 0

        group = _group_by_name("entra:g-123")
        assert group is not None

        from src.repositories import user_group_members_repo

        members = user_group_members_repo().list_members_for_group(group["id"])
        assert {m["id"] for m in members} == {"u-alice"}
        assert members[0]["source"] == ACL_SYNC_SOURCE

        mirrored_grants = _grants_for_collection(col_mirrored)
        assert len(mirrored_grants) == 1
        assert mirrored_grants[0]["group_id"] == group["id"]
        assert mirrored_grants[0]["assigned_by"] == ACL_SYNC_SENTINEL

        # The manual scope's collection is never touched by the sync.
        assert _grants_for_collection(col_manual) == []


class TestAdminGrantSurvivesStaleRemoved:
    def test_admin_grant_survives_reconciliation_stale_sentinel_grant_removed(self, acl_env, monkeypatch):
        from app.resource_types import ResourceType
        from src.repositories import resource_grants_repo, user_groups_repo

        conn_id = _make_connection()
        col_id = _make_collection("Reconcile Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")
        _make_user("u-bob", "bob@example.com")

        groups_repo = user_groups_repo()
        admin_group = groups_repo.create(name="admin-group", description=None, created_by="admin-user")
        stale_group = groups_repo.create(name="entra:stale-oid", description=None, created_by=ACL_SYNC_SENTINEL)

        grants = resource_grants_repo()
        grants.ensure_grant(admin_group["id"], ResourceType.COLLECTION.value, col_id, assigned_by="admin-user")
        grants.ensure_grant(stale_group["id"], ResourceType.COLLECTION.value, col_id, assigned_by=ACL_SYNC_SENTINEL)

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-new")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-new": [{"mail": "bob@example.com", "userPrincipalName": "bob@example.com"}]}),
        )

        acl_sync.run_acl_sync({"connection_id": conn_id})

        current_group_ids = {g["group_id"] for g in _grants_for_collection(col_id)}
        assert admin_group["id"] in current_group_ids, "admin-assigned grant must survive reconciliation"
        assert stale_group["id"] not in current_group_ids, "stale sentinel grant must be removed"
        new_group = _group_by_name("entra:g-new")
        assert new_group is not None
        assert new_group["id"] in current_group_ids


class TestIdempotentRerun:
    def test_rerun_with_unchanged_graph_state_writes_no_delta_audit(self, acl_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("Idempotent Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")
        _make_user("u-carol", "carol@example.com")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-1")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-1": [{"mail": "carol@example.com", "userPrincipalName": "carol@example.com"}]}),
        )

        acl_sync.run_acl_sync({"connection_id": conn_id})
        grants_after_first = _grants_for_collection(col_id)
        added_before = _audit_count(action="sharepoint_acl.grant_added")
        removed_before = _audit_count(action="sharepoint_acl.grant_removed")
        replaced_before = _audit_count(action="sharepoint_acl.membership_replaced")

        acl_sync.run_acl_sync({"connection_id": conn_id})
        grants_after_second = _grants_for_collection(col_id)

        assert len(grants_after_first) == len(grants_after_second) == 1
        assert _audit_count(action="sharepoint_acl.grant_added") == added_before, "no new grant_added on re-run"
        assert _audit_count(action="sharepoint_acl.grant_removed") == removed_before, "no new grant_removed on re-run"
        assert _audit_count(action="sharepoint_acl.membership_replaced") == replaced_before, (
            "no new membership_replaced on re-run"
        )


class TestFailSoftGroupExpansion:
    def test_group_expansion_failure_keeps_previous_membership_and_marks_stale(self, acl_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("FailSoft Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")
        _make_user("u-dave", "dave@example.com")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-1")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-1": [{"mail": "dave@example.com", "userPrincipalName": "dave@example.com"}]}),
        )
        acl_sync.run_acl_sync({"connection_id": conn_id})

        group = _group_by_name("entra:g-1")
        from src.repositories import user_group_members_repo

        members_repo = user_group_members_repo()
        assert {m["id"] for m in members_repo.list_members_for_group(group["id"])} == {"u-dave"}

        async def _raise(token, group_id):
            raise SharePointGraphError("transient graph error", status_code=503)

        monkeypatch.setattr(graph_client, "list_group_transitive_members", _raise)

        result = acl_sync.run_acl_sync({"connection_id": conn_id})

        # Fail-soft: previous membership is untouched.
        assert {m["id"] for m in members_repo.list_members_for_group(group["id"])} == {"u-dave"}
        assert result["errors"] == []  # fail-soft on identity resolution is not a hard connection failure
        assert "scope-1" in _last_run(conn_id)["stale_scopes"]
        # The group is still honored (its grant was computed from classify,
        # unaffected by the expansion failure) — grant is untouched too.
        assert _grants_for_collection(col_id)[0]["group_id"] == group["id"]


class TestCaseInsensitiveEmailMatch:
    def test_mixed_case_upn_matches_lowercase_login(self, acl_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("CI Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")
        _make_user("u-eve", "alice.novak@example.com")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-1")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake(
                {
                    "g-1": [
                        {"mail": None, "userPrincipalName": "Alice.Novak@EXAMPLE.com"},
                    ]
                }
            ),
        )

        result = acl_sync.run_acl_sync({"connection_id": conn_id})

        assert result["matched"] == 1
        assert result["unmatched"] == 0
        group = _group_by_name("entra:g-1")
        from src.repositories import user_group_members_repo

        members = user_group_members_repo().list_members_for_group(group["id"])
        assert {m["id"] for m in members} == {"u-eve"}


class TestStalenessSuspension:
    def _setup_stale_connection(self, monkeypatch, guarantee_mode: str) -> tuple:
        from app.resource_types import ResourceType
        from src.repositories import resource_grants_repo, source_connections_repo, user_groups_repo

        monkeypatch.setenv("AGNES_ACL_GUARANTEE_MODE", guarantee_mode)

        conn_id = _make_connection()
        col_id = _make_collection(f"Stale Col {guarantee_mode}")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")

        group = user_groups_repo().create(name="entra:existing", description=None, created_by=ACL_SYNC_SENTINEL)
        resource_grants_repo().ensure_grant(
            group["id"], ResourceType.COLLECTION.value, col_id, assigned_by=ACL_SYNC_SENTINEL
        )

        # Mark the connection's last successful run far enough in the past
        # to exceed the default 72h cap.
        row = source_connections_repo().get(conn_id)
        config = dict(row.get("config") or {})
        config["acl_sync_last_success_at"] = "2020-01-01T00:00:00+00:00"
        source_connections_repo().update(conn_id, config=config)

        async def _boom(*args, **kwargs):
            raise SharePointGraphError("token request failed", status_code=502)

        monkeypatch.setattr(graph_client, "get_app_token", _boom)

        return conn_id, col_id

    def test_must_not_stale_past_cap_suspends_sentinel_grants(self, acl_env, monkeypatch):
        conn_id, col_id = self._setup_stale_connection(monkeypatch, "must_not")

        acl_sync.run_acl_sync({"connection_id": conn_id})

        assert _grants_for_collection(col_id) == []

    def test_should_not_stale_grants_persist(self, acl_env, monkeypatch):
        conn_id, col_id = self._setup_stale_connection(monkeypatch, "should_not")

        acl_sync.run_acl_sync({"connection_id": conn_id})

        assert len(_grants_for_collection(col_id)) == 1


class TestFeatureFlagOff:
    def test_flag_off_is_a_clean_noop(self, acl_env, monkeypatch):
        monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "false")
        conn_id = _make_connection()
        col_id = _make_collection("Flag Off Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")

        result = acl_sync.run_acl_sync({"connection_id": conn_id})

        assert result == {"skipped": "acl_mirroring disabled"}
        assert _grants_for_collection(col_id) == []
        assert _group_by_name("entra:g-1") is None


class TestConfigPatchRaceRegression:
    """NB-1 review finding: ``_sync_connection`` and ``_sweep_connection``
    both read-modify-write ``source_connections.config`` — a nightly sync
    and the weekly sweep landing on the same connection could silently drop
    each other's just-written bookkeeping key. Simulate the sweep's write
    landing strictly between the sync's own connection read and its own
    config write, and assert both keys survive (the fix: ``config_patch``
    re-reads fresh instead of trusting the sync's caller-held snapshot)."""

    def test_concurrent_sweep_bookkeeping_key_survives_sync_run(self, acl_env, monkeypatch):
        from src.repositories import source_connections_repo

        conn_id = _make_connection()
        col_id = _make_collection("Race Col")
        _add_scope(conn_id, source_scope_id="scope-1", collection_id=col_id, access_mode="mirrored")
        _make_user("u-frank", "frank@example.com")

        monkeypatch.setattr(
            graph_client,
            "list_item_permissions",
            _perms_fake({("drive-1", "scope-1"): [_group_perm("g-1")]}),
        )
        monkeypatch.setattr(
            graph_client,
            "list_group_transitive_members",
            _members_fake({"g-1": [{"mail": "frank@example.com", "userPrincipalName": "frank@example.com"}]}),
        )

        async def _token_then_concurrent_sweep_write(tenant_id, client_id, private_key, *, client_secret=""):
            # The sync has already read its `connection` snapshot (in
            # `_run_acl_sync_async`) by the time it gets here — this is the
            # earliest point in `_sync_connection`'s body we can hook to
            # land a concurrent write. Simulate the weekly sweep committing
            # its own bookkeeping key in that window, strictly before the
            # sync writes its own.
            source_connections_repo().config_patch(conn_id, {"acl_sweep_last_full": "2026-08-30T00:00:00+00:00"})
            return "fake-token"

        monkeypatch.setattr(graph_client, "get_app_token", _token_then_concurrent_sweep_write)

        acl_sync.run_acl_sync({"connection_id": conn_id})

        config = source_connections_repo().get(conn_id)["config"]
        assert config.get("acl_sweep_last_full") == "2026-08-30T00:00:00+00:00", (
            "the sweep's concurrently-written key must survive the sync's own config write"
        )
        assert "acl_sync_last_run" in config
        assert config.get("acl_sync_last_success_at") is not None
