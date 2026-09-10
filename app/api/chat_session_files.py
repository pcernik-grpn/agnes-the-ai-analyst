"""Chat session-workspace file endpoints (#1611).

A chat agent's deliverables (a rendered ``.docx``/``.pptx``, a chart, a
report) land in the session's workdir on the Agnes host — with the default
docker sandbox the session dir IS the container's ``/work`` (bind-mounted,
see ``app/chat/docker_provider.py``), and skill output written through the
``.claude`` symlink lands in the caller's per-user workspace. Until now the
web chat had no way to reach any of it (the only channel out was inline SVG
in the reply). These routes give the chat UI a list / preview / download /
save-to-Library surface over those files.

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
  The ONE exception is ``…/files/raw``, the preview viewer, and it is an
  exception by construction rather than by trust: it serves only the closed
  ``_PREVIEW_INLINE_MEDIA`` extension map (images + PDF), takes the media
  type FROM that map rather than from the file, and 415s everything else. An
  agent-authored ``.html`` or ``.svg`` therefore has no inline route at all —
  ``…/files/preview`` shows those as SOURCE text, never rendered.
- **Harvested first, the sandbox second (#2268).** A deliverable used to
  exist only inside the sandbox, so once the idle reaper paused a session
  every file it had produced was gone and this listing answered "No files
  here yet" for a conversation that had just built a document. The turn-end
  harvest (``app.chat.artifact_harvest``, fired from ``app.chat.manager``)
  now copies ``outputs/`` into the object store as ``agent_artifacts`` rows,
  and these routes serve those rows: a harvested file is listed, downloaded
  and previewed with no live sandbox involved at all. The sandbox is still
  consulted and still WINS for anything it holds — it has the fresher bytes
  and it also holds files outside ``outputs/``, which are never harvested —
  but it is a fallback now, not the source of truth. An engine outage
  degrades to the harvested copies instead of a 502; only a session with
  nothing harvested still surfaces the outage.
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
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.auth.access import require_resource_access
from app.chat.artifact_harvest import sanitize_filename
from app.chat.kai_engine_files import (
    ENGINE_SANDBOX_PROVIDERS,
    EngineFilesUnavailable,
    EngineFileTooLarge,
    fetch_engine_file_bytes,
    fetch_engine_listing,
    open_engine_download,
)
from app.chat.workdir import WORKSPACE_LINK_ENTRIES, _safe_email_dir
from app.resource_types import ResourceType
from app.utils import get_data_dir
from src.object_store import object_store
from src.repositories import agent_artifacts_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

#: Chat ids whose engine-listing failure has already logged a full
#: traceback this process. The Files panel polls a live session every few
#: seconds, so an outage lasting minutes used to write one full traceback
#: PER POLL for the identical stack (observed live: 8 and 3 occurrences
#: across two sessions in one evening) — the stack carries no new
#: information on the second poll onward, so only the first occurrence for
#: a given chat id gets one; later polls still warn, just without repeating
#: it. Bounded FIFO (oldest-inserted evicted first) so a long-running
#: process accumulating many distinct failing sessions cannot grow this
#: without limit.
_ENGINE_LISTING_FAILURE_LOGGED: OrderedDict[str, None] = OrderedDict()
_ENGINE_LISTING_FAILURE_LOGGED_MAX = 512


def _first_engine_listing_failure(chat_id: str) -> bool:
    """``True`` the first time this chat id's listing failure is seen this
    process, ``False`` for every later call with the same id."""
    first = chat_id not in _ENGINE_LISTING_FAILURE_LOGGED
    _ENGINE_LISTING_FAILURE_LOGGED[chat_id] = None
    _ENGINE_LISTING_FAILURE_LOGGED.move_to_end(chat_id)
    while len(_ENGINE_LISTING_FAILURE_LOGGED) > _ENGINE_LISTING_FAILURE_LOGGED_MAX:
        _ENGINE_LISTING_FAILURE_LOGGED.popitem(last=False)
    return first


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

#: Formats the browser draws itself, served inline as the real bytes by the
#: ``…/files/raw`` viewer. Deliberately a CLOSED map keyed on the extension
#: with the media type taken FROM the map — never from the file, never from
#: ``mimetypes`` — because everything else this module serves is pinned to an
#: attachment precisely so an agent-authored file cannot become same-origin
#: markup. Same closed-list posture, and the same list, as the Library's
#: ``_PREVIEW_INLINE_MEDIA`` (``app/api/collections.py``): HTML and SVG are
#: absent on purpose and must stay absent.
_PREVIEW_INLINE_MEDIA: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
}

#: Extensions whose bytes ARE the text of the preview. ``html``/``svg``/``xml``
#: belong here rather than above: shown as SOURCE in a ``<pre>``, never
#: rendered — which is the whole reason they are not in the inline map.
_PREVIEW_TEXTUAL_EXTS: frozenset[str] = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "csv",
        "tsv",
        "json",
        "jsonl",
        "yaml",
        "yml",
        "log",
        "sql",
        "py",
        "html",
        "svg",
        "xml",
    }
)

#: Bytes pulled off disk (or through the engine) to build a preview. A glance
#: is capped so a 2 GB deliverable cannot be read into memory to render a
#: modal — past this the answer is "download it".
_PREVIEW_MAX_BYTES = 25 * 1024 * 1024

#: Characters returned for a textual preview.
_PREVIEW_MAX_CHARS = 20_000


# ---------------------------------------------------------------------------
# Provider gating
# ---------------------------------------------------------------------------

#: Providers whose sessions run in a remote engine sandbox: their files are
#: never on this host, so the routes below must not walk the host session dir
#: for them. Mirrors the provider-branching resolver pattern of
#: ``app.chat.skills_catalog.marketplace_delivery``. Defined once in
#: ``app.chat.kai_engine_files`` and shared with the turn-end harvest, so the
#: browse surface and the harvest surface can never disagree about which
#: provider keeps its files in the engine.
_ENGINE_SANDBOX_PROVIDERS = ENGINE_SANDBOX_PROVIDERS


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


class SlidePreview(BaseModel):
    """One slide of a ``.pptx`` preview — see ``src/office_preview.Slide``."""

    index: int
    title: str = ""
    lines: list[str] = []


class SheetPreview(BaseModel):
    """One worksheet of an ``.xlsx`` preview — see ``src/office_preview.Sheet``.

    ``rows`` is rectangular (every row padded to the widest), so the client
    renders a grid without re-deriving the column count. ``truncated`` is per
    sheet: one small tab can sit beside a 50 000-row export in the same
    workbook, and a modal-wide "showing the first rows" would be wrong about
    the small one.
    """

    name: str
    rows: list[list[str]] = []
    truncated: bool = False


class SessionFilePreview(BaseModel):
    """What to show for one session file, and how — the modal's single fetch.

    ``kind`` is the client's only switch, exactly as on the Library's sibling
    endpoint (``GET /api/collections/{id}/files/{fid}/preview``):

    * ``image`` / ``pdf`` — fetch ``raw_url``, let the browser draw it.
    * ``slides`` — ``slides`` holds the deck's text, slide by slide.
    * ``sheets`` — ``sheets`` holds the workbook's cells, sheet by sheet.
    * ``text`` — ``text`` holds it; ``truncated`` says a glance is all this is.
    * ``none`` — ``reason`` says why, in the words the modal shows.
    """

    path: str
    name: str
    kind: str
    file_type: str | None = None
    size_bytes: int | None = None
    raw_url: str | None = None
    text: str | None = None
    slides: list[SlidePreview] | None = None
    sheets: list[SheetPreview] | None = None
    truncated: bool = False
    #: ``file`` when the bytes ARE the text, ``extracted`` when this module
    #: pulled the words out of a container format (a deck, a document).
    source: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _owned_session_or_404(request: Request, chat_id: str, user: object) -> dict:
    """Return the caller as a plain user dict, 404-ing on any session the
    caller does not own (and 403-ing restricted principals, which have no
    single identity to own a session's files)."""
    user, _session = _owned_session_and_row_or_404(request, chat_id, user)
    return user


def _owned_session_and_row_or_404(request: Request, chat_id: str, user: object) -> tuple[dict, Any]:
    """Same ownership check as :func:`_owned_session_or_404`, but also hands
    back the session row a caller already paid the repo round trip for —
    e.g. ``sandbox_id``, which the engine-listing gate below reads without a
    second lookup."""
    from app.auth.session_principal import PRINCIPAL_TYPES

    if isinstance(user, PRINCIPAL_TYPES) or not isinstance(user, dict) or not user.get("email"):
        raise HTTPException(status_code=403, detail="co_session cannot browse session files")

    repo = getattr(request.app.state, "chat_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail={"kind": "chat_disabled"})
    s = repo.get_session(chat_id)
    if s is None or s.user_email != user["email"]:
        raise HTTPException(status_code=404)
    return user, s


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
# Harvested artifacts — the copy that outlives the sandbox (#2268)
# ---------------------------------------------------------------------------


def _harvested_rows(chat_id: str) -> list[dict]:
    """Every artifact harvested for this session, newest last.

    Never raises: the Files panel degrading to "only what the sandbox still
    has" is bad, but an exception here would take the whole listing down —
    including the live half that is working fine.
    """
    try:
        return agent_artifacts_repo().list_for_session(chat_id)
    except Exception:
        logger.exception("chat_session_files: harvested-artifact lookup failed for session %s", chat_id)
        return []


def _harvested_entries(chat_id: str) -> list[dict]:
    """Harvested rows as listing entries, at the path the sandbox used.

    The harvest scans exactly ``{SANDBOX_WORKDIR}/outputs`` and stores the
    file's sanitized basename, so ``outputs/<filename>`` reconstructs the
    session-relative path the LIVE listing reports for the same file. That is
    what lets the two lists merge on ``path`` without ever showing a file
    twice — and what makes a download of a listed path resolvable from either
    side.
    """
    entries: list[dict] = []
    for row in _harvested_rows(chat_id):
        name = sanitize_filename(str(row.get("filename") or ""))
        created = row.get("created_at")
        entries.append(
            {
                "path": f"{_OUTPUTS_PREFIX}{name}",
                "name": name,
                "size_bytes": int(row.get("size_bytes") or 0),
                "modified_at": created.isoformat() if hasattr(created, "isoformat") else created,
            }
        )
    return entries


def _harvested_row_for_path(chat_id: str, rel: str) -> dict | None:
    """The harvested row a session-relative path refers to, if any.

    Only ``outputs/<name>`` can match — a harvested artifact is always a flat
    file directly under ``outputs/`` — so a nested or elsewhere path is
    answered without touching the repo.
    """
    if not rel.startswith(_OUTPUTS_PREFIX):
        return None
    name = rel[len(_OUTPUTS_PREFIX) :]
    if not name or "/" in name:
        return None
    for row in _harvested_rows(chat_id):
        if sanitize_filename(str(row.get("filename") or "")) == name:
            return row
    return None


async def _harvested_bytes(row: dict) -> bytes | None:
    """The artifact's bytes from the object store, or ``None`` when the store
    is unconfigured / the object is gone (a row without its blob is a
    "missing file", never a 500)."""
    store = object_store()
    if store is None:
        return None
    try:
        return await asyncio.to_thread(store.get_bytes, row["object_key"])
    except Exception:
        logger.exception("chat_session_files: object-store read failed for %s", row.get("object_key"))
        return None


async def _harvested_download(chat_id: str, rel: str) -> Response | None:
    """A download response served from the harvested copy, or ``None`` when
    there is none. Identical posture to the live paths: attachment, nosniff,
    active content types pinned to a non-renderable one."""
    row = _harvested_row_for_path(chat_id, rel)
    if row is None:
        return None
    data = await _harvested_bytes(row)
    if data is None:
        return None
    name = sanitize_filename(str(row.get("filename") or ""))
    return Response(
        content=data,
        media_type=_download_media_type(name),
        headers=_attachment_headers(name),
    )


async def _harvested_preview_bytes(chat_id: str, rel: str) -> bytes | None:
    """Preview bytes from the harvested copy, honouring the same size
    ceiling as the live paths."""
    row = _harvested_row_for_path(chat_id, rel)
    if row is None:
        return None
    if int(row.get("size_bytes") or 0) > _PREVIEW_MAX_BYTES:
        raise _TooLargeToPreview
    return await _harvested_bytes(row)


def _merge_harvested(live: SessionFilesResponse, harvested: list[dict]) -> SessionFilesResponse:
    """Live listing + the harvested files it does not already contain.

    The live sandbox wins on collision: it has the fresher bytes for a file
    that was rewritten after its harvest, and its entry carries the mtime the
    harvested one cannot. Deliverables stay first (a stable sort, so each
    group keeps the order its source produced) and the whole thing stays
    capped at ``_MAX_LIST_FILES``.
    """
    if not harvested:
        return live
    known = {f.path for f in live.files}
    merged = list(live.files) + [SessionFileEntry(**h) for h in harvested if h["path"] not in known]
    merged.sort(key=lambda f: not f.path.startswith(_OUTPUTS_PREFIX))
    truncated = live.truncated or len(merged) > _MAX_LIST_FILES
    return SessionFilesResponse(
        files=merged[:_MAX_LIST_FILES],
        truncated=truncated,
        source=live.source,
        # Something IS here, whatever the sandbox's fate — the whole point.
        supported=True,
    )


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

    Answers from two sources, harvested first (#2268): every file this
    session's turns wrote to ``outputs/`` was copied out of the sandbox as it
    was produced, so it is listed whether or not the sandbox still exists.
    The live sandbox is merged on top — it wins for a file it still holds
    (fresher bytes, real mtime) and contributes everything outside
    ``outputs/``, which is never harvested.

    The live half walks the session dir (including the
    ``.claude``/``snapshots``/… links into your workspace, where skills write
    their deliverables), skipping machine noise (``.git``, ``__pycache__``,
    …) and anything resolving outside your own session/workspace. Returns at
    most 300 entries, deliverables first; ``truncated`` reports when more
    existed. Sessions on an engine-sandbox provider (``kai-agent``) never
    walk the host dir — see the module docstring; they report
    ``source="engine"``.

    An engine-sandbox session with no ``sandbox_id`` yet (a brand-new chat,
    or one whose sandbox was torn down) never asks the engine at all: this
    route is polled the instant a conversation opens, well before the first
    turn can mint a sandbox, and the engine has no way to know a chat id it
    has never seen — every such poll used to land a "chat not found" 404 on
    the engine's own log for no reason. The answer is the same shape a 404
    from the engine gives today, just without the round trip.
    """
    user, session = _owned_session_and_row_or_404(request, chat_id, user)
    cfg = _chat_config(request)
    harvested = _harvested_entries(chat_id)
    if _files_source(cfg) == "engine":
        if not getattr(session, "sandbox_id", None):
            return _merge_harvested(
                SessionFilesResponse(files=[], truncated=False, source="engine", supported=False),
                harvested,
            )
        try:
            live = await _list_engine_files(user, chat_id, cfg)
        except EngineFilesUnavailable:
            # The traceback is identical every poll of the same outage, so
            # only the first one for this chat id is worth its weight in
            # the log — see _first_engine_listing_failure.
            loud = _first_engine_listing_failure(chat_id)
            repeat_note = "" if loud else " (repeat poll, traceback suppressed — see the first occurrence)"
            if not harvested:
                # Nothing of our own to serve — the outage IS the answer.
                logger.warning(
                    "chat_session_files: engine listing unavailable for session %s%s",
                    chat_id,
                    repeat_note,
                    exc_info=loud,
                )
                raise _engine_unavailable_502() from None
            logger.warning(
                "chat_session_files: engine listing unavailable for session %s — serving %d harvested artifact(s)%s",
                chat_id,
                len(harvested),
                repeat_note,
                exc_info=loud,
            )
            live = SessionFilesResponse(files=[], truncated=False, source="engine", supported=False)
        return _merge_harvested(live, harvested)
    files, truncated = _walk_session_files(user["email"], chat_id)
    host = SessionFilesResponse(files=[SessionFileEntry(**f) for f in files], truncated=truncated)
    return _merge_harvested(host, harvested)


async def _list_engine_files(user: dict, chat_id: str, cfg: object) -> SessionFilesResponse:
    """Proxy the listing from the engine's sandbox file browser.

    Raises :class:`EngineFilesUnavailable` rather than mapping it to a 502
    here: whether an unreachable engine is fatal depends on what has been
    harvested for this session, which only the caller knows.
    """
    token = await _engine_token(user, chat_id)
    listing = await fetch_engine_listing(
        base_url=_engine_base_url(cfg),
        chat_id=chat_id,
        token=token,
        max_files=_MAX_LIST_FILES,
        transport=_ENGINE_TRANSPORT,
    )
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
    # Engine listings carry no mtime to sort by, but the deliverables-first
    # promise holds: outputs/ ahead of everything, then stable by path.
    files.sort(key=lambda f: (not f.path.startswith(_OUTPUTS_PREFIX), f.path))
    return SessionFilesResponse(files=files, truncated=truncated, source="engine", supported=True)


@router.get("/sessions/{chat_id}/files/download")
async def download_session_file(
    chat_id: str,
    request: Request,
    path: str = Query(..., description="Session-relative file path, as returned by the listing."),
    user: dict = Depends(require_chat_access),
) -> Response:
    """Download one file from this chat session's workspace.

    Served from the live sandbox when it still has the file, and from the
    harvested copy otherwise (#2268) — a deliverable stays downloadable long
    after the sandbox that produced it is gone. Always an attachment with
    ``nosniff`` either way; HTML/SVG/XML bodies are additionally pinned to
    ``application/octet-stream`` so a generated file can never render as
    same-origin active content. 404 for a missing path and for anything
    resolving outside your session dir / workspace alike.
    """
    user = _owned_session_or_404(request, chat_id, user)
    rel = _validate_rel_path(path)
    cfg = _chat_config(request)
    if _files_source(cfg) == "engine":
        return await _download_engine_file(user, chat_id, rel, cfg)
    try:
        resolved = _resolve_file_or_404(user["email"], chat_id, rel)
    except HTTPException:
        harvested = await _harvested_download(chat_id, rel)
        if harvested is None:
            raise
        return harvested

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
    posture of the host path (attachment + nosniff + pinned media type).

    Falls back to the harvested copy when the engine no longer has the file
    (404 — the sandbox is gone) or cannot be reached at all: those are the
    two ways a produced deliverable used to become unreachable.
    """
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
        harvested = await _harvested_download(chat_id, rel)
        if harvested is not None:
            logger.warning(
                "chat_session_files: engine download unavailable for session %s — serving the harvested copy",
                chat_id,
                exc_info=True,
            )
            return harvested
        logger.warning("chat_session_files: engine download unavailable for session %s", chat_id, exc_info=True)
        raise _engine_unavailable_502() from None
    if opened is None:
        harvested = await _harvested_download(chat_id, rel)
        if harvested is not None:
            return harvested
        raise HTTPException(status_code=404, detail="file not found")
    iterator, handle = opened
    name = rel.rsplit("/", 1)[-1]
    return StreamingResponse(
        iterator,
        media_type=_download_media_type(name),
        headers=_attachment_headers(name),
        background=BackgroundTask(handle.aclose),
    )


# ---------------------------------------------------------------------------
# Preview — "did it build the deck I asked for?" without a download
# ---------------------------------------------------------------------------


def _preview_ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot + 1 :].lower() if dot > 0 else ""


class _TooLargeToPreview(Exception):
    """Bytes exist but exceed ``_PREVIEW_MAX_BYTES`` — a `none` preview, not
    an error: the row still downloads, and saying so is the useful answer."""


async def _preview_bytes(user: dict, chat_id: str, rel: str, cfg: object) -> bytes | None:
    """The file's bytes for preview purposes, from whichever sandbox holds
    them — or from the harvested copy when neither does (#2268). ``None``
    when the path does not resolve to a readable file anywhere: the same
    non-leaking 404 shape the download route uses."""
    if _files_source(cfg) == "engine":
        token = await _engine_token(user, chat_id)
        try:
            data = await fetch_engine_file_bytes(
                base_url=_engine_base_url(cfg),
                chat_id=chat_id,
                path=rel,
                token=token,
                max_bytes=_PREVIEW_MAX_BYTES,
                transport=_ENGINE_TRANSPORT,
            )
        except EngineFileTooLarge:
            raise _TooLargeToPreview from None
        except EngineFilesUnavailable:
            harvested = await _harvested_preview_bytes(chat_id, rel)
            if harvested is not None:
                return harvested
            logger.warning("chat_session_files: engine preview unavailable for session %s", chat_id, exc_info=True)
            raise _engine_unavailable_502() from None
        if data is not None:
            return data
        return await _harvested_preview_bytes(chat_id, rel)
    try:
        resolved = _resolve_file_or_404(user["email"], chat_id, rel)
    except HTTPException:
        harvested = await _harvested_preview_bytes(chat_id, rel)
        if harvested is None:
            raise
        return harvested
    if resolved.stat().st_size > _PREVIEW_MAX_BYTES:
        raise _TooLargeToPreview
    return await asyncio.to_thread(resolved.read_bytes)


@router.get("/sessions/{chat_id}/files/preview", response_model=SessionFilePreview)
async def preview_session_file(
    chat_id: str,
    request: Request,
    path: str = Query(..., description="Session-relative file path, as returned by the listing."),
    user: dict = Depends(require_chat_access),
) -> SessionFilePreview:
    """What to show for one session file, and how.

    The point of this endpoint is the deliverable a browser cannot draw. A
    ``.pptx`` handed to an ``<iframe>`` is a download prompt, so a deck (and a
    ``.docx``, and an ``.xlsx``) is previewed as its own content, read
    straight out of the OOXML archive by ``src/office_preview`` — no optional
    extra, no conversion service. Images and PDFs point at the sibling ``…/raw`` viewer; textual
    files carry their own first ``_PREVIEW_MAX_CHARS``; everything else
    reports ``kind="none"`` with the sentence the modal shows.

    Deliberately one endpoint for every format, mirroring the Library's
    ``…/files/{id}/preview``: the client must not have to know which
    extensions are safe to stream inline — that list is a security boundary
    and it lives on this side.
    """
    user = _owned_session_or_404(request, chat_id, user)
    rel = _validate_rel_path(path)
    cfg = _chat_config(request)
    name = rel.rsplit("/", 1)[-1]
    ext = _preview_ext(name)

    def out(**fields: Any) -> SessionFilePreview:
        """Every return below is the same envelope with a different payload —
        keep the identity fields in one place so a branch cannot forget one."""
        return SessionFilePreview(path=rel, name=name, file_type=ext or None, **fields)

    if ext in _PREVIEW_INLINE_MEDIA:
        # Never read these here: the browser fetches the bytes itself from
        # …/raw, which is the one route allowed to serve them inline.
        return out(
            kind="image" if ext != "pdf" else "pdf",
            raw_url=("/api/chat/sessions/" + quote(chat_id, safe="") + "/files/raw?path=" + quote(rel, safe="")),
        )

    from src.office_preview import is_office_preview_ext

    office_kind = is_office_preview_ext(ext)
    if office_kind is None and ext not in _PREVIEW_TEXTUAL_EXTS:
        return out(
            kind="none",
            reason=f"No preview for '.{ext or 'unknown'}' files — download it to open it.",
        )

    try:
        data = await _preview_bytes(user, chat_id, rel, cfg)
    except _TooLargeToPreview:
        return out(
            kind="none",
            reason=(f"This file is larger than {_PREVIEW_MAX_BYTES // 1024 // 1024} MB — download it to open it."),
        )
    if data is None:
        raise HTTPException(status_code=404, detail="file not found")

    if office_kind == "slides":
        from src.office_preview import pptx_slides

        slides, truncated = await asyncio.to_thread(pptx_slides, data)
        if not slides:
            return out(
                kind="none",
                size_bytes=len(data),
                reason="This presentation's slides could not be read — download it to open it.",
            )
        return out(
            kind="slides",
            size_bytes=len(data),
            slides=[SlidePreview(index=s.index, title=s.title, lines=s.lines) for s in slides],
            truncated=truncated,
            source="extracted",
        )

    if office_kind == "sheets":
        from src.office_preview import xlsx_sheets

        sheets, truncated = await asyncio.to_thread(xlsx_sheets, data)
        if not sheets:
            return out(
                kind="none",
                size_bytes=len(data),
                reason="This workbook's sheets could not be read — download it to open it.",
            )
        return out(
            kind="sheets",
            size_bytes=len(data),
            sheets=[SheetPreview(name=s.name, rows=s.rows, truncated=s.truncated) for s in sheets],
            truncated=truncated,
            source="extracted",
        )

    if office_kind == "text":
        from src.office_preview import docx_paragraphs

        text, truncated = await asyncio.to_thread(docx_paragraphs, data)
        if not text:
            return out(
                kind="none",
                size_bytes=len(data),
                reason="This document's text could not be read — download it to open it.",
            )
        return out(
            kind="text",
            size_bytes=len(data),
            text=text[:_PREVIEW_MAX_CHARS],
            truncated=truncated or len(text) > _PREVIEW_MAX_CHARS,
            source="extracted",
        )

    text = data.decode("utf-8", errors="replace")
    return out(
        kind="text",
        size_bytes=len(data),
        text=text[:_PREVIEW_MAX_CHARS],
        truncated=len(text) > _PREVIEW_MAX_CHARS,
        source="file",
    )


def _inline_headers(filename: str) -> dict[str, str]:
    """Viewer response posture: inline, non-sniffable, framable only by us.

    The app-wide defaults are ``X-Frame-Options: DENY`` + ``frame-ancestors
    'none'``, which would block the preview modal's own PDF ``<iframe>`` —
    same-origin framing included. Narrowed to SELF here (never wider) for
    this one response, exactly as the Library's ``…/raw`` does;
    ``SecurityHeadersMiddleware`` applies its defaults with ``setdefault``,
    so these win.
    """
    safe = os.path.basename(filename.replace("\r", "").replace("\n", "").replace('"', "")) or "file"
    quoted = quote(safe)
    disposition = f'inline; filename="{safe}"' if quoted == safe else f"inline; filename*=utf-8''{quoted}"
    return {
        "Content-Disposition": disposition,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
    }


@router.get("/sessions/{chat_id}/files/raw")
async def raw_session_file(
    chat_id: str,
    request: Request,
    path: str = Query(..., description="Session-relative file path, as returned by the listing."),
    user: dict = Depends(require_chat_access),
) -> Response:
    """Stream one session file INLINE for the browser to draw (images + PDF).

    The only route in this module that does not force an attachment, which is
    why it serves the closed ``_PREVIEW_INLINE_MEDIA`` set and nothing else,
    with the media type read out of that map rather than guessed from the
    file. Any other extension is 415 pointing at ``…/preview`` — this is a
    viewer, not a second download route.
    """
    user = _owned_session_or_404(request, chat_id, user)
    rel = _validate_rel_path(path)
    name = rel.rsplit("/", 1)[-1]
    media = _PREVIEW_INLINE_MEDIA.get(_preview_ext(name))
    if not media:
        raise HTTPException(
            status_code=415,
            detail=(
                f"no inline preview for '{name}' — "
                f"GET /api/chat/sessions/{chat_id}/files/preview?path=… describes what to show instead"
            ),
        )

    cfg = _chat_config(request)

    async def _harvested_inline() -> Response | None:
        """The harvested copy, drawn inline under the same closed media map
        the live paths use — the media type still comes from
        ``_PREVIEW_INLINE_MEDIA``, never from the stored row."""
        row = _harvested_row_for_path(chat_id, rel)
        if row is None:
            return None
        data = await _harvested_bytes(row)
        if data is None:
            return None
        return Response(content=data, media_type=media, headers=_inline_headers(name))

    if _files_source(cfg) == "engine":
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
            harvested = await _harvested_inline()
            if harvested is not None:
                return harvested
            logger.warning("chat_session_files: engine raw unavailable for session %s", chat_id, exc_info=True)
            raise _engine_unavailable_502() from None
        if opened is None:
            harvested = await _harvested_inline()
            if harvested is not None:
                return harvested
            raise HTTPException(status_code=404, detail="file not found")
        iterator, handle = opened
        return StreamingResponse(
            iterator,
            media_type=media,
            headers=_inline_headers(name),
            background=BackgroundTask(handle.aclose),
        )

    try:
        resolved = _resolve_file_or_404(user["email"], chat_id, rel)
    except HTTPException:
        harvested = await _harvested_inline()
        if harvested is None:
            raise
        return harvested
    return FileResponse(
        path=str(resolved),
        media_type=media,
        headers=_inline_headers(resolved.name),
    )


@router.post("/sessions/{chat_id}/files/save-artefact", response_model=SaveArtefactResponse)
async def save_session_file_as_artefact(
    chat_id: str,
    body: SaveArtefactBody,
    request: Request,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_chat_access),
) -> SaveArtefactResponse:
    """Save one session-workspace file as a private single-file artifact.

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
        raise HTTPException(status_code=415, detail=f"'{name}' could not be saved as an artifact.")

    slug = (created.get("collection") or {}).get("slug") or ""
    from src.ingest.runner import ingest_file

    background_tasks.add_task(ingest_file, created["file_id"])
    logger.info(
        "chat_session_files: user=%s session=%s saved %s as artifact %s",
        user["email"],
        chat_id,
        rel,
        slug,
    )
    return SaveArtefactResponse(artefact_slug=slug, library_url=f"/library/{slug}")
