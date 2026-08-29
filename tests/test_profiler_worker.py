"""End-to-end test for the profiler worker subprocess.

Drives ``src._profiler_worker`` over the runner — writes a tiny parquet
to a temp dir, invokes the worker with JSON args, and asserts the
returned profile dict has the shape downstream callers expect from
``profile_table``.
"""

from pathlib import Path

import duckdb
import pytest

from src._subprocess_runner import run_subprocess_job


@pytest.fixture
def tiny_parquet(tmp_path: Path) -> Path:
    """Materialize a 3-row, 2-column parquet ``profile_table`` can scan."""
    pq = tmp_path / "tiny.parquet"
    conn = duckdb.connect()
    try:
        safe = str(pq).replace("'", "''")
        conn.execute(
            f"COPY (SELECT * FROM (VALUES (1, 'a'), (2, 'b'), (3, 'c')) AS t(id, label)) TO '{safe}' (FORMAT PARQUET)"
        )
    finally:
        conn.close()
    return pq


@pytest.fixture
def decimal_parquet(tmp_path: Path) -> Path:
    """Parquet with a DECIMAL column, as produced by NUMERIC warehouse types."""
    pq = tmp_path / "decimal.parquet"
    conn = duckdb.connect()
    try:
        safe = str(pq).replace("'", "''")
        conn.execute(
            f"COPY (SELECT * FROM (VALUES "
            f"(CAST(1.25 AS DECIMAL(18,2)), 'a'), "
            f"(CAST(2.50 AS DECIMAL(18,2)), 'b'), "
            f"(CAST(3.75 AS DECIMAL(18,2)), 'c')) "
            f"AS t(amount, label)) TO '{safe}' (FORMAT PARQUET)"
        )
    finally:
        conn.close()
    return pq


def test_worker_serializes_decimal_profile(decimal_parquet):
    # DECIMAL columns (Snowflake/warehouse NUMERIC) yield decimal.Decimal
    # stats from DuckDB, which json.dumps refuses without a default handler —
    # the worker crashed and failed the whole data-refresh job (TCRD-243).
    result = run_subprocess_job(
        "src._profiler_worker",
        {
            "table_name": "decimals",
            "table_id": "decimals",
            "parquet_path": str(decimal_parquet),
        },
        timeout_sec=60,
    )
    assert isinstance(result, dict)
    assert "row_count" in result or "rows" in result or "columns" in result


def test_worker_returns_profile_dict(tiny_parquet, tmp_path):
    # Worker runs in the same checkout as the parent, no PYTHONPATH override
    # required — but tests run inside the repo's venv which is on PATH.
    result = run_subprocess_job(
        "src._profiler_worker",
        {
            "table_name": "tiny",
            "table_id": "tiny",
            "parquet_path": str(tiny_parquet),
        },
        timeout_sec=60,
    )
    assert isinstance(result, dict)
    # ``profile_table`` returns a structure with at least these keys; if
    # the worker swallowed an exception or misroutes its output we'd see
    # an empty or wrong-shape dict here.
    assert "row_count" in result or "rows" in result or "columns" in result
