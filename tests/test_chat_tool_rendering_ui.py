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
    assert 'attachMessageActions(tailArticle, stripNextActionsFence(m.content || ""))' in js
    assert "attachMessageActions(currentAssistantArticle, stripSourcesFence" not in js
    assert "attachMessageActions(tailArticle, stripSourcesFence" not in js


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
    assert "Omit it in exactly two cases" in flat, "the prompt must say when NOT to emit it"


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
        # Track C7 (@delegation MVP) — the in-sandbox SDK tool
        # (app/chat/runner.py::_delegation_mcp_server) names itself this way.
        ["mcp__agnes-delegation__delegate_to_agent", {"agent_slug": "b-agent", "message": "hi"}],
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
    assert res[5] == "Delegating to another agent"
    assert res[6] == "Totally unknown tool"
    assert res[7] == "tool"
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


def test_a_collapsed_step_carries_no_args_only_an_outcome():
    """A collapsed step is one quiet line: a verb and an outcome. The whole
    command used to sit on it, which made it the single longest thing in a
    settled transcript — `agnes query "SELECT sum(total) FROM orders"` beside
    every row of a six-step run is most of what made the trail feel crowded.
    The args moved into the body, one click away.

    The summary ELEMENT stays, because a failed call writes its diagnosis there
    (_setToolCardError): on an error, what went wrong is the one thing worth a
    collapsed line, and it is now the only thing this slot ever holds. The
    summarizer that used to fill it is gone rather than left unused."""
    js = _read(CHAT_JS)
    assert "_summarizeArgs" not in js, "dead once the header stopped showing args — deleted, not orphaned"

    card = js[js.index("function _buildToolCard") : js.index("function _toolErrorLine")]
    assert 'summary.className = "cloud-chat-tool-summary"' in card, "the slot survives for the error line"
    assert "summary.textContent" not in card, "nothing but the error line may fill it"

    err = js[js.index("function _setToolCardError") : js.index("// ---------- Tool-call groups")]
    assert "summary.textContent = line;" in err, "the diagnosis is what the slot is for now"


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


def test_args_render_on_card_expand_with_no_nested_toggle():
    """One click total: expanding the card shows the args — the old nested args
    toggle was a second click inside a collapsed card."""
    js = _read(CHAT_JS)
    card = js[js.index("function _buildToolCard") : js.index("function renderToolCallStart")]
    assert "_argsPanels(args)" in card
    assert "Show args" not in js, "no nested args toggle on either path"


def test_a_command_arg_is_a_code_block_not_escaped_json():
    """`{"command": "agnes query \\"SELECT * FROM orders\\""}` made the reader
    undo the escaping in their head to reach the SQL they opened the card for.
    A command / SQL arg renders as a code block in its own language; anything
    LEFT OVER still gets the JSON panel, so no arg drops out of the record."""
    js = _read(CHAT_JS)
    fn = js[js.index("const _ARG_LANGUAGES") : js.index("function _toolCallId")]
    assert '{ command: ["Command", "bash"], sql: ["SQL", "sql"] }' in js, (
        "the two language args, each with the language to highlight it as"
    )
    assert "_codePanel(label, value, language" in fn, "a language arg goes to the code panel"
    assert '_jsonPanel(panels.length ? "Other args" : "Args", rest' in fn, (
        "leftovers keep the JSON panel, and a call with no language arg is unchanged"
    )
    code = js[js.index("function _codePanel") : js.index("const _ARG_LANGUAGES")]
    assert "code.textContent = text" in code, "the command text is set verbatim — never re-escaped"
    assert "enhanceCodeBlocks(" in code, "the shared pass adds the copy button"


