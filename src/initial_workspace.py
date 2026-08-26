"""Per-instance Initial Workspace Template — clone, validate, zip.

Mirrors ``src/marketplace.py`` in shape (shallow clone, fetch+reset on
re-sync, token redaction, threading lock) but is a SINGLETON: there is at
most one initial-workspace template per Agnes instance. Configuration
lives in ``instance.yaml`` under ``initial_workspace:`` rather than the
DB (read by ``app/api/initial_workspace.py``, not by this module).

This module has no HTTP surface. Callers (server-side template-repo
management — clone, validate, build_zip, sync_template):
  - ``app/api/initial_workspace.py::sync_endpoint`` (admin "Sync now" click)
  - ``app/api/initial_workspace.py::status_endpoint`` (analyst CLI status probe)
  - ``app/api/initial_workspace.py::zip_endpoint`` (analyst CLI content fetch)

Pure workspace-init helpers (no typer / CLI dependencies) are appended
below ``delete_template_dir``. These are callable from both the CLI
(``cli/lib/initial_workspace.py`` wraps them) and the server-side chat
manager when hydrating per-user workdirs.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import threading
import zipfile

import duckdb
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.utils import get_initial_workspace_dir
from src.marketplace import _credential_args, _git_env, _redact, _scrub_credentialed_remote

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SEC = 300

# Two admins clicking "Sync now" simultaneously would otherwise race on the
# same working directory (one clone in progress, the other tries to fetch
# from a half-cloned target). Process-local lock is enough because Agnes
# is the sole writer to ``${DATA_DIR}/initial-workspace/``.
_sync_lock = threading.Lock()

# Convention: only the contents of ``<repo-root>/workspace/`` get shipped
# to the analyst's local workspace. Anything else in the repo (README.md,
# CI config, docs, scripts for the admin team) lives at the repo root and
# is INVISIBLE to Agnes. This split keeps the repo usable both as a
# normal codebase (with its own README + CI) and as a workspace template.
_WORKSPACE_SUBDIR = "workspace"

# Paths Agnes reserves and refuses to extract from an admin's template
# repo. Stored RELATIVE TO ``<repo-root>/workspace/`` — so
# ``.claude/init-complete`` here means ``workspace/.claude/init-complete``
# in the admin's repo.
#
# ``.claude/init-complete`` is the sentinel the CLI writes at the end of
# ``agnes init`` (and reads on subsequent runs to decide
# resume-vs-refuse semantics). If admin's repo shipped this file, the
# extraction would clobber Agnes's completion-tracking write, breaking
# override-workspace detection.
#
# Surface the rejection at SYNC time (admin sees it in the Sync-now
# modal) rather than at extract time (analyst sees a confusing failure
# half-way through ``agnes init``). The repo on disk stays unchanged —
# admin must commit + push a fix.
_RESERVED_PATHS: tuple[str, ...] = (".claude/init-complete",)


class TemplateValidationError(ValueError):
    """Raised by ``validate_template_tree`` on a structurally unsafe or
    Agnes-reserved entry. Surfaces in the Sync-now modal so the admin
    sees the specific path that's wrong.
    """


def _run_git(
    args: List[str],
    cwd: Optional[Path] = None,
    *,
    url: Optional[str] = None,
    token: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run git with the PAT supplied via the environment, never on argv.

    Same contract as ``src.marketplace._run_git`` and sharing its helpers — this
    module syncs the initial-workspace template repo, which takes a PAT the same
    way the marketplace sync does (2026-08-05 audit, F-2).
    """
    env = _git_env(token)
    args = [*_credential_args(url or "", token), *args]
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SEC,
        check=True,
    )


