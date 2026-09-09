"""``admin_access_picture`` — the one MCP call behind the admin starters.

The chat landing page offers an admin three governance starters ("who has
access to which data", "what is shared with no one", "what does a non-admin
see"). Until this tool existed, no MCP tool could answer them: ``stack_browse``
is caller-scoped, ``effective_access`` is self-only, and the admin RBAC reads
(``/api/admin/access-overview``, ``/api/admin/data-packages``) had no MCP leg
at all — so the agent correctly answered "I cannot read this from here" to the
page's own suggestions.

The tool composes three admin GETs the way the ``/admin`` gap cards do, so the
fold rules (which tables count as unreachable when unpackaged, what an
``everyone`` audience means) are the dashboard's, not a second opinion.
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
    r.raise_for_status = MagicMock()
    return r


def _import_mod():
    pytest.importorskip("mcp", reason="mcp package not installed")
    import app.api.mcp_http as mod

    return mod


# A small instance: three groups (Admin, Finance, the Everyone carrier), two
# packages with tables, one empty package nobody is granted, and a registry
# with one orphaned local table, one remote table and one internal table.
_OVERVIEW = {
    "account_total": 12,
    "groups": [
        {"id": "g_admin", "name": "Admin", "is_system": True, "is_everyone": False, "member_count": 2},
        {"id": "g_fin", "name": "Finance", "is_system": False, "is_everyone": False, "member_count": 3},
        {"id": "g_all", "name": "Everyone", "is_system": True, "is_everyone": True, "member_count": 14},
    ],
    "grants": [
        # Finance → Revenue, required tier.
        {
            "id": "gr1",
            "group_id": "g_fin",
            "resource_type": "data_package",
            "resource_id": "pkg_rev",
            "requirement": "required",
            "audience": "g_fin",
            "scope": None,
        },
        # Everyone-scoped grant on the carrier → Marketing package.
        {
            "id": "gr2",
            "group_id": "g_all",
            "resource_type": "data_package",
            "resource_id": "pkg_mkt",
            "requirement": "available",
            "audience": "everyone",
            "scope": "everyone",
        },
        # A grant on another resource type must not leak into the picture.
        {
            "id": "gr3",
            "group_id": "g_fin",
            "resource_type": "marketplace_plugin",
            "resource_id": "some-plugin",
            "requirement": "available",
            "audience": "g_fin",
            "scope": None,
        },
    ],
    "audiences": [],
    "resources": [],
    "families": [],
    "mcp_tool_grants": [],
}

_PACKAGES = [
    {"id": "pkg_rev", "slug": "revenue", "name": "Revenue", "status": "prod", "table_ids": ["t_orders", "t_invoices"]},
    {"id": "pkg_mkt", "slug": "marketing", "name": "Marketing", "status": "prod", "table_ids": ["t_campaigns"]},
    {"id": "pkg_empty", "slug": "empty", "name": "Empty", "status": "draft", "table_ids": []},
]

_TABLES = {
    "tables": [
        {"id": "t_orders", "name": "Orders", "query_mode": "local"},
        {"id": "t_invoices", "name": "Invoices", "query_mode": "materialized"},
        {"id": "t_campaigns", "name": "Campaigns", "query_mode": ""},
        {"id": "t_orphan", "name": "Orphan", "query_mode": "local"},
        {"id": "t_remote", "name": "BQ Sessions", "query_mode": "remote"},
        {"id": "t_internal", "name": "agnes_usage", "query_mode": "internal"},
    ],
    "count": 6,
}


def _dispatch(url: str, **_kwargs) -> MagicMock:
    if "/api/admin/access-overview" in url:
        return _mock_resp(_OVERVIEW)
    if "/api/admin/data-packages" in url:
        return _mock_resp(_PACKAGES)
    if "/api/catalog/tables" in url:
        return _mock_resp(_TABLES)
    raise AssertionError(f"unexpected GET {url}")


def _call(**kwargs) -> tuple[dict, AsyncMock]:
    mod = _import_mod()
    with patch("app.api.mcp_http._current_token") as tv, patch("httpx.AsyncClient") as MC:
        tv.get.return_value = "tok"
        mock_get = AsyncMock(side_effect=_dispatch)
        MC.return_value.__aenter__.return_value.get = mock_get
        result = _run(mod.admin_access_picture(**kwargs))
    return result, mock_get


def test_admin_access_picture_is_a_foundation_tool():
    from app.api.mcp.foundation_tools import FOUNDATION_TOOL_NAMES

    assert "admin_access_picture" in FOUNDATION_TOOL_NAMES


def test_each_package_says_which_groups_reach_it_and_how_big_they_are():
    result, _ = _call()

    by_id = {p["id"]: p for p in result["packages"]}
    rev = by_id["pkg_rev"]
    assert rev["name"] == "Revenue"
    assert rev["table_count"] == 2
    assert [t["id"] for t in rev["tables"]] == ["t_orders", "t_invoices"]
    assert rev["granted_to"] == [
        {
            "group_id": "g_fin",
            "group_name": "Finance",
            "requirement": "required",
            "audience": "group",
            "member_count": 3,
        }
    ]
    # An everyone-scoped grant is reported as the audience it is — every
    # account — never attributed to the carrier group's own roster.
    mkt = by_id["pkg_mkt"]
    assert mkt["granted_to"] == [
        {
            "group_id": "g_all",
            "group_name": "Everyone",
            "requirement": "available",
            "audience": "everyone",
            "member_count": 12,
        }
    ]
    assert result["account_total"] == 12
    assert result["source"] == "server"


def test_a_grant_on_another_resource_type_does_not_leak_into_the_picture():
    result, _ = _call()

    for pkg in result["packages"]:
        assert all(g["group_id"] != "g_fin" or pkg["id"] == "pkg_rev" for g in pkg["granted_to"])


def test_unreachable_lists_orphaned_distributable_tables_and_ungranted_packages():
    result, _ = _call()

    unreachable = result["unreachable"]
    # Same fold as the /admin gap card: blank query_mode reads as local,
    # `remote` answers server-side without a package, `internal` has no
    # parquet to pull — only the local orphan is unreachable.
    assert [t["id"] for t in unreachable["tables_in_no_package"]] == ["t_orphan"]
    assert [p["id"] for p in unreachable["packages_granted_to_no_group"]] == ["pkg_empty"]


def test_by_group_shows_the_non_admin_view_and_marks_admin_bypass():
    result, _ = _call()

    by_group = {g["group_id"]: g for g in result["by_group"]}
    fin = by_group["g_fin"]
    assert fin["bypasses_grants"] is False
    # Direct grant plus whatever Everyone gets — that is what a Finance member sees.
    assert sorted((p["id"], p["via"]) for p in fin["packages"]) == [("pkg_mkt", "everyone"), ("pkg_rev", "group")]

    admin = by_group["g_admin"]
    assert admin["bypasses_grants"] is True
    assert sorted(p["id"] for p in admin["packages"]) == ["pkg_empty", "pkg_mkt", "pkg_rev"]

    # The Everyone baseline is its own entry, sized in people, not carrier rows.
    everyone = by_group["everyone"]
    assert everyone["member_count"] == 12
    assert [p["id"] for p in everyone["packages"]] == ["pkg_mkt"]
    # The carrier group does not ALSO appear as an ordinary group.
    assert "g_all" not in by_group


def test_include_tables_false_keeps_counts_but_drops_table_lists():
    result, _ = _call(include_tables=False)

    for pkg in result["packages"]:
        assert "tables" not in pkg
        assert isinstance(pkg["table_count"], int)
    # The unreachable tray is the point of the call; it stays.
    assert [t["id"] for t in result["unreachable"]["tables_in_no_package"]] == ["t_orphan"]


def test_reads_the_three_admin_endpoints_the_dashboard_reads():
    _, mock_get = _call()

    urls = [c.args[0] for c in mock_get.call_args_list]
    assert any(u.endswith("/api/admin/access-overview") for u in urls)
    assert any(u.endswith("/api/admin/data-packages") for u in urls)
    assert any(u.endswith("/api/catalog/tables") for u in urls)
    pkg_call = next(c for c in mock_get.call_args_list if c.args[0].endswith("/api/admin/data-packages"))
    assert pkg_call.kwargs["params"] == {"include_table_ids": "true"}


def test_declares_itself_read_only():
    mod = _import_mod()
    tool = mod.mcp._tool_manager.get_tool("admin_access_picture")
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
