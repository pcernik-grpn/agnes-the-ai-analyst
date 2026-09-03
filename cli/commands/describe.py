"""`agnes describe <table>` — schema + sample rows (spec §4.1).

Registered as a flat ``@app.command("describe")`` in ``cli/main.py`` rather
than as a ``Typer.Typer`` subcommand-group + callback. The group pattern
mis-parses ``agnes describe TABLE -n 5`` (positional + short option with a
separated INTEGER value) — Typer hands the "5" to the positional and then
errors on a missing TABLE_ID. There were no actual subcommands of
``describe`` to justify the group wrapping anyway. Issue surfaced from a
real analyst session following the CLAUDE.md "agent rails" workflow.
"""

import json as json_lib
import typer
from cli.query_hints import row_scope_note
from cli.v2_client import api_get_json, V2ClientError


def describe(
    table_id: str = typer.Argument(...),
    n: int = typer.Option(5, "-n", "--rows", help="Sample rows count"),
    json: bool = typer.Option(False, "--json"),
):
    """Show schema + sample rows for a table."""
    try:
        sch = api_get_json(f"/api/v2/schema/{table_id}")
        sam = api_get_json(f"/api/v2/sample/{table_id}", n=n)
    except V2ClientError as e:
        if e.status_code == 404:
            typer.echo(
                f"Table '{table_id}' not found in the registry.\n"
                "  - List available tables:  agnes catalog\n"
                f'  - Search everything:      agnes search "{table_id}"',
                err=True,
            )
        else:
            typer.echo(f"Error: describe failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    # Table access policies (§10): `sample.row_scope` is present when this
    # table's rows are filtered by an access policy -- disclose it the same
    # way `agnes query` does, unconditionally on stderr so `--json` stdout
    # stays a clean parse of the full schema+sample payload.
    scope_note = row_scope_note(sam.get("row_scope"))
    if scope_note:
        typer.echo(scope_note, err=True)

    if json:
        typer.echo(json_lib.dumps({"schema": sch, "sample": sam}, indent=2, default=str))
        return

    typer.echo(f"Table: {sch['table_id']}")
    typer.echo("")
    typer.echo("Schema:")
    for c in sch.get("columns", []):
        typer.echo(f"  {c['name']:30s} {c['type']}")
    typer.echo("")
    typer.echo(f"Sample ({len(sam.get('rows', []))} rows):")
    for row in sam.get("rows", []):
        typer.echo(f"  {row}")
