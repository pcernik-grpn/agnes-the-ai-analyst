"""Reading the app list must not call a tool that writes (#2154).

Nothing in the MCP protocol says "this one lists apps", so the lister has to
be guessed. The guess was one substring test — a name carrying both ``data``
and ``app`` — and the first match won. On the connector this feature was built
around, that match is ``create_python_js_data_app_git_credential`` and the real
lister, ``get_data_apps``, is fifteenth. "Read the app list", presented as a
read, therefore PUT a mutating tool into materialize mode and invoked it with
``{}``.

Two halves, tested here together because either alone leaves the hole open:

  * the client ranks candidates and refuses to nominate a write tool at all;
  * the endpoint refuses ``lister: true`` for a tool it can see is not a
    lister, so a hand-rolled request cannot do what the UI no longer does.

The regression this file most wants to prevent is the OBVIOUS fix: gating on
``readOnlyHint``. It is a tri-state, most public servers send nothing, and the
registry stores that as ``mutating: true`` — so "candidates must be declared
read-only" would make the flow impossible on precisely the servers it exists
for. ``test_an_unannotated_server_can_still_list`` pins that.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "app" / "web" / "static" / "js" / "components" / "linked_apps_panel.js"


# --------------------------------------------------------------------------
# The client's choice
# --------------------------------------------------------------------------


def _candidates(tools: list[dict]) -> list[dict]:
    """Run the SHIPPED selection under node against ``tools``."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        "global.window = {};\n"
        f"const src = require('fs').readFileSync({str(PANEL)!r}, 'utf8');\n"
        "new Function('window', src)(global.window);\n"
        f"const tools = {json.dumps(tools)};\n"
        "console.log(JSON.stringify(window.LinkedAppsPanel.listerCandidates(tools)));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


#: The shape the reporter hit, trimmed to the tools that matter and kept in
#: the alphabetical order the source detail returns them in — which is what
#: made `create_…` win under the old first-match rule.
KEBOOLA_SHAPE = [
    {
        "tool_id": "t1",
        "original_name": "create_python_js_data_app_git_credential",
        "mutating": True,
        "input_schema": {"type": "object", "required": ["configuration_id"]},
    },
    {"tool_id": "t2", "original_name": "deploy_data_app", "mutating": True},
    {"tool_id": "t3", "original_name": "modify_python_js_data_app", "mutating": True},
    {"tool_id": "t4", "original_name": "get_data_apps", "mutating": True},
]


def test_the_reported_shape_picks_the_real_lister():
    """`get_data_apps`, not the alphabetically-first write tool."""
    got = _candidates(KEBOOLA_SHAPE)
    assert got, "no candidate at all — the flow would report 'cannot list apps'"
    assert got[0]["name"] == "get_data_apps"
    assert got[0]["tool_id"] == "t4"


@pytest.mark.parametrize(
    "name",
    [
        "create_python_js_data_app_git_credential",
        "deploy_data_app",
        "modify_python_js_data_app",
        "delete_data_app",
        "update_data_app",
    ],
)
def test_a_write_shaped_name_is_never_a_candidate(name):
    """Not merely ranked last — absent. A fallback that can reach a write
    tool is the bug, so there must be nothing to fall back TO."""
    got = _candidates([{"tool_id": "x", "original_name": name, "mutating": False}])
    assert got == [], f"{name} was offered as the app lister"


def test_a_tool_that_requires_arguments_is_not_a_candidate():
    """The lister is invoked with `{}`. A schema demanding an argument cannot
    answer that call — the one hard fact here, as against a name."""
    got = _candidates(
        [
            {
                "tool_id": "x",
                "original_name": "get_data_app_detail",
                "mutating": False,
                "input_schema": {"type": "object", "required": ["app_id"]},
            }
        ]
    )
    assert got == []


def test_an_unannotated_server_can_still_list():
    """The regression the obvious fix would introduce.

    A server that publishes no annotations registers every tool as
    `mutating: True`. Requiring a declared read-only would leave this server
    with no lister at all.
    """
    got = _candidates([{"tool_id": "t4", "original_name": "get_data_apps", "mutating": True}])
    assert [c["name"] for c in got] == ["get_data_apps"]


