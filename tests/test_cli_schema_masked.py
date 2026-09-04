"""`agnes schema <table>` -- the human render marks a policy-masked column
(table access policies design doc §11); `--json` stays a raw passthrough of
whatever ``GET /api/v2/schema`` returned.
"""

from unittest.mock import patch

from typer.testing import CliRunner

from cli.main import app

_SCHEMA_PAYLOAD = {
    "table_id": "invoices",
    "source_type": "keboola",
    "sql_flavor": "duckdb",
    "columns": [
        {"name": "id", "type": "VARCHAR", "nullable": False, "description": "", "hidden": False, "masked": False},
        {
            "name": "email",
            "type": "VARCHAR",
            "nullable": True,
            "description": "contact address",
            "hidden": False,
            "masked": True,
        },
    ],
    "partition_by": None,
    "clustered_by": [],
    "where_dialect_hints": {},
}


def test_human_render_flags_the_masked_column():
    with patch("cli.commands.schema.api_get_json", return_value=_SCHEMA_PAYLOAD):
        result = CliRunner().invoke(app, ["schema", "invoices"])
    assert result.exit_code == 0, result.output
    lines = {line.strip() for line in result.output.splitlines()}
    assert any("email" in line and "(masked by access policy)" in line for line in lines)
    assert not any(line.startswith("id") and "masked" in line for line in lines)


def test_json_render_is_the_raw_passthrough():
    # `--json` before the positional `table_id` -- the same Typer
    # subcommand-group parsing quirk `cli/commands/describe.py`'s own
    # docstring documents (a trailing option after a positional confuses the
    # group's `COMMAND` slot).
    with patch("cli.commands.schema.api_get_json", return_value=_SCHEMA_PAYLOAD):
        result = CliRunner().invoke(app, ["schema", "--json", "invoices"])
    assert result.exit_code == 0, result.output
    assert '"masked": true' in result.output
