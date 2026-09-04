"""`agnes admin facts stats rebuild` — fact-graph collection-stats summary
maintenance (TCRD-296 synthesis E.21).

Talks to the live server through `POST /api/admin/facts/stats/rebuild`.
Deliberately not MCP-exposed: an unscoped call recomputes every collection
in the graph, an operator decision no analyst query needs — see
`docs/api-reference.md` -> "Collection-stats summary rebuild".
"""

from __future__ import annotations

import json as json_lib
from typing import List

import typer

from cli.client import api_post
from cli.error_render import render_error

admin_facts_app = typer.Typer(help="Fact-graph maintenance (TCRD-296 synthesis E.21)")
stats_app = typer.Typer(help="Collection-stats summary maintenance")
admin_facts_app.add_typer(stats_app, name="stats")


@stats_app.command("rebuild")
def stats_rebuild(
    corpus_id: List[str] = typer.Option(
        [],
        "--corpus-id",
        help="Scope the rebuild to this collection (repeatable). Omit to rebuild every "
        "collection that currently carries a claim.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Recompute the fact-graph collection-stats summary from `claims`.

    The summary (`fact_collection_stats` / `fact_collection_membership` /
    `edge_collection_membership`) is what `agnes facts search`/`type-map`,
    the Library index, and the admin graph-counts card read instead of
    scanning every claim per request — kept current automatically by
    ingest. Run this once after upgrading to a version carrying this
    feature (the migration that creates the tables does not backfill
    them), or any time the numbers look stale for a specific collection.
    """
    body: dict = {}
    if corpus_id:
        body["corpus_ids"] = list(corpus_id)
    resp = api_post("/api/admin/facts/stats/rebuild", json=body or None)
    if resp.status_code != 200:
        typer.echo(render_error(resp.status_code, resp.json()), err=True)
        raise typer.Exit(1)

    data = resp.json()
    if as_json:
        typer.echo(json_lib.dumps(data, indent=2))
        return
    typer.echo(f"Rebuilt collection-stats summary for {data.get('collections_rebuilt', 0)} collection(s).")
