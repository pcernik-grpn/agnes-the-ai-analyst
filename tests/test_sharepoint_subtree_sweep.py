"""Tests for the ``sharepoint-subtree-sweep`` worker job body (2026-08-30
plan, Task 7 — ``connectors/sharepoint/acl_sync.py::run_subtree_sweep``).

Builds connection/scope rows directly via the repos under a fresh
``DATA_DIR`` (same idiom as ``tests/test_sharepoint_acl_sync.py``). Graph is
never called: ``graph_client.get_app_token``, ``graph_client
.list_item_children`` and ``graph_client.probe_unique_permissions`` are
monkeypatched with async fakes on the ``graph_client`` module itself (the
sweep body calls them module-qualified — ``graph_client.xxx(...)`` —
specifically so this monkeypatch idiom works).
"""

from __future__ import annotations

import pytest

from connectors.sharepoint import acl_sync, graph_client

CONN_ID = "conn-sweep-1"


@pytest.fixture
def sweep_env(tmp_path, monkeypatch):
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


def _make_connection(connection_id: str = CONN_ID, *, config_extra: "dict | None" = None) -> str:
    from src.repositories import source_connections_repo

    config = {"tenant_id": "tenant-1", "client_id": "client-1", "scopes": []}
    if config_extra:
        config.update(config_extra)
    source_connections_repo().create(
        id=connection_id,
        name="Test SharePoint",
        source_type="sharepoint",
        config=config,
    )
    return connection_id


def _add_scope(
    connection_id: str,
    *,
    source_scope_id: str,
    collection_id: str,
    drive_id: "str | None" = "drive-1",
    access_mode: str = "mirrored",
    display_path: "str | None" = None,
    excluded_subtrees: "list | None" = None,
) -> None:
    from src.repositories import source_connections_repo

    repo = source_connections_repo()
    row = repo.get(connection_id)
    config = dict(row.get("config") or {})
    scopes = list(config.get("scopes") or [])
    scope = {
        "source_scope_id": source_scope_id,
        "display_path": display_path or source_scope_id,
        "anonymize": False,
        "collection_id": collection_id,
        "drive_id": drive_id,
        "access_mode": access_mode,
    }
    if excluded_subtrees is not None:
        scope["excluded_subtrees"] = excluded_subtrees
    scopes.append(scope)
    config["scopes"] = scopes
    repo.update(connection_id, config=config)


