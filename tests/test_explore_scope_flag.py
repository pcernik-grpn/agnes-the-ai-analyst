"""Tests for `agnes explore --scope auto|local|server` (B4).

`agnes explore` used to be local-only-by-default with a boolean `--remote`
flag — a command-UX-standard violation (canonical: `--scope auto|local|
server`, `--remote`/`--local` frozen legacy shorthands, default = auto).
Mirrors `tests/test_cli_query_scope.py`'s coverage of the same contract on
`agnes query`.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
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
    import duckdb

    db_dir = tmp_config / "local" / "user" / "duckdb"
    db_dir.mkdir(parents=True)
    conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
    conn.execute("CREATE TABLE orders (id INTEGER, amount DOUBLE, status VARCHAR)")
    conn.executemany(
        "INSERT INTO orders VALUES (?, ?, ?)",
        [(1, 99.5, "shipped"), (2, 200.0, "pending"), (3, 50.0, "shipped")],
    )
    conn.close()


class TestScopeServer:
    def test_scope_server_routes_to_remote(self):
        """`--scope server` fetches the catalog profile from the server,
        exactly like the legacy `--remote` flag."""
        profile = {"table": "orders", "row_count": 1000, "columns": [{"name": "id"}]}
        with patch("cli.client.api_get", return_value=_resp(200, profile)) as mock_get:
            result = runner.invoke(app, ["explore", "--scope", "server", "orders"])
        assert result.exit_code == 0
        assert "orders" in result.output
        mock_get.assert_called_once_with("/api/catalog/profile/orders")

    def test_remote_flag_maps_to_scope_server(self):
        """`--remote` is a frozen alias for `--scope server` — still works."""
        profile = {"table": "orders", "row_count": 1000, "columns": [{"name": "id"}]}
        with patch("cli.client.api_get", return_value=_resp(200, profile)) as mock_get:
            result = runner.invoke(app, ["explore", "--remote", "orders"])
        assert result.exit_code == 0
        assert "orders" in result.output
        mock_get.assert_called_once_with("/api/catalog/profile/orders")

    def test_scope_server_and_remote_agree(self, tmp_config):
        """`--scope server` and `--remote` hit the server even when a local
        DB with the same table exists — no accidental local read."""
        _make_local_db(tmp_config)
        profile = {"table": "orders", "row_count": 999999}
        with patch("cli.client.api_get", return_value=_resp(200, profile)) as mock_get:
            result = runner.invoke(app, ["explore", "--scope", "server", "orders"])
        assert result.exit_code == 0
        assert "999999" in result.output
        mock_get.assert_called_once()


class TestScopeAutoDefault:
    def test_default_scope_is_auto_uses_local_when_available(self, tmp_config):
        """No `--scope`/`--remote` given, a local DB with the table exists:
        behaves like `--scope local` — no server call, no [scope] note."""
        _make_local_db(tmp_config)
        with patch("cli.client.api_get") as mock_get:
            result = runner.invoke(app, ["explore", "orders"])
        assert result.exit_code == 0
        assert "orders" in result.output
        mock_get.assert_not_called()
        assert "[scope]" not in result.output

    def test_auto_falls_back_to_server_when_no_local_db(self):
        """Default scope=auto with no local DuckDB yet: falls back to the
        server instead of just failing (the old default-local behavior)."""
        profile = {"table": "orders", "row_count": 42}
        with patch("cli.client.api_get", return_value=_resp(200, profile)) as mock_get:
            result = runner.invoke(app, ["explore", "orders"])
        assert result.exit_code == 0
        assert "[scope] no local data yet" in result.output
        assert "running server-side" in result.output
        mock_get.assert_called_once_with("/api/catalog/profile/orders")

    def test_auto_falls_back_to_server_on_local_table_miss(self, tmp_config):
        """A local DB exists but lacks the requested table: `--scope auto`
        falls back to the server rather than just listing local tables."""
        _make_local_db(tmp_config)
        profile = {"table": "unit_economics", "row_count": 7}
        with patch("cli.client.api_get", return_value=_resp(200, profile)) as mock_get:
            result = runner.invoke(app, ["explore", "unit_economics"])
        assert result.exit_code == 0
        assert "[scope] 'unit_economics' not found locally" in result.output
        assert "running server-side" in result.output
        mock_get.assert_called_once_with("/api/catalog/profile/unit_economics")


class TestScopeLocalExplicit:
    def test_scope_local_no_fallback_on_missing_db(self, tmp_config):
        """`--scope local` keeps today's behavior exactly: no fallback."""
        with patch("cli.client.api_get") as mock_get:
            result = runner.invoke(app, ["explore", "--scope", "local", "orders"])
        assert result.exit_code == 1
        assert "Local DuckDB not found" in result.output
        mock_get.assert_not_called()

    def test_scope_local_no_fallback_on_missing_table(self, tmp_config):
        _make_local_db(tmp_config)
        with patch("cli.client.api_get") as mock_get:
            result = runner.invoke(app, ["explore", "--scope", "local", "nonexistent_xyz"])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()
        mock_get.assert_not_called()


class TestScopeConflictsAndValidation:
    def test_invalid_scope_value_rejected(self):
        result = runner.invoke(app, ["explore", "--scope", "bogus", "orders"])
        assert result.exit_code == 1
        assert "--scope" in result.output

    def test_remote_and_explicit_scope_local_conflict(self):
        result = runner.invoke(app, ["explore", "--remote", "--scope", "local", "orders"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output


class TestJsonOutputStillWorks:
    def test_scope_server_json_flag(self):
        profile = {"table": "orders", "row_count": 500}
        with patch("cli.client.api_get", return_value=_resp(200, profile)):
            result = runner.invoke(app, ["explore", "--scope", "server", "--json", "orders"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["table"] == "orders"
