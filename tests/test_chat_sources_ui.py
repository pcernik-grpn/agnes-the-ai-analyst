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
    assert 'attachMessageActions(tailArticle, stripNextActionsFence(m.content || ""))' in js
    assert "attachMessageActions(currentAssistantArticle, stripSourcesFence" not in js
    assert "attachMessageActions(tailArticle, stripSourcesFence" not in js


def test_chips_come_from_the_server_verdict_only():
    """The client has no record of what actually ran; a second opinion derived
    from less information would be worse than none."""
    js = _read(CHAT_JS)
    assert "renderSourcesChips(tailBubble, m.sources)" in js
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


def test_the_sources_row_says_each_thing_once():
    """Three separate repetitions made the row read as noise. Each chip spelled
    its CATEGORY as a word (five tables meant reading "table" five times); each
    unverified chip shouted UNVERIFIED, so the common case — the model names
    more than it queries — became the row's dominant colour and a genuinely
    checked source had no way to look calm; and an `assumption`, which is a
    caveat about method with nothing to open, sat in the same pill vocabulary
    as two links.

    Category is now a glyph, the verdict is summarised once for the row, and
    assumptions have their own line. Nothing was dropped: the category word and
    the verdict both ride the chip's accessible name."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderSourcesChips") : js.index("// ---------- Next-actions block")]

    assert "_CLAIM_ICON" in fn and "msg-source-icon" in fn, "category rides a glyph"
    assert "msg-source-kind" not in fn, "the category word is off the chip's face"
    assert 'chip.setAttribute("aria-label"' in fn, (
        "what left the face must not leave the chip — the category and verdict ride the name"
    )
    assert "`${kindWord} ${c.ref}, unverified`" in fn

    # The verdict, once for the row rather than per chip.
    assert "const unverified = provenance.filter((c) => c.verified === false).length;" in fn
    assert "`${unverified} unverified`" in fn
    # Counted over ALL references, not just the visible ones — a count that
    # changed when you expanded the row would be worse than none.
    assert "provenance.filter" in fn and "chips.slice" in fn

    # Verified is the calm state: it adds nothing to the base chip.
    css = _read(CHAT_CSS)
    ok = re.search(r"\.msg-source-chip\.is-ok \{(.*?)\}", _code_only(css), re.DOTALL)
    assert ok is None, "a verified chip wears the base chip — no fill of its own"

    # Assumptions get their own ROW either way — TCRD-289's version (chips with
    # an origin badge and a rationale) landed while this was in review and says
    # strictly more than the prose line this branch first drew, so its row is
    # the one kept. What matters here is unchanged: they are not filed in among
    # the things you can open.
    assert ".msg-sources.is-assumptions {" in css, "assumptions keep a row of their own"


def test_the_source_chip_states_use_ink_not_line_tokens():
    """`--ds-accent-*-line` is tuned for a border sitting on its OWN tinted
    fill. The chip has no tint — it is plain --ds-surface-dim — so on it the
    success line measured 2.94:1 and the warn line 1.41:1: a state marker you
    cannot see, below even the 3:1 that WCAG 1.4.11 asks of a meaningful
    graphic, let alone the 4.5:1 the 10px text owes.

    The `-ink` pair measures 6.36:1 / 6.88:1 here and 9.68:1 / 11.2:1 in dark.
    The rule, not the numbers, is what this guards: nothing on this row may
    carry state in a `-line` token. (A percentage mix toward transparent was
    the other candidate and is worse than either — the ink token flips
    lightness between themes, so one percentage lands in two different places.)
    """
    css = _code_only(_read(CHAT_CSS))
    row = css[css.index(".msg-source-chip {") : css.index(".msg-source-chip.is-none {")]
    assert "-line)" not in row, "a --ds-accent-*-line token on the sources row — invisible on an untinted chip"
    assert "color-mix" not in row, "a transparent mix resolves differently per theme"
    assert "var(--ds-accent-success-ink)" in row and "var(--ds-accent-warn-ink)" in row

    # The glyph inherits rather than naming a third colour, so it cannot fall
    # out of sync with the ink beside it.
    icon = re.search(r"\.msg-source-icon \{(.*?)\}", css, re.DOTALL)
    assert icon and "color: inherit;" in icon.group(1)


def test_an_answer_resting_only_on_assumptions_declares_no_sources():
    """An assumption is not provenance, so the empty state is keyed on the
    references — an answer resting only on assumptions has, truthfully,
    declared no source. It still shows its assumptions: the row falls through
    rather than returning early.

    TCRD-289 landed the same conclusion independently while this branch was in
    review, and its `provenance` split is the one kept here."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderSourcesChips") : js.index("// ---------- Next-actions block")]
    assert "if (!provenance.length) {" in fn, "the empty state is keyed on references, not on claims"
    assert "none declared" in fn
    # No early return in that branch — the assumptions row below still runs.
    empty = fn[fn.index("if (!provenance.length) {") :]
    empty = empty[: empty.index("\n  }")]
    assert "return" not in empty, "an answer with only assumptions still has assumptions to show"

    rows = _render({"declared": True, "claims": [dict(_ASSUMPTION, ref="paid orders only")]})
    assert len(rows) == 2, "the sources row and the assumptions row"
    assert _label(rows[0]) == "Sources"
    assert [c["text"] for c in _chips(rows[0])] == ["none declared"]
    assert _label(rows[1]) == "Assumptions"
    assert len(_chips(rows[1])) == 1


