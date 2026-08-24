"""Tests for RemoteQueryEngine — two-phase BQ registration + DuckDB execution."""

from unittest.mock import MagicMock

import duckdb
import pyarrow as pa
import pytest

from connectors.bigquery.access import BqAccess, BqAccessError, BqProjects
from src.remote_query import RemoteQueryEngine, RemoteQueryError, _validate_bq_sql, _validate_sql


def _make_bq_access(client):
    """Build a BqAccess that yields the given mock client. Used by tests that
    inject a fake BQ client into RemoteQueryEngine."""
    return BqAccess(
        BqProjects(billing="test-billing", data="test-data"),
        client_factory=lambda projects: client,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def analytics_conn():
    conn = duckdb.connect()
    conn.execute("CREATE TABLE orders (id INT, date DATE, amount DECIMAL(10,2))")
    conn.execute("INSERT INTO orders VALUES (1, '2026-01-01', 100.0), (2, '2026-01-15', 200.0)")
    yield conn
    conn.close()


def _make_bq_mock(arrow_table, count_value=None):
    """Build a minimal BQ client mock.

    First call to client.query() returns a count job, second returns a data job.
    If count_value is None, infer it from arrow_table.num_rows.
    """
    if count_value is None:
        count_value = arrow_table.num_rows

    count_arrow = pa.table({"count": pa.array([count_value], type=pa.int64())})

    count_job = MagicMock()
    count_job.to_arrow.return_value = count_arrow

    data_job = MagicMock()
    data_job.to_arrow.return_value = arrow_table

    mock_client = MagicMock()
    mock_client.query.side_effect = [count_job, data_job]

    return mock_client


# ---------------------------------------------------------------------------
# TestRemoteQueryEngineRegister
# ---------------------------------------------------------------------------


class TestRemoteQueryEngineRegister:
    def test_register_bq_success(self, analytics_conn):
        """Mock BQ client returning an Arrow table; verify view is queryable."""
        arrow_table = pa.table(
            {
                "order_id": pa.array([10, 20, 30], type=pa.int64()),
                "revenue": pa.array([1.0, 2.0, 3.0], type=pa.float64()),
            }
        )
        mock_client = _make_bq_mock(arrow_table)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
            max_bq_registration_rows=500_000,
        )

        result = engine.register_bq("bq_orders", "SELECT order_id, revenue FROM bq.orders")

        assert result["alias"] == "bq_orders"
        assert result["rows"] == 3
        assert result["columns"] == ["order_id", "revenue"]
        assert result["memory_mb"] > 0

        # The alias must be queryable from DuckDB
        rows = analytics_conn.execute("SELECT COUNT(*) FROM bq_orders").fetchone()
        assert rows[0] == 3

    def test_register_bq_trailing_semicolon_does_not_break_count_wrap(self, analytics_conn):
        """_validate_bq_sql tolerates one trailing `;`; register_bq must strip
        it before embedding the SQL in the COUNT(*) pre-check subquery, or a
        validated query fails there instead of registering."""
        arrow_table = pa.table({"order_id": pa.array([10, 20, 30], type=pa.int64())})
        mock_client = _make_bq_mock(arrow_table)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
            max_bq_registration_rows=500_000,
        )

        result = engine.register_bq("bq_orders", "SELECT order_id FROM bq.orders;")

        assert result["rows"] == 3
        count_sql = mock_client.query.call_args_list[0].args[0]
        assert ";" not in count_sql

    def test_register_bq_row_limit_exceeded(self, analytics_conn):
        """COUNT pre-check returns a value exceeding the row limit → RemoteQueryError."""
        arrow_table = pa.table({"x": pa.array([1], type=pa.int64())})
        # count exceeds limit
        mock_client = _make_bq_mock(arrow_table, count_value=1_000_000)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
            max_bq_registration_rows=500_000,
        )

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bq_big", "SELECT * FROM bq.huge_table")

        assert exc_info.value.error_type == "row_limit"
        assert exc_info.value.details["count"] == 1_000_000

    def test_register_bq_invalid_alias(self, analytics_conn):
        engine = RemoteQueryEngine(analytics_conn)
        # Space in alias — invalid identifier
        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bad alias", "SELECT 1")
        assert exc_info.value.error_type == "query_error"

        # Reserved alias — information_schema
        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("information_schema", "SELECT 1")
        assert exc_info.value.error_type == "query_error"

        # Valid alias — should not raise from alias validation
        # (will raise later trying to reach BQ without a client, but not from alias check)
        try:
            engine.register_bq("valid_name", "SELECT 1")
        except RemoteQueryError as exc:
            assert exc.error_type != "query_error" or "Invalid alias" not in str(exc)
        except (ImportError, ModuleNotFoundError):
            pass  # Expected — no BQ package in test env

    def test_register_bq_missing_package(self, analytics_conn):
        """When google-cloud-bigquery is not installed, BqAccess raises
        BqAccessError(bq_lib_missing); the engine must translate that to
        RemoteQueryError."""

        def _missing_lib_factory(projects):
            raise BqAccessError(
                "bq_lib_missing",
                "google-cloud-bigquery is not installed",
            )

        bq = BqAccess(
            BqProjects(billing="test-billing", data="test-data"),
            client_factory=_missing_lib_factory,
        )
        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=bq,
            max_bq_registration_rows=500_000,
        )

        with pytest.raises(RemoteQueryError, match="google-cloud-bigquery"):
            engine.register_bq("bq_alias", "SELECT 1")