def test_a_language_arg_is_only_taken_when_it_is_a_non_empty_string():
    """`{command: ""}` / `{command: {…}}` must fall back to the JSON panel
    rather than render an empty or `[object Object]` code block."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _argsPanels") : js.index("function _toolCallId")]
    assert 'typeof value !== "string" || value.trim() === ""' in fn


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
        # A JSON string that is NOT an envelope must survive as a string
        # THROUGH THIS FUNCTION: unwrapping is about envelopes only. The
        # decision to parse `agnes … --json` output lives one layer out, in
        # _tabularToolResult, and is narrowed to strings that coerce to a
        # table — see test_a_tabular_result_that_arrived_as_text_still_
        # becomes_a_table.
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


def test_a_tabular_result_that_arrived_as_text_still_becomes_a_table():
    """The path most turns actually take: the agent reaches data through `agnes
    query` over **Bash**, whose stdout is a string, so the object-shaped check
    never saw it and a 400-row answer rendered as one line of JSON in a
    paragraph. The preview now tests the payload AS AN OBJECT, parsing a string
    first — through the SAME `_asToolResultObject` the fact_claims route uses,
    not a second near-copy of it.

    Narrow by construction: parsing decides nothing on its own. Only a payload
    that ALSO coerces to a table renders differently, so a markdown table, CLI
    prose and a JSON string of any other shape keep what they had."""
    js = _read(CHAT_JS)
    preview = js[
        js.index("function _renderToolResultPreview") : js.index(
            "/** Console output whose alignment carries the meaning"
        )
    ]
    assert "const parsed = _asToolResultObject(result);" in preview, "reuse the existing parser, do not clone it"
    assert "const table = payload ? _coerceToTablePreview(payload) : null;" in preview, (
        "the table decision — and the whole behaviour change — stays with _coerceToTablePreview"
    )
    assert "_appendSemanticValidationNotice(table, payload)" in preview, (
        "the notice reads the PARSED payload, not the string it arrived as"
    )
    assert "JSON.parse(result)" not in preview, (
        "one parse per result: the string branch's notice reads the same `payload`"
    )

    # The parse itself, executed: `_asToolResultObject` composed with the
    # narrowing at the call site must accept the two real query shapes and
    # reject everything the string path still owns.
    fn = js[js.index("function _unwrapMcpEnvelope") : js.index("/** Build the preview block")]
    cases = {
        # `agnes query --json` prints exactly this
        "json_string_array": json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}]),
        # `POST /api/query` answers exactly this
        "json_string_colrows": json.dumps({"columns": ["c"], "rows": [[1]]}),
        "object": {"columns": ["c"], "rows": [[1]]},
        "markdown_table": "| a | b |\n|---|---|\n| 1 | 2 |",
        "cli_prose": "no rows found",
        "json_scalar_string": "123",
        "number": 42,
    }
    script = (
        fn
        + "\nconst narrow = (v) => { const p = _asToolResultObject(v);"
        + ' return p && typeof p === "object" ? p : null; };\n'
        + f"process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)})"
        + ".map(([k, v]) => [k, narrow(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["json_string_array"] == [{"a": 1, "b": 2}, {"a": 3, "b": 4}], "parsed, so it can reach the table"
    assert res["json_string_colrows"] == {"columns": ["c"], "rows": [[1]]}
    assert res["object"] == cases["object"], "an object result is handed straight through"
    assert res["markdown_table"] is None, "marked keeps markdown tables"
    assert res["cli_prose"] is None
    assert res["json_scalar_string"] is None, "`123` parses, but a number is not a table"
    assert res["number"] is None


def test_console_output_keeps_its_alignment():
    """`agnes query` defaults to `--format table` — a rich box table — and
    `agnes catalog`/`describe` print the same way. Run through marked it came
    out as one mangled paragraph: newlines collapsed, and the column alignment
    IS the content. Box-drawing characters route it to a <pre> instead.

    Deliberately not a pipe/dash test: a markdown table is pipes and dashes
    too, and marked should keep that one."""
    js = _read(CHAT_JS)
    assert "const _CONSOLE_TABLE_RE = /[\\u2500-\\u257F]/;" in js, "box-drawing range, nothing wider"
    script = (
        js[js.index("const _CONSOLE_TABLE_RE") : js.index("/** Build the preview block")]
        + "const cases = {"
        + '  rich_table: "\\u250f\\u2501\\u2513\\n\\u2503 a \\u2503",'
        + '  record_view: "\\u2500\\u2500\\u2500 row 1 \\u2500\\u2500\\u2500\\n  id : 1",'
        + '  markdown_table: "| a | b |\\n|---|---|\\n| 1 | 2 |",'
        + '  ascii_plus_table: "+---+\\n| a |\\n+---+",'
        + '  prose: "no rows found",'
        + "};"
        + "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(cases)"
        + ".map(([k, v]) => [k, _CONSOLE_TABLE_RE.test(v)]))));"
    )
    res = json.loads(_node_run(script))
    assert res["rich_table"] is True
    assert res["record_view"] is True, "the psql-style wide-table fallback is aligned output too"
    assert res["markdown_table"] is False, "marked keeps markdown tables"
    assert res["ascii_plus_table"] is False, "+---+ is ambiguous with markdown — left on the markdown path"
    assert res["prose"] is False

    fn = js[js.index("function _renderConsoleTableResult") : js.index("/** Append a small advisory note")]
    css = _read(CHAT_CSS)
    assert "pre.textContent =" in fn, "plain text — no markdown, no highlighting"
    assert "_expandInPlace" not in fn, (
        "no line cap and no 'show all' step: the block is already bounded on screen by its "
        "own max-height, so capping the content too only put it behind a click"
    )
    assert fn.count('className = "cloud-chat-tool-console"') == 1, "one <pre>, holding all of it"

    # Bounded by the scroll region rather than by a content cap — which is the
    # whole reason the cap could go.
    console_css = css[css.index(".cloud-chat-tool-console {") :]
    console_css = console_css[: console_css.index("}")]
    assert "max-height:" in console_css and "overflow: auto;" in console_css


def test_the_console_pre_does_not_reflow():
    """A wrapped column is a broken column: the <pre> scrolls sideways."""
    css = _read(CHAT_CSS)
    rule = css[
        css.index(".cloud-chat-tool-console {") : css.index(".cloud-chat-tool-result-full .cloud-chat-tool-console")
    ]
    assert "white-space: pre;" in rule, "never pre-wrap — that destroys the alignment"
    assert "overflow: auto;" in rule


def test_a_tool_card_code_panel_wears_the_card_surface():
    """The panel rules ask for `background: transparent` over the panel's own
    --ds-surface, but `.cloud-chat-messages details pre.code-block-wrap`
    outranks them (0,2,2 vs 0,1,1) and repainted every panel --ds-code-bg — a
    dark slab dropped into a light card, for what is usually one line of shell.
    The correcting selectors must keep out-ranking it, and must NOT drag the
    --ds-code-* tokens onto that surface: every value in that family is tuned
    against the near-black --ds-code-bg."""
    css = _read(CHAT_CSS)
    assert ".cloud-chat-messages .cloud-chat-tool pre.code-block-wrap {" in css, (
        "three classes, so the card's own intent wins the cascade"
    )
    rule = css[
        css.index(".cloud-chat-messages .cloud-chat-tool pre.code-block-wrap {") : css.index(
            ".cloud-chat-tool-console {"
        )
    ]
    assert "background: var(--ds-surface-dim);" in rule, (
        "a quiet tint sized to the code — not the transcript's slab, and not nothing "
        "at all: the hover copy button needs somewhere to sit"
    )
    assert "--ds-code-bg" not in rule, "the dark code surface must not follow the panel"
    # Geometry as well as paint. The shared rule's --space-3 padding and
    # --space-2 margin used to nest inside the panel's own, which is most of
    # why one line of shell painted a box five lines tall.
    assert "margin: 0;" in rule, "no nested margin — the panel owns the spacing"
    assert "display: inline-block;" in rule and "max-width: 100%;" in rule, (
        "the tint sizes to the code, not to the reading column, and still stops at it"
    )
    assert '[class*="hljs-"]' in rule and "color: inherit;" in rule, (
        "hljs token colours fall back to the card's ink instead of being remapped"
    )


def test_show_all_rows_is_a_table_not_json():
    js = _read(CHAT_JS)
    assert "function _buildResultTable" in js
    body = js[js.index("function _coerceToTablePreview") : js.index("// ---------- Data-app split-pane preview")]
    assert "Show all rows (JSON)" not in body, "the expansion is a table now"
    assert "_TOOL_RESULT_FULL_ROWS_MAX" in body, "a DOM cap must exist for huge results"
    assert "tableWrap.replaceChildren(" in body, (
        "the expansion replaces the preview's rows in the SAME wrapper rather than appending a second table under it"
    )
    assert "fullWrap" not in body, "a second table wrapper is the duplication this replaced"


def test_expanding_a_capped_preview_grows_it_instead_of_copying_it():
    """The old shape rendered the preview and then, behind a toggle, a second
    copy of the same payload starting over from its first row — so "Show full
    output" on a 22-line listing showed lines 1-12, then lines 1-22 underneath,
    the first twelve of them twice. Both the console preview and the table
    preview did it. There is one payload, so there is one element.

    Also strictly cheaper: nothing past the preview is built until asked for,
    where the table's full copy used to be built eagerly at construction."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _expandInPlace") : js.index("/** Console output whose alignment carries the meaning")]
    assert "paint(expanded)" in fn, "one element, repainted — not two rendered"
    assert 'setAttribute("aria-expanded"' in fn, "a control that is not a <details> must say so itself"
    assert "metaEl.hidden = !line" in fn, (
        "expanded, 'Showing 12 of 22' is stale — it reports the cap only while one applies"
    )

    # The table is the caller that still needs it. A long console dump is one
    # text node and simply scrolls (test_console_output_keeps_its_alignment);
    # a long table is hundreds of elements, so it still earns a cap.
    table = js[js.index("function _coerceToTablePreview") : js.index("// ---------- Data-app split-pane preview")]
    assert "_expandInPlace({" in table
    # The over-cap raw-JSON route IS additional content (the rows the table
    # drops), so it stays a real disclosure.
    assert 'rawDet.className = "cloud-chat-tool-result-full"' in table, (
        "past the cap the dropped rows must still be reachable"
    )


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
    # The LIGHT table pass is the exception (TCRD-288): marked's bare <table>
    # has no border, no padding and a centered bold header, so without it a
    # streamed table looked like a different widget until finalize.
    assert "enhanceTables(currentAssistantBody)" in stream, "a streamed table is styled while it grows"
    flush = js[js.index("function _flushStreamingTail") : js.index("function _resetStreamingState")]
    assert "enhanceTables(currentAssistantBody)" in flush, (
        "the full-text flush replaces the DOM — a cancelled/error frame must not un-style the table"
    )


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


