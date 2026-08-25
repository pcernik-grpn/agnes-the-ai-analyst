"""KaiEngineProvider — web chat on the embedded kai-agent turn engine.

Covers the whole seam (``chat.provider: kai-agent``, app/chat/kai_engine_provider.py):

* the SSE → runner-frame translation contract (tokens, tool calls/results,
  approvals, errors, terminal discipline — every turn ends in ``done``);
* the control frames back the other way (cancel → POST /stop with the
  engine's abort noise suppressed; approval_decision → POST /approval);
* turn serialization (a mid-turn user_msg is buffered, never a 409);
* session-token minting + refresh, and the provider lifecycle mapping
  (UUID-only ids, resume-from-sandbox-ref, pause == teardown);
* the two manager touches: engine-shaped session ids at create time, and
  the native ticket mint skipped for a provider that brings its own
  credentials;
* the boot gate (KAI_HOST_JWT_SECRET) and the ``chat.kai_agent_url`` config
  key;
* ``mint_engine_session_token`` signing the same claim set the
  ``POST /api/kai/sessions`` route serves.

asyncio.run() per the project convention (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import duckdb
import httpx
import pytest

from src.db import _ensure_schema

from app.chat.config import ChatConfig, load_chat_config
from app.chat.kai_engine_provider import KaiEngineProvider
from app.chat.manager import ChatManager
from app.chat.persistence import ChatRepository
from app.chat.types import Surface
from app.chat.workdir import WorkdirManager
from app.coordination.factory import reset_coordination_for_tests


@pytest.fixture(autouse=True)
def _reset_coordination():
    reset_coordination_for_tests()
    yield
    reset_coordination_for_tests()


# ---------------------------------------------------------------------------
# fake engine (httpx transport)
# ---------------------------------------------------------------------------


def _sse(events: list[dict]) -> bytes:
    out = b""
    for i, event in enumerate(events):
        out += f"id: {i}\ndata: {json.dumps(event)}\n\n".encode()
    return out


class _GatedStream(httpx.AsyncByteStream):
    """SSE body that can hold mid-stream on an event (cancel/approval tests)."""

    def __init__(self, pre: list[dict], gate: "asyncio.Event | None" = None, post: list[dict] | None = None):
        self._pre, self._gate, self._post = pre, gate, post or []

    async def __aiter__(self):
        yield b":ping\n\n"
        if self._pre:
            yield _sse(self._pre)
        if self._gate is not None:
            await self._gate.wait()
        if self._post:
            yield _sse(self._post)

    async def aclose(self) -> None:
        return None


class _RawStream(httpx.AsyncByteStream):
    """SSE body from raw bytes — for wire shapes _GatedStream can't express
    (e.g. a stream that closes without the trailing blank line)."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    async def __aiter__(self):
        yield self._raw

    async def aclose(self) -> None:
        return None


class FakeEngine(httpx.AsyncBaseTransport):
    """Just enough of the engine's HTTP surface: POST /api/chat (SSE),
    POST /api/chat/{id}/stop, POST /api/chat/{id}/approval."""

    def __init__(self) -> None:
        self.chat_requests: list[dict] = []
        self.stop_count = 0
        self.approvals: list[dict] = []
        #: Per-turn scripts, consumed in order. Each: {"status": int} or
        #: {"pre": [...], "gate": Event|None, "post": [...]}.
        self.turns: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = await request.aread()
        if request.method == "POST" and path == "/api/chat":
            self.chat_requests.append({"body": json.loads(body), "auth": request.headers.get("authorization", "")})
            spec = self.turns.pop(0) if self.turns else {"pre": [{"type": "finish"}]}
            status = spec.get("status", 200)
            if status != 200:
                return httpx.Response(status, json={"message": "refused"})
            stream: httpx.AsyncByteStream
            if "raw" in spec:
                stream = _RawStream(spec["raw"])
            else:
                stream = _GatedStream(spec.get("pre", []), spec.get("gate"), spec.get("post"))
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        if request.method == "POST" and path.endswith("/stop"):
            self.stop_count += 1
            return httpx.Response(200, json={"stopped": True})
        if request.method == "POST" and path.endswith("/approval"):
            self.approvals.append(json.loads(body))
            return httpx.Response(200, json={"success": True, "toolUseId": "x", "approved": True})
        return httpx.Response(404, json={"message": "no such route"})


def _mint_factory(calls: list, ttl: int = 12 * 3600):
    def _mint(user_email: str, session_id: str) -> tuple[str, int]:
        calls.append((user_email, session_id))
        return f"tok-{len(calls)}", int(time.time()) + ttl

    return _mint


