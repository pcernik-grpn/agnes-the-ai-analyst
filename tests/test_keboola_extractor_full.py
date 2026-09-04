"""Full tests for the Keboola extractor connector."""

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

from tests.conftest import create_mock_extract
from tests.helpers.contract import validate_extract_contract


@pytest.fixture
def output_dir(tmp_path):
    d = tmp_path / "extracts" / "keboola"
    d.mkdir(parents=True)
    return str(d)


@pytest.fixture
def extracts_dir(tmp_path):
    d = tmp_path / "extracts"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def sample_local_configs():
    return [
        {
            "id": "in.c-finance.orders",
            "name": "orders",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "orders",
            "query_mode": "local",
            "description": "Finance orders",
        },
        {
            "id": "in.c-finance.customers",
            "name": "customers",
            "source_type": "keboola",
            "bucket": "in.c-finance",
            "source_table": "customers",
            "query_mode": "local",
            "description": "Customer data",
        },
    ]


def _mock_attach(conn, url, token):
    """Mock extension attach: creates kbc alias so views can be created."""
    conn.execute("ATTACH ':memory:' AS kbc")
    return True


def _write_parquet(pq_path, data_sql="SELECT 1 AS id, 'test' AS name"):
    local_conn = duckdb.connect()
    local_conn.execute(f"COPY ({data_sql}) TO '{pq_path}' (FORMAT PARQUET)")
    local_conn.close()


