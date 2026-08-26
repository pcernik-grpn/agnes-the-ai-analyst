"""Per-instance Initial Workspace Template — admin + analyst endpoints.

Config lives in ``${DATA_DIR}/state/instance.yaml`` under the
``initial_workspace:`` key. Token (PAT) lives in ``.env_overlay`` via
``app.secrets.persist_overlay_token``; YAML stores only the env-var name.

Endpoints:

  GET    /api/admin/initial-workspace          admin: read current config
  POST   /api/admin/initial-workspace          admin: register / edit
  DELETE /api/admin/initial-workspace          admin: remove
  POST   /api/admin/initial-workspace/sync     admin: manual "Sync now"

  GET    /api/initial-workspace                analyst (PAT): status + manifest
  GET    /api/initial-workspace.zip            analyst (PAT): content
  POST   /api/initial-workspace/applied        analyst (PAT): audit event

When admin registers a template, ``agnes init`` (new CLI flow) probes
``GET /api/initial-workspace``; on ``configured: true`` it downloads the
zip + extracts to the analyst's workspace, bypassing the default
Agnes-generated workspace files entirely. See ``docs/initial-workspace-override.md``
for the full responsibility-transfer contract.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user, get_optional_user
from app.secrets import persist_overlay_token
from src.initial_workspace import (
    TemplateValidationError,
    build_zip,
    delete_template_dir,
    list_template_files,
    sync_template,
)

from src.repositories import (
    audit_repo,
)


logger = logging.getLogger(__name__)

# Two routers so the admin path can sit under one prefix and the
# analyst-facing path under another; both are registered in app/main.py.
router = APIRouter(tags=["initial_workspace"])

# Conventional env-var name for the singleton template PAT. Mirrors the
# marketplace pattern (`AGNES_MARKETPLACE_<SLUG>_TOKEN`) so an operator
# poking around ``.env_overlay`` can recognize the line at a glance.
_TOKEN_ENV_NAME = "AGNES_INITIAL_WORKSPACE_TOKEN"


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Upsert payload — fields not provided are left untouched on update.

    ``token = None`` means "leave existing PAT alone".
    ``token = ""``   means "clear PAT".
    ``token = "ghp_..."`` means "set/rotate PAT".

    ``sync_schedule``: optional nightly auto-sync cadence (#622 Slice 3 PR-B).
    ``None`` (field absent) leaves the existing schedule untouched; ``""``
    clears it (disable auto-sync); a non-empty value sets it. Validated against
    the scheduler grammar (``daily HH:MM`` / ``every Nm`` / ``cron …``) so a
    typo can't silently disable the nightly job.
    """

    url: str
    branch: Optional[str] = None
    token: Optional[str] = None
    sync_schedule: Optional[str] = None


class AdminInitialWorkspaceResponse(BaseModel):
    """Admin-facing view; surfaces sync state + has_token (no secret leak)."""

    configured: bool = False
    url: Optional[str] = None
    branch: Optional[str] = None
    has_token: bool = False
    sync_schedule: Optional[str] = None
    last_synced_at: Optional[str] = None
    last_commit_sha: Optional[str] = None
    last_error: Optional[str] = None
    file_count: int = 0


class AnalystInitialWorkspaceResponse(BaseModel):
    """PAT-authed analyst view; what ``agnes init`` consumes."""

    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = []


class AppliedRequest(BaseModel):
    """CLI audit event after the analyst's workspace has been extracted."""

    mode: str  # "force_overwrite" | "fresh_install" | "update"
    template_sha: Optional[str] = None
    files_overwritten: int = 0
    files_created: int = 0


# ---------------------------------------------------------------------------
# YAML overlay read/write helpers
# ---------------------------------------------------------------------------


