"""The SharePoint source card's extraction block (2026-08-31 design §4/§6).

The card's own markup contributes three EMPTY anchors; everything visible is
rendered by the `extraction-observability` script. So the page test asserts
the anchors exist, and the rendering tests execute the real functions under
`node` against a seeded status payload — the same pattern
`TestSharePointSourceCardRendering` uses for the card's other halves.

Every assertion here is a rule, not a pixel: absolute counters and no
fraction/bar/ETA, a stalled run drawn without a live pulse, stale numbers
kept and marked rather than blanked, a typed 501 that explains itself, and
no Stop control while the crawl has no cancel flag.
"""

from __future__ import annotations

from tests import _ds_page_source

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = _ds_page_source.TEMPLATE


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


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


_SIGNATURES = (
    "function _esc(s) {",
    "function _extS(id) {",
    "function _extEsc(v) {",
    "function _extTime(iso) {",
    "function _extDuration(seconds) {",
    "function _extNum(n) {",
    "function _extTruncMiddle(path, max) {",
    "const EXT_DOT = {",
    "function _extDot(outcome) {",
    "function _extIsFactsPhase(run) {",
    "function _extPhaseCountText(run) {",
    "function _extRenderCrawlCell(connId, status) {",
    "function _extRunLine(run) {",
    "const EXT_STOP_REASON_TEXT = {",
    "function _extStopReasonText(reason) {",
    "function _extThrottleLine(run) {",
    "function _extActivityHtml(activity) {",
    "function _extErrorsSummaryHtml(connId, runId, errorCount) {",
    "async function _extLoadErrorDetail(details) {",
    "function _extErrorItemsHtml(runDetail) {",
    "function _extRunRowHtml(connId, st) {",
    "function _extConfigRowHtml(connId) {",
    "function _extPanelHtml(tone, title, body, connId, retry) {",
    "function _extRenderInAgnesButton(connId, status) {",
    "function _extFactsJobLine(job) {",
    "function _extRenderFactsButton(connId, status) {",
    "function _extRender(connId) {",
    "function _extRunsHtml(connId, body) {",
    "const EXT_ORIGIN_LABEL = {",
    "function _extConfigHtml(body) {",
)