def test_streaming_safe_text_withholds_a_table_head_until_its_delimiter_row():
    """TCRD-288. Marked renders a GFM table only once the delimiter row has one
    cell per header cell; until then the header line, and then the growing
    delimiter row, come out as a paragraph of raw pipes. The painter withholds
    exactly that window — a lone pipe line with no table row above it, or that
    header plus a delimiter still streaming — and releases it at the newline
    that ends the delimiter row. A data row is never withheld: a table already
    on screen keeps growing row by row. Runs the SHIPPED helper under node."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _streamingSafeText") : js.index("function _scheduleStreamRender")]
    cases = {
        "header_alone": "Intro:\n\n| Name | Rev |",
        "header_then_newline": "Intro:\n\n| Name | Rev |\n",
        "delimiter_partial": "Intro:\n\n| Name | Rev |\n|---|-",
        "delimiter_complete_no_newline": "Intro:\n\n| Name | Rev |\n|---|---|",
        "delimiter_done": "Intro:\n\n| Name | Rev |\n|---|---|\n",
        "row_partial": "Intro:\n\n| Name | Rev |\n|---|---|\n| Ac",
        "rows": "Intro:\n\n| Name | Rev |\n|---|---|\n| Acme | 1 |\n| Bob | 2 |",
        "dash_only_data_row": "| a |\n|---|\n| - |",
        "header_interrupting_a_paragraph": "Intro:\n| Name | Rev |",
        "pipe_in_prose": "Use a | b here",
        "pipe_inside_open_code": "```sql\nSELECT a | b",
        "head_after_closed_code": "```sql\nSELECT 1\n```\n\n| a | b |",
        "open_trailer_after_table": "| a | b |\n|---|---|\n| 1 | 2 |\n\n```next_actions\n- x",
        # Prose that opens with a pipe is indistinguishable from a header's
        # first cell until either a second pipe or the newline arrives — so it
        # is withheld while short, and released once it has run longer than
        # any first cell could (the bound caps the cost of the false positive).
        "pipe_prose_short": "Note:\n| a note that starts with a pipe",
        "pipe_prose_long": "Note:\n| " + "a note that starts with a pipe and keeps going " * 3,
        "long_first_cell_then_pipe": "Note:\n| " + "x" * 90 + " | Rev",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _streamingSafeText(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["pipe_prose_short"] == "Note:\n", "a short lone pipe line could still be a header's first cell"
    assert res["pipe_prose_long"] == cases["pipe_prose_long"], (
        "a lone pipe line past the first-cell bound with no second pipe is prose — released"
    )
    assert res["long_first_cell_then_pipe"] == "Note:\n", "a second pipe makes it a header candidate again"
    assert res["header_alone"] == "Intro:\n\n", "a header with no delimiter yet is a paragraph of pipes — hidden"
    assert res["header_then_newline"] == "Intro:\n\n"
    assert res["delimiter_partial"] == "Intro:\n\n", "the delimiter row is still streaming"
    assert res["delimiter_complete_no_newline"] == "Intro:\n\n", "released at the delimiter's newline"
    assert res["delimiter_done"] == cases["delimiter_done"], "head complete — marked renders a table from here"
    assert res["row_partial"] == cases["row_partial"], "a data row is never withheld: the table grows live"
    assert res["rows"] == cases["rows"]
    assert res["dash_only_data_row"] == cases["dash_only_data_row"], (
        "a `| - |` DATA row under a real head is a row, not a delimiter in progress"
    )
    assert res["header_interrupting_a_paragraph"] == "Intro:\n", "GFM lets a table interrupt a paragraph"
    assert res["pipe_in_prose"] == cases["pipe_in_prose"], "only a line that OPENS with a pipe is a table row"
    assert res["pipe_inside_open_code"] == cases["pipe_inside_open_code"], "inside an open code fence a pipe is code"
    assert res["head_after_closed_code"] == "```sql\nSELECT 1\n```\n\n", "fence parity first, then the table head"
    assert res["open_trailer_after_table"] == "| a | b |\n|---|---|\n| 1 | 2 |\n\n", (
        "a finished table stays; the open trailer after it is withheld as before"
    )


def test_a_table_streamed_token_by_token_never_renders_as_raw_pipes():
    """The property the helper exists for, checked against the SHIPPED marked
    build rather than a re-statement of its rules: replay a model-written
    table one character at a time through `_streamingSafeText` and parse
    every prefix — no prefix may yield a paragraph that opens with a pipe,
    and the table must appear the moment the delimiter row's newline lands
    (not at the end of the turn). Fails on the pre-fix painter, which handed
    marked the raw prefix."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _streamingSafeText") : js.index("function _scheduleStreamRender")]
    marked = Path("app/web/static/vendor/marked.min.js").resolve()
    full = (
        "Here are the numbers:\n\n"
        "| Client | Sponsor (PE parent) | Price |\n"
        "|---|---|---|\n"
        "| **Northwind Parts** | Alpine Capital | $47,500 |\n"
        "| Harbor Supply | Meridian Partners | $125,000 |\n\n"
        "Both are closed won."
    )
    head_done = full.index("|---|---|---|\n") + len("|---|---|---|\n")
    script = (
        fn
        + f"\nconst marked = require({json.dumps(str(marked))});\n"
        + f"const full = {json.dumps(full)};\n"
        + "const rawPipe = [], table = [];\n"
        + "for (let i = 1; i <= full.length; i++) {\n"
        + "  const html = marked.parse(_streamingSafeText(full.slice(0, i)));\n"
        + "  if (/<p>\\s*\\|/.test(html)) rawPipe.push(i);\n"
        + "  if (html.includes('<table')) table.push(i);\n"
        + "}\n"
        + "process.stdout.write(JSON.stringify({rawPipe, firstTable: table.length ? table[0] : null, "
        + "tableCount: table.length}));\n"
    )
    res = json.loads(_node_run(script))
    assert res["rawPipe"] == [], f"raw-pipe paragraph rendered at prefixes {res['rawPipe'][:5]}"
    assert res["firstTable"] == head_done, "the table appears at the delimiter row's newline, not at turn end"
    assert res["tableCount"] == len(full) - head_done + 1, "and stays a table for every later prefix"


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
# A tool card is a <details> collapsed to its header line (status, name,
# args-or-error, timing); expanding shows the formatted-JSON args and result.
# NOTHING opens a card on its own, a failure included: a failed call used to
# open itself, which put its ARGS dump on screen above the answer (#1974). Its
# diagnosis rides the header line instead, which is the part folding keeps.
# Any card expanded during a turn folds back the moment that turn ends.


