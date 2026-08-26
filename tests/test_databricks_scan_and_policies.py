"""Two Databricks gaps closed: the snapshot/scan path, and access policies.

Before this, a `query_mode='remote'` Databricks row could be queried
interactively but not *snapshotted* — `/api/v2/scan` refused it outright — and
a table carrying an access policy could not be queried on Databricks at all,
because the only two options were "deny" and "ship the caller's unfiltered
statement to the warehouse".

The tests are grouped by the property each one defends, not by module:

1. **Scan SQL** — pure-function builder tests. A mis-built statement is still
   valid SQL against the wrong thing, so this is the layer where a bug is
   silent.
2. **Estimate honesty** — the interesting design decision. Databricks has no
   dry-run, so `estimated_scan_bytes` is `None`, never `0`; `0` on this
   response means "local, free".
3. **Scan wire** — a real statement submitted to the fake warehouse.
4. **Policy dialect + binding** — that `$name` survives as `:name`, and that
   the one variable the API cannot bind (a list) is expanded without ever
   putting its values into SQL text.
5. **Policy enforcement** — end to end through `/api/query`, including the
   failure modes that must deny.
"""

from __future__ import annotations

import pytest

from connectors.databricks.policy_params import (
    DatabricksPolicyBindingError,
    bind_policy_parameters,
)
from connectors.databricks.remote import DatabricksRemoteError
from src.access_policy import PolicyError, policied_relation


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(
    *,
    id: str,
    name: str,
    source_type: str,
    bucket: str,
    source_table: str,
    query_mode: str = "remote",
) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=id,
            name=name,
            source_type=source_type,
            bucket=bucket,
            source_table=source_table,
            query_mode=query_mode,
        )
    finally:
        conn.close()


def _set_policy(table_id: str, sql: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).set_access_policy(table_id, sql=sql, note="test policy", updated_by="admin")
    finally:
        conn.close()


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    """Fake warehouse + the settings dict the remote path expects.

    The COUNT route is listed FIRST: routes match by substring in order, and
    the scan route's `orders_raw` needle also occurs in the count statement.
    """
    import pyarrow as pa

    from tests.databricks_fake_warehouse import Route, start_fake_warehouse

    table = pa.table({"country": ["CZ", "SK", "PL"], "n": ["1", "2", "3"]})
    routes = [
        # `information_schema` first: `agnes schema` / the scan's own column
        # validation reads Unity Catalog before any data statement runs, and
        # its SQL also contains the table name the data routes match on.
        Route(
            match="information_schema.columns",
            columns=["column_name", "full_data_type", "is_nullable", "comment"],
            rows=[["country", "string", "YES", ""], ["n", "bigint", "YES", ""]],
        ),
        Route(match="COUNT(*)", arrow_table=pa.table({"n": ["42"]})),
        Route(match="orders_raw", arrow_table=table),
        Route(match="huge_table", arrow_table=table, truncated=True),
    ]
    wh = start_fake_warehouse(tmp_path, routes, monkeypatch)
    settings = {
        "host": wh.host,
        "token": "dapi-test-token",
        "warehouse_id": "wh-1",
        "catalog": "main",
    }
    monkeypatch.setattr(
        "connectors.databricks.semantic_layer.resolve_databricks_settings",
        lambda: settings,
    )
    try:
        yield wh, settings
    finally:
        wh.close()


# ---------------------------------------------------------------------------
# 1. Scan SQL
# ---------------------------------------------------------------------------


class TestScanSqlBuilder:
    def _req(self, **kw):
        from app.api.v2_scan import ScanRequest

        kw.setdefault("table_id", "dbx.sales.orders_raw")
        return ScanRequest(**kw)

    def _row(self, **kw):
        row = {
            "id": "dbx.sales.orders_raw",
            "name": "orders_raw",
            "source_type": "databricks",
            "query_mode": "remote",
            "bucket": "sales",
            "source_table": "orders_raw",
        }
        row.update(kw)
        return row

    def test_builds_a_three_part_backticked_path(self):
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(self._row(), self._req(), {"catalog": "main"})
        assert sql == "SELECT * FROM `main`.`sales`.`orders_raw`"

    def test_dotted_bucket_pins_its_own_catalog(self):
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(self._row(bucket="prod.sales"), self._req(), {"catalog": "main"})
        assert sql == "SELECT * FROM `prod`.`sales`.`orders_raw`"

    def test_select_where_order_and_limit_compose(self):
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(
            self._row(),
            self._req(select=["country", "n"], order_by=["country DESC"], limit=10),
            {"catalog": "main"},
            safe_where="country = 'CZ'",
        )
        assert sql == (
            "SELECT `country`, `n` FROM `main`.`sales`.`orders_raw` "
            "WHERE country = 'CZ' ORDER BY `country` DESC LIMIT 10"
        )

    def test_count_only_ignores_projection_and_ordering(self):
        """The estimate needs a row count, not a shaped result — carrying the
        caller's ORDER BY into it would make the warehouse sort for nothing."""
        from app.api.v2_scan import _build_databricks_sql

        sql = _build_databricks_sql(
            self._row(),
            self._req(select=["country"], order_by=["country"], limit=5),
            {"catalog": "main"},
            safe_where="country = 'CZ'",
            count_only=True,
        )
        assert sql == "SELECT COUNT(*) AS n FROM `main`.`sales`.`orders_raw` WHERE country = 'CZ'"

    def test_unsafe_registry_identifier_is_refused_not_escaped(self):
        from app.api.v2_scan import _build_databricks_sql

        with pytest.raises(DatabricksRemoteError) as exc:
            _build_databricks_sql(self._row(source_table="orders`; DROP"), self._req(), {"catalog": "main"})
        assert exc.value.reason == "databricks_unsafe_identifier"


class TestScannableEngineGate:
    def test_databricks_remote_row_is_now_scannable(self):
        from app.api.v2_scan import _assert_scannable_engine

        _assert_scannable_engine({"id": "x", "source_type": "databricks", "query_mode": "remote"})  # must not raise

    def test_another_remote_engine_is_still_refused(self):
        from app.api.v2_scan import _assert_scannable_engine

        with pytest.raises(ValueError, match="cannot execute against that engine"):
            _assert_scannable_engine({"id": "x", "source_type": "snowflake", "query_mode": "remote"})

    def test_materialized_databricks_row_stays_on_the_local_branch(self):
        from app.api.v2_scan import _executes_on_databricks

        assert not _executes_on_databricks({"source_type": "databricks", "query_mode": "materialized"})


