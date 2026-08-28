"""`agnes admin sso` — external SSO login config (design 2026-08-28).

CLI counterpart to the ``/api/admin/sso`` surface. Each subcommand maps 1:1
to one HTTP endpoint:

  - ``status``       → ``GET    /api/admin/sso/config``
  - ``set``          → ``PUT    /api/admin/sso/config``
  - ``set-secret``   → ``PUT    /api/admin/sso/client-secret``
  - ``clear-secret`` → ``DELETE /api/admin/sso/client-secret``
  - ``delete``       → ``DELETE /api/admin/sso/config``
  - ``test``         → ``POST   /api/admin/sso/test-config``
  - ``identities``   → ``GET    /api/admin/sso/identities``
  - ``unlink``       → ``DELETE /api/admin/sso/identities/{user_id}``

The client secret is read from a hidden prompt — never passed on the
command line (argv is visible to co-tenants). Destructive ops require
``--yes`` or an interactive confirm. Requires the Postgres app-state
backend — a DuckDB-backed instance answers a typed 501, surfaced verbatim.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_post, api_put

admin_sso_app = typer.Typer(help="Admin: external SSO login (Entra ID OIDC) runtime config")


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
        else (json.dumps(detail) if detail is not None else (resp.text or f"HTTP {resp.status_code}"))
    )
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _get_config() -> dict:
    resp = api_get("/api/admin/sso/config")
    if resp.status_code != 200:
        _fail(resp)
    return resp.json()


@admin_sso_app.command("status")
def status(as_json: bool = typer.Option(False, "--json", help="Machine-readable output")):
    """Show the SSO config status (never the secret)."""
    cfg = _get_config()
    if as_json:
        typer.echo(json.dumps(cfg, indent=2, default=str))
        return
    if not cfg["configured"]:
        typer.echo("External SSO sign-in: not configured")
        typer.echo(f"Vault key configured: {cfg['vault_key_configured']}")
        return
    typer.echo(f"External SSO sign-in: {'ENABLED' if cfg['enabled'] else 'configured, disabled'}")
    typer.echo(f"  Provider type:   {cfg['provider_type']}")
    typer.echo(f"  Tenant:          {cfg['tenant_id']}")
    typer.echo(f"  Client ID:       {cfg['client_id']}")
    typer.echo(f"  Button label:    Sign in with {cfg['display_name']}")
    typer.echo(f"  Allowed domains: {', '.join(cfg['allowed_email_domains'])}")
    typer.echo(f"  Client secret:   {'stored' if cfg['has_client_secret'] else 'MISSING'}")
    typer.echo(f"  Vault key:       {'configured' if cfg['vault_key_configured'] else 'MISSING (AGNES_VAULT_KEY)'}")
    if cfg.get("updated_at"):
        typer.echo(f"  Updated:         {cfg['updated_at']} by {cfg.get('updated_by') or '?'}")


@admin_sso_app.command("set")
def set_config(
    tenant_id: Optional[str] = typer.Option(
        None, "--tenant-id", help="Directory (tenant) ID — GUID or verified domain"
    ),
    client_id: Optional[str] = typer.Option(None, "--client-id", help="Application (client) ID"),
    display_name: Optional[str] = typer.Option(None, "--display-name", help='Login button label ("Sign in with …")'),
    domains: Optional[str] = typer.Option(
        None, "--domains", help="Comma-separated allowed email domains (the customer's own domains only)"
    ),
    enable: Optional[bool] = typer.Option(
        None, "--enable/--disable", help="Offer / stop offering the button on the login page"
    ),
):
    """Create or update the SSO config. Omitted flags keep their stored value."""
    current = _get_config()
    if not current["configured"] and not all((tenant_id, client_id, display_name, domains)):
        typer.echo(
            "Error: no SSO config exists yet — the first `set` needs all of "
            "--tenant-id, --client-id, --display-name and --domains",
            err=True,
        )
        raise typer.Exit(1)

    body = {
        "tenant_id": tenant_id if tenant_id is not None else current["tenant_id"],
        "client_id": client_id if client_id is not None else current["client_id"],
        "display_name": display_name if display_name is not None else current["display_name"],
        "allowed_email_domains": (
            [d.strip() for d in domains.split(",") if d.strip()]
            if domains is not None
            else current["allowed_email_domains"]
        ),
        "enabled": enable if enable is not None else current["enabled"],
    }
    resp = api_put("/api/admin/sso/config", json=body)
    if resp.status_code != 200:
        _fail(resp)
    cfg = resp.json()
    typer.echo(f"SSO config saved ({'enabled' if cfg['enabled'] else 'disabled'}).")
    if not cfg["has_client_secret"]:
        typer.echo("Client secret is not stored yet — run: agnes admin sso set-secret")


@admin_sso_app.command("set-secret")
def set_secret():
    """Set or rotate the client secret (hidden prompt — never on argv)."""
    value = typer.prompt("Client secret", hide_input=True)
    resp = api_put("/api/admin/sso/client-secret", json={"value": value})
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo("Client secret stored (encrypted in the server vault).")


@admin_sso_app.command("clear-secret")
def clear_secret(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
):
    """Clear the stored client secret (the login button disappears)."""
    if not yes:
        typer.confirm(
            "Clear the stored client secret? SSO sign-in stops working until a new one is stored.",
            abort=True,
        )
    resp = api_delete("/api/admin/sso/client-secret")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo("Client secret cleared.")


@admin_sso_app.command("delete")
def delete_config(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
):
    """Delete the SSO config (+ secret). Linked identities are KEPT."""
    if not yes:
        typer.confirm(
            "Delete the SSO configuration? Linked identities are kept; the login button disappears.",
            abort=True,
        )
    resp = api_delete("/api/admin/sso/config")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo("SSO configuration deleted (linked identities kept).")


@admin_sso_app.command("test")
def test_config():
    """Server-side probe: tenant validation, secret decryptability, and the
    tenant's OIDC discovery document."""
    resp = api_post("/api/admin/sso/test-config")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if body.get("ok"):
        typer.echo("OK — discovery document fetched:")
        typer.echo(f"  issuer:                 {body.get('issuer')}")
        typer.echo(f"  authorization_endpoint: {body.get('authorization_endpoint')}")
        typer.echo(f"  token_endpoint:         {body.get('token_endpoint')}")
        typer.echo("For an end-to-end proof, run the admin test sign-in from the web UI (/auth/sso/login?mode=test).")
    else:
        typer.echo(f"FAILED: {body.get('error')}", err=True)
        raise typer.Exit(1)


