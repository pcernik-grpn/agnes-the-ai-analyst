"""A disabled `ds.button` LINK keeps its reason reachable.

Devin's third finding on #2334, and the generalisation of a defect found in
that PR's own first cut. The chain:

* `aria-disabled` is advisory on an anchor — it stops nothing — so
  `style-custom.css` gives `.btn[aria-disabled="true"]` `pointer-events: none`
  to make it bite;
* `pointer-events: none` also removes the element from hit-testing (confirmed
  in Chromium: `elementFromPoint` over such a link does not return it), so a
  `title` on the anchor can never be displayed;
* every current caller of `ds.button(href=…, disabled=True)` passes exactly
  such a title — the reason the control is off.

So the macro wraps a disabled link and puts `attrs` on the wrapper. The anchor
stays inoperable to pointer and keyboard; the reason stays hoverable.

A `<button disabled>` is untouched: the browser refuses its activation without
`pointer-events`, and its tooltip still works.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path("app/web/templates")
STYLESHEET = Path("app/web/static/style-custom.css")


def _render(call: str) -> str:
    """Render through the APP's own Jinja environment.

    Not a fresh one: `_components.html` carries a doc example whose nested
    `{# … #}` closes the outer comment early (Jinja comments do not nest), so
    a `{{ ds.tabs(…) }}` sample below it is live template code. The app's env
    tolerates it; a default `Environment` raises on the undefined call. That is
    a pre-existing wart in that file, not this macro's, and going through the
    real env also means this test exercises what the product renders.
    """
    from app.web.router import templates

    return templates.env.from_string("{% import '_components.html' as ds %}" + call).render()


REASON = 'title="Wait for the in-flight review to finish before editing."'


class TestADisabledLinkIsWrapped:
    def test_the_reason_lands_on_the_wrapper_not_the_anchor(self):
        html = _render("{{ ds.button('Edit', href='#', disabled=True, attrs='%s') }}" % REASON.replace("'", ""))
        assert "ds-disabled-wrap" in html
        wrap = html[: html.index("<a ")]
        anchor = html[html.index("<a ") : html.index("</a>")]
        assert "title=" in wrap, "the hoverable element must carry the reason"
        assert "title=" not in anchor, "an anchor with pointer-events:none cannot show a tooltip"

    def test_the_anchor_is_still_inoperable_both_ways(self):
        html = _render("{{ ds.button('Edit', href='#', disabled=True) }}")
        anchor = html[html.index("<a ") : html.index("</a>")]
        assert 'aria-disabled="true"' in anchor
        assert 'tabindex="-1"' in anchor

    def test_the_wrapper_has_a_rule_so_it_does_not_shift_the_layout(self):
        css = re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(encoding="utf-8"), flags=re.S)
        assert ".ds-disabled-wrap" in css, "the wrapper needs a box or the button moves"


class TestNothingElseChanged:
    def test_an_enabled_link_is_not_wrapped(self):
        html = _render("{{ ds.button('Go', href='/x', attrs='data-k=\"v\"') }}")
        assert "ds-disabled-wrap" not in html
        assert 'data-k="v"' in html
        assert "aria-disabled" not in html

    def test_a_disabled_button_is_not_wrapped(self):
        """The browser refuses a disabled <button> on its own, and its tooltip
        still works — wrapping it would be churn."""
        html = _render("{{ ds.button('Save', disabled=True, attrs='%s') }}" % REASON.replace("'", ""))
        assert "ds-disabled-wrap" not in html
        assert "disabled" in html
        assert "title=" in html


class TestTheRealCallSitesAreCovered:
    """The two call sites that would have gone silent. If either stops passing
    a title, this test is what says so."""

    def test_both_marketplace_edit_links_still_carry_a_reason(self):
        for name in ("marketplace_item_detail.html", "marketplace_plugin_detail.html"):
            src = (TEMPLATES / name).read_text(encoding="utf-8")
            i = src.index("Edit (review in flight)")
            block = src[i : i + 400]
            assert "disabled=True" in block, name
            assert "title=" in block, name