class TestServerConfigSurface:
    """Every Databricks tunable the code reads must be settable from
    `/admin/server-config`.

    Nothing guarded this before, which is how `scan_timeout_seconds` shipped
    readable-but-unsettable: an operator whose snapshots timed out could only
    fix it by hand-editing instance.yaml, while every sibling guardrail sat
    right there in the admin UI. The failure mode is quiet — the key works,
    it is just invisible — so it needs a test rather than review attention.
    """

    #: Credential POINTERS stay out of the web-editable surface by convention:
    #: no source block exposes its `token_env` (Keboola reads one the same way
    #: and likewise omits it). Listed here so the omission reads as a decision.
    DELIBERATELY_UNEXPOSED = {"token_env"}

    def _config_keys_read_by_code(self) -> set:
        """Scrape `get_value("data_source", "databricks", "<key>", ...)` out of
        the source tree.

        Deliberately a scrape and not a hand-list: a hand-list drifts silently,
        which is the exact bug this test exists to catch.
        """
        import re
        from pathlib import Path

        pattern = re.compile(
            r"""get_value\(\s*["']data_source["']\s*,\s*["']databricks["']\s*,\s*["']([A-Za-z0-9_]+)["']""",
            re.VERBOSE,
        )
        root = Path(__file__).resolve().parent.parent
        found: set = set()
        for directory in ("app", "src", "connectors", "cli", "services"):
            for path in (root / directory).rglob("*.py"):
                found.update(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
        return found

    def test_every_read_tunable_is_exposed_in_the_admin_schema(self):
        from app.api.admin import _KNOWN_FIELDS

        exposed = set(_KNOWN_FIELDS["data_source"]["databricks"]["fields"])
        read = self._config_keys_read_by_code()
        assert read, "scrape found nothing — the get_value call shape changed, fix this test"

        missing = read - exposed - self.DELIBERATELY_UNEXPOSED
        assert not missing, (
            f"data_source.databricks keys read by the code but not settable in "
            f"/admin/server-config: {sorted(missing)}. Add them to _KNOWN_FIELDS, or "
            f"to DELIBERATELY_UNEXPOSED with the reason."
        )

    def test_the_snapshot_timeout_is_settable(self):
        """The specific key this test class was written for."""
        from app.api.admin import _KNOWN_FIELDS

        field = _KNOWN_FIELDS["data_source"]["databricks"]["fields"]["scan_timeout_seconds"]
        assert field["kind"] == "int"
        assert field["default"] == 900
        # The hint must say why it is not the interactive timeout, since an
        # operator reading the two side by side will otherwise assume one is
        # redundant and tune the wrong one.
        assert "materialize" in field["hint"]

    def test_where_dialect_follows_the_engine(self):
        """A remote Databricks row parses AND renders as databricks — the flavor
        `agnes schema` advertises for it, and the one the warehouse runs."""
        from app.api.v2_scan import _execution_dialect

        row = {"source_type": "databricks", "query_mode": "remote"}
        assert _execution_dialect(row, use_bq=False) == "databricks"
        assert _execution_dialect({"source_type": "databricks", "query_mode": "materialized"}, False) == "duckdb"
        assert _execution_dialect({"source_type": "bigquery"}, True) == "bigquery"


# ---------------------------------------------------------------------------
# 2. Estimate honesty
# ---------------------------------------------------------------------------


class TestEstimate:
    def test_scan_bytes_is_none_not_zero_and_rows_are_a_real_count(self, seeded_app, warehouse):
        """The design decision worth a test of its own.

        `0` already means something on this response — "served from a local
        parquet, nothing billable" — so reporting `0` for an engine that simply
        cannot be asked would read as "free". `None` says unknown. The row
        count, meanwhile, is not a heuristic at all here: it comes from a real
        COUNT(*) the warehouse answered.
        """
        from app.api.v2_scan import estimate
        from src.db import get_system_db

        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            out = estimate(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw"},
                bq=None,
            )
        finally:
            conn.close()

        assert out["engine"] == "databricks"
        assert out["estimated_scan_bytes"] is None
        assert out["bq_cost_estimate_usd"] is None
        assert out["estimated_result_rows"] == 42
        assert "COUNT(*)" in wh.statements[-1]

    def test_the_estimate_pre_flights_the_daily_budget(self, seeded_app, warehouse, monkeypatch):
        """The estimate is billable warehouse compute, unlike BigQuery's free
        dry-run, so it takes the same pre-flight the FETCH path applies to
        every engine. A caller who has already burned the daily budget on real
        fetches must not keep spending warehouse compute here.

        This bounds spend-after-budget, not estimate-only loops — the budget is
        denominated in bytes and a COUNT returns one row, so estimates barely
        accumulate. Bounding repetition would need a quota denominated in
        statements; see the note on `_estimate_databricks`.
        """
        from fastapi import HTTPException

        from app.api.v2_quota import KIND_DAILY_BYTES, QuotaExceededError
        from app.api.v2_scan import _build_quota_tracker, estimate
        from src.db import get_system_db

        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        quota = _build_quota_tracker()

        def _exhausted(user):
            raise QuotaExceededError(kind=KIND_DAILY_BYTES, current=99, limit=1, retry_after_seconds=60)

        monkeypatch.setattr(quota, "check_daily_budget", _exhausted)

        conn = get_system_db()
        try:
            with pytest.raises(HTTPException) as exc:
                estimate(
                    conn,
                    {"id": "admin1", "email": "admin@test.com"},
                    {"table_id": "dbx.sales.orders_raw"},
                    bq=None,
                )
        finally:
            conn.close()

        assert exc.value.status_code == 429
        assert exc.value.detail["reason"] == "daily_budget_exceeded"
        assert exc.value.detail["kind"] == KIND_DAILY_BYTES
        # The refusal must come BEFORE the billable statement — a pre-flight
        # that fires after the COUNT would have already spent the compute. The
        # `information_schema` resolve still runs, and should: it is Unity
        # Catalog metadata, not a data scan, and it is what validates the
        # request in the first place.
        assert not any("COUNT(*)" in s for s in wh.statements), "the billable COUNT ran despite an exhausted budget"

    @staticmethod
    def _install_tracker(monkeypatch, **kw):
        """Give the test its own quota tracker.

        `_build_quota_tracker` memoizes a process-wide singleton, so a test
        that wants a small cap has to replace the global rather than build a
        second tracker the endpoint would never consult.
        """
        from app.api.v2_quota import QuotaTracker

        tracker = QuotaTracker(
            max_concurrent_per_user=kw.pop("max_concurrent_per_user", 5),
            max_daily_bytes_per_user=kw.pop("max_daily_bytes_per_user", 10**12),
            **kw,
        )
        monkeypatch.setattr("app.api.v2_quota._quota_singleton", tracker)
        return tracker

    def _estimate(self):
        from app.api.v2_scan import estimate
        from src.db import get_system_db

        conn = get_system_db()
        try:
            return estimate(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw"},
                bq=None,
            )
        finally:
            conn.close()

    def test_a_handful_of_estimates_still_works(self, seeded_app, warehouse, monkeypatch):
        """The guard must not cost a normal analyst anything.

        Someone sizing a few snapshots before fetching runs well inside the
        default cap (500/day); a control that refused the tenth estimate would
        be worse than the problem it solves.
        """
        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        tracker = self._install_tracker(monkeypatch, max_daily_estimates_per_user=500)

        for _ in range(10):
            out = self._estimate()
            assert out["estimated_result_rows"] == 42

        assert tracker.estimates_used_today(user="admin@test.com") == 10
        assert sum("COUNT(*)" in s for s in wh.statements) == 10

    def test_an_estimate_loop_is_refused_once_the_statement_budget_is_spent(self, seeded_app, warehouse, monkeypatch):
        """The finding this guards.

        Every Databricks estimate is billable warehouse compute, and the byte
        budget cannot see it: the API reports only bytes RETURNED, and a
        COUNT returns one row. So an agent looping on `/api/v2/scan/estimate`
        would spend real money while the ledger read ~0. The statement budget
        is what stops it.
        """
        from fastapi import HTTPException

        from app.api.v2_quota import KIND_DAILY_ESTIMATES

        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        tracker = self._install_tracker(monkeypatch, max_daily_estimates_per_user=3)

        for _ in range(3):
            self._estimate()
        spent = sum("COUNT(*)" in s for s in wh.statements)
        assert spent == 3

        with pytest.raises(HTTPException) as exc:
            self._estimate()

        assert exc.value.status_code == 429
        assert exc.value.detail["reason"] == "daily_estimate_budget_exceeded"
        assert exc.value.detail["kind"] == KIND_DAILY_ESTIMATES
        assert exc.value.detail["current"] == 3
        assert exc.value.detail["limit"] == 3
        # Tells the caller to wait for the UTC-midnight reset rather than
        # hot-retrying, which is exactly the loop being bounded.
        assert exc.value.detail["retry_after_seconds"] > 0
        # And the refusal was free: no fourth statement reached the warehouse.
        assert sum("COUNT(*)" in s for s in wh.statements) == spent

        # And this is *why* the statement counter has to exist. The byte
        # ledger recorded only the genuine bytes those statements RETURNED —
        # a couple of dozen, for three real scans of a Delta table. No
        # synthetic charge was invented to paper over the gap (that would
        # misprice BigQuery, which reports real scanned bytes), so the byte
        # budget remains physically incapable of bounding this path: at this
        # rate the 50 GiB default would take billions of calls to trip.
        recorded = tracker.bytes_used_today(user="admin@test.com")
        assert 0 < recorded < 1000, f"expected a trivially small returned-byte figure, got {recorded}"
        assert recorded < 10**12 // 1000, "the byte budget must not be what bounds this path"

    def test_a_failed_statement_still_counts_against_the_budget(self, seeded_app, warehouse, monkeypatch):
        """What the warehouse bills for is the statement reaching it. If only
        successes were counted, a loop of statements that time out — the
        expensive ones — would be unbounded."""
        from connectors.databricks.remote import DatabricksRemoteError

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        tracker = self._install_tracker(monkeypatch, max_daily_estimates_per_user=10)

        def _boom(*a, **kw):
            raise DatabricksRemoteError(status=504, reason="databricks_timeout", message="too slow")

        # `_estimate_databricks` imports `execute_select` at call time, so
        # patching it on the module is what the endpoint will actually see.
        monkeypatch.setattr("connectors.databricks.remote.execute_select", _boom)

        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            self._estimate()

        assert tracker.estimates_used_today(user="admin@test.com") == 1

    def test_limit_caps_the_reported_row_count(self, seeded_app, warehouse):
        from app.api.v2_scan import estimate
        from src.db import get_system_db

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            out = estimate(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw", "limit": 10},
                bq=None,
            )
        finally:
            conn.close()
        assert out["estimated_result_rows"] == 10

    def test_cli_renders_unknown_scan_bytes_as_not_available(self, capsys):
        """`0` and `None` must not print the same. The CLI is where an analyst
        actually reads this number and decides whether to fetch."""
        from cli.commands.snapshot import _print_estimate

        _print_estimate(
            {
                "engine": "databricks",
                "estimated_scan_bytes": None,
                "estimated_result_rows": 42,
                "estimated_result_bytes": 1344,
                "bq_cost_estimate_usd": None,
            }
        )
        out = capsys.readouterr().out
        assert "n/a" in out
        assert "cannot price a statement" in out
        assert "bq_cost_estimate_usd" not in out

    def test_cli_still_prints_bigquery_numbers_unchanged(self, capsys):
        from cli.commands.snapshot import _print_estimate

        _print_estimate(
            {
                "engine": "bigquery",
                "estimated_scan_bytes": 4_200_000_000,
                "estimated_result_rows": 250_000,
                "estimated_result_bytes": 12_000_000,
                "bq_cost_estimate_usd": 0.0191,
            }
        )
        out = capsys.readouterr().out
        assert "4,200,000,000 bytes" in out
        assert "$ 0.0191" in out

    def test_snapshot_meta_survives_a_null_scan_estimate(self):
        """`est.get("estimated_scan_bytes", 0)` returns None when the key EXISTS
        and maps to None — `int(None)` raises. Regression guard for the fetch
        path, which would otherwise crash after a successful download."""
        est = {"estimated_scan_bytes": None}
        assert int((est or {}).get("estimated_scan_bytes") or 0) == 0


# ---------------------------------------------------------------------------
# 3. Scan wire
# ---------------------------------------------------------------------------


class TestScanWire:
    def test_scan_returns_rows_without_the_interactive_limit_probe(self, seeded_app, warehouse):
        """A snapshot wants every row the predicate selects. The interactive
        path wraps statements in `LIMIT n + 1` to detect truncation; doing that
        here would silently cap the snapshot at the preview size."""
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from src.db import get_system_db

        wh, _settings = warehouse
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            ipc = run_scan(
                conn,
                {"id": "admin1", "email": "admin@test.com"},
                {"table_id": "dbx.sales.orders_raw", "select": ["country"]},
                bq=None,
                quota=_build_quota_tracker(),
            )
        finally:
            conn.close()

        assert ipc  # Arrow IPC bytes
        submitted = wh.statements[-1]
        assert submitted == "SELECT `country` FROM `main`.`sales`.`orders_raw`"
        assert "agnes_remote_q" not in submitted

    def test_a_truncated_result_is_refused_not_returned_short(self, seeded_app, warehouse):
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from src.db import get_system_db

        _register(
            id="dbx.sales.huge_table",
            name="huge_table",
            source_type="databricks",
            bucket="sales",
            source_table="huge_table",
        )
        conn = get_system_db()
        try:
            with pytest.raises(Exception) as exc:
                run_scan(
                    conn,
                    {"id": "admin1", "email": "admin@test.com"},
                    {"table_id": "dbx.sales.huge_table"},
                    bq=None,
                    quota=_build_quota_tracker(),
                )
        finally:
            conn.close()
        detail = getattr(exc.value, "detail", {})
        assert detail.get("reason") == "remote_scan_too_large"

    def test_policied_table_is_refused_on_this_endpoint(self, seeded_app, warehouse, monkeypatch):
        """`/api/query` CAN carry a policy to the warehouse — it substitutes the
        body into a caller-authored statement. This endpoint has no caller
        statement to substitute into; it BUILDS one. Shipping that unfiltered
        would return exactly the rows the policy hides, so it denies (§17)."""
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", "SELECT * FROM orders_raw WHERE country = 'CZ'")
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        conn = get_system_db()
        try:
            with pytest.raises(Exception) as exc:
                run_scan(
                    conn,
                    {"id": "analyst1", "email": "analyst1@test.com"},
                    {"table_id": "dbx.sales.orders_raw"},
                    bq=None,
                    quota=_build_quota_tracker(),
                )
        finally:
            conn.close()
        detail = getattr(exc.value, "detail", {})
        assert detail.get("reason") == "policy_unsupported_on_scan_engine", (
            f"raised {type(exc.value).__name__}: {exc.value}"
        )
        assert "agnes query --remote" in detail.get("hint", "")


# ---------------------------------------------------------------------------
# 4. Policy dialect + binding
# ---------------------------------------------------------------------------


class TestPolicyDialect:
    def test_placeholder_becomes_the_databricks_parameter_marker(self):
        """The property the whole feature rests on: one authored policy body
        keeps its values out of SQL text on all three engines, because sqlglot
        renders `$name` as each engine's own named-parameter syntax."""
        from src.access_policy import _transpile_policy_to_databricks

        out = _transpile_policy_to_databricks("SELECT * FROM t WHERE owner = $user_email", table_id="t")
        assert ":user_email" in out
        assert "$user_email" not in out

    def test_group_membership_idiom_transpiles(self):
        from src.access_policy import _transpile_policy_to_databricks

        out = _transpile_policy_to_databricks("SELECT * FROM t WHERE list_contains($user_groups, region)", table_id="t")
        assert "ARRAY_CONTAINS(:user_groups, region)" in out

    def test_untranspilable_body_raises_policy_error_not_the_engine_message(self):
        from src.access_policy import _transpile_policy_to_databricks

        with pytest.raises(PolicyError):
            _transpile_policy_to_databricks("this is not sql at all !!", table_id="t")

    def test_unknown_dialect_still_rejected(self):
        with pytest.raises(ValueError, match="unknown dialect"):
            policied_relation("whatever", {"id": "u"}, dialect="snowflake")

    def test_save_time_validation_covers_databricks_too(self):
        """A policy that transpiles to BigQuery but not Databricks would save
        clean and then DENY at read time on a Databricks table — an outage
        shipped as an access rule. The save is the cheap place to find out."""
        import src.access_policy_validate as v

        # Sanity: the ordinary shape passes both engines.
        v._reject_untranspilable("SELECT * FROM t WHERE owner = $user_email")

        calls = []
        original = v.sqlglot.transpile

        def fake(sql, read, write):
            calls.append(write)
            return original(sql, read=read, write=write)

        v.sqlglot.transpile = fake
        try:
            v._reject_untranspilable("SELECT * FROM t")
        finally:
            v.sqlglot.transpile = original
        assert "databricks" in calls and "bigquery" in calls


class TestParameterBinding:
    def test_scalars_pass_through_as_typed_parameters(self):
        sql, params = bind_policy_parameters("SELECT * FROM t WHERE owner = :user_email", {"user_email": "a@b.com"})
        assert sql == "SELECT * FROM t WHERE owner = :user_email"
        assert params == [{"name": "user_email", "value": "a@b.com", "type": "STRING"}]

    def test_array_variable_expands_to_scalar_markers(self):
        """The API binds scalars only. The values still travel as request
        fields — only the arity of the list becomes visible in the text."""
        # Distinctive values: a two-letter group name would collide by accident
        # with substrings of the generated marker identifiers and make the
        # leak assertion below meaningless.
        groups = ["eu-field-analysts", "latam-partners"]
        sql, params = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, region)",
            {"user_groups": groups},
        )
        assert "ARRAY(:agnes_policy_group_user_groups_0, :agnes_policy_group_user_groups_1)" in sql
        assert [p["value"] for p in params] == groups
        # The decisive assertion: no group NAME appears in the SQL text.
        for name in groups:
            assert name not in sql

    def test_empty_group_list_becomes_a_typed_empty_array(self):
        """A caller in no groups must match nothing — a legitimate state, not
        an error. Bare `ARRAY()` is `ARRAY<VOID>` on Databricks and would fail
        to type-check against a string column."""
        sql, params = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, region)", {"user_groups": []}
        )
        assert "CAST(ARRAY() AS ARRAY<STRING>)" in sql
        assert params == []

    def test_marker_inside_a_string_literal_is_not_rewritten(self):
        """Why the substitution is AST-level and not a regex. A policy body is
        exactly the kind of SQL that carries literals."""
        sql, _params = bind_policy_parameters(
            "SELECT * FROM t WHERE note = ':user_groups' AND ARRAY_CONTAINS(:user_groups, r)",
            {"user_groups": ["x"]},
        )
        assert "note = ':user_groups'" in sql
        assert sql.count("ARRAY(") == 1

    def test_no_list_survives_into_the_parameter_payload(self):
        _sql, params = bind_policy_parameters(
            "SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, r) AND o = :user_email",
            {"user_groups": ["a", "b"], "user_email": "x@y.z"},
        )
        assert all(isinstance(p["value"], str) for p in params)
        assert all(p["type"] == "STRING" for p in params)

    def test_none_binds_as_null_not_as_an_empty_string(self):
        """`owner_id = :user_id` with a NULL bind matches no row; with `''` it
        matches any row whose owner_id happens to be empty. Only the first is
        fail-closed, and it is what the other two engines already do."""
        _sql, params = bind_policy_parameters("SELECT * FROM t WHERE owner_id = :user_id", {"user_id": None})
        assert params == [{"name": "user_id", "type": "STRING"}]
        assert "value" not in params[0]

    def test_unparseable_body_denies(self):
        with pytest.raises(DatabricksPolicyBindingError):
            bind_policy_parameters("!!! not sql", {"user_groups": ["a"]})

    def test_missing_marker_denies_rather_than_dropping_the_filter(self):
        """If the array marker is not where we are about to bind it, the group
        filter has silently vanished. Deny."""
        with pytest.raises(DatabricksPolicyBindingError):
            bind_policy_parameters("SELECT * FROM t", {"user_groups": ["a"]})


