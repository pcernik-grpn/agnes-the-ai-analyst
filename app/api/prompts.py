"""Unified admin REST surface for managed prompts (#622 Slice 1).

Three managed prompts, addressed by a public ``kind`` vocabulary:

  - ``workspace``       → the analyst workspace ``CLAUDE.md`` (DB key ``claude_md``)
  - ``install``         → the install / setup prompt        (DB key ``welcome``)
  - ``facts-extraction`` → the fact-extraction prompt        (DB key ``facts_extraction``)

``facts-extraction`` (owner requirement 2026-09-01, "promptable") is the
rules half of what
``connectors.sharepoint.facts_extraction`` sends the model for every
document; its other half, the ontology, is not admin text at all and comes
from the semantic-model store. It joins this endpoint rather than growing a
surface of its own precisely because nothing new was needed: the same
``instance_templates`` table, the same routes, one more value in the
``kind`` vocabulary — no new route path, no new editor, no migration. It
differs from its two siblings in exactly two ways, both enforced below:

  - It is **not a Jinja template** and is never rendered through one (it
    has no context to interpolate — the document arrives in the user
    message), so ``_validate_template`` does not run a render over it.
    Nothing about it is an SSTI surface *because* nothing renders it.
  - It is **not git-bindable**: it is not a file in an Initial Workspace
    Template repo and has no seed path, so ``source``/``bind-git`` refuse
    it (409) rather than binding it to something no renderer would read.

Each of the other two prompts has an explicit ``source_mode`` toggle
(``editor`` ⇄ ``git``) that
supersedes the old implicit ``seed_owns()`` read-only lock:

  - ``editor``: the admin's DB override wins at render time (the editor is
    writable even when an Initial Workspace Template repo is registered — the
    production lock-out this issue fixes).
  - ``git``: the prompt binds to a file in the IWT clone; the editor goes
    read-only and the renderer reads the repo file.

These endpoints are admin-only (web UI surface, no analyst CLI/MCP analogue);
they're classified EXEMPT in ``tests/test_documentation_api_triple_surface.py``.
The legacy ``/api/admin/{welcome,workspace-prompt}-template`` endpoints remain
alive (grandfathered) for the old standalone editors.
"""

from __future__ import annotations

import logging
import re

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from jinja2 import TemplateError
from pydantic import BaseModel, Field

from app.auth.access import require_admin
from app.auth.dependencies import _get_db

# Module-level and cheap: `facts_prompt` imports only stdlib and holds the
# built-in default text plus this endpoint's `kind` token for it.
from connectors.sharepoint.facts_prompt import PROMPT_KIND as FACTS_PROMPT_KIND
from connectors.sharepoint.facts_prompt import default_prompt as facts_default_prompt
from src.prompt_render import make_prompt_env

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prompts"])

# Public `kind` → canonical seed path in the Initial Workspace Template
# repo. The DB keys (claude_md / welcome / facts_extraction) stay an
# internal detail of the repos; this is the single translation point per
# the build spec.
_KINDS = {
    "workspace": "workspace/CLAUDE.md",
    "install": "install-prompt/template.md.tmpl",
    # No seed path: this prompt ships as a Python constant in the connector
    # that uses it, not as a workspace file, so there is nothing to bind to.
    FACTS_PROMPT_KIND: "",
}

#: Kinds that can be bound to a file in the IWT clone. Declared as the
#: allowlist rather than derived from an empty seed path, so adding a kind
#: is an explicit decision about git-bindability rather than a side effect.
_GIT_BINDABLE_KINDS = frozenset({"workspace", "install"})


def _repo(kind: str):
    """Backend-aware repo for a managed prompt kind."""
    from src.repositories import claude_md_template_repo, facts_prompt_repo, welcome_template_repo

    if kind == "workspace":
        return claude_md_template_repo()
    if kind == "install":
        return welcome_template_repo()
    if kind == FACTS_PROMPT_KIND:
        # PG-only (A3 ratchet): on a DuckDB-backed instance this raises
        # `RequiresPostgresBackend`, which the app-wide handler translates
        # to a typed 501 — the documented fail-clean posture, never a raw
        # 500, and never a silently-empty override.
        return facts_prompt_repo()
    raise HTTPException(status_code=404, detail={"kind": "unknown_prompt_kind"})


