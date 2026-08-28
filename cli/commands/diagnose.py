"""Diagnose command — agnes diagnose."""

import json
from pathlib import Path

import typer

from cli.client import RedirectHardStop, api_get
from cli.config import get_sync_state, get_workspace_root
from cli.lib.jira_partition_check import detect_jira_partition_layout
from cli.lib.local_tables import local_table_names
from cli.lib.session_health import session_upload_health
from cli.lib.workspace_resolve import resolve_data_workspace

diagnose_app = typer.Typer(help="System diagnostics")


def _offered_table_names(manifest: dict) -> set[str]:
    """Which tables `agnes pull` would actually put on this laptop.

    Deliberately mirrors `cli/lib/pull.py:run_pull`'s download-set filter
    rather than counting the manifest's flat `tables` dict, so the comparison
    agrees with what pull fetches instead of with what the server lists:

    - The payload is a DICT keyed by table id (`app/api/sync.py`:
      `_build_manifest_for_user`), the shape every other consumer reads. A
      list is tolerated defensively (proxied / hand-crafted payloads) but is
      not the contract.
    - `query_mode='remote'` rows answer server-side and have no parquet at
      all; `server_only` rows have one but it is never distributed. Expecting
      either on disk reports a permanent, unfixable shortfall.
    - The flat dict is gated by `can_access_table`, whose Admin short-circuit
      bypasses the stack, so it over-lists for admins. When the manifest
      carries the typed v49 sections (`direct_tables` / `data_packages[]
      .tables[]`) those are the stack-scoped truth and `run_pull` filters
      through them; a pre-v49 server ships neither, and then the flat dict is
      all there is (`authorized_names is None` in `run_pull`).
    """
    raw = manifest.get("tables")
    entries: dict[str, dict] = {}
    if isinstance(raw, dict):
        entries = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    elif isinstance(raw, list):
        for t in raw:
            if isinstance(t, dict):
                tid = t.get("id") or t.get("name") or t.get("table_id")
                if tid:
                    entries[str(tid)] = t

    authorized: set[str] | None = None
    if any(k in manifest for k in ("direct_tables", "data_packages")):
        authorized = set()
        for pkg in manifest.get("data_packages") or []:
            for t in pkg.get("tables") or []:
                if isinstance(t, dict) and t.get("name"):
                    authorized.add(str(t["name"]))
        for t in manifest.get("direct_tables") or []:
            if isinstance(t, dict) and t.get("name"):
                authorized.add(str(t["name"]))

    offered: set[str] = set()
    for tid, info in entries.items():
        if (info.get("query_mode") or "local") == "remote":
            continue
        if info.get("server_only"):
            continue
        if authorized is not None and tid not in authorized:
            continue
        offered.add(tid)
    return offered


def _local_delivery_check() -> dict:
    """Can the analyst actually READ the data the server says they may?

    Every other check here asks the server about itself. This one compares
    what the manifest offers against what is on the laptop, because that gap
    is invisible from the server side: a fresh analyst whose `agnes pull`
    had 403'd on the download still saw "Overall: healthy" and "[ok] data".

    Severity vocabulary is the one the headline aggregation reads: a genuine
    shortfall is a `warning` (the whole point — a verdict that stays `healthy`
    through a total data outage is worse than no verdict), while the benign
    states are `info` so they never move it. No workspace at all is a normal
    first run, and a manifest this check cannot read is the `api` check's
    business, not evidence of missing data.
    """
    check: dict = {"name": "local-data", "audience": "analyst"}
    try:
        # Resolved the way the DATA commands resolve it (`agnes pull`,
        # `agnes query`, `agnes status`: `AGNES_LOCAL_DIR` override → cwd if
        # workspace-shaped → `workspace_root` anchor), not via the
        # push/session anchor alone — `agnes query` reads wherever
        # `resolve_data_workspace()` points, and inspecting any other
        # directory reports a false shortfall (or a false "run `agnes
        # init`") the moment the two differ: override set, analyst standing
        # in another workspace, stale or unset anchor.
        root = resolve_data_workspace()
        if root is None or not root.exists():
            check.update(
                status="info",
                detail="No workspace on this machine — run `agnes init` first.",
            )
            return check

        local = local_table_names(root / "server" / "parquet")

        try:
            resp = api_get("/api/sync/manifest")
            # A 401/403/500 comes back as an ordinary response with a JSON
            # error body — no `tables` key — which would read as "the server
            # offers nothing" and report a false green. Same pattern as
            # `cli/lib/pull.py`'s manifest fetch: non-2xx belongs in the
            # except branch below, not in the comparison.
            resp.raise_for_status()
            manifest = resp.json() or {}
            offered = _offered_table_names(manifest)
        except RedirectHardStop:
            # Derives from BaseException, so the clause below cannot take it —
            # and unhandled it would abort the whole command (exit 2, empty
            # output) on exactly the relocated deployment this command exists
            # to diagnose. The redirect verdict itself is the `api` check's
            # business; this row just declines the comparison.
            check.update(
                status="info",
                detail=f"{len(local)} table(s) local; could not read the manifest to compare (server redirected).",
                tables_local=len(local),
            )
            return check
        except Exception as e:
            check.update(
                status="info",
                detail=f"{len(local)} table(s) local; could not read the manifest to compare ({e}).",
                tables_local=len(local),
            )
            return check

        missing = offered - local
        check.update(tables_local=len(local), tables_offered=len(offered))
        if offered and not (offered & local):
            check.update(
                status="warning",
                detail=(
                    f"The server offers {len(offered)} table(s) but none are on this "
                    "machine — run `agnes pull` and read its output; a download "
                    "that 403s means the table is not in your stack yet."
                ),
            )
        elif missing:
            check.update(
                status="warning",
                detail=(
                    f"{len(offered) - len(missing)} of {len(offered)} offered table(s) "
                    "are local — `agnes pull` to fetch the rest."
                ),
            )
        else:
            check.update(status="ok", detail=f"{len(local)} table(s) available locally.")
        return check
    except Exception as e:
        check.update(status="info", detail=f"local delivery check failed: {e}")
        return check


