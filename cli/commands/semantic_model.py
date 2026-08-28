"""`agnes semantic-model` — the semantic layer for anyone signed in.

Everything here is reachable without admin: find a model (`search`), read its
metadata (`show`) or its document (`export`), check a document (`validate`) or
a query (`validate-query`) before either reaches the server, look objects up
(`context`), read the schema they must conform to (`schema`), propose a change
(`apply`), and report an answer that looked wrong (`feedback`).

RBAC is per-model, not per-command: `search`/`show`/`export`/`context`/
`validate-query` read whatever `status='valid'` models the caller can already
reach (a Data Package grant or a direct model grant). `validate` and `schema`
need nothing at all — `validate` never contacts a server.

Two commands are easy to confuse, so both helps say so:

  * ``validate <file>``  — is this DOCUMENT well-formed? Offline, no token.
  * ``validate-query <sql>`` — does this QUERY obey the models I can read?

The admin half — importing, deleting, sources, coverage, health, mutes — is
``agnes admin semantic`` (:mod:`cli.commands.admin_semantic`). The admin-only
commands that used to live in THIS group (``coverage``, ``health``, ``mute``,
``mutes``, ``unmute``) survive here for one release as hidden aliases; they
were always admin-gated on the API side, so their home was simply wrong.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List, Optional

import typer

from cli.client import api_get, api_post
from cli.commands import admin_semantic as _admin
from cli.deprecation import deprecated_alias, deprecation_notice

semantic_model_app = typer.Typer(
    help="The semantic layer: find models, read their documents, validate queries, propose changes"
)

_SEMANTIC_TYPES = ("dataset", "metric", "relationship")

_SEARCH_PATH = "/api/semantic-models/search"
# The public search endpoint caps `limit` server-side; `show` resolves a slug
# through it, so it asks for the ceiling rather than a page it might miss on.
_SEARCH_LIMIT_MAX = 100

# Feedback (F4.5) — "that answer looked wrong". `submit` is open to any
# signed-in caller; `list`/`resolve` are the admin side of the same queue.
# All three ship together on purpose: a report must be fileable from every
# surface (UI, chat, MCP, CLI), not only the ones an admin uses — and the
# queue stays beside the form so an admin working a report never has to
# switch groups mid-task.
feedback_app = typer.Typer(help="Report a wrong/unsupported answer, and work the report queue")
semantic_model_app.add_typer(feedback_app, name="feedback")

_FEEDBACK_SUBMIT_PATH = "/api/semantic-feedback"
_FEEDBACK_ADMIN_PATH = "/api/admin/semantic-feedback"
_FEEDBACK_STATUSES = ("open", "acknowledged", "resolved")


def _fail(resp) -> None:
    try:
        detail = resp.json().get("detail")
    except Exception:
        detail = None
    typer.echo(f"Error ({resp.status_code}): {detail or resp.text}", err=True)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Find and read — `search` / `show` / `export`
# ---------------------------------------------------------------------------


@semantic_model_app.command("search")
def search(
    term: str = typer.Argument(..., help="Substring to look for in a model's slug, name or description"),
    limit: int = typer.Option(
        10, "--limit", min=1, max=_SEARCH_LIMIT_MAX, help=f"Max models to return (server cap: {_SEARCH_LIMIT_MAX})"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """Find semantic models you can read, by substring.

    Case-insensitive match over slug/name/description, filtered to the models
    your grants actually reach (admins see everything). Reads
    `GET /api/semantic-models/search` — the same endpoint the MCP
    `semantic_model_search` tool uses.

    There is no local/server scope to choose: a semantic model is server
    state, and a laptop holds only the rendered read-only cache `agnes pull`
    writes under `<workspace>/semantic/`. The admin listing that also shows
    drafts and invalid documents is `agnes admin semantic list`.
    """
    resp = api_get(_SEARCH_PATH, params={"q": term, "limit": limit})
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    models = body.get("models") or []
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return

    if not models:
        typer.echo(f"No semantic model you can read matches {term!r}.")
        typer.echo("  Browse what is modelled instead: agnes semantic-model context dataset")
        typer.echo("  Ask an admin to grant you the Data Package a model is linked to, or to import one.")
        return

    typer.echo(f"{len(models)} semantic model(s) matching {term!r}:")
    for row in models:
        summary = row.get("description") or row.get("name") or ""
        typer.echo(f"  {(row.get('slug') or '?'):<24} {(row.get('source') or '?'):<10} {summary}")
    if len(models) >= limit:
        # Command-UX standard: a partial result never passes for a whole one.
        typer.echo(f"  … this is the first {limit} — raise --limit or narrow the term")
    typer.echo("  Read one: agnes semantic-model show <slug> / agnes semantic-model export <slug>")


@semantic_model_app.command("show")
def show(
    slug: str = typer.Argument(..., help="Model slug (or id) from `agnes semantic-model search`"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """One model's metadata — provenance, status, content hash.

    Resolved through the same public search endpoint `search` uses, so it
    needs no admin endpoint and shows exactly the models you may read. The
    document body itself is `agnes semantic-model export <slug>`; the
    admin view that also reaches drafts and invalid documents is
    `agnes admin semantic show <id|slug>`.
    """
    resp = api_get(_SEARCH_PATH, params={"q": slug, "limit": _SEARCH_LIMIT_MAX})
    if resp.status_code != 200:
        _fail(resp)

    needle = slug.casefold()
    models = resp.json().get("models") or []
    row = next(
        (
            m
            for m in models
            if str(m.get("slug") or "").casefold() == needle or str(m.get("id") or "").casefold() == needle
        ),
        None,
    )
    if row is None:
        typer.echo(f"No semantic model {slug!r} you can read.", err=True)
        if len(models) >= _SEARCH_LIMIT_MAX:
            # The lookup rides a capped substring search, so "not found" and
            # "past the cap" are not the same thing and must not read the same.
            typer.echo(
                f"  {len(models)} models contain that text — the server caps this search at "
                f"{_SEARCH_LIMIT_MAX}, so an exact match may be past the cap. Narrow it:",
                err=True,
            )
        typer.echo(f"  agnes semantic-model search {slug}", err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps(row, indent=2, default=str))
        return

    typer.echo(f"ID:           {row.get('id')}")
    typer.echo(f"Slug:         {row.get('slug')}")
    typer.echo(f"Name:         {row.get('name')}")
    typer.echo(f"Description:  {row.get('description') or '(none)'}")
    typer.echo(f"Source:       {row.get('source')} (source_ref={row.get('source_ref')})")
    typer.echo(f"Status:       {row.get('status')}")
    typer.echo(f"Spec version: {row.get('spec_version')}")
    typer.echo(f"Content hash: {row.get('content_hash')}")
    typer.echo(f"  Read the document: agnes semantic-model export {row.get('slug')}")


@semantic_model_app.command("export")
def export_model(
    slug: str = typer.Argument(..., help="Model slug"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write to this file instead of stdout"),
):
    """Print one model's stored document, byte for byte.

    Comments and key order survive — the server never re-serializes it. Reads
    the public, resource-gated `GET /api/semantic-models/{slug}.yaml`, so this
    is not an admin operation: anyone who may read the model may read its
    document. Round-trip for an edit: export, change, then
    `agnes semantic-model apply --expect-hash <content_hash>`.
    """
    resp = api_get(f"/api/semantic-models/{slug}.yaml")
    if resp.status_code == 404:
        typer.echo(f"Semantic model not found: {slug}", err=True)
        typer.echo(f"  Look for it: agnes semantic-model search {slug}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 403:
        typer.echo(f"Access denied: {resp.json().get('detail', resp.text)}", err=True)
        typer.echo("  Ask an admin to link the model to a Data Package you are granted.", err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        _fail(resp)
    text = resp.text
    if output:
        Path(output).write_text(text)
        typer.echo(f"Wrote {output}")
        return
    typer.echo(text, nl=not text.endswith("\n"))


# ---------------------------------------------------------------------------
# Check before you commit — `validate` (document) / `validate-query` (SQL)
# ---------------------------------------------------------------------------


@semantic_model_app.command("validate")
def validate_document(
    path: str = typer.Argument(..., help="Path to a local Ossie YAML document"),
):
    """Schema-check a local DOCUMENT against the vendored Ossie spec.

    Runs entirely offline: no server, no token, no admin. An author fixing a
    document should not need a reachable instance to iterate — which is why
    this is not an admin command.

    NOT `validate-query`: that one checks a SQL statement against the models
    you can read, and needs a server. This one only asks whether a file is a
    well-formed semantic-model document.
    """
    p = Path(path)
    if not p.exists():
        typer.echo(f"Path not found: {path}", err=True)
        raise typer.Exit(1)

    from src.semantic.document_validation import validate_document as _validate

    result = _validate(p.read_text())
    if result.ok:
        typer.echo(f"OK — spec version {result.spec_version}")
        return
    typer.echo("Invalid document:", err=True)
    for e in result.errors:
        typer.echo(f"  {e}", err=True)
    raise typer.Exit(1)


@semantic_model_app.command("apply")
def apply(
    path: str = typer.Argument(..., help="Path to an Ossie document (YAML)"),
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
    `agnes semantic-model validate <file>`.
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
            typer.echo("  Pre-check locally: agnes semantic-model validate <file>", err=True)
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
    """Validate a SQL QUERY against the semantic layer: constraint violations,
    dialect fit, and (optionally) which expected datasets/metrics/
    relationships the query hits.

    Best-effort text matching against declared semantic-model documents —
    not SQL parsing (see the server-side validator's own LIMITATIONS
    docstring). Reads every `status='valid'` semantic model you can access;
    if none exist (or none are accessible to you), prints a "no semantic
    model" notice instead of a misleading all-clear.

    NOT `semantic-model validate`: that one schema-checks a DOCUMENT file
    offline. This one needs a server and checks a statement.
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
        _fail(resp)

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
        typer.echo("Warning: one or more used metrics are not locally executable on the target engine.")

    if "missing_expected_objects" in body and body["missing_expected_objects"]:
        typer.echo("Missing expected objects:")
        for obj in body["missing_expected_objects"]:
            typer.echo(f"  {obj.get('type')}: {obj.get('name')}")
    if "unexpected_detected_objects" in body and body["unexpected_detected_objects"]:
        typer.echo("Unexpected detected objects:")
        for obj in body["unexpected_detected_objects"]:
            typer.echo(f"  {obj.get('type')}: {obj.get('name')}")


