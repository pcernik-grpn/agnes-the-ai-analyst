"""SyncOrchestrator's DuckLake copy-ingest rebuild path (wave-2G, task 3).

Exercises the REAL ``ducklake`` DuckDB extension end-to-end — no mocking of
DuckLake itself. If the extension cannot be INSTALL/LOAD'ed in this
environment (offline, or a DuckDB build predating it), every test here is
skipped via ``pytestmark`` with a loud reason, same pattern as
``tests/test_ducklake_session.py``. This file never fakes success.

Fixtures build extracts shaped like the REAL connector contract — actual
parquet files under ``data/`` plus a minimal ``extract.duckdb`` whose
``_meta`` rows point local-mode tables at
``CREATE VIEW ... AS SELECT * FROM read_parquet(...)`` (see
``connectors/keboola/extractor.py::_register_local_meta``) — rather than
the simplified "real table baked directly into extract.duckdb" shape the
pre-existing ``tests/test_orchestrator.py`` fixture uses. That matters here
because the ducklake ingest path's whole job is reading real parquet-backed
extract data and copying it into the DuckLake catalog.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _extension_available() -> bool:
    try:
        probe = duckdb.connect(":memory:")
        try:
            probe.execute("INSTALL ducklake")
            probe.execute("LOAD ducklake")
        finally:
            probe.close()
        return True
    except Exception:
        return False


_DUCKLAKE_EXTENSION_AVAILABLE = _extension_available()

pytestmark = pytest.mark.skipif(
    not _DUCKLAKE_EXTENSION_AVAILABLE,
    reason=(
        "DuckDB 'ducklake' extension could not be INSTALL/LOAD'ed in this "
        "environment (offline, or DuckDB build predates the extension). "
        "Skipping real DuckLake orchestrator tests rather than faking "
        "success — see src/ducklake_session.py::ducklake_available()."
    ),
)


def _write_local_table(data_dir: Path, conn: duckdb.DuckDBPyConnection, name: str, rows: list[dict]) -> tuple[int, int]:
    """Write a real parquet file for *name* and register a
    ``read_parquet``-backed view over it in *conn* — mirrors
    ``connectors/keboola/extractor.py``'s local-mode contract exactly.

    Returns (row_count, size_bytes) for the caller's ``_meta`` insert.
    """
    pq_path = data_dir / f"{name}.parquet"
    if rows:
        cols = list(rows[0].keys())
        arrow_table = pa.table({c: [r[c] for r in rows] for c in cols})
    else:
        arrow_table = pa.table({"id": pa.array([], type=pa.string())})
    pq.write_table(arrow_table, pq_path)

    safe = str(pq_path).replace("'", "''")
    conn.execute(f"CREATE OR REPLACE VIEW \"{name}\" AS SELECT * FROM read_parquet('{safe}')")
    return len(rows), pq_path.stat().st_size


def _create_ducklake_extract(extracts_dir: Path, source_name: str, tables: list[dict]) -> None:
    """Build ``extracts_dir/source_name/{extract.duckdb, data/*.parquet}``.

    Each entry in *tables* is ``{"name": ..., "data": [...], "query_mode":
    "local"|"remote" (default "local"), "no_inner_object": bool (default
    False)}``. Remote-mode entries get a ``_meta`` row with no local
    parquet/inner-object at all — matching the real contract (their inner
    object is a view over an externally-ATTACHed extension, irrelevant here
    since the ducklake ingest path skips remote rows before ever trying to
    read them). ``no_inner_object=True`` simulates the local-mode analogue
    (e.g. keboola ``use_extension=False``): a ``_meta`` row is inserted but
    no backing view/parquet is ever created, so a read against the name
    fails with ``CatalogException``.

    Recreates the source directory from scratch if it already exists, so a
    test can call this twice for the same source to simulate "new sync
    happened" between two orchestrator calls.
    """
    source_dir = extracts_dir / source_name
    if source_dir.exists():
        import shutil

        shutil.rmtree(source_dir)
    source_dir.mkdir(parents=True)
    data_dir = source_dir / "data"
    data_dir.mkdir()

    db_path = source_dir / "extract.duckdb"
    conn = duckdb.connect(str(db_path))
    try:
        conn.execute(
            """CREATE TABLE _meta (
                table_name VARCHAR, description VARCHAR, rows BIGINT,
                size_bytes BIGINT, extracted_at TIMESTAMP,
                query_mode VARCHAR DEFAULT 'local'
            )"""
        )
        for t in tables:
            name = t["name"]
            rows_data = t.get("data", [])
            query_mode = t.get("query_mode", "local")

            if query_mode == "remote":
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?)",
                    [name, t.get("description", ""), len(rows_data), 0, "remote"],
                )
                continue

            if t.get("no_inner_object"):
                conn.execute(
                    "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?)",
                    [name, t.get("description", ""), len(rows_data), 0, query_mode],
                )
                continue

            row_count, size_bytes = _write_local_table(data_dir, conn, name, rows_data)
            conn.execute(
                "INSERT INTO _meta VALUES (?, ?, ?, ?, current_timestamp, ?)",
                [name, t.get("description", ""), row_count, size_bytes, query_mode],
            )
    finally:
        conn.close()


@pytest.fixture
def ducklake_env(tmp_path, monkeypatch):
    """Fresh DATA_DIR, ``analytics.backend=ducklake`` (DuckDB-file catalog,
    single-process default), and clean module singleton state for every
    test — reset before AND after so no state leaks across tests, mirroring
    ``tests/test_ducklake_session.py``'s fixture.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_ANALYTICS_BACKEND", "ducklake")
    for var in ("AGNES_DUCKLAKE_CATALOG_DSN", "AGNES_DUCKLAKE_DATA_PATH"):
        monkeypatch.delenv(var, raising=False)

    extracts_dir = tmp_path / "extracts"
    extracts_dir.mkdir()

    import src.analytics_backend as ab
    import src.ducklake_session as ds

    ab.reset_analytics_backend_cache()
    ds.close_ducklake_sessions()
    yield {"data_dir": tmp_path, "extracts_dir": extracts_dir}
    ds.close_ducklake_sessions()
    ab.reset_analytics_backend_cache()


class TestDucklakeRebuild:
    def test_rebuild_two_sources_tables_and_master_views_queryable(self, ducklake_env):
        from src.ducklake_session import get_ducklake_read
        from src.orchestrator import SyncOrchestrator

        _create_ducklake_extract(
            ducklake_env["extracts_dir"],
            "src_a",
            [{"name": "orders", "data": [{"id": "1", "total": "100"}]}],
        )
        _create_ducklake_extract(
            ducklake_env["extracts_dir"],
            "src_b",
            [{"name": "issues", "data": [{"key": "PROJ-1"}]}],
        )

        result = SyncOrchestrator().rebuild()

        assert set(result.get("src_a", [])) == {"orders"}
        assert set(result.get("src_b", [])) == {"issues"}

        r = get_ducklake_read()
        try:
            assert r.execute('SELECT total FROM lake."src_a"."orders" WHERE id=\'1\'').fetchone()[0] == "100"
            assert r.execute('SELECT key FROM lake."src_b"."issues"').fetchone()[0] == "PROJ-1"
            # Master views expose the same rows under the unqualified name
            # legacy's server.duckdb master views use, under lake.main.
            assert r.execute('SELECT total FROM lake."main"."orders"').fetchone()[0] == "100"
            assert r.execute('SELECT key FROM lake."main"."issues"').fetchone()[0] == "PROJ-1"
        finally:
            r.close()

    def test_rebuild_source_only_reingests_that_source(self, ducklake_env):
        """Incremental win: rebuild_source('src_a') must NOT touch src_b's
        DuckLake table/snapshot, even though src_b's own extract on disk
        changed in between."""
        from src.ducklake_session import close_ducklake_sessions, get_ducklake_read
        from src.orchestrator import SyncOrchestrator

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(extracts_dir, "src_a", [{"name": "orders", "data": [{"id": "1"}]}])
        _create_ducklake_extract(extracts_dir, "src_b", [{"name": "issues", "data": [{"key": "PROJ-1"}]}])

        orch = SyncOrchestrator()
        orch.rebuild()

        r = get_ducklake_read()
        baseline_snapshot_id = r.execute("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
        r.close()
        # Release the reader's own extract-source ATTACHes (per
        # get_ducklake_read()'s docstring: "newly added extract sources
        # become visible only on the next (re)open") before rewriting
        # extract.duckdb on disk below — otherwise the fresh
        # duckdb.connect() in _create_ducklake_extract collides with the
        # reader's still-live READ_ONLY attach of the same file path.
        close_ducklake_sessions()

        # Simulate new syncs landing on disk for BOTH sources — src_a gets
        # a second row, src_b gets a second row too.
        _create_ducklake_extract(extracts_dir, "src_a", [{"name": "orders", "data": [{"id": "1"}, {"id": "2"}]}])
        _create_ducklake_extract(
            extracts_dir,
            "src_b",
            [{"name": "issues", "data": [{"key": "PROJ-1"}, {"key": "PROJ-2"}]}],
        )

        tables = orch.rebuild_source("src_a")
        assert set(tables) == {"orders"}

        r = get_ducklake_read()
        try:
            # src_a WAS re-ingested — sees the new row.
            orders_rows = r.execute('SELECT id FROM lake."src_a"."orders" ORDER BY id').fetchall()
            assert [row[0] for row in orders_rows] == ["1", "2"]

            # src_b's ducklake table is UNTOUCHED — still the stale
            # one-row snapshot from the full rebuild, even though its
            # on-disk extract now has 2 rows.
            issues_rows = r.execute('SELECT key FROM lake."src_b"."issues"').fetchall()
            assert [row[0] for row in issues_rows] == ["PROJ-1"]

            # No new snapshot touched src_b's schema/table.
            new_snapshots = r.execute(
                "SELECT changes FROM lake.snapshots() WHERE snapshot_id > ?",
                [baseline_snapshot_id],
            ).fetchall()
        finally:
            r.close()

        assert new_snapshots, "rebuild_source('src_a') should have produced at least one new snapshot"
        for (changes,) in new_snapshots:
            for change_list in changes.values():
                if change_list:
                    for entry in change_list:
                        assert "src_b" not in str(entry), (
                            f"rebuild_source('src_a') must not touch src_b's schema, found: {entry}"
                        )

    def test_rebuild_source_invalid_name_raises(self, ducklake_env):
        """rebuild_source() validates the identifier BEFORE any backend
        dispatch, so an invalid name fails loudly the same way on the
        ducklake backend as on legacy — see
        tests/test_orchestrator.py::test_rebuild_source_invalid_name_raises."""
        from src.orchestrator import InvalidSourceNameError, SyncOrchestrator

        orch = SyncOrchestrator()
        with pytest.raises(InvalidSourceNameError):
            orch.rebuild_source("bad-name")

    def test_rebuild_records_invalid_source_name_as_error(self, ducklake_env):
        """A full rebuild() on the ducklake backend must attribute a
        skipped invalid-identifier source directory via
        last_rebuild_errors — not just a log WARNING — while still
        ingesting every valid source in the same batch. DuckLake
        counterpart to
        tests/test_orchestrator.py::test_rebuild_records_invalid_source_name_as_error."""
        from src.orchestrator import SyncOrchestrator

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(extracts_dir, "src_a", [{"name": "orders", "data": [{"id": "1"}]}])

        bad_dir = extracts_dir / "bad-name"
        bad_dir.mkdir()
        (bad_dir / "data").mkdir()
        conn = duckdb.connect(str(bad_dir / "extract.duckdb"))
        try:
            conn.execute(
                """CREATE TABLE _meta (
                    table_name VARCHAR, description VARCHAR, rows BIGINT,
                    size_bytes BIGINT, extracted_at TIMESTAMP,
                    query_mode VARCHAR DEFAULT 'local'
                )"""
            )
        finally:
            conn.close()

        orch = SyncOrchestrator()
        result = orch.rebuild()

        assert "bad-name" not in result
        assert "bad-name" in orch.last_rebuild_errors
        assert "must match" in orch.last_rebuild_errors["bad-name"]
        assert "src_a" in result

    def test_view_name_collision_honors_ownership(self, ducklake_env):
        """Two sources both publish a table named 'shared'. First-come-
        first-served (alphabetical iteration order): src_a wins, src_b's
        colliding table is refused — same view_ownership semantics as the
        legacy backend (tests/test_view_collision_detection.py)."""
        from src.ducklake_session import get_ducklake_read
        from src.orchestrator import SyncOrchestrator
        from src.repositories import view_ownership_repo

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(
            extracts_dir,
            "src_a",
            [{"name": "shared", "data": [{"id": "a1"}]}, {"name": "a_only", "data": [{"id": "1"}]}],
        )
        _create_ducklake_extract(
            extracts_dir,
            "src_b",
            [{"name": "shared", "data": [{"id": "b1"}]}, {"name": "b_only", "data": [{"id": "2"}]}],
        )

        result = SyncOrchestrator().rebuild()

        assert "shared" in result["src_a"]
        assert "a_only" in result["src_a"]
        assert "shared" not in result["src_b"], "src_b should NOT have published a colliding 'shared' table"
        assert "b_only" in result["src_b"]

        repo = view_ownership_repo()
        assert repo.get_owner("shared") == "src_a"
        assert repo.get_owner("a_only") == "src_a"
        assert repo.get_owner("b_only") == "src_b"

        r = get_ducklake_read()
        try:
            # src_a's data wins; no lake."src_b"."shared" table was created.
            assert r.execute('SELECT id FROM lake."main"."shared"').fetchone()[0] == "a1"
            with pytest.raises(duckdb.Error):
                r.execute('SELECT * FROM lake."src_b"."shared"').fetchall()
        finally:
            r.close()

    def test_remote_table_skipped_but_claims_ownership(self, ducklake_env):
        """query_mode='remote' rows are not copy-ingested into the lake
        catalog (no local parquet backs them), but still occupy the
        view_ownership namespace so a local table elsewhere can't silently
        steal the name."""
        from src.orchestrator import SyncOrchestrator
        from src.repositories import view_ownership_repo

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(
            extracts_dir,
            "bigquery",
            [{"name": "web_sessions", "data": [{"x": "1"}], "query_mode": "remote"}],
        )
        _create_ducklake_extract(
            extracts_dir,
            "keboola",
            [{"name": "orders", "data": [{"id": "1"}]}],
        )

        result = SyncOrchestrator().rebuild()

        # Remote table is not part of the ducklake-ingested table list...
        assert "web_sessions" not in result.get("bigquery", [])
        assert "orders" in result.get("keboola", [])

        # ...but it still claimed its name in view_ownership.
        repo = view_ownership_repo()
        assert repo.get_owner("web_sessions") == "bigquery"

    def test_writer_creates_remote_wrapper_view_for_registered_remote_table(self, ducklake_env):
        """A registered query_mode='remote' table gets its
        ``lake.main.<name>`` wrapper view created by the WRITER during
        rebuild (moved off the per-request reader path). Asserted directly
        on the writer connection — the view exists and resolves."""
        from tests.conftest import create_mock_extract
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.orchestrator import SyncOrchestrator
        from src.ducklake_session import get_ducklake_write

        extracts_dir = ducklake_env["extracts_dir"]
        # create_mock_extract (unlike this file's _create_ducklake_extract)
        # builds the remote table's inner object — mirroring the real
        # extractor, which writes a `CREATE VIEW "<name>" AS SELECT * FROM
        # bq...` into extract.duckdb — which the writer wrapper references.
        create_mock_extract(
            extracts_dir,
            "bigquery",
            [{"name": "web_sessions", "data": [], "query_mode": "remote"}],
        )
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="bigquery.web_sessions",
                name="web_sessions",
                source_type="bigquery",
                bucket="dataset",
                source_table="web_sessions",
                query_mode="remote",
            )
        finally:
            conn.close()

        SyncOrchestrator().rebuild()

        w = get_ducklake_write()
        try:
            found = w.execute(
                "SELECT table_name FROM information_schema.views "
                "WHERE table_catalog='lake' AND table_schema='main' AND table_name='web_sessions'"
            ).fetchall()
            assert found == [("web_sessions",)], "writer must own the remote wrapper view"
        finally:
            w.close()

    def test_full_rebuild_drops_stale_remote_wrapper_view(self, ducklake_env):
        """De-registering a remote table and re-running a FULL rebuild
        drops its now-dangling ``lake.main`` wrapper view (the long-lived
        DuckLake catalog otherwise leaks it forever)."""
        from tests.conftest import create_mock_extract
        from src.db import get_system_db
        from src.repositories.table_registry import TableRegistryRepository
        from src.orchestrator import SyncOrchestrator
        from src.ducklake_session import get_ducklake_write

        extracts_dir = ducklake_env["extracts_dir"]
        create_mock_extract(
            extracts_dir,
            "bigquery",
            [{"name": "web_sessions", "data": [], "query_mode": "remote"}],
        )
        conn = get_system_db()
        try:
            TableRegistryRepository(conn).register(
                id="bigquery.web_sessions",
                name="web_sessions",
                source_type="bigquery",
                bucket="dataset",
                source_table="web_sessions",
                query_mode="remote",
            )
        finally:
            conn.close()

        SyncOrchestrator().rebuild()

        def _wrapper_exists() -> bool:
            w = get_ducklake_write()
            try:
                return bool(
                    w.execute(
                        "SELECT 1 FROM information_schema.views "
                        "WHERE table_catalog='lake' AND table_schema='main' "
                        "AND table_name='web_sessions'"
                    ).fetchall()
                )
            finally:
                w.close()

        assert _wrapper_exists()

        # De-register the remote table + drop it from the extract, then
        # rebuild: the reconcile must drop the stale wrapper view.
        conn = get_system_db()
        try:
            conn.execute("DELETE FROM table_registry WHERE id = ?", ["bigquery.web_sessions"])
        finally:
            conn.close()
        import shutil

        shutil.rmtree(extracts_dir / "bigquery")

        SyncOrchestrator().rebuild()

        assert not _wrapper_exists(), "stale remote wrapper view must be dropped on full rebuild"

    def test_inner_object_less_row_does_not_block_other_sources_claim(self, ducklake_env):
        """Legacy-parity ordering (review finding): a ``_meta`` row with no
        backing inner object (e.g. keboola ``use_extension=False``) must be
        skipped BEFORE the view-ownership claim, not after — otherwise it
        wins a name collision it has no real data for and blocks a
        DIFFERENT source's legitimate table of the same name.

        Source ``src_a`` (alphabetically first, so processed first) has an
        inner-object-less ``foo``; source ``src_b`` has a real ``foo``.
        Legacy semantics (and the fixed ordering here): ``src_b``'s ``foo``
        is exposed, since ``src_a``'s row never had anything to claim on
        behalf of."""
        from src.orchestrator import SyncOrchestrator
        from src.repositories import view_ownership_repo

        extracts_dir = ducklake_env["extracts_dir"]
        _create_ducklake_extract(
            extracts_dir,
            "src_a",
            [{"name": "foo", "no_inner_object": True}],
        )
        _create_ducklake_extract(
            extracts_dir,
            "src_b",
            [{"name": "foo", "data": [{"id": "real"}]}],
        )

        result = SyncOrchestrator().rebuild()

        assert "foo" not in result.get("src_a", [])
        assert "foo" in result.get("src_b", [])

        repo = view_ownership_repo()
        assert repo.get_owner("foo") == "src_b"

        from src.ducklake_session import get_ducklake_read

        r = get_ducklake_read()
        try:
            assert r.execute('SELECT id FROM lake."main"."foo"').fetchone()[0] == "real"
        finally:
            r.close()

    def test_ingest_bounded_memory_streams_large_table(self, ducklake_env):
        """IMPORTANT review finding: the DuckLake copy-ingest read
        (``ro.sql(...).to_arrow_reader(batch_size=...)``) must stream the
        source table batch-by-batch rather than materializing it fully in
        Python-process memory. This builds a real ~235MB/20M-row
        parquet-backed extract and runs the real ingest — under a tightened
        DuckLake writer memory budget — in a CHILD process, asserting it
        completes with the correct row count AND that the child's peak RSS
        stays well below what a full-table materialization would need.

        Why two child processes, and why the ingest child measures its OWN
        before/after RSS rather than the parent reading
        ``RUSAGE_CHILDREN``: building the 20M-row pyarrow fixture inflates
        whatever process does it (``ru_maxrss`` is a monotonic high-water
        mark that never comes back down within a process lifetime) well
        past anything the ingest itself needs, and so does the cost of
        importing the full app stack (``src.orchestrator`` pulls in
        FastAPI, the BigQuery connector, etc.). Measuring the *parent*
        test's RSS before/after (or reading ``RUSAGE_CHILDREN``, which
        reports the child's TOTAL lifetime peak including its own imports)
        folds those fixed costs into the delta and swamps the actual
        ingest signal — that version of this test passed even when
        manually reverted to a fully materializing read, which is not the
        load-bearing test the review asked for. Splitting fixture-build
        (``build``) and ingest (``ingest``) into separate processes, and
        having the ``ingest`` child take its "before" snapshot only AFTER
        all its imports are done, isolates the measurement to the ingest
        call alone.

        Note on what this actually proves: DuckDB's own ``memory_limit``
        PRAGMA does not bound a fully-materialized ``pyarrow.Table`` (that
        memory lives in pyarrow's own arena, invisible to DuckDB's buffer
        manager) — so neither the streaming nor a hypothetical buffering
        implementation raises a catchable ``duckdb.OutOfMemoryException`` at
        this data size in this environment. The real, load-bearing signal is
        process RSS growth: measured directly against this environment's
        real DuckDB 1.5.2 + ducklake extension via this child-process
        harness, streaming via ``to_arrow_reader(100_000)`` grows the
        ingest child's peak RSS by roughly 300-700MB for this dataset,
        while swapping in a full ``to_arrow_table()`` materialize (same
        data, same tight memory_limit) grows it by roughly 1.8-2GB — a ~3-4x
        difference. The bound below sits well above the streaming path's
        actual usage and well below the buffering path's, so a regression
        back to a materializing form fails this test (verified manually by
        monkeypatching ``duckdb.DuckDBPyRelation.to_arrow_reader`` to route
        through ``to_arrow_table()`` and re-running this test — see
        ``.superpowers/sdd/task-3-report.md`` for the recorded numbers).
        """
        import subprocess

        data_dir = str(ducklake_env["data_dir"])
        child_script = str(Path(__file__).parent / "_ducklake_bounded_memory_child.py")
        n_rows = 20_000_000
        memory_limit = "200MB"

        build_proc = subprocess.run(
            [sys.executable, child_script, "build", data_dir, str(n_rows)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert build_proc.returncode == 0 and "BUILD_OK" in build_proc.stdout, (
            f"fixture build failed: stdout={build_proc.stdout!r} stderr={build_proc.stderr[-4000:]!r}"
        )
        size_bytes = int(build_proc.stdout.strip().splitlines()[-1].split()[1])
        assert size_bytes > 150_000_000, "fixture should produce a real multi-hundred-MB parquet"

        ingest_proc = subprocess.run(
            [sys.executable, child_script, "ingest", data_dir, memory_limit],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert ingest_proc.returncode == 0 and "RESULT_OK" in ingest_proc.stdout, (
            f"child ingest failed: stdout={ingest_proc.stdout!r} stderr={ingest_proc.stderr[-4000:]!r}"
        )
        _, row_count_str, delta_mb_str = ingest_proc.stdout.strip().splitlines()[-1].split()
        row_count = int(row_count_str)
        delta_mb = float(delta_mb_str)

        assert row_count == n_rows
        assert delta_mb < 1200, (
            f"ingest child grew peak RSS by {delta_mb:.0f}MB for a ~235MB table under a 200MB "
            "DuckLake memory_limit — this is consistent with a full-table materialization "
            "(.to_arrow_table()/.fetchall()) rather than the expected batch-by-batch stream "
            "via .to_arrow_reader(); see this test's docstring for measured reference numbers"
        )

    def test_legacy_backend_default_untouched(self, tmp_path, monkeypatch):
        """Zero-config default (no AGNES_ANALYTICS_BACKEND set) must still
        route through the untouched legacy rebuild-and-swap path."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("AGNES_ANALYTICS_BACKEND", raising=False)
        (tmp_path / "extracts").mkdir()
        (tmp_path / "analytics").mkdir()

        import src.analytics_backend as ab

        ab.reset_analytics_backend_cache()
        try:
            from src.orchestrator import SyncOrchestrator

            db_path = str(tmp_path / "analytics" / "server.duckdb")
            orch = SyncOrchestrator(analytics_db_path=db_path)
            result = orch.rebuild()
            assert result == {}
            # The legacy path's artifact — server.duckdb — must exist;
            # nothing ducklake-related should have been touched.
            assert Path(db_path).exists()
        finally:
            ab.reset_analytics_backend_cache()


class TestMigrateToBackendDucklakeDirection:
    """``SyncOrchestrator.migrate_to_backend("ducklake")`` (wave-2G Task 6)
    against the REAL ``ducklake`` extension — the ``to="legacy"``
    direction (no extension needed) is covered in
    ``tests/test_orchestrator.py::TestMigrateToBackend``.

    The whole point of ``migrate_to_backend`` is that it works
    regardless of the currently configured ``analytics.backend`` — every
    test here deliberately configures ``legacy`` (the OPPOSITE of the
    migration target) to prove that.
    """

    def test_migrate_to_ducklake_ingests_from_extracts_while_configured_backend_is_legacy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.delenv("AGNES_ANALYTICS_BACKEND", raising=False)
        for var in ("AGNES_DUCKLAKE_CATALOG_DSN", "AGNES_DUCKLAKE_DATA_PATH"):
            monkeypatch.delenv(var, raising=False)
        extracts_dir = tmp_path / "extracts"
        extracts_dir.mkdir()

        import src.analytics_backend as ab
        import src.ducklake_session as ds

        ab.reset_analytics_backend_cache()
        ds.close_ducklake_sessions()
        try:
            assert ab.analytics_backend() == "legacy"

            _create_ducklake_extract(
                extracts_dir,
                "keboola",
                [{"name": "orders", "data": [{"id": "1", "total": "100"}]}],
            )

            from src.orchestrator import SyncOrchestrator

            result = SyncOrchestrator().migrate_to_backend("ducklake")
            assert set(result.get("keboola", [])) == {"orders"}

            # Populated the lake even though analytics_backend() still
            # says "legacy" — migrate_to_backend never consulted it.
            assert ab.analytics_backend() == "legacy"

            r = ds.get_ducklake_read()
            try:
                row = r.execute('SELECT total FROM lake."keboola"."orders" WHERE id=\'1\'').fetchone()
                assert row[0] == "100"
                assert r.execute('SELECT total FROM lake."main"."orders"').fetchone()[0] == "100"
            finally:
                r.close()
        finally:
            ds.close_ducklake_sessions()
            ab.reset_analytics_backend_cache()
