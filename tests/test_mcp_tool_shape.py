"""The write-verb list has three readers and no two of them may drift.

``src/mcp_tool_shape.py`` is the definition; ``app/api/admin_mcp.py`` refuses a
lister nomination with it (#2154), ``scripts/repair_mcp_materialize_debris.py``
finds the rows the old heuristic wrote with it (#2251), and
``linked_apps_panel.js`` ranks candidates with its own copy because it cannot
import Python. A client that nominated what the server then rejected is exactly
the drift these tests exist to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.mcp_tool_shape import WRITE_VERB, is_write_shaped, required_arguments, tool_name

# Anchored to THIS file, never to the cwd: a worktree-based session runs
# pytest from a sibling checkout often enough that a relative path here
# reads a different branch's copy of the panel and fails on nothing.
_REPO = Path(__file__).resolve().parents[1]
_PANEL = _REPO / "app/web/static/js/components/linked_apps_panel.js"


def _js_write_verbs() -> list[str]:
    """The verbs `WRITE_VERB` in the panel matches, in source order."""
    src = _PANEL.read_text()
    m = re.search(r"var WRITE_VERB = /\^\((?P<body>[^)]+)\)\(_\|\$\)/;", src)
    assert m, "WRITE_VERB not found in linked_apps_panel.js — did it move or change shape?"
    return m.group("body").split("|")


def _py_write_verbs() -> list[str]:
    m = re.search(r"\^\((?P<body>.+)\)\(_\|\$\)$", WRITE_VERB.pattern, re.S)
    assert m, "WRITE_VERB pattern no longer has the shape this guard reads"
    return m.group("body").replace("\n", "").split("|")


def test_python_and_js_write_verbs_are_identical():
    assert _py_write_verbs() == _js_write_verbs()


def test_the_reported_tool_is_write_shaped():
    # The tool #2154 actually invoked on a live instance.
    assert is_write_shaped("create_python_js_data_app_git_credential")


def test_read_shaped_names_are_not_write_shaped():
    for name in (
        "get_data_apps",
        "list_data_apps",
        "read_wiki_contents",
        "search_data_app_logs",
        # A write verb has to lead AND be a whole segment: `created_at_report`
        # starts with the letters of `create` and is not the verb.
        "created_at_report",
        "settings_for_data_app",
    ):
        assert not is_write_shaped(name), name


def test_write_shape_is_case_insensitive_and_null_safe():
    assert is_write_shaped("DELETE_data_app")
    assert not is_write_shaped("")
    assert not is_write_shaped(None)  # type: ignore[arg-type]


def test_required_arguments_tolerates_upstream_junk():
    assert required_arguments({"required": ["repo", "page"]}) == ["repo", "page"]
    assert required_arguments({"required": []}) == []
    assert required_arguments({}) == []
    assert required_arguments(None) == []
    # A schema that is not a dict, and a `required` that is not a list, both
    # arrive through a registry column holding upstream JSON.
    assert required_arguments("not a schema") == []  # type: ignore[arg-type]
    assert required_arguments({"required": "repo"}) == []


def test_tool_name_prefers_the_upstream_name():
    assert tool_name({"original_name": "get_apps", "exposed_name": "kbc_get_apps"}) == "get_apps"
    assert tool_name({"exposed_name": "kbc_get_apps"}) == "kbc_get_apps"
    assert tool_name(None) == ""
    assert tool_name({}) == ""
