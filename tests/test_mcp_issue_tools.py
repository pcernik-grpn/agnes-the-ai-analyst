"""MCP tools for issue reporting (step 1, Task 3).

Mirrors ``tests/test_mcp_semantic_admin_tools.py``'s pattern for the HTTP
foundation transport: each tool is a thin wrapper over the matching REST
endpoint (``app/api/issues.py``, Task 2), so what is pinned here is that the
wrapper calls the right endpoint with the right payload/params, that
``X-Agnes-Client: mcp`` is merged into (never replacing) the caller's own
auth headers, and that the seven tools declare the right read-only/behaviour
flags. The four any-caller tools are also registered on the stdio ``agnes
mcp`` server (``cli/mcp/server.py``) — the admin trio stays HTTP-only, same
as the rest of that closed tool set (#1707 Block 6).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.run(coro)


def _mock_resp(data: Any, status: int = 200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = data
    r.reason_phrase = "OK" if status < 400 else "Error"
    r.text = ""
    r.request = MagicMock()
    r.request.url = "http://testserver"
    r.raise_for_status = MagicMock()
    return r


def _mod():
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mod

    return mod


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


ISSUE_TOOL_NAMES = (
    "report_issue",
    "list_my_issues",
    "get_issue",
    "issue_comment",
    "issue_queue_list",
    "issue_reply",
    "issue_resolve",
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_all_seven_are_foundation_tools():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ISSUE_TOOL_NAMES:
        assert name in FOUNDATION_TOOL_NAMES


def test_registered_on_both_http_transports(seeded_app):
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http

    sse_names = _tool_names(mcp_http.mcp)
    for name in ISSUE_TOOL_NAMES:
        assert name in sse_names

    app_obj = seeded_app["client"].app
    streamable_mcp = app_obj.state.mcp_streamable_instance
    assert streamable_mcp is not None
    streamable_names = _tool_names(streamable_mcp)
    for name in ISSUE_TOOL_NAMES:
        assert name in streamable_names


def test_behaviour_flags():
    """report_issue/issue_comment/issue_reply/issue_resolve write; the three
    list/get tools are read-only. A read-only hint on a write would let a
    client auto-approve filing a report or replying on the user's behalf."""
    mod = _mod()
    tools = {t.name: t for t in asyncio.run(mod.mcp.list_tools())}
    expect_read_only = {
        "report_issue": False,
        "list_my_issues": True,
        "get_issue": True,
        "issue_comment": False,
        "issue_queue_list": True,
        "issue_reply": False,
        "issue_resolve": False,
    }
    for name, read_only in expect_read_only.items():
        assert tools[name].annotations.readOnlyHint is read_only, name


# ---------------------------------------------------------------------------
# HTTP transport — request shape
# ---------------------------------------------------------------------------


class TestReportIssue:
    def test_posts_the_payload_with_the_mcp_client_header(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"id": "iss_abc", "number": 42}, status=201))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.report_issue(title="Tables render raw", kind="bug"))

        assert result["number"] == 42
        assert post.call_args[0][0] == f"{mod._BASE}/api/issues"
        assert post.call_args[1]["json"] == {"title": "Tables render raw", "kind": "bug"}
        headers = post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-Agnes-Client"] == "mcp"

    def test_omits_none_fields(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"id": "iss_abc", "number": 1}, status=201))
            MC.return_value.__aenter__.return_value.post = post
            _run(mod.report_issue(title="x"))

        assert post.call_args[1]["json"] == {"title": "x", "kind": "bug"}


class TestListMyIssues:
    def test_gets_mine_with_status_and_limit(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            get = AsyncMock(return_value=_mock_resp({"data": [], "count": 0, "truncated": None}))
            MC.return_value.__aenter__.return_value.get = get
            result = _run(mod.list_my_issues())

        assert result == {"data": [], "count": 0, "truncated": None}
        assert get.call_args[0][0] == f"{mod._BASE}/api/issues/mine"
        assert get.call_args[1]["params"] == {"status": "open", "limit": 50}
        assert get.call_args[1]["headers"]["X-Agnes-Client"] == "mcp"


class TestGetIssue:
    def test_gets_the_issue_by_ref(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            get = AsyncMock(return_value=_mock_resp({"id": "iss_abc", "number": 42, "comments": []}))
            MC.return_value.__aenter__.return_value.get = get
            result = _run(mod.get_issue("42"))

        assert result["number"] == 42
        assert get.call_args[0][0] == f"{mod._BASE}/api/issues/42"
        assert get.call_args[1]["headers"]["X-Agnes-Client"] == "mcp"


class TestIssueComment:
    def test_posts_the_comment_body(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"id": "isc_1", "author_kind": "reporter"}, status=201))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.issue_comment("42", "more detail"))

        assert result["author_kind"] == "reporter"
        assert post.call_args[0][0] == f"{mod._BASE}/api/issues/42/comments"
        assert post.call_args[1]["json"] == {"body": "more detail"}
        assert post.call_args[1]["headers"]["X-Agnes-Client"] == "mcp"


