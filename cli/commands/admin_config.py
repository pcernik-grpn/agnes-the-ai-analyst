"""`agnes admin config export` / `agnes admin config apply` — round-trip the
server-config OVERLAY as reviewable YAML (Track D3).

The "onboard a new client via a reviewed PR" building block: `export` dumps
`GET /api/admin/server-config/overlay` (the raw, editable-section-only
instance.yaml overlay, secrets stripped) as deterministic YAML; `apply`
reads that YAML back and POSTs it through the SAME validated path the admin
UI uses (`POST /api/admin/server-config`) — section allowlisting, deep-merge,
danger-zone confirmation, and audit logging all apply exactly as they would
for a form save. A section outside the server's editable allowlist is
dropped client-side with a warning (defense-in-depth only — the server
independently rejects an unknown section with 400), and a literal
secret-shaped value is stripped before the request ever leaves this
machine: this command is for git-committed config, never for shipping real
credentials. An env-var NAME (`token_env`) or an unresolved `${VAR}`
reference is not itself a secret and is left alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import typer
import yaml

from cli.client import api_get, api_post

admin_config_app = typer.Typer(help="Export/apply the server-config overlay as reviewable YAML", no_args_is_help=True)


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


def _is_env_ref(value: Any) -> bool:
    """True for an unresolved ``${VAR}`` placeholder — a pointer to an env
    var, not a cleartext secret. Mirrors the server's own
    ``app.api.admin._looks_like_env_ref``."""
    return isinstance(value, str) and value.startswith("${") and value.endswith("}")


def _scrub_secrets(value: Any, secret_patterns: list[str], warned: list[str], path: str = "") -> Any:
    """Recursively drop literal secret-shaped values before a request ever
    leaves this machine.

    ``secret_patterns`` comes from the server's own GET /server-config
    response (``secret_key_patterns``) — this is defense-in-depth, not the
    authoritative gate; the server never accepts a literal secret through
    this path either way, since `apply` is meant for git-committed config.
    A key ending in ``_env`` (an env-var NAME), a ``${VAR}`` reference, or a
    boolean value is left alone — a boolean cannot itself be a credential,
    and several switches (e.g. `mcp.allow_query_param_token`) have "token"/
    "secret" substrings in their name purely by naming coincidence (the same
    reasoning as the server's own `_declared_boolean_fields()` guard).
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else k
            k_lower = k.lower()
            is_secret_key = not k_lower.endswith("_env") and any(p in k_lower for p in secret_patterns)
            if is_secret_key and not isinstance(v, bool) and not _is_env_ref(v):
                warned.append(child_path)
                continue
            out[k] = _scrub_secrets(v, secret_patterns, warned, child_path)
        return out
    if isinstance(value, list):
        return [_scrub_secrets(item, secret_patterns, warned, path) for item in value]
    return value


def _diff(before: dict, after: dict, path: str = "") -> list[dict[str, Any]]:
    """Flat path-level diff between two section dicts. Both sides have
    already been through the same secret-scrub (they come from the same
    /server-config/overlay projection and this command's own filtering), so
    no further masking is needed here."""
    rows: list[dict[str, Any]] = []
    keys = sorted(set(before.keys()) | set(after.keys()))
    for key in keys:
        new_path = f"{path}.{key}" if path else key
        b_val = before.get(key)
        a_val = after.get(key)
        if isinstance(b_val, dict) and isinstance(a_val, dict):
            rows.extend(_diff(b_val, a_val, new_path))
        elif b_val != a_val:
            rows.append({"path": new_path, "before": b_val, "after": a_val})
    return rows


@admin_config_app.command("export")
def export_config(
    out: Optional[Path] = typer.Option(None, "--out", help="Write to this file instead of stdout"),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of YAML"),
) -> None:
    """Export the current server-config overlay as clean, reviewable YAML.

    Calls GET /api/admin/server-config/overlay — the raw on-disk overlay
    (not the merged, env-resolved effective config `agnes admin
    config-surface` reports), filtered to sections `apply` accepts back,
    with secret-shaped literal values already stripped server-side. The
    output is valid `agnes admin config apply` input — commit it to a
    reviewed PR to onboard a new client from a known-good baseline.
    """
    resp = api_get("/api/admin/server-config/overlay")
    if resp.status_code != 200:
        _fail(resp)
    sections = resp.json().get("sections", {})
    if as_json:
        text = json.dumps(sections, indent=2, sort_keys=True) + "\n"
    else:
        text = yaml.dump(sections, default_flow_style=False, sort_keys=True)
    if out:
        out.write_text(text)
        typer.echo(f"Wrote {out}")
    else:
        typer.echo(text, nl=False)


@admin_config_app.command("apply")
def apply_config(
    file: Path = typer.Argument(..., help="YAML file previously produced by `agnes admin config export`"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show the diff vs the current overlay; write nothing"),
    confirm_danger: bool = typer.Option(
        False,
        "--confirm-danger",
        help="Required to apply changes touching the auth or server sections",
    ),
    confirm_connection_change: bool = typer.Option(
        False,
        "--confirm-connection-change",
        help="Required to repoint a data_source connection that already has registered tables",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit a machine-readable diff (--dry-run only)"),
) -> None:
    """Apply a YAML file to the server-config overlay through the same
    validated path as the admin UI: POST /api/admin/server-config.

    Section allowlisting, deep-merge, danger-zone confirmation, and audit
    logging all happen server-side exactly as they would for a form save —
    a section outside the editable allowlist is rejected, not silently
    written. This command additionally filters unknown sections and
    literal secret-shaped values out of the request BEFORE it is sent
    (client-side defense-in-depth; the server is still the final gate).
    """
    resolved = file.expanduser().resolve()
    if not resolved.is_file():
        typer.echo(f"Error: {file} is not a file", err=True)
        raise typer.Exit(1)
    try:
        raw = yaml.safe_load(resolved.read_text())
    except yaml.YAMLError as e:
        typer.echo(f"Error: {file} is not valid YAML: {e}", err=True)
        raise typer.Exit(1) from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        typer.echo(
            f"Error: {file} must contain a mapping of section -> config, got {type(raw).__name__}",
            err=True,
        )
        raise typer.Exit(1)
    for section, patch in raw.items():
        if not isinstance(patch, dict):
            typer.echo(f"Error: section '{section}' must be a mapping, got {type(patch).__name__}", err=True)
            raise typer.Exit(1)

    meta_resp = api_get("/api/admin/server-config")
    if meta_resp.status_code != 200:
        _fail(meta_resp)
    meta = meta_resp.json()
    editable_sections = set(meta.get("editable_sections", []))
    secret_patterns = [p.lower() for p in meta.get("secret_key_patterns", [])]
    danger_sections = set(meta.get("danger_sections", []))

    unknown_sections = sorted(set(raw.keys()) - editable_sections)
    if unknown_sections:
        typer.echo(
            f"Ignoring non-editable section(s): {', '.join(unknown_sections)} "
            f"(editable: {', '.join(sorted(editable_sections))})",
            err=True,
        )

    warned: list[str] = []
    sections: dict[str, Any] = {}
    for section, patch in raw.items():
        if section in unknown_sections:
            continue
        sections[section] = _scrub_secrets(patch, secret_patterns, warned)

    if warned:
        typer.echo(
            f"Stripped {len(warned)} secret-shaped literal value(s) before applying "
            f"(config apply never sends real secrets): {', '.join(warned)}",
            err=True,
        )

    if not sections:
        typer.echo("Nothing to apply (no editable sections left after filtering).", err=True)
        raise typer.Exit(1)

    overlay_resp = api_get("/api/admin/server-config/overlay")
    if overlay_resp.status_code != 200:
        _fail(overlay_resp)
    current_overlay = overlay_resp.json().get("sections", {})

    diff_current = {section: current_overlay.get(section, {}) for section in sections}
    diff_rows = _diff(diff_current, sections)

    if dry_run:
        if as_json:
            typer.echo(json.dumps(diff_rows, indent=2, sort_keys=True, default=str))
        elif not diff_rows:
            typer.echo("No changes.")
        else:
            for row in diff_rows:
                typer.echo(f"{row['path']}: {row['before']!r} -> {row['after']!r}")
        return

    touched_danger = sorted(set(sections.keys()) & danger_sections)
    payload: dict[str, Any] = {
        "sections": sections,
        "confirm_danger": confirm_danger,
        "confirm_connection_change": confirm_connection_change,
    }
    resp = api_post("/api/admin/server-config", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    typer.echo(f"Applied {len(sections)} section(s): {', '.join(sorted(sections))}")
    if touched_danger:
        typer.echo(f"Danger-zone section(s) touched: {', '.join(touched_danger)}")
    if body.get("restart_required"):
        typer.echo("Restart required for at least one section to take effect.")
