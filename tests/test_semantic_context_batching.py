"""One call, not N: the semantic read surfaces batch.

`GET /api/semantic-models/context` has always accepted a LIST of selections,
but both callers hardcoded a single-element list — so "what exists here?"
cost one round trip per type and reading twenty metrics cost twenty calls.
Each of those payloads then sits in the agent's conversation for the rest of
the session, which is the term that dominates a long session's cost.

These tests pin the batching at the two surfaces an agent actually uses
(the MCP foundation tool and the CLI), by asserting on the selections each
one puts on the wire.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from cli.commands.semantic_model import semantic_model_app


def _captured_selections_from_cli(args: list[str]) -> list[dict]:
    seen: dict = {}

    def _api_get(path, params=None, **kwargs):
        seen["path"] = path
        seen["params"] = params or {}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"results": [], "unknown_types": []}
        return resp

    with patch("cli.commands.semantic_model.api_get", _api_get):
        result = CliRunner().invoke(semantic_model_app, args)
    assert result.exit_code == 0, result.output
    return json.loads(seen["params"]["selections"])


class TestCliBatches:
    def test_several_types_are_one_call(self):
        selections = _captured_selections_from_cli(["context", "dataset", "metric", "relationship"])
        assert [s["semantic_type"] for s in selections] == ["dataset", "metric", "relationship"]
        # No ids => compact mode for each, which is the cheap first pass.
        assert all(s["ids"] is None for s in selections)

    def test_a_single_type_still_works_unchanged(self):
        selections = _captured_selections_from_cli(["context", "metric"])
        assert selections == [{"semantic_type": "metric", "ids": None}]

    def test_several_ids_are_one_call(self):
        selections = _captured_selections_from_cli(
            ["context", "metric", "--id", "revenue", "--id", "aov", "--id", "margin"]
        )
        assert selections == [{"semantic_type": "metric", "ids": ["revenue", "aov", "margin"]}]


class _RecordingMcp:
    """Captures the tool functions `register_foundation_tools` registers."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def _decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _decorate

    # `progressive_tool` may reach for either spelling depending on build.
    def add_tool(self, fn, *args, **kwargs):
        self.tools[getattr(fn, "__name__", "?")] = fn
        return fn


class TestMcpToolBatches:
    """Drives the REAL tool body against a stubbed HTTP layer, so what is
    asserted is the selections that actually go on the wire."""

    @staticmethod
    def _tool():
        from app.api.mcp.foundation_tools import register_foundation_tools

        mcp = _RecordingMcp()
        register_foundation_tools(mcp, base_url="http://x", headers_fn=lambda: {})
        assert "get_semantic_context" in mcp.tools, sorted(mcp.tools)[:5]
        return mcp.tools["get_semantic_context"]

    @classmethod
    def _selections_for(cls, semantic_type, ids=None) -> list[dict]:
        fn = cls._tool()
        captured: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"results": [], "unknown_types": []}

            @staticmethod
            def raise_for_status():
                return None

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, headers=None, params=None, timeout=None):
                captured["params"] = params or {}
                return _Resp()

        with patch("app.api.mcp.foundation_tools.httpx.AsyncClient", _Client):
            kwargs = {"semantic_type": semantic_type}
            if ids is not None:
                kwargs["ids"] = ids
            asyncio.run(fn(**kwargs))
        return json.loads(captured["params"]["selections"])

    def test_string_and_list_are_both_accepted(self):
        assert self._selections_for("dataset") == [{"semantic_type": "dataset", "ids": None}]
        assert self._selections_for(["dataset", "metric", "relationship"]) == [
            {"semantic_type": "dataset", "ids": None},
            {"semantic_type": "metric", "ids": None},
            {"semantic_type": "relationship", "ids": None},
        ]

    def test_ids_apply_to_every_requested_type(self):
        assert self._selections_for(["metric", "dataset"], ids=["revenue"]) == [
            {"semantic_type": "metric", "ids": ["revenue"]},
            {"semantic_type": "dataset", "ids": ["revenue"]},
        ]

    def test_several_ids_for_one_type_are_one_call(self):
        assert self._selections_for("metric", ids=["revenue", "aov"]) == [
            {"semantic_type": "metric", "ids": ["revenue", "aov"]}
        ]


def test_unknown_type_is_passed_through_not_crashed_on():
    """An unknown type comes back from the server as `unknown_types`; the
    client must not pre-judge it with a traceback."""
    selections = _captured_selections_from_cli(["context", "nope"])
    assert selections == [{"semantic_type": "nope", "ids": None}]
