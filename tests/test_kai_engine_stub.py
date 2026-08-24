"""The dev stub engine, driven through the REAL provider.

``tests/test_kai_engine_provider.py`` fakes the engine with a bespoke
``httpx.AsyncBaseTransport`` per test, which is the right tool for pinning
translation edge cases. What it cannot tell you is whether the thing a
developer actually runs locally — ``services/kai_engine_stub`` — still speaks
a dialect the provider understands. This file closes that loop: the stub
served by uvicorn on a real socket on one side, ``KaiEngineProvider`` on the
other, so its SSE record framing, its JWT verification and its scenario
scripts are all under test rather than merely documented.

It also pins the ORDER of a turn's frames. Issue #1504 (tool calls rendered
after the answer instead of at their position) was a renderer bug, but nothing
in the suite asserted the interleaving the renderer depends on, so a
regression in the provider or the stub would land silently and the next person
to look would be debugging the browser again.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from app.chat.kai_engine_provider import KaiEngineProvider

_SECRET = "test-stub-secret"


def _mint(user_email: str, session_id: str) -> tuple[str, int]:
    """A real host-shaped session JWT, signed the way app.api.kai signs it, so
    the stub's verification is exercised instead of bypassed."""
    from app.api.kai import _sign_session_jwt

    now = int(time.time())
    expires_at = now + 3600
    token = _sign_session_jwt(
        claims={
            "sub": user_email,
            "tenant": "agnes",
            "scope_id": session_id,
            "downstream_credential": "cred",
            "read_only": False,
            "iss": "agnes",
            "aud": "kai-agent",
            "iat": now,
            "exp": expires_at,
        },
        secret=_SECRET,
    )
    return token, expires_at


