"""Chat session-workspace file endpoints (#1611).

A chat agent's deliverables (a rendered ``.docx``/``.pptx``, a chart, a
report) land in the session's workdir on the Agnes host — with the default
docker sandbox the session dir IS the container's ``/work`` (bind-mounted,
see ``app/chat/docker_provider.py``), and skill output written through the
``.claude`` symlink lands in the caller's per-user workspace. Until now the
web chat had no way to reach any of it (the only channel out was inline SVG
in the reply). These routes give the chat UI a list / download / save-to-
Library surface over those files.

Scope and posture:

- **Owner-only.** Every route resolves the session via
  ``app.state.chat_repo`` and 404s unless it belongs to the caller — the
  same non-leaking check ``GET /api/chat/sessions/{chat_id}/messages`` uses.
- **Paths are adversarial input.** Filenames and symlinks inside the
  sandbox are chosen by whatever the agent's tool calls did, so every
  requested path is validated (no absolute paths, no ``..``, no
  backslashes) AND realpath-contained: the resolved target must live under
  the caller's session dir or their own workspace. A symlink pointing
  anywhere else (another user's workspace, ``state/system.duckdb``) fails
  containment and 404s; the listing walk applies the same rule before
  descending into a directory.
- **Downloads never render.** Responses are always
  ``Content-Disposition: attachment`` + ``nosniff``, and active content
  types (HTML/SVG/XML) are additionally pinned to
  ``application/octet-stream`` so a crafted file cannot become same-origin
  markup. Mirrors the collections raw-file posture
  (``app/api/collections.py``).
- **Remote engines degrade to empty, not errors.** A session whose sandbox
  ran on a remote turn engine has no local files; the listing is empty
  rather than a failure (the files live in the remote sandbox, which is a
  provider-side gap, not a caller error).
"""

from __future__ import annotations

import logging
import mimetypes
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.access import require_resource_access
from app.chat.workdir import _safe_email_dir
from app.resource_types import ResourceType
from app.utils import get_data_dir

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# Same resource gate as the rest of the chat API (app/api/chat.py): the caller
# must have the "Cloud chat" feature grant (or be an Admin).
require_chat_access = require_resource_access(ResourceType.CHAT, "chat")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Directory names never descended into during the listing walk. The
#: ``.claude`` tree is deliberately NOT excluded — the #1611 repro wrote its
#: deliverables into ``.claude/skills/<name>/`` — so the walk only skips
#: unambiguous machine noise.
_SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})

#: Hard ceiling on files examined per listing — a runaway tree (a vendored
#: dependency, an extracted archive) stops here instead of stat-ing forever.
_MAX_SCAN_FILES = 20_000

#: Files returned per listing, newest-first. The UI's job is "hand me the
#: deliverables this conversation just produced", so recency is the honest
#: ranking and anything beyond this cap is noise (flagged via ``truncated``).
_MAX_LIST_FILES = 300

#: Content types that a browser could interpret as same-origin active content.
#: Downloads of these are pinned to application/octet-stream on top of the
#: attachment disposition + nosniff every response already carries.
_ACTIVE_CONTENT_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
    }
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SessionFileEntry(BaseModel):
    path: str
    name: str
    size_bytes: int
    modified_at: str


class SessionFilesResponse(BaseModel):
    files: list[SessionFileEntry]
    truncated: bool


class SaveArtefactBody(BaseModel):
    path: str


