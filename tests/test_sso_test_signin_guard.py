""" "Test sign-in" is genuinely unreachable when there is nothing to test.

The control starts a REAL sign-in against the stored SSO config
(``/auth/sso/login?mode=test``), and it is an ``<a>``, not a ``<button>``. Two
consequences the page got wrong:

* the 501 branch disables ``#sso-section input, #sso-section button`` — a
  selector an anchor is not in, so on a DuckDB instance the one control that
  starts a sign-in stayed live inside a section the server had just refused;
* the healthy path toggled a ``btn-disabled`` class that **no stylesheet in the
  repo defines**, so the guard was written and inert.

The fix uses the convention that already exists — ``aria-disabled``, which
``ds.button`` emits and ``style-custom.css`` styles — plus the shared rule
gaining ``pointer-events: none``, because ``opacity`` and ``cursor`` stop a
``<button>`` (the browser refuses it) and stop nothing at all on an anchor.

That last point is why the CSS half is tested by parsing the declaration rather
than grepping for the class name: the previous bug WAS a class name present in
the source with no rule behind it, and a substring test would have passed on it.
"""

from __future__ import annotations

import re
from pathlib import Path

STYLESHEET = Path("app/web/static/style-custom.css")
TEMPLATE = Path("app/web/templates/admin_server_config.html")

LINK_ID = "sso-test-signin-link"


def _css_without_comments() -> str:
    """Comments first. Several of these selectors are quoted verbatim in the
    prose beside them, so a naive scan matches a rule that exists only in a
    comment — the trap Zdenek hit writing the rail guards."""
    return re.sub(r"/\*.*?\*/", "", STYLESHEET.read_text(encoding="utf-8"), flags=re.S)


def _declarations_for(selector: str) -> list[str]:
    """Every declaration block whose selector list includes ``selector``."""
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css_without_comments()):
        parts = [s.strip() for s in sel.split(",")]
        if selector in parts:
            out.append(body)
    return out


class TestTheSharedDisabledRuleStopsAnAnchor:
    def test_aria_disabled_gets_pointer_events_none(self):
        """The mechanism. Without this, `[aria-disabled]` on an `<a class="btn">`
        is decoration: half opacity, a not-allowed cursor, and a working link."""
        bodies = _declarations_for('.btn[aria-disabled="true"]')
        assert bodies, 'no rule targets .btn[aria-disabled="true"]'
        joined = " ".join(bodies)
        assert re.search(r"pointer-events\s*:\s*none", joined), joined

    def test_the_two_spellings_still_agree_on_the_visual(self):
        """`:disabled` and `[aria-disabled]` share a rule precisely so they read
        the same; the fix must not have split them apart."""
        for selector in (".btn:disabled", '.btn[aria-disabled="true"]'):
            joined = " ".join(_declarations_for(selector))
            assert "opacity" in joined, selector
            assert "not-allowed" in joined, selector

    def test_the_phantom_class_is_gone_repo_wide(self):
        """`btn-disabled` was toggled by JS and defined by nothing. If it comes
        back, it comes back inert."""
        css = _css_without_comments()
        assert "btn-disabled" not in css, "define it or do not toggle it"


class TestBothDisableBranchesReachTheAnchor:
    def _src(self) -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    def test_the_healthy_path_sets_the_attribute_not_the_phantom_class(self):
        src = self._src()
        assert 'classList.toggle(\n      "btn-disabled"' not in src
        assert '"btn-disabled"' not in src, "the class does nothing; the attribute does"
        assert 'ssoTest.setAttribute("aria-disabled"' in src

    def test_the_healthy_path_also_takes_it_out_of_the_tab_order(self):
        """`pointer-events: none` closes the pointer; the keyboard needs its own
        answer or the control is still reachable by Tab and Enter."""
        src = self._src()
        assert 'ssoTest.setAttribute("tabindex", "-1")' in src
        assert 'ssoTest.removeAttribute("tabindex")' in src, "and restored when it becomes testable"

    def test_the_501_branch_reaches_the_anchor_too(self):
        """The branch the original finding was about: an anchor is in neither
        `input` nor `button`, so the section-wide disable skipped it."""
        src = self._src()
        i = src.index("Requires the Postgres app-state backend")
        j = src.index("#sso-section input, #sso-section button")
        # The anchor is handled after the section-wide sweep, inside the same
        # 501 branch — anchored by position so a stray occurrence elsewhere in
        # the file cannot satisfy this.
        tail = src[j : j + 1200]
        assert i < j
        assert LINK_ID in tail
        assert 'setAttribute("aria-disabled", "true")' in tail
        assert 'setAttribute("tabindex", "-1")' in tail

    def test_the_control_is_still_an_anchor(self):
        """If it ever becomes a <button>, this whole guard is redundant and
        should be deleted rather than left asserting a shape that moved."""
        src = self._src()
        assert re.search(rf'<a[^>]*id="{LINK_ID}"', src), "no longer an anchor — revisit this file"

