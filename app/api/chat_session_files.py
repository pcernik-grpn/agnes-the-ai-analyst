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
- **Provider-gated.** The host walk above is only correct for providers
  whose sandbox works directly on the host session dir (``docker``). Under
  ``chat.provider: kai-agent`` — the default — the agent runs in the
  engine's own remote sandbox and its files never land on this host, while
  the host session dir still exists and holds nothing but workspace-template
  symlinks (``prepare_session_dir`` runs for every provider). Walking it
  would list hundreds of template files that are not session output, so
  engine-backed sessions never touch the host walk: they answer with
  ``source="engine"`` and either the engine's actual sandbox files (proxied
  via ``app.chat.kai_engine_files``) or ``supported=false`` when the engine
  exposes no files channel for the chat.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.auth.access import require_resource_access
from app.chat.kai_engine_files import (
    EngineFilesUnavailable,
    EngineFileTooLarge,
    fetch_engine_file_bytes,
    fetch_engine_listing,
    open_engine_download,
)
from app.chat.workdir import WORKSPACE_LINK_ENTRIES, _safe_email_dir
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

#: Directory names never descended into during the listing walk — unambiguous
#: machine noise, at any depth.
_SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})

#: Top-level entries that are the WORKSPACE TEMPLATE, not session output.
#: ``WorkdirManager.prepare_session_dir`` symlinks these into every session
#: dir for every provider, so walking them listed the operator's bundled
#: skills, hooks and scaffolds as if the agent had just produced them — on a
#: real conversation the deliverable was buried under dozens of
#: ``scaffolds/nodejs-dashboard/...`` rows. Excluded at the TOP LEVEL only:
#: the exclusion is about "this tree came from the template", not about the
#: name, so a directory the agent itself creates deeper in the session dir is
#: unaffected.
#:
#: This also aligns the two sources: the engine's own sandbox browser filters
#: dot-directories for the same reason, so a deliverable under ``.claude/``
#: was never going to be reachable there either. The workspace prompt now
#: tells the agent to write deliverables to ``outputs/`` instead.
_TEMPLATE_ENTRIES = frozenset(WORKSPACE_LINK_ENTRIES)

#: Deliverables live here by convention (the workspace prompt says so, and the
#: agent-API harvest scans the same directory). Sorted ahead of everything
#: else so the thing the user asked for is never below incidental scratch.
_OUTPUTS_PREFIX = "outputs/"

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
# Provider gating
# ---------------------------------------------------------------------------

#: Providers whose sessions run in a remote engine sandbox: their files are
#: never on this host, so the routes below must not walk the host session dir
#: for them. Mirrors the provider-branching resolver pattern of
#: ``app.chat.skills_catalog.marketplace_delivery``.
_ENGINE_SANDBOX_PROVIDERS = frozenset({"kai-agent"})


def _files_source(chat_config: object) -> str:
    """``"host"`` or ``"engine"`` — where this instance's session files live.

    Defensive ``getattr`` on purpose: a config double without ``provider``
    (and a MagicMock-style object) must resolve to the local, no-outbound-HTTP
    host path — the same duck-typed-double rule the provider capability flags
    follow (see tests/test_kai_engine_provider.py).
    """
    provider = str(getattr(chat_config, "provider", "") or "").strip().lower()
    return "engine" if provider in _ENGINE_SANDBOX_PROVIDERS else "host"


def _chat_config(request: Request):
    """One definition of "the chat config", shared with the skills catalog."""
    from app.api.chat import _chat_config_for_delivery

    return _chat_config_for_delivery(request)


#: Test seam: swaps the engine HTTP transport (httpx.ASGITransport onto the
#: stub app / MockTransport) — same injection idea as KaiEngineProvider's
#: ``transport=`` constructor arg, adapted to a module without a constructor.
_ENGINE_TRANSPORT: httpx.AsyncBaseTransport | None = None

#: Hard ceiling on one proxied engine download. The engine enforces its own
#: (smaller) cap; this bounds the proxy even against a misbehaving engine.
_MAX_PROXY_DOWNLOAD_BYTES = 100 * 1024 * 1024


def _engine_base_url(chat_config: object) -> str:
    url = str(getattr(chat_config, "kai_agent_url", "") or "").strip()
    if not url:
        # provider says kai-agent but no engine URL — operator misconfig.
        raise _engine_unavailable_502()
    return url