def validate_template_tree(root: Path) -> None:
    """Walk ``root/workspace/`` and reject paths Agnes refuses to ship.

    Called from two places:
      1. ``sync_template`` after a successful clone — surfaces validation
         errors to the admin in the Sync-now modal response.
      2. ``build_zip`` — second-layer defense in case a path snuck in
         between sync and zip-build (shouldn't happen, but cheap to
         re-check).

    Strict pre-check: ``<root>/workspace/`` MUST exist. The repo root
    itself is admin's territory (README, CI configs, etc.) — only the
    ``workspace/`` subdir reaches the analyst. Repos that don't follow
    this convention are rejected so analysts never receive admin-only
    files by accident.

    Within the ``workspace/`` subtree, rejects:
      * Symlinks anywhere in the tree (analyst-side extraction following
        symlinks would let admin's repo escape the workspace dir).
      * ``..`` segments (defense in depth; ``git clone`` shouldn't produce
        these, but a manually-edited working dir could).
      * Absolute paths (same rationale).
      * Agnes-reserved paths in ``_RESERVED_PATHS`` (currently
        ``.claude/init-complete``, i.e. ``workspace/.claude/init-complete``
        in the repo).

    Raises ``TemplateValidationError`` with the offending path and the
    reason. The error message must NOT leak any token-containing string.
    """
    if not root.exists():
        return
    workspace_dir = root / _WORKSPACE_SUBDIR
    if not workspace_dir.is_dir():
        raise TemplateValidationError(
            f"Repository must contain a {_WORKSPACE_SUBDIR!r} directory at root; "
            "its contents are what gets shipped to analyst workspaces. "
            "Files outside `workspace/` (README, CI configs, etc.) stay in "
            "the repo and are NOT delivered to analysts."
        )
    for entry in workspace_dir.rglob("*"):
        if ".git" in entry.relative_to(workspace_dir).parts:
            # Defensive — admin should never ship a nested `.git/` inside
            # `workspace/`, but ignore if they do (git plumbing of any
            # nested submodule shouldn't reach the analyst).
            continue
        if entry.is_symlink():
            rel = entry.relative_to(workspace_dir).as_posix()
            raise TemplateValidationError(f"symlinks are not allowed in the template repo: {_WORKSPACE_SUBDIR}/{rel}")
        rel_posix = entry.relative_to(workspace_dir).as_posix()
        if ".." in rel_posix.split("/"):
            raise TemplateValidationError(f"path contains '..' segment: {_WORKSPACE_SUBDIR}/{rel_posix}")
        if entry.is_absolute() and not str(entry).startswith(str(workspace_dir)):
            raise TemplateValidationError(f"path escapes template root: {_WORKSPACE_SUBDIR}/{rel_posix}")
        if rel_posix in _RESERVED_PATHS:
            raise TemplateValidationError(
                f"path {_WORKSPACE_SUBDIR}/{rel_posix} is reserved by Agnes "
                "(written by `agnes init` after a successful run). "
                "Remove it from your template repo."
            )


def sync_template(
    url: str,
    branch: Optional[str] = None,
    token_env: Optional[str] = None,
) -> dict:
    """Shallow-clone (first run) or fetch+reset (subsequent runs) the
    template repo into ``${DATA_DIR}/initial-workspace/``.

    Returns ``{commit_sha, path, file_count}``. Raises ``RuntimeError`` on
    git failure (token-redacted) or ``TemplateValidationError`` when the
    cloned tree contains an Agnes-reserved or structurally unsafe path.

    Serialized via the module-level ``_sync_lock`` so two parallel admin
    clicks don't race the working directory.
    """
    if not url:
        raise ValueError("initial-workspace template: url is required")

    token = os.environ.get(token_env, "") if token_env else ""
    target = get_initial_workspace_dir()
    is_git = (target / ".git").is_dir()
    action = "update" if is_git else "clone"

    with _sync_lock:
        # F-2 upgrade path: clean a credentialed remote written by a pre-fix
        # Agnes before doing anything else with this checkout.
        if is_git:
            _scrub_credentialed_remote(target, url)
        try:
            if not is_git:
                if target.exists():
                    shutil.rmtree(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                clone_args = ["clone", "--depth", "1"]
                if branch:
                    clone_args += ["--branch", branch]
                clone_args += [url, str(target)]
                _run_git(clone_args, url=url, token=token)
            else:
                ref = branch or "HEAD"
                _run_git(["fetch", "--depth", "1", "origin", ref], cwd=target, url=url, token=token)
                _run_git(["reset", "--hard", "FETCH_HEAD"], cwd=target)
            sha = _run_git(["rev-parse", "HEAD"], cwd=target).stdout.strip()
        except subprocess.CalledProcessError as e:
            stderr = _redact(e.stderr or "", token).strip()
            raise RuntimeError(f"git {action} failed: {stderr}") from None
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"git {action} timed out after {GIT_TIMEOUT_SEC}s") from None

        # Run the strict validator AFTER the working tree is settled.
        # A failure here leaves the working dir on disk so the admin can
        # inspect with `git -C ${DATA_DIR}/initial-workspace/ log` what
        # got cloned — we don't auto-rollback.
        validate_template_tree(target)

        files = list_template_files()
        logger.info(
            "initial-workspace %s sha=%s files=%d",
            action,
            sha,
            len(files),
        )
        return {"commit_sha": sha, "path": str(target), "file_count": len(files)}


def list_template_files() -> List[str]:
    """Walk ``<initial-workspace>/workspace/``, exclude ``.git/``, return
    sorted POSIX-style relative paths. Deterministic order so
    ``build_zip`` and the status endpoint return stable content / ETag.

    Paths are returned RELATIVE TO the workspace subdir (NOT the repo
    root) — that's the layout the analyst's local workspace will mirror
    after extraction. A repo file at ``workspace/CLAUDE.md`` shows up
    here as ``"CLAUDE.md"``.

    Returns an empty list when the working tree does not exist (i.e.
    template is registered but never synced) OR when the ``workspace/``
    subdir is missing (admin's repo doesn't follow the convention —
    ``sync_template`` would have already failed, this is defense in
    depth for callers that bypass sync).
    """
    target = get_initial_workspace_dir()
    if not target.exists():
        return []
    workspace_dir = target / _WORKSPACE_SUBDIR
    if not workspace_dir.is_dir():
        return []
    out: List[str] = []
    for entry in workspace_dir.rglob("*"):
        if not entry.is_file():
            continue
        rel_parts = entry.relative_to(workspace_dir).parts
        if ".git" in rel_parts:
            continue
        out.append("/".join(rel_parts))
    out.sort()
    return out


