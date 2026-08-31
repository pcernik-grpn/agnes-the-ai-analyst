"""`agnes catalog` — list registered tables and metric definitions (spec §4.1)."""

import json as json_lib
from typing import Optional

import typer

from cli.client import api_get
from cli.v2_client import api_get_json, V2ClientError

catalog_app = typer.Typer(help="List tables (and metrics, with --metrics) visible to you")


@catalog_app.callback(invoke_without_command=True)
def catalog(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass the server's catalog cache"),
    metrics: bool = typer.Option(
        False,
        "--metrics",
        help="List metric definitions instead of tables. Combine with --show <id> for details.",
    ),
    show: Optional[list[str]] = typer.Option(
        None,
        "--show",
        help="Show details for a metric id — repeatable, so several metrics cost one command (implies --metrics).",
    ),
):
    """List tables visible to you (RBAC-filtered).

    With ``--metrics`` lists registered metric definitions; pair with
    ``--show <id>`` to dump one definition.
    """
    if ctx.invoked_subcommand is not None:
        return

    if show and not metrics:
        metrics = True

    if metrics:
        if show:
            _show_metrics(show, as_json=json)
        else:
            _list_metrics(as_json=json)
        return

    try:
        data = api_get_json("/api/v2/catalog", refresh=int(refresh))
    except V2ClientError as e:
        typer.echo(f"Error: catalog fetch failed: {e}", err=True)
        raise typer.Exit(5)

    if json:
        typer.echo(json_lib.dumps(data, indent=2))
        return
    # Human-readable table.
    # ENTITY column shows the upstream entity_type for remote BigQuery rows
    # (BASE TABLE / VIEW / MATERIALIZED_VIEW) — matters because views don't
    # support predicate pushdown, so an analyst should reach for `agnes
    # snapshot create` rather than `agnes query --remote` on a view.
    # For non-remote rows (local / materialized) entity_type is NULL upstream
    # and we render a dash — those modes don't have an analogous distinction.
    typer.echo(f"{'ID':30s}  {'SOURCE':10s}  {'MODE':8s}  {'ENTITY':18s}  NAME")
    for t in data.get("tables", []):
        entity = t.get("entity_type") or "-"
        typer.echo(f"{t['id']:30s}  {t['source_type']:10s}  {t['query_mode']:8s}  {entity:18s}  {t.get('name', '')}")


def _list_metrics(as_json: bool, category: Optional[str] = None) -> None:
    """List metric definitions from the server."""
    params = {}
    if category:
        params["category"] = category

    resp = api_get("/api/metrics", params=params)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    metrics = data if isinstance(data, list) else data.get("metrics", [])

    if as_json:
        typer.echo(json_lib.dumps(metrics, indent=2, default=str))
        return

    if not metrics:
        typer.echo("No metrics found.")
        return

    # Group by category for display
    by_category: dict = {}
    for m in metrics:
        cat = m.get("category", "uncategorized")
        by_category.setdefault(cat, []).append(m)

    for cat, items in sorted(by_category.items()):
        typer.echo(f"\n[{cat}]")
        for m in items:
            name = m.get("name", m.get("id", "?"))
            display = m.get("display_name", name)
            unit = m.get("unit", "")
            unit_str = f" ({unit})" if unit else ""
            typer.echo(f"  {name:30s} {display}{unit_str}")


def _show_metrics(metric_ids: list[str], as_json: bool) -> None:
    """Show details for one or more metric ids in a single command.

    Looked up per id (each keeps its own 404/403 semantics — the list
    endpoint silently omits an inaccessible metric, which would read as
    "no such metric"), but rendered as ONE result: an agent reading twenty
    definitions should spend one command and one result on it, not twenty.
    A failed id is reported and the rest still print; the exit code is
    non-zero if any id failed.
    """
    if as_json:
        found: list[dict] = []
        failures: list[dict] = []
        for metric_id in metric_ids:
            ok, payload = _fetch_metric(metric_id)
            (found if ok else failures).append(payload)
        typer.echo(json_lib.dumps({"metrics": found, "failed": failures}, indent=2, default=str))
        if failures:
            raise typer.Exit(1)
        return

    failed = 0
    for index, metric_id in enumerate(metric_ids):
        ok, payload = _fetch_metric(metric_id)
        if not ok:
            failed += 1
            typer.echo(payload["error"], err=True)
            for hint in payload.get("hints", []):
                typer.echo(hint, err=True)
            continue
        if index:
            typer.echo("")
        _echo_one_metric(metric_id, payload)
    if failed:
        raise typer.Exit(1)