def _make_collection(name: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(
        name=name, slug=name.lower().replace(" ", "-"), description=None, created_by="test"
    )


def _connection_scope(connection_id: str, source_scope_id: str) -> dict:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(connection_id)
    scopes = (row.get("config") or {}).get("scopes") or []
    return next(s for s in scopes if s.get("source_scope_id") == source_scope_id)


def _last_run(connection_id: str = CONN_ID) -> dict:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(connection_id)
    return (row.get("config") or {}).get("acl_sweep_last_run") or {}


# ---------------------------------------------------------------------------
# graph fakes — a fixed tree: root -> A(unique), B(clean); B -> C(unique).
# A's children must NEVER be listed/probed (the whole point of "exclude,
# never descend").
# ---------------------------------------------------------------------------


async def _fake_get_app_token(tenant_id, client_id, private_key, *, client_secret=""):
    return "fake-token"


def _tree_fakes():
    async def fake_children(token, drive_id, item_id):
        if item_id == "root":
            return [
                {"id": "A", "name": "A", "is_folder": True, "child_count": 1},
                {"id": "B", "name": "B", "is_folder": True, "child_count": 1},
            ]
        if item_id == "B":
            return [{"id": "C", "name": "C", "is_folder": True, "child_count": 0}]
        if item_id == "C":
            return []
        if item_id == "A":
            pytest.fail("A/child must never be probed — A was already excluded as broken-inheritance")
        raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

    async def fake_probe(token, drive_id, item_ids):
        flags = {"A": True, "B": False, "C": True}
        return {i: flags.get(i) for i in item_ids}

    return fake_children, fake_probe


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestBrokenInheritanceWalk:
    def test_excludes_subtree_roots_and_never_descends_into_them(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("Sweep Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        fake_children, fake_probe = _tree_fakes()
        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["connections"] == 1
        assert result["scopes"] == 1
        assert result["excluded"] == 2
        assert result["errors"] == []

        scope = _connection_scope(conn_id, "root")
        paths = {(item["item_id"], item["path"]) for item in scope["excluded_subtrees"]}
        assert paths == {("A", "Root/A"), ("C", "Root/B/C")}
        assert all("detected_at" in item for item in scope["excluded_subtrees"])

        last_run = _last_run(conn_id)
        assert last_run["ok"] is True
        assert last_run["excluded"] == 2
        # One "list children" call each for root and B, plus one $batch probe
        # call each for [A, B] and for [C] — never for A's own children.
        assert last_run["requests"] == 4
        assert last_run["truncated"] is False

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert row["config"]["acl_sweep_last_full"] == last_run["at"]

    def test_manual_scope_is_never_swept(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_mirrored = _make_collection("Mirrored Col")
        col_manual = _make_collection("Manual Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_mirrored, display_path="Root")
        _add_scope(conn_id, source_scope_id="manual-1", collection_id=col_manual, access_mode="manual")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        fake_children, fake_probe = _tree_fakes()
        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["scopes"] == 1  # only the mirrored scope is processed
        manual_scope = _connection_scope(conn_id, "manual-1")
        assert "excluded_subtrees" not in manual_scope


class TestMissingDriveId:
    def test_scope_without_drive_id_is_skipped_not_crashed(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("No Drive Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, drive_id=None)

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == [{"connection_id": conn_id, "error": "missing_drive_id"}]
        last_run = _last_run(conn_id)
        assert last_run["ok"] is False
        assert last_run["error"] == "missing_drive_id"


class TestGraphErrorFailClosed:
    def test_error_mid_walk_keeps_previous_exclusions_and_does_not_advance_last_full(self, sweep_env, monkeypatch):
        from connectors.sharepoint.graph_client import SharePointGraphError

        conn_id = _make_connection()
        col_id = _make_collection("Flaky Col")
        previous = [{"item_id": "OLD", "path": "Root/OLD", "detected_at": "2026-08-20T00:00:00+00:00"}]
        _add_scope(
            conn_id,
            source_scope_id="root",
            collection_id=col_id,
            display_path="Root",
            excluded_subtrees=previous,
        )

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def boom(token, drive_id, item_id):
            raise SharePointGraphError("simulated outage")

        monkeypatch.setattr(graph_client, "list_item_children", boom)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"][0]["error"] == "simulated outage"
        scope = _connection_scope(conn_id, "root")
        # PREVIOUS exclusion list untouched — fail-closed, no partial diff.
        assert scope["excluded_subtrees"] == previous

        from src.repositories import source_connections_repo

        row = source_connections_repo().get(conn_id)
        assert "acl_sweep_last_full" not in (row.get("config") or {})


class TestFeatureFlagOff:
    def test_returns_skipped_when_disabled(self, sweep_env, monkeypatch):
        monkeypatch.setenv("AGNES_ACL_MIRRORING_ENABLED", "false")
        result = acl_sync.run_subtree_sweep({"connection_id": CONN_ID})
        assert result == {"skipped": "acl_mirroring disabled"}


class TestSweepDueSelfGuard:
    def test_sweep_all_skips_a_recently_swept_connection(self, sweep_env, monkeypatch):
        from datetime import datetime, timezone

        conn_id = _make_connection()
        col_id = _make_collection("Recently Swept Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        from src.repositories import source_connections_repo

        repo = source_connections_repo()
        row = repo.get(conn_id)
        config = dict(row["config"])
        config["acl_sweep_last_full"] = datetime.now(timezone.utc).isoformat()
        repo.update(conn_id, config=config)

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        fake_children, fake_probe = _tree_fakes()
        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": None})

        assert result["connections"] == 0
        assert result["skipped_not_due"] == 1

    def test_explicit_connection_id_bypasses_the_due_guard(self, sweep_env, monkeypatch):
        from datetime import datetime, timezone

        conn_id = _make_connection()
        col_id = _make_collection("Explicit Sweep Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        from src.repositories import source_connections_repo

        repo = source_connections_repo()
        row = repo.get(conn_id)
        config = dict(row["config"])
        config["acl_sweep_last_full"] = datetime.now(timezone.utc).isoformat()
        repo.update(conn_id, config=config)

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)
        fake_children, fake_probe = _tree_fakes()
        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["connections"] == 1
        assert result["skipped_not_due"] == 0


class TestUnknownProbeFailsClosed:
    def test_none_probe_result_is_excluded_too(self, sweep_env, monkeypatch):
        """An unreadable signal (probe returns None — "unknown") is treated
        the SAME as a detected break: fail-closed, never rendered as
        "clean" just because the signal was unavailable."""
        conn_id = _make_connection()
        col_id = _make_collection("Unknown Probe Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [{"id": "X", "name": "X", "is_folder": True, "child_count": 0}]
            raise AssertionError(f"X must never be probed further: {item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            return {i: None for i in item_ids}

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["excluded"] == 1
        scope = _connection_scope(conn_id, "root")
        assert scope["excluded_subtrees"][0]["item_id"] == "X"
        assert _last_run(conn_id)["unknown_probes"] == 1