async def _spawn(provider: KaiEngineProvider, chat_id: str | None = None):
    return await provider.spawn(
        workdir=Path("/tmp"),
        env={"AGNES_SESSION_ID": chat_id or str(uuid.uuid4()), "AGNES_USER_EMAIL": "u@x"},
        argv=[],
    )


async def _send(handle, frame: dict) -> None:
    handle.stdin.write((json.dumps(frame) + "\n").encode())
    await handle.stdin.drain()


async def _drain_until_done(handle, timeout: float = 5.0) -> list[dict]:
    frames: list[dict] = []

    async def _read() -> None:
        while True:
            line = await handle.stdout.readline()
            if not line:
                return
            frame = json.loads(line)
            frames.append(frame)
            if frame.get("type") == "done":
                return

    await asyncio.wait_for(_read(), timeout)
    return frames


def _types(frames: list[dict]) -> list[str]:
    return [f["type"] for f in frames]


# ---------------------------------------------------------------------------
# SSE → frame translation
# ---------------------------------------------------------------------------


def test_turn_translates_stream_to_frames():
    async def _run():
        engine = FakeEngine()
        engine.turns = [
            {
                "pre": [
                    {"type": "start"},
                    {"type": "text-start", "id": "t1"},
                    {"type": "text-delta", "id": "t1", "delta": "Hel"},
                    {"type": "text-delta", "id": "t1", "delta": "lo"},
                    {"type": "text-end", "id": "t1"},
                    {"type": "tool-input-start", "toolCallId": "call-1", "toolName": "run_query"},
                    {
                        "type": "tool-input-available",
                        "toolCallId": "call-1",
                        "toolName": "run_query",
                        "input": {"sql": "SELECT 1"},
                    },
                    {"type": "tool-output-available", "toolCallId": "call-1", "output": {"rows": 1}},
                    {"type": "text-start", "id": "t2"},
                    {"type": "text-delta", "id": "t2", "delta": "Done."},
                    {"type": "text-end", "id": "t2"},
                    {"type": "finish"},
                ]
            }
        ]
        mint_calls: list = []
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory(mint_calls), transport=engine)
        chat_id = str(uuid.uuid4())
        handle = await _spawn(provider, chat_id)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()

        assert _types(frames) == [
            "runner_ready",
            "token",
            "token",
            "tool_call",
            "tool_result",
            "token",
            "assistant_message",
            "done",
        ]
        tool_call = frames[3]
        assert tool_call["tool"] == "run_query"
        assert tool_call["tool_use_id"] == "call-1"
        assert tool_call["args"] == {"sql": "SELECT 1"}
        tool_result = frames[4]
        assert tool_result["tool_use_id"] == "call-1"
        assert json.loads(tool_result["result"]) == {"rows": 1}
        # Segments join with a blank line, like the runner's TextBlock join.
        assert frames[6]["content"] == "Hello\n\nDone."

        # Wire contract toward the engine.
        req = engine.chat_requests[0]
        assert req["auth"] == "Bearer tok-1"
        assert req["body"]["id"] == chat_id
        assert req["body"]["supportsApprovalRequestedEvent"] is True
        part = req["body"]["message"]["parts"][0]
        assert part == {"type": "text", "text": "hi"}
        uuid.UUID(req["body"]["message"]["id"])  # engine requires a uuid message id

    asyncio.run(_run())


def test_http_refusal_emits_error_then_done():
    async def _run():
        engine = FakeEngine()
        engine.turns = [{"status": 409}]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()
        assert _types(frames) == ["runner_ready", "error", "done"]
        assert frames[1]["kind"] == "engine_error"
        assert "409" in frames[1]["message"]

    asyncio.run(_run())


def test_final_record_without_trailing_blank_line_still_counts():
    """A stream that closes right after its last `data:` line (no trailing
    blank-line record boundary) must still dispatch that record — dropping it
    loses whatever the engine said last (Devin Review on this PR)."""

    async def _run():
        engine = FakeEngine()
        raw = (
            b'data: {"type": "text-delta", "id": "a", "delta": "whole answer"}\n\n'
            b'data: {"type": "finish"}\n'  # stream ends here — no blank line
        )
        engine.turns = [{"raw": raw}]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()
        assert _types(frames) == ["runner_ready", "token", "assistant_message", "done"]
        # And a final PAYLOAD-BEARING record is translated, not just `finish`:
        engine2 = FakeEngine()
        engine2.turns = [{"raw": b'data: {"type": "text-delta", "id": "a", "delta": "tail"}\n'}]
        provider2 = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine2)
        handle2 = await _spawn(provider2)
        await _send(handle2, {"type": "user_msg", "text": "hi"})
        frames2 = await _drain_until_done(handle2)
        await handle2.kill()
        assert any(f.get("type") == "token" and f["text"] == "tail" for f in frames2)
        assert any(f.get("type") == "assistant_message" and f["content"] == "tail" for f in frames2)

    asyncio.run(_run())


