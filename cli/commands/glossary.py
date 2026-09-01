"""`agnes glossary {search,show}` — Keboola-imported business-term glossary."""

import json as json_lib

import typer

from cli.client import api_get, error_detail

glossary_app = typer.Typer(help="Search and show glossary terms")


@glossary_app.command("search")
def search_glossary(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(20, "--limit", help="Max results"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Relevance-ranked search across glossary term + definition."""
    resp = api_get("/api/glossary/search", params={"q": query, "limit": limit})
    if resp.status_code != 200:
        typer.echo(f"Failed: {error_detail(resp)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    terms = data.get("terms", [])

    if json:
        typer.echo(json_lib.dumps(terms, indent=2, default=str))
        return

    if not terms:
        typer.echo("No glossary terms found.")
        typer.echo(
            f"Try a broader query, or confirm the glossary is populated — "
            f"it's filled by the Keboola semantic-layer sync, so an empty "
            f"result for '{query}' may mean the sync hasn't run yet."
        )
        return

    for t in terms:
        # Prefer the server's plain-text projection — the column also holds
        # definitions imported verbatim from an external catalog, often rich
        # HTML. Falls back to the stored value against an older server.
        typer.echo(f"{t['term']:30s} {t.get('definition_text') or t.get('definition', '')}")


@glossary_app.command("show")
def show_glossary_term(
    glossary_id: str = typer.Argument(..., help="Glossary term id"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show details for a single glossary term."""
    resp = api_get(f"/api/glossary/{glossary_id}")
    if resp.status_code == 404:
        typer.echo(f"Glossary term not found: {glossary_id}", err=True)
        typer.echo(
            "Try: agnes glossary search <query>  — the id must come from a search result, not a guessed slug.",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(f"Failed: {error_detail(resp)}", err=True)
        raise typer.Exit(1)

    t = resp.json()

    if json:
        typer.echo(json_lib.dumps(t, indent=2, default=str))
        return

    typer.echo(f"ID:         {t.get('id', glossary_id)}")
    typer.echo(f"Term:       {t.get('term', '')}")
    typer.echo(f"Definition: {t.get('definition_text') or t.get('definition', '')}")
    if t.get("see_also"):
        typer.echo(f"See also:   {', '.join(t['see_also'])}")
