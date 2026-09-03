"""`agnes admin service-account` — headless identity CRUD + PAT issuance
(issue #1534).

CLI counterpart to the ``/api/admin/service-accounts`` surface. Each
subcommand maps 1:1 to one HTTP endpoint:

  - ``create``       → ``POST /api/admin/service-accounts``
  - ``list``         → ``GET  /api/admin/service-accounts``
  - ``token``        → ``POST /api/admin/service-accounts/{id}/tokens``
  - ``deactivate``   → ``PATCH /api/admin/service-accounts/{id}`` (``active: false``)
  - ``activate``     → ``PATCH /api/admin/service-accounts/{id}`` (``active: true``)
  - ``revoke-token`` → ``DELETE /auth/admin/tokens/{token_id}`` (the
    existing admin-on-behalf token surface — service accounts mint no
    second revocation pipeline of their own)

Requires the Postgres app-state backend — a DuckDB-backed instance answers
a typed 501, surfaced verbatim (see ``_fail``).
"""

from __future__ import annotations

import json as _json
import re
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_patch, api_post

service_account_app = typer.Typer(help="Admin: service-account identities (headless, own grants, own PATs)")


def _fail(resp) -> None:
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if detail is None and isinstance(body, dict):
        detail = body.get("error")
    msg = (
        detail
        if isinstance(detail, str)
        else (_json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}"))
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _resolve_service_account_id(ref: str) -> str:
    """Accept either an id or a slug (the part before ``@service.local``)."""
    resp = api_get("/api/admin/service-accounts")
    if resp.status_code != 200:
        _fail(resp)
    for row in resp.json():
        if row["id"] == ref or row["email"] == ref or row["email"].split("@")[0] == ref:
            return row["id"]
    typer.echo(f"Service account not found: {ref}", err=True)
    raise typer.Exit(1)


def _parse_ttl(ttl: Optional[str]) -> Optional[int]:
    """Parse "30d", "90d", "365d", "never" → days (int) or None. Mirrors
    cli/commands/tokens.py's own ``_parse_ttl`` — kept as a private copy
    rather than a shared import, since the two commands' Typer apps have no
    other coupling and this is the only thing they'd share."""
    if not ttl or ttl.lower() in ("never", "none", "no-expiry"):
        return None
    m = re.fullmatch(r"(\d+)d", ttl.lower().strip())
    if not m:
        raise typer.BadParameter(f"Invalid TTL: {ttl}. Use e.g. 30d, 90d, 365d, or 'never'.")
    return int(m.group(1))


@service_account_app.command("create")
def create(
    name: str = typer.Argument(..., help="Display name (e.g. 'CI Bot')"),
    slug: str = typer.Option(..., "--slug", help="Lowercase id for the synthetic address (e.g. 'ci-bot')"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Create a service account. Gets a synthetic `<slug>@service.local`
    address, is active immediately, and is never added to the Admin group."""
    resp = api_post("/api/admin/service-accounts", json={"name": name, "slug": slug})
    if resp.status_code != 201:
        _fail(resp)
    data = resp.json()
    if as_json:
        typer.echo(_json.dumps(data, indent=2))
        return
    typer.echo(f"Created service account {data['id']}")
    typer.echo(f"  name:  {data['name']}")
    typer.echo(f"  email: {data['email']}")
    typer.echo("")
    typer.echo(f"Grant it a group, then mint a token:  agnes admin service-account token {data['id']} --name ci")


@service_account_app.command("list")
def list_accounts(as_json: bool = typer.Option(False, "--json")):
    """List service accounts with a per-account token summary."""
    resp = api_get("/api/admin/service-accounts")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No service accounts yet. Create one with: agnes admin service-account create <name> --slug <slug>")
        return
    typer.echo(f"{'ID':36s} {'EMAIL':30s} {'ACTIVE':7s} {'TOKENS':7s} {'LAST USED':20s} SOONEST EXPIRY")
    for r in rows:
        typer.echo(
            f"{r['id']:36s} {r['email']:30s} {str(r['active']):7s} {r['token_count']:<7} "
            f"{(r.get('last_used_at') or '-'):20s} {r.get('soonest_expiry') or '-'}"
        )


@service_account_app.command("token")
def mint_token(
    ref: str = typer.Argument(..., help="Service account id, slug, or email"),
    name: str = typer.Option(..., "--name", help="Human label for the token"),
    ttl: str = typer.Option("90d", "--ttl", help="Lifetime (e.g. 30d, 90d, 365d, never)"),
    raw: bool = typer.Option(False, "--raw", help="Print only the raw token (for CI)"),
):
    """Mint a PAT FOR the service account. Requires an interactive admin
    session — a PAT-authenticated admin gets a typed 403 here too (#1292)."""
    account_id = _resolve_service_account_id(ref)
    body = {"name": name, "expires_in_days": _parse_ttl(ttl)}
    resp = api_post(f"/api/admin/service-accounts/{account_id}/tokens", json=body)
    if resp.status_code != 201:
        _fail(resp)
    data = resp.json()
    if raw:
        typer.echo(data["token"])
        return
    typer.echo("Service-account personal access token created — this is shown ONCE:")
    typer.echo("")
    typer.echo(f"    {data['token']}")
    typer.echo("")
    typer.echo(f"id:      {data['id']}")
    typer.echo(f"name:    {data['name']}")
    typer.echo(f"expires: {data.get('expires_at') or 'never'}")


@service_account_app.command("deactivate")
def deactivate(ref: str = typer.Argument(..., help="Service account id, slug, or email")):
    """Deactivate — the same `users.active` flip a human account gets;
    every one of its PATs stops authenticating immediately."""
    account_id = _resolve_service_account_id(ref)
    resp = api_patch(f"/api/admin/service-accounts/{account_id}", json={"active": False})
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Deactivated {ref}")


@service_account_app.command("activate")
def activate(ref: str = typer.Argument(..., help="Service account id, slug, or email")):
    """Re-activate a deactivated service account."""
    account_id = _resolve_service_account_id(ref)
    resp = api_patch(f"/api/admin/service-accounts/{account_id}", json={"active": True})
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Activated {ref}")


@service_account_app.command("revoke-token")
def revoke_token(token_id: str = typer.Argument(..., help="Token id (see `agnes admin service-account list`/API)")):
    """Revoke one of a service account's tokens — reuses the existing
    admin-on-behalf token surface (`DELETE /auth/admin/tokens/{id}`); a
    service account mints no second revocation pipeline of its own."""
    resp = api_delete(f"/auth/admin/tokens/{token_id}")
    if resp.status_code != 204:
        _fail(resp)
    typer.echo(f"Revoked token {token_id}")
