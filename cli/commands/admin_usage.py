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
