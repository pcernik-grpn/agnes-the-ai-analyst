"""The local-dev audience switch, on every page.

It was inline in the chat page's empty banner, which gave it two problems:
it read as product chrome rather than a tool, and its links pointed at /chat,
so following any link off that page silently dropped the preview. It is now a
corner affordance rendered from the base layout, and the preview is resolved
once for every page that renders chrome.

The security property is unchanged and is the thing to keep: it must never be
reachable on a real deployment, and it must never be mistaken for a
role-switcher. It changes what RENDERS. It grants nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARTIAL = ROOT / "app" / "web" / "templates" / "_dev_preview.html"
BASE = ROOT / "app" / "web" / "templates" / "base_ds.html"
ROUTER = ROOT / "app" / "web" / "router.py"


@pytest.fixture(scope="module")
def partial() -> str:
    return PARTIAL.read_text(encoding="utf-8")


class TestItIsOnEveryPage:
    def test_the_base_layout_includes_it(self):
        base = BASE.read_text(encoding="utf-8")
        assert '{% include "_dev_preview.html" %}' in base
        assert "css/dev_preview.css" in base

    def test_the_preview_is_resolved_for_all_chrome(self):
        """One resolver, called where every page gets it. It used to live in
        the /chat route, which is why the parameter was inert everywhere
        else."""
        router = ROUTER.read_text(encoding="utf-8")
        assert "def _resolve_dev_preview(" in router
        assert '"dev_preview": _preview,' in router

    def test_the_links_carry_the_current_page(self, partial):
        """Hard-coded /chat links are what made this a chat-only tool: every
        option navigated away from whatever you were previewing."""
        assert "{% set _path = request.url.path %}" in partial
        assert 'href="{{ _path }}?preview=member"' in partial
        assert 'href="{{ _path }}?preview=empty"' in partial
        assert "/chat?preview=" not in partial

    def test_the_real_view_has_a_clean_url(self, partial):
        """`?preview=` is dropped rather than set to some "off" value, so the
        normal case stays shareable."""
        assert 'href="{{ _path }}"' in partial


class TestItStaysADevTool:
    def test_it_renders_nothing_without_the_gate(self, partial):
        assert "{% if dev_preview_available %}" in partial

    def test_the_gate_is_local_dev_mode(self):
        router = ROUTER.read_text(encoding="utf-8")
        assert "def _dev_preview_enabled(" in router
        assert "is_local_dev_mode()" in router

    def test_only_the_three_known_modes_are_honoured(self):
        """A value we do not recognise must be ignored outright, not
        half-honoured."""
        from app.web.router import DEV_PREVIEW_MODES

        assert set(DEV_PREVIEW_MODES) == {"member", "admin", "empty"}

    def test_it_says_what_it_does_not_change(self, partial):
        """The one sentence that stops it being read as a role-switcher. An
        admin previewing `member` still has every permission they had."""
        assert "permissions are unchanged" in partial

    def test_it_sits_below_anything_modal(self):
        """A tool must never cover a dialog the developer is trying to read."""
        css = (ROOT / "app" / "web" / "static" / "css" / "dev_preview.css").read_text(encoding="utf-8")
        assert "z-index: 900" in css


class TestTheRailFollowsThePreview:
    def test_member_takes_the_admin_destination_away(self):
        """Otherwise the preview shows an admin rail beside a member page and
        answers the wrong question."""
        rail = (ROOT / "app" / "web" / "templates" / "_app_rail.html").read_text(encoding="utf-8")
        assert "{% if session.user.is_admin and dev_preview != 'member' %}" in rail

    def test_it_is_a_render_change_not_a_permission_change(self):
        """The rail hides the link; nothing revokes anything. /admin still
        answers if the caller types it, because they are still an admin."""
        rail = (ROOT / "app" / "web" / "templates" / "_app_rail.html").read_text(encoding="utf-8")
        assert "dev_preview" in rail
        # The gate is still primarily the real permission.
        assert "session.user.is_admin and dev_preview" in rail


class TestTheOldInlineSwitchIsGone:
    def test_the_chat_page_no_longer_carries_its_own(self):
        chat = (ROOT / "app" / "web" / "templates" / "chat.html").read_text(encoding="utf-8")
        assert "cset-devsw" not in chat, "the inline switch is back — it can only link to /chat"

    def test_its_styles_went_with_it(self):
        css = (ROOT / "app" / "web" / "static" / "style-custom.css").read_text(encoding="utf-8")
        assert "cset-devsw" not in css, "dead styles for a switch that no longer exists"


class TestUseAgnesElsewhere:
    """The rail's route out of the product.

    Everything else in the rail is a destination INSIDE Agnes. This one is how
    you take Agnes somewhere else — an editor, a terminal — so it sits in the
    foot beside the profile rather than in the nav above, and it points at the
    section that actually hands you the endpoint and the install command.
    """

    @pytest.fixture(scope="class")
    def rail(self) -> str:
        return (ROOT / "app" / "web" / "templates" / "_app_rail.html").read_text(encoding="utf-8")

    def test_it_links_to_the_section_not_the_page(self, rail):
        """`/how-it-works` alone drops you at the top of a long page you then
        have to scan for the thing you came for."""
        assert 'href="/how-it-works#connect"' in rail

    def test_the_anchor_it_points_at_exists(self):
        """A fragment that matches no id fails silently — you land at the top
        of the page and nothing says why."""
        page = (ROOT / "app" / "web" / "templates" / "how_it_works.html").read_text(encoding="utf-8")
        assert 'id="connect"' in page, "the #connect section is gone — the rail link now goes nowhere"
        assert "Set up your tools" in page

    def test_it_sits_above_the_profile_row(self, rail):
        assert rail.index("Use {{ instance_brand_short }} elsewhere") < rail.index('id="userMenu"')

    def test_it_is_outside_the_collapsible_nav(self, rail):
        """Same reason the profile is: it stays reachable in the ≤1024px bar
        with the nav collapsed."""
        assert rail.index('<div class="rail-foot">') < rail.index("Use {{ instance_brand_short }} elsewhere")

    def test_it_is_not_styled_as_a_muted_state(self, rail):
        """`rail-i--muted` italicises the label and exists for "Admin paused"
        — a condition being reported, not a place to go."""
        i = rail.index("Use {{ instance_brand_short }} elsewhere")
        anchor = rail.rfind("<a class=", 0, i)
        assert "rail-i--muted" not in rail[anchor:i]

    def test_it_uses_the_instance_brand(self, rail):
        """"Use Agnes elsewhere" on an instance that calls itself something
        else would be the one place the rename did not reach."""
        assert "Use {{ instance_brand_short }} elsewhere" in rail
