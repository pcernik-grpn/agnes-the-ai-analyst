"""Admin commands — agnes admin."""

import json

import typer

from cli.client import api_delete, api_get, api_post, api_put
from cli.commands.admin_activity import activity_app
from cli.commands.admin_analytics import analytics_app as admin_analytics_app
from cli.commands.admin_ask import app as admin_ask_app
from cli.commands.admin_autodoc import autodoc_tables
from cli.commands.admin_config import admin_config_app
from cli.commands.admin_connection import admin_connection_app
from cli.commands.admin_data_package import admin_data_package_app
from cli.commands.admin_digest import admin_digest_app
from cli.commands.admin_doctor import doctor_app as admin_doctor_app
from cli.commands.admin_jobs import admin_jobs_app
from cli.commands.admin_marketplace import admin_marketplace_app
from cli.commands.admin_mcp import mcp_app as admin_mcp_app
from cli.commands.admin_memory_domain import admin_memory_domain_app
from cli.commands.admin_metrics import admin_metrics_app
from cli.commands.admin_news import admin_news_app
from cli.commands.admin_semantic import admin_semantic_app
from cli.commands.admin_semantic_layer import admin_semantic_layer_app
from cli.commands.admin_semantic_model import admin_semantic_model_app
from cli.commands.admin_semantic_source import admin_semantic_source_app
from cli.commands.admin_sessions import sessions_app as admin_sessions_app
from cli.commands.admin_skills import admin_skills_app
from cli.commands.admin_sso import admin_sso_app
from cli.commands.admin_store import admin_store_app
from cli.commands.admin_usage import app as admin_usage_app
from cli.commands.db import db_app as admin_db_app
from cli.commands.memory_admin import memory_admin_app
from src.repositories import (
    column_metadata_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

admin_app = typer.Typer(help="Admin operations (requires admin role)")
admin_app.add_typer(
    activity_app, name="activity", help="Activity Center — audit_log timeline, health pulse, sync history"
)
admin_app.add_typer(admin_ask_app, name="ask", help="Ask a natural-language question about telemetry")
admin_app.add_typer(admin_metrics_app, name="metrics")
admin_app.add_typer(admin_sessions_app, name="sessions", help="Browse Claude Code sessions across all users")
admin_app.add_typer(admin_store_app, name="store")
admin_app.add_typer(admin_news_app, name="news")
admin_app.add_typer(memory_admin_app, name="memory")
# Telemetry subcommand: primary name is "telemetry", "usage" kept as an
# alias so existing operator scripts that call `agnes admin usage export …`
# keep working through this release. Drop the alias in a future cleanup
# once external callers have caught up.
admin_app.add_typer(admin_usage_app, name="telemetry", help="Telemetry export and admin queries")
admin_app.add_typer(admin_usage_app, name="usage", help="(deprecated alias of `telemetry`)")
admin_app.add_typer(admin_data_package_app, name="data-package", help="Data Package CRUD (v49)")
admin_app.add_typer(admin_memory_domain_app, name="memory-domain", help="Memory Domain CRUD (v49)")
admin_app.add_typer(admin_digest_app, name="digest", help="Maintained digest CRUD (K4)")
admin_app.add_typer(admin_db_app, name="db", help="Manage app-state DB backend (DuckDB / Postgres)")
admin_app.add_typer(admin_sso_app, name="sso", help="External SSO login (Entra ID OIDC) runtime config")
admin_app.add_typer(
    admin_doctor_app, name="doctor", help="Deployment-gate diagnostics (`agnes admin doctor --new-instance`)"
)
admin_app.add_typer(
    admin_marketplace_app, name="marketplace", help="Curated marketplace ops (list / sync / disable-plugin)"
)
admin_app.add_typer(admin_mcp_app, name="mcp", help="Universal MCP source + tool admin")
# One semantic-layer admin group (#1707 Block 6). The three it replaced were
# split by which endpoint family they called, which is not something a reader
# can guess; they stay registered HIDDEN for one release so no existing
# invocation breaks, each printing the new path on stderr before delegating.
admin_app.add_typer(
    admin_semantic_app, name="semantic", help="The semantic layer: documents, their sources, and its health"
)
admin_app.add_typer(admin_semantic_layer_app, name="semantic-layer", hidden=True)
admin_app.add_typer(admin_semantic_model_app, name="semantic-model", hidden=True)
admin_app.add_typer(admin_semantic_source_app, name="semantic-source", hidden=True)
admin_app.add_typer(
    admin_connection_app, name="connection", help="Named source-connection CRUD (multi-project Keboola)"
)
admin_app.add_typer(
    admin_config_app, name="config", help="Export/apply the server-config overlay as reviewable YAML (Track D3)"
)
admin_app.add_typer(admin_skills_app, name="skill", help="Contributed skills management")
admin_app.add_typer(admin_jobs_app, name="jobs", help="Job queue admin (wave-2B worker runtime)")
admin_app.add_typer(admin_analytics_app, name="analytics", help="DuckLake analytics-backend migration (wave-2G)")
# Single direct command (mirrors `register-table` / `discover-and-register`):
# LLM-generate descriptions for undescribed tables (#399).
admin_app.command("autodoc-tables")(autodoc_tables)

# Table access policies (design doc §13.2, plan Task 16) — a narrow,
# purpose-specific nested group, mirroring `data-package` / `memory-domain` /
# `connection` / `mcp` / `semantic` above. NOT the generic `agnes admin
# table` catch-all the design doc explicitly rejects hanging noun-verb
# commands off of. Attach/replace/clear stays flat on `update-table --policy`
# (mirrors `--query`); this group is read-only inspection — the stored policy
# (`show`) and a single-persona dry-run (`preview`, §13.1). `preview` is the
# CLI surface Task 14's `POST /policy/preview` EXEMPT classification names.
table_policy_app = typer.Typer(help="Table access-policy inspection (design doc §13.2)")
admin_app.add_typer(table_policy_app, name="table-policy")


@admin_app.command("add-user")
def add_user(
    email: str = typer.Argument(..., help="User email"),
    name: str = typer.Option("", help="User display name"),
    invite: bool = typer.Option(
        False,
        "--invite",
        help="Also issue a setup link and email it (needs SMTP_HOST). The link is printed either way.",
    ),
):
    """Add a new user, optionally inviting them.

    Without --invite the account exists but carries no way in yet — issue the
    setup link later with `agnes admin reset-password <email>`. New users also
    start with no group memberships — to make them admin, add them to the Admin
    group separately:

        agnes admin group add-member <admin-group-id> <email>
    """
    resp = api_post(
        "/api/users",
        json={"email": email, "name": name or email.split("@")[0], "send_invite": invite},
    )
    if resp.status_code != 201:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    typer.echo(f"Created user: {data['email']} (id: {data['id']})")

    if not invite:
        typer.echo(
            "  No invitation sent — the account has no way in yet.\n"
            "  Re-run with --invite next time, or send the setup link now:\n"
            f"    agnes admin reset-password {data['email']}"
        )
        return

    # An invite the server never issued must not read as one that went out —
    # the person would sit waiting for mail that is not coming.
    setup_url = data.get("invite_url")
    if not setup_url:
        typer.echo(
            "  Invitation requested but the server returned no setup link.\n"
            "  The account exists — do NOT re-run add-user; send the link with:\n"
            f"    agnes admin reset-password {data['email']}",
            err=True,
        )
        raise typer.Exit(1)

    if data.get("invite_email_sent"):
        typer.echo(f"  Invitation emailed to {data['email']}.")
    else:
        typer.echo(
            "  Warning: email transport not configured — send this link to the user yourself.",
            err=True,
        )
    typer.echo(f"  Setup link: {setup_url}")


@admin_app.command("list-users")
def list_users(as_json: bool = typer.Option(False, "--json")):
    """List all users."""
    resp = api_get("/api/users")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    users = resp.json()
    if as_json:
        typer.echo(json.dumps(users, indent=2))
    else:
        for u in users:
            status_str = "active" if u.get("active", True) else "DEACTIVATED"
            admin_flag = "admin" if u.get("is_admin") else "user"
            typer.echo(f"  {u['email']:30s} {admin_flag:6s} {status_str:12s} id={u['id'][:8]}")


@admin_app.command("remove-user")
def remove_user(user_id: str = typer.Argument(..., help="User ID to remove")):
    """Remove a user."""
    resp = api_delete(f"/api/users/{user_id}")
    if resp.status_code == 204:
        typer.echo("User removed.")
    else:
        typer.echo(f"Failed: {resp.text}", err=True)
        raise typer.Exit(1)


@admin_app.command("register-table")
def register_table(
    name: str = typer.Argument(..., help="Table display name (DuckDB view name for BQ)"),
    source_type: str = typer.Option(
        "keboola", help="Source type: keboola | bigquery | jira | local | databricks | snowflake"
    ),
    bucket: str = typer.Option(
        "",
        help="Source bucket (Keboola), dataset (BigQuery), or schema (Databricks; 'catalog.schema' overrides the default catalog)",
    ),
    source_table: str = typer.Option("", help="Source table name in the bucket/dataset/schema"),
    query_mode: str | None = typer.Option(
        None,
        help="Query mode: local | remote | materialized (default: local for keboola/jira/local, materialized for databricks/snowflake, remote for bigquery)",
    ),
    query: str = typer.Option(
        "",
        "--query",
        help=(
            "SQL body for query_mode='materialized' (BigQuery only). Inline SQL or `@path/to.sql` to read from disk."
        ),
    ),
    description: str = typer.Option("", help="Table description"),
    sync_schedule: str = typer.Option(
        "",
        help="Cron schedule (e.g. 'every 6h' / 'daily 03:00'); honored by materialized BQ rows",
    ),
    # v26 Keboola sync-strategy support
    sync_strategy: str = typer.Option(
        "full_refresh",
        "--sync-strategy",
        help="Keboola: full_refresh (default) | incremental | partitioned",
    ),
    primary_key: str = typer.Option(
        "",
        "--primary-key",
        help="Primary key column(s), comma-separated. Required for incremental dedup.",
    ),
    incremental_window_days: int = typer.Option(
        None,
        "--incremental-window-days",
        help="Backtrack window applied to last_sync (default 7 at sync time)",
    ),
    max_history_days: int = typer.Option(
        None,
        "--max-history-days",
        help="Cap on first-sync history depth (None = unbounded)",
    ),
    where_filters_json: str = typer.Option(
        "",
        "--where-filters-json",
        help=(
            "JSON array of {column, operator, values}. Inline JSON or "
            "@path/to/filters.json. Date placeholders supported: "
            "{{today}}, {{last_week}}, {{last_3_months}}, etc. "
            "(see connectors.keboola.where_filters for the full list). "
            "Filters force the SDK extraction path (slower than the "
            "DuckDB extension); use only when needed."
        ),
    ),
    partition_by: str = typer.Option(
        "",
        "--partition-by",
        help="Date column driving partition keys (required for partitioned strategy)",
    ),
    partition_granularity: str = typer.Option(
        "",
        "--partition-granularity",
        help="day | month (default) | year — for partitioned strategy",
    ),
    initial_load_chunk_days: int = typer.Option(
        None,
        "--initial-load-chunk-days",
        help="Chunk size for partitioned first-sync chunked initial load (default 30)",
    ),
    server_only: bool = typer.Option(
        False,
        "--server-only",
        help=(
            "Keep the table server-side: queryable via `agnes query --remote`, "
            "listed in the catalog, but its parquet is never downloaded by "
            "`agnes pull`. Only valid with --query-mode local|materialized."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Run validation + (BQ) source-side check without writing to the registry",
    ),
    connection_id: str = typer.Option(
        None,
        "--connection-id",
        help=(
            "Pin this table to a named source connection (source_connections.id). "
            "NULL uses the default connection for the row's source_type."
        ),
    ),
):
    """Register a single table.

    Modes:
    - **local** (Keboola): batch pull, parquet on disk. Requires
      `--bucket` + `--source-table`.
    - **remote** (BigQuery): view only, queries go to BQ. Requires
      `--bucket` + `--source-table`.
    - **materialized** (BigQuery): server-side scheduled SQL → parquet.
      Requires `--query` (inline or `@file.sql`) AND `--bucket` (BQ
      dataset of the destination identifier). `--source-table` defaults
      to the registered `name` when omitted; explicit override is rare.
      Note: `agnes schema <name>` builds the BQ identifier as
      `bq.<bucket>.<source_table>` even for materialized rows, so an
      empty `--bucket` here registers the row but breaks subsequent
      schema/describe calls.

    `--dry-run` goes through /precheck (BQ remote only — for materialized
    rows, dry-run is a no-op since the SQL itself is the contract).
    """
    from pathlib import Path

    if query_mode is None:
        if source_type == "bigquery":
            query_mode = "remote"
        elif source_type in ("databricks", "snowflake"):
            query_mode = "materialized"
        else:
            query_mode = "local"

    # Resolve --query @file.sql shorthand.
    source_query = ""
    if query:
        if query.startswith("@"):
            sql_path = Path(query[1:])
            if not sql_path.exists():
                typer.echo(f"Error: SQL file not found: {sql_path}", err=True)
                raise typer.Exit(2)
            source_query = sql_path.read_text(encoding="utf-8").strip()
        else:
            source_query = query.strip()

    # Keboola materialized rows can omit --query: a NULL source_query means
    # "full-table export via Storage API export-async" (see v25→v26
    # migration notes). Databricks materialized rows can omit it too — the
    # server generates the full-table dump SQL from --bucket/--source-table
    # (+ data_source.databricks.catalog). For BigQuery materialized rows,
    # --query is still required — BQ has no analogous "full table" semantic
    # at the registry layer (the path is a SELECT against
    # `<project>.<dataset>.<table>`, which the admin must spell out).
    if query_mode == "materialized" and not source_query and source_type not in ("keboola", "databricks", "snowflake"):
        typer.echo(
            "Error: --query-mode materialized requires --query (literal SQL or @path.sql) for source_type="
            + source_type,
            err=True,
        )
        raise typer.Exit(2)

    # Bucket is load-bearing on materialized rows. For BQ it backs the
    # destination identifier (`agnes schema <name>` builds `bq."<bucket>"."
    # <src>"` from it; an empty bucket trips "unsafe BQ identifier in
    # registry" at query time). For Keboola it's the bucket id passed to
    # `/v2/storage/tables/<bucket>.<source_table>/export-async` — without
    # it the export call would 404. Same requirement, different rationale.
    if query_mode == "materialized" and not bucket:
        typer.echo(
            "Error: --query-mode materialized requires --bucket (the "
            "BQ dataset / Keboola bucket id for the source identifier).",
            err=True,
        )
        raise typer.Exit(2)

    # --server-only suppresses *distribution* of a server-stored parquet. A
    # remote row has none (every query goes live upstream), so the pairing is
    # incoherent — the same check the server-side validator makes
    # (RegisterTableRequest._check_server_only_query_mode). Fail before the
    # round-trip so the operator sees the conflict immediately.
    if server_only and query_mode == "remote":
        typer.echo(
            "Error: --server-only is only valid with --query-mode local or "
            "materialized (a 'remote' table has no server-stored parquet to "
            "suppress from agnes pull).",
            err=True,
        )
        raise typer.Exit(2)

    payload = {
        "name": name,
        "source_type": source_type,
        "bucket": bucket,
        "source_table": source_table or name,
        "query_mode": query_mode,
        "description": description,
    }
    # Omit empty optional fields so the server-side validator doesn't see
    # `source_query=""` on a remote/local row (which would trigger the
    # "source_query forbidden" branch).
    if source_query:
        payload["source_query"] = source_query
    if sync_schedule:
        payload["sync_schedule"] = sync_schedule
    if connection_id is not None:
        payload["connection_id"] = connection_id

    # v26 sync-strategy support fields. Always send sync_strategy (it has a
    # default). Send the rest only when the operator set them — empty/None
    # → omit so the server stores NULL.
    payload["sync_strategy"] = sync_strategy
    if primary_key:
        payload["primary_key"] = [c.strip() for c in primary_key.split(",") if c.strip()]
    if incremental_window_days is not None:
        payload["incremental_window_days"] = incremental_window_days
    if max_history_days is not None:
        payload["max_history_days"] = max_history_days
    if partition_by:
        payload["partition_by"] = partition_by
    if partition_granularity:
        payload["partition_granularity"] = partition_granularity
    if initial_load_chunk_days is not None:
        payload["initial_load_chunk_days"] = initial_load_chunk_days
    # Omit when false — the server defaults to false, so an unflagged
    # registration sends the same body it sent before this flag existed.
    if server_only:
        payload["server_only"] = True
    if where_filters_json:
        # Inline JSON or @path/to.json
        if where_filters_json.startswith("@"):
            wf_path = Path(where_filters_json[1:])
            if not wf_path.exists():
                typer.echo(f"Error: where_filters file not found: {wf_path}", err=True)
                raise typer.Exit(2)
            wf_text = wf_path.read_text(encoding="utf-8")
        else:
            wf_text = where_filters_json
        try:
            import json as _json

            payload["where_filters"] = _json.loads(wf_text)
        except _json.JSONDecodeError as e:
            typer.echo(f"Error: --where-filters-json is not valid JSON: {e}", err=True)
            raise typer.Exit(2)

    if dry_run:
        # Hits /precheck — no DB write, but for BQ does a real
        # bigquery.Client(project).get_table() round-trip so the operator
        # gets the same NotFound / Forbidden error they'd see at
        # registration time, before committing.
        resp = api_post("/api/admin/register-table/precheck", json=payload)
        if resp.status_code == 200:
            data = resp.json()
            t = data.get("table") or {}
            typer.echo("[DRY RUN] precheck OK")
            typer.echo(f"  name:         {t.get('name')}")
            typer.echo(f"  source_type:  {t.get('source_type')}")
            typer.echo(f"  bucket:       {t.get('bucket')}")
            typer.echo(f"  source_table: {t.get('source_table')}")
            if t.get("project_id"):
                typer.echo(f"  project_id:   {t.get('project_id')}")
            if t.get("rows") is not None:
                typer.echo(f"  rows:         {t.get('rows'):,}")
            if t.get("size_bytes") is not None:
                typer.echo(f"  size_bytes:   {t.get('size_bytes'):,}")
            cols = t.get("columns") or []
            if cols:
                typer.echo(f"  columns ({len(cols)}):")
                for c in cols:
                    typer.echo(f"    - {c.get('name'):<32s} {c.get('type', '')}")
            return
        typer.echo(f"Precheck failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    resp = api_post("/api/admin/register-table", json=payload)
    # 200 (BQ sync materialize OK), 201 (legacy non-BQ), and 202 (BQ
    # background materialize) are all success.
    if resp.status_code in (200, 201, 202):
        if resp.status_code == 202:
            typer.echo(f"Registered (materializing in background): {name}")
        else:
            typer.echo(f"Registered: {name}")

        # Post-success hints. Two operator gotchas this catches:
        #
        # 1. `agnes pull` does not auto-materialize newly-registered
        #    rows — registration adds a registry row, but the parquet
        #    is built only when the scheduler tick runs (or first-sync
        #    is triggered manually). Without this hint operators see
        #    "Updated 0 tables" on `agnes pull` and assume something
        #    is broken.
        # 2. `register-table` does NOT auto-grant. `agnes catalog`
        #    filters per-user via `resource_grants`, so operators
        #    other than the registering admin won't see the new row
        #    until a grant is created.
        #
        # Hint #1 only fires for `local` and `materialized` (the modes
        # that actually produce a parquet); 202-async path covers a
        # different signal, so don't double-message there.
        if query_mode in ("local", "materialized") and resp.status_code != 202:
            typer.echo(
                "  Next: run `agnes setup first-sync` to materialize the parquet (or wait for the scheduler tick)."
            )
        typer.echo(
            f"  Note: register-table does not auto-grant. Run "
            f"`agnes admin grant create <group> table {name}` to "
            f"make this visible in `agnes catalog` for non-admin users."
        )
        # Third hint: BQ-remote rows can fail at first analyst query if the
        # SA lacks dataViewer/jobUser. Pointing at the smoke command
        # surfaces the failure at registration time, not 30 minutes later.
        if query_mode == "remote":
            typer.echo(
                f"  Note: this is a remote-query table. Verify the SA can read it:\n"
                f'    agnes query --remote "SELECT COUNT(*) FROM {name}"\n'
                f'  If it 403s, see docs/admin/query-modes.md → "BigQuery → IAM".'
            )
    elif resp.status_code == 409:
        typer.echo(f"Already exists: {name}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


@admin_app.command("discover-and-register")
def discover_and_register(
    source_type: str = typer.Option("keboola", help="Source type: keboola"),
    token: str = typer.Option(None, help="Keboola Storage API token"),
    url: str = typer.Option(None, help="Keboola stack URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be registered"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Discover all tables from a Keboola source and register them."""
    import os

    import httpx

    if source_type != "keboola":
        typer.echo(
            f"Discovery is not implemented for source_type='{source_type}'. "
            "Only Keboola Storage API discovery is supported; "
            "register individual tables with `agnes admin register-table`.",
            err=True,
        )
        raise typer.Exit(2)

    kbc_token = token or os.environ.get("KEBOOLA_STORAGE_TOKEN", "")
    kbc_url = url or os.environ.get("KEBOOLA_STACK_URL", "")

    if not kbc_token or not kbc_url:
        typer.echo("Need KEBOOLA_STORAGE_TOKEN and KEBOOLA_STACK_URL (env or --token/--url)", err=True)
        raise typer.Exit(1)

    typer.echo(f"Discovering tables from {kbc_url}...")
    resp = httpx.get(f"{kbc_url.rstrip('/')}/v2/storage/tables", headers={"X-StorageApi-Token": kbc_token}, timeout=30)
    resp.raise_for_status()
    tables = resp.json()
    typer.echo(f"Found {len(tables)} tables")

    if as_json and dry_run:
        typer.echo(
            json.dumps(
                [
                    {
                        "id": t["id"],
                        "name": t["name"],
                        "bucket": t.get("bucket", {}).get("id", ""),
                        "rows": t.get("rowsCount", 0),
                    }
                    for t in tables
                ],
                indent=2,
            )
        )
        return

    registered = 0
    skipped = 0
    errors = 0

    for t in tables:
        name = t["name"]
        bucket_id = t.get("bucket", {}).get("id", "")

        if dry_run:
            typer.echo(f"  [DRY RUN] {name:30s} bucket={bucket_id:20s} rows={t.get('rowsCount', 0):>10,}")
            continue

        # Keboola tables always go through the Storage API export-async
        # path (`materialize_query`), which is `query_mode='materialized'`
        # in the registry. A NULL source_query means "full table export"
        # — same effective semantics the old 'local' mode gave, but via
        # the Storage API instead of the DuckDB extension. See
        # connectors/keboola/storage_api.py + the v25→v26 migration.
        resp = api_post(
            "/api/admin/register-table",
            json={
                "name": name,
                "source_type": source_type,
                "bucket": bucket_id,
                "source_table": name,
                "query_mode": "materialized",
                "description": f"Auto-discovered from {source_type}",
            },
        )

        # 200 (synchronous materialize), 201 (legacy non-BQ insert),
        # and 202 (background materialize) are all success — mirrors
        # the matrix in the single-table register-table command. Pre-fix
        # this only accepted 201, so every successful BQ row counted as
        # an error (review NIT 6 in #119).
        if resp.status_code in (200, 201, 202):
            registered += 1
            suffix = " (materializing in background)" if resp.status_code == 202 else ""
            typer.echo(f"  ✓ {name}{suffix}")
        elif resp.status_code == 409:
            skipped += 1
        else:
            errors += 1
            typer.echo(f"  ✗ {name}: {resp.json().get('detail', resp.text)}")

    if not dry_run:
        typer.echo(f"\nDone: {registered} registered, {skipped} already existed, {errors} errors")


@admin_app.command("sync")
def sync(
    source: str = typer.Option(
        None,
        "--source",
        help=(
            "Restrict the rebuild to one source_type (keboola | bigquery | "
            "jira | local | databricks). Omit for a full sweep of every registered table."
        ),
    ),
    tables: list[str] = typer.Argument(
        None,
        help="Optional table ids to rebuild (default: all due tables).",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Trigger a data sync. `--source` scopes a partial rebuild to one source.

    Posts to `/api/sync/trigger`, which enqueues a `data-refresh` job
    (worker runtime, wave-2B) instead of running the sync inline;
    `--source` is passed through as the `?source=` query param so only
    that source's local + materialized rows are rebuilt, leaving the
    other source's extract untouched. Returns 409 (with the in-flight
    job's `job_id`) if a `data-refresh` job is already queued or running —
    poll it with `agnes admin jobs show <job_id>`.
    """
    params = {"source": source} if source else None
    json_body = {"tables": list(tables)} if tables else None
    resp = api_post("/api/sync/trigger", params=params, json=json_body)

    if resp.status_code == 409:
        detail = resp.json().get("detail")
        job_id = detail.get("job_id") if isinstance(detail, dict) else None
        msg = "A sync is already in progress — try again shortly."
        if job_id:
            msg += f" (job_id={job_id}, check `agnes admin jobs show {job_id}`)"
        typer.echo(msg, err=True)
        raise typer.Exit(1)
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        scope = data.get("source", "all")
        which = data.get("tables", "all")
        job_id = data.get("job_id")
        suffix = f" (job_id={job_id})" if job_id else ""
        typer.echo(f"Sync triggered (source={scope}, tables={which}).{suffix}")


@admin_app.command("list-tables")
def list_tables(as_json: bool = typer.Option(False, "--json")):
    """List registered tables."""
    resp = api_get("/api/admin/registry")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.text}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(f"Registered tables: {data['count']}")
        for t in data["tables"]:
            # `or` rather than `.get(key, default)`: `bucket` is nullable in
            # table_registry (every non-Keboola row has none) and the key IS
            # present carrying null, so a default never fired and `None` hit a
            # `:20s` format spec — TypeError, listing dead after the header.
            typer.echo(
                f"  {t.get('name') or '?':30s} src={t.get('source_type') or '?':10s} "
                f"mode={t.get('query_mode') or '?':6s} bucket={t.get('bucket') or '':20s}"
            )
            # #754 — surface WHY a table shows 0 rows synced (sync_state's
            # status + persisted skip-reason/error), so "N total, 0 synced"
            # is explained right here instead of requiring a separate
            # `GET /api/admin/registry` inspection.
            sync_status = t.get("last_sync_status")
            if sync_status:
                line = f"    sync={sync_status}"
                reason = t.get("last_sync_error")
                if reason:
                    line += f" ({reason})"
                typer.echo(line)


@admin_app.command("unregister-table")
def unregister_table(
    table_id: str = typer.Argument(..., help="Table id to unregister"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt (for scripts).",
    ),
):
    """Unregister a table from the registry.

    Calls `DELETE /api/admin/registry/{table_id}`. The server unhooks the
    master view, removes the canonical parquet for materialized rows, and
    clears the matching `sync_state` row. Issue #177.
    """
    if not yes:
        typer.echo(f"About to unregister table: {table_id}")
        if not typer.confirm("Continue?"):
            typer.echo("Aborted.")
            raise typer.Exit(0)
    resp = api_delete(f"/api/admin/registry/{table_id}")
    if resp.status_code == 204:
        typer.echo(f"Unregistered: {table_id}")
        return
    if resp.status_code == 404:
        typer.echo(f"Not registered: {table_id}", err=True)
        raise typer.Exit(1)
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"Failed: {detail}", err=True)
    raise typer.Exit(1)


@admin_app.command("update-table")
def update_table(
    table_id: str = typer.Argument(..., help="Table id to update"),
    name: str = typer.Option(None, "--name", help="New display name"),
    bucket: str = typer.Option(None, "--bucket", help="New bucket / dataset"),
    source_table: str = typer.Option(None, "--source-table", help="New source table name"),
    query_mode: str = typer.Option(
        None,
        "--query-mode",
        help="New query mode: local | remote | materialized",
    ),
    query: str = typer.Option(
        None,
        "--query",
        help=(
            "New SQL body for query_mode='materialized' (BigQuery). "
            "Inline SQL or `@path/to.sql` to read from disk. Use "
            "`--query=` (empty value) to clear."
        ),
    ),
    description: str = typer.Option(None, "--description", help="New description"),
    sync_schedule: str = typer.Option(
        None,
        "--sync-schedule",
        help="New cron schedule (e.g. 'every 6h' / 'daily 03:00'); honored by materialized BQ rows",
    ),
    source_type: str = typer.Option(
        None,
        "--source-type",
        help="Change source type. Rare — most edits keep this fixed.",
    ),
    server_only: bool | None = typer.Option(
        None,
        "--server-only/--no-server-only",
        help=(
            "Toggle server-side-only distribution (#607): keep the table "
            "queryable via `agnes query --remote` without `agnes pull` ever "
            "downloading it. A table must be --server-only (or "
            "--query-mode remote) before an access policy can be attached "
            "(design doc §3.1)."
        ),
    ),
    policy: str = typer.Option(
        None,
        "--policy",
        help=(
            "New SQL access-policy body (table access policies design doc "
            "§13.2). Must be `@path/to.sql` — unlike --query, inline SQL is "
            "not accepted (policies are typically multi-line). Use "
            "`--policy=` (empty value) to clear the policy. Requires "
            "--policy-note when setting a non-empty body."
        ),
    ),
    policy_note: str = typer.Option(
        None,
        "--policy-note",
        help="Why this access policy exists. Required whenever --policy sets a non-empty body.",
    ),
    policy_mapping: bool | None = typer.Option(
        None,
        "--policy-mapping/--no-policy-mapping",
        help=(
            "Mark/unmark this table as referenceable from another table's "
            "access-policy body (design doc §15) — e.g. a person-to-cost-centre "
            "mapping table a policy joins against. Marking does NOT itself "
            "grant analysts access to the table."
        ),
    ),
    connection_id: str = typer.Option(
        None,
        "--connection-id",
        help=(
            "Pin this table to a named source connection (source_connections.id). "
            "NULL uses the default connection for the row's source_type."
        ),
    ),
):
    """Update a registered table.

    Calls `PUT /api/admin/registry/{table_id}` with only the supplied
    fields. Field omitted → unchanged. Issue #177.

    For BQ rows, the server schedules a background rebuild so the master
    view picks up the change without waiting for the next scheduled sync.
    Switching `query_mode` away from `materialized` clears the stale
    `source_query` automatically.

    `--policy` / `--policy-note` / `--policy-mapping` attach, replace, or
    clear the table's access policy (design doc §13.2); inspect the result
    with `agnes admin table-policy show|preview`.
    """
    from pathlib import Path

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if bucket is not None:
        payload["bucket"] = bucket
    if source_table is not None:
        payload["source_table"] = source_table
    if query_mode is not None:
        payload["query_mode"] = query_mode
    if description is not None:
        payload["description"] = description
    if sync_schedule is not None:
        payload["sync_schedule"] = sync_schedule
    if source_type is not None:
        payload["source_type"] = source_type
    if server_only is not None:
        payload["server_only"] = server_only
    if query is not None:
        if query.startswith("@"):
            sql_path = Path(query[1:])
            if not sql_path.exists():
                typer.echo(f"Error: SQL file not found: {sql_path}", err=True)
                raise typer.Exit(2)
            payload["source_query"] = sql_path.read_text(encoding="utf-8").strip()
        else:
            payload["source_query"] = query.strip()
    if policy is not None:
        if policy == "":
            payload["access_policy_sql"] = None
        elif policy.startswith("@"):
            policy_path = Path(policy[1:])
            if not policy_path.exists():
                typer.echo(f"Error: policy SQL file not found: {policy_path}", err=True)
                raise typer.Exit(2)
            payload["access_policy_sql"] = policy_path.read_text(encoding="utf-8").strip()
        else:
            typer.echo(
                "Error: --policy requires `@path/to.sql` (inline SQL is not "
                "accepted — access policies are typically multi-line). Use "
                "`--policy=` (empty value) to clear the policy.",
                err=True,
            )
            raise typer.Exit(2)
    if policy_note is not None:
        payload["access_policy_note"] = policy_note
    if policy_mapping is not None:
        payload["policy_mapping"] = policy_mapping
    if connection_id is not None:
        payload["connection_id"] = connection_id

    if not payload:
        typer.echo(
            "No fields supplied. Pass at least one of --name, --bucket, "
            "--source-table, --query-mode, --query, --description, "
            "--sync-schedule, --source-type, --server-only, --policy, "
            "--policy-note, --policy-mapping, --connection-id.",
            err=True,
        )
        raise typer.Exit(2)

    resp = api_put(f"/api/admin/registry/{table_id}", json=payload)
    if resp.status_code == 200:
        data = resp.json()
        updated = data.get("updated") or sorted(payload.keys())
        typer.echo(f"Updated {table_id}: {', '.join(updated)}")
        return
    if resp.status_code == 404:
        typer.echo(f"Not registered: {table_id}", err=True)
        raise typer.Exit(1)
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"Failed: {detail}", err=True)
    # design doc §3.1 — a policy write rejected because the table is
    # neither query_mode='remote' nor server_only. Name the exact next
    # command instead of leaving the operator to re-derive it from the
    # (already actionable, but generic) server message.
    if "access_policy_requires_undistributed" in str(detail):
        typer.echo(
            f"  Next: agnes admin update-table {table_id} --server-only "
            "(or --query-mode remote) to make the table eligible for a policy.",
            err=True,
        )
    raise typer.Exit(1)


@table_policy_app.command("show")
def table_policy_show(
    table_id: str = typer.Argument(..., help="Table id"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show the access policy attached to a table (design doc §4).

    No single-row registry GET exists (only the collection), so this reads
    `GET /api/admin/registry` and picks out the matching row — the same
    approach `list-tables` / `_resolve_user_id` already use.
    """
    resp = api_get("/api/admin/registry")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    row = next((t for t in resp.json().get("tables", []) if t.get("id") == table_id), None)
    if row is None:
        typer.echo(f"Not registered: {table_id}", err=True)
        raise typer.Exit(1)

    sql = row.get("access_policy_sql")
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "id": table_id,
                    "access_policy_sql": sql,
                    "access_policy_note": row.get("access_policy_note"),
                    "access_policy_updated_at": row.get("access_policy_updated_at"),
                    "access_policy_updated_by": row.get("access_policy_updated_by"),
                    "policy_mapping": bool(row.get("policy_mapping")),
                },
                indent=2,
            )
        )
        return

    if not sql:
        typer.echo(f"{table_id}: no access policy attached.")
        if row.get("policy_mapping"):
            typer.echo("  (marked policy_mapping=true — referenceable from other tables' policies)")
        return

    typer.echo(f"Access policy for {table_id}:")
    typer.echo(f"  note:           {row.get('access_policy_note') or ''}")
    typer.echo(f"  updated_by:     {row.get('access_policy_updated_by') or ''}")
    typer.echo(f"  updated_at:     {row.get('access_policy_updated_at') or ''}")
    typer.echo(f"  policy_mapping: {bool(row.get('policy_mapping'))}")
    typer.echo("  sql:")
    for line in sql.splitlines():
        typer.echo(f"    {line}")


@table_policy_app.command("preview")
def table_policy_preview(
    table_id: str = typer.Argument(..., help="Table id"),
    sql: str = typer.Option(
        None,
        "--sql",
        help=(
            "Preview a CANDIDATE policy body instead of the stored one — "
            "`@path/to.sql` (same rule as `update-table --policy`; never "
            "saved by this command). Omit to preview the stored policy."
        ),
    ),
    as_user: str = typer.Option(
        None, "--as", help="Preview as this real user (id or email), using their LIVE group membership."
    ),
    as_groups: str = typer.Option(
        None,
        "--as-groups",
        help="Preview as an ad-hoc, comma-separated group set — no real user needs to exist.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Run a stored or candidate access policy as a chosen persona and show
    what it does (design doc §13.1) — calls
    `POST /api/admin/registry/{id}/policy/preview`, the single-persona
    primitive the admin UI's persona matrix is built from. Exactly one of
    --as / --as-groups selects the persona. Every call is audited
    server-side.
    """
    from pathlib import Path

    if (as_user is None) == (as_groups is None):
        typer.echo(
            "Error: choose exactly one of --as <user> or --as-groups a,b to select the preview persona.",
            err=True,
        )
        raise typer.Exit(2)

    payload: dict = {}
    if sql is not None:
        if not sql.startswith("@"):
            typer.echo(
                "Error: --sql requires `@path/to.sql` (inline SQL is not accepted — see `update-table --policy`).",
                err=True,
            )
            raise typer.Exit(2)
        sql_path = Path(sql[1:])
        if not sql_path.exists():
            typer.echo(f"Error: SQL file not found: {sql_path}", err=True)
            raise typer.Exit(2)
        payload["sql"] = sql_path.read_text(encoding="utf-8").strip()
    if as_user is not None:
        payload["as_user"] = as_user
    if as_groups is not None:
        payload["as_groups"] = [g.strip() for g in as_groups.split(",") if g.strip()]

    resp = api_post(f"/api/admin/registry/{table_id}/policy/preview", json=payload)
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Failed: {detail}", err=True)
        raise typer.Exit(1)

    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return

    rows_visible = body.get("rows_visible", 0)
    rows_total = body.get("rows_total", 0)
    typer.echo(f"Preview for {table_id}: {rows_visible} of {rows_total} row(s) visible")

    columns = body.get("columns") or []
    if columns:
        typer.echo("  columns:")
        for col in columns:
            marker = " (hidden)" if col.get("hidden") else ""
            typer.echo(f"    {col['name']}{marker}")

    sample = body.get("sample_rows") or []
    if sample:
        typer.echo(f"  sample ({len(sample)} row(s)):")
        for sample_row in sample:
            typer.echo("    " + ", ".join(f"{k}={v}" for k, v in sample_row.items()))

    # design doc §13.2's closing sentence + plan Task 16 deliverable 4: a
    # bare "0" reads identically whether this persona legitimately has no
    # rows or an upstream policy_mapping table (§15) is empty/stale — don't
    # print an unqualified 0. An unresolvable persona (co-drive-style)
    # never reaches this line: it fails the command above, non-zero exit,
    # before any row count is printed.
    if rows_visible == 0 and rows_total > 0:
        typer.echo(
            "  Note: 0 rows visible to this persona — this can be a "
            "legitimate empty slice (the persona genuinely has no matching "
            "rows), or — if the policy joins a mapping table "
            "(policy_mapping=true) — an empty/stale mapping. Run "
            f"`agnes admin table-policy show {table_id}` for the policy body "
            "and `agnes admin list-tables` for the mapping table's sync "
            "status. (An unresolvable persona would have failed this "
            "command outright, above, rather than showing 0 rows.)"
        )


@admin_app.command("metadata-show")
def metadata_show(
    table_id: str = typer.Argument(..., help="Table ID to show metadata for"),
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show column metadata for a table."""
    resp = api_get(f"/api/admin/metadata/{table_id}")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        columns = data.get("columns", [])
        if not columns:
            typer.echo(f"No column metadata for table: {table_id}")
            return
        typer.echo(f"Column metadata for table: {table_id} ({len(columns)} columns)")
        typer.echo(f"  {'COLUMN':<30s} {'BASETYPE':<12s} {'CONFIDENCE':<12s} DESCRIPTION")
        typer.echo("  " + "-" * 80)
        for col in columns:
            typer.echo(
                f"  {col['column_name']:<30s} {col.get('basetype') or '':^12s} "
                f"{col.get('confidence') or '':^12s} {col.get('description') or ''}"
            )


@admin_app.command("metadata-apply")
def metadata_apply(
    proposal_path: str = typer.Argument(..., help="Path to proposal JSON file"),
    push_to_source: bool = typer.Option(False, "--push-to-source", help="Push metadata to Keboola after import"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without applying"),
):
    """Apply a metadata proposal JSON to DuckDB."""
    import os

    if not os.path.exists(proposal_path):
        typer.echo(f"Proposal file not found: {proposal_path}", err=True)
        raise typer.Exit(1)

    with open(proposal_path, "r", encoding="utf-8") as f:
        proposal = json.load(f)

    tables = proposal.get("tables", {})
    total = sum(len(t.get("columns", {})) for t in tables.values())

    if dry_run:
        typer.echo(f"[DRY RUN] Would import {total} column(s) from {len(tables)} table(s):")
        for table_id, table_data in tables.items():
            columns = table_data.get("columns", {})
            for col_name, col_data in columns.items():
                typer.echo(
                    f"  {table_id}.{col_name}: basetype={col_data.get('basetype')} "
                    f"description={col_data.get('description')}"
                )
        return

    from src.db import get_system_db
    from src.repositories import use_pg

    # Repo work routes through the factory (PG or DuckDB); the system DuckDB
    # must never be opened on a Postgres instance (get_system_db raises there).
    conn = None if use_pg() else get_system_db()
    try:
        repo = column_metadata_repo()
        count = repo.import_proposal(proposal_path)
        typer.echo(f"Imported {count} column(s) from proposal.")
    finally:
        if conn is not None:
            conn.close()

    if push_to_source:
        for table_id in tables:
            resp = api_post(f"/api/admin/metadata/{table_id}/push")
            if resp.status_code == 200:
                typer.echo(f"Pushed metadata for {table_id} to source.")
            else:
                typer.echo(f"Failed to push {table_id}: {resp.json().get('detail', resp.text)}", err=True)


# ---- User management (#11) ----


def _resolve_user_id(ref: str) -> str:
    """Accept either a UUID or an email; look up email → id via list."""
    if "@" not in ref:
        return ref
    resp = api_get("/api/users")
    if resp.status_code != 200:
        typer.echo(f"Could not list users: {resp.text}", err=True)
        raise typer.Exit(1)
    for u in resp.json():
        if u.get("email") == ref:
            return u["id"]
    typer.echo(f"User not found: {ref}", err=True)
    raise typer.Exit(1)


def _print_user_result(resp, ok_msg: str) -> None:
    if resp.status_code in (200, 204):
        typer.echo(ok_msg)
    else:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        typer.echo(f"Failed: {detail}", err=True)
        raise typer.Exit(1)


@admin_app.command("set-role")
def set_role(
    user_ref: str = typer.Argument(..., help="User id or email"),
    role: str = typer.Argument(..., help="(removed — see message)"),
):
    """[REMOVED] Roles were replaced by group memberships in v0.25."""
    typer.echo(
        "Error: 'agnes admin set-role' was removed in v0.25.\n"
        "  Roles were replaced by group memberships.\n"
        f"  Make {user_ref!r} admin:\n"
        "    agnes admin group list                        # find Admin group id\n"
        f"    agnes admin group add-member <admin-id> {user_ref}\n",
        err=True,
    )
    raise typer.Exit(2)


@admin_app.command("deactivate")
def deactivate(user_ref: str = typer.Argument(..., help="User id or email")):
    """Deactivate a user (blocks login, existing tokens also rejected)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/deactivate")
    _print_user_result(resp, f"Deactivated {user_ref}")


@admin_app.command("activate")
def activate(user_ref: str = typer.Argument(..., help="User id or email")):
    """Re-activate a deactivated user."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/activate")
    _print_user_result(resp, f"Activated {user_ref}")


@admin_app.command("reset-password")
def reset_password(user_ref: str = typer.Argument(..., help="User id or email")):
    """Generate a reset token (emailed if SMTP/SendGrid configured)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/reset-password")
    if resp.status_code == 200:
        data = resp.json()
        typer.echo(f"Reset URL: {data['reset_url']}")
        typer.echo(f"Email sent: {data['email_sent']}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


@admin_app.command("set-password")
def set_password(
    user_ref: str = typer.Argument(..., help="User id or email"),
    password: str = typer.Option(
        ...,
        prompt=True,
        hide_input=True,
        confirmation_prompt=True,
        help="New password (hidden input)",
    ),
):
    """Set a user's password directly (force-reset flow)."""
    uid = _resolve_user_id(user_ref)
    resp = api_post(f"/api/users/{uid}/set-password", json={"password": password})
    if resp.status_code == 204:
        typer.echo(f"Password set for {user_ref}")
    else:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)


# ---- Access management (v12 — user_groups + members + resource_grants) ----
#
# Calls the unified access REST API under /api/admin (see app/api/access.py).
# Every endpoint requires Admin user_group membership.

group_app = typer.Typer(help="User group + membership management")
grant_app = typer.Typer(help="Resource grant CRUD")
admin_app.add_typer(group_app, name="group")
admin_app.add_typer(grant_app, name="grant")


def _fail(resp, prefix: str = "Failed") -> None:
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    typer.echo(f"{prefix}: {detail}", err=True)
    raise typer.Exit(1)


def _print_rows(rows: list, columns: list[tuple[str, str, int]]) -> None:
    header = "  " + "  ".join(f"{h:<{w}s}" for _, h, w in columns)
    typer.echo(header)
    typer.echo("  " + "-" * (len(header) - 2))
    for row in rows:
        cells = []
        for key, _, width in columns:
            val = row.get(key)
            cells.append(f"{(str(val) if val is not None else ''):<{width}s}")
        typer.echo("  " + "  ".join(cells))


def _resolve_group_id(ref: str) -> str:
    """Accept group id (UUID-ish) or name; look up via /api/admin/groups."""
    resp = api_get("/api/admin/groups")
    if resp.status_code != 200:
        _fail(resp, prefix="Could not list groups")
    for g in resp.json():
        if g["id"] == ref or g["name"] == ref:
            return g["id"]
    typer.echo(f"Group not found: {ref}", err=True)
    raise typer.Exit(1)


def _resolve_grant_id(ref: str) -> str:
    """Accept full grant UUID or 8-char prefix (as printed by ``grant list``).

    Grants have no human-readable name — the only identifier is the UUID
    that gets generated at create time. The default tabular output of
    ``agnes admin grant list`` shows the first 8 chars under the ``short_id``
    column so an operator can eyeball-copy it into ``grant delete``; this
    helper bridges that workflow by listing all grants and matching the ref
    against either the full id or the 8-char prefix. Ambiguous prefix
    matches abort with a clear error rather than picking one silently.
    """
    resp = api_get("/api/admin/grants")
    if resp.status_code != 200:
        _fail(resp, prefix="Could not list grants")
    matches = [g for g in resp.json() if g.get("id") == ref or (g.get("id") or "").startswith(ref)]
    if not matches:
        typer.echo(f"Grant not found: {ref}", err=True)
        raise typer.Exit(1)
    if len(matches) > 1:
        typer.echo(
            f"Ambiguous grant prefix {ref!r} matches {len(matches)} grants: " + ", ".join(m["id"][:8] for m in matches),
            err=True,
        )
        raise typer.Exit(1)
    return matches[0]["id"]


@group_app.command("list")
def group_list(as_json: bool = typer.Option(False, "--json")):
    """List all user groups."""
    resp = api_get("/api/admin/groups")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"User groups: {len(rows)}")
    _print_rows(
        rows,
        [
            ("name", "NAME", 24),
            ("description", "DESCRIPTION", 40),
            ("is_system", "SYSTEM", 7),
            ("member_count", "MEMBERS", 8),
            ("grant_count", "GRANTS", 7),
        ],
    )


@group_app.command("create")
def group_create(
    name: str = typer.Argument(..., help="Group name"),
    description: str = typer.Option("", help="Description"),
):
    """Create a new user group."""
    resp = api_post("/api/admin/groups", json={"name": name, "description": description or None})
    if resp.status_code != 201:
        _fail(resp)
    typer.echo(f"Created group: {name} (id={resp.json()['id']})")


@group_app.command("delete")
def group_delete(group_ref: str = typer.Argument(..., help="Group id or name")):
    """Delete a user group (and its members + grants)."""
    gid = _resolve_group_id(group_ref)
    resp = api_delete(f"/api/admin/groups/{gid}")
    if resp.status_code in (200, 204):
        typer.echo(f"Deleted group {group_ref}")
        return
    _fail(resp)


@group_app.command("members")
def group_members(group_ref: str = typer.Argument(..., help="Group id or name")):
    """List members of a group."""
    gid = _resolve_group_id(group_ref)
    resp = api_get(f"/api/admin/groups/{gid}/members")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    typer.echo(f"Members: {len(rows)}")
    _print_rows(
        rows,
        [
            ("email", "EMAIL", 30),
            ("name", "NAME", 20),
            ("source", "SOURCE", 14),
            ("active", "ACTIVE", 7),
        ],
    )


@group_app.command("add-member")
def group_add_member(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    email: str = typer.Argument(..., help="User email"),
):
    """Add a user to a group (source='admin' — survives Google sync)."""
    gid = _resolve_group_id(group_ref)
    resp = api_post(f"/api/admin/groups/{gid}/members", json={"email": email})
    if resp.status_code != 201:
        _fail(resp)
    typer.echo(f"Added {email} to {group_ref}")


@group_app.command("remove-member")
def group_remove_member(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    email: str = typer.Argument(..., help="User email"),
):
    """Remove a user from a group (only admin-source rows can be removed this way)."""
    gid = _resolve_group_id(group_ref)
    user_id = _resolve_user_id(email)
    resp = api_delete(f"/api/admin/groups/{gid}/members/{user_id}")
    if resp.status_code in (200, 204):
        typer.echo(f"Removed {email} from {group_ref}")
        return
    _fail(resp)


@grant_app.command("list")
def grant_list(
    resource_type: str = typer.Option("", "--type", help="Filter by resource type"),
    group_ref: str = typer.Option("", "--group", help="Filter by group id or name"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List resource grants."""
    params = {}
    if resource_type:
        params["resource_type"] = resource_type
    if group_ref:
        params["group_id"] = _resolve_group_id(group_ref)
    resp = api_get("/api/admin/grants", params=params)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Resource grants: {len(rows)}")
    # Surface a short id so the default tabular output is usable as
    # input to `agnes admin grant delete <id>` without first re-running
    # with --json. First 8 chars of the UUID are unambiguous in practice
    # (grant ids are random UUIDs; collisions on the 8-char prefix
    # within a single instance's resource_grants table are astronomically
    # unlikely). The matching bridge lives in `_resolve_grant_id` so
    # `grant delete` accepts either the full UUID or the 8-char short_id
    # printed here — and aborts loudly on the rare ambiguous prefix.
    for r in rows:
        r["short_id"] = (r.get("id") or "")[:8]
    _print_rows(
        rows,
        [
            ("short_id", "ID", 9),
            ("group_name", "GROUP", 20),
            ("resource_type", "RESOURCE TYPE", 22),
            ("resource_id", "RESOURCE ID", 40),
            ("requirement", "REQUIREMENT", 12),
            ("assigned_by", "ASSIGNED BY", 24),
        ],
    )


@grant_app.command("create")
def grant_create(
    group_ref: str = typer.Argument(..., help="Group id or name"),
    resource_type: str = typer.Argument(..., help="Resource type (e.g. marketplace_plugin)"),
    resource_id: str = typer.Argument(..., help="Resource path (e.g. foundry-ai/metrics-plugin)"),
    requirement: str = typer.Option(
        "available",
        "--requirement",
        help="'available' (user opts in via stack) or 'required' (auto-in-stack for all group members)",
    ),
):
    """Grant a group access to a specific resource.

    Arguments are positional, not flags — adjust shell completions /
    scripts accordingly:

    \b
        agnes admin grant create <group> <resource_type> <resource_id>

    Example:

    \b
        agnes admin grant create analysts table order_economics
        agnes admin grant create analysts marketplace_plugin foundry-ai/metrics
        agnes admin grant create critical-ops data_package weekly-revenue --requirement required

    v49: the optional ``--requirement`` flag controls whether the grant
    is opt-in (``available``, default) or always-in-stack (``required``).
    When passed on a NEW (group, resource_type, resource_id) tuple the
    server creates an ``available`` grant and the CLI then PUTs the
    requirement update — this two-step is needed because POST doesn't
    accept the field directly. When the tuple already exists, the 409
    is followed by a list+match to find the existing grant id and a
    PUT to flip the requirement (idempotent if it's already at the
    desired level).
    """
    if requirement not in ("available", "required"):
        typer.echo(
            f"--requirement must be 'available' or 'required', got {requirement!r}",
            err=True,
        )
        raise typer.Exit(2)
    gid = _resolve_group_id(group_ref)
    resp = api_post(
        "/api/admin/grants",
        json={
            "group_id": gid,
            "resource_type": resource_type,
            "resource_id": resource_id,
        },
    )
    if resp.status_code == 409:
        # Existing grant — find its id so we can PUT a requirement update.
        # Re-list with both filters to scope the lookup tightly.
        ls = api_get(
            "/api/admin/grants",
            params={"group_id": gid, "resource_type": resource_type},
        )
        if ls.status_code != 200:
            _fail(ls)
        existing = next(
            (r for r in ls.json() if r.get("resource_id") == resource_id),
            None,
        )
        if not existing:
            typer.echo(
                "Server reported grant exists but list lookup couldn't find it.",
                err=True,
            )
            raise typer.Exit(1)
        grant_id = existing["id"]
        current = existing.get("requirement") or "available"
        if current == requirement:
            typer.echo(
                f"Grant {group_ref}: {resource_type}/{resource_id} already exists with requirement={requirement}"
            )
            return
        upd = api_put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": requirement},
        )
        if upd.status_code != 200:
            _fail(upd)
        typer.echo(f"Updated existing grant {group_ref}: {resource_type}/{resource_id} requirement={requirement}")
        return
    if resp.status_code != 201:
        _fail(resp)
    new_grant = resp.json()
    grant_id = new_grant["id"]
    # If the caller wanted 'required', flip with a PUT — server POST
    # always creates 'available'.
    if requirement == "required":
        upd = api_put(
            f"/api/admin/grants/{grant_id}",
            json={"requirement": "required"},
        )
        if upd.status_code != 200:
            _fail(upd)
        typer.echo(f"Granted {group_ref}: {resource_type}/{resource_id} requirement=required")
        return
    typer.echo(f"Granted {group_ref}: {resource_type}/{resource_id}")


@grant_app.command("delete")
def grant_delete(
    grant_ref: str = typer.Argument(..., help="Grant id (full UUID or 8-char short_id from `grant list`)"),
):
    """Delete a grant by id.

    Accepts either the full UUID or the 8-char short_id printed by
    ``agnes admin grant list``. See :func:`_resolve_grant_id` for the
    matching rules (exact match preferred; otherwise unique prefix match).
    """
    grant_id = _resolve_grant_id(grant_ref)
    resp = api_delete(f"/api/admin/grants/{grant_id}")
    if resp.status_code in (200, 204):
        typer.echo(f"Deleted grant {grant_id}")
        return
    _fail(resp)


@grant_app.command("resource-types")
def grant_resource_types(as_json: bool = typer.Option(False, "--json")):
    """List the resource types modules have registered."""
    resp = api_get("/api/admin/resource-types")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json()
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    _print_rows(
        rows,
        [
            ("key", "KEY", 28),
            ("display_name", "DISPLAY NAME", 28),
            ("id_format", "ID FORMAT", 36),
        ],
    )


# ---------------------------------------------------------------------------
# Config-surface introspection
# ---------------------------------------------------------------------------


@admin_app.command("config-surface")
def config_surface(
    as_json: bool = typer.Option(False, "--json", help="Output as JSON"),
):
    """Show the complete per-instance configuration surface.

    Prints every instance.yaml/env-var knob with its current value, which
    tier supplied it (env/yaml/default), the registered Initial Workspace
    Template (if any), and every registered marketplace. Corresponds to
    GET /api/admin/config-surface.
    """
    resp = api_get("/api/admin/config-surface")
    if resp.status_code != 200:
        typer.echo(f"Failed: {resp.json().get('detail', resp.text)}", err=True)
        raise typer.Exit(1)

    data = resp.json()

    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return

    # Knobs table
    knobs = data.get("knobs", [])
    typer.echo(f"Config knobs ({len(knobs)}):")
    typer.echo(f"  {'RESOLVER':<38s} {'SOURCE':<8s} {'CURRENT VALUE'}")
    typer.echo("  " + "-" * 76)
    for k in knobs:
        val = k.get("current_value")
        val_str = str(val) if val is not None else ""
        if len(val_str) > 40:
            val_str = val_str[:37] + "..."
        typer.echo(f"  {k['resolver']:<38s} {k['source']:<8s} {val_str}")

    # Initial workspace
    iw = data.get("initial_workspace")
    typer.echo("")
    if iw:
        typer.echo(f"Initial Workspace Template: {iw.get('url')}")
        typer.echo(f"  branch:       {iw.get('branch') or '(default)'}")
        typer.echo(f"  last_sync:    {iw.get('last_sync_sha') or '(never synced)'}")
    else:
        typer.echo("Initial Workspace Template: (not registered)")

    # Marketplaces
    mps = data.get("marketplaces", [])
    typer.echo("")
    typer.echo(f"Marketplaces ({len(mps)}):")
    for m in mps:
        typer.echo(f"  {m['name']}: {m['url']}")
    if not mps:
        typer.echo("  (none registered)")

    # Infra repo
    infra = data.get("infra_repo_url", "")
    typer.echo("")
    typer.echo(f"Infra repo URL: {infra or '(not set)'}")


# ---------------------------------------------------------------------------
# Break-glass: out-of-band admin grant.
#
# Talks directly to system.duckdb — no HTTP, no auth dependency. The whole
# point is recovery for the case where the running server's authorization
# layer is broken or there is no admin left to authenticate as. Requires
# filesystem access to ${DATA_DIR}/state/system.duckdb and is therefore
# restricted to operators with shell access on the host.
# ---------------------------------------------------------------------------


breakglass_app = typer.Typer(
    help="Out-of-band recovery (talks directly to system.duckdb)",
)
admin_app.add_typer(breakglass_app, name="break-glass")


@breakglass_app.command("grant-admin")
def break_glass_grant_admin(
    email: str = typer.Argument(..., help="Email of the user to promote"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
) -> None:
    """Grant Admin-group membership to a user without going through the API.

    Operates directly on system.duckdb. Use when the server is up but the
    Admin group has no live members (race, mistake, accidental DELETE) or
    when bootstrapping a brand-new install before any admin exists. Membership
    is recorded with source='cli_break_glass' so it's distinguishable from
    google_sync / admin / system_seed in audits.

    The DuckDB file must not be locked by a running app process — stop the
    app or use a separate replica before running this.
    """
    import uuid as _uuid

    from src.db import SYSTEM_ADMIN_GROUP, get_system_db
    from src.repositories import use_pg
    from src.user_identity import normalize_email

    if not yes:
        confirm = typer.confirm(
            f"Grant Admin-group membership to {email!r} (break-glass)?",
            default=False,
        )
        if not confirm:
            typer.echo("Aborted.")
            raise typer.Exit(1)

    # Repo work routes through the factory (PG or DuckDB); the system DuckDB
    # must never be opened on a Postgres instance (get_system_db raises there).
    conn = None if use_pg() else get_system_db()
    try:
        users = users_repo()
        groups = user_groups_repo()
        members = user_group_members_repo()

        admin_group = groups.get_by_name(SYSTEM_ADMIN_GROUP)
        if admin_group is None:
            typer.echo(
                f"FATAL: '{SYSTEM_ADMIN_GROUP}' group missing. Start the app "
                "once so _seed_system_groups can recreate it, then retry.",
                err=True,
            )
            raise typer.Exit(2)

        # Normalize BEFORE the read: get_by_email_ci folds case in SQL but does
        # NOT trim, so a padded argument would miss the existing row and then
        # fail the UNIQUE(email) constraint on insert — the last-resort admin
        # recovery dying on a stray space.
        normalized = normalize_email(email)
        existing = users.get_by_email_ci(normalized)
        if existing is None:
            user_id = _uuid.uuid4().hex
            users.create(
                id=user_id,
                email=normalized,
                name=normalized.split("@", 1)[0],
            )
            typer.echo(f"Created user {email} (id={user_id[:8]}…)")
        else:
            user_id = existing["id"]

        if members.has_membership(user_id, admin_group["id"]):
            typer.echo(f"{email} is already a member of '{SYSTEM_ADMIN_GROUP}'.")
            return

        members.add_member(
            user_id=user_id,
            group_id=admin_group["id"],
            source="cli_break_glass",
            added_by="cli:break-glass",
        )
        typer.echo(f"Granted Admin to {email}. Audit source='cli_break_glass'.")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


@admin_app.command("duplicate-accounts")
def duplicate_accounts(
    limit: int = typer.Option(0, "--limit", help="Show at most N colliding addresses (0 = all)"),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as JSON"),
) -> None:
    """Report accounts whose emails differ only in case.

    `users` is UNIQUE on `email`, so `Ann@corp.com` and `ann@corp.com` are two
    perfectly legal rows that no constraint objects to and no address-sorted
    list view puts side by side. Sign-in resolves such a collision through
    `get_by_email_ci`, which picks the OLDEST row — deterministic, but it means
    a person can own an account nothing will ever sign them in to, and an
    operator who deactivates the row they happened to find may not have
    disabled the identity at all. This names every collision and marks which
    row sign-in actually reaches.

    Two classes of collision are reported, because both are the same address
    to a person: rows differing only in case, and rows differing only by
    surrounding whitespace. The second is worse — `get_by_email_ci` folds case
    but does NOT trim the column, so a stored ` ann@corp.com` is reached by no
    address anyone can type, and such a row is marked unreachable rather than
    ever named as the resolved one. A LONE padded row is equally unreachable but
    is not a duplicate, so it is out of this report's scope.

    Read-only. Which row to keep is a judgement call — group memberships,
    PATs, sessions and audit history hang off the id — so this reports and the
    operator merges. `--json` carries an explicit column projection, never the
    whole row: `users` also holds `password_hash` and live one-time-link
    tokens, and this output is meant to be pasted into a ticket.

    Reads the active state backend directly through the repository factory
    rather than an API endpoint, like `break-glass`: it is an operator
    diagnostic for whoever is about to run the reconciliation, and that person
    already has database access.
    """
    from src.db import get_system_db
    from src.repositories import use_pg

    conn = None if use_pg() else get_system_db()
    try:
        groups = users_repo().list_case_variant_duplicates()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    shown = groups[:limit] if limit > 0 else groups

    if as_json:
        typer.echo(
            json.dumps(
                {"total_addresses": len(groups), "shown": len(shown), "duplicates": shown},
                indent=2,
                default=str,
            )
        )
        return

    if not groups:
        typer.echo("No case-variant duplicate accounts found.")
        return

    typer.echo(f"{len(groups)} address(es) held by more than one account:\n")
    for g in shown:
        typer.echo(f"  {g['email']}  ({g['count']} accounts)")
        if g["resolved_id"] is None:
            typer.echo("      !! no row here is reachable by sign-in — every variant is padded")
        for u in g["users"]:
            if u["id"] == g["resolved_id"]:
                resolves = "← sign-in resolves here"
            elif u.get("unreachable_by_sign_in"):
                # Stored with surrounding whitespace. The doors normalize their
                # INPUT but the lookup does not trim the column, so no address
                # anyone can type reaches this row at all.
                resolves = "← unreachable: address stored with whitespace"
            else:
                resolves = ""
            state = "active" if u.get("active", True) else "DEACTIVATED"
            # Full id, not the 8-char prefix `list-users` shows: this report
            # exists to be acted on, and the reconciliation below addresses
            # rows BY ID precisely because the address is ambiguous here.
            typer.echo(f"      {u['email']:34s} {state:12s} {u['id']}  {resolves}")
        typer.echo("")

    if limit > 0 and len(groups) > len(shown):
        typer.echo(f"… {len(groups) - len(shown)} more (raise --limit or use --json for all).\n")

    typer.echo(
        "To reconcile: KEEP the row marked 'sign-in resolves here', re-grant its\n"
        "groups with `agnes admin group add-member`, then deactivate the other\n"
        "with `agnes admin deactivate <id>` — by id, because `deactivate <email>`\n"
        "matches one exact spelling, which is the ambiguity this report is about.\n"
        "Deactivating the row sign-in does NOT resolve to leaves the identity\n"
        "reachable.\n"
        "\n"
        "Keeping the OTHER row is not supported by these commands: `group\n"
        "add-member` takes an address and the server resolves it the same way\n"
        "sign-in does, so the grant lands on the marked row no matter which one\n"
        "you meant. Deactivating the marked row first does not redirect it — the\n"
        "lookup ignores active state deliberately, so a disabled row still wins.\n"
        "If you must keep the other row, move the data at the database level."
    )
