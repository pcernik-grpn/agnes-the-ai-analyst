"""Materialized BQ tables read schema from the local parquet, not from
BigQuery INFORMATION_SCHEMA. Issue #261 fix verification."""

from pathlib import Path
from unittest.mock import patch


def test_materialized_bq_schema_does_not_call_bq(tmp_path: Path, monkeypatch):
    """When `query_mode='materialized'`, `build_schema_uncached` must
    bypass `_fetch_bq_schema` and read from the local parquet."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Create a tiny parquet with two columns so the parquet reader has
    # something to DESCRIBE.
    import duckdb

    parquet_dir = tmp_path / "extracts" / "bigquery" / "data"
    parquet_dir.mkdir(parents=True)
    parquet_path = parquet_dir / "orders.parquet"
    conn = duckdb.connect(":memory:")
    conn.execute(f"COPY (SELECT 1 AS event_id, 'USD' AS currency) TO '{parquet_path}' (FORMAT PARQUET)")
    conn.close()

    from app.api.v2_schema import build_schema_uncached

    fake_row = {
        "id": "orders",
        "source_type": "bigquery",
        "query_mode": "materialized",
        "bucket": "dwh",
        "source_table": "orders",
    }
    fake_bq = object()  # never used — BQ path must be skipped

    with (
        patch("app.api.v2_schema._fetch_bq_schema") as mock_bq_schema,
        patch("app.api.v2_schema._fetch_bq_table_options") as mock_bq_opts,
    ):
        result = build_schema_uncached(
            conn=None,
            table_id="orders",
            bq=fake_bq,
            row=fake_row,
        )
    mock_bq_schema.assert_not_called()
    mock_bq_opts.assert_not_called()
    cols = {c["name"] for c in result["columns"]}
    assert cols == {"event_id", "currency"}
    assert result["sql_flavor"] == "duckdb"


def test_schema_resolves_parquet_keyed_by_registry_name(tmp_path: Path, monkeypatch):
    """The materialized sync keys the parquet filename by registry `name`
    (`app/api/sync.py::_run_materialized_pass` convention) while this surface
    receives the `id`; the register handler slugifies the id from the name
    (lower + spaces→underscores), so the two routinely differ and the id-keyed
    lookup 404-ed a healthy, fully-synced table."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import duckdb

    parquet_dir = tmp_path / "extracts" / "keboola" / "data"
    parquet_dir.mkdir(parents=True)
    conn = duckdb.connect(":memory:")
    conn.execute(
        f"COPY (SELECT 1 AS order_id, 'EUR' AS currency) TO '{parquet_dir / 'Orders 90d.parquet'}' (FORMAT PARQUET)"
    )
    conn.close()

    from app.api.v2_schema import build_schema_uncached

    fake_row = {
        "id": "orders_90d",
        "name": "Orders 90d",
        "source_type": "keboola",
        "query_mode": "materialized",
        "bucket": "in.c-main",
        "source_table": "ORDERS_90D",
    }
    result = build_schema_uncached(conn=None, table_id="orders_90d", bq=object(), row=fake_row)
    cols = {c["name"] for c in result["columns"]}
    assert cols == {"order_id", "currency"}


def test_remote_bq_schema_still_calls_bq(tmp_path: Path, monkeypatch):
    """Sanity: remote BQ tables (not materialized) still go through BQ."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.api.v2_schema import build_schema_uncached

    fake_row = {
        "id": "ue",
        "source_type": "bigquery",
        "query_mode": "remote",
        "bucket": "finance",
        "source_table": "ue",
    }
    fake_bq = object()
    with (
        patch(
            "app.api.v2_schema._fetch_bq_schema",
            return_value=[],
        ) as mock_bq_schema,
        patch(
            "app.api.v2_schema._fetch_bq_table_options",
            return_value={},
        ),
    ):
        build_schema_uncached(conn=None, table_id="ue", bq=fake_bq, row=fake_row)
    mock_bq_schema.assert_called_once()
