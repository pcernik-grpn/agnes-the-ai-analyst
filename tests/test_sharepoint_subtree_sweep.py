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


def _zones(connection_id: str = CONN_ID) -> list:
    from src.repositories import source_connections_repo

    row = source_connections_repo().get(connection_id)
    return (row.get("config") or {}).get("acl_zones") or []


def _audit_count(action: "str | None" = None) -> int:
    from src.repositories import audit_repo

    rows, _ = audit_repo().query(action=action, limit=1000)
    return len(rows)


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


# ---------------------------------------------------------------------------
# 2026-08-31 plan, Task 3 — file-level probes, drive-relative paths,
# permission zones.
# ---------------------------------------------------------------------------


class TestFileProbing:
    def test_file_with_unique_permissions_is_excluded(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("File Probe Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        descended: list = []

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [
                    {"id": "A", "name": "A", "is_folder": True, "child_count": 0},
                    {"id": "F", "name": "F.docx", "is_folder": False, "child_count": 0},
                ]
            if item_id == "A":
                descended.append(item_id)
                return []
            raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            flags = {"A": False, "F": True}
            return {i: flags.get(i) for i in item_ids}

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        scope = _connection_scope(conn_id, "root")
        excluded = scope["excluded_subtrees"]
        assert len(excluded) == 1
        entry = excluded[0]
        assert entry["item_id"] == "F"
        assert entry["kind"] == "file"
        assert entry["rel_path"] == "F.docx"
        assert entry["path"] == "Root/F.docx"
        assert descended == ["A"], "folder A must still be descended into"


class TestPermissionZones:
    def test_folder_break_becomes_zone_when_switch_on(self, sweep_env, monkeypatch):
        monkeypatch.setenv("AGNES_ACL_ZONES_ENABLED", "true")
        conn_id = _make_connection()
        col_id = _make_collection("Zone Parent Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        descended: list = []

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [{"id": "Z", "name": "Z", "is_folder": True, "child_count": 1}]
            if item_id == "Z":
                descended.append(item_id)
                return [{"id": "C", "name": "Child", "is_folder": True, "child_count": 0}]
            if item_id == "C":
                descended.append(item_id)
                return []
            raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            flags = {"Z": True, "C": False}
            return {i: flags.get(i) for i in item_ids}

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        scope = _connection_scope(conn_id, "root")
        assert scope["excluded_subtrees"] == [], "the zone root itself must never be excluded"
        assert descended == ["Z", "C"], "the walk must descend into a newly-created zone"

        zones = _zones(conn_id)
        assert len(zones) == 1
        zone = zones[0]
        assert zone["zone_item_id"] == "Z"
        assert zone["status"] == "active"
        assert zone["parent_scope_id"] == "root"
        assert zone["collection_id"].startswith("col_")

        from src.repositories import file_corpora_repo

        assert file_corpora_repo().get(zone["collection_id"]) is not None
        assert _audit_count("sharepoint_acl.zone_created") == 1

    def test_zone_dissolves_when_inheritance_relinks(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("Zone Dissolve Parent Col")
        zone_col_id = _make_collection("Zone Dissolve Zone Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        from src.repositories import source_connections_repo

        repo = source_connections_repo()
        row = repo.get(conn_id)
        config = dict(row["config"])
        config["acl_zones"] = [
            {
                "zone_item_id": "Z",
                "parent_scope_id": "root",
                "drive_id": "drive-1",
                "name": "Z",
                "display_path": "Root/Z",
                "rel_path": "Z",
                "collection_id": zone_col_id,
                "detected_at": "2026-08-20T00:00:00+00:00",
                "status": "active",
            }
        ]
        repo.update(conn_id, config=config)

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [{"id": "Z", "name": "Z", "is_folder": True, "child_count": 0}]
            if item_id == "Z":
                return []
            raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            return {i: False for i in item_ids}

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        zones = _zones(conn_id)
        assert len(zones) == 1
        assert zones[0]["status"] == "dissolved"
        assert _audit_count("sharepoint_acl.zone_dissolved") == 1

    def test_unknown_probe_never_creates_zone(self, sweep_env, monkeypatch):
        monkeypatch.setenv("AGNES_ACL_ZONES_ENABLED", "true")
        conn_id = _make_connection()
        col_id = _make_collection("Zone Unknown Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [{"id": "U", "name": "U", "is_folder": True, "child_count": 0}]
            raise AssertionError(f"U must never be probed further: {item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            return {i: None for i in item_ids}

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        scope = _connection_scope(conn_id, "root")
        assert scope["excluded_subtrees"][0]["item_id"] == "U"
        assert scope["excluded_subtrees"][0]["kind"] == "folder"
        assert _zones(conn_id) == []

    def test_rel_path_is_drive_relative(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("Rel Path Col")
        _add_scope(
            conn_id,
            source_scope_id="root2",
            collection_id=col_id,
            display_path="Site/Documents/Team",
        )

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def fake_children(token, drive_id, item_id):
            if item_id == "root2":
                return [{"id": "Sub", "name": "Sub", "is_folder": True, "child_count": 0}]
            raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            return {i: True for i in item_ids}

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        scope = _connection_scope(conn_id, "root2")
        entry = scope["excluded_subtrees"][0]
        assert entry["item_id"] == "Sub"
        assert entry["rel_path"] == "Team/Sub"
        assert entry["path"] == "Site/Documents/Team/Sub"


# ---------------------------------------------------------------------------
# 2026-08-31 plan, Task 6 — retroactive cleanup (DuckDB path-matching path;
# stable-id matching on PG is covered by
# tests/db_pg/test_sharepoint_acl_phase_pg.py).
# ---------------------------------------------------------------------------


def _add_corpus_file(collection_id: str, *, path: str, filename: "str | None" = None) -> str:
    from src.repositories import corpus_files_repo

    return corpus_files_repo().add(
        corpus_id=collection_id,
        filename=filename or path.rsplit("/", 1)[-1],
        sha256="0" * 64,
        file_type="text/plain",
        size_bytes=1,
        storage_path=None,
        path=path,
    )


def _corpus_file_paths(collection_id: str) -> set:
    from src.repositories import corpus_files_repo

    return {row["path"] for row in corpus_files_repo().list_for_corpus(collection_id)}


class TestRetroactiveCleanup:
    def test_excluded_folder_purges_matching_files_keeps_the_rest(self, sweep_env, monkeypatch):
        conn_id = _make_connection()
        col_id = _make_collection("Cleanup Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        _add_corpus_file(col_id, path="Secret/a.docx")
        _add_corpus_file(col_id, path="open/keep.docx")

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [{"id": "Secret", "name": "Secret", "is_folder": True, "child_count": 0}]
            raise AssertionError(f"Secret must never be probed further: {item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            return {i: True for i in item_ids}  # broken inheritance -> excluded (zones off)

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        assert _corpus_file_paths(col_id) == {"open/keep.docx"}
        assert _audit_count("sharepoint_acl.content_purged") == 1
        last_run = _last_run(conn_id)
        assert last_run["removed_files"] == 1
        assert last_run["dissolved_zones"] == 0

    def test_dissolved_zone_is_fully_retired(self, sweep_env, monkeypatch):
        from app.resource_types import ResourceType
        from src.repositories import file_corpora_repo, resource_grants_repo, source_connections_repo, user_groups_repo

        conn_id = _make_connection()
        col_id = _make_collection("Dissolve Parent Col")
        zone_col_id = _make_collection("Dissolve Zone Col")
        _add_scope(conn_id, source_scope_id="root", collection_id=col_id, display_path="Root")

        repo = source_connections_repo()
        row = repo.get(conn_id)
        config = dict(row["config"])
        config["acl_zones"] = [
            {
                "zone_item_id": "Z",
                "parent_scope_id": "root",
                "drive_id": "drive-1",
                "name": "Z",
                "display_path": "Root/Z",
                "rel_path": "Z",
                "collection_id": zone_col_id,
                "detected_at": "2026-08-20T00:00:00+00:00",
                "status": "active",
            }
        ]
        repo.update(conn_id, config=config)

        _add_corpus_file(zone_col_id, path="Z/report.docx")

        group = user_groups_repo().create(
            name="entra:zone-sentinel", description=None, created_by=acl_sync.ACL_SYNC_SENTINEL
        )
        resource_grants_repo().ensure_grant(
            group["id"], ResourceType.COLLECTION.value, zone_col_id, assigned_by=acl_sync.ACL_SYNC_SENTINEL
        )

        monkeypatch.setattr(graph_client, "get_app_token", _fake_get_app_token)

        async def fake_children(token, drive_id, item_id):
            if item_id == "root":
                return [{"id": "Z", "name": "Z", "is_folder": True, "child_count": 0}]
            if item_id == "Z":
                return []
            raise AssertionError(f"unexpected list_item_children call for item_id={item_id!r}")

        async def fake_probe(token, drive_id, item_ids):
            return {i: False for i in item_ids}  # inheritance re-linked -> dissolve

        monkeypatch.setattr(graph_client, "list_item_children", fake_children)
        monkeypatch.setattr(graph_client, "probe_unique_permissions", fake_probe)

        result = acl_sync.run_subtree_sweep({"connection_id": conn_id})

        assert result["errors"] == []
        assert _corpus_file_paths(zone_col_id) == set()
        assert file_corpora_repo().get(zone_col_id) is None, "the dissolved zone's collection must be soft-deleted"
        assert [
            g
            for g in resource_grants_repo().list_all(resource_type=ResourceType.COLLECTION.value)
            if g.get("resource_id") == zone_col_id
        ] == [], "resource_grants rows for the dissolved zone's collection must be gone"

        last_run = _last_run(conn_id)
        assert last_run["removed_files"] == 1
        assert last_run["dissolved_zones"] == 1