@pytest.fixture(scope="module")
def stub_env():
    """The stub on a REAL socket, served by uvicorn in a background thread.

    Not ``httpx.ASGITransport``: that collects the response body before
    handing it back, so a turn that blocks mid-stream waiting for an approval
    decision deadlocks — the client never sees the card it is supposed to
    answer. The engine's contract is a *progressively delivered* stream, and a
    transport that cannot express that is the wrong fixture for testing it.
    A real listener also means these tests exercise the same path a developer
    hits from the browser, which is the whole point of the stub.
    """
    import importlib
    import socket
    import threading

    import uvicorn

    os.environ["KAI_HOST_JWT_SECRET"] = _SECRET
    os.environ["KAI_HOST_JWT_ISSUER"] = "agnes"
    os.environ["KAI_HOST_JWT_AUDIENCE"] = "kai-agent"
    # The stub's human-visible pacing would otherwise add seconds per turn.
    os.environ["KAI_STUB_STEP_DELAY"] = "0"
    # Keep a blocked approval from holding a failing test for five minutes.
    os.environ["KAI_STUB_APPROVAL_TIMEOUT"] = "10"

    import services.kai_engine_stub.api as stub_api

    importlib.reload(stub_api)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(uvicorn.Config(stub_api.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:  # pragma: no cover - CI stall guard
            raise RuntimeError("kai stub server did not start")
        time.sleep(0.05)
    stub_api.base_url = f"http://127.0.0.1:{port}"
    try:
        yield stub_api
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _provider(stub_api) -> KaiEngineProvider:
    return KaiEngineProvider(base_url=stub_api.base_url, mint=_mint)


async def _turn(stub_api, text: str) -> list[dict]:
    provider = _provider(stub_api)
    handle = await provider.spawn(
        workdir=Path("/tmp"),
        env={"AGNES_SESSION_ID": str(uuid.uuid4()), "AGNES_USER_EMAIL": "u@example.com"},
        argv=[],
    )
    handle.stdin.write((json.dumps({"type": "user_msg", "text": text}) + "\n").encode())
    await handle.stdin.drain()
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

    await asyncio.wait_for(_read(), 15.0)
    return frames


def test_interleaved_scenario_keeps_text_and_tools_in_wire_order(stub_env):
    """The shape #1504 is about: text, tool, text, tool, text. The renderer
    segments on exactly this order, so it has to survive the round trip."""
    frames = asyncio.run(_turn(stub_env, "show me the interleaved turn"))
    shape = [f["type"] for f in frames if f["type"] in ("token", "tool_call", "tool_result")]
    # Collapse runs of tokens: what matters is that text appears BEFORE the
    # first tool, BETWEEN the two tools, and AFTER the last one.
    collapsed: list[str] = []
    for kind in shape:
        if not collapsed or collapsed[-1] != kind:
            collapsed.append(kind)
    assert collapsed == [
        "token",
        "tool_call",
        "tool_result",
        "token",
        "tool_call",
        "tool_result",
        "token",
    ], f"interleaving lost: {collapsed}"


def test_turn_ends_with_assistant_message_then_done(stub_env):
    frames = asyncio.run(_turn(stub_env, "interleaved"))
    assert [f["type"] for f in frames][-2:] == ["assistant_message", "done"]
    content = next(f for f in frames if f["type"] == "assistant_message")["content"]
    assert "server status" in content and "CZ leads" in content, (
        "the persisted answer must be the whole turn's text, both segments included"
    )


def test_mcp_envelope_reaches_the_client_unflattened(stub_env):
    """The stub forwards the MCP `{content:[{type:text}]}` envelope verbatim,
    the way the real engine does — the renderer is what unwraps it. A stub
    that pre-joined the blocks would hide that path entirely."""
    frames = asyncio.run(_turn(stub_env, "interleaved"))
    first_result = next(f for f in frames if f["type"] == "tool_result")["result"]
    assert "content" in first_result, "the envelope must survive to the client"
    assert json.loads(first_result)["content"][0]["type"] == "text"


def test_tabular_result_survives_as_structured_json(stub_env):
    frames = asyncio.run(_turn(stub_env, "interleaved"))
    results = [f["result"] for f in frames if f["type"] == "tool_result"]
    table = json.loads(results[-1])
    assert table["columns"] == ["country", "sessions"]
    assert table["rows"][0] == ["CZ", 1204]


def test_failing_tool_becomes_a_tool_result_not_a_turn_error(stub_env):
    """`tool-output-error` is a failed TOOL, not a failed turn — the card goes
    red, the turn still finishes with an answer."""
    frames = asyncio.run(_turn(stub_env, "make it fail"))
    types = [f["type"] for f in frames]
    assert "tool_result" in types
    assert "error" not in types, "a tool failure must not surface as a turn error"
    assert types[-1] == "done"


def test_engine_error_event_surfaces_as_an_error_frame(stub_env):
    frames = asyncio.run(_turn(stub_env, "trigger an error please"))
    types = [f["type"] for f in frames]
    assert "error" in types
    err = next(f for f in frames if f["type"] == "error")
    assert err["kind"] == "engine_error"
    assert "overloaded" in err["message"]


def test_stub_rejects_a_token_it_cannot_verify(stub_env):
    """The stub verifies the host mint the way the real engine does, so a
    drifted issuer or secret fails locally instead of only in a deployment."""

    def _bad_mint(user_email: str, session_id: str) -> tuple[str, int]:
        return "not.a.jwt", int(time.time()) + 3600

    provider = KaiEngineProvider(base_url=stub_env.base_url, mint=_bad_mint)

    async def _run() -> list[dict]:
        handle = await provider.spawn(
            workdir=Path("/tmp"),
            env={"AGNES_SESSION_ID": str(uuid.uuid4()), "AGNES_USER_EMAIL": "u@example.com"},
            argv=[],
        )
        handle.stdin.write((json.dumps({"type": "user_msg", "text": "interleaved"}) + "\n").encode())
        await handle.stdin.drain()
        frames: list[dict] = []
        while True:
            line = await asyncio.wait_for(handle.stdout.readline(), 10.0)
            if not line:
                break
            frames.append(json.loads(line))
            if frames[-1].get("type") == "done":
                break
        return frames

    frames = asyncio.run(_run())
    err = next(f for f in frames if f["type"] == "error")
    assert "401" in err["message"], f"expected an auth refusal, got {err['message']!r}"


def test_approval_scenario_blocks_the_turn_until_a_decision_arrives(stub_env):
    """The stub holds the turn open on the approval the way the engine does,
    so the card's whole round trip — request out, decision in, turn resumes —
    is exercised against a real HTTP handler rather than a gate object."""
    provider = _provider(stub_env)

    async def _run() -> list[dict]:
        handle = await provider.spawn(
            workdir=Path("/tmp"),
            env={"AGNES_SESSION_ID": str(uuid.uuid4()), "AGNES_USER_EMAIL": "u@example.com"},
            argv=[],
        )
        handle.stdin.write((json.dumps({"type": "user_msg", "text": "needs approval"}) + "\n").encode())
        await handle.stdin.drain()
        seen: list[dict] = []
        while True:
            seen.append(json.loads(await asyncio.wait_for(handle.stdout.readline(), 10.0)))
            if seen[-1].get("type") == "approval_request":
                break
        card = seen[-1]
        handle.stdin.write(
            (
                json.dumps({"type": "approval_decision", "request_id": card["request_id"], "decision": "allow"}) + "\n"
            ).encode()
        )
        await handle.stdin.drain()
        while True:
            seen.append(json.loads(await asyncio.wait_for(handle.stdout.readline(), 10.0)))
            if seen[-1].get("type") == "done":
                return seen

    frames = asyncio.run(_run())
    types = [f["type"] for f in frames]
    assert "approval_request" in types and "approval_resolved" in types
    # The turn RESUMED after the decision: the tool's own result landed and the
    # answer completed, rather than the stream dying with the card.
    assert types.index("tool_result") > types.index("approval_resolved")
    assert types[-2:] == ["assistant_message", "done"]


def test_every_non_blocking_scenario_completes_a_turn(stub_env):
    """A scenario that desyncs from the provider (unknown event type, missing
    id) would strand a turn. Cheap insurance that the catalogue stays live.
    `approval` is excluded by construction — it is SUPPOSED to block until a
    decision arrives, which the test above supplies."""
    for keyword in sorted(stub_env.SCENARIOS):
        if keyword == "approval":
            continue
        probe = "something with no script" if keyword == "default" else keyword
        frames = asyncio.run(_turn(stub_env, probe))
        assert frames and frames[-1]["type"] == "done", f"scenario {keyword!r} never finished"


def test_stub_is_not_reachable_without_the_dev_profile():
    """The stub answers every turn with a canned script, so it must never be
    something a default `docker compose up` can start."""
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    block = compose[compose.index("  kai-agent:") :]
    block = block[: block.index("\n  telegram-bot:")]
    assert 'profiles: ["kai-stub"]' in block, "the stub must sit behind its own compose profile"


def test_engine_url_has_an_env_override():
    """`chat.provider` is pinnable from the deployment env; its endpoint has to
    be too, or infrastructure can only half-configure the pair — and the
    yaml-only default names a compose host that no laptop can resolve."""
    from app.chat.config import _resolve_kai_agent_url

    assert _resolve_kai_agent_url({}) == "http://kai-agent:3000"
    assert _resolve_kai_agent_url({"kai_agent_url": "http://from-yaml:3000"}) == "http://from-yaml:3000"
    os.environ["AGNES_CHAT_KAI_AGENT_URL"] = "http://from-env:3000"
    try:
        assert _resolve_kai_agent_url({"kai_agent_url": "http://from-yaml:3000"}) == "http://from-env:3000"
        os.environ["AGNES_CHAT_KAI_AGENT_URL"] = "   "
        assert _resolve_kai_agent_url({"kai_agent_url": "http://from-yaml:3000"}) == "http://from-yaml:3000", (
            "a blank env value means unset, matching every sibling resolver"
        )
    finally:
        os.environ.pop("AGNES_CHAT_KAI_AGENT_URL", None)


def test_a_failed_tool_is_marked_as_such_on_the_frame(stub_env):
    """`tool-output-error` carries the verdict; the frame must too. The client
    used to sniff the payload for a leading "error"/"traceback", so a genuine
    failure reading "Catalog Error: Table … does not exist" rendered with a
    success tick and folded itself shut — found by driving this stub through
    the browser, invisible to every transport-level test."""
    frames = asyncio.run(_turn(stub_env, "make it fail"))
    results = [f for f in frames if f["type"] == "tool_result"]
    assert results, "the failing tool must still produce a result frame"
    assert results[-1]["is_error"] is True
    assert not _looks_like_error_text(results[-1]["result"]), (
        "this fixture exists BECAUSE the text defeats the old heuristic — if it "
        "now starts with 'error', the regression it guards is no longer covered"
    )


def test_a_successful_tool_is_not_marked_as_an_error(stub_env):
    frames = asyncio.run(_turn(stub_env, "interleaved"))
    for result in (f for f in frames if f["type"] == "tool_result"):
        assert result["is_error"] is False


def _looks_like_error_text(result: str) -> bool:
    """The client-side heuristic, mirrored so the fixture above can assert it
    would have missed this failure."""
    head = str(result).strip()[:12].lower()
    return head.startswith("error") or head.startswith("traceback")


def test_content_is_not_the_concatenation_of_the_deltas(stub_env):
    """The regression guard for the whole segmentation design.

    A real turn opens a new text part after each tool call, and the provider
    persists the answer as `"\\n\\n".join(part.strip() …)`. So `content` differs
    from the raw delta stream at the seams — which is why a client must not
    rebuild its display by slicing `content` against the deltas it saw. If
    this assertion ever flips to "equal", the stub has stopped reproducing the
    real shape and the client-side guard in test_chat_tool_rendering_ui.py is
    guarding nothing.
    """
    frames = asyncio.run(_turn(stub_env, "interleaved"))
    deltas = "".join(f["text"] for f in frames if f["type"] == "token")
    content = next(f for f in frames if f["type"] == "assistant_message")["content"]
    assert content != deltas, (
        "the stub must emit MULTIPLE text parts so content is a joined-and-stripped "
        "assembly, not a plain concatenation — see _assign_part_ids"
    )
    # Both still carry the same prose; only the seams differ.
    assert "server status" in content and "CZ leads" in content


def test_text_parts_are_distinct_across_tool_boundaries(stub_env):
    """Pins the cause of the divergence above, so a failure says which half
    broke: the stub's part-id assignment, or the provider's join."""
    import services.kai_engine_stub.api as stub_api

    ids = [e["id"] for e in stub_api.SCENARIOS["interleav"] if e.get("type") == "text-delta"]
    assert len(set(ids)) >= 3, f"expected a new text part after each tool call, got {ids}"
    assert None not in ids, "every text delta must carry a resolved part id"
