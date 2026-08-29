"""`agnes facts search|neighbors|claims` — fact graph over Collections
(read surface, build order step 6 of
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md).

Facts have NO local scope (spec §12) — there is nothing to `agnes pull`, so
every call always runs server-side. That is a deliberate, permanent
deviation from the `--scope auto|local|server` command-UX convention (see
`.claude/skills/agnes-conventions/references/command-ux.md`), not a
degraded fallback — so instead of a `--scope` flag (there is nothing to
switch between) every command labels its origin `[server]` on stderr and
says why.
"""

from __future__ import annotations

import json as json_lib
from typing import Any, List, Optional

import typer

from cli.client import api_get, api_post
from cli.error_render import render_error
from cli.query_hints import facts_not_found_hint

facts_app = typer.Typer(help="Query the fact graph over Collections (server-side only, no local cache)")

_SERVER_LABEL = (
    "[server] facts have no local scope (spec §12) — always queried server-side, "
    "unlike `agnes query`'s local/server auto-routing"
)


def _echo_server_label() -> None:
    typer.echo(_SERVER_LABEL, err=True)


def _parse_filter(raw: str) -> "tuple[str, Any]":
    """Split one ``--filter key=value`` into ``(key, value)``.

    Splits on the FIRST ``=`` only, so a value that itself contains ``=``
    (a URL, a base64 blob) stays whole. The value is JSON-decoded when
    possible (``--filter age=30`` sends the number ``30``, matching how the
    attribute was likely ingested) and falls back to the raw string
    otherwise (the common case: ``--filter status=active``).
    """
    if "=" not in raw:
        raise typer.BadParameter(f"--filter must be key=value, got {raw!r}")
    key, _, value = raw.partition("=")
    if not key:
        raise typer.BadParameter(f"--filter key must not be empty, got {raw!r}")
    try:
        parsed = json_lib.loads(value)
    except (json_lib.JSONDecodeError, ValueError):
        parsed = value
    return key, parsed


def _format_attrs(attrs: Optional[dict]) -> str:
    """Render a subject's projected ``attrs`` (or a claim's raw ``attrs``)
    compactly. A projected conflict (``{"conflicted": true, "values": [...]}``)
    renders as ``key=⚠ conflicted (n values)`` instead of picking one value
    silently — the projection genuinely could not resolve it."""
    if not attrs:
        return "-"
    parts = []
    for k, v in attrs.items():
        if isinstance(v, dict) and v.get("conflicted"):
            n = len(v.get("values") or [])
            parts.append(f"{k}=⚠ conflicted ({n} values)")
        elif isinstance(v, dict) and "value" in v:
            parts.append(f"{k}={v['value']}")
        else:
            parts.append(f"{k}={v}")
    return "; ".join(parts)


