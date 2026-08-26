"""Render the analyst-workspace CLAUDE.md prompt.

The template source is admin-editable at /admin/workspace-prompt.  When no
override is set, the default content is the Jinja2 markdown template shipped
at config/claude_md_template.txt.  When an override is saved, it replaces the
default for every call to render_claude_md().

Override content is a Jinja2 template (autoescape=False, StrictUndefined).
Available placeholders: instance.{name,subtitle}, server.{url,hostname},
sync_interval, data_source.{type,source_types}, tables (list of
{name,description,query_mode,source_type}), metrics.{count,categories},
semantic_layer.has_models (True iff the calling user can read >=1 valid
semantic model), marketplaces (RBAC-filtered list),
user.{id,email,name,is_admin,groups}, now, today.

See also: surfaced as the "Agent Workspace Prompt" admin editor at
/admin/workspace-prompt.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import duckdb
from src.prompt_render import make_prompt_env

from app.instance_config import (
    get_data_source_type,
    get_instance_name,
    get_instance_subtitle,
    get_sync_interval,
)

logger = logging.getLogger(__name__)


def _load_default_template() -> str:
    """Load the shipped CLAUDE.md default template.

    Resolution order (first hit wins):
      1. importlib.resources lookup in the installed `config` package — works
         in both editable installs and wheel-installed deployments. This is
         the canonical path on container deployments where `/app/config/`
         may be bind-mounted to overlay instance-specific config (instance.yaml)
         and shadow the image-baked template file.
      2. Filesystem path relative to this module — for dev runs from a checkout.
      3. Last-resort embedded fallback so the renderer never fails outright.
    """
    # 1. Package-resource path (preferred — works under wheel installs)
    try:
        from importlib import resources

        ref = resources.files("config").joinpath("claude_md_template.txt")
        if ref.is_file():
            return ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass

    # 2. Filesystem path relative to this module (dev checkout)
    fs_path = Path(__file__).resolve().parent.parent / "config" / "claude_md_template.txt"
    if fs_path.exists():
        return fs_path.read_text(encoding="utf-8")

    # 3. Embedded fallback (image stripped down, partial Docker COPY, etc.)
    return (
        "# {{ instance.name }} — AI Harness\n\n"
        "This workspace is connected to {{ server.url }}.\n"
        "Data refreshes every {{ sync_interval }}.\n"
    )


def _missing_table_excs() -> "tuple[type[BaseException], ...]":
    """Exception types that mean 'the backing table doesn't exist yet'
    (e.g. a half-migrated DB) — the graceful-degrade boundary this module
    has tolerated since the original DuckDB-only ``duckdb.CatalogException``
    catch. Postgres surfaces the same condition as
    ``sqlalchemy.exc.ProgrammingError`` (wrapping ``UndefinedTable``), so
    both are caught regardless of which backend the repo factory resolves
    to."""
    excs: "tuple[type[BaseException], ...]" = (duckdb.CatalogException,)
    try:
        from sqlalchemy.exc import ProgrammingError

        excs = excs + (ProgrammingError,)
    except ImportError:
        pass
    return excs


def _list_tables(conn: duckdb.DuckDBPyConnection | None, *, user: dict) -> list[dict[str, Any]]:
    """Return registered tables filtered by the calling user's RBAC grants.

    For admins, returns all tables. For non-admins, returns only tables the
    user has explicit ``resource_grants(resource_type='table')`` access to.
    """
    from src.rbac import get_accessible_tables
    from src.repositories import table_registry_repo

    try:
        allowed_ids = get_accessible_tables(user, conn)  # None=admin, list=non-admin
        if allowed_ids is not None and not allowed_ids:
            return []
        rows = table_registry_repo().list_all()
    except _missing_table_excs():
        return []
    if allowed_ids is not None:
        allowed_set = set(allowed_ids)
        rows = [r for r in rows if r.get("id") in allowed_set]
    return [
        {
            "name": r.get("name"),
            "description": r.get("description") or "",
            "query_mode": r.get("query_mode") or "local",
            "source_type": r.get("source_type") or "",
        }
        for r in rows
    ]


def _metrics_summary(conn: duckdb.DuckDBPyConnection | None, *, user: dict) -> dict[str, Any]:
    """Count/categories for metrics the calling user's table-stack grants
    cover — same gate as GET /api/metrics (app/api/metrics.py:_first_inaccessible_table).
    A metric with no table_name/tables (nothing to gate) is always visible.
    """
    from app.api.metrics import _first_inaccessible_table
    from src.rbac import get_accessible_tables
    from src.repositories import metric_repo

    try:
        rows = metric_repo().list()
        allowed = get_accessible_tables(user, conn)  # None=admin, list=non-admin
        allowed_set = None if allowed is None else set(allowed)
        rows = [r for r in rows if _first_inaccessible_table(r, allowed_set) is None]
    except _missing_table_excs():
        return {"count": 0, "categories": []}
    return {
        "count": len(rows),
        "categories": sorted({r.get("category") for r in rows if r.get("category")}),
    }


def _has_semantic_layer(conn: duckdb.DuckDBPyConnection | None, *, user: dict[str, Any]) -> bool:
    """True iff the calling user can read at least one ``status='valid'``
    semantic model — same RBAC tier as ``GET /api/semantic-models/search``
    (``app/api/semantic_models.py::_can_read_model``: admin, a grant on the
    model itself, or a grant on a Data Package it's linked to).

    Gates the "Semantic layer" CLAUDE.md section: a user with zero
    accessible models gets no section telling them to prefer a layer they
    cannot actually read.
    """
    from app.api.semantic_models import _can_read_model
    from src.repositories import semantic_model_repo

    try:
        rows = semantic_model_repo().list_all()
        # _can_read_model stays INSIDE the guard: it hits resource_grants /
        # user_group_members / data_package_semantic_models, any of which may be
        # absent on a half-migrated database — the whole onboarding document
        # must degrade to "no section", not error (mirrors _metrics_summary;
        # Devin review on #1398).
        # `conn` may be None on Postgres (see this module's entry points). The
        # callee's annotation has not caught up, and it is reached only inside
        # the guard above, which degrades to "no section" — so widening
        # `app/api/semantic_models._can_read_model` is left to whoever owns it.
        return any(
            row.get("status") == "valid" and _can_read_model(user, row, conn)  # type: ignore[arg-type]
            for row in rows
        )
    except _missing_table_excs():
        return False


def _marketplaces_for_user(conn: duckdb.DuckDBPyConnection | None, user: dict[str, Any]) -> list[dict[str, Any]]:
    """Return marketplaces with the plugins the user is allowed to see.

    Delegates RBAC filtering entirely to resolve_allowed_plugins, which
    returns List[dict] with marketplace_slug, original_name, etc.
    Results are grouped by marketplace slug; display names are fetched
    from marketplace_registry via the repo factory.
    """
    from src.repositories import marketplace_registry_repo

    try:
        from src.marketplace_filter import resolve_allowed_plugins

        # Same as above: None-tolerant in practice (this whole block is wrapped),
        # annotation not yet widened at the callee.
        allowed = resolve_allowed_plugins(conn, user)  # type: ignore[arg-type]
    except Exception:
        logger.exception("_marketplaces_for_user: marketplace plugin resolution failed")
        return []
    if not allowed:
        return []

    # Build slug → display name lookup from registry
    slugs = {p["marketplace_slug"] for p in allowed}
    try:
        all_rows = marketplace_registry_repo().list_all()
    except _missing_table_excs():
        all_rows = []
    slug_to_name: dict[str, str] = {r["id"]: r["name"] for r in all_rows if r["id"] in slugs}

    grouped: dict[str, dict[str, Any]] = {}
    for plugin in allowed:
        slug = plugin["marketplace_slug"]
        bucket = grouped.setdefault(
            slug,
            {
                "slug": slug,
                "name": slug_to_name.get(slug, slug),
                "plugins": [],
            },
        )
        bucket["plugins"].append({"name": plugin["original_name"]})

    return list(grouped.values())


def build_claude_md_context(
    # Accepts ``None`` for the same reason ``render_claude_md`` does. It DOES
    # pass the conn on (``_list_tables``, ``_metrics_summary``,
    # ``_has_semantic_layer``, ``_marketplaces_for_user``) — an earlier version
    # of this comment claimed otherwise, from a grep whose range collapsed on
    # its own end pattern. What is true is that every one of those tolerates
    # ``None`` and falls through to the repository factory
    # (``get_accessible_tables``'s conn is Optional, and the marketplace read
    # is wrapped), which is why Postgres has passed ``None`` here since #518.
    conn: duckdb.DuckDBPyConnection | None,
    *,
    user: dict[str, Any],
    server_url: str,
    is_sandbox: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Compose the Jinja2 render context for the CLAUDE.md template. Pure, no side effects.

    ``now`` pins the clock the template renders (``{{ now }}``, ``{{ today }}``).
    The default is the wall clock, which is right for a document written once at
    workspace init. A caller that must produce the SAME bytes for the same
    inputs has to pin it: the shipped template ends with "generated
    {{ today }}", so an unpinned render changes at every UTC date rollover.

    ``is_sandbox`` distinguishes the two surfaces that render this same
    template: the ephemeral chat sandbox (``app/main.py``'s
    ``_render_workspace_prompt``, always ``True``) versus an analyst's own
    laptop workspace (``GET /api/welcome``, called by ``agnes init`` — the
    default, ``False``). A handful of claims in the template (preinstalled
    libraries, "this filesystem is not the user's computer") are only true
    for the former; template sections that need to say something different
    for a laptop workspace branch on this flag.
    """
    now = datetime.now(timezone.utc) if now is None else now
    parsed = urlparse(server_url)
    tables = _list_tables(conn, user=user)
    return {
        "instance": {
            "name": get_instance_name(),
            "subtitle": get_instance_subtitle(),
        },
        "server": {
            "url": server_url,
            "hostname": parsed.hostname or "",
        },
        "sync_interval": get_sync_interval(),
        "data_source": {
            "type": get_data_source_type(),
            # Distinct source_types of the tables the CALLER can see. The
            # primary `type` alone can't gate engine-specific template
            # sections: a multi-source instance registers e.g. BigQuery or
            # Databricks remote rows while `type` stays 'local'/'keboola'.
            "source_types": sorted({t["source_type"] for t in tables if t["source_type"]}),
        },
        "tables": tables,
        "metrics": _metrics_summary(conn, user=user),
        "semantic_layer": {"has_models": _has_semantic_layer(conn, user=user)},
        "marketplaces": _marketplaces_for_user(conn, user),
        "user": {
            "id": user.get("id", ""),
            "email": user.get("email", ""),
            "name": user.get("name") or "",
            "is_admin": bool(user.get("is_admin")),
            "groups": user.get("groups") or [],
        },
        "now": now,
        "today": now.date().isoformat(),
        "is_sandbox": is_sandbox,
    }


def compute_default_claude_md(
    conn: duckdb.DuckDBPyConnection,
    *,
    user: dict[str, Any],
    server_url: str,
    is_sandbox: bool = False,
) -> str:
    """Return the rendered default CLAUDE.md from config/claude_md_template.txt.

    Renders the shipped Jinja2 template with the given user's RBAC context.
    On TemplateError, raises — callers that want graceful fallback should catch.
    """
    source = _load_default_template()
    env = make_prompt_env()
    template = env.from_string(source)
    return template.render(**build_claude_md_context(conn, user=user, server_url=server_url, is_sandbox=is_sandbox))


def render_claude_md(
    # Optional because Postgres instances pass ``None``: the function routes
    # its own state reads through the repository factory, so on PG there is no
    # system DuckDB to open (opening it there is a forbidden invariant). Both
    # sandbox callers have always passed None on PG; the annotation had simply
    # not caught up.
    conn: duckdb.DuckDBPyConnection | None,
    *,
    user: dict[str, Any],
    server_url: str,
    is_sandbox: bool = False,
    now: datetime | None = None,
) -> str:
    """Resolve the active template (override or default) and render it for the given user.

    ``now`` pins the rendered clock — see :func:`build_claude_md_context`.

    When an admin override is set, renders it via Jinja2 (StrictUndefined, autoescape=False).
    When no override is set, renders the shipped default template.

    On TemplateError, raises — the API layer catches this and returns 400/500.

    #622: resolution honors the workspace prompt's ``source_mode`` toggle —
    ``'editor'`` returns the DB override (or default when unset, today's
    behavior); ``'git'`` binds to the IWT clone file. A None result (no
    override, or a git-bound file that's missing) falls back to the shipped
    default, then renders through Jinja as before.

    ``is_sandbox``: see ``build_claude_md_context``. Callers rendering for the
    chat sandbox must pass ``True``; the default (``False``) matches the
    laptop-workspace / ``GET /api/welcome`` path.
    """
    from src.initial_workspace import resolve_prompt

    content, _mode = resolve_prompt("workspace", conn)
    source = content if content else _load_default_template()
    env = make_prompt_env()
    template = env.from_string(source)
    return template.render(
        **build_claude_md_context(conn, user=user, server_url=server_url, is_sandbox=is_sandbox, now=now)
    )
