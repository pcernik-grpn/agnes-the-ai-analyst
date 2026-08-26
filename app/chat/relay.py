"""Sandbox-local loopback relay for the chat secret broker.

Runs inside the chat sandbox alongside the CLI subprocesses (``claude``,
``agnes``, ``agnes mcp``). It is the only thing in the sandbox that ever
holds a broker ticket, and it holds it **in memory only** — never in
``os.environ`` (subprocess envs are inherited/inspectable) and never on
disk. CLI subprocesses are pointed at this relay's loopback address with a
dummy key; the relay attaches the real ticket as the ``Authorization``
header on the outbound leg to the Agnes server's broker routes
(``/api/broker/{anthropic,agnes-api,agnes-mcp,data-apps}``).

Tickets are pushed over stdin by the runner (see ``app/chat/runner.py``)
via ``set_tickets`` at spawn and after every resume. Until a fresh ticket
has been pushed since the most recent resume signal, the relay refuses to
serve (``disarm`` / the ``_armed`` flag) — see AC-G-resume-fresh.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Maps the inbound loopback path prefix to the ticket scope required to
# forward it. `/anthropic` and `/agnes-api` ride the `main` ticket;
# `/agnes-mcp` rides the `mcp` ticket (spawned as a separate subprocess with
# a narrower scope); `/data-apps` rides the `data_apps` ticket (2026-07-24
# wave 3B) — a narrower scope than `main`, confined server-side to the
# `/api/data-apps` control-plane surface (see `app/api/broker.py`'s
# `data_apps_broker`).
_SCOPE_FOR_PREFIX = {
    "/anthropic": "main",
    "/agnes-api": "main",
    "/agnes-mcp": "mcp",
    "/data-apps": "data_apps",
    # Git smart-HTTP for a hosted app's repo. Shares the `data_apps` ticket
    # but is deliberately NOT an envelope route: git speaks raw HTTP with
    # binary bodies and its own content types, which the {method, path, body}
    # JSON envelope cannot carry. It rides the transparent leg instead, like
    # `/anthropic` — the broker attaches the real git credential on the
    # outbound side, so the sandbox never holds one.
    "/data-apps.git": "data_apps",
}

# Prefixes whose broker route replays a ``{method, path, body}`` envelope
# in-process under the ticket's resolved identity (live RBAC). The in-sandbox
# CLI / MCP server make NATIVE REST calls (GET/POST/… to a real path), and the
# relay always POSTs to the broker, so the native method + target path must be
# carried in the envelope — otherwise the call arrives as
# ``POST /api/broker/agnes-api/<subpath>`` and 405s (the broker only serves the
# exact ``/agnes-api``, ``/agnes-mcp`` + ``/data-apps`` envelope routes).
# ``/anthropic`` is NOT here: it is a transparent external proxy that forwards
# the raw body + SDK headers to the pinned Anthropic host at the native subpath.
_ENVELOPE_PREFIXES = frozenset({"/agnes-api", "/agnes-mcp", "/data-apps"})

# The `/anthropic` leg proxies LLM completions that routinely run for tens of
# seconds to minutes. httpx's 5s default read timeout would abort every real
# completion here — the sandbox-side twin of the same bug on the broker's
# outbound client — so the relay's outbound client gets a generous read timeout
# while keeping connect/write/pool bounded. Fast `/agnes-api` and `/agnes-mcp`
# calls still return promptly; the long read timeout only permits slow ones.
_OUTBOUND_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)



async def _read_request_body(reader: "asyncio.StreamReader", headers: dict[str, str]) -> bytes:
    """The request body, whether it was sent sized or chunked.

    `Content-Length` alone is not enough now that git rides this relay: git
    sends a pack over `Transfer-Encoding: chunked` once it grows past a small
    threshold, and a chunked request carries no length — so the old
    `readexactly(length) if length else b""` forwarded an EMPTY body and a
    bigger push silently arrived with nothing in it. Small pushes, being
    sized, worked, which is what made it look like a size limit rather than a
    missing code path. (Devin Review on #1252.)

    Trailers after the terminating zero-length chunk are consumed and dropped:
    nothing downstream reads them, and leaving them in the stream would
    desynchronise the next request on a kept-alive connection.
    """
    if "chunked" in headers.get("transfer-encoding", "").lower():
        chunks: list[bytes] = []
        while True:
            size_line = await reader.readline()
            if not size_line:
                break
            size_field = size_line.split(b";", 1)[0].strip()
            if not size_field:
                continue
            try:
                size = int(size_field, 16)
            except ValueError:
                break
            if size == 0:
                while True:  # trailers, terminated by a blank line
                    trailer = await reader.readline()
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)  # the CRLF that closes each chunk
        return b"".join(chunks)

    length = int(headers.get("content-length", "0") or "0")
    return await reader.readexactly(length) if length else b""


class Relay:
    """In-sandbox HTTP forwarder that never persists real credentials.

    Binds a loopback-only (``127.0.0.1``) listener. CLI subprocesses talk to
    it with a dummy key; it attaches the real short-lived ticket on the
    outbound leg to the Agnes server's broker routes.
    """

    def __init__(self, server_url: str) -> None:
        self._server_url = server_url.rstrip("/")
        self._main_ticket: Optional[str] = None
        self._mcp_ticket: Optional[str] = None
        self._data_apps_ticket: Optional[str] = None
        self._armed = False
        self._server: Optional[asyncio.base_events.Server] = None
        self._client: Optional[httpx.AsyncClient] = None

    def set_tickets(self, main: str, mcp: str, data_apps: str = "") -> None:
        """Store fresh tickets in memory only.

        Never writes to ``os.environ`` or disk — the only copies of the
        ticket values live in these instance attributes. ``data_apps``
        defaults to ``""`` so a caller on the old (pre-wave-3B) two-ticket
        ``ticket_push`` frame shape still works unchanged.
        """
        self._main_ticket = main
        self._mcp_ticket = mcp
        self._data_apps_ticket = data_apps
        self._armed = True

    def disarm(self) -> None:
        """Refuse to serve until the next ``set_tickets`` call.

        Used on the resume path: between "the old runner paused" and "fresh
        tickets pushed for the resumed session", the relay must not forward
        with a stale ticket.
        """
        self._armed = False

    def _ticket_for_path(self, path: str) -> Optional[str]:
        prefix = "/" + path.lstrip("/").split("/", 1)[0]
        scope = _SCOPE_FOR_PREFIX.get(prefix)
        if scope == "main":
            return self._main_ticket
        if scope == "mcp":
            return self._mcp_ticket
        if scope == "data_apps":
            return self._data_apps_ticket
        return None

    # Headers never forwarded upstream: hop-by-hop framing (recomputed by
    # httpx) plus the caller's credentials, which the relay REPLACES with the
    # ticket. Everything else the in-sandbox SDK set (Content-Type,
    # anthropic-version, …) must reach the broker or the Anthropic API rejects
    # the call (Devin review on #849).
    _DROP_INBOUND_HEADERS = frozenset(
        {"host", "content-length", "connection", "transfer-encoding", "authorization", "x-api-key"}
    )

    def _transparent_request(
        self,
        path: str,
        inbound_headers: Optional[dict[str, str]],
    ) -> tuple[str, dict[str, str]]:
        """URL + outbound headers for the transparent (``/anthropic``) leg.

        Shared by the buffered ``_forward`` branch and the streaming path in
        ``_handle_connection`` so the header-drop/credential-swap policy can
        never drift between the two. Raises ``RuntimeError`` when the relay
        is not armed or holds no ticket for the path's scope.
        """
        if not self._armed:
            raise RuntimeError("relay not armed: no tickets pushed since last resume")
        ticket = self._ticket_for_path(path)
        if not ticket:
            raise RuntimeError(f"relay has no ticket for the scope of path {path!r}")
        headers = {k: v for k, v in (inbound_headers or {}).items() if k.lower() not in self._DROP_INBOUND_HEADERS}
        headers["Authorization"] = f"Bearer {ticket}"
        return f"{self._server_url}/api/broker{path}", headers

    async def _forward(
        self,
        path: str,
        body: bytes,
        inbound_headers: Optional[dict[str, str]] = None,
        method: str = "POST",
    ) -> httpx.Response:
        """Forward an inbound loopback request to the real broker route.

        For ``/agnes-api`` and ``/agnes-mcp`` the broker replays a
        ``{method, path, body}`` envelope under the ticket's resolved identity,
        so the native HTTP ``method`` and target sub-path (with query string)
        are wrapped into that envelope and POSTed to the exact broker route.
        For ``/anthropic`` (transparent external proxy) the caller's headers
        (minus hop-by-hop framing and the dummy credential, which is replaced
        by the ticket) and raw body pass through to the native sub-path so the
        broker can hand ``Content-Type``/``anthropic-version`` to
        ``api.anthropic.com``. Raises ``RuntimeError`` if the relay is not
        armed or holds no ticket for the requested path's scope — callers must
        never fall back to an unauthenticated or stale-ticket request.
        """
        if not self._armed:
            raise RuntimeError("relay not armed: no tickets pushed since last resume")
        ticket = self._ticket_for_path(path)
        if not ticket:
            raise RuntimeError(f"relay has no ticket for the scope of path {path!r}")

        prefix = "/" + path.lstrip("/").split("/", 1)[0]
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_OUTBOUND_TIMEOUT)
        try:
            if prefix in _ENVELOPE_PREFIXES:
                # Carry the native method + sub-path (+ query) + JSON body in
                # the envelope the broker's replay expects; POST to the exact
                # /agnes-api or /agnes-mcp route (never the native sub-path,
                # which 405s).
                subpath = path[len(prefix) :] or "/"
                parsed: object | None = None
                if body:
                    try:
                        parsed = json.loads(body)
                    except (ValueError, UnicodeDecodeError):
                        parsed = None
                envelope = {"method": method.upper(), "path": subpath, "body": parsed}
                url = f"{self._server_url}/api/broker{prefix}"
                headers = {"Authorization": f"Bearer {ticket}"}
                return await client.post(url, json=envelope, headers=headers)

            # Transparent proxy (``/anthropic``, ``/data-apps.git``): raw body
            # + caller headers. The METHOD has to pass through: git's very
            # first call is `GET /info/refs?service=git-upload-pack`, and a
            # hardcoded POST turned it into a 405 before the repo was ever
            # reached. `/anthropic` is unaffected — it only ever POSTs.
            url, headers = self._transparent_request(path, inbound_headers)
            return await client.request(method.upper(), url, content=body, headers=headers)
        finally:
            if owns_client:
                await client.aclose()

    async def start(self, port_hint: int = 0) -> int:
        """Bind a loopback-only HTTP listener and return the bound port.

        Uses stdlib ``asyncio.start_server`` with a minimal HTTP/1.1 request
        parser — no new dependency for the listener side; the outbound leg
        to the Agnes server uses ``httpx`` (already a project dependency).
        """
        self._client = httpx.AsyncClient(timeout=_OUTBOUND_TIMEOUT)

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await self._handle_connection(reader, writer)
            except Exception:
                logger.exception("relay: error handling loopback connection")
            finally:
                with contextlib.suppress(Exception):
                    writer.close()
                    await writer.wait_closed()

        self._server = await asyncio.start_server(_handle, host="127.0.0.1", port=port_hint)
        assert self._server.sockets is not None
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_line = await reader.readline()
        if not request_line:
            return
        try:
            method, path, _version = request_line.decode("latin-1").strip().split(" ", 2)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()

        body = await _read_request_body(reader, headers)

        prefix = "/" + path.lstrip("/").split("/", 1)[0]
        try:
            if prefix == "/anthropic" and self._client is not None:
                # Streaming-capable transparent leg: the model's completion
                # arrives as SSE, and buffering it here (the old
                # ``client.post`` + Content-Length write) collapsed every
                # token delta into one burst at turn end — the user stared
                # at silence, then the whole answer appeared at once. Open
                # the upstream response WITHOUT reading the body; if it is
                # an event stream, forward chunks as they arrive.
                url, out_headers = self._transparent_request(path, headers)
                req = self._client.build_request("POST", url, content=body, headers=out_headers)
                resp = await self._client.send(req, stream=True)
                ctype = resp.headers.get("content-type", "")
                if 200 <= resp.status_code < 300 and ctype.startswith("text/event-stream"):
                    try:
                        await self._stream_response(writer, resp, ctype)
                    finally:
                        await resp.aclose()
                    return
                # Non-stream (errors, count_tokens JSON, …): fall through to
                # the buffered write below with identical legacy behavior.
                try:
                    await resp.aread()
                finally:
                    await resp.aclose()
            else:
                resp = await self._forward(path, body, headers, method=method)
        except RuntimeError as exc:
            payload = str(exc).encode()
            writer.write(f"HTTP/1.1 503 Service Unavailable\r\nContent-Length: {len(payload)}\r\n\r\n".encode())
            writer.write(payload)
            await writer.drain()
            return

        content = resp.content
        writer.write(f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}\r\n".encode())
        # Forward Content-Type so the in-sandbox SDK/CLI (both httpx-based)
        # decode the body correctly — without it httpx defaults to
        # application/octet-stream and JSON parsing can take a wrong path
        # (Devin review on #849). Content-Length is recomputed from the fully
        # buffered body; other hop-by-hop headers are deliberately not relayed.
        ctype = resp.headers.get("content-type")
        if ctype:
            writer.write(f"Content-Type: {ctype}\r\n".encode())
        writer.write(f"Content-Length: {len(content)}\r\n\r\n".encode())
        writer.write(content)
        await writer.drain()

    async def _stream_response(
        self,
        writer: asyncio.StreamWriter,
        resp: httpx.Response,
        ctype: str,
    ) -> None:
        """Forward an SSE upstream response chunk-by-chunk as it arrives.

        HTTP/1.1 chunked transfer encoding on the raw loopback socket — the
        in-sandbox CLI's HTTP client consumes chunked bodies natively, and
        the terminating ``0\\r\\n\\r\\n`` marks end-of-stream without a
        Content-Length (unknowable up front). ``aiter_bytes`` (not
        ``aiter_raw``) so httpx undoes any upstream ``Content-Encoding``
        first — we do not forward that header, so forwarding still-compressed
        raw bytes would corrupt the body. ``drain()`` after every chunk is
        the whole point: it pushes each token delta to the CLI immediately.
        """
        writer.write(
            f"HTTP/1.1 {resp.status_code} {resp.reason_phrase}\r\n"
            f"Content-Type: {ctype}\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        await writer.drain()
        async for chunk in resp.aiter_bytes():
            if not chunk:
                continue
            writer.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        await writer.drain()

    async def stop(self) -> None:
        """Close the listener and the outbound client (used at session teardown)."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
