"""The repair for tools the old lister guess left in `materialize` mode (#2251).

#2249 refuses a write-shaped lister nomination, which closes the route that
CREATES this state. It does not clear the rows already written: on an instance
where "Read the app list" was clicked before that fix, the wrongly chosen tool
still sits in ``materialize`` mode, and a source-level "Materialize now" runs
every materialize-mode tool without consulting ``read_only`` — so the #2154
call is still reachable by a different door.

Asserted on the MODE the extractor selects on, not on a log line: "the row is
still there in materialize mode" is the whole defect, and a repair that
reported success while leaving the mode would look identical in output.
"""

from __future__ import annotations

import pytest

from src.repositories.tool_registry import MATERIALIZE, PASSTHROUGH

# The tool #2154 actually invoked, and the real lister it should have picked.
_WRITE_TOOL = "create_python_js_data_app_git_credential"
_LISTER = "get_data_apps"
#: What the panel wrote alongside the mode — the fingerprint of the buggy flow.
_LISTER_SCHEDULE = "daily 03:00"


@pytest.fixture
def debris_env(e2e_env, monkeypatch):
    """One MCP source with three tools: the debris, the real lister, and a
    write tool left alone in passthrough (where registration puts it)."""
    from src.repositories import mcp_sources_repo, tool_registry_repo

    sources, tools = mcp_sources_repo(), tool_registry_repo()
    sources.upsert(id="src_1", name="Keboola MCP", transport="http", url="https://mcp.example.com/mcp")

    def add(tool_id, name, mode, schedule=None, enabled=True, required=None):
        tools.upsert(
            tool_id=tool_id,
            source_id="src_1",
            original_name=name,
            exposed_name="kbc_" + name,
            mode=mode,
            input_schema={"type": "object", "required": required} if required else {"type": "object"},
            mutating=True,
            schedule=schedule,
            enabled=enabled,
        )

    # The debris: a write tool in materialize mode, carrying the fingerprint.
    add("t_debris", _WRITE_TOOL, MATERIALIZE, schedule=_LISTER_SCHEDULE)
    # The legitimate lister, materialized on purpose. Must survive.
    add("t_lister", _LISTER, MATERIALIZE, schedule=_LISTER_SCHEDULE)
    # A write tool where registration leaves it. Must survive untouched.
    add("t_passthrough", "delete_data_app", PASSTHROUGH)
    return tools


def test_report_only_changes_nothing(debris_env):
    """The default run is a report — the issue asks for it explicitly, because
    a name is a guess and an admin may have materialized a write tool on
    purpose."""
    from scripts.repair_mcp_materialize_debris import repair

    assert repair(apply=False) == 1
    assert debris_env.get("t_debris")["mode"] == MATERIALIZE


def test_apply_reverts_only_the_debris(debris_env):
    from scripts.repair_mcp_materialize_debris import repair

    assert repair(apply=True) == 1
    assert debris_env.get("t_debris")["mode"] == PASSTHROUGH
    # The read-shaped lister keeps the mode the feature needs.
    assert debris_env.get("t_lister")["mode"] == MATERIALIZE
    assert debris_env.get("t_lister")["schedule"] == _LISTER_SCHEDULE
    # An untouched passthrough row stays exactly as it was.
    assert debris_env.get("t_passthrough")["mode"] == PASSTHROUGH


def test_the_schedule_goes_with_the_mode(debris_env):
    """A schedule only ever meant something to a materialize row; leaving it on
    a reverted one would keep the fingerprint of a bug that is gone."""
    from scripts.repair_mcp_materialize_debris import repair

    repair(apply=True)
    assert debris_env.get("t_debris")["schedule"] is None


def test_the_rest_of_the_row_survives_the_revert(debris_env):
    """Reverting re-upserts the whole row, so every field it restates is a
    field it could silently drop."""
    from scripts.repair_mcp_materialize_debris import repair

    before = debris_env.get("t_debris")
    repair(apply=True)
    after = debris_env.get("t_debris")
    for field in ("source_id", "original_name", "exposed_name", "input_schema", "mutating", "enabled"):
        assert after[field] == before[field], field


def test_a_disabled_debris_row_is_repaired_too(e2e_env):
    """Disabled is not safe: the row still carries the mode, and re-enabling it
    is one click away from invoking the tool."""
    from scripts.repair_mcp_materialize_debris import repair
    from src.repositories import mcp_sources_repo, tool_registry_repo

    sources, tools = mcp_sources_repo(), tool_registry_repo()
    sources.upsert(id="src_2", name="Other", transport="http", url="https://mcp.example.com/mcp")
    tools.upsert(
        tool_id="t_off",
        source_id="src_2",
        original_name=_WRITE_TOOL,
        exposed_name="kbc_off",
        mode=MATERIALIZE,
        schedule=_LISTER_SCHEDULE,
        enabled=False,
    )
    assert repair(apply=True) == 1
    row = tools.get("t_off")
    assert row["mode"] == PASSTHROUGH
    # Repaired, not re-enabled — the admin's own switch is not ours to flip.
    assert not row["enabled"]


def test_a_clean_instance_reports_nothing_and_is_idempotent(debris_env):
    from scripts.repair_mcp_materialize_debris import repair

    assert repair(apply=True) == 1
    assert repair(apply=True) == 0