def _read_section() -> dict:
    """Return ``initial_workspace:`` section from the merged static + overlay
    instance.yaml, or empty dict when the section is absent.
    """
    from app.api.admin import _load_current_instance_yaml

    cfg = _load_current_instance_yaml()
    section = cfg.get("initial_workspace") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def _write_section(patch: dict) -> dict:
    """Deep-merge ``patch`` into the ``initial_workspace:`` overlay section
    and write the file atomically. Returns the resulting section.

    Mirrors ``app/api/admin.py::update_server_config``'s read-modify-write
    sequence but scoped to one section: invalidate cache, read overlay,
    merge, atomic write, invalidate cache again. Serialized by the same
    ``_overlay_write_lock`` so concurrent saves from this endpoint AND
    from /admin/server-config don't race.
    """
    import yaml

    from app.api.admin import _deep_merge, _overlay_write_lock
    from app.instance_config import reset_cache
    from app.secrets import _state_dir

    config_path = _state_dir() / "instance.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with _overlay_write_lock:
        reset_cache()

        # Read existing overlay payload — never the merged static + overlay
        # view. Writing the merged result back would copy static keys (and
        # resolved env-var placeholders) into the overlay, shadowing future
        # updates to the static file. Same rationale as the marketplace +
        # server-config write paths.
        overlay_payload: dict[str, Any] = {}
        if config_path.exists():
            try:
                overlay_payload = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                logger.exception(
                    "initial-workspace: refusing to overwrite corrupt overlay at %s",
                    config_path,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                        "back up and remove the file, or fix it by hand"
                    ),
                ) from e

        existing = overlay_payload.get("initial_workspace")
        if not isinstance(existing, dict):
            existing = {}
        merged = _deep_merge(existing, patch)
        overlay_payload["initial_workspace"] = merged

        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False))
        # 0600 on the TEMP file, before the rename — see the server-config
        # editor in app/api/admin.py. `write_text` creates the temp at the
        # process umask and `os.replace` carries that mode onto the
        # destination, so without this a save here would hand the whole
        # overlay — database url with its password inline, connector
        # credentials — back to every uid on the shared data volume, undoing
        # the lockdown for the file's entire lifetime rather than for an
        # instant.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
        logger.info(
            "initial-workspace: wrote `initial_workspace:` section to %s",
            config_path,
        )

        reset_cache()
        return merged


def _drop_section() -> bool:
    """Remove the ``initial_workspace:`` section from the overlay file.
    Returns True iff a section was present and removed.
    """
    import yaml

    from app.api.admin import _overlay_write_lock
    from app.instance_config import reset_cache
    from app.secrets import _state_dir

    config_path = _state_dir() / "instance.yaml"
    if not config_path.exists():
        return False

    with _overlay_write_lock:
        reset_cache()
        try:
            overlay_payload = yaml.safe_load(config_path.read_text()) or {}
        except Exception as e:
            logger.exception(
                "initial-workspace: refusing to overwrite corrupt overlay at %s",
                config_path,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                    "back up and remove the file, or fix it by hand"
                ),
            ) from e
        if "initial_workspace" not in overlay_payload:
            return False
        overlay_payload.pop("initial_workspace", None)
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False))
        # 0600 before the rename — see `_write_section` above. Clearing the
        # section rewrites the whole overlay, so this path relaxes the mode
        # just as thoroughly as saving one.
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, config_path)
        reset_cache()
        return True


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: Optional[str],
    action: str,
    params: Optional[dict] = None,
) -> None:
    """Same shape as ``app/api/marketplaces.py::_audit``. Best-effort —
    audit failure must never abort the actual operation.
    """
    try:
        safe_params: Optional[dict] = None
        if params:
            safe_params = {}
            for k, v in params.items():
                if isinstance(v, datetime):
                    safe_params[k] = v.isoformat()
                else:
                    safe_params[k] = v
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource="initial_workspace",
            params=safe_params,
        )
    except Exception:
        logger.exception("audit log write failed for %s", action)


def _section_to_admin_response(section: dict, file_count: int = 0) -> AdminInitialWorkspaceResponse:
    if not section.get("url"):
        return AdminInitialWorkspaceResponse(configured=False)
    token_env = section.get("token_env") or ""
    has_token = bool(token_env) and bool(os.environ.get(token_env, ""))
    return AdminInitialWorkspaceResponse(
        configured=True,
        url=section.get("url"),
        branch=section.get("branch"),
        has_token=has_token,
        # Normalize the cleared marker ("" in the overlay — distinct from a
        # null/absent key so the scheduler can tell "disabled" from "never
        # configured") back to null for the API: externally, empty == disabled.
        sync_schedule=section.get("sync_schedule") or None,
        last_synced_at=section.get("last_synced_at"),
        last_commit_sha=section.get("last_commit_sha"),
        last_error=section.get("last_error"),
        file_count=file_count,
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/admin/initial-workspace",
    response_model=AdminInitialWorkspaceResponse,
)
async def admin_get(
    user: dict = Depends(require_admin),
):
    """Return the current ``initial_workspace:`` config + sync state."""
    section = _read_section()
    file_count = len(list_template_files()) if section.get("last_commit_sha") else 0
    return _section_to_admin_response(section, file_count=file_count)


