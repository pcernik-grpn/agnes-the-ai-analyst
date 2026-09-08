"""The data-app preview directive survives every wire shape a tool result has.

Written from a real transcript: the agent called ``agnes_data_app_refresh``
and ``agnes_data_app_credentials`` mid-answer and the chat rendered
"Preview unavailable." twice, under the finished reply, with the shareable URL
nowhere on screen — while the prose around the calls read "Let me refresh
it:The preview should be refreshing now."

Two defects. The engine provider forwards kai-agent's raw MCP envelope
(``{content:[{type:"text",text:"<json>"}]}``) json.dumps'd, so the directive sat
one level below where handleFrame looked; and the preview tool call, which
renders no card, skipped the seal every other inline block performs, so the
next delta landed mid-paragraph.

Node-executed tests run the shipped functions; content assertions pin the two
call sites that are easy to undo by accident.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _read() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _preview_helpers(js: str) -> str:
    """The unwrap + directive check + error copy, sliced from the shipped file."""
    unwrap = js[js.index("function _unwrapMcpEnvelope") : js.index("function _renderToolResultPreview")]
    directive = js[js.index("const _PREVIEW_RENDER_KINDS") : js.index("function handlePreviewDirective")]
    error_copy = js[js.index("//: A raised tool's error text") : js.index("function _renderPreviewToolError")]
    return unwrap + "\n" + directive + "\n" + error_copy


REFRESH = {"render": "data_app_preview_refresh", "slug": "arr-trends"}
CREDENTIALS = {"render": "data_app_credentials", "slug": "arr-trends", "url": "/apps/arr-trends/", "password": None}
DISABLED = {"error": "data_apps_disabled", "message": "Data apps are disabled on this instance."}
RAISED = "agnes_data_app_credentials(arr-trends) failed (HTTP 404): app not found"


def _envelope(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _resolve(cases: dict) -> dict:
    js = _read()
    script = (
        _preview_helpers(js)
        + "\nconst cases = "
        + json.dumps(cases)
        + ";\nconst out = {};\nfor (const [k, raw] of Object.entries(cases)) {\n"
        + "  const r = _unwrapPreviewToolResult(raw);\n"
        + "  out[k] = { directive: _isPreviewDirective(r) ? r : null, copy: _previewErrorMessage(r) };\n"
        + "}\nprocess.stdout.write(JSON.stringify(out));\n"
    )
    return json.loads(_node_run(script))


class TestTheDirectiveIsFoundInEveryWireShape:
    def test_the_three_shapes_resolve_to_the_same_directive(self):
        res = _resolve(
            {
                "object": REFRESH,  # already parsed
                "runner_string": json.dumps(REFRESH),  # native runner: joined text blocks
                "engine_envelope": _envelope(REFRESH),  # kai-agent's raw CallToolResult
                "engine_envelope_string": json.dumps(_envelope(REFRESH)),  # …json.dumps'd by the provider
            }
        )
        for shape, got in res.items():
            assert got["directive"] == REFRESH, f"{shape}: the refresh directive was not recognised"

    def test_the_credentials_directive_keeps_its_url_through_the_envelope(self):
        """This is the one the reader was missing: 'open the app directly at
        its URL:' followed by nothing."""
        res = _resolve({"enveloped": json.dumps(_envelope(CREDENTIALS))})
        assert res["enveloped"]["directive"] == CREDENTIALS
        assert res["enveloped"]["directive"]["url"] == "/apps/arr-trends/"


class TestANonDirectiveNamesItsReason:
    def test_the_friendly_disabled_payload_reads_the_same_enveloped_or_not(self):
        res = _resolve({"plain": DISABLED, "enveloped": json.dumps(_envelope(DISABLED))})
        for shape, got in res.items():
            assert got["directive"] is None, shape
            assert got["copy"] == "Data apps are disabled on this instance.", shape

    def test_a_raised_tool_error_shows_its_own_text(self):
        """The constant hid the diagnosis. A raised ValueError reaches the
        client as its message string, not JSON — that string IS the answer."""
        res = _resolve({"raised": RAISED, "enveloped": json.dumps({"content": [{"type": "text", "text": RAISED}]})})
        for shape, got in res.items():
            assert got["directive"] is None, shape
            assert got["copy"] == RAISED, shape
            assert "Preview unavailable" not in got["copy"], shape

    def test_a_result_that_says_nothing_still_says_something(self):
        res = _resolve({"empty": "", "blank": "   ", "bare_object": {"slug": "x"}})
        for shape, got in res.items():
            assert got["copy"] == "Preview unavailable.", shape

    def test_a_wall_of_error_text_is_capped(self):
        res = _resolve({"long": "x" * 5000})
        assert len(res["long"]["copy"]) <= 601
        assert res["long"]["copy"].endswith("…")


class TestTheCallSitesArePinned:
    def test_handle_frame_resolves_the_result_before_the_directive_check(self):
        js = _read()
        case = js[js.index('case "tool_result": {') : js.index('case "assistant_message":')]
        assert "const result = _unwrapPreviewToolResult(frame.result);" in case
        assert "if (_isPreviewDirective(result))" in case
        assert "renderToolCallEnd(frame);" in case, "the generic card still gets the raw wire frame"

    def test_a_cardless_preview_tool_call_still_seals_the_streaming_bubble(self):
        """Every inline block (tool card, approval card, question card) seals
        the bubble it lands after — #1504. The preview tools render no card but
        are still a boundary; skipping the seal glued the sentence before the
        call to the one after it."""
        js = _read()
        branch = js[js.index("if (_isPreviewTool(frame.tool)) {") : js.index('if (frame.tool === "AskUserQuestion") {')]
        assert "_sealStreamingSegment();" in branch


class TestThePaneHasAColumnUnderRail:
    """The second half of the same transcript: once the directive WAS
    recognised, the pane still showed as a 0px strip — "App preview" and
    nothing under it. The rail layout collapses the shell to one grid column
    with a selector that outranks `.cloud-chat-shell.has-preview-pane`, so the
    pane (the shell's second child) fell into an implicit row of a
    `grid-template-rows: 100%` grid. The rail layout is hard-wired since Wave 0,
    so the pane needs its own rail-scoped track."""

    def _rail_pane_rule(self) -> str:
        css = CHAT_CSS.read_text(encoding="utf-8")
        start = css.index('html[data-ui-layout="rail"] .cloud-chat-shell.has-preview-pane {')
        return css[start : css.index("}", start)]

    def test_rail_gives_the_open_pane_a_grid_track(self):
        rule = self._rail_pane_rule()
        assert "grid-template-columns" in rule
        assert "--chat-preview-width" in rule, "the pane track must be sized off the same var the pane reads"
        assert "--chat-sidebar-width" not in rule, "rail renders no sidebar; a sidebar track would swallow the thread"

    def test_narrow_viewports_stack_the_pane_with_an_explicit_row(self):
        css = CHAT_CSS.read_text(encoding="utf-8")
        media = css.index(
            "@media (max-width: 900px)", css.index('html[data-ui-layout="rail"] .cloud-chat-shell.has-preview-pane {')
        )
        block = css[media : css.index("\n}\n", media)]
        assert "grid-template-rows" in block, "without an explicit second row the pane is a 0px strip again"
