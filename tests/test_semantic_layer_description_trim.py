"""``trimRedundantDescriptions`` actually reaches the rows it is written for.

The bug this guards: the function selected ``.sl-item[data-cat]``, and no
``.sl-item`` has ever carried ``data-cat`` — that attribute name belongs to the
filter menu's ``.fbar-cat`` categories. So it matched zero rows, did nothing,
and every metric printed its description twice, once on the row and again in
the panel below it.

A source assertion could not have caught that, and this is the point of running
the function instead of reading it: the broken selector was *present in the
source*, spelled plausibly, and any test grepping for ``querySelectorAll`` or
for the function's name passed happily over it. So the shim below answers only
to the selector that matches the shipped markup — feed it the old one and it
sees no rows, and these tests fail.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path("app/web/templates/semantic_layer_list.html")

#: The selector the template's own rows carry (`data-tab="all_metrics"` /
#: `"all_glossary"`). The shim recognises this and nothing else.
LIVE_SELECTOR = ".sl-item[data-tab]"


def _extract_function(name: str) -> str:
    """The named function's source, brace-balanced, from the template."""
    src = TEMPLATE.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    depth = 0
    started = False
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
            started = True
        elif src[i] == "}":
            depth -= 1
            if started and depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run(items: list[dict]) -> list[dict]:
    """Drive the shipped function over ``items``, return each panel's state.

    Each item is ``{row, panel, clipped}``; ``panel: None`` means the row has
    no detail panel, which is how the glossary rows render.
    """
    fn = _extract_function("trimRedundantDescriptions")
    script = f"""
const SPEC = {json.dumps(items)};
const LIVE = {json.dumps(LIVE_SELECTOR)};

class Desc {{
  constructor(text, clipped) {{
    this.textContent = text;
    // A clipped row is taller than its box; an unclipped one fits exactly.
    this.scrollHeight = clipped ? 40 : 20;
    this.clientHeight = 20;
  }}
}}

class Item {{
  constructor(spec) {{
    this._row = spec.row === null ? null : new Desc(spec.row, spec.clipped);
    this._panel = spec.panel === null ? null : new Desc(spec.panel, false);
    if (this._panel) this._panel.hidden = null;   // untouched sentinel
  }}
  querySelector(sel) {{
    if (sel === '.sl-row__desc') return this._row;
    if (sel === '.sl-detail__desc') return this._panel;
    return null;
  }}
}}

const ITEMS = SPEC.map((s) => new Item(s));
const document = {{
  // Answers ONLY the selector the shipped markup carries. The old
  // `[data-cat]` spelling gets an empty list, which is what it got in a real
  // browser too.
  querySelectorAll: (sel) => (sel === LIVE ? ITEMS : []),
}};

{fn}

trimRedundantDescriptions();
console.log(JSON.stringify(ITEMS.map((i) => ({{
  hidden: i._panel ? i._panel.hidden : "no-panel",
}}))));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        proc = subprocess.run(["node", path], capture_output=True, text=True)
    finally:
        Path(path).unlink(missing_ok=True)
    if proc.returncode == 127:
        pytest.skip("node unavailable")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return json.loads(proc.stdout)


def test_a_panel_repeating_a_fully_visible_row_is_hidden():
    """The case that was broken: nothing to gain by opening the panel, so it
    goes away instead of printing the same sentence twice."""
    out = _run([{"row": "Revenue net of refunds.", "panel": "Revenue net of refunds.", "clipped": False}])
    assert out[0]["hidden"] is True


def test_a_panel_repeating_a_CLIPPED_row_survives():
    """Opening it is the only way to read the rest, so it stays — this is the
    half that keeps the fix from degrading into 'always hide'."""
    out = _run([{"row": "Revenue net of refunds.", "panel": "Revenue net of refunds.", "clipped": True}])
    assert out[0]["hidden"] is False


def test_a_panel_that_says_more_is_left_alone():
    out = _run([{"row": "Revenue.", "panel": "Revenue, net of refunds and credits.", "clipped": False}])
    assert out[0]["hidden"] is None, "a differing panel must not be touched at all"


def test_whitespace_differences_do_not_count_as_saying_more():
    """`norm()` earns its place: the row and the panel are rendered by
    different paths, so the same sentence arrives differently wrapped."""
    out = _run([{"row": "Revenue net\n  of refunds.", "panel": "Revenue net of refunds. ", "clipped": False}])
    assert out[0]["hidden"] is True


def test_a_row_with_no_panel_is_skipped():
    """How the glossary rows render — no detail panel to weigh."""
    out = _run([{"row": "A term.", "panel": None, "clipped": False}])
    assert out[0]["hidden"] == "no-panel"


def test_the_selector_matches_the_markup_the_template_renders():
    """Belt and braces on the actual defect: the attribute the function selects
    on must be one the rows carry. `data-cat` appears in this template only on
    `.fbar-cat` filter categories, never on an `.sl-item`."""
    src = TEMPLATE.read_text(encoding="utf-8")
    fn = _extract_function("trimRedundantDescriptions")
    assert LIVE_SELECTOR in fn
    assert ".sl-item[data-cat]" not in fn
    # And the markup really does carry it, so the pairing is checked from both
    # ends rather than agreeing with itself.
    assert '<div class="sl-item" data-tab="all_metrics"' in src
