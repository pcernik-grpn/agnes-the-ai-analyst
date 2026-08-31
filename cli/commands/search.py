"""`agnes search` — unified knowledge search (K2, #797).

One query fans out server-side over document Collections (hybrid
lexical+vector), the corporate-memory knowledge base (fulltext), the
table catalog (lexical cards), business metrics, and the glossary.
Table hits carry a pivot hint — query them with SQL via `agnes query`.
Metric hits link to /semantic-layer?tab=all_metrics; glossary hits show the term
definition inline.
"""

from __future__ import annotations

import json as json_lib

import typer

from cli.v2_client import V2ClientError, api_get_json

_SCOPES = ("server", "local")

search_app = typer.Typer(help="Unified search across documents, knowledge, and the catalog.")


@search_app.callback(invoke_without_command=True)
def search(
    query: str = typer.Argument(..., help="Search query"),
    k: int = typer.Option(10, "--k", "--limit", help="Max results"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
    local: bool = typer.Option(
        False,
        "--local",
        help=(
            "Shorthand for --scope local: search knowledge artifacts pulled by "
            "`agnes pull` (offline; documents only — knowledge rules are already "
            "in .claude/rules/, the table catalog needs the server)"
        ),
    ),
    scope: str | None = typer.Option(
        None,
        "--scope",
        help="server (documents + knowledge + catalog + metrics + glossary) | local (offline, documents only)",
    ),
):
    """One query across documents, the knowledge base, and the data catalog."""
    if scope is not None and scope not in _SCOPES:
        typer.echo(f"Invalid --scope {scope!r} — expected one of {_SCOPES}.", err=True)
        raise typer.Exit(1)
    if local and scope == "server":
        typer.echo("--local conflicts with --scope server.", err=True)
        raise typer.Exit(1)
    effective_local = local or scope == "local"

    if effective_local:
        from pathlib import Path as _Path

        from cli.config import get_workspace_root
        from src.search.local import local_search

        ws = get_workspace_root()
        if not ws:
            typer.echo("No workspace configured — run `agnes init` (or unset --local).", err=True)
            raise typer.Exit(1)
        body = {"query": query, "results": local_search(query, workspace=_Path(ws), k=k), "source": "local"}
        typer.echo("offline scope: documents only — knowledge + catalog need the server", err=True)
    else:
        try:
            body = api_get_json("/api/knowledge/search", q=query, k=k)
        except V2ClientError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)

    if as_json:
        typer.echo(json_lib.dumps(body, indent=2, default=str))
        return
    sources_line = (
        "sources: documents (local)"
        if body.get("source") == "local"
        else "sources: documents + knowledge + catalog + metrics + glossary (server)"
    )
    results = body.get("results", [])
    if not results:
        typer.echo("No matches.")
        typer.echo(sources_line)
        return
    for r in results:
        t = r.get("type")
        if t == "chunk":
            # A name match carries the file's opening text, which does NOT
            # contain the query — without the marker the line reads as a
            # quotation that answers the question. (Devin Review on #1267.)
            how = " (matched by file name)" if r.get("matched_on") == "filename" else ""
            typer.echo(
                f"[{r.get('score')}] doc  {r.get('filename')} #{r.get('ordinal')}{how}: "
                f"{(r.get('text') or '')[:110]}"
            )
        elif t == "knowledge":
            typer.echo(f"[{r.get('score')}] know {r.get('title')}: {(r.get('snippet') or '')[:110]}")
        elif t == "metric":
            label = r.get("display_name") or r.get("name") or r.get("id") or "?"
            typer.echo(f"[{r.get('score')}] mtrc {label}: {(r.get('description') or '')[:110]}")
        elif t == "glossary":
            typer.echo(f"[{r.get('score')}] term {r.get('term')}: {(r.get('definition') or '')[:110]}")
        else:
            typer.echo(f"[{r.get('score')}] tbl  {r.get('name')} — {r.get('pivot_hint')}")
    typer.echo(sources_line)
