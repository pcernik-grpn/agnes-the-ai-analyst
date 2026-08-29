"""`agnes semantic-model` — non-admin, read-tier commands over the semantic
layer: validate a query before running it, and the agent read-parity tools
`context` / `schema` (parity spec §4/§5).

CLI counterpart to ``POST /api/semantic-models/validate-query`` + the MCP
``validate_semantic_query`` foundation tool, and to
``GET /api/semantic-models/context`` / ``GET /api/semantic-models/schema`` +
the MCP ``get_semantic_context`` / ``get_semantic_schema`` foundation tools —
same request/response shape across all three surfaces. Not to be confused
with ``agnes admin semantic-model validate``, which schema-checks a
*document* locally against the vendored Ossie spec; every command here reads
against whatever valid semantic models the caller can already read (mirrors
the non-admin ``/api/semantic-models/search`` + ``export`` RBAC tier — a Data
Package or direct model grant, not admin-only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from cli.client import api_delete, api_get, api_post

semantic_model_app = typer.Typer(help="Read the semantic layer: validate queries, browse context, inspect schema")

_SEMANTIC_TYPES = ("dataset", "metric", "relationship")

# Cross-domain coverage (F4.1) — admin-facing, so it lives in its own
# sub-group rather than crowding the read-tier commands above.
coverage_app = typer.Typer(
    help="What each data source still lacks: semantic model, metrics, glossary, skill, agent, knowledge base",
    invoke_without_command=True,
)
semantic_model_app.add_typer(coverage_app, name="coverage")

_COVERAGE_PATH = "/api/admin/semantic-model/coverage"
_COVERAGE_TAGS_PATH = "/api/admin/semantic-model/coverage/tags"
_COVERAGE_DOMAINS = ("semantic", "metrics", "glossary", "skill", "agent", "knowledge_base")
_COVERAGE_TAGGABLE = ("marketplace_plugin", "agent", "memory_domain")
_STATUS_GLYPHS = {"ok": "ok", "partial": "partial", "missing": "MISSING", "not_applicable": "n/a"}

# Feedback (F4.5) — "that answer looked wrong". `submit` is open to any
# signed-in caller; `list`/`resolve` are the admin side of the same queue.
# All three ship together on purpose: a report must be fileable from every
# surface (UI, chat, MCP, CLI), not only the ones an admin uses.
feedback_app = typer.Typer(help="Report a wrong/unsupported answer, and work the report queue")
semantic_model_app.add_typer(feedback_app, name="feedback")

_FEEDBACK_SUBMIT_PATH = "/api/semantic-feedback"
_FEEDBACK_ADMIN_PATH = "/api/admin/semantic-feedback"
_FEEDBACK_STATUSES = ("open", "acknowledged", "resolved")

# Muting a health check (F4.3). Top-level verbs, not a `mute` sub-group: the
# three commands are one action each (`mute` / `unmute` / `mutes`), and a group
# whose every member is a bare verb reads as `mute mute`.
_MUTES_PATH = "/api/admin/semantic-layer/mutes"
_MUTE_SCOPE_FORMS = ("source:<source-id>", "domain:<domain>", "source:<source-id>:domain:<domain>")

# Health roll-up (F4.2).
_HEALTH_PATH = "/api/admin/semantic-layer/health"


@semantic_model_app.command("apply")
def apply(
    path: str = typer.Argument(..., help="Path to a semantic-model document (YAML)"),
    description: Optional[str] = typer.Option(None, "--description", help="Listing description for the model"),
    expect_hash: Optional[str] = typer.Option(
        None,
        "--expect-hash",
        help="For edits: the content_hash the edit was based on — a mismatch refuses instead of overwriting.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Apply a hand-authored semantic-model document — create or edit.

    The outcome depends on your authority and is always labeled: admins get
    ``Applied`` (the model is live); everyone else gets ``Submitted for
    review`` (an admin approves or rejects it — the model is NOT live until
    then). Offline pre-check without a server or token:
    `agnes admin semantic-model validate <file>`.
    """
    p = Path(path)
    if not p.exists():
        typer.echo(f"File not found: {path}", err=True)
        raise typer.Exit(1)

    payload: dict = {"document": p.read_text()}
    if description is not None:
        payload["description"] = description
    if expect_hash is not None:
        payload["expected_content_hash"] = expect_hash

    resp = api_post("/api/semantic-models/apply", json=payload)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        typer.echo(f"Failed: {detail}", err=True)
        if isinstance(detail, dict) and detail.get("code") == "invalid_document":
            typer.echo("  Pre-check locally: agnes admin semantic-model validate <file>", err=True)
        raise typer.Exit(1)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    if body.get("outcome") == "applied":
        model = body.get("model") or {}
        typer.echo(f"Applied: {model.get('slug', '?')} (live)")
    else:
        typer.echo(
            f"Submitted for review: {body.get('suggestion_id', '?')} — "
            "an admin will approve or reject it; the model is not live yet."
        )


