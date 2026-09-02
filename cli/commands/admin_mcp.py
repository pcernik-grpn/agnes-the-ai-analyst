"""`agnes admin mcp` — Universal MCP source + tool admin CLI.

CLI counterpart to the ``/api/admin/mcp-sources`` and ``/api/admin/mcp-tools``
surfaces. Two subcommand groups:

  - ``source``  → register / list / inspect / classify / materialize MCP servers
  - ``tool``    → list / register / delete individual tool entries

The CLI is a thin REST client. All persistence lives on the server (the CLI
machine and the agnes server may be different boxes). Source-resolution by
name happens via ``GET /api/admin/mcp-sources`` + filter; anything starting
with ``src_`` is treated as an opaque id.

Pattern follows ``admin_data_package.py``: small ``_fail`` helper, plain
``typer.echo`` for simple status messages, Rich ``Table`` for multi-row
output. See M7 of the universal-MCP POC spec for context.
"""

from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from cli.client import api_delete, api_get, api_post, api_put

mcp_app = typer.Typer(help="Admin: Universal MCP source + tool management")
source_app = typer.Typer(help="MCP source registration and inspection")
tool_app = typer.Typer(help="MCP tool registry CRUD")
mcp_app.add_typer(source_app, name="source")
mcp_app.add_typer(tool_app, name="tool")


_console = Console()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_secret_line(what: str) -> str:
    """Read one line of secret material from stdin.

    Announces itself on stderr FIRST when stdin is a terminal. Without that an
    interactive admin sees a command that has simply stopped, with nothing on
    screen saying it is waiting for them — indistinguishable from a hang
    (Devin Review on #1124). The notice goes to stderr and only on a TTY, so
    the piped form (`printf %s "$SECRET" | agnes …`, CI) stays byte-clean.

    ``getpass`` would hide the typing but does not work on a piped stdin,
    which is the form the help text recommends — so a plain read it is.
    """
    import sys

    if sys.stdin.isatty():
        typer.echo(f"Reading {what} from stdin — type it and press Enter (Ctrl-C to abort):", err=True)
    return sys.stdin.readline().rstrip("\n")


def _fail(resp) -> None:
    """Render a server error and exit non-zero. Mirrors the helper in
    ``admin_data_package.py`` so behaviour is consistent across admin CLIs."""
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


def _resolve_source_id(ref: str) -> str:
    """Accept either an opaque id (``src_*``) or a human name.

    Names are resolved via ``GET /api/admin/mcp-sources`` + linear scan.
    The cost is one extra GET; admins call this rarely and adding a
    server-side ``GET ?name=`` endpoint only for the CLI's convenience
    isn't justified.
    """
    if ref.startswith("src_"):
        return ref
    resp = api_get("/api/admin/mcp-sources")
    if resp.status_code != 200:
        _fail(resp)
    payload = resp.json()
    for row in payload if isinstance(payload, list) else payload.get("sources", []):
        if row.get("name") == ref or row.get("id") == ref:
            return row["id"]
    typer.echo(f"MCP source not found: {ref}", err=True)
    raise typer.Exit(1)


def _print_source_table(rows: list[dict]) -> None:
    table = Table(title=f"MCP sources ({len(rows)})")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("NAME", style="bold")
    table.add_column("TRANSPORT")
    table.add_column("ENDPOINT")
    table.add_column("AUTH")
    # DNS-free url-policy sweep/report (#1216 part 1) — `would_refuse` means
    # the CURRENT policy would reject this row's url if it were saved today;
    # it stays live because `check_source_url` only gates configuration-time
    # writes. Blank only for stdio (the API returns `null`: the url is inert
    # there); an ordinary hostname the DNS-free check cannot judge shows
    # "ok". `no_wrap` so a narrow render squeezes ENDPOINT, never folds the
    # verdict mid-word.
    table.add_column("URL POLICY", no_wrap=True)
    for row in rows:
        endpoint = row.get("url") or row.get("command") or ""
        args = row.get("args") or []
        if args and not row.get("url"):
            endpoint = f"{endpoint} {' '.join(args)}".strip()
        auth = row.get("auth_method") or ""
        if row.get("auth_secret_env"):
            auth = f"{auth}:{row['auth_secret_env']}" if auth else row["auth_secret_env"]
        verdict = row.get("url_policy_verdict") or {}
        url_policy = str(verdict.get("verdict") or "")
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("name") or ""),
            str(row.get("transport") or ""),
            str(endpoint),
            str(auth),
            url_policy,
        )
    _console.print(table)


