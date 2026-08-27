"""Safe markdown → HTML renderer for curator-authored marketplace content.

Two stages:

1. **Render** — `markdown-it-py` in CommonMark mode (no raw HTML pass-through,
   no autolink to javascript:, no unsafe blocks). Tables and strikethrough
   are enabled because they show up routinely in `long_description` /
   `sample_interaction.assistant`. Linkify is OFF — curators write explicit
   links; auto-linking bare strings adds attack surface without value here.
   Callers whose stored text may itself BE html rather than markdown pass
   `html_source=True` for a second renderer that does pass raw HTML through
   to stage 2 — see the `render_safe` docstring for when that is right.

2. **Sanitize** — funnel the rendered HTML through `nh3` (Rust-backed ammonia
   allowlist) so anything the renderer let through that we don't want
   reaching the browser (raw HTML the curator inlined, `javascript:` URLs,
   on*-handlers, unknown tags) gets stripped.

Used by `app/api/marketplace.py` to pre-render `description` and
`sample_interaction.assistant` from `marketplace-metadata.json` before the
HTML lands in `PluginDetailResponse`, and by the `/catalog/semantics` metric
rows (`html_source=True` there). The template injects with `{{ x | safe }}`
trusting the stored value — no second-pass sanitization on render.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Optional

import nh3
from markdown_it import MarkdownIt

from src.semantic.keboola_sources import KEBOOLA_SEMANTIC_LAYER_SOURCES


# CommonMark-strict renderer. `html=False` disables inline raw HTML so a
# curator who pastes `<script>` inside markdown gets the literal string
# rendered, not an executable tag. `linkify` is off to keep bare strings
# from becoming clickable links.
_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")

# Same renderer with raw HTML pass-through, for stored text that may itself
# BE html rather than markdown (`html_source=True` below). Safety still rests
# on the same nh3 allowlist — the difference is only whether a `<strong>` in
# the source becomes markup or becomes the visible characters `<strong>`.
_md_html_source = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table").enable("strikethrough")


# nh3 allowlist — narrower than `src/sanitize_news.py` (which supports
# admin-edited HTML with iframes). Marketplace descriptions don't need
# iframes, images, or HTML5 details — just text formatting + links + code.
_ALLOWED_TAGS: set[str] = {
    "p",
    "br",
    "h2",
    "h3",
    "h4",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "b",
    "i",
    "s",
    "code",
    "pre",
    "blockquote",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "hr",
}

_ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    # `rel` is managed via nh3's `link_rel` param; do NOT list here.
    "a": {"href", "title"},
    "th": {"align"},
    "td": {"align"},
}

_ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto"}

# Structural block containers kept ONLY on the `html_source` path. An imported
# description is free to lay its lines out with <div>, and stripping those with
# no replacement fuses the text they separated ("sales.Excludes refunds") — the
# same defect render_plain avoids by inserting boundaries before sanitization,
# except here the fix has to preserve structure rather than a space. No entry is
# added to _ALLOWED_ATTRIBUTES for them, so every attribute (including on*
# handlers) is still stripped; these carry layout, never behaviour. Curator
# markdown keeps the narrow set — it has <p> and never needs these.
_HTML_SOURCE_EXTRA_TAGS: set[str] = {
    "div",
    "section",
    "article",
    "dl",
    "dt",
    "dd",
    "figure",
    "figcaption",
    # The headings the narrow allowlist omits. It keeps h2-h4 so a curator
    # cannot outrank the page's own headings; an imported description is not
    # authored against this page at all, and dropping its <h1> fused the
    # sections it separated ("Section ASection B").
    "h1",
    "h5",
    "h6",
    # The headings the narrow allowlist omits. It keeps h2-h4 so a curator
    # cannot outrank the page's own headings; an imported description is not
    # authored against this page at all, and dropping its <h1> fused the
    # sections it separated ("Section ASection B").
}


def render_safe(markdown: Optional[str], *, html_source: bool = False) -> str:
    """Render curator-authored markdown to sanitized HTML.

    Returns ``""`` for ``None`` or empty input. The output is safe to inject
    into a template with `{{ x | safe }}` — every attack surface markdown-it
    leaves open (raw `<script>`, `javascript:` URLs, event handlers) is
    stripped by nh3 before return.

    ``html_source=True`` for stored text whose dialect is not guaranteed to be
    markdown — a metric description imported verbatim from an external catalog
    sits in the same column as a hand-authored markdown one, and such catalogs
    routinely store rich HTML. Raw HTML then renders as markup instead of as
    its own escaped characters, and the allowlist gains the structural block
    containers in ``_HTML_SOURCE_EXTRA_TAGS`` so a <div>-laid-out description
    keeps its line breaks. Attributes, URL schemes and `link_rel` are unchanged,
    and no tag that can carry behaviour is added — so this widens what is
    *displayed*, never what can *act*. Leave it off for curator-authored
    content, where pasted HTML showing up as literal text is the intended tell.
    """
    if not markdown:
        return ""
    html = (_md_html_source if html_source else _md).render(markdown)
    return nh3.clean(
        html,
        tags=(_ALLOWED_TAGS | _HTML_SOURCE_EXTRA_TAGS) if html_source else _ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


# Block-level element tags (plus <br>) mark word boundaries when flattening
# rendered HTML to plain text; without this, "<p>a</p><p>b</p>" collapses to
# "ab". The list deliberately includes block elements the render allowlist does
# NOT keep (div, section, …): an imported description is free to separate its
# lines with <div>, and those tags must still leave a boundary behind when they
# are stripped — see the ordering note in render_plain.
#
# OPENING tags count too, not just closing ones. Nested structure separates text
# with an opening tag and no closing tag in between — "<figure>A<figcaption>B"
# has nothing but `<figcaption>` between A and B — so a close-only pattern
# fused them. Redundant spaces are free: render_plain collapses runs at the end.
_BLOCK_TAGS = "p|li|h[1-6]|tr|t[dh]|blockquote|pre|div|section|article|figure|figcaption|dd|dt|dl"
# The attribute run skips over quoted values, because `>` inside one does not
# close the tag — `<div title="a>b">` is a single tag, and a `[^>]*>` run cut it
# in half and left `b">` as visible text in the preview.
#
# Two properties keep it cheap on hostile input. The three alternatives are
# mutually exclusive on their first character (a quote starts a quoted run,
# anything else takes the bare branch), so there is no exponential backtracking.
# And the bare branch excludes `<`, so an unterminated tag stops at the next
# tag start instead of scanning to the end of the text — without that, N
# unclosed tags each rescanned the remainder and the whole substitution went
# quadratic (20 KB of `<div ` took 620 ms). A `<` inside a quoted value is
# still consumed by the quoted branch, so well-formed tags are unaffected.
# IGNORECASE because this runs on the RENDERED html, before nh3 — and nh3 is
# what normalizes tag case. `<DIV>` reaches this pattern exactly as the upstream
# catalog wrote it, and a lower-case-only pattern left those lines fused.
_BLOCK_BOUNDARY_RE = re.compile(
    rf"</?(?:{_BLOCK_TAGS})\b(?:\"[^\"]*\"|'[^']*'|[^'\"<>])*>|<br ?/?>",
    re.IGNORECASE,
)


def render_plain(markdown: Optional[str], *, html_source: bool = False) -> str:
    """Plain-text projection of ``render_safe`` output.

    For one-line previews and client-side filter indexes where markup,
    literal ``**`` / ``#`` as much as HTML tags, is noise. Pipeline:
    render + sanitize (``render_safe``), turn block boundaries into spaces,
    strip every remaining tag (nh3 with an empty allowlist), unescape
    entities back to text, collapse whitespace. The result is data, not
    HTML: inject with normal Jinja escaping, never ``| safe``.

    ``html_source`` carries the same meaning as in ``render_safe``, and this
    projection is why it exists: without it an HTML-blob input is escaped by
    the renderer into entities, so the tag-strip below finds no tags to
    remove and the closing ``unescape`` hands them back as visible ``<p>``
    / ``<strong>`` characters — markup surviving into the one place that
    promises none.
    """
    if not markdown:
        return ""
    # Boundaries are inserted into the RENDERED html, before any sanitization —
    # not into `render_safe`'s output. A <div> is not on the render allowlist,
    # so by then it is already gone and the two lines it separated have fused
    # ("First line.Second line."). Stripping every tag afterwards with an empty
    # allowlist is stricter than the render allowlist, so nothing survives here
    # that would survive there.
    rendered = (_md_html_source if html_source else _md).render(markdown)
    text = html_lib.unescape(nh3.clean(_BLOCK_BOUNDARY_RE.sub(" ", rendered), tags=set()))
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Which stored rows are HTML rather than markdown
# ---------------------------------------------------------------------------

# `source` values (on `metric_definitions` / `glossary_terms`) whose text is the
# upstream catalog's, stored with no normalization — routinely rich HTML. Every
# other writer produces markdown: `manual` (the admin UI / POST endpoints) and
# `yaml_import` (docs/metrics/*.yaml). Includes both the pre- and post-flat-table-
# cutover Keboola writer sources (`src.semantic.keboola_sources`) — the
# projector's rows carry HTML descriptions from the same upstream catalog as
# the retired flat composer's did.
HTML_DIALECT_SOURCES = KEBOOLA_SEMANTIC_LAYER_SOURCES


def stores_html(row: dict) -> bool:
    """Whether ``row``'s authored text should be read as HTML, not markdown.

    Keyed on the WRITER (the ``source`` column), not on what the text looks
    like. Sniffing the content is tempting and wrong: a markdown description is
    free to contain `List<int>` or `orders <shipped>`, and handing that to
    ``html_source=True`` deletes the fragment — markdown-it emits it as an
    unknown tag, the nh3 allowlist rejects it, and since a pseudo-tag carries no
    child text the characters vanish rather than being escaped and shown (an
    unclosed one takes the rest of the line with it). The dialect is a property
    of who wrote the row, and the row records that.

    The trade-off does not disappear, it just lands where it belongs: an
    HTML-dialect row whose text happens to contain `<int>` still loses it. That
    is the row class where HTML is the documented dialect.
    """
    return (row.get("source") or "") in HTML_DIALECT_SOURCES


__all__ = ["HTML_DIALECT_SOURCES", "render_plain", "render_safe", "stores_html"]