@semantic_model_app.command("validate-query")
def validate_query(
    sql: str = typer.Argument(..., help="SQL statement to validate"),
    expect: Optional[str] = typer.Option(
        None,
        "--expect",
        help='JSON list of expected objects, e.g. \'[{"type":"metric","name":"mrr"}]\'',
    ),
    target_engine: str = typer.Option("duckdb", "--target-engine", help="Engine the query will run on"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Validate SQL against the semantic layer: constraint violations,
    dialect fit, and (optionally) which expected datasets/metrics/
    relationships the query hits.

    Best-effort text matching against declared semantic-model documents —
    not SQL parsing (see the server-side validator's own LIMITATIONS
    docstring). Reads every `status='valid'` semantic model you can access;
    if none exist (or none are accessible to you), prints a "no semantic
    model" notice instead of a misleading all-clear.
    """
    payload: dict = {"sql": sql, "target_engine": target_engine}
    if expect:
        try:
            payload["expected"] = json.loads(expect)
        except ValueError as exc:
            typer.echo(f"--expect is not valid JSON: {exc}", err=True)
            raise typer.Exit(1) from exc

    resp = api_post("/api/semantic-models/validate-query", json=payload)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        typer.echo(f"Error ({resp.status_code}): {detail or resp.text}", err=True)
        raise typer.Exit(1)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if not body.get("available", True):
        typer.echo(body.get("message") or "No semantic model is available to validate against.")
        return

    status = "VALID" if body.get("valid") else "INVALID"
    typer.echo(f"{status} — {body.get('summary', '')}")

    if body.get("violations"):
        typer.echo("Violations:")
        for v in body["violations"]:
            typer.echo(f"  [{v.get('severity')}] {v.get('name')}: {v.get('reason')}")

    if body.get("post_execution_checks"):
        typer.echo("Cannot be checked before running:")
        for chk in body["post_execution_checks"]:
            typer.echo(f"  {chk.get('name')}: {chk.get('reason')}")

    if body.get("mixed_dialect_warning"):
        typer.echo(f"Warning: {body['mixed_dialect_warning']}")

    if not body.get("locally_executable", True):
        # Name them when the server says which — "one or more used metrics"
        # leaves the analyst to work out which of five is the problem. Falls
        # back to the vague form against an older server that has no
        # `not_executable_metrics`.
        offenders = ", ".join(str(m) for m in (body.get("not_executable_metrics") or []))
        subject = offenders if offenders else "one or more used metrics"
        typer.echo(f"Warning: {subject} — not locally executable on the target engine.")

    if "missing_expected_objects" in body and body["missing_expected_objects"]:
        typer.echo("Missing expected objects:")
        for obj in body["missing_expected_objects"]:
            typer.echo(f"  {obj.get('type')}: {obj.get('name')}")
    if "unexpected_detected_objects" in body and body["unexpected_detected_objects"]:
        typer.echo("Unexpected detected objects:")
        for obj in body["unexpected_detected_objects"]:
            typer.echo(f"  {obj.get('type')}: {obj.get('name')}")


def _fail(resp) -> None:
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    typer.echo(f"Error ({resp.status_code}): {detail or resp.text}", err=True)
    raise typer.Exit(1)


@semantic_model_app.command("context")
def context(
    semantic_type: str = typer.Argument(..., help=f"One of: {', '.join(_SEMANTIC_TYPES)}"),
    id: Optional[List[str]] = typer.Option(  # noqa: A002 - CLI flag name, not shadowing intentionally
        None, "--id", help="Specific object id/name — repeatable. Omit for every object of this type (compact)."
    ),
    model: Optional[List[str]] = typer.Option(
        None,
        "--model",
        help="Restrict to this model id, slug, or name (the `[model]` label shown in output) — "
        "repeatable, case-insensitive. Omit for every accessible model.",
    ),
    limit: int = typer.Option(0, "--limit", min=0, help="Cap objects shown per type (0 = no cap)."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Look up datasets/metrics/relationships from your accessible semantic models.

    Omitting `--id` returns every object of `semantic_type` COMPACTLY (name +
    a short summary); passing one or more `--id` returns the FULL attributes
    of just those objects. Mirrors `GET /api/semantic-models/context` and the
    MCP `get_semantic_context` foundation tool.
    """
    selections = [{"semantic_type": semantic_type, "ids": id or None}]
    params: dict = {"selections": json.dumps(selections)}
    if model:
        params["model_ids"] = model

    resp = api_get("/api/semantic-models/context", params=params)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()

    # `--limit` is a client-side slice, per type, and the truncation is stated
    # out loud (the command-UX standard forbids a silent partial result).
    truncated: dict = {}
    if limit and limit > 0:
        for entry in body.get("results", []):
            objs = entry.get("objects", [])
            if len(objs) > limit:
                truncated[entry.get("semantic_type")] = len(objs)
                entry["objects"] = objs[:limit]

    if as_json:
        # The JSON surface discloses the cap too — a machine caller must not
        # read a sliced list as "these are all of them" (command-UX standard:
        # silent partial scope is forbidden; Devin review on #1398).
        if truncated:
            body["truncated"] = {"limit": limit, "total_by_type": truncated}
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if body.get("unknown_types"):
        typer.echo(f"Unknown semantic type(s): {', '.join(body['unknown_types'])}", err=True)

    for entry in body.get("results", []):
        objects = entry.get("objects", [])
        stype = entry.get("semantic_type")
        total = truncated.get(stype)
        count = f"{len(objects)} of {total}" if total else f"{len(objects)}"
        typer.echo(f"{stype} ({entry.get('mode')}): {count} object(s)")
        if not objects and id:
            typer.echo(f"  no match for --id {', '.join(id)} — omit --id to list every {stype} compactly", err=True)
        for obj in objects:
            if entry.get("mode") == "compact":
                typer.echo(f"  {obj.get('name')} [{obj.get('model')}] — {obj.get('summary') or '(no summary)'}")
            else:
                typer.echo(f"  {obj.get('name')} [{obj.get('model')}]")
                typer.echo(
                    f"    {json.dumps({k: v for k, v in obj.items() if k not in ('name', 'model')}, default=str)}"
                )
        if total:
            typer.echo(f"  … {total - len(objects)} more — raise --limit or narrow with --id/--model")


@coverage_app.command("tables")
def coverage_tables(
    limit: int = typer.Option(0, "--limit", min=0, help="Cap the number of tables listed (0 = no cap)."),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """List registered tables with NO valid semantic model describing them.

    Source-agnostic — covers a table bound through any semantic source
    (Keboola, git, manual, upload, connection), not just Keboola. Mirrors
    `GET /api/admin/semantic-coverage`. Admin-only; distinct from `agnes
    admin semantic-layer coverage`, which predicts live Keboola metric
    importability rather than reading stored coverage, and from the bare
    `agnes semantic-model coverage` (F4.1's cross-domain, per-source grid,
    which this narrower table-level flat list sits alongside under the same
    `coverage` group).
    """
    resp = api_get("/api/admin/semantic-coverage")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    tables = body.get("tables") or []
    if as_json:
        if limit and limit > 0 and len(tables) > limit:
            body["tables"] = tables[:limit]
            body["truncated"] = {"limit": limit, "total": len(tables)}
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if not tables:
        typer.echo("Every registered table has semantic-layer coverage.")
        return

    shown = tables[:limit] if limit and limit > 0 else tables
    typer.echo(f"{len(tables)} table(s) with no semantic-layer coverage:")
    for row in shown:
        typer.echo(f"  {row.get('id')} ({row.get('name')})")
    if limit and limit > 0 and len(tables) > limit:
        typer.echo(f"  … and {len(tables) - limit} more — raise --limit to see them")


@semantic_model_app.command("schema")
def schema(
    semantic_type: List[str] = typer.Argument(..., help=f"One or more of: {', '.join(_SEMANTIC_TYPES)}"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Show the vendored Apache Ossie JSON Schema for one or more object types.

    Served straight from the schema every semantic-model document is
    validated against — never a hand-written copy. Mirrors
    `GET /api/semantic-models/schema` and the MCP `get_semantic_schema`
    foundation tool.
    """
    resp = api_get("/api/semantic-models/schema", params={"semantic_types": semantic_type})
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if body.get("unknown_types"):
        typer.echo(f"Unknown semantic type(s): {', '.join(body['unknown_types'])}", err=True)
    for type_name, ref in body.get("types", {}).items():
        def_name = ref.get("$ref", "").rsplit("/", 1)[-1]
        typer.echo(f"=== {type_name} ({def_name}) ===")
        typer.echo(json.dumps(body["$defs"].get(def_name, {}), indent=2, default=str))


# ---------------------------------------------------------------------------
# Cross-domain coverage (F4.1) — `agnes semantic-model coverage …`
# ---------------------------------------------------------------------------


def _print_coverage(*, source: Optional[str], as_json: bool) -> None:
    """Render `GET /api/admin/semantic-model/coverage` as a source × domain grid.

    Server-side computation, so there is no local/server scope to choose:
    the report is a fact about the SERVER's registry and connections, and a
    laptop has neither. (Command-UX standard: no new boolean scope flag —
    there is no second scope to name here.)
    """
    params = {"source": source} if source else None
    resp = api_get(_COVERAGE_PATH, params=params)
    if resp.status_code == 501:
        typer.echo(
            "Coverage needs the Postgres app-state backend — this instance still runs the frozen "
            "DuckDB backend. Migrate it (see docs/migrations.md) to use this report.",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    sources = body.get("sources") or []
    if not sources:
        hint = f" for --source {source}" if source else ""
        typer.echo(f"No data sources to report on{hint}.")
        typer.echo("  Connect one at /admin/data-sources, or `agnes admin connection add`.")
        return

    widths = {d: max(len(d), 12) for d in _COVERAGE_DOMAINS}
    name_width = max(max(len(s.get("name") or s["source_id"]) for s in sources), 6)
    header = "SOURCE".ljust(name_width) + "  " + "  ".join(d.ljust(widths[d]) for d in _COVERAGE_DOMAINS)
    typer.echo(header)
    for entry in sources:
        domains = entry.get("domains") or {}
        cells = []
        for domain in _COVERAGE_DOMAINS:
            status = (domains.get(domain) or {}).get("status") or "?"
            cells.append(_STATUS_GLYPHS.get(status, status).ljust(widths[domain]))
        typer.echo((entry.get("name") or entry["source_id"]).ljust(name_width) + "  " + "  ".join(cells))

    # The grid says WHICH cell is short; the lines below say what to do about
    # it. A grid on its own is a scoreboard, and the report is not one.
    for entry in sources:
        gaps = [
            (domain, cell)
            for domain, cell in (entry.get("domains") or {}).items()
            if (cell or {}).get("status") in ("missing", "partial")
        ]
        if not gaps:
            continue
        typer.echo("")
        typer.echo(f"{entry.get('name') or entry['source_id']} ({entry.get('source_type') or 'unknown'}):")
        for domain, cell in gaps:
            action = cell.get("action") or {}
            suffix = f" → {action['label']}: {action['href']}" if action.get("href") else ""
            typer.echo(f"  {domain}: {cell.get('detail') or cell.get('status')}{suffix}")


@coverage_app.callback(invoke_without_command=True)
def coverage(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(
        None, "--source", help="Only this source connection id (`__local__` for tables with no connection)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """What each connected data source still lacks (admin only).

    One row per source, one column per domain — semantic model, metrics,
    glossary, skill, agent, knowledge base — as
    `ok` / `partial` / `MISSING` / `n/a`. `n/a` is not a gap: it means the
    domain cannot be filled for that source type in this build (e.g. no
    semantic-layer adapter exists for it), so it is deliberately not
    reported as work to do.

    Mirrors `GET /api/admin/semantic-model/coverage` and the MCP
    `semantic_model_coverage` tool. `coverage show` is the explicit form of
    this bare invocation.
    """
    if ctx.invoked_subcommand is not None:
        return
    _print_coverage(source=source, as_json=as_json)


@coverage_app.command("show")
def coverage_show(
    source: Optional[str] = typer.Option(
        None, "--source", help="Only this source connection id (`__local__` for tables with no connection)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Explicit form of the bare `agnes semantic-model coverage` (same output)."""
    _print_coverage(source=source, as_json=as_json)


@coverage_app.command("tag")
def coverage_tag(
    resource_type: str = typer.Argument(..., help=f"One of: {', '.join(_COVERAGE_TAGGABLE)}"),
    resource_id: str = typer.Argument(..., help="Same id format the RBAC grant for that type uses"),
    source_id: str = typer.Argument(..., help="source_connections.id this resource is ABOUT"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Record that a skill / agent / knowledge domain is about a data source.

    This is the only input the coverage report cannot derive: those three
    live in their own tables with no notion of a source. Mirrors
    `POST /api/admin/semantic-model/coverage/tags` and the MCP
    `semantic_model_coverage_tag` tool.
    """
    if resource_type not in _COVERAGE_TAGGABLE:
        typer.echo(
            f"Unknown resource type {resource_type!r} — expected one of {', '.join(_COVERAGE_TAGGABLE)}", err=True
        )
        raise typer.Exit(1)

    resp = api_post(
        _COVERAGE_TAGS_PATH,
        json={"resource_type": resource_type, "resource_id": resource_id, "source_id": source_id},
    )
    if resp.status_code == 409:
        typer.echo(f"Already tagged: {resource_type} {resource_id!r} → {source_id}", err=True)
        typer.echo("  See the current tags: agnes semantic-model coverage --json", err=True)
        raise typer.Exit(1)
    if resp.status_code == 404:
        typer.echo(f"No source connection {source_id!r}.", err=True)
        typer.echo("  List them: agnes admin connection list", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 201):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Tagged: {resource_type} {resource_id!r} → {source_id} (tag {body.get('id')})")


@coverage_app.command("untag")
def coverage_untag(
    tag_id: str = typer.Argument(..., help="Tag id from `agnes semantic-model coverage --json`"),
):
    """Remove one source tag.

    Mirrors `DELETE /api/admin/semantic-model/coverage/tags/{tag_id}` and the
    MCP `semantic_model_coverage_untag` tool.
    """
    resp = api_delete(f"{_COVERAGE_TAGS_PATH}/{tag_id}")
    if resp.status_code == 404:
        typer.echo(f"No coverage tag {tag_id!r}.", err=True)
        typer.echo("  Find the id: agnes semantic-model coverage --json", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Untagged: {tag_id}")


# ---------------------------------------------------------------------------
# Feedback (F4.5) — `agnes semantic-model feedback …`
# ---------------------------------------------------------------------------


def _fail_needs_postgres(resp, what: str) -> None:
    """Turn the PG-only 501 into the one sentence that names the fix.

    Feedback lives in a Postgres-only table (A3 PG-first ratchet), so an
    instance still on the frozen DuckDB app-state backend cannot store a
    report at all. Printing the raw 501 body would leave the reader guessing
    that their question was malformed.
    """
    if resp.status_code != 501:
        return
    typer.echo(
        f"{what} needs the Postgres app-state backend — this instance still runs the frozen "
        "DuckDB backend. Migrate it (see docs/migrations.md) to use the feedback queue.",
        err=True,
    )
    raise typer.Exit(1)


@feedback_app.command("submit")
def feedback_submit(
    question: str = typer.Argument(..., help="The question whose answer looked wrong, as it was asked"),
    sql: Optional[str] = typer.Option(None, "--sql", help="The SQL that produced the suspect answer, if there was one"),
    metric: Optional[str] = typer.Option(None, "--metric", help="Metric id the answer relied on (e.g. revenue/mrr)"),
    comment: Optional[str] = typer.Option(None, "--comment", help="What looks wrong about it"),
    model_hash: Optional[str] = typer.Option(
        None,
        "--model-hash",
        help="content_hash of the semantic model that produced the answer — pins the report to a document "
        "version, so a later rewrite does not make it look like a complaint about the current text. For "
        "scripted callers that know it (`agnes admin semantic-model show <slug> --json` reports it).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Report that an answer looked wrong or unsupported (anyone signed in).

    Not an admin command: the person who ran the analysis is the one who sees
    the bad number. Only the question is required — a concept nobody ever
    defined has no SQL and no metric to name, and that is exactly the case
    most worth reporting.

    Mirrors `POST /api/semantic-feedback` and the MCP `flag_semantic_issue`
    tool.
    """
    payload = {
        "question": question,
        "sql": sql,
        "metric_id": metric,
        "comment": comment,
        "model_content_hash": model_hash,
    }
    resp = api_post(_FEEDBACK_SUBMIT_PATH, json={k: v for k, v in payload.items() if v is not None})
    _fail_needs_postgres(resp, "Filing feedback")
    if resp.status_code not in (200, 201):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Filed: {body.get('id')} ({body.get('status', 'open')})")
    typer.echo("  An admin sees it at /admin/semantic-layer?tab=feedback")


@feedback_app.command("list")
def feedback_list(
    status: Optional[str] = typer.Option(
        None, "--status", help=f"Only this status ({', '.join(_FEEDBACK_STATUSES)}); omit for all"
    ),
    limit: int = typer.Option(0, "--limit", min=0, help="Cap rows shown (0 = no cap)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """The report queue, newest first (admin only).

    Mirrors `GET /api/admin/semantic-feedback` and the MCP
    `semantic_feedback_list` tool.
    """
    if status is not None and status not in _FEEDBACK_STATUSES:
        typer.echo(f"Unknown status {status!r} — expected one of {', '.join(_FEEDBACK_STATUSES)}", err=True)
        raise typer.Exit(1)

    resp = api_get(_FEEDBACK_ADMIN_PATH, params={"status": status} if status else None)
    _fail_needs_postgres(resp, "The feedback queue")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    items = body.get("items") or []
    total = len(items)
    truncated = limit and limit > 0 and total > limit
    if truncated:
        items = items[:limit]

    if as_json:
        # The JSON surface discloses the cap too — a machine caller must not
        # read a sliced list as "these are all of them" (command-UX standard).
        out = {"items": items, "count": len(items)}
        if truncated:
            out["truncated"] = {"limit": limit, "total": total}
        typer.echo(json.dumps(out, indent=2, default=str))
        return

    if not items:
        scope = f" with status {status}" if status else ""
        typer.echo(f"No feedback reports{scope}.")
        typer.echo('  File one: agnes semantic-model feedback submit "<question>"')
        return

    for item in items:
        typer.echo(
            f"{item.get('id')}  {(item.get('status') or '?'):<12} "
            f"{(item.get('created_by') or 'unknown'):<28} {item.get('question')}"
        )
        if item.get("metric_id"):
            typer.echo(f"    metric: {item['metric_id']}")
        if item.get("comment"):
            typer.echo(f"    comment: {item['comment']}")
        if item.get("sql"):
            typer.echo(f"    sql: {item['sql']}")
        if item.get("resolution_note"):
            typer.echo(f"    resolved by {item.get('resolved_by')}: {item['resolution_note']}")
    if truncated:
        typer.echo(f"… {total - len(items)} more — raise --limit or narrow with --status")


@feedback_app.command("resolve")
def feedback_resolve(
    feedback_id: str = typer.Argument(..., help="Report id from `agnes semantic-model feedback list`"),
    note: Optional[str] = typer.Option(None, "--note", help="What was done about it — recorded on the report"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Close one report, on the record (admin only).

    Mirrors `POST /api/admin/semantic-feedback/{id}/resolve` and the MCP
    `semantic_feedback_resolve` tool.
    """
    resp = api_post(f"{_FEEDBACK_ADMIN_PATH}/{feedback_id}/resolve", json={"resolution_note": note})
    _fail_needs_postgres(resp, "Resolving feedback")
    if resp.status_code == 404:
        typer.echo(f"No semantic feedback {feedback_id!r}.", err=True)
        typer.echo("  Find the id: agnes semantic-model feedback list", err=True)
        raise typer.Exit(1)
    if resp.status_code == 409:
        typer.echo(f"Already resolved: {feedback_id}", err=True)
        typer.echo("  See who closed it: agnes semantic-model feedback list --status resolved", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    typer.echo(f"Resolved: {feedback_id} by {body.get('resolved_by')}")


# ---------------------------------------------------------------------------
# Mutes (F4.3) — `agnes semantic-model mute|unmute|mutes`
# ---------------------------------------------------------------------------


def _fail_mute_needs_postgres(resp, what: str) -> None:
    """Turn the PG-only 501 into the one sentence that names the fix."""
    if resp.status_code != 501:
        return
    typer.echo(
        f"{what} needs the Postgres app-state backend — this instance still runs the frozen "
        "DuckDB backend. Migrate it (see docs/migrations.md) to mute checks.",
        err=True,
    )
    raise typer.Exit(1)


def _detail(resp) -> dict:
    """The typed `detail` object, or `{}` — never an exception.

    A non-JSON error body (a proxy's HTML 502, say) must not turn a refusal
    into a traceback: the caller still needs to print SOMETHING actionable.
    """
    try:
        detail = resp.json().get("detail")
    except Exception:
        return {}
    return detail if isinstance(detail, dict) else {}


@semantic_model_app.command("mute")
def mute(
    scope: str = typer.Argument(..., help=f"What to silence — one of: {', '.join(_MUTE_SCOPE_FORMS)}"),
    reason: Optional[str] = typer.Option(
        None, "--reason", help="Why it is expected — stored on the mute and shown wherever it appears"
    ),
    expires: Optional[str] = typer.Option(
        None,
        "--expires",
        help="ISO-8601 instant to un-silence itself at (e.g. 2026-10-01T00:00:00Z); omit = until unmuted",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Silence one semantic-layer check you already know about (admin only).

    Muting is legitimate — a gap you have read, judged expected and scheduled
    should not shout on every page load. Muting ANONYMOUSLY is not: your
    identity, the time and (if you give one) your reason are stored with it and
    shown wherever the mute appears, so nobody later has to guess whether the
    check was fixed or hidden. Pass `--reason` unless there is genuinely
    nothing to say.

    Scopes: `domain:<domain>` silences one domain across every source,
    `source:<id>` silences one source entirely, and
    `source:<id>:domain:<domain>` silences the single cell. The coverage
    domains are semantic, metrics, glossary, skill, agent, knowledge_base
    (`agnes semantic-model coverage` prints them as columns); `__local__` is
    the source id of the bucket for tables with no connection.

    Mirrors `POST /api/admin/semantic-layer/mutes` and the MCP
    `mute_semantic_check` tool.
    """
    payload = {"scope": scope, "reason": reason, "expires_at": expires}
    resp = api_post(_MUTES_PATH, json={k: v for k, v in payload.items() if v is not None})
    _fail_mute_needs_postgres(resp, "Muting a check")

    if resp.status_code == 400:
        detail = _detail(resp)
        typer.echo(detail.get("message") or f"Could not mute {scope!r}.", err=True)
        if detail.get("error") == "invalid_scope":
            typer.echo(f"  Expected one of: {', '.join(_MUTE_SCOPE_FORMS)}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 404:
        typer.echo(f"No such data source in scope {scope!r}.", err=True)
        typer.echo("  List them: agnes admin connection list", err=True)
        raise typer.Exit(1)
    if resp.status_code == 409:
        typer.echo(f"Already muted: {_detail(resp).get('message') or scope}", err=True)
        typer.echo("  See it: agnes semantic-model mutes", err=True)
        raise typer.Exit(1)
    if resp.status_code == 422:
        # The only field the server parses for us is `--expires`; a 422 here is
        # almost always an unparseable instant, so name the format rather than
        # dumping pydantic's field-path list at the reader.
        typer.echo(f"Could not read --expires {expires!r} — expected ISO-8601, e.g. 2026-10-01T00:00:00Z.", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 201):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    until = f" until {body.get('expires_at')}" if body.get("expires_at") else ""
    typer.echo(f"Muted: {body.get('scope')}{until} (mute {body.get('id')})")
    if not body.get("reason"):
        # Not an error — the API allows it — but the whole point of the record
        # is the next reader, and "muted by you, no reason given" is a thin
        # thing to inherit.
        typer.echo("  No reason recorded — add one with: agnes semantic-model unmute <id> then re-mute with --reason")


@semantic_model_app.command("unmute")
def unmute(
    mute_id: str = typer.Argument(..., help="Mute id from `agnes semantic-model mutes`"),
):
    """Let one check report again (admin only).

    Mirrors `DELETE /api/admin/semantic-layer/mutes/{mute_id}` and the MCP
    `unmute_semantic_check` tool.
    """
    resp = api_delete(f"{_MUTES_PATH}/{mute_id}")
    _fail_mute_needs_postgres(resp, "Unmuting a check")
    if resp.status_code == 404:
        typer.echo(f"No semantic-layer mute {mute_id!r}.", err=True)
        typer.echo("  Find the id: agnes semantic-model mutes", err=True)
        raise typer.Exit(1)
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Unmuted: {mute_id}")


@semantic_model_app.command("mutes")
def mutes(
    include_expired: bool = typer.Option(
        False, "--include-expired", help="Also show mutes that have lapsed (the record outlives the silence)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """What is currently silenced, and who silenced it (admin only).

    Server-side state, so there is no local/server scope to choose — mutes are
    a fact about the instance's own health report.

    Mirrors `GET /api/admin/semantic-layer/mutes` and the MCP
    `semantic_mutes_list` tool.
    """
    resp = api_get(_MUTES_PATH, params={"include_expired": "true"} if include_expired else None)
    _fail_mute_needs_postgres(resp, "The muted-check list")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    items = body.get("items") or []
    if not items:
        typer.echo("No muted checks." if include_expired else "No muted checks (nothing is being silenced).")
        typer.echo('  Silence one you already know about: agnes semantic-model mute domain:<domain> --reason "…"')
        return

    for item in items:
        expires = item.get("expires_at")
        window = f"until {expires}" if expires else "no expiry"
        typer.echo(f"{item.get('id')}  {(item.get('scope') or '?'):<44} {window}")
        # Who and when on their own line, always printed: they are the record,
        # not a detail the reader has to ask for with a flag.
        typer.echo(f"    muted by {item.get('muted_by') or 'unknown'} at {item.get('muted_at')}")
        typer.echo(f"    reason: {item.get('reason') or '(none given)'}")


# ---------------------------------------------------------------------------
# Health roll-up (F4.2) — `agnes semantic-model health`
# ---------------------------------------------------------------------------


@semantic_model_app.command("health")
def health(as_json: bool = typer.Option(False, "--json", help="Emit raw JSON")):
    """Is the semantic layer trustworthy right now (admin only)?

    Sync failures, models whose source was deleted or renamed away from under
    them, documents that failed validation, three static quality checks (a
    metric with no description, one name defined twice with a different
    formula, a cross-dataset metric with no declared relationship), a roll-up
    of `coverage`'s missing/partial counts, and every active mute.

    Mirrors `GET /api/admin/semantic-layer/health` and the MCP
    `semantic_layer_health` tool.
    """
    resp = api_get(_HEALTH_PATH)
    _fail_mute_needs_postgres(resp, "The health report")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    sources = body.get("sources") or []
    failed = [s for s in sources if s.get("last_sync_status") == "error"]
    if failed:
        typer.echo(f"Sync failures ({len(failed)}):")
        for s in failed:
            typer.echo(f"  {s.get('name') or s['source_id']}: {s.get('last_sync_error') or 'unknown error'}")
        typer.echo("")

    orphaned = body.get("orphaned_models") or []
    if orphaned:
        typer.echo(f"Models whose source is gone ({len(orphaned)}):")
        for m in orphaned:
            typer.echo(f"  {m.get('slug') or m['model_id']} (source_ref={m.get('source_ref')})")
        typer.echo("")

    invalid = body.get("invalid_models") or []
    if invalid:
        typer.echo(f"Invalid documents ({len(invalid)}):")
        for m in invalid:
            typer.echo(f"  {m.get('slug') or m['model_id']}: {m.get('validation_errors')}")
        typer.echo("")

    missing_desc = body.get("metrics_missing_description") or []
    if missing_desc:
        typer.echo(f"Metrics with no description ({len(missing_desc)}):")
        for m in missing_desc:
            typer.echo(f"  {m.get('name')}")
        typer.echo("")

    dupes = body.get("duplicate_metric_names") or []
    if dupes:
        typer.echo(f"Metric names defined more than once, differently ({len(dupes)}):")
        for d in dupes:
            typer.echo(f"  {d.get('name')} — {len(d.get('expressions') or [])} different formula(s)")
        typer.echo("")

    missing_rel = body.get("metrics_missing_relationships") or []
    if missing_rel:
        typer.echo(f"Cross-dataset metrics with no declared relationship ({len(missing_rel)}):")
        for m in missing_rel:
            typer.echo(f"  {m.get('metric_name')} spans {', '.join(m.get('datasets') or [])}")
        typer.echo("")

    summary = body.get("coverage_summary") or {}
    typer.echo(f"Coverage: {summary.get('missing_count', 0)} missing, {summary.get('partial_count', 0)} partial")

    mutes_list = body.get("mutes") or []
    if mutes_list:
        typer.echo(f"Muted ({len(mutes_list)} of the above are silenced — see `agnes semantic-model mutes`)")

    if not (failed or orphaned or invalid or missing_desc or dupes or missing_rel):
        typer.echo("No sync failures, disconnected models, or invalid documents.")