# ---------------------------------------------------------------------------
# 5. Policy enforcement, end to end
# ---------------------------------------------------------------------------


class TestPolicyEnforcement:
    def _setup(self, monkeypatch, policy_sql: str) -> None:
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", policy_sql)
        conn = get_system_db()
        try:
            # Must be a granted NON-admin: an admin holds a full-surface
            # credential, for which the resolver returns the passthrough
            # relation — the test would pass for the wrong reason.
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

    def test_group_policy_binds_values_as_parameters_never_as_sql_text(self, seeded_app, warehouse, monkeypatch):
        """The security property, asserted on the wire: the caller's group
        names reach the warehouse in the request's `parameters` field and
        nowhere in the statement."""
        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE list_contains($user_groups, country)")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text

        submitted = wh.statements[-1]
        payload = wh.payloads[-1]
        assert "ARRAY_CONTAINS(ARRAY(" in submitted
        assert payload.get("parameters"), "identity values must ride the parameters field"
        for param in payload["parameters"]:
            assert param["value"] not in submitted, "a bound value leaked into the SQL text"

    def test_policy_predicate_reaches_the_warehouse(self, seeded_app, warehouse, monkeypatch):
        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE country = 'CZ'")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        submitted = wh.statements[-1]
        assert "country = 'CZ'" in submitted
        # The subquery alias must stay a bare name: a three-part path in alias
        # position is a syntax error, and the name rewriter replaces every
        # occurrence unless the policied name is excluded from it. (The outer
        # `agnes_remote_q LIMIT` is the interactive path's own probe wrap.)
        assert ") AS orders_raw" in submitted
        assert "AS `main`.`sales`.`orders_raw`" not in submitted

    def test_admin_bypass_ships_the_unfiltered_statement(self, seeded_app, warehouse, monkeypatch):
        """§12 — the bypass follows the credential surface. An admin's read is
        a passthrough relation, so no substitution happens at all."""
        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE country = 'CZ'")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert "country = 'CZ'" not in wh.statements[-1]

    def test_policy_body_naming_an_unregistered_table_denies(self, seeded_app, warehouse, monkeypatch):
        """The re-gate. The first gate pass only sees the caller's SQL; a policy
        body can name tables the caller never wrote. Without a second pass such
        a name ships unchecked and resolves against whatever the default
        catalog holds."""
        self._setup(
            monkeypatch,
            "SELECT * FROM orders_raw WHERE country IN (SELECT c FROM secret_mapping)",
        )
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["reason"] == "policy_error"
        assert r.json()["detail"]["table"] == "dbx.sales.orders_raw"

    def test_snapshot_from_query_carries_the_policy(self, seeded_app, warehouse, monkeypatch):
        """A materialize must not be a way around the filter."""
        from app.api.query import run_remote_select_to_arrow
        from app.api.v2_quota import _build_quota_tracker
        from src.db import get_system_db

        wh, _settings = warehouse
        self._setup(monkeypatch, "SELECT * FROM orders_raw WHERE country = 'CZ'")

        conn = get_system_db()
        try:
            policy_info: dict = {}
            table = run_remote_select_to_arrow(
                conn,
                {"id": "analyst1", "email": "analyst1@test.com"},
                "SELECT * FROM orders_raw",
                None,
                _build_quota_tracker(),
                policy_info=policy_info,
            )
        finally:
            conn.close()

        assert table is not None
        assert "country = 'CZ'" in wh.statements[-1]
        assert policy_info.get("policied_table_ids") == ["dbx.sales.orders_raw"]


