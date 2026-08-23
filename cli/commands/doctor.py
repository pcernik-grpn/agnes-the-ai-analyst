"""``agnes doctor`` — collect a redacted support bundle into one file.

The support loop's first artifact: "run one command, attach one file". The
bundle has two origin-labeled sections:

- **Client** — always present, works fully offline: CLI version, server URL,
  auth verdict (never the token), workspace/pull state, the local-delivery
  comparison from ``agnes diagnose``, and the tail of the client-side
  transport error log.
- **Server** — ``GET /api/admin/doctor/support`` (admin-only). When the
  caller is not an admin or the server is unreachable, the section is
  replaced by an explicit one-line reason — a partial bundle must say it is
  partial (command-UX standard), and the moments a doctor is needed most are
  exactly the moments the server half is missing.

The rendered text is scrubbed before it leaves the process: the configured
token's literal value plus ``Bearer``/``Authorization``/``token=`` patterns
(the error-log tail may quote request headers).

Sibling: ``agnes admin doctor --new-instance`` is the *deployment gate*
(active pass/fail checks, admin-only); this command is the *support
snapshot* (state collection, degrades gracefully for any user).
Design: docs/superpowers/specs/2026-08-23-support-bundle-doctor-design.md.
"""

import json
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

import typer

from cli.client import RedirectHardStop, api_get
from cli.config import _config_dir, get_server_url, get_token

doctor_app = typer.Typer(help="Collect a redacted support bundle (one attachable file)")

_ERROR_LOG_TAIL_LINES = 80

_SCRUB_REPLACEMENT = "<redacted>"
# Ordered: literal token replacement happens first (strongest guarantee),
# then pattern scrubs for credential shapes the error-log tail may quote.
_SCRUB_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)[^\s\"']+"),
    re.compile(r"(?i)\b(token|api[_-]?key|secret|password)=([^&\s\"']+)"),
)


def _scrub(text: str, literals: tuple[str, ...]) -> str:
    for lit in literals:
        # Length guard so a degenerate short token can't turn the scrub into
        # a text shredder (replacing e.g. every "1234" in the bundle).
        if lit and len(lit) >= 8:
            text = text.replace(lit, _SCRUB_REPLACEMENT)
    text = _SCRUB_PATTERNS[0].sub(rf"\g<1>{_SCRUB_REPLACEMENT}", text)
    text = _SCRUB_PATTERNS[1].sub(rf"\g<1>{_SCRUB_REPLACEMENT}", text)
    text = _SCRUB_PATTERNS[2].sub(rf"\g<1>={_SCRUB_REPLACEMENT}", text)
    return text


def _human_bytes(n) -> str:
    if not isinstance(n, (int, float)):
        return "?"
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"


def _collect_auth() -> dict:
    token = get_token()
    if not token:
        # Detail carries the next step only — the renderer supplies the
        # "not logged in" label, and repeating it read as a stutter.
        return {"token_present": False, "detail": "run `agnes login`"}

    info: dict = {"token_present": True}
    try:
        import jwt

        payload = jwt.decode(token, options={"verify_signature": False})
        info["email"] = payload.get("email")
    except Exception:
        # Opaque PAT — fall back to the email recorded next to it.
        try:
            data = json.loads((_config_dir() / "token.json").read_text(encoding="utf-8"))
            info["email"] = data.get("email")
        except Exception:
            info["email"] = None
    try:
        from cli.token_status import format_status_line

        info["token_status"] = format_status_line(token)
    except Exception:
        pass

    # Live probe: does the server accept this credential right now?
    try:
        resp = api_get("/api/health/detailed")
        if resp.status_code == 200:
            info["verified"] = True
            info["caller_role"] = (resp.json() or {}).get("caller_role")
        elif resp.status_code == 401:
            info["verified"] = False
            info["detail"] = "server rejected the token (401) — run `agnes login`"
        else:
            info["verified"] = None
            info["detail"] = f"auth probe returned HTTP {resp.status_code}"
    except RedirectHardStop as e:
        info["verified"] = None
        info["detail"] = f"server relocated: {e.user_message}"
    except Exception as e:
        info["verified"] = None
        info["detail"] = f"server unreachable: {e}"
    return info


