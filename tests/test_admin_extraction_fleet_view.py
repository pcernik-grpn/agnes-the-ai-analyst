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
    "function fmtNextRun(iso) {",
    "function fmtRate(rate) {",
    "function fmtCost(usd) {",
    "function tokenTotals(usage) {",
    "function factsCell(facts) {",
    "function phaseCell(run) {",
    "function shardBadgeHtml(connId, run) {",
    "function _shardCheckpointAgeS(checkpointAt) {",
    "function shardRowHtml(shard) {",
    "function renderShardDisclosureRow(row) {",
    "function toggleShardDisclosure(connId) {",
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


def _run_facts_cell_js(facts: dict) -> str:
    out = _run_node(
        f"""
const facts = {json.dumps(facts)};
console.log(JSON.stringify({{ html: factsCell(facts) }}));
"""
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
# factsCell — orphans_swept (TCRD-296 C.12): the pass's own single
# end-of-pass sweep count, surfaced only for a FINISHED pass and only when
# there was something to report.
# ---------------------------------------------------------------------------


def test_facts_cell_shows_orphans_swept_for_a_finished_pass():
    html = _run_facts_cell_js({"docs_done": 40, "docs_total": 40, "phase_active": False, "orphans_swept": 3})
    assert "3 orphans swept" in html


def test_facts_cell_uses_singular_for_one_orphan_swept():
    html = _run_facts_cell_js({"docs_done": 40, "docs_total": 40, "phase_active": False, "orphans_swept": 1})
    assert "1 orphan swept" in html
    assert "1 orphans swept" not in html


def test_facts_cell_omits_the_swept_note_when_nothing_was_swept():
    html = _run_facts_cell_js({"docs_done": 40, "docs_total": 40, "phase_active": False, "orphans_swept": 0})
    assert "swept" not in html


def test_facts_cell_omits_the_swept_note_when_the_field_is_unset():
    """A run report from BEFORE this field existed (or a connection that
    never reached facts) must render exactly as it did before — no
    `undefined`/`null` leaking into the cell."""
    html = _run_facts_cell_js({"docs_done": 40, "docs_total": 40, "phase_active": False})
    assert "swept" not in html


def test_facts_cell_omits_the_swept_note_while_the_pass_is_still_running():
    html = _run_facts_cell_js({"docs_done": 10, "docs_total": 40, "phase_active": True, "orphans_swept": 5})
    assert "swept" not in html


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


# ---------------------------------------------------------------------------
# Cancel run (stalled-crawl-cancel fix, TCRD-296 gap 32) — offered for a
# running/stalled row, never a finished one, and never disabled by "live"
# (force-closing a live run is the entire point).
# ---------------------------------------------------------------------------


def test_cancel_button_shown_for_a_running_run():
    row = json.loads(json.dumps(_ROW))
    row["run"]["id"] = "er_abc123"
    html = _run_row_js(row)
    assert "Cancel run" in html
    assert "extCancelRun('sp1', 'er_abc123')" in html


def test_cancel_button_shown_for_a_stalled_run():
    row = json.loads(json.dumps(_ROW))
    row["run"]["id"] = "er_stalled1"
    row["run"]["outcome"] = "stalled"
    html = _run_row_js(row)
    assert "Cancel run" in html
    assert "extCancelRun('sp1', 'er_stalled1')" in html


def test_cancel_button_hidden_for_a_done_run():
    row = json.loads(json.dumps(_ROW))
    row["run"]["id"] = "er_done1"
    row["run"]["outcome"] = "done"
    html = _run_row_js(row)
    assert "Cancel run" not in html


def test_cancel_button_hidden_when_there_is_no_run():
    row = json.loads(json.dumps(_ROW))
    row["run"] = None
    html = _run_row_js(row)
    assert "Cancel run" not in html


# ---------------------------------------------------------------------------
# Shard roll-up (2026-09-03 auto-parallel-crawl design §4.7) — the Phase
# cell's "k/K shards" badge and the disclosure row it toggles.
# ---------------------------------------------------------------------------


def test_shard_badge_absent_for_an_inline_run():
    out = _run_node(f"console.log(JSON.stringify({{ html: shardBadgeHtml('sp1', {json.dumps(_ROW['run'])}) }}));")
    assert out["html"] == ""


def test_shard_badge_absent_when_there_is_no_run_at_all():
    out = _run_node("console.log(JSON.stringify({ html: shardBadgeHtml('sp1', null) }));")
    assert out["html"] == ""


def test_shard_badge_shows_done_over_total_and_is_clickable():
    run = {"mode": "sharded", "shards_total": 8, "shards_done": 3}
    out = _run_node(f"console.log(JSON.stringify({{ html: shardBadgeHtml('sp1', {json.dumps(run)}) }}));")
    assert "3/8 shards" in out["html"]
    assert "toggleShardDisclosure('sp1')" in out["html"]


def test_shard_row_marks_an_unknown_expected_count_honestly():
    """A shard whose persisted plan could not be found (a resync since
    planned) says "≈ ?" — never a fabricated 0."""
    shard = {
        "index": 1,
        "label": "part 1/2",
        "outcome": "running",
        "files_done": 5,
        "files_seen": 5,
        "expected": None,
        "checkpoint_at": None,
        "error": None,
        "stuck": False,
    }
    out = _run_node(
        f"const document = {{ createElement: () => new FakeEl() }};\n"
        f"console.log(JSON.stringify({{ html: shardRowHtml({json.dumps(shard)}) }}));",
        extra_state=_DISCLOSURE_FAKE_EL,
    )
    assert "≈ ?" in out["html"]


def test_shard_row_shows_a_live_expected_count_and_flags_stuck():
    shard = {
        "index": 1,
        "label": "part 1/2",
        "outcome": "stalled",
        "files_done": 5,
        "files_seen": 5,
        "expected": 400,
        "checkpoint_at": None,
        "error": None,
        "stuck": True,
    }
    out = _run_node(
        f"const document = {{ createElement: () => new FakeEl() }};\n"
        f"console.log(JSON.stringify({{ html: shardRowHtml({json.dumps(shard)}) }}));",
        extra_state=_DISCLOSURE_FAKE_EL,
    )
    assert "≈ 400" in out["html"]
    assert "Stuck?" in out["html"]


def test_shard_row_shows_the_error_verbatim():
    shard = {
        "index": 2,
        "label": "remainder",
        "outcome": "failed",
        "files_done": 0,
        "files_seen": 0,
        "expected": 0,
        "checkpoint_at": None,
        "error": "CrawlError: boom",
        "stuck": False,
    }
    out = _run_node(
        f"const document = {{ createElement: () => new FakeEl() }};\n"
        f"console.log(JSON.stringify({{ html: shardRowHtml({json.dumps(shard)}) }}));",
        extra_state=_DISCLOSURE_FAKE_EL,
    )
    assert "CrawlError: boom" in out["html"]


_DISCLOSURE_FAKE_EL = """
class FakeEl {
  constructor() { this._html = ""; this._text = ""; this.className = ""; this.hidden = false; this.id = ""; }
  set textContent(v) { this._text = v == null ? "" : String(v); }
  get textContent() { return this._text; }
  get innerHTML() {
    if (this._html) return this._html;
    return this._text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  set innerHTML(v) { this._html = v; }
}
"""


def test_disclosure_row_renders_one_row_per_shard_and_starts_hidden():
    row = {
        "connection_id": "sp1",
        "run": {
            "mode": "sharded",
            "shards": [
                {
                    "index": 1,
                    "label": "part 1/2",
                    "outcome": "done",
                    "files_done": 10,
                    "files_seen": 10,
                    "expected": 10,
                    "checkpoint_at": None,
                    "error": None,
                    "stuck": False,
                },
                {
                    "index": 2,
                    "label": "remainder",
                    "outcome": "running",
                    "files_done": 2,
                    "files_seen": 2,
                    "expected": None,
                    "checkpoint_at": None,
                    "error": None,
                    "stuck": False,
                },
            ],
        },
    }
    out = _run_node(
        f"""
const document = {{ createElement: () => new FakeEl() }};
const row = {json.dumps(row)};
const tr = renderShardDisclosureRow(row);
console.log(JSON.stringify({{ hidden: tr.hidden, id: tr.id, html: tr.innerHTML }}));
""",
        extra_state=_DISCLOSURE_FAKE_EL,
    )
    assert out["hidden"] is True
    assert out["id"] == "ext-shard-disclosure-sp1"
    assert "part 1/2" in out["html"]
    assert "remainder" in out["html"]


def test_disclosure_row_is_null_for_an_inline_run():
    out = _run_node(
        """
const document = { createElement: () => new FakeEl() };
const row = { connection_id: "sp1", run: { mode: "inline" } };
const tr = renderShardDisclosureRow(row);
console.log(JSON.stringify({ isNull: tr === null }));
""",
        extra_state="class FakeEl {}",
    )
    assert out["isNull"] is True


def test_disclosure_row_is_null_when_shards_were_never_fetched():
    """A run with `mode: "sharded"` but no `shards` key (the caller never
    passed `children=`) must not crash — null, same as inline."""
    out = _run_node(
        """
const document = { createElement: () => new FakeEl() };
const row = { connection_id: "sp1", run: { mode: "sharded", shards_total: 2, shards_done: 1 } };
const tr = renderShardDisclosureRow(row);
console.log(JSON.stringify({ isNull: tr === null }));
""",
        extra_state="class FakeEl {}",
    )
    assert out["isNull"] is True