def _engine_unavailable_502() -> HTTPException:
    return HTTPException(status_code=502, detail={"kind": "engine_files_unavailable"})


async def _engine_token(user: dict, chat_id: str) -> str:
    """Session JWT for the engine calls — the provider's own mint, off-loop
    (it is synchronous and does a repo write; its 503 propagates as-is)."""
    from app.api.kai import mint_engine_session_token

    token, _expires = await asyncio.to_thread(mint_engine_session_token, user["email"], chat_id)
    return token


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SessionFileEntry(BaseModel):
    path: str
    name: str
    size_bytes: int
    # Engine listings carry no mtime, so the field is optional there.
    modified_at: str | None = None


class SessionFilesResponse(BaseModel):
    files: list[SessionFileEntry]
    truncated: bool
    #: Where the listing came from: "host" (docker session dir) or "engine"
    #: (the kai-agent engine's remote sandbox).
    source: str = "host"
    #: False when the session's files live in an engine sandbox the connected
    #: engine does not expose (no files channel for this chat).
    supported: bool = True


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
    they stay inside the containment bases), deliverables first, capped.

    The workspace-template trees (``_TEMPLATE_ENTRIES``) are excluded at the
    top level — they are the operator's bundled skills/scaffolds, present in
    every session for every provider, and listing them buried the actual
    deliverable. Within what remains, ``outputs/`` sorts ahead of everything
    else and the rest is newest-first.
    """
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
        at_top = root_path == sdir
        kept_dirs = []
        for d in sorted(dirs):
            if d in _SKIP_DIR_NAMES or (at_top and d in _TEMPLATE_ENTRIES):
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
            if at_top and fname in _TEMPLATE_ENTRIES:
                continue
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
                        "modified_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
                    },
                )
            )
        if truncated:
            break

    # Deliverables first, then newest-first within each group.
    collected.sort(key=lambda item: (not item[1]["path"].startswith(_OUTPUTS_PREFIX), -item[0]))
    if len(collected) > _MAX_LIST_FILES:
        truncated = True
    return [entry for _, entry in collected[:_MAX_LIST_FILES]], truncated


def _attachment_headers(filename: str) -> dict[str, str]:
    # Strip CR/LF/quotes so an agent-chosen name cannot split the header or
    # break out of the quoted filename — same treatment as
    # app/chat/artifact_harvest.sanitize_filename.
    safe = os.path.basename(filename.replace("\r", "").replace("\n", "").replace('"', "")) or "download"
    # ASGI header values are encoded latin-1, so a name carrying any code
    # point above 255 (CJK, Cyrillic, emoji — all reachable, the agent picks
    # the name) would blow up building the response. Emit the same two shapes
    # Starlette's own FileResponse(filename=…) does — plain while the name
    # survives percent-quoting unchanged, RFC 5987 extended otherwise — so
    # the real name is preserved instead of mangled, and the header is always
    # pure ASCII. Mirrors app/api/attachments.py.
    quoted = quote(safe)
    disposition = f'attachment; filename="{safe}"' if quoted == safe else f"attachment; filename*=utf-8''{quoted}"
    return {
        "Content-Disposition": disposition,
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
    by modification time; ``truncated`` reports when more existed. Sessions
    on an engine-sandbox provider (``kai-agent``) never walk the host dir —
    see the module docstring; they report ``source="engine"``.
    """
    user = _owned_session_or_404(request, chat_id, user)
    cfg = _chat_config(request)
    if _files_source(cfg) == "engine":
        return await _list_engine_files(user, chat_id, cfg)
    files, truncated = _walk_session_files(user["email"], chat_id)
    return SessionFilesResponse(files=[SessionFileEntry(**f) for f in files], truncated=truncated)


