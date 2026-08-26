"""REST endpoints for the agent-workspace-prompt (analyst CLAUDE.md).

- GET  /api/welcome                              : analyst-facing rendered CLAUDE.md (auth required)
- GET  /api/admin/workspace-prompt-template      : raw template override + live default (admin)
- PUT  /api/admin/workspace-prompt-template      : set override (admin)
- DELETE /api/admin/workspace-prompt-template    : reset to default (admin)
- POST /api/admin/workspace-prompt-template/preview : live preview without persisting (admin)
"""

import datetime
import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from jinja2 import TemplateError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user
from src.claude_md import build_claude_md_context, compute_default_claude_md, render_claude_md
from src.prompt_render import make_prompt_env

from src.repositories import (
    claude_md_template_repo,
)

logger = logging.getLogger(__name__)


router = APIRouter(tags=["claude_md"])

# Stub context used to validate that a saved template renders end-to-end,
# not just that it parses. Mirrors the shape of build_claude_md_context() output.
# user is an authenticated user so templates that reference user.* are validated.
_VALIDATION_STUB_CONTEXT = {
    "instance": {"name": "Example", "subtitle": "Example Org"},
    "server": {"url": "https://example.com", "hostname": "example.com"},
    "sync_interval": "1h",
    "data_source": {"type": "keboola", "source_types": ["keboola"]},
    "tables": [{"name": "orders", "description": "Sample orders", "query_mode": "local", "source_type": "keboola"}],
    "metrics": {"count": 3, "categories": ["revenue", "growth"]},
    "semantic_layer": {"has_models": True},
    "marketplaces": [{"slug": "example", "name": "Example Marketplace", "plugins": [{"name": "plugin-a"}]}],
    "user": {
        "id": "u",
        "email": "user@example.com",
        "name": "User",
        "is_admin": False,
        "groups": ["Everyone"],
    },
    "now": datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
    "today": "2026-01-01",
    # Admin-authored overrides validate against the laptop-workspace shape —
    # the chat sandbox never renders an admin override through this stub.
    "is_sandbox": False,
}

# Same stub with an anonymous-style user context to validate templates against
# the case where a user dict is present but minimal (analyst). The CLAUDE.md
# endpoint always requires auth, so user is never None — but templates may
# accidentally reference fields that aren't in the context.
_VALIDATION_STUB_CONTEXT_ANON = {
    **{k: v for k, v in _VALIDATION_STUB_CONTEXT.items() if k != "user"},
    "user": {
        "id": "u2",
        "email": "anon@example.com",
        "name": "",
        "is_admin": False,
        "groups": ["Everyone"],
    },
}


# Substrings that, when found in an admin-saved CLAUDE.md override, signal
# the override is stale relative to the post-clean-bootstrap CLI surface.
# Surfaced via TemplateGetResponse.legacy_strings_detected so the admin UI
# can render a yellow banner prompting re-authoring.
_LEGACY_STRINGS = (
    "data/parquet",
    "da sync",
    "da fetch",
    "da analyst setup",
    "da metrics list",
    "da metrics show",
)


def _scan_legacy_strings(text: str) -> list[str]:
    """Return sorted unique substrings from _LEGACY_STRINGS present in text."""
    return sorted({s for s in _LEGACY_STRINGS if s in text})


class ClaudeMdResponse(BaseModel):
    content: str


class TemplateGetResponse(BaseModel):
    content: Optional[str]
    default: str  # live default rendered with calling admin's context
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None
    # Substrings from _LEGACY_STRINGS detected in the saved override (if any).
    # Empty when no override is set or when the override is clean. Surfaced
    # so the admin UI can prompt re-authoring after a CLI surface rename.
    legacy_strings_detected: list[str] = []
    # #622: the prompt's source toggle. `"editor"` = DB override editable;
    # `"git"` = bound to the IWT clone file (editor read-only, edit in repo).
    # `source` is retained for backward-compat with the old grandfathered UI
    # ("local"/"seed"); new callers read `source_mode`/`git_path`.
    source: str = "local"
    seed_path: Optional[str] = None
    source_mode: str = "editor"
    git_path: Optional[str] = None


# Path inside the seed repo for the analyst CLAUDE.md template. Shared
# between the GET/PUT/DELETE gate so the admin UI banner names the same
# file the endpoints check.
_SEED_PATH = "workspace/CLAUDE.md"


class TemplatePutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class TemplatePreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


# ---------------------------------------------------------------------------
# Analyst-facing endpoint — returns rendered CLAUDE.md
# ---------------------------------------------------------------------------


