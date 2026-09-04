"""`agnes admin knowledge packaging run|status` — knowledge-artifact
packaging (K3, #798; TCRD-296 synthesis C.15).

Talks to the live server through `POST /api/admin/run-knowledge-packaging`
(enqueue) and `GET /api/admin/knowledge-packaging/status` (observability).
The pass itself runs as the `knowledge-packaging` worker job kind — this
CLI never runs packaging in-process, same as `agnes admin analytics
migrate` (`cli/commands/admin_analytics.py`), which this module mirrors.
"""

from __future__ import annotations

import json as _json

import typer

from cli.client import api_get, api_post

packaging_app = typer.Typer(help="Per-collection knowledge.duckdb artifact packaging (K3)")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        typer.echo(f"Error ({resp.status_code}): {_json.dumps(detail)}", err=True)
    else:
        msg = detail if isinstance(detail, str) else (resp.text or f"HTTP {resp.status_code}")
        typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@packaging_app.command("run")
def run(
    as_json: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
) -> None:
    """Enqueue a knowledge-packaging run (rebuilds any Collection whose
    chunk content changed since the last pass).

    Runs as a worker job, not synchronously — poll `agnes admin jobs show
    <job_id>` or `agnes admin knowledge packaging status` for the result.
    A run already in flight is reported as an in-progress error (409),
    not a second redundant run. A process/instance with no worker role
    fails clean with a typed 501 instead of queueing work nothing will
    ever claim.
    """
    resp = api_post("/api/admin/run-knowledge-packaging")
    if resp.status_code not in (202, 409):
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(_json.dumps(body))
        if resp.status_code == 409:
            raise typer.Exit(1)
        return

    if resp.status_code == 409:
        detail = body.get("detail") if isinstance(body, dict) else body
        job_id = detail.get("job_id") if isinstance(detail, dict) else None
        typer.echo(
            f"A knowledge-packaging run is already in progress (job {job_id}). "
            f"Check `agnes admin jobs show {job_id}` for status.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Status: {body.get('status')}")
    typer.echo(f"Job:    {body.get('job_id')}")
    typer.echo(f"Check `agnes admin jobs show {body.get('job_id')}` for the result.")


@packaging_app.command("status")
def status(
    as_json: bool = typer.Option(False, "--json", help="Output JSON for scripting"),
) -> None:
    """Show the last knowledge-packaging run's outcome, whether one is
    running right now, and (best-effort) when the next scheduled run is
    due."""
    resp = api_get("/api/admin/knowledge-packaging/status")
    if resp.status_code != 200:
        _fail(resp)

    body = resp.json()
    if as_json:
        typer.echo(_json.dumps(body))
        return

    typer.echo(f"Running:  {body.get('running')}")
    typer.echo(f"Next due: {body.get('next_due') or '(unknown)'}")
    last_run = body.get("last_run")
    if not last_run:
        typer.echo("Last run: (none yet)")
        return
    typer.echo(f"Last run: job {last_run.get('job_id')} — {last_run.get('status')}")
    typer.echo(f"  created:  {last_run.get('created_at')}")
    typer.echo(f"  finished: {last_run.get('finished_at') or '(still running)'}")
    result = last_run.get("result")
    if result:
        typer.echo(
            f"  built={len(result.get('built', []))} skipped={len(result.get('skipped', []))} "
            f"pruned={len(result.get('pruned', []))} errors={len(result.get('errors', []))} "
            f"interrupted_reason={result.get('interrupted_reason')}"
        )
