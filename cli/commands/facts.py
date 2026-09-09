"""`agnes facts search|neighbors|edges|claims` — fact graph over Collections
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


def _echo_candidates_capped_note() -> None:
    """`candidates_capped: true` — the server ranked only the best N name
    matches for QUERY (a bounded candidate set, see `FactsPgRepository.search`),
    so the page is the best-ranked subset, not every match. Visibility and
    `--filter` are evaluated AFTER that cap, so a filter cannot reach a match
    the cap excluded — only a narrower QUERY or a different TYPE can."""
    typer.echo(
        "(more names match QUERY than the server ranks per call — this is the best-ranked subset, not every "
        "match; narrow QUERY to a longer or more specific name, or pick a narrower TYPE. --filter is applied "
        "after that cap and cannot widen it)",
        err=True,
    )


@facts_app.command("search")
def search_facts(
    fact_type: str = typer.Argument(..., metavar="TYPE", help="Subject type to search (e.g. 'person', 'engagement')"),
    q: Optional[str] = typer.Argument(
        None,
        metavar="[QUERY]",
        help=(
            "Optional free-text name lookup (e.g. a person or organization name). Under four characters it "
            "must start a name token ('llr' finds 'llr-corp' and 'acme-llr', not 'fullrange')."
        ),
    ),
    filter: List[str] = typer.Option([], "--filter", help="Attribute filter key=value (repeatable)"),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Max results (server caps at 100)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Search typed subjects (facts) by type, an optional name, and attribute filters.

    QUERY matches subject ALIASES only (never claim text) — an exact or
    prefix match on the name ranks first. The server ranks a bounded set of
    name matches per call; when more names match than that, the table is
    the best-ranked matches and a note asks you to narrow QUERY. Attributes
    are projected from YOUR
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
    capped = bool(data.get("candidates_capped"))
    if not subjects:
        typer.echo(f"No facts found for type '{fact_type}'{f' matching {q!r}' if q else ''}.")
        if capped:
            # An empty page is not "no matches" when the server capped the
            # name match: the best-ranked candidates all failed visibility or
            # --filter, and matches may exist beyond the cap. Disclose it
            # here exactly as the non-empty branch does.
            _echo_candidates_capped_note()
        else:
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
    if capped:
        _echo_candidates_capped_note()
    if data.get("limit_applied"):
        typer.echo(
            f"(showing the first {limit} of YOUR visible results — raise --limit for more; "
            "never a signal that grants hid additional matches)",
            err=True,
        )


@facts_app.command("type-map")
def facts_type_map(
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Show every node type AND every edge (relationship) type in the graph,
    each with a live count of what YOU can see.

    Each node-type row is a way in: pass its TYPE to `agnes facts search` to
    list the subjects behind the number. Each edge-type row is a valid
    `--edge-types` value for `agnes facts neighbors` — check here BEFORE
    traversing a well-connected subject, since omitting `--edge-types`
    returns every relationship type it has. Counts are gated exactly as
    `search`/`neighbors` are, so a number is what you could reach and never
    a total that includes evidence you cannot read. A type you have no
    visible subjects/edges for is omitted entirely rather than shown as 0.
    """
    resp = api_get("/api/facts/type-map")
    if resp.status_code != 200:
        typer.echo(render_error(resp.status_code, resp.json()), err=True)
        raise typer.Exit(1)

    _echo_server_label()
    data = resp.json()
    if json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    types = data.get("types", [])
    edge_types = data.get("edge_types", [])
    if not types and not edge_types:
        typer.echo("No node or edge types are visible to you.")
        typer.echo(
            "Either nothing has been extracted into the graph yet, or none of its evidence "
            "is in a collection you can read — ask an admin about collection grants."
        )
        return

    if types:
        width = max(len(t["type"]) for t in types)
        typer.echo(f"{'TYPE':{width}s}  COUNT")
        for t in types:
            typer.echo(f"{t['type']:{width}s}  {t['count']}")
        typer.echo(f"\n{data.get('total', 0)} subjects across {len(types)} types.")
        typer.echo("Run `agnes facts search <TYPE>` to list the subjects behind a row.", err=True)

    if edge_types:
        ewidth = max(len(t["type"]) for t in edge_types)
        typer.echo(f"\n{'EDGE TYPE':{ewidth}s}  COUNT")
        for t in edge_types:
            typer.echo(f"{t['type']:{ewidth}s}  {t['count']}")
        typer.echo("Run `agnes facts neighbors <ID> --edge-types <TYPE>` to traverse just that relationship.", err=True)


