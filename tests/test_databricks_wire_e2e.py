"""Wire-level tests for the Databricks connector against a real HTTPS server.

These are the closest thing to an end-to-end run that does not need a live
Databricks workspace: the connector's **real** `requests`-based client speaks
the Statement Execution API over TLS to `tests/databricks_fake_warehouse.py`,
Arrow bytes are parsed off an actual socket, and the full server-side chain
(materialized pass → parquet → extract.duckdb → orchestrator rebuild → master
view) runs unmocked on top of it.

What still needs a real workspace (and is therefore NOT claimed here): that
Databricks itself spells the metric-view `table_type` and the `SHOW CREATE
TABLE` body the way this connector expects. Those are vendor facts, not
protocol facts — see the module docstring in
`connectors/databricks/semantic_layer.py`.
"""

from __future__ import annotations

import duckdb
import pytest

from tests.databricks_fake_warehouse import Route, start_fake_warehouse

pa = pytest.importorskip("pyarrow")


_METRIC_VIEW_YAML = """
version: 1.1
source: SELECT * FROM main.sales.orders
dimensions:
  - name: order_date
    expr: o_orderdate
measures:
  - name: order_count
    expr: COUNT(o_orderkey)
"""


def _orders_table(rows: int = 2500):
    return pa.table(
        {
            "order_date": pa.array([f"2026-01-{(i % 28) + 1:02d}" for i in range(rows)]),
            "amount": pa.array([str(i) for i in range(rows)]),
        }
    )


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """A fake warehouse answering both the semantic-layer discovery queries
    and a bulk Arrow extraction, spanning multiple chunks."""
    routes = [
        Route(
            match="information_schema.tables",
            columns=["table_catalog", "table_schema", "table_name", "comment"],
            rows=[["main", "sales", "orders_metrics", "Sales KPIs"]],
        ),
        Route(
            match="SHOW CREATE TABLE",
            columns=["createtab_stmt"],
            rows=[[f"CREATE VIEW x WITH METRICS\nLANGUAGE YAML\nAS $$\n{_METRIC_VIEW_YAML}\n$$"]],
        ),
        # chunk_rows < row count so the client must resolve + fetch several
        # presigned links, the path a single-chunk fake would never exercise.
        Route(match="FROM `main`.`sales`.`orders`", arrow_table=_orders_table(), chunk_rows=1000),
    ]
    wh = start_fake_warehouse(tmp_path, routes, monkeypatch)
    yield wh
    wh.close()


def _client(warehouse):
    from connectors.databricks.client import DatabricksStatementClient

    return DatabricksStatementClient(
        host=warehouse.host,
        token="tok-secret",
        warehouse_id="wh-1",
        poll_interval_s=0.0,
    )


class TestClientOverTheWire:
    def test_inline_query_polls_then_returns_rows(self, warehouse):
        columns, rows = _client(warehouse).execute_rows(
            "SELECT table_catalog, table_schema, table_name, comment "
            "FROM `main`.information_schema.tables WHERE table_type IN ('METRIC_VIEW')"
        )
        assert columns == ["table_catalog", "table_schema", "table_name", "comment"]
        assert rows == [["main", "sales", "orders_metrics", "Sales KPIs"]]
        # The submit answered PENDING, so a real poll round-trip happened.
        assert warehouse.requests_for("/api/2.0/sql/statements")[0].method == "POST"
        assert any(r.method == "GET" for r in warehouse.requests_for("/api/2.0/sql/statements/"))

    def test_bearer_token_is_sent_to_the_api(self, warehouse):
        _client(warehouse).execute_rows("SELECT 1 FROM information_schema.tables")
        api_calls = [r for r in warehouse.requests if r.path.startswith("/api/")]
        assert api_calls, "no API calls recorded"
        assert all(r.headers.get("Authorization") == "Bearer tok-secret" for r in api_calls)

    def test_arrow_result_streams_every_chunk_off_the_wire(self, warehouse):
        result = _client(warehouse).execute_to_arrow_batches("SELECT * FROM `main`.`sales`.`orders`")
        assert result.truncated is False
        batches = list(result.iter_batches())
        assert sum(b.num_rows for b in batches) == 2500
        # 2500 rows / 1000 per chunk → 3 presigned downloads.
        assert len(warehouse.requests_for("/external/")) == 3

    def test_presigned_download_never_carries_the_workspace_token(self, warehouse):
        """The security contract, verified on the actual wire: a presigned URL
        is itself the credential, so forwarding the workspace bearer to the
        storage host would leak it to a third party."""
        result = _client(warehouse).execute_to_arrow_batches("SELECT * FROM `main`.`sales`.`orders`")
        list(result.iter_batches())
        external = warehouse.requests_for("/external/")
        assert external, "no presigned downloads were made"
        for request in external:
            assert "Authorization" not in {k.title() for k in request.headers.keys()}, (
                f"workspace token leaked to the storage host on {request.path}"
            )


