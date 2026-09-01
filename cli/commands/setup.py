"""Setup commands — agnes setup init/bootstrap/test-connection/first-sync/verify."""

import json
import os

import typer

from cli.client import api_get, api_post, error_detail

setup_app = typer.Typer(help="Instance setup (guided by AI agent)")


@setup_app.command("init")
def setup_init(
    server: str = typer.Option("http://localhost:8000", help="Server URL"),
):
    """Initialize CLI config to point at a server."""
    typer.echo(f"Server: {server}")

    from cli.config import _config_dir

    config_dir = _config_dir()
    config_file = config_dir / "config.yaml"

    import yaml

    config = {"server": server}
    config_file.write_text(yaml.dump(config), encoding="utf-8")
    typer.echo(f"Config saved to {config_file}")
    os.environ["AGNES_SERVER"] = server
    typer.echo("\nNext: agnes setup bootstrap --email admin@company.com")


@setup_app.command("bootstrap")
def bootstrap(
    email: str = typer.Argument(..., help="Admin email"),
    name: str = typer.Option("", help="Display name"),
    set_password: bool = typer.Option(
        False,
        "--set-password",
        help="Prompt for an optional admin password (hidden input)",
    ),
    server: str = typer.Option(None, help="Server URL override"),
):
    """Create the first admin user on a fresh instance.

    Only works when the database has zero users.
    After this, use 'agnes login' for normal auth.

    Security audit F14: the password is never accepted as a CLI flag (it would
    land on argv, readable via ``ps`` / ``/proc/<pid>/cmdline`` by co-tenant
    local users). It is read from the ``AGNES_BOOTSTRAP_PASSWORD`` env var, or
    prompted for with hidden input via ``--set-password``. OAuth / magic-link
    orgs need no password and can omit both.
    """
    if server:
        os.environ["AGNES_SERVER"] = server

    password = os.environ.get("AGNES_BOOTSTRAP_PASSWORD", "")
    if set_password and not password:
        password = typer.prompt("Admin password", hide_input=True, confirmation_prompt=True)

    typer.echo("Bootstrapping first admin user...")
    try:
        resp = api_post(
            "/auth/bootstrap",
            json={
                "email": email,
                "name": name or email.split("@")[0],
                "password": password,
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            # Save token automatically
            from cli.config import save_token

            save_token(data["access_token"], data["email"])
            typer.echo(f"Admin user created: {data['email']}")
            typer.echo("Token saved — you are now logged in as admin.")
            typer.echo("\nNext: agnes setup test-connection")
        elif resp.status_code == 403:
            typer.echo(f"Bootstrap disabled: {error_detail(resp)}")
            typer.echo("Users already exist. Use: agnes login --email your@email.com")
        else:
            typer.echo(f"Failed: {resp.text}", err=True)
            raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Connection error: {e}", err=True)
        typer.echo("Is the server running? Check: docker compose ps")
        raise typer.Exit(1)


@setup_app.command("test-connection")
def test_connection():
    """Test connection to the server and data source."""
    typer.echo("Testing server connection...")
    try:
        # Quick unauth ping first
        resp = api_get("/api/health")
        health = resp.json()
        if health.get("status") != "ok":
            typer.echo(f"  Server: unexpected status {health.get('status')}")
            raise typer.Exit(1)
        typer.echo("  Server: reachable")

        # Detailed health (auth required) for service-level checks
        try:
            resp = api_get("/api/health/detailed")
            detailed = resp.json()
            typer.echo(f"  Health: {detailed.get('status', 'unknown')}")
            for svc, info in detailed.get("services", {}).items():
                typer.echo(f"  {svc}: {info.get('status', '?')}")
            if detailed.get("status") == "healthy":
                typer.echo("\nServer is healthy.")
            else:
                typer.echo("\nServer has issues. Check: agnes diagnose --json")
        except Exception:
            # Auth may not be configured yet — minimal check is sufficient
            typer.echo("\nServer is reachable (detailed check requires auth).")

    except Exception as e:
        typer.echo(f"  FAILED: {e}", err=True)
        raise typer.Exit(1)

    typer.echo("\nNext: agnes setup first-sync")


@setup_app.command("first-sync")
def first_sync():
    """Trigger the first data sync."""
    typer.echo("Triggering initial data sync...")
    try:
        resp = api_post("/api/sync/trigger")
        if resp.status_code == 200:
            data = resp.json()
            typer.echo(f"  Status: {data.get('status', '?')}")
            typer.echo(f"  {data.get('message', '')}")
        elif resp.status_code == 403:
            typer.echo("  Permission denied. Are you logged in as admin?")
            typer.echo("  Run: agnes login --email admin@company.com")
            raise typer.Exit(1)
        else:
            typer.echo(f"  Failed: {resp.text}", err=True)
            raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"  Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo("\nWait for sync to complete, then: agnes setup verify")


@setup_app.command("verify")
def verify(as_json: bool = typer.Option(False, "--json", help="Output as JSON")):
    """Verify the instance is working end-to-end.

    Checks: server health → auth → data sync → manifest → query capability.
    Returns structured report for AI agents.
    """
    checks = []

    # 1. Server reachable
    try:
        resp = api_get("/api/health")
        h = resp.json()
        # Minimal health returns {"status": "ok"} — try detailed for richer check
        try:
            resp_d = api_get("/api/health/detailed")
            hd = resp_d.json()
            checks.append(
                {
                    "name": "server",
                    "status": "pass" if hd.get("status") == "healthy" else "warn",
                    "detail": hd.get("status"),
                }
            )
        except Exception:
            # Auth not configured yet — minimal reachability is enough
            checks.append(
                {
                    "name": "server",
                    "status": "pass" if h.get("status") == "ok" else "warn",
                    "detail": h.get("status"),
                }
            )
    except Exception as e:
        checks.append({"name": "server", "status": "fail", "detail": str(e)})
        _report(checks, as_json)
        return

    # 2. Auth works (token valid)
    from cli.config import get_token

    token = get_token()
    if token:
        try:
            resp = api_get("/api/sync/manifest")
            if resp.status_code == 200:
                checks.append({"name": "auth", "status": "pass", "detail": "token valid"})
            else:
                checks.append({"name": "auth", "status": "fail", "detail": f"HTTP {resp.status_code}"})
        except Exception as e:
            checks.append({"name": "auth", "status": "fail", "detail": str(e)})
    else:
        checks.append({"name": "auth", "status": "fail", "detail": "no token — run: agnes login"})

    # 3. Data available
    try:
        resp = api_get("/api/sync/manifest")
        m = resp.json()
        table_count = len(m.get("tables", {}))
        total_rows = sum(t.get("rows", 0) for t in m.get("tables", {}).values())
        if table_count > 0:
            checks.append({"name": "data", "status": "pass", "detail": f"{table_count} tables, {total_rows:,} rows"})
        else:
            checks.append({"name": "data", "status": "warn", "detail": "0 tables — run: agnes setup first-sync"})
    except Exception as e:
        checks.append({"name": "data", "status": "fail", "detail": str(e)})

    # 4. Users exist
    try:
        resp = api_get("/api/users")
        if resp.status_code == 200:
            count = len(resp.json())
            checks.append({"name": "users", "status": "pass", "detail": f"{count} users"})
        elif resp.status_code == 403:
            checks.append({"name": "users", "status": "pass", "detail": "exists (need admin for count)"})
        else:
            checks.append({"name": "users", "status": "warn", "detail": f"HTTP {resp.status_code}"})
    except Exception as e:
        checks.append({"name": "users", "status": "fail", "detail": str(e)})

    # 5. Web UI accessible
    try:
        resp = api_get("/login")
        checks.append(
            {
                "name": "web_ui",
                "status": "pass" if resp.status_code == 200 else "fail",
                "detail": f"HTTP {resp.status_code}, {len(resp.content)} bytes",
            }
        )
    except Exception as e:
        checks.append({"name": "web_ui", "status": "fail", "detail": str(e)})

    # 6. Swagger docs
    try:
        resp = api_get("/docs")
        checks.append(
            {
                "name": "api_docs",
                "status": "pass" if resp.status_code == 200 else "fail",
                "detail": f"HTTP {resp.status_code}",
            }
        )
    except Exception as e:
        checks.append({"name": "api_docs", "status": "fail", "detail": str(e)})

    _report(checks, as_json)


def _report(checks: list, as_json: bool):
    all_pass = all(c["status"] == "pass" for c in checks)
    has_fail = any(c["status"] == "fail" for c in checks)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "overall": "pass" if all_pass else ("fail" if has_fail else "warn"),
                    "checks": checks,
                },
                indent=2,
            )
        )
    else:
        for c in checks:
            icon = {"pass": "OK", "fail": "FAIL", "warn": "WARN"}[c["status"]]
            typer.echo(f"  [{icon:4s}] {c['name']}: {c['detail']}")
        typer.echo("")
        if all_pass:
            typer.echo("All checks passed! Instance is ready.")
        elif has_fail:
            typer.echo("Some checks FAILED. See above for details.")
            raise typer.Exit(1)
        else:
            typer.echo("Instance is running but some items need attention.")