def _run_js(body: str, *, state: dict | None = None) -> dict:
    tpl = _ds_page_source.page_source()
    fns = "\n".join(_extract_block(tpl, sig) for sig in _SIGNATURES)
    script = f"""
const EXT_MAX_FAILURES = 3;
{fns}

const _extState = {json.dumps(state or {})};
const _elements = {{
  "ext-block-sp1": {{ hidden: true, innerHTML: "" }},
  "ext-crawl-live-sp1": {{ hidden: true, innerHTML: "" }},
  "ext-inagnes-btn-sp1": {{ dataset: {{ extractionReady: "1" }}, disabled: false, title: "" }},
  "ext-facts-btn-sp1": {{ dataset: {{ factsReady: "1" }}, disabled: false, title: "" }},
}};
const document = {{ getElementById: (id) => _elements[id] }};

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


_RUNNING = {
    "connection_id": "sp1",
    "running": {
        "id": "er_1",
        "outcome": "running",
        "stored_status": "running",
        "stale_s": 4.0,
        "liveness_note": None,
        "started_at": "2026-08-31T14:02:00+00:00",
        "checkpoint_at": "2026-08-31T14:08:41+00:00",
        "elapsed_s": 391.0,
        "files_done": 812,
        "files_seen": 812,
        "new": 800,
        "changed": 12,
        "unchanged": 0,
        "http_429": 4,
        "throttle_wait_s": 38.0,
        "usage": {},
    },
    "last_completed": None,
    "runs_total": 8,
    "can_stop": False,
    "as_of": "2026-08-31T14:08:44+00:00",
}


def _state(**over):
    st = {"failures": 0, "stopped": False, "data": None, "lastOk": None, "error": None}
    st.update(over)
    return {"sp1": st}


class TestCardAnchors:
    """The card contributes empty anchors and nothing else — so the block can
    be rendered entirely by its own script with no load-order coupling."""

    def test_template_carries_the_three_anchors(self):
        tpl = _ds_page_source.page_source()
        assert 'id="ext-crawl-live-${row.id}"' in tpl
        assert 'id="ext-block-${row.id}" data-ext-conn="${row.id}"' in tpl
        assert 'id="ext-drawer-${row.id}"' in tpl

    def test_page_renders_for_an_admin(self, seeded_app):
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        try:
            resp = c.get("/admin/data-sources", headers={"Accept": "text/html"})
        finally:
            c.cookies.clear()
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "ext-block-" in body
        assert "toggleExtractionDrawer" in body
        # The poll cadence the design fixes (3 s active / 30 s idle) is in the
        # page, not invented per render.
        assert "EXT_POLL_ACTIVE_MS = 3000" in body
        assert "EXT_POLL_IDLE_MS = 30000" in body


class TestRunRow:
    def test_a_live_run_shows_absolute_counters_and_no_fraction(self):
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING),
        )
        html = out["html"]
        assert "812 files processed" in html
        assert "800 new" in html
        # No fraction, no percentage, no ETA — anywhere.
        assert "812/" not in html
        assert "%" not in html
        assert "left" not in html.lower().replace("<", " ")

    def test_a_live_run_names_the_moment_its_numbers_were_true(self):
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING),
        )
        assert "as of" in out["html"]

    def test_throttling_states_both_the_count_and_the_wait(self):
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING),
        )
        html = out["html"]
        assert "4× HTTP 429" in html
        assert "38s waited" in html

    def test_no_stop_button_is_drawn_and_the_absence_is_explained(self):
        """v1 has no cooperative cancel flag; a button without a mechanism
        would be a lie, so the row says so in words instead."""
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING),
        )
        html = out["html"]
        assert "Stop after this file" not in html
        assert "cannot be stopped from here yet" in html

    def test_a_stalled_run_is_not_drawn_as_a_live_pulse(self):
        stalled = json.loads(json.dumps(_RUNNING))
        stalled["running"]["outcome"] = "stalled"
        stalled["running"]["stale_s"] = 4200.0
        stalled["running"]["liveness_note"] = "no checkpoint for 4200s"
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=stalled),
        )
        html = out["html"]
        assert "ext-dot--live" not in html
        assert "ext-dot--warn" in html
        assert "no longer reporting" in html
        assert "no checkpoint for 4200s" in html

    def test_never_run_says_so_instead_of_showing_zeros(self):
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data={"running": None, "last_completed": None, "runs_total": 0, "can_stop": False}),
        )
        html = out["html"]
        assert "never run" in html
        assert "0 files" not in html

    def test_a_failed_poll_keeps_the_numbers_and_marks_them_stale(self):
        """It never blanks them, and never redraws them as current."""
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING, error="HTTP 502", lastOk="2026-08-31T14:08:44+00:00", failures=1),
        )
        html = out["html"]
        assert "812 files processed" in html  # kept
        assert "stale" in html
        assert "couldn&#39;t refresh" in html or "couldn't refresh" in html

    def test_the_history_button_carries_the_true_total(self):
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING),
        )
        assert "Run history (8)" in out["html"]

    def test_the_configuration_row_opens_a_read_out_never_an_editor(self):
        """The extraction block is deploy-time; a control here would be a
        second settings surface, which is what "zero new navigation" forbids."""
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_RUNNING),
        )
        html = out["html"]
        assert "Configuration" in html
        assert "Read-only" in html
        assert "toggleExtractionDrawer(&#39;sp1&#39;, &#39;config&#39;)" in html or "'sp1', 'config'" in html
        assert "<input" not in html
        assert "<select" not in html

    def test_a_last_run_that_erred_on_everything_reads_as_a_failure_not_green(self):
        """The production incident this guards: a card that shows a green
        dot and 'done' next to 1,263 silent failures is worse than showing
        nothing — it actively reassures. `outcome`/`status` come from the
        SERVER's recorded status (item 4's own guard); this only checks the
        card renders whatever the server says, honestly."""
        failed_last = {
            "running": None,
            "last_completed": {
                "id": "er_9",
                "outcome": "failed",
                "finished_at": "2026-08-31T10:00:00+00:00",
                "duration_s": 900.0,
                "files_done": 1551,
                "new": 0,
                "changed": 0,
                "unchanged": 0,
                "errors": 1263,
                "skips_total": 7,
                "skips_listed": 7,
                "usage": {},
            },
            "runs_total": 3,
            "can_stop": False,
        }
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=failed_last),
        )
        html = out["html"]
        assert "ext-dot--ok" not in html
        assert "ext-dot--danger" in html
        assert "failed" in html
        assert "1,263 error" in html or "1263 error" in html
        assert "ext-danger" in html

    def test_a_clean_last_run_shows_no_error_line_and_a_green_dot(self):
        clean_last = {
            "running": None,
            "last_completed": {
                "id": "er_8",
                "outcome": "done",
                "finished_at": "2026-08-31T10:00:00+00:00",
                "duration_s": 30.0,
                "files_done": 5,
                "new": 5,
                "changed": 0,
                "unchanged": 0,
                "errors": 0,
                "skips_total": 0,
                "usage": {},
            },
            "runs_total": 1,
            "can_stop": False,
        }
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=clean_last),
        )
        html = out["html"]
        assert "ext-dot--ok" in html
        assert "error" not in html.lower()

    def test_the_error_summary_carries_the_run_id_for_the_lazy_fetch(self):
        """`ontoggle` fetches `.../extraction/runs/{run_id}` on first open —
        the connection id and run id must be recoverable from the markup
        alone, since nothing else threads them to the handler."""
        failed_last = {
            "running": None,
            "last_completed": {
                "id": "er_42",
                "outcome": "failed",
                "finished_at": "2026-08-31T10:00:00+00:00",
                "duration_s": 10.0,
                "files_done": 10,
                "new": 0,
                "changed": 0,
                "errors": 10,
                "usage": {},
            },
            "runs_total": 1,
            "can_stop": False,
        }
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=failed_last),
        )
        html = out["html"]
        assert 'data-conn="sp1"' in html
        assert 'data-run="er_42"' in html
        assert "_extLoadErrorDetail(this)" in html


class TestDegradation:
    def test_a_501_renders_once_and_explains_itself(self):
        out = _run_js(
            '_extRender("sp1"); console.log(JSON.stringify({html: _elements["ext-block-sp1"].innerHTML}));',
            state=_state(stopped=True),
        )
        html = out["html"]
        assert "needs a Postgres backend" in html
        # …and offers no Retry: retrying cannot change the answer.
        assert "extRetry" not in html

    def test_three_failures_with_no_data_collapse_to_a_failed_panel(self):
        out = _run_js(
            '_extRender("sp1"); console.log(JSON.stringify({html: _elements["ext-block-sp1"].innerHTML}));',
            state=_state(failures=3, error="HTTP 500"),
        )
        html = out["html"]
        assert "Couldn&#39;t read the extraction run state" in html or "Couldn't read" in html
        assert "extRetry(&#39;sp1&#39;)" in html or "extRetry('sp1')" in html

    def test_the_crawl_cell_stays_the_document_count_when_nothing_runs(self):
        out = _run_js(
            '_extRenderCrawlCell("sp1", {running: null});'
            'console.log(JSON.stringify({hidden: _elements["ext-crawl-live-sp1"].hidden,'
            ' html: _elements["ext-crawl-live-sp1"].innerHTML}));'
        )
        assert out["hidden"] is True
        assert out["html"] == ""

    def test_the_crawl_cell_goes_live_only_while_a_run_is_live(self):
        out = _run_js(
            f"_extRenderCrawlCell('sp1', {json.dumps(_RUNNING)});"
            'console.log(JSON.stringify({hidden: _elements["ext-crawl-live-sp1"].hidden,'
            ' html: _elements["ext-crawl-live-sp1"].innerHTML}));'
        )
        assert out["hidden"] is False
        assert "crawling" in out["html"]
        assert "812 files" in out["html"]


# --------------------------------------------------------------------------
# The facts (LLM graph-extraction) phase — owner-frustration fix, 2026-09-02:
# a healthy multi-hour facts pass never checkpointed at all, so the SAME
# `files_done` the crawl froze at kept being shown next to a "stalled" chip
# for the whole run. Once `_RunRecorder.checkpoint_facts` starts writing
# again, the card must say WHICH phase is live and how far it has gotten,
# not just keep repeating the crawl's own numbers.
# --------------------------------------------------------------------------


_FACTS_RUNNING = json.loads(json.dumps(_RUNNING))
_FACTS_RUNNING["running"]["activity"] = {
    "phase": "facts",
    "current_path": "Engagements/northwind-rollout.docx",
    "current_started_at": "2026-08-31T14:08:30+00:00",
    "recent": [],
}
_FACTS_RUNNING["running"]["facts_progress"] = {"docs_done": 340, "docs_total": 1200}


class TestFactsPhaseRendering:
    def test_the_run_row_shows_document_progress_not_the_frozen_file_count(self):
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=_FACTS_RUNNING),
        )
        html = out["html"]
        assert "extracting facts" in html
        assert "340/1,200 documents processed" in html or "340/1200 documents processed" in html
        assert "812 files processed" not in html

    def test_the_pipeline_strip_cell_also_reflects_the_facts_phase(self):
        out = _run_js(
            f"_extRenderCrawlCell('sp1', {json.dumps(_FACTS_RUNNING)});"
            'console.log(JSON.stringify({html: _elements["ext-crawl-live-sp1"].innerHTML}));'
        )
        html = out["html"]
        assert "extracting facts" in html
        assert "documents" in html
        assert "files" not in html

    def test_a_stalled_facts_run_still_says_stalled_first(self):
        """Outcome takes precedence over phase wording — `stalled` must
        never be softened into "extracting facts" just because the phase
        happens to be facts when the checkpoint went quiet."""
        stalled = json.loads(json.dumps(_FACTS_RUNNING))
        stalled["running"]["outcome"] = "stalled"
        stalled["running"]["liveness_note"] = "no checkpoint for 4595s"
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=stalled),
        )
        html = out["html"]
        assert "no longer reporting" in html
        assert "extracting facts" not in html

    def test_a_facts_phase_run_with_no_progress_recorded_yet_falls_back_to_files(self):
        """Defensive: `activity.phase` and `facts_progress` are written
        together by `checkpoint_facts`, but a row missing one must not
        crash the card or invent a document count from nothing."""
        no_progress = json.loads(json.dumps(_FACTS_RUNNING))
        no_progress["running"]["facts_progress"] = None
        out = _run_js(
            'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
            state=_state(data=no_progress),
        )
        html = out["html"]
        assert "812 files processed" in html


class TestRunsDrawer:
    _RUNS = {
        "runs": [
            {
                "id": "er_2",
                "outcome": "interrupted",
                "started_at": "2026-08-30T21:00:00+00:00",
                "duration_s": 252.0,
                "files_done": 318,
                "new": 318,
                "skips_total": 0,
                "resumable": True,
                "error": None,
            },
            {
                "id": "er_1",
                "outcome": "failed",
                "started_at": "2026-08-30T15:00:00+00:00",
                "duration_s": 2.0,
                "files_done": 0,
                "skips_total": 0,
                "error": "CrawlError: sharepoint.enabled is false — refusing to run",
            },
        ],
        "total": 7,
    }

    def test_rows_render_outcome_duration_and_files(self):
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(self._RUNS)})}}));")
        html = out["html"]
        assert "interrupted" in html
        assert "failed" in html
        assert "318 files" in html
        assert "4m 12s" in html

    def test_a_failed_row_shows_its_refusal_verbatim(self):
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(self._RUNS)})}}));")
        assert "sharepoint.enabled is false" in out["html"]

    def test_only_the_interrupted_row_gets_the_resume_reassurance(self):
        """A crash must never carry copy telling an operator its work was
        kept and will resume."""
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(self._RUNS)})}}));")
        html = out["html"]
        assert html.count("the next run resumes") == 1
        # …and it sits in the interrupted row, above the failed one.
        assert html.index("the next run resumes") < html.index("sharepoint.enabled is false")

    def test_untruncated_history_is_named_not_silently_dropped(self):
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(self._RUNS)})}}));")
        assert "5 older runs not shown" in out["html"]

    def test_a_skip_list_shorter_than_the_skip_count_says_so(self):
        runs = {
            "runs": [
                {
                    "id": "er_9",
                    "outcome": "done",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 664.0,
                    "files_done": 1240,
                    "skips_total": 9,
                    "skips_listed": 7,
                }
            ],
            "total": 1,
        }
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")
        assert "9 not indexed" in out["html"]
        assert "7 listed by name" in out["html"]

    def test_a_timed_out_run_names_its_exit(self):
        """ "the ceiling did its job" and "something broke" both render as a
        failed run — the recorded reason is what tells them apart."""
        runs = {
            "runs": [
                {
                    "id": "er_t",
                    "outcome": "failed",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 3600.0,
                    "files_done": 4102,
                    "interrupted_reason": "timeout",
                    "resumable": False,
                    "error": "run exceeded extraction.timeout_s",
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "stopped early — the run hit its time ceiling" in html
        # The reason line and the resume line are INDEPENDENT: this fixture
        # pins a server that did not vouch for resumability, and the drawer
        # must then stay silent about it even though the reason is one that
        # usually is resumable. (The resumable-timeout case is its own test.)
        assert "the next run resumes" not in html

    def test_a_resumable_timeout_gets_the_reassurance_despite_being_failed(self):
        """The copy follows the server's `resumable` verdict, not the outcome
        word — a timeout persisted its state and costs re-work, not coverage."""
        runs = {
            "runs": [
                {
                    "id": "er_t",
                    "outcome": "failed",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 3600.0,
                    "files_done": 4102,
                    "interrupted_reason": "timeout",
                    "resumable": True,
                    "error": "run exceeded extraction.timeout_s",
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "stopped early — the run hit its time ceiling" in html
        assert "the next run resumes from where it stopped" in html

    def test_a_crash_never_gets_the_resume_reassurance(self):
        runs = {
            "runs": [
                {
                    "id": "er_c",
                    "outcome": "failed",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 4.0,
                    "files_done": 0,
                    "interrupted_reason": "error",
                    "resumable": False,
                    "error": "RuntimeError: graph exploded",
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "the next run resumes" not in html
        assert "graph exploded" in html

    def test_a_throttle_stop_explains_itself_and_is_resumable(self):
        """A 429-budget abort leaves consistent state, so it earns the resume
        line — and it says WHAT ran out, not the slug "throttled"."""
        runs = {
            "runs": [
                {
                    "id": "er_th",
                    "outcome": "failed",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 900.0,
                    "files_done": 512,
                    "http_429": 120,
                    "throttle_wait_s": 900.0,
                    "interrupted_reason": "throttled",
                    "resumable": True,
                    "error": "GraphThrottled: 429 budget exhausted",
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "throttling budget was exhausted" in html
        assert "the next run resumes from where it stopped" in html

    def test_an_unknown_stop_reason_is_shown_verbatim_never_hidden(self):
        """A slug we do not recognize is still information — collapsing it
        into a generic phrase would be worse than not explaining it."""
        runs = {
            "runs": [
                {
                    "id": "er_u",
                    "outcome": "failed",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "files_done": 1,
                    "interrupted_reason": "quota_exhausted",
                    "resumable": False,
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "quota_exhausted" in html
        assert "the next run resumes" not in html

    def test_a_run_with_no_recorded_reason_says_nothing_about_one(self):
        runs = {
            "runs": [
                {
                    "id": "er_n",
                    "outcome": "done",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 12.0,
                    "files_done": 5,
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "stopped early" not in html

    def test_no_runs_is_an_empty_state_not_a_blank_drawer(self):
        out = _run_js("console.log(JSON.stringify({html: _extRunsHtml('sp1', {runs: [], total: 0})}));")
        assert "No extraction runs recorded yet" in out["html"]

    def test_a_row_with_errors_gets_the_same_treatment_the_last_run_block_does(self):
        """`skips_total` already gets a warn line here — `errors` must too,
        with the run's own id threaded through for the lazy fetch."""
        runs = {
            "runs": [
                {
                    "id": "er_bad",
                    "outcome": "failed",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 900.0,
                    "files_done": 1551,
                    "new": 0,
                    "changed": 0,
                    "errors": 1263,
                    "skips_total": 7,
                    "skips_listed": 7,
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "1,263 error" in html or "1263 error" in html
        assert 'data-conn="sp1"' in html
        assert 'data-run="er_bad"' in html
        assert "7 not indexed" in html  # the pre-existing skips line, unaffected

    def test_a_row_with_no_errors_gets_no_error_line(self):
        runs = {
            "runs": [
                {
                    "id": "er_ok",
                    "outcome": "done",
                    "started_at": "2026-08-31T09:00:00+00:00",
                    "duration_s": 12.0,
                    "files_done": 5,
                    "errors": 0,
                }
            ],
            "total": 1,
        }
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml('sp1', {json.dumps(runs)})}}));")["html"]
        assert "ext-errors" not in html


