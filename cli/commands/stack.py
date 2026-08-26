"""`agnes stack` — user-facing stack management (v49 unified stack).

Four subcommands mirror the user-side `/api/stack/*` endpoints:

  - `agnes stack list [--type plugin|data_package|memory_domain]`
  - `agnes stack browse [--type data_package|memory_domain]`
  - `agnes stack add <type> <id>`
  - `agnes stack remove <type> <id>`

The `data_package` and `memory_domain` types are routed through the new
`/api/stack` surface; `plugin` is intentionally NOT supported by this
subcommand (per design D1 — plugins keep the existing
``/api/marketplace`` flow). Passing `--type plugin` to `list` is a soft
error pointing at `agnes marketplace`.

Output is a Rich table for humans; `--json` is honored on `list` for
scripts. Typed server errors (`already_required`, `no_grant`,
`cannot_remove_required`) are surfaced as one-line messages with hints.
"""

from __future__ import annotations

import json
from typing import Optional

import typer

from cli.client import api_get, api_post, api_delete

stack_app = typer.Typer(help="Manage your stack (data packages + memory domains)")


_SUPPORTED_TYPES = ("data_package", "memory_domain")
_PLUGIN_HINT = "Plugins are managed via the marketplace flow — see `agnes marketplace`."


def _fail(resp, *, expected: tuple[int, ...] = (200, 201)) -> None:
    """Render a typed-error message and exit non-zero. Mirrors admin.py."""
    try:
        body = resp.json()
    except Exception:
        body = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail:
        msg = detail
    elif isinstance(detail, dict):
        msg = detail.get("kind") or detail.get("message") or json.dumps(detail)
    else:
        msg = resp.text or f"HTTP {resp.status_code}"
    typer.echo(f"Error ({resp.status_code}): {msg}", err=True)
    raise typer.Exit(1)


def _validate_type(value: str, *, allow_plugin_for_list: bool = False) -> str:
    if value == "plugin":
        typer.echo(_PLUGIN_HINT, err=True)
        raise typer.Exit(2)
    if value not in _SUPPORTED_TYPES:
        typer.echo(
            f"Unknown --type {value!r}. Supported: {', '.join(_SUPPORTED_TYPES)}.",
            err=True,
        )
        raise typer.Exit(2)
    return value


# The tier's human vocabulary — 'automatic' (in every member's stack, no add
# needed) / 'optional' (add it yourself) — matching the admin UI and the
# Library. The API enum ('required'/'available') is a wire format: it stays
# exact in --json and in the --requirement flag on `agnes admin grant`.
_TIER_WORDS = {"required": "automatic", "available": "optional"}


def _tier_word(raw: str) -> str:
    return _TIER_WORDS.get(raw, raw)