class TestIssueQueueList:
    def test_gets_the_admin_queue(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            get = AsyncMock(return_value=_mock_resp({"data": [], "count": 0, "truncated": None}))
            MC.return_value.__aenter__.return_value.get = get
            result = _run(mod.issue_queue_list(status="open", limit=100))

        assert result == {"data": [], "count": 0, "truncated": None}
        assert get.call_args[0][0] == f"{mod._BASE}/api/admin/issues"
        assert get.call_args[1]["params"] == {"status": "open", "limit": 100}


class TestIssueReply:
    def test_posts_the_same_comments_endpoint(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"id": "isc_2", "author_kind": "admin"}, status=201))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.issue_reply("42", "on it"))

        assert result["author_kind"] == "admin"
        assert post.call_args[0][0] == f"{mod._BASE}/api/issues/42/comments"
        assert post.call_args[1]["json"] == {"body": "on it"}


class TestIssueResolve:
    def test_posts_the_resolution_note(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            post = AsyncMock(return_value=_mock_resp({"number": 42, "status": "resolved"}))
            MC.return_value.__aenter__.return_value.post = post
            result = _run(mod.issue_resolve("42", "fixed in 0.99.0"))

        assert result["status"] == "resolved"
        assert post.call_args[0][0] == f"{mod._BASE}/api/admin/issues/42/resolve"
        assert post.call_args[1]["json"] == {"resolution_note": "fixed in 0.99.0"}

    def test_a_second_resolve_surfaces_the_typed_409(self):
        mod = _mod()
        with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
            tv.get.return_value = "tok"
            body = {"error": "already_resolved", "message": "#42 was already resolved by ops at T"}
            MC.return_value.__aenter__.return_value.post = AsyncMock(
                return_value=_mock_resp({"detail": body}, status=409)
            )
            with pytest.raises(Exception) as exc:
                _run(mod.issue_resolve("42"))

        assert "already_resolved" in str(exc.value) or "already resolved" in str(exc.value)


# ---------------------------------------------------------------------------
# Stdio transport — cli/mcp/server.py
# ---------------------------------------------------------------------------


def test_stdio_registers_the_four_any_caller_tools():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as stdio_server

    names = _tool_names(stdio_server.mcp)
    for name in ("report_issue", "list_my_issues", "get_issue", "issue_comment"):
        assert name in names
    # The admin trio is deliberately absent from the stdio server.
    for name in ("issue_queue_list", "issue_reply", "issue_resolve"):
        assert name not in names


def test_stdio_report_issue_calls_api_post_json():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as stdio_server

    with patch.object(stdio_server, "api_post_json", return_value={"id": "iss_abc", "number": 1}) as post:
        result = stdio_server.report_issue(title="x", kind="bug")

    assert result == {"id": "iss_abc", "number": 1}
    assert post.call_args[0] == ("/api/issues", {"title": "x", "kind": "bug"})


def test_stdio_list_my_issues_calls_api_get_json():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as stdio_server

    with patch.object(stdio_server, "api_get_json", return_value={"data": [], "count": 0}) as get:
        result = stdio_server.list_my_issues()

    assert result == {"data": [], "count": 0}
    get.assert_called_once_with("/api/issues/mine", status="open", limit=50)


def test_stdio_get_issue_calls_api_get_json():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as stdio_server

    with patch.object(stdio_server, "api_get_json", return_value={"id": "iss_abc"}) as get:
        result = stdio_server.get_issue("42")

    assert result == {"id": "iss_abc"}
    get.assert_called_once_with("/api/issues/42")


def test_stdio_issue_comment_calls_api_post_json():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as stdio_server

    with patch.object(stdio_server, "api_post_json", return_value={"id": "isc_1"}) as post:
        result = stdio_server.issue_comment("42", "more detail")

    assert result == {"id": "isc_1"}
    post.assert_called_once_with("/api/issues/42/comments", {"body": "more detail"})


def test_stdio_translates_v2_client_error():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from cli.mcp import server as stdio_server
    from cli.v2_client import V2ClientError

    with (
        patch.object(stdio_server, "api_post_json", side_effect=V2ClientError(status_code=501, body={})),
        pytest.raises(ValueError, match="report_issue"),
    ):
        stdio_server.report_issue(title="x")
