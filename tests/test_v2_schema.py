# tests/test_v2_schema.py
import asyncio
import importlib
from unittest.mock import patch, MagicMock
import pytest
from fastapi import HTTPException


@pytest.fixture
def reload_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import src.db as db_module
    importlib.reload(db_module)
    yield db_module


def _seed_bq_table(conn):
    _ensure_admin1(conn)
    from src.repositories.table_registry import TableRegistryRepository
    TableRegistryRepository(conn).register(
        id="bq_view", name="bq_view", source_type="bigquery",
        bucket="ds", source_table="bq_view", query_mode="remote",
    )


def _ensure_admin1(conn):
    """Seed an admin user with id='admin1' + Admin group membership so
    {"id": "admin1", ...} dicts pass the can_access admin shortcut."""
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.users import UserRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    if UserRepository(conn).get_by_id('admin1') is None:
        UserRepository(conn).create(id='admin1', email='admin1@test.com', name='Admin')
    admin_gid = conn.execute(
        'SELECT id FROM user_groups WHERE name = ?', [SYSTEM_ADMIN_GROUP]
    ).fetchone()
    if admin_gid:
        UserGroupMembersRepository(conn).add_member(
            'admin1', admin_gid[0], source='system_seed',
        )


def _bq(billing="billing-proj", data="data-proj"):
    """Build a BqAccess wired to default factories. For tests that monkeypatch
    `_fetch_bq_schema` / `_fetch_bq_table_options` whole, the inner factories
    are never called."""
    from connectors.bigquery.access import BqAccess, BqProjects
    return BqAccess(BqProjects(billing=billing, data=data))


class TestSchemaEndpoint:
    def test_bq_table_returns_columns_and_dialect_hints(self, reload_db, monkeypatch):
        from app.api import v2_schema
        # Stub the BQ schema fetch to avoid hitting real BQ
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda bq, dataset, table, **kw: [
                {"name": "event_date", "type": "DATE", "nullable": False, "description": ""},
                {"name": "country_code", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_table_options",
            lambda bq, dataset, table, **kw: {"partition_by": "event_date", "clustered_by": []},
        )

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = v2_schema.build_schema(conn, user, "bq_view", bq=_bq())
        finally:
            conn.close()
        assert data["table_id"] == "bq_view"
        assert data["sql_flavor"] == "bigquery"
        assert {c["name"] for c in data["columns"]} == {"event_date", "country_code"}
        assert "where_dialect_hints" in data
        assert data["partition_by"] == "event_date"

    def test_unknown_table_raises_404(self, reload_db):
        from app.api.v2_schema import build_schema, NotFound
        conn = reload_db.get_system_db()
        try:
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(NotFound):
                build_schema(conn, user, "missing", bq=_bq())
        finally:
            conn.close()

    def test_rbac_check_runs_before_cache(self, reload_db, monkeypatch):
        """Regression: cache lookup used to happen before the RBAC check, and the
        cache key had no user component — so an unauthorized user could read
        cached schema fetched by an authorized one. The fix moves RBAC ahead."""
        from app.api import v2_schema
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda *a, **kw: [{"name": "x", "type": "STRING", "nullable": True, "description": ""}],
        )
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda *a, **kw: {})
        # Stub can_access_table — admin1 always allowed (warms cache),
        # everyone else denied (must hit RBAC check, not cached payload).
        monkeypatch.setattr(
            "app.api.v2_schema.can_access_table",
            lambda user, tid, conn: user.get("id") == "admin1",
        )

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            # Admin warms the cache.
            admin = {"id": "admin1", "email": "admin@x.com"}
            v2_schema.build_schema(conn, admin, "bq_view", bq=_bq())
            # Non-admin must hit RBAC denial — cache must NOT short-circuit.
            other = {"id": "viewer1", "email": "viewer@x.com"}
            with pytest.raises(PermissionError):
                v2_schema.build_schema(conn, other, "bq_view", bq=_bq())
        finally:
            conn.close()


