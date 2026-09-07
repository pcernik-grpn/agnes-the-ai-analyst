"""The Library's "N more in <other tab>" notice reads the filters correctly.

Two defects, both in the two lines that decide whether the reader is looking
for something:

* ``searching`` was ``!!el.value`` — a bare space is truthy, so typing one
  character and deleting it left the notice offering to carry a query nobody
  made to the other tab.
* ``filtered`` read only ``#lib-filter-menu input[data-facet]:checked``. A
  ``toggle`` facet declared with a ``control`` (``LIB_FACETS``' ``stack`` →
  ``#lib-stack-toggle``) rides the bar as a button and keeps its state in
  ``aria-pressed``, never in the menu — so the notice stayed silent for the
  facet most likely to hide rows.

Run rather than grepped, for the same reason as the description-trim guard: the
broken predicates were both present and plausible in the source.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path("app/web/templates/library.html")


def _extract_function(name: str) -> str:
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


def _run(*, search: str, menu_checked: bool, stack_pressed: bool) -> dict:
    """Drive the shipped ``syncCrossTab`` and report whether it spoke.

    ``otherTabHit`` is stubbed to always find something, so the only thing
    under test is whether the function believes a search or filter is active.
    """
    fn = _extract_function("syncCrossTab")
    script = f"""
const SEARCH = {json.dumps(search)};
const MENU_CHECKED = {json.dumps(menu_checked)};
const STACK_PRESSED = {json.dumps(stack_pressed)};

const crosstab = {{
  hidden: null,
  _text: {{ textContent: "" }},
  _jump: {{}},
  querySelector: (sel) => (sel === '[data-crosstab-text]' ? crosstab._text : crosstab._jump),
}};

const document = {{
  getElementById: (id) => {{
    if (id === 'lib-crosstab') return crosstab;
    if (id === 'lib-search') return {{ value: SEARCH }};
    // Not empty, so `thisTabEmpty` is false and the notice is allowed to speak.
    if (id === 'lib-noresults') return {{ hidden: true }};
    return null;
  }},
  querySelector: (sel) => {{
    if (sel === '#lib-filter-menu input[data-facet]:checked') return MENU_CHECKED ? {{}} : null;
    if (sel === '#lib-stack-toggle[aria-pressed="true"]') return STACK_PRESSED ? {{}} : null;
    return null;
  }},
}};

function otherTabHit() {{ return {{ n: 3, label: "Capabilities", tab: "capabilities" }}; }}

{fn}

syncCrossTab();
console.log(JSON.stringify({{ hidden: crosstab.hidden, text: crosstab._text.textContent }}));
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


def test_whitespace_alone_is_not_a_search():
    """The defect: a leftover space after deleting a query used to count."""
    out = _run(search="   ", menu_checked=False, stack_pressed=False)
    assert out["hidden"] is True


def test_an_empty_box_with_no_filters_stays_quiet():
    out = _run(search="", menu_checked=False, stack_pressed=False)
    assert out["hidden"] is True


def test_a_real_query_speaks():
    """The positive control — without it the two tests above would pass just as
    well if the notice were broken outright and never appeared."""
    out = _run(search="revenue", menu_checked=False, stack_pressed=False)
    assert out["hidden"] is False
    assert out["text"] == "3 more in Capabilities"


def test_a_menu_facet_alone_speaks():
    out = _run(search="", menu_checked=True, stack_pressed=False)
    assert out["hidden"] is False


def test_the_bar_toggle_alone_speaks():
    """The second defect. `#lib-stack-toggle` is a `control` facet, so its state
    lives in `aria-pressed` and the menu query never sees it."""
    out = _run(search="", menu_checked=False, stack_pressed=True)
    assert out["hidden"] is False


def test_a_padded_query_is_still_a_query():
    """Trimming decides whether it counts; it must not discard a real one."""
    out = _run(search="  revenue  ", menu_checked=False, stack_pressed=False)
    assert out["hidden"] is False
