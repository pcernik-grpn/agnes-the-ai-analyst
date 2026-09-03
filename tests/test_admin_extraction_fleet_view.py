"""The extraction fleet dashboard (`/admin/extraction`, rendered by the
externalized ``admin_extraction.js``, TCRD-296 synthesis, 2026-09-03 — moved
out of the template the same way the source card's own script did a day
earlier, see ``data_sources_extraction_observability.js``).

Same pattern as `test_admin_data_sources_extraction.py`'s `_run_js`: the
script's own row/strip-rendering functions are extracted verbatim and
executed under `node` against a seeded row/jobs payload, so an assertion
here fails the moment the real markup stops saying what the test claims.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "admin" / "admin_extraction.js"

_SIGNATURES = (
    "function esc(s) {",
    "function detailMessage(body, fallback) {",
    "function fmtAgo(seconds) {",
    "function fmtRate(rate) {",
    "function fmtCost(usd) {",
    "function tokenTotals(usage) {",
    "function factsCell(facts) {",
    "function phaseCell(run) {",
    "function _extPendingFor(connId) {",
    "function actionsCell(row) {",
    "function renderRow(row) {",
    "function renderJobsStrip(jobs) {",
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


def _run_node(body: str, *, extra_state: str = "") -> dict:
    src = SCRIPT.read_text(encoding="utf-8")
    fns = "\n".join(_extract_block(src, sig) for sig in _SIGNATURES)
    script = f"""
const extPending = {{}};
const extActionMsg = {{}};
{extra_state}
{fns}

{body}
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


def _run_row_js(row: dict) -> str:
    out = _run_node(
        f"""
const document = {{ createElement: () => new FakeEl() }};
const row = {json.dumps(row)};
console.log(JSON.stringify({{ html: renderRow(row).innerHTML }}));
""",
        extra_state="""
class FakeEl {
  constructor() { this._html = ""; this._text = ""; this.className = ""; }
  set textContent(v) { this._text = v == null ? "" : String(v); }
  get textContent() { return this._text; }
  get innerHTML() {
    if (this._html) return this._html;
    return this._text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  set innerHTML(v) { this._html = v; }
}
""",
    )
    return out["html"]