class TestErrorDetail:
    """`_extErrorItemsHtml` — the itemized rows rendered once the per-run
    fetch (`GET …/extraction/runs/{run_id}`, A3) resolves. Its shape mirrors
    `report.errors_detail`, the same `{items, total, listed, truncated}`
    envelope `skips` already uses server-side."""

    def _html(self, report_errors_detail):
        detail = {"report": {"errors_detail": report_errors_detail}}
        out = _run_js(f"console.log(JSON.stringify({{html: _extErrorItemsHtml({json.dumps(detail)})}}));")
        return out["html"]

    def test_each_item_shows_path_reason_status_and_message(self):
        html = self._html(
            {
                "items": [
                    {"path": "Reports/Q3.docx", "reason": "download_failed", "detail": "HTTP 500", "status_code": 500}
                ],
                "total": 1,
                "listed": 1,
                "truncated": False,
            }
        )
        assert "Reports/Q3.docx" in html
        assert "download_failed" in html
        assert "HTTP 500" in html
        assert "500" in html

    def test_a_status_less_error_shows_no_invented_status(self):
        html = self._html(
            {
                "items": [
                    {"path": "Reports/Q4.docx", "reason": "ingest_failed", "detail": "boom", "status_code": None}
                ],
                "total": 1,
                "listed": 1,
                "truncated": False,
            }
        )
        assert "HTTP" not in html
        assert "boom" in html

    def test_truncation_is_named_not_silently_dropped(self):
        items = [
            {"path": f"Reports/f{i}.docx", "reason": "download_failed", "detail": "HTTP 500", "status_code": 500}
            for i in range(2)
        ]
        html = self._html({"items": items, "total": 1263, "listed": 2, "truncated": True})
        assert "1,263" in html or "1263" in html
        assert "2" in html

    def test_no_recorded_items_is_an_honest_empty_state(self):
        html = self._html({"items": [], "total": 0, "listed": 0, "truncated": False})
        assert "No per-file detail" in html


