"""The source of the /admin/access page's behaviour, in one place.

The page used to be a single 7,400-line template: markup, CSS and ~4,500
lines of JavaScript in one `<script>`. Every guard over its behaviour was
therefore a text scan of that one file. The script now lives in
`app/web/static/js/admin_access.js` — a static asset a test can execute
under node, which is the point of moving it — and the template renders a
`<script type="application/json" id="ax-boot-data">` blob plus a module tag.

The scans want the same subject they always had: everything this page is
made of. Concatenating the two halves keeps every existing assertion exactly
as strong as it was and, more usefully, means a guard does not have to know
which half a given string ended up in — a string that later moves between
markup and script does not silently stop being checked.

`access_js()` is the narrower subject, for a guard that is genuinely about
the script (or that wants to slice a function out and run it under node).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

ACCESS_TEMPLATE = _ROOT / "app" / "web" / "templates" / "admin_access.html"
ACCESS_JS = _ROOT / "app" / "web" / "static" / "js" / "admin_access.js"


def access_template() -> str:
    """The markup + CSS half."""
    return ACCESS_TEMPLATE.read_text(encoding="utf-8")


def access_js() -> str:
    """The behaviour half — the static module the page loads."""
    return ACCESS_JS.read_text(encoding="utf-8")


def access_page_source() -> str:
    """Both halves, as the one file they used to be."""
    return f"{access_template()}\n{access_js()}"


def with_module(response_text: str) -> str:
    """A `GET /admin/access` response, plus the module that response loads.

    What a browser ends up with. A guard that asserts on the page's behaviour
    wants this rather than the response body alone: fetching still proves the
    route renders and that the boot blob carries what the script reads, and
    the module supplies the rest — the half that used to be inlined.
    """
    return f"{response_text}\n{access_js()}"
