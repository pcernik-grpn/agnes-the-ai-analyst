"""Sandbox-local loopback relay (`app/chat/relay.py`).

The relay is the only thing inside the chat sandbox that ever holds a broker
ticket, and it must hold it in memory only — never in `os.environ` (which
subprocesses inherit and which can be dumped via `env`/`/proc/*/environ`)
and never on disk. These tests pin that guarantee plus the fail-closed
behavior before any ticket has been pushed.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.chat.relay import Relay


class _CapturingClient:
    """Records the last post(url, json=..., content=..., headers=...) call."""

    def __init__(self):
        self.calls = []

    async def post(self, url, json=None, content=None, headers=None):
        return await self.request("POST", url, json=json, content=content, headers=headers)

    async def request(self, method, url, json=None, content=None, headers=None):
        # The transparent leg forwards the caller's METHOD (git opens with a
        # GET); the envelope leg always POSTs. Record both through one entry
        # point so a test can assert on the method that actually went out.
        self.calls.append(
            {"method": method, "url": url, "json": json, "content": content, "headers": headers}
        )

        class _Resp:
            status_code = 200
            content = b"{}"
            reason_phrase = "OK"
            headers: dict = {}

        return _Resp()


def test_relay_wraps_agnes_api_native_call_into_envelope():
    """The broker's /agnes-api route replays a {method, path, body} envelope.
    The in-sandbox CLI makes NATIVE REST calls (e.g. GET /api/v2/catalog) and
    the relay always POSTs to the broker, so the native method + target path
    (with query string) MUST be carried in the envelope — otherwise the call
    arrives as POST /api/broker/agnes-api/<subpath> and 405s."""

    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        fake = _CapturingClient()
        r._client = fake
        await r._forward("/agnes-api/api/v2/catalog?refresh=0", b"", {"x": "y"}, method="GET")
        return fake.calls[-1]

    call = asyncio.run(_run())
    # Envelope POSTed to the EXACT broker route, not the native subpath.
    assert call["url"] == "http://agnes:8000/api/broker/agnes-api"
    assert call["json"] == {"method": "GET", "path": "/api/v2/catalog?refresh=0", "body": None}
    assert call["headers"]["Authorization"] == "Bearer TOKMAIN"


def test_relay_wraps_agnes_mcp_post_body_into_envelope():
    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        fake = _CapturingClient()
        r._client = fake
        await r._forward("/agnes-mcp/api/query", json.dumps({"sql": "SELECT 1"}).encode(), {}, method="POST")
        return fake.calls[-1]

    call = asyncio.run(_run())
    assert call["url"] == "http://agnes:8000/api/broker/agnes-mcp"
    assert call["json"] == {"method": "POST", "path": "/api/query", "body": {"sql": "SELECT 1"}}
    assert call["headers"]["Authorization"] == "Bearer TOKMCP"  # mcp scope ticket


def test_relay_wraps_data_apps_post_body_into_envelope():
    """The broker's /data-apps route (wave 3B, data_apps scope) follows the
    same envelope contract as /agnes-api and /agnes-mcp."""

    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP", data_apps="TOKDATAAPPS")
        fake = _CapturingClient()
        r._client = fake
        await r._forward("/data-apps/api/data-apps", b"", {}, method="GET")
        return fake.calls[-1]

    call = asyncio.run(_run())
    assert call["url"] == "http://agnes:8000/api/broker/data-apps"
    assert call["json"] == {"method": "GET", "path": "/api/data-apps", "body": None}
    assert call["headers"]["Authorization"] == "Bearer TOKDATAAPPS"


def test_relay_anthropic_stays_transparent_not_enveloped():
    """The /anthropic leg is a transparent external proxy — it must NOT be
    wrapped in an envelope; the raw body + SDK headers pass through to the
    broker's Anthropic proxy at the native subpath."""

    async def _run():
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        fake = _CapturingClient()
        r._client = fake
        await r._forward(
            "/anthropic/v1/messages", b'{"model":"x"}', {"content-type": "application/json"}, method="POST"
        )
        return fake.calls[-1]

    call = asyncio.run(_run())
    assert call["url"] == "http://agnes:8000/api/broker/anthropic/v1/messages"
    assert call["json"] is None  # raw content, not an envelope
    assert call["content"] == b'{"model":"x"}'