@router.get("/api/welcome", response_model=ClaudeMdResponse)
def get_welcome(
    request: Request,
    server_url: Optional[str] = Query(None, description="Server URL used in rendered CLAUDE.md"),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the rendered CLAUDE.md for the authenticated analyst.

    The CLI calls this endpoint during ``agnes init`` to write
    ``<workspace>/CLAUDE.md``. The content is RBAC-filtered per the
    calling user.

    ``server_url`` query param lets the CLI pass the origin it knows so
    the rendered content references the correct server URL rather than the
    request host (which may differ behind a proxy).
    """
    effective_url = server_url or str(request.base_url).rstrip("/")
    try:
        # This is a laptop workspace, not the chat sandbox: is_sandbox=False
        # is the default, but spelled out here because it's the crux of what
        # this endpoint renders differently from app/main.py's sandbox path.
        content = render_claude_md(conn, user=user, server_url=effective_url, is_sandbox=False)
    except TemplateError as exc:
        logger.warning("render_claude_md failed (template error): %s", exc)
        raise HTTPException(status_code=500, detail=f"Template render error: {exc}")
    except Exception:
        logger.exception("render_claude_md failed (unexpected)")
        raise HTTPException(status_code=500, detail="Internal error rendering CLAUDE.md")
    return ClaudeMdResponse(content=content)


# ---------------------------------------------------------------------------
# Admin endpoints — CRUD for the workspace-prompt template override
# ---------------------------------------------------------------------------


@router.get("/api/admin/workspace-prompt-template", response_model=TemplateGetResponse)
def admin_get_workspace_template(
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    server_url = str(request.base_url).rstrip("/")
    # #622: the source toggle replaced the implicit seed_owns() read-only lock.
    # In git mode we surface the bound IWT file content (read-only); in editor
    # mode the DB override is editable even when an IWT repo is registered.
    meta = claude_md_template_repo().get_meta()
    live_default = compute_default_claude_md(conn, user=user, server_url=server_url)

    if meta["source_mode"] == "git":
        from src.initial_workspace import resolve_prompt

        git_content, _mode = resolve_prompt("workspace", conn)
        return TemplateGetResponse(
            content=git_content or "",
            default=live_default,
            updated_at=meta["updated_at"].isoformat() if meta["updated_at"] else None,
            updated_by=meta["updated_by"],
            legacy_strings_detected=[],
            source="seed",
            seed_path=meta["git_path"] or _SEED_PATH,
            source_mode="git",
            git_path=meta["git_path"] or _SEED_PATH,
        )

    legacy_hits = _scan_legacy_strings(meta["content"] or "")
    return TemplateGetResponse(
        content=meta["content"],
        default=live_default,
        updated_at=meta["updated_at"].isoformat() if meta["updated_at"] else None,
        updated_by=meta["updated_by"],
        legacy_strings_detected=legacy_hits,
        source="local",
        source_mode="editor",
    )


@router.put("/api/admin/workspace-prompt-template")
def admin_put_workspace_template(
    payload: TemplatePutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Save an admin override for the analyst CLAUDE.md template.

    Two-pass Jinja2 validation (autoescape=False, StrictUndefined):
    - Pass 1: render with an authenticated user stub — catches undefined
      placeholders and syntax errors.
    - Pass 2: render with a minimal anon-style user stub — catches templates
      that hard-depend on admin-only context fields.
    """
    # #622: the editor is always writable in editor mode (even with an IWT
    # repo registered — that was the production lock-out this fixes). Saving is
    # only refused in git mode, where the DB content is dead-code the renderer
    # would not consult — a confusing "where did my edit go" silent loss.
    if claude_md_template_repo().get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": (
                    "This prompt is in Git source mode (bound to the Initial "
                    "Workspace Template repo). Switch to Editor override in "
                    "/admin/prompts before saving, or edit the bound file in "
                    "the repo + 'Sync now'."
                ),
            },
        )

    env = make_prompt_env()
    try:
        template = env.from_string(payload.content)
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")

    try:
        template.render(**_VALIDATION_STUB_CONTEXT_ANON)
    except TemplateError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Template fails for non-admin analyst users: {e}. "
                "Wrap user-dependent expressions in {{% if user.is_admin %}}...{{% endif %}} "
                "or ensure the template renders correctly for all users."
            ),
        )

    claude_md_template_repo().set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/workspace-prompt-template", status_code=204)
def admin_reset_workspace_template(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    # #622: reset is allowed in editor mode; refused in git mode (no DB
    # override to clear — the renderer reads the bound repo file).
    if claude_md_template_repo().get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": (
                    "This prompt is in Git source mode — there is no Editor "
                    "override to reset. Switch to Editor override in "
                    "/admin/prompts, or edit the bound repo file."
                ),
            },
        )
    claude_md_template_repo().reset(updated_by=user["email"])
    return Response(status_code=204)


@router.post("/api/admin/workspace-prompt-template/preview", response_model=ClaudeMdResponse)
def admin_preview_workspace_template(
    payload: TemplatePreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Render arbitrary template content against the live RBAC context for the
    calling admin, without persisting. Used by the /admin/workspace-prompt editor's
    Preview button so admins can see their edits before saving."""
    env = make_prompt_env()
    try:
        template = env.from_string(payload.content)
        ctx = build_claude_md_context(conn, user=user, server_url=str(request.base_url).rstrip("/"))
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    return ClaudeMdResponse(content=rendered)