@facts_app.command("search")
def search_facts(
    fact_type: str = typer.Argument(..., metavar="TYPE", help="Subject type to search (e.g. 'person', 'engagement')"),
    q: Optional[str] = typer.Argument(
        None, metavar="[QUERY]", help="Optional free-text name lookup (e.g. a person or organization name)"
    ),
    filter: List[str] = typer.Option([], "--filter", help="Attribute filter key=value (repeatable)"),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Max results (server caps at 100)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Search typed subjects (facts) by type, an optional name, and attribute filters.

    QUERY matches subject ALIASES only (never claim text) — an exact or
    prefix match on the name ranks first. Attributes are projected from YOUR
    readable evidence only, per key, latest-`document_date`-wins — a genuine
    tie between two documents shows as `⚠ conflicted (n values)` rather than
    silently picking one. Use `agnes facts claims <id>` on a result to see
    the underlying quotes, or `agnes facts neighbors <id>` to traverse its
    edges.
    """
    filters: dict = {}
    for raw in filter:
        k, v = _parse_filter(raw)
        filters[k] = v

    body: dict = {"type": fact_type, "filters": filters, "limit": limit}
    if q:
        body["q"] = q
    resp = api_post("/api/facts/search", json=body)
    if resp.status_code != 200:
        typer.echo(render_error(resp.status_code, resp.json()), err=True)
        raise typer.Exit(1)

    _echo_server_label()
    data = resp.json()
    if json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    subjects = data.get("subjects", [])
    if not subjects:
        typer.echo(f"No facts found for type '{fact_type}'{f' matching {q!r}' if q else ''}.")
        typer.echo(
            "Try a different QUERY or --filter, or drop them entirely to see everything of this type you can read."
        )
        return

    typer.echo(f"{'ID':20s}  {'TYPE':14s}  {'CLAIMS':6s}  {'QUOTES':6s}  ATTRS")
    for s in subjects:
        marker = " [revealed]" if s.get("revealed") else ""
        typer.echo(
            f"{s['id']:20s}  {s['type']:14s}  {s.get('claim_count', 0):<6d}  "
            f"{s.get('quote_count', 0):<6d}  {_format_attrs(s.get('attrs'))}{marker}"
        )
    if data.get("limit_applied"):
        typer.echo(
            f"(showing the first {limit} of YOUR visible results — raise --limit for more; "
            "never a signal that grants hid additional matches)",
            err=True,
        )


@facts_app.command("neighbors")
def facts_neighbors(
    subject_id: str = typer.Argument(..., help="Fact id to traverse from (from `agnes facts search`)"),
    edge_types: str = typer.Option("", "--edge-types", help="Comma-separated edge type filter (default: all)"),
    depth: int = typer.Option(1, "--depth", min=1, max=2, help="Traversal depth, 1 (default) or 2"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Bounded graph traversal from one subject (depth <= 2).

    Re-checks visibility at EVERY hop: an edge into a subject whose claims
    you cannot read is dropped silently, never revealed as "there but
    hidden" (spec §5 rule 3).
    """
    body: dict = {"subject_id": subject_id, "depth": depth}
    if edge_types:
        body["edge_types"] = [t.strip() for t in edge_types.split(",") if t.strip()]

    resp = api_post("/api/facts/neighbors", json=body)
    if resp.status_code == 404:
        typer.echo(facts_not_found_hint(subject_id, surface="cli"), err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(render_error(resp.status_code, resp.json()), err=True)
        raise typer.Exit(1)

    _echo_server_label()
    data = resp.json()
    if json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])
    typer.echo(f"{len(nodes)} node(s), {len(edges)} edge(s)")
    typer.echo(f"{'ID':20s}  TYPE")
    for n in nodes:
        marker = " [revealed]" if n.get("revealed") else ""
        typer.echo(f"{n['id']:20s}  {n.get('type', '')}{marker}")
    if edges:
        typer.echo("")
        typer.echo(f"{'EDGE':20s}  {'TYPE':14s}  {'SRC':20s}  DST")
        for e in edges:
            typer.echo(f"{e['id']:20s}  {e.get('type', ''):14s}  {e.get('src', ''):20s}  {e.get('dst', '')}")

    truncated = data.get("truncated") or {}
    hit = [k for k, v in truncated.items() if v]
    if hit:
        typer.echo(f"(truncated: {', '.join(hit)} — narrow --edge-types or lower --depth)", err=True)


@facts_app.command("claims")
def facts_claims(
    subject_id: str = typer.Argument(..., help="Fact or edge id (from `agnes facts search`/`neighbors`)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Show YOUR readable evidence for one subject — quote, document, date.

    `404` (never `403`) whether the id is wrong or you simply have no
    readable claim for it — see the hint printed below for the next step.
    """
    resp = api_get(f"/api/facts/{subject_id}/claims")
    if resp.status_code == 404:
        typer.echo(facts_not_found_hint(subject_id, surface="cli"), err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(render_error(resp.status_code, resp.json()), err=True)
        raise typer.Exit(1)

    _echo_server_label()
    data = resp.json()
    if json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    claims = data.get("claims", [])
    if data.get("revealed"):
        typer.echo("(this subject is under an admin 'revealed' correction — quotes are withheld for everyone)")
    if not claims:
        typer.echo("No readable claims.")
        return

    for c in claims:
        doc = c.get("document") or {}
        typer.echo(f"[{c['id']}] {doc.get('name', '?')} ({c.get('document_date') or 'undated'})")
        quote = c.get("quote") or ""
        typer.echo(f"  {quote}" if quote else "  (quote withheld)")
        attrs = c.get("attrs")
        if attrs:
            typer.echo(f"  attrs: {_format_attrs(attrs)}")