def test_relay_holds_tickets_in_memory_only():
    r = Relay(server_url="http://agnes:8000")
    r.set_tickets(main="TOKMAIN", mcp="TOKMCP", data_apps="TOKDATAAPPS")

    # the relay must not export tickets to the process environment
    assert "TOKMAIN" not in os.environ.values()
    assert "TOKMCP" not in os.environ.values()
    assert "TOKDATAAPPS" not in os.environ.values()
    # ...but they must be held in-memory on the instance
    assert r._main_ticket == "TOKMAIN"
    assert r._mcp_ticket == "TOKMCP"
    assert r._data_apps_ticket == "TOKDATAAPPS"


def test_relay_refuses_before_tickets():
    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        with pytest.raises(RuntimeError):
            await r._forward("/agnes-api", b"{}")

    asyncio.run(_run())


def test_relay_refuses_after_disarm():
    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        r.disarm()
        with pytest.raises(RuntimeError):
            await r._forward("/agnes-api", b"{}")

    asyncio.run(_run())


def test_relay_refuses_wrong_scope_ticket():
    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        # only an mcp ticket is set; /agnes-api requires the main scope
        r.set_tickets(main="", mcp="TOKMCP")
        with pytest.raises(RuntimeError):
            await r._forward("/agnes-api", b"{}")

    asyncio.run(_run())


def test_relay_forwards_sdk_headers_and_swaps_credential():
    """The relay must forward the caller's headers (Content-Type,
    anthropic-version, …) so the broker's Anthropic proxy can pass them to
    api.anthropic.com, while dropping hop-by-hop framing and REPLACING the
    dummy credential with the real ticket (Devin review on #849)."""
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        content = b"{}"
        reason_phrase = "OK"
        headers: dict = {}

    class _FakeClient:
        async def post(self, url, content=None, headers=None):
            return await self.request("POST", url, content=content, headers=headers)

        async def request(self, method, url, content=None, headers=None):
            captured["method"] = method
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResp()

    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        r._client = _FakeClient()
        inbound = {
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "authorization": "Bearer dummy-sandbox-key",
            "x-api-key": "dummy-sandbox-key",
            "host": "127.0.0.1",
            "content-length": "2",
        }
        await r._forward("/anthropic/v1/messages", b"{}", inbound)

    asyncio.run(_run())
    h = captured["headers"]
    lower = {k.lower() for k in h}
    # SDK headers survive
    assert h["content-type"] == "application/json"
    assert h["anthropic-version"] == "2023-06-01"
    # ticket replaces the dummy credential; the dummy never leaks upward
    assert h["Authorization"] == "Bearer TOKMAIN"
    assert "dummy-sandbox-key" not in " ".join(h.values())
    # hop-by-hop framing + the caller's dummy x-api-key are dropped
    assert "host" not in lower
    assert "content-length" not in lower
    assert "x-api-key" not in lower