def _collect_workspace() -> dict:
    from cli.config import get_sync_state, get_workspace_root
    from cli.lib.local_tables import count_local_tables
    from cli.lib.session_paths import list_session_files
    from cli.lib.workspace_resolve import resolve_data_workspace

    root = resolve_data_workspace()
    if root is None or not root.exists():
        return {"path": None, "detail": "`agnes init` never ran on this machine"}

    out: dict = {"path": str(root)}
    out["initialized"] = (root / ".claude" / "init-complete").exists()
    try:
        queryable, unregistered = count_local_tables(root)
        out["tables_queryable"] = queryable
        out["tables_downloaded_no_view"] = unregistered
    except Exception as e:
        out["tables_detail"] = f"count failed: {e}"
    try:
        state = get_sync_state(root)
        out["last_pull"] = state.get("last_sync")
        out["pulled_tables"] = len(state.get("tables", {}) or {})
    except Exception as e:
        out["last_pull_detail"] = f"sync state unreadable: {e}"
    try:
        ws_root = get_workspace_root()
        out["sessions_pending_upload"] = len(list_session_files(Path(ws_root))) if ws_root else 0
    except Exception:
        pass
    return out


def _collect_local_delivery() -> dict:
    # The manifest-vs-disk comparison `agnes diagnose` runs — reused, not
    # re-derived (one diagnostics vocabulary).
    from cli.commands.diagnose import _local_delivery_check

    return _local_delivery_check()


def _collect_error_log_tail() -> list[str] | None:
    # Same file `cli/client.py:_log_traceback` writes (`_LOG_FILE`); resolved
    # at call time so `$AGNES_CONFIG_DIR` overrides are honored.
    log_path = _config_dir() / "last-error.log"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return lines[-_ERROR_LOG_TAIL_LINES:] or None


def _fetch_server_section() -> dict:
    """The admin support snapshot — or an explicit reason why not."""
    try:
        resp = api_get("/api/admin/doctor/support", timeout=60.0)
    except RedirectHardStop as e:
        return {"unavailable": f"server relocated: {e.user_message}"}
    except BaseException as e:  # noqa: BLE001 — a doctor must not die mid-report
        return {"unavailable": f"server unreachable: {e}"}
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (401, 403):
        return {
            "unavailable": (
                "requires admin — ask an instance admin to run `agnes doctor` and attach their bundle's server section"
            )
        }
    return {"unavailable": f"server returned HTTP {resp.status_code}"}


def _build_bundle() -> dict:
    from cli.main import _cli_version

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client": {
            "cli_version": _cli_version(),
            "platform": platform.platform(),
            "server_url": get_server_url(),
            "config_dir": str(_config_dir()),
            "auth": _collect_auth(),
            "workspace": _collect_workspace(),
            "local_data": _collect_local_delivery(),
            "recent_errors": _collect_error_log_tail(),
        },
        "server": _fetch_server_section(),
    }


def _status_mark(status) -> str:
    return {"ok": "OK", "warning": "WARN", "error": "FAIL", "info": "INFO"}.get(status, str(status or "?"))


