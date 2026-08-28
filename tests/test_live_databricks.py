"""Live Databricks tests — require real Databricks SQL-warehouse credentials
in environment variables.

Run with: pytest tests/test_live_databricks.py -m live -v
Requires: DATABRICKS_HOST, DATABRICKS_TOKEN, DATABRICKS_WAREHOUSE_ID.
Optional: DATABRICKS_CATALOG — a catalog to search for a Unity Catalog
metric view. Only ``test_semantic_refresh_finds_a_real_metric_view`` uses it;
that test skips (rather than fails) when it is unset or the catalog has no
metric view, since a live-creds workspace is not guaranteed to have one.

All tests are read-only and deliberately tiny/bounded (single-row SELECTs,
small ``max_bytes``/``cap_bytes`` caps) — a real run against a live warehouse
costs approximately nothing.

Why this suite exists: two connector assumptions about the vendor's wire
shape were previously verified only by reading Databricks documentation,
never against a real workspace —
``information_schema.tables.table_type`` spelling for a metric view
(``connectors/databricks/semantic_ossie.py::_METRIC_VIEW_TABLE_TYPES``) and
the ``SHOW CREATE TABLE ... $$<yaml>$$`` body shape
(``connectors/databricks/semantic_ossie.py::_YAML_BODY_RE``).
``test_semantic_refresh_finds_a_real_metric_view`` asserts both directly
against a real metric view when one is available.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live

DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "")
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "")
DATABRICKS_WAREHOUSE_ID = os.environ.get("DATABRICKS_WAREHOUSE_ID", "")
DATABRICKS_CATALOG = os.environ.get("DATABRICKS_CATALOG", "")


@pytest.fixture(autouse=True)
def require_databricks_env():
    """Skip all tests in this module if Databricks credentials are missing."""
    if not (DATABRICKS_HOST and DATABRICKS_TOKEN and DATABRICKS_WAREHOUSE_ID):
        pytest.skip(
            "Databricks credentials not set. Export DATABRICKS_HOST, "
            "DATABRICKS_TOKEN and DATABRICKS_WAREHOUSE_ID to run live tests "
            "(optionally DATABRICKS_CATALOG for the metric-view discovery test)."
        )


def _make_client():
    from connectors.databricks.client import DatabricksStatementClient

    return DatabricksStatementClient(
        host=DATABRICKS_HOST,
        token=DATABRICKS_TOKEN,
        warehouse_id=DATABRICKS_WAREHOUSE_ID,
    )


# ---------------------------------------------------------------------------
# materialize (connectors/databricks/extractor.py)
# ---------------------------------------------------------------------------


def test_materialize_small(tmp_path):
    """`materialize_query` runs a tiny SELECT on the real warehouse and
    writes a parquet with the expected row — a generous `max_bytes` cap
    does not falsely trip on a genuinely small result."""
    from connectors.databricks.extractor import materialize_query

    client = _make_client()
    result = materialize_query(
        "live_test_table",
        client=client,
        output_dir=str(tmp_path),
        source_query="SELECT 1 AS x",
        max_bytes=1024,  # 1 KiB — comfortably above a one-row, one-column result
    )
    assert result["rows"] == 1
    assert result["query_mode"] == "materialized"

    parquet_path = tmp_path / "data" / "live_test_table.parquet"
    assert parquet_path.exists()

    import duckdb

    rows = duckdb.sql(f"SELECT * FROM read_parquet('{parquet_path}')").fetchall()
    assert rows == [(1,)]


def test_materialize_respects_a_tiny_max_bytes_cap(tmp_path):
    """A cap too small for the result raises MaterializeBudgetError and
    writes nothing — never a silently truncated parquet."""
    from connectors.bigquery.extractor import MaterializeBudgetError
    from connectors.databricks.extractor import materialize_query

    client = _make_client()
    with pytest.raises(MaterializeBudgetError):
        materialize_query(
            "live_test_table_capped",
            client=client,
            output_dir=str(tmp_path),
            source_query="SELECT id FROM range(100000)",
            max_bytes=1,  # 1 byte — no real result fits
        )
    assert not (tmp_path / "data" / "live_test_table_capped.parquet").exists()


# ---------------------------------------------------------------------------
# remote (connectors/databricks/remote.py)
# ---------------------------------------------------------------------------


def test_remote_select():
    """`execute_select` (the analyst `query_mode='remote'` path) returns the
    real row for a trivial statement, untruncated."""
    from connectors.databricks.remote import execute_select

    client = _make_client()
    columns, rows, truncated, total_bytes = execute_select(
        "SELECT 1 AS x, 'hello' AS y",
        settings={},  # unused when `client` is injected
        limit=10,
        cap_bytes=1024,
        timeout_s=60,
        client=client,
    )
    assert columns == ["x", "y"]
    assert rows == [[1, "hello"]]
    assert truncated is False
    assert total_bytes >= 0


def test_remote_select_refuses_a_result_over_a_tiny_byte_cap():
    """`execute_select`'s byte cap is honored: an over-cap result is refused
    (`remote_scan_too_large`), never silently shortened."""
    from connectors.databricks.remote import DatabricksRemoteError, execute_select

    client = _make_client()
    with pytest.raises(DatabricksRemoteError) as excinfo:
        execute_select(
            "SELECT id FROM range(100000)",
            settings={},
            limit=1000,
            cap_bytes=1,
            timeout_s=60,
            client=client,
        )
    assert excinfo.value.reason == "remote_scan_too_large"


def test_remote_scan_to_arrow():
    """`execute_scan_to_arrow` (the materialize/snapshot path) returns a
    real pyarrow.Table for a trivial statement."""
    from connectors.databricks.remote import execute_scan_to_arrow

    client = _make_client()
    table = execute_scan_to_arrow(
        "SELECT 1 AS x",
        settings={},
        cap_bytes=1024,
        timeout_s=60,
        client=client,
    )
    assert table.num_rows == 1
    assert table.column("x").to_pylist() == [1]


# ---------------------------------------------------------------------------
# semantic layer (connectors/databricks/semantic_ossie.py) — the headline
# ---------------------------------------------------------------------------


def test_semantic_refresh_finds_a_real_metric_view():
    """Verifies the two vendor-specific wire assumptions
    `connectors/databricks/semantic_ossie.py` makes, against a real
    workspace instead of only Databricks documentation:
    `information_schema.tables.table_type` spelling for a metric view, and
    the `SHOW CREATE TABLE ... $$<yaml>$$` body shape.

    Skips (does not fail) when DATABRICKS_CATALOG is unset or the catalog
    has no metric views: a live-creds workspace is not guaranteed to have
    one configured, and a false FAIL there would be noise, not signal.
    """
    from connectors.databricks.semantic_ossie import (
        _METRIC_VIEW_TABLE_TYPES,
        _escape_sql_literal,
        _list_metric_views,
        _quote_dbx_ident,
        compose_document,
        extract_yaml_from_create,
    )
    from src.semantic.document_validation import validate_document

    if not DATABRICKS_CATALOG:
        pytest.skip("DATABRICKS_CATALOG not set — export it to run the metric-view discovery test.")

    client = _make_client()
    views = _list_metric_views(client, DATABRICKS_CATALOG)
    if not views:
        pytest.skip(
            f"No Unity Catalog metric views found in catalog {DATABRICKS_CATALOG!r}. "
            "Point DATABRICKS_CATALOG at a catalog with at least one metric view "
            "to exercise the vendor-fact assertions this test exists for."
        )

    catalog, schema, view, comment = views[0]

    # Vendor fact #1: the real information_schema.tables.table_type spelling
    # matches what _list_metric_views filtered on (_METRIC_VIEW_TABLE_TYPES).
    _cols, info_rows = client.execute_rows(
        f"SELECT table_type FROM {_quote_dbx_ident(catalog)}.information_schema.tables "
        f"WHERE table_schema = '{_escape_sql_literal(schema)}' "
        f"AND table_name = '{_escape_sql_literal(view)}'"
    )
    assert info_rows, f"{catalog}.{schema}.{view} vanished between discovery and verification"
    assert info_rows[0][0] in _METRIC_VIEW_TABLE_TYPES, (
        f"real table_type {info_rows[0][0]!r} is not in _METRIC_VIEW_TABLE_TYPES="
        f"{_METRIC_VIEW_TABLE_TYPES} — the vendor's INFORMATION_SCHEMA vocabulary has "
        "drifted; update connectors/databricks/semantic_ossie.py"
    )

    # Vendor fact #2: SHOW CREATE TABLE really embeds the YAML body between $$.
    fqn_quoted = f"{_quote_dbx_ident(catalog)}.{_quote_dbx_ident(schema)}.{_quote_dbx_ident(view)}"
    _cols, create_rows = client.execute_rows(f"SHOW CREATE TABLE {fqn_quoted}")
    assert create_rows, f"SHOW CREATE TABLE returned nothing for {catalog}.{schema}.{view}"
    create_stmt = str(create_rows[0][0])
    yaml_text = extract_yaml_from_create(create_stmt)
    assert yaml_text is not None, (
        f"SHOW CREATE TABLE for {catalog}.{schema}.{view} did not contain a $$...$$ "
        "YAML body — the assumed metric-view SQL shape has drifted; update "
        "connectors/databricks/semantic_ossie.py::_YAML_BODY_RE"
    )

    doc = compose_document(catalog, schema, view, comment, yaml_text)
    assert doc is not None, f"compose_document returned None for real metric view {catalog}.{schema}.{view}"
    result = validate_document(doc)
    assert result.ok, result.errors