def test_a_long_source_row_caps_before_it_wraps():
    """Past the cap the rest fold behind "+N more" rather than wrapping the row
    to a second and third line. Under it there is no control at all — the
    common answer is untouched."""
    js = _read(CHAT_JS)
    fn = js[js.index("function renderSourcesChips") : js.index("// ---------- Next-actions block")]
    assert "const _SOURCES_VISIBLE = 4;" in js
    assert "if (chips.length <= _SOURCES_VISIBLE) {" in fn, "no control below the cap"
    assert "_expandInPlace({" in fn, (
        "the same grow-in-place control the tool results use — not a second copy of the list"
    )


def test_the_sources_label_is_not_set_in_the_muted_tone():
    """Measured at 3.93:1 against the bubble with --ds-text-muted — under WCAG
    AA at 10px. This row exists to be read."""
    css = _read(CHAT_CSS)
    block = re.search(r"\.msg-sources-label \{(.*?)\}", _code_only(css), re.DOTALL)
    assert block, ".msg-sources-label moved — re-point this guard"
    assert "--ds-text-secondary" in block.group(1)
    assert "--ds-text-muted" not in block.group(1)


def test_the_unverified_flag_is_not_shrunk_below_the_chip():
    """`font-size: 0.85em` of --text-xs measured 8.5px — the one phrase on the
    row that has to be legible, set smaller than everything around it.

    The flag used to sit INSIDE a chip, where inheriting was parity and any
    font-size at all was the shrink. It is now the row's own summary — said
    once instead of repeated per chip — so it has to set a size, and parity is
    asserted directly: the same token the chips use."""
    css = _read(CHAT_CSS)
    flag = re.search(r"\.msg-source-flag \{(.*?)\}", _code_only(css), re.DOTALL)
    assert flag, ".msg-source-flag moved — re-point this guard"
    chip = re.search(r"\.msg-source-chip \{(.*?)\}", _code_only(css), re.DOTALL)
    assert chip, ".msg-source-chip moved — re-point this guard"
    size = re.search(r"font-size: (var\(--[a-z-]+\));", flag.group(1))
    assert size, "the flag must state its own size now that it is not inside a chip"
    assert f"font-size: {size.group(1)};" in chip.group(1), "the flag is set smaller than the chips it summarises"
    assert not re.search(r"font-size: [\d.]+em", flag.group(1)), (
        "no relative font-size — an em multiple of the chip's --text-xs is how it got to 8.5px"
    )


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


# ── the chip is the link ───────────────────────────────────────────────────
# Checking a number means opening the thing it came from, and the chip that
# names that thing rendered as a dead label — the answer's most obvious next
# click went nowhere (#1974).