def list_iwt_repo_files() -> List[str]:
    """Repo-ROOT-relative file list of the synced IWT clone, for the admin
    bind-git picker (#622 Slice 3).

    Unlike list_template_files() (workspace/-relative, workspace/ subtree
    only), this returns paths the bind-git endpoint accepts verbatim — e.g.
    'workspace/CLAUDE.md', 'install-prompt/template.md.tmpl'. Excludes the
    repo's own .git/ tree and any symlinks (a symlink target could escape the
    clone root; resolve_prompt/_is_within would reject it at read time anyway,
    so never surface an unbindable entry). Empty list when no clone exists /
    IWT not configured.
    """
    iwt_root = _iwt_snapshot()
    if iwt_root is None:
        return []
    out: List[str] = []
    for entry in iwt_root.rglob("*"):
        if entry.is_symlink() or not entry.is_file():
            continue
        rel = entry.relative_to(iwt_root)
        if ".git" in rel.parts:
            continue
        out.append(rel.as_posix())
    out.sort()
    return out


def build_zip(conn=None, *, user=None, server_url=None) -> bytes:
    """Build an in-memory zip of ``<initial-workspace>/workspace/``,
    excluding ``.git/`` and anything outside the ``workspace/`` subdir.
    Re-runs ``validate_template_tree`` first as defense in depth —
    sync_template already validates, but a manual edit on disk between
    sync and zip-fetch should still fail-closed.

    Entry names are relative to ``workspace/`` so the analyst's
    extraction lands files directly at workspace root (e.g.
    ``workspace/CLAUDE.md`` in the repo → ``CLAUDE.md`` at workspace
    root after extraction).

    Admin overlay (#622): when ``conn`` is provided and the workspace prompt
    is in ``source_mode='editor'`` with override content set, the admin's
    edited ``CLAUDE.md`` REPLACES the IWT clone's ``workspace/CLAUDE.md`` in
    the zip. This is THE chokepoint that fixes the issue: override-mode
    ``agnes init`` serves this zip verbatim (it bypasses ``/api/welcome``), so
    without the overlay the admin editor would ship nothing.

    Rendering (#638 review): the override is a Jinja2 template (validated
    StrictUndefined at save time), and the zip path skips the ``/api/welcome``
    render — so when ``user`` + ``server_url`` are supplied (the analyst zip
    endpoint), the overlay is rendered for the requesting user before
    zipping, matching what the non-IWT init path ships. A render failure is
    logged and drops the overlay (pure clone) — never raw template syntax.
    Without ``user`` (the cloud-chat workdir fetch, which re-renders
    ``CLAUDE.md`` itself via ``render_claude_md``) the override ships
    verbatim. On DuckDB, ``conn=None`` (defensive callers, tests,
    ``delete_template_dir`` path) skips the overlay → pure clone. On Postgres
    the overlay is resolved via the repository factory even with ``conn=None``
    (the system DuckDB must never be opened on PG), so a PG caller still gets
    the admin override.

    Returns the zip bytes. Caller computes ``ETag`` from the bytes (or
    from ``last_commit_sha`` for a cheaper stable identifier).
    """
    target = get_initial_workspace_dir()
    validate_template_tree(target)

    workspace_overlay: Optional[str] = None
    # On Postgres the overlay is resolved through the repository factory
    # (resolve_prompt / build_claude_md_context both honor use_pg() with
    # conn=None), so the overlay must run even though no system-DuckDB conn is
    # passed — opening one on PG is forbidden. On DuckDB a conn is required.
    from src.repositories import use_pg as _use_pg

    if conn is not None or _use_pg():
        try:
            content, mode = resolve_prompt("workspace", conn)
            if mode == "editor" and content is not None:
                workspace_overlay = content
                if user is not None and server_url is not None:
                    from src.claude_md import build_claude_md_context
                    from src.prompt_render import make_prompt_env

                    # F4: sandboxed — 'workspace' override is admin-authored.
                    env = make_prompt_env()
                    workspace_overlay = env.from_string(content).render(
                        **build_claude_md_context(conn, user=user, server_url=server_url)
                    )
        except Exception:
            # An overlay failure must NEVER block serving the clone — the
            # zip is on the analyst's init critical path. Drop the overlay
            # entirely rather than ship raw Jinja syntax.
            workspace_overlay = None
            logger.exception("build_zip: workspace-prompt overlay failed")

    workspace_dir = target / _WORKSPACE_SUBDIR
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Single enumeration — the guard below must see the same snapshot
        # the loop wrote, or a file appearing between two walks could leave
        # the zip without ANY CLAUDE.md (loop skipped it, guard saw it).
        files = list_template_files()
        for rel in files:
            if rel == "CLAUDE.md" and workspace_overlay is not None:
                zf.writestr("CLAUDE.md", workspace_overlay)
            else:
                zf.write(workspace_dir / rel, arcname=rel)
        # If the clone has no CLAUDE.md at all but the admin set an editor
        # override, still ship it — otherwise override-mode init would get
        # no prompt despite an admin having authored one.
        if workspace_overlay is not None and "CLAUDE.md" not in files:
            zf.writestr("CLAUDE.md", workspace_overlay)
    return buf.getvalue()


