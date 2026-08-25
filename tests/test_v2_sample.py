# tests/test_v2_sample.py
import importlib
from unittest.mock import MagicMock
import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clear_sample_cache():
    """The sample-result TTL cache is module-level; clear it between tests so
    cached payloads from a sibling test don't mask call paths.

    This used to be scoped to `TestBqAccessErrors` alone, which left the rest
    of the file exposed to the same hazard: the cache is
    `TTLCache(maxsize=512, ttl_seconds=3600)` keyed on `f"{table_id}|{n}"`, it
    outlives a test by an hour, and every test here samples the same seeded
    ids (`bq_view`, `local_t`). Nothing in the key distinguishes one test's
    monkeypatched `_fetch_bq_sample` from another's, so whichever runs first
    wins and the next one reads its rows — with the fetch never called, which
    makes the monkeypatch look ignored rather than shadowed.

    It stayed latent only while the polluting pair happened to land in
    different pytest-xdist workers. Adding test files anywhere in the suite
    moves the pytest-split boundaries and can put them together, at which
    point `TestSampleAccessPolicyBqBranch::
    test_non_policied_bq_table_is_unaffected` fails asserting
    `[{'col': 'secret'}]` == `[{'event_date': '2026-04-27'}]` — the left side
    being `test_rbac_check_runs_before_cache`'s rows.
    """
    from app.api import v2_sample

    v2_sample._sample_cache.clear()
    yield
    v2_sample._sample_cache.clear()


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
    `_fetch_bq_sample` whole, the inner factories are never called."""
    from connectors.bigquery.access import BqAccess, BqProjects

    return BqAccess(BqProjects(billing=billing, data=data))


class TestSampleEndpoint:
    def test_returns_n_rows_for_bq_table(self, reload_db, monkeypatch):
        from app.api import v2_sample

        monkeypatch.setattr(
            v2_sample,
            "_fetch_bq_sample",
            lambda bq, dataset, table, n: [
                {"event_date": "2026-04-27", "country_code": "CZ"},
                {"event_date": "2026-04-26", "country_code": "SK"},
            ],
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=2, bq=_bq())
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert len(data["rows"]) == 2

    def test_caps_n_at_100(self, reload_db, monkeypatch):
        from app.api import v2_sample

        captured = {}

        def fake_fetch(bq, dataset, table, n):
            captured["n"] = n
            return []

        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", fake_fetch)
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            v2_sample.build_sample(conn, user, "bq_view", n=999, bq=_bq())
        finally:
            conn.close()
        assert captured["n"] == 100

    def test_sample_handles_nan_values_in_rows(self, reload_db, monkeypatch):
        """Regression: rows containing NaN floats from a DuckDB / BigQuery
        scan used to crash the response with `ValueError: Out of range
        float values are not JSON compliant: nan`. The endpoint now
        sanitizes NaN/±inf to None before returning the payload."""
        from app.api import v2_sample

        v2_sample._sample_cache.clear()
        monkeypatch.setattr(
            v2_sample,
            "_fetch_bq_sample",
            lambda bq, dataset, table, n: [
                {"col": float("nan"), "ok": 1.0},
                {"col": float("inf"), "ok": 2.0},
                {"col": float("-inf"), "ok": 3.0},
            ],
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=3, bq=_bq())
        finally:
            conn.close()
        assert data["rows"] == [
            {"col": None, "ok": 1.0},
            {"col": None, "ok": 2.0},
            {"col": None, "ok": 3.0},
        ]
        # Belt-and-braces: payload must round-trip through stdlib json
        # in strict mode (allow_nan=False) — that's what FastAPI's
        # serializer enforces internally.
        import json as _json

        _json.dumps(data, allow_nan=False)  # must not raise

    def test_sample_handles_nested_nan_in_arrays(self, reload_db, monkeypatch):
        """Sanitizer recurses into nested lists/dicts — array-typed BQ
        cells with NaN inside also serialize cleanly."""
        from app.api import v2_sample

        v2_sample._sample_cache.clear()
        monkeypatch.setattr(
            v2_sample,
            "_fetch_bq_sample",
            lambda *a, **kw: [{"arr": [1.0, float("nan"), 3.0], "nested": {"x": float("inf")}}],
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=1, bq=_bq())
        finally:
            conn.close()
        assert data["rows"][0]["arr"] == [1.0, None, 3.0]
        assert data["rows"][0]["nested"] == {"x": None}
        import json as _json

        _json.dumps(data, allow_nan=False)

    def test_rbac_check_runs_before_cache(self, reload_db, monkeypatch):
        """Regression: cache check used to come before RBAC, leaking sample rows
        cached by an authorized user to subsequent unauthorized callers."""
        from app.api import v2_sample

        monkeypatch.setattr(
            v2_sample,
            "_fetch_bq_sample",
            lambda *a, **kw: [{"col": "secret"}],
        )
        monkeypatch.setattr(
            "app.api.v2_sample.can_access_table",
            lambda user, tid, conn: user.get("id") == "admin1",
        )
        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            admin = {"id": "admin1", "email": "admin@x.com"}
            v2_sample.build_sample(conn, admin, "bq_view", n=2, bq=_bq())
            other = {"id": "viewer1", "email": "viewer@x.com"}
            with pytest.raises(PermissionError):
                v2_sample.build_sample(conn, other, "bq_view", n=2, bq=_bq())
        finally:
            conn.close()

    def test_materialized_bq_table_reads_parquet_not_bq(self, reload_db, monkeypatch):
        """Regression: build_sample routed materialized tables (source_type='bigquery',
        query_mode='materialized') to _fetch_bq_sample, which attempted a live BQ
        query for data that lives locally as parquet — causing HTTP 500.

        After the fix, query_mode='materialized' must always fall through to the
        local parquet read path, regardless of source_type."""
        import duckdb as _duckdb
        from app.api import v2_sample
        from app.utils import get_data_dir

        v2_sample._sample_cache.clear()

        bq_called = []

        def _fake_bq_fetch(*a, **kw):
            bq_called.append(True)
            return []

        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", _fake_bq_fetch)

        parquet_dir = get_data_dir() / "extracts" / "bigquery" / "data"
        parquet_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = parquet_dir / "order_economics.parquet"
        c = _duckdb.connect(":memory:")
        try:
            c.execute(
                "COPY (SELECT 'Los Angeles' AS customer_city, 100 AS orders "
                "UNION ALL SELECT 'New York', 80 AS orders) "
                f"TO '{parquet_path}' (FORMAT PARQUET)"
            )
        finally:
            c.close()

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            from src.repositories.table_registry import TableRegistryRepository

            TableRegistryRepository(conn).register(
                id="order_economics",
                name="order_economics",
                source_type="bigquery",
                query_mode="materialized",
                bucket="finance_unit_economics",
                source_table="order_economics",
            )
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "order_economics", n=5, bq=_bq())
        finally:
            conn.close()

        assert not bq_called, "_fetch_bq_sample must not be called for materialized tables"
        assert data["table_id"] == "order_economics"
        assert len(data["rows"]) == 2
        cities = {r["customer_city"] for r in data["rows"]}
        assert cities == {"Los Angeles", "New York"}


class TestBqAccessErrors:
    """Issue #134: structured 502 translation on BQ errors in sample path.

    These tests exercise the REAL translation path through `BqAccess` +
    `translate_bq_error` by injecting a duckdb_session whose execute() raises
    the Google API exception. That's the production path — Phase 1
    monkeypatches of `_fetch_bq_sample` whole would skip the translation logic
    and only test the outer wrap (which has been removed in Phase 2).

    Key difference from /scan: /sample SQL is server-constructed (validated
    identifiers + LIMIT n), so a BadRequest from BQ means registry corruption,
    NOT user input → translates to `bq_upstream_error` (HTTP 502), not 400.
    """

    def test_sample_returns_502_on_bq_forbidden_serviceusage(self, reload_db, bq_access):
        """When the BQ extension raises Forbidden mentioning serviceusage,
        the endpoint must translate to HTTP 502 with a structured body
        whose `error` is `cross_project_forbidden` and whose hint mentions
        `billing_project`."""
        from app.api import v2_sample
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden("Permission denied: serviceusage.services.use on project foo")
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}

            # Endpoint is async — drive it directly. dependency_overrides only
            # fires through TestClient/HTTP, so pass `bq=bq` explicitly.
            with pytest.raises(HTTPException) as exc_info:
                (
                    v2_sample.sample(
                        table_id="bq_view",
                        n=5,
                        user=user,
                        conn=conn,
                        bq=bq,
                    )
                )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_sample_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, bq_access):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden` (no billing_project hint)."""
        from app.api import v2_sample
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden("Access Denied: Table foo.bar.baz: User does not have permission")
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}

            with pytest.raises(HTTPException) as exc_info:
                (
                    v2_sample.sample(
                        table_id="bq_view",
                        n=5,
                        user=user,
                        conn=conn,
                        bq=bq,
                    )
                )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_sample_returns_502_on_bq_bad_request(self, reload_db, bq_access):
        """`/sample` SQL is server-constructed (validated identifiers + LIMIT n),
        so a BQ BadRequest means registry corruption, not user input. Must
        surface as HTTP 502 with `bq_upstream_error` (NOT 400 / `bq_bad_request`
        like /scan does — that's the key difference from Task 2.7)."""
        from app.api import v2_sample
        from google.api_core.exceptions import BadRequest

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = BadRequest("Syntax error: unexpected token at line 1, column 5")
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}

            with pytest.raises(HTTPException) as exc_info:
                (
                    v2_sample.sample(
                        table_id="bq_view",
                        n=5,
                        user=user,
                        conn=conn,
                        bq=bq,
                    )
                )
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_upstream_error"
        assert "Syntax error" in detail["message"]

    def test_sample_passes_billing_project_to_bigquery_query(self, reload_db, bq_access):
        """Regression guard: bq.projects.billing must be passed to bigquery_query()
        as the billing project (positional arg 0). Verifies the migration didn't
        regress the original cross-project bug fix."""
        from app.api import v2_sample

        captured = {}

        def _fake_execute(sql, params):
            # Capture the bigquery_query() call args.
            if "bigquery_query" in sql:
                captured["billing_project"] = params[0]
                captured["bq_sql"] = params[1]
            result = MagicMock()
            result.fetchdf.return_value.to_dict.return_value = []
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            (
                v2_sample.sample(
                    table_id="bq_view",
                    n=5,
                    user=user,
                    conn=conn,
                    bq=bq,
                )
            )
        finally:
            conn.close()

        assert captured["billing_project"] == "billing-proj"
        # FROM clause uses data project (where the table actually lives)
        assert "`data-proj.ds.bq_view`" in captured["bq_sql"]