@router.post(
    "/api/admin/initial-workspace",
    response_model=AdminInitialWorkspaceResponse,
)
async def admin_post(
    body: RegisterRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register / update the template repo.

    Only ``url``, ``branch``, and ``token_env`` land in the YAML overlay.
    Sync state (``last_synced_at`` / ``last_commit_sha`` / ``last_error``)
    is written by ``POST .../sync``, not here — saving a config change
    should NOT silently invalidate the existing sync state.
    """
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")
    if not url.startswith("https://"):
        # Match the marketplace contract: HTTPS only. file://, ssh://, http://
        # are rejected outright at this layer rather than letting `git clone`
        # surface a less-clear failure later.
        raise HTTPException(
            status_code=422,
            detail="url must be https://",
        )
    # SSRF guard (audit L2): the URL is git-cloned server-side, so reject hosts
    # resolving to a private/reserved network, like the other admin URL fields.
    from app.api.admin import _validate_url_not_private

    _validate_url_not_private(url, field_name="url")

    patch: dict[str, Any] = {
        "url": url,
        "branch": (body.branch or "").strip() or None,
    }

    # sync_schedule routing — three-state like token (#622 Slice 3 PR-B):
    #   None  → field absent, leave existing schedule untouched
    #   ""    → clear (disable auto-sync)
    #   "…"   → set, but only after validating against the scheduler grammar
    #           so a typo can't silently disable the nightly job.
    #
    # The clear writes an explicit empty string (NOT None/null): the scheduler
    # reads the YAML through get_value, which collapses both a null value and an
    # absent key to its default — so a null would be indistinguishable from
    # "never configured" and would silently fall back to the daily default. An
    # explicit "" survives that read and lets _iw_sync_schedule() disable the
    # nightly job, honoring the documented "leave empty to disable" contract.
    if body.sync_schedule is not None:
        sched = body.sync_schedule.strip()
        if sched == "":
            patch["sync_schedule"] = ""
        else:
            from src.scheduler import is_valid_schedule

            if not is_valid_schedule(sched):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "sync_schedule must be a valid scheduler cadence: "
                        "'daily HH:MM' (UTC), 'every Nm'/'every Nh', or "
                        "'cron <5-field expr>'"
                    ),
                )
            patch["sync_schedule"] = sched

    # Token routing — same three-state semantics as the marketplace POST:
    # None = leave alone, "" = clear, non-empty = rotate.
    token_changed: Optional[str] = None
    if body.token is not None:
        if body.token == "":
            persist_overlay_token(_TOKEN_ENV_NAME, None)
            patch["token_env"] = None
            token_changed = "cleared"
        else:
            persist_overlay_token(_TOKEN_ENV_NAME, body.token)
            patch["token_env"] = _TOKEN_ENV_NAME
            token_changed = "rotated"

    merged = _write_section(patch)

    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.register",
        params={
            "url": url,
            "branch": patch.get("branch"),
            "token": token_changed,
        },
    )

    file_count = len(list_template_files()) if merged.get("last_commit_sha") else 0
    return _section_to_admin_response(merged, file_count=file_count)


@router.delete("/api/admin/initial-workspace", status_code=204)
async def admin_delete(
    purge: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove the ``initial_workspace:`` config + PAT.

    Optional ``?purge=true`` also wipes ``${DATA_DIR}/initial-workspace/``
    from disk. Default leaves the working copy in place so an admin can
    inspect / re-register the same URL without re-cloning.
    """
    section = _read_section()
    had_section = _drop_section()
    if section.get("token_env"):
        persist_overlay_token(section["token_env"], None)
    purged = False
    if purge:
        purged = delete_template_dir()

    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.delete",
        params={"purge": purge, "purged": purged, "had_section": had_section},
    )
    return Response(status_code=204)