def _require_git_bindable(kind: str) -> None:
    """409 for a kind that has no Initial Workspace Template file to bind.

    Refused BEFORE the repo is touched: the facts-extraction repo has no
    ``set_source_mode``/``bind_git`` at all (see
    ``src/repositories/facts_prompt_pg.py``'s docstring for why a
    silently-accepting stub would be worse), so this is the check that
    turns an impossible action into an explained one.
    """
    if kind in _GIT_BINDABLE_KINDS:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "kind": "prompt_not_git_bindable",
            "hint": (
                "This prompt is not a file in the Initial Workspace Template repo — "
                "it ships as a built-in default and is overridden here in the editor."
            ),
        },
    )


def _validate_kind(kind: str) -> None:
    if kind not in _KINDS:
        raise HTTPException(
            status_code=404,
            detail={
                "kind": "unknown_prompt_kind",
                "hint": f"kind must be one of {sorted(_KINDS)}",
            },
        )


def _live_default(kind: str, conn, *, user: dict, server_url: str) -> str:
    if kind == "workspace":
        from src.claude_md import compute_default_claude_md

        return compute_default_claude_md(conn, user=user, server_url=server_url)
    if kind == FACTS_PROMPT_KIND:
        # A static constant, not a render: this prompt has no per-instance
        # context, which is exactly why it is not a template.
        return facts_default_prompt()
    from src.welcome_template import compute_default_agent_prompt

    return compute_default_agent_prompt(conn, user=user, server_url=server_url)


def _reject_token_placeholder(content: str) -> None:
    """400 when install-prompt content references the retired ``{token}``.

    The renderer stopped substituting `{token}` when the PAT handoff moved
    to /home step 4 (the token is saved to ~/.agnes/token before the prompt
    is generated, and the prompt body must stay token-free). Content still
    carrying the placeholder would emit the literal string `{token}` and
    produce installs that authenticate with garbage — reject at save time
    (editor PUT) and at bind time (git mode) instead of failing every
    analyst.
    """
    if "{token}" not in content:
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "Template invalid: `{token}` is no longer a supported "
            "placeholder — the install prompt must not embed the access "
            "token. The token is saved to ~/.agnes/token by the install "
            "guide's step 4 and read via `agnes init --token-file`; "
            "reference that file path instead."
        ),
    )


# Matches a bare single-brace `{server_url}` — the non-Jinja placeholder
# `compute_default_agent_prompt()`'s output uses, meant to be substituted by
# a later `str.replace("{server_url}", ...)` pass (see
# app/web/setup_instructions.py, _claude_setup_instructions.jinja). An admin
# override is instead rendered through Jinja2 (`env.from_string(content)`),
# which only processes double-brace `{{ }}` syntax — single braces pass
# through completely unprocessed. Excludes `{{server_url}}` (no-space
# double-brace, valid Jinja) via the lookaround so a legitimate override
# isn't rejected.
_BARE_SERVER_URL_RE = re.compile(r"(?<!\{)\{server_url\}(?!\})")


def _reject_bare_server_url_placeholder(content: str) -> None:
    """400 when install-prompt content carries the un-substitutable
    single-brace ``{server_url}``.

    A real install transcript hit this: an admin seeded the override editor
    from the live default (which legitimately uses this placeholder, since
    it's substituted by a later ``str.replace`` pass outside Jinja — see
    ``compute_default_agent_prompt``) and saved it verbatim. Jinja2 only
    processes ``{{ }}`` (and the render context has no top-level
    ``server_url`` anyway — it's ``server.url``, see
    ``src/welcome_template.py::build_context``), so the single-brace
    placeholder survived the render untouched and reached the install agent
    as the literal text ``{server_url}`` — no server to contact, every
    download/onboard step unusable. Reject at save time (editor PUT) and at
    bind time (git mode), mirroring :func:`_reject_token_placeholder`.
    """
    if not _BARE_SERVER_URL_RE.search(content):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "Template invalid: `{server_url}` (single brace) is not "
            "substituted in an editor/git override — the override renders "
            "through Jinja2, which only processes `{{ }}`, and the render "
            "context has no top-level `server_url` anyway (see "
            "src/welcome_template.py::build_context). Use "
            "`{{ server.url }}` instead."
        ),
    )


