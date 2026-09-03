"""The overview says how many of an MCP source's tools each group may use.

Audit finding F6. An ``mcp_source`` grant is necessary but not sufficient: it
decides whether the group sees the server at all, and is ANDed with the
per-tool ``tool_grants`` maintained on the source's own page. A row that
rendered only the source grant implied completeness it could not deliver —
the admin finished the visible step and the tool stayed dark, with the other
half of the answer on a page this one never mentioned.

``/api/admin/access-overview`` now carries ``mcp_tool_grants``:
``{source_id: {"total": M, "by_group": {group_id: N}}}``, built once from the
tool registry. Both repository calls it makes exist on both app-state
backends.
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _overview(seeded_app):
    r = seeded_app["client"].get("/api/admin/access-overview", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 200
    return r.json()


def test_the_key_is_always_present_and_a_mapping(seeded_app):
    """Empty on an instance with no MCP sources — never absent, so the page
    can read it without a guard for the seed case."""
    ov = _overview(seeded_app)
    assert "mcp_tool_grants" in ov
    assert isinstance(ov["mcp_tool_grants"], dict)


def test_every_source_on_the_page_has_an_entry_shaped_for_the_row(seeded_app):
    """If a source is offered as a grantable item, the row must be able to
    say "N of M tools granted" for it — so its entry exists and has the two
    fields the row reads. A source with no tools reports ``total: 0`` so the
    row says so, rather than printing a fraction of nothing."""
    ov = _overview(seeded_app)
    sources = [
        it["resource_id"]
        for r in ov["resources"]
        if r["type_key"] == "mcp_source"
        for b in r["blocks"]
        for it in b["items"]
    ]
    for sid in sources:
        entry = ov["mcp_tool_grants"].get(sid)
        assert entry is not None, f"source {sid} is grantable but has no tool-count entry"
        assert isinstance(entry["total"], int) and entry["total"] >= 0
        assert isinstance(entry["by_group"], dict)
        assert all(isinstance(n, int) and 0 <= n <= entry["total"] for n in entry["by_group"].values())


def test_registered_tools_are_counted_per_source_and_per_group(seeded_app, tmp_path, monkeypatch):
    """Register a source with two tools, grant one to Admin, and read it back.

    Exercises the actual arithmetic rather than the empty seed: total counts
    every tool of the source, by_group counts only the granted ones, and a
    group with no tool grants is simply absent from ``by_group`` — which the
    row reads as 0.
    """
    from src.repositories import mcp_sources_repo, tool_registry_repo

    ov0 = _overview(seeded_app)
    admin_gid = next(g["id"] for g in ov0["groups"] if g["name"] == "Admin")

    sid = "f6-test-server"
    mcp_sources_repo().upsert(id=sid, name="F6 test server", transport="http", url="http://mcp.test/f6")
    tools = tool_registry_repo()
    tools.upsert(tool_id="f6_a", source_id=sid, original_name="a", exposed_name="f6_a", mode="passthrough")
    tools.upsert(tool_id="f6_b", source_id=sid, original_name="b", exposed_name="f6_b", mode="passthrough")
    tools.add_grant("f6_a", admin_gid)

    entry = _overview(seeded_app)["mcp_tool_grants"][sid]
    assert entry["total"] == 2
    assert entry["by_group"] == {admin_gid: 1}