class TestNotSyncedDetail:
    """Registered-but-dataless tables must explain the pending/failing first
    sync instead of the misleading bare "table not found" (which reads as
    "registration failed" to the admin who just registered the table)."""

    @staticmethod
    def _register_keboola_row(conn, table_id):
        from src.repositories.table_registry import TableRegistryRepository

        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type="keboola",
            bucket="in.c-main",
            source_table="orders",
            query_mode="materialized",
        )

    def test_registered_but_unsynced_table_explains_pending_sync(self, reload_db):
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_keboola_row(conn, "kbc_fresh")
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotSyncedError) as exc_info:
                v2_sample.build_sample(conn, user, "kbc_fresh", n=5, bq=_bq())
        finally:
            conn.close()
        assert "no synced data yet" in exc_info.value.detail
        assert "kbc_fresh" in exc_info.value.detail

    def test_last_sync_error_included_when_recorded(self, reload_db):
        from app.api import v2_sample
        from src.repositories.sync_state import SyncStateRepository

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_keboola_row(conn, "kbc_broken")
            SyncStateRepository(conn).set_error("kbc_broken", "GET .../export-async -> HTTP 404: nonexistent table")
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotSyncedError) as exc_info:
                v2_sample.build_sample(conn, user, "kbc_broken", n=5, bq=_bq())
        finally:
            conn.close()
        assert "last sync error" in exc_info.value.detail
        assert "HTTP 404" in exc_info.value.detail

    def test_endpoint_maps_not_synced_detail_to_404(self, reload_db):
        """The route handler must surface TableNotSyncedError.detail — not the
        generic `table '…' not found` message."""
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_keboola_row(conn, "kbc_endpoint")
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                v2_sample.sample(table_id="kbc_endpoint", n=5, user=user, conn=conn, bq=_bq())
        finally:
            conn.close()
        assert exc_info.value.status_code == 404
        assert "no synced data yet" in exc_info.value.detail