class TestStopReasonVocabularyAgrees:
    """The stop-reason contract has ONE producer (the crawl's `_stop_reason`)
    and, on this side, TWO consumers: `RESUMABLE_STOP_REASONS` in Python
    decides whether Agnes promises "your work is safe", and
    `EXT_STOP_REASON_TEXT` in the template decides how the reason reads.
    Two tables written against one upstream vocabulary is exactly the shape
    that drifts silently — a reason added to one and forgotten in the other
    fails nothing at runtime. This pins them to each other.
    """

    @staticmethod
    def _js_reason_keys() -> set:
        block = _extract_block(_ds_page_source.page_source(), "const EXT_STOP_REASON_TEXT = {")
        return set(re.findall(r"^\s*([a-z_]+):", block, re.M))

    def test_every_resumable_reason_has_a_human_phrase(self):
        """A resumable stop is the one an operator most needs to understand
        — it is the row that says "do not re-run this". Rendering it as a
        bare slug would undercut the reassurance sitting right beneath it."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS

        missing = set(RESUMABLE_STOP_REASONS) - self._js_reason_keys()
        assert not missing, f"resumable stop reasons with no rendered phrase: {sorted(missing)}"

    def test_the_unresumable_reason_is_phrased_but_never_vouched_for(self):
        """`error` must read legibly AND must not be claimed resumable —
        the two tables disagreeing on this one is the costly direction."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS

        assert "error" in self._js_reason_keys()
        assert "error" not in RESUMABLE_STOP_REASONS

    def test_no_phrase_exists_for_a_reason_python_has_never_heard_of(self):
        """A phrase without a matching upstream value is dead copy that will
        one day be attached to the wrong thing."""
        from app.api.admin_extraction import RESUMABLE_STOP_REASONS

        known = set(RESUMABLE_STOP_REASONS) | {"error"}
        assert self._js_reason_keys() <= known, (
            f"rendered phrases for reasons nothing produces: {sorted(self._js_reason_keys() - known)}"
        )


