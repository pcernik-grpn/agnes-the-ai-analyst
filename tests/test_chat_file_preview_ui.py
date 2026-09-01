"""The shared file-preview modal, run under node against a DOM stub.

``app/web/static/js/file_preview.js`` was the Library's modal and is now also
the chat session-files drawer's, which makes two things worth pinning:

1. The ``slides`` kind renders. It is the whole reason the chat can preview a
   deliverable at all — a browser has no renderer for ``.pptx``, so the server
   sends the deck's words and this file draws them. A test that only greps the
   source for ``'slides'`` would pass on a renderer that throws.
2. Server strings still land via ``textContent``. Slide titles and bullets are
   agent-authored text arriving over JSON; the day one of them reaches
   ``innerHTML`` the drawer becomes stored XSS against its own reader.

Same ``node -e`` pattern as ``tests/test_chat_files_drawer_ui.py`` — there is
no DOM harness in CI, so the stub stands in for the document and the fetch and
the assertions are about the tree the renderer actually built.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PREVIEW_JS = Path("app/web/static/js/file_preview.js")
PREVIEW_CSS = Path("app/web/static/css/file_preview.css")
CHAT_HTML = Path("app/web/templates/chat.html")
CHAT_JS = Path("app/web/static/js/chat.js")


def _node_run(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


#: A DOM small enough to read and complete enough for the renderer to run.
#: `innerHTML` is a plain recorded property: the static SVG markup the modal
#: sets is legitimate, and a test can then assert that NOTHING else used it.
_HARNESS = """
function mkEl(tag) {
  const node = {
    tag, className: '', innerHTML: '', src: '', href: '',
    alt: '', title: '', type: '', kids: [], attrs: {}, dataset: {},
    parentNode: null, _text: '',
    appendChild(c) { c.parentNode = this; this.kids.push(c); return c; },
    removeChild(c) { this.kids = this.kids.filter((k) => k !== c); return c; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    addEventListener() {},
    focus() {},
  };
  // Assigning textContent REPLACES the children — which is exactly how the
  // renderer clears the "Loading preview…" placeholder before drawing. A stub
  // that treated it as an inert string would leave the placeholder in the
  // tree and quietly hide whether the render happened at all.
  Object.defineProperty(node, 'textContent', {
    get() { return node._text; },
    set(v) { node._text = String(v); node.kids = []; },
  });
  return node;
}
const document = {
  body: mkEl('body'),
  activeElement: null,
  createElement: mkEl,
  addEventListener() {},
  removeEventListener() {},
};
const window = {};
let _payload = null;
function fetch() {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(_payload) });
}

// Walk the built tree into something assertable from Python.
function dump(node) {
  return {
    tag: node.tag,
    cls: node.className,
    text: node.textContent,
    html: node.innerHTML,
    src: node.src,
    href: node.href,
    kids: node.kids.map(dump),
  };
}
function find(node, cls) {
  if (node.className === cls) return node;
  for (const k of node.kids) { const hit = find(k, cls); if (hit) return hit; }
  return null;
}
function collect(node, cls, out) {
  out = out || [];
  if (node.className === cls) out.push(node);
  for (const k of node.kids) collect(k, cls, out);
  return out;
}
"""


def _render(payload: dict, opts: dict | None = None) -> dict:
    """Open the modal on ``payload`` and return the built tree."""
    script = (
        _HARNESS
        + PREVIEW_JS.read_text(encoding="utf-8")
        + "\n_payload = "
        + json.dumps(payload)
        + ";\n"
        + "window.openFilePreview("
        + json.dumps({"previewUrl": "/api/chat/sessions/c1/files/preview?path=outputs/deck.pptx", **(opts or {})})
        + ");\n"
        # The renderer runs off a promise chain; one macrotask is enough.
        + "setTimeout(() => { console.log(JSON.stringify(dump(document.body))); }, 0);\n"
    )
    return json.loads(_node_run(script))


def _flatten(node: dict, out: list | None = None) -> list:
    out = [] if out is None else out
    out.append(node)
    for kid in node["kids"]:
        _flatten(kid, out)
    return out


def test_a_deck_renders_one_card_per_slide_in_order() -> None:
    tree = _render(
        {
            "kind": "slides",
            "name": "engagement_type_breakdown.pptx",
            "file_type": "pptx",
            "truncated": False,
            "slides": [
                {"index": 1, "title": "Engagement Type Breakdown", "lines": ["Current Portfolio Overview"]},
                {"index": 2, "title": "AIVB Sub-Types", "lines": ["Rapid: 14", "Full: 7"]},
            ],
        }
    )
    nodes = _flatten(tree)
    numbers = [n["text"] for n in nodes if n["cls"] == "fp-slide__num"]
    titles = [n["text"] for n in nodes if n["cls"] == "fp-slide__title"]
    assert numbers == ["Slide 1", "Slide 2"]
    assert titles == ["Engagement Type Breakdown", "AIVB Sub-Types"]

    bullets = [n["text"] for n in nodes if n["tag"] == "li"]
    assert bullets == ["Current Portfolio Overview", "Rapid: 14", "Full: 7"]

    # The deck's words are a glance, and the note has to say so — otherwise a
    # reader takes an unstyled bullet list for the rendered slide.
    note = next(n for n in nodes if n["cls"] == "fp-note")
    assert "not the rendered layout" in note["text"]


def test_slide_text_never_reaches_innerhtml() -> None:
    """The one security property of this renderer. Slide text is agent-authored
    and arrives over JSON; the only innerHTML in the modal is the static header
    glyph, and it must stay the only one."""
    tree = _render(
        {
            "kind": "slides",
            "name": "x.pptx",
            "slides": [{"index": 1, "title": "<img src=x onerror=alert(1)>", "lines": ["<script>alert(2)</script>"]}],
        }
    )
    nodes = _flatten(tree)
    assert any(n["text"] == "<img src=x onerror=alert(1)>" for n in nodes)
    assert any(n["text"] == "<script>alert(2)</script>" for n in nodes)
    # Only the head glyph's static <svg> uses innerHTML anywhere in the tree.
    with_html = [n["html"] for n in nodes if n["html"]]
    assert len(with_html) == 1
    assert with_html[0].startswith("<svg")
    assert "onerror" not in with_html[0]


def test_a_truncated_deck_says_it_is_showing_the_first_slides() -> None:
    tree = _render(
        {
            "kind": "slides",
            "name": "long.pptx",
            "truncated": True,
            "slides": [{"index": 1, "title": "One", "lines": []}],
        }
    )
    note = next(n for n in _flatten(tree) if n["cls"] == "fp-note")
    assert "Showing the first slides." in note["text"]


def test_a_slide_with_no_text_says_so_rather_than_rendering_an_empty_card() -> None:
    tree = _render({"kind": "slides", "name": "x.pptx", "slides": [{"index": 1, "title": "", "lines": []}]})
    empties = [n["text"] for n in _flatten(tree) if n["cls"] == "fp-slide__empty"]
    assert empties == ["No text on this slide."]


def test_an_image_kind_still_draws_from_raw_url() -> None:
    """The Library's kinds must keep working — this file now has two callers."""
    tree = _render({"kind": "image", "name": "chart.png", "raw_url": "/api/chat/sessions/c1/files/raw?path=chart.png"})
    img = next(n for n in _flatten(tree) if n["cls"] == "fp-img")
    assert img["src"] == "/api/chat/sessions/c1/files/raw?path=chart.png"


