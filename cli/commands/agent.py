"""`agnes agent` — analyst-side CLI over the agent-profile API (Task 11,
`docs/superpowers/plans/2026-07-22-agent-api-v1a.md`).

Thin wrapper around the management surface (`app/api/agents_admin.py`,
`/api/v1/agents` CRUD + scope + PAT issuance — session-token only, every
route rejects a PAT) and the runtime surface (`app/api/agent_runtime.py`,
`POST /api/v1/agents/{slug}/responses` + `GET /api/v1/jobs/{id}` — callable
with either a session token or an agent PAT scoped to that agent).

Each subcommand maps 1:1 to one HTTP endpoint:

  - ``list``      -> ``GET  /api/v1/agents``
  - ``create``     -> ``POST /api/v1/agents``
  - ``show``       -> ``GET  /api/v1/agents`` (list + filter by slug — the
                       management API only supports lookup by id, and an
                       extra id-lookup round trip buys nothing the list
                       response doesn't already carry)
  - ``scope set``  -> ``PUT  /api/v1/agents/{id}/scope``
  - ``token``      -> ``POST /api/v1/agents/{id}/tokens``
  - ``delete``     -> ``DELETE /api/v1/agents/{id}``
  - ``ask``        -> ``POST /api/v1/agents/{slug}/responses`` (runtime
                       surface takes the slug directly, not the id), then
                       polls ``GET /api/v1/jobs/{id}`` on a `202` until the
                       job reaches a terminal status or ``--timeout`` runs out.

Every management subcommand except ``ask`` needs an interactive session
token — the server 403s a PAT outright (`require_session_token`). ``ask``
itself works with either credential; the CLI doesn't distinguish, it just
sends whatever `agnes auth` currently holds and renders a 401/403 the same
way as any other error.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import typer

from cli.client import api_delete, api_get, api_patch, api_post, api_put
from cli.error_render import render_error

agent_app = typer.Typer(help="Manage agent profiles, scope, tokens, and one-shot asks")
scope_app = typer.Typer(help="Manage an agent's resource scope grants")
agent_app.add_typer(scope_app, name="scope")
webhooks_app = typer.Typer(help="Manage an agent's outbound job-completion webhooks")
agent_app.add_typer(webhooks_app, name="webhooks")
memory_app = typer.Typer(help="Manage an agent's private memory notebook (owner-facing)")
agent_app.add_typer(memory_app, name="memory")
schedule_app = typer.Typer(help="Manage an agent's scheduled runs")
agent_app.add_typer(schedule_app, name="schedule")

# Valid `?status=` filter values for `GET /api/v1/agents/{id}/memories` —
# mirrors `app/api/agents_admin.py`'s memory-row `status` column (the server
# doesn't validate the value, it just filters; kept here for `--help` text
# only).
_MEMORY_STATUS_VALUES = ("pending", "active", "archived")

# Mirrors `app/api/agent_webhooks.py`'s `_DEFAULT_EVENTS` — the CLI's own
# help text only (the server applies this default when `--event` is
# omitted; not re-implemented here).
_DEFAULT_WEBHOOK_EVENTS = ("job.completed", "job.failed")

# Server default (`CreateAgentTokenRequest.expires_in_days = 90`) mirrored
# here so the CLI's own default matches what omitting the flag would give
# you on the API directly.
_DEFAULT_TOKEN_EXPIRES_DAYS = 90

# Runtime `/responses` defaults (mirrors `app/api/agent_runtime.py`'s
# `_DEFAULT_TIMEOUT_S` / `_MAX_TIMEOUT_S`) — kept in sync manually since the
# CLI has no import-time dependency on the FastAPI router module.
_DEFAULT_ASK_TIMEOUT_S = 120
_MAX_ASK_TIMEOUT_S = 600
# Extra headroom on the HTTP client timeout over the server's own bounded
# wait, so the CLI's socket doesn't time out a hair before the server
# would have replied with its own 200/202.
_HTTP_TIMEOUT_MARGIN_S = 10.0
# Poll cadence for the background-job path. Deliberately short — job
# results for `agent_response` are typically ready within a few seconds
# once degraded to background.
_POLL_INTERVAL_S = 2.0

_TERMINAL_JOB_STATUSES = ("completed", "failed")


def _fail(resp) -> None:
    """Render an HTTP error response and exit non-zero.

    Uses the shared ``cli.error_render`` formatter so ``{"detail": {"code":
    ..., "message": ...}}`` bodies (every `_err()` raise in
    `agents_admin.py` / `agent_runtime.py`) render as a readable
    ``Error: <code> (HTTP <status>)`` block instead of a flattened dict dump.
    """
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    typer.echo(render_error(resp.status_code, body), err=True)
    raise typer.Exit(1)


def _resolve_agent(slug: str) -> dict:
    """Find an agent by slug via the list endpoint.

    The management API has no get-by-slug route (only get-by-id), so every
    slug-addressed subcommand pays one list round trip. Exits with a
    next-step hint on a miss, matching the command-UX standard's "not
    found must point forward" rule.
    """
    resp = api_get("/api/v1/agents")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    for row in rows:
        if row.get("slug") == slug:
            return row
    typer.echo(f"Agent not found: {slug}. List your agents with: agnes agent list", err=True)
    raise typer.Exit(1)


def _print_agent(row: dict) -> None:
    typer.echo(f"id:                {row.get('id')}")
    typer.echo(f"slug:              {row.get('slug')}")
    typer.echo(f"name:              {row.get('name')}")
    if row.get("description"):
        typer.echo(f"description:       {row['description']}")
    typer.echo(f"model:             {row.get('model') or 'server default (no model policy)'}")
    typer.echo(
        f"token_budget:      {row.get('token_budget_monthly') if row.get('token_budget_monthly') is not None else '(unbounded)'}"
    )
    typer.echo(f"plugins_mode:      {row.get('plugins_mode')}")
    typer.echo(f"connections_mode:  {row.get('connections_mode')}")
    typer.echo(f"tables_mode:       {row.get('tables_mode')}")
    typer.echo(f"memory_mode:       {row.get('memory_mode')}")
    typer.echo(f"memory_write_mode: {row.get('memory_write_mode')}")
    typer.echo(f"is_default:        {row.get('is_default', False)}")
    typer.echo(f"created_at:        {row.get('created_at')}")


@agent_app.command("list")
def list_agents(as_json: bool = typer.Option(False, "--json")):
    """List your agent profiles."""
    resp = api_get("/api/v1/agents")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Agents: {len(rows)}")
    if not rows:
        typer.echo("No agents yet. Create one with: agnes agent create <name> --slug <slug>")
        return
    slug_w = max(len("SLUG"), max((len(r.get("slug", "")) for r in rows), default=4))
    name_w = max(len("NAME"), max((len(r.get("name", "")) for r in rows), default=4))
    typer.echo(f"{'ID':<36}  {'SLUG':<{slug_w}}  {'NAME':<{name_w}}  MODEL")
    for r in rows:
        typer.echo(
            f"{r.get('id', ''):<36}  {r.get('slug', ''):<{slug_w}}  {r.get('name', ''):<{name_w}}  "
            f"{r.get('model') or 'server default (no model policy)'}"
        )


@agent_app.command("create")
def create_agent(
    name: str = typer.Argument(..., help="Display name"),
    slug: str = typer.Option(..., "--slug", help="Lowercase kebab-case unique id (immutable after creation)"),
    prompt_file: Optional[str] = typer.Option(
        None, "--prompt-file", help="Path to a file containing the system prompt"
    ),
    model: Optional[str] = typer.Option(
        None, "--model", help="Model override (omit for server default — no model policy)"
    ),
    budget: Optional[int] = typer.Option(None, "--budget", help="Monthly token budget"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Create a new agent profile.

    New agents default all four scope modes (plugins/connections/tables/
    memory) to `selected` server-side — use `agnes agent scope set` to grant
    specific resources.
    """
    payload: dict = {"name": name, "slug": slug}
    if prompt_file:
        path = Path(prompt_file)
        if not path.exists():
            typer.echo(f"Error: prompt file not found: {path}", err=True)
            raise typer.Exit(2)
        payload["system_prompt"] = path.read_text(encoding="utf-8").strip()
    if model is not None:
        payload["model"] = model
    if budget is not None:
        payload["token_budget_monthly"] = budget

    resp = api_post("/api/v1/agents", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    row = resp.json()
    if as_json:
        typer.echo(json.dumps(row, indent=2))
        return
    typer.echo(f"Created agent id={row.get('id')} slug={row.get('slug')} name={row.get('name')}")


@agent_app.command("show")
def show_agent(
    slug: str = typer.Argument(..., help="Agent slug"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show one agent profile's full detail."""
    row = _resolve_agent(slug)
    if as_json:
        typer.echo(json.dumps(row, indent=2))
        return
    _print_agent(row)


@agent_app.command("delete")
def delete_agent(
    slug: str = typer.Argument(..., help="Agent slug"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete an agent profile (and revoke every PAT minted for it). The
    default agent cannot be deleted — the server rejects that with
    `default_agent_undeletable`."""
    row = _resolve_agent(slug)
    if not yes:
        confirm = typer.confirm(f"Delete agent '{slug}' ({row['id']})?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/v1/agents/{row['id']}")
    if resp.status_code != 204:
        _fail(resp)
    typer.echo(f"Deleted agent {slug}")


@scope_app.command("set")
def scope_set(
    slug: str = typer.Argument(..., help="Agent slug"),
    plugin: list[str] = typer.Option([], "--plugin", help="Plugin id to grant (repeatable)"),
    table: list[str] = typer.Option([], "--table", help="Table id to grant (repeatable)"),
    data_package: list[str] = typer.Option(
        [],
        "--data-package",
        help="Data package id to grant (repeatable); also grants the tables in it",
    ),
    collection: list[str] = typer.Option([], "--collection", help="File collection id to grant (repeatable)"),
    connection: list[str] = typer.Option([], "--connection", help="Connection id to grant (repeatable)"),
    memory_domain: list[str] = typer.Option([], "--memory-domain", help="Memory domain id to grant (repeatable)"),
    slack_channel: list[str] = typer.Option(
        [],
        "--slack-channel",
        help="Slack channel id whose @mentions route to this agent (repeatable; "
        "one agent per channel — the server 409s on a channel bound elsewhere)",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Replace an agent's resource scope grants.

    Every item passed here is a `(item_type, item_id)` pair PUT in a single
    call — the server replaces the full grant set, it does not merge. At
    least one of the repeatable options is required.
    """
    items: list[dict] = []
    items += [{"item_type": "plugin", "item_id": v} for v in plugin]
    items += [{"item_type": "table", "item_id": v} for v in table]
    items += [{"item_type": "data_package", "item_id": v} for v in data_package]
    items += [{"item_type": "collection", "item_id": v} for v in collection]
    items += [{"item_type": "connection", "item_id": v} for v in connection]
    items += [{"item_type": "memory_domain", "item_id": v} for v in memory_domain]
    items += [{"item_type": "slack_channel", "item_id": v} for v in slack_channel]
    if not items:
        typer.echo(
            "Error: at least one of --plugin/--table/--data-package/--collection/--connection/"
            "--memory-domain/--slack-channel is required.",
            err=True,
        )
        raise typer.Exit(2)

    row = _resolve_agent(slug)
    # Replace-not-merge has a sharper edge for slack_channel items than for
    # plugins/tables: dropping one silently unbinds a live Slack integration
    # (mentions revert to the generic agent-less profile with no error). Warn
    # when this PUT would do that; still proceed — the contract is replace.
    kept = set(slack_channel)
    # Purely advisory — a proxy 502/HTML body or transient 500 here must not
    # abort the PUT the user actually asked for.
    try:
        detail = api_get(f"/api/v1/agents/{row['id']}").json()
    except Exception:
        detail = None
    if isinstance(detail, dict):
        dropped = [
            i["item_id"]
            for i in detail.get("scope", [])
            if i.get("item_type") == "slack_channel" and i["item_id"] not in kept
        ]
        if dropped:
            typer.echo(
                f"Warning: this replaces the FULL scope set and will UNBIND Slack channel(s) "
                f"{', '.join(dropped)} — mentions there revert to the generic profile. "
                f"Re-include --slack-channel <id> to keep a binding.",
                err=True,
            )
    resp = api_put(f"/api/v1/agents/{row['id']}/scope", json={"items": items})
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Scope set for agent {slug}: {len(body.get('items', []))} item(s)")


@agent_app.command("token")
def create_token(
    slug: str = typer.Argument(..., help="Agent slug"),
    name: str = typer.Option(..., "--name", help="Human label for the token"),
    expires_days: int = typer.Option(
        _DEFAULT_TOKEN_EXPIRES_DAYS,
        "--expires-days",
        help=f"Lifetime in days (default {_DEFAULT_TOKEN_EXPIRES_DAYS}); 0 = never expires",
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="Print only the raw secret to stdout (for CI/scripts) — mirrors `agnes auth token create --raw`",
    ),
):
    """Mint an agent PAT.

    Requires all four scope modes (plugins/connections/tables/memory) to be
    `selected` — the server 403s with `agent_not_selected_mode` for an
    `all`-mode agent (including the default agent). Run `agnes agent scope
    set` first.
    """
    row = _resolve_agent(slug)
    payload = {"name": name, "expires_in_days": None if expires_days == 0 else expires_days}
    resp = api_post(f"/api/v1/agents/{row['id']}/tokens", json=payload)
    if resp.status_code != 200:
        _fail(resp)
    data = resp.json()
    if raw:
        typer.echo(
            "Agent personal access token created — this is shown ONCE and cannot be retrieved again.",
            err=True,
        )
        typer.echo(data["token"])
        return
    typer.echo("Agent personal access token created — this is shown ONCE and cannot be retrieved again:")
    typer.echo("")
    typer.echo(f"    {data['token']}")
    typer.echo("")
    typer.echo(f"id:      {data['id']}")
    typer.echo(f"name:    {data['name']}")
    typer.echo(f"agent:   {slug}")
    typer.echo(f"expires: {data.get('expires_at') or 'never'}")


@agent_app.command("usage")
def agent_usage(
    slug: str = typer.Argument(..., help="Agent slug"),
    period: Optional[str] = typer.Option(
        None, "--period", help="Month to report, YYYY-MM (default: current UTC month)"
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Show one agent's monthly token usage against its budget.

    Maps to `GET /api/v1/agents/{slug}/usage` — callable with either an
    interactive session token or an agent PAT scoped to this agent (same
    auth as `agnes agent ask`).
    """
    params: dict = {}
    if period is not None:
        params["period"] = period
    resp = api_get(f"/api/v1/agents/{slug}/usage", params=params)
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"period:              {body.get('period')}")
    typer.echo(f"agent:               {body.get('agent_slug')}")
    typer.echo(f"input_tokens:        {body.get('input_tokens')}")
    typer.echo(f"output_tokens:       {body.get('output_tokens')}")
    typer.echo(f"cache_read_tokens:   {body.get('cache_read_tokens')}")
    typer.echo(f"cache_creation_tokens: {body.get('cache_creation_tokens')}")
    typer.echo(f"total_tokens:        {body.get('total_tokens')}")
    budget_limit = body.get("budget_limit")
    budget_remaining = body.get("budget_remaining")
    typer.echo(f"budget_limit:        {budget_limit if budget_limit is not None else '(unbounded)'}")
    typer.echo(f"budget_remaining:    {budget_remaining if budget_remaining is not None else '(unbounded)'}")


def _render_ask_answer(body: dict, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(body.get("answer") or "(no answer)")


def _render_job_result(job: dict, as_json: bool) -> None:
    if job.get("status") == "failed":
        err = job.get("error")
        if isinstance(err, dict):
            typer.echo(f"Job failed: {err.get('code', 'error')}: {err.get('message', '')}", err=True)
        else:
            typer.echo(f"Job failed: {err}", err=True)
        raise typer.Exit(1)
    if as_json:
        typer.echo(json.dumps(job, indent=2))
        return
    result = job.get("result") or {}
    typer.echo(result.get("answer") or "(no answer)")


def _poll_job(job_id: str, timeout_s: float) -> dict:
    """Poll `GET /api/v1/jobs/{job_id}` until it reaches a terminal status or
    `timeout_s` elapses.

    Bounded two ways: primarily by a wall-clock deadline (`time.monotonic()
    at entry + timeout_s`) so the budget the caller passed in is what
    actually gets spent, and secondarily by attempt count (`timeout_s //
    _POLL_INTERVAL_S`, floor 1) so a mocked/no-op `time.sleep` in tests (or a
    genuinely instant server) can't turn this into an unbounded hot loop —
    attempts still happen within the deadline, they just can't out-count it.
    """
    deadline = time.monotonic() + timeout_s
    max_attempts = max(1, int(timeout_s // _POLL_INTERVAL_S) + 1)
    job: dict = {}
    for attempt in range(max_attempts):
        resp = api_get(f"/api/v1/jobs/{job_id}")
        if resp.status_code != 200:
            _fail(resp)
        job = resp.json()
        if job.get("status") in _TERMINAL_JOB_STATUSES:
            return job
        if time.monotonic() >= deadline:
            break
        if attempt < max_attempts - 1:
            time.sleep(_POLL_INTERVAL_S)
    typer.echo(
        f"Timed out after {timeout_s:.0f}s waiting for job {job_id} "
        f"(status={job.get('status')}). The run keeps going server-side — "
        f"it is not cancelled by this timeout. Check it later with a fresh "
        f"`agnes agent ask` call, or inspect `GET /api/v1/jobs/{job_id}` directly.",
        err=True,
    )
    raise typer.Exit(1)


@agent_app.command("ask")
def ask(
    slug: str = typer.Argument(..., help="Agent slug"),
    prompt: str = typer.Argument(..., help="Prompt to send"),
    timeout: Optional[int] = typer.Option(
        None,
        "--timeout",
        help=(
            f"Max seconds for the synchronous wait (default {_DEFAULT_ASK_TIMEOUT_S}, "
            f"capped at {_MAX_ASK_TIMEOUT_S} for the sync leg — background job polling "
            "continues for the full --timeout value)"
        ),
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """One-shot request/response over an agent (`POST /responses`).

    A `200` is the answer, ready now. A `202` means the sync wait outran
    the timeout (or the server degraded immediately) — this command polls
    `GET /api/v1/jobs/{id}` for the *remainder* of the `--timeout` budget
    (the sync POST above may itself have consumed a chunk of it), so the
    total wall-clock time this command can spend stays bounded by
    `--timeout` instead of doubling to (sync wait) + (full poll budget).
    The underlying run is never cancelled by a client-side timeout; only
    the CLI's own wait is bounded.
    """
    start = time.monotonic()
    total_timeout = _DEFAULT_ASK_TIMEOUT_S if timeout is None else max(1, timeout)
    server_timeout = min(total_timeout, _MAX_ASK_TIMEOUT_S)
    resp = api_post(
        f"/api/v1/agents/{slug}/responses",
        json={"input": prompt, "timeout_s": server_timeout},
        timeout=server_timeout + _HTTP_TIMEOUT_MARGIN_S,
    )
    if resp.status_code == 200:
        _render_ask_answer(resp.json(), as_json)
        return
    if resp.status_code == 202:
        job_id = resp.json()["job_id"]
        remaining = max(1, total_timeout - int(time.monotonic() - start))
        job = _poll_job(job_id, remaining)
        _render_job_result(job, as_json)
        return
    _fail(resp)


# ---------------------------------------------------------------------------
# webhooks — outbound job-completion notifications (V1b Task 6/8)
# ---------------------------------------------------------------------------


def _print_webhook(row: dict) -> None:
    typer.echo(f"id:                  {row.get('id')}")
    typer.echo(f"url:                 {row.get('url')}")
    typer.echo(f"events:              {', '.join(row.get('events') or [])}")
    typer.echo(f"active:              {row.get('active')}")
    typer.echo(f"consecutive_failures: {row.get('consecutive_failures')}")
    typer.echo(f"created_at:          {row.get('created_at')}")


@webhooks_app.command("list")
def webhooks_list(
    slug: str = typer.Argument(..., help="Agent slug"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List an agent's outbound webhook registrations. Never includes the
    signing secret — that is shown once, at `webhooks add` time only."""
    resp = api_get(f"/api/v1/agents/{slug}/webhooks")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Webhooks: {len(rows)}")
    if not rows:
        typer.echo(f"No webhooks yet. Register one with: agnes agent webhooks add {slug} --url <https-url>")
        return
    for i, row in enumerate(rows):
        if i:
            typer.echo("")
        _print_webhook(row)


@webhooks_app.command("add")
def webhooks_add(
    slug: str = typer.Argument(..., help="Agent slug"),
    url: str = typer.Option(..., "--url", help="HTTPS callback URL (must resolve to a public address)"),
    event: list[str] = typer.Option(
        [],
        "--event",
        help=f"Event to subscribe (repeatable); default: {', '.join(_DEFAULT_WEBHOOK_EVENTS)}",
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Register an outbound webhook for job-completion notifications.

    The delivery payload is a small notification (`{event, job_id,
    agent_slug, status, ts}`), never the agent's answer — fetch the actual
    result afterward via `GET /api/v1/jobs/{id}`. The HMAC signing secret
    is printed exactly ONCE below and cannot be retrieved again; store it
    to verify the `x-agnes-signature` header on each delivery.
    """
    payload: dict = {"url": url}
    if event:
        payload["events"] = event
    resp = api_post(f"/api/v1/agents/{slug}/webhooks", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    data = resp.json()
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        return
    typer.echo(
        "Webhook signing secret created — this is shown ONCE and cannot be retrieved again:",
        err=True,
    )
    typer.echo("")
    typer.echo(f"    {data['secret']}")
    typer.echo("")
    _print_webhook(data)


@webhooks_app.command("delete")
def webhooks_delete(
    slug: str = typer.Argument(..., help="Agent slug"),
    webhook_id: str = typer.Argument(..., help="Webhook id (from `agnes agent webhooks list`)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Delete one of an agent's outbound webhook registrations."""
    if not yes:
        confirm = typer.confirm(f"Delete webhook '{webhook_id}' from agent '{slug}'?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/v1/agents/{slug}/webhooks/{webhook_id}")
    if resp.status_code != 204:
        _fail(resp)
    typer.echo(f"Deleted webhook {webhook_id}")


# ---------------------------------------------------------------------------
# memory — owner-facing inspect/approve/archive/delete over an agent's
# private memory notebook (V1c Task 5/7). REST: `/api/v1/agents/{id}/
# memories[/{memory_id}]`. No MCP analogue by design (see
# `tests/test_documentation_api_triple_surface.py`'s
# `_AGENT_MEMORY_ADMIN_REASON`) — this is the human-witnessed governance
# surface over what an agent is allowed to "remember" about itself, not
# something an agent tool call should ever reach.
# ---------------------------------------------------------------------------


def _memory_in_budget_marker(row: dict) -> str:
    """`in_budget` is only present on `active` rows (see `_serialize_memory`
    in `app/api/agents_admin.py`) — C4: an approved memory can still be
    shadowed behind newer active content past the ~6000-token materialize
    budget, so "active" alone doesn't mean "in effect"."""
    if row.get("status") != "active":
        return "-"
    return "in effect" if row.get("in_budget") else "shadowed"


def _print_memory(row: dict) -> None:
    typer.echo(f"id:         {row.get('id')}")
    typer.echo(f"status:     {row.get('status')}")
    if row.get("status") == "active":
        typer.echo(f"in_budget:  {_memory_in_budget_marker(row)}")
    typer.echo(f"content:    {row.get('content')}")
    typer.echo(f"created_at: {row.get('created_at')}")


@memory_app.command("list")
def memory_list(
    slug: str = typer.Argument(..., help="Agent slug"),
    status: Optional[str] = typer.Option(
        None, "--status", help=f"Filter by status: {'|'.join(_MEMORY_STATUS_VALUES)} (default: all)"
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """List an agent's private memory notebook.

    Every `active` row is marked "in effect" or "shadowed" — the same
    newest-first, ~6000-token materialize-budget split the server applies
    when spawning a fresh session, so an owner who just approved a memory
    can tell whether it will actually land in the agent's next run.
    """
    row = _resolve_agent(slug)
    params: dict = {}
    if status is not None:
        params["status"] = status
    resp = api_get(f"/api/v1/agents/{row['id']}/memories", params=params)
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Memories: {len(rows)}")
    if not rows:
        typer.echo("No memories yet — the agent hasn't written anything to remember.")
        return
    for i, r in enumerate(rows):
        if i:
            typer.echo("")
        _print_memory(r)


@memory_app.command("approve")
def memory_approve(
    slug: str = typer.Argument(..., help="Agent slug"),
    memory_id: str = typer.Argument(..., help="Memory id (from `agnes agent memory list`)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Approve a pending memory, promoting it to active.

    A no-op (still `200`) if the memory isn't currently `pending` — mirrors
    the server's `agent_memories_repo().approve` semantics.
    """
    row = _resolve_agent(slug)
    resp = api_patch(f"/api/v1/agents/{row['id']}/memories/{memory_id}", json={"action": "approve"})
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Memory {memory_id} approved (status={body.get('status')})")


@memory_app.command("archive")
def memory_archive(
    slug: str = typer.Argument(..., help="Agent slug"),
    memory_id: str = typer.Argument(..., help="Memory id (from `agnes agent memory list`)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Archive a memory (pending or active), removing it from what
    materializes into future runs."""
    row = _resolve_agent(slug)
    resp = api_patch(f"/api/v1/agents/{row['id']}/memories/{memory_id}", json={"action": "archive"})
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json()
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    typer.echo(f"Memory {memory_id} archived (status={body.get('status')})")


@memory_app.command("delete")
def memory_delete(
    slug: str = typer.Argument(..., help="Agent slug"),
    memory_id: str = typer.Argument(..., help="Memory id (from `agnes agent memory list`)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Permanently delete one of an agent's memories."""
    row = _resolve_agent(slug)
    if not yes:
        confirm = typer.confirm(f"Delete memory '{memory_id}' from agent '{slug}'?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/v1/agents/{row['id']}/memories/{memory_id}")
    if resp.status_code != 204:
        _fail(resp)
    typer.echo(f"Deleted memory {memory_id}")


# ---------------------------------------------------------------------------
# schedule — scheduled runs (agent-schedules design,
# docs/superpowers/specs/2026-08-17-agent-schedules-design.md). Slug-keyed,
# same as webhooks — `app/api/agent_schedules.py`'s CRUD has no get-by-name
# route (only list + PATCH/DELETE by id), so `remove`/`enable`/`disable`
# resolve a schedule name via one `list` round trip, same pattern
# `_resolve_agent` uses for slug -> id.
# ---------------------------------------------------------------------------


def _print_schedule(row: dict) -> None:
    typer.echo(f"id:          {row.get('id')}")
    typer.echo(f"name:        {row.get('name')}")
    typer.echo(f"schedule:    {row.get('schedule')}")
    typer.echo(f"prompt:      {row.get('prompt')}")
    typer.echo(f"enabled:     {row.get('enabled')}")
    typer.echo(f"last_run_at: {row.get('last_run_at') or '(never)'}")
    typer.echo(f"last_status: {row.get('last_status') or '-'}")
    typer.echo(f"last_job_id: {row.get('last_job_id') or '-'}")


def _resolve_schedule(slug: str, name: str) -> dict:
    resp = api_get(f"/api/v1/agents/{slug}/schedules")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    for row in rows:
        if row.get("name") == name:
            return row
    typer.echo(
        f"Schedule not found: {name}. List this agent's schedules with: agnes agent schedule list {slug}",
        err=True,
    )
    raise typer.Exit(1)


@schedule_app.command("list")
def schedule_list(
    slug: str = typer.Argument(..., help="Agent slug"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List an agent's scheduled runs."""
    resp = api_get(f"/api/v1/agents/{slug}/schedules")
    if resp.status_code != 200:
        _fail(resp)
    rows = resp.json().get("data", [])
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"Schedules: {len(rows)}")
    if not rows:
        typer.echo(
            f"No schedules yet. Add one with: agnes agent schedule add {slug} "
            f'--name <name> --schedule "cron 0 7 * * 1-5" --prompt <prompt>'
        )
        return
    for i, row in enumerate(rows):
        if i:
            typer.echo("")
        _print_schedule(row)


def _skill_templated_prompt(skill: str) -> str:
    """Resolve ``skill`` against ``GET /api/v2/marketplace/skills`` and render
    the same prompt template the builder UI's Skill dropdown uses. Pure
    client-side sugar — the API payload stays a plain ``prompt`` string.

    Matches (case-insensitively) on a skill's display ``name``, its directory
    ``skill_name``, or the qualified ``<plugin_name>:<skill_name>`` — the
    qualified form disambiguates when two plugins ship a same-named skill.
    """
    resp = api_get("/api/v2/marketplace/skills")
    if resp.status_code != 200:
        _fail(resp)
    entries = resp.json().get("skills", [])
    want = skill.strip().lower()
    matches = [
        s
        for s in entries
        if want
        in {
            (s.get("name") or "").lower(),
            (s.get("skill_name") or "").lower(),
            f"{s.get('plugin_name')}:{s.get('skill_name')}".lower(),
        }
    ]
    if not matches:
        typer.echo(f"Skill not found: {skill}.", err=True)
        if entries:
            names = sorted({f"{s.get('plugin_name')}:{s.get('skill_name')}" for s in entries})
            typer.echo("Available skills: " + ", ".join(names), err=True)
        else:
            typer.echo("No marketplace skills are available to you.", err=True)
        raise typer.Exit(1)
    if len({(s.get("plugin_name"), s.get("skill_name")) for s in matches}) > 1:
        names = sorted({f"{s.get('plugin_name')}:{s.get('skill_name')}" for s in matches})
        typer.echo(
            f"Skill name '{skill}' is ambiguous — qualify it as one of: " + ", ".join(names),
            err=True,
        )
        raise typer.Exit(1)
    entry = matches[0]
    label = entry.get("name") or entry.get("skill_name")
    description = (entry.get("description") or "").strip()
    return f"Run the {label} skill" + (f": {description}" if description else ".")


@schedule_app.command("add")
def schedule_add(
    slug: str = typer.Argument(..., help="Agent slug"),
    name: str = typer.Option(..., "--name", help="Run-type label, unique per agent (e.g. morning-briefing)"),
    schedule: str = typer.Option(
        ...,
        "--schedule",
        help="'every Nm'/'every Nh', 'daily HH:MM[,HH:MM]' (UTC), or 'cron <5-field expr>' (UTC)",
    ),
    prompt: Optional[str] = typer.Option(None, "--prompt", help="Prompt sent to the agent on each fire"),
    skill: Optional[str] = typer.Option(
        None,
        "--skill",
        help="Template the prompt from a marketplace skill ('<name>' or '<plugin>:<skill>'); "
        "mutually exclusive with --prompt",
    ),
    disabled: bool = typer.Option(False, "--disabled", help="Create the schedule disabled"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Add a scheduled run for an agent."""
    if (prompt is None) == (skill is None):
        typer.echo(
            "Provide exactly one of --prompt or --skill (--skill only templates a starting prompt; "
            "to tweak it, pass the full text via --prompt).",
            err=True,
        )
        raise typer.Exit(1)
    if prompt is None:
        prompt = _skill_templated_prompt(skill)  # type: ignore[arg-type]  # exactly-one guard above
    payload = {"name": name, "schedule": schedule, "prompt": prompt, "enabled": not disabled}
    resp = api_post(f"/api/v1/agents/{slug}/schedules", json=payload)
    if resp.status_code != 201:
        _fail(resp)
    row = resp.json()
    if as_json:
        typer.echo(json.dumps(row, indent=2))
        return
    typer.echo(f"Created schedule id={row.get('id')} name={row.get('name')} schedule={row.get('schedule')}")


@schedule_app.command("remove")
def schedule_remove(
    slug: str = typer.Argument(..., help="Agent slug"),
    name: str = typer.Argument(..., help="Schedule name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
):
    """Remove a scheduled run."""
    row = _resolve_schedule(slug, name)
    if not yes:
        confirm = typer.confirm(f"Delete schedule '{name}' from agent '{slug}'?")
        if not confirm:
            raise typer.Abort()
    resp = api_delete(f"/api/v1/agents/{slug}/schedules/{row['id']}")
    if resp.status_code != 204:
        _fail(resp)
    typer.echo(f"Deleted schedule {name}")


def _set_schedule_enabled(slug: str, name: str, enabled: bool) -> None:
    row = _resolve_schedule(slug, name)
    resp = api_patch(f"/api/v1/agents/{slug}/schedules/{row['id']}", json={"enabled": enabled})
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Schedule {name} {'enabled' if enabled else 'disabled'}")


@schedule_app.command("enable")
def schedule_enable(
    slug: str = typer.Argument(..., help="Agent slug"),
    name: str = typer.Argument(..., help="Schedule name"),
):
    """Enable a scheduled run."""
    _set_schedule_enabled(slug, name, True)


@schedule_app.command("disable")
def schedule_disable(
    slug: str = typer.Argument(..., help="Agent slug"),
    name: str = typer.Argument(..., help="Schedule name"),
):
    """Disable a scheduled run."""
    _set_schedule_enabled(slug, name, False)
