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
    renderMarkdownSafe for consistency, every diagram loses its colours.

    The insertion moved into `_buildMermaidFigure` (the figure now carries a
    toolbar, so building it is its own step) and the redraw in
    `rerenderMermaidForTheme` is a second insertion point. Both are checked:
    a sanitizer creeping into either one costs the diagram its palette."""
    js = _read(CHAT_JS)
    build = js[js.index("function _buildMermaidFigure") : js.index("function downloadMermaidSvg")]
    assert "stage.innerHTML = svg;" in build
    redraw = js[js.index("function rerenderMermaidForTheme") : js.index("if (typeof MutationObserver")]
    assert "stage.innerHTML = svg;" in redraw
    for body in (build, redraw):
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
    assert (
        "naming a table you did not query, a term you did not look up, or a file you did not open, "
        "is worse than naming none" in md
    ), (
        "the claim-only-what-you-used rule has to survive in the prompt — it widened to cover "
        "`document:` when that kind was added and `glossary:` when that one was, it did not go away"
    )
    for kind in ("document:", "glossary:"):
        assert kind in md, "every checkable kind has to be taught, or the model smuggles it into `assumption:`"


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
        """Mermaid rendering is async — at chip time it may still be its <pre>.

        Both names matter and NEITHER is `mermaid`: the sanitized fence is
        `<pre><code class="language-mermaid">` and the rendered figure is
        `.msg-mermaid`. The check used to name `pre.mermaid` / `.mermaid`,
        which match neither, so the pre-render form this test exists to cover
        was never actually covered — a diagram-only answer read as having no
        figure at all."""
        js = _chat_js()
        fn = js[js.index("function _bubbleHasFigure") : js.index("function renderSourcesChips")]
        for sel in ("table", "svg", "code.language-mermaid", ".msg-mermaid"):
            assert sel in fn, f"figure check misses {sel}"
        assert "pre.mermaid" not in fn, "a selector that matches nothing is not coverage"


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
  getAttribute(k) { return (this.attrs || {})[k] ?? null; }
  // The collapsed assumptions row reads its own state back off the DOM
  // rather than closing over a `let`, so the harness has to answer both.
  get classList() {
    const self = this;
    const words = () => self.className.split(/\s+/).filter(Boolean);
    const write = (w) => { self.className = w.join(" "); };
    return {
      contains: (c) => words().includes(c),
      add: (c) => { if (!words().includes(c)) write([...words(), c]); },
      remove: (c) => write(words().filter((x) => x !== c)),
      toggle: (c, force) => {
        const want = force === undefined ? !words().includes(c) : force;
        want ? write([...new Set([...words(), c])]) : write(words().filter((x) => x !== c));
        return want;
      },
    };
  }
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
             attrs: this.attrs || {}, hidden: !!this.hidden,
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


def _find(node: dict, cls: str) -> list[dict]:
    """Every descendant carrying `cls`, depth-first.

    Recursive rather than one-level because both rows nest, and differently:
    the provenance row wraps its chips in `.msg-sources-list` so the
    cap/"+N more" control can repaint them without disturbing the label, and
    the assumptions row wraps its LABEL in the toggle button it became when
    the row started life collapsed. A helper that knew either shape would
    have to be re-taught by the next one."""
    out = []
    for c in node.get("children", []):
        if cls in c.get("cls", ""):
            out.append(c)
        else:
            out.extend(_find(c, cls))
    return out


def _chips(row: dict) -> list[dict]:
    """Every chip in a row. Chips never nest, so recursion cannot double."""
    return _find(row, "msg-source-chip")


def _label(row: dict) -> str:
    return _find(row, "msg-sources-label")[0]["text"]


def _parts(chip: dict) -> dict[str, dict]:
    return {c["cls"]: c for c in chip["children"] if c.get("cls")}


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
    parts = _parts(chip)
    badge = parts["msg-source-origin is-origin-data"]
    assert badge["title"], "the badge explains its category on hover"
    assert badge["text"] == "data gap"
    assert parts["msg-source-text"]["text"] == "signed date proxied by OPPORTUNITY_CLOSE_DATE"
    # No "WHY" label and no "assumes" word: both were on EVERY row, which is
    # the repetition the provenance row already dropped. Neither leaves the
    # chip — they ride the accessible name.
    assert parts["msg-source-why"]["text"] == "no executed-SOW date exists in the CRM"
    assert "msg-source-kind" not in parts, "the category word is off the row's face"
    assert not any("msg-source-why-label" in c.get("cls", "") for c in chip["children"])
    assert chip["attrs"]["aria-label"] == ("assumes signed date proxied by OPPORTUNITY_CLOSE_DATE, data gap"), (
        "the category and the origin still reach a screen reader"
    )


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
    for selector in (".msg-source-origin", ".msg-source-why"):
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


# ── documents are provenance, not assumptions ──────────────────────────────
# The reader's half of the same fix: with `document:` in the vocabulary, a
# fact-graph answer's citations belong in the row headed "Sources" — the row
# that was saying "none declared" directly above five named PDFs, because
# provenance was judged on tables and metrics and the model's only legal slot
# for a filename was `assumption:`.

_DOCUMENT = {"kind": "document", "ref": "Q3_Board_Review.pdf", "verified": True}


def test_a_document_chip_lands_in_the_sources_row():
    """Not in the assumptions row, and not in a third row of its own: a
    document is something the answer READ, which is what the Sources row is
    for. The `provenance` split already keys on `kind !== "assumption"`, so
    this is what stops a future kind from silently landing in the caveats."""
    rows = _render({"declared": True, "claims": [_DOCUMENT, dict(_ASSUMPTION)]})
    assert [_label(r) for r in rows] == ["Sources", "Assumptions"]
    (prov,) = _chips(rows[0])
    assert prov["text"].strip() == "Q3_Board_Review.pdf"
    assert len(_chips(rows[1])) == 1, "the assumption stays an assumption"


def test_an_answer_citing_only_documents_does_not_say_none_declared():
    """The defect verbatim: five cited PDFs under the words "none declared".
    A document is a declared source, so the empty state must not fire."""
    rows = _render({"declared": True, "claims": [_DOCUMENT, {"kind": "document", "ref": "b.docx", "verified": True}]})
    texts = [c["text"].strip() for c in _chips(rows[0])]
    assert "none declared" not in texts
    assert texts == ["Q3_Board_Review.pdf", "b.docx"]


def test_a_document_wears_its_own_glyph_and_keeps_the_word_on_its_name():
    """Same bargain the other kinds struck: the category is a glyph on the
    chip's face and the WORD survives on the accessible name, so nothing is
    lost to a screen reader."""
    rows = _render({"declared": True, "claims": [_DOCUMENT]})
    (chip,) = _chips(rows[0])
    icons = [c for c in chip["children"] if "msg-source-icon" in c.get("cls", "")]
    assert icons, "a document chip carries a category glyph like a table or a metric"
    assert chip["attrs"]["aria-label"] == "document Q3_Board_Review.pdf, verified"


def test_an_unverified_document_is_flagged_like_any_other_reference():
    """A filename nothing opened is what a fabricated citation looks like, and
    it counts toward the row's one summarised verdict."""
    rows = _render({"declared": True, "claims": [dict(_DOCUMENT, verified=False)]})
    (chip,) = _chips(rows[0])
    assert "is-unverified" in chip["cls"]
    assert chip["attrs"]["aria-label"].endswith(", unverified")
    flags = [c["text"].strip() for c in rows[0]["children"] if "msg-source-flag" in c.get("cls", "")]
    assert flags == ["1 unverified"]


