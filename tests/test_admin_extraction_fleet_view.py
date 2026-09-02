"""The extraction fleet dashboard (`/admin/extraction`, `admin_extraction.html`).

Same pattern as `test_admin_data_sources_extraction.py`'s `_run_js`: the
template's own row-rendering functions are extracted verbatim and executed
under `node` against a seeded row payload, so an assertion here fails the
moment the real markup stops saying what the test claims.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_extraction.html"

_SIGNATURES = (
    "function esc(s) {",
    "function fmtAgo(seconds) {",
    "function fmtRate(rate) {",
    "function fmtCost(usd) {",
    "function tokenTotals(usage) {",
    "function factsCell(facts) {",
    "function phaseCell(run) {",
    "function renderRow(row) {",
)


def _extract_block(text: str, opener: str) -> str:
    """The brace-balanced body of one declaration, from its signature."""
    start = text.index(opener)
    depth = 0
    started = False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces extracting {opener!r}")


def _run_js(row: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    fns = "\n".join(_extract_block(tpl, sig) for sig in _SIGNATURES)
    script = f"""
class FakeEl {{
  constructor() {{ this._html = ""; this._text = ""; this.className = ""; }}
  set textContent(v) {{ this._text = v == null ? "" : String(v); }}
  get textContent() {{ return this._text; }}
  get innerHTML() {{
    if (this._html) return this._html;
    return this._text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }}
  set innerHTML(v) {{ this._html = v; }}
}}
const document = {{ createElement: () => new FakeEl() }};

{fns}

const row = {json.dumps(row)};
console.log(JSON.stringify({{ html: renderRow(row).innerHTML }}));
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
    return json.loads(proc.stdout)["html"]


_ROW = {
    "connection_id": "sp1",
    "connection_name": "Legal SharePoint",
    "run": {
        "outcome": "running",
        "phase": "crawl",
        "files_done": 812,
        "files_seen": 900,
        "error": None,
    },
    "files_per_min": 12.5,
    "checkpoint_age_s": 10.0,
    "stuck": False,
    "facts": {"docs_done": None, "docs_total": None, "phase_active": False, "usage": {}},
    "estimated_cost_usd": 0.01,
}


def test_files_cell_shows_no_age_filter_note_when_nothing_was_filtered():
    html = _run_js(_ROW)
    assert "filtered by age" not in html


def test_files_cell_names_how_many_were_filtered_by_age():
    """An operator scanning the fleet table must be able to tell whether
    `extraction.crawl.min_modified` is doing anything for a connection,
    without opening its source card."""
    row = json.loads(json.dumps(_ROW))
    row["run"]["filtered_by_age"] = 37
    html = _run_js(row)
    assert "37" in html
    assert "filtered by age" in html


def test_no_run_at_all_renders_no_age_filter_note():
    row = json.loads(json.dumps(_ROW))
    row["run"] = None
    html = _run_js(row)
    assert "filtered by age" not in html