# ---------------------------------------------------------------------------
# TestRemoteQueryEngineExecute
# ---------------------------------------------------------------------------


class TestRemoteQueryEngineExecute:
    def test_execute_local_only(self, analytics_conn):
        """Query local table; result dict has correct structure."""
        engine = RemoteQueryEngine(analytics_conn)
        result = engine.execute("SELECT id, amount FROM orders ORDER BY id")

        assert result["columns"] == ["id", "amount"]
        assert result["row_count"] == 2
        assert result["truncated"] is False
        assert len(result["rows"]) == 2
        # Non-standard types (Decimal) must be serialized to str
        for row in result["rows"]:
            for val in row:
                assert isinstance(val, (int, float, bool, str, type(None)))

    def test_execute_with_registered_bq(self, analytics_conn):
        """Manually register an Arrow table, then JOIN it with local orders."""
        bq_arrow = pa.table(
            {
                "id": pa.array([1, 2], type=pa.int64()),
                "label": pa.array(["first", "second"], type=pa.utf8()),
            }
        )
        mock_client = _make_bq_mock(bq_arrow)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
            max_bq_registration_rows=500_000,
        )
        engine.register_bq("bq_labels", "SELECT id, label FROM bq.labels")

        result = engine.execute(
            "SELECT o.id, o.amount, b.label FROM orders o JOIN bq_labels b ON o.id = b.id ORDER BY o.id"
        )

        assert result["row_count"] == 2
        assert "label" in result["columns"]

    def test_execute_respects_max_result_rows(self, analytics_conn):
        """When max_result_rows=1, result is truncated after 1 row."""
        engine = RemoteQueryEngine(analytics_conn, max_result_rows=1)
        result = engine.execute("SELECT id FROM orders ORDER BY id")

        assert result["row_count"] == 1
        assert result["truncated"] is True

    def test_execute_invalid_sql(self, analytics_conn):
        """DROP TABLE must be rejected with RemoteQueryError(error_type='query_error')."""
        engine = RemoteQueryEngine(analytics_conn)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.execute("DROP TABLE orders")

        assert exc_info.value.error_type == "query_error"


# ---------------------------------------------------------------------------
# _validate_sql unit tests
# ---------------------------------------------------------------------------