class TestByDesignNotLocalTablesDoNotBlameTheSync:
    """A row that is never materialized locally must not be reported as a
    pending or failing first sync.

    `build_sample` special-cased only BigQuery non-materialized rows, so a
    Keboola row registered `query_mode='remote'` — the shape this PR adds — and
    any `server_only` row fell through to `resolve_local_parquet`, got `None`
    legitimately, and were explained as "the first sync is pending or failing".
    That sends an admin hunting a sync job that does not exist and never will
    (Devin Review on #1189).
    """

    @staticmethod
    def _register(conn, table_id, *, query_mode="remote", server_only=False):
        from src.repositories.table_registry import TableRegistryRepository

        TableRegistryRepository(conn).register(
            id=table_id,
            name=table_id,
            source_type="keboola",
            bucket="in.c-main",
            source_table="orders",
            query_mode=query_mode,
            server_only=server_only,
        )

    def _detail_for(self, reload_db, table_id, **kw):
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register(conn, table_id, **kw)
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotPreviewableError) as exc_info:
                v2_sample.build_sample(conn, user, table_id, n=5, bq=_bq())
        finally:
            conn.close()
        return exc_info.value.detail

    def test_remote_mode_row_is_not_described_as_a_pending_sync(self, reload_db):
        detail = self._detail_for(reload_db, "kbc_remote", query_mode="remote")
        assert "first sync" not in detail, detail
        assert "no synced data yet" not in detail, detail
        assert "query_mode='remote'" in detail
        assert "--remote" in detail, "must point at the way to actually read it"

    def test_server_only_still_blames_the_sync_because_the_server_does_copy_it(self, reload_db):
        """`server_only` suppresses DISTRIBUTION, not materialization.

        The server still writes the parquet — `app/api/sync.py` says as much
        ("remote tables have no server parquet at all, and server_only ones are
        deliberately not distributed"), and the registration validator rejects
        `server_only` together with `query_mode='remote'` for exactly that
        reason. So a missing parquet HERE is a real pending or failing sync. An
        earlier version of this fix lumped it in with remote and reassured the
        admin that nothing was wrong, hiding a broken job (Devin Review on
        #1189).

        The predicate that was borrowed from — sync.py's signed-URL gate —
        combines the two correctly, because for DISTRIBUTION they coincide. For
        previewability on the server they do not.
        """
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register(conn, "kbc_srv_only", query_mode="materialized", server_only=True)
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotSyncedError) as exc_info:
                v2_sample.build_sample(conn, user, "kbc_srv_only", n=5, bq=_bq())
        finally:
            conn.close()
        assert not isinstance(exc_info.value, v2_sample.TableNotPreviewableError)
        assert "no synced data yet" in exc_info.value.detail

    def test_server_only_with_remote_is_rejected_at_registration(self):
        """Why there is no "both" case to disambiguate: the combination cannot
        be persisted. An earlier test asserted a precedence rule between the two
        by writing straight to the repository, bypassing this validator — a test
        for a state the product does not allow."""
        import pytest as _pytest

        from app.api.admin import RegisterTableRequest

        with _pytest.raises(ValueError, match="server_only"):
            RegisterTableRequest(
                id="x",
                name="x",
                source_type="keboola",
                bucket="in.c-main",
                source_table="orders",
                query_mode="remote",
                server_only=True,
            )

    def test_still_a_not_synced_error_so_existing_catches_and_the_404_hold(self, reload_db):
        from app.api import v2_sample

        assert issubclass(v2_sample.TableNotPreviewableError, v2_sample.TableNotSyncedError)
        assert issubclass(v2_sample.TableNotPreviewableError, FileNotFoundError)

    def test_a_genuinely_unsynced_local_row_still_blames_the_sync(self, reload_db):
        """The regression guard for the fix itself: narrowing must not silence
        the case the not-synced message was written for."""
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register(conn, "kbc_local_fresh", query_mode="materialized")
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotSyncedError) as exc_info:
                v2_sample.build_sample(conn, user, "kbc_local_fresh", n=5, bq=_bq())
        finally:
            conn.close()
        assert not isinstance(exc_info.value, v2_sample.TableNotPreviewableError)
        assert "no synced data yet" in exc_info.value.detail


