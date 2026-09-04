"""Stop button + live activity on the SharePoint extraction card's `Run` row.

Companion to `test_admin_data_sources_extraction.py` (whose harness this file
mirrors exactly — same `_run_js`/`_extract_block` pattern, same execute-the-
real-functions-under-`node` approach) and to the "2026-08-31 design §4"
extraction-observability block: this file covers only what changed on top of
it — a cooperative Stop control and a live "what is it touching right now"
line — without re-testing the honest degradations that file already pins.

Rules this file exists to hold:
- The Stop button draws only while a run is `running`/`stalled` AND the
  server has not said `can_stop === false` (the old "cannot be stopped from
  here yet" sentence keeps covering that case — the two are mutually
  exclusive, never both drawn).
- Clicking Stop flips the row to a disabled "Stopping…" state synchronously,
  before the network call resolves — the poll, not the click, is what later
  confirms the run actually ended.
- `activity` is optional on the running-run object (older rows, or a DuckDB
  instance that never recorded it) — its absence draws nothing.
- Every path in `activity` is untrusted-ish (a filename a document owner
  chose) and must render as escaped text, never raw HTML.
"""

from __future__ import annotations

from tests import _ds_page_source

import json
import subprocess
import tempfile
from pathlib import Path

import pytest


# Kept for any future caller that needs the template path itself — content
# reads go through `_ds_page_source.page_source()` (perf follow-up,
# 2026-09-03: most of this page's JS moved into extracted static files, see
# tests/_ds_page_source.py).
TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_data_sources.html"


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
    "function _extLiveLine(connId, status) {",
    "function _extRunLine(run) {",
    "const EXT_STOP_REASON_TEXT = {",
    "function _extStopReasonText(reason) {",
    "function _extThrottleLine(run) {",
    "function _extActivityHtml(activity) {",
    "function _extShardCountText(run) {",
    "function _extShardsHtml(shards) {",
    "function _extFactsJobLine(job) {",
    "function _extFactsPendingLine(status) {",
    "function _extScanOcrPausedLine(run) {",
    "function _extFactsEta(seconds) {",
    "function _extFactsThroughputNote(status) {",
    "function _extRunRowHtml(connId, st) {",
    "function _extConfigRowHtml(connId) {",
    "function _extPanelHtml(tone, title, body, connId, retry) {",
    "function _extRenderRunNowButton(connId, status) {",
    "function _extRenderFactsButton(connId, status) {",
    "function _extNotIndexedCounts(status) {",
    "function _extRenderHeadTiles(connId, status) {",
    "function _extRenderNotIndexedPanel(connId, status) {",
    "function _extRenderFactsPanel(connId, status) {",
    "function _spStateChip(fs, status) {",
    "function _extStateChip(connId, status) {",
    "function _extRender(connId) {",
    "function _extRenderNextRun(connId, status) {",
    "function _extRenderRunsCount(connId, status) {",
)

# The network-touching functions, pulled in only for the tests that click
# the button — kept separate so the pure-rendering tests above don't need a
# `fetch`/`confirm` mock at all.
_ASYNC_SIGNATURES = _SIGNATURES + (
    "async function _extFetchOne(connId) {",
    "async function extStopRun(connId) {",
    "async function extCancelRun(connId, runId) {",
)


def _elements_js() -> str:
    return """
const SOURCE_PIPELINES = {};
const _elements = {
  "ext-block-sp1": { hidden: true, innerHTML: "" },
  "sp-liveline-sp1": { hidden: true, innerHTML: "" },
  "sp-chip-sp1": { className: "", textContent: "" },
  "sp-tile-notindexed-sp1": { textContent: "" },
  "sp-tile-spent-sp1": { textContent: "" },
  "sp-notindexed-body-sp1": { innerHTML: "" },
  "sp-facts-body-sp1": { innerHTML: "" },
  "sp-history-count-sp1": { textContent: "" },
  "sp-runnow-btn-sp1": { dataset: { extractionReady: "1" }, disabled: false, title: "", style: {} },
  "sp-stop-btn-sp1": { dataset: {}, disabled: false, textContent: "", style: {} },
  "sp-cancel-btn-sp1": { dataset: {}, disabled: false, textContent: "", style: {} },
  "ext-facts-btn-sp1": { dataset: { factsReady: "1" }, disabled: false, title: "" },
};
const document = { getElementById: (id) => _elements[id] };
"""