def _validate_template(kind: str, content: str) -> None:
    """Two-pass Jinja validation matching the legacy editors' contract.

    Reuses the per-kind stub contexts so a save through /api/admin/prompts is
    held to the same bar as the grandfathered /api/admin/*-template editors.

    ``facts-extraction`` is exempt because it is not a template: nothing
    renders it (it is handed to the model as literal system text), so there
    is no context to validate against and no render-time execution to
    sandbox. Validating it as Jinja would REJECT legitimate prompts — a
    rule about `{"id": ...}` JSON output contains braces a Jinja parser
    reads as syntax.
    """
    if kind == FACTS_PROMPT_KIND:
        return
    if kind == "workspace":
        from app.api.claude_md import (
            _VALIDATION_STUB_CONTEXT,
            _VALIDATION_STUB_CONTEXT_ANON,
        )

        anon_msg = (
            "Template fails for non-admin analyst users: {e}. Wrap "
            "user-dependent expressions in an {% if user.is_admin %} guard."
        )
    else:
        from app.api.welcome import (
            _VALIDATION_STUB_CONTEXT,
            _VALIDATION_STUB_CONTEXT_ANON,
        )

        anon_msg = (
            "Template fails for anonymous /setup visitors: {e}. Wrap "
            "user-dependent expressions in an {% if user %} guard — /setup "
            "is publicly accessible."
        )

    if kind == "install":
        _reject_token_placeholder(content)
        _reject_bare_server_url_placeholder(content)

    env = make_prompt_env()  # F4: sandboxed — admin-authored content
    try:
        template = env.from_string(content)
        template.render(**_VALIDATION_STUB_CONTEXT)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    try:
        template.render(**_VALIDATION_STUB_CONTEXT_ANON)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=anon_msg.format(e=e))


class PromptGetResponse(BaseModel):
    kind: str
    source_mode: str
    #: Where the EFFECTIVE prompt comes from — ``builtin`` (no override
    #: stored; ``default`` below is what runs), ``admin`` (the editor
    #: override in ``content``), or ``git`` (bound to an IWT file).
    origin: str = "builtin"
    content: str | None
    git_path: str | None = None
    base_sha: str | None = None
    default: str
    updated_at: str | None = None
    updated_by: str | None = None
    iwt_configured: bool = False
    # --- Slice 2 (#622): per-file blob-sha divergence ---
    diverged: bool = False
    # True iff the bound file's current blob sha != the stored base_sha.
    current_blob_sha: str | None = None
    # Live blob sha of git_path in the IWT clone (None when absent/unbound).


class PromptPutRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


class SourceRequest(BaseModel):
    mode: str = Field(..., pattern="^(editor|git)$")


class BindGitRequest(BaseModel):
    git_path: str = Field(..., min_length=1, max_length=1024)


class PreviewRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=200_000)


def _git_path_exists(kind: str, git_path: str) -> bool:
    """True iff ``git_path`` resolves to a file in the IWT clone.

    Paths are REPO-relative for both kinds — workspace files live under
    ``workspace/`` (bind e.g. ``workspace/CLAUDE.md``), the install prompt
    at repo root — because ``resolve_prompt`` resolves the stored path
    against the repo root. Validated via ``resolve_seed_file`` constrained
    to the ``iwt`` tier (the bundled fallback is not "operator content"
    you can bind to). An earlier revision validated workspace paths against
    ``list_template_files()`` (workspace-RELATIVE names), which let admins
    bind paths ``resolve_prompt`` could never find — and rejected the ones
    it could (#638 review).
    """
    from src.initial_workspace import resolve_seed_file

    resolved = resolve_seed_file(git_path)
    return resolved is not None and resolved[1] == "iwt"


class IwtFilesResponse(BaseModel):
    iwt_configured: bool
    files: list[str]
    suggested: dict[str, str]  # kind -> canonical seed path


