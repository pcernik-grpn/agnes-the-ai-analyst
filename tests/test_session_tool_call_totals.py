"""Admin surfaces must report ALL tool calls, not just native ones.

The UsageProcessor splits a session's calls into three disjoint buckets —
``tool_calls`` (native tool_use), ``mcp_calls`` (``mcp__*`` tools) and
``subagent_dispatches`` (Task/Agent) — while ``tool_errors`` counts
``is_error`` across every bucket. A web-chat session whose tools are almost
all MCP used to render as "Tool calls: 2, Errors: 15" (errors > calls).

The repo-level fix (projections + KPI/sort totals summing all three buckets,
on both backends) is pinned in ``tests/db_pg/test_usage_contract.py``. These
grep-level checks pin the DISPLAY layer: each surface that renders a
"tool calls" number must read the breakdown fields, so a template edit can't
quietly regress to the native-only count. Style follows
``test_session_detail_tokens.py::TestDetailPageRendersTokens``.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path("app/web/templates")


class TestTemplatesTotalAllCallKinds:
    def test_detail_header_totals_and_breaks_down_calls(self):
        template = (TEMPLATES / "admin_session_detail.html").read_text(encoding="utf-8")
        assert "mcp_calls" in template and "subagent_dispatches" in template, (
            "the detail header must total native + MCP + subagent calls, not render s.tool_calls alone"
        )
        assert "native" in template and "MCP" in template, (
            "the detail header must show the per-kind breakdown next to the total"
        )

    def test_sessions_list_cell_totals_all_call_kinds(self):
        template = (TEMPLATES / "admin_sessions.html").read_text(encoding="utf-8")
        assert "mcp_calls" in template and "subagent_dispatches" in template, (
            "the sessions-list Tool calls column must total native + MCP + subagent calls"
        )

    def test_user_detail_cell_totals_all_call_kinds(self):
        template = (TEMPLATES / "admin_user_detail.html").read_text(encoding="utf-8")
        assert "mcp_calls" in template and "subagent_dispatches" in template, (
            "the user-detail sessions table must total native + MCP + subagent calls"
        )

    def test_me_activity_cell_totals_all_call_kinds(self):
        template = (TEMPLATES / "me_activity.html").read_text(encoding="utf-8")
        assert "mcp_calls" in template and "subagent_dispatches" in template, (
            "the self-service sessions table must total native + MCP + subagent calls"
        )


class TestCliTotalsAllCallKinds:
    def test_cli_list_and_show_total_all_call_kinds(self):
        source = Path("cli/commands/admin_sessions.py").read_text(encoding="utf-8")
        assert "mcp_calls" in source and "subagent_dispatches" in source, (
            "`agnes admin sessions list/show` must total native + MCP + subagent calls like the web surfaces do"
        )