def test_a_declared_read_only_outranks_an_unannotated_sibling():
    """`readOnlyHint` ranks a candidate up; it never removes one."""
    got = _candidates(
        [
            {"tool_id": "a", "original_name": "list_data_apps", "mutating": True},
            {"tool_id": "b", "original_name": "get_data_apps", "mutating": False},
        ]
    )
    assert [c["name"] for c in got] == ["get_data_apps", "list_data_apps"]


def test_the_probe_shape_is_understood_too():
    """The MCP builder holds un-registered tools carrying the upstream's own
    tri-state `read_only`, not a registry row's `mutating`."""
    got = _candidates(
        [
            {"name": "create_data_app", "read_only": False},
            {"name": "get_data_apps", "read_only": True},
        ]
    )
    assert [c["name"] for c in got] == ["get_data_apps"]


def test_a_tool_unrelated_to_apps_is_not_a_candidate():
    got = _candidates([{"tool_id": "x", "original_name": "get_buckets", "mutating": False}])
    assert got == []


# --------------------------------------------------------------------------
# The endpoint's own refusal
# --------------------------------------------------------------------------
#
# `MaterializeRequest.lister` trusted the caller's designation for ANY tool id.
# The UI no longer nominates a write tool, but "the client stopped sending it"
# is not a guard — the flag makes the run project rows into `data_apps`, so a
# hand-rolled request could still invoke a mutating tool and catalogue whatever
# it wrote. The endpoint applies the same two disqualifiers, and it must refuse
# BEFORE dialling: the damage is the call itself, not the projection.

pytest.importorskip("mcp", reason="mcp SDK not installed")


def _seed_lister_source(tools: list[dict], source_id: str = "src_lister") -> None:
    from src.db import get_system_db
    from src.repositories.mcp_sources import MCPSourceRepository
    from src.repositories.tool_registry import ToolRegistryRepository

    conn = get_system_db()
    MCPSourceRepository(conn).upsert(
        id=source_id,
        name="lister-probe",
        transport="http",
        url="https://upstream.example.com/mcp",
        auth_method="bearer",
        scope="shared",
    )
    reg = ToolRegistryRepository(conn)
    for t in tools:
        reg.upsert(
            tool_id=t["tool_id"],
            source_id=source_id,
            original_name=t["name"],
            exposed_name=t["name"],
            mode="materialize",
            schedule="daily 03:00",
            input_schema=t.get("input_schema"),
            mutating=t.get("mutating", True),
        )
    conn.close()


@pytest.fixture
def never_dialled(monkeypatch):
    """Fails the test if the extractor is reached at all."""
    calls = []

    async def _boom(**kwargs):
        calls.append(kwargs)
        return {"tables": [], "errors": []}

    monkeypatch.setattr("connectors.mcp.extractor.extract_source_async", _boom)
    return calls


