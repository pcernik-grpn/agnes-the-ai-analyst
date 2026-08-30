"""Track C7 MVP (@delegation) — the runner-side (in-sandbox) half.

Pure-function coverage for `app/chat/runner.py`'s `_delegation_mcp_server` /
`_call_delegation_endpoint`, mirroring the existing direct-import style
`tests/test_chat_runner.py` uses for `_agnes_mcp_servers` — no real SDK
connection or subprocess needed for these guard-clause / config-shape
checks. The security-critical behavior (RBAC gate, depth-1, one-per-turn,
budget, caller-bound child session) lives server-side in
`ChatManager.handle_delegation` and is covered by `tests/test_agent_
delegation.py`; this file only proves the sandbox-side tool degrades
cleanly rather than exercising the actual network round-trip (an ephemeral
sandbox relay is not something this suite can stand up).
"""

from __future__ import annotations

import asyncio

import pytest


def test_call_delegation_endpoint_no_server_configured(monkeypatch):
    """No AGNES_SERVER (fake-agent / misconfigured spawn) degrades to a
    denial dict instead of attempting a network call."""
    from app.chat import runner

    monkeypatch.delenv("AGNES_SERVER", raising=False)

    result = asyncio.run(runner._call_delegation_endpoint("some-agent", "hello"))
    assert result["status"] == "denied"
    assert result["reason"] == "delegation_unavailable"


@pytest.mark.parametrize("agent_slug,message", [("", "hello"), ("some-agent", ""), ("", "")])
def test_call_delegation_endpoint_rejects_empty_inputs(monkeypatch, agent_slug, message):
    """Malformed tool args (empty slug or message) are denied before any
    network attempt — AGNES_SERVER is set here specifically to prove the
    guard fires before the HTTP leg, not because it's unreachable."""
    from app.chat import runner

    monkeypatch.setenv("AGNES_SERVER", "http://127.0.0.1:1/agnes-api")

    result = asyncio.run(runner._call_delegation_endpoint(agent_slug, message))
    assert result["status"] == "denied"
    assert result["reason"] == "invalid_request"


def test_call_delegation_endpoint_degrades_on_unreachable_server(monkeypatch):
    """A relay that refuses the connection degrades to a denial dict — a
    broken/absent relay must not raise out of the model's tool call."""
    from app.chat import runner

    # Port 1 is a privileged, essentially-always-closed port — a fast,
    # reliable "nothing is listening here" without depending on any real
    # network state.
    monkeypatch.setenv("AGNES_SERVER", "http://127.0.0.1:1/agnes-api")

    result = asyncio.run(runner._call_delegation_endpoint("some-agent", "hello"))
    assert result["status"] == "denied"
    assert result["reason"] == "delegation_request_failed"
    assert result["answer"] is None


def test_delegation_mcp_server_registers_the_tool():
    """The in-process SDK MCP server builds successfully against the
    installed claude-agent-sdk and names the tool `delegate_to_agent` —
    the exact name the endpoint-facing tests exercise via
    `ChatManager.handle_delegation`."""
    from app.chat import runner

    server = runner._delegation_mcp_server()
    if server is None:
        pytest.skip("installed claude-agent-sdk predates create_sdk_mcp_server/tool")
    assert server["type"] == "sdk"
    assert server["name"] == "agnes-delegation"
