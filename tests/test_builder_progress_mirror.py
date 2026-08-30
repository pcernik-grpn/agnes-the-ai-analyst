"""The builder's progress line is computed twice — keep the two the same.

The panel is hand-editable, so the line above it has to answer to the FORM,
not to the last reply: `syncProgress()` had one caller (the turn handler), and
an author who typed everything by hand was still told nothing was settled.
The fix gives the page its own copy of the slot rules, which buys correctness
at the price of a duplicate.

This is the guard on that duplicate. The server's `_SLOTS` stay the source of
truth — keys, labels, and the character thresholds, which are the store's own
guardrail floors rather than round numbers — and this test fails the moment
one side moves without the other.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.api.entity_builder import _SLOTS

SKILLS = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "skills.html"


@pytest.fixture(scope="module")
def js_rules() -> dict:
    """`SLOT_RULES` out of the page, as data."""
    markup = SKILLS.read_text(encoding="utf-8")
    m = re.search(r"var SLOT_RULES = (\{.*?\n  \});", markup, re.S)
    assert m, "SLOT_RULES is gone from skills.html — the progress line no longer mirrors the server"
    # JS object literal → JSON: single quotes to double, bare keys quoted,
    # trailing commas dropped. Labels carry no apostrophes, and this test is
    # the thing that fails if one is ever added.
    raw = m.group(1)
    assert "\\'" not in raw, "a label gained an escaped apostrophe — teach this parser about it"
    raw = re.sub(r"'([^']*)'", r'"\1"', raw)
    raw = re.sub(r"(?<![\"\w])(\w+)\s*:", r'"\1":', raw)
    raw = re.sub(r",(\s*[}\]])", r"\1", raw)
    return json.loads(raw)


@pytest.mark.parametrize("entity_type", sorted(_SLOTS))
def test_every_type_is_mirrored(js_rules, entity_type):
    assert entity_type in js_rules, f"{entity_type} has server slots but no client rules"
    server = _SLOTS[entity_type]
    client = js_rules[entity_type]
    assert [s.key for s in server] == [r["key"] for r in client], (
        f"{entity_type}: slot keys or their ORDER differ — the line lists open slots in this order"
    )
    assert [s.label for s in server] == [r["label"] for r in client], (
        f"{entity_type}: the words the author reads differ between the two implementations"
    )


@pytest.mark.parametrize("entity_type", sorted(_SLOTS))
def test_the_thresholds_agree(js_rules, entity_type):
    """The predicates are lambdas, so they are checked by BEHAVIOUR: build the
    draft each client rule says is enough, and require the server to agree."""
    for rule in js_rules[entity_type]:
        slot = next(s for s in _SLOTS[entity_type] if s.key == rule["key"])
        for field, chars in rule["any"]:
            enough = {field: "x" * chars}
            assert slot.known(enough), (
                f"{entity_type}.{rule['key']}: the page counts {chars} chars of {field} as settled, "
                f"the server does not"
            )
            if chars > 1:
                one_short = {field: "x" * (chars - 1)}
                assert not slot.known(one_short), (
                    f"{entity_type}.{rule['key']}: the server settles on fewer than {chars} chars of "
                    f"{field} — the page is stricter than the interview"
                )


def test_the_line_reports_the_panel_not_the_last_reply(js_rules):
    """The regression itself: a page that only recomputed on a turn."""
    markup = SKILLS.read_text(encoding="utf-8")
    assert "convSlots" not in markup, "the server's slot answer is cached again — it goes stale on every keystroke"
    assert markup.count("syncProgress()") >= 3, "syncProgress is not called from the field-input path"


def test_the_transcript_caps_agree():
    """The page keeps as much transcript as the prompt replays.

    They were different: the prompt used the last MAX_HISTORY turns while the
    page kept and uploaded all of them, so past that mark the model silently
    stopped seeing the start of a conversation that was still on screen — read
    by the author as the assistant forgetting, not as a limit.
    """
    from app.api.builder_core import MAX_HISTORY, MAX_MESSAGE_CHARS

    markup = SKILLS.read_text(encoding="utf-8")
    for name, value in (("MAX_HISTORY", MAX_HISTORY), ("MAX_MSG_CHARS", MAX_MESSAGE_CHARS)):
        assert f"var {name} = {value};" in markup, (
            f"the page's {name} no longer matches the server's ({value})"
        )
