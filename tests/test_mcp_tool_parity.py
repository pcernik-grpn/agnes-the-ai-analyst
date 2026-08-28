"""Both MCP transports must expose the same foundation tools.

Root cause of the drift this guards against: mcp_streamable.py hand-duplicated
6 of the 24 foundation tools defined in mcp_http.py, so a remote OAuth
connector (streamable transport) silently lost knowledge_search,
collections_*, skills, chat_skills, stack_*, store_*, and admin tools. Both
transports now register from the shared `app.api.mcp.foundation_tools` module.
"""

from __future__ import annotations

import asyncio

import pytest


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_sse_exposes_all_foundation_tools():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert set(FOUNDATION_TOOL_NAMES) <= _tool_names(mcp_http.mcp)


def test_streamable_exposes_all_foundation_tools(seeded_app):
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    app = seeded_app["client"].app
    mcp = app.state.mcp_streamable_instance
    assert mcp is not None, "streamable MCP instance was not mounted (check SERVER_URL/AGNES_BASE_URL in env)"

    assert set(FOUNDATION_TOOL_NAMES) <= _tool_names(mcp)


def test_glossary_search_is_a_foundation_tool():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert "glossary_search" in FOUNDATION_TOOL_NAMES


def test_semantic_model_tools_are_foundation_tools():
    """Open semantic-layer contract (Task 12)."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ("semantic_model_search", "semantic_model_get"):
        assert name in FOUNDATION_TOOL_NAMES


def test_validate_semantic_query_is_a_foundation_tool():
    """Query-validation engine wiring (wave 3)."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert "validate_semantic_query" in FOUNDATION_TOOL_NAMES


def test_semantic_context_and_schema_tools_are_foundation_tools():
    """Agent read-parity tools (wave 4)."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ("get_semantic_context", "get_semantic_schema"):
        assert name in FOUNDATION_TOOL_NAMES


def test_activity_tool_is_a_foundation_tool():
    """Unified Activity Center timeline (E3 slice 2) — CLI/REST/web had no
    MCP counterpart before this."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert "activity" in FOUNDATION_TOOL_NAMES


def test_data_apps_tools_are_foundation_tools():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ("data_apps_list", "data_app_get", "data_app_deploy", "data_app_logs"):
        assert name in FOUNDATION_TOOL_NAMES


