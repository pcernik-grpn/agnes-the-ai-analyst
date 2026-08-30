"""Soft-enforce semantic validation on POST /api/query (consumption loop, block 2).

Product decision: enforcement is SOFT. A violation is a *warning* attached to
an otherwise untouched 200 — never a block, never a different status code,
never a missing row. The field is present only when there is something worth
saying (an error-severity constraint violation, or a used metric with no
expression for the engine the statement just ran on), so a clean query and an
instance with no semantic layer both look exactly as they did before.

Everything below drives the real endpoint over a real DuckDB (the seeded
`orders` extract), plus the two consumer surfaces that have to carry the
field onward: the MCP `query` passthrough and `agnes query`'s stderr note.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner as ClickCliRunner
from typer.main import get_command

from cli.main import app as _cli_app

_click_app = get_command(_cli_app)


# A model whose `revenue` metric carries an error-severity required_filter
# constraint the test SQL does not satisfy, and which declares only a
# BigQuery expression (so `locally_executable` is False against duckdb).
def _document(*, with_constraint: bool = True, dialect: str = "bigquery") -> dict:
    document: dict = {
        "name": "retail",
        "datasets": [{"name": "orders", "source": "db.orders", "fields": [{"name": "amount"}]}],
        "metrics": [
            {
                "name": "revenue",
                "dataset": "orders",
                "expression": {"dialects": [{"dialect": dialect, "expression": "SUM(amount)"}]},
            }
        ],
    }
    if with_constraint:
        document["custom_extensions"] = [
            {
                "vendor_name": "agnes",
                "data": {
                    "constraints": [
                        {
                            "name": "revenue_needs_tenant_filter",
                            "constraint_type": "required_filter",
                            "rule": "tenant_id = current_tenant()",
                            "severity": "error",
                            "metrics": ["revenue"],
                        }
                    ]
                },
            }
        ]
    return document


def _seed_model(*, slug: str = "retail", **kwargs) -> dict:
    from src.repositories import semantic_model_repo

    document = _document(**kwargs)
    return semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description="Retail orders and revenue.",
        document=f"version: '0.2.0.dev0'\nsemantic_model:\n  - name: {slug}\n",
        document_json={"semantic_model": [document]},
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


@pytest.fixture
def orders_app(seeded_app, mock_extract_factory):
    """The seeded app with one queryable local `orders` table (no semantic
    model yet — each test seeds the model shape it needs)."""
    from src.db import get_system_db
    from src.orchestrator import SyncOrchestrator
    from src.repositories.table_registry import TableRegistryRepository

    env = seeded_app["env"]
    mock_extract_factory(
        "keboola",
        [
            {
                "name": "orders",
                "data": [
                    {"id": "1", "amount": "100", "revenue": "100"},
                    {"id": "2", "amount": "150", "revenue": "150"},
                ],
            }
        ],
    )
    SyncOrchestrator(analytics_db_path=env["analytics_db"]).rebuild()

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id="orders",
            name="orders",
            source_type="keboola",
            query_mode="local",
        )
    finally:
        conn.close()
    return seeded_app


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _query(app, token: str, sql: str = "SELECT revenue FROM orders"):
    return app["client"].post("/api/query", json={"sql": sql}, headers=_auth(token))


# ── REST: the field itself ────────────────────────────────────────────────


class TestQueryResponseField:
    def test_warns_on_an_error_severity_constraint_violation(self, orders_app):
        _seed_model()
        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        body = r.json()
        # Soft: the result is untouched.
        assert body["row_count"] == 2, body
        sv = body["semantic_validation"]
        assert sv is not None, body
        assert sv["valid"] is False
        assert any("revenue_needs_tenant_filter" in w for w in sv["warnings"]), sv
        assert sv["used_metrics"] == ["revenue"]

    def test_warns_when_a_used_metric_is_not_locally_executable(self, orders_app):
        """No constraint at all, but the only declared dialect is BigQuery
        while the statement ran on DuckDB — the number is not reproducible."""
        _seed_model(with_constraint=False)
        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        sv = r.json()["semantic_validation"]
        assert sv is not None
        assert sv["locally_executable"] is False
        assert sv["warnings"], sv

    def test_omitted_when_the_query_is_clean(self, orders_app):
        """A model whose metric runs on the local engine and carries no
        violated constraint says nothing."""
        _seed_model(with_constraint=False, dialect="duckdb")
        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        assert r.json()["semantic_validation"] is None

    def test_omitted_when_the_instance_has_no_semantic_model(self, orders_app):
        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        assert r.json()["semantic_validation"] is None

    def test_omitted_for_a_user_who_cannot_read_any_model(self, orders_app):
        """RBAC matches the rest of the read surface (`_can_read_model`): a
        model with no package/direct grant is invisible to a non-admin, so
        the warning it would have produced is invisible too."""
        from app.auth.jwt import create_access_token
        from src.db import get_system_db
        from src.repositories.users import UserRepository
        from tests.conftest import grant_table_via_package

        _seed_model()
        conn = get_system_db()
        try:
            UserRepository(conn).create(id="u_plain", email="plain@example.com", name="Plain")
            grant_table_via_package(conn, "orders", "u_plain", group_name="OrdersReaders")
        finally:
            conn.close()

        token = create_access_token("u_plain", "plain@example.com")
        r = _query(orders_app, token)
        assert r.status_code == 200, r.text
        assert r.json()["row_count"] == 2
        assert r.json()["semantic_validation"] is None

    def test_a_validator_failure_never_breaks_the_query(self, orders_app, monkeypatch):
        """Any internal failure of this step is swallowed: the query result
        is what the caller asked for, minus the advisory field."""
        import app.api.semantic_models as sm_mod

        _seed_model()

        def _boom(*_a, **_kw):
            raise RuntimeError("validator exploded")

        monkeypatch.setattr(sm_mod, "validate_query", _boom)
        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2
        assert body["semantic_validation"] is None

    def test_field_is_part_of_the_wire_contract(self):
        from app.api.query import QueryResponse

        assert "semantic_validation" in QueryResponse.model_json_schema()["properties"]
        assert QueryResponse(columns=[], rows=[], row_count=0).semantic_validation is None


# ── MCP: the passthrough carries it ───────────────────────────────────────


class TestMcpQueryPassthrough:
    def test_semantic_validation_survives_the_mcp_query_tool(self):
        import asyncio
        from unittest.mock import AsyncMock

        pytest.importorskip("mcp", reason="mcp package not installed")
        import app.api.mcp_http as mod

        payload = {
            "columns": ["revenue"],
            "rows": [[100]],
            "truncated": False,
            "semantic_validation": {"valid": False, "warnings": ["constraint 'x' violated"]},
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            result = asyncio.run(mod.query("SELECT revenue FROM orders"))

        assert result["semantic_validation"] == payload["semantic_validation"]

    def _run_query_tool(self, payload):
        import asyncio
        from unittest.mock import AsyncMock

        pytest.importorskip("mcp", reason="mcp package not installed")
        import app.api.mcp_http as mod

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = payload
        resp.raise_for_status = MagicMock()

        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            MC.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
            return asyncio.run(mod.query("SELECT revenue FROM orders"))

    def test_an_advisory_never_pushes_a_deliverable_result_over_the_output_cap(self, monkeypatch):
        """`ensure_output_size` RAISES rather than truncating, so an advisory
        bolted onto a borderline result could fail a query that would have
        succeeded a day earlier. An advisory must never do that: it is capped,
        then dropped, before the rows are."""
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "4000")
        rows = [[i, "x" * 40] for i in range(60)]  # ~3.5k chars of real result
        payload = {
            "columns": ["id", "blob"],
            "rows": rows,
            "truncated": False,
            "semantic_validation": {
                "valid": False,
                "warnings": [f"constraint 'c{i}' violated" for i in range(40)],
                "violations": [{"name": f"c{i}", "reason": "y" * 200} for i in range(40)],
                "post_execution_checks": [{"name": f"p{i}", "reason": "z" * 200} for i in range(40)],
                "used_metrics": ["revenue"],
                "used_datasets": ["orders"],
                "summary": "s" * 500,
                "detection": "best-effort text match",
                "locally_executable": False,
            },
        }
        result = self._run_query_tool(payload)

        assert result["rows"] == rows, "the rows the caller asked for are untouched"
        sv = result.get("semantic_validation")
        if sv is not None:
            assert len(sv["warnings"]) < 40
            assert sv.get("truncated") is True

    def test_a_result_too_large_on_its_own_still_raises(self, monkeypatch):
        """Dropping the advisory does not turn the pre-existing oversize guard
        off — a genuinely huge result must still tell the agent to narrow."""
        from src.mcp_tooling import MCPOutputTooLarge

        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "2000")
        payload = {
            "columns": ["blob"],
            "rows": [["x" * 100] for _ in range(200)],
            "truncated": False,
            "semantic_validation": {"valid": False, "warnings": ["w"]},
        }
        with pytest.raises(MCPOutputTooLarge):
            self._run_query_tool(payload)

    def test_a_small_response_keeps_the_full_advisory(self, monkeypatch):
        monkeypatch.setenv("AGNES_MCP_MAX_OUTPUT_CHARS", "100000")
        payload = {
            "columns": ["revenue"],
            "rows": [[100]],
            "truncated": False,
            "semantic_validation": {"valid": False, "warnings": ["a", "b"], "violations": [{"name": "c"}]},
        }
        result = self._run_query_tool(payload)
        assert result["semantic_validation"] == payload["semantic_validation"]


# ── CLI: `[semantic]` note on stderr ──────────────────────────────────────


class TestCliSemanticNote:
    @pytest.fixture(autouse=True)
    def _tmp_config(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
        (tmp_path / "config").mkdir()
        (tmp_path / "local").mkdir()

    @staticmethod
    def _resp(json_data):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = json_data
        return r

    _WARNED = {
        "columns": ["revenue"],
        "rows": [[100]],
        "truncated": False,
        "semantic_validation": {
            "valid": False,
            "warnings": [
                "constraint 'revenue_needs_tenant_filter' on metric 'revenue': required filter not found",
                "metric 'revenue' has no expression for duckdb",
            ],
        },
    }

    def test_each_warning_is_printed_to_stderr(self):
        with patch("cli.client.api_post", return_value=self._resp(self._WARNED)):
            result = ClickCliRunner().invoke(_click_app, ["query", "SELECT revenue FROM orders", "--remote"])
        assert result.exit_code == 0
        assert result.stderr.count("[semantic]") == 2
        assert "revenue_needs_tenant_filter" in result.stderr

    def test_no_note_when_the_field_is_absent(self):
        payload = {"columns": ["x"], "rows": [[1]], "truncated": False, "semantic_validation": None}
        with patch("cli.client.api_post", return_value=self._resp(payload)):
            result = ClickCliRunner().invoke(_click_app, ["query", "SELECT x FROM t", "--remote"])
        assert result.exit_code == 0
        assert "[semantic]" not in result.stderr

    def test_json_stdout_stays_pure(self):
        with patch("cli.client.api_post", return_value=self._resp(self._WARNED)):
            result = ClickCliRunner().invoke(
                _click_app, ["query", "SELECT revenue FROM orders", "--remote", "--format", "json"]
            )
        assert result.exit_code == 0
        assert json.loads(result.stdout.strip()) == [{"revenue": 100}]
        assert "[semantic]" not in result.stdout
        assert "[semantic]" in result.stderr


# ── Which engine the advisory is judged against ───────────────────────────


def _seed_raw_model(document: dict, *, slug: str = "retail") -> dict:
    """Seed one valid model from a hand-built model dict."""
    from src.repositories import semantic_model_repo

    return semantic_model_repo().upsert(
        id=f"manual/_/{slug}",
        slug=slug,
        name=slug,
        description=None,
        document=f"version: '0.2.0.dev0'\nsemantic_model:\n  - name: {slug}\n",
        document_json={"semantic_model": [document]},
        spec_version="0.2.0.dev0",
        content_hash=f"hash-{slug}",
        source="manual",
        source_ref=None,
        status="valid",
        validation_errors=None,
        validated_at=None,
    )


class _StubAnalytics:
    """Records every SQL that reaches DuckDB; optionally raises a BQ-style
    parse error for the rewritten statement so the fallback path fires."""

    description = [("c0",)]

    def __init__(self, *, fail_on_bigquery_query: bool = False):
        self.sqls: list[str] = []
        self._fail = fail_on_bigquery_query

    def execute(self, sql, *args, **kwargs):
        self.sqls.append(sql)
        if self._fail and "bigquery_query(" in sql:
            raise RuntimeError("BinderException: Query execution failed: Syntax error: Unexpected token at [1:42]")

        class _R:
            def fetchmany(self, _n):
                return [(1,)]

        return _R()

    def close(self):
        pass


@pytest.fixture
def engine_probe(monkeypatch):
    """Capture the ``target_engine`` the endpoint judged the statement on."""
    import app.api.semantic_models as sm_mod

    seen: dict = {"engine": None, "calls": 0}

    def _spy(sql, user, conn, *, target_engine="duckdb"):
        seen["engine"] = target_engine
        seen["calls"] += 1

    monkeypatch.setattr(sm_mod, "semantic_validation_for_query", _spy)
    return seen


@pytest.fixture
def stub_bq(monkeypatch):
    monkeypatch.setattr("app.api.query._bq_dry_run_bytes", lambda *a, **k: 1024, raising=False)

    class _FakeProjects:
        data = "test-data-prj"
        billing = "test-billing-prj"

    class _FakeBqAccess:
        projects = _FakeProjects()

    monkeypatch.setattr("app.api.query.get_bq_access", lambda: _FakeBqAccess(), raising=False)


def _register_bq_remote(name: str, bucket: str, source_table: str) -> None:
    from src.db import get_system_db
    from src.repositories.table_registry import TableRegistryRepository

    conn = get_system_db()
    try:
        TableRegistryRepository(conn).register(
            id=f"bq.{bucket}.{source_table}",
            name=name,
            source_type="bigquery",
            bucket=bucket,
            source_table=source_table,
            query_mode="remote",
        )
    finally:
        conn.close()


class TestEngineLabel:
    """The advisory's `locally_executable` verdict is only as good as the
    engine label it is judged against — a metric declared BigQuery-only is
    fine on a statement BigQuery composed and wrong on one DuckDB ran. The
    label must therefore come from what ACTUALLY composed the statement, not
    from "a BQ table was mentioned somewhere"."""

    def test_all_local_query_is_judged_on_duckdb(self, orders_app, engine_probe):
        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        assert engine_probe["engine"] == "duckdb"

    def test_bigquery_pushdown_is_judged_on_bigquery(self, orders_app, engine_probe, stub_bq, monkeypatch):
        """The rewrite fired and BQ composed the whole statement."""
        _register_bq_remote("ue", "fin", "ue")
        monkeypatch.setattr("app.api.query.get_analytics_db_readonly", lambda: _StubAnalytics(), raising=False)

        r = orders_app["client"].post(
            "/api/query",
            json={"sql": "SELECT count(*) FROM ue WHERE country = 'CZ'"},
            headers=_auth(orders_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert engine_probe["engine"] == "bigquery"

    def test_cross_source_join_bigquery_never_composed_is_judged_on_duckdb(
        self, orders_app, engine_probe, stub_bq, monkeypatch
    ):
        """A JOIN across a BQ-remote and a local table makes the rewriter bail
        (`did_rewrite=False`) and DuckDB's ATTACH-catalog path run the SQL —
        so a DuckDB-only metric must NOT be reported as unexecutable, and a
        BigQuery-only one must not pass silently."""
        _register_bq_remote("ue", "fin", "ue")
        stub = _StubAnalytics()
        monkeypatch.setattr("app.api.query.get_analytics_db_readonly", lambda: stub, raising=False)

        r = orders_app["client"].post(
            "/api/query",
            json={"sql": "SELECT count(*) FROM ue JOIN orders ON ue.id = orders.id"},
            headers=_auth(orders_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert not any("bigquery_query(" in s for s in stub.sqls), "precondition: the rewriter must have bailed"
        assert engine_probe["engine"] == "duckdb"

    def test_bigquery_parse_error_fallback_is_judged_on_duckdb(self, orders_app, engine_probe, stub_bq, monkeypatch):
        """The rewrite fired, BQ refused it, and the statement re-ran through
        DuckDB. The numbers came from DuckDB, so the verdict must too."""
        _register_bq_remote("ue", "fin", "ue")
        stub = _StubAnalytics(fail_on_bigquery_query=True)
        monkeypatch.setattr("app.api.query.get_analytics_db_readonly", lambda: stub, raising=False)

        r = orders_app["client"].post(
            "/api/query",
            json={"sql": "SELECT (count(*))::INT FROM ue"},
            headers=_auth(orders_app["admin_token"]),
        )
        assert r.status_code == 200, r.text
        assert len(stub.sqls) == 2, "precondition: rewrite raised, then fell back"
        assert engine_probe["engine"] == "duckdb"

    def test_databricks_plan_is_judged_on_databricks(self, orders_app, engine_probe, monkeypatch):
        monkeypatch.setattr("app.api.query._databricks_remote_plan", lambda *a, **k: {"sql": "SELECT 1"}, raising=False)
        monkeypatch.setattr(
            "app.api.query._execute_databricks_plan",
            lambda *a, **k: (["c0"], [(1,)], False, 128),
            raising=False,
        )
        r = _query(orders_app, orders_app["admin_token"], sql="SELECT revenue FROM dbx_orders")
        assert r.status_code == 200, r.text
        assert engine_probe["engine"] == "databricks"


# ── The gate must stay cheap ──────────────────────────────────────────────


class TestExistenceGateCost:
    def test_no_valid_model_never_loads_a_document(self, orders_app, monkeypatch):
        """Every query pays this gate. With no semantic layer it must cost one
        COUNT — not a `list_all()` that drags `document` + `document_json` for
        every row."""
        from src.repositories.semantic_models import SemanticModelsRepository

        calls = {"list_all": 0, "count_valid": 0}
        real_list_all = SemanticModelsRepository.list_all
        real_count = SemanticModelsRepository.count_valid

        def _list_all(self, **kw):
            calls["list_all"] += 1
            return real_list_all(self, **kw)

        def _count(self):
            calls["count_valid"] += 1
            return real_count(self)

        monkeypatch.setattr(SemanticModelsRepository, "list_all", _list_all)
        monkeypatch.setattr(SemanticModelsRepository, "count_valid", _count)

        r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        assert calls["count_valid"] == 1
        assert calls["list_all"] == 0


# ── What the warning actually says ────────────────────────────────────────


def _two_metric_document() -> dict:
    return {
        "name": "retail",
        "datasets": [{"name": "orders", "source": "db.orders", "fields": [{"name": "amount"}]}],
        "metrics": [
            {
                "name": "revenue",
                "dataset": "orders",
                "expression": {"dialects": [{"dialect": "duckdb", "expression": "SUM(amount)"}]},
            },
            {
                "name": "margin",
                "dataset": "orders",
                "expression": {"dialects": [{"dialect": "bigquery", "expression": "SUM(amount) * 0.3"}]},
            },
        ],
    }


class TestWarningText:
    def test_only_the_unexecutable_metric_is_named(self, orders_app):
        """`revenue` composes on DuckDB and `margin` does not — naming both
        accuses a metric that is perfectly fine."""
        _seed_raw_model(_two_metric_document())
        r = _query(
            orders_app,
            orders_app["admin_token"],
            sql="SELECT revenue, amount AS margin FROM orders",
        )
        assert r.status_code == 200, r.text
        sv = r.json()["semantic_validation"]
        assert sv is not None
        assert sv["not_executable_metrics"] == ["margin"]
        dialect_warning = [w for w in sv["warnings"] if "no expression declared" in w]
        assert len(dialect_warning) == 1, sv["warnings"]
        assert "margin" in dialect_warning[0]
        assert "revenue" not in dialect_warning[0], dialect_warning[0]

    def test_the_advisory_says_the_match_is_textual(self, orders_app):
        """Detection is a best-effort name match, not SQL parsing — a column
        that happens to share a metric's name matches too. The payload and the
        warning both say so, or a heuristic hit reads as a confirmed
        violation."""
        _seed_model()
        r = _query(orders_app, orders_app["admin_token"])
        sv = r.json()["semantic_validation"]
        assert "text match" in sv["detection"].lower()
        assert all("best-effort text match" in w for w in sv["warnings"]), sv["warnings"]


# ── post-execution checks travel as information ───────────────────────────


class TestPostExecutionChecks:
    def test_they_are_forwarded_but_never_evaluated(self, orders_app):
        """Issue #1707 decision 7: a rule that cannot be checked before running
        is allowed, not validated, and surfaced as information. This caller
        runs AFTER execution, so dropping them threw away the only list the
        analyst could act on — but it still must not guess a verdict."""
        document = _document()
        document["custom_extensions"][0]["data"]["constraints"].append(
            {
                "name": "revenue_non_negative",
                "constraint_type": "value_range",
                "rule": "revenue >= 0",
                "severity": "warning",
                "metrics": ["revenue"],
            }
        )
        _seed_raw_model(document)

        r = _query(orders_app, orders_app["admin_token"])
        sv = r.json()["semantic_validation"]
        assert [c["name"] for c in sv["post_execution_checks"]] == ["revenue_non_negative"]
        # Information, not a verdict: it neither becomes a warning line nor
        # touches `valid`.
        assert not any("revenue_non_negative" in w for w in sv["warnings"]), sv["warnings"]

    def test_a_post_execution_check_alone_never_raises_the_advisory(self, orders_app):
        """Nothing else to say → no field at all. Otherwise the advisory would
        appear on every query touching a metric with an unverifiable rule, and
        an agent learns to ignore a field that is always there."""
        document = _two_metric_document()
        document["metrics"] = [document["metrics"][0]]  # duckdb-only; executable
        document["custom_extensions"] = [
            {
                "vendor_name": "agnes",
                "data": {
                    "constraints": [
                        {
                            "name": "revenue_non_negative",
                            "constraint_type": "value_range",
                            "rule": "revenue >= 0",
                            "severity": "warning",
                            "metrics": ["revenue"],
                        }
                    ]
                },
            }
        ]
        _seed_raw_model(document)
        r = _query(orders_app, orders_app["admin_token"])
        assert r.json()["semantic_validation"] is None


# ── A failing advisory is a log line, not a traceback per query ────────────


class TestFailureLogging:
    def test_a_systemic_failure_logs_a_warning_not_a_traceback(self, orders_app, monkeypatch, caplog):
        """The failure mode here is systemic (a half-migrated database, an
        unreadable document) — it fires on EVERY query. A full traceback per
        query buries the log it is trying to explain."""
        import logging

        import app.api.semantic_models as sm_mod

        _seed_model()

        def _boom(*_a, **_kw):
            raise RuntimeError("validator exploded")

        monkeypatch.setattr(sm_mod, "validate_query", _boom)
        with caplog.at_level(logging.WARNING, logger="app.api.query"):
            r = _query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200
        records = [rec for rec in caplog.records if "semantic validation failed" in rec.getMessage()]
        assert records, caplog.text
        assert records[0].levelno == logging.WARNING
        assert records[0].exc_info is None, "no per-query traceback"
        assert "validator exploded" in records[0].getMessage(), "the error itself must still be in the line"


# ── POST /api/query/hybrid gets the same advisory (P2-1) ───────────────────


def _hybrid_query(app, token: str, sql: str = "SELECT revenue FROM orders"):
    return app["client"].post(
        "/api/query/hybrid",
        json={"sql": sql, "register_bq": {}},
        headers=_auth(token),
    )


class TestHybridQueryResponseField:
    """`POST /api/query/hybrid` (`app/api/query_hybrid.py`) used to skip the
    semantic advisory entirely — the one query path that combines BigQuery
    and local data said nothing about a violated constraint or a
    not-locally-executable metric, unlike the plain `/api/query` endpoint.
    Mirrors `TestQueryResponseField`'s cases against the hybrid endpoint."""

    def test_warns_on_an_error_severity_constraint_violation(self, orders_app):
        _seed_model()
        r = _hybrid_query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2, body
        sv = body["semantic_validation"]
        assert sv is not None, body
        assert sv["valid"] is False
        assert any("revenue_needs_tenant_filter" in w for w in sv["warnings"]), sv

    def test_warns_when_a_used_metric_is_not_locally_executable(self, orders_app):
        _seed_model(with_constraint=False)
        r = _hybrid_query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        sv = r.json()["semantic_validation"]
        assert sv is not None
        assert sv["locally_executable"] is False

    def test_omitted_when_the_query_is_clean(self, orders_app):
        _seed_model(with_constraint=False, dialect="duckdb")
        r = _hybrid_query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        assert r.json()["semantic_validation"] is None

    def test_omitted_when_the_instance_has_no_semantic_model(self, orders_app):
        r = _hybrid_query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        assert r.json()["semantic_validation"] is None

    def test_a_validator_failure_never_breaks_the_hybrid_query(self, orders_app, monkeypatch):
        import app.api.semantic_models as sm_mod

        _seed_model()

        def _boom(*_a, **_kw):
            raise RuntimeError("validator exploded")

        monkeypatch.setattr(sm_mod, "validate_query", _boom)
        r = _hybrid_query(orders_app, orders_app["admin_token"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["row_count"] == 2
        assert body["semantic_validation"] is None