def _print_tool_table(rows: list[dict], *, title: str = "MCP tools") -> None:
    table = Table(title=f"{title} ({len(rows)})")
    table.add_column("TOOL_ID", style="cyan", no_wrap=True)
    table.add_column("SOURCE", style="bold")
    table.add_column("ORIGINAL")
    table.add_column("EXPOSED")
    table.add_column("MODE")
    table.add_column("SCHEDULE")
    table.add_column("ENABLED")
    for row in rows:
        table.add_row(
            str(row.get("tool_id") or row.get("id") or ""),
            str(row.get("source_id") or row.get("source_name") or ""),
            str(row.get("original_name") or ""),
            str(row.get("exposed_name") or ""),
            str(row.get("mode") or ""),
            str(row.get("schedule") or ""),
            "yes" if row.get("enabled", True) else "no",
        )
    _console.print(table)


def _print_discovered_tools(rows: list[dict]) -> None:
    table = Table(title=f"Discovered upstream tools ({len(rows)})")
    table.add_column("ORIGINAL_NAME", style="cyan", no_wrap=True)
    table.add_column("DESCRIPTION")
    table.add_column("MUTATING")
    table.add_column("INPUT_SCHEMA")
    for row in rows:
        desc = (row.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 60:
            desc = desc[:57] + "..."
        schema = row.get("input_schema") or {}
        if isinstance(schema, dict):
            props = schema.get("properties") or {}
            schema_summary = ", ".join(sorted(props.keys())) if props else "{}"
        else:
            schema_summary = str(schema)
        if len(schema_summary) > 40:
            schema_summary = schema_summary[:37] + "..."
        table.add_row(
            str(row.get("original_name") or row.get("name") or ""),
            desc,
            "yes" if row.get("mutating") else "no",
            schema_summary,
        )
    _console.print(table)


def _print_proposals(rows: list[dict]) -> None:
    table = Table(title=f"Classifier proposals ({len(rows)})")
    table.add_column("ORIGINAL_NAME", style="cyan", no_wrap=True)
    table.add_column("SUGGESTED_MODE", style="bold")
    table.add_column("SUGGESTED_EXPOSED")
    table.add_column("SCHEDULE")
    table.add_column("RATIONALE")
    for row in rows:
        rationale = (row.get("rationale") or row.get("reason") or "").strip()
        if len(rationale) > 50:
            rationale = rationale[:47] + "..."
        table.add_row(
            str(row.get("original_name") or row.get("name") or ""),
            str(row.get("suggested_mode") or row.get("mode") or ""),
            str(row.get("suggested_exposed_name") or row.get("exposed_name") or ""),
            str(row.get("schedule") or ""),
            rationale,
        )
    _console.print(table)


# ---------------------------------------------------------------------------
# source: add / list / show / delete / test
# ---------------------------------------------------------------------------


@source_app.command("add")
def source_add(
    name: str = typer.Argument(..., help="Human-friendly source name (e.g. 'crm-internal')"),
    transport: str = typer.Option(
        ...,
        "--transport",
        help="MCP transport: stdio | sse | http",
    ),
    command: Optional[str] = typer.Option(
        None,
        "--command",
        help="Executable path for transport=stdio (e.g. /usr/local/bin/crm-mcp)",
    ),
    args: list[str] = typer.Option(
        None,
        "--arg",
        help="Argument to pass to the stdio command. Repeatable: --arg foo --arg bar.",
    ),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="Endpoint URL for transport=sse or transport=http",
    ),
    auth_method: Optional[str] = typer.Option(
        None,
        "--auth-method",
        help="Auth scheme for the upstream MCP server (e.g. bearer)",
    ),
    auth_secret_env: Optional[str] = typer.Option(
        None,
        "--auth-secret-env",
        help="Name of the env var on the agnes server that holds the secret",
    ),
    scope: Optional[str] = typer.Option(
        None,
        "--scope",
        help=(
            "Credential scope: 'shared' (default — server-wide secret used "
            "for every caller) or 'per_user' (each analyst stores their own "
            "via `agnes mcp my-secret set`)"
        ),
    ),
):
    """Register a new MCP source.

    Two shapes — pick one per transport:

      - ``--transport=stdio --command=PATH [--arg X --arg Y]``
      - ``--transport=sse --url=URL`` or ``--transport=http --url=URL``

    Auth (``--auth-method`` / ``--auth-secret-env``) is optional and forwarded
    verbatim to the server. The actual secret value never leaves the agnes
    box — the CLI only passes the env-var name to dereference.
    """
    if transport not in ("stdio", "sse", "http"):
        typer.echo(
            f"--transport must be one of stdio | sse | http (got {transport!r})",
            err=True,
        )
        raise typer.Exit(2)
    if transport == "stdio" and not command:
        typer.echo("--command is required for --transport=stdio", err=True)
        raise typer.Exit(2)
    if transport in ("sse", "http") and not url:
        typer.echo(f"--url is required for --transport={transport}", err=True)
        raise typer.Exit(2)
    if command and transport != "stdio":
        typer.echo(
            f"--command is only valid for --transport=stdio (got {transport!r})",
            err=True,
        )
        raise typer.Exit(2)
    if url and transport == "stdio":
        typer.echo("--url is only valid for --transport=sse|http", err=True)
        raise typer.Exit(2)

    payload: dict = {"name": name, "transport": transport}
    if command:
        payload["command"] = command
    if args:
        payload["args"] = list(args)
    if url:
        payload["url"] = url
    if auth_method:
        payload["auth_method"] = auth_method
    if auth_secret_env:
        payload["auth_secret_env"] = auth_secret_env
    if scope:
        if scope not in ("shared", "per_user"):
            typer.echo("--scope must be 'shared' or 'per_user'", err=True)
            raise typer.Exit(2)
        payload["scope"] = scope

    resp = api_post("/api/admin/mcp-sources", json=payload)
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json() or {}
    src_id = body.get("id") or body.get("source_id") or "?"
    typer.echo(f"Registered MCP source: {name} (id={src_id})")