def test_engine_error_event_persists_partial_text():
    async def _run():
        engine = FakeEngine()
        engine.turns = [
            {
                "pre": [
                    {"type": "text-start", "id": "t1"},
                    {"type": "text-delta", "id": "t1", "delta": "partial answer"},
                    {"type": "error", "errorText": "SDK execution failed"},
                    {"type": "finish"},
                ]
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()
        assert _types(frames) == ["runner_ready", "token", "error", "assistant_message", "done"]
        # The partial the user already saw survives into history — the
        # open (never text-end'ed) segment still contributes.
        assert frames[3]["content"] == "partial answer"
        assert frames[2]["message"] == "SDK execution failed"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# cancel + approvals
# ---------------------------------------------------------------------------


def test_cancel_posts_stop_and_suppresses_abort_noise():
    async def _run():
        engine = FakeEngine()
        gate = asyncio.Event()
        engine.turns = [
            {
                "pre": [
                    {"type": "text-start", "id": "t1"},
                    {"type": "text-delta", "id": "t1", "delta": "thinking"},
                ],
                "gate": gate,
                # What the engine's stream bus emits after a stop: the abort
                # surfaces as an error event, then the guaranteed finish.
                "post": [{"type": "error", "errorText": "Aborted"}, {"type": "finish"}],
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        # Wait until the delta reached us so the turn is provably in flight.
        first = [json.loads(await handle.stdout.readline()) for _ in range(2)]
        assert _types(first) == ["runner_ready", "token"]
        await _send(handle, {"type": "cancel"})
        for _ in range(50):
            if engine.stop_count:
                break
            await asyncio.sleep(0.02)
        assert engine.stop_count == 1
        gate.set()
        frames = await _drain_until_done(handle)
        await handle.kill()
        # The abort error is the outcome the user asked for — no error frame.
        assert "error" not in _types(frames)
        # The partial still lands in history.
        assert any(f.get("type") == "assistant_message" and f["content"] == "thinking" for f in frames)

    asyncio.run(_run())


def test_approval_round_trip():
    async def _run():
        engine = FakeEngine()
        gate = asyncio.Event()
        engine.turns = [
            {
                "pre": [
                    {
                        "type": "tool-input-available",
                        "toolCallId": "call-9",
                        "toolName": "create_config",
                        "input": {"name": "x"},
                    },
                    {"type": "tool-approval-request", "toolCallId": "call-9"},
                ],
                "gate": gate,
                "post": [
                    {"type": "tool-output-available", "toolCallId": "call-9", "output": "created"},
                    {"type": "finish"},
                ],
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})

        seen: list[dict] = []
        while True:
            frame = json.loads(await handle.stdout.readline())
            seen.append(frame)
            if frame.get("type") == "approval_request":
                break
        card = seen[-1]
        # The card is keyed on the engine's toolCallId, names the tool, and
        # previews the input it already streamed.
        assert card["request_id"] == "call-9"
        assert card["tool"] == "create_config"
        assert json.loads(card["command"]) == {"name": "x"}

        await _send(handle, {"type": "approval_decision", "request_id": "call-9", "decision": "allow"})
        for _ in range(50):
            if engine.approvals:
                break
            await asyncio.sleep(0.02)
        assert engine.approvals == [{"toolUseId": "call-9", "approved": True}]
        resolved = json.loads(await handle.stdout.readline())
        assert resolved["type"] == "approval_resolved"
        assert resolved["request_id"] == "call-9"
        assert resolved["decision"] == "allow"

        gate.set()
        frames = await _drain_until_done(handle)
        await handle.kill()
        assert any(f.get("type") == "tool_result" and f.get("tool_use_id") == "call-9" for f in frames)

    asyncio.run(_run())


def test_unanswered_approval_retired_at_turn_end():
    async def _run():
        engine = FakeEngine()
        engine.turns = [
            {
                "pre": [
                    {"type": "tool-input-available", "toolCallId": "call-2", "toolName": "t", "input": {}},
                    {"type": "tool-approval-request", "toolCallId": "call-2"},
                    {"type": "finish"},
                ]
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()
        resolved = [f for f in frames if f["type"] == "approval_resolved"]
        assert len(resolved) == 1
        assert resolved[0]["request_id"] == "call-2"
        assert resolved[0]["decision"] == "cancelled"
        # A card the engine never resolved must never outlive its turn — the
        # manager retires pending cards only on approval_resolved.
        assert frames.index(resolved[0]) < frames.index(frames[-1])

    asyncio.run(_run())


def test_engine_side_denial_resolves_the_card():
    async def _run():
        engine = FakeEngine()
        engine.turns = [
            {
                "pre": [
                    {"type": "tool-input-available", "toolCallId": "call-3", "toolName": "t", "input": {}},
                    {"type": "tool-approval-request", "toolCallId": "call-3"},
                    # The engine's own approval window expired → denied output.
                    {"type": "tool-output-error", "toolCallId": "call-3", "errorText": "denied"},
                    {"type": "finish"},
                ]
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()
        resolved = [f for f in frames if f["type"] == "approval_resolved"]
        assert [f["decision"] for f in resolved] == ["deny"]
        result = [f for f in frames if f["type"] == "tool_result"]
        assert result and result[0]["result"] == "denied"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# turn serialization + token refresh
# ---------------------------------------------------------------------------


def test_mid_turn_user_msg_is_buffered_until_the_turn_ends():
    async def _run():
        engine = FakeEngine()
        gate = asyncio.Event()
        engine.turns = [
            {
                "pre": [{"type": "text-start", "id": "a"}, {"type": "text-delta", "id": "a", "delta": "one"}],
                "gate": gate,
                "post": [{"type": "finish"}],
            },
            {"pre": [{"type": "finish"}]},
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "first"})
        first = [json.loads(await handle.stdout.readline()) for _ in range(2)]
        assert _types(first) == ["runner_ready", "token"]
        # Second message lands mid-turn — the engine would 409 a concurrent
        # POST, so it must be buffered, not sent.
        await _send(handle, {"type": "user_msg", "text": "second"})
        await asyncio.sleep(0.05)
        assert len(engine.chat_requests) == 1
        gate.set()
        await _drain_until_done(handle)  # first turn's tail
        frames2 = await _drain_until_done(handle)  # queued second turn
        await handle.kill()
        assert len(engine.chat_requests) == 2
        assert engine.chat_requests[1]["body"]["message"]["parts"][0]["text"] == "second"
        assert frames2[-1]["type"] == "done"

    asyncio.run(_run())


def test_session_token_is_reminted_when_expiring():
    async def _run():
        engine = FakeEngine()
        engine.turns = [{"pre": [{"type": "finish"}]}, {"pre": [{"type": "finish"}]}]
        calls: list = []
        # Expiry inside the refresh margin → every bearer lookup re-mints.
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory(calls, ttl=60), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "one"})
        await _drain_until_done(handle)
        await _send(handle, {"type": "user_msg", "text": "two"})
        await _drain_until_done(handle)
        await handle.kill()
        assert len(calls) == 2
        assert engine.chat_requests[0]["auth"] == "Bearer tok-1"
        assert engine.chat_requests[1]["auth"] == "Bearer tok-2"

    asyncio.run(_run())

    async def _run_long():
        engine = FakeEngine()
        engine.turns = [{"pre": [{"type": "finish"}]}, {"pre": [{"type": "finish"}]}]
        calls: list = []
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory(calls), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "one"})
        await _drain_until_done(handle)
        await _send(handle, {"type": "user_msg", "text": "two"})
        await _drain_until_done(handle)
        await handle.kill()
        assert len(calls) == 1  # 12 h of runway — no re-mint

    asyncio.run(_run_long())


# ---------------------------------------------------------------------------
# provider lifecycle
# ---------------------------------------------------------------------------


def test_spawn_of_legacy_id_yields_dead_handle_with_legible_error():
    """A `chat_<hex>` session (created pre-provider-switch) must not tear the
    WebSocket down with a bare RuntimeError — it gets a handle that answers
    every message with a clear error card and never contacts the engine."""

    async def _run():
        engine = FakeEngine()
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await provider.spawn(
            workdir=Path("/tmp"),
            env={"AGNES_SESSION_ID": "chat_abc123", "AGNES_USER_EMAIL": "u@x"},
            argv=[],
        )
        boot = await _drain_until_done(handle)
        assert _types(boot) == ["runner_ready", "error", "done"]
        assert boot[1]["kind"] == "engine_session_unusable"
        # The boot-time copy can predate the WS seat — every user_msg re-emits.
        await _send(handle, {"type": "user_msg", "text": "hello?"})
        again = await _drain_until_done(handle)
        assert _types(again) == ["error", "done"]
        await handle.kill()
        assert engine.chat_requests == []

    asyncio.run(_run())


def test_resume_takes_owner_from_env_with_repo_fallback(monkeypatch):
    async def _run():
        import src.repositories as repos

        chat_id = str(uuid.uuid4())
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=FakeEngine())
        # Normal path: the manager passes the owner in env — no repo touch.
        fake_repo = MagicMock()
        monkeypatch.setattr(repos, "chat_session_repo", lambda: fake_repo)
        handle = await provider.resume(
            sandbox_id=f"kai-engine:{chat_id}",
            runner_pid=1,
            env={
                "AGNES_SESSION_ID": chat_id,
                "AGNES_USER_EMAIL": "owner@x",
                # The approval knobs must stay sticky across pause/resume —
                # identity alone silently flipped approvals back ON.
                "AGNES_APPROVALS": "off",
                "AGNES_APPROVAL_TIMEOUT_SECONDS": "900",
            },
        )
        assert handle.sandbox_id == f"kai-engine:{chat_id}"
        assert handle._user_email == "owner@x"
        assert handle._approvals_enabled is False
        assert handle._approval_timeout_seconds == 900
        await handle.kill()
        fake_repo.get_session.assert_not_called()
        # Fallback: an env-less caller recovers the owner through the factory.
        fake_repo.get_session.return_value = SimpleNamespace(user_email="fallback@x")
        handle2 = await provider.resume(sandbox_id=f"kai-engine:{chat_id}", runner_pid=1, env={})
        assert handle2._user_email == "fallback@x"
        await handle2.kill()
        fake_repo.get_session.assert_called_once_with(chat_id)

    asyncio.run(_run())


def test_web_decision_is_not_double_resolved_by_engine_output():
    """After a web ALLOW, the engine's own tool-output must not resolve the
    same card a second time (nor may turn-end retire it as cancelled)."""

    async def _run():
        engine = FakeEngine()
        gate = asyncio.Event()
        engine.turns = [
            {
                "pre": [
                    {"type": "tool-input-available", "toolCallId": "call-7", "toolName": "t", "input": {}},
                    {"type": "tool-approval-request", "toolCallId": "call-7"},
                ],
                "gate": gate,
                "post": [
                    {"type": "tool-output-available", "toolCallId": "call-7", "output": "ok"},
                    {"type": "finish"},
                ],
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        while True:
            frame = json.loads(await handle.stdout.readline())
            if frame.get("type") == "approval_request":
                break
        await _send(handle, {"type": "approval_decision", "request_id": "call-7", "decision": "allow"})
        for _ in range(250):
            if engine.approvals:
                break
            await asyncio.sleep(0.02)
        gate.set()
        frames = await _drain_until_done(handle)
        await handle.kill()
        resolved = [f for f in frames if f["type"] == "approval_resolved"]
        assert [f["decision"] for f in resolved] == ["allow"]

    asyncio.run(_run())


def test_approvals_kill_switch_auto_denies():
    """chat.approvals_enabled=false (AGNES_APPROVALS=off in the spawn env)
    mirrors the native gate: each request is denied instantly instead of
    letting the tool wait out the engine's window."""

    async def _run():
        engine = FakeEngine()
        gate = asyncio.Event()
        engine.turns = [
            {
                "pre": [
                    {"type": "tool-input-available", "toolCallId": "call-8", "toolName": "t", "input": {}},
                    {"type": "tool-approval-request", "toolCallId": "call-8"},
                ],
                "gate": gate,
                "post": [{"type": "finish"}],
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await provider.spawn(
            workdir=Path("/tmp"),
            env={"AGNES_SESSION_ID": str(uuid.uuid4()), "AGNES_USER_EMAIL": "u@x", "AGNES_APPROVALS": "off"},
            argv=[],
        )
        await _send(handle, {"type": "user_msg", "text": "hi"})
        for _ in range(250):
            if engine.approvals:
                break
            await asyncio.sleep(0.02)
        assert engine.approvals == [{"toolUseId": "call-8", "approved": False}]
        gate.set()
        frames = await _drain_until_done(handle)
        await handle.kill()
        resolved = [f for f in frames if f["type"] == "approval_resolved"]
        assert [f["decision"] for f in resolved] == ["deny"]

    asyncio.run(_run())


def test_approval_card_timeout_label_follows_operator_config():
    async def _run():
        engine = FakeEngine()
        engine.turns = [
            {
                "pre": [
                    {"type": "tool-input-available", "toolCallId": "c", "toolName": "t", "input": {}},
                    {"type": "tool-approval-request", "toolCallId": "c"},
                    {"type": "finish"},
                ]
            }
        ]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await provider.spawn(
            workdir=Path("/tmp"),
            env={
                "AGNES_SESSION_ID": str(uuid.uuid4()),
                "AGNES_USER_EMAIL": "u@x",
                # The knob the manager threads to every provider's spawn env.
                "AGNES_APPROVAL_TIMEOUT_SECONDS": "900",
            },
            argv=[],
        )
        await _send(handle, {"type": "user_msg", "text": "hi"})
        frames = await _drain_until_done(handle)
        await handle.kill()
        card = next(f for f in frames if f["type"] == "approval_request")
        assert card["timeout_seconds"] == 900

    asyncio.run(_run())


def test_pause_is_teardown_and_wait_returns_clean():
    async def _run():
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=FakeEngine())
        handle = await _spawn(provider)
        await provider.pause(handle)
        assert await handle.wait() == 0
        assert await handle.stdout.readline() == b""  # EOF → pump task ends
        # No-op remainder of the contract.
        await provider.keepalive(handle, timeout_seconds=60)
        await provider.destroy(sandbox_id=handle.sandbox_id)

    asyncio.run(_run())


def test_kill_mid_turn_tears_down_promptly():
    async def _run():
        engine = FakeEngine()
        gate = asyncio.Event()  # never set — the turn holds forever
        engine.turns = [{"pre": [{"type": "text-delta", "id": "a", "delta": "x"}], "gate": gate}]
        provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]), transport=engine)
        handle = await _spawn(provider)
        await _send(handle, {"type": "user_msg", "text": "hi"})
        first = [json.loads(await handle.stdout.readline()) for _ in range(2)]
        assert _types(first) == ["runner_ready", "token"]
        # Teardown with the SSE stream still open must not wait on it.
        await asyncio.wait_for(handle.kill(), timeout=5)
        assert await asyncio.wait_for(handle.wait(), timeout=1) == 0
        assert await handle.stdout.readline() == b""

    asyncio.run(_run())


def test_provider_declares_its_capabilities():
    provider = KaiEngineProvider(base_url="http://engine:3000", mint=_mint_factory([]))
    assert provider.syncs_workspace is True  # manager must not upload a workspace
    assert provider.provides_own_credentials is True  # manager must not mint native tickets
    # No stage_file: the manager's _file_stager capability check must opt out,
    # so no restore-context/wheel upload is attempted (the engine holds the
    # transcript itself).
    assert not hasattr(provider, "stage_file")


# ---------------------------------------------------------------------------
# manager integration
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path, config: ChatConfig) -> ChatManager:
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    bundled = tmp_path / "bundled"
    bundled.mkdir(exist_ok=True)
    (bundled / "CLAUDE.md").write_text("d")
    workdir_mgr = WorkdirManager(
        data_dir=tmp_path / "data",
        repo=repo,
        bundled_template_dir=bundled,
        server_url="https://example",
        agnes_version="0.55.0",
        get_marketplace_sha=lambda: "sha-1",
        get_template_status=lambda: None,
    )
    provider = MagicMock()
    provider.spawn = AsyncMock()
    return ChatManager(provider=provider, workdir_mgr=workdir_mgr, repo=repo, config=config)


def test_create_session_mints_uuid_ids_for_engine_provider(tmp_path):
    mgr = _make_manager(tmp_path, ChatConfig(enabled=True, provider="kai-agent"))
    session = asyncio.run(mgr.create_session(user_email="u@x", surface=Surface.WEB))
    uuid.UUID(session.id)  # engine stores body.id in a uuid column


def test_create_session_keeps_chat_hex_ids_for_native_providers(tmp_path):
    mgr = _make_manager(tmp_path, ChatConfig(enabled=True, provider="e2b"))
    session = asyncio.run(mgr.create_session(user_email="u@x", surface=Surface.WEB))
    assert session.id.startswith("chat_")


def test_slack_producer_mints_uuid_ids_for_engine_provider(tmp_path):
    """The producer twin (api role, no ChatManager) must mint the same
    engine-shaped ids — a `chat_<hex>` Slack DM row on an engine instance
    de-dupes forever to a session the provider can never spawn."""
    import duckdb as _duckdb

    from app.chat.manager import resolve_or_create_slack_session

    conn = _duckdb.connect(":memory:")
    _ensure_schema(conn)
    repo = ChatRepository(conn)
    session = resolve_or_create_slack_session(
        repo,
        ChatConfig(enabled=True, provider="kai-agent"),
        user_email="u@x",
        surface=Surface.SLACK_DM,
        slack_channel_id="D123",
    )
    uuid.UUID(session.id)
    native = resolve_or_create_slack_session(
        repo,
        ChatConfig(enabled=True, provider="e2b"),
        user_email="u@x",
        surface=Surface.SLACK_DM,
        slack_channel_id="D456",
    )
    assert native.id.startswith("chat_")


def test_revoke_native_tickets_skipped_for_own_credentials_provider(tmp_path, monkeypatch):
    """The scope-blind sweep is the revoke half of the mint/revoke pair — for
    a provider that brings its own credentials it must skip too, or a resume
    401s the engine's in-flight per-turn tickets with nothing re-minting."""
    import app.chat.manager as manager_mod

    tickets = MagicMock()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: tickets)
    mgr = _make_manager(tmp_path, ChatConfig(enabled=True, provider="kai-agent"))
    mgr._provider.provides_own_credentials = True
    mgr._revoke_native_tickets("c-9")
    tickets.revoke_session.assert_not_called()
    # A plain MagicMock provider (attribute exists but is a truthy Mock, not
    # the literal True) keeps the native sweep — the duck-typed-double rule.
    mgr2 = _make_manager(tmp_path, ChatConfig(enabled=True, provider="e2b"))
    mgr2._revoke_native_tickets("c-9")
    tickets.revoke_session.assert_called_once_with("c-9")