@pytest.mark.parametrize(
    "tool_id,name,schema",
    [
        ("w1", "create_python_js_data_app_git_credential", {"required": ["configuration_id"]}),
        ("w2", "deploy_data_app", None),
        ("w3", "get_data_app_detail", {"required": ["app_id"]}),
    ],
    ids=["write-verb-and-required-args", "write-verb", "requires-arguments"],
)
def test_lister_is_refused_for_a_tool_that_cannot_be_one(seeded_app, never_dialled, tool_id, name, schema):
    _seed_lister_source([{"tool_id": tool_id, "name": name, "input_schema": schema}])
    r = seeded_app["client"].post(
        "/api/admin/mcp-sources/src_lister/materialize",
        json={"tool_id": tool_id, "lister": True},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"] == "not_a_lister_tool"
    assert never_dialled == [], "the endpoint dialled the upstream before refusing"


def test_a_real_lister_is_allowed_through(seeded_app, never_dialled):
    """The guard must not close the door it exists to keep usable — an
    unannotated `get_data_apps` is the common case, not an edge."""
    _seed_lister_source([{"tool_id": "ok1", "name": "get_data_apps", "mutating": True}])
    r = seeded_app["client"].post(
        "/api/admin/mcp-sources/src_lister/materialize",
        json={"tool_id": "ok1", "lister": True},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200, r.text
    assert len(never_dialled) == 1, "the run never happened"


def test_the_same_tool_still_materializes_without_the_lister_flag(seeded_app, never_dialled):
    """The refusal is about DESIGNATING a lister, not about running a tool.
    Per-tool "Materialize now" on a write tool is a deliberate admin act and
    is untouched."""
    _seed_lister_source([{"tool_id": "w2", "name": "deploy_data_app"}])
    r = seeded_app["client"].post(
        "/api/admin/mcp-sources/src_lister/materialize",
        json={"tool_id": "w2"},
        headers={"Authorization": f"Bearer {seeded_app['admin_token']}"},
    )
    assert r.status_code == 200, r.text
    assert len(never_dialled) == 1


# --------------------------------------------------------------------------
# How the choice is offered
# --------------------------------------------------------------------------


def _panel_html(candidates: list[dict], tool_id: str) -> str:
    """Render the panel with `candidates` and return its markup."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        "global.window = {};\n"
        f"const src = require('fs').readFileSync({str(PANEL)!r}, 'utf8');\n"
        "new Function('window', src)(global.window);\n"
        "const p = window.LinkedAppsPanel.create({ groups: () => [], onChange: () => {} });\n"
        f"p.setSource('src1', {tool_id!r}, {json.dumps(candidates)});\n"
        "console.log(JSON.stringify({html: p.html()}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["html"]


TWO = [
    {"name": "get_data_apps", "tool_id": "t4"},
    {"name": "list_data_apps_summary", "tool_id": "t5"},
]


def test_one_candidate_is_stated_not_offered():
    """Nothing to choose, so no control — but which tool reads the list is
    still worth saying, because a wrong one fails silently."""
    html = _panel_html([{"name": "get_data_apps", "tool_id": "t4"}], "t4")
    assert "get_data_apps" in html
    assert "<select" not in html, "a one-option select is a control that cannot do anything"


def test_several_candidates_get_a_select_not_a_row_of_buttons():
    """Configuration, not a view switch — so a select, which also survives
    the 40-character tool names real servers use."""
    html = _panel_html(TWO, "t4")
    assert "<select" in html
    assert "data-la-lister" in html
    assert "ag-tglbtn" not in html.split("Tool that reads")[1][:400], (
        "the lister choice is still drawn as toggle buttons"
    )


def test_the_ranked_pick_is_the_selected_value():
    html = _panel_html(TWO, "t4")
    assert '<option value="t4" selected>get_data_apps</option>' in html
    assert '<option value="t5">list_data_apps_summary</option>' in html


def test_a_tool_id_the_server_no_longer_offers_is_called_out():
    """Silence here would leave the read pointed at a tool that is gone."""
    html = _panel_html(TWO, "t_vanished")
    assert "no longer offered" in html
    assert "selected" not in html, "a vanished tool must not appear chosen"


def test_choosing_another_tool_drops_what_was_read():
    """A list read through one tool, shown under another tool's name, is the
    bug that would replace this one."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        "global.window = {};\n"
        f"const src = require('fs').readFileSync({str(PANEL)!r}, 'utf8');\n"
        "new Function('window', src)(global.window);\n"
        "const p = window.LinkedAppsPanel.create({ groups: () => [], onChange: () => {} });\n"
        f"p.setSource('src1', 't4', {json.dumps(TWO)});\n"
        "p.handleInput({ hasAttribute: (a) => a === 'data-la-lister', value: 't5' });\n"
        "console.log(JSON.stringify({state: p.state(), html: p.html()}));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["state"]["fetched"] is False
    assert res["state"]["total"] == 0
    assert '<option value="t5" selected>' in res["html"], "the new tool is not the chosen one"