# ---------------------------------------------------------------------------
# Agent read-parity — `context` / `schema`
# ---------------------------------------------------------------------------


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
        "scripted callers that know it (`agnes semantic-model show <slug> --json` reports it).",
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

    Stays beside `submit` rather than moving to `agnes admin semantic`: an
    admin working a report reads the same queue the reporter filed into, and
    splitting the pair across two groups would make the round trip a group
    switch. Mirrors `GET /api/admin/semantic-feedback` and the MCP
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
        out: dict[str, Any] = {"items": items, "count": len(items)}
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
# Deprecated aliases — the admin-gated commands that used to live here
#
# `coverage`, `health`, `mute`, `mutes` and `unmute` all call `require_admin`
# endpoints; sitting in the any-user group advertised authority the caller did
# not have. They moved to `agnes admin semantic …` and survive here, hidden,
# for one release. Each delegates to the SAME function the new path runs, so
# an alias cannot drift from what it replaces.
# ---------------------------------------------------------------------------

for _name, _fn in (
    ("health", _admin.health),
    ("mute", _admin.mute),
    ("mutes", _admin.mutes),
    ("unmute", _admin.unmute),
):
    deprecated_alias(
        semantic_model_app,
        name=_name,
        old=f"semantic-model {_name}",
        new=f"admin semantic {_name}",
        fn=_fn,
    )