class TestValidateSql:
    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE foo",
            "DELETE FROM foo",
            "INSERT INTO foo VALUES (1)",
            "UPDATE foo SET x=1",
            "ALTER TABLE foo ADD COLUMN y INT",
            "CREATE TABLE foo (x INT)",
            "COPY foo TO '/tmp/out.csv'",
            "ATTACH '/db.duckdb'",
            "DETACH db",
            "LOAD 'extension'",
            "INSTALL httpfs",
            "SELECT read_parquet('/data/file.parquet')",
            "SELECT * FROM '../secret/file'",
            "SELECT 1; DROP TABLE foo",
            # SQLite-compat catalog views leak local view SQL (absolute parquet
            # paths) when this engine runs against a DuckDB conn carrying the
            # orchestrator's master views — kept in lockstep with
            # app/api/query.py's _BLOCKED_SQL_TOKENS.
            "SELECT sql FROM sqlite_master",
            "SELECT sql FROM sqlite_schema",
            "SELECT sql FROM sqlite_temp_master",
            "SELECT sql FROM sqlite_temp_schema",
            "SELECT * FROM duckdb_external_file_cache()",
        ],
    )
    def test_blocked_sql(self, sql):
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_sql(sql)
        assert exc_info.value.error_type == "query_error"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM orders",
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
            "select count(*) from orders",
            "with t as (select 1) select * from t",
        ],
    )
    def test_allowed_sql(self, sql):
        # Should not raise
        _validate_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM orders;",
            "select count(*) from orders;",
        ],
    )
    def test_allows_single_trailing_semicolon(self, sql):
        # A single trailing `;` is routine SQL formatting, not a second
        # statement — must not be confused with the multi-statement guard.
        _validate_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "-- leading comment\nSELECT 1 AS x",
            "-- first\n-- second\nSELECT id FROM orders",
            "/* block comment */ SELECT 1 AS x",
            "/* multi\nline block */\nWITH t AS (SELECT 1) SELECT * FROM t",
        ],
    )
    def test_allowed_sql_with_leading_comment(self, sql):
        # DuckDB tolerates leading comments, so the local `agnes query` path
        # runs them fine — the --remote guard must not reject them either.
        _validate_sql(sql)

    def test_blocked_keyword_in_leading_comment_still_blocked(self):
        # The blocklist must keep scanning the full SQL (comments included),
        # so a leading comment can never smuggle a blocked keyword past it.
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_sql("-- drop this table first\nSELECT 1")
        assert exc_info.value.error_type == "query_error"

    def test_block_comment_false_close_rejects_trailing_non_select(self):
        # `/*/ … */` — the block-comment end marker must not be found
        # overlapping the `/*` opener. DuckDB treats `/*/ SELECT 1 */` as a
        # comment and executes the trailing statement, so the guard must not be
        # fooled into seeing the comment's fake `SELECT` and waving through a
        # non-SELECT (Devin Review on PR #743).
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_sql("/*/ SELECT 1 */ SET memory_limit='1GB'")
        assert exc_info.value.error_type == "query_error"

    def test_block_comment_with_slash_body_before_real_select_allowed(self):
        # `/*/ header */` is a valid block comment (body starts with `/`); a
        # real SELECT after it must still pass. The overlapping-close bug used
        # to reject this too.
        _validate_sql("/*/ header */ SELECT 1")


# ---------------------------------------------------------------------------
# _validate_bq_sql unit tests
# ---------------------------------------------------------------------------


class TestValidateBqSql:
    def test_information_schema_is_allowed(self):
        """INFORMATION_SCHEMA queries must pass BQ SQL validation."""
        # Should not raise
        _validate_bq_sql("SELECT * FROM dataset.INFORMATION_SCHEMA.COLUMNS")

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE x",
            "INSERT INTO x VALUES (1)",
            "DELETE FROM x",
            "UPDATE x SET y=1",
            "ALTER TABLE x ADD COLUMN z INT",
            "CREATE TABLE x (y INT)",
            "TRUNCATE TABLE x",
            "MERGE INTO x USING y ON x.id=y.id WHEN MATCHED THEN UPDATE SET x.a=y.a",
            "SELECT 1; DROP TABLE x",
        ],
    )
    def test_blocked_bq_sql(self, sql):
        """Write/mutation operations must be rejected."""
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_bq_sql(sql)
        assert exc_info.value.error_type == "query_error"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM dataset.INFORMATION_SCHEMA.COLUMNS",
            "SELECT id FROM project.dataset.table",
            "WITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
        ],
    )
    def test_allowed_bq_sql(self, sql):
        """Valid read-only BQ queries must pass."""
        # Should not raise
        _validate_bq_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id FROM project.dataset.table;",
            "SELECT * FROM dataset.INFORMATION_SCHEMA.COLUMNS;",
        ],
    )
    def test_allows_single_trailing_semicolon(self, sql):
        # A single trailing `;` is routine SQL formatting, not a second
        # statement — must not be confused with the multi-statement guard.
        _validate_bq_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "-- leading comment\nSELECT 1 AS x",
            "-- first\n-- second\nSELECT id FROM project.dataset.table",
            "/* block */ SELECT 1 AS x",
            "/* multi\nline */\nWITH cte AS (SELECT 1 AS x) SELECT x FROM cte",
        ],
    )
    def test_allowed_bq_sql_with_leading_comment(self, sql):
        # BigQuery tolerates leading comments; the guard must not reject a
        # stored metric whose SQL begins with a `--` header comment.
        _validate_bq_sql(sql)

    def test_blocked_keyword_in_leading_comment_still_blocked(self):
        # Blocklist keeps scanning the full SQL — a comment cannot smuggle a
        # blocked write keyword past _validate_bq_sql.
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_bq_sql("-- drop the old rows\nSELECT 1")
        assert exc_info.value.error_type == "query_error"

    def test_block_comment_false_close_rejects_trailing_non_select(self):
        # Same `/*/` overlapping-close guard for the BQ validator.
        with pytest.raises(RemoteQueryError) as exc_info:
            _validate_bq_sql("/*/ SELECT 1 */ SET x = 1")
        assert exc_info.value.error_type == "query_error"

    def test_block_comment_with_slash_body_before_real_select_allowed(self):
        _validate_bq_sql("/*/ header */ SELECT 1")