@facts_app.command("facets")
def facts_facets(
    types: Optional[str] = typer.Option(
        None, "--types", help="Comma-separated fact types (default: client, industry, service_offering, doc_type)"
    ),
    limit: int = typer.Option(50, "--limit", min=1, max=200, help="Max values per type"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """List the entity values you can filter documents by, with a document count each.

    The vocabulary comes from the extraction pass rather than hand-entered
    tags. Counts cover only documents in collections you can read, so a facet
    never reports files you could not open.
    """
    path = "/api/facts/facets?limit_per_type=" + str(limit)
    if types:
        path += "&types=" + types
    resp = api_get(path)
    if resp.status_code != 200:
        typer.echo(render_error(resp.status_code, resp.json()), err=True)
        raise typer.Exit(1)

    _echo_server_label()
    data = resp.json()
    if json:
        typer.echo(json_lib.dumps(data, indent=2, default=str))
        return

    facets = data.get("facets", {})
    if not any(facets.values()):
        typer.echo("No entity values are visible to you.")
        typer.echo(
            "Either nothing has been extracted into the graph yet, or none of its evidence "
            "is in a collection you can read."
        )
        return

    for ftype, values in facets.items():
        if not values:
            continue
        typer.echo(f"\n{ftype}")
        for v in values:
            typer.echo(f"  {v['document_count']:>5}  {v['label']}")


@facts_app.command("neighbors")
def facts_neighbors(
    subject_id: str = typer.Argument(..., help="Fact id to traverse from (from `agnes facts search`)"),
    edge_types: str = typer.Option(
        "", "--edge-types", help="Comma-separated edge type filter — see `agnes facts type-map` (default: all)"
    ),
    depth: int = typer.Option(1, "--depth", min=1, max=2, help="Traversal depth, 1 (default) or 2"),
    claims: int = typer.Option(
        0, "--claims", min=0, max=3, help="Attach this many newest readable quotes per edge inline (0-3)"
    ),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Bounded graph traversal from one subject (depth <= 2).

    Pass --edge-types when you know it — a well-connected subject can carry
    many relationship types, and omitting --edge-types returns ALL of them.
    Run `agnes facts type-map` first for a cheap list of valid edge type
    names with a count each. For EVERY relationship of one type (not just
    one subject's), use `agnes facts edges <edge_type>` instead.

    Re-checks visibility at EVERY hop: an edge into a subject whose claims
    you cannot read is dropped silently, never revealed as "there but
    hidden" (spec §5 rule 3).
    """
    body: dict = {"subject_id": subject_id, "depth": depth}
    if edge_types:
        body["edge_types"] = [t.strip() for t in edge_types.split(",") if t.strip()]
    if claims:
        body["include_claims"] = claims

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
        _echo_edge_table(edges)

    truncated = data.get("truncated") or {}
    hit = [k for k, v in truncated.items() if v]
    if hit:
        typer.echo(f"(truncated: {', '.join(hit)} — narrow --edge-types, lower --depth or --claims)", err=True)


def _echo_edge_table(edges: List[dict]) -> None:
    """The shared edge rendering for `neighbors` and `edges`: one row per
    edge, then any inline claims (``--claims``) indented under it."""
    typer.echo(f"{'EDGE':20s}  {'TYPE':14s}  {'SRC':20s}  DST")
    for e in edges:
        typer.echo(f"{e['id']:20s}  {e.get('type', ''):14s}  {e.get('src', ''):20s}  {e.get('dst', '')}")
        for c in e.get("claims") or []:
            doc = c.get("document") or {}
            quote = c.get("quote") or "(quote withheld)"
            suffix = " …" if c.get("quote_truncated") else ""
            typer.echo(
                f"    [{c.get('id', '?')}] {doc.get('name', '?')} ({c.get('document_date') or 'undated'}): {quote}{suffix}"
            )


@facts_app.command("edges")
def facts_edges(
    edge_type: str = typer.Argument(..., help="Relationship type to list — see `agnes facts type-map` for the names"),
    src_type: str = typer.Option("", "--src-type", help="Only edges whose source fact has this type"),
    dst_type: str = typer.Option("", "--dst-type", help="Only edges whose destination fact has this type"),
    src_id: str = typer.Option("", "--src", help="Only edges out of this fact id"),
    dst_id: str = typer.Option("", "--dst", help="Only edges into this fact id"),
    extend: str = typer.Option(
        "", "--extend", help="Second relationship type to follow one hop from each edge's --extend-from endpoint"
    ),
    extend_from: str = typer.Option("dst", "--extend-from", help="Endpoint the extension starts from: dst or src"),
    claims: int = typer.Option(
        0, "--claims", min=0, max=3, help="Attach this many newest readable quotes per edge inline (0-3)"
    ),
    limit: Optional[int] = typer.Option(None, "--limit", min=1, max=100, help="Max edges per hop (server caps at 100)"),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Every readable relationship of ONE type, both ends included, in one
    call (TCRD-295) — the shape of "which A relate to which B": list the
    type, optionally filter by endpoint type or anchor on one id, follow a
    second type one hop with --extend, and get the quotes to cite with
    --claims. Use `agnes facts neighbors` only when you start from ONE
    known subject.

    Edges and both endpoints are filtered to what YOU can read; an unknown
    or unreadable type is an empty page, not an error (spec §5 rule 2).
    """
    body: dict = {"edge_type": edge_type}
    if src_type:
        body["src_type"] = src_type
    if dst_type:
        body["dst_type"] = dst_type
    if src_id:
        body["src_id"] = src_id
    if dst_id:
        body["dst_id"] = dst_id
    if extend:
        body["extend_edge_type"] = extend
        body["extend_from"] = extend_from
    if claims:
        body["include_claims"] = claims
    if limit is not None:
        body["limit"] = limit

    resp = api_post("/api/facts/edges", json=body)
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
    if not edges:
        typer.echo(
            f"No readable '{edge_type}' edges. Check the relationship name with `agnes facts type-map` — "
            "an unknown type and one outside your access look the same on purpose."
        )
        return
    typer.echo(f"{'ID':20s}  {'TYPE':14s}  ALIASES")
    for n in nodes:
        marker = " [revealed]" if n.get("revealed") else ""
        aliases = ", ".join(n.get("aliases") or [])
        typer.echo(f"{n['id']:20s}  {n.get('type', ''):14s}  {aliases}{marker}")
    typer.echo("")
    _echo_edge_table(edges)

    truncated = data.get("truncated") or {}
    hit = [k for k, v in truncated.items() if v]
    if hit:
        typer.echo(
            f"(truncated: {', '.join(hit)} — narrow with --src-type/--dst-type/--src/--dst, or lower --claims)",
            err=True,
        )


@facts_app.command("claims")
def facts_claims(
    subject_id: str = typer.Argument(..., help="Fact or edge id (from `agnes facts search`/`neighbors`/`edges`)"),
    limit: Optional[int] = typer.Option(
        None, "--limit", min=1, max=200, help="Max claims, newest first (server default 25, cap 200)"
    ),
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
) -> None:
    """Show YOUR readable evidence for one subject — quote, document, date.

    Newest first, capped (default 25) — a note says when more exist.
    `404` (never `403`) whether the id is wrong or you simply have no
    readable claim for it — see the hint printed below for the next step.
    """
    path = f"/api/facts/{subject_id}/claims"
    if limit is not None:
        path += f"?limit={limit}"
    resp = api_get(path)
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
    if data.get("limit_applied"):
        typer.echo(
            f"(showing the newest {len(claims)} — more readable claims exist; raise --limit for older evidence)",
            err=True,
        )
