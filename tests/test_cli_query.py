"""Tests for agnes query command."""

import csv
import io
import json
import pytest
from unittest.mock import patch, MagicMock

from click.testing import CliRunner as ClickCliRunner
from typer.main import get_command
from typer.testing import CliRunner
from cli.main import app

runner = CliRunner()
# Typer's CliRunner mixes stdout/stderr into a single buffer (Click 8.1-era
# behaviour), which raises ValueError on `.stderr` access. Click 8.2+ runs
# them separately by default. Convert the Typer app to a Click command and
# invoke it via Click's runner for the bytes_scanned tests that need to
# inspect `result.stderr` (Devin Review BUG_0001 on #598).
split_runner = ClickCliRunner()
click_app = get_command(app)


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


class TestRemoteQuery:
    def test_remote_query_success(self):
        """--remote sends SQL to server and prints results."""
        # api_post is imported inside _query_remote so mock the source module
        payload = {"columns": ["id", "name"], "rows": [[1, "Alice"]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", "SELECT * FROM users", "--remote"])
        assert result.exit_code == 0

    def test_remote_query_failure(self):
        """--remote prints error message on API failure (#160 §4.7: shared
        renderer surfaces the detail; the prior `Query failed: ...` prefix
        was dropped in favor of HTTP-status + structured detail)."""
        with patch("cli.client.api_post", return_value=_resp(400, {"detail": "bad SQL"})):
            result = runner.invoke(app, ["query", "SELECT bad", "--remote"])
        assert result.exit_code == 1
        # Renderer formats string-detail as `HTTP 400: bad SQL`
        assert "HTTP 400" in result.output
        assert "bad SQL" in result.output

    def test_remote_query_5xx_exits_nonzero(self):
        """5xx server errors propagate to a nonzero exit code (issue #345 C).

        Without this, ``set -e`` shells, CI pipelines, and any wrapper
        script that checks ``$?`` to detect failure would silently
        proceed even when the server returned ``HTTP 502:`` to stdout.
        The reporter who filed #345 hit the exact rc=0-on-502 pattern
        on a slightly older CLI build; this guards against any future
        regression that drops the ``raise typer.Exit(1)`` from the
        non-200 branch of ``_query_remote``.
        """
        with patch("cli.client.api_post", return_value=_resp(502, text="bad gateway")):
            result = runner.invoke(app, ["query", "SELECT 1", "--remote"])
        assert result.exit_code != 0, (
            f"Expected nonzero exit for HTTP 502, got rc={result.exit_code}. Output: {result.output}"
        )
        assert "HTTP 502" in result.output

    def test_remote_query_json_alias_equals_format_json(self):
        """``--json`` is a shortcut for ``--format json`` (issue #345 D).

        Paste-prompts and LLM-assisted analysts routinely reach for
        ``agnes query --json`` first; the typer "Did you mean --stdin?"
        suggestion the absence of this flag previously produced was
        actively misleading.
        """
        payload = {"columns": ["id"], "rows": [[1]], "truncated": False}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", "SELECT 1", "--remote", "--json"])
        assert result.exit_code == 0
        # Output is parseable JSON, equivalent to --format json
        parsed = json.loads(result.output.strip())
        assert parsed == [{"id": 1}]

    def test_json_and_explicit_csv_format_are_mutually_exclusive(self):
        """``--json --format csv`` is contradictory; reject with rc=1.

        ``--json --format json`` is allowed (redundant but consistent);
        ``--json`` alone is the common case.
        """
        result = runner.invoke(app, ["query", "SELECT 1", "--json", "--format", "csv"])
        assert result.exit_code == 1
        assert "mutually exclusive" in result.output

    def test_remote_query_truncated(self):
        """Truncated result shows warning."""
        payload = {"columns": ["id"], "rows": [[i] for i in range(5)], "truncated": True}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", "SELECT id FROM t", "--remote", "--limit", "5"])
        assert result.exit_code == 0
        assert "truncated" in result.output

    def test_remote_query_bytes_scanned_notice_on_stderr(self):
        """#393: a non-null bytes_scanned prints a human-readable dry-run
        estimate to STDERR (mirrors the `truncated` notice). Uses the
        module-level ``split_runner`` so result.stderr is captured
        separately (Devin Review BUG_0001 on #598)."""
        payload = {
            "columns": ["id"],
            "rows": [[1]],
            "truncated": False,
            "bytes_scanned": 4_500_000_000,
        }
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = split_runner.invoke(click_app, ["query", "SELECT id FROM web_sessions", "--remote"])
        assert result.exit_code == 0
        assert "BigQuery scanned" in result.stderr
        assert "dry-run estimate" in result.stderr
        # 4.5e9 bytes ~= 4.2 GB
        assert "GB" in result.stderr

    def test_remote_query_no_bytes_scanned_notice_when_none(self):
        """#393: local-style payload (bytes_scanned None) emits no notice."""
        payload = {"columns": ["id"], "rows": [[1]], "truncated": False, "bytes_scanned": None}
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", "SELECT 1", "--remote"])
        assert result.exit_code == 0
        assert "BigQuery scanned" not in result.output

    def test_remote_query_bytes_scanned_keeps_stdout_pure_json(self):
        """#393: the bytes_scanned notice goes to STDERR only — `--format
        json` stdout stays parseable JSON. Uses the module-level
        ``split_runner`` so we can verify stdout stays pure JSON and the
        notice lands only on stderr (Devin Review BUG_0001 on #598)."""
        payload = {
            "columns": ["id"],
            "rows": [[1]],
            "truncated": False,
            "bytes_scanned": 1_073_741_824,
        }
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = split_runner.invoke(click_app, ["query", "SELECT id FROM t", "--remote", "--format", "json"])
        assert result.exit_code == 0
        # stdout is pure JSON — no notice leaked in.
        parsed = json.loads(result.stdout.strip())
        assert parsed == [{"id": 1}]
        assert "BigQuery scanned" not in result.stdout
        # ...but the notice is present on stderr.
        assert "BigQuery scanned" in result.stderr

    def test_remote_query_csv_format_round_trips_special_values(self):
        """#1801: `--remote` renders through the same `_output` csv branch
        as `--scope local` (and therefore the default `--scope auto`
        fallback) — not a separate, still-broken code path. Remote rows
        arrive as JSON lists rather than local's DuckDB tuples;
        csv.writer must handle both the same way.
        """
        payload = {
            "columns": ["comma", "quote", "newline", "nullable", "empty", "n"],
            "rows": [["a,b", 'say "hi"', "line1\nline2", None, "", 2]],
            "truncated": False,
        }
        sql = (
            "SELECT 'a,b' AS comma, 'say \"hi\"' AS quote, "
            "'line1' || chr(10) || 'line2' AS newline, "
            "NULL AS nullable, '' AS empty, 2 AS n"
        )
        with patch("cli.client.api_post", return_value=_resp(200, payload)):
            result = runner.invoke(app, ["query", sql, "--remote", "--format", "csv"])
        assert result.exit_code == 0

        parsed = list(csv.reader(io.StringIO(result.output)))
        assert parsed[0] == ["comma", "quote", "newline", "nullable", "empty", "n"]
        assert parsed[1] == ["a,b", 'say "hi"', "line1\nline2", "", "", "2"]

    def test_remote_query_uses_long_timeout(self):
        """--remote passes the long-running QUERY_TIMEOUT_S to api_post.

        BigQuery SELECTs routinely take minutes; the default 30s httpx
        timeout dies long before the query finishes. Regression guard for
        the fix that introduced AGNES_QUERY_TIMEOUT (default 300s).
        """
        from cli.client import QUERY_TIMEOUT_S

        payload = {"columns": [], "rows": [], "truncated": False}
        mock_post = MagicMock(return_value=_resp(200, payload))
        with patch("cli.client.api_post", mock_post):
            result = runner.invoke(app, ["query", "SELECT 1", "--remote"])
        assert result.exit_code == 0
        assert mock_post.call_args.kwargs["timeout"] == QUERY_TIMEOUT_S
        assert QUERY_TIMEOUT_S >= 300.0


class TestLocalQuery:
    def test_local_query_no_db(self, tmp_config):
        """`--scope local` query without DuckDB exits with guidance that leads
        with the no-download `--remote` path (and still mentions `agnes
        pull`). The default `--scope auto` instead falls back to the server
        (see tests/test_cli_query_scope.py)."""
        result = runner.invoke(app, ["query", "SELECT 1", "--scope", "local"])
        assert result.exit_code == 1
        out = result.output.lower()
        assert "--remote" in out
        assert "agnes pull" in out

    def test_local_query_with_real_db(self, tmp_config):
        """Local query executes against real DuckDB."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE nums (n INTEGER)")
        conn.execute("INSERT INTO nums VALUES (1), (2), (3)")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT SUM(n) as total FROM nums", "--format", "json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["total"] == 6

    def test_local_query_csv_format(self, tmp_config):
        """--format csv produces CSV output."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        conn.execute("INSERT INTO t VALUES (1, 'x')")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT a, b FROM t", "--format", "csv"])
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert lines[0] == "a,b"
        assert "1,x" in lines[1]

    def test_local_query_csv_format_escapes_comma_and_quote(self, tmp_config):
        """RFC 4180 escaping (#1801): the issue's canonical example — a
        value containing a comma must be wrapped in double quotes, and an
        embedded double quote must be doubled. Asserts on the literal
        physical CSV bytes (not just a round-trip) so a fix that handles
        the delimiter but forgets to double an embedded quote can't pass
        by accident.
        """
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        result = runner.invoke(
            app,
            ["query", "SELECT 'a,b' AS comma, 'say \"hi\"' AS quote", "--format", "csv"],
        )
        assert result.exit_code == 0
        lines = result.output.strip().splitlines()
        assert lines[0] == "comma,quote"
        assert lines[1] == '"a,b","say ""hi"""'

    def test_local_query_csv_format_round_trips_special_values(self, tmp_config):
        """RFC 4180 escaping (#1801): every value class that broke the
        previous plain ",".join(...) — a comma, an embedded double quote,
        an embedded newline, NULL, and an empty string — must round-trip
        through a standard CSV parser. Mirrors the issue's exact repro
        shape (plus NULL/empty columns). Verified via round-trip
        (`csv.reader` over the CLI's own stdout) rather than literal
        string matching — the exact physical quoting bytes for the
        comma/quote case are already pinned by
        test_local_query_csv_format_escapes_comma_and_quote.
        """
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        sql = (
            "SELECT 'a,b' AS comma, 'say \"hi\"' AS quote, "
            "'line1' || chr(10) || 'line2' AS newline, "
            "NULL AS nullable, '' AS empty, 2 AS n"
        )
        result = runner.invoke(app, ["query", sql, "--format", "csv"])
        assert result.exit_code == 0

        parsed = list(csv.reader(io.StringIO(result.output)))
        assert parsed[0] == ["comma", "quote", "newline", "nullable", "empty", "n"]
        assert parsed[1] == ["a,b", 'say "hi"', "line1\nline2", "", "", "2"]

    def test_local_query_table_format(self, tmp_config):
        """Default table format renders without crash."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.execute("INSERT INTO t VALUES (42)")
        conn.close()

        result = runner.invoke(app, ["query", "SELECT id FROM t"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_local_query_limit(self, tmp_config):
        """--limit restricts rows returned."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        conn = duckdb.connect(str(db_dir / "analytics.duckdb"))
        conn.execute("CREATE TABLE big (n INTEGER)")
        conn.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(100)])
        conn.close()

        result = runner.invoke(app, ["query", "SELECT n FROM big", "--format", "json", "--limit", "5"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 5

    def test_local_query_sql_error(self, tmp_config):
        """SQL syntax error exits with error (`--scope local`: no fallback)."""
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        result = runner.invoke(app, ["query", "SELECT * FROM nonexistent_table_xyz", "--scope", "local"])
        assert result.exit_code == 1
        assert "Query error" in result.output

    def test_local_query_missing_table_hints_remote(self, tmp_config):
        """Querying a table absent from local DuckDB surfaces a hint about
        `query_mode='remote'` tables alongside the original DuckDB error
        (`--scope local`: no fallback — see test_cli_query_scope.py for the
        default `--scope auto` fallback behavior).

        Reproduces the analyst-session UX gap where DuckDB's nearest-name
        ("Did you mean <other_table>") suggestion sent the user down the
        wrong path — they thought the table didn't exist or they typo'd,
        when in fact it's a remote table that intentionally has no local
        view.
        """
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        result = runner.invoke(app, ["query", "DESCRIBE unit_economics", "--scope", "local"])
        assert result.exit_code == 1
        # Original DuckDB diagnostic must remain visible (don't break logging).
        assert "Query error" in result.output
        assert "Table with name unit_economics does not exist" in result.output
        # New hint fires.
        assert "query_mode='remote'" in result.output
        assert "agnes catalog" in result.output
        assert "agnes schema" in result.output
        assert "agnes query --remote" in result.output

    def test_local_query_syntax_error_does_not_show_remote_hint(self, tmp_config):
        """A non-missing-table failure (e.g. raw syntax error) must NOT
        trigger the new remote-mode hint — the regex only matches DuckDB's
        `Table with name X does not exist` shape.
        """
        import duckdb

        db_dir = tmp_config / "local" / "user" / "duckdb"
        db_dir.mkdir(parents=True)
        duckdb.connect(str(db_dir / "analytics.duckdb")).close()

        # Trailing FROM with no relation -> ParserException, not CatalogException.
        result = runner.invoke(app, ["query", "SELECT * FROM"])
        assert result.exit_code == 1
        assert "Query error" in result.output
        assert "query_mode='remote'" not in result.output
        assert "agnes query --remote" not in result.output