# ---------------------------------------------------------------------------
# Hybrid Query BigQuery integration tests (mocked BQ client)
# ---------------------------------------------------------------------------


class TestHybridQueryBigQuery:
    """Tests for the two-phase BQ registration + DuckDB execution flow.

    These test the RemoteQueryEngine's register_bq + execute pipeline
    with a mocked BQ client, simulating the /api/query/hybrid endpoint.
    """

    def test_register_bq_creates_temporary_view_in_duckdb(self, analytics_conn):
        """register_bq parameter creates a temporary view in DuckDB that is
        queryable via the registered alias."""
        arrow_table = pa.table(
            {
                "date": pa.array(["2026-01-01", "2026-01-15"], type=pa.utf8()),
                "views": pa.array([100, 200], type=pa.int64()),
            }
        )
        mock_client = _make_bq_mock(arrow_table)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
        )

        result = engine.register_bq("traffic", "SELECT date, views FROM bq.traffic")
        assert result["alias"] == "traffic"
        assert result["rows"] == 2

        # The alias is queryable from DuckDB as a view/table
        rows = analytics_conn.execute("SELECT views FROM traffic ORDER BY views").fetchall()
        assert rows[0][0] == 100
        assert rows[1][0] == 200

    def test_sql_query_can_join_local_table_with_registered_bq(self, analytics_conn):
        """SQL query can JOIN local table with registered BQ result."""
        # Local orders table already exists from fixture
        bq_arrow = pa.table(
            {
                "date": pa.array(["2026-01-01", "2026-01-15"], type=pa.utf8()),
                "views": pa.array([50, 75], type=pa.int64()),
            }
        )
        mock_client = _make_bq_mock(bq_arrow)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
        )
        engine.register_bq("traffic", "SELECT date, views FROM bq.traffic")

        result = engine.execute(
            "SELECT o.id, o.amount, t.views FROM orders o JOIN traffic t ON o.date = t.date ORDER BY o.id"
        )

        assert result["row_count"] == 2
        assert "views" in result["columns"]
        assert "amount" in result["columns"]
        # Verify the join produced correct data
        assert result["rows"][0][2] == 50  # views for 2026-01-01
        assert result["rows"][1][2] == 75  # views for 2026-01-15

    def test_multiple_register_bq_parameters_simultaneously(self, analytics_conn):
        """Multiple register_bq parameters work simultaneously — each creates
        an independent view that can be joined together."""
        traffic_arrow = pa.table(
            {
                "date": pa.array(["2026-01-01", "2026-01-15"], type=pa.utf8()),
                "views": pa.array([100, 200], type=pa.int64()),
            }
        )
        revenue_arrow = pa.table(
            {
                "date": pa.array(["2026-01-01", "2026-01-15"], type=pa.utf8()),
                "revenue": pa.array([1000.0, 2000.0], type=pa.float64()),
            }
        )

        # First call returns count + data for traffic, second for revenue
        traffic_count = pa.table({"count": pa.array([2], type=pa.int64())})
        revenue_count = pa.table({"count": pa.array([2], type=pa.int64())})

        traffic_count_job = MagicMock()
        traffic_count_job.to_arrow.return_value = traffic_count
        traffic_data_job = MagicMock()
        traffic_data_job.to_arrow.return_value = traffic_arrow

        revenue_count_job = MagicMock()
        revenue_count_job.to_arrow.return_value = revenue_count
        revenue_data_job = MagicMock()
        revenue_data_job.to_arrow.return_value = revenue_arrow

        mock_client = MagicMock()
        mock_client.query.side_effect = [
            traffic_count_job,
            traffic_data_job,
            revenue_count_job,
            revenue_data_job,
        ]

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
        )

        engine.register_bq("traffic", "SELECT date, views FROM bq.traffic")
        engine.register_bq("revenue", "SELECT date, revenue FROM bq.revenue")

        result = engine.execute(
            "SELECT t.date, t.views, r.revenue FROM traffic t JOIN revenue r ON t.date = r.date ORDER BY t.views"
        )

        assert result["row_count"] == 2
        assert set(result["columns"]) == {"date", "views", "revenue"}

    def test_invalid_bq_sql_returns_meaningful_error(self, analytics_conn):
        """Invalid BQ SQL (blocked keyword) returns RemoteQueryError with
        error_type='query_error'."""
        engine = RemoteQueryEngine(analytics_conn)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bad", "DROP TABLE important_data")

        assert exc_info.value.error_type == "query_error"
        assert "blocked" in str(exc_info.value).lower() or "drop" in str(exc_info.value).lower()

    def test_missing_bigquery_credentials_returns_proper_error(self, analytics_conn):
        """Missing BigQuery credentials surface as RemoteQueryError, not a crash.

        Simulated by a BqAccess whose client_factory raises BqAccessError —
        the same shape get_bq_access() would produce on bq_lib_missing /
        not_configured in production.
        """

        def _missing_lib_factory(projects):
            raise BqAccessError(
                "bq_lib_missing",
                "google-cloud-bigquery is not installed",
            )

        bq = BqAccess(
            BqProjects(billing="test-billing", data="test-data"),
            client_factory=_missing_lib_factory,
        )
        engine = RemoteQueryEngine(analytics_conn, bq_access=bq)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bq_data", "SELECT 1")

        assert exc_info.value.error_type == "bq_error"
        # Should mention the missing package or config, not a raw traceback
        detail = str(exc_info.value).lower()
        assert "bigquery" in detail or "google" in detail

    def test_bq_query_error_returns_meaningful_error(self, analytics_conn):
        """When the BQ client raises an exception during query, the engine
        wraps it in RemoteQueryError with error_type='bq_error'."""
        mock_client = MagicMock()
        mock_client.query.side_effect = Exception("Connection refused")

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
        )

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bq_data", "SELECT 1 FROM dataset.table")

        assert exc_info.value.error_type == "bq_error"
        assert "connection refused" in str(exc_info.value).lower()

    def test_bq_count_precheck_failure_returns_bq_error(self, analytics_conn):
        """When the BQ COUNT(*) pre-check fails, the engine returns
        RemoteQueryError with error_type='bq_error'."""
        mock_client = MagicMock()
        count_job = MagicMock()
        count_job.to_arrow.side_effect = Exception("Permission denied")
        mock_client.query.return_value = count_job

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
        )

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bq_data", "SELECT 1 FROM dataset.table")

        assert exc_info.value.error_type == "bq_error"

    def test_bq_row_limit_exceeded_returns_row_limit_error(self, analytics_conn):
        """When BQ result exceeds max_bq_registration_rows, returns
        RemoteQueryError with error_type='row_limit'."""
        arrow_table = pa.table({"x": pa.array([1], type=pa.int64())})
        mock_client = _make_bq_mock(arrow_table, count_value=999_999)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
            max_bq_registration_rows=500_000,
        )

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("big_data", "SELECT * FROM huge_table")

        assert exc_info.value.error_type == "row_limit"
        assert exc_info.value.details["count"] == 999_999

    def test_bq_memory_limit_exceeded_returns_memory_limit_error(self, analytics_conn):
        """When the Arrow table exceeds max_memory_mb, returns
        RemoteQueryError with error_type='memory_limit'."""
        # Create a table that reports a large nbytes
        big_arrow = pa.table({"x": pa.array([1] * 1000, type=pa.int64())})
        mock_client = _make_bq_mock(big_arrow)

        engine = RemoteQueryEngine(
            analytics_conn,
            bq_access=_make_bq_access(mock_client),
            max_memory_mb=0.001,  # tiny limit → guaranteed exceed
        )

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("big_data", "SELECT * FROM wide_table")

        assert exc_info.value.error_type == "memory_limit"

    def test_hybrid_query_execute_error_returns_query_error(self, analytics_conn):
        """When the final DuckDB SQL execution fails, returns
        RemoteQueryError with error_type='query_error'."""
        engine = RemoteQueryEngine(analytics_conn)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.execute("SELECT * FROM nonexistent_table")

        assert exc_info.value.error_type == "query_error"

    def test_reserved_alias_rejected(self, analytics_conn):
        """Reserved aliases (information_schema, main, etc.) are rejected."""
        engine = RemoteQueryEngine(analytics_conn)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("information_schema", "SELECT 1")

        assert exc_info.value.error_type == "query_error"

    def test_invalid_alias_rejected(self, analytics_conn):
        """Aliases that aren't valid SQL identifiers are rejected."""
        engine = RemoteQueryEngine(analytics_conn)

        with pytest.raises(RemoteQueryError) as exc_info:
            engine.register_bq("bad alias!", "SELECT 1")

        assert exc_info.value.error_type == "query_error"
