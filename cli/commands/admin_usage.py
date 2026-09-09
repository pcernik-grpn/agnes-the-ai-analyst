"""`agnes admin usage` — telemetry export from the terminal."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from cli.client import get_client

app = typer.Typer(help="Telemetry export and admin queries.")


def _handle_error(resp, context: str) -> None:
    """Print a clean error and exit non-zero for non-2xx responses."""
    if resp.status_code in (401, 403):
        typer.echo(
            "[err] authentication required — run `agnes auth login` or import a PAT",
            err=True,
        )
        raise typer.Exit(1)
    if resp.status_code >= 400:
        try:
            body = resp.json().get("detail", resp.text)
        except Exception:
            body = resp.text
        typer.echo(f"[err] server returned {resp.status_code}: {body}", err=True)
        raise typer.Exit(1)


def _handle_llm_error(resp, context: str) -> None:
    """``_handle_error`` plus the LLM-observability-specific hint: a typed
    ``501 requires_postgres_backend`` means this instance's app-state backend
    is still DuckDB — the ledger and the feedback table are Postgres-only
    under the A3 ratchet, so there is nothing this command can do until the
    instance migrates (docs/migrations.md)."""
    if resp.status_code == 501:
        try:
            body = resp.json()
        except Exception:
            body = {}
        if body.get("error") == "requires_postgres_backend":
            typer.echo(
                f"[err] {context} needs the Postgres app-state backend (see docs/migrations.md)",
                err=True,
            )
            raise typer.Exit(1)
    _handle_error(resp, context)


@app.command()
def export(
    format: str = typer.Option("csv", "--format", help="csv|json|parquet"),
    since: Optional[str] = typer.Option(
        None, "--since",
        help="ISO date or datetime, e.g. '2026-01-01' or '2026-05-01T00:00:00Z'."
    ),
    until: Optional[str] = typer.Option(
        None, "--until",
        help="ISO date or datetime (exclusive upper bound)."
    ),
    user: Optional[str] = typer.Option(None, "--user"),
    source: Optional[str] = typer.Option(None, "--source"),
    out: Optional[Path] = typer.Option(None, "--out", help="Write to file; else stdout."),
):
    """Export telemetry events filtered by since/until/user/source."""
    if format not in ("csv", "json", "parquet"):
        typer.echo(f"[err] format must be csv|json|parquet, got {format!r}", err=True)
        raise typer.Exit(1)

    params: dict = {"format": format}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    if user:
        params["user_id"] = user
    if source:
        params["source"] = source

    try:
        with get_client(timeout=120.0) as client:
            with client.stream("GET", "/api/admin/telemetry/export", params=params) as resp:
                if resp.status_code in (401, 403):
                    typer.echo(
                        "[err] authentication required — run `agnes auth login` or import a PAT",
                        err=True,
                    )
                    raise typer.Exit(1)
                if resp.status_code >= 400:
                    body = resp.read().decode(errors="replace")
                    typer.echo(f"[err] server returned {resp.status_code}: {body}", err=True)
                    raise typer.Exit(1)

                sink = out.open("wb") if out else sys.stdout.buffer
                try:
                    for chunk in resp.iter_bytes():
                        sink.write(chunk)
                    if out:
                        typer.echo(f"wrote {out}", err=True)
                finally:
                    if out:
                        sink.close()
    except typer.Exit:
        raise
    except Exception as exc:
        typer.echo(f"[err] cannot reach server: {exc}", err=True)
        raise typer.Exit(1)


@app.command()
def summary(
    window: str = typer.Option("7d", "--window", help="7d|30d|all"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
    limit: int = typer.Option(10, "--limit", help="Max tables to show."),
):
    """Query telemetry: top tables, scan bytes, and remote/local split (#410).

    Aggregates the query.remote / query.local / snapshot.create audit rows over
    the selected window — which tables are queried, how often, and (for remote
    tables) how many bytes were scanned.
    """
    if window not in ("7d", "30d", "all"):
        typer.echo(f"[err] window must be 7d|30d|all, got {window!r}", err=True)
        raise typer.Exit(1)

    client = get_client(timeout=60)
    try:
        resp = client.get("/api/admin/telemetry/summary", params={"window": window})
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    _handle_error(resp, "summary")
    data = resp.json()
    qt = data.get("query_telemetry") or {}

    if json_out:
        import json
        typer.echo(json.dumps(qt, indent=2))
        return

    typer.echo(f"Query telemetry — window {data.get('window', window)}")
    typer.echo(
        f"  total scan bytes: {qt.get('total_scan_bytes', 0):,}   "
        f"remote: {qt.get('remote_queries', 0)}   "
        f"local: {qt.get('local_queries', 0)}   "
        f"snapshots: {qt.get('snapshot_creates', 0)}"
    )
    tables = (qt.get("top_tables") or [])[:limit]
    if not tables:
        typer.echo("  (no query activity in this window)")
        return
    typer.echo("")
    typer.echo(
        f"  {'table':<52} {'queries':>8} {'failed':>7} {'remote':>7} "
        f"{'local':>6} {'scan_bytes':>14}"
    )
    for t in tables:
        # A `*` marks an id that is not in the table registry: it was parsed
        # out of query SQL and may not name a real table. The column is wide
        # enough for a qualified `project.dataset.table` path, which is how an
        # unresolved id is now spelled.
        label = str(t.get("table_id", ""))[:51]
        if t.get("registered") is False:
            label = f"{label}*"
        typer.echo(
            f"  {label:<52} "
            f"{t.get('queries', 0):>8} {t.get('failed', 0):>7} "
            f"{t.get('remote', 0):>7} "
            f"{t.get('local', 0):>6} {t.get('scan_bytes', 0):>14,}"
        )
    if any(t.get("registered") is False for t in tables):
        typer.echo("")
        typer.echo("  * not in the table registry (parsed from query SQL)")


@app.command("chat-cost")
def chat_cost(
    window: str = typer.Option("7d", "--window", help="1d|7d|30d|all"),
    user: str = typer.Option(None, "--user", help="Restrict to one session owner's email."),
    limit: int = typer.Option(20, "--limit", help="Max (session, model) rows to show."),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
):
    """Measured chat cost, split cached vs uncached — not a model, a measurement.

    Whether an AI workflow is expensive turns almost entirely on how a
    re-read of a large cached prefix is priced: at the full input rate it
    dominates the bill, at the real cached rate (~0.1x input) it nearly
    vanishes. This reads both halves straight out of the recorded per-message
    usage and prices each session by the model it actually ran on
    (src/llm_pricing.py), so a cost claim can be checked instead of modelled.

    A row marked `cache:unavailable` predates the recording of prompt-cache
    figures: its cached tokens are unknown, not zero, and its cost is a floor.
    """
    if window not in ("1d", "7d", "30d", "all"):
        typer.echo(f"[err] window must be 1d|7d|30d|all, got {window!r}", err=True)
        raise typer.Exit(1)

    params: dict = {"window": window, "limit": limit}
    if user:
        params["user"] = user

    client = get_client(timeout=60)
    try:
        resp = client.get("/api/admin/telemetry/chat-cost", params=params)
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    _handle_error(resp, "chat-cost")
    data = resp.json()

    if json_out:
        import json

        typer.echo(json.dumps(data, indent=2, default=str))
        return

    t = data.get("totals") or {}
    share = t.get("cached_input_share")
    share_str = f"{share:.1%}" if isinstance(share, (int, float)) else "n/a"
    typer.echo(f"Chat cost — window {data.get('window', window)}")
    typer.echo(
        f"  total: ${t.get('cost_usd', 0):.4f}   "
        f"in {t.get('input_tokens', 0):,}   out {t.get('output_tokens', 0):,}   "
        f"cache read {t.get('cache_read_tokens', 0):,}   cache write {t.get('cache_creation_tokens', 0):,}"
    )
    typer.echo(f"  share of read input served from cache: {share_str}")

    sessions = data.get("sessions") or []
    if not sessions:
        typer.echo("  (no assistant messages in this window)")
    else:
        typer.echo("")
        typer.echo(f"  {'session':<26} {'model':<20} {'msgs':>5} {'cost':>10} {'cached%':>8}  cache")
        for row in sessions:
            read_input = (
                (row.get("input_tokens") or 0)
                + (row.get("cache_read_tokens") or 0)
                + (row.get("cache_creation_tokens") or 0)
            )
            pct = f"{(row.get('cache_read_tokens') or 0) / read_input:.1%}" if read_input else "n/a"
            typer.echo(
                f"  {str(row.get('session_id', ''))[:25]:<26} "
                f"{str(row.get('model') or '-')[:19]:<20} "
                f"{row.get('messages', 0):>5} "
                f"${row.get('cost_usd', 0):>9.4f} "
                f"{pct:>8}  {row.get('cache_accounting', '?')}"
            )

    for note in data.get("notes") or []:
        typer.echo("")
        typer.echo(f"  note: {note}")


@app.command("llm-cost")
def llm_cost(
    window: str = typer.Option("7d", "--window", help="1d|7d|30d|all"),
    by: str = typer.Option("workload", "--by", help="workload|agent|user|model|purpose"),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
):
    """Measured LLM cost across every workload, priced at write time.

    Unlike `chat-cost` (chat sessions only), this reads the `llm_calls`
    ledger — every call from every call site (chat, builders, extraction,
    corporate memory, ...) — grouped by workload/agent/user/model/purpose.
    """
    if window not in ("1d", "7d", "30d", "all"):
        typer.echo(f"[err] window must be 1d|7d|30d|all, got {window!r}", err=True)
        raise typer.Exit(1)
    if by not in ("workload", "agent", "user", "model", "purpose"):
        typer.echo(f"[err] by must be workload|agent|user|model|purpose, got {by!r}", err=True)
        raise typer.Exit(1)

    client = get_client(timeout=60)
    try:
        resp = client.get("/api/admin/telemetry/llm-cost", params={"window": window, "by": by})
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    _handle_llm_error(resp, "llm-cost")
    data = resp.json()

    if json_out:
        import json

        typer.echo(json.dumps(data, indent=2, default=str))
        return

    t = data.get("totals") or {}
    typer.echo(
        f"LLM cost — window {data.get('window', window)} by {data.get('by', by)}: "
        f"${t.get('cost_usd', 0):.4f} over {t.get('calls', 0)} calls"
    )
    groups = data.get("groups") or []
    if not groups:
        typer.echo(
            "  (no LLM calls recorded in this window — the ledger fills as calls happen; see docs/observability.md)"
        )
    else:
        typer.echo("")
        typer.echo(
            f"  {'group':<28} {'calls':>6} {'in':>10} {'out':>8} {'cache rd':>10} {'cache wr':>9} "
            f"{'cached%':>8} {'cost':>10}"
        )
        for g in groups:
            share = g.get("cached_input_share")
            share_str = f"{share:.1%}" if isinstance(share, (int, float)) else "n/a"
            typer.echo(
                f"  {str(g.get('key') or '-')[:27]:<28} {g.get('calls', 0):>6} "
                f"{g.get('input_tokens', 0):>10,} {g.get('output_tokens', 0):>8,} "
                f"{g.get('cache_read_tokens', 0):>10,} {g.get('cache_creation_tokens', 0):>9,} "
                f"{share_str:>8} ${g.get('cost_usd', 0):>9.4f}"
            )
    for note in data.get("notes") or []:
        typer.echo("")
        typer.echo(f"  note: {note}")


@app.command("llm-calls")
def llm_calls(
    session_id: Optional[str] = typer.Option(None, "--session-id"),
    turn_id: Optional[str] = typer.Option(None, "--turn-id"),
    job_id: Optional[str] = typer.Option(None, "--job-id"),
    user_id: Optional[str] = typer.Option(None, "--user-id"),
    limit: int = typer.Option(50, "--limit", help="Max rows to show."),
    before: Optional[str] = typer.Option(None, "--before", help="ISO cursor — fetch rows older than this."),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
):
    """Detail rows for one session/turn/job/user from the LLM call ledger.

    One of --session-id, --turn-id, --job-id, --user-id is required — this
    is a drill-down into ONE unit of work, never an unbounded dump of every
    call the instance ever made.
    """
    if not any([session_id, turn_id, job_id, user_id]):
        typer.echo("[err] one of --session-id, --turn-id, --job-id, --user-id is required", err=True)
        raise typer.Exit(1)

    params: dict = {"limit": limit}
    if session_id:
        params["session_id"] = session_id
    if turn_id:
        params["turn_id"] = turn_id
    if job_id:
        params["job_id"] = job_id
    if user_id:
        params["user_id"] = user_id
    if before:
        params["before"] = before

    client = get_client(timeout=60)
    try:
        resp = client.get("/api/admin/telemetry/llm-calls", params=params)
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    _handle_llm_error(resp, "llm-calls")
    data = resp.json()

    if json_out:
        import json

        typer.echo(json.dumps(data, indent=2, default=str))
        return

    rows = data.get("rows") or []
    if not rows:
        typer.echo("  no calls recorded for that id — is this instance Postgres-backed? see docs/observability.md")
    else:
        typer.echo(
            f"  {'time':<26} {'kind':<11} {'workload':<12} {'purpose':<18} {'model':<22} "
            f"{'in':>8} {'out':>7} {'cache rd':>9} {'cost':>9}  status"
        )
        for row in rows:
            model = row.get("model_response") or row.get("model_requested") or "-"
            typer.echo(
                f"  {str(row.get('created_at') or '')[:25]:<26} {str(row.get('kind') or '-')[:10]:<11} "
                f"{str(row.get('workload') or '-')[:11]:<12} {str(row.get('purpose') or '-')[:17]:<18} "
                f"{str(model)[:21]:<22} {row.get('input_tokens', 0):>8,} {row.get('output_tokens', 0):>7,} "
                f"{row.get('cache_read_tokens', 0):>9,} ${row.get('cost_usd', 0):>8.4f}  {row.get('status', '-')}"
            )
        if data.get("next_before"):
            typer.echo(f"  more available — rerun with --before {data['next_before']}")
    for note in data.get("notes") or []:
        typer.echo(f"  note: {note}")


@app.command("feedback")
def feedback(
    window: str = typer.Option("7d", "--window", help="1d|7d|30d|all"),
    verdict: Optional[str] = typer.Option(None, "--verdict", help="up|down"),
    limit: int = typer.Option(50, "--limit", help="Max rows to show."),
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of a table."),
):
    """Chat-turn thumbs feedback — never prints the comment text in the
    table (use --json for that); the table shows only its length."""
    if window not in ("1d", "7d", "30d", "all"):
        typer.echo(f"[err] window must be 1d|7d|30d|all, got {window!r}", err=True)
        raise typer.Exit(1)
    if verdict is not None and verdict not in ("up", "down"):
        typer.echo(f"[err] verdict must be up|down, got {verdict!r}", err=True)
        raise typer.Exit(1)

    params: dict = {"window": window, "limit": limit}
    if verdict:
        params["verdict"] = verdict

    client = get_client(timeout=60)
    try:
        resp = client.get("/api/admin/telemetry/feedback", params=params)
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    _handle_llm_error(resp, "feedback")
    data = resp.json()

    if json_out:
        import json

        typer.echo(json.dumps(data, indent=2, default=str))
        return

    rows = data.get("rows") or []
    if not rows:
        typer.echo(
            "  no feedback recorded in this window — is this instance Postgres-backed? see docs/observability.md"
        )
        return
    typer.echo(f"  {'time':<26} {'session':<20} {'turn':<20} {'user':<24} {'verdict':<8} comment")
    for row in rows:
        comment = row.get("comment") or ""
        comment_label = f"{len(comment)} chars" if comment else "-"
        typer.echo(
            f"  {str(row.get('created_at') or '')[:25]:<26} {str(row.get('session_id') or '-')[:19]:<20} "
            f"{str(row.get('turn_id') or '-')[:19]:<20} {str(row.get('user_id') or '-')[:23]:<24} "
            f"{row.get('verdict', '-'):<8} {comment_label}"
        )


@app.command()
def reprocess():
    """Force re-extraction of all sessions for the usage processor."""
    client = get_client(timeout=60)
    try:
        resp = client.post("/api/admin/telemetry/reprocess")
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 401:
        typer.echo("[err] authentication required", err=True)
        raise typer.Exit(1)
    if resp.status_code == 403:
        typer.echo("[err] admin only", err=True)
        raise typer.Exit(1)
    if resp.status_code >= 400:
        typer.echo(f"[err] {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)
    data = resp.json()
    typer.echo("Reprocess scheduled — UsageProcessor will re-extract on next scheduler tick.")
    for k, v in data.get("deleted", {}).items():
        typer.echo(f"  deleted {k}: {v}")


@app.command()
def prune(
    json_out: bool = typer.Option(False, "--json", help="Emit raw JSON instead of summary."),
):
    """Prune usage_events older than USAGE_EVENTS_RETENTION_DAYS env var on the server."""
    client = get_client(timeout=60)
    try:
        resp = client.post("/api/admin/telemetry/prune")
    except Exception as e:
        typer.echo(f"[err] cannot reach server: {e}", err=True)
        raise typer.Exit(1)
    if resp.status_code == 401:
        typer.echo("[err] authentication required", err=True)
        raise typer.Exit(1)
    if resp.status_code == 403:
        typer.echo("[err] admin only", err=True)
        raise typer.Exit(1)
    if resp.status_code >= 400:
        typer.echo(f"[err] {resp.status_code}: {resp.text}", err=True)
        raise typer.Exit(1)
    data = resp.json()
    if json_out:
        import json
        typer.echo(json.dumps(data, indent=2))
        return
    if data.get("status") == "skipped":
        typer.echo(f"Skipped: {data.get('reason')}")
    else:
        typer.echo(
            f"Pruned {data['deleted']} events older than {data['retention_days']} days; "
            f"{data['remaining']} remain."
        )