class TestConfigDrawer:
    _CONFIG = {
        "effective": [
            {
                "key": "sharepoint.enabled",
                "label": "Enabled",
                "value": True,
                "origin": "env",
                "env_name": "AGNES_SHAREPOINT_ENABLED",
                "editable": False,
                "lock_reason": "set by the environment (AGNES_SHAREPOINT_ENABLED)",
                "note": None,
            },
            {
                "key": "extraction.crawler.max_file_mb",
                "label": "Max file size (MB)",
                "value": 50,
                "origin": "default",
                "env_name": None,
                "editable": False,
                "lock_reason": "deploy-time configuration",
                "note": "a document over this cap never appears in the collection",
            },
            {
                "key": None,
                "label": "Checkpoint granularity",
                "value": "every 200 delta rows",
                "origin": "builtin",
                "env_name": None,
                "editable": False,
                "lock_reason": "a code constant — there is no setting to change",
                "note": None,
            },
        ],
        "scopes": [
            {
                "display_path": "Finance/Contracts",
                "anonymize": True,
                "anonymization_declared": True,
                "audience_classes": [{"name": "Legal"}, {"name": "Finance"}],
                "no_group_warning": False,
            },
            {
                "display_path": "HR/Handbook",
                "anonymize": True,
                "anonymization_declared": False,
                "audience_classes": [],
                "no_group_warning": True,
            },
        ],
        "section_editable": False,
        "section_lock_reason": "The `extraction` section is not admin-writable.",
    }

    def _html(self):
        return _run_js(f"console.log(JSON.stringify({{html: _extConfigHtml({json.dumps(self._CONFIG)})}}));")["html"]

    def test_every_row_shows_its_origin(self):
        html = self._html()
        assert "env AGNES_SHAREPOINT_ENABLED" in html
        assert "built-in default" in html
        assert "built in" in html

    def test_locked_rows_render_as_deploy_time_with_a_reason(self):
        html = self._html()
        assert "deploy-time" in html
        assert "set by the environment" in html

    def test_the_size_cap_explains_what_a_skip_costs(self):
        html = self._html()
        assert "never appears in the collection" in html

    def test_requested_and_declared_are_never_collapsed(self):
        html = self._html()
        assert "anonymize ✓ requested · declared ✓" in html
        assert "anonymize ✓ requested · not declared" in html

    def test_a_scope_nobody_can_see_is_flagged(self):
        assert "no group can see this collection" in self._html()

    def test_the_section_lock_reason_is_stated_once(self):
        assert "not admin-writable" in self._html()


