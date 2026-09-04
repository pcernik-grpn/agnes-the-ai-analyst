"""Diagrams in chat: the palette follows the page, a wide diagram stays
readable, and a half-arrived fence is not called broken.

The rendering half of ` ```mermaid ` support. The sanitizer collision and the
"is it loaded with the page" guards live in tests/test_chat_sources_ui.py and
stay there; what this file adds is the behaviour that made diagrams actually
usable rather than merely present:

  * **Theme.** The palette was read off `dataset.colorScheme`, which nothing
    in this app ever sets — `_theme_resolve.html` writes `data-theme`. So the
    read matched nothing on every instance and every diagram drew in mermaid's
    light palette, dark text on a dark ground included. Fixing the attribute
    is half of it; the other half is that mermaid bakes colours into the
    markup, so a diagram already on screen has to be redrawn when the theme
    changes under it.
  * **Fit.** Mermaid sizes its output for the 900px box it lays out in, so in
    a ~700px bubble a diagram was clipped and scrolled sideways instead of
    simply being drawn smaller.
  * **Truncation.** A turn sealed mid-fence (a tool card lands while the
    diagram is still arriving) parsed as broken and got an error note that
    finalize then silently replaced with the diagram. A self-correcting error
    message is worse than nothing.

Pure helpers are executed under node — the shipped functions, sliced out of
the shipped file, never a copy. Everything else is a content assertion on a
call shape that is easy to undo by accident, following the pattern of the two
sibling chat-UI test modules.
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
CLAUDE_MD_TEMPLATE = Path("config/claude_md_template.txt")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _code_only(text: str) -> str:
    """Source with comments stripped — a rule's own explanation necessarily
    contains the token the rule forbids."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)


def _pure_helpers() -> str:
    """The self-contained, DOM-free block of the mermaid section.

    Sliced rather than duplicated: a copy in the test file would keep passing
    after the shipped function changed, which is the one thing these tests
    exist to prevent.
    """
    js = _read(CHAT_JS)
    start = js.index("// ── pure helpers ───")
    end = js.index("// ── theme ───")
    return js[start:end]


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


# ── fit: a diagram shrinks to the column instead of being clipped ───────────


def test_make_responsive_svg_executable():
    """Mermaid lays out in a 900px sandbox and stamps that width on the root
    <svg>. In a narrower bubble the result was a horizontal scrollbar over a
    cropped diagram — the reader lost the shape, which is the entire reason to
    draw one."""
    cases = {
        "fixed_size": '<svg id="a" width="900" height="400" viewBox="0 0 900 400"><g/></svg>',
        "no_height": '<svg width="100%" viewBox="0 0 640 300"><g/></svg>',
        "keeps_style_vars": '<svg width="800" style="--bg: #fff; max-width: 800px;" viewBox="0 0 800 200"><g/></svg>',
        "inner_icon_untouched": ('<svg viewBox="0 0 500 100"><svg class="icon" width="24" height="24"></svg></svg>'),
        "no_viewbox": '<svg width="300" height="100"><g/></svg>',
    }
    script = (
        _pure_helpers()
        + "\nprocess.stdout.write(JSON.stringify(Object.fromEntries("
        + f"Object.entries({json.dumps(cases)}).map(([k, v]) => [k, makeResponsiveSvg(v)]))));\n"
    )
    res = json.loads(_node_run(script))

    assert 'width="100%"' in res["fixed_size"]
    assert 'height="400"' not in res["fixed_size"], "a fixed height defeats the max-width"
    assert "max-width: 900px" in res["fixed_size"], "capped at its natural size on a wide screen"

    assert "max-width: 640px" in res["no_height"]

    assert "--bg: #fff" in res["keeps_style_vars"], (
        "mermaid derives its colours from custom properties in this attribute — "
        "dropping it resolves every derived colour to black"
    )
    assert res["keeps_style_vars"].count("max-width") == 1, "the stale max-width is replaced, not doubled"

    assert '<svg class="icon" width="24" height="24">' in res["inner_icon_untouched"], (
        "only the ROOT tag is rewritten; an inner icon's fixed size IS its layout"
    )

    # No viewBox: the tag is returned untouched. Width and height ARE the sizing
    # in that case (`hasDrawnContent` documents the same case), and there is no
    # max-width to put back — stripping them would leave an <svg> with no
    # intrinsic height, which collapses to the CSS default instead of scaling.
    assert res["no_viewbox"] == '<svg width="300" height="100"><g/></svg>', (
        "a diagram sized by width/height must keep them; .msg-mermaid-stage overflow is the fallback"
    )


