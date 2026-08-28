"""Fact-graph tool rendering in the web chat (design doc §13.2 "Chat").

Follows the pattern of tests/test_chat_tool_rendering_ui.py: node-executed
tests run the SHIPPED pure functions (no copies), content assertions pin
the DOM-building call shapes that are easy to undo by accident. Functions
that build DOM (`_renderFactClaimsPreview`, `renderFactsScopeLine`) are not
node-executable without a DOM (this repo carries no jsdom dependency), so
those are pinned structurally instead — same trade-off the pre-existing
suite already makes for `_buildToolCard`/`renderSourcesChips`/etc.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CHAT_JS = Path("app/web/static/js/chat.js")
CHAT_CSS = Path("app/web/static/css/chat.css")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


# ── tool head: raw id -> human head, the glossary_search precedent ──────────


def test_fact_tools_get_human_heads_via_tool_label():
    """Same mechanism as every other tool label (_TOOL_LABELS), not a
    bespoke branch — `fact_search`/`fact_neighbors`/`fact_claims` render
    through `_toolLabel` exactly like Bash/Read/glossary_search do."""
    js = _read(CHAT_JS)
    fn = js[js.index("const _TOOL_LABELS") : js.index("function renderApprovalRequest")]
    cases = [
        ["fact_search", {}],
        ["fact_neighbors", {"subject_id": "f_1"}],
        ["fact_claims", {"subject_id": "f_1"}],
        ["mcp__agnes__fact_claims", {"subject_id": "f_1"}],
    ]
    script = fn + f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(([t, a]) => _toolLabel(t, a))));\n"
    res = json.loads(_node_run(script))
    assert res[0] == "Searched the knowledge graph"
    assert res[1] == "Walked related facts"
    assert res[2] == "Read the evidence"
    assert res[3] == "Read the evidence", "mcp__<server>__ prefix must strip the same as any other tool"


# ── evidence tally: pure Set bookkeeping, fully node-executable ─────────────


def test_record_fact_claims_evidence_is_pure_and_deduplicates():
    js = _read(CHAT_JS)
    fn = js[js.index("let _turnFactDocumentIds") : js.index("/** The `fact_claims` tool result's bespoke preview")]
    script = (
        fn
        + """
    _recordFactClaimsEvidence({
      claims: [
        {corpus_file_id: "cf1", corpus_id: "col_a"},
        {corpus_file_id: "cf2", corpus_id: "col_a"},
        {corpus_file_id: "cf1", corpus_id: "col_a"},
      ],
    });
    _recordFactClaimsEvidence({claims: [{corpus_file_id: "cf3", corpus_id: "col_b"}]});
    process.stdout.write(JSON.stringify({
      docs: _turnFactDocumentIds.size,
      cols: _turnFactCollectionIds.size,
    }));
    """
    )
    res = json.loads(_node_run(script))
    assert res == {"docs": 3, "cols": 2}


def test_record_fact_claims_evidence_ignores_non_claims_shapes():
    """search()/neighbors() results carry no document identifiers at all —
    the tally must stay untouched by anything that isn't the claims shape."""
    js = _read(CHAT_JS)
    fn = js[js.index("let _turnFactDocumentIds") : js.index("/** The `fact_claims` tool result's bespoke preview")]
    script = (
        fn
        + """
    _recordFactClaimsEvidence({subjects: [{id: "f_1"}]});
    _recordFactClaimsEvidence(null);
    _recordFactClaimsEvidence("not an object");
    _recordFactClaimsEvidence({nodes: [], edges: []});
    process.stdout.write(JSON.stringify({docs: _turnFactDocumentIds.size, cols: _turnFactCollectionIds.size}));
    """
    )
    res = json.loads(_node_run(script))
    assert res == {"docs": 0, "cols": 0}


def test_reset_facts_turn_evidence_clears_both_sets():
    js = _read(CHAT_JS)
    fn = js[js.index("let _turnFactDocumentIds") : js.index("/** The `fact_claims` tool result's bespoke preview")]
    script = (
        fn
        + """
    _recordFactClaimsEvidence({claims: [{corpus_file_id: "cf1", corpus_id: "col_a"}]});
    _resetFactsTurnEvidence();
    process.stdout.write(JSON.stringify({docs: _turnFactDocumentIds.size, cols: _turnFactCollectionIds.size}));
    """
    )
    res = json.loads(_node_run(script))
    assert res == {"docs": 0, "cols": 0}


# ── _asToolResultObject: the runner-collapsed-string gap _unwrapMcpEnvelope
#    alone leaves open (see its own docstring / test_mcp_envelope_unwraps_to_
#    its_payload's `json_string_not_envelope` case) ─────────────────────────


