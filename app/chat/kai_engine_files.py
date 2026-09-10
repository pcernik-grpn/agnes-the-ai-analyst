"""HTTP client for the kai-agent engine's sandbox file routes (#1611).

Sessions on ``chat.provider: kai-agent`` run in the engine's own remote
sandbox, so their files never touch this host. The engine already exposes a
read-only file browser over that sandbox — the same one Keboola's UI renders:

    GET {kai_agent_url}/api/chat/{chat_id}/sandbox/files?path=<rel dir>
        200 -> {"entries": [{"name", "path", "type": "file"|"dir", "size"?}]}
        (one directory level, engine-side filtered: dotfiles, .git, .claude,
        node_modules etc. never appear)
    GET {kai_agent_url}/api/chat/{chat_id}/sandbox/file/download?path=<rel>
        200 -> raw bytes (the engine serves attachment/nosniff; the caller
        re-derives all response headers anyway and never forwards these)

Auth on both: ``Authorization: Bearer <session JWT>`` — the exact token
``app.api.kai.mint_engine_session_token`` signs, i.e. the same credential the
embedded provider (``kai_engine_provider.py``) uses for ``POST /api/chat``.

Error mapping is the caller's contract, implemented here once:

- Engine 404 on the LISTING = "no files channel for this chat" — an unknown
  chat id or an engine build predating the sandbox-files routes.
  Deliberately collapsed, no body sniffing: both mean the honest answer is
  ``supported: false``, never an error. A 404 on the DOWNLOAD maps to a
  plain 404 (unknown path is by far the common case once the listing
  worked).
- Engine 400 = "this id cannot be served" — treated the same as a 404,
  never as an outage. Observed live: a session minted before the instance's
  ``chat.provider`` was switched TO ``kai-agent`` keeps its older
  ``chat_<hex>`` id, and the engine's chat table is a Postgres ``uuid``
  column, so that id fails at the DB layer before an existence check ever
  runs — a 400, not the 404 an unknown-but-well-formed id gets (the exact
  id-shape case ``KaiEngineProvider._handle`` already refuses before
  minting a turn handle, for the identical reason). Rather than
  pre-validating the id shape here (a second place that would have to stay
  in sync with the engine's own rule), the response the engine actually
  gives is trusted and folded into the same "no files channel" bucket a 404
  already gets — no traceback, because this is not a failure to warn about.
- Engine 401/403 = the host JWT contract is misconfigured (secret/iss/aud
  drift) — loud log, then :class:`EngineFilesUnavailable`, because "no
  files" would mask an operator problem.
- Engine 5xx / network failure = :class:`EngineFilesUnavailable` (the route
  answers 502).

Two consumers share this one client. The Files-panel routes
(``app.api.chat_session_files``) browse; the turn-end artifact harvest
(``app.chat.artifact_harvest``, fired from ``app.chat.manager``) reads the
very same routes through :class:`EngineFilesHandle`, an adapter shaped like
the provider file API (``handle.files.list`` / ``.read``) the harvest
consumes — an engine-backed session has no such handle of its own, and
without the adapter the harvest would silently skip the one provider whose
sandbox Agnes cannot keep alive (#2268).

The listing is a bounded breadth-first walk (the engine lists one directory
level per request): at most ``_MAX_LIST_REQUESTS`` directory fetches,
``_MAX_DEPTH`` levels deep, ``max_files`` file entries — past any cap the
result is flagged ``truncated`` rather than silently short. Every path the
engine returns is re-validated by the caller before use; entries whose paths
fail validation are dropped (the engine is trusted infrastructure, but its
listing reflects agent-chosen filenames — adversarial-adjacent input).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from app.chat.workdir import WORKSPACE_LINK_ENTRIES

logger = logging.getLogger(__name__)

#: Providers whose session files live in the ENGINE's own sandbox rather than
#: on this host. One list, shared by the file routes
#: (``app.api.chat_session_files``) and the turn-end artifact harvest
#: (``app.chat.manager``) — a provider added to one and not the other would
#: mean a session whose files are browsable but never harvested (or the
#: reverse), which is precisely the split #2268 was.
ENGINE_SANDBOX_PROVIDERS = frozenset({"kai-agent"})


def is_engine_sandbox(chat_config: object) -> bool:
    """Whether this instance's chat sessions run in the engine's sandbox.

    Defensive ``getattr`` on purpose (same duck-typed-double rule as the
    provider capability flags): a config double without ``provider`` must
    resolve to the local, no-outbound-HTTP path.
    """
    provider = str(getattr(chat_config, "provider", "") or "").strip().lower()
    return provider in ENGINE_SANDBOX_PROVIDERS


#: Same posture as the provider's turn calls (kai_engine_provider.py): the
#: engine answers listing/download from an already-running (or auto-resumed)
#: sandbox, so generous-but-finite timeouts.
_CONNECT_TIMEOUT_SECONDS = 15.0
_READ_TIMEOUT_SECONDS = 30.0

#: Bounds for the breadth-first listing walk.
_MAX_LIST_REQUESTS = 50
_MAX_DEPTH = 6

#: Streamed download chunk size.
_CHUNK_BYTES = 64 * 1024

#: Top-level workspace-TEMPLATE entries, skipped exactly as the host walk skips
#: them (``app/api/chat_session_files.py``). The engine serves this instance's
#: own workspace tarball into its sandbox, so the template lands there too —
#: and the engine's browser filters only DOT-directories, which hides
#: ``.claude`` but not ``scaffolds/`` or ``CLAUDE.md``. Without this the walk
#: reported the operator's bundled scaffold as files the conversation
#: produced: observed on a live kai-agent instance as a listing of
#: ``scaffolds/nodejs-dashboard/{package.json,index.html,postcss.config.js,…}``
#: with the user's actual document nowhere in sight.
#:
#: Derived from the same source of truth as the host side rather than
#: re-typed, so a new template entry cannot be filtered on one surface and
#: leak on the other.
_TEMPLATE_ENTRIES = frozenset(WORKSPACE_LINK_ENTRIES)


class EngineFilesUnavailable(Exception):
    """The engine could not answer (network, 5xx, auth misconfig) — distinct
    from "the engine answered and has no files channel" (listing 404)."""


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(_READ_TIMEOUT_SECONDS, connect=_CONNECT_TIMEOUT_SECONDS)


def _client(base_url: str, token: str, transport: httpx.AsyncBaseTransport | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_timeout(),
        transport=transport,
    )


def _raise_for_engine_status(resp: httpx.Response, *, chat_id: str, what: str) -> None:
    """Map non-2xx, non-404 engine answers onto EngineFilesUnavailable."""
    if resp.status_code in (401, 403):
        logger.error(
            "kai_engine_files: engine rejected the host session JWT (%s, %s for chat %s) — "
            "KAI_HOST_JWT_SECRET / issuer / audience misconfiguration, not a missing file",
            resp.status_code,
            what,
            chat_id,
        )
        raise EngineFilesUnavailable(f"engine rejected credentials ({resp.status_code})")
    if resp.status_code >= 400:
        raise EngineFilesUnavailable(f"engine answered {resp.status_code} for {what}")


async def fetch_engine_listing(
    *,
    base_url: str,
    chat_id: str,
    token: str,
    max_files: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[dict], bool] | None:
    """List the engine sandbox's files for ``chat_id``, flattened.

    Returns ``(entries, truncated)`` with entries shaped like the host
    listing's dicts (``path``/``name``/``size_bytes``; no ``modified_at`` —
    the engine listing carries none), or ``None`` when the engine 404s or
    400s the listing (no files channel for this chat — see the module
    docstring for why a 400 belongs in this bucket too). Raises
    :class:`EngineFilesUnavailable` on any other failure.
    """
    collected: list[dict] = []
    truncated = False
    # Breadth-first over directories; the engine returns one level per call.
    queue: list[tuple[str, int]] = [("", 0)]
    requests_made = 0

    try:
        async with _client(base_url, token, transport) as client:
            while queue:
                if requests_made >= _MAX_LIST_REQUESTS or len(collected) >= max_files:
                    truncated = True
                    break
                dir_path, depth = queue.pop(0)
                resp = await client.get(
                    f"/api/chat/{chat_id}/sandbox/files",
                    params={"path": dir_path} if dir_path else None,
                )
                requests_made += 1
                if dir_path == "" and resp.status_code in (400, 404):
                    return None  # no files channel for this chat
                if resp.status_code == 404:
                    # A subdirectory vanished between the parent listing and
                    # this call — benign in a live sandbox, keep walking.
                    continue
                # A 400 on a CHILD is a different animal: the malformed-id
                # case that makes 400 mean "no files channel" is settled at
                # the root, which this chat id already passed. Swallowing it
                # here would drop that directory's files from an otherwise
                # successful, untruncated answer — silence where the reader
                # would see an outage.
                _raise_for_engine_status(resp, chat_id=chat_id, what="listing")
                try:
                    body = resp.json()
                except ValueError as exc:
                    raise EngineFilesUnavailable("engine listing body is not JSON") from exc
                entries = body.get("entries") if isinstance(body, dict) else None
                if not isinstance(entries, list):
                    raise EngineFilesUnavailable("engine listing has no entries array")
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    kind = entry.get("type")
                    path = entry.get("path")
                    name = entry.get("name")
                    if not isinstance(path, str) or not isinstance(name, str):
                        continue
                    # Template, not session output — drop the whole tree at the
                    # root (a directory the AGENT creates deeper keeps its name
                    # whatever it is called).
                    if depth == 0 and name in _TEMPLATE_ENTRIES:
                        continue
                    if kind == "dir":
                        if depth + 1 < _MAX_DEPTH:
                            queue.append((path, depth + 1))
                        else:
                            truncated = True
                        continue
                    if kind != "file":
                        continue
                    if len(collected) >= max_files:
                        truncated = True
                        break
                    size = entry.get("size")
                    collected.append(
                        {
                            "path": path,
                            "name": name,
                            "size_bytes": int(size) if isinstance(size, (int, float)) else 0,
                            "modified_at": None,
                        }
                    )
    except httpx.HTTPError as exc:
        raise EngineFilesUnavailable(f"engine unreachable: {exc!r}") from exc
    return collected, truncated


async def open_engine_download(
    *,
    base_url: str,
    chat_id: str,
    path: str,
    token: str,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[AsyncIterator[bytes], _DownloadHandle] | None:
    """Open a streaming download of one engine sandbox file.

    Returns ``(byte_iterator, handle)`` — the caller must arrange for
    ``handle.aclose()`` to run after the response is sent (Starlette
    ``BackgroundTask``) — or ``None`` when the engine 404s or 400s the path
    (see the module docstring: a 400 means this chat id cannot be served,
    the same "nothing here" answer as an unknown path). Raises
    :class:`EngineFilesUnavailable` on other engine failures and
    :class:`EngineFileTooLarge` when Content-Length already exceeds
    ``max_bytes`` (a missing Content-Length is enforced inside the stream:
    it aborts past the cap, which tears the download — visible failure, the
    alternative is a silently truncated file).
    """
    client = _client(base_url, token, transport)
    try:
        req = client.build_request(
            "GET",
            f"/api/chat/{chat_id}/sandbox/file/download",
            params={"path": path},
        )
        resp = await client.send(req, stream=True)
        if resp.status_code in (400, 404):
            await resp.aclose()
            await client.aclose()
            return None
        try:
            _raise_for_engine_status(resp, chat_id=chat_id, what="download")
        except EngineFilesUnavailable:
            await resp.aclose()
            await client.aclose()
            raise
        declared = resp.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            await resp.aclose()
            await client.aclose()
            raise EngineFileTooLarge(int(declared))
    except httpx.HTTPError as exc:
        await client.aclose()
        raise EngineFilesUnavailable(f"engine unreachable: {exc!r}") from exc

    handle = _DownloadHandle(client=client, response=resp)

    async def _iter() -> AsyncIterator[bytes]:
        sent = 0
        try:
            async for chunk in resp.aiter_bytes(_CHUNK_BYTES):
                sent += len(chunk)
                if sent > max_bytes:
                    # Headers are already on the wire — aborting mid-stream is
                    # the only honest option left (never a silently cut file).
                    raise EngineFileTooLarge(sent)
                yield chunk
        except BaseException:
            # Starlette runs the cleanup BackgroundTask only after a stream
            # that finished normally — an abort (cap hit, engine error,
            # client disconnect → GeneratorExit) must close the upstream
            # response + client here or the httpx connection leaks. aclose()
            # is idempotent, so the BackgroundTask's later call is harmless.
            await handle.aclose()
            raise

    return _iter(), handle


class EngineFileTooLarge(Exception):
    """The engine file exceeds the proxy cap (maps to 413 when caught
    before the first byte)."""

    def __init__(self, size: int) -> None:
        super().__init__(f"engine file exceeds proxy cap ({size} bytes)")
        self.size = size


class _DownloadHandle:
    """Owns the streaming response + client for post-response cleanup."""

    def __init__(self, *, client: httpx.AsyncClient, response: httpx.Response) -> None:
        self._client = client
        self._response = response

    async def aclose(self) -> None:
        try:
            await self._response.aclose()
        finally:
            await self._client.aclose()


async def fetch_engine_file_bytes(
    *,
    base_url: str,
    chat_id: str,
    path: str,
    token: str,
    max_bytes: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes | None:
    """Fetch one engine sandbox file fully into memory (save-artefact path).

    ``None`` on engine 404 (unknown path / no channel). Raises
    :class:`EngineFileTooLarge` past ``max_bytes`` — enforced while
    streaming, so an unbounded body cannot balloon memory first.
    """
    opened = await open_engine_download(
        base_url=base_url,
        chat_id=chat_id,
        path=path,
        token=token,
        max_bytes=max_bytes,
        transport=transport,
    )
    if opened is None:
        return None
    iterator, handle = opened
    chunks: list[bytes] = []
    try:
        async for chunk in iterator:
            chunks.append(chunk)
    finally:
        await handle.aclose()
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Harvest adapter: the engine's file routes, shaped like a sandbox handle
# ---------------------------------------------------------------------------


@dataclass
class EngineEntry:
    """``EntryInfo``-shaped row for :class:`EngineFilesHandle`'s listing —
    the same three fields the docker provider's shim exposes
    (``app/chat/docker_provider.py``), because that is the shape
    ``app.chat.artifact_harvest`` reads (``.name`` + ``.type``)."""

    name: str
    path: str
    type: str


def _sandbox_rel(path: str) -> str:
    """Sandbox-ABSOLUTE path -> the workspace-RELATIVE one the engine speaks.

    The harvest addresses files as ``{SANDBOX_WORKDIR}/outputs/x`` (the path
    they have inside a native sandbox); the engine's routes take a path
    relative to its own sandbox workspace root (``outputs/x``). This is the
    whole translation — no traversal risk beyond what the engine already
    enforces on its side, but a leading ``/`` would simply 404, so strip it.
    """
    from app.chat.provider import SANDBOX_WORKDIR

    rel = path
    rel = rel.removeprefix(SANDBOX_WORKDIR)
    return rel.lstrip("/")


async def fetch_engine_dir(
    *,
    base_url: str,
    chat_id: str,
    token: str,
    path: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict] | None:
    """One directory level of the engine sandbox, unfiltered.

    The sibling :func:`fetch_engine_listing` walks the whole tree and drops
    the workspace template — right for a file BROWSER, wrong for the harvest,
    which wants exactly the contents of one directory (``outputs/``) and
    nothing else. ``None`` when the engine 404s or 400s (no such dir / no
    files channel for this chat — see the module docstring for the 400
    case); :class:`EngineFilesUnavailable` otherwise.
    """
    try:
        async with _client(base_url, token, transport) as client:
            resp = await client.get(
                f"/api/chat/{chat_id}/sandbox/files",
                params={"path": path} if path else None,
            )
            if resp.status_code in (400, 404):
                return None
            _raise_for_engine_status(resp, chat_id=chat_id, what="listing")
            try:
                body = resp.json()
            except ValueError as exc:
                raise EngineFilesUnavailable("engine listing body is not JSON") from exc
    except httpx.HTTPError as exc:
        raise EngineFilesUnavailable(f"engine unreachable: {exc!r}") from exc
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise EngineFilesUnavailable("engine listing has no entries array")
    return [e for e in entries if isinstance(e, dict)]


class EngineFilesHandle:
    """The sandbox file API (``handle.files.list`` / ``.read``) over the
    engine's HTTP file routes (#2268).

    ``app.chat.artifact_harvest`` reads a session's ``outputs/`` back out of
    a live sandbox through the provider's file API. An engine-backed session
    has no such handle — ``KaiEngineHandle`` speaks turns, not files — so the
    turn-end harvest would silently skip exactly the provider whose sandbox
    is ephemeral and out of Agnes's control (both ``keepalive`` and
    ``destroy`` are no-ops there). This adapter closes that gap with the
    plumbing this module already ships behind the Files panel; it is not a
    second file subsystem.

    ``self.files is self``: the harvest reads ``handle.files``, and there is
    nothing for a separate object to hold.
    """

    def __init__(
        self,
        *,
        base_url: str,
        chat_id: str,
        token: str,
        max_read_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url
        self._chat_id = chat_id
        self._token = token
        self._max_read_bytes = max_read_bytes
        self._transport = transport

    @property
    def files(self) -> EngineFilesHandle:
        return self

    async def list(self, path: str) -> list[EngineEntry]:
        """Entries of one sandbox directory.

        Raises rather than returning ``[]`` when the directory is absent —
        that is how the harvest recognises "this session produced nothing"
        (it catches, logs at debug, and returns no artifacts).
        """
        entries = await fetch_engine_dir(
            base_url=self._base_url,
            chat_id=self._chat_id,
            token=self._token,
            path=_sandbox_rel(path),
            transport=self._transport,
        )
        if entries is None:
            raise FileNotFoundError(path)
        out: list[EngineEntry] = []
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            raw_path = entry.get("path")
            out.append(
                EngineEntry(
                    name=name,
                    path=raw_path if isinstance(raw_path, str) else name,
                    type="DIR" if entry.get("type") == "dir" else "FILE",
                )
            )
        return out

    async def read(self, path: str, format: str = "bytes") -> bytes:
        data = await fetch_engine_file_bytes(
            base_url=self._base_url,
            chat_id=self._chat_id,
            path=_sandbox_rel(path),
            token=self._token,
            max_bytes=self._max_read_bytes,
            transport=self._transport,
        )
        if data is None:
            raise FileNotFoundError(path)
        return data