_ROW = {
    "connection_id": "sp1",
    "connection_name": "Legal SharePoint",
    "run": {
        "outcome": "running",
        "stored_status": "running",
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
    "failed_items_count": 0,
    "empty_items_count": 0,
}


def test_files_cell_shows_no_age_filter_note_when_nothing_was_filtered():
    html = _run_row_js(_ROW)
    assert "filtered by age" not in html


def test_files_cell_names_how_many_were_filtered_by_age():
    """An operator scanning the fleet table must be able to tell whether
    `extraction.crawl.min_modified` is doing anything for a connection,
    without opening its source card."""
    row = json.loads(json.dumps(_ROW))
    row["run"]["filtered_by_age"] = 37
    html = _run_row_js(row)
    assert "37" in html
    assert "filtered by age" in html


def test_no_run_at_all_renders_no_age_filter_note():
    row = json.loads(json.dumps(_ROW))
    row["run"] = None
    html = _run_row_js(row)
    assert "filtered by age" not in html


# ---------------------------------------------------------------------------
# Every reprocessing action an operator needed the shell for (TCRD-296):
# "Retry failed (N)"/"Retry empty (N)" and "Re-run" on the fleet row too.
# ---------------------------------------------------------------------------


def test_a_live_run_disables_both_retry_buttons_and_omits_rerun():
    row = json.loads(json.dumps(_ROW))
    row["failed_items_count"] = 4
    row["empty_items_count"] = 1
    html = _run_row_js(row)
    assert "Retry failed (4)" in html
    assert "Retry empty (1)" in html
    assert "Re-run" not in html
    assert "extRetryFailed('sp1')\"\n      disabled" in html or "disabled" in html


def test_an_idle_connection_with_no_backlog_shows_both_retry_buttons_disabled():
    row = json.loads(json.dumps(_ROW))
    row["run"] = None
    html = _run_row_js(row)
    assert "Retry failed (0)" in html
    assert "Retry empty (0)" in html
    assert "Re-run" not in html


def test_a_backlog_with_no_live_run_enables_the_retry_buttons():
    row = json.loads(json.dumps(_ROW))
    row["run"] = {
        "outcome": "done",
        "stored_status": "done",
        "phase": "crawl",
        "files_done": 900,
        "files_seen": 900,
        "error": None,
    }
    row["failed_items_count"] = 2
    row["empty_items_count"] = 3
    html = _run_row_js(row)
    assert "Retry failed (2)" in html
    assert "Retry empty (3)" in html
    assert "Re-run" not in html  # `done` — nothing to re-run FROM


def test_a_failed_last_run_offers_rerun():
    row = json.loads(json.dumps(_ROW))
    row["run"] = {
        "outcome": "failed",
        "stored_status": "failed",
        "phase": "crawl",
        "files_done": 40,
        "error": "lease expired after max attempts",
    }
    html = _run_row_js(row)
    assert "Re-run" in html


def test_an_interrupted_last_run_also_offers_rerun():
    row = json.loads(json.dumps(_ROW))
    row["run"] = {
        "outcome": "interrupted",
        "stored_status": "interrupted",
        "phase": "crawl",
        "files_done": 40,
        "error": None,
    }
    html = _run_row_js(row)
    assert "Re-run" in html


def test_a_pending_retry_click_locks_its_own_button_with_a_progress_label():
    row = json.loads(json.dumps(_ROW))
    row["run"] = None
    row["failed_items_count"] = 4
    out = _run_node(
        f"""
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
const row = {json.dumps(row)};
_extPendingFor("sp1").failed = true;
console.log(JSON.stringify({{ html: renderRow(row).innerHTML }}));
""",
    )
    html = out["html"]
    assert "Retrying…" in html
    assert "extRetryFailed('sp1')\"\n      disabled" in html or "disabled" in html


# ---------------------------------------------------------------------------
# The queued-vs-running lane-starvation strip
# ---------------------------------------------------------------------------


def test_jobs_strip_hides_itself_when_there_are_no_kinds():
    out = _run_node(
        """
const _el = { hidden: false, innerHTML: "x" };
const document = {
  getElementById: () => _el,
  createElement: () => ({ _t: "", set textContent(v) { this._t = v == null ? "" : String(v); }, get innerHTML() { return this._t; } }),
};
renderJobsStrip({});
console.log(JSON.stringify({ hidden: _el.hidden, html: _el.innerHTML }));
"""
    )
    assert out["hidden"] is True
    assert out["html"] == ""


def test_jobs_strip_shows_queued_and_running_per_kind():
    out = _run_node(
        """
const _el = { hidden: true, innerHTML: "" };
const document = {
  getElementById: () => _el,
  createElement: () => ({ _t: "", set textContent(v) { this._t = v == null ? "" : String(v); }, get innerHTML() { return this._t; } }),
};
renderJobsStrip({
  "corpus-extraction": { queued: 3, running: 1 },
  "sharepoint-facts-extraction": { queued: 0, running: 0 },
});
console.log(JSON.stringify({ hidden: _el.hidden, html: _el.innerHTML }));
"""
    )
    assert out["hidden"] is False
    assert "corpus-extraction" in out["html"]
    assert "3 queued / 1 running" in out["html"]
    assert "sharepoint-facts-extraction" in out["html"]
    assert "0 queued / 0 running" in out["html"]


def test_jobs_strip_flags_a_starved_lane():
    """Queued with nothing running is exactly the lane-starvation signal
    this strip exists to surface without SQL."""
    out = _run_node(
        """
const _el = { hidden: true, innerHTML: "" };
const document = {
  getElementById: () => _el,
  createElement: () => ({ _t: "", set textContent(v) { this._t = v == null ? "" : String(v); }, get innerHTML() { return this._t; } }),
};
renderJobsStrip({ "corpus-extraction": { queued: 5, running: 0 } });
console.log(JSON.stringify({ html: _el.innerHTML }));
"""
    )
    assert "starved" in out["html"]


def test_jobs_strip_does_not_flag_a_healthy_lane():
    out = _run_node(
        """
const _el = { hidden: true, innerHTML: "" };
const document = {
  getElementById: () => _el,
  createElement: () => ({ _t: "", set textContent(v) { this._t = v == null ? "" : String(v); }, get innerHTML() { return this._t; } }),
};
renderJobsStrip({ "corpus-extraction": { queued: 2, running: 2 } });
console.log(JSON.stringify({ html: _el.innerHTML }));
"""
    )
    assert "starved" not in out["html"]