class TestKeboolaExtractorFull:
    def test_run_with_extension_creates_contract_compliant_db(self, output_dir, sample_local_configs):
        """run() with extension produces extract.duckdb that passes contract validation."""
        from connectors.keboola.extractor import run

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS id, 'Alice' AS name")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            result = run(output_dir, sample_local_configs, "https://kbc.example.com", "token-abc")

        assert result["tables_extracted"] == 2
        assert result["tables_failed"] == 0
        assert result["errors"] == []

        db_path = str(Path(output_dir) / "extract.duckdb")
        validate_extract_contract(db_path)

    def test_run_fallback_to_legacy_client(self, output_dir):
        """When DuckDB extension unavailable, falls back to legacy client."""
        from connectors.keboola.extractor import run

        configs = [{"name": "t", "id": "in.c-test.t", "query_mode": "local", "description": ""}]

        def mock_legacy(tc, pq_path, url, token):
            _write_parquet(pq_path, "SELECT 99 AS value")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", return_value=False),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=mock_legacy),
        ):
            result = run(output_dir, configs, "https://kbc.example.com", "token-abc")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 0
        # Verify data is actually readable (parquet stores integers as int, not str)
        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"), read_only=True)
        row = conn.execute("SELECT value FROM t").fetchone()
        conn.close()
        assert row[0] == 99

    def test_meta_table_schema_correct(self, output_dir):
        """_meta table must have exactly the required columns in the right order."""
        from connectors.keboola.extractor import run

        configs = [{"name": "t", "query_mode": "local", "description": "desc"}]

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS x")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            run(output_dir, configs, "https://kbc.example.com", "token-abc")

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"), read_only=True)
        cols = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='_meta' ORDER BY ordinal_position"
        ).fetchall()
        conn.close()
        assert [c[0] for c in cols] == ["table_name", "description", "rows", "size_bytes", "extracted_at", "query_mode"]

    def test_remote_attach_table_created_for_remote_tables(self, output_dir):
        """_remote_attach table is created when any table has query_mode='remote'."""
        from connectors.keboola.extractor import run

        def mock_attach_with_schema(conn, url, token):
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-events"')
            conn.execute('CREATE TABLE kbc."in.c-events"."big_table" (id VARCHAR)')
            return True

        configs = [
            {
                "name": "big_table",
                "bucket": "in.c-events",
                "source_table": "big_table",
                "query_mode": "remote",
                "description": "Large remote table",
            }
        ]

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_schema):
            result = run(output_dir, configs, "https://kbc.example.com", "token-abc")

        assert result["tables_extracted"] == 1

        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"), read_only=True)
        ra = conn.execute("SELECT alias, extension, url, token_env FROM _remote_attach").fetchone()
        conn.close()

        assert ra[0] == "kbc"
        assert ra[1] == "keboola"
        assert ra[2] == "https://kbc.example.com"
        assert ra[3] == "KEBOOLA_STORAGE_TOKEN"

    def test_remote_tables_not_downloaded(self, output_dir):
        """Remote tables have no parquet file — they are views pointing to kbc."""
        from connectors.keboola.extractor import run

        def mock_attach_with_schema(conn, url, token):
            conn.execute("ATTACH ':memory:' AS kbc")
            conn.execute('CREATE SCHEMA kbc."in.c-big"')
            conn.execute('CREATE TABLE kbc."in.c-big"."events" (id VARCHAR)')
            return True

        configs = [
            {
                "name": "events",
                "bucket": "in.c-big",
                "source_table": "events",
                "query_mode": "remote",
                "description": "",
            }
        ]

        with patch("connectors.keboola.extractor._try_attach_extension", side_effect=mock_attach_with_schema):
            run(output_dir, configs, "https://kbc.example.com", "token-abc")

        # No parquet file for remote table
        assert not (Path(output_dir) / "data" / "events.parquet").exists()

        # _meta has remote query_mode
        conn = duckdb.connect(str(Path(output_dir) / "extract.duckdb"), read_only=True)
        row = conn.execute("SELECT query_mode FROM _meta WHERE table_name='events'").fetchone()
        conn.close()
        assert row[0] == "remote"

    def test_partial_failure_continues(self, output_dir, sample_local_configs, monkeypatch):
        """A single table failure should not abort remaining tables."""
        from connectors.keboola.extractor import run

        call_count = [0]

        def failing_first(conn, tc, pq_path, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("Connection reset")
            _write_parquet(pq_path, "SELECT 1 AS id")

        # When extension scan fails, the per-table flow now retries via
        # _extract_via_legacy. Mock it to re-raise so we observe the final
        # error surface; the contract under test is "single table failure
        # doesn't abort remaining tables", not which path produced the
        # message. Force inline mode (AGNES_KEBOOLA_PARALLELISM=1) so the
        # mock survives — the parallel path uses ProcessPoolExecutor which
        # spawns subprocesses that don't see the mock.
        monkeypatch.setenv("AGNES_KEBOOLA_PARALLELISM", "1")

        def legacy_reraise(tc, pq_path, url, token):
            raise RuntimeError("Connection reset")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=failing_first),
            patch("connectors.keboola.extractor._extract_via_legacy", side_effect=legacy_reraise),
        ):
            result = run(output_dir, sample_local_configs, "https://kbc.example.com", "token-abc")

        assert result["tables_extracted"] == 1
        assert result["tables_failed"] == 1
        assert len(result["errors"]) == 1
        assert "Connection reset" in result["errors"][0]["error"]

    def test_create_mock_extract_contract(self, extracts_dir):
        """create_mock_extract helper produces contract-compliant extract.duckdb."""
        db_path = create_mock_extract(
            extracts_dir,
            "keboola",
            [
                {"name": "orders", "data": [{"id": "1", "amount": "100"}], "query_mode": "local"},
            ],
        )
        validate_extract_contract(str(db_path))

    def test_data_directory_created(self, output_dir, sample_local_configs):
        """data/ subdirectory is created alongside extract.duckdb."""
        from connectors.keboola.extractor import run

        def write_pq(conn, tc, pq_path, *a, **kw):
            _write_parquet(pq_path, "SELECT 1 AS id")

        with (
            patch("connectors.keboola.extractor._try_attach_extension", side_effect=_mock_attach),
            patch("connectors.keboola.extractor._extract_via_extension", side_effect=write_pq),
        ):
            run(output_dir, sample_local_configs, "https://kbc.example.com", "token-abc")

        assert (Path(output_dir) / "data").is_dir()
        assert (Path(output_dir) / "data" / "orders.parquet").exists()
        assert (Path(output_dir) / "data" / "customers.parquet").exists()
