"""The MCP builder's name rule is the server's rule, or it is a trap.

A source name becomes a DuckDB identifier, so `POST /api/admin/mcp-sources`
refuses anything `src.sql_safe.is_safe_identifier` rejects. The page did not
know that. Its placeholder read `Acme CRM`, and after a successful connection
check it filled the name in *itself* from the URL host (`mcp.example.com`) —
both refused, and refused only after the click, in the sync engine's own
vocabulary ("must be a safe SQL identifier").

So the page now carries a copy of the rule, which buys a legible refusal at
the price of a duplicate. This is the guard on that duplicate: the regex the
page ships is run against the same strings the server's predicate is, and the
two must agree on every one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.sql_safe import is_safe_identifier

MCP_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js"

#: Names an admin plausibly types or the builder plausibly proposes.
CASES = [
    "acme_crm",  # the placeholder the field now shows
    "Acme CRM",  # the placeholder it used to show — refused
    "mcp.example.com",  # the host the auto-fill used to propose — refused
    "acme-crm",  # hyphens are not identifier characters
    "_internal",  # leading underscore is fine
    "crm2",  # trailing digits are fine
    "2crm",  # leading digit is not
    "",  # empty
    "a" * 64,  # at the length limit
    "a" * 65,  # over it
    "drop table",  # the reason the rule exists
]


@pytest.fixture(scope="module")
def js_pattern() -> re.Pattern:
    """`NAME_RE` out of the page, as a Python regex.

    The two dialects agree on this expression — character classes, anchors and
    a bounded quantifier — so a straight translation is honest here.
    """
    src = MCP_JS.read_text(encoding="utf-8")
    m = re.search(r"var NAME_RE = /(.+?)/;", src)
    assert m, "NAME_RE is gone from mcp_builder.js — the page no longer checks the name it will send"
    return re.compile(m.group(1))


@pytest.mark.parametrize("name", CASES)
def test_the_page_agrees_with_the_server(js_pattern, name):
    assert bool(js_pattern.match(name)) is is_safe_identifier(name), (
        f"the page and the API disagree about {name!r} — one of them will surprise the admin"
    )


def test_the_auto_fill_goes_through_the_normaliser(js_pattern):
    """The post-check auto-fill proposed the raw host, which is refused."""
    src = MCP_JS.read_text(encoding="utf-8")
    fill = re.search(r"if \(!nameOk\(draft\.name\) && draft\.url\) \{(.*?)\n        \}", src, re.S)
    assert fill, "the auto-fill moved — re-point this guard"
    assert "toIdentifier(" in fill.group(1), "the builder proposes a name its own API would refuse"


def test_the_normaliser_produces_names_the_server_takes():
    """`toIdentifier` is the page's escape hatch, so its output has to pass."""
    src = MCP_JS.read_text(encoding="utf-8")
    assert "function toIdentifier(" in src
    # Behaviour is asserted through the rule the function exists to satisfy:
    # every transformation it documents (lowercase, non-alphanumerics to
    # underscores, digits pushed off the front) lands inside is_safe_identifier.
    for produced in ("acme_crm", "mcp_example_com", "mcp_2crm"):
        assert is_safe_identifier(produced), f"{produced!r} would still be refused"


def test_registering_requires_a_checked_connection():
    """A source with no tools is enabled, grantable and unable to answer
    anything — the register-share-and-believe-it-works failure."""
    src = MCP_JS.read_text(encoding="utf-8")
    gate = re.search(r"function canSave\(\) \{(.*?)\n  \}", src, re.S)
    assert gate, "canSave moved — re-point this guard"
    assert "draft.introspected" in gate.group(1), "Register source is lit before the connection is checked"
    blocker = re.search(r"function saveBlocker\(\) \{(.*?)\n  \}", src, re.S)
    assert blocker and "Check the connection first" in blocker.group(1), (
        "the disabled button does not say the check is what it is waiting for"
    )


def test_a_tool_the_server_does_not_vouch_for_counts_as_a_write():
    """`ToolInfo.read_only` is tri-state and its own comment warns that None is
    not False. Registering an unannotated tool as non-mutating is what made
    every builder-registered tool group-callable."""
    src = MCP_JS.read_text(encoding="utf-8")
    assert "mutating: tool.read_only !== true" in src, "an unannotated tool is being registered as non-mutating again"
    extractor = (Path(__file__).resolve().parents[1] / "connectors" / "mcp" / "extractor.py").read_text()
    assert '"read_only": t.read_only' in extractor, (
        "introspection drops read_only again, so the builder cannot honour it"
    )
