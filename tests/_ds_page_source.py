"""The Data-sources page's script, as the browser actually assembles it.

Two dozen test files harvest functions out of `admin_data_sources.html` by
name — some string-matching them, some extracting the source and running it
under `node`. That worked while every line lived in one inline `<script>`,
and it silently stops working the moment a function moves into a file the
page loads instead: `tpl.index(signature)` raises, and the failure reads as
"the function is gone" rather than "the test is looking in one of two places".

So the unit of truth for those tests is the page PLUS the classic scripts it
loads, which is what `page_source()` returns. Using it makes a test
indifferent to which file a function sits in, which is the property that lets
the page keep being split up — the wizard had to come out of here so the
data-package builder could open the same drawer, and it could not come out
while every test was pinned to its address.

Only classic (non-deferred, non-module) scripts belong in `_LOADED`: those
share one global scope with the inline block, which is why moving a function
into one is behaviour-preserving. A module or a deferred component
(`js/components/*.js`) has its own scope and its own tests.
"""

from __future__ import annotations

from pathlib import Path

_WEB = Path(__file__).resolve().parents[1] / "app" / "web"

TEMPLATE = _WEB / "templates" / "admin_data_sources.html"

#: Classic scripts the page loads, in load order. Extend this when another
#: slice of the inline block moves out — that is the whole maintenance cost.
_LOADED = (
    _WEB / "static" / "js" / "ds_helpers.js",
    _WEB / "static" / "js" / "ds_add_data_wizard.js",
    # The page's own script, then the three subsystems split out of it (perf
    # follow-up, 2026-09-03) — all `defer`, all classic, one global scope.
    _WEB / "static" / "js" / "admin" / "data_sources_page.js",
    _WEB / "static" / "js" / "admin" / "data_sources_sharepoint_wizard.js",
    _WEB / "static" / "js" / "admin" / "data_sources_extraction_observability.js",
    _WEB / "static" / "js" / "admin" / "data_sources_anon_preview.js",
)

#: Markup the page pulls in with `{% include %}`. Tests assert on the wizard's
#: fields and step strip as readily as on its behaviour, and both moved out
#: together, so both have to come back together.
_INCLUDED = (_WEB / "templates" / "_add_data_wizard.html",)

#: The page's stylesheet, moved out of its three inline <style> blocks for the
#: same reason as the script — the builder needs those rules for the wizard
#: drawer. Appended for assertions about CSS, which used to read the template.
_STYLES = (_WEB / "static" / "css" / "ds_page.css",)


def page_source() -> str:
    """Template text with every classic script it loads appended."""
    parts = [TEMPLATE.read_text(encoding="utf-8")]
    parts.extend(p.read_text(encoding="utf-8") for p in _INCLUDED if p.exists())
    parts.extend(p.read_text(encoding="utf-8") for p in _LOADED if p.exists())
    parts.extend(p.read_text(encoding="utf-8") for p in _STYLES if p.exists())
    return "\n".join(parts)


def template_text() -> str:
    """The template alone — for assertions about MARKUP, not about script."""
    return TEMPLATE.read_text(encoding="utf-8")


def scripts_only() -> str:
    """Just the classic scripts the page loads, concatenated."""
    return "\n".join(p.read_text(encoding="utf-8") for p in _LOADED if p.exists())


def rendered_with_scripts(html: str) -> str:
    """A FETCHED page plus the classic scripts it loads.

    Some tests GET the page over a TestClient and then assert on script
    content — which worked while the script was inline and silently stops
    meaning anything once it moves into a file. Appending the loaded files
    keeps those assertions honest without pretending the script is still in
    the markup, and leaves assertions about SERVER-RENDERED values (which
    only exist in the response) working as they were.
    """
    return html + "\n" + scripts_only()


def styles_only() -> str:
    """Just the page's stylesheet.

    A test asserting on CSS must not be handed `page_source()`: the same
    selector appears there as a STRING inside the script (`querySelector(
    ".ds-sf-conn-error__msg")`), and an `index()` for it lands in JavaScript
    400 characters from any declaration.
    """
    return "\n".join(p.read_text(encoding="utf-8") for p in _STYLES if p.exists())
