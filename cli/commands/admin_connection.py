"""`agnes admin connection` — manage named source connections (multi-project Keboola).

CLI counterpart to the ``/api/admin/source-connections`` surface.
Each subcommand maps 1:1 to one HTTP endpoint:

  - ``list``    → ``GET /api/admin/source-connections``
  - ``add``     → ``POST /api/admin/source-connections`` + ``PUT /{id}/secret``
  - ``remove``  → ``DELETE /api/admin/source-connections/{id}``
  - ``test``    → ``POST /api/admin/source-connections/{id}/test``
  - ``secret``  → ``PUT /{id}/secret`` (or ``DELETE /{id}/secret?kind=``
                  with ``--remove``); ``--kind storage|master`` selects which
                  vault secret (``master`` = semantic-layer owner token)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post, api_put

admin_connection_app = typer.Typer(help="Admin: named source-connection CRUD")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    msg = (
        detail
        if isinstance(detail, str)
        else (json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}"))
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


@admin_connection_app.command("list")
def list_connections(
    source_type: Optional[str] = typer.Option(None, "--source-type", help="Filter by source type (e.g. keboola)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List named source connections."""
    params = {}
    if source_type:
        params["source_type"] = source_type
    resp = api_get("/api/admin/source-connections", params=params or None)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No connections.")
        return
    name_w = max(len("NAME"), max(len(r.get("name", "")) for r in rows))
    type_w = max(len("TYPE"), max(len(r.get("source_type", "")) for r in rows))
    typer.echo(f"{'ID':<26}  {'NAME':<{name_w}}  {'TYPE':<{type_w}}  DEFAULT  STACK URL")
    for r in rows:
        cfg = r.get("config") or {}
        url = cfg.get("stack_url", "") if isinstance(cfg, dict) else ""
        default = "yes" if r.get("is_default") else ""
        typer.echo(
            f"{r['id']:<26}  {r.get('name', ''):<{name_w}}  {r.get('source_type', ''):<{type_w}}  {default:<7}  {url}"
        )