_SCHEDULE_SIGNATURES = (
    "function _extS(id) {",
    "async function _extFetchOne(connId) {",
    "function _extConnectionIds() {",
    "function _extNextDelay() {",
    "async function _extTick() {",
    "function _extSchedule() {",
    "function _extAfterCardsPainted() {",
)


def _run_schedule_js(body: str) -> dict:
    """The poll's timing, on a fake clock.

    Cards on this page arrive over fetch, so the harness starts with none —
    which is exactly the state the script self-starts against in a browser.
    """
    tpl = _ds_page_source.page_source()
    fns = "\n".join(_extract_block(tpl, sig) for sig in _SCHEDULE_SIGNATURES)
    script = f"""
const EXT_POLL_ACTIVE_MS = 3000;
const EXT_POLL_IDLE_MS = 30000;
const EXT_POLL_MAX_BACKOFF_MS = 60000;
const EXT_MAX_FAILURES = 3;

let NOW = 0;
let CARDS = [];                 // no [data-ext-conn] until the cards paint
const requests = [];
const renders = [];
const timers = [];

const document = {{
  visibilityState: "visible",
  querySelectorAll: () => CARDS.map((id) => ({{ dataset: {{ extConn: id }} }})),
  getElementById: () => null,
}};
function setTimeout(fn, ms) {{ timers.push({{ id: timers.length + 1, at: NOW + ms, fn }}); return timers.length; }}
function clearTimeout(id) {{ const t = timers.find((x) => x.id === id); if (t) t.cancelled = true; }}
async function fetch(url) {{
  requests.push(NOW);
  return {{ ok: true, status: 200, json: async () => ({{ connection_id: "sp1", runs_total: 0, as_of: null }}) }};
}}
function _extRender(connId) {{ renders.push({{ t: NOW, connId }}); }}

const _extState = {{}};
let _extTimer = null;
let _extInFlight = false;

{fns}

async function runClock(untilMs) {{
  for (let guard = 0; guard < 200; guard++) {{
    const next = timers.filter((t) => !t.cancelled && !t.fired && t.at > NOW).sort((a, b) => a.at - b.at)[0];
    if (next) next.fired = true;
    if (!next || next.at > untilMs) break;
    NOW = next.at;
    await next.fn();
  }}
  NOW = untilMs;
}}

(async () => {{
{body}
}})();
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


class TestPollFollowsTheCards:
    """The poll must be armed by the paint, not by the idle interval.

    The script self-starts at parse time, when the connections fetch has not
    resolved and the page carries no card. That first tick finds nothing to
    ask about, and the scheduler — reading a still-empty state map — arms the
    IDLE interval, so the run row used to appear 30 s (and, when a mutation
    repainted over it mid-tick, 60 s) after the card it belongs to.
    """

    def test_the_first_request_follows_the_paint_not_the_idle_interval(self):
        out = _run_schedule_js("""
  await _extTick();                       // parse time: no cards on the page
  _extSchedule();
  await runClock(1500);                   // the connections fetch resolves
  CARDS = ["sp1"];
  await _extAfterCardsPainted();
  await runClock(2000);
  console.log(JSON.stringify({ first: requests.length ? requests[0] : null, count: requests.length }));