def test_boot_provider_allowlist_admits_kai_agent():
    """The lifespan's provider allowlist runs BEFORE every kai-specific gate —
    if it does not admit the value, the whole feature is dead on arrival with
    a log naming only e2b/docker (found by review; pinned so a refactor of the
    boot chain cannot silently regress it)."""
    body = Path("app/main.py").read_text()
    assert 'not in ("e2b", "docker", "kai-agent")' in body


def test_push_ticket_frame_skips_native_mints_for_own_credentials_provider(tmp_path, monkeypatch):
    import app.chat.manager as manager_mod

    mgr = _make_manager(tmp_path, ChatConfig(enabled=True, provider="kai-agent"))
    mgr._provider.provides_own_credentials = True
    tickets = MagicMock()
    monkeypatch.setattr(manager_mod, "ticket_repo", lambda: tickets)
    live = SimpleNamespace(chat_id="c-1", handle=MagicMock(), _stdin_lock=asyncio.Lock())
    asyncio.run(mgr._push_ticket_frame(live))
    # No native scope minted, nothing written to the handle — but the session
    # is still marked current-protocol (the resume path gates on it).
    tickets.mint.assert_not_called()
    live.handle.stdin.write.assert_not_called()
    assert "c-1" in mgr._known_protocol_sessions