def delete_template_dir() -> bool:
    """Remove the working copy at ``${DATA_DIR}/initial-workspace/``.
    Returns True iff the directory existed and was removed.
    """
    target = get_initial_workspace_dir()
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


# ---------------------------------------------------------------------------
# Seed-file resolution — IWT clone (operator) > bundled snapshot (wheel)
#
# Used by:
#   - ``app/web/setup_instructions.py`` to load the install-prompt template
#   - ``src/connectors_manifest.py`` to enumerate connector-* SKILL.md files
#   - ``app/api/claude_md.py`` + ``app/api/welcome.py`` to gate admin editors
#     when the operator's seed owns the corresponding template
#
# Rule: operator IWT clone ALWAYS beats the bundled snapshot. The bundle is
# the parity-preserving fallback so fresh installs (no IWT configured) still
# render an install prompt and ship the canonical connectors.
# ---------------------------------------------------------------------------

_BUNDLED_SEED_DIR = Path(__file__).resolve().parent / "_bundled_seed"


def bundled_seed_path() -> Path:
    """Return the on-disk path to the bundled seed snapshot shipped inside
    the Agnes wheel (``src/_bundled_seed/``). Treat as read-only.
    """
    return _BUNDLED_SEED_DIR


def is_configured() -> bool:
    """True iff an admin has registered an Initial Workspace Template URL
    in ``instance.yaml`` (operator-side switch). Filesystem presence of a
    clone is NOT enough — an admin can unset the URL while the working
    copy lingers, and that "unset" state must beat a stale clone.
    """
    # Lazy import to avoid pulling app.api into src module load time.
    from app.api.initial_workspace import _read_section

    return bool(_read_section().get("url"))


def _iwt_snapshot() -> Optional[Path]:
    """Single-call atomic read of the IWT state. Returns the on-disk
    clone path iff (a) ``instance.yaml`` currently reports IWT
    configured AND (b) the clone directory exists at that moment.
    Returns ``None`` otherwise.

    Why a single helper: ``seed_owns``, ``resolve_seed_file``, and
    ``list_seed_files`` each used to do a 2-step probe (``is_configured``
    then a separate ``.is_file()``/``.is_dir()``). If an admin clicked
    "unset URL" between the two steps, the answer was inconsistent —
    yes-then-no or no-then-yes — and a downstream editor could land in
    a state that contradicts the YAML's source of truth. Funneling
    both probes through one helper collapses the inconsistency window
    to the gap between this helper's two stat calls (microseconds),
    and the answer is then re-used consistently by the caller without
    a second YAML read mid-function.
    """
    if not is_configured():
        return None
    iwt_root = get_initial_workspace_dir()
    if not iwt_root.is_dir():
        return None
    return iwt_root