def _run_js(body: str, *, state: dict | None = None, signatures=_SIGNATURES, preamble: str = "") -> dict:
    tpl = _ds_page_source.page_source()
    fns = "\n".join(_extract_block(tpl, sig) for sig in signatures)
    script = f"""
const EXT_MAX_FAILURES = 3;
{preamble}
{fns}

const _extState = {json.dumps(state or {})};
{_elements_js()}

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


def _running(**over):
    run = {
        "id": "er_1",
        "outcome": "running",
        "started_at": "2026-08-31T14:02:00+00:00",
        "checkpoint_at": "2026-08-31T14:08:41+00:00",
        "elapsed_s": 391.0,
        "files_done": 812,
        "new": 800,
        "changed": 12,
        "unchanged": 0,
    }
    run.update(over)
    return {
        "connection_id": "sp1",
        "running": run,
        "last_completed": None,
        "runs_total": 8,
        "can_stop": True,
        "as_of": "2026-08-31T14:08:44+00:00",
    }


def _state(**over):
    st = {"failures": 0, "stopped": False, "data": None, "lastOk": None, "error": None, "stopping": False}
    st.update(over)
    return {"sp1": st}


def _run_row_html(status: dict, *, stopping: bool = False) -> str:
    out = _run_js(
        'console.log(JSON.stringify({html: _extRunRowHtml("sp1", _extState["sp1"])}));',
        state=_state(data=status, stopping=stopping),
    )
    return out["html"]


def _render_run_now_button(status) -> dict:
    return _run_js(
        f"""