def test_data_apps_draft_tools_are_foundation_tools():
    """Wave 3B draft/credential MCP tools (Task 8)."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in ("data_app_create_draft", "data_app_delete_draft", "data_app_git_credential"):
        assert name in FOUNDATION_TOOL_NAMES


def test_data_apps_preview_tools_are_foundation_tools():
    """Wave 3C in-chat preview loop MCP tools (Task 4) — chat-surface-only,
    no REST/CLI analogue."""
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    for name in (
        "agnes_data_app_preview",
        "agnes_data_app_refresh",
        "agnes_data_app_close",
        "agnes_data_app_credentials",
    ):
        assert name in FOUNDATION_TOOL_NAMES


def test_data_app_tool_names_is_subset_of_foundation():
    """The data-app family constant may not drift from the foundation list."""
    from app.api.mcp.foundation_tools import (
        DATA_APP_TOOL_NAMES,
        FOUNDATION_TOOL_NAMES,
    )

    assert set(DATA_APP_TOOL_NAMES) <= set(FOUNDATION_TOOL_NAMES)


def test_stdio_server_exposes_data_app_family():
    """The CLI stdio ``agnes mcp`` server (cli/mcp/server.py) — the surface the
    in-chat authoring agent connects through — must expose the whole data-app
    family, or the chat agent can neither author/deploy nor emit the
    ``data_app_preview`` render frame (the wave-3C in-chat preview pane).

    The two HTTP foundation transports are covered above; the stdio server is a
    separately hand-maintained curated subset, so it needs its own guard.
    """
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api.mcp.foundation_tools import DATA_APP_TOOL_NAMES
    from cli.mcp import server as stdio_server

    assert set(DATA_APP_TOOL_NAMES) <= _tool_names(stdio_server.mcp)


# --- Behaviour annotations (CON-1) ---------------------------------------
#
# Both the Anthropic and the OpenAI directory submissions check that every tool
# declares what it does to state. A client that auto-approves read-only calls
# also relies on the flag, so an unannotated tool is a safety gap and not just a
# submission blocker. `progressive_tool` makes `read_only` a required keyword;
# these lock the result in place from the outside.


def _tools_by_name(mcp) -> dict:
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_every_foundation_tool_declares_its_behaviour():
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    tools = _tools_by_name(mcp_http.mcp)
    for name in FOUNDATION_TOOL_NAMES:
        ann = getattr(tools[name], "annotations", None)
        assert ann is not None, f"{name} carries no annotations"
        assert ann.title, f"{name} has no human-readable title"
        assert ann.readOnlyHint is not None, f"{name} does not declare readOnlyHint"
        assert ann.destructiveHint is not None, f"{name} does not declare destructiveHint"
        assert ann.openWorldHint is not None, f"{name} does not declare openWorldHint"


def test_a_read_only_tool_is_never_marked_destructive():
    """The invariant `progressive_tool` enforces — a reader destroys nothing."""
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    tools = _tools_by_name(mcp_http.mcp)
    for name in FOUNDATION_TOOL_NAMES:
        ann = tools[name].annotations
        if ann.readOnlyHint:
            assert ann.destructiveHint is False, f"{name} is read-only but flagged destructive"


"""Verbs a tool title may open with — the action the caller is authorizing."""
_TITLE_VERBS = {
    "apply",
    "list",
    "get",
    "search",
    "read",
    "create",
    "update",
    "delete",
    "add",
    "remove",
    "browse",
    "subscribe",
    "unsubscribe",
    "rate",
    "publish",
    "deploy",
    "enqueue",
    "migrate",
    "ask",
    "upload",
    "dismiss",
    "reingest",
    "contribute",
    "describe",
    "query",
    "refresh",
    "close",
    "preview",
    "audit",
    "set",
    "test",
    "sync",
    "check",
    "run",
    "open",
    "show",
    "register",
}


def test_every_tool_title_says_what_calling_it_does():
    """A tool picker renders the title, so the title has to name an action.

    Agnes names tools `resource_action` (`collections_list` → "Collections
    List"): the verb is last rather than first, which is not OpenAI's
    `get_order_status` shape but does say what the call does. What genuinely
    told the reader nothing was the dozen bare nouns — `catalog`, `schema`,
    `skills`. Renaming the tools would break every configured client, so the
    action lives in the title instead (`TITLE_OVERRIDES` in src/mcp_tooling.py).

    So this enforces the invariant that actually matters — a title names an
    action SOMEWHERE — and deliberately does not require verb-first, which
    would be a cosmetic rewrite of 51 working titles.
    """
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    tools = _tools_by_name(mcp_http.mcp)
    offenders = []
    for name in FOUNDATION_TOOL_NAMES:
        title = tools[name].annotations.title or ""
        if not any(word.lower() in _TITLE_VERBS for word in title.split(" ")):
            offenders.append(f"{name} → {title!r}")
    assert not offenders, (
        "these titles name no action, so a tool picker shows a bare noun; add a "
        "TITLE_OVERRIDES entry in src/mcp_tooling.py: " + ", ".join(offenders)
    )


def test_stdio_and_http_agree_on_shared_tool_behaviour():
    """A tool of the same name must mean the same thing on both surfaces.

    The stdio server is hand-maintained, so nothing but a test stops
    `stack_unsubscribe` from being destructive over HTTP and read-only over
    stdio — exactly the drift the name-parity guards above already prevent for
    the tool *list*.
    """
    pytest.importorskip("mcp", reason="mcp package not installed")
    from app.api import mcp_http
    from cli.mcp import server as stdio_server

    http_tools = _tools_by_name(mcp_http.mcp)
    stdio_tools = _tools_by_name(stdio_server.mcp)

    shared = set(http_tools) & set(stdio_tools)
    assert shared, "expected the two surfaces to share tools"
    for name in sorted(shared):
        h, s = http_tools[name].annotations, stdio_tools[name].annotations
        assert s is not None, f"stdio {name} carries no annotations"
        assert (h.readOnlyHint, h.destructiveHint) == (s.readOnlyHint, s.destructiveHint), (
            f"{name} declares different behaviour on stdio vs HTTP"
        )
        assert h.title == s.title, f"{name} is titled differently on stdio vs HTTP"
