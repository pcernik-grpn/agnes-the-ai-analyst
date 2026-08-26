"""Tests for agnes explore command."""

import json
import pytest
from unittest.mock import patch, MagicMock

from typer.testing import CliRunner
from cli.main import app

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


def _make_local_db(tmp_config):
    """Create a local DuckDB with a sample table for exploration tests."""
    import duckdb

    db_dir = tmp_config / "local" / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
    conn.execute("CREATE TABLE orders (id INTEGER, amount DOUBLE, status VARCHAR)")
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?)",
        [
            (1, 99.5, "shipped"),
            (2, 200.0, "pending"),
            (3, 50.0, "shipped"),
        ],
    )
    conn.close()
    return db_dir / "analytics.duckdb"


class TestExploreLocal:
    def test_explore_existing_table(self, tmp_config):
        """Exploring an existing local table shows row count and columns."""
        _make_local_db(tmp_config)
        result = runner.invoke(app, ["explore", "orders"])
        assert result.exit_code == 0
        assert "orders" in result.output
        assert "3" in result.output  # row count

    def test_explore_no_db(self, tmp_config):
        """Exploring without local DB now falls back to the server under
        the default --scope auto (command-UX standard: default = auto,
        not local-only) instead of just failing. Mocked like every other
        server-path test in this file / test_explore_scope_flag.py — an
        unmocked call here would hit a real (unreachable in CI) server and
        surface as an uncaught AgnesTransportError instead of exercising
        the command's own behavior."""
        profile = {"table": "orders", "row_count": 42}
        with patch("cli.client.api_get", return_value=_resp(200, profile)) as mock_get:
            result = runner.invoke(app, ["explore", "orders"])
        assert result.exit_code == 0, result.output
        assert "[scope] no local data yet" in result.output
        assert "running server-side" in result.output
        mock_get.assert_called_once_with("/api/catalog/profile/orders")

    def test_explore_missing_table(self, tmp_config):
        """A local DB exists but lacks the requested table: --scope auto
        falls back to the server rather than just listing local tables."""
        _make_local_db(tmp_config)
        with patch("cli.client.api_get", return_value=_resp(404, {"detail": "Not found"}, "Not found")) as mock_get:
            result = runner.invoke(app, ["explore", "nonexistent_xyz"])
        assert result.exit_code == 1
        assert "[scope] 'nonexistent_xyz' not found locally" in result.output
        assert "running server-side" in result.output
        mock_get.assert_called_once_with("/api/catalog/profile/nonexistent_xyz")

    def test_explore_json_flag(self, tmp_config):
        """--json flag produces valid JSON with table info.
        Note: with explore's callback pattern, options must precede positional args.
        """
        _make_local_db(tmp_config)
        result = runner.invoke(app, ["explore", "--json", "orders"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["table"] == "orders"
        assert data["row_count"] == 3
        assert len(data["columns"]) == 3
        assert len(data["sample_rows"]) <= 5


class TestExploreRemote:
    def test_explore_remote_success(self):
        """--remote flag fetches catalog profile from server.
        api_get is imported inside _explore_remote so mock the source module.
        """
        profile = {
            "table": "orders",
            "row_count": 1000,
            "columns": [{"name": "id"}, {"name": "amount"}],
        }
        with patch("cli.client.api_get", return_value=_resp(200, profile)):
            result = runner.invoke(app, ["explore", "--remote", "orders"])
        assert result.exit_code == 0
        assert "orders" in result.output

    def test_explore_remote_not_found(self):
        """--remote with unknown table exits with error."""
        with patch("cli.client.api_get", return_value=_resp(404, {"detail": "Not found"}, "Not found")):
            result = runner.invoke(app, ["explore", "--remote", "unknown_table"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "Profile not found" in result.output

    def test_explore_remote_json_flag(self):
        """--remote --json outputs raw API JSON."""
        profile = {"table": "orders", "row_count": 500}
        with patch("cli.client.api_get", return_value=_resp(200, profile)):
            result = runner.invoke(app, ["explore", "--remote", "--json", "orders"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["table"] == "orders"
