"""Tests for `agnes query --scope auto|local|server` and the shared
`cli.query_hints` helper (local→server fallback for `--scope auto`).
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app
from cli.query_hints import (
    column_not_found_hint,
    missing_table,
    remote_table_hint,
    unregistered_table_hint,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGNES_LOCAL_DIR", str(tmp_path / "local"))
    (tmp_path / "config").mkdir()
    (tmp_path / "local").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


class TestMissingTableHelper:
    def test_missing_table_matches_duckdb_catalog_error(self):
        assert missing_table("Table with name unit_economics does not exist") == "unit_economics"

    def test_missing_table_no_match_on_syntax_error(self):
        assert missing_table("Parser Error: syntax error at or near ...") is None

    def test_remote_table_hint_cli_surface(self):
        hint = remote_table_hint("unit_economics", surface="cli")
        assert "agnes query --remote" in hint
        assert "agnes catalog" in hint
        assert "agnes schema" in hint

    def test_remote_table_hint_mcp_surface(self):
        hint = remote_table_hint("unit_economics", surface="mcp")
        assert "query" in hint
        assert "agnes query --remote" not in hint


class TestColumnAndUnregisteredTableHintsReExported:
    """`column_not_found_hint`/`unregistered_table_hint` are defined once in
    `src.query_error_hints` (importable from both `app/api/query.py` and this
    CLI-only wheel), and re-exported here so `cli.query_hints` stays the one
    place CLI/stdio-MCP code reaches for a query "not found" hint."""

    def test_column_not_found_hint_is_reachable_from_cli_query_hints(self):
        hint = column_not_found_hint(
            'Binder Error: Table "s" does not have a column named "X"',
            "SELECT s.X FROM orders s",
        )
        assert hint is not None
        assert "orders" in hint
        assert "schema orders" in hint

    def test_unregistered_table_hint_is_reachable_from_cli_query_hints(self):
        hint = unregistered_table_hint("Catalog Error: Table with name typo_table does not exist!")
        assert hint is not None
        assert "catalog" in hint


class TestScopeAutoFallback:
    def test_auto_falls_back_to_remote_when_no_local_db(self, tmp_config):
        """`--scope auto` (the default) with no local DuckDB yet prints a
        one-line note and runs the query server-side."""
        payload = {"columns": ["id"], "rows": [[1]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)) as mock_post:
            result = runner.invoke(app, ["query", "SELECT 1 AS id"])
        assert result.exit_code == 0
        assert "[scope] no local data yet" in result.output
        assert "running server-side" in result.output
        assert mock_post.call_args.kwargs["json"]["sql"] == "SELECT 1 AS id"

    def test_auto_falls_back_to_remote_on_local_table_miss(self, tmp_config):
        """A local DB exists but doesn't have the queried table — `--scope
        auto` falls back to server-side instead of failing."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        payload = {"columns": ["x"], "rows": [[1]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)) as mock_post:
            result = runner.invoke(app, ["query", "SELECT * FROM unit_economics"])
        assert result.exit_code == 0
        assert "'unit_economics' not found locally" in result.output
        assert "running server-side" in result.output
        assert mock_post.call_args.kwargs["json"]["sql"] == "SELECT * FROM unit_economics"

    def test_auto_does_not_fall_back_on_local_success(self, tmp_config):
        """When the local query succeeds, `--scope auto` behaves exactly
        like a plain local query — no server call, no [scope] note."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE nums (n INTEGER)")
        conn.execute("INSERT INTO nums VALUES (1), (2), (3)")
        conn.close()

        with patch("cli.client.api_post") as mock_post:
            result = runner.invoke(app, ["query", "SELECT SUM(n) AS total FROM nums", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["total"] == 6
        mock_post.assert_not_called()
        assert "[scope]" not in result.output

    def test_auto_does_not_fall_back_on_non_table_miss_error(self, tmp_config):
        """A non-missing-table local failure (syntax error) fails exactly
        like `--scope local` — no fallback to the server."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        with patch("cli.client.api_post") as mock_post:
            result = runner.invoke(app, ["query", "SELECT * FROM"])
        assert result.exit_code == 1
        assert "Query error" in result.output
        mock_post.assert_not_called()


class TestScopeLocalNoFallback:
    def test_scope_local_keeps_existing_hint_no_fallback(self, tmp_config):
        """`--scope local` preserves today's behavior exactly: error + hint,
        no server-side fallback."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        with patch("cli.client.api_post") as mock_post:
            result = runner.invoke(app, ["query", "DESCRIBE unit_economics", "--scope", "local"])
        assert result.exit_code == 1
        assert "Query error" in result.output
        assert "Table with name unit_economics does not exist" in result.output
        assert "query_mode='remote'" in result.output
        assert "agnes query --remote" in result.output
        mock_post.assert_not_called()

    def test_local_flag_shorthand_behaves_like_scope_local(self, tmp_config):
        """`--local` is shorthand for `--scope local`: no fallback."""
        result = runner.invoke(app, ["query", "SELECT 1", "--local"])
        assert result.exit_code == 1
        out = result.output.lower()
        assert "--remote" in out
        assert "agnes pull" in out


class TestScopeConflicts:
    def test_remote_and_local_conflict(self):
        result = runner.invoke(app, ["query", "SELECT 1", "--remote", "--local"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_remote_and_explicit_scope_local_conflict(self):
        result = runner.invoke(app, ["query", "SELECT 1", "--remote", "--scope", "local"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_local_and_explicit_scope_server_conflict(self):
        result = runner.invoke(app, ["query", "SELECT 1", "--local", "--scope", "server"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_invalid_scope_value_rejected(self):
        result = runner.invoke(app, ["query", "SELECT 1", "--scope", "bogus"])
        assert result.exit_code == 1
        assert "--scope" in result.output

    def test_remote_flag_still_forces_server_scope(self):
        """`--remote` alone (no explicit --scope) still routes to the
        server, unaffected by the new default scope=auto."""
        payload = {"columns": ["id"], "rows": [[1]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)) as mock_post:
            result = runner.invoke(app, ["query", "SELECT 1 AS id", "--remote"])
        assert result.exit_code == 0
        mock_post.assert_called_once()
