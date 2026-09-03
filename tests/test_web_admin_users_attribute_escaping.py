"""`esc()` on /admin/users must be attribute-safe, not just text-safe.

The page builds rows with `innerHTML` template literals and interpolates
`esc(...)` inside double-quoted attributes (`title="…"`, `data-*="…"`).
The original helper round-tripped through `textContent -> innerHTML`, which
encodes `&`, `<`, `>` — but NOT quotes. Free-form text (a service account's
`name`) placed in an attribute could therefore close the attribute with `"`
and plant an event-handler attribute on the element: stored XSS on an
admin page (found by security review of #1534; the repo playbook's
"sanitize before innerHTML" rule).

`esc()` cannot be executed under node (it needs a real DOM, and a stub would
re-implement the escaping under test), so this pins the source: the helper
must keep encoding both quote characters for as long as any attribute
interpolation exists on the page.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "admin_users.html"


def _esc_source() -> str:
    src = _TEMPLATE.read_text()
    m = re.search(r"function esc\(s\)", src)
    assert m, "esc() helper not found in admin_users.html"
    depth, j, started = 0, m.start(), False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                j += 1
                break
        j += 1
    return src[m.start() : j]


def test_esc_encodes_double_and_single_quotes():
    body = _esc_source()
    assert "&quot;" in body, (
        'esc() no longer encodes `"` — it is interpolated inside double-quoted '
        "attributes, where an unescaped quote breaks out of the attribute value"
    )
    assert "&#39;" in body, "esc() no longer encodes `'` — keep it attribute-safe for both quote styles"


def test_the_page_still_interpolates_esc_inside_attributes():
    """If this stops matching, the pin above may be retirable — but only after
    confirming no `="${esc(...)}"`-style attribute interpolation remains."""
    src = _TEMPLATE.read_text()
    assert re.search(r'="\$\{esc\(', src), (
        "expected at least one attribute interpolation of esc() on the page; "
        "if all are gone, re-evaluate (do not silently drop) the quote-escaping pin"
    )