# ---------------------------------------------------------------------------
# boot gate + config
# ---------------------------------------------------------------------------


def test_kai_agent_boot_gate_requires_the_shared_secret(monkeypatch):
    from app.main import _chat_kai_agent_ok

    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)
    assert not _chat_kai_agent_ok(ChatConfig(enabled=True, provider="kai-agent"))
    monkeypatch.setenv("KAI_HOST_JWT_SECRET", "s" * 32)
    assert _chat_kai_agent_ok(ChatConfig(enabled=True, provider="kai-agent"))
    # Every engine call carries the session bearer token to kai_agent_url —
    # a non-http(s) shape is refused at boot rather than failing per turn.
    assert not _chat_kai_agent_ok(ChatConfig(enabled=True, provider="kai-agent", kai_agent_url="kai-agent:3000"))
    assert not _chat_kai_agent_ok(ChatConfig(enabled=True, provider="kai-agent", kai_agent_url=""))
    monkeypatch.delenv("KAI_HOST_JWT_SECRET", raising=False)
    # Other providers and disabled chat are out of this gate's scope.
    assert _chat_kai_agent_ok(ChatConfig(enabled=True, provider="e2b"))
    assert _chat_kai_agent_ok(ChatConfig(enabled=False, provider="kai-agent"))


def test_chat_config_parses_kai_agent_provider(tmp_path):
    yml = tmp_path / "instance.yaml"
    yml.write_text("chat:\n  enabled: true\n  provider: kai-agent\n  kai_agent_url: http://engine:9000\n")
    cfg = load_chat_config(yml)
    assert cfg.provider == "kai-agent"
    assert cfg.kai_agent_url == "http://engine:9000"
    yml.write_text("chat:\n  enabled: true\n  provider: kai-agent\n")
    assert load_chat_config(yml).kai_agent_url == "http://kai-agent:3000"


