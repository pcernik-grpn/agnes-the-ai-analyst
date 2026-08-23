# tests/test_v2_scan.py
import importlib
from unittest.mock import MagicMock, patch
import pyarrow as pa
import pytest
from fastapi import HTTPException

from app.api.v2_arrow import parse_ipc_bytes


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module

    importlib.reload(db_module)
    yield db_module


def _seed(conn):
    _ensure_admin1(conn)
    from src.repositories.table_registry import TableRegistryRepository

    TableRegistryRepository(conn).register(
        id="bq_view",
        name="bq_view",
        source_type="bigquery",
        bucket="ds",
        source_table="bq_view",
        query_mode="remote",
    )


def _ensure_admin1(conn):
    """Seed an admin user with id='admin1' + Admin group membership so
    {"id": "admin1", ...} dicts pass the can_access admin shortcut."""
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


def _bq(billing="billing-proj", data="data-proj"):
    """Build a BqAccess wired to default factories. For tests that monkeypatch
    `_run_bq_scan` whole, the inner factories are never called."""
    from connectors.bigquery.access import BqAccess, BqProjects

    return BqAccess(BqProjects(billing=billing, data=data))


class TestScan:
    def test_returns_arrow_ipc_for_simple_request(self, reload_db, monkeypatch):
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"], "country_code": ["CZ"]})
        monkeypatch.setattr(
            v2_scan,
            "_run_bq_scan",
            lambda bq, sql, **kw: (fake_table, {}),
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 100,
            }
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc_bytes)
        assert got.num_rows == 1
        assert got.column_names == ["event_date", "country_code"]

    def test_quota_concurrent_exceeded_raises_429(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.v2_quota import QuotaTracker, QuotaExceededError, KIND_CONCURRENT

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql, **kw: (fake_table, {}))

        tracker = QuotaTracker(max_concurrent_per_user=1, max_daily_bytes_per_user=10**12)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "limit": 1}

            # Hold one concurrent slot
            with tracker.acquire(user="a@x.com"):
                with pytest.raises(QuotaExceededError) as e:
                    v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
                assert e.value.kind == KIND_CONCURRENT
        finally:
            conn.close()

    def test_validator_rejection_propagates(self, reload_db, monkeypatch):
        from app.api import v2_scan
        from app.api.where_validator import WhereValidationError, REJECT_UNKNOWN_FUNCTION

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )

        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "where": "event_date = NUKE_FN()",
            }
            with pytest.raises(WhereValidationError) as e:
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
            assert e.value.kind == REJECT_UNKNOWN_FUNCTION
        finally:
            conn.close()


class TestOrderByValidation:
    """Regression: order_by was concatenated raw into FROM clause SQL — exploitable."""

    def test_unknown_column_rejected(self, reload_db, monkeypatch):
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "order_by": ["bogus_col"], "limit": 1}
            with pytest.raises(ValueError, match="unknown order_by"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_subquery_injection_rejected(self, reload_db, monkeypatch):
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "order_by": ["(SELECT secret FROM read_csv('/etc/passwd') LIMIT 1)"],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid order_by"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_backtick_in_column_name_rejected(self, reload_db, monkeypatch):
        """Defense in depth: even though BQ INFORMATION_SCHEMA never returns
        backticks in column names, an analyst-supplied select entry containing
        one must be rejected at the validator. Otherwise it would break out
        of the `…` quoted identifier in _build_bq_sql."""
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date`+ INJECTED --"],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid column name"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_double_quote_in_column_name_rejected(self, reload_db, monkeypatch):
        """Same defense for the local DuckDB path which uses `\"…\"` quoting."""
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"id": "INTEGER"},
        )
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            from src.repositories.table_registry import TableRegistryRepository

            TableRegistryRepository(conn).register(
                id="local_t",
                name="local_t",
                source_type="keboola",
                bucket="b",
                source_table="local_t",
                query_mode="local",
            )
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "local_t",
                "select": ['id"; DROP TABLE x; --'],
                "limit": 1,
            }
            with pytest.raises(ValueError, match="invalid column name"):
                v2_scan.run_scan(conn, user, req, bq=_bq(data=""), quota=tracker)
        finally:
            conn.close()

    def test_reserved_word_columns_get_quoted_in_bq_sql(self):
        """Regression: a column literally named `order` (a SQL reserved word)
        must be backtick-quoted in BQ SQL, otherwise the generated query
        would be `SELECT order FROM ...` which doesn't parse."""
        from app.api.v2_scan import _build_bq_sql, ScanRequest

        sql = _build_bq_sql(
            {"bucket": "ds", "source_table": "t"},
            "p",
            ScanRequest(table_id="t", select=["order", "group"], order_by=["order DESC"], limit=10),
        )
        assert "`order`" in sql
        assert "`group`" in sql
        assert "SELECT order " not in sql.lower().replace("`", "")  # not unquoted

    def test_known_column_with_direction_accepted(self, reload_db, monkeypatch):
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql, **kw: (fake_table, {}))
        tracker = v2_scan._build_quota_tracker()
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date"], "order_by": ["event_date DESC"], "limit": 1}
            # No exception
            v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()


