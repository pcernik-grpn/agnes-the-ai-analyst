"""One tab strip, four pages — pinned on the STYLESHEETS, not on a screenshot.

Four copies of one look had drifted apart, which is why People and Access did
not match Data while using the same component:

  * `.tab-strip` in `style-custom.css`, on LEGACY tokens (`--primary`,
    `--text-secondary`, `--border`) — People and Access;
  * `.tab-flow__item` in `admin_page.css`, on `--ds-` tokens — Data;
  * a page-local rewrite in `library.html` re-underlining a pill segmented
    control — Library.

The third is the tell: the Library's block opens by explaining that its tabs
should be "underlined rather than pilled", which is a description of the
component it was not using. A component nobody can reach from their page gets
rewritten in place, and then there are four.

There is now one definition, in the global component sheet — global because
Library is not an admin page and cannot reach `admin_page.css`. The flow
variant keeps its arrows and its divider; those are connectors, not items.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[1] / "app" / "web" / "static"
COMPONENTS = CSS / "css" / "components.css"
ADMIN_PAGE = CSS / "css" / "admin_page.css"
LEGACY = CSS / "style-custom.css"
TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"


def _blocks(text: str, selector: str) -> list[str]:
    """Every rule block whose SELECTOR list mentions `selector`.

    Comments are stripped first. Without that, the prose explaining where a
    rule went ("its rules moved to components.css") reads as part of the next
    block's selector, and the check reports a rule that is not there — which
    is how a guard ends up failing for a reason that has nothing to do with
    what it guards.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        if selector in m.group(1):
            out.append(m.group(0))
    return out


class TestOneDefinition:
    def test_the_item_is_defined_in_the_global_component_sheet(self):
        """Global, not admin-only: Library is not an admin page."""
        css = COMPONENTS.read_text()
        assert ".tab-strip__item" in css
        assert "border-bottom: 2px solid transparent" in css

    def test_the_legacy_copy_is_gone(self):
        """It was the copy on legacy tokens, and the reason two admin pages
        rendered a different blue from the third."""
        css = LEGACY.read_text()
        assert not _blocks(css, ".tab-strip"), "style-custom.css defines .tab-strip again"

    def test_the_flow_variant_keeps_connectors_and_nothing_else(self):
        """`.tab-flow` adds arrows to a strip; it does not restate what a tab
        is. Its own comment said so while carrying a second copy of the item
        rules — which is exactly how the two drifted."""
        css = ADMIN_PAGE.read_text()
        for block in _blocks(css, ".tab-flow__item"):
            # The responsive `flex: 0 0 auto` is layout inside the scroll
            # container, not an item look.
            assert "color" not in block and "font-size" not in block, (
                f"admin_page.css restyles the tab item again: {block.strip()[:90]}"
            )

    @pytest.mark.parametrize(
        "prop", ["font-size: 13.5px", "font-weight: 600", "padding: 9px 14px 10px"]
    )
    def test_the_shared_item_carries_the_look(self, prop):
        assert prop in COMPONENTS.read_text()

    def test_the_shared_rules_use_ds_tokens_only(self):
        """The legacy copy's `--primary` / `--text-secondary` / `--border` are
        what made the same component render two different blues."""
        blocks = "\n".join(_blocks(COMPONENTS.read_text(), ".tab-strip"))
        for legacy in ("var(--primary)", "var(--text-secondary)", "var(--border)"):
            assert legacy not in blocks, f"the shared tab rules use the legacy token {legacy}"


