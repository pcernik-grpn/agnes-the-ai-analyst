"""``admin_access_picture`` — the one MCP call behind the admin starters.

The chat landing page offers an admin three governance starters ("who has
access to which data", "what is shared with no one", "what does a non-admin
see"). Until this tool existed, no MCP tool could answer them: ``stack_browse``
is caller-scoped, ``effective_access`` is self-only, and the admin RBAC reads
(``/api/admin/access-overview``, ``/api/admin/data-packages``,
``/api/admin/registry``) had no MCP leg at all — so the agent correctly
answered "I cannot read this from here" to the page's own suggestions.

The tool composes three admin GETs the way the ``/admin`` gap cards do, so the
fold rules (which tables count as unreachable when unpackaged, what an
``everyone`` audience means, which package statuses an analyst can actually
reach) are the dashboard's and the stack resolver's, not a second opinion.

Why the table inventory is ``/api/admin/registry`` and not the catalog: every
agent credential (the chat sandbox JWT, an MCP-OAuth connector token, the
engine's MCP ticket) is ``credential_surface='stack'``, and for that surface
the catalog narrows even an ADMIN to their own stack — which is exactly the
set of tables that are NOT orphaned. The registry is gated by plain
``require_admin`` and is the inventory ``agnes admin list-tables`` reads.
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


# A small instance: three groups (Admin, Finance, the Everyone carrier); a
# prod package granted to Finance, a prod package granted to everyone, a prod
# package granted to nobody, a DRAFT granted to Finance and a COMING-SOON
# granted to everyone; a registry with one orphaned local table, one remote
# table and one internal table (the registry says which rows are packaged).
_OVERVIEW = {
    "account_total": 12,
    "groups": [
        {"id": "g_admin", "name": "Admin", "is_system": True, "is_everyone": False, "member_count": 2},
        {"id": "g_fin", "name": "Finance", "is_system": False, "is_everyone": False, "member_count": 3},
        {"id": "g_all", "name": "Everyone", "is_system": True, "is_everyone": True, "member_count": 14},
    ],
    "grants": [
        {
            "id": "gr1",
            "group_id": "g_fin",
            "resource_type": "data_package",
            "resource_id": "pkg_rev",
            "requirement": "required",
            "audience": "g_fin",
            "scope": None,
        },
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
        # Lifecycle: a draft Finance can "see" only as an admin, and a
        # coming-soon everyone can browse but never pull.
        {
            "id": "gr4",
            "group_id": "g_fin",
            "resource_type": "data_package",
            "resource_id": "pkg_draft",
            "requirement": "available",
            "audience": "g_fin",
            "scope": None,
        },
        {
            "id": "gr5",
            "group_id": "g_all",
            "resource_type": "data_package",
            "resource_id": "pkg_soon",
            "requirement": "required",
            "audience": "everyone",
            "scope": "everyone",
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
    {"id": "pkg_empty", "slug": "empty", "name": "Empty", "status": "prod", "table_ids": []},
    {"id": "pkg_draft", "slug": "wip", "name": "WIP", "status": "draft", "table_ids": ["t_wip"]},
    {"id": "pkg_soon", "slug": "soon", "name": "Soon", "status": "coming-soon", "table_ids": []},
]

_REGISTRY = {
    "tables": [
        {"id": "t_orders", "name": "Orders", "query_mode": "local", "packaged": True},
        {"id": "t_invoices", "name": "Invoices", "query_mode": "materialized", "packaged": True},
        {"id": "t_campaigns", "name": "Campaigns", "query_mode": "", "packaged": True},
        {"id": "t_wip", "name": "WIP table", "query_mode": "local", "packaged": True},
        {"id": "t_orphan", "name": "Orphan", "query_mode": "local", "packaged": False},
        {"id": "t_remote", "name": "BQ Sessions", "query_mode": "remote", "packaged": False},
        {"id": "t_internal", "name": "agnes_usage", "query_mode": "internal", "packaged": False},
    ],
    "count": 7,
}


def _dispatch(url: str, **_kwargs) -> MagicMock:
    if "/api/admin/access-overview" in url:
        return _mock_resp(_OVERVIEW)
    if "/api/admin/data-packages" in url:
        return _mock_resp(_PACKAGES)
    if "/api/admin/registry" in url:
        return _mock_resp(_REGISTRY)
    raise AssertionError(f"unexpected GET {url}")


def _call(auto_membership: bool = True, dispatch=_dispatch, **kwargs) -> tuple[dict, AsyncMock]:
    mod = _import_mod()
    with (
        patch("app.api.mcp_http._current_token") as tv,
        patch("httpx.AsyncClient") as MC,
        patch("app.api.mcp.foundation_tools._stack_auto_membership", return_value=auto_membership),
    ):
        tv.get.return_value = "tok"
        mock_get = AsyncMock(side_effect=dispatch)
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
    assert rev["status"] == "prod"
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
    assert result["captured_at"]


def test_a_grant_on_another_resource_type_does_not_leak_into_the_picture():
    result, _ = _call()

    for pkg in result["packages"]:
        for g in pkg["granted_to"]:
            assert (g["group_id"], pkg["id"]) != ("g_fin", "some-plugin")
    assert all(p["id"] != "some-plugin" for p in result["packages"])


def test_unreachable_uses_the_registry_packaged_flag_and_the_dashboard_fold():
    result, _ = _call()

    unreachable = result["unreachable"]
    # Same fold as the /admin gap card: blank query_mode reads as local,
    # `remote` answers server-side without a package, `internal` has no
    # parquet to pull — only the local orphan is unreachable.
    assert [t["id"] for t in unreachable["tables_in_no_package"]] == ["t_orphan"]
    assert unreachable["tables_in_no_package_total"] == 1
    assert unreachable["tables_in_no_package_truncated"] is False
    # A draft granted to nobody is the normal authoring state, not a gap;
    # only a package an analyst could see, granted to nobody, is one.
    assert [p["id"] for p in unreachable["packages_granted_to_no_group"]] == ["pkg_empty"]


def test_registry_rows_without_a_packaged_flag_fall_back_to_the_package_union():
    registry = {"tables": [{k: v for k, v in t.items() if k != "packaged"} for t in _REGISTRY["tables"]], "count": 7}

    def dispatch(url: str, **_kwargs):
        if "/api/admin/registry" in url:
            return _mock_resp(registry)
        return _dispatch(url)

    result, _ = _call(dispatch=dispatch)

    assert [t["id"] for t in result["unreachable"]["tables_in_no_package"]] == ["t_orphan"]


def test_by_group_shows_the_non_admin_view_and_marks_admin_bypass():
    result, _ = _call()

    by_group = {g["group_id"]: g for g in result["by_group"]}
    fin = by_group["g_fin"]
    assert fin["bypasses_grants"] is False
    # Direct grant plus whatever Everyone gets — that is what a Finance member
    # sees. The draft (hidden from analysts) and the coming-soon package
    # (browsable, never deliverable) are NOT in the reach — the same rule
    # StackResolver.stack applies.
    assert sorted((p["id"], p["via"]) for p in fin["packages"]) == [("pkg_mkt", "everyone"), ("pkg_rev", "group")]

    admin = by_group["g_admin"]
    assert admin["bypasses_grants"] is True
    assert sorted(p["id"] for p in admin["packages"]) == ["pkg_draft", "pkg_empty", "pkg_mkt", "pkg_rev", "pkg_soon"]

    # The Everyone baseline is its own entry, sized in people, not carrier rows.
    everyone = by_group["everyone"]
    assert everyone["member_count"] == 12
    assert [p["id"] for p in everyone["packages"]] == ["pkg_mkt"]
    # The carrier group does not ALSO appear as an ordinary group.
    assert "g_all" not in by_group


def test_packages_carry_their_lifecycle_visibility():
    result, _ = _call()

    by_id = {p["id"]: p for p in result["packages"]}
    assert (by_id["pkg_rev"]["visible_to_analysts"], by_id["pkg_rev"]["deliverable"]) == (True, True)
    assert (by_id["pkg_draft"]["visible_to_analysts"], by_id["pkg_draft"]["deliverable"]) == (False, True)
    assert (by_id["pkg_soon"]["visible_to_analysts"], by_id["pkg_soon"]["deliverable"]) == (True, False)


def test_auto_membership_mode_puts_every_grant_in_the_stack():
    result, _ = _call(auto_membership=True)

    assert result["membership_mode"] == "auto"
    fin = next(g for g in result["by_group"] if g["group_id"] == "g_fin")
    assert {p["id"]: p["in_stack"] for p in fin["packages"]} == {"pkg_rev": "always", "pkg_mkt": "always"}
    assert not any("opt-in" in n for n in result["notes"])


def test_classic_mode_marks_available_grants_as_subscription_dependent():
    result, _ = _call(auto_membership=False)

    assert result["membership_mode"] == "classic"
    fin = next(g for g in result["by_group"] if g["group_id"] == "g_fin")
    assert {p["id"]: p["in_stack"] for p in fin["packages"]} == {"pkg_rev": "always", "pkg_mkt": "if_subscribed"}
    assert any("subscrib" in n for n in result["notes"])


def test_include_tables_false_keeps_counts_but_drops_table_lists():
    result, _ = _call(include_tables=False)

    for pkg in result["packages"]:
        assert "tables" not in pkg
        assert isinstance(pkg["table_count"], int)
    # The unreachable tray is the point of the call; it stays.
    assert [t["id"] for t in result["unreachable"]["tables_in_no_package"]] == ["t_orphan"]


def test_unpackaged_list_is_capped_with_an_honest_total(monkeypatch):
    import app.api.mcp.foundation_tools as ft

    monkeypatch.setattr(ft, "_ACCESS_PICTURE_LIST_CAP", 1)
    registry = {
        "tables": _REGISTRY["tables"]
        + [{"id": "t_orphan2", "name": "Orphan 2", "query_mode": "local", "packaged": False}],
        "count": 8,
    }

    def dispatch(url: str, **_kwargs):
        if "/api/admin/registry" in url:
            return _mock_resp(registry)
        return _dispatch(url)

    result, _ = _call(dispatch=dispatch)

    unreachable = result["unreachable"]
    assert len(unreachable["tables_in_no_package"]) == 1
    assert unreachable["tables_in_no_package_total"] == 2
    assert unreachable["tables_in_no_package_truncated"] is True


def test_a_full_package_page_is_reported_as_truncated(monkeypatch):
    import app.api.mcp.foundation_tools as ft

    monkeypatch.setattr(ft, "_ACCESS_PICTURE_PACKAGE_LIMIT", len(_PACKAGES))
    result, mock_get = _call()

    assert result["packages_truncated"] is True
    pkg_call = next(c for c in mock_get.call_args_list if c.args[0].endswith("/api/admin/data-packages"))
    assert pkg_call.kwargs["params"] == {"include_table_ids": "true", "limit": str(len(_PACKAGES))}


def test_a_short_package_page_is_not_truncated():
    result, _ = _call()

    assert result["packages_truncated"] is False


def test_reads_the_three_admin_wide_endpoints_not_the_caller_scoped_catalog():
    _, mock_get = _call()

    urls = [c.args[0] for c in mock_get.call_args_list]
    assert any(u.endswith("/api/admin/access-overview") for u in urls)
    assert any(u.endswith("/api/admin/data-packages") for u in urls)
    assert any(u.endswith("/api/admin/registry") for u in urls)
    # The catalog narrows a stack-surface admin to their own stack — the one
    # set of tables that is never orphaned — so it must not be the inventory.
    assert not any("/api/catalog/tables" in u or "/api/v2/catalog" in u for u in urls)


def test_declares_itself_read_only():
    mod = _import_mod()
    tool = mod.mcp._tool_manager.get_tool("admin_access_picture")
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