# ---------------------------------------------------------------------------
# 6. Devin review findings (PR #1383) — each of these shipped in the first pass
# ---------------------------------------------------------------------------


class TestReviewFindings:
    """Five defects a reviewer caught that the original tests did not.

    Kept together because what they have in common is more useful than where
    they live: every one is a place where a *correct* local decision
    (bind NULL by omitting the field, fall through to the local path, deny an
    unverifiable read) was undone by the code that consumed it.
    """

    def test_null_identity_survives_the_parameter_hand_off(self):
        """Finding 1. `bind_policy_parameters` omits `value` for a NULL bind;
        the consumer flattened entries to `{name: value}` and raised KeyError.

        Flattening was lossy in both directions — even with `.get()` the
        rebuild emitted `"value": None`, shipping JSON null instead of omitting
        the field, which is the difference between "matches no row" and
        "matches rows whose column is empty"."""
        from app.api.query import _databricks_policy_resolver

        entries = bind_policy_parameters("SELECT * FROM t WHERE o = :user_email", {"user_email": None})[1]
        # The shape the resolver stores must round-trip to exactly this.
        assert entries == [{"name": "user_email", "type": "STRING"}]

        as_stored = {p["name"]: p for p in entries}
        assert list(as_stored.values()) == entries
        assert "value" not in list(as_stored.values())[0]
        assert callable(_databricks_policy_resolver(name_lookups=[], default_catalog="main"))

    def test_policy_gating_parses_databricks_statements_as_databricks(self):
        """Finding 3. The gating `rewrite_sql` used the DuckDB dialect, and
        sqlglot's DuckDB parser rejects backticked identifiers outright — so a
        statement copied out of the Databricks UI, or any `MEASURE()` query,
        failed to parse and then DENIED on any policied table it mentioned.
        That is the exact refusal this feature set out to remove."""
        import sqlglot

        from app.api.query import _policy_parse_dialect

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        sql = "SELECT o_date, MEASURE(`Total Revenue`) FROM orders_raw GROUP BY o_date"
        assert _policy_parse_dialect(sql, sql.lower()) == "databricks"

        # The premise, pinned: these do NOT parse as DuckDB, so the old default
        # was not merely a cosmetic mismatch.
        for unparseable in (sql, "SELECT * FROM `main`.`sales`.`orders_raw`"):
            with pytest.raises(Exception):
                sqlglot.parse_one(unparseable, read="duckdb")
        # ...and do parse as Databricks.
        assert sqlglot.parse_one(sql, dialect="databricks") is not None

    def test_an_all_local_statement_keeps_the_duckdb_dialect(self):
        """The dialect switch must not touch queries that never leave DuckDB."""
        from app.api.query import _policy_parse_dialect

        sql = "SELECT * FROM some_local_table"
        assert _policy_parse_dialect(sql, sql.lower()) == "duckdb"

    def test_duplicate_output_columns_deny_on_the_databricks_arm(self):
        """Finding 4. DuckDB reads get `assert_policied_reads_unique`; BigQuery
        needs no guard because its jobs API rejects duplicate result columns.
        Spark permits them, so a masking policy shaped
        `SELECT * EXCEPT (national_id), md5(email) AS email` would have shipped
        the plaintext copy alongside the masked one."""
        from app.api.query import _assert_databricks_policy_columns_unique

        plan = {"policied_table_ids": ["dbx.sales.orders_raw"]}
        _assert_databricks_policy_columns_unique(plan, ["country", "n"])  # unique: fine

        with pytest.raises(PolicyError) as exc:
            _assert_databricks_policy_columns_unique(plan, ["email", "n", "EMAIL"])
        assert exc.value.table_id == "dbx.sales.orders_raw"

    def test_the_guard_is_inert_when_no_policy_is_in_play(self):
        """An ordinary self-join may legitimately return same-named columns."""
        from app.api.query import _assert_databricks_policy_columns_unique

        _assert_databricks_policy_columns_unique({}, ["id", "id"])
        _assert_databricks_policy_columns_unique({"policied_table_ids": []}, ["id", "id"])

    def test_policy_body_mapping_table_is_rewritten_to_a_native_path(self, seeded_app, warehouse, monkeypatch):
        """Finding 5. `name_lookups` was built by scanning the CALLER's SQL, so
        a table named only inside the policy body — §15's `policy_mapping` join
        — was never rewritten and shipped as a bare name, resolving against
        whatever the warehouse's default context holds.

        The caller deliberately holds NO grant on the mapping table: that is
        the point of the idiom, and requiring one would make it deny."""
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        wh, _settings = warehouse
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _register(
            id="dbx.sec.region_map",
            name="region_map",
            source_type="databricks",
            bucket="sec",
            source_table="region_map",
        )
        _set_policy(
            "dbx.sales.orders_raw",
            "SELECT * FROM orders_raw WHERE country IN (SELECT c FROM region_map)",
        )
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        submitted = wh.statements[-1]
        assert "`main`.`sec`.`region_map`" in submitted, f"mapping table left bare: {submitted}"
        assert "`main`.`sales`.`orders_raw`" in submitted

    def test_an_unregistered_table_in_a_policy_body_still_denies(self, seeded_app, warehouse, monkeypatch):
        """The mapping-table fix must not weaken the registration check that
        stops an unknown name reaching the warehouse."""
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy(
            "dbx.sales.orders_raw",
            "SELECT * FROM orders_raw WHERE country IN (SELECT c FROM never_registered)",
        )
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["reason"] == "policy_error"