@diagnose_app.callback(invoke_without_command=True)
def diagnose(
    ctx: typer.Context,
    symptom: str = typer.Option(None, "--symptom", help="Describe the problem"),
    component: str = typer.Option(None, "--component", help="Check specific component"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
    include_schema: bool = typer.Option(
        False,
        "--include-schema",
        help=(
            "Include the DB schema-version check. Off by default since the "
            "answer is rarely actionable on a healthy instance and shows up "
            "as noise in the agent-facing output (issue #204). On when the "
            "operator is verifying a migration."
        ),
    ),
    include_operator_checks: bool = typer.Option(
        False,
        "--include-operator-checks",
        help=(
            "Aggregate the headline status across operator-side checks "
            "(stale tables, session-pipeline cadence, BQ billing config) "
            "in addition to analyst-side ones. Default off when the caller "
            "is an analyst — those checks aren't actionable from a fresh "
            "analyst install and reading `Overall: degraded` on first run "
            "erodes trust in the install (issue #345 B). Admins/operators "
            "auto-promote to the full headline based on the server-reported "
            "caller_role."
        ),
    ),
):
    """Run comprehensive system diagnostics. AI-agent friendly output."""
    # If a subcommand was invoked (e.g. `agnes diagnose system`), defer to it
    # rather than running the default whole-system diagnostic.
    if ctx.invoked_subcommand is not None:
        return

    # #1312 (remaining scope, item 3): `diagnose` never named the workspace
    # it inspected anywhere, even though `_local_delivery_check` below
    # already resolves and reads one internally. Resolved the same way the
    # data commands do (`resolve_data_workspace()` — AGNES_LOCAL_DIR
    # override → cwd if workspace-shaped → the anchored workspace_root),
    # not the push/session anchor `get_workspace_root()` used a few checks
    # down — the two are deliberately different resolvers.
    workspace = resolve_data_workspace()

    checks = []
    # ``caller_role`` is present only on servers shipping the
    # role-aware health fields (issue #345 B). Legacy servers don't
    # ship it; absent role disables audience filtering so we don't
    # regress against an older server with the full-aggregation
    # contract the rest of the CLI was written against.
    caller_role: str | None = None

    # 1. API reachability
    try:
        resp = api_get("/api/health")
        resp.json()  # a 200 carrying a non-JSON body is not a healthy api
        checks.append(
            {"name": "api", "status": "ok", "audience": "analyst", "latency_ms": resp.elapsed.total_seconds() * 1000}
        )

        # Detailed health (auth required) for service-level checks
        try:
            params = {"include": "schema"} if include_schema else None
            resp_d = api_get("/api/health/detailed", params=params)
            detailed = resp_d.json()
            if "caller_role" in detailed:
                caller_role = detailed["caller_role"]
            for svc_name, svc_data in detailed.get("services", {}).items():
                check = {"name": svc_name, "status": svc_data.get("status", "unknown")}
                check.update({k: v for k, v in svc_data.items() if k != "status"})
                checks.append(check)
        except RedirectHardStop:
            # Scoped opt-in: the OUTER handler answers "can we reach the
            # server at all", and that question is already answered — the
            # reachability probe above succeeded and filed its `ok` row. A
            # redirect on this best-effort extra (a proxy that rewrites only
            # some paths) must not unwind into it and append a second,
            # contradicting `api` row saying `error` beside the `ok` one:
            # the reader is then told both, and anything selecting the first
            # match by name gets whichever landed first.
            #
            # `RedirectHardStop` derives from `BaseException`, so the clause
            # below cannot do this for us — being named here is the point.
            # Treated exactly like every other failure of this probe, whose
            # contract is that a missing detail is not worth a row.
            # (Devin Review on #1277.)
            pass
        except Exception:
            # Auth may not be configured — minimal reachability is sufficient
            pass
    except RedirectHardStop as e:
        # The opt-in. Everything else in the CLI keeps the old behaviour —
        # the message on stderr and exit 2 — because it does not write this
        # clause. This command does, because reporting a failed check IS the
        # command: dying here was exit 2, empty stdout and not one check,
        # from the thing whose job is to say what is wrong.
        checks.append({"name": "api", "status": "error", "audience": "analyst", "detail": e.user_message})
    except Exception as e:
        checks.append({"name": "api", "status": "error", "audience": "analyst", "detail": str(e)})

    # Issue #244: detect sessions on disk that aren't reaching the server by
    # comparing transcripts in the workspace's Claude Code folder against the
    # upload-ledger entries. Anchored on the `workspace_root` config key (the
    # same anchor `agnes push` uses). Adds one `session-upload` check entry.
    try:
        ws_root = get_workspace_root()
        if ws_root:
            cap = session_upload_health(Path(ws_root))
        else:
            cap = {
                "name": "session-upload",
                "status": "info",
                "detail": "no workspace_root in config — run `agnes init`",
            }
        cap.setdefault("audience", "analyst")
        checks.append(cap)
    except Exception as e:
        checks.append(
            {"name": "session-upload", "status": "info", "audience": "analyst", "detail": f"health check failed: {e}"}
        )

    # Issue #394: detect Jira partition layout (flat YYYY-MM vs hive month=*/).
    # Resolves the Jira data directory from the DATA_DIR env var (mirrors how
    # the connector itself locates its output); defaults to /data/extracts/jira.
    # Operator-only audience — analysts can't act on a partition migration.
    try:
        import os

        _data_root = Path(os.environ.get("DATA_DIR", "/data"))
        _jira_dir = _data_root / "extracts" / "jira"
        jira_check = detect_jira_partition_layout(_jira_dir)
        jira_check.setdefault("audience", "operator")
        checks.append(jira_check)
    except Exception as e:
        checks.append(
            {
                "name": "jira-partition-format",
                "status": "info",
                "audience": "operator",
                "detail": f"partition check failed: {e}",
            }
        )

    # Local delivery: does the analyst actually HAVE the data the server says
    # they may read? Every check above this point asks the server about
    # itself, so `agnes diagnose` reported "Overall: healthy" including
    # "[ok] data" while a fresh analyst's tables were unreachable — the
    # manifest listed one table, `agnes pull` had 403'd on the download, and
    # nothing on the laptop said so. A diagnostic that stays green through a
    # total data outage is worse than none: the analyst who hit this stopped
    # trusting it and went to raw curl.
    #
    # Analyst-audience on purpose — this is the analyst's own workspace, and
    # it is the one thing they can act on ("run agnes pull", "ask for the
    # grant"). A real shortfall is a `warning` so it reaches the headline;
    # the benign states (no workspace yet, manifest unreadable) stay `info`
    # and never move it — a first run is not a broken instance.
    checks.append(_local_delivery_check())

    # Determine overall — `info` and `unknown` surface in the per-check
    # output but never promote the headline (issue #178).
    #
    # Audience-aware headline (issue #345 B): when the server reports a
    # ``caller_role``, analysts see analyst-only aggregation by default;
    # operators auto-promote to the full headline; analysts can manually
    # opt in via ``--include-operator-checks``. Legacy servers that don't
    # ship ``caller_role`` keep the original full-aggregation behaviour
    # — no analyst-only filtering until the server tags checks.
    role_aware = caller_role is not None
    operator_mode = (not role_aware) or include_operator_checks or caller_role != "analyst"
    relevant = checks if operator_mode else [c for c in checks if c.get("audience") == "analyst"]
    overall = "healthy"
    for c in relevant:
        if c["status"] == "error":
            overall = "unhealthy"
            break
        if c["status"] == "warning":
            overall = "degraded"

    # Generate suggested actions
    actions = []
    for c in checks:
        if c["status"] == "error" and c["name"] == "api":
            actions.append("Server unreachable. Check: docker compose ps, agnes server logs")
        if c.get("stale_tables"):
            for t in c["stale_tables"]:
                actions.append(f"Table '{t}' is stale. Run: agnes server logs scheduler | grep {t}")
        if c["name"] == "session-upload" and c["status"] == "warning":
            actions.append(
                "Session upload may be failing. Run `agnes push --dry-run` to see "
                "which transcripts would upload and confirm the workspace_root anchor "
                "(check `agnes diagnose` / config) resolves to your Claude Code folder."
            )

    result = {
        # #1312 — the workspace this run inspected (None when nothing
        # resolves — no AGNES_LOCAL_DIR, cwd unshaped, no anchor).
        "workspace": str(workspace) if workspace is not None else None,
        "overall": overall,
        "caller_role": caller_role,
        "checks": checks,
        "suggested_actions": actions,
    }

    if as_json:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(f"Workspace: {workspace if workspace is not None else 'none found — run `agnes init` first'}")
        # When analysts are filtered to analyst-only aggregation, surface
        # any operator-side warnings as a secondary line so they're not
        # invisible — they just don't get to drive the headline.
        operator_warns = [
            c for c in checks if c.get("audience") == "operator" and c.get("status") in ("warning", "error")
        ]
        if not operator_mode and operator_warns:
            typer.echo(
                f"Overall: {overall} (analyst-side); "
                f"{len(operator_warns)} operator-side "
                f"{'warning' if len(operator_warns) == 1 else 'warnings'}"
            )
        else:
            typer.echo(f"Overall: {overall}")
        for c in checks:
            detail = ""
            if "detail" in c:
                detail = f" — {c['detail']}"
            if "tables" in c:
                detail = f" ({c['tables']} tables)"
            if "latency_ms" in c:
                detail = f" ({c['latency_ms']:.0f}ms)"
            typer.echo(f"  [{c['status']:7s}] {c['name']}{detail}")
        if actions:
            typer.echo("\nSuggested actions:")
            for a in actions:
                typer.echo(f"  - {a}")


@diagnose_app.command("system")
def system_status(
    local: bool = typer.Option(False, "--local", help="Show local-only status (no server)"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show server-side health status (was `agnes status` pre-clean-bootstrap).

    Reports server reachability and per-service health. Use `agnes status` for
    workspace-side state (initialized? data fresh?).
    """
    if local:
        state = get_sync_state()
        info = {
            "mode": "local",
            "tables_synced": len(state.get("tables", {})),
            "last_sync": state.get("last_sync", "never"),
            "tables": state.get("tables", {}),
        }
        if as_json:
            typer.echo(json.dumps(info, indent=2))
        else:
            typer.echo("Mode: offline (local data)")
            typer.echo(f"Tables synced: {info['tables_synced']}")
            typer.echo(f"Last sync: {info['last_sync']}")
        return

    try:
        # Minimal health ping first
        resp = api_get("/api/health")
        minimal = resp.json()
        if minimal.get("status") != "ok":
            if as_json:
                typer.echo(json.dumps(minimal, indent=2))
            else:
                typer.echo(f"Status: {minimal.get('status', 'unknown')}")
            return

        # Detailed health (auth required) for service-level info
        try:
            resp = api_get("/api/health/detailed")
            data = resp.json()
        except Exception:
            data = minimal

        if as_json:
            typer.echo(json.dumps(data, indent=2))
        else:
            typer.echo(f"Status: {data.get('status', 'unknown')}")
            for name, check in data.get("services", {}).items():
                s = check.get("status", "?")
                detail = ""
                if "tables" in check:
                    detail = f" ({check['tables']} tables, {check.get('total_rows', 0)} rows)"
                if "count" in check:
                    detail = f" ({check['count']})"
                if check.get("stale_tables"):
                    detail += f" [stale: {', '.join(check['stale_tables'])}]"
                typer.echo(f"  {name}: {s}{detail}")
    except Exception as e:
        typer.echo(f"Cannot reach server: {e}", err=True)
        typer.echo("Use --local for offline status.")
        raise typer.Exit(1)