# ---------------------------------------------------------------------------
# mint helper (app/api/kai.py)
# ---------------------------------------------------------------------------


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def test_mint_engine_session_token_signs_the_route_claim_set(monkeypatch):
    import app.api.kai as kai_mod

    secret = "test-kai-host-secret-that-is-at-least-32-chars"
    monkeypatch.setenv("KAI_HOST_JWT_SECRET", secret)
    monkeypatch.delenv("KAI_HOST_JWT_ISSUER", raising=False)
    monkeypatch.delenv("KAI_HOST_JWT_AUDIENCE", raising=False)
    monkeypatch.delenv("KAI_TENANT_ID", raising=False)
    tickets = MagicMock()
    tickets.mint.return_value = "cred-42"
    monkeypatch.setattr(kai_mod, "ticket_repo", lambda: tickets)

    session_id = str(uuid.uuid4())
    token, expires_at = kai_mod.mint_engine_session_token("u@x", session_id)

    header_b64, payload_b64, sig_b64 = token.split(".")
    claims = json.loads(_b64url_decode(payload_b64))
    assert claims["sub"] == "u@x"
    assert claims["scope_id"] == session_id
    assert claims["downstream_credential"] == "cred-42"
    assert claims["read_only"] is False
    assert claims["iss"] == "agnes"
    assert claims["aud"] == "kai-agent"
    assert claims["exp"] == expires_at
    assert claims["exp"] - claims["iat"] == 12 * 60 * 60
    # The credential is minted against the session with the session TTL.
    tickets.mint.assert_called_once_with(session_id, "kai_session", ttl_seconds=12 * 60 * 60)
    # Signature verifies under the shared secret (the engine's check).
    expected = hmac.new(secret.encode(), f"{header_b64}.{payload_b64}".encode("ascii"), hashlib.sha256).digest()
    assert _b64url_decode(sig_b64) == expected