class TestPartitionedTablePreview:
    """A partitioned table lays its data out as a DIRECTORY of per-period files
    (`data/<table_id>/2025_11.parquet`), so the single-file lookup returned None
    and `build_sample` reported a pending or failing first sync for a table whose
    every sync had succeeded (Devin Review on #1189)."""

    def test_glob_resolver_finds_a_partitioned_directory(self, tmp_path, monkeypatch):
        import pandas as pd

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        part_dir = tmp_path / "extracts" / "keboola" / "data" / "kbc_sales"
        part_dir.mkdir(parents=True)
        pd.DataFrame({"amount": [1, 2]}).to_parquet(part_dir / "2025_11.parquet")
        pd.DataFrame({"amount": [3]}).to_parquet(part_dir / "2025_12.parquet")

        from app.utils import resolve_local_parquet, resolve_local_parquet_glob

        assert resolve_local_parquet("kbc_sales", "keboola") is None, "precondition: no single file"
        target = resolve_local_parquet_glob("kbc_sales", "keboola")
        assert target is not None and target.endswith("*.parquet"), target

        # ...and DuckDB reads every partition through it.
        from src.db import _open_duckdb

        c = _open_duckdb(":memory:")
        try:
            total = c.execute("SELECT COUNT(*) FROM read_parquet(?)", [target]).fetchone()[0]
        finally:
            c.close()
        assert total == 3, "all partitions must be readable through the glob"

    def test_single_file_layout_still_wins(self, tmp_path, monkeypatch):
        """The common case must not regress: a plain parquet resolves to the file
        itself, not to a glob."""
        import pandas as pd

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        data = tmp_path / "extracts" / "keboola" / "data"
        data.mkdir(parents=True)
        pd.DataFrame({"a": [1]}).to_parquet(data / "kbc_orders.parquet")

        from app.utils import resolve_local_parquet_glob

        target = resolve_local_parquet_glob("kbc_orders", "keboola")
        assert target is not None and target.endswith("kbc_orders.parquet")
        assert "*" not in target

    def test_neither_layout_still_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data").mkdir(parents=True)
        from app.utils import resolve_local_parquet_glob

        assert resolve_local_parquet_glob("kbc_missing", "keboola") is None

    def test_an_empty_partition_directory_is_not_mistaken_for_data(self, tmp_path, monkeypatch):
        """A directory with no parquet in it means the sync has not produced a
        partition yet — that IS the pending-sync case, so it must stay None
        rather than resolve to a glob matching nothing."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        (tmp_path / "extracts" / "keboola" / "data" / "kbc_empty").mkdir(parents=True)
        from app.utils import resolve_local_parquet_glob

        assert resolve_local_parquet_glob("kbc_empty", "keboola") is None


class TestRemoteRowLiveSample:
    """`query_mode='remote'` rows (non-BQ) preview live through their
    analytics view instead of being refused outright.

    The orchestrator maintains a view over the re-ATTACHed source for every
    remote row that resolves locally (`_remote_attach` — Snowflake `sf`,
    Keboola `kbc`, Databricks with attach_enabled), and `/api/query` already
    serves these rows through that view; the sample endpoint refusing them
    was a parity gap with the BigQuery live branch above. When the view
    cannot serve (attach lost, engine with no local view), the pre-existing
    by-design refusal stays — enriched with the real failure so the admin is
    not sent hunting a sync job while the actual error goes unreported.
    """

    @staticmethod
    def _register_remote(conn, table_id, *, with_policy=False):
        from src.repositories.table_registry import TableRegistryRepository

        repo = TableRegistryRepository(conn)
        repo.register(
            id=table_id,
            name=table_id,
            source_type="snowflake",
            bucket="GOLD",
            source_table="BI_X",
            query_mode="remote",
        )
        if with_policy:
            repo.set_access_policy(table_id, sql=f"SELECT * FROM {table_id}", note="test", updated_by="admin")

    @staticmethod
    def _create_analytics_view(table_id, rows_sql):
        """Stand in for the orchestrator: materialize the analytics DB with a
        relation named like the remote row's view."""
        import os
        from pathlib import Path

        import duckdb as _duckdb

        data_dir = Path(os.environ["DATA_DIR"])
        (data_dir / "analytics").mkdir(parents=True, exist_ok=True)
        c = _duckdb.connect(str(data_dir / "analytics" / "server.duckdb"))
        try:
            c.execute(f'CREATE TABLE "{table_id}" AS {rows_sql}')
        finally:
            c.close()

    def test_remote_row_returns_live_rows_from_the_analytics_view(self, reload_db):
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_live")
            self._create_analytics_view("sf_live", "SELECT 1 AS a, 'x' AS b UNION ALL SELECT 2, 'y'")
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "sf_live", n=5, bq=_bq())
        finally:
            conn.close()
        assert data["table_id"] == "sf_live"
        assert data["source"] == "snowflake"
        assert len(data["rows"]) == 2
        assert data["rows"][0] == {"a": 1, "b": "x"}

    def test_n_caps_the_live_rows(self, reload_db):
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_capped")
            self._create_analytics_view("sf_capped", "SELECT * FROM range(10)")
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "sf_capped", n=3, bq=_bq())
        finally:
            conn.close()
        assert len(data["rows"]) == 3

    def test_remote_wins_over_a_stale_parquet_left_by_a_materialized_era(self, reload_db, monkeypatch):
        """A row flipped materialized→remote can leave its old parquet on
        disk. `query_mode='remote'` means every read goes live — serving the
        stale (possibly empty) copy would silently show outdated data, so the
        mode check must run BEFORE parquet resolution."""
        from app.api import v2_sample

        monkeypatch.setattr(
            "app.api.v2_sample.resolve_local_parquet_glob",
            lambda *a, **kw: pytest.fail("remote row must not resolve a local parquet"),
            raising=False,
        )
        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_flipped")
            self._create_analytics_view("sf_flipped", "SELECT 'live' AS origin")
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "sf_flipped", n=5, bq=_bq())
        finally:
            conn.close()
        assert data["rows"] == [{"origin": "live"}]

    def test_missing_view_falls_back_to_the_by_design_refusal(self, reload_db):
        """No analytics view (attach lost, or an engine with no local view):
        the by-design message survives — including the pointer at the way to
        actually read the table — plus the real failure, so nothing lies."""
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_noview")
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotPreviewableError) as exc_info:
                v2_sample.build_sample(conn, user, "sf_noview", n=5, bq=_bq())
        finally:
            conn.close()
        detail = exc_info.value.detail
        assert "--remote" in detail
        assert "query_mode='remote'" in detail
        assert "first sync" not in detail
        assert "no synced data yet" not in detail
        # The real failure is reported, not swallowed behind the reassurance.
        assert "live sample" in detail

    def test_policied_remote_row_fails_closed_for_a_filtered_caller(self, reload_db, monkeypatch):
        """Same Task-13 ratchet as the BQ live branch: policy rewrite is not
        wired into this surface, so a caller the policy would filter must
        never see the raw live rows."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        from app.api import v2_sample

        monkeypatch.setattr("app.api.v2_sample.can_access_table", lambda user, tid, conn: True)
        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_policied", with_policy=True)
            self._create_analytics_view("sf_policied", "SELECT 'secret' AS s")
            non_admin = {"id": "viewer1", "email": "viewer@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                v2_sample.build_sample(conn, non_admin, "sf_policied", n=2, bq=_bq())
        finally:
            conn.close()
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == {"reason": "policy_error", "table": "sf_policied"}

    def test_admin_bypass_on_a_policied_remote_row(self, reload_db, monkeypatch):
        """Mirrors the BQ branch: `policied_relation` itself decides the
        admin bypass, so an admin keeps seeing the raw live sample."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_policied_adm", with_policy=True)
            self._create_analytics_view("sf_policied_adm", "SELECT 'admin-visible' AS s")
            admin = {"id": "admin1", "email": "admin1@test.com"}
            data = v2_sample.build_sample(conn, admin, "sf_policied_adm", n=2, bq=_bq())
        finally:
            conn.close()
        assert data["rows"] == [{"s": "admin-visible"}]