# Registered BEFORE the dynamic /{kind} GET so the static `iwt-files` segment
# wins route-matching — otherwise FastAPI binds it to get_prompt(kind="iwt-files")
# and returns 404 unknown_prompt_kind.
@router.get("/api/admin/prompts/iwt-files", response_model=IwtFilesResponse)
async def list_iwt_files(user: dict = Depends(require_admin)):
    """Repo-root-relative bindable file list from the synced IWT clone, for the
    bind-git picker (#622 Slice 3). Returns ``files == []`` (200, not 404) when
    IWT is unconfigured — the UI disables git mode in that case. Every path
    returned is a valid ``bind-git`` input (load-bearing invariant). One
    endpoint for both cards: the file set is the same repo; the front-end
    pre-selects ``suggested[kind]`` per card.

    Admin-web-only (no analyst CLI/MCP analogue) → EXEMPT in the triple-surface
    gate, alongside the other ``/api/admin/prompts/*`` routes.
    """
    from src.initial_workspace import (
        PROMPT_SEED_PATHS,
        is_configured,
        list_iwt_repo_files,
    )

    return IwtFilesResponse(
        iwt_configured=is_configured(),
        files=list_iwt_repo_files(),
        suggested=dict(PROMPT_SEED_PATHS),
    )


@router.get("/api/admin/prompts/{kind}", response_model=PromptGetResponse)
async def get_prompt(
    kind: str,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    from src.initial_workspace import blob_sha, is_configured, resolve_prompt

    meta = _repo(kind).get_meta()
    server_url = str(request.base_url).rstrip("/")
    default = _live_default(kind, conn, user=user, server_url=server_url)

    if meta["source_mode"] == "git":
        git_content, _mode = resolve_prompt(kind, conn)
        content = git_content
    else:
        content = meta["content"]

    # Slice 2 (#622): per-file blob-sha divergence. Meaningful whenever a
    # binding exists (git mode OR a Slice-3 imported-then-edited editor file
    # that keeps its git_path back-reference) — the guard fires on git_path,
    # not source_mode, so editor-mode import-backref divergence flows through
    # here automatically once Slice 3 adds the import action.
    diverged = False
    current_blob = None
    git_path = meta["git_path"]
    if git_path and is_configured():
        current_blob = blob_sha(git_path)
        base = meta["base_sha"]
        # Loud default: a stored base that doesn't match the live blob ->
        # diverged. current_blob None (file removed from the repo) -> diverged.
        if base is not None and current_blob != base:
            diverged = True
        elif base is None and current_blob is not None:
            # Bound but never stamped (legacy / edge) -> diverged so the
            # operator re-reconciles rather than trust a stale bind.
            diverged = True

    # Where the EFFECTIVE prompt comes from, stated rather than inferred by
    # each caller from the content/source_mode pair. The fact-extraction
    # config drawer reads this to say which rules a run used, and the same
    # value travels into the extraction run report as `prompt_origin` (see
    # connectors/sharepoint/facts_prompt.py::resolve_extraction_prompt).
    if meta["source_mode"] == "git":
        origin = "git"
    elif content:
        origin = "admin"
    else:
        origin = "builtin"

    return PromptGetResponse(
        kind=kind,
        origin=origin,
        source_mode=meta["source_mode"],
        content=content,
        git_path=meta["git_path"],
        base_sha=meta["base_sha"],
        default=default,
        updated_at=meta["updated_at"].isoformat() if meta["updated_at"] else None,
        updated_by=meta["updated_by"],
        iwt_configured=is_configured(),
        diverged=diverged,
        current_blob_sha=current_blob,
    )


@router.put("/api/admin/prompts/{kind}")
async def put_prompt(
    kind: str,
    payload: PromptPutRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    repo = _repo(kind)
    if repo.get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": (
                    "Switch to Editor override before saving — this prompt is "
                    "bound to the Initial Workspace Template repo."
                ),
            },
        )
    _validate_template(kind, payload.content)
    repo.set(payload.content, updated_by=user["email"])
    return {"status": "ok"}