class TestEveryStripUsesIt:
    """The four surfaces, by their markup. A page that keeps its own class but
    loses the shared one silently goes back to looking like itself."""

    @pytest.mark.parametrize(
        "template,marker",
        [
            ("_admin_tabs.html", "tab-strip__item tab-flow__item"),
            ("admin_access.html", 'class="tab-strip ax-by"'),
            ("library.html", 'class="tab-strip lib-tabs"'),
        ],
    )
    def test_the_strip_carries_the_shared_classes(self, template, marker):
        assert marker in (TEMPLATES / template).read_text()

    def test_the_library_container_is_not_also_the_pill_control(self):
        """`.fbar-seg` on the CONTAINER is the pill segmented control's own
        chrome — grey fill, 1px border, 10px radius, 3px padding
        (filter_toolbar.css) — and `.tab-strip` cannot undo it: two of those
        tie on specificity and lose on source order (library.html links
        filter_toolbar.css from `head_extra`, after components.css), and
        `border-radius` the strip never sets at all.

        Carrying both classes therefore rendered both looks at once: a
        bordered grey box with underlined tabs sitting inside it. This is the
        regression the marker above cannot catch on its own — "tab-strip
        lib-tabs" is a substring of "fbar-seg tab-strip lib-tabs", so the
        buggy combination passes it."""
        src = (TEMPLATES / "library.html").read_text()
        tag = src.split('id="lib-tabs"', 1)[0].rsplit("<div", 1)[1]
        assert "fbar-seg" not in tag, (
            f"the library tab container carries the pill control's chrome again: <div{tag}"
        )

    def test_the_library_keeps_its_engine_hook(self):
        """`.fbar-seg__btn` is how filter_toolbar.js finds these buttons. The
        look moved to `.tab-strip`; the hook must not move with it, or the
        Knowledge/Capabilities switch silently stops working — which renders
        identically to working."""
        src = (TEMPLATES / "library.html").read_text()
        assert "fbar-seg__btn" in src
        assert 'id="lib-tabs"' in src

    def test_the_library_no_longer_restyles_the_tab_itself(self):
        """Placement is the page's; the look is the component's."""
        src = (TEMPLATES / "library.html").read_text()
        for gone in (
            ".lib-head .lib-tabs .fbar-seg__btn {",
            ".lib-head .lib-tabs .fbar-seg__btn.is-active {",
        ):
            assert gone not in src, f"library.html restyles the tab again: {gone}"


class TestActiveStateAttributeIsMaintained:
    """The attributes the active rule STYLES on must be the attributes the
    engine WRITES — the divergence #1898 item 2 survived on.

    `.tab-strip`'s active rule keys on the class and on `[aria-checked="true"]`,
    because the Library's and /chats' strips are radiogroups rather than
    tablists. `filter_toolbar.js` wrote only `aria-selected`, so the
    server-rendered `aria-checked="true"` stayed on the DEFAULT segment for the
    life of the page: pick the second tab and both matched the active rule, both
    went blue, and the strip stopped saying where you were. Removing the pill
    chrome (the earlier half of this fix) made the tabs look right and left this
    untouched.

    Asserted as a relationship rather than as one literal, so the next
    attribute added to either side has to be added to both."""

    JS = CSS / "js" / "filter_toolbar.js"

    def _set_segment(self) -> str:
        """`setSegment`'s body, CODE ONLY.

        Sliced to the next section comment rather than to the next `function `,
        because the body contains a callback of its own and splitting on that cut
        the body off at its first line. Line comments are then stripped, which is
        not fussiness: the first version of this test passed while the bug was
        reintroduced, because the comment explaining the bug says
        `aria-checked` and a grep cannot tell prose from code."""
        js = self.JS.read_text(encoding="utf-8")
        body = js.split("function setSegment", 1)[1].split("// ──", 1)[0]
        return "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("//"))

    def test_every_styled_state_attribute_is_written_by_the_engine(self):
        css = COMPONENTS.read_text(encoding="utf-8")
        blocks = _blocks(css, ".tab-strip .fbar-seg__btn")
        styled = set()
        for b in blocks:
            styled.update(re.findall(r'\[(aria-[a-z]+)="true"\]', b.split("{", 1)[0]))
        assert styled, "the active rule no longer keys on any aria attribute — re-read this test"

        seg = self._set_segment()
        for attr in sorted(styled):
            assert attr in seg, (
                f"components.css styles a tab active off [{attr}] but setSegment never writes it — "
                "the server-rendered value will stay on the default segment and two tabs will read active"
            )

    def test_a_radio_gets_aria_checked_not_aria_selected(self):
        """`aria-selected` is for tabs and options; on `role="radio"` it means
        nothing, and every caller of this engine is a radiogroup."""
        seg = self._set_segment()
        assert "'radio'" in seg and "aria-checked" in seg, seg

        for name in ("library.html", "chats.html"):
            markup = (TEMPLATES / name).read_text(encoding="utf-8")
            strip = markup[markup.index('role="radiogroup"'):]
            assert 'role="radio"' in strip[:2000], f"{name}: the segments are no longer radios"