_extRenderRunNowButton("sp1", {json.dumps(status)});
console.log(JSON.stringify({{
  runDisplay: _elements["sp-runnow-btn-sp1"].style.display,
  stopDisplay: _elements["sp-stop-btn-sp1"].style.display,
  cancelDisplay: _elements["sp-cancel-btn-sp1"].style.display,
}}));
"""
    )


class TestStopButtonVisibility:
    """Stop/Cancel now live in the HEAD (source-card redesign §2.1's "the
    primary slot shows Stop while live"), rendered by
    `_extRenderRunNowButton` — not inside `_extRunRowHtml`'s own markup any
    more (that row has nothing left to act on directly)."""

    def test_shows_for_a_running_run(self):
        out = _render_run_now_button(_running())
        assert out["stopDisplay"] == ""
        assert out["runDisplay"] == "none"

    def test_shows_for_a_stalled_run(self):
        out = _render_run_now_button(_running(outcome="stalled", liveness_note="no checkpoint for 4200s"))
        assert out["stopDisplay"] == ""
        assert out["cancelDisplay"] == ""

    def test_hidden_once_the_run_is_a_completed_one(self):
        status = {
            "running": None,
            "last_completed": {"outcome": "done", "files_done": 100, "finished_at": "2026-08-31T14:10:00+00:00"},
            "runs_total": 9,
            "can_stop": True,
        }
        out = _render_run_now_button(status)
        assert out["stopDisplay"] == "none"
        assert out["runDisplay"] == ""

    def test_hidden_when_the_server_says_can_stop_is_false(self):
        """The pre-existing honest-degradation sentence keeps covering this
        case in the Run row — the head's Stop button is never drawn
        instead of it."""
        status = _running()
        status["can_stop"] = False
        out = _render_run_now_button(status)
        assert out["stopDisplay"] == "none"
        html = _run_row_html(status)
        assert "cannot be stopped from here yet" in html

    def test_never_run_draws_no_stop_button_either(self):
        out = _render_run_now_button({"running": None, "last_completed": None, "runs_total": 0, "can_stop": True})
        assert out["stopDisplay"] == "none"


class TestStoppingState:
    def test_the_button_flips_to_disabled_stopping_before_the_click_resolves(self):
        """`extStopRun` mutates state and re-renders synchronously, BEFORE its
        `await fetch(...)` — so the disabled state is observable the instant
        the click handler returns control, not once the network answers. The
        button lives in the HEAD now (`sp-stop-btn-<id>`), not inside the
        Run row's own markup."""
        out = _run_js(
            """
globalThis.confirm = () => true;
const fetch = () => new Promise(() => {});  // never resolves — we only care about the synchronous prefix
extStopRun("sp1");
console.log(JSON.stringify({
  text: _elements["sp-stop-btn-sp1"].textContent,
  disabled: _elements["sp-stop-btn-sp1"].disabled,
}));
""",
            state=_state(data=_running()),
            signatures=_ASYNC_SIGNATURES,
        )
        assert out["text"] == "Stopping…"
        assert out["disabled"] is True

    def test_a_declined_confirm_sends_no_request_and_changes_nothing(self):
        out = _run_js(
            """
globalThis.confirm = () => false;
let called = false;
const fetch = () => { called = true; return new Promise(() => {}); };
extStopRun("sp1");
console.log(JSON.stringify({ called, html: _elements["ext-block-sp1"].innerHTML }));
""",
            state=_state(data=_running()),
            signatures=_ASYNC_SIGNATURES,
        )
        assert out["called"] is False
        assert out["html"] == ""  # nothing was rendered — extStopRun returned before touching the DOM


class TestStopPostsToTheRightUrl:
    def test_posts_to_the_connections_stop_endpoint(self):
        out = _run_js(
            """
globalThis.confirm = () => true;
const calls = [];
const fetch = (url, opts) => {
  calls.push({ url, method: opts && opts.method, credentials: opts && opts.credentials });
  return new Promise(() => {});
};
extStopRun("sp1");
console.log(JSON.stringify({ calls }));
""",
            state=_state(data=_running()),
            signatures=_ASYNC_SIGNATURES,
        )
        assert out["calls"] == [
            {
                "url": "/api/admin/sharepoint/connections/sp1/extraction/stop",
                "method": "POST",
                "credentials": "include",
            }
        ]

    def test_url_encodes_the_connection_id(self):
        out = _run_js(
            """
globalThis.confirm = () => true;
const calls = [];
const fetch = (url) => { calls.push(url); return new Promise(() => {}); };
extStopRun("sp/weird id");
console.log(JSON.stringify({ calls }));
""",
            state={
                "sp/weird id": {
                    "failures": 0,
                    "stopped": False,
                    "data": _running(),
                    "lastOk": None,
                    "error": None,
                    "stopping": False,
                }
            },
            signatures=_ASYNC_SIGNATURES,
        )
        assert out["calls"] == ["/api/admin/sharepoint/connections/sp%2Fweird%20id/extraction/stop"]


class TestStopOutcomeHandling:
    def test_a_202_clears_stopping_once_the_run_is_gone_and_refreshes_status(self):
        out = _run_js(
            """
globalThis.confirm = () => true;
const calls = [];
const fetch = (url) => {
  calls.push(url);
  if (url.endsWith("/extraction/stop")) {
    return Promise.resolve({ status: 202, json: async () => ({ stop_requested_at: "2026-09-01T00:00:00Z" }) });
  }
  return Promise.resolve({ ok: true, status: 200, json: async () => ({ running: null, last_completed: null, runs_total: 9, can_stop: true, as_of: "x" }) });
};
await extStopRun("sp1");
console.log(JSON.stringify({ stopping: _extState["sp1"].stopping, calls }));
""",
            state=_state(data=_running()),
            signatures=_ASYNC_SIGNATURES,
        )
        assert out["stopping"] is False
        assert out["calls"] == [
            "/api/admin/sharepoint/connections/sp1/extraction/stop",
            "/api/admin/sharepoint/connections/sp1/extraction/status",
        ]

    def test_a_rejected_stop_clears_stopping_and_leaves_the_button_clickable_again(self):
        out = _run_js(
            """
globalThis.confirm = () => true;
const fetch = () => Promise.resolve({ status: 409, json: async () => ({ detail: { error: "not_running", message: "no run to stop" } }) });
await extStopRun("sp1");
console.log(JSON.stringify({
  stopping: _extState["sp1"].stopping,
  stopDisabled: _elements["sp-stop-btn-sp1"].disabled,
  stopText: _elements["sp-stop-btn-sp1"].textContent,
}));
""",
            state=_state(data=_running()),
            signatures=_ASYNC_SIGNATURES,
        )
        assert out["stopping"] is False
        assert out["stopDisabled"] is False
        assert out["stopText"] == "Stop"


class TestStopReasonRendersSentence:
    def test_the_stopped_slug_reads_as_a_sentence(self):
        out = _run_js('console.log(JSON.stringify({text: _extStopReasonText("stopped")}));')
        assert out["text"] == "stopped by an admin"

    def test_stopped_is_not_shown_as_a_bare_slug(self):
        out = _run_js('console.log(JSON.stringify({text: _extStopReasonText("stopped")}));')
        assert out["text"] != "stopped"


class TestLiveActivity:
    def test_phase_and_current_path_render(self):
        run = _running(activity={"phase": "convert", "current_path": "Finance/Contracts/2026/acme.pdf", "recent": []})
        html = _run_row_html(run)
        assert "convert" in html
        assert "acme.pdf" in html

    def test_current_path_is_middle_truncated_with_the_full_path_in_a_title(self):
        long_path = "Finance/Contracts/2026/" + ("subfolder/" * 10) + "very-long-quarterly-report-final-v3.pdf"
        run = _running(activity={"phase": "crawl", "current_path": long_path, "recent": []})
        html = _run_row_html(run)
        assert long_path not in html.replace(f'title="{long_path}"', "")  # not shown UN-truncated outside the title
        assert f'title="{long_path}"' in html
        assert "…" in html

    def test_absent_activity_renders_no_scaffolding(self):
        run = _running()
        run["running"].pop("activity", None)
        html = _run_row_html(run)
        assert "ext-activity" not in html

    def test_null_activity_also_renders_nothing(self):
        run = _running(activity=None)
        html = _run_row_html(run)
        assert "ext-activity" not in html

    def test_recent_paths_render_with_outcome_dots(self):
        run = _running(
            activity={
                "phase": "convert",
                "current_path": "a.pdf",
                "recent": [
                    {"path": "one.docx", "outcome": "done"},
                    {"path": "two.pdf", "outcome": "failed"},
                ],
            }
        )
        html = _run_row_html(run)
        assert "one.docx" in html
        assert "two.pdf" in html
        assert "ext-dot--ok" in html  # "done"
        assert "ext-dot--danger" in html  # "failed"

    def test_recent_is_capped_at_five_even_if_the_server_sends_more(self):
        recent = [{"path": f"file{i}.pdf", "outcome": "done"} for i in range(8)]
        run = _running(activity={"phase": "convert", "current_path": "x.pdf", "recent": recent})
        html = _run_row_html(run)
        assert html.count("ext-activity__recent-item") == 5
        for i in range(5):
            assert f"file{i}.pdf" in html
        for i in range(5, 8):
            assert f"file{i}.pdf" not in html

    def test_a_malicious_path_never_lands_as_html(self):
        """Pin: a path is an untrusted, document-owner-chosen filename. It
        must render as ESCAPED TEXT, never be parsed as markup."""
        evil = "<script>alert(1)</script>.pdf"
        run = _running(
            activity={"phase": "convert", "current_path": evil, "recent": [{"path": evil, "outcome": "done"}]}
        )
        html = _run_row_html(run)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_recent_path_is_also_escaped(self):
        evil = "<img src=x onerror=alert(1)>.pdf"
        run = _running(
            activity={"phase": "convert", "current_path": "fine.pdf", "recent": [{"path": evil, "outcome": "done"}]}
        )
        html = _run_row_html(run)
        assert "<img" not in html
        assert "&lt;img" in html


class TestActivityDoesNotBreakOlderRows:
    def test_a_stalled_run_with_no_activity_still_renders_its_liveness_note(self):
        run = _running(outcome="stalled", liveness_note="no checkpoint for 4200s")
        run["running"].pop("activity", None)
        html = _run_row_html(run)
        assert "no checkpoint for 4200s" in html
        assert "ext-activity" not in html


class TestRunNowButtonReflectsLiveRun:
    """The head's `Run now ▾` button is server-rendered once, from dispatch
    bookkeeping (`identity.extraction_ready`) — but it must never keep
    offering itself while a run is already active, since the server can
    only ever 409 that click (`extraction_already_running`). Renamed from
    `_extRenderInAgnesButton` / `TestInAgnesButtonReflectsLiveRun` — the row
    it used to live in no longer exists (source-card redesign §2.1)."""

    def test_disables_the_button_while_a_run_is_active(self):
        out = _run_js(
            f"""
_extRenderRunNowButton("sp1", {json.dumps(_running())});
console.log(JSON.stringify({{
  disabled: _elements["sp-runnow-btn-sp1"].disabled,
  title: _elements["sp-runnow-btn-sp1"].title,
}}));
"""
        )
        assert out["disabled"] is True
        assert "already running" in out["title"]

    def test_re_enables_once_the_run_is_gone_and_the_connection_stays_ready(self):
        idle_status = {"running": None, "last_completed": None, "runs_total": 9, "can_stop": True}
        out = _run_js(
            f"""
_extRenderRunNowButton("sp1", {json.dumps(_running())});
_extRenderRunNowButton("sp1", {json.dumps(idle_status)});
console.log(JSON.stringify({{
  disabled: _elements["sp-runnow-btn-sp1"].disabled,
  title: _elements["sp-runnow-btn-sp1"].title,
}}));
"""
        )
        assert out["disabled"] is False
        assert out["title"] == ""

    def test_stays_disabled_when_the_server_never_marked_extraction_ready(self):
        """A run ending must restore the SERVER's own capability gate
        (producer configured, `sharepoint.enabled`) — never blindly
        re-enable a button that was never allowed to run in the first
        place."""
        out = _run_js(
            """
_elements["sp-runnow-btn-sp1"].dataset.extractionReady = "0";
_extRenderRunNowButton("sp1", null);
console.log(JSON.stringify({ disabled: _elements["sp-runnow-btn-sp1"].disabled }));
"""
        )
        assert out["disabled"] is True

    def test_a_missing_button_element_is_a_no_op(self):
        out = _run_js(
            f"""
let threw = false;
try {{ _extRenderRunNowButton("no-such-conn", {json.dumps(_running())}); }} catch (e) {{ threw = true; }}
console.log(JSON.stringify({{ threw }}));
"""
        )
        assert out["threw"] is False