class TestBqAccessErrors:
    """Issue #134: structured 502/400 translation on BQ errors in scan path.

    These tests exercise the REAL translation path through `BqAccess` +
    `translate_bq_error` by injecting a BQ client whose query() raises the
    Google API exception. That's the production path since #752 moved the
    billable scan job onto `client.query(...)` (was the DuckDB
    `bigquery_query()` extension) — Phase 1 monkeypatches of `_run_bq_scan`
    whole would skip the translation logic and only test the outer wrap
    (which has been removed in Phase 2)."""

    def test_scan_returns_502_on_bq_forbidden_serviceusage(self, reload_db, bq_access):
        """When _run_bq_scan raises Forbidden mentioning serviceusage, the
        endpoint must translate to HTTP 502 with `cross_project_forbidden`
        and a hint that mentions billing_project."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        # Mock BQ client whose query() raises Forbidden — exercises the
        # translation path in _run_bq_scan.
        mock_client = MagicMock()
        mock_client.query.side_effect = Forbidden("Permission denied: serviceusage.services.use on project foo")
        bq = bq_access(client=mock_client, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date", "country_code"],
                "where": "event_date > DATE '2026-01-01'",
                "limit": 1000,
            }
            with patch.object(
                v2_scan,
                "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    (v2_scan.scan_endpoint(raw=req, user=user, conn=conn, bq=bq))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "hint" in detail["details"]
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_scan_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, bq_access):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden`."""
        from app.api import v2_scan
        from google.api_core.exceptions import Forbidden

        mock_client = MagicMock()
        mock_client.query.side_effect = Forbidden("Access Denied: Table foo.bar.baz: User does not have permission")
        bq = bq_access(client=mock_client, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "limit": 1,
            }
            with patch.object(
                v2_scan,
                "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    (v2_scan.scan_endpoint(raw=req, user=user, conn=conn, bq=bq))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_scan_returns_400_on_bq_bad_request(self, reload_db, bq_access):
        """`/scan` SQL is user-derived (built from req.select/where/order_by),
        so a BQ BadRequest must surface as HTTP 400 with `bq_bad_request`."""
        from app.api import v2_scan
        from google.api_core.exceptions import BadRequest

        mock_client = MagicMock()
        mock_client.query.side_effect = BadRequest("Syntax error: unexpected token at line 1, column 5")
        bq = bq_access(client=mock_client, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            req = {
                "table_id": "bq_view",
                "select": ["event_date"],
                "limit": 1,
            }
            with patch.object(
                v2_scan,
                "_resolve_schema",
                lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
            ):
                with pytest.raises(HTTPException) as exc_info:
                    (v2_scan.scan_endpoint(raw=req, user=user, conn=conn, bq=bq))
        finally:
            conn.close()

        assert exc_info.value.status_code == 400
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_bad_request"
        assert "Syntax error" in detail["message"]


def test_resolve_schema_passes_bq_kwarg_to_build_schema(monkeypatch, bq_access):
    """Regression guard: _resolve_schema must call build_schema with the new bq= kwarg.

    Previously called with project_id= kwarg from the pre-Phase-2 signature, which
    throws TypeError after build_schema was migrated to take bq= in #134 Task 2.9.
    Caught in final code review. Every other test in this file monkeypatches
    _resolve_schema wholesale, so the bug slipped past the suite.

    This test does NOT monkeypatch _resolve_schema — it stubs build_schema one
    layer up to capture the kwargs _resolve_schema actually passes.
    """
    from app.api import v2_scan

    captured_kwargs: dict = {}

    def fake_build_schema(conn, user, table_id, **kwargs):
        captured_kwargs.update(kwargs)
        return {"columns": []}

    monkeypatch.setattr("app.api.v2_scan.build_schema", fake_build_schema)
    bq = bq_access()  # default test billing/data

    v2_scan._resolve_schema(conn=None, user=None, table_id="t", bq=bq)

    assert "bq" in captured_kwargs, "build_schema must be called with bq= kwarg, not project_id="
    assert "project_id" not in captured_kwargs, (
        "build_schema no longer accepts project_id= — that's the bug this guards"
    )
    assert captured_kwargs["bq"] is bq


def _reader_of(rows: int) -> pa.RecordBatchReader:
    """A streaming reader over `rows` int64 rows, in 512-row batches — the
    shape duckdb>=1.5 `.arrow()` returns instead of a materialized Table."""
    table = pa.table({"v": list(range(rows))})
    return pa.RecordBatchReader.from_batches(table.schema, table.to_batches(max_chunksize=512))


class TestMaxResultBytesTruncation:
    """Regression for the 2026-06-11 production crash: duckdb>=1.5 `.arrow()`
    returns a pyarrow RecordBatchReader, and the max_result_bytes truncation
    guard assumed a pyarrow.Table (`.num_rows`/`.slice`) — every over-cap
    scan result died with AttributeError instead of being truncated."""

    def _run(self, reload_db, monkeypatch, result, raw_request, cap=4096):
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"v": "INT64"},
        )
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql, **kw: (result, {}))
        import app.api.query as query_mod

        monkeypatch.setattr(
            query_mod,
            "run_remote_select_to_arrow",
            lambda conn, user, sql, *, bq, quota, policy_info=None: result,
        )
        monkeypatch.setattr(v2_scan, "_max_result_bytes", lambda: cap)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            tracker = v2_scan._build_quota_tracker()
            return v2_scan.run_scan(
                conn,
                user,
                raw_request,
                bq=_bq(data="proj"),
                quota=tracker,
            )
        finally:
            conn.close()

    def test_bq_reader_over_cap_truncates_instead_of_crashing(
        self,
        reload_db,
        monkeypatch,
    ):
        ipc = self._run(
            reload_db,
            monkeypatch,
            _reader_of(10_000),
            {"table_id": "bq_view", "select": ["v"]},
        )
        got = parse_ipc_bytes(ipc)
        assert 0 < got.num_rows < 2_000  # truncated near the 4 KiB cap

    def test_from_query_reader_over_cap_truncates_instead_of_crashing(
        self,
        reload_db,
        monkeypatch,
    ):
        ipc = self._run(
            reload_db,
            monkeypatch,
            _reader_of(10_000),
            {"from_query": "SELECT v FROM t"},
        )
        got = parse_ipc_bytes(ipc)
        assert 0 < got.num_rows < 2_000

    def test_table_over_cap_still_truncates(self, reload_db, monkeypatch):
        """Characterization: the pre-1.5 pyarrow.Table shape keeps truncating."""
        table = pa.table({"v": list(range(10_000))})
        ipc = self._run(
            reload_db,
            monkeypatch,
            table,
            {"table_id": "bq_view", "select": ["v"]},
        )
        got = parse_ipc_bytes(ipc)
        assert 0 < got.num_rows < 2_000

    def test_reader_under_cap_returns_all_rows(self, reload_db, monkeypatch):
        ipc = self._run(
            reload_db,
            monkeypatch,
            _reader_of(100),
            {"table_id": "bq_view", "select": ["v"]},
            cap=10_000_000,
        )
        got = parse_ipc_bytes(ipc)
        assert got.num_rows == 100
        assert got.column_names == ["v"]