""")
        assert out["first"] == 1500, out
        assert out["count"] == 1, out

    def test_a_paint_with_nothing_known_yet_re_arms_the_cadence(self):
        """The pending idle timer is not the schedule any more — a live run
        must poll at the ACTIVE cadence from the paint, not inherit the 30 s
        wheel the empty page armed."""
        out = _run_schedule_js("""
  await _extTick();
  _extSchedule();
  await runClock(1500);
  CARDS = ["sp1"];
  await _extAfterCardsPainted();
  await runClock(40000);
  console.log(JSON.stringify({ requests }));
""")
        # 1500 (the paint), then the re-armed idle wheel — never a first ask
        # at 30 000 with the paint 28.5 s earlier.
        assert out["requests"][0] == 1500, out
        assert out["requests"][1] == 31500, out

    def test_a_repaint_over_known_state_redraws_without_asking_again(self):
        """A mutation's `loadConnections()` rebuilds the card and wipes the
        block. Redrawing it from cache costs nothing; asking again would put
        a request on every mutation."""
        out = _run_schedule_js("""
  CARDS = ["sp1"];
  await _extTick();                       // one real read
  const after = requests.length;
  NOW = 5000;
  _extAfterCardsPainted();                // the card was rebuilt underneath
  console.log(JSON.stringify({
    requests_before: after,
    requests_after: requests.length,
    repainted: renders.filter((r) => r.t === 5000).map((r) => r.connId),
  }));
