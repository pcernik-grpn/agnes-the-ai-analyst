"""CLI tests for `agnes semantic-model validate-query` (query-validation
engine wiring, parity spec §5).

Not to be confused with its sibling `agnes semantic-model validate`, which
schema-checks a DOCUMENT file offline — covered, along with the admin document
CRUD surface (`agnes admin semantic …`), in `tests/test_cli_semantic_model.py`
and `tests/test_cli_semantic_consolidation.py`. This is the query validator.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNES_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "config").mkdir()
    yield tmp_path


def _resp(status_code=200, json_data=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_data if json_data is not None else {}
    r.text = text
    return r


_VALID_RESULT = {
    "available": True,
    "valid": True,
    "used_datasets": ["orders"],
    "used_metrics": ["revenue"],
    "matched_relationships": [],
    "violations": [],
    "post_execution_checks": [],
    "sql_dialects": ["duckdb"],
    "mixed_dialect_warning": None,
    "locally_executable": True,
    "summary": "Query references 1 dataset(s) and 1 metric(s) from the semantic layer; no constraint violations detected.",
    "detection": (
        "Datasets and metrics were detected by a best-effort text match on their declared names, not by parsing "
        "the SQL — a column or alias that shares a name matches too. Treat this as a prompt to check, not a proof."
    ),
}


class TestValidateQuery:
    def test_posts_sql_payload(self):
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, _VALID_RESULT)) as m:
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT SUM(revenue) FROM orders"])
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["sql"] == "SELECT SUM(revenue) FROM orders"
        assert m.call_args.kwargs["json"]["target_engine"] == "duckdb"
        assert "VALID" in result.output

    def test_json_flag_emits_raw_json(self):
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, _VALID_RESULT)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT 1", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == _VALID_RESULT

    def test_non_json_output_carries_the_detection_caveat(self):
        """Issue #1707 finding A18: the dataset/metric list printed above this
        line is a best-effort text match, not a confirmed fact -- the human
        (non-`--json`) output must say so unmissably, not bury it in
        `summary` or omit it."""
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, _VALID_RESULT)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT SUM(revenue) FROM orders"])
        assert result.exit_code == 0
        assert "best-effort text match" in result.output.lower()

    def test_non_json_output_falls_back_when_server_omits_detection(self):
        """An older server without the `detection` field must still get a
        caveat -- never silently drop it just because the key is absent."""
        body = {k: v for k, v in _VALID_RESULT.items() if k != "detection"}
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT SUM(revenue) FROM orders"])
        assert result.exit_code == 0
        assert "best-effort text match" in result.output.lower()

    def test_error_violation_prints_invalid_and_reason(self):
        body = {
            **_VALID_RESULT,
            "valid": False,
            "violations": [
                {
                    "name": "region_filter_required",
                    "constraint_type": "required_filter",
                    "rule": "region = 'EU'",
                    "severity": "error",
                    "metrics": ["revenue"],
                    "reason": "required filter not found in the query: region = 'EU'",
                }
            ],
        }
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT SUM(revenue) FROM orders"])
        assert result.exit_code == 0
        assert "INVALID" in result.output
        assert "region_filter_required" in result.output
        assert "required filter not found" in result.output

    def test_post_execution_checks_do_not_flip_exit_code(self):
        body = {
            **_VALID_RESULT,
            "post_execution_checks": [
                {
                    "name": "non_negative_value",
                    "constraint_type": "value_range",
                    "rule": "value >= 0",
                    "severity": "warning",
                    "metrics": ["revenue"],
                    "reason": "rule cannot be checked before executing the query",
                }
            ],
        }
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT SUM(revenue) FROM orders"])
        assert result.exit_code == 0
        assert "VALID" in result.output
        assert "non_negative_value" in result.output

    def test_locally_executable_false_prints_warning(self):
        body = {**_VALID_RESULT, "locally_executable": False}
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT mrr FROM orders"])
        assert result.exit_code == 0
        assert "not locally executable" in result.output.lower()

    def test_locally_executable_false_names_the_offending_metrics(self):
        """A vague "one or more used metrics" leaves the analyst to work out
        which one. The server now returns the names, so print them."""
        body = {
            **_VALID_RESULT,
            "used_metrics": ["revenue", "mrr"],
            "locally_executable": False,
            "not_executable_metrics": ["mrr"],
        }
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT revenue, mrr FROM orders"])
        assert result.exit_code == 0
        warning = next(line for line in result.output.splitlines() if "not locally executable" in line.lower())
        assert "mrr" in warning
        assert "revenue" not in warning, warning

    def test_no_semantic_model_prints_message_without_failing(self):
        body = {
            "available": False,
            "error": "no_semantic_model",
            "message": "No semantic model is available to validate against.",
        }
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, body)):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT 1"])
        assert result.exit_code == 0
        assert "No semantic model" in result.output

    def test_expect_option_is_parsed_and_posted(self):
        with patch("cli.commands.semantic_model.api_post", return_value=_resp(200, _VALID_RESULT)) as m:
            result = runner.invoke(
                app,
                [
                    "semantic-model",
                    "validate-query",
                    "SELECT 1",
                    "--expect",
                    '[{"type": "metric", "name": "revenue"}]',
                ],
            )
        assert result.exit_code == 0
        assert m.call_args.kwargs["json"]["expected"] == [{"type": "metric", "name": "revenue"}]

    def test_expect_option_rejects_invalid_json(self):
        result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT 1", "--expect", "not-json"])
        assert result.exit_code == 1
        assert "not valid JSON" in result.output

    def test_server_error_exits_nonzero(self):
        with patch(
            "cli.commands.semantic_model.api_post",
            return_value=_resp(500, {"detail": "boom"}),
        ):
            result = runner.invoke(app, ["semantic-model", "validate-query", "SELECT 1"])
        assert result.exit_code == 1
        assert "boom" in result.output