@router.post("/api/admin/initial-workspace/sync")
async def admin_sync(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Manual "Sync now" — clone or fast-forward the template repo, then
    persist ``last_synced_at`` + ``last_commit_sha`` (or ``last_error``)
    back to the YAML overlay.

    Returns ``{action, commit_sha, file_count, path}`` on success or
    surfaces a ``400`` with the validation / git error so the admin sees
    it in the Sync-now modal. The error payload uses the typed-``kind``
    shape the CLI's error renderer already understands.

    Manual sync errors loudly (``400 not_configured``) when no repo is
    registered — that's intentional UX for a button click. The nightly
    scheduler uses ``/sync-if-configured`` instead, which short-circuits
    silently.
    """
    section = _read_section()
    if not section.get("url"):
        raise HTTPException(
            status_code=400,
            detail={"kind": "not_configured", "hint": "Register a repo first"},
        )
    # _do_sync runs blocking git clone/fast-forward; offload it so the async
    # handler doesn't stall the event loop for the duration of the round-trip.
    return await run_in_threadpool(_do_sync, conn, user.get("id"))


@router.post("/api/admin/initial-workspace/sync-if-configured")
async def admin_sync_if_configured(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Scheduler-facing nightly auto-sync wrapper (#622 Slice 3 PR-B).

    Short-circuits to ``200 {"skipped": true, "reason": "not_configured"}``
    when no template repo is registered, so the nightly job never logs a
    warning on instances without an IWT. When configured, delegates to the
    same ``_do_sync`` logic the manual route uses — which still propagates a
    genuine clone/fast-forward failure as ``400 {"kind": "git_failed"}``.
    That is intentional: a git failure on a *configured* IWT should be loud,
    and the scheduler logs the non-2xx as a warning without hard-failing the
    job.

    Mirrors the Jira jobs' "endpoint self-gates, scheduler stays dumb"
    idiom — the scheduler tuple is a one-liner with no conditional logic.

    Admin-web/scheduler-only (no analyst CLI/MCP analogue) → EXEMPT in the
    triple-surface gate, alongside the other ``/api/admin/initial-workspace/*``
    routes.
    """
    section = _read_section()
    if not section.get("url"):
        return {"skipped": True, "reason": "not_configured"}
    # Offload the blocking git sync off the event loop (see admin_sync).
    return await run_in_threadpool(_do_sync, conn, user.get("id"))


def _do_sync(conn: duckdb.DuckDBPyConnection, user_id: Optional[str]) -> dict:
    """Clone / fast-forward the registered template repo and persist sync
    state. Shared by the manual ``/sync`` route and the nightly
    ``/sync-if-configured`` wrapper.

    Callers MUST ensure a repo is registered (``_read_section()['url']``
    truthy) before calling — this helper assumes it and reads the section
    fresh. Raises ``HTTPException`` (400 with a typed ``kind``) on
    validation / git failure, persisting ``last_error`` first.
    """
    section = _read_section()
    try:
        result = sync_template(
            url=section["url"],
            branch=section.get("branch"),
            token_env=section.get("token_env"),
        )
    except TemplateValidationError as e:
        # Persist the error so the admin UI can render it on next page
        # load (the modal-result path also displays it inline). The repo
        # on disk stays as cloned so the admin can inspect what's there.
        _write_section({"last_error": str(e)})
        _audit(
            conn,
            actor_id=user_id,
            action="initial_workspace.sync_failed",
            params={"error": str(e), "kind": "validation"},
        )
        raise HTTPException(
            status_code=400,
            detail={"kind": "template_invalid", "message": str(e)},
        ) from None
    except (RuntimeError, ValueError) as e:
        _write_section({"last_error": str(e)})
        _audit(
            conn,
            actor_id=user_id,
            action="initial_workspace.sync_failed",
            params={"error": str(e), "kind": "git"},
        )
        raise HTTPException(
            status_code=400,
            detail={"kind": "git_failed", "message": str(e)},
        ) from None

    now_iso = datetime.now(timezone.utc).isoformat()
    _write_section(
        {
            "last_synced_at": now_iso,
            "last_commit_sha": result["commit_sha"],
            "last_error": None,
        }
    )

    # New seed content landed → connector manifest cache is stale. Drop it
    # so the next render scan picks up renamed/added/removed connectors
    # immediately rather than waiting for process restart.
    try:
        from src.connectors_manifest import invalidate_cache

        invalidate_cache()
    except Exception:
        logger.exception("connectors_manifest: cache invalidation after sync failed")

    # Render dry-run — exercise the install-prompt + manifest path against
    # the freshly-synced clone so the operator sees parse failures
    # immediately, never ships a broken seed silently to analysts. The
    # admin UI red-banners this block when `ok=false`.
    render_dry_run = _compute_render_dry_run()

    # Slice 2 (#622): surface (never silently) which bound prompts the new
    # commit moved. Git-mode prompts re-read the repo file live, so what they
    # SERVE is never stale — but their stored base_sha baseline drifts, and the
    # operator wants to know the bound file moved under them. Best-effort; a
    # probe failure must never block the sync.
    diverged_prompts: list[str] = []
    try:
        from src.initial_workspace import blob_sha
        from src.repositories import claude_md_template_repo, welcome_template_repo

        for kind, repo in (
            ("install", welcome_template_repo()),
            ("workspace", claude_md_template_repo()),
        ):
            m = repo.get_meta()
            gp = m.get("git_path")
            if m.get("source_mode") == "git" and gp:
                if blob_sha(gp) != m.get("base_sha"):
                    diverged_prompts.append(kind)
    except Exception:
        logger.exception("initial-workspace: divergence probe after sync failed")

    _audit(
        conn,
        actor_id=user_id,
        action="initial_workspace.sync",
        params={
            "commit_sha": result["commit_sha"],
            "file_count": result["file_count"],
            "render_ok": render_dry_run.get("ok"),
            "diverged_prompts": diverged_prompts,
        },
    )

    return {
        "action": "sync_ok",
        "commit_sha": result["commit_sha"],
        "file_count": result["file_count"],
        "path": result["path"],
        "synced_at": now_iso,
        "render_dry_run": render_dry_run,
    }


_CANONICAL_INSTALL_TEMPLATE = "install-prompt/template.md.tmpl"


def _install_prompt_bound_git_path() -> Optional[str]:
    """Repo-relative seed path the install prompt actually renders, or
    ``None`` when it renders no seed file (editor mode). ``bind-git``
    accepts any repo-relative path, so the bound file is not always the
    canonical template. Conservative on a meta-read failure: assume the
    canonical path so its `{token}` hit stays a hard error (a false
    block beats silently shipping a token-embedding seed).
    """
    try:
        from src.repositories import welcome_template_repo

        meta = welcome_template_repo().get_meta()
    except Exception:
        logger.exception("render dry-run: install prompt meta read failed")
        return _CANONICAL_INSTALL_TEMPLATE
    if meta.get("source_mode") != "git":
        return None
    return meta.get("git_path") or _CANONICAL_INSTALL_TEMPLATE


def _compute_render_dry_run() -> dict:
    """Validate the freshly-synced seed by exercising the manifest scan +
    install-prompt renderer. Returns a structured summary the admin UI
    surfaces inline.

    Errors block the operator from claiming "seed is good"; warnings
    surface in the modal but don't gate the sync (analysts can still hit
    /home — the renderer skips the bad connector, the rest works).
    """
    summary: dict = {
        "ok": True,
        "scaffolding_source": "bundled",
        "connectors_found": 0,
        "connectors": [],
        "warnings": [],
        "errors": [],
    }

    try:
        from src.connectors_manifest import load_manifest
        from src.initial_workspace import is_configured, resolve_seed_file

        bound_git_path = _install_prompt_bound_git_path()

        # Scaffolding tier — IWT clone first, bundled fallback.
        template = resolve_seed_file(_CANONICAL_INSTALL_TEMPLATE)
        if template is not None:
            content, source = template
            summary["scaffolding_source"] = source
            # Retired-placeholder guard: the editor save path rejects
            # `{token}` (app/api/prompts.py) and bind-git checks the bound
            # file, but a later `Sync now` can move an already-bound seed
            # onto content that embeds it. Rendered literally, the old
            # `{token}` heredoc would write the string `{token}` into every
            # analyst's ~/.agnes/token and 401 every `agnes init`. Only an
            # install prompt git-bound to THIS path renders this file,
            # though — for an editor-mode prompt, a prompt bound to a
            # different path (scanned separately below), or a
            # bundled-fallback hit, a legacy IWT template would hard-error
            # unrelated syncs, so those degrade to a warning (the bind-git
            # flip re-rejects `{token}` anyway).
            if "{token}" in content:
                msg = (
                    "install-prompt/template.md.tmpl references the retired "
                    "`{token}` placeholder — the install prompt must not "
                    "embed the access token (it is saved to ~/.agnes/token "
                    "by the install guide and read via `agnes init "
                    "--token-file`)"
                )
                if source == "iwt" and bound_git_path == _CANONICAL_INSTALL_TEMPLATE:
                    summary["errors"].append(msg)
                    summary["ok"] = False
                else:
                    summary["warnings"].append(
                        msg + " (warning only: the install prompt does not currently render this file)"
                    )
        elif is_configured():
            # IWT configured but neither tier has the file — that's the
            # bundled fallback below, but only if the bundle survived
            # release (a deployer stripping `src/_bundled_seed/` would
            # land here). Surface as error so the admin sees it.
            summary["errors"].append(
                "install-prompt/template.md.tmpl missing from both IWT clone "
                "and bundled seed — install prompt will not render"
            )
            summary["ok"] = False

        # A git-bound install prompt can point at ANY repo-relative path
        # (bind-git doesn't restrict it to the canonical template) — the
        # scan above never inspects such a file even though it is the one
        # analysts actually get. Scan the bound file too, same escalation
        # rule: hard error only for operator-synced (IWT) content.
        if bound_git_path is not None and bound_git_path != _CANONICAL_INSTALL_TEMPLATE:
            bound_file = resolve_seed_file(bound_git_path)
            if bound_file is not None and "{token}" in bound_file[0]:
                msg = (
                    f"{bound_git_path} (git-bound to the install prompt) "
                    "references the retired `{token}` placeholder — the "
                    "install prompt must not embed the access token (it is "
                    "saved to ~/.agnes/token by the install guide and read "
                    "via `agnes init --token-file`)"
                )
                if bound_file[1] == "iwt":
                    summary["errors"].append(msg)
                    summary["ok"] = False
                else:
                    summary["warnings"].append(
                        msg + " (warning only: resolved from the bundled seed, not the operator's synced clone)"
                    )

        # Manifest tier — same resolution, scoped to connector-*/SKILL.md.
        manifest = load_manifest()
        summary["connectors_found"] = len(manifest)
        summary["connectors"] = [e.slug for e in manifest]

        # Body probe — load_manifest() reads only the frontmatter, so an
        # entry whose SKILL.md body `agnes connectors show <slug>` can't
        # resolve still parses. The install prompt renders tiles either
        # way (bodies are fetched on demand, not inlined); this is where
        # the gap surfaces to the operator. A missing REQUIRED body is an
        # error (the mandatory step's fetch will 404), a missing optional
        # body just a warning.
        from src.connectors_manifest import load_connector_body

        for entry in manifest:
            if load_connector_body(entry.slug) is None:
                if entry.required:
                    summary["errors"].append(
                        f"required connector {entry.slug}: SKILL.md body "
                        "missing from the synced seed — the mandatory "
                        "install step's `agnes connectors show` will fail"
                    )
                    summary["ok"] = False
                else:
                    summary["warnings"].append(
                        f"connector {entry.slug}: SKILL.md body missing "
                        "from the synced seed — `agnes connectors show "
                        f"{entry.slug}` will fail until the seed is fixed"
                    )

        # The built-in prompt renderer no longer reads seed content
        # (the thin prompt ignores the connector manifest), so rendering
        # it here would validate nothing about the sync. What a seed CAN
        # break now is a forked install-prompt template: on the git-bound
        # path only `{server_url}` and the Jinja context are substituted
        # (docs/seed-repo-contract.md §5), so a retired or unwired
        # single-brace placeholder renders literally into the analyst's
        # prompt. Surface that to the operator here.
        from src.initial_workspace import (
            PROMPT_SEED_PATHS,
            UNWIRED_PLACEHOLDER_RE,
            resolve_seed_file,
        )

        # Scan the file the prompt is actually bound to when a custom
        # git_path is set (same resolution rule as the `{token}` probe
        # above) — the canonical template matters only when it is the one
        # analysts get.
        scan_path = (
            bound_git_path
            if bound_git_path is not None
            else PROMPT_SEED_PATHS["install"]
        )
        tmpl = resolve_seed_file(scan_path)
        if tmpl is not None:
            tmpl_text, _tmpl_source = tmpl
            unwired = sorted(
                {
                    name
                    for name in UNWIRED_PLACEHOLDER_RE.findall(tmpl_text)
                    if name != "{server_url}"
                }
            )
            if unwired:
                msg = (
                    f"{scan_path} (install prompt) references placeholder(s) "
                    f"{', '.join(unwired)} that nothing substitutes on the "
                    "git-bound prompt path — they will render literally in "
                    "the analyst's prompt (only {server_url} and Jinja "
                    "{{ ... }} context are replaced; see "
                    "docs/seed-repo-contract.md section 5)"
                )
                if bound_git_path is None:
                    # Editor mode: the prompt renders the DB override or the
                    # shipped default, not this seed file — the finding only
                    # matters for a future git binding / fork of the template.
                    msg += (
                        " (warning only: the install prompt does not"
                        " currently render this file)"
                    )
                summary["warnings"].append(msg)

            # A git-bound template is also rendered through the sandboxed
            # Jinja path — and a render error there is SILENT for analysts
            # (`/setup` falls back to the built-in default with only a log
            # line). Validate the render here with the same stub context the
            # welcome-template save endpoint uses, so a broken seed commit
            # surfaces to the operator instead. Editor mode skips this: the
            # seed file is not rendered at all there, and the DB override
            # was already validated at save time.
            # Unlike the `{token}` probe (where a meta-read failure must stay
            # conservative — a false block beats shipping a token-embedding
            # seed), render validation must only fire on a CONFIRMED git
            # binding: `_install_prompt_bound_git_path` falls back to the
            # canonical path on a meta-read failure, and hard-failing the
            # sync over a template an editor-mode instance never renders
            # would turn a transient DB hiccup into a false alarm.
            confirmed_git = False
            try:
                from src.repositories import welcome_template_repo

                confirmed_git = welcome_template_repo().get_meta().get("source_mode") == "git"
            except Exception:  # noqa: BLE001 — meta unreadable → not confirmed
                confirmed_git = False

            if bound_git_path is not None and confirmed_git:
                from app.api.welcome import (
                    _VALIDATION_STUB_CONTEXT,
                    _VALIDATION_STUB_CONTEXT_ANON,
                )
                from src.prompt_render import make_prompt_env

                try:
                    env = make_prompt_env()
                    template = env.from_string(tmpl_text)
                    template.render(**_VALIDATION_STUB_CONTEXT)
                    # /setup is publicly reachable, so the anonymous shape
                    # must render too — with StrictUndefined, `user.email`
                    # without an `{% if user %}` guard fails only here.
                    template.render(**_VALIDATION_STUB_CONTEXT_ANON)
                except Exception as exc:  # noqa: BLE001 — any render failure means silent fallback
                    msg = (
                        f"{scan_path} (git-bound to the install prompt) does "
                        f"not render: {type(exc).__name__}: {exc}. Analysts "
                        "silently get the built-in default prompt instead."
                    )
                    if _tmpl_source == "iwt":
                        summary["errors"].append(msg)
                        summary["ok"] = False
                    else:
                        summary["warnings"].append(
                            msg
                            + " (warning only: resolved from the bundled "
                            "seed, not the operator's synced clone)"
                        )
    except Exception as e:
        summary["ok"] = False
        summary["errors"].append(f"render dry-run raised: {e!r}")
        logger.exception("initial-workspace: render dry-run failed")

    return summary


# ---------------------------------------------------------------------------
# Analyst (PAT-authed) endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/initial-workspace",
    response_model=AnalystInitialWorkspaceResponse,
)
async def analyst_status(
    user: dict = Depends(get_current_user),
):
    """Status probe consumed by ``agnes init``. Always 200.

    Returns ``configured: false`` when no template is registered (CLI then
    falls through to the existing default flow). Returns ``configured:
    true, synced: false`` when registered but never synced (or last sync
    failed); CLI shows a typed error pointing at /admin/server-config.
    Returns full metadata + manifest when configured + synced.
    """
    section = _read_section()
    if not section.get("url"):
        return AnalystInitialWorkspaceResponse(configured=False)
    synced = bool(section.get("last_commit_sha"))
    return AnalystInitialWorkspaceResponse(
        configured=True,
        synced=synced,
        template_source=section.get("url"),
        template_sha=section.get("last_commit_sha"),
        synced_at=section.get("last_synced_at"),
        files=list_template_files() if synced else [],
    )