""")
        assert out["requests_before"] == 1, out
        assert out["requests_after"] == 1, out
        assert out["repainted"] == ["sp1"], out

    def test_the_paint_hook_is_called_from_every_card_paint(self):
        tpl = _ds_page_source.page_source()
        # Guarded by `typeof`: the hook lives in a later script block than the
        # renderers that call it, and the parser may run a fetch continuation
        # between the two.
        assert tpl.count('if (typeof _extAfterCardsPainted === "function") _extAfterCardsPainted();') == 2


class TestFactsPassSurfacesOnTheCard:
    """The standalone facts pass is a JOB (`sharepoint-facts-extraction`),
    not a crawl run — it opens no `extraction_runs` row — so the status
    poll carries it as `facts_job` off the job queue, and the card must
    (a) say so in the Run row and (b) never keep offering "Extract facts
    now" while one is queued or running: the server can only answer that
    click with `409 facts_extraction_already_running`."""

    _IDLE = {
        "connection_id": "sp1",
        "running": None,
        "last_completed": None,
        "runs_total": 0,
        "can_stop": True,
        "as_of": "2026-09-02T10:00:00+00:00",
    }

    def test_a_queued_facts_pass_is_named_in_the_run_row(self):
        data = {
            **self._IDLE,
            "facts_job": {"id": "job-9", "status": "queued", "created_at": "2026-09-02T09:58:00+00:00"},
        }
        out = _run_js(
            "console.log(JSON.stringify({ html: _extRunRowHtml('sp1', _extState.sp1) }));",
            state=_state(data=data),
        )
        html = out["html"]
        assert "facts pass" in html.lower()
        assert "queued" in html
        assert "job-9" in html

    def test_a_running_facts_pass_reads_running_not_queued(self):
        data = {
            **self._IDLE,
            "facts_job": {
                "id": "job-10",
                "status": "running",
                "created_at": "2026-09-02T09:58:00+00:00",
                "started_at": "2026-09-02T09:59:00+00:00",
            },
        }
        out = _run_js(
            "console.log(JSON.stringify({ html: _extRunRowHtml('sp1', _extState.sp1) }));",
            state=_state(data=data),
        )
        html = out["html"]
        assert "facts pass" in html.lower()
        assert "running" in html
        assert "queued" not in html

    def test_no_facts_pass_draws_no_facts_line(self):
        data = {**self._IDLE, "facts_job": None}
        out = _run_js(
            "console.log(JSON.stringify({ html: _extRunRowHtml('sp1', _extState.sp1) }));",
            state=_state(data=data),
        )
        assert "facts pass" not in out["html"].lower()

    def test_the_button_is_disabled_while_a_pass_is_in_flight_and_restored_after(self):
        body = """
_extRenderFactsButton('sp1', { facts_job: { id: 'job-9', status: 'queued' } });
const during = { disabled: _elements['ext-facts-btn-sp1'].disabled, title: _elements['ext-facts-btn-sp1'].title };
_extRenderFactsButton('sp1', { facts_job: null });
const after = { disabled: _elements['ext-facts-btn-sp1'].disabled, title: _elements['ext-facts-btn-sp1'].title };
console.log(JSON.stringify({ during, after }));
"""
        out = _run_js(body)
        assert out["during"]["disabled"] is True
        assert "already" in out["during"]["title"].lower()
        assert out["after"]["disabled"] is False
        assert out["after"]["title"] == ""

    def test_a_pass_ending_never_re_enables_a_button_the_server_gated_off(self):
        body = """
_elements['ext-facts-btn-sp1'].dataset.factsReady = '0';
_elements['ext-facts-btn-sp1'].disabled = true;
_extRenderFactsButton('sp1', { facts_job: null });
console.log(JSON.stringify({ disabled: _elements['ext-facts-btn-sp1'].disabled }));
"""
        assert _run_js(body)["disabled"] is True
