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
  chat id (e.g. a pre-provider-switch ``chat_<hex>`` session) or an engine
  build predating the sandbox-files routes. Deliberately collapsed, no body
  sniffing: both mean the honest answer is ``supported: false``, never an
  error. A 404 on the DOWNLOAD maps to a plain 404 (unknown path is by far
  the common case once the listing worked).
- Engine 401/403 = the host JWT contract is misconfigured (secret/iss/aud
  drift) — loud log, then :class:`EngineFilesUnavailable`, because "no
  files" would mask an operator problem.
- Engine 5xx / network failure = :class:`EngineFilesUnavailable` (the route
  answers 502).

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

import httpx

logger = logging.getLogger(__name__)

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
    the engine listing carries none), or ``None`` when the engine 404s the
    listing (no files channel for this chat). Raises
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
                if resp.status_code == 404:
                    if dir_path == "":
                        return None  # no files channel for this chat
                    continue  # a subdirectory vanished mid-walk; keep going
                _raise_for_engine_status(resp, chat_id=chat_id, what="listing")
                body = resp.json()
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
    ``BackgroundTask``) — or ``None`` when the engine 404s the path.
    Raises :class:`EngineFilesUnavailable` on other engine failures and
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
        if resp.status_code == 404:
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