class TestBqAccessErrors:
    """Issue #134: structured 502 translation on BQ errors in the strict /schema path.

    These tests exercise the REAL translation path through `BqAccess` +
    `translate_bq_error` by injecting a duckdb_session whose execute() raises
    the Google API exception. That's the production path — Phase 1
    monkeypatches of `_fetch_bq_schema` whole would skip the translation logic
    and only test the outer wrap (which has been removed in Phase 2).

    `/schema` differs from `/scan` and `/scan/estimate`: the SQL is
    server-constructed (queries `INFORMATION_SCHEMA.COLUMNS` with validated
    identifiers, no user-derived fragments), so `BadRequest` → 502
    (`bq_upstream_error`) rather than 400. Same as `/sample`.
    """

    @pytest.fixture(autouse=True)
    def _clear_schema_cache(self):
        # Prevent payloads from earlier tests from short-circuiting the fetch.
        from app.api import v2_schema
        v2_schema._schema_cache.clear()
        yield
        v2_schema._schema_cache.clear()

    def test_schema_returns_502_on_bq_forbidden_serviceusage(self, reload_db, bq_access):
        """When the BQ extension raises Forbidden mentioning serviceusage, the
        endpoint must translate to HTTP 502 with `cross_project_forbidden`
        and a hint that mentions billing_project."""
        from app.api import v2_schema
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden(
            "Permission denied: serviceusage.services.use on project foo"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            # Endpoint is async — drive it directly. dependency_overrides only
            # fires through TestClient/HTTP, so pass `bq=bq` explicitly.
            with pytest.raises(HTTPException) as exc_info:
                (v2_schema.schema(
                    table_id="bq_view", user=user, conn=conn, bq=bq,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "cross_project_forbidden"
        assert "hint" in detail["details"]
        assert "billing_project" in detail["details"]["hint"].lower()

    def test_schema_returns_502_on_bq_forbidden_non_serviceusage(self, reload_db, bq_access):
        """A Forbidden that is NOT about serviceusage (e.g. dataset-level ACL)
        still becomes a 502, but with `bq_forbidden`."""
        from app.api import v2_schema
        from google.api_core.exceptions import Forbidden

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Forbidden(
            "Access Denied: Dataset foo.bar: User does not have permission"
        )
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                (v2_schema.schema(
                    table_id="bq_view", user=user, conn=conn, bq=bq,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["error"] == "bq_forbidden"

    def test_schema_returns_502_on_bq_bad_request(self, reload_db, bq_access):
        """`/schema` SQL is server-constructed (INFORMATION_SCHEMA.COLUMNS with
        validated identifiers); a BadRequest here means registry corruption →
        upstream error, not user fault. Translate to HTTP 502 (`bq_upstream_error`),
        same as `/sample`, opposite of `/scan*`."""
        from app.api import v2_schema
        from google.api_core.exceptions import BadRequest

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = BadRequest("Syntax error at line 1, column 5")
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            with pytest.raises(HTTPException) as exc_info:
                (v2_schema.schema(
                    table_id="bq_view", user=user, conn=conn, bq=bq,
                ))
        finally:
            conn.close()

        assert exc_info.value.status_code == 502
        detail = exc_info.value.detail
        assert isinstance(detail, dict)
        assert detail["error"] == "bq_upstream_error"
        assert "Syntax error" in detail["message"]

    def test_schema_returns_200_with_empty_partition_on_table_options_failure(
        self, reload_db, monkeypatch,
    ):
        """REGRESSION GUARD: `_fetch_bq_table_options` is best-effort
        (`try/except Exception → return {}` with a logger.warning). On internal
        failure the helper returns `{}`; the endpoint MUST still return 200 with
        the column list and `partition_by=None, clustered_by=[]`.

        Phase 2 preserved the swallow-all contract — this test verifies it
        end-to-end with the helper patched to return the empty dict it would
        yield on failure, AND a Phase-2-specific regression guard that exercises
        the real helper raising an upstream error and confirms it's swallowed
        (not surfaced as 502)."""
        from app.api import v2_schema

        # Strict path returns real schema.
        monkeypatch.setattr(
            v2_schema, "_fetch_bq_schema",
            lambda bq, dataset, table, **kw: [
                {"name": "event_date", "type": "DATE", "nullable": False, "description": ""},
                {"name": "country_code", "type": "STRING", "nullable": True, "description": ""},
            ],
        )
        # Best-effort path returns `{}` (the documented swallow-all output).
        monkeypatch.setattr(v2_schema, "_fetch_bq_table_options", lambda bq, dataset, table, **kw: {})

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            data = (v2_schema.schema(
                table_id="bq_view", user=user, conn=conn, bq=_bq(),
            ))
        finally:
            conn.close()

        # Endpoint returns 200 (no HTTPException raised) with columns present.
        assert data["table_id"] == "bq_view"
        assert {c["name"] for c in data["columns"]} == {"event_date", "country_code"}
        # Partition info absent or None (the swallow-all returns {} on failure).
        assert data.get("partition_by") is None
        assert data.get("clustered_by") == []

    def test_table_options_swallows_bq_errors_returns_empty_dict(self, reload_db, bq_access):
        """REGRESSION GUARD (Phase 2 production path): `_fetch_bq_table_options`
        must swallow ANY exception from BQ and return `{}`. Calling it directly
        with a bq whose duckdb_session().execute() raises Forbidden / BadRequest /
        unknown — all must produce `{}`, NOT a BqAccessError that the endpoint
        would 502 on. This is the load-bearing contract for `/schema` to keep
        returning 200 on permissioned tables / cross-project misconfigurations."""
        from app.api import v2_schema
        from google.api_core.exceptions import Forbidden, BadRequest

        for exc in [
            Forbidden("Permission denied: serviceusage.services.use on project foo"),
            Forbidden("Access Denied: Dataset foo.bar"),
            BadRequest("Syntax error"),
            RuntimeError("totally unexpected"),
        ]:
            mock_conn = MagicMock()
            mock_conn.execute.side_effect = exc
            bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")
            assert v2_schema._fetch_bq_table_options(bq, "ds", "bq_view") == {}, (
                f"swallow-all contract violated for {type(exc).__name__}: {exc}"
            )

    def test_schema_passes_billing_project_to_bigquery_query(self, reload_db, bq_access):
        """Regression guard: bq.projects.billing must be passed to bigquery_query()
        as the billing project (positional arg 0), and the FROM clause must
        reference bq.projects.data (NOT billing). Verifies the migration didn't
        regress the cross-project bug fix from Phase 1."""
        from app.api import v2_schema

        captured = {}

        def _fake_execute(sql, params):
            if "bigquery_query" in sql:
                # _fetch_bq_schema executes first; capture once.
                if "bq_sql" not in captured:
                    captured["billing_project"] = params[0]
                    captured["bq_sql"] = params[1]
            result = MagicMock()
            result.fetchall.return_value = []
            return result

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = _fake_execute
        bq = bq_access(duckdb_conn=mock_conn, billing="billing-proj", data="data-proj")

        conn = reload_db.get_system_db()
        try:
            _seed_bq_table(conn)
            user = {"id": "admin1", "email": "a@x.com"}
            (v2_schema.schema(
                table_id="bq_view", user=user, conn=conn, bq=bq,
            ))
        finally:
            conn.close()

        assert captured["billing_project"] == "billing-proj"
        # FROM clause uses data project (where INFORMATION_SCHEMA.COLUMNS lives)
        assert "`data-proj.ds.INFORMATION_SCHEMA.COLUMNS`" in captured["bq_sql"]


class TestBuildSchemaUncached:
    """The uncached entry point exists for warmup, which has no user
    context. RBAC + cache check live in `build_schema`; the BQ work +
    cache write live in `build_schema_uncached`."""

    def test_uncached_function_exists_and_does_not_take_user(self):
        """Signature: build_schema_uncached(conn, table_id, *, bq)"""
        from app.api.v2_schema import build_schema_uncached
        import inspect
        sig = inspect.signature(build_schema_uncached)
        params = list(sig.parameters)
        assert "user" not in params, (
            "build_schema_uncached should NOT require a user — that's "
            "the whole point of the split (warmup has no user)."
        )
        assert "table_id" in params
        assert "bq" in params

    def test_build_schema_delegates_to_uncached(self, monkeypatch):
        """build_schema should call build_schema_uncached after RBAC+cache check."""
        from app.api import v2_schema

        called_with = {}
        def fake_uncached(conn, table_id, *, bq, row=None):
            called_with["table_id"] = table_id
            called_with["row"] = row
            return {"table_id": table_id, "columns": []}

        monkeypatch.setattr(v2_schema, "build_schema_uncached", fake_uncached)
        # Bypass the cache + RBAC for this assertion — both are tested elsewhere.
        monkeypatch.setattr(v2_schema._schema_cache, "get", lambda k, default=None: None)
        monkeypatch.setattr(v2_schema, "can_access_table", lambda u, tid, c: True)

        # Synthetic registry row.
        from unittest.mock import MagicMock
        repo_mock = MagicMock()
        repo_mock.get.return_value = {"id": "x", "source_type": "bigquery"}
        monkeypatch.setattr(v2_schema, "table_registry_repo", lambda: repo_mock)

        v2_schema.build_schema(
            conn=MagicMock(), user={"id": "u"}, table_id="x", bq=MagicMock(),
        )
        assert called_with["table_id"] == "x"

    def test_uncached_raises_notfound_for_unregistered_table(self):
        """Warmup-direct call against an unregistered id raises NotFound,
        not FileNotFoundError or other surprise."""
        from app.api.v2_schema import build_schema_uncached, NotFound
        from unittest.mock import MagicMock

        conn = MagicMock()
        repo_mock = MagicMock()
        repo_mock.get.return_value = None
        # Patch the factory function the implementation imports.
        import app.api.v2_schema as v2_schema_mod
        original = v2_schema_mod.table_registry_repo
        v2_schema_mod.table_registry_repo = lambda: repo_mock
        try:
            with pytest.raises(NotFound):
                build_schema_uncached(conn, "nonexistent", bq=MagicMock())
        finally:
            v2_schema_mod.table_registry_repo = original