def test_tool_call_card_is_a_details_element_collapsed_by_default():
    js = _read(CHAT_JS)
    card = js[js.index("function _buildToolCard") : js.index("const _TOOL_ERROR_LINE_CHARS")]
    assert 'document.createElement("details")' in card, "the whole card must be collapsible, not just its nested panels"
    assert 'document.createElement("summary")' in card, "the header becomes the <details>'s native toggle"
    # Collapsed by default, with no exception: a card that opens itself is a
    # card that puts its args panel on screen unasked (#1974).
    assert "wrap.open = true" not in card, (
        "cards start as just the header line — the name is the toggle (user ask on #1504 follow-up)"
    )
    start = js[js.index("function renderToolCallStart") : js.index("function renderToolCallEnd")]
    assert "wrap.open = true" not in start
    assert "_currentTurnToolCards.push(wrap)" in start, (
        "tracked so every card opened this turn folds together at turn end"
    )
    end = js[js.index("function renderToolCallEnd") : js.index("/** Fold every tool-call card")]
    assert "wrap.open = true" not in end
    assert "if (isError) _setToolCardError(wrap, result)" in end, (
        "a FAILED card must show its diagnosis without a click — on the header, not by opening"
    )


def test_a_failed_card_puts_its_diagnosis_on_the_header_as_plain_text():
    """The error line names internal endpoints ("400 Bad Request for
    http://localhost:8000/api/query"). Rendered through markdown, marked's GFM
    autolinker turned that into a link the chat offered the reader (#1974) —
    so the header line is written with textContent, and a failed result's BODY
    is a <pre>, never `renderMarkdownSafe`."""
    js = _read(CHAT_JS)
    setter = js[js.index("function _setToolCardError") : js.index("function renderToolCallStart")]
    assert "summary.textContent = line" in setter, "the diagnosis is text, never markup"
    assert "innerHTML" not in setter

    preview = js[js.index("function _renderToolResultPreview") : js.index("function _appendSemanticValidationNotice")]
    err_branch = preview[preview.index("if (isError === true") : preview.index("// Already-tabular shapes")]
    assert "pre.textContent = result" in err_branch, "an error body renders as text"
    assert "renderMarkdownSafe" not in err_branch, (
        "markdown on an error string only adds a clickable internal URL (#1974)"
    )
    # …and both live and replayed paths have to TELL the renderer it failed,
    # or the branch above is unreachable on one of them.
    assert "_renderToolResultPreview(result, toolName, isError)" in js
    assert "_renderToolResultPreview(result, tool, wrapIsError)" in js


