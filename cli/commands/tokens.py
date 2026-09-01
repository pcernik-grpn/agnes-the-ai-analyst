"""`agnes auth token` — manage personal access tokens (#12)."""

import json as _json
import re
from typing import Optional

import typer

from cli.client import api_post, api_get, api_delete, error_detail

token_app = typer.Typer(help="Personal access tokens (long-lived CLI/CI auth)")


def _parse_ttl(ttl: Optional[str]) -> Optional[int]:
    """Parse "30d", "90d", "365d", "never" → days (int) or None."""
    if not ttl or ttl.lower() in ("never", "none", "no-expiry"):
        return None
    m = re.fullmatch(r"(\d+)d", ttl.lower().strip())
    if not m:
        raise typer.BadParameter(f"Invalid TTL: {ttl}. Use e.g. 30d, 90d, 365d, or 'never'.")
    return int(m.group(1))


@token_app.command("create")
def create(
    name: str = typer.Option(..., "--name", help="Human label for the token"),
    ttl: str = typer.Option("90d", "--ttl", help="Lifetime (e.g. 30d, 90d, 365d, never)"),
    raw: bool = typer.Option(False, "--raw", help="Print only the raw token (for CI)"),
    surface: str = typer.Option(
        "all",
        "--surface",
        help=(
            "Data-read surface: 'all' (default — full catalog for admins, "
            "unchanged automation behavior) or 'stack' (catalog/query follow "
            "your stack even as admin; inert for non-admins)."
        ),
    ),
):
    """Create a new personal access token."""
    if surface not in ("all", "stack"):
        typer.echo("error: --surface must be 'all' or 'stack'", err=True)
        raise typer.Exit(1)
    body = {"name": name, "expires_in_days": _parse_ttl(ttl), "surface": surface}
    resp = api_post("/auth/tokens", json=body)
    if resp.status_code != 201:
        typer.echo(f"Failed: {error_detail(resp)}", err=True)
        raise typer.Exit(1)
    data = resp.json()
    if raw:
        typer.echo(data["token"])
        return
    typer.echo("Personal access token created — this is shown ONCE:")
    typer.echo("")
    typer.echo(f"    {data['token']}")
    typer.echo("")
    typer.echo(f"id:      {data['id']}")
    typer.echo(f"name:    {data['name']}")
    typer.echo(f"expires: {data.get('expires_at') or 'never'}")
    typer.echo("")
    typer.echo("Export it so `agnes` can use it:")
    typer.echo(f"    export AGNES_TOKEN={data['token']}")


@token_app.command("list")
def list_tokens(as_json: bool = typer.Option(False, "--json")):
    """List your personal access tokens."""
    resp = api_get("/auth/tokens")
    if resp.status_code != 200:
        typer.echo(f"Failed: {error_detail(resp)}", err=True)
        raise typer.Exit(1)
    rows = resp.json()
    if as_json:
        typer.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No tokens yet. Create one with: agnes auth token create --name <label>")
        return
    typer.echo(f"{'ID':36s} {'NAME':20s} {'PREFIX':10s} {'EXPIRES':20s} {'LAST USED':20s} STATUS")
    for r in rows:
        status = "revoked" if r.get("revoked_at") else "active"
        typer.echo(
            f"{r['id']:36s} {r['name']:20s} {r['prefix']:10s} "
            f"{(r.get('expires_at') or 'never'):20s} "
            f"{(r.get('last_used_at') or '-'):20s} {status}"
        )


@token_app.command("revoke")
def revoke(
    ident: str = typer.Argument(..., help="Token id, prefix, or name"),
):
    """Revoke a token."""
    resp = api_get("/auth/tokens")
    if resp.status_code != 200:
        typer.echo(f"Failed to list tokens: {resp.text}", err=True)
        raise typer.Exit(1)
    rows = resp.json()
    match = next(
        (r for r in rows if r["id"] == ident or r["prefix"] == ident or r["name"] == ident),
        None,
    )
    if not match:
        typer.echo(f"No token matches {ident}", err=True)
        raise typer.Exit(1)
    del_resp = api_delete(f"/auth/tokens/{match['id']}")
    if del_resp.status_code != 204:
        typer.echo(f"Failed: {del_resp.text}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Revoked token {match['id']} ({match['name']})")