def test_a_pdf_kind_frames_the_raw_url() -> None:
    tree = _render({"kind": "pdf", "name": "report.pdf", "raw_url": "/api/chat/sessions/c1/files/raw?path=report.pdf"})
    frame = next(n for n in _flatten(tree) if n["cls"] == "fp-frame")
    assert frame["tag"] == "iframe"
    assert frame["src"].endswith("path=report.pdf")


def test_a_none_kind_shows_the_servers_own_sentence_verbatim() -> None:
    """The client must not invent its own copy for "cannot preview this" — the
    server knows why (wrong format, too large, unreadable archive) and the
    reader needs that reason, not a generic one."""
    tree = _render({"kind": "none", "name": "data.parquet", "reason": "No preview for '.parquet' files — download it."})
    msg = next(n for n in _flatten(tree) if n["cls"] == "fp-msg")
    assert msg["text"] == "No preview for '.parquet' files — download it."


def test_the_foot_offers_a_download_when_the_caller_passes_one() -> None:
    """Previewing is a step on the way to taking the file: the modal opens ON
    the row, so "give me it" must not require closing and finding the row."""
    tree = _render(
        {"kind": "slides", "name": "deck.pptx", "slides": [{"index": 1, "title": "One", "lines": []}]},
        opts={
            "downloadHref": "/api/chat/sessions/c1/files/download?path=outputs/deck.pptx",
            "downloadName": "deck.pptx",
        },
    )
    actions = [n for n in _flatten(tree) if n["tag"] == "a" and n["text"] == "Download"]
    assert len(actions) == 1
    assert actions[0]["href"].endswith("path=outputs/deck.pptx")


def test_no_download_action_when_the_caller_offers_none() -> None:
    """The Library's two callers pass no download URL and must not grow a
    dead button because the chat needed one."""
    tree = _render({"kind": "none", "name": "x.bin", "reason": "nope"})
    assert not [n for n in _flatten(tree) if n["tag"] == "a" and n["text"] == "Download"]


def test_the_chat_page_actually_ships_the_modal() -> None:
    """A renderer nothing loads previews nothing. Both assets have to be on
    the chat page, and the drawer has to call the opener."""
    html = CHAT_HTML.read_text(encoding="utf-8")
    assert "css/file_preview.css" in html
    assert "js/file_preview.js" in html

    js = CHAT_JS.read_text(encoding="utf-8")
    assert "window.openFilePreview" in js
    assert "/files/preview?path=" in js


def test_the_slide_card_styles_use_only_design_system_tokens() -> None:
    """Same rule the rest of the app follows: no raw hex, so the cards track
    the active theme like every other surface."""
    import re

    css = PREVIEW_CSS.read_text(encoding="utf-8")
    slide_block = "\n".join(line for line in css.splitlines() if ".fp-slide" in line or "--ds-" in line)
    assert "--ds-" in slide_block
    for rule in re.findall(r"\.fp-slide[^{]*\{[^}]*\}", css, re.DOTALL):
        assert "#" not in rule, f"raw hex colour in a slide-card rule: {rule}"