def test_the_root_svg_regex_is_anchored():
    """The bug this prevents is specific: with an unanchored match, a root tag
    carrying no height (mermaid emits `width="100%"` alone) meant the FIRST
    inner icon's height was stripped instead — detaching that icon."""
    helpers = _pure_helpers()
    assert "_ROOT_SVG_TAG = /^(" in helpers, "must be anchored to the start of the string"
    assert "_ROOT_VIEWBOX = /^" in helpers


# ── truncation: an unfinished diagram is not a broken one ───────────────────


def test_is_truncated_diagram_executable():
    """Mermaid's jison parsers report end-of-input as token 1; real syntax
    errors fail on a real token. Only that distinction keeps a mid-stream seal
    from printing an error the next frame retracts."""
    cases = {
        "truncated": {"hash": {"token": 1}},
        "real_syntax_error": {"hash": {"token": 27}},
        "no_hash": {"message": "boom"},
        "null": None,
    }
    script = (
        _pure_helpers()
        + "\nprocess.stdout.write(JSON.stringify(Object.fromEntries("
        + f"Object.entries({json.dumps(cases)}).map(([k, v]) => [k, isTruncatedDiagram(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["truncated"] is True
    assert res["real_syntax_error"] is False, "a genuinely broken diagram must still show its note"
    assert res["no_hash"] is False
    assert res["null"] is False


def test_a_truncated_diagram_gets_no_error_note():
    js = _read(CHAT_JS)
    body = js[js.index("function renderMermaidBlocks") : js.index("function rerenderMermaidForTheme")]
    assert "if (isTruncatedDiagram(err)) continue;" in body, (
        "an incomplete fence must be left alone — finalize renders it a moment later"
    )
    assert "msg-mermaid-error" in body, "a genuinely broken diagram still says so"


def test_the_error_note_is_not_stacked_on_re_render():
    """The same bubble is walked more than once (seal, finalize, history
    reload). Without the guard a broken diagram collected one note per pass."""
    js = _read(CHAT_JS)
    body = js[js.index("function renderMermaidBlocks") : js.index("function rerenderMermaidForTheme")]
    assert 'previousElementSibling.classList.contains("msg-mermaid-error")' in body


def test_an_empty_layout_is_treated_as_a_failure():
    """A degenerate `viewBox="0 0 0 0"` is a silent layout failure. Cached and
    inserted, it is an empty box the reader cannot tell from a bug in Agnes."""
    cases = {
        "degenerate": '<svg viewBox="0 0 0 0"></svg>',
        "drawn": '<svg viewBox="0 0 640 300"><g/></svg>',
        "no_viewbox": '<svg width="300" height="100"></svg>',
        "inner_only": '<svg width="10"><svg viewBox="0 0 0 0"></svg></svg>',
    }
    script = (
        _pure_helpers()
        + "\nprocess.stdout.write(JSON.stringify(Object.fromEntries("
        + f"Object.entries({json.dumps(cases)}).map(([k, v]) => [k, hasDrawnContent(v)]))));\n"
    )
    res = json.loads(_node_run(script))
    assert res["degenerate"] is False
    assert res["drawn"] is True
    assert res["no_viewbox"] is True, "some diagram types are sized by width/height instead"
    assert res["inner_only"] is True, "root tag only — a nested viewBox is not the diagram's"


# ── theme ───────────────────────────────────────────────────────────────────


def test_the_palette_reads_the_attribute_this_app_actually_sets():
    """`dataset.colorScheme` is set by nothing in this codebase. The read
    always came back undefined, so `theme` always resolved to the light
    default — on a dark instance that is dark text on a dark ground."""
    js = _read(CHAT_JS)
    assert "dataset.colorScheme" not in _code_only(js), (
        "nothing writes colorScheme; _theme_resolve.html writes data-theme"
    )
    assert "function _mermaidThemeKey" in js
    assert "document.documentElement.dataset.theme" in js
    resolve = _read(Path("app/web/templates/_theme_resolve.html"))
    assert "el.dataset.theme =" in resolve, "the attribute this keys off must be the one that is written"


def test_the_diagram_palette_is_built_from_design_tokens():
    """Mermaid's own light/dark pair matches none of Agnes's four themes.
    Building on `base` and mapping every colour to a `--ds-*` token is what
    makes a diagram read as part of the page — and carries a re-skin for
    free, since the tokens are what changes."""
    js = _read(CHAT_JS)
    cfg = js[js.index("function _mermaidConfig") : js.index("// ── loading + rendering")]
    assert 'theme: "base"' in cfg, "mermaid's built-in themes are fixed palettes; base + variables is not"
    for token in ("--ds-surface", "--ds-border", "--ds-primary", "--ds-text-primary", "--ds-font"):
        assert token in cfg, f"palette must be derived from {token}"
    assert 'securityLevel: "strict"' in cfg, "the diagram source stays untrusted"
    assert "noteTextColor: warnInk" in cfg, (
        "a highlight box takes its ink from the token paired with its background — "
        "--ds-text-primary is near-white on a dark instance, which measured 1.47:1"
    )
    assert "tertiaryColor: surface" in cfg, (
        "an ER relationship label is a label, not a warning; on the warn tint it was unreadable"
    )


def test_a_theme_switch_redraws_the_diagrams_on_screen():
    """Colours are baked into mermaid's markup, so without this a switch left
    every diagram already rendered as an island in the previous palette until
    the thread was reloaded."""
    js = _read(CHAT_JS)
    assert "function rerenderMermaidForTheme" in js
    assert 'attributeFilter: ["data-theme"]' in js, (
        "observing the attribute catches the user menu AND a mid-session OS change, "
        "without the toggle needing to know diagrams exist"
    )
    assert "fig.dataset.mermaidSrc" in js, "the source rides on the node so a redraw needs no re-parse"
    obs = js[js.index("if (typeof MutationObserver") :]
    assert "if (now === _lastMermaidTheme) return;" in obs, (
        "data-theme-variant and other attribute writes must not trigger a redraw"
    )


def test_the_render_cache_is_keyed_by_theme_and_bounded():
    js = _read(CHAT_JS)
    assert 'const key = themeKey + "\\n" + source;' in js, (
        "the palette is baked in, so the same source under another theme is a different SVG"
    )
    assert js.count('const key = themeKey + "\\n" + source;') == 2, "render and redraw must agree on the key"
    assert "_MERMAID_CACHE_MAX" in js, "a long-lived tab must not accumulate every diagram it ever showed"


# ── the toolbar: a diagram worth drawing rarely fits the column ─────────────


def test_a_diagram_can_be_expanded_copied_and_saved():
    js = _read(CHAT_JS)
    assert "function openMermaidLightbox" in js
    assert "function downloadMermaidSvg" in js
    build = js[js.index("function _buildMermaidFigure") : js.index("function downloadMermaidSvg")]
    for label in ('"Expand"', '"Copy"', '"SVG"'):
        assert label in build
    assert 'b.setAttribute("aria-label", title)' in build, "icon-free buttons still need a name for AT"
    assert '"```mermaid\\n" + source + "\\n```"' in build, (
        "copy hands back the fence the agent wrote, not the generated SVG"
    )


def test_the_saved_svg_does_not_depend_on_a_browser_to_show_its_labels():
    """Mermaid draws every label as HTML in a `<foreignObject>` — 101 of them
    in a mid-sized ER diagram — and `<foreignObject>` is an optional part of
    the SVG spec that standalone consumers (design tools, server-side
    rasterizers) commonly skip, drawing the boxes and dropping every label.
    Re-rendering with `htmlLabels: false` lays the same diagram out with real
    `<text>`/`<tspan>`, i.e. core SVG every consumer implements."""
    js = _read(CHAT_JS)
    dl = js[js.index("function downloadMermaidSvg") : js.index("function openMermaidLightbox")]
    assert "cfg.htmlLabels = false;" in dl
    assert "htmlLabels: false" in dl, "flowchart carries its own htmlLabels switch"
    assert "mermaid.initialize(_mermaidConfig());" in dl, (
        "the renderer must be restored, or the next diagram inherits the export settings"
    )
    assert ".catch(fallback)" in dl, "a failed re-render saves the on-screen markup rather than nothing"


def test_the_lightbox_is_dismissable_and_leaves_the_message_intact():
    js = _read(CHAT_JS)
    box = js[js.index("function openMermaidLightbox") : js.index("/** Swap every ```mermaid")]
    assert "svg.cloneNode(true)" in box, "the message must still have its diagram after closing"
    assert "panel.appendChild(canvas)" in box, (
        "pan/zoom transforms the canvas INSIDE the panel, so the diagram's own "
        "ground stays put and clips instead of sliding away with it"
    )
    assert 'e.key === "Escape"' in box
    assert 'document.removeEventListener("keydown", onKey)' in box, "no listener may outlive the dialog"
    assert "if (e.target === back) dismiss()" in box, (
        "a click that merely ended a drag across the diagram must not close it"
    )
    assert 'back.setAttribute("aria-modal", "true")' in box


def test_the_toolbar_is_styled_from_tokens_and_stays_keyboard_reachable():
    css = _read(CHAT_CSS)
    assert ".msg-mermaid-bar" in css
    assert ".msg-mermaid:focus-within .msg-mermaid-bar" in css, "hover-only chrome is unreachable from the keyboard"
    assert ".msg-mermaid-lightbox" in css
    # Comments stripped: the rule's own explanation names the token it forbids.
    scrim = _code_only(css[css.index(".msg-mermaid-lightbox {") : css.index(".msg-mermaid-panel {")])
    assert "--ds-text-primary" not in scrim, (
        "that token inverts with the theme — mixing it produced a near-WHITE "
        "wash on a dark instance, and the page read straight through the dialog"
    )
    assert "rgba(0, 0, 0" in scrim, "a full-screen scrim has to be theme-stable"
    block = css[css.index("/* Mermaid output.") : css.index(".msg-time {")]
    assert not re.search(r":\s*#[0-9a-fA-F]{3,8}\b", _code_only(block)), (
        "design-system contract: --ds-* tokens only, no raw hex"
    )


# ── the prompt: a diagram nobody draws helps nobody ─────────────────────────


def test_the_prompt_asks_for_a_diagram_rather_than_permitting_one():
    """The rendering exists either way; what decides whether a reader ever
    sees a diagram is whether the agent reaches for one unprompted."""
    for path in (WORKSPACE_CLAUDE_MD, CLAUDE_MD_TEMPLATE):
        # Prose is hard-wrapped at ~76 columns, so every assertion here runs
        # against whitespace-normalized text — a phrase that happens to
        # straddle a line break is not a missing phrase.
        md = re.sub(r"\s+", " ", _read(path))
        assert "**Draw one whenever the answer is about how things connect.**" in md, path
        assert "don't offer to draw one instead of drawing it" in md, path
        assert "mermaid draws relationships and cannot plot values" in md, (
            f"the chart/diagram split must survive the rewrite ({path})"
        )


def test_the_prompt_names_the_types_that_fail_here():
    """Each of these is something that renders as an error or a blank rather
    than a diagram, so naming them is cheaper than the failed turn."""
    for path in (WORKSPACE_CLAUDE_MD, CLAUDE_MD_TEMPLATE):
        md = re.sub(r"\s+", " ", _read(path))
        # Backticked, so a bare "pie" elsewhere in the prompt cannot pass this.
        for bad in ("`C4Context`", "`pie`", "`mindmap`", "`gitGraph`", "`architecture-beta`"):
            assert bad in md, f"{bad} not warned against in {path}"
        assert "dateFormat YYYY-MM-DD" in md, f"gantt time-only formats fail silently ({path})"
        assert 'icon: "lucide:name"' in md and "fa:fa-*" in md, (
            f"no icon pack is registered here, so both icon syntaxes leave a blank ({path})"
        )


def test_the_cli_branch_still_says_a_terminal_cannot_render_one():
    """The template serves both surfaces. Telling a CLI agent to draw
    diagrams would be telling it to emit source nothing renders."""
    md = re.sub(r"\s+", " ", _read(CLAUDE_MD_TEMPLATE))
    assert "a terminal has no renderer for it" in md
