"""Tests for Keboola extractor."""

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from tests.helpers.contract import validate_extract_contract


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "keboola"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def sample_configs():
    return [
        {
            "id": "in.c-crm.orders",
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-crm",
            "source_table": "orders",
            "query_mode": "local",
            "description": "Order data",
        },
        {
            "id": "in.c-crm.customers",
            "name": "customers",
            "source_type": "keboola",
            "bucket": "in.c-crm",
            "source_table": "customers",
            "query_mode": "local",
            "description": "Customer data",
        },
    ]


def _mock_attach(conn, url, token):
    """Mock that says extension is available and ATTACHes a fake kbc catalog."""
    # Create in-memory DB as kbc so views referencing kbc."bucket"."table" can be created
    conn.execute("ATTACH ':memory:' AS kbc")
    return True


def _write_parquet(pq_path, data_sql="SELECT 1 AS id, 'test' AS name"):
    """Helper to write a parquet file with given SQL."""
    local_conn = duckdb.connect()
    local_conn.execute(f"COPY ({data_sql}) TO '{pq_path}' (FORMAT PARQUET)")
    local_conn.close()


class TestKeboolaExtractor:
    def test_creates_extract_duckdb(self, output_dir, sample_configs):
        """Test that run() creates extract.duckdb with correct structure."""
        from connectors.keboola.extractor import run

        def write_parquet(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path)

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_parquet),
        ):
            result = run(output_dir, sample_configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 2
        assert result["tables_failed"] == 0

        # Verify extract.duckdb exists and has correct structure
        db_path = Path(output_dir) / "extract.duckdb"
        assert db_path.exists()

        conn = duckdb.connect(str(db_path))
        try:
            # Check _meta table
            meta = conn.execute("SELECT * FROM _meta ORDER BY table_name").fetchall()
            assert len(meta) == 2
            names = {row[0] for row in meta}
            assert names == {"orders", "customers"}

            # Check all are 'local' query_mode
            modes = {row[5] for row in meta}
            assert modes == {"local"}
        finally:
            conn.close()

        validate_extract_contract(str(db_path))

    def test_remote_tables_not_downloaded(self, output_dir):
        """Test that tables with query_mode='remote' are registered but not downloaded."""
        from connectors.keboola.extractor import run

        configs = [
            {
                "name": "big_table",
                "bucket": "in.c-events",
                "source_table": "big_table",
                "query_mode": "remote",
                "description": "Too large to sync",
            }
        ]

        def mock_attach_with_schema(conn, url, token):
            """Mock kbc with the expected bucket schema so remote views can be created."""
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-events"')
            conn.execute('CREATE TABLE kbc."in.c-events"."big_table" (id VARCHAR)')
            return True

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_schema):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            meta = conn.execute("SELECT query_mode FROM _meta WHERE table_name='big_table'").fetchone()
            assert meta[0] == "remote"

            # _remote_attach table should exist with Keboola connection info
            ra = conn.execute("SELECT alias, extension, url, token_env FROM _remote_attach").fetchone()
            assert ra[0] == "kbc"
            assert ra[1] == "keboola"
            assert ra[2] == "https://example.com"
            assert ra[3] == "KEBOOLA_STORAGE_TOKEN"
        finally:
            conn.close()

        # No parquet file should exist
        assert not (Path(output_dir) / "data" / "big_table.parquet").exists()

    def test_remote_view_heals_bucket_prefixed_source_table(self, output_dir):
        """A row registered by the pre-fix Data-sources wizard carries the FULL
        Keboola id in source_table; the remote branch would then build
        kbc."in.c-events"."in.c-events.big_table". validate_quoted_identifier
        accepts it (dots are legal in Keboola's `in.c-foo` convention), so
        nothing else catches it — the extension's eager CREATE VIEW validation
        raises. Normalizing at use must make the view resolve."""
        from connectors.keboola.extractor import run

        configs = [
            {
                "name": "big_table",
                "bucket": "in.c-events",
                "source_table": "in.c-events.big_table",  # legacy wizard shape
                "query_mode": "remote",
                "description": "Too large to sync",
            }
        ]

        def mock_attach_with_schema(conn, url, token):
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-events"')
            conn.execute('CREATE TABLE kbc."in.c-events"."big_table" (id VARCHAR)')
            return True

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_schema):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1, result
        assert result["tables_failed"] == 0, result

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            # The view body must target the BARE in-bucket name. (Querying it
            # here would need the kbc re-ATTACH the _remote_attach row exists
            # for, so assert on the stored definition instead.)
            sql = conn.execute("SELECT sql FROM duckdb_views() WHERE view_name = 'big_table'").fetchone()[0]
            # Compare with quoting stripped — DuckDB re-quotes identifiers in
            # the stored definition, so the quoting style is not the contract.
            unquoted = sql.replace('"', "")
            assert "kbc.in.c-events.big_table" in unquoted, sql
            assert "in.c-events.in.c-events" not in unquoted, sql
        finally:
            conn.close()

    def test_local_extension_path_heals_bucket_prefixed_source_table(self, output_dir):
        """The query_mode='local' extension path re-composes the same
        kbc.<bucket>.<source_table> identifier, so a legacy wizard row would
        COPY FROM kbc."in.c-crm"."in.c-crm.orders" — nonexistent. Normalizing
        at use must make the extraction succeed against the bare name."""
        from connectors.keboola.extractor import run

        configs = [
            {
                "name": "orders",
                "bucket": "in.c-crm",
                "source_table": "in.c-crm.orders",  # legacy wizard shape
                "query_mode": "local",
                "description": "Order data",
            }
        ]

        def mock_attach_with_rows(conn, url, token):
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-crm"')
            conn.execute('CREATE TABLE kbc."in.c-crm"."orders" (id INTEGER)')
            conn.execute('INSERT INTO kbc."in.c-crm"."orders" VALUES (1), (2)')
            return True

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_rows):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1, result
        assert result["tables_failed"] == 0, result
        pq = Path(output_dir) / "data" / "orders.parquet"
        assert pq.exists(), "extension path wrote no parquet"
        c = duckdb.connect()
        try:
            assert c.execute(f"SELECT COUNT(*) FROM read_parquet('{pq}')").fetchone()[0] == 2
        finally:
            c.close()

    def test_remote_view_failure_does_not_abort_sibling_tables(self, output_dir):
        """An unresolvable remote view must degrade to tables_failed like every
        other branch. Unguarded it raised out of run(), skipped the atomic
        extract.duckdb rename, and took the whole source's extraction with it."""
        from connectors.keboola.extractor import run

        configs = [
            {
                "name": "broken",
                "bucket": "in.c-events",
                "source_table": "does_not_exist_upstream",
                "query_mode": "remote",
                "description": "",
            },
            {
                "name": "healthy",
                "bucket": "in.c-events",
                "source_table": "big_table",
                "query_mode": "remote",
                "description": "",
            },
        ]

        def mock_attach_with_schema(conn, url, token):
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-events"')
            conn.execute('CREATE TABLE kbc."in.c-events"."big_table" (id VARCHAR)')
            return True

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_schema):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_failed"] == 1, result
        assert result["tables_extracted"] == 1, result
        # The atomic rename must still have happened — the sibling is usable.
        extract_db = Path(output_dir) / "extract.duckdb"
        assert extract_db.exists(), "extract.duckdb missing — the rename was skipped"
        conn = duckdb.connect(str(extract_db))
        try:
            names = {r[0] for r in conn.execute("SELECT table_name FROM _meta").fetchall()}
            assert "healthy" in names
            assert "broken" not in names
        finally:
            conn.close()

    def test_handles_extraction_failure(self, output_dir, sample_configs, monkeypatch):
        """Test that a failed table doesn't stop other tables from extracting."""
        from connectors.keboola.extractor import run

        call_count = 0

        def side_effect(conn, tc, pq_path, *a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network error")
            # Second call succeeds
            _write_parquet(pq_path, "SELECT 1 AS id")

        # Mock the legacy fallback too — without it the real client
        # attempts an HTTPS round-trip to the test URL and hangs ~minute.
        # Force inline (PARALLELISM=1) so the mock survives — the parallel
        # path would spawn a subprocess that doesn't see the patch.
        monkeypatch.setenv("AGNES_KEBOOLA_PARALLELISM", "1")

        def legacy_reraise(tc, pq_path, url, token):
            raise Exception("Network error")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=side_effect),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=legacy_reraise),
        ):
            result = run(output_dir, sample_configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 1
        assert len(result["errors"]) == 1

    def test_creates_data_directory(self, output_dir, sample_configs):
        """Test that data/ subdirectory is created."""
        from connectors.keboola.extractor import run

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS id")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            run(output_dir, sample_configs, "https://example.com", "test-token")

        assert (Path(output_dir) / "data").is_dir()
        assert (Path(output_dir) / "data" / "orders.parquet").exists()

    def test_views_queryable(self, output_dir):
        """Test that views in extract.duckdb can be queried."""
        from connectors.keboola.extractor import run

        configs = [{"name": "test_table", "query_mode": "local", "description": "Test"}]

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 42 AS value, 'hello' AS msg")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            run(output_dir, configs, "https://example.com", "test-token")

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            result = conn.execute("SELECT value, msg FROM test_table").fetchone()
            assert result[0] == 42
            assert result[1] == "hello"
        finally:
            conn.close()

    def test_meta_table_schema(self, output_dir):
        """Test that _meta table has all required columns."""
        from connectors.keboola.extractor import run

        configs = [{"name": "t", "query_mode": "local", "description": "desc"}]

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS x")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            run(output_dir, configs, "https://example.com", "test-token")

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        try:
            cols = conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='_meta' ORDER BY ordinal_position"
            ).fetchall()
            col_names = [c[0] for c in cols]
            assert col_names == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"]
        finally:
            conn.close()

    def test_legacy_fallback_when_extension_unavailable(self, output_dir):
        """Test that legacy client is used when extension attach fails."""
        from connectors.keboola.extractor import run

        configs = [{"name": "t", "id": "in.c-test.t", "query_mode": "local", "description": ""}]

        def mock_legacy(tc, pq_path, url, token):
            _write_parquet(pq_path, "SELECT 1 AS id")

        # Extension not available
        with (
            patch("connectors.keboola.extractor._try_attach_extension", return_value=False),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 0


# ---------------------------------------------------------------------------
# Connector failure mode tests
# ---------------------------------------------------------------------------


class TestKeboolaExtractorFailureModes:
    """Tests for Keboola extractor failure handling and resilience."""

    def test_extractor_crash_does_not_corrupt_extract_duckdb(self, output_dir, sample_configs):
        """If the extractor crashes mid-extraction, the temp DB is not moved
        into place, so the existing extract.duckdb (if any) is not corrupted.
        The atomic write pattern (tmp + rename) protects against this."""
        from connectors.keboola.extractor import run

        # First, create a valid extract.duckdb
        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS id")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            run(output_dir, sample_configs[:1], "https://example.com", "test-token")

        db_path = Path(output_dir) / "extract.duckdb"
        assert db_path.exists()
        # Verify it's valid
        conn = duckdb.connect(str(db_path))
        conn.execute("SELECT * FROM _meta").fetchall()
        conn.close()

        # Now simulate a crash during a second extraction — the extension
        # attach raises an exception after the tmp file is created.
        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=RuntimeError("crash")):
            try:
                run(output_dir, sample_configs, "https://example.com", "test-token")
            except Exception:
                pass  # The extractor catches internally and returns stats

        # The extract.duckdb should still exist and be valid (atomic swap
        # means the old file is untouched if the new one didn't complete)
        assert db_path.exists()

    def test_partial_data_write_incomplete_parquet(self, output_dir):
        """When a parquet file write fails mid-stream, the extractor records
        the table as failed in stats but continues with other tables."""
        from connectors.keboola.extractor import run

        configs = [
            {"name": "good_table", "query_mode": "local", "description": "OK"},
            {"name": "bad_table", "query_mode": "local", "description": "Will fail"},
        ]

        call_count = 0

        def side_effect(conn, tc, pq_path, *a, **kw):
            nonlocal call_count
            call_count += 1
            if tc["name"] == "bad_table":
                raise IOError("Disk full — partial write")
            _write_parquet(pq_path, "SELECT 1 AS id")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=side_effect),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        # One table succeeded, one failed
        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 1
        assert len(result["errors"]) == 1
        assert "bad_table" in result["errors"][0]["table"]

        # The good table's parquet file exists
        assert (Path(output_dir) / "data" / "good_table.parquet").exists()
        # The bad table's parquet file should NOT exist (failed before write)
        assert not (Path(output_dir) / "data" / "bad_table.parquet").exists()

    def test_network_timeout_during_extraction(self, output_dir, monkeypatch):
        """Network timeout during extraction should return a meaningful error
        in the stats, not crash the whole process."""
        from connectors.keboola.extractor import run
        import socket

        configs = [
            {"name": "timeout_table", "query_mode": "local", "description": "Will timeout"},
            {"name": "ok_table", "query_mode": "local", "description": "OK"},
        ]

        call_count = 0

        def side_effect(conn, tc, pq_path, *a, **kw):
            nonlocal call_count
            call_count += 1
            if tc["name"] == "timeout_table":
                raise socket.timeout("Connection timed out")
            _write_parquet(pq_path, "SELECT 1 AS id")

        # When extension scan fails, the per-table flow now retries via
        # _extract_via_legacy. Mock it to re-raise the same socket.timeout
        # so we observe the final error surface; the contract under test is
        # "extension failure doesn't crash, error makes it into stats, other
        # tables continue", not which path produced the message.
        # Force PARALLELISM=1 so the mock survives — the parallel path uses
        # ProcessPoolExecutor which spawns subprocesses that don't see the
        # mock.
        monkeypatch.setenv("AGNES_KEBOOLA_PARALLELISM", "1")

        def legacy_reraise(tc, pq_path, url, token):
            raise socket.timeout("Connection timed out")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=side_effect),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=legacy_reraise),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 1
        assert "timed out" in result["errors"][0]["error"].lower()

    def test_extension_unavailable_fallback_to_client(self, output_dir):
        """When DuckDB Keboola extension fails to load, the extractor falls
        back to the legacy HTTP client."""
        from connectors.keboola.extractor import run

        configs = [
            {
                "name": "t",
                "id": "in.c-test.t",
                "query_mode": "local",
                "bucket": "in.c-test",
                "source_table": "t",
                "description": "",
            }
        ]

        def mock_legacy(tc, pq_path, url, token):
            _write_parquet(pq_path, "SELECT 42 AS value")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", return_value=False),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 0

        # Verify the data is queryable
        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        val = conn.execute("SELECT value FROM t").fetchone()
        assert val[0] == 42
        conn.close()

    def test_extension_per_table_failure_falls_back_to_legacy(self, output_dir):
        """When ATTACH succeeds but the per-table extension scan fails (e.g.
        Keboola QueryService schema/role mismatch — keboola/duckdb-extension#17),
        the extractor retries that table via the legacy Storage-API client."""
        from connectors.keboola.extractor import run

        configs = [
            {
                "name": "t",
                "id": "in.c-test.t",
                "query_mode": "local",
                "bucket": "in.c-test",
                "source_table": "t",
                "description": "",
            }
        ]

        def extension_scan_fails(conn, tc, pq_path, *a, **kw):
            raise RuntimeError(
                "Keboola scan failed: Schema 'KBC_USE4_NNNN.\"in.c-test\"' does not exist or not authorized."
            )

        def legacy_succeeds(tc, pq_path, url, token):
            _write_parquet(pq_path, "SELECT 7 AS value")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=extension_scan_fails),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=legacy_succeeds),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 0
        # Verify the legacy-produced data is queryable
        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"))
        val = conn.execute("SELECT value FROM t").fetchone()
        assert val[0] == 7
        conn.close()

    def test_all_tables_fail_returns_full_failure_stats(self, output_dir, monkeypatch):
        """When every table fails, the extractor returns all failures in stats
        without crashing."""
        from connectors.keboola.extractor import run

        configs = [
            {"name": "t1", "query_mode": "local", "description": ""},
            {"name": "t2", "query_mode": "local", "description": ""},
        ]

        def always_fail(conn, tc, pq_path, *a, **kw):
            raise RuntimeError("Extraction failed")

        # Mock legacy too — otherwise it would attempt a real HTTP call to
        # the fake URL on each per-table fallback retry. Force inline mode
        # (AGNES_KEBOOLA_PARALLELISM=1) so the mock survives — the parallel
        # path uses ProcessPoolExecutor which spawns subprocesses that
        # don't see the mock.
        monkeypatch.setenv("AGNES_KEBOOLA_PARALLELISM", "1")

        def legacy_also_fails(tc, pq_path, url, token):
            raise RuntimeError("Extraction failed")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=always_fail),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=legacy_also_fails),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 0
        assert result["tables_failed"] == 2
        assert len(result["errors"]) == 2

    def test_legacy_parallelism_one_runs_inline(self, output_dir, monkeypatch):
        """AGNES_KEBOOLA_PARALLELISM=1 keeps the legacy fallback inline —
        no ProcessPoolExecutor, so unittest.mock patches survive. Useful
        as a debugging escape hatch and the path used by tests below.

        Why processes (not threads) for the parallel path: the legacy
        client's `export_table` does `os.chdir(temp_dir)` to direct
        kbcstorage's slice-file downloads into a per-call temp directory.
        `os.chdir` is process-global, so two threads racing on it land
        slice files in the wrong directory and the merge step fails with
        `[Errno 2] No such file or directory`. Process workers each have
        their own CWD and don't interfere."""
        from connectors.keboola.extractor import run

        configs = [
            {"name": f"u{i}", "query_mode": "local", "description": "", "bucket": "in.c-test", "source_table": f"u{i}"}
            for i in range(3)
        ]

        call_count = 0

        def mock_legacy(tc, pq_path, url, token):
            nonlocal call_count
            call_count += 1
            _write_parquet(pq_path, "SELECT 1 AS x")

        def extension_always_fails(conn, tc, pq_path, *a, **kw):
            raise RuntimeError("Schema not authorized")

        monkeypatch.setenv("AGNES_KEBOOLA_PARALLELISM", "1")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=extension_always_fails),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 3
        assert result["tables_failed"] == 0
        assert call_count == 3, "all 3 tables should have called the patched legacy fn"

    def test_legacy_uses_process_pool_when_parallel(self, output_dir, monkeypatch):
        """Structural check: when len(legacy_queue) > 1 and
        AGNES_KEBOOLA_PARALLELISM > 1, the parallel path imports
        `concurrent.futures.ProcessPoolExecutor` to drain the queue.

        The mock can't ride into subprocesses (mocks aren't picklable),
        so we patch ProcessPoolExecutor itself and verify it's invoked
        with the expected worker count."""
        from connectors.keboola.extractor import run

        configs = [
            {"name": f"t{i}", "query_mode": "local", "description": "", "bucket": "in.c-test", "source_table": f"t{i}"}
            for i in range(5)
        ]

        def extension_always_fails(conn, tc, pq_path, *a, **kw):
            raise RuntimeError("Schema not authorized")

        # Stand in for ProcessPoolExecutor — runs everything in-process
        # so we can verify the call shape without dealing with pickling.
        seen_max_workers = []

        class _FakePool:
            def __init__(self, max_workers):
                seen_max_workers.append(max_workers)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def submit(self, fn, *args, **kwargs):
                from concurrent.futures import Future

                f: Future = Future()
                try:
                    # Inline the legacy call so the parquet ends up on disk
                    # for the orchestrator's downstream stat + _meta logic.
                    f.set_result(fn(*args, **kwargs))
                except Exception as e:
                    f.set_exception(e)
                return f

        def mock_legacy(tc, pq_path, url, token):
            _write_parquet(pq_path, "SELECT 1 AS x")

        monkeypatch.setenv("AGNES_KEBOOLA_PARALLELISM", "4")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=extension_always_fails),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy),
            patch("concurrent.futures.ProcessPoolExecutor", _FakePool),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 5
        assert result["tables_failed"] == 0
        assert seen_max_workers == [4], f"Expected ProcessPoolExecutor(max_workers=4); got {seen_max_workers}"

    def test_unsafe_identifier_skipped_not_crashed(self, output_dir):
        """Tables with unsafe identifiers are skipped with an error in stats,
        not causing a crash."""
        from connectors.keboola.extractor import run

        configs = [
            {"name": "bad-name", "query_mode": "local", "description": "hyphen not allowed"},
            {"name": "good_name", "query_mode": "local", "description": "OK"},
        ]

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS id")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            result = run(output_dir, configs, "https://example.com", "test-token")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 1
        assert result["errors"][0]["error"] == "unsafe identifier"

    def test_compute_exit_code_full_success(self):
        from connectors.keboola.extractor import compute_exit_code

        stats = {"tables_failed": 0, "errors": []}
        assert compute_exit_code(stats, 5) == 0

    def test_compute_exit_code_partial_failure(self):
        from connectors.keboola.extractor import compute_exit_code

        stats = {"tables_failed": 2, "errors": [{}, {}]}
        assert compute_exit_code(stats, 5) == 2

    def test_compute_exit_code_full_failure(self):
        from connectors.keboola.extractor import compute_exit_code

        stats = {"tables_failed": 5, "errors": [{}] * 5}
        assert compute_exit_code(stats, 5) == 1

    def test_compute_exit_code_no_tables(self):
        from connectors.keboola.extractor import compute_exit_code

        stats = {"tables_failed": 0, "errors": []}
        assert compute_exit_code(stats, 0) == 0