# `coverage` is a group, so its notice rides the group callback: Click runs a
# group callback before the subcommand, which covers the bare invocation and
# `tables`/`show`/`tag`/`untag` alike with one line.
_coverage_alias_app = typer.Typer(
    help="(deprecated alias of `agnes admin semantic coverage`)",
    invoke_without_command=True,
)
semantic_model_app.add_typer(_coverage_alias_app, name="coverage", hidden=True)


@_coverage_alias_app.callback(invoke_without_command=True)
def _coverage_alias(
    ctx: typer.Context,
    source: Optional[str] = typer.Option(
        None, "--source", help="Only this source connection id (`__local__` for tables with no connection)"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON"),
):
    """(deprecated alias of `agnes admin semantic coverage`)"""
    sub = ctx.invoked_subcommand
    suffix = f" {sub}" if sub else ""
    deprecation_notice(f"semantic-model coverage{suffix}", f"admin semantic coverage{suffix}")
    if sub is not None:
        return
    _admin.coverage_show(source=source, as_json=as_json)


for _name, _fn in (
    ("show", _admin.coverage_show),
    ("tables", _admin.coverage_tables),
    ("tag", _admin.coverage_tag),
    ("untag", _admin.coverage_untag),
):
    _coverage_alias_app.command(_name, hidden=True)(_fn)