def test_a_document_chip_is_a_label_not_a_dead_link():
    """#1974's rule is that a chip links when its ref identifies a page. A
    filename does not: document detail is `/library/{slug}/f/{file_id}`, and
    the model knows neither the collection slug nor the file id. So a document
    stays a plain label rather than becoming a link to a guess — the same
    reason an assumption is not one."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    fn = js[js.index("function _claimHref") : js.index("function renderSourcesChips")]
    script = (
        fn
        + """
process.stdout.write(JSON.stringify({
  document: _claimHref({kind: 'document', ref: 'Q3_Board_Review.pdf'}),
  table: _claimHref({kind: 'table', ref: 'orders'}),
}));
"""
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["document"] == "", "a filename does not identify a page"
    assert res["table"] == "/catalog/t/orders", "the kinds that DO resolve still link"

    rows = _render({"declared": True, "claims": [_DOCUMENT]})
    (chip,) = _chips(rows[0])
    assert chip["tag"] == "span" and "is-link" not in chip["cls"]


def test_the_client_knows_every_kind_the_server_can_send():
    """`_CLAIM_LABEL` is what the aria-label is built from — a kind missing
    from it renders its raw wire word to a screen reader. Pinned against the
    server's vocabulary so neither can gain a kind the other never heard of,
    the same contract `test_the_client_vocabulary_is_the_servers` keeps for
    assumption origins."""
    import re as _re

    from app.chat.sources import VERIFIABLE_KINDS

    js = _read(CHAT_JS)
    label = _re.search(r"const _CLAIM_LABEL = \{(.*?)\};", js, _re.DOTALL)
    assert label
    client_kinds = set(_re.findall(r"(\w+):", label.group(1)))
    assert VERIFIABLE_KINDS | {"assumption"} == client_kinds, (
        "the client's claim vocabulary and the server's have drifted"
    )
    # Every checkable kind also needs a glyph; assumptions deliberately have none.
    icon = _re.search(r"const _CLAIM_ICON = \{(.*?)\};", js, _re.DOTALL)
    assert icon
    assert set(_re.findall(r"(\w+):", icon.group(1))) == set(VERIFIABLE_KINDS)


# ── the assumptions row opens closed ───────────────────────────────────────
# An assumption is what you check when you doubt the number, not something you
# read on the way past it. Expanded it was the tallest thing under the answer —
# five full-width pills at 43px, mono, filled — which put the method caveats
# above the answer's own provenance in the reading order. So the row starts
# collapsed, and the label is the control that opens it.


def _arow(claims: list[dict]) -> dict:
    (row,) = [r for r in _render({"declared": True, "claims": claims}) if "is-assumptions" in r["cls"]]
    return row


def _toggle(row: dict) -> dict:
    return _find(row, "msg-assumptions-toggle")[0]


def test_the_assumptions_row_starts_collapsed():
    row = _arow([_ASSUMPTION, dict(_ASSUMPTION, ref="paid orders only")])
    assert "is-collapsed" in row["cls"]
    assert _toggle(row)["attrs"]["aria-expanded"] == "false"
    (lst,) = _find(row, "msg-assumptions-list")
    assert lst["hidden"] is True, "the list is hidden, not absent — the chips stay in the DOM"
    assert len(_chips(row)) == 2, "collapsed hides the list; it does not drop the assumptions"


def test_the_collapsed_row_says_how_much_is_behind_it():
    """Collapsing a thing to nothing is how the "none declared" signal drifted
    in the first place — absence has to stay visible. The count is on the
    toggle, so a reader can see there are caveats without opening them."""
    row = _arow([_ASSUMPTION, dict(_ASSUMPTION, ref="paid orders only"), dict(_ASSUMPTION, ref="EU only")])
    assert _label(row) == "Assumptions"
    (count,) = _find(row, "msg-assumptions-count")
    assert count["text"] == "3"


def test_own_judgment_is_not_buried_by_the_collapse():
    """The one origin TCRD-289 exists to surface: the answer's own choice,
    with nothing in the question, the definitions or the data behind it.
    Hiding that behind a disclosure would undo the point, so it is summarised
    ON the closed toggle — the same once-per-row treatment the provenance row
    gives "N unverified"."""
    row = _arow([dict(_ASSUMPTION, origin="judgment"), dict(_ASSUMPTION, ref="EU only", origin="judgment")])
    (flag,) = _find(row, "msg-assumptions-judged")
    assert flag["text"] == "2 on own judgment"
    assert flag["title"], "the flag explains itself on hover"


def test_a_row_with_no_own_judgment_carries_no_flag():
    """A flag that is always there is not a flag."""
    row = _arow([_ASSUMPTION, dict(_ASSUMPTION, ref="paid orders only", origin="user")])
    assert _find(row, "msg-assumptions-judged") == []


def test_the_toggle_opens_and_closes_the_list():
    """Driven through the real handler in node, not asserted off the source."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    fn = js[js.index("const _CLAIM_LABEL") : js.index("// ---------- Next-actions block")]
    verdict = {"declared": True, "claims": [_ASSUMPTION]}
    script = (
        _MINI_DOM
        + fn
        + f"""
const bubble = new El("div");
renderSourcesChips(bubble, {json.dumps(verdict)});
const row = bubble.children.find((c) => c.className.includes("is-assumptions"));
const toggle = row.children.find((c) => c.className.includes("msg-assumptions-toggle"));
const list = row.children.find((c) => c.className.includes("msg-assumptions-list"));
const snap = () => ({{
  expanded: toggle.attrs["aria-expanded"], hidden: !!list.hidden,
  collapsed: row.className.includes("is-collapsed"),
}});
const before = snap();
toggle.onclick();
const opened = snap();
toggle.onclick();
process.stdout.write(JSON.stringify({{ before, opened, reclosed: snap() }}));
"""
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["before"] == {"expanded": "false", "hidden": True, "collapsed": True}
    assert res["opened"] == {"expanded": "true", "hidden": False, "collapsed": False}
    assert res["reclosed"] == res["before"], "the control has to close what it opened"


def test_an_assumption_is_a_row_not_a_pill():
    """The measured defect: as a `.msg-source-chip` it inherited a pill —
    fill, border, radius, mono — and five of them outweighed the provenance
    above. Rows carry the grouping on a hairline instead. The three
    properties are the rule; the exact values are not."""
    css = _code_only(_read(CHAT_CSS))
    block = re.search(r"\.msg-source-chip\.is-assumption \{(.*?)\}", css, re.DOTALL)
    assert block
    body = block.group(1)
    assert "background: none" in body, "an assumption row carries no fill of its own"
    assert "border: 0" in body and "border-left:" in body, "the pill's border becomes one hairline"
    assert "border-radius: 0" in body, "a rounded full-width row reads as a control"
    assert "var(--ds-font)" in body, "prose about method is not an identifier — not mono"


def test_the_hidden_list_can_actually_be_hidden():
    """The half a DOM test cannot see, and the one that shipped broken.

    The JS sets `hidden` correctly either way, so every structural test above
    passes whether or not the row actually collapses — only real CSS decides.
    Two ways it can be defeated, and both were live at some point here:

    1. The list borrowing `.msg-sources-list`, which is `display: contents`.
       An element that generates no box has no box to hide, so `hidden` is
       inert on it.
    2. Any author `display` on the list at all. It beats the UA stylesheet's
       `[hidden] { display: none }` on cascade ORIGIN — no specificity on the
       author rule changes that — so the row opens expanded with `hidden`
       set and ignored.

    So: the list must not carry the shared class, and the `[hidden]`
    companion rule must be present to survive a later `display:` being added.
    """
    js = _read(CHAT_JS)
    assert 'alist.className = "msg-assumptions-list";' in js, (
        "the assumptions list must not borrow `msg-sources-list` — that class is "
        "`display: contents`, which `hidden` cannot suppress"
    )
    css = _code_only(_read(CHAT_CSS))
    shared = re.search(r"\.msg-sources-list \{(.*?)\}", css, re.DOTALL)
    assert shared and "display: contents" in shared.group(1), "re-point this guard"
    own = re.search(r"\.msg-assumptions-list \{(.*?)\}", css, re.DOTALL)
    assert own and "display" not in own.group(1), (
        "an author `display` here outranks the UA [hidden] rule on cascade origin"
    )
    assert re.search(r"\.msg-assumptions-list\[hidden\] \{\s*display: none", css), (
        "without an explicit `.msg-assumptions-list[hidden] { display: none }` a later "
        "`display:` on the rule above silently re-breaks the collapse"
    )


# ── a glossary term is a citation, not a caveat (#2258) ────────────────────
# The vocabulary had no word for a governed business term, so a term the
# answer leaned on could only surface as `assumption: … | origin: definition`
# — a citation filed as a caveat about method, which is the category error
# the prompt already warns about for a file. These run the real server parser
# into the real renderer: the block goes in, a chip comes out.

_GLOSSARY_ANSWER = "Headcount is 412 FTE.\n\n```sources\nglossary: Full-time equivalent\n```\n"
_GLOSSARY_CALLS = [{"tool": "Bash", "args": {"command": 'agnes glossary search "full-time equivalent"'}}]


def _glossary_verdict(answer: str = _GLOSSARY_ANSWER, calls=None) -> dict:
    from app.chat.sources import verdict

    return verdict(answer, _GLOSSARY_CALLS if calls is None else calls).to_dict()


def test_a_cited_term_travels_from_the_block_to_a_chip():
    """The whole path, end to end: the server parses the `glossary:` line out
    of the answer, checks it against the turn's tool calls, and the client
    draws it as a chip in the SOURCES row — not among the assumptions, where
    it used to be the only slot the vocabulary left it."""
    v = _glossary_verdict()
    assert [(c["kind"], c["verified"]) for c in v["claims"]] == [("glossary", True)]
    rows = _render(v)
    assert [_label(r) for r in rows] == ["Sources"], "a term is provenance, not a caveat"
    (chip,) = _chips(rows[0])
    assert chip["text"].strip() == "Full-time equivalent"
    assert "is-ok" in chip["cls"]
    assert chip["attrs"]["aria-label"] == "glossary Full-time equivalent, verified"
    icons = [c for c in chip["children"] if "msg-source-icon" in c.get("cls", "")]
    assert icons, "a term chip carries a category glyph like a table, a metric or a document"


def test_a_term_nothing_looked_up_is_flagged_like_any_other_reference():
    """A governed-sounding term nothing ran on is what an invented definition
    looks like, and it counts toward the row's one summarised verdict."""
    v = _glossary_verdict("An FTE is 40h.\n\n```sources\nglossary: Fully burdened cost\n```\n")
    assert [c["verified"] for c in v["claims"]] == [False]
    rows = _render(v)
    (chip,) = _chips(rows[0])
    assert "is-unverified" in chip["cls"]
    assert chip["attrs"]["aria-label"].endswith(", unverified")
    flags = [c["text"].strip() for c in rows[0]["children"] if "msg-source-flag" in c.get("cls", "")]
    assert flags == ["1 unverified"]


def test_an_answer_citing_only_terms_does_not_say_none_declared():
    """The defect verbatim, in the row: a governed definition IS a declared
    source, so the empty state must not fire above one."""
    rows = _render(_glossary_verdict())
    assert "none declared" not in [c["text"].strip() for c in _chips(rows[0])]


def test_a_term_chip_opens_the_glossary_not_the_metrics_tab():
    """The chip's destination is the glossary's OWN surface.

    `#1974`'s rule is that a chip links where its ref identifies a page, and a
    term does: `/semantic-layer?tab=all_glossary` renders `glossary_terms`
    and filters client-side on `?q=`, the same shape the metric chip uses one
    tab over.

    Which tab is not cosmetic. The metrics tab is narrowed per caller — every
    metric bound to a table outside the caller's Data Package stack is dropped
    before render — while the glossary is deliberately not gated that way
    (business vocabulary, not data). Pointing a term at `all_metrics` would
    answer a glossary citation with a metrics-shaped, per-caller-filtered read
    that can only ever miss the term, so the two destinations must stay
    distinct.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = _read(CHAT_JS)
    fn = js[js.index("function _claimHref") : js.index("function renderSourcesChips")]
    script = (
        fn
        + """
process.stdout.write(JSON.stringify({
  glossary: _claimHref({kind: 'glossary', ref: 'Full-time equivalent'}),
  metric: _claimHref({kind: 'metric', ref: 'headcount/active'}),
  empty: _claimHref({kind: 'glossary', ref: ''}),
  escaped: _claimHref({kind: 'glossary', ref: 'gross & net margin?'}),
}));
"""
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout)
    assert res["glossary"] == "/semantic-layer?tab=all_glossary&q=Full-time%20equivalent"
    assert "all_metrics" not in res["glossary"], "a term must not be resolved through the metrics read"
    assert res["metric"] == "/semantic-layer?tab=all_metrics&q=headcount%2Factive", "the metric chip is unchanged"
    assert res["empty"] == ""
    assert res["escaped"] == "/semantic-layer?tab=all_glossary&q=gross%20%26%20net%20margin%3F", (
        "the ref is model output landing in a URL — encoded, never pasted"
    )

    rows = _render(_glossary_verdict())
    (chip,) = _chips(rows[0])
    assert chip["tag"] == "a" and "is-link" in chip["cls"]
    assert "is-ok" in chip["cls"], "the link class must not cost the chip its verification state"


def test_the_two_registries_the_chips_point_at_keep_their_own_read_rules():
    """Why the destinations above may not be swapped, asserted on the rules
    themselves rather than on prose.

    `GET /api/glossary*` is any authenticated caller with no per-resource
    narrowing; `GET /api/metrics` drops every metric whose table is outside
    the caller's stack (#953). The day either changes, the chip that points at
    it needs rethinking — so pin both here, next to the link they justify.
    """
    import inspect

    from app.api.glossary import get_glossary_term, list_glossary_terms, search_glossary_terms
    from app.api.metrics import list_metrics

    for fn in (list_glossary_terms, search_glossary_terms, get_glossary_term):
        src = inspect.getsource(fn)
        assert "get_accessible_tables" not in src and "_first_inaccessible_table" not in src, (
            f"{fn.__name__} now narrows per caller — the glossary chip's destination assumes it does not"
        )
    assert "_first_inaccessible_table" in inspect.getsource(list_metrics), (
        "the metrics read is the FILTERED one — if that stopped being true, re-read the chip destinations"
    )


def test_the_prompt_offers_the_kind_and_says_a_term_is_not_an_assumption():
    """The parser's vocabulary and the text that teaches it are one contract.
    A kind the model is never told about is a kind it keeps smuggling into
    `assumption:`, which is the defect — and the assumption bullet has to say
    so outright, the way it already does for a file."""
    md = _read(WORKSPACE_CLAUDE_MD)
    section = md[md.index("Say where every number came from") :]
    assert "`glossary:`" in section
    fence = section[section.index("```sources") :]
    fence = fence[: fence.index("```", 3)]
    assert "glossary:" in fence, "the example the model imitates has to show the line"
    assert "`glossary:`" in section[section.index("- `assumption:`") :], (
        "the assumption bullet must name the term's real slot, as it already does for a document"
    )
    checked = re.sub(r"\s+", " ", section)
    assert "`glossary:` and `document:` is checked against the tools you actually ran" in checked, (
        "the model has to be told the new kind is checked, or it reads as decoration"
    )
    assert "not its id" in checked, (
        "the ref has to be the TERM: the chip's destination filters the glossary tab on `?q=` "
        "over rows indexed by term/definition/see-also — an id would open an empty list, and a "
        "chip that lands on nothing is the dead label #1974 removed"
    )