class TestTheReasonStaysReachable:
    """The trap this fix walked into once already.

    `pointer-events: none` is what makes `aria-disabled` bite on an anchor, and
    it also removes the element from hit-testing entirely — verified in
    Chromium: `document.elementFromPoint` over the link does not return the
    link, and Playwright refuses to hover it. So a `title` on the <a> itself
    can NEVER be displayed, and the first cut of this fix put it there: the
    control became inert and its explanation became invisible, on a ticket
    whose whole theme is that every refusal carries its exit.

    The wrapper is not disabled, so it still takes the hover.
    """

    def _src(self) -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    def test_a_wrapper_exists_to_carry_the_reason(self):
        src = self._src()
        assert 'id="sso-test-signin-wrap"' in src
        # And it really does wrap the link, rather than sitting beside it.
        i = src.index('id="sso-test-signin-wrap"')
        j = src.index(f'id="{LINK_ID}"')
        k = src.index("</span>", i)
        assert i < j < k, "the wrapper must enclose the anchor"

    def test_the_title_is_never_set_on_the_anchor(self):
        """An anchor that cannot be hovered cannot show a tooltip."""
        src = self._src()
        assert 'ssoTest.setAttribute("title"' not in src
        assert 'ssoTestUnsupported.setAttribute("title"' not in src

    def test_both_branches_set_it_on_the_wrapper(self):
        src = self._src()
        assert 'ssoTestWrap.setAttribute("title"' in src
        assert 'ssoTestUnsupportedWrap.setAttribute("title"' in src

    def test_the_gate_is_not_keyed_on_enabled(self):
        """Test mode is designed to work BEFORE SSO is switched on
        (`providers/sso.py:522` — "works pre-enable and outside the
        allowlist"), so gating on `enabled` would remove the control's only
        purpose: checking a config you have not turned on yet."""
        src = self._src()
        assert "s.configured && s.has_client_secret" in src
        assert "s.enabled &&" not in src.split("ssoTestable")[0][-400:]

class TestItFailsClosed:
    """Devin's finding on #2334. Three states were open before:

    the window between first paint and `ssoLoad` resolving (it is called
    un-awaited), a `/config` request that throws (the catch renders the error
    badge and never returns to this control), and a 501 arriving late. The
    control starts a REAL sign-in, so all three must leave it shut.
    """

    def _src(self) -> str:
        return TEMPLATE.read_text(encoding="utf-8")

    def test_the_markup_renders_it_disabled(self):
        src = self._src()
        i = src.index(f'id="{LINK_ID}"')
        tag = src[i : src.index(">", i)]
        assert 'aria-disabled="true"' in tag, tag
        assert 'tabindex="-1"' in tag, tag

    def test_the_wrapper_explains_the_initial_state(self):
        src = self._src()
        i = src.index('id="sso-test-signin-wrap"')
        assert "title=" in src[i : i + 200]

    def test_a_failed_config_read_says_so_rather_than_leaving_the_placeholder(self):
        """It stays shut either way — it is rendered that way — but the reader
        should not be left with "checking…" over a request that already
        failed."""
        src = self._src()
        j = src.index("Failed to load SSO config")
        assert "sso-test-signin-wrap" in src[j : j + 600]

    def test_only_a_proven_config_opens_it(self):
        """The one path that may enable it is the resolved, configured one."""
        src = self._src()
        assert 'ssoTest.removeAttribute("tabindex")' in src
        # And that removal sits under the testable branch, not at the top.
        k = src.index("const ssoTestable")
        assert src.index('ssoTest.removeAttribute("tabindex")') > k