@router.delete("/api/admin/prompts/{kind}", status_code=204)
async def reset_prompt(
    kind: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    repo = _repo(kind)
    if repo.get_meta()["source_mode"] == "git":
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "prompt_in_git_mode",
                "hint": "No Editor override to reset while in Git source mode.",
            },
        )
    repo.reset(updated_by=user["email"])
    return Response(status_code=204)


@router.post("/api/admin/prompts/{kind}/source")
async def set_source(
    kind: str,
    payload: SourceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    _require_git_bindable(kind)
    from src.initial_workspace import is_configured

    if payload.mode == "git" and not is_configured():
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "iwt_not_configured",
                "hint": (
                    "Register an Initial Workspace Template repo in "
                    "/admin/server-config before binding a prompt to Git."
                ),
            },
        )
    _repo(kind).set_source_mode(payload.mode, updated_by=user["email"])
    return {"status": "ok", "source_mode": payload.mode}


@router.post("/api/admin/prompts/{kind}/bind-git")
async def bind_git(
    kind: str,
    payload: BindGitRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    _require_git_bindable(kind)
    from src.initial_workspace import is_configured

    if not is_configured():
        raise HTTPException(
            status_code=409,
            detail={
                "kind": "iwt_not_configured",
                "hint": (
                    "Register an Initial Workspace Template repo in "
                    "/admin/server-config before binding a prompt to Git."
                ),
            },
        )
    from src.initial_workspace import resolve_seed_file

    if not _git_path_exists(kind, payload.git_path):
        raise HTTPException(
            status_code=400,
            detail={
                "kind": "git_path_not_found",
                "git_path": payload.git_path,
                "hint": (
                    "Path is not present in the synced Initial Workspace "
                    "Template clone. Paths are repo-relative — workspace "
                    "files live under workspace/ (e.g. workspace/CLAUDE.md). "
                    "Check the path + 'Sync now'."
                ),
            },
        )
    if kind == "install":
        # Git-bound content never passes through _validate_template (PUT is
        # refused with prompt_in_git_mode), so the retired-`{token}` guard
        # and the bare-`{server_url}` guard fire here at bind time; the
        # seed-sync render dry-run covers later syncs moving already-bound
        # content onto a legacy seed.
        resolved_seed = resolve_seed_file(payload.git_path)
        if resolved_seed is not None:
            _reject_token_placeholder(resolved_seed[0])
            _reject_bare_server_url_placeholder(resolved_seed[0])

    # Stamp the per-file git BLOB sha as the binding's base (Slice 2):
    # precise divergence — flips only when THIS file's content changes, not
    # when any unrelated commit lands. (Slice 1 stamped the HEAD commit sha;
    # the divergence comparator treats a stored commit sha that doesn't match
    # the live blob as diverged, the safe loud default for legacy bindings.)
    from src.initial_workspace import blob_sha

    base = blob_sha(payload.git_path)
    _repo(kind).bind_git(payload.git_path, base_sha=base, updated_by=user["email"])
    return {"status": "ok", "source_mode": "git", "git_path": payload.git_path}


@router.post("/api/admin/prompts/{kind}/preview")
async def preview_prompt(
    kind: str,
    payload: PreviewRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _validate_kind(kind)
    if kind == FACTS_PROMPT_KIND:
        # Nothing renders this prompt, so a preview IS the content. Echoed
        # rather than 404'd so one editor UI can call one endpoint for
        # every kind, and rendered through nothing so the preview can never
        # differ from what the model is actually sent.
        return {"content": payload.content}
    server_url = str(request.base_url).rstrip("/")
    env = make_prompt_env()  # F4: sandboxed — admin-authored content
    try:
        template = env.from_string(payload.content)
        if kind == "workspace":
            from src.claude_md import build_claude_md_context

            ctx = build_claude_md_context(conn, user=user, server_url=server_url)
        else:
            from src.welcome_template import build_context

            ctx = build_context(user=user, server_url=server_url)
        rendered = template.render(**ctx)
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=f"Template invalid: {e}")
    if kind == "install":
        # The shipped default still carries the single-brace {server_url}
        # placeholder (substituted at bind time, not through Jinja) — an
        # override that doesn't touch it must still resolve, so the preview
        # matches what a real install prompt would send.
        rendered = rendered.replace("{server_url}", server_url)
    return {"content": rendered}