def test_a_failed_stop_is_not_reported_as_a_successful_one(caplog):
    """`_post_approval` raises for status; `_post_stop` did not. A 4xx/5xx
    refusal therefore read as a successful stop, and the turn hung with the
    composer locked until the SSE read timeout — the user pressed Stop,
    nothing happened, and nothing reached the log either."""
    import logging

    src = Path("app/chat/kai_engine_provider.py").read_text()
    fn = src[src.index("async def _post_stop") :]
    fn = fn[: fn.index("async def _post_approval")]
    assert "raise_for_status()" in fn, "a refused stop must be logged, not swallowed as success"
    assert "logger.warning" in fn
    del logging, caplog


def test_the_dead_handle_does_not_assert_a_cause_it_cannot_know():
    """The handle sees only the id shape. Claiming the conversation
    "predates" the provider was wrong for the co-drive sessions that used to
    reach here seconds after creation, and it sent the user to make another
    dead one."""
    src = Path("app/chat/kai_engine_provider.py").read_text()
    assert "predates chat.provider=kai-agent" not in src
    assert "not in the engine's format" in src


def test_the_admin_banner_labels_the_engine_secret():
    """`secret_status` reports `kai_host_jwt_secret`; without a label the row
    renders the raw snake_case key instead of the env var an operator sets."""
    tpl = Path("app/web/templates/admin_server_config.html").read_text()
    labels = tpl[tpl.index("const CHAT_SECRET_LABELS") :]
    labels = labels[: labels.index("};")]
    assert "kai_host_jwt_secret" in labels and "KAI_HOST_JWT_SECRET" in labels


def test_the_agent_api_refuses_rather_than_running_the_wrong_persona():
    """`POST /api/v1/agents/{slug}/sessions` promises to run as THAT agent.

    The persona and memory notebook reach a turn by being materialized into
    the session workspace, which a self-credentialed provider never reads —
    so the caller would get the instance template persona under the agent's
    name, and a narrowed-scope agent would 403 on every tool call besides.
    Silently answering wrong is worse than refusing.
    """
    src = Path("app/api/agent_sessions.py").read_text()
    fn = src[src.index("async def create_agent_session") :]
    fn = fn[: fn.index("\n@router") if "\n@router" in fn else len(fn)]
    assert "provides_own_credentials" in fn, "the agent API must not run on a provider that skips the workspace"
    # Reach the provider defensively: a manager without `_provider` (any test
    # double, and the api-role thin producer) must not turn this guard into an
    # AttributeError 500 on a route that has nothing to do with the engine.
    assert 'getattr(manager, "_provider", None)' in fn, "reading manager._provider directly 500s on a manager without one"
    assert "agent_sessions_unavailable_on_provider" in fn
    # Refused BEFORE the session is created, not after.
    assert fn.index("provides_own_credentials") < fn.index("manager.create_session")
