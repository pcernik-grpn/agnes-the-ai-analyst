"""Wave-1 tool-rendering guards: the reader sees labels, tables and buttons —
never raw JSON or raw markdown source — and JSON stays one click away.

Follows the pattern of tests/test_chat_sources_ui.py: content assertions pin
the call shapes that are easy to undo by accident; node-executed tests run the
shipped functions, not copies.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")
WORKSPACE_CLAUDE_MD = Path("app/initial_workspace_default/CLAUDE.md")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


# ── next_actions: the trailer is chrome, not content ─────────────────────────


def test_next_actions_helper_exists_and_both_paths_use_it():
    js = _read(CHAT_JS)
    assert "function renderAnswerMarkdown" in js
    assert "stripNextActionsFence(stripSourcesFence(" in js, (
        "renderAnswerMarkdown must strip both trailers, sources first"
    )
    assert js.count("renderAnswerMarkdown(") >= 3, "renderMessage, finalizeAssistantMessage and the helper definition"
    assert "renderMarkdownSafe(stripSourcesFence(" not in js, "render paths must go through renderAnswerMarkdown now"


def test_clipboard_strips_next_actions_but_keeps_sources():
    """Suggestions are chrome — a copied transcript without them loses nothing.
    Provenance is not chrome; the sources fence stays (see
    tests/test_chat_sources_ui.py for the full rationale)."""
    js = _read(CHAT_JS)
    assert "attachMessageActions(currentAssistantArticle, stripNextActionsFence(content))" in js
    assert 'attachMessageActions(article, stripNextActionsFence(m.content || ""))' in js
    assert "attachMessageActions(currentAssistantArticle, stripSourcesFence" not in js
    assert "attachMessageActions(article, stripSourcesFence" not in js


def test_extract_next_actions_executable():
    js = _read(CHAT_JS)
    fn = js[js.index("const _NEXT_ACTIONS_OPEN_RE") : js.index("function renderAnswerMarkdown")]
    cases = {
        "with_block": "Done.\n\n```next_actions\n- Break it down by country\n- Chart the trend\n```\n",
        "no_block": "Done.",
        "unterminated_kept": "Done.\n\n```next_actions\n- Break it down",
        "two_blocks": "a\n\n```next_actions\n- x\n```\n\nb\n\n```next_actions\n- y\n```\n",
        "code_block_kept": "See:\n\n```sql\nSELECT 1\n```\n\n```next_actions\n- Next\n```\n",
        "caps_capped": "t\n\n```next_actions\n- a\n- b\n- c\n- d\n- e\n```\n",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, extractNextActions(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["with_block"]["text"] == "Done."
    assert res["with_block"]["actions"] == ["Break it down by country", "Chart the trend"]
    assert res["no_block"] == {"text": "Done.", "actions": []}
    assert res["unterminated_kept"]["text"] == "Done.\n\n```next_actions\n- Break it down", (
        "an unterminated opener is not a block — stripping it would eat the answer"
    )
    assert res["unterminated_kept"]["actions"] == []
    assert "```next_actions" not in res["two_blocks"]["text"], "the pattern must be global"
    assert res["two_blocks"]["actions"] == ["x", "y"]
    assert "```sql" in res["code_block_kept"]["text"], "an ordinary code block must survive"
    assert len(res["caps_capped"]["actions"]) == 3, "at most 3 buttons"


def test_next_action_buttons_render_and_are_singular():
    js = _read(CHAT_JS)
    assert "function renderNextActions" in js
    assert "function _clearNextActions" in js
    body = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert "textContent" in body and "innerHTML" not in body, "suggestion text is model output — textContent only"
    # finalize renders them; a new user message clears them.
    assert re.search(r"renderNextActions\(", js[js.index("function finalizeAssistantMessage") :]), (
        "finalizeAssistantMessage must render the chips"
    )
    assert js.count("_clearNextActions()") >= 2, "cleared on new turn AND before re-render"


def test_next_action_click_reuses_the_suggestion_flow():
    js = _read(CHAT_JS)
    body = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert 'dispatchEvent(new SubmitEvent("submit"' in body, (
        "clicking a chip submits like the existing suggested-prompt buttons"
    )


def test_history_reload_restores_chips_only_when_the_answer_is_the_tail():
    """Live behavior: a new user message clears the chips ("they age out the
    moment the conversation moves on"). A reload must agree — restoring them
    under an answer that already has a user message after it (error-aborted
    turn, mid-turn full_refresh) would resurrect suggestions the conversation
    moved past."""
    js = _read(CHAT_JS)
    body = js[js.index("async function loadAndRenderHistory") : js.index("async function openSession")]
    assert "renderNextActions(" in body, "a reload must restore the chips"
    assert 'lastTurnMsg.role === "assistant"' in body, (
        "chips are restored only while the newest turn message is the assistant's answer"
    )


def test_next_action_chip_styles_use_ds_tokens():
    css = _read(CHAT_CSS)
    assert ".cloud-chat-next-actions" in css
    block = css[css.index(".cloud-chat-next-action {") :]
    block = block[: block.index("}")]
    assert "var(--ds-radius-btn)" in block, (
        "a labelled button wears --ds-radius-btn — the design system reserves pill for badges"
    )
    assert "--ds-radius-pill" not in block


def test_the_prompt_mandates_the_next_actions_trailer():
    md = _read(WORKSPACE_CLAUDE_MD)
    flat = re.sub(r"\s+", " ", md)
    assert "```next_actions" in md
    assert "one-click buttons" in flat
    assert "Skip the block" in flat, "the prompt must say when NOT to emit it"


def test_the_template_carries_the_trailer_for_the_sandbox_only():
    """The LIVE sandbox prompt renders from config/claude_md_template.txt
    (app/main.py hands render_claude_md(is_sandbox=True) to WorkdirManager);
    the bundled CLAUDE.md is the fallback. Both must carry the contract, or
    the buttons never appear on a normally-deployed instance. The template's
    copy is sandbox-gated: a terminal session has nothing that lifts the
    fence, so mandating it there would put raw wire format on every answer.
    (Found by /agnes-review on the first cut of this change.)"""
    tpl = Path("config/claude_md_template.txt").read_text(encoding="utf-8")
    assert "```next_actions" in tpl, "the rendered sandbox prompt must mandate the trailer"
    section_start = tpl.index("## Offer the next step")
    guard_open = tpl.rindex("{% if is_sandbox %}", 0, section_start)
    guard_close = tpl.index("{% endif %}", section_start)
    body = tpl[guard_open:guard_close]
    assert "```next_actions" in body, "the section must sit inside an is_sandbox guard"
    assert "{% if" not in body[len("{% if is_sandbox %}") :], "no nested guard — the whole section is sandbox-only"


# ── push sinks: the trailer must never reach Slack as raw wire format ────────


def test_strip_next_actions_block_removes_the_fence_and_keeps_the_prose():
    from app.chat.sources import strip_next_actions_block

    content = "Done.\n\n```next_actions\n- Break it down by country\n```"
    assert strip_next_actions_block(content) == "Done."
    assert strip_next_actions_block("just prose") == "just prose"
    assert strip_next_actions_block("") == ""
    # An unterminated opener is not a block — same rule as the sources fence.
    half = "Done.\n\n```next_actions\n- half"
    assert strip_next_actions_block(half) == half
    # An ordinary code block survives.
    kept = "See:\n\n```sql\nSELECT 1\n```"
    assert strip_next_actions_block(kept) == kept


def test_the_slack_sink_strips_the_next_actions_trailer_on_both_post_paths():
    """Slack sessions run in the SAME sandbox as web chat
    (services/slack_bot/events.py creates them through the same ChatManager,
    whose workdir prompt mandates the trailer on every answer). The web client
    lifts the fence into buttons; Slack has no buttons wired to it, so without
    this strip every Slack reply would end in a fenced block of wire format —
    the exact failure mode the sources fence already solved there."""
    src = Path("services/slack_bot/sink.py").read_text(encoding="utf-8")
    assert src.count("strip_next_actions_block(strip_block(") == 2, (
        "both the streaming reply and the ephemeral responder must strip both trailers"
    )


# ── tool labels: a reader-facing verb, never a raw tool id ───────────────────


def test_tool_label_executable():
    js = _read(CHAT_JS)
    fn = js[js.index("const _TOOL_LABELS") : js.index("function renderApprovalRequest")]
    cases = [
        ["Bash", {"command": 'agnes query "SELECT 1"'}],
        ["Bash", {"command": "agnes catalog --json"}],
        ["Bash", {"command": "ls -la"}],
        ["Read", {"file_path": "/tmp/x"}],
        ["mcp__agnes__crm_search_accounts", {"q": "acme"}],
        ["totally_unknown_tool", {}],
        [None, None],
    ]
    script = fn + f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(([t, a]) => _toolLabel(t, a))));\n"
    res = json.loads(_node_run(script))
    assert res[0] == "Querying data"
    assert res[1] == "Reading the data catalog"
    assert res[2] == "Running a command"
    assert res[3] == "Reading a file"
    assert res[4] == "Crm search accounts", "mcp prefix stripped, words humanized"
    assert res[5] == "Totally unknown tool"
    assert res[6] == "tool"
    assert not any("mcp__" in r for r in res)


def test_live_and_history_headers_use_the_label():
    """Both paths run through _buildToolCard, so the humanized label and the
    raw-id tooltip are shared by construction — and formatToolCall keeps
    agreeing for the transcript export."""
    js = _read(CHAT_JS)
    card = js[js.index("function _buildToolCard") : js.index("function renderToolCallStart")]
    assert "_toolLabel(tool, args)" in card
    assert "name.title = tool" in card, "the raw id stays reachable as a tooltip"
    assert "_toolLabel(tc.tool, tc.args)" in js, "history formatToolCall must agree"


def test_summarize_args_shows_the_command_line():
    js = _read(CHAT_JS)
    fn = js[js.index("function _summarizeArgs") : js.index("const _TOOL_LABELS")]
    cases = {"cmd": {"command": "agnes schema hr_headcount"}, "sql": {"sql": "SELECT 1"}}
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _summarizeArgs(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["cmd"] == "agnes schema hr_headcount"
    assert res["sql"] == "SELECT 1"


# ── tool results: JSON is one click away, never the primary rendering ───────


def test_result_json_renders_directly_inside_the_collapsed_card():
    """The card itself starts collapsed — its header IS the "one click away"
    a nested Structured-result toggle used to provide. Opening the card must
    show the formatted JSON immediately; a second details inside would make
    it two clicks to see what a tool returned."""
    js = _read(CHAT_JS)
    body = js[
        js.index('wrap.className = "cloud-chat-tool-result is-json"') : js.index("function _coerceToTablePreview")
    ]
    assert "Structured result" not in body, "no nested toggle — the JSON is the body"
    assert '_jsonPanel("Result"' in body, "the JSON fallback rides the shared formatted-JSON panel"


def test_json_panel_is_highlighted_capped_and_keeps_a_full_route():
    """`language-json` pins hljs (auto-detect misreads short payloads);
    oversize payloads render capped with the whole thing one lazy toggle
    away — the same idiom as the table preview's raw-JSON route."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _jsonPanel") : js.index("function _toolCallId")]
    assert 'code.className = "language-json"' in fn
    assert "_TOOL_JSON_PREVIEW_CHARS" in fn, "a DOM cap must exist for huge payloads"
    assert '"toggle"' in fn, "the full payload is filled lazily, on first open"
    assert "enhanceCodeBlocks(" in fn, "the shared pass adds highlight + copy button"


def test_args_render_as_formatted_json_on_card_expand():
    """One click total: expanding the card shows the args as formatted JSON —
    the old nested args toggle was a second click inside a collapsed card."""
    js = _read(CHAT_JS)
    card = js[js.index("function _buildToolCard") : js.index("function renderToolCallStart")]
    assert '_jsonPanel("Args"' in card
    assert "Show args" not in js, "no nested args toggle on either path"


def test_mcp_envelope_unwraps_to_its_payload():
    """kai-agent delivers MCP results as the raw {content:[{type:"text",…}]}
    envelope; the reader got a string-in-a-string with escaped newlines.
    Unwrap to the joined text, parsed as JSON when it is JSON."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _unwrapMcpEnvelope") : js.index("function _renderToolResultPreview")]
    cases = {
        "json_payload": {"content": [{"type": "text", "text": '{"status": "ok"}'}]},
        "text_payload": {"content": [{"type": "text", "text": "plain **markdown**"}]},
        "joined": {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        "not_envelope": {"columns": ["a"], "rows": [[1]]},
        "mixed_blocks": {"content": [{"type": "image", "data": "x"}]},
        "empty_content": {"content": []},
        # handleFrame's parse is a LOCAL for the preview-directive check —
        # renderToolCallEnd gets frame.result, the raw wire string.
        "string_envelope": '{"content": [{"type": "text", "text": "{\\"status\\": \\"ok\\"}"}]}',
        "markdown_string": "| a | b |\n|---|---|\n| 1 | 2 |",
        # A JSON string that is NOT an envelope must survive as a string:
        # parsing every JSON-shaped result here would re-route unrelated
        # tools (`agnes … --json`) through the table/JSON panel, a far wider
        # behaviour change than unwrapping an envelope.
        "json_string_not_envelope": '{"rows": 3, "table": "orders"}',
        "json_scalar_string": "123",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _unwrapMcpEnvelope(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["json_payload"] == {"status": "ok"}, "JSON payload comes back parsed"
    assert res["text_payload"] == "plain **markdown**", "text payload comes back as the string"
    assert res["joined"] == "a\nb", "multiple text blocks join"
    assert res["not_envelope"] == cases["not_envelope"], "non-envelopes pass through untouched"
    assert res["mixed_blocks"] == cases["mixed_blocks"], "non-text blocks disable the unwrap"
    assert res["empty_content"] == cases["empty_content"]
    assert res["string_envelope"] == {"status": "ok"}, "a wire-string envelope unwraps all the way"
    assert res["markdown_string"] == cases["markdown_string"], "markdown strings pass through for marked"
    assert res["json_string_not_envelope"] == cases["json_string_not_envelope"], (
        "a JSON string that is not an envelope keeps its existing string rendering"
    )
    assert res["json_scalar_string"] == cases["json_scalar_string"], (
        "`123` parses as a number but is not an envelope — hand back the string"
    )


def test_show_all_rows_is_a_table_not_json():
    js = _read(CHAT_JS)
    assert "function _buildResultTable" in js
    body = js[js.index("function _coerceToTablePreview") : js.index("// ---------- Data-app split-pane preview")]
    assert "Show all rows (JSON)" not in body, "the expansion is a table now"
    assert "_TOOL_RESULT_FULL_ROWS_MAX" in body, "a DOM cap must exist for huge results"
    assert body.count("_buildResultTable(") == 2, "preview and expansion share one builder"


# ── streaming: markdown renders as it arrives ────────────────────────────────


def test_streaming_renders_markdown_not_textcontent():
    js = _read(CHAT_JS)
    body = js[js.index("function appendToken") : js.index("function finalizeAssistantMessage")]
    assert "currentAssistantBody.textContent = currentAssistantText" not in body, (
        "raw markdown source must not sit on screen until turn end"
    )
    assert "_scheduleStreamRender()" in body
    # The PER-PAINT path only: _flushStreamingTail/_resetStreamingState run
    # once at close-out and legitimately finish the bubble — the throttled
    # painter is what must stay light.
    stream = js[js.index("function _renderStreamingMarkdown") : js.index("function _flushStreamingTail")]
    assert "renderAnswerMarkdown(" in stream, "streaming and finalize must share the pipeline"
    assert "renderMermaidBlocks" not in stream, "no mermaid on partial content"
    assert "enhanceCodeBlocks" not in stream, "heavy enhancement waits for finalize"


def test_finalize_clears_the_stream_timer():
    js = _read(CHAT_JS)
    fin = js[js.index("function finalizeAssistantMessage") : js.index("// ---------- Inline tool-call blocks")]
    assert "_streamRenderTimer" in fin, "a late tick must not repaint a finalized bubble"


def test_turn_stopping_frames_flush_the_withheld_stream_tail():
    """_streamingSafeText withholds the tail after a trailing open fence while
    it streams; only a full repaint shows it. cancelled / confirmation_required
    / error may be the turn's last word — a trailing assistant_message is
    common (graceful interrupt, the watchdog's partial-save, the budget stop)
    but NOT guaranteed — so each must flush the full accumulated text itself,
    WITHOUT dropping the stream pointers: when the trailing assistant_message
    does arrive, finalize must land in this same bubble, not a duplicate."""
    js = _read(CHAT_JS)
    assert "function _flushStreamingTail" in js
    fn = js[js.index("function _flushStreamingTail") : js.index("function _resetStreamingState")]
    assert "renderAnswerMarkdown(currentAssistantText)" in fn, "full text, not the streaming-safe slice"
    assert "_streamingSafeText" not in fn, "the flush paint must not withhold anything"
    assert "currentAssistantArticle = null" not in fn, "flush must NOT reset — finalize may still be coming"
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    assert sw.count("_flushStreamingTail()") >= 3, "cancelled, confirmation_required, error"


def test_a_turn_that_never_finalizes_cannot_leak_into_the_next_bubble():
    """When NO assistant_message ever follows a stop frame (interrupt surfaced
    as an exception, hard crash), the stream pointers must still be dropped —
    or the NEXT turn's tokens append into the stale bubble after the stale
    text. `done` is the turn terminator (always after any assistant_message),
    and the next submit is the belt for a turn that never even got a done."""
    js = _read(CHAT_JS)
    reset = js[js.index("function _resetStreamingState") : js.index("function appendToken")]
    assert "_flushStreamingTail()" in reset, "reset flushes the tail first"
    assert "currentAssistantArticle = null" in reset and 'currentAssistantText = ""' in reset
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    done_case = sw[sw.index('case "done":') :]
    assert "_resetStreamingState()" in done_case[: done_case.index("break;")]
    submit = js[js.index("async function submitUserMessage") : js.index("function autosizeComposer")]
    assert "_resetStreamingState()" in submit


def test_conversation_switch_resets_the_streaming_pointers():
    """openSession wipes #chat-messages, but the stream pointers used to
    survive the switch: a pending 150 ms tick painted into a detached node,
    and a token arriving on the new socket without a submit appended the OLD
    conversation's text into an invisible bubble. The switch must drop the
    streaming state like `done` and submit do."""
    js = _read(CHAT_JS)
    body = js[js.index("async function openSession") : js.index("function handleFrame")]
    assert "_resetStreamingState()" in body


def test_reset_finalizes_an_orphan_bubble():
    """A stopped turn whose assistant_message never came still deserves a
    finished bubble: highlighted code, sortable tables, mermaid, the copy/
    actions row, chips from a completed trailer, latest-assistant marking.
    Sources chips are deliberately absent — they render the SERVER's verdict,
    and a turn that never finalized has none. Double-attach is impossible on
    the normal path: after finalize the pointers are null and reset returns
    before the tail."""
    js = _read(CHAT_JS)
    reset = js[js.index("function _resetStreamingState") : js.index("function appendToken")]
    for call in (
        "enhanceCodeBlocks(",
        "enhanceTables(",
        "renderMermaidBlocks(",
        "attachMessageActions(",
        "renderNextActions(",
        "_markLatestAssistant(",
        "maybeMakeCollapsible(",
    ):
        assert call in reset, f"orphan close-out must run the finalize tail: missing {call}"
    assert "renderSourcesChips" not in reset, "no server verdict exists for an unfinalized turn"


def test_reload_chips_sit_above_the_actions_row():
    """Live order is chips-then-actions (finalize renders chips before
    attachMessageActions appends the row). On reload, renderMessage has
    already appended .msg-actions before loadAndRenderHistory adds the chips
    — so renderNextActions must insert BEFORE an existing actions row, or the
    two paths disagree about the bubble's tail."""
    js = _read(CHAT_JS)
    body = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert ".msg-actions" in body and "insertBefore" in body


def test_over_cap_result_keeps_a_raw_json_route():
    """The capped table dropped rows past _TOOL_RESULT_FULL_ROWS_MAX with no
    route to the rest (the old JSON dump had them all). Over the cap ONLY, a
    secondary details offers the raw JSON — filled lazily on first open so a
    huge dump costs nothing until asked for, and via textContent so the
    payload can never execute."""
    js = _read(CHAT_JS)
    body = js[js.index("function _coerceToTablePreview") : js.index("// ---------- Data-app split-pane preview")]
    assert "Raw JSON (all " in body
    assert '"toggle"' in body, "the dump is built lazily, on first open"
    assert "JSON.stringify(rows" in body
    assert "total > _TOOL_RESULT_FULL_ROWS_MAX" in body, "the raw route exists only past the cap"


def test_transcript_export_keeps_the_raw_tool_id():
    """The export header is `tool: <label> (<raw id>)` — the humanized label
    reads well, but a transcript pasted into a bug report or another tool
    still needs the real id. The DOM history block keeps the id reachable as
    a tooltip instead, mirroring the live tool-block header."""
    js = _read(CHAT_JS)
    fmt = js[js.index("function formatToolCall") : js.index("async function fetchTranscriptMarkdown")]
    assert "tool: tc.tool" in fmt, "formatToolCall must carry the raw id alongside the label"
    export = js[js.index("async function fetchTranscriptMarkdown") : js.index("function wireCopyTranscript")]
    assert "${call.label} (${call.tool})" in export
    card = js[js.index("function _buildToolCard") : js.index("function renderToolCallStart")]
    assert "name.title = tool" in card, "the rendered card's tooltip carries the raw id (both paths)"


def test_streaming_safe_text_executable():
    js = _read(CHAT_JS)
    fn = js[js.index("function _streamingSafeText") : js.index("function _scheduleStreamRender")]
    cases = {
        "plain": "Hello **world**",
        "open_lang_partial": "Answer.\n\n```sour",
        "open_next_actions": "Answer.\n\n```next_actions\n- half",
        "open_sql_with_body": "Answer.\n\n```sql\nSELECT 1",
        "closed_fence": "Answer.\n\n```sql\nSELECT 1\n```\n",
        "bare_open_no_newline": "Answer.\n\n```",
        # The instant of close: the trailer's CLOSING fence just streamed in,
        # no trailing newline yet. Counting backticks naively reads that
        # closer as a fresh opener, chops there, and hands the renderer an
        # UNTERMINATED trailer — which the strip helpers deliberately keep —
        # so the wire format flashed on screen until the next repaint.
        "closed_trailer_instant": "Answer.\n\n```next_actions\n- x\n```",
        "closed_sources_instant": "Answer.\n\n```sources\ntable: orders\n```",
        "opener_after_closed_block": "```sql\nSELECT 1\n```\n\n```next_actions\n- x",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _streamingSafeText(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["plain"] == "Hello **world**"
    assert res["open_lang_partial"] == "Answer.\n\n", "a partial wire-trailer id is hidden"
    assert res["open_next_actions"] == "Answer.\n\n", "a streaming wire trailer is hidden"
    assert "SELECT 1" in res["open_sql_with_body"], "a streaming CODE block stays visible"
    assert res["closed_fence"] == cases["closed_fence"], "closed fences pass through"
    assert res["bare_open_no_newline"] == "Answer.\n\n", "a bare open fence waits for its id"
    assert res["closed_trailer_instant"] == cases["closed_trailer_instant"], (
        "a JUST-CLOSED trailer passes through whole — renderAnswerMarkdown strips a complete "
        "block; chopping at its closer leaves an unterminated one that renders raw"
    )
    assert res["closed_sources_instant"] == cases["closed_sources_instant"]
    assert res["opener_after_closed_block"] == "```sql\nSELECT 1\n```\n\n", (
        "fence parity: the withhold point is the trailer's own opener, not the last backticks"
    )


# ── turn-stopping events: visible in the transcript, not only the status bar ─


def test_confirmation_required_is_handled():
    js = _read(CHAT_JS)
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    assert 'case "confirmation_required":' in sw, (
        "the tool-budget stop used to be silently dropped — the turn just froze"
    )


def test_errors_and_cancels_reach_the_transcript():
    js = _read(CHAT_JS)
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    assert sw.count("renderSystemNote(") >= 3, "confirmation_required, error, cancelled"
    body = js[js.index("function renderSystemNote") : js.index("function renderSystemNote") + 800]
    assert "textContent" in body and ".innerHTML" not in body


def test_next_action_chips_wear_the_button_radius():
    """Design-system shape rule: pill radius is badge language; every labelled
    button wears --ds-radius-btn. (Devin Review on this PR.)"""
    css = _read(CHAT_CSS)
    rule = css[css.index(".cloud-chat-next-action {") : css.index(".cloud-chat-next-action:hover")]
    assert "var(--ds-radius-btn)" in rule
    assert "radius-pill" not in rule


def test_system_note_styles_exist():
    css = _read(CHAT_CSS)
    assert ".cloud-chat-system-note" in css


# ── Long-message collapse: a cap for extremes, not for ordinary answers ──────


def _collapse_threshold_px() -> str:
    """The one number both surfaces must agree on, as it appears in chat.js."""
    m = re.search(r"COLLAPSE_THRESHOLD_PX = (\d+)", _read(CHAT_JS))
    assert m, "COLLAPSE_THRESHOLD_PX must stay a plain literal"
    return m.group(1)


def test_collapse_threshold_and_css_clamp_agree():
    """The threshold that DECIDES to collapse is a JS constant; the max-height
    that DOES the clamping is a CSS literal. Two hardcoded pixel values that
    must stay the same number — drift means a body is judged at one height and
    cut at another (raising only the JS side would still clamp a 2500px answer
    down to 480px, i.e. exactly the bug the raise was meant to remove).

    EVERY clamp for that selector is checked, not just the first one in the
    file: chat.css already carries per-breakpoint max-height overrides for other
    elements, so a later `@media` block re-clamping .msg-body would diverge from
    the constant while a first-match-only guard kept passing."""
    threshold = _collapse_threshold_px()
    clamps = re.findall(
        r"\.msg-bubble\.is-collapsible \.msg-body \{[^}]*?max-height: (\d+)px",
        _read(CHAT_CSS),
    )
    assert clamps, "the collapsible clamp must keep a literal max-height"
    assert set(clamps) == {threshold}, (
        f"chat.js collapses over {threshold}px but chat.css clamps at {sorted(set(clamps))}px"
    )


def test_collapse_threshold_is_actually_consumed_by_the_collapse_decision():
    """Both numbers can agree while the constant is dead: hardcoding the
    comparison inline (`if (body.scrollHeight <= 480) return;`) leaves the
    declaration and the CSS literal untouched, so a pure agreement guard stays
    green while the runtime regresses to exactly the reported bug. Pin that the
    decision reads the constant, and that no bare pixel literal is compared
    against the measured height."""
    js = _read(CHAT_JS)
    body = js[js.index("function maybeMakeCollapsible") : js.index("function enhanceCodeBlocks")]
    assert "COLLAPSE_THRESHOLD_PX" in body, "the collapse decision must read the constant, not a literal"
    stray = re.search(r"scrollHeight\s*[<>]=?\s*\d", body)
    assert not stray, f"scrollHeight compared against a literal: {stray.group(0)!r}"


def test_every_finish_path_offers_the_collapse():
    """maybeMakeCollapsible is what puts the cap on a finished turn, and there
    are three ways a turn finishes: the normal completed answer, an orphan whose
    assistant_message never came, and a reload rendering history. Only the
    orphan path was pinned (test_reset_finalizes_an_orphan_bubble), so dropping
    the call from the normal path — the one every real answer takes — would ship
    silently green."""
    js = _read(CHAT_JS)
    for fn, end in (
        ("function finalizeAssistantMessage", "function _toolCallId"),
        ("function renderMessage", "function enhanceTables"),
    ):
        body = js[js.index(fn) : js.index(end)]
        assert "maybeMakeCollapsible(" in body, f"{fn} must offer the collapse"


def test_collapse_cap_clears_an_ordinary_long_answer():
    """The collapse fires at FINALIZE, so the reader watches a message stream in
    full and then sees it snap shut. At 480px (~20 lines) that hit nearly every
    real answer. The cap is kept for genuine extremes only, so its floor must
    stay far above an ordinary answer's height."""
    threshold = int(re.search(r"COLLAPSE_THRESHOLD_PX = (\d+)", _read(CHAT_JS)).group(1))
    assert threshold >= 2000, f"COLLAPSE_THRESHOLD_PX={threshold}px collapses ordinary answers; the cap is for extremes"


# ── tool cards: header line by default, one click to the JSON ───────────────
# A tool card is a <details> collapsed to its header line (status, name, args
# summary, timing); expanding shows the formatted-JSON args and result. A
# failed call opens itself. Any card expanded during a turn folds back the
# moment that turn ends.


def test_tool_call_card_is_a_details_element_collapsed_by_default():
    js = _read(CHAT_JS)
    card = js[js.index("function _buildToolCard") : js.index("function renderToolCallStart")]
    assert 'document.createElement("details")' in card, "the whole card must be collapsible, not just its nested panels"
    assert 'document.createElement("summary")' in card, "the header becomes the <details>'s native toggle"
    # Collapsed by default: the ONLY thing that opens a card on construction is
    # a failed call, whose output is the diagnosis nobody knows to click for.
    assert "if (wrapIsError) wrap.open = true" in card
    assert card.count("wrap.open = true") == 1, (
        "cards start as just the header line — the name is the toggle (user ask on #1504 follow-up)"
    )
    start = js[js.index("function renderToolCallStart") : js.index("function renderToolCallEnd")]
    assert "wrap.open = true" not in start
    assert "_currentTurnToolCards.push(wrap)" in start, (
        "tracked so every card opened this turn folds together at turn end"
    )
    end = js[js.index("function renderToolCallEnd") : js.index("function _collapseFinishedToolCalls")]
    assert "if (isError) wrap.open = true" in end, (
        "a FAILED card must open itself — its output is the diagnosis nobody knows to click for"
    )


def test_collapse_finished_tool_calls_folds_and_clears_the_turn_list():
    js = _read(CHAT_JS)
    assert "function _collapseFinishedToolCalls" in js
    fn = js[js.index("function _collapseFinishedToolCalls") : js.index("function _looksLikeToolError")]
    assert "wrap.open = false" in fn
    assert "_currentTurnToolCards = []" in fn, "a card belongs to exactly one turn's collapse pass"


def test_every_turn_terminal_frame_collapses_this_turns_tool_cards():
    """done is the normal path, but cancelled/error/confirmation_required also
    stop the turn — a card left permanently expanded under a note instead of
    an answer is the same clutter this feature exists to avoid."""
    js = _read(CHAT_JS)
    sw = js[js.index("switch (frame.type)") : js.index("function applySessionRename")]
    for case in ('case "done":', 'case "cancelled":', 'case "confirmation_required":', 'case "error":'):
        start = sw.index(case)
        block = sw[start : sw.index("break;", start)]
        assert "_collapseFinishedToolCalls()" in block, f"{case} must collapse this turn's tool cards"


def test_tool_head_summary_gets_pointer_cursor_scoped_to_the_real_toggle():
    """.cloud-chat-tool-head is also reused (on a plain <div>) by the
    approval/question cards, which are not collapsible — a bare-class cursor
    rule would paint a false affordance on those too. The rule must be
    scoped to the actual <summary>."""
    css = _read(CHAT_CSS)
    assert "summary.cloud-chat-tool-head" in css
    assert re.search(r"(?<!summary)\.cloud-chat-tool-head\s*\{[^}]*cursor:\s*pointer", css) is None


def test_a_failed_tool_card_is_not_folded_shut():
    """renderToolCallEnd marks a failed card `is-error` — red border, warning
    icon — because its output is the thing the reader needs. Folding it is
    worst on the `error` terminal frame: the turn died mid-tool and the card
    that explains why would go behind a click nobody knows to make."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _collapseFinishedToolCalls") :]
    fn = fn[: fn.index("\n}")]
    assert "is-error" in fn, "a failed card's output must survive the fold"


def test_no_bare_details_box_rule_can_flatten_a_tool_card():
    """A tool card IS a <details> — in the stream AND inside a bubble on the
    reload path — so ANY box rule on a bare `details` descendant of
    .cloud-chat-messages (or of .msg-bubble) outranks the whole
    .cloud-chat-tool* family (0,1,1 vs 0,1,0) and flattens the cards:
    horizontal margin 0, 1px border instead of the 3px status edge, cramped
    padding. That was the #1504 styling regression. The legacy flat block
    that needed such a rule is gone — both paths build the same card. Only
    the code-block rules may stay broad: they paint the highlighted JSON
    INSIDE the cards."""
    css = re.sub(r"/\*.*?\*/", "", _read(CHAT_CSS), flags=re.S)
    for rule in re.finditer(r"\.(?:cloud-chat-messages|msg-bubble)\s+(?:\.msg-bubble\s+)?details([^{]*)\{", css):
        rest = rule.group(1)
        assert "code" in rest or "pre.code-block-wrap" in rest, (
            f"box rule on a bare descendant `details` ({rest.strip()!r}) reaches the "
            "tool cards and flattens them — target .cloud-chat-tool* instead (#1504)"
        )


def test_both_paths_build_the_same_tool_card():
    """A refresh used to downgrade an answer's evidence to a flat grey
    `tool: <label>` box — same information, none of the design. Live and
    reload must build the card through one shared constructor."""
    js = _read(CHAT_JS)
    assert "function _buildToolCard" in js
    start = js[js.index("function renderToolCallStart") : js.index("function renderToolCallEnd")]
    assert "_buildToolCard({" in start, "the live path must use the shared card"
    hist = js[js.index("function renderMessage") : js.index("function enhanceTables")]
    assert "_buildToolCard({" in hist, "the reload path must use the SAME card, not a bespoke details"
    assert "summary.textContent = `tool: " not in js, "the flat legacy block is gone"


def test_a_replayed_card_shows_the_outcome_the_record_actually_carries():
    """Since schema v123 the persisted part carries `state` / `result` /
    `is_error`, so a reloaded card renders the REAL icon, status edge and
    result body — that is the whole point of storing parts, and it is what
    makes live and reload one component rather than two that resemble each
    other. It routes the result through the same `_renderToolResultPreview`
    the live path uses, so a table is a table and an MCP envelope is unwrapped
    on both. A part with NO state (a pre-v123 row) still claims nothing."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _buildToolCard") : js.index("function renderToolCallStart")]
    assert "_renderToolResultPreview(result)" in fn, "one result renderer for both paths"
    assert 'state === "output-error"' in fn and 'state === "output-available"' in fn, (
        "the persisted state maps onto the same is-error / is-done classes a live result produces"
    )
    assert 'icon.textContent = "✓"' in fn and 'icon.textContent = "⚠"' in fn
    # Neutral only when there is genuinely nothing to report: the fallback
    # class, and an icon appended only when it has content.
    assert 'let statusClass = "is-replayed"' in fn
    assert "if (icon.textContent) head.appendChild(icon)" in fn, (
        "a stateless (pre-v123) part must not get an empty icon slot"
    )
    css = re.sub(r"/\*.*?\*/", "", _read(CHAT_CSS), flags=re.S)
    replayed = css[css.index(".cloud-chat-tool.is-replayed") :]
    replayed = replayed[: replayed.index("}")]
    assert "accent-success" not in replayed and "accent-info" not in replayed, (
        "the stateless fallback's edge must stay neutral"
    )
    # Status only. Layout overrides here mean the card is being nested
    # somewhere with different geometry than the live stream's — which is how
    # replayed cards ended up as wide as each message's longest line.
    for prop in ("margin", "max-width", "width"):
        assert prop not in replayed, (
            f"`{prop}` on .is-replayed — a replayed card must inherit the live card's "
            "geometry by being appended in the same place, not by overriding it"
        )


def test_replayed_cards_are_siblings_in_the_messages_column():
    """`.msg-bubble` is `flex: 0 1 auto; max-width: 80%` — sized by its widest
    line — so a card nested inside one cannot match the live card's reading-
    column width, and two answers of different lengths produced two different
    card widths. Append them where the live stream appends its own."""
    js = _read(CHAT_JS)
    body = js[js.index("function renderMessage") : js.index("// ---------- Result table enhancement")]
    assert "bubble.appendChild(_buildToolCard(" not in body, "a card inside the bubble inherits the bubble's width"
    assert 'for (const el of _buildHistoryTail(parts, m)) $("chat-messages").appendChild(el)' in body
    # The collapse measures the answer, and the tail is no longer part of it.
    assert body.index("maybeMakeCollapsible(article)") < body.index("_buildHistoryTail(parts, m)")


def test_history_renders_parts_in_order_and_degrades_for_pre_v123_rows():
    """`m.parts` is the turn's sequence (schema v123), so a reload walks it and
    puts each tool card back where it ran — closing the half of #1504 that a
    client-side fix never could. A row without parts keeps the only honest
    fallback: the answer, then its positionless tool calls after it, because
    the ordering those rows lost is not in the data to recover."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _buildHistoryTail") : js.index("function _buildContinuationBubble")]
    # The parts walk handles both kinds, in one pass, in array order.
    assert 'part.type === "text"' in fn and 'part.type === "tool"' in fn
    assert "for (const part of parts)" in fn, "array order IS the turn order — no sorting, no bucketing"
    # State rides through to the card rather than being recomputed.
    assert "state: part.state" in fn and "isError: part.is_error === true" in fn
    # Legacy branch: guarded on the absence of parts, and still skips the
    # nameless cancelled/interrupted markers.
    assert "if (!parts) {" in fn
    assert "if (!formatToolCall(tc)) continue;" in fn


def test_the_tool_card_comment_does_not_claim_a_persisted_record():
    """Cards are built only from live `tool_call` frames; loadAndRenderHistory
    replays messages, not tool calls, so a reload leaves no card at all. The
    header line is the trail for the session, not a permanent record."""
    js = _read(CHAT_JS)
    assert "as the permanent record" not in js


# ── live segmentation: blocks render AT their position in the turn (#1504) ──
# Tokens used to keep appending into the one pre-block bubble, so a turn that
# went text → tool → text displayed as [all text][all blocks]. Now every
# inline block (tool card, approval card, question card) SEALS the streaming
# bubble; the next token opens a fresh bubble below the block, and finalize
# renders only the text after the last seal.


def test_inline_blocks_seal_the_streaming_bubble():
    js = _read(CHAT_JS)
    assert "function _sealStreamingSegment" in js
    for fn, end in (
        ("function renderToolCallStart", "function renderToolCallEnd"),
        ("function renderApprovalRequest", "function resolveApprovalCard"),
        ("function renderQuestionRequest", "function resolveQuestionCard"),
    ):
        body = js[js.index(fn) : js.index(end)]
        assert "_sealStreamingSegment()" in body, f"{fn} appends an inline block — it must seal first"


def test_seal_concatenates_exactly_and_skips_the_heavy_tail():
    """The sealed prefix must be the PLAIN join of the streamed deltas — the
    finalize subtraction relies on it — and a sealed bubble gets only the
    light finish: actions row, chips and latest-marking belong to the turn's
    LAST bubble."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _sealStreamingSegment") : js.index("function appendToken")]
    assert "_turnSealedText += currentAssistantText" in fn, "exact concatenation, no separator"
    assert "attachMessageActions" not in fn
    assert "renderSourcesChips" not in fn
    assert "_markLatestAssistant" not in fn