def test_a_replayed_failure_gets_its_header_line_after_the_header_exists():
    """`_setToolCardError` finds the summary by querying the CARD, so called
    before `wrap.appendChild(head)` it silently does nothing and a reloaded
    failure shows its args sketch where its error should be. Ordering, not
    presence — which is why this is a position assertion."""
    js = _read(CHAT_JS)
    card = js[js.index("function _buildToolCard") : js.index("const _TOOL_ERROR_LINE_CHARS")]
    assert card.index("wrap.appendChild(head);") < card.index("if (wrapIsError) _setToolCardError(wrap, result);")


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


def test_a_failed_tool_card_folds_with_the_rest_because_its_diagnosis_is_on_the_header():
    """A failed card used to be exempt from the end-of-turn fold, on the
    argument that folding put the diagnosis behind a click nobody knows to
    make. That argument is answered rather than abandoned: the diagnosis moved
    ONTO the header line (`_setToolCardError`), which is exactly the part
    folding keeps, so the exemption now only preserves the args dump the fold
    exists to clear (#1974)."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _collapseFinishedToolCalls") :]
    fn = fn[: fn.index("\n}")]
    assert 'classList.contains("is-error")' not in fn, (
        "no card is exempt from the fold — the header carries the error either way"
    )
    assert "wrap.open = false" in fn
    # The header line the fold leaves behind has to be the one carrying it.
    end = js[js.index("function renderToolCallEnd") : js.index("/** Fold every tool-call card")]
    assert "_setToolCardError(wrap, result)" in end


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
    css = re.sub(r"/\*.*?\*/", "", _read(CHAT_CSS), flags=re.DOTALL)
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
    # `tool` rides along too (facts-graph wave — routes `fact_claims` through
    # its own preview) but it is still the ONE shared call for both paths.
    assert "_renderToolResultPreview(result, tool, wrapIsError)" in fn, "one result renderer for both paths"
    assert 'state === "output-error"' in fn and 'state === "output-available"' in fn, (
        "the persisted state maps onto the same is-error / is-done classes a live result produces"
    )
    # Sprite icons since #1503 — check for done, triangle-alert for error.
    assert 'iconEl("check")' in fn and 'iconEl("triangle-alert")' in fn
    # Neutral only when there is genuinely nothing to report: the fallback
    # class, and an icon appended only when it has content.
    assert 'let statusClass = "is-replayed"' in fn
    assert "if (icon.firstChild) head.appendChild(icon)" in fn, (
        "a stateless (pre-v123) part must not get an empty icon slot"
    )
    css = re.sub(r"/\*.*?\*/", "", _read(CHAT_CSS), flags=re.DOTALL)
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
    assert 'for (const node of _groupConsecutiveToolCards(nodes)) $("chat-messages").appendChild(node)' in body, (
        "every node — bubbles and cards alike — is appended to the messages column in order, "
        "runs of cards wrapped in the same group the live path builds (#1974)"
    )
    # The collapse measures the tail article after insertion; the cards and
    # earlier segments are siblings, not part of the answer's height.
    assert body.index("for (const node of _groupConsecutiveToolCards(nodes))") < body.index(
        "maybeMakeCollapsible(tailArticle)"
    )


def test_history_renders_parts_in_order_with_nothing_hoisted():
    """`m.parts` is the turn's sequence (schema v123), so a reload walks it and
    puts each tool card back where it ran — closing the half of #1504 a
    client-side fix never could.

    Crucially it must not HOIST. An earlier cut painted the first TEXT part
    into the message bubble regardless of position, which reversed a turn that
    OPENS with a tool call: the prose came out above the card that ran first,
    while the live stream renders card-then-text for the same turn. Order is
    array order, and the only way to guarantee it is to never reorder.

    A row without parts keeps the only honest fallback: the answer, then its
    positionless tool calls after it, because the ordering those rows lost is
    not in the data to recover."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderMessage") : js.index("// ---------- Result table enhancement")]
    assert "for (const part of parts)" in fn, "array order IS the turn order — no sorting, no bucketing"
    assert 'part.type === "text"' in fn and 'part.type === "tool"' in fn
    # No hoisting: the first text part must not be selected out of sequence.
    assert "textParts" not in fn, "selecting a text part by index reorders the turn"
    assert ".filter(" not in fn.split("if (!parts)")[0], "no pre-pass over parts before the ordered walk"
    # State rides through to the card rather than being recomputed.
    assert "state: part.state" in fn and "isError: part.is_error === true" in fn
    # Legacy branch: guarded on the absence of parts, and still skips the
    # nameless cancelled/interrupted markers.
    assert "if (!parts) {" in fn
    assert "if (!formatToolCall(tc)) continue;" in fn
    # The primary article is whichever text bubble comes first, and a
    # tool-only row still gets one — appended LAST so cards keep their spots.
    assert "if (primary === null) pushTextBubble(m.content)" in fn