def test_table_and_metric_chips_link_to_the_thing_they_name():
    """The workspace prompt asks for the REGISTRY ID on a `table:` line and the
    canonical `family/name` on a `metric:` one, which is what these two
    destinations take. An `assumption:` names nothing to open and stays a
    label."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    fn = js[js.index("function _claimHref") : js.index("function renderSourcesChips")]
    script = (
        fn
        + """
process.stdout.write(JSON.stringify({
  table: _claimHref({kind: 'table', ref: 'hr_headcount'}),
  metric: _claimHref({kind: 'metric', ref: 'headcount/active'}),
  assumption: _claimHref({kind: 'assumption', ref: 'contractors excluded'}),
  empty: _claimHref({kind: 'table', ref: ''}),
  escaped: _claimHref({kind: 'table', ref: 'a b/c?d&e'}),
}));
"""
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["table"] == "/catalog/t/hr_headcount"
    assert res["metric"] == "/semantic-layer?tab=all_metrics&q=headcount%2Factive"
    assert res["assumption"] == "", "an assumption has nothing to open"
    assert res["empty"] == ""
    assert res["escaped"] == "/catalog/t/a%20b%2Fc%3Fd%26e", (
        "the ref is model output landing in a URL — encoded, never pasted"
    )


def test_a_linked_chip_is_an_anchor_and_keeps_its_verification_state():
    js = _read(CHAT_JS)
    fn = js[js.index("function renderSourcesChips") : js.index("// ---------- Next-actions block")]
    assert 'document.createElement(href ? "a" : "span")' in fn, (
        "a chip with somewhere to go is an <a> — not a span with a click handler"
    )
    assert 'chip.className = `msg-source-chip ${state}${href ? " is-link" : ""}`' in fn, (
        "the state class must survive the link class, or the verified/unverified signal is lost"
    )
    css = _read(CHAT_CSS)
    block = css[css.index("a.msg-source-chip.is-link {") :]
    block = block[: block.index("}")]
    # NO colour declaration at all. `color: inherit` looked like the way to keep
    # the state ink and did the opposite: this selector is (0,2,1) against
    # `.msg-source-chip.is-ok`'s (0,2,0), so it won and every linked chip took
    # the surrounding text colour. The state rules are author-level and already
    # outrank the UA's anchor blue, so there is nothing to say here.
    # (Copilot review on #1985.)
    assert "color" not in block, (
        "the chip keeps its own state colour — anything said about colour here outranks the "
        "state rules and erases the verified/unverified signal"
    )


# ── assumptions: origin + why (TCRD-289) ───────────────────────────────────
# Six `assumes …` chips under one SOURCES label, and the reader could not tell
# where any of them came from or why it was made. The server now hands the
# client an `origin` (closed vocabulary) and a `why` per assumption; the
# client draws assumptions on their own row, each with an origin badge and the
# rationale underneath. These run the real renderer in node over a minimal
# DOM, so they test what is drawn — not what the source text contains.

_MINI_DOM = r"""
class El {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.className = "";
    this.textContent = ""; this.title = ""; this.href = ""; this.nodeType = 1;
  }
  appendChild(c) { this.children.push(c); return c; }
  insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? this.children.length : i, 0, c); return c; }
  replaceChildren(...c) { this.children = []; c.forEach((x) => this.appendChild(x)); }
  setAttribute(k, v) { this.attrs = this.attrs || {}; this.attrs[k] = String(v); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
  get text() {
    return this.nodeType === 3 ? this.textContent
      : (this.children.length ? this.children.map((c) => c.text).join(" ") : this.textContent);
  }
  toJSON() {
    if (this.nodeType === 3) return { text: this.textContent };
    return { tag: this.tagName, cls: this.className, title: this.title, href: this.href,
             attrs: this.attrs || {},
             text: this.text, children: this.children.map((c) => c.toJSON()) };
  }
}
const document = {
  createElement: (t) => new El(t),
  createTextNode: (s) => { const n = new El("#text"); n.nodeType = 3; n.textContent = String(s); return n; },
};
// The chip's category glyph and the row's "+N more" control come from the
// shared helpers, which live outside the slice this harness evals. Stubbed to
// the shape the renderer uses: an icon element, and the [meta, button] pair.
function iconEl(name) { const i = new El("svg"); i.className = "icon-" + name; return i; }
function _expandInPlace({ paint, expandLabel }) {
  paint(false);
  const b = new El("button");
  b.className = "msg-source-more";
  b.textContent = expandLabel;
  return [null, b];
}
"""


def _render(verdict: dict) -> list[dict]:
    """Run renderSourcesChips over a fake bubble; return the rows it appended."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    fn = js[js.index("const _CLAIM_LABEL") : js.index("// ---------- Next-actions block")]
    script = (
        _MINI_DOM
        + fn
        + f"""
const bubble = new El("div");
renderSourcesChips(bubble, {json.dumps(verdict)});
process.stdout.write(JSON.stringify(bubble.children.map((c) => c.toJSON())));
"""
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _chips(row: dict) -> list[dict]:
    """Every chip in a row, whether or not it sits in the list wrapper.

    The provenance row nests its chips in `.msg-sources-list` so the
    cap/"+N more" control can repaint just the chips without disturbing the
    label or the trailing summary. The assumptions row has no cap and appends
    its chips directly. Flattening one level covers both."""
    out = []
    for c in row["children"]:
        if "msg-sources-list" in c["cls"]:
            out.extend(k for k in c["children"] if "msg-source-chip" in k["cls"])
        elif "msg-source-chip" in c["cls"]:
            out.append(c)
    return out


def _label(row: dict) -> str:
    return next(c["text"] for c in row["children"] if "msg-sources-label" in c["cls"])


_ASSUMPTION = {
    "kind": "assumption",
    "ref": "signed date proxied by OPPORTUNITY_CLOSE_DATE",
    "verified": None,
    "origin": "data",
    "why": "no executed-SOW date exists in the CRM",
}


def test_assumptions_are_drawn_on_their_own_row_under_sources():
    """A `table:` is something the answer READ; an `assumption:` is something
    it DECIDED. Filed together under one label they read as neither."""
    rows = _render({"declared": True, "claims": [{"kind": "table", "ref": "orders", "verified": True}, _ASSUMPTION]})
    assert [_label(r) for r in rows] == ["Sources", "Assumptions"]
    assert "is-assumptions" in rows[1]["cls"]
    # The category is a glyph on this row now, with the word on `aria-label`
    # (see the icon commit), so the chip's TEXT is the ref alone.
    (prov,) = _chips(rows[0])
    assert prov["text"].strip() == "orders", "the sources row holds provenance only"
    assert prov["attrs"]["aria-label"] == "table orders, verified", "the category is still named"
    assert len(_chips(rows[1])) == 1


def test_an_assumption_chip_shows_its_origin_badge_and_its_rationale():
    (row,) = [r for r in _render({"declared": True, "claims": [_ASSUMPTION]}) if "is-assumptions" in r["cls"]]
    (chip,) = _chips(row)
    assert chip["tag"] == "span", "an assumption names nothing to open — never a link"
    assert "is-assumption" in chip["cls"] and "is-origin-data" in chip["cls"]
    parts = {c["cls"]: c for c in chip["children"]}
    assert parts["msg-source-kind"]["text"] == "assumes"
    badge = parts["msg-source-origin is-origin-data"]
    assert badge["title"], "the badge explains its category on hover"
    assert badge["text"] == "data gap"
    assert parts["msg-source-text"]["text"] == "signed date proxied by OPPORTUNITY_CLOSE_DATE"
    assert parts["msg-source-why"]["text"] == "why no executed-SOW date exists in the CRM"


@pytest.mark.parametrize(
    ("origin", "label"),
    [
        ("user", "from your question"),
        ("definition", "from a definition"),
        ("data", "data gap"),
        ("judgment", "own judgment"),
    ],
)
def test_every_origin_in_the_vocabulary_has_reader_facing_copy(origin, label):
    (row,) = _render({"declared": True, "claims": [{**_ASSUMPTION, "origin": origin}]})[1:]
    (chip,) = _chips(row)
    badge = next(c for c in chip["children"] if "msg-source-origin" in c["cls"])
    assert badge["text"] == label
    assert f"is-origin-{origin}" in badge["cls"]


@pytest.mark.parametrize("origin", [None, "salesforce", ""], ids=["missing", "off-vocabulary", "empty"])
def test_an_assumption_without_a_stated_origin_says_so(origin):
    """The absence made visible — the same rule as "none declared". A legacy
    line (history predating this change, or a model that ignored the segments)
    keeps its statement and gets a dashed badge, not a blank."""
    claim = {"kind": "assumption", "ref": "excludes contractors", "verified": None, "origin": origin, "why": None}
    (row,) = _render({"declared": True, "claims": [claim]})[1:]
    (chip,) = _chips(row)
    assert "is-origin-unstated" in chip["cls"]
    badge = next(c for c in chip["children"] if "msg-source-origin" in c["cls"])
    assert badge["text"] == "origin not stated"
    assert "is-origin-unstated" in badge["cls"]
    assert not any("msg-source-why" in c["cls"] for c in chip["children"]), "no rationale, no why line"


def test_a_pre_tcrd_289_claim_without_the_new_keys_still_renders():
    """`GET /sessions/{id}/messages` recomputes the verdict from saved content,
    so every row carries the keys — but a client must not depend on it."""
    claim = {"kind": "assumption", "ref": "x", "verified": None}
    rows = _render({"declared": True, "claims": [claim]})
    assert [_label(r) for r in rows] == ["Sources", "Assumptions"]
    assert [c["text"] for c in _chips(rows[0])] == ["none declared"], (
        "an answer that named only assumptions has, truthfully, declared no source"
    )


def test_the_client_vocabulary_is_the_servers():
    """`_ASSUMPTION_ORIGIN` in chat.js and `ASSUMPTION_ORIGINS` in
    app/chat/sources.py are one contract written twice. The server normalizes,
    the client labels; a value one knows and the other does not is a badge
    that never appears or a badge with no copy."""
    from app.chat.sources import ASSUMPTION_ORIGINS

    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    fn = js[js.index("const _ASSUMPTION_ORIGIN = {") : js.index("const _ASSUMPTION_ORIGIN_UNSTATED")]
    out = subprocess.run(
        [node, "-e", fn + "process.stdout.write(JSON.stringify(Object.keys(_ASSUMPTION_ORIGIN)));"],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert sorted(json.loads(out.stdout)) == sorted(ASSUMPTION_ORIGINS)


def test_the_origin_badge_is_not_shrunk_below_the_chip():
    """Same defect class as `.msg-source-flag`: the word that places an
    assumption must not be the smallest thing on the row."""
    css = _code_only(_read(CHAT_CSS))
    for selector in (".msg-source-origin", ".msg-source-why", ".msg-source-why-label"):
        block = re.search(re.escape(selector) + r" \{(.*?)\}", css, re.DOTALL)
        assert block, f"{selector} moved — re-point this guard"
        assert "font-size" not in block.group(1)


def test_only_the_judgment_badge_is_amber():
    """Amber on the BADGE, not the chip, and only for the origin the reader
    most needs to weigh — nothing in the question, the definitions or the
    data settles it. The other three are categories, not warnings."""
    css = _code_only(_read(CHAT_CSS))
    judgment = re.search(r"\.msg-source-origin\.is-origin-judgment \{(.*?)\}", css, re.DOTALL)
    assert judgment and "--ds-accent-warn" in judgment.group(1)
    for origin in ("user", "definition", "data"):
        assert f".msg-source-origin.is-origin-{origin}" not in css, f"{origin} must not carry a colour of its own"
    unstated = re.search(r"\.msg-source-origin\.is-origin-unstated \{(.*?)\}", css, re.DOTALL)
    assert unstated and "dashed" in unstated.group(1), "an unstated origin is dashed, like `is-none`"
    chip = re.search(r"\.msg-source-chip\.is-assumption \{(.*?)\}", css, re.DOTALL)
    assert chip and "--ds-accent-warn" not in chip.group(1)


def test_the_assumptions_row_has_no_second_hairline():
    css = _code_only(_read(CHAT_CSS))
    block = re.search(r"\.msg-sources\.is-assumptions \{(.*?)\}", css, re.DOTALL)
    assert block and "border-top: 0" in block.group(1), "two rules under one answer read as two answers"
