"""Tests for the stdio MCP server's dynamic passthrough registration.

Cover:

* The dynamic helper handles a missing server (V2ClientError on the GET)
  by returning ``[]`` silently — never explodes the stdio entrypoint.
* When the server returns a tool list, every entry is registered on the
  given FastMCP instance with the correct exposed name + description,
  and the synthesized callable posts to the right ``/call`` endpoint.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("mcp", reason="mcp SDK not installed")

from cli.mcp._dynamic_passthrough import register_passthrough_tools
from cli.v2_client import V2ClientError
from mcp.server.fastmcp import FastMCP


def _fresh_mcp() -> FastMCP:
    return FastMCP("AgnesTest", instructions="test")


# ── helper: GET failure paths ─────────────────────────────────────────────


def test_register_silent_on_v2_client_error():
    mcp_inst = _fresh_mcp()
    with patch(
        "cli.mcp._dynamic_passthrough.api_get_json",
        side_effect=V2ClientError(status_code=404, body="not found"),
    ):
        registered = register_passthrough_tools(mcp_inst)
    assert registered == []
    # No tools registered means tool list is the empty set (FastMCP exposes
    # `_tool_manager` — using only its public size invariant here).
    assert mcp_inst._tool_manager.list_tools() == []


def test_register_silent_on_unexpected_exception():
    mcp_inst = _fresh_mcp()
    with patch(
        "cli.mcp._dynamic_passthrough.api_get_json",
        side_effect=ConnectionError("dns failed"),
    ):
        registered = register_passthrough_tools(mcp_inst)
    assert registered == []


# ── helper: success path ──────────────────────────────────────────────────


def _sample_tool_list():
    return [
        {
            "tool_id": "test-upstream.lookup",
            "source_id": "src_test",
            "source_name": "test-upstream",
            "exposed_name": "lookup",
            "description": "Look up a thing.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        {
            # Tool with non-identifier prop name — must fall back to **kwargs.
            "tool_id": "test-upstream.weird",
            "source_id": "src_test",
            "source_name": "test-upstream",
            "exposed_name": "weird",
            "description": "Bad prop name.",
            "input_schema": {
                "type": "object",
                "properties": {"some-thing": {"type": "string"}},
                "required": ["some-thing"],
            },
        },
    ]


def test_register_registers_each_tool_with_namespaced_name():
    mcp_inst = _fresh_mcp()
    with patch("cli.mcp._dynamic_passthrough.api_get_json", return_value=_sample_tool_list()):
        registered = register_passthrough_tools(mcp_inst)
    assert registered == ["test-upstream.lookup", "test-upstream.weird"]

    # The two tools should both be discoverable on the FastMCP instance.
    names = {t.name for t in mcp_inst._tool_manager.list_tools()}
    assert names == {"test-upstream.lookup", "test-upstream.weird"}


def test_registered_callable_posts_to_invoke_endpoint():
    mcp_inst = _fresh_mcp()
    posted = []

    def _fake_post(path, payload):
        posted.append((path, payload))
        return {"is_error": False, "text": "ok"}

    with patch("cli.mcp._dynamic_passthrough.api_get_json", return_value=_sample_tool_list()):
        with patch("cli.mcp._dynamic_passthrough.api_post_json", side_effect=_fake_post):
            register_passthrough_tools(mcp_inst)

            # Resolve the registered tool's callable and invoke it directly.
            tool = mcp_inst._tool_manager.get_tool("test-upstream.lookup")
            assert tool is not None
            # FastMCP wraps the callable; calling Tool.fn directly skips
            # parameter validation — sufficient for routing assertion.
            result = tool.fn(query="Alice", limit=5)

    assert result == "ok"
    assert posted == [
        (
            "/api/mcp/passthrough/tools/test-upstream.lookup/call",
            {"arguments": {"query": "Alice", "limit": 5}},
        )
    ]


def test_registered_kwargs_fallback_for_unsafe_prop_names():
    """Tools whose input schema has non-identifier prop names register a
    ``**kwargs`` wrapper so exec() never sees the unsafe identifier."""
    mcp_inst = _fresh_mcp()
    posted = []

    def _fake_post(path, payload):
        posted.append((path, payload))
        return {"is_error": False, "text": "ok"}

    with patch("cli.mcp._dynamic_passthrough.api_get_json", return_value=_sample_tool_list()):
        with patch("cli.mcp._dynamic_passthrough.api_post_json", side_effect=_fake_post):
            register_passthrough_tools(mcp_inst)
            tool = mcp_inst._tool_manager.get_tool("test-upstream.weird")
            # Invoke with a non-identifier key — must go through the
            # kwargs wrapper and forward as-is.
            tool.fn(**{"some-thing": "x"})

    assert posted == [
        (
            "/api/mcp/passthrough/tools/test-upstream.weird/call",
            {"arguments": {"some-thing": "x"}},
        )
    ]


def _noarg_tool_list():
    return [
        {
            "tool_id": "test-upstream.ping",
            "source_id": "src_test",
            "source_name": "test-upstream",
            "exposed_name": "ping",
            "description": "No-arg tool.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def test_registered_noarg_tool_has_no_kwargs_param():
    """Tools with an empty input schema (no properties) must register a
    parameterless callable — NOT a ``**kwargs`` wrapper, which FastMCP
    renders as a required ``kwargs`` field that breaks the only valid
    (empty) call."""
    mcp_inst = _fresh_mcp()
    with patch("cli.mcp._dynamic_passthrough.api_get_json", return_value=_noarg_tool_list()):
        register_passthrough_tools(mcp_inst)

    tool = mcp_inst._tool_manager.get_tool("test-upstream.ping")
    assert tool is not None
    # ``Tool.parameters`` is overwritten with the upstream input_schema after
    # registration, so it always looks clean. The schema FastMCP *actually*
    # validates incoming calls against is derived from the synthesized
    # function signature (``fn_metadata.arg_model``) — that's where the
    # ``**kwargs`` wrapper leaks a required ``kwargs`` field and breaks the
    # only valid (empty) call. Assert on the real validation model.
    schema = tool.fn_metadata.arg_model.model_json_schema()
    props = (schema or {}).get("properties") or {}
    required = (schema or {}).get("required") or []
    assert "kwargs" not in props, f"unexpected kwargs param: {schema}"
    assert "kwargs" not in required


# ── behaviour annotations (issue #2161) ───────────────────────────────────


def test_registered_tools_carry_read_only_hint_from_the_mutating_flag():
    """An MCP client that auto-approves read-only calls keys on
    ``annotations.readOnlyHint``; the stdio mirror derives it from the
    listing's ``mutating`` flag exactly as the server transports do."""
    mcp_inst = _fresh_mcp()
    tools = _sample_tool_list()
    tools[0]["mutating"] = False
    tools[1]["mutating"] = True
    with patch("cli.mcp._dynamic_passthrough.api_get_json", return_value=tools):
        register_passthrough_tools(mcp_inst)
    lookup = mcp_inst._tool_manager.get_tool("test-upstream.lookup")
    weird = mcp_inst._tool_manager.get_tool("test-upstream.weird")
    assert lookup.annotations.readOnlyHint is True and lookup.annotations.destructiveHint is False
    assert weird.annotations.readOnlyHint is False and weird.annotations.destructiveHint is True


def test_a_listing_without_the_mutating_field_yields_tools_that_ask():
    """A server that predates the field: fail toward asking, never toward
    running a possibly-writing tool unasked."""
    mcp_inst = _fresh_mcp()
    with patch("cli.mcp._dynamic_passthrough.api_get_json", return_value=_sample_tool_list()):
        register_passthrough_tools(mcp_inst)
    lookup = mcp_inst._tool_manager.get_tool("test-upstream.lookup")
    assert lookup.annotations.readOnlyHint is False


def test_the_two_annotation_helpers_agree():
    """``cli/mcp/_dynamic_passthrough.py`` keeps a copy of the server-side
    helper (it must not import the server side); pin the two to the same
    verdicts so a change to one cannot leave the other behind."""
    from app.api.mcp.tools_generator import passthrough_annotations as server_side
    from cli.mcp._dynamic_passthrough import passthrough_annotations as cli_side

    for mutating in (True, False, None, 1, 0):
        assert server_side(mutating) == cli_side(mutating)
