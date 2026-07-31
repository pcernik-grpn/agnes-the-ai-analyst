"""`_rebuild_duckdb_views` must build ONE view per partitioned table
(directory of parts) over a hive glob, while single-file tables keep their
stem view. Real parquet parts, so read_parquet actually resolves them.
"""
from __future__ import annotations

import duckdb

from cli.lib.pull import _rebuild_duckdb_views


def _write_parquet(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = duckdb.connect()
    try:
        c.execute(f"COPY (SELECT range AS id FROM range({n})) TO '{path}' (FORMAT PARQUET)")
    finally:
        c.close()


def _analytics(workspace):
    return duckdb.connect(str(workspace / "user" / "duckdb" / "analytics.duckdb"))


def test_partitioned_dir_builds_one_hive_view(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "issues" / "month=2026-06" / "data.parquet", 3)
    _write_parquet(pq / "issues" / "month=2026-07" / "data.parquet", 2)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 5
        # hive_partitioning surfaces `month` from the dir names
        assert conn.execute("SELECT count(DISTINCT month) FROM issues").fetchone()[0] == 2
    finally:
        conn.close()


def test_single_file_table_still_builds_stem_view(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 5)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 5
    finally:
        conn.close()


def test_mixed_single_and_partitioned(tmp_path):
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 4)
    _write_parquet(pq / "issues" / "month=2026-06" / "data.parquet", 7)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 4
        assert conn.execute("SELECT count(*) FROM issues").fetchone()[0] == 7
    finally:
        conn.close()


def test_staging_dir_is_ignored(tmp_path):
    """A leftover .staging-* dir from an interrupted sync must not become a view."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / ".staging-issues" / "month=2026-06" / "data.parquet", 3)
    _write_parquet(pq / "account.parquet", 1)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()}
        assert "account" in names
        assert not any(n.startswith(".staging") for n in names)
    finally:
        conn.close()


def test_flat_partitioned_layout_builds_queryable_view(tmp_path):
    """Keboola flat-partitioned layout ({key}.parquet, NO key=value dirs) must
    build a queryable view — hive_partitioning=true must not error on it
    (Devin re-review: flat layout untested)."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "cost" / "2025_11.parquet", 4)
    _write_parquet(pq / "cost" / "2025_12.parquet", 6)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cost").fetchone()[0] == 10
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# Local snapshots must survive the rebuild.
#
# `agnes snapshot create` writes `<workspace>/user/snapshots/<name>.parquet`
# and registers a view named `<name>`. The rebuild drops every view and used
# to re-create only those backed by `server/parquet`, so a snapshot's view was
# destroyed by the next pull and never came back — while `agnes snapshot list`
# (which reads the meta sidecars off disk) kept reporting it as present.
# ---------------------------------------------------------------------------


def _snap_dir(workspace):
    return workspace / "user" / "snapshots"


def test_snapshot_view_is_registered_by_the_rebuild(tmp_path):
    """The self-healing case: a snapshot parquet with no view gets one."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 1)
    _write_parquet(_snap_dir(tmp_path) / "cz_recent.parquet", 9)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cz_recent").fetchone()[0] == 9
    finally:
        conn.close()


def test_snapshot_view_survives_a_second_rebuild(tmp_path):
    """The reported bug: the snapshot worked, then a pull silently killed it."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 1)
    _write_parquet(_snap_dir(tmp_path) / "cz_recent.parquet", 9)

    _rebuild_duckdb_views(tmp_path, pq)
    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM cz_recent").fetchone()[0] == 9
    finally:
        conn.close()


def test_server_table_wins_a_name_collision_with_a_snapshot(tmp_path):
    """A registered table is canonical: a same-named snapshot must not shadow it."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 4)
    _write_parquet(_snap_dir(tmp_path) / "account.parquet", 99)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 4
    finally:
        conn.close()


def test_snapshot_does_not_shadow_a_user_base_table(tmp_path):
    """Same guard the server-parquet loop already honors."""
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    _write_parquet(_snap_dir(tmp_path) / "scratch.parquet", 7)

    db_path = tmp_path / "user" / "duckdb" / "analytics.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE scratch AS SELECT 1 AS id")
    finally:
        conn.close()

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM scratch").fetchone()[0] == 1
        kind = conn.execute(
            "SELECT table_type FROM information_schema.tables WHERE table_name='scratch'"
        ).fetchone()[0]
        assert kind == "BASE TABLE"
    finally:
        conn.close()


def test_missing_snapshots_dir_is_a_no_op(tmp_path):
    """Most workspaces have never created a snapshot."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 2)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 2
    finally:
        conn.close()


def test_corrupt_snapshot_is_skipped_without_aborting_the_rebuild(tmp_path):
    """A truncated snapshot must not cost the user their server-table views."""
    pq = tmp_path / "server" / "parquet"
    _write_parquet(pq / "account.parquet", 3)
    snaps = _snap_dir(tmp_path)
    snaps.mkdir(parents=True, exist_ok=True)
    (snaps / "broken.parquet").write_bytes(b"not a parquet file")
    _write_parquet(snaps / "good.parquet", 5)

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM good").fetchone()[0] == 5
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables").fetchall()}
        assert "broken" not in names
    finally:
        conn.close()


def test_meta_sidecar_is_not_mistaken_for_a_snapshot(tmp_path):
    """`<name>.meta.json` sits next to `<name>.parquet`; only the parquet counts."""
    pq = tmp_path / "server" / "parquet"
    pq.mkdir(parents=True, exist_ok=True)
    snaps = _snap_dir(tmp_path)
    _write_parquet(snaps / "cz_recent.parquet", 3)
    (snaps / "cz_recent.meta.json").write_text('{"name": "cz_recent"}', encoding="utf-8")

    _rebuild_duckdb_views(tmp_path, pq)

    conn = _analytics(tmp_path)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW'").fetchall()}
        assert names == {"cz_recent"}
    finally:
        conn.close()