class TestParquetKeyedByRegistryName:
    """The write side keys the parquet FILENAME by `table_registry.name`
    (`app/api/sync.py::_run_materialized_pass`: "the parquet filename [is]
    keyed by `table_registry.name`"; the extractors' `tc["name"]`), while the
    read surfaces receive the registry `id` off the request path. The
    register handler derives the id by slugifying the name
    (`name.strip().lower().replace(" ", "_")`), so the two routinely differ —
    and then the id-keyed lookup misses a healthy, fully-synced table, which
    `build_sample` reported as "the first sync is pending or failing"
    (observed live: uppercase Keboola source-table names registered through
    the Data-sources wizard)."""

    ID = "orders_90d"
    NAME = "Orders 90d"  # the exact pair the register handler's slugify produces

    def _register(self, conn):
        _ensure_admin1(conn)
        from src.repositories.table_registry import TableRegistryRepository

        TableRegistryRepository(conn).register(
            id=self.ID,
            name=self.NAME,
            source_type="keboola",
            bucket="in.c-main",
            source_table="ORDERS_90D",
            query_mode="materialized",
        )

    def _write_parquet_by_name(self, data_dir):
        import duckdb as _duckdb

        data_dir.mkdir(parents=True, exist_ok=True)
        parquet_path = data_dir / f"{self.NAME}.parquet"
        c = _duckdb.connect(":memory:")
        try:
            c.execute(f"COPY (SELECT 1 AS order_id, 'EUR' AS currency) TO '{parquet_path}' (FORMAT PARQUET)")
        finally:
            c.close()

    def test_glob_resolver_prefers_the_registry_name_key(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        self._write_parquet_by_name(tmp_path / "extracts" / "keboola" / "data")

        from app.utils import resolve_local_parquet_glob

        # Pin the gap the keyword exists for: the bare id-keyed lookup misses.
        assert resolve_local_parquet_glob(self.ID, "keboola") is None
        target = resolve_local_parquet_glob(self.ID, "keboola", registry_name=self.NAME)
        assert target is not None and target.endswith(f"{self.NAME}.parquet"), target

    def test_partitioned_directory_keyed_by_name_resolves_too(self, tmp_path, monkeypatch):
        """The partitioned sync writes `data/<name>/` — same keying convention,
        same miss."""
        import pandas as pd

        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        part_dir = tmp_path / "extracts" / "keboola" / "data" / self.NAME
        part_dir.mkdir(parents=True)
        pd.DataFrame({"amount": [1, 2]}).to_parquet(part_dir / "2026_01.parquet")

        from app.utils import resolve_local_parquet_glob

        assert resolve_local_parquet_glob(self.ID, "keboola") is None
        target = resolve_local_parquet_glob(self.ID, "keboola", registry_name=self.NAME)
        assert target is not None and target.endswith("*.parquet"), target

    def test_unset_registry_name_changes_nothing(self, tmp_path, monkeypatch):
        """Callers that don't pass the keyword keep the exact old behavior."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        self._write_parquet_by_name(tmp_path / "extracts" / "keboola" / "data")

        from app.utils import resolve_local_parquet_glob

        assert resolve_local_parquet_glob(self.ID, "keboola") is None
        assert resolve_local_parquet_glob(self.ID, "keboola", registry_name=None) is None

    def test_sample_resolves_when_wizard_id_differs_from_name(self, reload_db):
        from app.api import v2_sample
        from app.utils import get_data_dir

        self._write_parquet_by_name(get_data_dir() / "extracts" / "keboola" / "data")
        conn = reload_db.get_system_db()
        try:
            self._register(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, self.ID, n=5, bq=_bq())
        finally:
            conn.close()
        assert data["table_id"] == self.ID
        assert data["rows"] == [{"order_id": 1, "currency": "EUR"}]

    def test_not_synced_detail_reads_sync_state_under_the_name_key(self, reload_db):
        """sync_state is keyed by the registry NAME (same write-side
        convention), so the 404 detail's last-sync-error lookup must try the
        name too — keyed by id it never surfaced the recorded failure for a
        wizard-registered row."""
        from app.api import v2_sample
        from src.repositories.sync_state import SyncStateRepository

        conn = reload_db.get_system_db()
        try:
            self._register(conn)  # no parquet written — genuinely unsynced
            SyncStateRepository(conn).set_error(self.NAME, "export-async -> HTTP 403: token lacks bucket access")
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(v2_sample.TableNotSyncedError) as exc_info:
                v2_sample.build_sample(conn, user, self.ID, n=5, bq=_bq())
        finally:
            conn.close()
        assert "last sync error" in exc_info.value.detail, exc_info.value.detail
        assert "HTTP 403" in exc_info.value.detail


class TestSampleAccessPolicyBqBranch:
    """Task 13 (§8 ratchet) — the BQ live-query branch of `build_sample` had
    NO access-policy enforcement at all: `_fetch_bq_sample` pushes straight
    to BigQuery with no `policied_relation` call anywhere on the path.
    `_fetch_bq_sample` is monkeypatched to return a recognizable row so a
    regression (the new guard silently not firing) shows up as leaked
    content reaching the caller, not just a passing assert.
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
        from app.api import v2_sample

        leaked = [{"secret_col": "leaked-row"}]
        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", lambda *a, **kw: leaked)
        # can_access_table's real stack-gate needs a data-package grant this
        # test has no reason to set up — the policy-guard behavior under
        # test fires AFTER that check, so stub it exactly like
        # test_rbac_check_runs_before_cache above does.
        monkeypatch.setattr("app.api.v2_sample.can_access_table", lambda user, tid, conn: True)

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_policied_bq_table(conn, "bq_policied")
            non_admin = {"id": "viewer1", "email": "viewer@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                v2_sample.build_sample(conn, non_admin, "bq_policied", n=2, bq=_bq())
        finally:
            conn.close()
        assert exc_info.value.status_code == 500
        assert exc_info.value.detail == {"reason": "policy_error", "table": "bq_policied"}

    def test_admin_bypass_is_unaffected(self, reload_db, monkeypatch):
        """Admin/no-policy unchanged (§12) — the guard checks
        `policied_relation`'s own admin bypass, not a bare
        `access_policy_sql` truthiness, so an admin keeps reading the raw
        sample exactly as before this change."""
        monkeypatch.setenv("AGNES_ACCESS_POLICIES_ENABLED", "true")
        from app.api import v2_sample

        admin_rows = [{"secret_col": "admin-visible-row"}]
        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", lambda *a, **kw: admin_rows)

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_policied_bq_table(conn, "bq_policied_admin")
            admin = {"id": "admin1", "email": "admin1@test.com"}
            data = v2_sample.build_sample(conn, admin, "bq_policied_admin", n=2, bq=_bq())
        finally:
            conn.close()
        assert data["rows"] == admin_rows

    def test_non_policied_bq_table_is_unaffected(self, reload_db, monkeypatch):
        """The inert case: a table with no access_policy_sql keeps the
        pre-existing (unfiltered) BQ-sample behavior exactly."""
        from app.api import v2_sample

        rows = [{"event_date": "2026-04-27"}]
        monkeypatch.setattr(v2_sample, "_fetch_bq_sample", lambda *a, **kw: rows)

        conn = reload_db.get_system_db()
        try:
            _seed(conn)  # registers "bq_view" with no access_policy_sql
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_sample.build_sample(conn, user, "bq_view", n=2, bq=_bq())
        finally:
            conn.close()
        assert data["rows"] == rows


class TestRemoteLiveMeansLive:
    """The `remote` branch justifies its position — ahead of parquet
    resolution — with "`remote` means every read goes live", and then wrote
    its result into a one-hour `TTLCache` under a plain `{table_id}|{n}` key.
    Serving a 60-minute-old copy is the same staleness the branch was moved to
    avoid, just from a different store.

    Cost was the reason to cache, and it does not survive contact with the
    numbers: the read is `SELECT * FROM <view> LIMIT n` with n ≤ 100. For
    Snowflake and Keboola that is a bounded pull through the extension. Only
    Databricks-with-`attach_enabled` — off by default and marked experimental
    — makes it a parquet read, and buying liveness for the two supported
    engines at that price is the right trade.
    """

    _register_remote = staticmethod(TestRemoteRowLiveSample._register_remote)
    _create_analytics_view = staticmethod(TestRemoteRowLiveSample._create_analytics_view)

    @staticmethod
    def _replace_view(table_id, rows_sql):
        import os
        from pathlib import Path

        import duckdb as _duckdb

        c = _duckdb.connect(str(Path(os.environ["DATA_DIR"]) / "analytics" / "server.duckdb"))
        try:
            c.execute(f'CREATE OR REPLACE TABLE "{table_id}" AS {rows_sql}')
        finally:
            c.close()

    def test_a_second_read_sees_the_upstream_change(self, reload_db):
        from app.api import v2_sample

        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_fresh")
            self._create_analytics_view("sf_fresh", "SELECT 'v1' AS v")
            user = {"id": "admin1", "email": "a@x.com"}
            first = v2_sample.build_sample(conn, user, "sf_fresh", n=5, bq=_bq())
            assert first["rows"] == [{"v": "v1"}]

            self._replace_view("sf_fresh", "SELECT 'v2' AS v")
            second = v2_sample.build_sample(conn, user, "sf_fresh", n=5, bq=_bq())
        finally:
            conn.close()

        assert second["rows"] == [{"v": "v2"}], (
            "a 'live' remote sample was served from the 1h cache -- the branch's own "
            "justification for running ahead of parquet resolution says every read goes live"
        )

    def test_a_local_row_still_caches(self, reload_db, monkeypatch):
        """Non-vacuity: only `remote` opts out. A local/materialized row keeps
        the cache it has always had, so this is a targeted exemption rather
        than a quiet removal of the whole cache."""
        from app.api import v2_sample

        assert v2_sample._sample_cache is not None
        conn = reload_db.get_system_db()
        try:
            _ensure_admin1(conn)
            self._register_remote(conn, "sf_cached_check")
        finally:
            conn.close()
        # The decision itself, read directly — no fixture can observe a cache
        # hit without also observing the fetch it skipped.
        cacheable = v2_sample._sample_is_cacheable
        assert cacheable(source_type="snowflake", query_mode="local", has_access_policy=False) is True
        assert cacheable(source_type="snowflake", query_mode="materialized", has_access_policy=False) is True
        assert cacheable(source_type="snowflake", query_mode="remote", has_access_policy=False) is False
        assert cacheable(source_type="snowflake", query_mode="local", has_access_policy=True) is False

    def test_bigquery_remote_previews_stay_cached(self):
        """The exemption is about liveness for the extension-resolved engines;
        for BigQuery it would have been a COST regression instead.

        A BQ remote row takes the branch above the new one — `source_type ==
        "bigquery"` and not `materialized` — into `_fetch_bq_sample`, which
        pushes the statement to BigQuery. That is a metered, billable scan per
        call, unlike every other engine on this surface, and it was cacheable
        before the exemption existed (`cacheable = not has_access_policy`). So
        every catalog tile render and every `agnes describe` would have billed
        a fresh query. Caught in review on the train that introduced it.

        A policied BQ row is still uncacheable — the identity-free cache key
        is the reason there, and it outranks cost."""
        from app.api import v2_sample as mod

        cacheable = mod._sample_is_cacheable
        assert cacheable(source_type="bigquery", query_mode="remote", has_access_policy=False) is True
        assert cacheable(source_type="BigQuery", query_mode="remote", has_access_policy=False) is True, (
            "source_type comparison must not be case-sensitive"
        )
        assert cacheable(source_type="bigquery", query_mode="remote", has_access_policy=True) is False, (
            "a policied row stays uncacheable regardless of engine -- the key has no identity in it"
        )
        # Non-vacuity: the exemption still bites for the engines it was for.
        assert cacheable(source_type="keboola", query_mode="remote", has_access_policy=False) is False
        assert cacheable(source_type="databricks", query_mode="remote", has_access_policy=False) is False