def test_a_tool_first_turn_reloads_card_before_prose():
    """The specific inversion: parts `[tool, text]` must emit the card first.
    Executed rather than asserted textually, because this is about the ORDER
    the loop produces, not the text of the loop."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderMessage") : js.index("// ---------- Result table enhancement")]
    # Extract just the ordering decision and run it over a tool-first row.
    harness = """
    const order = [];
    const parts = [
      {type: "tool", tool: "Bash", args: {}, state: "output-available", result: "r", is_error: false},
      {type: "text", text: "Found it."},
    ];
    let primary = null;
    const pushTextBubble = (t) => { if (primary === null) primary = "primary"; order.push("text"); };
    for (const part of parts) {
      if (!part) continue;
      if (part.type === "text") { pushTextBubble(part.text); continue; }
      if (part.type === "tool" && part.tool) order.push("card");
    }
    if (primary === null) pushTextBubble("");
    process.stdout.write(JSON.stringify(order));
    """
    assert json.loads(_node_run(harness)) == ["card", "text"], (
        "a turn that opens with a tool call must reload card-then-prose, matching the live stream"
    )
    # And the shipped code contains no index-based text selection that would
    # reintroduce the hoist.
    assert "textParts[0]" not in fn


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


# ── the trailer is a system-prompt contract, not only project memory ─────────


def test_the_sandbox_system_prompt_carries_the_next_actions_contract():
    """Why this exists at all, given the CLAUDE.md sections above already say
    it: an admin Workspace Prompt override (src/initial_workspace.py::
    resolve_prompt) REPLACES the shipped template wholesale, so on such an
    instance the section is simply gone and no template edit can bring the
    chips back. The system prompt is the surface an override cannot reach.
    NOT justified as a compliance fix — a 3-arm A/B against a real model
    could not distinguish it from the CLAUDE.md wording alone."""
    src = Path("app/chat/runner.py").read_text(encoding="utf-8")
    assert "_NEXT_ACTIONS_CONTRACT" in src
    start = src.index('_NEXT_ACTIONS_CONTRACT = """')
    contract = src[start : src.index('"""\n', start + len('_NEXT_ACTIONS_CONTRACT = """'))]
    assert "```next_actions" in contract
    assert "exactly TWO" in contract, "a fixed count is what makes compliance checkable"
    # It must reach the CLI as an append to the stock preset — assigning
    # `system_prompt` outright would replace Claude Code's own preset.
    assert 'options_kwargs["system_prompt"] = {' in src
    assert '"preset": "claude_code"' in src
    assert '"append": "\\n\\n".join(append_parts)' in src, (
        "the contract and the restored transcript must COMPOSE — an earlier shape "
        "had the restore branch own the append slot, so one silently replaced the other"
    )
    assert "append_parts = [_NEXT_ACTIONS_CONTRACT]" in src
    # A sandbox image older than the wheel's SDK pin has no `system_prompt`
    # field; passing it is a TypeError that kills every turn. Before this
    # change the append slot was reached only by the rare restore path, so
    # the guard was optional. It is not any more.
    assert 'if "system_prompt" in getattr(ClaudeAgentOptions, "__dataclass_fields__", {}):' in src


def test_the_trailer_is_written_before_the_sources_block():
    """Both trailers are withheld from the painter while they stream
    (_streamingSafeText), so their ORDER decides when the buttons can be
    drawn: next_actions first means the chips land with the answer instead
    of after the whole tail. All three prompt surfaces must agree, or the
    model picks one at random."""
    runner = Path("app/chat/runner.py").read_text(encoding="utf-8")
    assert "BEFORE any `sources` block" in runner
    for path in (Path("config/claude_md_template.txt"), WORKSPACE_CLAUDE_MD):
        flat = re.sub(r"\s+", " ", _read(path))
        assert "before any `sources` block" in flat, path


def test_no_prompt_surface_keeps_the_conversation_is_over_opt_out():
    """The root cause was a judgement call the model kept making. Naming two
    hard cases in the system prompt is worthless while a workspace prompt
    loaded alongside it still says "skip the block when the conversation is
    clearly over" — the model would simply follow the permissive one, and the
    admin-readable contract would not be the same contract. (Copilot review
    on this PR.)"""
    for path in (
        Path("app/chat/runner.py"),
        Path("config/claude_md_template.txt"),
        WORKSPACE_CLAUDE_MD,
    ):
        flat = re.sub(r"\s+", " ", _read(path))
        assert "conversation is clearly over" not in flat, path
    for path in (Path("config/claude_md_template.txt"), WORKSPACE_CLAUDE_MD):
        flat = re.sub(r"\s+", " ", _read(path))
        assert "Omit it in exactly two cases" in flat, path


def test_pending_chips_are_inert_until_the_turn_ends():
    """The mid-stream row exists to SHOW the follow-ups early, not to let one
    be fired into a running turn: submitUserMessage has no in-flight guard
    and calls _resetStreamingState(), so the old turn's remaining frames
    would render below the new user message."""
    js = _read(CHAT_JS)
    body = js[js.index("function renderNextActions") : js.index("function _clearNextActions")]
    assert "function renderNextActions(bubble, actions, pending = false)" in js
    assert "btn.disabled = pending;" in body
    assert "if (btn.disabled) return;" in body, "the handler must refuse too, not only the attribute"
    stream = js[js.index("function _renderStreamingMarkdown") : js.index("function _flushStreamingTail")]
    assert "streamedActions, true)" in stream, "the streaming draw is the only pending one"
    # Every finish path renders the row enabled — a chip that stayed dead
    # after the turn ended would be worse than no chip at all.
    for call in re.findall(r"renderNextActions\([^;]*?\);", js[js.index("function finalizeAssistantMessage") :]):
        assert "true)" not in call, f"finalize must render enabled chips: {call}"


def test_chips_are_drawn_mid_stream_not_only_at_finalize():
    """The trailer rides inside the same stream, so the buttons are knowable
    the moment its closing fence arrives. Everything between that moment and
    the assistant_message frame is turn-close latency the reader used to
    spend watching a caret."""
    js = _read(CHAT_JS)
    body = js[js.index("function _renderStreamingMarkdown") : js.index("function _flushStreamingTail")]
    assert "extractNextActions(currentAssistantText).actions" in body
    assert "renderNextActions(" in body
    assert "if (streamedActions.length)" in body, (
        "a paint mid-trailer parses nothing yet and must not clear a row already up"
    )


def test_structured_output_validation_survives_the_trailer():
    """The contract makes the trailer near-certain on EVERY reply, agent-API
    ones included. Left on, it broke JSON validation twice over: the raw
    parse fails on the trailing fence, and the fence fallback then returns
    the TRAILER's body instead of the JSON."""
    from app.chat.structured_output import validate

    fmt = {"type": "json_schema", "schema": {"type": "object", "required": ["n"]}}
    answer = '{"n": 4}\n\n```next_actions\n- Chart it\n- Break it down\n```'
    ok, parsed, err = validate(answer, fmt)
    assert ok, err
    assert parsed == {"n": 4}

    both = '{"n": 4}\n\n```next_actions\n- Chart it\n```\n\n```sources\ntable: orders\n```'
    ok, parsed, err = validate(both, fmt)
    assert ok, err
    assert parsed == {"n": 4}

    # A fenced JSON answer still parses, and an unfenced one whose own string
    # content holds a fence is still parsed whole (the pre-existing rule).
    ok, parsed, _ = validate('```json\n{"n": 1}\n```', fmt)
    assert ok and parsed == {"n": 1}
    ok, parsed, _ = validate('{"n": 1, "code": "```py\\npass\\n```"}', fmt)
    assert ok and parsed["n"] == 1


