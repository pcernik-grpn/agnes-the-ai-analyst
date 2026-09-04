"""`agnes schema <table>` — show columns + BQ flavor hints (spec §4.1)."""

import json as json_lib
import typer
from cli.v2_client import api_get_json, V2ClientError

schema_app = typer.Typer(help="Show column metadata for a table")


@schema_app.callback(invoke_without_command=True)
def schema(
    ctx: typer.Context,
    table_id: str = typer.Argument(...),
    json: bool = typer.Option(False, "--json"),
):
    """Show column metadata for a table."""
    if ctx.invoked_subcommand is not None:
        return
    try:
        data = api_get_json(f"/api/v2/schema/{table_id}")
    except V2ClientError as e:
        if e.status_code == 404:
            typer.echo(
                f"Table '{table_id}' not found in the registry.\n"
                "  - List available tables:  agnes catalog\n"
                f'  - Search everything:      agnes search "{table_id}"',
                err=True,
            )
        else:
            typer.echo(f"Error: schema fetch failed: {e}", err=True)
        raise typer.Exit(5 if e.status_code >= 500 else 8 if e.status_code == 403 else 2)

    if json:
        typer.echo(json_lib.dumps(data, indent=2))
        return

    flavor = data.get("sql_flavor", "duckdb")
    typer.echo(f"Table: {data['table_id']}  ({data['source_type']} — use {flavor.upper()} SQL dialect)")
    typer.echo("")
    typer.echo(f"{'COLUMN':30s}  {'TYPE':15s}  {'NULL':5s}  DESCRIPTION")
    for c in data.get("columns", []):
        description = c.get("description", "")
        if c.get("masked"):
            description = f"{description} (masked by access policy)".strip()
        typer.echo(f"{c['name']:30s}  {c['type']:15s}  {'YES' if c.get('nullable') else 'NO':5s}  {description}")
    if data.get("partition_by"):
        typer.echo(f"\nPartition: {data['partition_by']}")
    if data.get("clustered_by"):
        typer.echo(f"Clustered: {', '.join(data['clustered_by'])}")
    if data.get("where_dialect_hints"):
        typer.echo("\nWHERE dialect hints:")
        for k, v in data["where_dialect_hints"].items():
            typer.echo(f"  {k:25s}  {v}")
