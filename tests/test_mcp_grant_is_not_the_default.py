"""Connecting a tool server must not hand out write access by default.

The audit's single most likely unintended outcome was four clicks long: check
the connection, name it, add the only realistic group, press the primary. Every
introspected tool arrived switched ON — including the ones that can change data
upstream — the grant is source-wide, and the primary said only "Register
source". So the fast path gave every user's agents every destructive tool the
server offered, and nothing on screen restated that before or after.

Two changes, pinned here. Read-only tools arrive on and writes arrive off, so
the section's own instruction ("turn off anything agents should not call")
stops describing a review nobody performs. And the primary names what it is
about to do, from the same two selections it commits.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "web" / "static" / "js" / "components"
MCP = JS / "mcp_builder.js"
LISTING = ROOT / "app" / "web" / "templates" / "admin_mcp_sources.html"


def test_a_write_capable_tool_arrives_off():
    src = MCP.read_text(encoding="utf-8")
    assert "draft.enabled[t.name] = t.read_only === true;" in src, (
        "every discovered tool is switched on again, writes included"
    )


def test_an_unannotated_tool_counts_as_a_write_here_too():
    """`read_only` is tri-state and its own comment warns that absent is not
    False. The same rule that registers an unannotated tool as mutating has to
    govern whether it arrives switched on."""
    src = MCP.read_text(encoding="utf-8")
    assert "read_only: (t && typeof t.read_only === 'boolean') ? t.read_only : null," in src
    # `=== true` is the whole point: null must not pass.
    assert "t.read_only === true" in src


def test_the_section_no_longer_describes_a_review_nobody_performs():
    src = MCP.read_text(encoding="utf-8")
    assert "Read-only tools are on; anything " in src and "is off until you turn it on" in src, (
        "the Tools section still tells the admin to turn things off that are already off"
    )


def test_the_primary_names_what_registering_will_grant():
    src = MCP.read_text(encoding="utf-8")
    body = re.search(r"function registerLabel\(\) \{(.*?)\n  \}", src, re.S)
    assert body, "registerLabel is gone — the primary says only 'Register source' again"
    b = body.group(1)
    assert "enabledTools()" in b and "draft.groups" in b, (
        "the label is computed from something other than what the button commits"
    )
    assert "can write" in b


@pytest.mark.parametrize(
    "tools,groups,expected",
    [
        # Nothing granted yet: the plain verb, because nothing else is true.
        ([("search", True)], [], "Register source"),
        ([("search", True)], ["Analysts"], "Register and give Analysts 1 tool"),
        (
            [("search", True), ("write_row", False)],
            ["Everyone"],
            "Register and give Everyone 2 tools (1 can write)",
        ),
        # Unannotated counts as a write in the label too.
        ([("search", True), ("sync", None)], ["Everyone"], "Register and give Everyone 2 tools (1 can write)"),
        ([("a", True), ("b", True)], ["X", "Y"], "Register and give 2 groups 2 tools"),
    ],
)
def test_the_label_reads_correctly(tools, groups, expected):
    """Run the shipped function, rather than restating its arithmetic here."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = MCP.read_text(encoding="utf-8")
    body = re.search(r"function registerLabel\(\) \{.*?\n  \}", src, re.S).group(0)
    script = f"""
      var draft = {{ groups: {json.dumps([{"id": g, "name": g} for g in groups])} }};
      var TOOLS = {json.dumps([{"name": n, "read_only": r} for n, r in tools])};
      function enabledTools() {{ return TOOLS; }}
      {body}
      process.stdout.write(registerLabel());
    """
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == expected


def test_the_listing_page_has_one_creation_path():
    """The modal on this page posted whatever was typed — no connection check,
    no tools — and it was what the admin nav led to, while the builder that
    refuses to register an unreachable server was linked from nowhere here."""
    src = LISTING.read_text(encoding="utf-8")
    assert 'href=\'/admin/mcp-sources/new\'' in src or 'href="/admin/mcp-sources/new"' in src
    for dead in ("create-modal", "open-create-btn", "confirm-create-btn", "new-transport"):
        assert dead not in src.replace("the three create-modal", ""), f"{dead} is back on the listing page"
