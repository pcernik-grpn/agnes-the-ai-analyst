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

Only classic (non-module) scripts belong in `_LOADED`: those share one
global scope with the inline block — `defer`red or not — which is why moving
a function into one is behaviour-preserving. A module or a component with
its own scope (`js/components/*.js`) has its own tests.
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


def page_source() -> str:
    """Template text with every classic script it loads appended."""
    parts = [TEMPLATE.read_text(encoding="utf-8")]
    parts.extend(p.read_text(encoding="utf-8") for p in _INCLUDED if p.exists())
    parts.extend(p.read_text(encoding="utf-8") for p in _LOADED if p.exists())
    return "\n".join(parts)


def template_text() -> str:
    """The template alone — for assertions about MARKUP, not about script."""
    return TEMPLATE.read_text(encoding="utf-8")