def test_a_segmented_turn_paints_the_local_tail_not_a_slice_of_server_content():
    """`assistant_message.content` is NOT the concatenation of the streamed
    deltas: the engine provider builds it as `"\\n\\n".join(part.strip() …)`
    over text parts (`_TurnState.text`) and the native runner consolidates
    TextBlocks the same way. So reconstructing the final bubble by subtracting
    a "sealed prefix" from `content` only works on a turn with ONE text part;
    on a real multi-part turn the match misses, and any fallback that then
    re-renders `content` whole puts all the text back below all the cards —
    the exact bug the segmentation exists to fix. Display therefore comes from
    the deltas the client actually showed; `content` stays the RECORD."""
    js = _read(CHAT_JS)
    fin = js[js.index("function finalizeAssistantMessage") : js.index("// ---------- Inline tool-call blocks")]
    assert "startsWith(_turnSealedText)" not in fin, "no prefix arithmetic against server content"
    assert "_turnSealedText.length" not in fin, "no slicing of server content"
    assert "segmented ? currentAssistantText : content" in fin, (
        "segmented turns paint the local tail; unsegmented ones the authoritative content"
    )
    assert "attachMessageActions(currentAssistantArticle, stripNextActionsFence(content))" in fin, (
        "the copy row hands over the WHOLE answer, not the tail the bubble shows"
    )


def test_the_trailing_segment_finish_path_also_caps_a_long_answer():
    """A turn that ends on a tool/approval/question card takes the early
    return, which attaches chips and the copy row to the last sealed bubble.
    Every other finish path caps an over-long answer; this one must too."""
    js = _read(CHAT_JS)
    fin = js[js.index("function finalizeAssistantMessage") : js.index("// ---------- Inline tool-call blocks")]
    early = fin[
        fin.index("if (!currentAssistantArticle && !tail.trim()") : fin.index(
            '_turnSealedText = "";\n  _turnSealedArticles = [];\n  if ('
        )
    ]
    assert "maybeMakeCollapsible(article)" in early


def test_reset_clears_the_seal_bookkeeping():
    """An orphan turn keeps its sealed bubbles on screen, but the NEXT turn's
    finalize must not subtract this turn's text."""
    js = _read(CHAT_JS)
    reset = js[js.index("function _resetStreamingState") : js.index("function _sealStreamingSegment")]
    assert '_turnSealedText = ""' in reset
    assert "_turnSealedArticles = []" in reset