@admin_connection_app.command("add")
def add_connection(
    name: str = typer.Option(..., "--name", help="Human-readable name"),
    stack_url: str = typer.Option(..., "--stack-url", help="Keboola stack URL"),
    token: str = typer.Option(..., "--token", help="Keboola Storage API token"),
    source_type: str = typer.Option("keboola", "--source-type", help="Source type"),
    default: bool = typer.Option(False, "--default/--no-default", help="Set as default connection"),
):
    """Add a named source connection and store its token in the vault."""
    payload = {
        "name": name,
        "source_type": source_type,
        "config": {"stack_url": stack_url},
        "is_default": default,
    }
    resp = api_post("/api/admin/source-connections", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    body = resp.json()
    conn_id = body.get("id")
    typer.echo(f"Created connection id={conn_id}")

    secret_resp = api_put(
        f"/api/admin/source-connections/{conn_id}/secret",
        json={"value": token},
    )
    if secret_resp.status_code not in (200, 204):
        _fail(secret_resp)
    typer.echo(f"Token stored in vault for connection {conn_id}")


@admin_connection_app.command("remove")
def remove_connection(
    connection_id: str = typer.Argument(..., help="Connection id"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Remove a named source connection."""
    if not yes:
        confirm = typer.confirm(f"Delete connection {connection_id}?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/source-connections/{connection_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted connection {connection_id}")


@admin_connection_app.command("secret")
def set_secret(
    connection_id: str = typer.Argument(..., help="Connection id"),
    kind: str = typer.Option("storage", "--kind", help="storage | master (master = semantic-layer owner token)"),
    remove: bool = typer.Option(False, "--remove", help="Clear the secret instead of setting it"),
    from_file: Optional[str] = typer.Option(
        None,
        "--from-file",
        help="Read the secret from this file ('-' = stdin) — for multiline values "
        "like a SharePoint cert+key PEM that cannot travel through the prompt",
    ),
):
    """Set or clear a connection's vault secret. The value is read from a
    hidden prompt, or — for multiline material like a combined cert+key
    PEM — from a file via ``--from-file`` (the path is argv, the secret
    never is)."""
    if kind not in ("storage", "master"):
        typer.echo("Error: --kind must be storage or master", err=True)
        raise typer.Exit(1)
    if remove:
        if from_file:
            typer.echo("Error: --remove clears the secret; it cannot be combined with --from-file", err=True)
            raise typer.Exit(1)
        resp = api_delete(f"/api/admin/source-connections/{connection_id}/secret", params={"kind": kind})
        if resp.status_code not in (200, 204):
            _fail(resp)
        typer.echo(f"Cleared {kind} secret for {connection_id}")
        return
    if from_file:
        if from_file == "-":
            token = sys.stdin.read().strip()
        else:
            try:
                token = Path(from_file).read_text().strip()
            except OSError as exc:
                typer.echo(f"Error: could not read {from_file}: {exc}", err=True)
                raise typer.Exit(1) from exc
        if not token:
            typer.echo(f"Error: {from_file} is empty", err=True)
            raise typer.Exit(1)
    else:
        token = typer.prompt("Token", hide_input=True)
    resp = api_put(f"/api/admin/source-connections/{connection_id}/secret", json={"value": token, "kind": kind})
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Stored {kind} secret for {connection_id}")


@admin_connection_app.command("chat-tools")
def chat_tools(
    connection_id: str = typer.Argument(..., help="Connection id"),
    disable: bool = typer.Option(False, "--disable", help="Remove the derived MCP source instead"),
):
    """Expose a Keboola project's own MCP tools to the chat agent.

    Derives an MCP source from the connection and copies its storage token
    into the MCP vault. Rotating that token propagates to the copy on its own
    — storing a new secret on the connection updates both — so this does not
    have to be re-run for a rotation.

    The derived source lands with no tool grants — grant its tools under
    ``/admin/mcp-sources`` (each tool's Grants page) before analysts see
    anything; ``agnes admin grant`` works on resource grants and cannot
    touch MCP tool grants.
    """
    if disable:
        resp = api_delete(f"/api/admin/source-connections/{connection_id}/chat-tools")
        if resp.status_code not in (200, 204):
            _fail(resp)
        typer.echo(f"Chat tools disabled for {connection_id}")
        return
    resp = api_post(f"/api/admin/source-connections/{connection_id}/chat-tools", json={})
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json() if resp.content else {}
    count = body.get("tools_registered", 0)
    typer.echo(
        f"Chat tools enabled for {connection_id}: {count} tools registered (MCP source {body.get('source_id', '?')})"
    )
    # Registering is not granting, and MCP tool grants are their own surface —
    # `agnes admin grant` works on resource grants and cannot touch these.
    # (Devin Review on the original PR; the Introspect step it used to name is
    # gone now that enabling registers the tools itself.)
    typer.echo(
        "Registered is not reachable — grant them under /admin/mcp-sources, "
        "on each tool's Grants page, before analysts see anything."
    )
    # A `mutating` tool is refused for every non-admin by the passthrough
    # policy gate, grant or no grant — and a tool the upstream does not
    # annotate as read-only is recorded as mutating. Without this line, an
    # upstream that annotates nothing makes the grant advice above a false
    # promise. (Devin Review on this PR, fifth round.)
    admin_only = body.get("tools_admin_only") or 0
    if admin_only:
        scope = "All" if admin_only == count else f"{admin_only} of {count}"
        typer.echo(
            f"{scope} registered tools are recorded as mutating (no read-only annotation "
            "upstream is not a claim of safety) and stay unreachable for the group until "
            "an admin opts each one in: `agnes admin mcp tool grant <tool_id> --group <g> "
            "--allow-mutating`. Review any that are actually read-only under /admin/mcp-sources."
        )


@admin_connection_app.command("test")
def test_connection(
    connection_id: str = typer.Argument(..., help="Connection id"),
):
    """Test connectivity for a named source connection."""
    resp = api_post(f"/api/admin/source-connections/{connection_id}/test", json={})
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json()
    if body.get("ok"):
        project = body.get("project_name", "")
        typer.echo(f"OK — project: {project}" if project else "OK")
    else:
        typer.echo(f"FAILED — {body.get('error', 'unknown error')}", err=True)
        raise typer.Exit(1)