class TestMaterializedScanServedLocally:
    """A `source_type='bigquery'` + `query_mode='materialized'` row must be
    served from the server-side parquet the scheduled materialize run wrote —
    never by re-scanning the raw upstream BQ table (a billable-scan cost
    regression). Mirrors the v2_schema materialized branch, issue #261."""

    SCHEMA = {"v": "INT64", "d": "DATE", "s": "STRING", "x": "STRING"}

    def _seed_materialized(self, conn, table_id="mat_t"):
        _ensure_admin1(conn)
        from src.repositories.table_registry import TableRegistryRepository

        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type="bigquery",
            bucket="ds",
            source_table="raw_events",
            query_mode="materialized",
        )

    def _write_parquet(self, tmp_path):
        """Write the materialized parquet exactly where materialize_query()
        puts it: ${DATA_DIR}/extracts/bigquery/data/<table_id>.parquet."""
        import datetime

        import pyarrow.parquet as pq

        table = pa.table(
            {
                "v": [1, 2, 3, 4, 5],
                "d": [
                    datetime.date(2026, 1, 1),
                    datetime.date(2026, 1, 15),
                    datetime.date(2026, 2, 1),
                    datetime.date(2026, 2, 15),
                    datetime.date(2026, 3, 1),
                ],
                "s": ["alpha", "beta", "gamma", "delta", "epsilon"],
                "x": ["1", "2", "3", "4", "5"],
            }
        )
        data_dir = tmp_path / "extracts" / "bigquery" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data_dir / "mat_t.parquet")
        return table

    def _patch_no_bq(self, monkeypatch):
        """Every BQ touchpoint raises — proving materialized scans never
        reach BigQuery, on the happy path or any error path."""
        from app.api import v2_scan

        def _boom(*a, **kw):
            raise AssertionError("BigQuery must not be touched for a materialized row")

        monkeypatch.setattr(v2_scan, "_run_bq_scan", _boom)
        monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _boom)
        monkeypatch.setattr(v2_scan, "_resolve_schema", lambda *a, **kw: dict(self.SCHEMA))

    def _run(self, reload_db, monkeypatch, req, tracker=None):
        from app.api import v2_scan

        self._patch_no_bq(monkeypatch)
        conn = reload_db.get_system_db()
        try:
            self._seed_materialized(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            tracker = tracker or v2_scan._build_quota_tracker()
            return v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()

    def test_served_from_parquet_without_bq(self, reload_db, tmp_path, monkeypatch):
        self._write_parquet(tmp_path)
        ipc = self._run(reload_db, monkeypatch, {"table_id": "mat_t", "select": ["v"]})
        got = parse_ipc_bytes(ipc)
        assert got.column_names == ["v"]
        assert sorted(got.column("v").to_pylist()) == [1, 2, 3, 4, 5]

    def test_scan_resolves_parquet_keyed_by_registry_name(self, reload_db, tmp_path, monkeypatch):
        """The materialized sync keys the parquet filename by registry `name`
        (`_run_materialized_pass` convention) while /scan receives the `id`;
        the register handler slugifies the id from the name (lower +
        spaces→underscores), so the two routinely differ and the id-keyed
        lookup 404-ed a healthy, fully-synced table."""
        import pyarrow.parquet as pq

        from app.api import v2_scan

        self._patch_no_bq(monkeypatch)
        data_dir = tmp_path / "extracts" / "keboola" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"v": [1, 2, 3]}), data_dir / "Mat Orders 90d.parquet")

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            from src.repositories.table_registry import TableRegistryRepository

            TableRegistryRepository(conn).register(
                id="mat_orders_90d",
                name="Mat Orders 90d",
                source_type="keboola",
                bucket="in.c-main",
                source_table="MAT_ORDERS_90D",
                query_mode="materialized",
            )
            user = {"id": "admin1", "email": "a@x.com"}
            ipc = v2_scan.run_scan(
                conn,
                user,
                {"table_id": "mat_orders_90d", "select": ["v"]},
                bq=_bq(data="proj"),
                quota=v2_scan._build_quota_tracker(),
            )
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc)
        assert sorted(got.column("v").to_pylist()) == [1, 2, 3]

    def test_applies_select_where_limit_order(self, reload_db, tmp_path, monkeypatch):
        self._write_parquet(tmp_path)
        ipc = self._run(
            reload_db,
            monkeypatch,
            {
                "table_id": "mat_t",
                "select": ["v"],
                "where": "v > 2",
                "order_by": ["v DESC"],
                "limit": 2,
            },
        )
        got = parse_ipc_bytes(ipc)
        assert got.column("v").to_pylist() == [5, 4]

    @pytest.mark.parametrize(
        "where,expected_v",
        [
            # The exact BQ-flavor patterns deployed workspaces teach agents
            # to copy into `agnes snapshot create --where`. Each must
            # validate (parse=bigquery), transpile (render=duckdb), and
            # filter the parquet correctly.
            ("d >= DATE_SUB(DATE '2026-02-01', INTERVAL 30 DAY)", [2, 3, 4, 5]),
            ("d >= DATE '2026-02-01'", [3, 4, 5]),
            ("REGEXP_CONTAINS(s, r'^(al|be)')", [1, 2]),
            ("CAST(x AS INT64) = 3", [3]),
            ("DATE_TRUNC(d, MONTH) = DATE '2026-02-01'", [3, 4]),
        ],
    )
    def test_transpiles_bq_where_flavor(self, reload_db, tmp_path, monkeypatch, where, expected_v):
        self._write_parquet(tmp_path)
        ipc = self._run(
            reload_db,
            monkeypatch,
            {"table_id": "mat_t", "select": ["v"], "where": where, "order_by": ["v"]},
        )
        got = parse_ipc_bytes(ipc)
        assert got.column("v").to_pylist() == expected_v

    def test_accepts_duckdb_flavor_where(self, reload_db, tmp_path, monkeypatch):
        """The schema endpoint advertises sql_flavor='duckdb' for materialized
        rows (#261), so a client following that guidance writes DuckDB syntax
        (e.g. `::` casts). sqlglot's permissive BigQuery parse accepts it and
        the execution-dialect render normalizes it — both flavors must work
        (PR #946 review, schema/scan dialect mismatch)."""
        self._write_parquet(tmp_path)
        ipc = self._run(
            reload_db,
            monkeypatch,
            {"table_id": "mat_t", "select": ["v"], "where": "x::BIGINT = 3"},
        )
        got = parse_ipc_bytes(ipc)
        assert got.column("v").to_pylist() == [3]

    def test_runtime_conversion_error_in_data_raises_400_not_500(self, reload_db, tmp_path, monkeypatch):
        """Pin: DuckDB surfaces data-dependent conversion errors eagerly at
        execute().arrow() — inside the fail-loud guard. If a future duckdb
        moved evaluation into lazy batch streaming, the error would fire
        during IPC serialization (outside the guard) and escape as an
        unhandled duckdb.Error (500); this test would catch that regression."""
        import pyarrow.parquet as pq

        bad = pa.table({"v": list(range(10_000)), "x": [str(i) for i in range(9_999)] + ["abc"]})
        data_dir = tmp_path / "extracts" / "bigquery" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(bad, data_dir / "mat_t.parquet")
        with pytest.raises(ValueError, match="local scan failed"):
            self._run(
                reload_db,
                monkeypatch,
                {"table_id": "mat_t", "select": ["v"], "where": "CAST(x AS INT64) = 3"},
            )

    def test_corrupt_parquet_propagates_not_client_400(self, reload_db, tmp_path, monkeypatch):
        """The other side of the fail-loud boundary (PR #946 review round 3):
        a corrupt/unreadable parquet is an operational failure the caller
        cannot fix by editing the query, so it must NOT be converted to a
        client-facing 400. Empirical duckdb quirk: it raises
        InvalidInputException, which subclasses ProgrammingError — the guard
        re-raises it explicitly before the client-error catch."""
        import duckdb

        data_dir = tmp_path / "extracts" / "bigquery" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "mat_t.parquet").write_bytes(b"not a parquet file")
        with pytest.raises(duckdb.InvalidInputException) as exc_info:
            self._run(reload_db, monkeypatch, {"table_id": "mat_t", "select": ["v"]})
        assert not isinstance(exc_info.value, ValueError)

    def test_unexecutable_where_raises_valueerror_not_500(self, reload_db, tmp_path, monkeypatch):
        """A predicate that validates but fails at DuckDB bind/execution time
        must surface as ValueError (→ 400), not an unhandled duckdb.Error."""
        self._write_parquet(tmp_path)
        with pytest.raises(ValueError, match="local scan failed"):
            self._run(
                reload_db,
                monkeypatch,
                # validates fine, but DuckDB can't convert 'abc' to INT64
                {"table_id": "mat_t", "select": ["v"], "where": "v = 'abc'"},
            )

    def test_missing_parquet_raises_404_no_bq_fallback(self, reload_db, monkeypatch):
        """Materialize hasn't run yet → 404. NEVER a fallback to a billable
        raw-table scan (the incident lesson) — the raising _run_bq_scan patch
        would fail this test if the fallback existed."""
        with pytest.raises(FileNotFoundError):
            self._run(reload_db, monkeypatch, {"table_id": "mat_t", "select": ["v"]})

    def test_missing_parquet_real_schema_path_raises_404(self, reload_db, monkeypatch):
        """Regression (PR #946 review): with `_resolve_schema` NOT stubbed,
        the real build_schema raises v2_schema.NotFound for a materialized
        row whose parquet is absent. That must translate to FileNotFoundError
        (→ 404), not escape the endpoints' except tuples as a 500.

        Uses a dedicated table_id so the module-level schema TTLCache can
        never satisfy the lookup from a previous test's cached entry."""
        from app.api import v2_scan

        def _boom(*a, **kw):
            raise AssertionError("BigQuery must not be touched for a materialized row")

        monkeypatch.setattr(v2_scan, "_run_bq_scan", _boom)
        monkeypatch.setattr(v2_scan, "_bq_dry_run_bytes", _boom)
        # NOTE: _resolve_schema deliberately NOT patched — exercises the real
        # build_schema path (registry row exists, parquet does not).
        conn = reload_db.get_system_db()
        try:
            self._seed_materialized(conn, table_id="mat_t_cold")
            user = {"id": "admin1", "email": "a@x.com"}
            tracker = v2_scan._build_quota_tracker()
            with pytest.raises(FileNotFoundError):
                v2_scan.run_scan(
                    conn,
                    user,
                    {"table_id": "mat_t_cold", "select": ["v"]},
                    bq=_bq(data="proj"),
                    quota=tracker,
                )
            with pytest.raises(FileNotFoundError):
                v2_scan.estimate(conn, user, {"table_id": "mat_t_cold"}, bq=_bq(data="proj"))
        finally:
            conn.close()

    def test_counts_result_bytes_toward_daily_quota(self, reload_db, tmp_path, monkeypatch):
        from app.api.v2_quota import QuotaTracker

        self._write_parquet(tmp_path)
        tracker = QuotaTracker(max_concurrent_per_user=5, max_daily_bytes_per_user=10**9)
        self._run(reload_db, monkeypatch, {"table_id": "mat_t", "select": ["v"]}, tracker=tracker)
        assert tracker.bytes_used_today("a@x.com") > 0