def test_relay_outbound_client_has_generous_read_timeout():
    """Regression: the relay's outbound httpx client proxies LLM completions
    on the `/anthropic` leg. httpx's 5s default read timeout would abort every
    real completion (the sandbox-side twin of the broker's timeout bug), so
    ``start()`` must build the client with a generous read timeout."""
    import httpx

    captured: dict = {}

    orig = httpx.AsyncClient

    def _spy(*a, **k):
        captured["timeout"] = k.get("timeout")
        return orig(*a, **k)

    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        import app.chat.relay as relay_mod

        real = relay_mod.httpx.AsyncClient
        relay_mod.httpx.AsyncClient = _spy  # type: ignore[assignment]
        try:
            port = await r.start(port_hint=0)
            assert port > 0
        finally:
            relay_mod.httpx.AsyncClient = real  # type: ignore[assignment]
            await r.stop()

    asyncio.run(_run())
    t = captured["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.read is not None and t.read >= 60.0, t


def test_relay_streams_sse_chunks_incrementally():
    """The transparent /anthropic leg must forward SSE chunks AS THEY ARRIVE
    (chunked transfer), not buffer the whole completion — buffering collapsed
    every token delta into one end-of-turn burst. The fake upstream gates its
    second chunk on an event: the first chunk MUST reach the loopback client
    while the gate is still closed (a buffering relay would hang and trip the
    read timeout)."""

    async def _run():
        gate = asyncio.Event()

        class _Resp:
            status_code = 200
            reason_phrase = "OK"
            headers = {"content-type": "text/event-stream"}

            async def aiter_bytes(self):
                yield b"event: first\n\n"
                await gate.wait()
                yield b"event: second\n\n"

            async def aread(self):
                raise AssertionError("SSE body must not be buffered")

            async def aclose(self):
                return None

        class _Client:
            def build_request(self, method, url, content=None, headers=None):
                return (method, url)

            async def send(self, req, stream=False):
                assert stream is True
                return _Resp()

            async def aclose(self):
                return None

        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        port = await r.start()
        r._client = _Client()  # replace the real outbound client post-start
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()

            # First chunk must arrive while the gate is CLOSED.
            head = b""
            while b"event: first" not in head:
                head += await asyncio.wait_for(reader.read(1024), timeout=5)
            assert b"Transfer-Encoding: chunked" in head
            assert b"Content-Type: text/event-stream" in head

            gate.set()
            tail = head
            while b"event: second" not in tail or b"0\r\n\r\n" not in tail:
                tail += await asyncio.wait_for(reader.read(1024), timeout=5)
            writer.close()
        finally:
            await r.stop()

    asyncio.run(_run())


def test_relay_non_sse_anthropic_response_stays_buffered():
    """Non-stream /anthropic responses (JSON endpoints, upstream errors) keep
    the legacy buffered Content-Length write — only event streams switch to
    chunked forwarding."""

    async def _run():
        class _Resp:
            status_code = 400
            reason_phrase = "Bad Request"
            headers = {"content-type": "application/json"}
            content = b'{"error":{"message":"nope"}}'

            async def aread(self):
                return self.content

            async def aclose(self):
                return None

        class _Client:
            def build_request(self, method, url, content=None, headers=None):
                return (method, url)

            async def send(self, req, stream=False):
                return _Resp()

            async def aclose(self):
                return None

        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")
        port = await r.start()
        r._client = _Client()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"POST /anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
            await writer.drain()
            # Accumulate until the whole buffered response has arrived — a
            # single read() returns whatever bytes happen to be available and
            # frequently yields only the status line, which made this test
            # flaky. Same read-until-complete idiom as the streaming test above.
            data = b""
            while b'{"error":{"message":"nope"}}' not in data:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
                if not chunk:  # EOF — fall through to the assertions below
                    break
                data += chunk
            assert b"HTTP/1.1 400" in data
            assert b"Content-Length: 28" in data
            assert b'{"error":{"message":"nope"}}' in data
            writer.close()
        finally:
            await r.stop()

    asyncio.run(_run())


def test_relay_carries_git_transparently_with_the_callers_method():
    """`/data-apps.git` is a transport for git smart-HTTP, so it rides the
    transparent leg (raw body, caller headers) rather than the JSON envelope —
    and the METHOD has to survive: git's first call is
    `GET /info/refs?service=git-upload-pack`, which a hardcoded POST turned
    into a 405 before the repo was ever reached.
    """
    client = _CapturingClient()

    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP", data_apps="TOKAPPS")
        r._client = client
        await r._forward(
            "/data-apps.git/my-app/info/refs?service=git-upload-pack",
            b"",
            {"content-type": "application/x-git-upload-pack-request"},
            method="GET",
        )

    asyncio.run(_run())
    call = client.calls[-1]
    assert call["method"] == "GET", "the caller's method must reach the broker"
    assert call["url"] == (
        "http://agnes:8000/api/broker/data-apps.git/my-app/info/refs?service=git-upload-pack"
    ), "path AND query must pass through untouched"
    assert call["json"] is None, "git must not be wrapped in the JSON envelope"
    assert call["headers"]["Authorization"] == "Bearer TOKAPPS", "rides the data_apps ticket"


def test_relay_refuses_git_without_a_data_apps_ticket():
    """Fail closed: no ticket for the scope means no request, never an
    unauthenticated one."""
    import pytest

    async def _run() -> None:
        r = Relay(server_url="http://agnes:8000")
        r.set_tickets(main="TOKMAIN", mcp="TOKMCP")  # no data_apps ticket
        with pytest.raises(RuntimeError, match="no ticket for the scope"):
            await r._forward("/data-apps.git/my-app/info/refs", b"", {}, method="GET")

    asyncio.run(_run())