# ── tool-call groups: a run of calls is one line, not a wall ────────────────
# A research turn can open with a dozen calls before its first sentence, which
# rendered as two screens of machinery above the answer (#1974). Consecutive
# cards fold into ONE <details>; a run of one is left alone.

#: A DOM small enough to read and complete enough for the shipped group
#: builders to run — same idiom as tests/test_chat_file_preview_ui.py.
_GROUP_HARNESS = """
function mkEl(tag) {
  const node = {
    tag, _cls: new Set(), children: [], parentNode: null, _text: '', open: undefined,
    attrs: {},
    get className() { return [...node._cls].join(' '); },
    set className(v) { node._cls = new Set(String(v).split(/\\s+/).filter(Boolean)); },
    classList: {
      add: (...c) => c.forEach((x) => node._cls.add(x)),
      remove: (...c) => c.forEach((x) => node._cls.delete(x)),
      contains: (c) => node._cls.has(c),
      toggle: (c, on) => { if (on) node._cls.add(c); else node._cls.delete(c); },
    },
    setAttribute(k, v) { node.attrs[k] = String(v); },
    appendChild(c) {
      if (c.parentNode) c.parentNode.children = c.parentNode.children.filter((k) => k !== c);
      c.parentNode = node; node.children.push(c); return c;
    },
    replaceChildren(...c) { node.children = []; c.forEach((x) => node.appendChild(x)); },
    querySelector(sel) {
      const cls = sel.replace(/^\\./, '');
      for (const k of node.children) {
        if (k._cls.has(cls)) return k;
        const deep = k.querySelector(sel);
        if (deep) return deep;
      }
      return null;
    },
  };
  Object.defineProperty(node, 'textContent', {
    get() { return node._text; },
    set(v) { node._text = String(v); node.children = []; },
  });
  return node;
}
const document = { createElement: mkEl };
function iconEl(name) { const i = mkEl('svg'); i.className = 'icon-' + name; return i; }
function $() { return mkEl('div'); }
let _currentToolGroup = null;
let _looseToolCard = null;

/** A stand-in tool card in one of the states the real one can be in. */
function card(state, name) {
  const c = mkEl('details');
  c.className = 'cloud-chat-tool ' + state;
  const n = mkEl('span');
  n.className = 'cloud-chat-tool-name';
  n.textContent = name || 'Did a thing';
  c.appendChild(n);
  return c;
}
function head(group, cls) { return group.querySelector(cls).textContent; }
"""


def _group_source() -> str:
    js = _read(CHAT_JS)
    return js[js.index("function _buildToolGroup()") : js.index("function renderToolCallStart(")]


def test_a_run_of_consecutive_cards_folds_into_one_group():
    """Executed, not asserted textually: what matters is the SHAPE the walk
    produces — one group per run, and no group at all around a lone call,
    which would cost a click and say nothing."""
    script = (
        _GROUP_HARNESS
        + _group_source()
        + """
const alone = _groupConsecutiveToolCards([card('is-done')]);
const run = _groupConsecutiveToolCards([card('is-done'), card('is-done'), card('is-error')]);
const split = _groupConsecutiveToolCards([card('is-done'), mkEl('article'), card('is-done')]);
const twoRuns = _groupConsecutiveToolCards([
  card('is-done'), card('is-done'), mkEl('article'), card('is-done'), card('is-done'),
]);
process.stdout.write(JSON.stringify({
  alone: alone.map((n) => n.tag + ':' + n.className),
  run_len: run.length,
  run_cls: run[0].className,
  run_cards: run[0].querySelector('.cloud-chat-tool-group-body').children.length,
  run_label: head(run[0], '.cloud-chat-tool-group-label'),
  run_meta: head(run[0], '.cloud-chat-tool-group-meta'),
  run_open: !!run[0].open,
  split: split.map((n) => n.className || n.tag),
  two_runs: twoRuns.map((n) => n.className || n.tag),
}));
"""
    )
    res = json.loads(_node_run(script))
    assert res["alone"] == ["details:cloud-chat-tool is-done"], "a lone call is not worth a group header"
    assert res["run_len"] == 1 and res["run_cards"] == 3, "the whole run becomes one node"
    assert "cloud-chat-tool-group" in res["run_cls"]
    assert res["run_label"] == "3 steps"
    assert res["run_meta"] == "2 failed" or res["run_meta"] == "1 failed"
    assert not res["run_open"], "a replayed group opens collapsed — it is the compact form"
    assert res["split"] == ["cloud-chat-tool is-done", "article", "cloud-chat-tool is-done"], (
        "prose between two calls ends the run — a group means 'these ran together'"
    )
    assert res["two_runs"][0].startswith("cloud-chat-tool-group")
    assert res["two_runs"][1] == "article"
    assert res["two_runs"][2].startswith("cloud-chat-tool-group")


