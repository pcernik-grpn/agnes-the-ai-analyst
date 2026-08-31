"""MCP steering toward the semantic layer (consumption loop, block 2).

The server-level ``instructions`` string and the wire descriptions of the
``catalog`` / ``query`` foundation tools are the only guidance an MCP client
reads *before* it starts writing SQL. An instance can hold a fully populated
semantic layer and still be queried as if it had none, because nothing in
that pre-flight text mentions it.

Two things are pinned here:

* the steering itself — glossary / context lookup for a business term,
  ``validate_semantic_query`` before running SQL over modeled data;
* that the two transports (SSE ``app/api/mcp_http.py`` and Streamable-HTTP
  ``app/api/mcp_streamable.py``) read ONE constant. They previously carried
  byte-identical hand-copies, which is exactly how the 18-of-24 tool drift
  this repo already fixed once got started.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _instructions() -> str:
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api.mcp.foundation_tools import SERVER_INSTRUCTIONS

    return SERVER_INSTRUCTIONS


def _tool_description(name: str) -> str:
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mod

    tool = mod.mcp._tool_manager.get_tool(name)
    assert tool is not None, f"foundation tool {name!r} is not registered"
    return tool.description or ""


class TestServerInstructions:
    def test_both_transports_read_one_shared_constant(self):
        """No transport may carry its own copy of the instructions prose."""
        pytest.importorskip("mcp", reason="mcp package not installed")
        import app.api.mcp_http as http_mod

        assert http_mod.mcp.instructions == _instructions()

        for rel in ("app/api/mcp_http.py", "app/api/mcp_streamable.py"):
            source = (_REPO_ROOT / rel).read_text()
            assert "SERVER_INSTRUCTIONS" in source, f"{rel} does not use the shared constant"
            assert "self-hosted AI harness" not in source, (
                f"{rel} still hand-copies the instructions prose — import SERVER_INSTRUCTIONS instead"
            )

    def test_steers_business_terms_to_the_semantic_layer(self):
        text = _instructions().lower()
        assert "glossary_search" in text
        assert "get_semantic_context" in text

    def test_asks_for_validation_before_running_sql_over_modeled_data(self):
        assert "validate_semantic_query" in _instructions()

    def test_keeps_the_original_discovery_steering(self):
        """The semantic guidance is additive — catalog/schema/describe/query
        and the server_info connectivity check must survive."""
        text = _instructions()
        for token in ("`catalog`", "`schema`", "`describe`", "`query`", "`server_info`"):
            assert token in text


class TestFoundationToolDescriptions:
    def test_query_description_points_at_the_validator(self):
        """The wire description is the FIRST docstring paragraph only, so a
        cross-reference buried further down never reaches the agent."""
        assert "validate_semantic_query" in _tool_description("query")

    def test_query_description_names_the_soft_enforce_response_field(self):
        assert "semantic_validation" in _tool_description("query")

    def test_catalog_description_points_at_the_semantic_layer(self):
        description = _tool_description("catalog")
        assert "glossary_search" in description or "get_semantic_context" in description