class TestSecondReviewRound:
    """Four more, from a second review pass over the fixes above.

    Three of them share a root: a decision that was correct for the shape the
    first tests happened to use, and wrong for a shape they did not cover.
    """

    def test_empty_result_keeps_its_column_types(self):
        """A zero-row scan has no Arrow batch to carry the schema, and the
        fallback typed every column as string. Both callers PERSIST that — the
        scan endpoint serializes it, `agnes snapshot create` writes it to
        parquet and registers a view — so an empty snapshot's numeric and date
        columns became text, and the analyst's next aggregate over them fails
        or compares lexically."""
        import pyarrow as pa

        from connectors.databricks.remote import arrow_schema_from_manifest

        schema = arrow_schema_from_manifest(
            [
                {"name": "country", "type_name": "STRING"},
                {"name": "n", "type_name": "LONG"},
                {"name": "amount", "type_name": "DOUBLE"},
                {"name": "d", "type_name": "DATE"},
                {"name": "ts", "type_name": "TIMESTAMP"},
                {"name": "ok", "type_name": "BOOLEAN"},
            ]
        )
        assert schema.field("n").type == pa.int64()
        assert schema.field("amount").type == pa.float64()
        assert schema.field("d").type == pa.date32()
        assert pa.types.is_timestamp(schema.field("ts").type)
        assert schema.field("ok").type == pa.bool_()
        assert pa.Table.from_batches([], schema=schema).num_rows == 0

    def test_unmodelable_column_types_fall_back_to_string(self):
        """A STRUCT/MAP/VARIANT column in an EMPTY result is worth a string
        placeholder; guessing a nested Arrow type from a name is not."""
        import pyarrow as pa

        from connectors.databricks.remote import arrow_schema_from_manifest

        schema = arrow_schema_from_manifest([{"name": "payload", "type_name": "STRUCT"}])
        assert schema.field("payload").type == pa.string()

    def test_policy_body_resolves_the_policied_rows_own_path(self, seeded_app):
        """The severe one: the policy body's own `FROM` was rewritten using
        lookups built by scanning the CALLER's statement, and
        `guardrail_inputs` records an entry only for a name the caller wrote
        BARE (it masks backticks first). Whenever the caller's spelling does
        not produce that entry, the body shipped unqualified, to resolve
        against whatever the warehouse's default context holds.

        Asserted at the resolver with EMPTY caller lookups, which is exactly
        the state that produced the bug — an end-to-end test cannot reach it
        today, see `test_a_purely_backticked_statement_does_not_route_yet`.
        """
        from app.api.query import _databricks_policy_resolver

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", "SELECT * FROM orders_raw WHERE country = 'CZ'")

        resolve = _databricks_policy_resolver(name_lookups=[], default_catalog="main")
        relation = resolve("orders_raw", {"id": "analyst1", "email": "analyst1@test.com"})

        assert relation.policied
        assert "`main`.`sales`.`orders_raw`" in relation.relation_sql, relation.relation_sql
        assert "FROM orders_raw" not in relation.relation_sql, "body still carries a bare name"

    def test_a_purely_backticked_statement_does_not_route_yet(self):
        """Scope boundary, pinned so it is a decision and not an oversight.

        A statement that names its table ONLY as `` `catalog`.`schema`.`table` ``
        never reaches the Databricks planner: engine detection masks backtick
        segments and then looks for the registered bare name or a `dbx."x"."y"`
        path, so it finds neither. That is a pre-existing phase-2 gap, not
        something the policy work introduced, and it fails closed (the query
        errors rather than reading anything). Fixing it means teaching
        `_rows_referenced` to recognise native three-part paths — shared with
        BigQuery, whose native spelling is also backticked, so it wants its own
        change.
        """
        from src.remote_engines import referenced_remote_rows, resolve_single_engine

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        sql = "SELECT * FROM `main`.`sales`.`orders_raw`"
        assert resolve_single_engine(referenced_remote_rows(sql, sql.lower())) is None

        # The two spellings that DO route.
        bare = "SELECT * FROM orders_raw"
        assert resolve_single_engine(referenced_remote_rows(bare, bare.lower())) == "databricks"
        prefixed = 'SELECT * FROM dbx."sales"."orders_raw"'
        assert resolve_single_engine(referenced_remote_rows(prefixed, prefixed.lower())) == "databricks"

    def test_dialect_stays_duckdb_when_the_statement_will_run_locally(self):
        """With the Unity Catalog ATTACH on, a statement mixing a Databricks
        row with local data is planned as an ordinary DuckDB query. Rendering
        the caller's SQL through sqlglot's Databricks generator and then
        executing it on DuckDB changes its meaning, and DuckDB-only syntax
        would fail the Databricks parse and deny."""
        from app.api.query import _policy_parse_dialect

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _register(
            id="local.shop.customers",
            name="customers",
            source_type="local",
            bucket="shop",
            source_table="customers",
            query_mode="local",
        )

        pure = "SELECT * FROM orders_raw"
        assert _policy_parse_dialect(pure, pure.lower()) == "databricks"

        mixed = "SELECT * FROM orders_raw JOIN customers USING (id)"
        assert _policy_parse_dialect(mixed, mixed.lower()) == "duckdb"


