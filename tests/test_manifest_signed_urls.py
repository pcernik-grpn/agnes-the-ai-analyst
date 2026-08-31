"""Tests for manifest v2 presigned URLs (three-plane wave 2-H, WS F, task
WF-2 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

Targets `_build_manifest_for_user`'s flat `tables` dict directly — that is
the shape `cli/lib/pull.py:run_pull`'s download loop actually reads
(`manifest.get("tables", {})`, keyed by table id, hash-compared per row).
The typed `data_packages[].tables[]` section (`_table_manifest_entry`) is
only used by `run_pull` to build a name-based RBAC filter, never consulted
for hash/signed_url — so it intentionally does NOT get these fields (see
the wave plan task WF-2 + the module docstring in `app/api/sync.py`).

Covers: store+md5-match -> signed_url present; unmirrored table -> absent;
stale/mismatched md5 -> absent; remote/server_only -> never; mode=off ->
never (even with a store); no store configured -> manifest identical to
today (backward compat); RBAC (inaccessible table never appears, hence no
signed_url); mirror-index read failure -> fail-open, no 500.
"""

from __future__ import annotations

import importlib

import pytest

from tests.object_store_fakes import FakeObjectStore


def _reload_db_module(monkeypatch, tmp_path):
    """Point DATA_DIR at tmp_path and reload db module so paths take effect."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "state").mkdir(exist_ok=True)
    import src.db as db_module

    importlib.reload(db_module)
    return db_module


def _ensure_admin1(conn):
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository

    if UserRepository(conn).get_by_id("admin1") is None:
        UserRepository(conn).create(id="admin1", email="admin1@test.com", name="Admin")
    admin_gid = conn.execute("SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]).fetchone()
    if admin_gid:
        UserGroupMembersRepository(conn).add_member(
            "admin1",
            admin_gid[0],
            source="system_seed",
        )


def _seed_table(conn, table_id, *, query_mode="local", server_only=False, md5="abc123"):
    from src.repositories.sync_state import SyncStateRepository
    from src.repositories.table_registry import TableRegistryRepository

    TableRegistryRepository(conn).register(
        id=table_id,
        name=table_id,
        source_type="keboola",
        bucket="sales",
        source_table=table_id,
        query_mode=query_mode,
        server_only=server_only,
    )
    SyncStateRepository(conn).update_sync(
        table_id=table_id,
        rows=10,
        file_size_bytes=1024,
        hash=md5,
    )


@pytest.fixture(autouse=True)
def _reset_caches():
    from src.distribution import reset_mirror_index_cache

    reset_mirror_index_cache()
    yield
    reset_mirror_index_cache()


def _configure_store(monkeypatch, store, *, mode="auto"):
    monkeypatch.setattr("app.api.sync.object_store", lambda: store)
    monkeypatch.setattr("app.api.sync.distribution_signed_urls_mode", lambda: mode)


def test_signed_url_present_when_store_configured_and_md5_matches(tmp_path, monkeypatch):
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "orders", md5="abc123")

        store = FakeObjectStore()
        write_mirror_index(store, {"orders": "abc123"})
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["orders"]
        assert entry["signed_url"] == "https://fake-object-store.example.com/orders.parquet?ttl=900"
        assert entry["signed_url_expires_at"]
        # presign called with the documented key + TTL bound (15 min)
        assert store.presign_calls == [("orders.parquet", 900)]
    finally:
        conn.close()


def test_signed_url_absent_when_table_not_in_mirror_index(tmp_path, monkeypatch):
    """Unmirrored table (nothing uploaded yet) -> no signed_url, client falls
    back to /api/data/{id}/download."""
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "orders", md5="abc123")

        store = FakeObjectStore()
        write_mirror_index(store, {})  # empty index — nothing mirrored yet
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["orders"]
        assert "signed_url" not in entry
        assert "signed_url_expires_at" not in entry
    finally:
        conn.close()


def test_signed_url_absent_when_mirror_md5_is_stale(tmp_path, monkeypatch):
    """Index carries the table but with a stale md5 (mirror hasn't caught up
    with the latest sync) -> no signed_url."""
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "orders", md5="new-hash")

        store = FakeObjectStore()
        write_mirror_index(store, {"orders": "stale-hash"})
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["orders"]
        assert "signed_url" not in entry
    finally:
        conn.close()


def test_signed_url_never_for_remote_tables(tmp_path, monkeypatch):
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "bq_table", query_mode="remote", md5="abc123")

        store = FakeObjectStore()
        write_mirror_index(store, {"bq_table": "abc123"})
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["bq_table"]
        assert "signed_url" not in entry
    finally:
        conn.close()


def test_signed_url_never_for_server_only_tables(tmp_path, monkeypatch):
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "big_table", server_only=True, md5="abc123")

        store = FakeObjectStore()
        write_mirror_index(store, {"big_table": "abc123"})
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["big_table"]
        assert "signed_url" not in entry
    finally:
        conn.close()


def test_signed_url_absent_when_mode_off_even_with_store(tmp_path, monkeypatch):
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "orders", md5="abc123")

        store = FakeObjectStore()
        write_mirror_index(store, {"orders": "abc123"})
        _configure_store(monkeypatch, store, mode="off")

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["orders"]
        assert "signed_url" not in entry
        assert store.presign_calls == []
    finally:
        conn.close()


def test_manifest_unchanged_when_object_store_is_none(tmp_path, monkeypatch):
    """No store configured -> manifest identical to today; no new keys at
    all (backward compat for old CLIs and byte-for-byte parity elsewhere in
    the test suite)."""
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "orders", md5="abc123")

        monkeypatch.setattr("app.api.sync.object_store", lambda: None)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["orders"]
        assert set(entry.keys()) == {
            "hash",
            "updated",
            "size_bytes",
            "rows",
            "query_mode",
            "server_only",
            "source_type",
        }
    finally:
        conn.close()


def test_rbac_inaccessible_table_never_gets_signed_url(tmp_path, monkeypatch):
    """A table the caller cannot access is absent from the manifest entirely
    (existing RBAC filter, upstream of this feature) -- so it can never
    carry a signed_url. Signed URLs never widen access."""
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index
    from src.repositories.users import UserRepository
    from src.repositories.user_groups import UserGroupsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.data_packages import DataPackagesRepository

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        UserRepository(conn).create(id="analyst1", email="analyst@test.com", name="Analyst")

        _seed_table(conn, "orders", md5="abc123")
        _seed_table(conn, "hidden", md5="def456")

        group = UserGroupsRepository(conn).create(name="ManifestSignedGroup", description="", created_by="test")
        gid = group["id"] if isinstance(group, dict) else group
        UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")

        pkg_repo = DataPackagesRepository(conn)
        pkg_id = pkg_repo.create(
            name="OrdersPkg",
            slug="orders-pkg-signed",
            description=None,
            icon=None,
            color=None,
            created_by="test",
        )
        pkg_repo.add_table(pkg_id, "orders", added_by="test")
        conn.execute(
            "INSERT INTO resource_grants(id, group_id, resource_type, resource_id, "
            "requirement, assigned_at, assigned_by) "
            "VALUES (?, ?, 'data_package', ?, 'required', CURRENT_TIMESTAMP, 'test')",
            ["grant-orders-pkg-signed", gid, pkg_id],
        )

        store = FakeObjectStore()
        write_mirror_index(store, {"orders": "abc123", "hidden": "def456"})
        _configure_store(monkeypatch, store)

        analyst = {"id": "analyst1", "email": "analyst@test.com"}
        manifest = _build_manifest_for_user(conn, analyst)
        assert set(manifest["tables"].keys()) == {"orders"}
        assert manifest["tables"]["orders"]["signed_url"]
    finally:
        conn.close()


def test_signed_url_never_for_internal_rbac_tables(tmp_path, monkeypatch):
    """Internal row-level-RBAC tables (``agnes_sessions`` / ``agnes_telemetry``
    / ``agnes_audit``) enforce access via a per-request row filter
    (`src.rbac.get_accessible_tables`), never via the sync_state/mirror
    pipeline's whole-parquet distribution. Even if one of these tables
    somehow ended up with a sync_state row + a matching mirror-index entry,
    `_apply_signed_url` must not hand out a signed_url for it — that would
    serve every user's rows to whoever holds the URL, bypassing the
    row-level filter entirely. Defense-in-depth: reuses
    `connectors.internal.access.is_internal_table`. Note the caller here is
    an ADMIN, for whom `get_accessible_tables` returns the "all" sentinel —
    so this guard, not the table gate, is what keeps the signed URL away
    (and it stays load-bearing now that internal tables are packageable and
    can legitimately reach a manifest)."""
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user
    from src.distribution import write_mirror_index

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "agnes_sessions", md5="abc123")

        store = FakeObjectStore()
        write_mirror_index(store, {"agnes_sessions": "abc123"})
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)
        entry = manifest["tables"]["agnes_sessions"]
        assert "signed_url" not in entry
        assert "signed_url_expires_at" not in entry
    finally:
        conn.close()


def test_mirror_index_read_failure_fails_open_no_signed_urls(tmp_path, monkeypatch):
    """A store outage while reading the marker index must degrade to "no
    signed_urls this cycle" -- never a manifest-build failure."""
    db_module = _reload_db_module(monkeypatch, tmp_path)
    from app.api.sync import _build_manifest_for_user

    conn = db_module.get_system_db()
    try:
        _ensure_admin1(conn)
        _seed_table(conn, "orders", md5="abc123")

        store = FakeObjectStore()
        store.fail_get_bytes = True
        _configure_store(monkeypatch, store)

        admin = {"id": "admin1", "email": "a@x.com"}
        manifest = _build_manifest_for_user(conn, admin)  # must not raise
        entry = manifest["tables"]["orders"]
        assert "signed_url" not in entry
    finally:
        conn.close()