@admin_sso_app.command("identities")
def identities(
    limit: int = typer.Option(50, "--limit", help="Page size (max 200)"),
    offset: int = typer.Option(0, "--offset", help="Page offset"),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output"),
):
    """List linked external identities (newest link first, paginated)."""
    resp = api_get("/api/admin/sso/identities", params={"limit": limit, "offset": offset})
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2, default=str))
        return
    if not body["identities"]:
        typer.echo(f"No linked identities (total: {body['total']}).")
        return
    for row in body["identities"]:
        last = row.get("last_login_at") or "never"
        typer.echo(f"{row['user_id']}  {row['email']}  subject={row['subject']}  last_login={last}")
    typer.echo(
        f"Showing {len(body['identities'])} of {body['total']} (limit {body['limit']}, offset {body['offset']})."
    )


@admin_sso_app.command("unlink")
def unlink(
    user_id: str = typer.Argument(..., help="Agnes user id whose external identity to unlink"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
):
    """Hard-delete one user's external-identity binding (the mis-attach
    recovery tool). The user row itself is untouched."""
    if not yes:
        typer.confirm(
            f"Unlink the external identity of user {user_id}? Their next SSO sign-in re-attaches by email.",
            abort=True,
        )
    resp = api_delete(f"/api/admin/sso/identities/{user_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"External identity unlinked for {user_id}.")