@stack_app.command("list")
def stack_list(
    type_filter: Optional[str] = typer.Option(None, "--type", help="data_package | memory_domain (omit for both)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """List items in your effective stack.

    By default (classic membership) the effective stack is the subscribe
    model — required grants plus the ``available`` ones you added via
    ``agnes stack add`` — and every member is downloaded by ``agnes pull``.
    When the instance runs auto-membership (``features.stack_auto_
    membership``), every grant is a member (required ∪ available, no
    subscription needed) and ``materialized`` marks the local copies.
    Without ``--type`` both data_packages and memory_domains are fetched
    and concatenated (the server has no all-types endpoint by design —
    keeps the API contract narrow).
    """
    if type_filter:
        types = [_validate_type(type_filter)]
    else:
        types = list(_SUPPORTED_TYPES)
    aggregated: list[dict] = []
    for t in types:
        resp = api_get("/api/stack", params={"type": t})
        if resp.status_code != 200:
            _fail(resp)
        body = resp.json() or {}
        for it in body.get("items", []):
            it["type"] = t
            aggregated.append(it)

    if as_json:
        typer.echo(json.dumps(aggregated, indent=2))
        return

    if not aggregated:
        typer.echo("Your stack is empty.")
        return

    name_w = max(len("NAME"), max((len(i.get("name", "")) for i in aggregated), default=4))
    type_w = max(len("TYPE"), max((len(i.get("type", "")) for i in aggregated), default=4))
    req_w = max(len("REQUIREMENT"), 11)
    header = f"{'NAME':<{name_w}}  {'TYPE':<{type_w}}  {'REQUIREMENT':<{req_w}}  DESCRIPTION"
    typer.echo(header)
    typer.echo("-" * len(header))
    for it in aggregated:
        desc = (it.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 60:
            desc = desc[:57] + "..."
        typer.echo(
            f"{it.get('name', '')[:name_w]:<{name_w}}  "
            f"{it.get('type', ''):<{type_w}}  "
            f"{_tier_word(it.get('requirement', '')):<{req_w}}  "
            f"{desc}"
        )


@stack_app.command("browse")
def stack_browse(
    type_filter: Optional[str] = typer.Option(None, "--type", help="data_package | memory_domain (omit for both)"),
    as_json: bool = typer.Option(False, "--json"),
):
    """Browse every resource granted to your groups — the full candidate
    set, whether or not it is in your stack yet.

    By default (classic membership) the ``IN STACK`` column marks the
    members — required grants plus the ones you added — and the rest are
    addable: ``agnes stack add <type> <id>`` joins your stack, then
    ``agnes pull`` downloads it. Under auto-membership
    (``features.stack_auto_membership``) every granted row reads ✓ and
    ``add`` only requests the local copy.
    Without ``--type`` both data_packages and memory_domains are fetched.
    """
    if type_filter:
        types = [_validate_type(type_filter)]
    else:
        types = list(_SUPPORTED_TYPES)
    aggregated: list[dict] = []
    for t in types:
        resp = api_get("/api/stack/browse", params={"type": t})
        if resp.status_code != 200:
            _fail(resp)
        body = resp.json() or {}
        for it in body.get("items", []):
            it["type"] = t
            aggregated.append(it)

    if as_json:
        typer.echo(json.dumps(aggregated, indent=2))
        return

    if not aggregated:
        typer.echo("No resources available to browse.")
        return

    # ID leads the row because it is the one token the reader has to carry to
    # the sibling command: `agnes stack add <type> <id>` takes the id, and the
    # table used to print NAME / TYPE / REQUIREMENT / IN STACK / DESCRIPTION
    # and no id at all. Names are display strings — the ones on a real
    # instance contain em dashes ("Sales — Northwind Orders"), so they are not
    # even retypeable — which left `--json` or the raw API as the only way to
    # learn what `add` wanted.
    id_w = max(len("ID"), max((len(str(i.get("id", ""))) for i in aggregated), default=2))
    name_w = max(len("NAME"), max((len(i.get("name", "")) for i in aggregated), default=4))
    type_w = max(len("TYPE"), max((len(i.get("type", "")) for i in aggregated), default=4))
    req_w = max(len("REQUIREMENT"), 11)
    in_w = len("IN STACK")
    header = (
        f"{'ID':<{id_w}}  {'NAME':<{name_w}}  {'TYPE':<{type_w}}  "
        f"{'REQUIREMENT':<{req_w}}  {'IN STACK':<{in_w}}  DESCRIPTION"
    )
    typer.echo(header)
    typer.echo("-" * len(header))
    for it in aggregated:
        desc = (it.get("description") or "").replace("\n", " ").strip()
        if len(desc) > 60:
            desc = desc[:57] + "..."
        mark = "✓" if it.get("in_stack") else ""
        typer.echo(
            f"{str(it.get('id', '')):<{id_w}}  "
            f"{it.get('name', '')[:name_w]:<{name_w}}  "
            f"{it.get('type', ''):<{type_w}}  "
            f"{_tier_word(it.get('requirement', '')):<{req_w}}  "
            f"{mark:<{in_w}}  "
            f"{desc}"
        )


@stack_app.command("add")
def stack_add(
    resource_type: str = typer.Argument(..., help="data_package | memory_domain"),
    resource_id: str = typer.Argument(..., help="Resource id to subscribe to"),
):
    """Add a granted data_package or memory_domain to your stack.

    By default (classic membership) this JOINS your stack — the resource
    becomes queryable and ``agnes pull`` starts downloading it. Under
    auto-membership (``features.stack_auto_membership``) every grant is
    already in your stack, so this only makes ``agnes pull`` ALSO fetch a
    local copy."""
    rt = _validate_type(resource_type)
    resp = api_post(
        "/api/stack/subscribe",
        json={"resource_type": rt, "resource_id": resource_id},
    )
    if resp.status_code != 200:
        # Translate server detail codes into actionable hints.
        try:
            body = resp.json()
        except Exception:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str):
            if detail.startswith("already_required"):
                typer.echo(
                    f"{resource_id} is already required for one of your groups — already downloaded, no subscription needed.",
                    err=True,
                )
                raise typer.Exit(0)
            if detail == "no_grant":
                typer.echo(
                    f"Access denied: your groups have no grant on {rt}/{resource_id}. Ask an admin to grant it.",
                    err=True,
                )
                raise typer.Exit(1)
        _fail(resp)
    typer.echo(f"Added {rt}/{resource_id} to your stack — run `agnes pull` to fetch it.")


@stack_app.command("remove")
def stack_remove(
    resource_type: str = typer.Argument(..., help="data_package | memory_domain"),
    resource_id: str = typer.Argument(..., help="Resource id to unsubscribe from"),
):
    """Remove an available data_package or memory_domain from your stack.

    By default (classic membership) this LEAVES your stack — the resource
    stops being queryable and ``agnes pull`` stops syncing it (re-add it
    any time with ``agnes stack add``). Under auto-membership
    (``features.stack_auto_membership``) the resource stays in your stack,
    still queryable server-side — only the local download is dropped.
    Removing a *required* resource is refused with a hint pointing at the
    grant — required resources are always in the stack and downloaded, no
    opt-out; the admin would need to downgrade the grant to `available`
    first.
    """
    rt = _validate_type(resource_type)
    resp = api_delete(f"/api/stack/subscription/{rt}/{resource_id}")
    # 0.54.26 design-rules pass moved this endpoint to 204; treat any
    # 2xx as success (covers both old and new servers).
    if resp.status_code >= 300:
        try:
            body = resp.json()
        except Exception:
            body = {}
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail.startswith("cannot_remove_required"):
            typer.echo(
                f"{rt}/{resource_id} is required by your group's grant — always downloaded, "
                f"ask an admin to downgrade to `available` first.",
                err=True,
            )
            raise typer.Exit(1)
        _fail(resp)
    typer.echo(
        f"Removed {rt}/{resource_id} from your stack subscriptions — under the classic "
        f"default it leaves your stack (re-add with `agnes stack add`); under "
        f"auto-membership it stays queryable server-side and only the local copy is dropped."
    )


# ---------------------------------------------------------------------------
# `agnes stack artefacts …` — add "Artefacts to My Stack" (see the product
# spec's Architecture decisions: artefacts are NOT routed through the
# data_package/memory_domain surface above — no admin-RBAC "required" tier,
# permission is ownership/sharing).
# ---------------------------------------------------------------------------

artefacts_app = typer.Typer(help="Add/remove artefacts (file collections) in your Stack")
stack_app.add_typer(artefacts_app, name="artefacts")


@artefacts_app.command("list")
def artefacts_candidates(as_json: bool = typer.Option(False, "--json")):
    """List artefacts eligible to add to your Stack — accessible to you
    (owned, shared with you/your team, or workspace-published) and not
    already in your Stack."""
    resp = api_get("/api/stack/artefacts/candidates")
    if resp.status_code != 200:
        _fail(resp)
    body = resp.json() or {}
    items = body.get("items", [])
    if as_json:
        typer.echo(json.dumps(body, indent=2))
        return
    if not items:
        total = body.get("total_accessible", 0)
        if total == 0:
            typer.echo("No artefacts exist yet — create one first (`agnes` web UI → /artefacts).")
        else:
            typer.echo("All artefacts you can access are already in your Stack.")
        return
    name_w = max(len("TITLE"), max((len(i.get("title", "")) for i in items), default=5))
    header = f"{'TITLE':<{name_w}}  {'TYPE':<10}  {'VISIBILITY':<10}  OWNER"
    typer.echo(header)
    typer.echo("-" * len(header))
    for it in items:
        typer.echo(
            f"{it.get('title', '')[:name_w]:<{name_w}}  "
            f"{it.get('type_label', ''):<10}  "
            f"{it.get('visibility_label', ''):<10}  "
            f"{it.get('owner_label', '')}"
        )


@artefacts_app.command("add")
def artefacts_add(corpus_id: str = typer.Argument(..., help="Artefact (collection) id")):
    """Add an artefact to your Stack so the default agent can use it."""
    resp = api_post(f"/api/stack/artefacts/{corpus_id}")
    if resp.status_code != 200:
        _fail(resp)
    typer.echo(f"Added {corpus_id} to your Stack.")


@artefacts_app.command("remove")
def artefacts_remove(corpus_id: str = typer.Argument(..., help="Artefact (collection) id")):
    """Remove an artefact from your Stack — drops agent access only; the
    artefact itself, its files, ownership and sharing are unaffected."""
    resp = api_delete(f"/api/stack/artefacts/{corpus_id}")
    if resp.status_code >= 300:
        _fail(resp)
    typer.echo(f"Removed {corpus_id} from your Stack.")
