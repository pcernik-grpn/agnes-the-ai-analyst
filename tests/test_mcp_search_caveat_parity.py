"""Both MCP servers must warn about the same search behaviour.

`tests/test_mcp_tool_parity.py` guards that the transports expose the same
tool NAMES. Nothing guarded what those tools SAY — and the stdio server
(`cli/mcp/server.py`) hand-maintains its own docstrings rather than
importing the HTTP ones, so guidance added to
`app/api/mcp/foundation_tools.py` does not reach it.

That split is exactly backwards for this fix: `app/chat/runner.py` spawns
`agnes mcp` — the stdio server — for the in-chat agent, so the surface
where the "I don't have access to your files or collections" answer was
actually produced was the one still missing the warning. Devin Review
caught it on this PR.

A docstring is what the model reads before its first call, so this is the
preventive half of the change; the runtime `hint` is the fallback.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTTP_TOOLS = ROOT / "app" / "api" / "mcp" / "foundation_tools.py"
STDIO_TOOLS = ROOT / "cli" / "mcp" / "server.py"

#: The three retrieval behaviours that make a reasonable query miss. Each
#: entry is (label, regex) — matched case-insensitively against a tool's
#: docstring in both servers.
#: Patterns tolerate line wrapping (``\s+``) — a docstring reflows, and a
#: guard that fails on where the line broke would be noise, not signal.
CAVEATS = (
    # File names ARE searched now, as a fallback when no body matches. The
    # caveat that mattered is the one about what a name hit is worth, so the
    # guard pins the fallback wording — an agent told "not indexed" would be
    # steered away from a query that works. (#1267)
    ("file names are a fallback", r"file\s+names?\s+are\s+a\s+fallback"),
    ("whole-word matching", r"whole[\s-]+word"),
    ("no wildcard", r"wildcard"),
)

#: Wording that must NOT survive anywhere in either server's tool docs.
STALE = (
    ("filenames are not indexed", r"filename[s]?\s+are\s+not\s+indexed"),
    # `collection_file_read` pages at ~20k characters, so one call is the
    # FIRST page, not the document. A search docstring that still promises a
    # full read from that call sends the agent straight back into the bug the
    # paging fixed — for any document over one page.
    ("a full read from one collection_file_read call", r"in\s+full\s+with\s+``collection_file_read"),
)

SEARCH_TOOLS = ("collections_search", "knowledge_search")


def _docstring(source: str, func: str) -> str:
    """The triple-quoted docstring of ``def func`` / ``async def func``."""
    m = re.search(
        rf"(?:async\s+)?def\s+{re.escape(func)}\s*\(.*?\)\s*->[^:]*:\s*\"\"\"(.*?)\"\"\"",
        source,
        re.S,
    )
    assert m, f"could not find {func}'s docstring"
    return m.group(1)


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
@pytest.mark.parametrize(("label", "pattern"), CAVEATS, ids=[c[0] for c in CAVEATS])
def test_http_tool_documents_the_caveat(tool, label, pattern):
    doc = _docstring(HTTP_TOOLS.read_text(encoding="utf-8"), tool)
    assert re.search(pattern, doc, re.I), f"{tool} (HTTP) does not mention: {label}"


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
@pytest.mark.parametrize(("label", "pattern"), CAVEATS, ids=[c[0] for c in CAVEATS])
def test_stdio_tool_documents_the_caveat(tool, label, pattern):
    """The stdio server backs the in-chat agent — the incident surface."""
    doc = _docstring(STDIO_TOOLS.read_text(encoding="utf-8"), tool)
    assert re.search(pattern, doc, re.I), f"{tool} (stdio) does not mention: {label}"


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
def test_both_servers_say_an_empty_result_is_not_an_access_problem(tool):
    """The wrong inference the whole change set exists to prevent."""
    for source, which in ((HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")):
        doc = _docstring(source.read_text(encoding="utf-8"), tool).lower()
        assert "access" in doc, f"{tool} ({which}) never rules out the access reading"


def test_the_guard_reads_real_docstrings():
    """A parser that silently matches nothing would pass every test above."""
    for source in (HTTP_TOOLS, STDIO_TOOLS):
        for tool in SEARCH_TOOLS:
            assert len(_docstring(source.read_text(encoding="utf-8"), tool)) > 200


# ---------------------------------------------------------------------------
# Devin Review on this PR: the guards above read the WHOLE docstring, but only
# its first paragraph reaches the model.
# ---------------------------------------------------------------------------


def _wire_description(source: str, func: str) -> str:
    """What `progressive_tool` actually puts on the wire for this tool.

    Both servers register through `src.mcp_tooling.progressive_tool`, which
    sends only the first docstring paragraph as the tool description and
    stashes the rest behind an on-demand `tool_docs('<name>')` call. Caveats
    living further down are therefore invisible to a model reading the tool
    list — the docstring guards above passed while the preventive half of this
    change set did nothing.
    """
    from src.mcp_tooling import summarize_docstring

    summary, _has_more = summarize_docstring(_docstring(source, func))
    return summary


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
@pytest.mark.parametrize(("label", "pattern"), CAVEATS, ids=[c[0] for c in CAVEATS])
def test_the_caveat_survives_into_the_wire_description(tool, label, pattern):
    for source, which in ((HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")):
        wire = _wire_description(source.read_text(encoding="utf-8"), tool)
        assert re.search(pattern, wire, re.I), (
            f"{tool} ({which}) mentions {label} only below the first paragraph, "
            f"so a model reading the tool list never sees it"
        )


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
def test_the_wire_description_points_at_the_hint(tool):
    """The runtime half only helps if the model is told to read it."""
    for source, which in ((HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")):
        wire = _wire_description(source.read_text(encoding="utf-8"), tool).lower()
        assert "hint" in wire, f"{tool} ({which}) never tells the model to read the hint"


def test_the_wire_extractor_is_not_silently_returning_everything():
    """A summarizer that returned the full docstring would pass every test
    above while proving nothing."""
    for source in (HTTP_TOOLS, STDIO_TOOLS):
        for tool in SEARCH_TOOLS:
            src = source.read_text(encoding="utf-8")
            assert len(_wire_description(src, tool)) < len(_docstring(src, tool))


@pytest.mark.parametrize(("label", "pattern"), STALE, ids=[s[0] for s in STALE])
def test_neither_server_still_ships_the_stale_caveat(label, pattern):
    """The advice and the engine have to agree — on BOTH surfaces, since a
    fix applied to one of them is the recurring shape of this bug."""
    for path in (HTTP_TOOLS, STDIO_TOOLS):
        assert not re.search(pattern, path.read_text(encoding="utf-8"), re.I), f"{path.name} still says: {label}"


# ---------------------------------------------------------------------------
# TCRD-287: an oversized result is shortened, never refused — and both servers
# must tell the model how to read a shortened hit in full.
# ---------------------------------------------------------------------------

COMPACTION_CONTRACT = (
    ("the truncated_fields marker", r"truncated_fields"),
    ("the whole-document follow-up", r"collection_file_read"),
    ("the follow-up is paged (next_offset)", r"next_offset"),
    ("prefix, not the whole passage", r"prefix"),
)


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
@pytest.mark.parametrize(("label", "pattern"), COMPACTION_CONTRACT, ids=[c[0] for c in COMPACTION_CONTRACT])
def test_both_servers_document_the_compaction_contract(tool, label, pattern):
    for source, which in ((HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")):
        doc = _docstring(source.read_text(encoding="utf-8"), tool)
        assert re.search(pattern, doc, re.I), f"{tool} ({which}) does not mention: {label}"


@pytest.mark.parametrize("tool", SEARCH_TOOLS)
def test_both_servers_apply_the_compaction(tool):
    """Docs without the call would be the docstring-only fix this file exists
    to catch. Each server's tool body must hand its response to the shared
    helper — the same one, so a borderline result cannot be cut on one
    transport and refused on the other."""
    for source, which in ((HTTP_TOOLS, "HTTP"), (STDIO_TOOLS, "stdio")):
        src = source.read_text(encoding="utf-8")
        m = re.search(rf"(?:async\s+)?def\s+{re.escape(tool)}\s*\(.*?(?=\n\s*@tool\(|\Z)", src, re.S)
        assert m, f"{tool} ({which}) not found"
        assert "compact_search_results(" in m.group(0) and f'"{tool}"' in m.group(0), (
            f"{tool} ({which}) does not route its response through compact_search_results"
        )
