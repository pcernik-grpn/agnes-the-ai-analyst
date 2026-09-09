"""`agnes admin conversations export` — the evaluation-corpus pull (design
2026-09-08 §3.12), mirroring `GET /api/admin/conversations/corpus`.

Admin-only, Postgres-only (a DuckDB-backed instance answers a typed 501),
gated by the instance's content-export policy. Follows the server's
`X-Next-Cursor` header across pages until it stops appearing; `--json`
reassembles the pages into one JSON array instead of writing raw
newline-delimited JSON.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import typer

from cli.client import api_get

app = typer.Typer(help="Conversation corpus export (evaluation), under the content-export policy.")


def _handle_error(resp) -> None:
    if resp.status_code in (401, 403):
        detail: Any = {}
        with contextlib.suppress(Exception):
            detail = resp.json().get("detail") or {}
        if isinstance(detail, dict) and detail.get("error") == "content_export_disabled":
            typer.echo(
                f"[err] conversation export is disabled by the content-export policy "
                f"(reason: {detail.get('reason', 'mode_off')}). An admin sets "
                "observability.content_export.mode in config/instance.yaml — see "
                "docs/observability.md.",
                err=True,
            )
        else:
            typer.echo(
                "[err] authentication required — run `agnes auth login` or import a PAT",
                err=True,
            )
        raise typer.Exit(1)
    if resp.status_code == 501:
        typer.echo(
            "[err] conversation export needs the Postgres app-state backend "
            "(this instance is still on the frozen DuckDB backend).",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code >= 400:
        detail: Any = resp.text
        with contextlib.suppress(Exception):
            detail = resp.json().get("detail", resp.text)
        typer.echo(f"[err] server returned {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)


@app.command("export")
def export_conversations(
    since: str = typer.Option(..., "--since", help="ISO date/datetime, inclusive lower bound. Required."),
    until: str | None = typer.Option(None, "--until", help="ISO date/datetime, exclusive upper bound; default now."),
    surface: str | None = typer.Option(None, "--surface", help="Filter to one chat surface."),
    agent_id: str | None = typer.Option(None, "--agent-id", help="Filter to one shared agent's sessions."),
    limit: int = typer.Option(200, "--limit", help="Records per page (server caps at 500)."),
    out: Path | None = typer.Option(None, "--out", help="Write to file; else stdout."),
    as_json: bool = typer.Option(False, "--json", help="Write one JSON array instead of newline-delimited JSON."),
):
    """Pull the evaluation-corpus export, following the server's cursor
    until exhausted.

    Content leaves the instance only under the content-export policy
    (`observability.content_export` in `config/instance.yaml`) — a disabled
    policy answers 403 and this command names the reason.
    """
    params: dict = {"since": since, "limit": limit}
    if until:
        params["until"] = until
    if surface:
        params["surface"] = surface
    if agent_id:
        params["agent_id"] = agent_id

    collected: list[dict] = []
    sink = None
    total = 0
    pages = 0
    cursor: str | None = None
    try:
        if out and not as_json:
            sink = out.open("w")
        while True:
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            try:
                resp = api_get("/api/admin/conversations/corpus", params=page_params)
            except Exception as exc:
                typer.echo(f"[err] cannot reach server: {exc}", err=True)
                raise typer.Exit(1) from exc
            _handle_error(resp)
            pages += 1

            lines = [ln for ln in resp.text.splitlines() if ln.strip()]
            if as_json:
                collected.extend(json.loads(ln) for ln in lines)
            else:
                text = "\n".join(lines)
                if text:
                    text += "\n"
                if sink:
                    sink.write(text)
                elif text:
                    typer.echo(text, nl=False)
            total += len(lines)

            cursor = resp.headers.get("X-Next-Cursor") or None
            if not cursor:
                break
    finally:
        if sink:
            sink.close()

    if total == 0:
        typer.echo(
            f"No conversations completed in [{since}, {until or 'now'}) for the given filters — "
            "widen --since/--until, or check the content-export policy if this is unexpected.",
            err=True,
        )
        return

    if as_json:
        payload = json.dumps(collected, indent=2, default=str)
        if out:
            out.write_text(payload)
        else:
            typer.echo(payload)

    if out:
        typer.echo(f"wrote {out} ({total} conversation(s), {pages} page(s))", err=True)


__all__ = ["app"]