def _is_within(root: Path, candidate: Path) -> bool:
    """True iff ``candidate`` resolves to a path inside ``root``.

    Guards against ``..``/symlink traversal in a caller-supplied ``rel_path``
    (#622): an admin binding a prompt to a git path like ``../../secrets/.env``
    must not let ``resolve_seed_file`` read a file outside the seed root and
    serve it into the workspace zip. ``.resolve()`` collapses ``..`` and
    follows symlinks before the containment check.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def blob_sha(rel_path: str) -> Optional[str]:
    """Return the git blob sha of ``rel_path`` in the IWT clone HEAD, or None.

    Per-file precision for divergence detection (#622 Slice 2): the blob sha
    changes iff the file's *content* changes, independent of unrelated commits
    landing in the IWT repo. ``rel_path`` is repo-relative (e.g.
    ``workspace/CLAUDE.md``, ``install-prompt/template.md.tmpl``).

    Returns None when no IWT clone exists, the path is absent from HEAD, the
    path escapes the clone root, or git errors — best-effort, because
    divergence is a UI hint and must never be a hard failure.

    Uses ``git rev-parse HEAD:<path>`` (the canonical committed-tree blob sha,
    works on the shallow ``--depth 1`` clone ``sync_template`` produces) rather
    than ``git hash-object`` on the working-tree file — avoiding working-tree-
    dirty edge cases.
    """
    iwt_root = _iwt_snapshot()
    if iwt_root is None:
        return None
    target = iwt_root / rel_path
    if not _is_within(iwt_root, target):
        return None
    try:
        out = _run_git(["rev-parse", f"HEAD:{rel_path}"], cwd=iwt_root)
        return out.stdout.strip() or None
    except Exception:
        return None


def resolve_seed_file(rel_path: str) -> Optional[tuple[str, str]]:
    """Look up a seed file by repo-relative path.

    ``rel_path`` is the path inside the seed repo root — e.g.
    ``install-prompt/template.md.tmpl`` or
    ``workspace/.claude/skills/connector-asana/SKILL.md``.

    Returns ``(content, source)`` where ``source`` is one of:
      * ``"iwt"`` — operator-configured Initial Workspace Template clone
      * ``"bundled"`` — bundled snapshot inside the wheel

    Returns ``None`` when neither tier has the file. Read errors propagate
    (a corrupt clone is an operator failure that should surface, not be
    silently masked by the bundle).
    """
    iwt_root = _iwt_snapshot()
    if iwt_root is not None:
        iwt_path = iwt_root / rel_path
        if _is_within(iwt_root, iwt_path) and iwt_path.is_file():
            return (iwt_path.read_text(encoding="utf-8"), "iwt")

    bundled_path = _BUNDLED_SEED_DIR / rel_path
    if _is_within(_BUNDLED_SEED_DIR, bundled_path) and bundled_path.is_file():
        return (bundled_path.read_text(encoding="utf-8"), "bundled")

    return None


def seed_owns(rel_path: str) -> bool:
    """True iff the operator-configured IWT clone has ``rel_path``.

    NOTE (#622): no longer gates the admin editors read-only — the explicit
    ``instance_templates.source_mode`` toggle (Git⇄Editor) replaced that
    implicit lock. Still used by tests and as a low-level probe; the editor
    endpoints consult :func:`resolve_prompt` instead.

    Does NOT consider the bundled snapshot — the bundle is Agnes's own
    fallback, not "operator-owned content".
    """
    iwt_root = _iwt_snapshot()
    if iwt_root is None:
        return False
    return (iwt_root / rel_path).is_file()


# ---------------------------------------------------------------------------
# Managed-prompt resolution (#622) — the single source-mode-aware entry point.
#
# The admin's `source_mode` toggle on `instance_templates` decides which tier
# wins per managed prompt:
#   - 'editor': the DB override (content) wins; None → caller falls back to the
#     bundled/shipped default exactly as before.
#   - 'git':    bind to the IWT clone file at `git_path` (or the prompt's
#     canonical seed path); missing file → (None, 'git'), caller falls back +
#     logs, matching the today's resolve_seed_file None contract.
#
# `kind` vocabulary is the public install/workspace; the DB keys are
# welcome/claude_md. The translation lives in the endpoint layer + the repo
# pickers below so callers only ever pass `kind`.
# ---------------------------------------------------------------------------

# Canonical repo-relative seed path per managed prompt (used when an operator
# binds git mode without naming an explicit path, and by the admin git-path
# validation as the default suggestion).
# Single-brace placeholder that nothing substitutes on the git-bound install
# prompt path (only `{server_url}` and the Jinja `{{ ... }}` context are
# replaced — docs/seed-repo-contract.md §5). Negative lookaround keeps Jinja
# expressions out: `{{today}}` written without spaces would otherwise match
# its inner `{today}` pair. Shared by the sync render dry-run
# (app/api/initial_workspace.py) and the bundled-template guard test
# (tests/test_bundled_seed_install_prompt.py) so the two scans cannot drift.
UNWIRED_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{[a-z][a-z0-9_]*\}(?!\})")

PROMPT_SEED_PATHS = {
    "install": "install-prompt/template.md.tmpl",
    "workspace": "workspace/CLAUDE.md",
}


def _prompt_repo(kind: str, conn=None):
    """Return the repo for a managed prompt ``kind``.

    On the Postgres backend the backend-aware factory ALWAYS wins — FastAPI
    handlers pass ``conn`` from the request-scoped ``_get_db()`` dependency,
    which is a DuckDB connection even when Postgres holds the app state, and
    binding the DuckDB repo to it would read ``instance_templates`` from the
    wrong engine (#638 review: the admin's override silently vanished from
    ``/setup`` on PG deployments). On DuckDB, a supplied ``conn`` binds the repo directly
    so the read sees the SAME connection the caller is using (matters for
    in-flight transactions + the renderer unit tests, which pass an isolated
    conn); without one, the factory resolves the default connection.
    """
    from src.repositories import (
        claude_md_template_repo,
        use_pg,
        welcome_template_repo,
    )

    if not use_pg() and conn is not None and isinstance(conn, duckdb.DuckDBPyConnection):
        if kind == "workspace":
            from src.repositories.claude_md_template import ClaudeMdTemplateRepository

            return ClaudeMdTemplateRepository(conn)
        if kind == "install":
            from src.repositories.welcome_template import WelcomeTemplateRepository

            return WelcomeTemplateRepository(conn)
        raise ValueError(f"unknown managed-prompt kind: {kind!r}")

    if kind == "workspace":
        return claude_md_template_repo()
    if kind == "install":
        return welcome_template_repo()
    raise ValueError(f"unknown managed-prompt kind: {kind!r}")


def resolve_prompt(kind: str, conn=None) -> tuple[Optional[str], str]:
    """Resolve the EFFECTIVE content for a managed prompt, honoring its
    ``source_mode`` toggle.

    ``kind`` is one of ``{'install', 'workspace'}``. Returns
    ``(content, source_mode)``:

      - ``source_mode == 'editor'``: ``(db_override_content, 'editor')``.
        ``content`` is ``None`` when no override is set — the caller then
        falls back to its bundled/shipped default, exactly as before.
      - ``source_mode == 'git'``: read ``git_path`` (or the prompt's canonical
        seed path) from the IWT clone and return ``(file_content, 'git')``.
        A missing file yields ``(None, 'git')`` so the caller logs + falls
        back, matching the ``resolve_seed_file`` None contract.

    ``conn``: when a DuckDB connection is passed it's used directly for the
    read (so the resolver sees the caller's connection); otherwise — and on
    Postgres — the backend-aware factory resolves the active backend.
    """
    meta = _prompt_repo(kind, conn).get_meta()
    mode = meta.get("source_mode") or "editor"

    if mode == "git":
        rel_path = meta.get("git_path") or PROMPT_SEED_PATHS.get(kind)
        if not rel_path:
            return (None, "git")
        iwt_root = _iwt_snapshot()
        if iwt_root is None:
            return (None, "git")
        # Same containment guard as resolve_seed_file — git_path comes from
        # the DB, and bind-time validation is not a substitute for a
        # read-time check (defense in depth against `..`/symlink escapes).
        target = iwt_root / rel_path
        if _is_within(iwt_root, target) and target.is_file():
            return (target.read_text(encoding="utf-8"), "git")
        logger.warning(
            "resolve_prompt(%s): git mode bound to %r but file is absent in the IWT clone — falling back to default",
            kind,
            rel_path,
        )
        return (None, "git")

    # editor mode (default)
    return (meta.get("content"), "editor")


def list_seed_files(rel_dir: str) -> List[Path]:
    """Enumerate seed files under a repo-relative directory, with IWT
    clone winning over the bundle as a whole. Used by the connector
    manifest scan: when IWT has any ``connector-*/`` skill, the bundle's
    connectors are ignored (operator seed is the source of truth).

    **Per-directory all-or-nothing**, by design: an IWT clone that ships
    only Asana hides the bundle's GWS + Atlassian skills from `/home`.
    That's the operator's explicit opt-in (they've taken ownership of
    the connector slate). Contrast with :func:`resolve_seed_file`
    (per-file IWT→bundle fallback) used elsewhere.

    Renderer invariant: callers MUST only ask `_load_connector_body`
    for slugs that came back from `load_manifest` (which uses this
    function). Asking for a bundled slug after an IWT directory take-
    over would hit `resolve_seed_file` and silently mix sources mid-
    prompt, defeating the all-or-nothing contract here.

    Returns absolute paths sorted alphabetically. Empty list when neither
    tier has the directory.
    """
    iwt_root = _iwt_snapshot()
    if iwt_root is not None:
        iwt_dir = iwt_root / rel_dir
        if iwt_dir.is_dir():
            return sorted(p for p in iwt_dir.rglob("*") if p.is_file())

    bundled_dir = _BUNDLED_SEED_DIR / rel_dir
    if bundled_dir.is_dir():
        return sorted(p for p in bundled_dir.rglob("*") if p.is_file())

    return []


# ---------------------------------------------------------------------------
# Pure workspace-init helpers — no typer, no prompts, no CLI dependencies.
# Usable from the CLI wrapper AND from the server-side chat manager.
# ---------------------------------------------------------------------------


@dataclass
class TemplateStatus:
    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    overwritten: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


@dataclass
class UpdateResult:
    """Outcome of :func:`update_workspace_from_template` (the 3-way
    backup-aware re-apply used by ``agnes update-workspace``).

    - ``created``    — files in the template that did not exist on disk.
    - ``updated``    — files the analyst had NOT touched (on-disk content
                       matched the stored baseline) → overwritten in place,
                       no ``.bak`` needed.
    - ``backed_up``  — files the analyst HAD changed (on-disk content
                       differed from the baseline, or no baseline existed)
                       → original copied to ``<name>.bak.<ts>`` first, then
                       overwritten. Each tuple is ``(rel_path, bak_rel_path)``.

    Files present on disk but absent from the template are left untouched
    and are NOT reported here (analyst-local additions survive silently).
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    backed_up: list[tuple[str, str]] = field(default_factory=list)


def _assert_safe_entry(name: str, workspace: Path) -> None:
    """Reject a zip entry name that would escape ``workspace``.

    Raises ``ValueError`` on ``..`` traversal, absolute paths, or an entry
    that resolves outside ``workspace``. Shared by
    :func:`extract_zip_to_workspace` and
    :func:`update_workspace_from_template` so both extraction paths apply
    the identical path-safety contract.
    """
    if name.startswith("/") or ".." in name.split("/"):
        raise ValueError(f"unsafe zip entry: {name!r}")
    target = (workspace / name).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"unsafe zip entry escapes workspace: {name!r}") from exc


def extract_zip_to_workspace(zip_bytes: bytes, workspace: Path) -> ExtractResult:
    """Validate then extract every zip entry into ``workspace``.

    Rejects ``..`` traversal, absolute paths, and entries that resolve
    outside ``workspace`` after resolution. Raises ``ValueError`` with a
    short message if any entry is unsafe (caller decides how to surface).
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            _assert_safe_entry(name, workspace)

        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            target = workspace / name
            (overwritten if target.exists() else created).append(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(65536):
                    dst.write(chunk)

    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))


def write_sentinel(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
    override: bool,
) -> None:
    """Write ``.claude/init-complete`` marking the workspace as initialized."""
    sentinel = workspace / ".claude" / "init-complete"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(
        f"completed_at: {datetime.now(timezone.utc).isoformat()}\n"
        f"agnes_version: {agnes_version}\n"
        f"server_url: {server_url}\n"
        f"override: {'true' if override else 'false'}\n"
        f"template_source: {template_source or ''}\n"
        f"template_sha: {template_sha or ''}\n",
        encoding="utf-8",
    )


def is_override_workspace(workspace: Path) -> bool:
    """True iff ``workspace`` was initialised from an admin-configured
    Initial Workspace Template (the sentinel carries ``override: true``).

    False on missing / unreadable sentinel, on sentinel without an override
    key, and on sentinel with ``override`` set to anything other than
    literal ``true`` (case-insensitive).

    Parser tolerates whitespace variants on either side of the ``:`` so a
    manually-edited sentinel with extra spaces still reads correctly —
    matches the pre-extraction behaviour from ``cli/lib/override.py``.
    """
    sentinel = workspace / ".claude" / "init-complete"
    if not sentinel.exists():
        return False
    try:
        text = sentinel.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip().lower() == "override" and value.strip().lower() == "true":
            return True
    return False


def read_sentinel_server_url(workspace: Path) -> Optional[str]:
    """Read the ``server_url`` recorded in ``.claude/init-complete``.

    Returns ``None`` when the sentinel is missing or unreadable, or when it
    carries no ``server_url`` line (sentinels written before the key existed).
    Same whitespace-tolerant line parsing as :func:`is_override_workspace`.
    """
    sentinel = workspace / ".claude" / "init-complete"
    if not sentinel.exists():
        return None
    try:
        text = sentinel.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip().lower() == "server_url":
            return value.strip()
    return None


def initialize_workspace_from_template(
    workspace: Path,
    template_zip_bytes: bytes,
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
) -> ExtractResult:
    """Extract template zip into ``workspace`` and write the override sentinel."""
    result = extract_zip_to_workspace(template_zip_bytes, workspace)
    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=template_source,
        template_sha=template_sha,
        override=True,
    )
    return result


# ---------------------------------------------------------------------------
# Backup-aware re-apply (`agnes update-workspace`) — pure 3-way diff engine.
#
# The baseline (the zip Agnes last installed) is passed in as bytes; this
# module does not decide WHERE it is stored — that's a client concern owned
# by ``cli/lib/initial_workspace.py`` (kept out of the workspace so it can't
# collide with template content). Here we only compare and write.
# ---------------------------------------------------------------------------


def _zip_entries(zip_bytes: bytes) -> dict[str, bytes]:
    """Read a zip into a ``{relative_name: content}`` map, skipping dir
    entries. Used to build the baseline lookup for the 3-way diff.
    """
    out: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            out[name] = zf.read(name)
    return out


def _unique_bak_path(path: Path) -> Path:
    """Return ``path`` if free, else append ``.1``, ``.2``, … until free.

    Guards the (rare) case of two updates landing in the same UTC second
    on the same file — the timestamp alone would collide and silently
    overwrite the first backup.
    """
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.name}.{i}")
        if not candidate.exists():
            return candidate
        i += 1


# Retention cap for ``<name>.bak.<ts>[.<n>]`` files written next to a workspace
# file (#1476: nothing pruned these before, so they accumulated forever —
# grep found no existing backup-retention precedent elsewhere in the
# codebase, so this is a fresh, conservative pick, not a match to a prior
# convention).
_MAX_BACKUPS_PER_FILE = 3


def _prune_backups(original: Path, *, keep: int = _MAX_BACKUPS_PER_FILE) -> None:
    """Delete all but the ``keep`` most recently created ``<original>.bak.*``
    files next to ``original``.

    Called after every ``.bak`` write (DEFAULT-mode ``CLAUDE.md`` refresh,
    ``agnes init --force``, and the OVERRIDE-mode 3-way merge) so backups
    stay bounded instead of growing one-per-day/one-per-merge forever.

    Filenames sort chronologically for the ``<name>.bak.<UTC timestamp>``
    scheme every caller uses (``YYYYMMDDTHHMMSSZ`` sorts lexicographically =
    chronologically); the rare ``.<n>`` collision suffix from
    ``_unique_bak_path`` sorts immediately after the plain timestamp it
    disambiguates, which is also chronologically correct.
    """
    backups = sorted(original.parent.glob(f"{original.name}.bak.*"), key=lambda p: p.name)
    stale = backups[:-keep] if keep > 0 else backups
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass


@dataclass
class UpdatePlan:
    """Dry classification of a re-apply — what WOULD happen, nothing written.

    ``backed_up`` holds the relative names that would be backed up (the
    ``.bak`` filenames are only decided at apply time, since they depend on
    the timestamp + collision resolution).
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    backed_up: list[str] = field(default_factory=list)


def classify_workspace_update(
    workspace: Path,
    new_zip_bytes: bytes,
    baseline_zip_bytes: Optional[bytes],
) -> UpdatePlan:
    """Compute the 3-way diff plan WITHOUT touching disk.

    Per file in ``new_zip_bytes`` (compared against on-disk state and the
    stored baseline):

    1. Not on disk                     → ``created``.
    2. On disk, identical to template  → no-op (omitted from the plan).
    3. On disk, == baseline            → analyst didn't touch it → ``updated``.
    4. On disk, != baseline / no
       baseline entry                  → analyst changed it → ``backed_up``.

    Shared by :func:`update_workspace_from_template` (decision) and the CLI
    preview/confirmation so both see the identical classification.
    """
    workspace = workspace.resolve()
    new_files = _zip_entries(new_zip_bytes)
    baseline_files = _zip_entries(baseline_zip_bytes) if baseline_zip_bytes else {}

    created: list[str] = []
    updated: list[str] = []
    backed_up: list[str] = []

    for name, new_content in new_files.items():
        target = workspace / name
        if not target.exists():
            created.append(name)
            continue
        try:
            disk_content: Optional[bytes] = target.read_bytes()
        except OSError:
            disk_content = None
        if disk_content == new_content:
            # Already matches the template — nothing to do.
            continue
        baseline_content = baseline_files.get(name)
        if baseline_content is None or disk_content != baseline_content:
            backed_up.append(name)
        else:
            updated.append(name)

    return UpdatePlan(
        created=sorted(created),
        updated=sorted(updated),
        backed_up=sorted(backed_up),
    )


def update_workspace_from_template(
    workspace: Path,
    new_zip_bytes: bytes,
    baseline_zip_bytes: Optional[bytes],
    *,
    agnes_version: str,
    server_url: str,
    template_source: Optional[str],
    template_sha: Optional[str],
) -> UpdateResult:
    """Backup-aware re-apply of the template zip into an existing workspace.

    Classification is delegated to :func:`classify_workspace_update`; this
    function executes the plan: ``created``/``updated`` files are written in
    place, ``backed_up`` files have their on-disk content copied to
    ``<name>.bak.<ts>`` first. Files on disk but absent from the template
    are left untouched. After a clean pass the override sentinel is
    refreshed with the new ``template_sha``. Persisting the new baseline is
    the caller's job (it owns the storage location) — see
    ``cli/lib/initial_workspace.py``.

    Raises ``ValueError`` (via :func:`_assert_safe_entry`) on any unsafe
    entry, before writing anything — caller decides how to surface it.
    """
    workspace = workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    # Validate the whole archive up front so a single unsafe entry aborts
    # before any file (or .bak) is written.
    new_files = _zip_entries(new_zip_bytes)
    for name in new_files:
        _assert_safe_entry(name, workspace)

    plan = classify_workspace_update(workspace, new_zip_bytes, baseline_zip_bytes)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backed_up: list[tuple[str, str]] = []

    # Back up analyst-modified files first (preserve their work), then write.
    for name in plan.backed_up:
        target = workspace / name
        disk_content = target.read_bytes()
        bak = _unique_bak_path(target.with_name(f"{target.name}.bak.{ts}"))
        bak.write_bytes(disk_content)
        backed_up.append((name, bak.relative_to(workspace).as_posix()))
        _prune_backups(target)

    for name in plan.created + plan.updated + plan.backed_up:
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new_files[name])

    # Refresh the sentinel only after a clean pass. The caller persists the
    # new baseline (it owns the storage location, outside the workspace).
    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=template_source,
        template_sha=template_sha,
        override=True,
    )

    return UpdateResult(
        created=list(plan.created),
        updated=list(plan.updated),
        backed_up=sorted(backed_up),
    )


def initialize_default_workspace(
    workspace: Path,
    *,
    agnes_version: str,
    server_url: str,
    bundled_template_dir: Path,
) -> ExtractResult:
    """Copy every file from ``bundled_template_dir`` into ``workspace``
    and write the non-override sentinel.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    overwritten: list[str] = []
    created: list[str] = []
    bundled_template_dir = bundled_template_dir.resolve()

    for src in bundled_template_dir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(bundled_template_dir)
        dst = workspace / rel
        (overwritten if dst.exists() else created).append(str(rel))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    write_sentinel(
        workspace,
        agnes_version=agnes_version,
        server_url=server_url,
        template_source=None,
        template_sha=None,
        override=False,
    )
    return ExtractResult(overwritten=sorted(overwritten), created=sorted(created))