@router.get("/api/initial-workspace.zip")
async def analyst_zip(
    request: Request,
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the zip of the cloned template tree (sans ``.git/``).

    Writes a server-side ``initial_workspace.fetch_started`` audit row so
    we have an authoritative event the analyst's PAT-holder cannot spoof
    (the matching ``initial_workspace.applied`` event from
    ``POST /applied`` is best-effort).

    404 when not configured (the CLI status probe should have caught
    this; defense in depth). 503 when configured but never synced — the
    CLI then surfaces a typed error pointing at "Sync now".
    """
    if user is None:
        # Browser → redirect to /login (target preserved via ?next=).
        # CLI / curl / API client → raw 401 they can handle.
        # This endpoint is the one `/api/*` URL designed to be hit directly
        # from a browser bookmark (analyst clean-install zip), so it
        # intentionally opts out of the global `_API_PATH_PREFIXES`
        # "never redirect /api/*" contract in `app/main.py`. Matching only
        # `text/html` — NOT `*/*` — mirrors `_wants_html()` in `app/main.py`:
        # `*/*` is curl's default and must keep getting the raw 401 so
        # tooling that parses `{"detail": "..."}` doesn't silently break.
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/login?next=/api/initial-workspace.zip", status_code=302)
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    section = _read_section()
    if not section.get("url"):
        raise HTTPException(status_code=404, detail={"kind": "not_configured"})
    if not section.get("last_commit_sha"):
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "initial_workspace_not_synced",
                "hint": "Admin must Sync now in /admin/server-config",
            },
        )

    try:
        # Pass conn so the workspace-prompt admin overlay (source_mode='editor')
        # replaces the clone's workspace/CLAUDE.md for override-mode init
        # (#622) — rendered for the requesting analyst, since this zip
        # bypasses the /api/welcome render step (#638 review).
        data = build_zip(conn, user=user, server_url=str(request.base_url).rstrip("/"))
    except TemplateValidationError as e:
        # Defense in depth — sync_template already validates, but a
        # manual edit on disk between sync and zip-fetch should fail
        # closed rather than serve invalid content.
        logger.warning("initial-workspace: build_zip validation failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"kind": "template_invalid", "message": str(e)},
        ) from None

    sha = section["last_commit_sha"]
    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.fetch_started",
        params={"template_sha": sha, "byte_count": len(data)},
    )

    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{sha}"',
            "Content-Disposition": 'attachment; filename="initial-workspace.zip"',
        },
    )


@router.post("/api/initial-workspace/applied")
async def analyst_applied(
    body: AppliedRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Best-effort audit event from ``agnes init`` confirming the
    analyst's workspace has been extracted + sentinel written.

    The authoritative anchor is the server-side
    ``initial_workspace.fetch_started`` event written by ``GET .../zip`` —
    a fetch_started without a matching applied = the analyst downloaded
    but never confirmed extraction (useful signal for operators).
    """
    if body.mode not in ("force_overwrite", "fresh_install", "update"):
        raise HTTPException(
            status_code=422,
            detail=(f"mode must be one of: force_overwrite, fresh_install, update (got {body.mode!r})"),
        )
    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.applied",
        params={
            "mode": body.mode,
            "template_sha": body.template_sha,
            "files_overwritten": body.files_overwritten,
            "files_created": body.files_created,
        },
    )
    return {"status": "ok"}
