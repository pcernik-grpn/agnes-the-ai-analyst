"""``agnes admin doctor`` — deployment-gate diagnostics.

CLI surface for ``POST /api/admin/doctor/new-instance``. Prints one
PASS/WARN/FAIL/INFO line per check and exits non-zero when any check errors,
so deploy pipelines can gate on it. The host-side siblings (COMPOSE_FILE ↔
instance.yaml, TLS predicate agreement) live in
``scripts/ops/post-deploy-smoke-test.sh`` — they need the VM's filesystem,
which this HTTP client deliberately does not touch.
"""

import json

import typer

from cli.client import api_post

doctor_app = typer.Typer(help="Deployment-gate diagnostics (admin)")

_STATUS_LABEL = {"ok": "PASS", "warning": "WARN", "error": "FAIL", "info": "INFO"}


@doctor_app.callback(invoke_without_command=True)
def doctor(
    new_instance: bool = typer.Option(
        False,
        "--new-instance",
        help=(
            "Run the new-instance deployment checks (login-door, email-delivery, "
            "chat-grant, agent-scope, app-state-backend, branding)"
        ),
    ),
    email_to: str = typer.Option(
        "",
        "--email-to",
        help="Have the email-delivery check send a real test message to this address",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Run server-side deployment checks against this instance (admin only)."""
    if not new_instance:
        typer.echo("Select a check profile: agnes admin doctor --new-instance", err=True)
        raise typer.Exit(2)

    body: dict = {}
    if email_to:
        body["email_to"] = email_to
    # Generous timeout: the email-delivery check may sit in an SMTP handshake.
    resp = api_post("/api/admin/doctor/new-instance", json=body, timeout=120.0)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Doctor failed: HTTP {resp.status_code}: {detail}", err=True)
        raise typer.Exit(1)

    report = resp.json()
    if as_json:
        typer.echo(json.dumps(report, indent=2))
    else:
        for check in report.get("checks", []):
            label = _STATUS_LABEL.get(check.get("status"), str(check.get("status")).upper())
            typer.echo(f"  {label:4} {check.get('name')}: {check.get('detail')}")
        typer.echo("")
        typer.echo(f"Overall: {report.get('status')}")
    raise typer.Exit(1 if report.get("status") == "error" else 0)