def _render_markdown(bundle: dict) -> str:
    c = bundle["client"]
    lines: list[str] = [
        "# Agnes support bundle",
        "",
        f"Generated: {bundle['generated_at']}",
        "",
        "## Client (this machine)",
        "",
        f"- CLI version: {c['cli_version']}",
        f"- Platform: {c['platform']}",
        f"- Server URL: {c['server_url']}",
        f"- Config dir: {c['config_dir']}",
    ]

    auth = c["auth"]
    if not auth.get("token_present"):
        lines.append(f"- Auth: not logged in — {auth.get('detail', '')}")
    else:
        verdict = {True: "verified against server", False: "REJECTED by server", None: "unverified"}[
            auth.get("verified")
        ]
        parts = [f"as {auth['email']}" if auth.get("email") else None, auth.get("token_status")]
        detail = ", ".join(p for p in parts if p)
        lines.append(f"- Auth: {verdict}" + (f" ({detail})" if detail else ""))
        if auth.get("caller_role"):
            lines.append(f"- Role: {auth['caller_role']}")
        if auth.get("detail"):
            lines.append(f"- Auth detail: {auth['detail']}")

    ws = c["workspace"]
    if ws.get("path") is None:
        lines.append(f"- Workspace: none — {ws.get('detail', '')}")
    else:
        lines.append(f"- Workspace: {ws['path']} (initialized: {ws.get('initialized')})")
        if "tables_queryable" in ws:
            extra = (
                f" (+{ws['tables_downloaded_no_view']} downloaded, no local view)"
                if ws.get("tables_downloaded_no_view")
                else ""
            )
            lines.append(f"- Local tables: {ws['tables_queryable']} queryable{extra}")
        lines.append(f"- Last pull: {ws.get('last_pull') or 'never'} ({ws.get('pulled_tables', 0)} tables tracked)")
        if "sessions_pending_upload" in ws:
            lines.append(f"- Sessions pending upload: {ws['sessions_pending_upload']}")

    local = c["local_data"]
    lines.append(f"- Local data delivery: [{_status_mark(local.get('status'))}] {local.get('detail', '')}")

    if c["recent_errors"]:
        lines += [
            "",
            "### Recent client errors (last-error.log tail)",
            "",
            "```text",
            *c["recent_errors"],
            "```",
        ]

    lines += ["", "## Server", ""]
    server = bundle["server"]
    if "unavailable" in server:
        lines.append(f"> Server section unavailable: {server['unavailable']}")
        lines.append("")
        return "\n".join(lines)

    build = server.get("build", {})
    lines += [
        "### Build",
        "",
        f"- Version: {build.get('version')} (package {build.get('package_version')}, channel {build.get('channel')})",
        f"- Image: {build.get('image_tag')} @ {build.get('commit_sha')}",
        f"- Deployed at: {build.get('deployed_at')}",
        "",
    ]

    schema = server.get("schema", {})
    lines += [
        "### Schema / migrations",
        "",
        f"- [{_status_mark(schema.get('status'))}] backend {schema.get('backend')}, "
        f"current {schema.get('current')}, expected {schema.get('expected')}"
        + (f" — {schema['detail']}" if schema.get("detail") else ""),
        "",
    ]

    retrieval = server.get("retrieval", {})
    lines += [
        "### Retrieval",
        "",
        f"- [{_status_mark(retrieval.get('status'))}] mode: {retrieval.get('mode')} — {retrieval.get('detail', '')}",
        "",
    ]

    sync = server.get("sync", {})
    lines += ["### Sync (per source)", ""]
    sources = sync.get("sources", {})
    if not sources:
        lines.append(f"- [{_status_mark(sync.get('status'))}] no registered tables" if "status" in sync else "- none")
    else:
        lines.append("| source | tables | ok | errors | stale | never synced | newest sync |")
        lines.append("|---|---|---|---|---|---|---|")
        for name, agg in sorted(sources.items()):
            lines.append(
                f"| {name} | {agg.get('tables')} | {agg.get('ok')} | {agg.get('errors')} "
                f"| {agg.get('stale')} | {agg.get('never_synced')} | {agg.get('last_sync_max') or '—'} |"
            )
        for name, agg in sorted(sources.items()):
            for err in agg.get("last_errors") or []:
                lines.append(
                    f"- FAIL {name}/{err.get('table_id')}: {err.get('error')} (last sync {err.get('last_sync') or 'never'})"
                )
    lines.append("")

    disk = server.get("disk", {})
    lines += [
        "### Disk",
        "",
        f"- [{_status_mark(disk.get('status'))}] {disk.get('data_dir')}: "
        f"{_human_bytes(disk.get('free_bytes'))} free of {_human_bytes(disk.get('total_bytes'))}",
        f"- system.duckdb: {_human_bytes(disk.get('system_db_bytes'))}; "
        f"analytics: {_human_bytes(disk.get('analytics_db_bytes'))}",
        "",
    ]

    proc = server.get("process", {})
    lines += [
        "### Process",
        "",
        f"- State backend: {proc.get('state_backend')}; roles: {', '.join(proc.get('roles') or []) or '?'}; "
        f"python {proc.get('python')}",
        "",
    ]

    secrets = server.get("secrets", {})
    lines += ["### Secrets (presence only — values are never collected)", ""]
    if isinstance(secrets, dict) and secrets and "status" not in secrets:
        for name in sorted(secrets):
            lines.append(f"- {name}: {'present' if secrets[name] else 'absent'}")
    else:
        lines.append(f"- unavailable: {secrets.get('detail', 'section crashed server-side')}")
    lines.append("")
    return "\n".join(lines)


@doctor_app.callback(invoke_without_command=True)
def doctor(
    output: str = typer.Option(
        "",
        "--output",
        "-o",
        help="Write the bundle to this path (default: ./agnes-doctor-<UTC timestamp>.md)",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print the structured bundle to stdout instead of writing a file"
    ),
):
    """Collect a redacted support bundle — one file to attach to a ticket.

    Always succeeds at producing the artifact: the client section works
    offline, and a missing server section (not an admin, server down) is
    replaced by an explicit reason. Secret values never enter the bundle.
    See also: `agnes diagnose` (live checks), `agnes admin doctor` (deploy gate).
    """
    bundle = _build_bundle()
    literals = tuple(t for t in (get_token(),) if t)

    if as_json:
        typer.echo(_scrub(json.dumps(bundle, indent=2, default=str), literals))
        return

    rendered = _scrub(_render_markdown(bundle), literals)
    if output:
        path = Path(output)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
        path = Path.cwd() / f"agnes-doctor-{stamp}.md"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    except OSError as e:
        typer.echo(f"Could not write bundle to {path}: {e}", err=True)
        raise typer.Exit(1)

    server_note = (
        "server section included"
        if "unavailable" not in bundle["server"]
        else (f"server section unavailable ({bundle['server']['unavailable']})")
    )
    typer.echo(f"Support bundle written: {path}")
    typer.echo(f"  {server_note}")
    typer.echo("  Attach this file to your support ticket.")
