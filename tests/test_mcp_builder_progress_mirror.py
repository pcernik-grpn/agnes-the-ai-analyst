"""The MCP builder's progress line is computed twice — keep the two the same.

Same guard as `test_builder_progress_mirror.py` does for /skills, for the same
reason: the panel is hand-editable, so the line above it has to answer to the
FORM. `convSlots` only ever arrived on a turn, and the input handler
deliberately skips repainting to protect the caret — so an admin who filled
the whole form by hand was still told nothing was settled, beside a lit
Register source.

The fix gives the page its own copy of the slot rules, which buys correctness
at the price of a duplicate. This is the guard on that duplicate: the server's
`_SLOTS` stay the source of truth, and this fails the moment one side moves
without the other. The predicates are lambdas, so they are checked by
BEHAVIOUR — the drafts the page counts as settled are run through the server's
own `known()`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.api.mcp_builder import _SLOTS

MCP_JS = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "components" / "mcp_builder.js"


@pytest.fixture(scope="module")
def js() -> str:
    return MCP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def client_slots(js: str) -> list[dict]:
    """The `localSlots()` table out of the page, as data."""
    block = re.search(r"function localSlots\(\) \{(.*?)\n  \}", js, re.S)
    assert block, "localSlots() is gone — the progress line no longer mirrors the server"
    rows = re.findall(r"\{ key: '(\w+)', label: '([^']+)', known: ([^}]+) \}", block.group(1))
    assert rows, "could not read the slot rows out of localSlots()"
    return [{"key": k, "label": lab, "known_expr": expr.strip()} for k, lab, expr in rows]


def test_the_same_slots_in_the_same_order(client_slots):
    """Order matters: the line names the first open slot as "next"."""
    assert [s.key for s in _SLOTS] == [r["key"] for r in client_slots]
    assert [s.label for s in _SLOTS] == [r["label"] for r in client_slots]


def test_the_endpoint_slot_follows_the_transport(client_slots):
    """`_endpoint_known` reads command for stdio and url otherwise — a page
    that checked only one of them would settle the slot on the wrong field."""
    slot = next(s for s in _SLOTS if s.key == "endpoint")
    assert slot.known({"transport": "stdio", "command": "npx server"})
    assert not slot.known({"transport": "stdio", "url": "https://example.com/mcp"})
    assert slot.known({"transport": "http", "url": "https://example.com/mcp"})
    assert not slot.known({"transport": "http", "command": "npx server"})
    block = re.search(r"function localSlots\(\) \{(.*?)\n  \}", MCP_JS.read_text(encoding="utf-8"), re.S)
    assert "transport" in block.group(1), "the page ignores the transport when reading the address"


def test_auth_is_settled_only_by_a_decision(client_slots):
    """ "Decided: none" is a real answer, so the draft carries `auth_decided`
    rather than inferring "no auth" from a blank field — inferring it settles
    the slot before the admin was ever asked."""
    slot = next(s for s in _SLOTS if s.key == "auth")
    assert not slot.known({}), "a fresh draft must not count auth as settled"
    assert slot.known({"auth_decided": True, "auth_method": ""})
    assert not slot.known({"auth_decided": True, "auth_method": "bearer"})
    assert slot.known({"auth_decided": True, "auth_method": "bearer", "auth_secret_env": "ACME_TOKEN"})
    block = re.search(r"function localSlots\(\) \{(.*?)\n  \}", MCP_JS.read_text(encoding="utf-8"), re.S)
    assert "auth_decided" in block.group(1), (
        "the page infers auth from a blank field instead of a decision"
    )


def test_the_line_reports_the_panel_not_the_last_reply(js):
    """The regression itself: a page that only recomputed on a turn."""
    assert "convSlots = body.slots" not in js, "the server's slot answer is cached again — it goes stale on a keystroke"
    assert "syncProgress()" in js, "nothing repaints the progress line"
    handler = re.search(r"data-mcp-field'\);\n      if \(f\) \{(.*?)\n        return;", js, re.S)
    assert handler, "the field-input branch moved — re-point this guard"
    assert "syncProgress()" in handler.group(1), "typing does not move the progress line"


def test_a_persisted_draft_is_discarded_when_it_commits(js):
    """A builder that keeps a draft owns clearing it.

    `discardDraft()` shipped with zero call sites: `save()` navigated away
    leaving the localStorage draft intact, so the next "+ Add → Connect an
    MCP source" resumed a source that had already been registered — endpoint
    and tool curation prefilled — and Save then attempted a duplicate
    registration. The draft has to go on the same tick the row is created,
    before the redirect.
    """
    assert js.count("discardDraft()") >= 1, "discardDraft() is defined and never called again"
    save = re.search(r"function save\(\) \{(.*?)\n  \}", js, re.S)
    assert save, "save() moved — re-point this guard"
    body = save.group(1)
    assert "discardDraft()" in body, "save() does not clear the draft it committed"
    assert body.index("discardDraft()") < body.index("window.location.href"), (
        "the draft is cleared after the redirect, which never runs"
    )
