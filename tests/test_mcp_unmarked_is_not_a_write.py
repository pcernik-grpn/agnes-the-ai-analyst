"""A tool the server said nothing about must not be called a write (#2155).

The MCP builder's Tools section badged every tool that had not declared
``readOnlyHint: true`` as **writes**, with a tooltip that admitted the
substitution out loud: *"This tool can change data on the server. Unmarked
tools count as writes."* Most public MCP servers publish no ``annotations`` at
all, so the common case was Agnes asserting a write the server never mentioned
— reproduced against a public server whose three tools are ``ask_question``,
``read_wiki_contents`` and ``read_wiki_structure``, all three badged as writes.

What is pinned here is the split between the DEFAULT and the LABEL. The
default was already right and stays untouched: an unmarked tool arrives off and
registers as ``mutating``, because the server saying nothing is not the server
saying it is safe. Only the sentence changes — three labels over the tri-state
annotation the builder already carries.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js"
CSS = ROOT / "app" / "web" / "static" / "css" / "builder.css"


def _mark(tool: dict) -> dict | None:
    """Run the shipped ``toolMark`` on one tool row, rather than restate it."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = MCP.read_text(encoding="utf-8")
    fn = re.search(r"\n  function toolMark\(t\) \{.*?\n  \}\n", src, re.S)
    assert fn, "toolMark is gone — the badge is deciding for itself again"
    script = f"""
      function esc(s) {{ return String(s); }}
      {fn.group(0)}
      process.stdout.write(JSON.stringify(toolMark({json.dumps(tool)})));
    """
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_declared_read_only_tool_carries_no_badge():
    assert _mark({"name": "search", "read_only": True, "read_only_origin": "server"}) is None


def test_a_declared_write_still_says_writes():
    """The safety signal the badge was added for does not get diluted."""
    m = _mark({"name": "delete_row", "read_only": False, "read_only_origin": "server"})
    assert m["text"] == "writes"
    assert m["cls"] == "mcp-writes"
    assert "declares" in m["title"], "the tooltip must attribute the claim to the server"


def test_an_unmarked_tool_says_unmarked_not_writes():
    """The bug, in one assertion."""
    m = _mark({"name": "read_wiki_contents", "read_only": None, "read_only_origin": "server"})
    assert m["text"] == "unmarked"
    assert m["cls"] == "mcp-unmarked", "it must not borrow the write badge's warning tint"
    assert "said nothing" in m["title"]


def test_an_unmarked_tool_is_still_off_by_default():
    """Three labels, not three permission states. Silence is still not safety:
    the toggle default and the registered ``mutating`` flag are unchanged."""
    src = MCP.read_text(encoding="utf-8")
    assert "draft.enabled[t.name] = t.read_only === true;" in src, (
        "an unmarked tool arrives switched on again"
    )
    assert "mutating: tool.read_only !== true," in src, (
        "an unmarked tool now registers as non-mutating — 'unmarked' has leaked "
        "from the label into the permission"
    )


def test_the_edit_path_admits_what_the_stored_flag_cannot_say():
    """``tool_registry.mutating`` is one boolean, so reading it back cannot tell
    a declared write from an unmarked tool. That path must not claim the server
    declared anything — nor call a declared destructive tool "unmarked"."""
    m = _mark({"name": "delete_row", "read_only": False, "read_only_origin": "registered"})
    assert m["text"] == "writes", "a stored write-capable tool must keep its warning"
    assert "cannot say" in m["title"], (
        "the edit path's tooltip claims a server declaration it cannot have read"
    )


def test_no_tooltip_folds_unmarked_into_writes_any_more():
    """The old title said the quiet part out loud. No badge may ship it."""
    src = MCP.read_text(encoding="utf-8")
    fn = re.search(r"\n  function toolMark\(t\) \{.*?\n  \}\n", src, re.S).group(0)
    assert "Unmarked tools count as writes" not in fn
    for state in (
        {"read_only": False, "read_only_origin": "server"},
        {"read_only": None, "read_only_origin": "server"},
        {"read_only": False, "read_only_origin": "registered"},
    ):
        assert "count as writes" not in _mark(dict(state, name="t"))["title"]


def test_the_unmarked_badge_is_styled_neutrally():
    """Not the amber: an absence of information is not a warning, and borrowing
    the warn tint is how an unannotated tool came to look like a declared one."""
    css = CSS.read_text(encoding="utf-8")
    block = re.search(r"\.mcp-unmarked \{.*?\}", css, re.S)
    assert block, ".mcp-unmarked has no style — the badge renders unstyled"
    assert "--ds-accent-warn" not in block.group(0)
    assert "#" not in block.group(0), "raw hex — the design system is tokens only"
