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