class SaveArtefactResponse(BaseModel):
    artefact_slug: str
    library_url: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _owned_session_or_404(request: Request, chat_id: str, user: object) -> dict:
    """Return the caller as a plain user dict, 404-ing on any session the
    caller does not own (and 403-ing restricted principals, which have no
    single identity to own a session's files)."""
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES) or not isinstance(user, dict) or not user.get("email"):
        raise HTTPException(status_code=403, detail="co_session cannot browse session files")

    repo = getattr(request.app.state, "chat_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail={"kind": "chat_disabled"})
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(status_code=404)
    return user


def _session_dir(email: str, chat_id: str) -> Path:
    slug = _safe_email_dir(email)
    return get_data_dir() / "users" / slug / "sessions" / chat_id


def _containment_bases(email: str, chat_id: str) -> tuple[Path, ...]:
    """Real paths a served file may resolve under: the session dir itself and
    the caller's own workspace (the session dir symlinks ``.claude``,
    ``snapshots`` etc. into it — deliverables written through those links
    physically live there)."""
    slug = _safe_email_dir(email)
    sdir = get_data_dir() / "users" / slug / "sessions" / chat_id
    ws = get_data_dir() / "users" / slug / "workspace"
    return (sdir.resolve(), ws.resolve())


def _is_contained(resolved: Path, bases: tuple[Path, ...]) -> bool:
    return any(resolved == base or resolved.is_relative_to(base) for base in bases)


def _validate_rel_path(raw: str) -> str:
    """Reject anything but a clean, relative, forward-slash path."""
    if not raw or raw != raw.strip():
        raise HTTPException(status_code=400, detail="path is required")
    if "\\" in raw or "\x00" in raw:
        raise HTTPException(status_code=400, detail="path contains disallowed characters")
    if raw.startswith("/") or Path(raw).is_absolute():
        raise HTTPException(status_code=400, detail="path must be relative to the session workspace")
    if any(seg in ("..", "") for seg in raw.split("/")):
        raise HTTPException(status_code=400, detail="path must not contain '..' or empty segments")
    return raw


def _resolve_file_or_404(email: str, chat_id: str, rel_path: str) -> Path:
    """Resolve ``rel_path`` against the session dir; the real target must be a
    regular file inside one of the containment bases. 404 otherwise — the
    response must not distinguish "exists but out of bounds" from "missing"."""
    sdir = _session_dir(email, chat_id)
    candidate = sdir / rel_path
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(status_code=404, detail="file not found") from None
    if not _is_contained(resolved, _containment_bases(email, chat_id)) or not resolved.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return resolved


def _walk_session_files(email: str, chat_id: str) -> tuple[list[dict], bool]:
    """Collect files reachable from the session dir (symlinks followed while
    they stay inside the containment bases), newest-first, capped."""
    sdir = _session_dir(email, chat_id)
    if not sdir.is_dir():
        return [], False

    bases = _containment_bases(email, chat_id)
    seen_dirs: set[Path] = set()
    collected: list[tuple[float, dict]] = []
    scanned = 0
    truncated = False

    for root, dirs, files in os.walk(sdir, followlinks=True):
        root_path = Path(root)
        # Prune in-place: skip noise dirs, dirs escaping containment, and
        # already-visited real dirs (symlink cycle guard).
        kept_dirs = []
        for d in sorted(dirs):
            if d in _SKIP_DIR_NAMES:
                continue
            try:
                real = (root_path / d).resolve()
            except (OSError, RuntimeError):
                continue
            if not _is_contained(real, bases) or real in seen_dirs:
                continue
            seen_dirs.add(real)
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for fname in files:
            if scanned >= _MAX_SCAN_FILES:
                truncated = True
                break
            scanned += 1
            fpath = root_path / fname
            try:
                real = fpath.resolve()
                if not _is_contained(real, bases) or not real.is_file():
                    continue
                st = real.stat()
            except (OSError, RuntimeError):
                continue
            rel = os.path.relpath(fpath, sdir)
            collected.append(
                (
                    st.st_mtime,
                    {
                        "path": rel.replace(os.sep, "/"),
                        "name": fname,
                        "size_bytes": st.st_size,
                        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                    },
                )
            )
        if truncated:
            break

    collected.sort(key=lambda item: item[0], reverse=True)
    if len(collected) > _MAX_LIST_FILES:
        truncated = True
    return [entry for _, entry in collected[:_MAX_LIST_FILES]], truncated


def _attachment_headers(filename: str) -> dict[str, str]:
    # Strip CR/LF/quotes so an agent-chosen name cannot split the header or
    # break out of the quoted filename — same treatment as
    # app/chat/artifact_harvest.sanitize_filename.
    safe = os.path.basename(filename.replace("\r", "").replace("\n", "").replace('"', "")) or "download"
    return {
        "Content-Disposition": f'attachment; filename="{safe}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/sessions/{chat_id}/files", response_model=SessionFilesResponse)
async def list_session_files(
    chat_id: str,
    request: Request,
    user: dict = Depends(require_chat_access),
) -> SessionFilesResponse:
    """List files in this chat session's workspace, newest-first.

    Walks the session dir (including the ``.claude``/``snapshots``/… links
    into your workspace, where skills write their deliverables), skipping
    machine noise (``.git``, ``__pycache__``, …) and anything resolving
    outside your own session/workspace. Returns at most 300 entries sorted
    by modification time; ``truncated`` reports when more existed. A session
    whose sandbox ran remotely has no local files and lists empty.
    """
    user = _owned_session_or_404(request, chat_id, user)
    files, truncated = _walk_session_files(user["email"], chat_id)
    return SessionFilesResponse(files=[SessionFileEntry(**f) for f in files], truncated=truncated)


@router.get("/sessions/{chat_id}/files/download")
async def download_session_file(
    chat_id: str,
    request: Request,
    path: str = Query(..., description="Session-relative file path, as returned by the listing."),
    user: dict = Depends(require_chat_access),
) -> FileResponse:
    """Download one file from this chat session's workspace.

    Always served as an attachment with ``nosniff``; HTML/SVG/XML bodies are
    additionally pinned to ``application/octet-stream`` so a generated file
    can never render as same-origin active content. 404 for a missing path
    and for anything resolving outside your session dir / workspace alike.
    """
    user = _owned_session_or_404(request, chat_id, user)
    rel = _validate_rel_path(path)
    resolved = _resolve_file_or_404(user["email"], chat_id, rel)

    media_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    if media_type in _ACTIVE_CONTENT_TYPES:
        media_type = "application/octet-stream"

    return FileResponse(
        path=str(resolved),
        media_type=media_type,
        headers=_attachment_headers(resolved.name),
    )


@router.post("/sessions/{chat_id}/files/save-artefact", response_model=SaveArtefactResponse)
async def save_session_file_as_artefact(
    chat_id: str,
    body: SaveArtefactBody,
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_chat_access),
) -> SaveArtefactResponse:
    """Save one session-workspace file as a private single-file artefact.

    The copy outlives the session and shows up in your Library
    (``/library/{slug}``), exactly like a document dropped into the chat
    composer. 415 for file types the corpus cannot ingest; 413 above the
    corpus's per-file ceiling.
    """
    from app.corpus_ingest import create_single_file_artefact
    from src.corpus_allowlist import MAX_UPLOAD_BYTES, classify

    user = _owned_session_or_404(request, chat_id, user)
    rel = _validate_rel_path(body.path)
    resolved = _resolve_file_or_404(user["email"], chat_id, rel)

    if classify(resolved.name) is None:
        raise HTTPException(
            status_code=415,
            detail=(f"'{resolved.name}' is not a file type the Library can ingest. Download it instead."),
        )
    size = resolved.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds the {MAX_UPLOAD_BYTES // 1024 // 1024} MB Library ceiling. Download it instead.",
        )
    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Saving to the Library requires a personal user account.")

    created = create_single_file_artefact(
        owner_id=user["id"],
        filename=resolved.name,
        data=resolved.read_bytes(),
    )
    if not created:
        raise HTTPException(status_code=415, detail=f"'{resolved.name}' could not be saved as an artefact.")

    slug = (created.get("collection") or {}).get("slug") or ""
    from src.ingest.runner import ingest_file

    background_tasks.add_task(ingest_file, created["file_id"])
    logger.info(
        "chat_session_files: user=%s session=%s saved %s as artefact %s",
        user["email"],
        chat_id,
        rel,
        slug,
    )
    return SaveArtefactResponse(artefact_slug=slug, library_url=f"/library/{slug}")
