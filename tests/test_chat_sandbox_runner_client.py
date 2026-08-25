"""SandboxRunnerClient unit tests — mock the sidecar at the HTTP boundary.

Uses the "fake SDK under the real code"
approach: an `httpx.MockTransport` stands in for the apps-runner sidecar so the
real client code (paths, headers, base64 framing, error mapping) runs.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from app.chat.sandbox_runner_client import (
    SandboxRunnerClient,
    SandboxRunnerError,
    SandboxRunnerUnavailable,
)


def _client(handler, **kw) -> SandboxRunnerClient:
    return SandboxRunnerClient(
        base_url="http://apps-runner:8600",
        token="tok",
        transport=httpx.MockTransport(handler),
        **kw,
    )


def test_up_posts_spec_with_token_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Runner-Token")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "started"})

    async def _run():
        c = _client(handler)
        out = await c.up("agnes-chatsbx-a-1", {"image": "agnes-chat-sandbox:dev"})
        assert out == {"status": "started"}

    asyncio.run(_run())
    assert seen["url"] == "http://apps-runner:8600/sandboxes/agnes-chatsbx-a-1/up"
    assert seen["token"] == "tok"
    assert seen["body"] == {"spec": {"image": "agnes-chat-sandbox:dev"}}


def test_error_status_raises_runner_error_with_detail():
    def handler(request):
        return httpx.Response(400, json={"detail": "image_not_allowed"})

    async def _run():
        c = _client(handler)
        with pytest.raises(SandboxRunnerError) as exc:
            await c.up("agnes-chatsbx-a-1", {})
        assert exc.value.status_code == 400
        assert exc.value.detail == "image_not_allowed"

    asyncio.run(_run())


def test_transport_error_raises_unavailable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    async def _run():
        c = _client(handler)
        with pytest.raises(SandboxRunnerUnavailable):
            await c.status("agnes-chatsbx-a-1")

    asyncio.run(_run())


def test_defaults_come_from_the_apps_runner_env(monkeypatch):
    """Same env contract as src/data_apps/runner_client.py — one sidecar, one
    URL/token pair, whether the caller is data apps or chat."""
    monkeypatch.setenv("APPS_RUNNER_URL", "http://sidecar:9999/")
    monkeypatch.setenv("APPS_RUNNER_TOKEN", "envtok")
    c = SandboxRunnerClient()
    assert c.base_url == "http://sidecar:9999"
    assert c.token == "envtok"


def test_write_file_base64_encodes_bytes():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "written", "bytes": 5})

    async def _run():
        await _client(handler).write_file("agnes-chatsbx-a-1", "/tmp/agnes-cli/x.whl", b"WHEEL")

    asyncio.run(_run())
    assert seen["body"]["path"] == "/tmp/agnes-cli/x.whl"
    assert base64.b64decode(seen["body"]["content_b64"]) == b"WHEEL"


def test_write_file_accepts_str_payload():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "written"})

    async def _run():
        await _client(handler).write_file("agnes-chatsbx-a-1", "/tmp/agnes-context.md", "# hi\n")

    asyncio.run(_run())
    assert base64.b64decode(seen["body"]["content_b64"]) == b"# hi\n"


def test_read_file_returns_bytes():
    """The sidecar streams op=read as a raw octet body (no base64 JSON
    envelope — it must never hold content × encoding copies in memory)."""

    def handler(request):
        assert request.url.params["op"] == "read"
        assert request.url.params["path"] == "/work/outputs/a.csv"
        assert request.headers["x-runner-token"] == "tok"
        return httpx.Response(200, content=b"a,b\n", headers={"content-type": "application/octet-stream"})

    async def _run():
        data = await _client(handler).read_file("agnes-chatsbx-a-1", "/work/outputs/a.csv")
        assert data == b"a,b\n"

    asyncio.run(_run())


def test_read_file_maps_errors_like_request():
    """The raw-bytes path keeps `_request`'s error contract: 4xx/5xx raise
    SandboxRunnerError with the JSON detail."""
    from app.chat.sandbox_runner_client import SandboxRunnerError

    def handler(request):
        return httpx.Response(413, json={"detail": "file_too_large"})

    async def _run():
        with pytest.raises(SandboxRunnerError) as exc_info:
            await _client(handler).read_file("agnes-chatsbx-a-1", "/work/outputs/huge.bin")
        assert exc_info.value.status_code == 413
        assert exc_info.value.detail == "file_too_large"

    asyncio.run(_run())


def test_list_files_returns_entries():
    def handler(request):
        assert request.url.params["op"] == "list"
        return httpx.Response(200, json={"entries": [{"name": "a.csv", "path": "/work/outputs/a.csv", "type": "FILE"}]})

    async def _run():
        entries = await _client(handler).list_files("agnes-chatsbx-a-1", "/work/outputs")
        assert entries[0]["name"] == "a.csv"

    asyncio.run(_run())


def test_send_stdin_base64_encodes():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok", "bytes": 3})

    async def _run():
        await _client(handler).send_stdin("agnes-chatsbx-a-1", b'{"type":"cancel"}\n')

    asyncio.run(_run())
    assert base64.b64decode(seen["body"]["data_b64"]) == b'{"type":"cancel"}\n'


def test_open_stream_yields_decoded_frames():
    frames = [
        json.dumps({"stream": "stdout", "data": base64.b64encode(b'{"type":"runner_ready"}\n').decode()}) + "\n",
        json.dumps({"stream": "stderr", "data": base64.b64encode(b"boom\n").decode()}) + "\n",
    ]

    seen = {}

    def handler(request):
        assert request.url.path == "/sandboxes/agnes-chatsbx-a-1/stream"
        seen["replay"] = request.url.params["replay"]

        async def _body():
            for f in frames:
                yield f.encode()

        return httpx.Response(200, content=_body())

    async def _run():
        stream = await _client(handler).open_stream("agnes-chatsbx-a-1")
        got = [chunk async for chunk in stream]
        await stream.aclose()
        assert got == [("stdout", b'{"type":"runner_ready"}\n'), ("stderr", b"boom\n")]
        assert seen["replay"] == "false"

        replayed = await _client(handler).open_stream("agnes-chatsbx-a-1", replay=True)
        await replayed.aclose()
        assert seen["replay"] == "true"

    asyncio.run(_run())


def test_open_stream_raises_on_error_status():
    def handler(request):
        return httpx.Response(404, json={"detail": "absent"})

    async def _run():
        with pytest.raises(SandboxRunnerError) as exc:
            await _client(handler).open_stream("agnes-chatsbx-a-1")
        assert exc.value.status_code == 404

    asyncio.run(_run())


def test_open_stream_transport_failure_is_unavailable():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    async def _run():
        with pytest.raises(SandboxRunnerUnavailable):
            await _client(handler).open_stream("agnes-chatsbx-a-1")

    asyncio.run(_run())


def test_lifecycle_calls_hit_the_expected_paths():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={"container": "running", "exit_code": None})
        if request.url.path == "/sandboxes":
            return httpx.Response(200, json={"sandboxes": [{"name": "agnes-chatsbx-a-1", "chat_id": "a"}]})
        if request.url.path == "/sandboxes/probe":
            return httpx.Response(200, json={"ok": True, "detail": "ready"})
        return httpx.Response(200, json={"status": "ok"})

    async def _run():
        c = _client(handler)
        await c.pause("agnes-chatsbx-a-1")
        await c.resume("agnes-chatsbx-a-1")
        await c.rm("agnes-chatsbx-a-1")
        assert (await c.status("agnes-chatsbx-a-1"))["container"] == "running"
        assert (await c.list_sandboxes())[0]["chat_id"] == "a"
        assert (await c.probe("agnes-chat-sandbox:dev"))["ok"] is True

    asyncio.run(_run())
    assert seen == [
        ("POST", "/sandboxes/agnes-chatsbx-a-1/pause"),
        ("POST", "/sandboxes/agnes-chatsbx-a-1/resume"),
        ("POST", "/sandboxes/agnes-chatsbx-a-1/rm"),
        ("GET", "/sandboxes/agnes-chatsbx-a-1/status"),
        ("GET", "/sandboxes"),
        ("GET", "/sandboxes/probe"),
    ]