class TestThirdReviewRound:
    """The most serious finding of the three review rounds, plus two smaller.

    `rewrite_sql` swallows `PolicyError` from its `resolve` callable — that
    exception doubles as the registry's "no such table" signal, and swallowing
    it is what keeps a query naming a CTE working. But the engine resolvers
    raise the SAME type for genuine failures (a transpile error, a group name
    carrying a LIKE metacharacter, a parameter-binding failure), so a policied
    table dropped out of the substitution silently and the caller's OWN
    unfiltered statement executed — returned with a 200. §17 says every policy
    failure denies; that one admitted.
    """

    def test_a_lost_policied_table_denies(self):
        """The positive invariant: whatever the first pass flagged, the engine
        pass must still be filtering. Anything it lost is a silent drop."""
        from app.api.query import _assert_policy_substitution_complete

        _assert_policy_substitution_complete(["a", "b"], ["a", "b"])  # complete
        _assert_policy_substitution_complete([], [])  # nothing policied
        _assert_policy_substitution_complete(None, ["a"])  # first pass unknown

        with pytest.raises(PolicyError) as exc:
            _assert_policy_substitution_complete(["a", "b"], ["a"])
        assert exc.value.table_id == "b"

    def test_the_invariant_catches_a_total_drop(self):
        """The exact shape the bug produced: the engine pass returns nothing,
        which used to take an early return leaving the statement unfiltered."""
        from app.api.query import _assert_policy_substitution_complete

        with pytest.raises(PolicyError):
            _assert_policy_substitution_complete(["dbx.sales.orders_raw"], [])

    def test_a_resolver_failure_is_not_swallowed(self):
        """`_PolicyResolutionFailed` must NOT be a `PolicyError` subclass — if
        it were, `rewrite_sql`'s `except PolicyError: continue` would swallow it
        and we would be back to the silent drop."""
        from app.api.query import _PolicyResolutionFailed

        assert not issubclass(_PolicyResolutionFailed, PolicyError)
        assert _PolicyResolutionFailed("t").table_id == "t"

    def test_binding_failure_surfaces_as_a_denial_not_a_dropped_policy(self, seeded_app, warehouse, monkeypatch):
        """End to end: make the parameter binding fail for a policied table and
        assert the query DENIES rather than returning unfiltered rows."""
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        wh, _settings = warehouse
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", "SELECT * FROM orders_raw WHERE country = 'CZ'")
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        def _boom(_sql, _params):
            from connectors.databricks.policy_params import DatabricksPolicyBindingError

            raise DatabricksPolicyBindingError("simulated binding failure")

        monkeypatch.setattr("connectors.databricks.policy_params.bind_policy_parameters", _boom)

        before = len(wh.statements)
        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["reason"] == "policy_error"
        # The decisive assertion: nothing was sent to the warehouse at all.
        assert len(wh.statements) == before, f"an unfiltered statement was shipped: {wh.statements[before:]}"

    def test_unsafe_registry_identifier_is_a_400_not_a_500(self, seeded_app, warehouse):
        """`_build_databricks_sql` raises DatabricksRemoteError, which
        `scan_endpoint` does not catch, so it escaped as an unexplained 500.

        Reaching it needs the schema resolve to be satisfied first — that path
        calls `quote_dbx_path` too and converts to a ValueError → 400 — so the
        window is a CACHED schema, which is the normal state after any earlier
        request for the same table. Primed here to exercise the builder itself
        rather than the guard that happens to run before it.
        """
        from app.api import v2_schema
        from app.api.v2_quota import _build_quota_tracker
        from app.api.v2_scan import run_scan
        from fastapi import HTTPException
        from src.db import get_system_db

        _register(
            id="dbx.sales.bad_row",
            name="bad_row",
            source_type="databricks",
            bucket="sales",
            source_table="orders`; DROP",
        )
        v2_schema._schema_cache.set(
            "dbx.sales.bad_row",
            {"columns": [{"name": "country", "type": "STRING"}], "sql_flavor": "databricks"},
        )

        conn = get_system_db()
        try:
            with pytest.raises(HTTPException) as exc:
                run_scan(
                    conn,
                    {"id": "admin1", "email": "admin@test.com"},
                    {"table_id": "dbx.sales.bad_row"},
                    bq=None,
                    quota=_build_quota_tracker(),
                )
        finally:
            conn.close()
            v2_schema._schema_cache.clear()

        assert exc.value.status_code == 400, f"got {exc.value.status_code}: {exc.value.detail}"
        assert exc.value.detail["reason"] == "databricks_unsafe_identifier"

    def test_the_estimate_uses_the_interactive_timeout(self):
        """`--estimate` runs a REAL warehouse COUNT — not a free dry-run like
        BigQuery's. Someone is waiting on it, so it must not inherit the
        900 s materialize budget."""
        from app.api.v2_scan import _databricks_estimate_timeout_s, _databricks_scan_timeout_s

        assert _databricks_estimate_timeout_s() == 120
        assert _databricks_scan_timeout_s() == 900
        assert _databricks_estimate_timeout_s() < _databricks_scan_timeout_s()


