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

from cli.client import api_get, api_post

semantic_model_app = typer.Typer(help="Read the semantic layer: validate queries, browse context, inspect schema")

_SEMANTIC_TYPES = ("dataset", "metric", "relationship")


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
        typer.echo("Warning: one or more used metrics are not locally executable on the target engine.")

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
