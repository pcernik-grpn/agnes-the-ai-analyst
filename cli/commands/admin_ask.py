"""agnes admin ask — natural-language telemetry query."""

from __future__ import annotations

import json
import sys

import typer

from cli.client import error_detail, get_client


app = typer.Typer(help="Ask a natural-language question about telemetry; LLM translates to SQL and runs it.")


@app.callback(invoke_without_command=True)
def ask(
    question: str = typer.Argument(..., help="Natural-language question, e.g. 'top 10 most used skills last 7 days'"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a formatted table."),
):
    """Translate question -> SQL -> execute. Prints SQL + result."""
    client = get_client(timeout=120)
    try:
        resp = client.post("/api/admin/telemetry/ask", json={"question": question})
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)

    if resp.status_code == 401:
        typer.echo("[err] authentication required — run `agnes auth login` or import a PAT", err=True)
        raise typer.Exit(1)
    if resp.status_code == 403:
        typer.echo("[err] admin only", err=True)
        raise typer.Exit(1)
    if resp.status_code == 503:
        typer.echo(f"[err] server: {error_detail(resp)}", err=True)
        raise typer.Exit(1)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"[err] server returned {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    if json_out:
        typer.echo(json.dumps(data, indent=2, default=str))
        return

    # Pretty print
    typer.echo()
    typer.echo(f"Question:  {data['question']}")
    if data.get("rationale"):
        typer.echo(f"Rationale: {data['rationale']}")
    typer.echo()
    typer.echo("SQL:")
    typer.echo(f"  {data['sql']}")
    typer.echo()

    if data.get("rejected"):
        typer.echo(f"[!] SQL rejected by server: {data['rejected']}", err=True)
        raise typer.Exit(1)

    cols = data.get("columns") or []
    rows = data.get("rows") or []
    if not rows:
        typer.echo("(no rows)")
    else:
        # Simple column-aligned print
        widths = [max(len(str(c)), max((len(str(r.get(c))) for r in rows), default=0)) for c in cols]
        header = "  ".join(str(c).ljust(w) for c, w in zip(cols, widths))
        typer.echo(header)
        typer.echo("  ".join("-" * w for w in widths))
        for r in rows:
            typer.echo("  ".join(str(r.get(c, "")).ljust(w) for c, w in zip(cols, widths)))
        if data.get("truncated"):
            typer.echo(f"\n... truncated to 1000 rows ({data['row_count']} returned)")
    typer.echo(f"\n(rows={data['row_count']}, llm={data.get('llm_ms')}ms, exec={data.get('exec_ms', 0)}ms)")
