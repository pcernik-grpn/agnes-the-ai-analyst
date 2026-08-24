"""The rendering half of the sources block, and mermaid's collision with the sanitizer.

`tests/test_chat_sources_verdict.py` covers what the server concludes. This
covers what the reader ends up looking at, and the two decisions that are easy
to undo by accident:

1. **The raw ```sources fence never reaches the screen, but never leaves the
   clipboard.** It is a wire format between the agent and the renderer. kai-agent
   strips its own mandated trailer (`next_actions`) from both — correct there,
   because suggestions are chrome. Provenance is not: a copied transcript is
   exactly what someone sends when they doubt a number, and dropping the
   sources from it removes the part that answers them.

2. **Mermaid output must NOT go through `renderMarkdownSafe`.** Its SVG carries
   a `<style>` block that every one of its colours depends on, and `style` is
   in `_DANGEROUS_TAGS`. Sanitizing it would leave a grey skeleton — a failure
   that looks like a mermaid bug rather than ours. The untrusted input is the
   diagram *source*, and `securityLevel: 'strict'` is what handles it.

Verified against the rendered page, not inferred: with the fence hidden, the
chips drawn from a server verdict, mermaid's `<style>` block intact in the
output, and the two contrast/size defects that measuring turned up (the
`SOURCES` label at 3.93:1, the `UNVERIFIED` flag at 8.5px) corrected.
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
CHAT_HTML = Path("app/web/templates/chat.html")
WORKSPACE_CLAUDE_MD = Path("app/initial_workspace_default/CLAUDE.md")
MERMAID = Path("app/web/static/vendor/mermaid.min.js")
LICENSES = Path("app/web/static/vendor/LICENSES.md")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Source with comments removed.

    Every "X must not appear here" assertion below has to run against code, not
    prose — the comment explaining why `--ds-text-muted` is wrong necessarily
    contains `--ds-text-muted`, and a naive containment check fails on the
    explanation rather than on the rule. Handles `/* … */` and `//` line
    comments; neither file contains a string literal with those sequences in
    it, which is what makes this safe here rather than in general.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


# ── the fence: hidden from the eye, kept in the record ──────────────────────


def test_both_render_paths_strip_the_fence():
    """History and the live turn must agree — a reload that suddenly showed the
    raw block would read as a different answer. Both trailers ride the shared
    renderAnswerMarkdown helper now."""
    js = _read(CHAT_JS)
    assert "renderMarkdownSafe(stripNextActionsFence(stripSourcesFence(" in js, (
        "renderAnswerMarkdown must strip sources first, then next_actions"
    )
    # renderMessage paints the first text part of a `parts` row, or the whole
    # content for a pre-v123 row — both through the helper.
    assert 'renderAnswerMarkdown(text || "")' in js, (
        "every history text bubble — parts walk and pre-v123 fallback alike — goes through the helper"
    )
    assert "renderAnswerMarkdown(tail)" in js, (
        "finalizeAssistantMessage must use the helper (on the post-seal tail — #1504 segmentation)"
    )


def test_the_clipboard_keeps_the_fence():
    """attachMessageActions is handed content with the SOURCES fence intact.
    (next_actions is stripped — suggestions are chrome; provenance is not.)"""
    js = _read(CHAT_JS)
    assert "attachMessageActions(currentAssistantArticle, stripNextActionsFence(content))" in js
    assert 'attachMessageActions(primary, stripNextActionsFence(m.content || ""))' in js
    assert "attachMessageActions(currentAssistantArticle, stripSourcesFence" not in js
    assert "attachMessageActions(primary, stripSourcesFence" not in js


def test_chips_come_from_the_server_verdict_only():
    """The client has no record of what actually ran; a second opinion derived
    from less information would be worse than none."""
    js = _read(CHAT_JS)
    assert "renderSourcesChips(bubble, m.sources)" in js
    assert 'renderSourcesChips(currentAssistantBody.closest(".msg-bubble"), frame && frame.sources)' in js


def test_an_answer_that_declared_nothing_and_claimed_nothing_stays_silent():
    """'No source declared' under a greeting is noise. The guard is the early
    return; without it every non-answer grows a provenance row.

    Refined after Devin Review: the exemption is now conditioned on the answer
    having rendered no FIGURE. A greeting still gets nothing; a table or chart
    with no declared source is exactly what the row exists to surface, and it
    was being shown as an ordinary answer.
    """
    js = _read(CHAT_JS)
    assert "if (!verdict.declared && claims.length === 0 && !_bubbleHasFigure(bubble)) return;" in js


# ── mermaid ─────────────────────────────────────────────────────────────────


def test_mermaid_is_vendored_and_licensed():
    assert MERMAID.exists(), "the vendored bundle is what makes this work offline"
    licenses = _read(LICENSES)
    assert "mermaid" in licenses.lower()
    assert "MIT" in licenses


def test_mermaid_is_not_loaded_with_the_page():
    """3.5 MB — an order of magnitude more than every other vendored asset put
    together. Tolerable only because a thread without a diagram never fetches
    it."""
    assert "mermaid" not in _read(CHAT_HTML), "mermaid must not be a script tag in the template"
    js = _read(CHAT_JS)
    assert "function loadMermaid()" in js
    assert "_mermaidReady" in js, "the load promise must be cached, not re-fetched per diagram"


def test_mermaid_output_bypasses_the_markdown_sanitizer():
    """The load-bearing decision. If someone routes this through
    renderMarkdownSafe for consistency, every diagram loses its colours."""
    js = _read(CHAT_JS)
    body = js[js.index("function renderMermaidBlocks") : js.index("// ---------- Sources block")]
    assert "fig.innerHTML = svg;" in body
    assert "renderMarkdownSafe" not in _code_only(body), (
        "mermaid's own <style> block is stripped by the sanitizer — see this test's docstring"
    )


def test_mermaid_treats_the_diagram_source_as_untrusted():
    js = _read(CHAT_JS)
    assert 'securityLevel: "strict"' in js


def test_a_broken_diagram_keeps_its_source_on_screen():
    """A diagram the agent got wrong is still information; a blank gap reads as
    a product fault."""
    js = _read(CHAT_JS)
    assert "msg-mermaid-error" in js
    assert "msg-mermaid-error" in _read(CHAT_CSS)


# ── the two defects measuring found ─────────────────────────────────────────


def test_the_sources_label_is_not_set_in_the_muted_tone():
    """Measured at 3.93:1 against the bubble with --ds-text-muted — under WCAG
    AA at 10px. This row exists to be read."""
    css = _read(CHAT_CSS)
    block = re.search(r"\.msg-sources-label \{(.*?)\}", _code_only(css), re.DOTALL)
    assert block, ".msg-sources-label moved — re-point this guard"
    assert "--ds-text-secondary" in block.group(1)
    assert "--ds-text-muted" not in block.group(1)


def test_the_unverified_flag_is_not_shrunk_below_the_chip():
    """`font-size: 0.85em` of --text-xs measured 8.5px — the one word on the row
    that has to be legible, set smaller than everything around it."""
    css = _read(CHAT_CSS)
    block = re.search(r"\.msg-source-flag \{(.*?)\}", _code_only(css), re.DOTALL)
    assert block, ".msg-source-flag moved — re-point this guard"
    assert "font-size" not in block.group(1)


# ── prompt contract ─────────────────────────────────────────────────────────


def test_the_prompt_asks_for_the_block_and_says_it_is_checked():
    """A model told only the format has no reason to be careful about the
    claim. It is told the claim is checked, and what an unsupported one looks
    like to the reader."""
    md = re.sub(r"\s+", " ", _read(WORKSPACE_CLAUDE_MD))
    assert "```sources" in _read(WORKSPACE_CLAUDE_MD)
    assert "table:" in md and "metric:" in md and "assumption:" in md
    assert "unverified" in md.lower()
    assert "naming a table you did not query is worse than naming none" in md


def test_the_prompt_separates_diagrams_from_charts():
    md = re.sub(r"\s+", " ", _read(WORKSPACE_CLAUDE_MD))
    assert "```mermaid" in _read(WORKSPACE_CLAUDE_MD)
    assert "mermaid draws relationships and cannot plot values" in md


# ── executable ──────────────────────────────────────────────────────────────


def test_the_fence_regex_removes_the_block_and_nothing_else():
    """Run the shipped regex, not a copy: a fence that survives puts the wire
    format on screen, and one that over-matches eats the answer."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    open_decl = re.search(r"const _SOURCES_OPEN_RE = .*?;", js)
    close_decl = re.search(r"const _SOURCES_CLOSE = .*?;", js)
    assert open_decl and close_decl, "the fence constants moved — re-point this guard"
    decl = type("D", (), {"group": lambda self, _n: open_decl.group(0) + "\n" + close_decl.group(0)})()
    fn = js[js.index("function stripSourcesFence") : js.index("const _CLAIM_LABEL")]
    cases = {
        "with_block": "MRR is $1.\n\n```sources\ntable: mrr\n```\n",
        "no_block": "MRR is $1.",
        "code_block_kept": "See:\n\n```sql\nSELECT 1\n```\n\n```sources\ntable: mrr\n```\n",
        # Two blocks: the earlier non-global pattern left the second on screen.
        "two_blocks": "a\n\n```sources\ntable: x\n```\n\nb\n\n```sources\ntable: y\n```\n",
        # Unterminated: not a block. Stripping to end-of-string would eat the
        # answer, and the loop must terminate.
        "unterminated_kept": "MRR is $1.\n\n```sources\ntable: mrr",
    }
    script = (
        decl.group(0)
        + "\n"
        + fn
        + f"\nprocess.stdout.write(JSON.stringify(Object.fromEntries(Object.entries({json.dumps(cases)}).map(([k,v]) => [k, stripSourcesFence(v)]))));\n"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["with_block"] == "MRR is $1."
    assert res["no_block"] == "MRR is $1."
    assert "```sql" in res["code_block_kept"], "an ordinary code block must survive"
    assert "table: mrr" not in res["code_block_kept"]
    assert res["two_blocks"] == "a\n\n\n\nb", res["two_blocks"]
    assert "```sources" not in res["two_blocks"], "a second block stayed on screen"
    assert res["unterminated_kept"] == "MRR is $1.\n\n```sources\ntable: mrr", (
        "an unterminated opener is not a block — stripping it would eat the answer"
    )


def _chat_js() -> str:
    import pathlib

    return (pathlib.Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "chat.js").read_text(
        encoding="utf-8"
    )


class TestEveryProvenanceBlockIsRemoved:
    """Devin Review on #1239: the pattern was not global.

    An answer that emits two provenance blocks had only the first removed,
    leaving the second on screen as a raw code fence — the wire format shown
    to the reader, which is the one thing `stripSourcesFence` exists to
    prevent.
    """

    def test_the_body_is_not_matched_with_a_regex(self):
        """The browser half of the linear-time fix.

        `[\\s\\S]*?```` rescans to end-of-string for every unterminated
        opener, so a long reply full of half-written markers cost work
        proportional to openers x length — in the reader's browser, on every
        message. Same trap fixed server-side in `app/chat/sources.py`.
        """
        js = _chat_js()
        assert "_SOURCES_FENCE_RE" not in js, "the non-greedy fence pattern is back"
        assert "_SOURCES_OPEN_RE" in js and "indexOf(_SOURCES_CLOSE" in js

    def test_every_block_is_still_removed(self):
        """The loop must not stop after the first — that was the earlier bug."""
        js = _chat_js()
        fn = js[js.index("function stripSourcesFence") : js.index("const _CLAIM_LABEL")]
        assert "for (;;)" in fn or "while" in fn, "only one block would be stripped"

    def test_an_unterminated_opener_stops_the_loop(self):
        """Otherwise a truncated answer swallows everything after it — and the
        loop would never terminate."""
        js = _chat_js()
        fn = js[js.index("function stripSourcesFence") : js.index("const _CLAIM_LABEL")]
        assert "=== -1) break" in fn


class TestAnUnsourcedFigureIsNotSilent:
    """Devin Review on #1239: the chip row bailed on the case it exists for.

    Staying silent when nothing is declared is right for a greeting — but the
    server cannot tell a figure from a sentence, which is why the row was
    silent for both. This runs after the body is in the DOM, so the client
    can tell.
    """

    def test_the_bail_out_is_conditioned_on_there_being_no_figure(self):
        js = _chat_js()
        assert "_bubbleHasFigure(bubble)" in js
        bail = [ln for ln in js.splitlines() if "!verdict.declared && claims.length === 0" in ln]
        assert bail, "the bail-out moved — re-point this guard"
        assert all("_bubbleHasFigure" in ln for ln in bail), (
            "an answer that rendered a figure with no declared source is shown as an ordinary answer"
        )

    def test_chrome_does_not_count_as_a_figure(self):
        """Every code block gets a copy BUTTON with an icon in it. A bare
        `svg, img` query matched that, so a plain answer containing a snippet
        — a greeting included — grew a "none declared" row. (Devin Review.)"""
        js = _chat_js()
        fn = js[js.index("function _bubbleHasFigure") : js.index("function renderSourcesChips")]
        assert ".msg-body" in fn, "the query must be scoped to the rendered answer"
        assert 'closest("button' in fn, "an icon inside a control still counts as a figure"

    def test_the_figure_check_covers_both_mermaid_forms(self):
        """Mermaid rendering is async — at chip time it may still be its <pre>."""
        js = _chat_js()
        fn = js[js.index("function _bubbleHasFigure") : js.index("function renderSourcesChips")]
        for sel in ("table", "svg", "pre.mermaid"):
            assert sel in fn, f"figure check misses {sel}"