class TestSemanticLayerOverTheWire:
    def test_metric_views_land_in_metric_definitions(self, warehouse, e2e_env, monkeypatch):
        from connectors.databricks.semantic_ossie import DatabricksMetricViewAdapter
        from src.repositories import metric_repo
        from src.semantic.importer import import_documents

        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: {
                "host": warehouse.host,
                "warehouse_id": "wh-1",
                "catalog": "main",
                "catalogs": ["main"],
                "token": "tok-secret",
            },
        )
        monkeypatch.setattr(DatabricksMetricViewAdapter, "_client", lambda self, settings: _client(warehouse))

        documents = DatabricksMetricViewAdapter().extract({})
        assert len(documents) == 1

        report = import_documents({"source": "ossie_connection", "source_ref": "databricks_default"}, documents)
        assert report.projection is not None
        assert report.projection.metrics_written == 1

        row = metric_repo().get("ossie_connection/databricks_default/main.sales.orders_metrics/order_count")
        assert row is not None
        assert row["sql"] == "SELECT MEASURE(`order_count`) FROM `main`.`sales`.`orders_metrics`"
        assert row["source"] == "ossie_connection"
        assert row["source_ref"] == "databricks_default"

    def test_discovery_sql_accepts_both_table_type_spellings(self, warehouse):
        """Vocabulary hardening: the emitted predicate must cover the
        underscore AND space spellings, mirroring the BigQuery extractor's
        MATERIALIZED VIEW / MATERIALIZED_VIEW normalisation."""
        from connectors.databricks.semantic_ossie import _list_metric_views

        _list_metric_views(_client(warehouse), "main")
        submitted = [r for r in warehouse.requests if r.method == "POST"]
        assert submitted, "no statement was submitted"
        # The statement body is not recorded, so assert on the constant the
        # query is built from plus a round-trip through the live server.
        from connectors.databricks.semantic_ossie import _METRIC_VIEW_TABLE_TYPES

        assert "METRIC_VIEW" in _METRIC_VIEW_TABLE_TYPES
        assert "METRIC VIEW" in _METRIC_VIEW_TABLE_TYPES


class TestFullMaterializedLoop:
    """Registry row → materialized pass → parquet → extract.duckdb →
    orchestrator rebuild → queryable master view, with real HTTP underneath."""

    def test_row_becomes_a_queryable_master_view(self, warehouse, e2e_env, monkeypatch):
        from app.api.sync import _run_materialized_pass
        from src.db import _ensure_schema
        from src.orchestrator import SyncOrchestrator
        from src.repositories.sync_state import SyncStateRepository
        from src.repositories.table_registry import TableRegistryRepository

        system_db = duckdb.connect(str(e2e_env["data_dir"] / "state" / "system.duckdb"))
        _ensure_schema(system_db)
        monkeypatch.setattr("app.api.sync.table_registry_repo", lambda: TableRegistryRepository(system_db))
        monkeypatch.setattr("app.api.sync.sync_state_repo", lambda: SyncStateRepository(system_db))
        monkeypatch.setattr(
            "connectors.databricks.semantic_layer.resolve_databricks_settings",
            lambda: {
                "host": warehouse.host,
                "warehouse_id": "wh-1",
                "catalog": "main",
                "catalogs": ["main"],
                "token": "tok-secret",
            },
        )

        TableRegistryRepository(system_db).register(
            id="dbx_orders",
            name="dbx_orders",
            source_type="databricks",
            bucket="main.sales",
            source_table="orders",
            query_mode="materialized",
            source_query="SELECT * FROM `main`.`sales`.`orders`",
        )

        try:
            summary = _run_materialized_pass(system_db, bq=None)
            assert summary["errors"] == []
            assert summary["materialized"] == ["dbx_orders"]

            # 1. parquet on disk, with every row that crossed the wire
            parquet = e2e_env["data_dir"] / "extracts" / "databricks" / "data" / "dbx_orders.parquet"
            assert parquet.exists()
            with duckdb.connect() as probe:
                assert probe.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet}')").fetchone()[0] == 2500

            # 2. registered in extract.duckdb per the connector contract
            with duckdb.connect(str(parquet.parent.parent / "extract.duckdb"), read_only=True) as ext:
                meta = ext.execute("SELECT table_name, rows, query_mode FROM _meta").fetchall()
            assert meta == [("dbx_orders", 2500, "materialized")]

            # 3. the orchestrator publishes it as a master view, unmodified
            views = SyncOrchestrator(analytics_db_path=e2e_env["analytics_db"]).rebuild()
            assert "dbx_orders" in [v for group in views.values() for v in group]

            # Read it back through the production read path — the master view
            # body is `SELECT * FROM databricks.dbx_orders`, so this also
            # proves the extract re-ATTACHes for a reader, not just that a
            # view object exists.
            from src.db import get_analytics_db_readonly

            analytics = get_analytics_db_readonly()
            try:
                assert analytics.execute("SELECT COUNT(*) FROM dbx_orders").fetchone()[0] == 2500
            finally:
                analytics.close()

            # 4. sync_state carries the result the manifest serves to `agnes pull`
            state = SyncStateRepository(system_db).get_table_state("dbx_orders")
            assert state["status"] == "ok"
            assert state["rows"] == 2500
            assert len(state["hash"]) == 32
        finally:
            system_db.close()

    def test_result_over_the_cap_is_rejected_and_writes_nothing(self, tmp_path, monkeypatch, e2e_env):
        """`byte_limit` truncation must surface as MaterializeBudgetError with
        no parquet left behind — verified against a server that really flags
        the manifest truncated."""
        from connectors.bigquery.extractor import MaterializeBudgetError
        from connectors.databricks.extractor import materialize_query

        routes = [Route(match="FROM `main`.`sales`.`orders`", arrow_table=_orders_table(50), truncated=True)]
        cert_dir = tmp_path / "capped"
        cert_dir.mkdir(parents=True, exist_ok=True)
        wh = start_fake_warehouse(cert_dir, routes, monkeypatch)
        try:
            with pytest.raises(MaterializeBudgetError) as exc_info:
                materialize_query(
                    "capped_row",
                    client=_client(wh),
                    output_dir=str(tmp_path / "out"),
                    source_query="SELECT * FROM `main`.`sales`.`orders`",
                    max_bytes=1024,
                )
            assert exc_info.value.table_id == "capped_row"
            assert not (tmp_path / "out" / "data" / "capped_row.parquet").exists()
        finally:
            wh.close()