def test_as_tool_result_object_handles_all_three_wire_shapes():
    js = _read(CHAT_JS)
    fn = js[js.index("function _unwrapMcpEnvelope") : js.index("function _recordFactClaimsEvidence")]
    cases = {
        "already_object": {"claims": [{"corpus_id": "a"}]},
        # The runner-collapsed shape: a raw JSON string, no {content:[...]} envelope —
        # _unwrapMcpEnvelope alone would hand this back UNCHANGED (still a string).
        "runner_collapsed_string": '{"claims": [{"corpus_id": "a"}], "revealed": false}',
        # A genuine MCP envelope still unwraps correctly too.
        "genuine_envelope": {"content": [{"type": "text", "text": '{"claims": []}'}]},
        "not_json_string": "plain text, not json",
    }
    script = (
        fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k, v]) => [k, _asToolResultObject(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["already_object"] == cases["already_object"]
    assert res["runner_collapsed_string"] == {"claims": [{"corpus_id": "a"}], "revealed": False}, (
        "a raw JSON string with no envelope must still come back parsed"
    )
    assert res["genuine_envelope"] == {"claims": []}
    assert res["not_json_string"] == "plain text, not json"


# ── fact_claims preview: structural pins (no jsdom in this repo) ────────────


def test_fact_claims_preview_checks_the_claims_shape_and_falls_through_otherwise():
    js = _read(CHAT_JS)
    fn = js[js.index("function _renderFactClaimsPreview") : js.index("/** Build the preview block for a tool result.")]
    assert "Array.isArray(result.claims)" in fn, "must guard the expected shape, not assume it"
    assert "return null" in fn, "an unexpected shape (e.g. an error payload) falls through to the generic renderer"


def test_fact_claims_preview_quote_is_sanitized_never_raw_innerhtml():
    """Verbatim document quotes are untrusted extracted text — the security
    playbook's non-negotiable: renderMarkdownSafe, never raw innerHTML."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _renderFactClaimsPreview") : js.index("/** Build the preview block for a tool result.")]
    assert "quote.innerHTML = renderMarkdownSafe(" in fn
    assert "marked.parse(" not in fn, "must not bypass the sanitizer"
    assert '.quote) || ""' in fn, "an absent quote must not throw"


def test_fact_claims_preview_source_link_gated_by_the_safe_scheme_regex():
    """open-in-source link only when source_url is present — AND only when
    it passes the same URL-scheme allowlist every other rendered link uses,
    since `document.source_url` is server data a caller does not control the
    display of but the crawler pipeline does populate from external input."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _renderFactClaimsPreview") : js.index("/** Build the preview block for a tool result.")]
    assert "claim.document.source_url" in fn
    assert "_SAFE_URL_SCHEME_RE.test(" in fn
    assert 'link.rel = "noopener noreferrer"' in fn
    assert "Open in source" in fn


def test_revealed_claims_note_and_empty_claims_note_both_present():
    js = _read(CHAT_JS)
    fn = js[js.index("function _renderFactClaimsPreview") : js.index("/** Build the preview block for a tool result.")]
    assert "result.revealed" in fn
    assert "result.claims.length === 0" in fn


def test_result_preview_routes_fact_claims_to_the_bespoke_renderer():
    js = _read(CHAT_JS)
    fn = js[js.index("function _renderToolResultPreview") : js.index("function _coerceToTablePreview")]
    assert '_bareToolName(toolName) === "fact_claims"' in fn
    assert "_renderFactClaimsPreview(_asToolResultObject(result))" in fn
    assert "fact_search" not in fn and "fact_neighbors" not in fn, (
        "only fact_claims gets the bespoke preview — search/neighbors stay on the generic path"
    )


def test_both_tool_card_paths_pass_the_tool_name_through():
    """Live (renderToolCallEnd) and replayed (_buildToolCard) both must
    reach the bespoke preview — a refresh must not silently downgrade a
    fact_claims card to a raw JSON dump."""
    js = _read(CHAT_JS)
    live = js[js.index("function renderToolCallEnd") : js.index("function _collapseFinishedToolCalls")]
    assert "_renderToolResultPreview(result, toolName)" in live
    assert "wrap.dataset.tool" in live, "the reliable tool name — frame.tool is often the call id, not the name"
    assert '_bareToolName(toolName) === "fact_claims"' in live
    assert "_recordFactClaimsEvidence(" in live


# ── end-of-turn scope line ───────────────────────────────────────────────


def test_render_facts_scope_line_only_when_documents_were_shown():
    js = _read(CHAT_JS)
    fn = js[js.index("function renderFactsScopeLine") : js.index("function renderNextActions")]
    assert "_turnFactDocumentIds.size" in fn
    assert "answered from" not in fn.lower() or "Answered from" in fn
    assert "_resetFactsTurnEvidence()" in fn, "the tally must not bleed into the next turn"


def test_finalize_renders_the_scope_line_on_every_exit_path():
    """Same three exit points renderSourcesChips/renderNextActions already
    cover — the footer is chrome bolted onto the SAME bubble, not a fourth
    code path."""
    js = _read(CHAT_JS)
    fin = js[js.index("function finalizeAssistantMessage") : js.index("// ---------- Inline tool-call blocks")]
    assert fin.count("renderFactsScopeLine(") == 3, (
        "segmented-empty-tail branch, main branch, and the tokenless-turn fallback"
    )


def test_collapse_finished_tool_calls_resets_evidence_defensively():
    """A turn that stops WITHOUT ever reaching finalizeAssistantMessage
    (cancelled/error before any text) must not leak this turn's tally into
    the next one's footer."""
    js = _read(CHAT_JS)
    fn = js[js.index("function _collapseFinishedToolCalls") : js.index("function _looksLikeToolError")]
    assert "_resetFactsTurnEvidence()" in fn


# ── CSS: ds tokens only, same voice as the existing sources trailer ────────


def test_facts_scope_line_and_claim_css_use_ds_tokens_only():
    css = _read(CHAT_CSS)
    for selector in (
        ".msg-facts-scope",
        ".cloud-chat-fact-claims-list",
        ".cloud-chat-fact-claim-quote",
        ".cloud-chat-fact-claim-source",
    ):
        assert selector in css, f"missing rule for {selector}"
    block = css[css.index(".msg-facts-scope {") : css.index(".msg-facts-scope {") + 400]
    block = block[: block.index("}") + 1]
    assert "#" not in block, "no raw hex — ds tokens only"
    assert "var(--ds-" in block or "var(--space-" in block