@source_app.command("list")
def source_list(
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """List all registered MCP sources."""
    resp = api_get("/api/admin/mcp-sources")
    if resp.status_code != 200:
        _fail(resp)
    payload = resp.json()
    rows = payload if isinstance(payload, list) else payload.get("sources", [])
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not rows:
        typer.echo("No MCP sources registered.")
        return
    _print_source_table(rows)


@source_app.command("show")
def source_show(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Show details of one MCP source."""
    src_id = _resolve_source_id(name_or_id)
    resp = api_get(f"/api/admin/mcp-sources/{src_id}")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"MCP source: {body.get('name', '?')} (id={body.get('id', src_id)})")
    for key in (
        "transport",
        "command",
        "args",
        "url",
        "auth_method",
        "auth_secret_env",
        "created_at",
        "updated_at",
    ):
        if key in body and body.get(key) not in (None, ""):
            val = body[key]
            if isinstance(val, (list, dict)):
                val = json.dumps(val)
            typer.echo(f"  {key:18s} {val}")


@source_app.command("delete")
def source_delete(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete an MCP source. Cascades to the source's tool entries server-side."""
    src_id = _resolve_source_id(name_or_id)
    if not yes:
        confirm = typer.confirm(f"Delete MCP source {name_or_id} (id={src_id})? Cascades tool entries.")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/mcp-sources/{src_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted MCP source {name_or_id} (id={src_id})")


@source_app.command("grant")
def source_grant(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    group: str = typer.Option(..., "--group", help="User group id to grant (or revoke with --revoke)"),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke instead of grant"),
):
    """Grant (or revoke) a group EVERY tool registered under one source.

    Per-tool grants suit an upstream curated a few tools at a time; a source
    that arrives with its whole toolset — a connected Keboola project brings
    around forty — is why this exists.
    """
    src_id = _resolve_source_id(name_or_id)
    if revoke:
        resp = api_delete(f"/api/admin/mcp-sources/{src_id}/grants/{group}")
        if resp.status_code not in (200, 204):
            _fail(resp)
        typer.echo(f"Revoked group {group} from every tool of {name_or_id}")
        return
    resp = api_post(f"/api/admin/mcp-sources/{src_id}/grants", json={"group_id": group})
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json() if resp.content else {}
    # "granted 0 of 37" and "granted 37 of 37" both read as success otherwise.
    typer.echo(
        f"Granted {body.get('granted', 0)} of {body.get('total', 0)} tools to {group}"
        + (f" ({body['already_granted']} already granted)" if body.get("already_granted") else "")
    )
    # A grant does not reach a mutating tool: the passthrough gate refuses
    # those for every non-admin anyway. Saying so beats the group finding out.
    if body.get("skipped_disabled"):
        typer.echo(
            f"{body['skipped_disabled']} disabled tools were skipped — a grant on a "
            "switched-off tool would take effect the moment someone re-enables it."
        )
    if body.get("admin_only"):
        typer.echo(
            f"{body['admin_only']} of them are mutating and stay unreachable for the group — "
            "opt in per tool with `agnes admin mcp tool grant <tool_id> --group <g> --allow-mutating`."
        )


@tool_app.command("grant")
def tool_grant(
    tool_id: str = typer.Argument(..., help="Tool id from `agnes admin mcp tool list`"),
    group: str = typer.Option(..., "--group", help="User group id to grant (or revoke with --revoke)"),
    allow_mutating: Optional[bool] = typer.Option(
        None,
        "--allow-mutating/--no-allow-mutating",
        help="Opt the group into (or out of) this tool's MUTATING surface (v121). "
        "Omit both flags to leave an existing grant's setting unchanged "
        "(a brand-new grant lands read-only).",
    ),
    revoke: bool = typer.Option(False, "--revoke", help="Revoke instead of grant"),
):
    """Grant (or revoke) ONE tool to a user group.

    The per-tool counterpart of `source grant`, and the only place a group
    can be opted into a mutating tool: a plain grant is read-only; with
    `--allow-mutating` members (and agent profiles owned by them, still
    narrowed by the agent's connection scope) may invoke the tool even
    though its registry row is marked mutating. Re-running with an explicit
    `--allow-mutating`/`--no-allow-mutating` updates the existing grant in
    place; omitting both leaves it unchanged.
    """
    if revoke:
        resp = api_delete(f"/api/admin/mcp-tools/{tool_id}/grants/{group}")
        if resp.status_code not in (200, 204):
            _fail(resp)
        typer.echo(f"Revoked group {group} from tool {tool_id}")
        return
    resp = api_post(
        f"/api/admin/mcp-tools/{tool_id}/grants",
        json={"group_id": group, "allow_mutating": allow_mutating},
    )
    if resp.status_code not in (200, 201):
        _fail(resp)
    if allow_mutating is None:
        typer.echo(f"Granted tool {tool_id} to {group} (mutating setting unchanged; new grants are read-only)")
    else:
        typer.echo(
            f"Granted tool {tool_id} to {group}" + (" (mutating allowed)" if allow_mutating else " (read-only)")
        )


@source_app.command("set-secret")
def source_set_secret(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    value: Optional[str] = typer.Option(
        None,
        "--value",
        help="Secret value. If omitted, read one line from stdin (recommended — keeps the value out of shell history).",
    ),
):
    """Store the server-wide vault secret for a source (RFC #461 §4).

    ``connectors/mcp/client.py`` consults the vault first, then falls
    back to the legacy ``auth_secret_env`` env-var, so flipping a source
    from env-var to vault is a one-command rollout. Plaintext lives only
    in the request body; at rest it's Fernet-encrypted in ``mcp_secrets``.
    """
    src_id = _resolve_source_id(name_or_id)
    if value is None:
        value = _read_secret_line("the secret value")
    if not value:
        typer.echo("set-secret: secret value is empty — refusing.", err=True)
        raise typer.Exit(2)
    resp = api_put(
        f"/api/admin/mcp-sources/{src_id}/secret",
        json={"value": value},
    )
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Stored vault secret for source {name_or_id} (id={src_id}).")


@source_app.command("clear-secret")
def source_clear_secret(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Drop the vault row for a source — falls back to ``auth_secret_env`` (if any) or anonymous."""
    src_id = _resolve_source_id(name_or_id)
    if not yes:
        if not typer.confirm(
            f"Clear vault secret for {name_or_id} (id={src_id})? "
            f"Upstream calls will fall back to auth_secret_env or anonymous."
        ):
            raise typer.Abort()
    resp = api_delete(f"/api/admin/mcp-sources/{src_id}/secret")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Cleared vault secret for source {name_or_id} (id={src_id}).")


@source_app.command("oauth-register")
def source_oauth_register(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    scopes: Optional[str] = typer.Option(
        None,
        "--scopes",
        help="Space-joined OAuth scopes to request (default: AS/resource defaults)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """RFC 9728/8414 discovery + RFC 7591 dynamic client registration for an
    ``auth_method='oauth'`` source (2026-07-30 outbound MCP OAuth sources
    spec §2). Idempotent — re-running replaces the registration, best-effort
    revoking the old one first.
    """
    src_id = _resolve_source_id(name_or_id)
    payload: dict = {}
    if scopes:
        payload["scopes"] = scopes
    resp = api_post(f"/api/admin/mcp-sources/{src_id}/oauth/register", json=payload or None)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Registered OAuth client for source {name_or_id} (id={src_id}):")
    for key in ("issuer", "client_id", "has_client_secret", "authorization_endpoint", "token_endpoint", "scopes"):
        if key in body:
            typer.echo(f"  {key:24s} {body[key]}")


@source_app.command("oauth-client")
def source_oauth_client(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    client_id: str = typer.Option(..., "--client-id", help="OAuth client_id at the upstream authorization server"),
    authorization_endpoint: str = typer.Option(
        ..., "--authorization-endpoint", help="AS authorization endpoint URL (https)"
    ),
    token_endpoint: str = typer.Option(..., "--token-endpoint", help="AS token endpoint URL (https)"),
    issuer: Optional[str] = typer.Option(
        None,
        "--issuer",
        help="AS issuer URL (default: authorization_endpoint's origin)",
    ),
    scopes: Optional[str] = typer.Option(None, "--scopes", help="Space-joined OAuth scopes to request"),
    client_secret: Optional[str] = typer.Option(
        None,
        "--client-secret",
        help="Confidential client secret. If omitted (and the AS needs one), read one line from stdin.",
    ),
    public_client: bool = typer.Option(
        False,
        "--public-client",
        help=(
            "Configure as a public (PKCE-only, no secret) client — skips the stdin secret "
            "prompt and CLEARS any secret already on file for this source."
        ),
    ),
    keep_secret: bool = typer.Option(
        False,
        "--keep-secret",
        help=(
            "Leave the stored client secret alone — for editing endpoints or scopes on an "
            "existing confidential client without re-entering it."
        ),
    ),
):
    """Manually configure the OAuth client for an ``auth_method='oauth'``
    source — the escape hatch for an authorization server without dynamic
    client registration (spec §2).
    """
    if public_client and keep_secret:
        typer.echo(
            "oauth-client: --public-client clears the secret and --keep-secret preserves it — pick one.", err=True
        )
        raise typer.Exit(2)
    src_id = _resolve_source_id(name_or_id)
    # Three server-side states, three ways to ask for them: --client-secret
    # replaces, --public-client clears, --keep-secret leaves it alone. Without
    # any of them we read stdin, which is the "pipe me the secret" form.
    if client_secret is None and not public_client and not keep_secret:
        client_secret = _read_secret_line("the client secret") or None
    payload: dict = {
        "client_id": client_id,
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
    }
    if issuer:
        payload["issuer"] = issuer
    if scopes:
        payload["scopes"] = scopes
    if public_client:
        # The endpoint reads an omitted client_secret as "keep whatever is on
        # file" — so omitting it here would silently leave an existing
        # confidential secret in place, and Agnes would keep sending Basic
        # auth (_post_token_request keys off secret presence). An explicit ""
        # is the documented "clear it" signal, and converting a registration
        # to public is exactly what this flag means (Devin Review on #1124).
        payload["client_secret"] = ""
    elif client_secret:
        payload["client_secret"] = client_secret
    resp = api_put(f"/api/admin/mcp-sources/{src_id}/oauth/client", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    typer.echo(f"Configured OAuth client for source {name_or_id} (id={src_id}): client_id={body.get('client_id')}")


@source_app.command("test")
def source_test(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Health-check the upstream MCP server (handshake + tools/list smoke test)."""
    src_id = _resolve_source_id(name_or_id)
    resp = api_post(f"/api/admin/mcp-sources/{src_id}/test")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    ok = bool(body.get("ok"))
    typer.echo(f"Source {name_or_id}: {'OK' if ok else 'FAILED'}")
    for key, val in body.items():
        if key == "ok":
            continue
        if isinstance(val, (list, dict)):
            val = json.dumps(val)
        typer.echo(f"  {key:18s} {val}")
    if not ok:
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# source: introspect / classify / register-suggested / materialize
# ---------------------------------------------------------------------------


@source_app.command("introspect")
def source_introspect(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Run ``tools/list`` against the upstream MCP server and print what it exposes."""
    src_id = _resolve_source_id(name_or_id)
    resp = api_post(f"/api/admin/mcp-sources/{src_id}/introspect")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    rows = body.get("tools", [])
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    if not rows:
        typer.echo(f"No tools discovered on source {name_or_id}.")
        return
    _print_discovered_tools(rows)


@source_app.command("classify")
def source_classify(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Run the heuristic classifier; show suggested mode per discovered tool."""
    src_id = _resolve_source_id(name_or_id)
    resp = api_post(f"/api/admin/mcp-sources/{src_id}/classify")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    rows = body.get("proposals", [])
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    if not rows:
        typer.echo(f"No proposals returned for source {name_or_id}.")
        return
    _print_proposals(rows)


@source_app.command("register-suggested")
def source_register_suggested(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print what would be registered without calling POST /api/admin/mcp-tools",
    ),
):
    """Accept the classifier proposals and bulk-register every non-skip tool.

    This is the "AI didn't help me curate, just take the defaults" fast path:

      - ``suggested_mode == skip`` → ignored
      - ``materialize`` → exposed_name = lowercase ``original_name``,
        schedule = ``every 6h`` (unless the proposal already carries one)
      - ``passthrough`` → exposed_name = ``{source_name}.{original_name}``

    Each tool is POSTed individually; the first 4xx aborts the batch (with
    the count of successes so far printed) so a misconfigured proposal
    doesn't silently swallow follow-on errors.
    """
    src_id = _resolve_source_id(name_or_id)

    # We need source.name for the passthrough default exposed_name. Even
    # when the caller passed an id, fetching the source row is cheap and
    # gives us the canonical name.
    src_resp = api_get(f"/api/admin/mcp-sources/{src_id}")
    if src_resp.status_code != 200:
        _fail(src_resp)
    src_body = src_resp.json() or {}
    source_name = src_body.get("name") or name_or_id

    cls_resp = api_post(f"/api/admin/mcp-sources/{src_id}/classify")
    if cls_resp.status_code != 200:
        _fail(cls_resp)
    proposals = (cls_resp.json() or {}).get("proposals", [])
    if not proposals:
        typer.echo(f"No proposals returned for source {name_or_id}.")
        return

    candidates: list[dict] = []
    for prop in proposals:
        mode = prop.get("suggested_mode") or prop.get("mode")
        if not mode or mode == "skip":
            continue
        original_name = prop.get("original_name") or prop.get("name")
        if not original_name:
            continue
        if mode == "materialize":
            exposed_name = prop.get("suggested_exposed_name") or prop.get("exposed_name") or original_name.lower()
            schedule = prop.get("schedule") or "every 6h"
        elif mode == "passthrough":
            exposed_name = (
                prop.get("suggested_exposed_name") or prop.get("exposed_name") or f"{source_name}.{original_name}"
            )
            schedule = prop.get("schedule")  # passthrough has no schedule
        else:
            typer.echo(
                f"  ! skipping {original_name}: unknown suggested_mode={mode!r}",
                err=True,
            )
            continue
        candidates.append(
            {
                "source_id": src_id,
                "original_name": original_name,
                "exposed_name": exposed_name,
                "mode": mode,
                "tool_id": prop.get("tool_id") or f"{src_id}.{exposed_name}",
                "input_schema": prop.get("input_schema"),
                "description": prop.get("description"),
                "schedule": schedule,
                "enabled": True,
            }
        )

    if not candidates:
        typer.echo(f"All proposals on {name_or_id} were classified as skip; nothing to register.")
        return

    if dry_run:
        typer.echo(f"[DRY RUN] would register {len(candidates)} tool(s):")
        for c in candidates:
            sched = f" schedule={c['schedule']}" if c.get("schedule") else ""
            typer.echo(f"  {c['mode']:11s} {c['original_name']:30s} → {c['exposed_name']}{sched}")
        return

    registered = 0
    for c in candidates:
        # Strip Nones so the server's payload validator doesn't see
        # explicit nulls where it expects omission.
        body = {k: v for k, v in c.items() if v is not None}
        resp = api_post("/api/admin/mcp-tools", json=body)
        if resp.status_code not in (200, 201):
            typer.echo(
                f"  ! failed to register {c['original_name']} → "
                f"{c['exposed_name']}: HTTP {resp.status_code} {resp.text}",
                err=True,
            )
            typer.echo(
                f"Aborted after {registered}/{len(candidates)} successes.",
                err=True,
            )
            raise typer.Exit(1)
        registered += 1
        typer.echo(f"  + registered {c['mode']:11s} {c['original_name']} → {c['exposed_name']}")
    typer.echo(f"Registered {registered} tool(s) on source {name_or_id}.")


@source_app.command("materialize")
def source_materialize(
    name_or_id: str = typer.Argument(..., help="Source name or id (src_*)"),
    tool_id: Optional[str] = typer.Option(
        None,
        "--tool-id",
        help="Restrict to one tool_id; omit to materialize every materialize-mode tool on the source",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """Force an immediate materialize run for the source (or one tool on it).

    Bypasses the scheduler; the server runs the materialize tool synchronously
    and returns the per-tool result(s). Useful right after a fresh
    ``register-suggested`` to verify the data lands without waiting up to 6h.
    """
    src_id = _resolve_source_id(name_or_id)
    payload = {"tool_id": tool_id} if tool_id else {}
    resp = api_post(
        f"/api/admin/mcp-sources/{src_id}/materialize",
        json=payload,
    )
    if resp.status_code not in (200, 202):
        _fail(resp)
    body = resp.json() or {}
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Materialize triggered for {name_or_id}{f' (tool {tool_id})' if tool_id else ''}.")
    for key, val in body.items():
        if isinstance(val, (list, dict)):
            val = json.dumps(val)
        typer.echo(f"  {key:18s} {val}")


# ---------------------------------------------------------------------------
# tool: list / register / delete
# ---------------------------------------------------------------------------


@tool_app.command("list")
def tool_list(
    source: Optional[str] = typer.Option(
        None,
        "--source",
        help="Filter to a source by name or id (src_*)",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit raw JSON to stdout"),
):
    """List registered tool entries (the rows in ``tool_registry``)."""
    params: dict = {}
    if source:
        params["source_id"] = _resolve_source_id(source)
    resp = api_get("/api/admin/mcp-tools", params=params or None)
    if resp.status_code != 200:
        _fail(resp)
    payload = resp.json()
    rows = payload if isinstance(payload, list) else payload.get("tools", [])
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return
    if not rows:
        typer.echo(f"No MCP tools registered{' for source ' + source if source else ''}.")
        return
    _print_tool_table(rows, title=f"MCP tools{' for ' + source if source else ''}")


@tool_app.command("register")
def tool_register(
    source: str = typer.Option(
        ...,
        "--source",
        help="Source name or id (src_*) the tool belongs to",
    ),
    original_name: str = typer.Option(
        ...,
        "--original-name",
        help="Tool name as the upstream MCP server advertises it",
    ),
    exposed_name: str = typer.Option(
        ...,
        "--exposed-name",
        help="Tool name as Agnes exposes it to AI clients",
    ),
    mode: str = typer.Option(
        ...,
        "--mode",
        help="materialize | passthrough",
    ),
    schedule: Optional[str] = typer.Option(
        None,
        "--schedule",
        help="Cron-style schedule for materialize mode (e.g. 'every 6h', 'daily 03:00')",
    ),
    description: Optional[str] = typer.Option(
        None,
        "--description",
        help="Human-readable description (defaults to upstream description if omitted)",
    ),
    tool_id: Optional[str] = typer.Option(
        None,
        "--tool-id",
        help="Override the synthesized tool_id (defaults to '<source_id>.<exposed_name>')",
    ),
    disabled: bool = typer.Option(
        False,
        "--disabled",
        help="Register as disabled (won't be exposed / scheduled until enabled)",
    ),
    mutating: bool = typer.Option(
        False,
        "--mutating",
        help="Mark tool as write/mutating — only admin callers can invoke (RFC #461 §3)",
    ),
    pii_fields: Optional[str] = typer.Option(
        None,
        "--pii-fields",
        help="Comma-separated JSON keys to redact in upstream responses (e.g. 'email,phone')",
    ),
    rate_limit_pm: Optional[int] = typer.Option(
        None,
        "--rate-limit-pm",
        help="Per-minute, per-user cap on invocations of this tool (omit for no limit)",
    ),
):
    """Register a single tool entry directly (no classifier).

    For bulk registration of every classifier-suggested tool, use
    ``agnes admin mcp source register-suggested`` instead.
    """
    if mode not in ("materialize", "passthrough"):
        typer.echo(
            f"--mode must be 'materialize' or 'passthrough' (got {mode!r})",
            err=True,
        )
        raise typer.Exit(2)
    if mode == "passthrough" and schedule:
        typer.echo(
            "--schedule is only valid for --mode=materialize (passthrough tools are live calls; no scheduling).",
            err=True,
        )
        raise typer.Exit(2)
    src_id = _resolve_source_id(source)
    payload: dict = {
        "source_id": src_id,
        "original_name": original_name,
        "exposed_name": exposed_name,
        "mode": mode,
        "enabled": not disabled,
    }
    payload["tool_id"] = tool_id or f"{src_id}.{exposed_name}"
    if schedule:
        payload["schedule"] = schedule
    if description:
        payload["description"] = description
    if mutating:
        payload["mutating"] = True
    if pii_fields:
        # Comma-list → JSON array on the wire; empty entries dropped so
        # ``--pii-fields ""`` doesn't accidentally redact every key.
        fields = [f.strip() for f in pii_fields.split(",") if f.strip()]
        if fields:
            payload["pii_fields"] = fields
    if rate_limit_pm is not None:
        if rate_limit_pm < 0:
            typer.echo("--rate-limit-pm must be >= 0 (0 = no limit)", err=True)
            raise typer.Exit(2)
        payload["rate_limit_pm"] = rate_limit_pm

    resp = api_post("/api/admin/mcp-tools", json=payload)
    if resp.status_code not in (200, 201):
        _fail(resp)
    body = resp.json() or {}
    tid = body.get("tool_id") or payload["tool_id"]
    typer.echo(f"Registered tool: {exposed_name} (tool_id={tid}, mode={mode})")


@tool_app.command("delete")
def tool_delete(
    tool_id: str = typer.Argument(..., help="Full tool_id (e.g. 'src_abc.accounts')"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete one tool entry from ``tool_registry``."""
    if not yes:
        confirm = typer.confirm(f"Delete tool {tool_id}?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/admin/mcp-tools/{tool_id}")
    if resp.status_code not in (200, 204):
        _fail(resp)
    typer.echo(f"Deleted tool {tool_id}")
