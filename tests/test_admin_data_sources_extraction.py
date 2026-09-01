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

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"


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
    "const EXT_DOT = {",
    "function _extDot(outcome) {",
    "function _extRenderCrawlCell(connId, status) {",
    "function _extRunLine(run) {",
    "const EXT_STOP_REASON_TEXT = {",
    "function _extStopReasonText(reason) {",
    "function _extThrottleLine(run) {",
    "function _extRunRowHtml(connId, st) {",
    "function _extConfigRowHtml(connId) {",
    "function _extPanelHtml(tone, title, body, connId, retry) {",
    "function _extRender(connId) {",
    "function _extRunsHtml(body) {",
    "const EXT_ORIGIN_LABEL = {",
    "function _extConfigHtml(body) {",
)


def _run_js(body: str, *, state: dict | None = None) -> dict:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    fns = "\n".join(_extract_block(tpl, sig) for sig in _SIGNATURES)
    script = f"""
const EXT_MAX_FAILURES = 3;
{fns}

const _extState = {json.dumps(state or {})};
const _elements = {{
  "ext-block-sp1": {{ hidden: true, innerHTML: "" }},
  "ext-crawl-live-sp1": {{ hidden: true, innerHTML: "" }},
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
        tpl = TEMPLATE.read_text(encoding="utf-8")
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
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(self._RUNS)})}}));")
        html = out["html"]
        assert "interrupted" in html
        assert "failed" in html
        assert "318 files" in html
        assert "4m 12s" in html

    def test_a_failed_row_shows_its_refusal_verbatim(self):
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(self._RUNS)})}}));")
        assert "sharepoint.enabled is false" in out["html"]

    def test_only_the_interrupted_row_gets_the_resume_reassurance(self):
        """A crash must never carry copy telling an operator its work was
        kept and will resume."""
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(self._RUNS)})}}));")
        html = out["html"]
        assert html.count("the next run resumes") == 1
        # …and it sits in the interrupted row, above the failed one.
        assert html.index("the next run resumes") < html.index("sharepoint.enabled is false")

    def test_untruncated_history_is_named_not_silently_dropped(self):
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(self._RUNS)})}}));")
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
        out = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")
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
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")["html"]
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
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")["html"]
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
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")["html"]
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
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")["html"]
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
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")["html"]
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
        html = _run_js(f"console.log(JSON.stringify({{html: _extRunsHtml({json.dumps(runs)})}}));")["html"]
        assert "stopped early" not in html

    def test_no_runs_is_an_empty_state_not_a_blank_drawer(self):
        out = _run_js("console.log(JSON.stringify({html: _extRunsHtml({runs: [], total: 0})}));")
        assert "No extraction runs recorded yet" in out["html"]


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
        block = _extract_block(TEMPLATE.read_text(encoding="utf-8"), "const EXT_STOP_REASON_TEXT = {")
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