class TestScanAccessPolicyBqBranch:
    """Task 13 (§8 ratchet) — `run_scan`'s BQ `use_bq` branch had NO
    access-policy enforcement at all: `_build_bq_sql` + `_run_bq_scan` push
    straight to BigQuery with no `policied_relation` call anywhere on the
    path. `_run_bq_scan` is monkeypatched to return a recognizable Arrow
    table so a regression (the new guard silently not firing) shows up as
    leaked content reaching the caller's IPC bytes, not just a passing
    assert.
    """

    def _register_policied_bq_table(self, conn, table_id: str) -> None:
        from src.repositories.table_registry import TableRegistryRepository

        repo = TableRegistryRepository(conn)
        repo.register(
            id=table_id,
            name=table_id,
            source_type="bigquery",
            bucket="ds",
            source_table=table_id,
            query_mode="remote",
        )
        repo.set_access_policy(table_id, sql=f"SELECT * FROM {table_id}", note="test", updated_by="admin")

    def test_non_admin_fails_closed_instead_of_leaking_the_raw_bq_rows(self, reload_db, monkeypatch):
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        from app.api import v2_scan

        leaked = pa.table({"secret_col": ["leaked-row"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql, **kw: (leaked, {}))
        monkeypatch.setattr(v2_scan, "_resolve_schema", lambda *a, **kw: {"secret_col": "STRING"})
        # can_access_table's real stack-gate needs a data-package grant this
        # test has no reason to set up — the policy-guard behavior under
        # test fires AFTER that check.
        monkeypatch.setattr(v2_scan, "can_access_table", lambda user, tid, conn: True)

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_policied_bq_table(conn, "bq_scan_policied")
            non_admin = {"id": "viewer1", "email": "viewer@x.com"}
            req = {"table_id": "bq_scan_policied", "select": ["secret_col"], "limit": 10}
            tracker = v2_scan._build_quota_tracker()
            with pytest.raises(HTTPException) as exc_info:
                v2_scan.run_scan(conn, non_admin, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == {"reason": "policy_error", "table": "bq_scan_policied"}

    def test_admin_bypass_is_unaffected(self, reload_db, monkeypatch):
        """Admin/no-policy unchanged (§12)."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        from app.api import v2_scan

        admin_table = pa.table({"secret_col": ["admin-visible-row"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql, **kw: (admin_table, {}))
        monkeypatch.setattr(v2_scan, "_resolve_schema", lambda *a, **kw: {"secret_col": "STRING"})

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_policied_bq_table(conn, "bq_scan_policied_admin")
            admin = {"id": "admin1", "email": "admin1@test.com"}
            req = {"table_id": "bq_scan_policied_admin", "select": ["secret_col"], "limit": 10}
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, admin, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc_bytes)
        assert got.column("secret_col").to_pylist() == ["admin-visible-row"]

    def test_non_policied_bq_table_is_unaffected(self, reload_db, monkeypatch):
        """The inert case: a table with no access_policy_sql keeps the
        pre-existing (unfiltered) BQ-scan behavior exactly."""
        from app.api import v2_scan

        monkeypatch.setattr(
            v2_scan,
            "_resolve_schema",
            lambda *a, **kw: {"event_date": "DATE", "country_code": "STRING"},
        )
        fake_table = pa.table({"event_date": ["2026-04-27"], "country_code": ["CZ"]})
        monkeypatch.setattr(v2_scan, "_run_bq_scan", lambda bq, sql, **kw: (fake_table, {}))

        conn = reload_db.get_system_db()
        try:
            _seed(conn)  # registers "bq_view" with no access_policy_sql
            user = {"id": "admin1", "email": "a@x.com"}
            req = {"table_id": "bq_view", "select": ["event_date", "country_code"], "limit": 100}
            tracker = v2_scan._build_quota_tracker()
            ipc_bytes = v2_scan.run_scan(conn, user, req, bq=_bq(data="proj"), quota=tracker)
        finally:
            conn.close()
        got = parse_ipc_bytes(ipc_bytes)
        assert got.num_rows == 1