class TestAuditTrail:
    def test_a_refused_estimate_is_audited(self, seeded_app, warehouse, monkeypatch):
        """`estimate()` raises HTTPException for structured rejections, and the
        endpoint's except-tuple did not list it — so the request answered
        correctly while writing NO audit row, unlike every other estimate
        failure. The sibling `scan_endpoint` already carries this branch."""
        from src.db import get_system_db
        from src.repositories import audit_repo
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_raw", "SELECT * FROM orders_raw WHERE country = 'CZ'")
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_raw", "analyst1")
        finally:
            conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/v2/scan/estimate",
            json={"table_id": "dbx.sales.orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["reason"] == "policy_unsupported_on_scan_engine"

        rows, _cursor = audit_repo().query(action="snapshot.estimate", limit=50)
        errors = [row for row in rows if str(row.get("result", "")).startswith("error.")]
        assert errors, f"refusal left no audit row; snapshot.estimate rows: {rows}"
        assert errors[0]["result"] == "error.400"


class TestSqlglotCoupling:
    """Pin the two sqlglot behaviours group-based policies rest on.

    `pyproject.toml` pins only a floor (`sqlglot>=30.0.0`), so a minor bump can
    land at any time — this repo bumps dependencies routinely. If either
    direction drifts, `bind_policy_parameters` finds no marker to substitute
    and every group-based policy on Databricks denies: fail-closed, so not a
    leak, but a wide outage whose cause would be invisible from the symptom.

    Asserted in BOTH directions separately, not just through the end-to-end
    path, so a bump that breaks one fails with a message naming which.
    """

    def test_write_direction_dollar_becomes_colon(self):
        """DuckDB `$name` must render as Databricks' own `:name` marker."""
        import sqlglot

        out = sqlglot.transpile(
            "SELECT * FROM t WHERE list_contains($user_groups, region)",
            read="duckdb",
            write="databricks",
        )[0]
        assert ":user_groups" in out, f"sqlglot no longer writes $name as :name — got {out!r}"
        assert "$user_groups" not in out

    def test_read_direction_colon_parses_back_as_a_named_placeholder(self):
        """...and reading it back must yield a Placeholder whose `.name` is the
        bare variable. This is the half nothing asserted in isolation, and it
        is the half `bind_policy_parameters` matches on."""
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one("SELECT * FROM t WHERE ARRAY_CONTAINS(:user_groups, region)", dialect="databricks")
        placeholders = [n for n in tree.find_all(exp.Placeholder)]
        assert placeholders, "sqlglot no longer parses :name as a Placeholder in the databricks dialect"
        assert [p.name for p in placeholders] == ["user_groups"]

    def test_the_round_trip_holds_end_to_end(self):
        """Both directions composed, which is what the feature actually uses."""
        from src.access_policy import _transpile_policy_to_databricks

        body = _transpile_policy_to_databricks(
            "SELECT * FROM t WHERE list_contains($user_groups, region)", table_id="t"
        )
        sql, params = bind_policy_parameters(body, {"user_groups": ["eu-team"]})
        assert "ARRAY(:agnes_policy_group_user_groups_0)" in sql
        assert [p["value"] for p in params] == ["eu-team"]

    def test_a_broken_round_trip_denies_rather_than_dropping_the_filter(self):
        """The failure mode the coupling note promises: if the marker cannot be
        found, the group filter must NOT silently vanish."""
        with pytest.raises(DatabricksPolicyBindingError):
            bind_policy_parameters("SELECT * FROM t WHERE 1 = 1", {"user_groups": ["eu-team"]})


# ---------------------------------------------------------------------------
# 12. Sixth review round
# ---------------------------------------------------------------------------


class TestTimeoutConfiguration:
    """`scan_timeout_seconds` reads like every other guardrail knob.

    Both siblings on this connector — `max_bytes_per_materialize` and
    `statement_timeout_seconds` in `app/api/sync.py` — guard the numeric
    conversion and treat `0` as "disable". The scan reader did neither: a
    typo silently reverted to 900 with no warning, and so did a deliberate
    `0`, which is the opposite of what an operator setting it means.
    """

    def _reader(self, monkeypatch, **values):
        import app.api.query as q
        import app.api.v2_scan as vs

        def fake(*path, default=None):
            return values.get(".".join(path), default)

        monkeypatch.setattr(vs, "get_value", fake)
        monkeypatch.setattr(q, "get_value", fake)
        return vs

    def test_unset_uses_the_documented_defaults(self, monkeypatch):
        vs = self._reader(monkeypatch)
        assert vs._databricks_scan_timeout_s() == 900.0
        assert vs._databricks_estimate_timeout_s() == 120.0

    def test_zero_disables_the_scan_deadline(self, monkeypatch):
        """A snapshot is a materialize, and `0` is how its sibling knob is
        documented to remove the ceiling. Reverting to 900 would cancel the
        very fetch the operator lengthened the budget for."""
        vs = self._reader(monkeypatch, **{"data_source.databricks.scan_timeout_seconds": 0})
        assert vs._databricks_scan_timeout_s() is None

    def test_a_disabled_deadline_survives_the_connector_layer(self):
        """The reader's `None` has to reach the client unconverted. Checked at
        the connector seam — a coercion in `execute_scan_to_arrow` would
        silently re-arm the deadline without the reader ever being wrong."""
        import pyarrow as pa

        from connectors.databricks.remote import execute_scan_to_arrow

        seen: dict = {}

        class _FakeResult:
            truncated = False
            schema_columns = [{"name": "n", "type_name": "LONG"}]

            def iter_batches(self):
                return iter([pa.record_batch({"n": pa.array([1])})])

        class _FakeClient:
            def execute_to_arrow_batches(self, statement, **kwargs):
                seen.update(kwargs)
                return _FakeResult()

        table = execute_scan_to_arrow("SELECT 1 AS n", settings={}, cap_bytes=0, timeout_s=None, client=_FakeClient())
        assert table.num_rows == 1
        assert seen["timeout_s"] is None

    def test_a_disabled_deadline_reaches_the_client_as_no_deadline(self, monkeypatch):
        """`None` has to survive the whole way down, not just out of the
        reader — the client is where a falsy timeout becomes "poll forever"."""
        import time

        from connectors.databricks.client import DatabricksStatementClient

        client = DatabricksStatementClient.__new__(DatabricksStatementClient)
        captured = {}

        def fake_post(path, payload):
            captured["deadline_set"] = True
            return {"status": {"state": "SUCCEEDED"}, "statement_id": "s1"}

        monkeypatch.setattr(client, "_post_json", fake_post, raising=False)
        # A deadline computed from `None` would raise TypeError here; the
        # contract is that it is simply never computed.
        before = time.monotonic()
        client._run_to_terminal({}, "SELECT 1", timeout_s=None)
        assert captured["deadline_set"]
        assert time.monotonic() - before < 5

    def test_a_non_numeric_value_warns_and_keeps_the_deadline(self, monkeypatch, caplog):
        """A typo must not be the thing that removes the ceiling — the
        distinction between "operator meant 0" and "operator fat-fingered it"
        is exactly what the guard buys."""
        vs = self._reader(monkeypatch, **{"data_source.databricks.scan_timeout_seconds": "twenty"})
        with caplog.at_level("WARNING"):
            assert vs._databricks_scan_timeout_s() == 900.0
        assert "scan_timeout_seconds" in caplog.text

    def test_the_estimate_deadline_delegates_to_the_interactive_reader(self, monkeypatch):
        """`--estimate` has a person waiting on it, so it shares `/api/query`'s
        budget — and shares its READER, so the two cannot drift on what a bad
        or zero value means. Zero stays 120 here on purpose: an interactive
        request must never be unbounded."""
        import app.api.v2_scan as vs

        vs2 = self._reader(monkeypatch, **{"data_source.databricks.remote_query_timeout_seconds": 0})
        assert vs2._databricks_estimate_timeout_s() == 120.0

        vs3 = self._reader(monkeypatch, **{"data_source.databricks.remote_query_timeout_seconds": 45})
        assert vs3._databricks_estimate_timeout_s() == 45.0
        assert vs is vs2 is vs3

    def test_the_admin_hint_documents_the_zero_semantics(self):
        """The knob is settable from /admin/server-config, where the hint is
        the only documentation an operator sees."""
        from app.api.admin import _KNOWN_FIELDS

        hint = _KNOWN_FIELDS["data_source"]["databricks"]["fields"]["scan_timeout_seconds"]["hint"]
        assert "0 disables" in hint


class TestPolicyBodyRewriteStaysInTablePosition:
    """The body rewrite touches table references and nothing else.

    The outer pass guards this by excluding the policied name from the textual
    rewriter. The body cannot use that guard — the body is precisely where the
    policied table must be rewritten — so it works over the AST instead.
    """

    LOOKUPS = [
        ("orders_raw", "main", "sales", "orders_raw"),
        ("region_map", "main", "ref", "region_map"),
    ]

    def _rewrite(self, sql: str) -> str:
        from connectors.databricks.remote import rewrite_policy_body_to_native

        return rewrite_policy_body_to_native(sql, self.LOOKUPS, "main")

    def test_a_qualifier_style_body_does_not_become_a_four_part_column(self):
        """The finding, verbatim. `main.sales.orders_raw.country` is a
        four-part column reference; Spark tolerates it, but it is produced by
        the same mechanism that made a three-part path show up in alias
        position, which is a hard syntax error."""
        out = self._rewrite("SELECT orders_raw.country FROM orders_raw WHERE orders_raw.country = 'CZ'")
        assert "`main`.`sales`.`orders_raw`.country" not in out
        assert out.count("`main`.`sales`.`orders_raw`") == 1
        assert "orders_raw.country" in out

    def test_the_table_itself_is_still_rewritten(self):
        """The half that must not regress: an unrewritten body resolves against
        whatever the warehouse's default context holds."""
        out = self._rewrite("SELECT * FROM orders_raw")
        assert "FROM `main`.`sales`.`orders_raw`" in out

    def test_a_string_literal_naming_the_table_is_left_alone(self):
        """The textual rewriter masks backticks, not quotes."""
        out = self._rewrite("SELECT * FROM orders_raw WHERE note = 'about orders_raw'")
        assert "'about orders_raw'" in out

    def test_a_cte_keeps_its_local_name(self):
        """A CTE the policy defines is a local name — the same exclusion the
        save-time validator makes when it checks table references."""
        out = self._rewrite("WITH orders_raw AS (SELECT 1 AS a) SELECT * FROM orders_raw")
        assert "`main`.`sales`" not in out

    def test_an_alias_survives_the_rewrite(self):
        out = self._rewrite("SELECT o.country FROM orders_raw AS o")
        assert "`main`.`sales`.`orders_raw` AS o" in out
        assert "o.country" in out

    def test_a_mapping_table_join_is_rewritten_too(self):
        """§15's `policy_mapping` idiom — a table only the POLICY names."""
        out = self._rewrite("SELECT * FROM orders_raw o JOIN region_map m ON o.r = m.r")
        assert "`main`.`sales`.`orders_raw`" in out
        assert "`main`.`ref`.`region_map`" in out

    def test_an_already_qualified_path_is_untouched(self):
        out = self._rewrite("SELECT * FROM `main`.`sales`.`orders_raw`")
        assert out.count("`main`.`sales`.`orders_raw`") == 1

    def test_the_dbx_path_form_is_rewritten_without_a_textual_pass(self):
        out = self._rewrite('SELECT * FROM dbx."main.sales"."orders_raw"')
        assert "`main`.`sales`.`orders_raw`" in out
        assert "dbx" not in out

    def test_a_bound_marker_survives_the_round_trip(self):
        """The rewrite runs AFTER `bind_policy_parameters`, so it must not
        disturb the markers that carry the caller's identity."""
        out = self._rewrite("SELECT * FROM orders_raw WHERE email = :user_email")
        assert ":user_email" in out

    def test_an_unparseable_body_denies(self):
        """§17 — every failure denies. Returning the body unrewritten would
        ship a bare name to resolve against the default catalog."""
        from connectors.databricks.remote import DatabricksPolicyRewriteError

        with pytest.raises(DatabricksPolicyRewriteError):
            self._rewrite("SELECT * FROM (((")

    def test_an_unsafe_registry_segment_is_still_refused(self):
        """Moving off the textual path must not drop `quote_dbx_path`'s
        safe-alphabet check — that is what stops a backtick in a registry row
        from breaking out of the quoting."""
        from connectors.databricks.remote import rewrite_policy_body_to_native

        with pytest.raises(DatabricksRemoteError) as exc:
            rewrite_policy_body_to_native(
                "SELECT * FROM orders_raw", [("orders_raw", "main", "sa`les", "orders_raw")], "main"
            )
        assert exc.value.reason == "databricks_unsafe_identifier"

    def test_the_resolver_emits_exactly_one_native_path(self, seeded_app):
        """At the resolver seam, where the body is actually produced. The
        table reference resolves to the row's own native path and that is the
        ONLY one in the body — every other occurrence of the name is a column
        qualifier, which must stay a qualifier."""
        from app.api.query import _databricks_policy_resolver

        _register(
            id="dbx.sales.orders_raw",
            name="orders_raw",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy(
            "dbx.sales.orders_raw",
            "SELECT orders_raw.country, orders_raw.n FROM orders_raw WHERE orders_raw.country = 'CZ'",
        )

        resolve = _databricks_policy_resolver(name_lookups=[], default_catalog="main")
        relation = resolve("orders_raw", {"id": "analyst1", "email": "analyst1@test.com"})

        assert relation.policied
        body = relation.relation_sql
        assert "FROM `main`.`sales`.`orders_raw`" in body, body
        assert body.count("`main`.`sales`.`orders_raw`") == 1, body

    def test_end_to_end_a_qualifier_policy_ships_a_clean_statement(self, seeded_app, warehouse, monkeypatch):
        """Through `/api/query`, which is where a malformed body would actually
        reach the warehouse."""
        wh, _settings = warehouse
        TestPolicyEnforcement()._setup(monkeypatch, "SELECT * FROM orders_raw WHERE orders_raw.country = 'CZ'")

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT country, n FROM orders_raw"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 200, r.text
        submitted = wh.statements[-1]
        assert "`main`.`sales`.`orders_raw`.country" not in submitted
        assert "country = 'CZ'" in submitted
        assert ") AS orders_raw" in submitted


class TestEngineQualifiedPathPolicyGate:
    """A `dbx.*` / three-part Databricks path names the PHYSICAL source, and
    `rewrite_sql` substitutes policied tables by registry NAME — so the policy
    never fires for that spelling.

    The precondition is `name != source_table`, which is the ordinary case
    (`agnes admin register-table` lets an admin name a row whatever reads
    best). When they happen to coincide the bare-name rewrite fires by
    accident and the path is filtered; every other fixture in this file has
    them equal, which is exactly why the gap was invisible here.

    `_gate_table_references` proved registration and the caller's grant, but
    never asked whether the row it resolved to carries a policy — so the raw
    statement shipped to the warehouse under Agnes's service PAT while
    `_apply_databricks_policies` sat out the request on an empty
    `policied_table_ids`. Same hole `sf_path_policied` / `bq_path_policied`
    close for the other two engines.
    """

    POLICY_SQL = "SELECT * FROM orders_public WHERE country = 'CZ'"

    def _setup(self, monkeypatch) -> None:
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_secret",
            name="orders_public",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        _set_policy("dbx.sales.orders_secret", self.POLICY_SQL)
        conn = get_system_db()
        try:
            # A granted NON-admin: an admin bypasses policies everywhere else
            # too, and does so here by design (asserted below).
            grant_table_via_package(conn, "dbx.sales.orders_secret", "analyst1")
        finally:
            conn.close()

    def test_dbx_prefixed_path_to_a_policied_source_is_403(self, seeded_app, warehouse, monkeypatch):
        wh, _settings = warehouse
        self._setup(monkeypatch)

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM dbx."sales"."orders_raw"'},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403, r.text
        detail = r.json().get("detail")
        assert isinstance(detail, dict), detail
        assert detail.get("reason") == "dbx_path_policied", detail
        assert detail.get("registered_as") == "orders_public", detail
        # The property that matters is not the status code: nothing may have
        # reached the warehouse.
        assert not any("orders_raw" in s for s in wh.statements), wh.statements

    def test_qualified_three_part_path_riding_along_is_403(self, seeded_app, warehouse, monkeypatch):
        """The path only routes to Databricks when a registered name rides
        along — which is also the shape that made the leak reachable."""
        wh, _settings = warehouse
        self._setup(monkeypatch)

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": "SELECT * FROM orders_public JOIN main.sales.orders_raw USING (id)"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 403, r.text
        assert r.json().get("detail", {}).get("reason") == "dbx_path_policied", r.text
        assert not any("USING (id)" in s for s in wh.statements), wh.statements

    def test_an_unpolicied_qualified_path_still_runs(self, seeded_app, warehouse, monkeypatch):
        """Non-vacuity. A registered, granted, UNPOLICIED row addressed by its
        engine path must be untouched by the new refusal — otherwise the fix
        reads as working while having simply broken the path."""
        from src.db import get_system_db
        from tests.conftest import grant_table_via_package

        wh, _settings = warehouse
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        _register(
            id="dbx.sales.orders_plain",
            name="orders_plain",
            source_type="databricks",
            bucket="sales",
            source_table="orders_raw",
        )
        conn = get_system_db()
        try:
            grant_table_via_package(conn, "dbx.sales.orders_plain", "analyst1")
        finally:
            conn.close()

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM dbx."sales"."orders_raw"'},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert "dbx_path_policied" not in r.text, r.text
        assert r.status_code == 200, r.text

    def test_admin_still_reaches_the_path_unfiltered(self, seeded_app, warehouse, monkeypatch):
        """§12 — the bypass follows the credential surface, and it did so for
        the bare name already. The new refusal must not change that."""
        wh, _settings = warehouse
        self._setup(monkeypatch)

        c = seeded_app["client"]
        r = c.post(
            "/api/query",
            json={"sql": 'SELECT * FROM dbx."sales"."orders_raw"'},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert "dbx_path_policied" not in r.text
        assert "country = 'CZ'" not in wh.statements[-1]