def test_group_header_reports_failures_and_the_live_step():
    """The header is derived from the CARDS, never from a counter — a counter
    that drifts reports a run that failed as one that did not."""
    script = (
        _GROUP_HARNESS
        + _group_source()
        + """
const settled = _buildToolGroup();
const sbody = settled.querySelector('.cloud-chat-tool-group-body');
[card('is-done'), card('is-error'), card('is-error')].forEach((c) => sbody.appendChild(c));
_updateToolGroupSummary(settled);

const live = _buildToolGroup();
const lbody = live.querySelector('.cloud-chat-tool-group-body');
lbody.appendChild(card('is-done', 'Querying data'));
lbody.appendChild(card('is-running', 'Reading a file'));
_updateToolGroupSummary(live);

const replayed = _buildToolGroup();
const rbody = replayed.querySelector('.cloud-chat-tool-group-body');
[card('is-replayed'), card('is-replayed')].forEach((c) => rbody.appendChild(c));
_updateToolGroupSummary(replayed);

process.stdout.write(JSON.stringify({
  settled: [settled.className, head(settled, '.cloud-chat-tool-group-label'),
            head(settled, '.cloud-chat-tool-group-meta'),
            settled.querySelector('.cloud-chat-tool-group-icon').children.map((c) => c.className)],
  live: [live.className, head(live, '.cloud-chat-tool-group-label'),
         head(live, '.cloud-chat-tool-group-meta')],
  replayed_icon: replayed.querySelector('.cloud-chat-tool-group-icon').children.length,
  replayed_cls: replayed.className,
}));
"""
    )
    res = json.loads(_node_run(script))
    assert "is-error" in res["settled"][0], "a run with a failure in it says so on its edge"
    assert res["settled"][1] == "3 steps"
    assert res["settled"][2] == "2 failed"
    assert res["settled"][3] == ["icon-triangle-alert"]
    assert "is-running" in res["live"][0]
    assert res["live"][1] == "Reading a file", "a collapsed live group still says what is happening now"
    assert res["live"][2] == "2 steps"
    assert res["replayed_icon"] == 0, "a pre-v123 run records no outcome — no tick it cannot evidence"
    assert "is-done" not in res["replayed_cls"]


def test_the_live_label_names_the_call_that_is_still_running():
    """Calls settle out of order, so "the last card" is not "the card that is
    running". Once the newest one finished while an earlier one was still
    going, the header read as running while naming a step already done.
    (Copilot review on #1985.)"""
    script = (
        _GROUP_HARNESS
        + _group_source()
        + """
const g = _buildToolGroup();
const body = g.querySelector('.cloud-chat-tool-group-body');
body.appendChild(card('is-running', 'Reading a file'));
body.appendChild(card('is-done', 'Querying data'));
_updateToolGroupSummary(g);
process.stdout.write(JSON.stringify({
  cls: g.className, label: head(g, '.cloud-chat-tool-group-label'),
  meta: head(g, '.cloud-chat-tool-group-meta'),
}));
"""
    )
    res = json.loads(_node_run(script))
    assert "is-running" in res["cls"]
    assert res["label"] == "Reading a file", "the header must name the call still in flight, not the last one added"
    assert res["meta"] == "2 steps"


def test_a_run_only_continues_while_its_group_is_still_the_stream_tail():
    """`_endToolGroup` is called from every appender that knows about runs, but
    the preview paths append an assistant article without going through any of
    them. A card dropped into the older group would jump visually back above
    that article, so the open group is tail-checked rather than trusted.
    (Copilot review on #1985.)"""
    js = _read(CHAT_JS)
    fn = js[js.index("function _appendToolCard") : js.index("/** Close the open run.")]
    assert "if (_currentToolGroup && stream.lastElementChild !== _currentToolGroup) _endToolGroup();" in fn
    assert fn.index("_endToolGroup()") < fn.index("if (_currentToolGroup) {"), (
        "the guard has to run BEFORE the group is used, or it guards nothing"
    )


def test_a_run_is_ended_by_everything_that_is_not_another_tool_card():
    """A group means "these ran together, between these two things the agent
    said". Anything else appended to the stream has to close it, or a group
    silently becomes "every tool call of the turn"."""
    js = _read(CHAT_JS)
    for fn_name, end in (
        ("function appendToken", "function finalizeAssistantMessage"),
        ("function renderSystemNote", "/** Show an ephemeral toast"),
        ("function renderApprovalRequest", "function resolveApprovalCard"),
        ("function renderQuestionRequest", "function resolveQuestionCard"),
        ("function _collapseFinishedToolCalls", "function _looksLikeToolError"),
    ):
        body = js[js.index(fn_name) : js.index(end)]
        assert "_endToolGroup()" in body, f"{fn_name} must close the run above it"
    # And a fresh/cleared transcript starts with no run open, or the first card
    # of the next conversation would join the last one's group.
    for clear in re.finditer(r'\$\("chat-messages"\)\.innerHTML = "";', js):
        tail = js[clear.end() : clear.end() + 120]
        assert "_endToolGroup()" in tail, "clearing the transcript must clear the open run"


def test_tool_group_css_uses_ds_tokens_only():
    css = _read(CHAT_CSS)
    block = css[css.index(".cloud-chat-tool-group {") : css.index(".cloud-chat-tool-head {")]
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", block) is None, "raw hex — the group is --ds-* like everything else"
    assert "var(--primary)" not in block, "the design system's token is --ds-primary"
    assert "--ds-accent-danger-line" in block and "--ds-accent-success-line" in block


# ── the composer must not grow over the transcript ─────────────────────────


def test_transcript_reserves_the_composer_s_measured_height():
    """In a live thread the composer floats over the messages, and the padding
    that kept the last turn clear of it was a constant sized for one line — so
    a long prompt grew up over the conversation you were answering (#1974)."""
    js = _read(CHAT_JS)
    assert "function _syncComposerHeightVar" in js
    fn = js[js.index("function _syncComposerHeightVar") : js.index("function autosizeComposer")]
    assert '"--chat-composer-h"' in fn and "offsetHeight" in fn
    autosize = js[js.index("function autosizeComposer") : js.index("if (typeof ResizeObserver")]
    assert autosize.count("_syncComposerHeightVar()") == 2, (
        "both exits of autosize publish the height — the empty one too, or clearing the box "
        "leaves the transcript padded for the text that was there"
    )
    assert "new ResizeObserver(_syncComposerHeightVar)" in js, (
        "the composer also changes height for reasons no keystroke reports"
    )
    css = re.sub(r"/\*.*?\*/", "", _read(CHAT_CSS), flags=re.DOTALL)
    rule = css[css.index(".cloud-chat-shell.has-thread .cloud-chat-messages {") :]
    rule = rule[: rule.index("}")]
    assert "var(--chat-composer-h" in rule, "the reserved space must track the measurement"
    assert "116px" not in rule, "the constant this replaced"
