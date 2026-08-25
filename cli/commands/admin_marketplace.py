"""`agnes admin marketplace` — curated-marketplace admin operations.

CLI counterpart to the ``/api/marketplaces`` admin surface. Each subcommand
maps 1:1 to one HTTP endpoint:

  - ``list``           → ``GET  /api/marketplaces``
  - ``sync``           → ``POST /api/marketplaces/{slug}/sync``
  - ``disable-plugin`` → ``POST /api/marketplaces/{slug}/plugins/{name}/disable``
  - ``enable-plugin``  → ``POST /api/marketplaces/{slug}/plugins/{name}/enable``

Plugins are addressed as one positional ``<marketplace>/<plugin>`` ref — the
same shape ``resource_grants.resource_id`` uses, so the ref an admin sees in
grant listings pastes straight into these commands.

Registration/deletion of marketplaces (which carry a git token) stay
web-UI-only on purpose; this group covers the day-2 operations an admin
needs scriptable: refresh content and retire/restore a plugin.
"""

from __future__ import annotations

import json
from typing import Tuple
from urllib.parse import quote

import typer

from cli.client import api_get, api_post

admin_marketplace_app = typer.Typer(help="Admin: curated marketplace operations")


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
    if resp.status_code == 404:
        typer.echo("Hint: `agnes admin marketplace list` shows registered marketplace slugs.", err=True)
    raise typer.Exit(1)


def _split_plugin_ref(ref: str) -> Tuple[str, str]:
    """Split ``<marketplace>/<plugin>`` — the grant resource_id shape."""
    marketplace, sep, plugin = ref.partition("/")
    if not sep or not marketplace.strip() or not plugin.strip():
        typer.echo(
            f"Error: expected <marketplace>/<plugin> (e.g. my-kit/my-plugin), got {ref!r}.",
            err=True,
        )
        typer.echo("Hint: `agnes admin marketplace list` shows registered marketplace slugs.", err=True)
        raise typer.Exit(1)
    return marketplace.strip(), plugin.strip()


def _plugin_action_path(marketplace: str, plugin: str, action: str) -> str:
    return f"/api/marketplaces/{quote(marketplace, safe='')}/plugins/{quote(plugin, safe='')}/{action}"


@admin_marketplace_app.command("list")
def list_marketplaces(
    as_json: bool = typer.Option(False, "--json"),
):
    """List registered curated marketplaces."""
    resp = api_get("/api/marketplaces")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        typer.echo("No marketplaces registered.")
        return
    slug_w = max(len("SLUG"), max(len(r.get("id", "")) for r in rows))
    name_w = max(len("NAME"), max(len(r.get("name", "") or "") for r in rows))
    typer.echo(f"{'SLUG':<{slug_w}}  {'NAME':<{name_w}}  {'PLUGINS':>7}  {'LAST SYNC':<20}  COMMIT")
    for r in rows:
        synced = (r.get("last_synced_at") or "never")[:19]
        sha = (r.get("last_commit_sha") or "")[:7] or "—"
        typer.echo(
            f"{r.get('id', ''):<{slug_w}}  {(r.get('name') or ''):<{name_w}}  "
            f"{r.get('plugin_count', 0):>7}  {synced:<20}  {sha}"
        )
        if r.get("last_error"):
            typer.echo(f"{'':<{slug_w}}  last error: {r['last_error']}")


@admin_marketplace_app.command("sync")
def sync_marketplace(
    slug: str = typer.Argument(..., help="Marketplace slug (see `agnes admin marketplace list`)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Clone/refresh one marketplace now (instead of waiting for the nightly sync)."""
    resp = api_post(f"/api/marketplaces/{quote(slug, safe='')}/sync", timeout=300.0)
    if resp.status_code != 200:
        _fail(resp)
    result = resp.json()
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    commit = (result.get("commit") or "")[:7] or "—"
    typer.echo(f"Synced {slug}: {result.get('action', 'ok')} @ {commit}, {result.get('plugin_count', 0)} plugin(s)")


@admin_marketplace_app.command("disable-plugin")
def disable_plugin(
    plugin_ref: str = typer.Argument(..., help="<marketplace>/<plugin>, e.g. my-kit/my-plugin"),
    revoke_grants: bool = typer.Option(
        False,
        "--revoke-grants",
        help="Also delete every group grant on the plugin (permanent retirement).",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Admin-disable a plugin instance-wide (drops out of every served surface).

    Plain disable keeps grants, so `enable-plugin` later restores the plugin
    for the same groups. With --revoke-grants the grants are deleted in the
    same call — the one-action retirement for a deprecated plugin.
    """
    marketplace, plugin = _split_plugin_ref(plugin_ref)
    kwargs = {"json": {"revoke_grants": True}} if revoke_grants else {}
    resp = api_post(_plugin_action_path(marketplace, plugin, "disable"), **kwargs)
    if resp.status_code != 200:
        _fail(resp)
    result = resp.json()
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    revoked = result.get("revoked_grants", 0)
    suffix = f" — revoked {revoked} grant(s)" if revoked else ""
    typer.echo(f"Disabled {marketplace}/{plugin}{suffix}")


@admin_marketplace_app.command("enable-plugin")
def enable_plugin(
    plugin_ref: str = typer.Argument(..., help="<marketplace>/<plugin>, e.g. my-kit/my-plugin"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Re-enable a previously disabled plugin (for callers still holding a grant).

    Note: a plugin whose upstream marketplace-metadata.json marks it
    ``deprecated`` is re-disabled automatically on the next sync.
    """
    marketplace, plugin = _split_plugin_ref(plugin_ref)
    resp = api_post(_plugin_action_path(marketplace, plugin, "enable"))
    if resp.status_code != 200:
        _fail(resp)
    result = resp.json()
    if as_json:
        typer.echo(json.dumps(result, indent=2))
        return
    typer.echo(f"Enabled {marketplace}/{plugin}")