async def _list_engine_files(user: dict, chat_id: str, cfg: object) -> SessionFilesResponse:
    """Proxy the listing from the engine's sandbox file browser."""
    token = await _engine_token(user, chat_id)
    try:
        listing = await fetch_engine_listing(
            base_url=_engine_base_url(cfg),
            chat_id=chat_id,
            token=token,
            max_files=_MAX_LIST_FILES,
            transport=_ENGINE_TRANSPORT,
        )
    except EngineFilesUnavailable:
        logger.warning("chat_session_files: engine listing unavailable for session %s", chat_id, exc_info=True)
        raise _engine_unavailable_502() from None
    if listing is None:
        return SessionFilesResponse(files=[], truncated=False, source="engine", supported=False)
    entries, truncated = listing
    files: list[SessionFileEntry] = []
    for entry in entries:
        # Engine listings reflect agent-chosen names — re-validate each path
        # with the same rule the download route enforces, drop what fails.
        try:
            _validate_rel_path(entry["path"])
        except HTTPException:
            continue
        files.append(SessionFileEntry(**entry))
    return SessionFilesResponse(files=files, truncated=truncated, source="engine", supported=True)


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
    cfg = _chat_config(request)
    if _files_source(cfg) == "engine":
        return await _download_engine_file(user, chat_id, rel, cfg)
    resolved = _resolve_file_or_404(user["email"], chat_id, rel)

    return FileResponse(
        path=str(resolved),
        media_type=_download_media_type(resolved.name),
        headers=_attachment_headers(resolved.name),
    )


def _download_media_type(filename: str) -> str:
    """Filename-derived media type with active content pinned to a
    non-renderable one — never taken from any upstream header."""
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if media_type in _ACTIVE_CONTENT_TYPES:
        media_type = "application/octet-stream"
    return media_type


async def _download_engine_file(user: dict, chat_id: str, rel: str, cfg: object) -> Response:
    """Stream one engine sandbox file through, with the exact response
    posture of the host path (attachment + nosniff + pinned media type)."""
    token = await _engine_token(user, chat_id)
    try:
        opened = await open_engine_download(
            base_url=_engine_base_url(cfg),
            chat_id=chat_id,
            path=rel,
            token=token,
            max_bytes=_MAX_PROXY_DOWNLOAD_BYTES,
            transport=_ENGINE_TRANSPORT,
        )
    except EngineFileTooLarge:
        raise HTTPException(status_code=413, detail="File exceeds the download size ceiling.") from None
    except EngineFilesUnavailable:
        logger.warning("chat_session_files: engine download unavailable for session %s", chat_id, exc_info=True)
        raise _engine_unavailable_502() from None
    if opened is None:
        raise HTTPException(status_code=404, detail="file not found")
    iterator, handle = opened
    name = rel.rsplit("/", 1)[-1]
    return StreamingResponse(
        iterator,
        media_type=_download_media_type(name),
        headers=_attachment_headers(name),
        background=BackgroundTask(handle.aclose),
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
    from src.corpus_allowlist import MAX_UPLOAD_BYTES, classify

    def _unsupported_type_415(filename: str) -> HTTPException:
        return HTTPException(
            status_code=415,
            detail=(f"'{filename}' is not a file type the Library can ingest. Download it instead."),
        )

    over_ceiling = HTTPException(
        status_code=413,
        detail=f"File exceeds the {MAX_UPLOAD_BYTES // 1024 // 1024} MB Library ceiling. Download it instead.",
    )

    user = _owned_session_or_404(request, chat_id, user)
    rel = _validate_rel_path(body.path)
    cfg = _chat_config(request)
    if _files_source(cfg) == "engine":
        name = rel.rsplit("/", 1)[-1]
        # Classify before spending the engine round-trip.
        if classify(name) is None:
            raise _unsupported_type_415(name)
        token = await _engine_token(user, chat_id)
        try:
            data = await fetch_engine_file_bytes(
                base_url=_engine_base_url(cfg),
                chat_id=chat_id,
                path=rel,
                token=token,
                max_bytes=MAX_UPLOAD_BYTES,
                transport=_ENGINE_TRANSPORT,
            )
        except EngineFileTooLarge:
            raise over_ceiling from None
        except EngineFilesUnavailable:
            logger.warning("chat_session_files: engine fetch unavailable for session %s", chat_id, exc_info=True)
            raise _engine_unavailable_502() from None
        if data is None:
            raise HTTPException(status_code=404, detail="file not found")
    else:
        resolved = _resolve_file_or_404(user["email"], chat_id, rel)
        name = resolved.name
        if classify(name) is None:
            raise _unsupported_type_415(name)
        if resolved.stat().st_size > MAX_UPLOAD_BYTES:
            raise over_ceiling
        data = resolved.read_bytes()

    if not user.get("id"):
        raise HTTPException(status_code=403, detail="Saving to the Library requires a personal user account.")

    from app.corpus_ingest import create_single_file_artefact

    created = create_single_file_artefact(
        owner_id=user["id"],
        filename=name,
        data=data,
    )
    if not created:
        raise HTTPException(status_code=415, detail=f"'{name}' could not be saved as an artefact.")

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