def _fetch_metric(metric_id: str) -> tuple[bool, dict]:
    """``(ok, payload)`` for one metric id — the metric dict on success, an
    ``{"error", "hints"}`` dict on failure. No process exit: a multi-id
    lookup must not lose the ids that did resolve."""
    resp = api_get(f"/api/metrics/{metric_id}")
    if resp.status_code == 404:
        hints = []
        # The semantic-layer cutover changed Keboola metric ids from
        # `keboola/<model-uuid>/<name>` to
        # `keboola_metastore/<connection>/<model>/<name>`. A stale old-shape id
        # (from memory or a saved note) 404s — point at a name lookup instead.
        if metric_id.startswith("keboola/"):
            name = metric_id.rsplit("/", 1)[-1]
            hints.append(
                f"  Keboola metric ids changed in the semantic-layer cutover. Try: "
                f"agnes catalog --metrics | grep {name}"
            )
        return False, {"id": metric_id, "error": f"Metric not found: {metric_id}", "hints": hints}
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        return False, {"id": metric_id, "error": f"Failed ({metric_id}): {detail}", "hints": []}
    return True, resp.json()


def _echo_one_metric(metric_id: str, m: dict) -> None:
    typer.echo(f"ID:           {m.get('id', metric_id)}")
    typer.echo(f"Name:         {m.get('name', '')}")
    typer.echo(f"Display Name: {m.get('display_name', '')}")
    typer.echo(f"Category:     {m.get('category', '')}")
    typer.echo(f"Type:         {m.get('type', '')}")
    if m.get("unit"):
        typer.echo(f"Unit:         {m['unit']}")
    if m.get("grain"):
        typer.echo(f"Grain:        {m['grain']}")
    if m.get("table_name"):
        typer.echo(f"Table:        {m['table_name']}")
    # Prefer the server's plain-text projection: this column also holds
    # descriptions imported verbatim from an external catalog, which are
    # often rich HTML, and this is the surface CLAUDE.md's agent rails send
    # agents to for the canonical business definition. Falls back to the raw
    # column so an older server (no `description_text`) still prints something.
    description = m.get("description_text") or m.get("description")
    if description:
        typer.echo(f"Description:  {description}")
    if m.get("sql"):
        typer.echo(f"SQL:\n  {m['sql']}")
    if m.get("synonyms"):
        typer.echo(f"Synonyms:     {', '.join(m['synonyms'])}")
    if m.get("notes"):
        typer.echo("Notes:")
        for note in m["notes"]:
            typer.echo(f"  - {note}")
    _echo_constraints(m.get("validation"))


def _echo_constraints(validation) -> None:
    """Print the rules that govern this metric, if any.

    This surface is where `cli/templates/global_rails.md` sends agents for the
    canonical definition, under "Never invent metric SQL". Printing the SQL
    while dropping the constraint attached to it hands the agent an
    authoritative-looking answer with its own caveat removed — so an
    unrecognized shape is dumped verbatim rather than skipped for not matching
    the one shape this renderer knows.
    """
    if isinstance(validation, str):
        # DuckDB hands JSON columns back as text on some read paths.
        try:
            validation = json_lib.loads(validation)
        except (ValueError, TypeError):
            pass
    if not validation:
        return

    typer.echo("Constraints:")
    rules = validation.get("rules") if isinstance(validation, dict) else None
    if not isinstance(rules, list):
        typer.echo(f"  {json_lib.dumps(validation, indent=2, default=str)}")
        return

    for rule in rules:
        if not isinstance(rule, dict):
            typer.echo(f"  - {rule}")
            continue
        name = rule.get("name") or "(unnamed)"
        qualifiers = ", ".join(str(v) for v in (rule.get("constraint_type"), rule.get("severity")) if v)
        head = f"{name} ({qualifiers})" if qualifiers else name
        body = rule.get("rule")
        typer.echo(f"  - {head}: {body}" if body else f"  - {head}")
