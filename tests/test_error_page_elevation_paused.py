"""The error page finishes the job `admin_elevation_paused` was created for.

`app/auth/access.py` raises a DISTINCT `admin_elevation_paused` detail
instead of a generic 403, and says why in its own comment: "so clients can
offer a 're-enable admin mode' action instead of a generic 403." The HTML
error page never implemented that half. An admin who paused their own
elevation and then clicked an admin link landed on a bare 403 showing the
raw machine string, with Go home / Back as the only actions — the one
control that resolves it lives on `/me/profile`, which the page does not
mention.

Also fixed here: `error.html` was the last template still titling itself
"Data Analyst Portal" while every other page uses `config.INSTANCE_NAME`,
so the browser tab changed brand on the error path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "error.html"


@pytest.fixture(scope="module")
def markup() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class TestTitleUsesInstanceName:
    def test_title_is_not_hardcoded_to_the_old_brand(self, markup):
        assert "Data Analyst Portal" not in markup

    def test_title_uses_the_same_source_as_every_other_page(self, markup):
        title = re.search(r"\{%\s*block title\s*%\}(.*?)\{%\s*endblock\s*%\}", markup, re.S)
        assert title, "no title block"
        assert "config.INSTANCE_NAME" in title.group(1)


class TestElevationPausedGetsItsAction:
    def test_page_offers_re_enabling_admin_mode(self, markup):
        """The action the distinct detail exists to enable."""
        assert "admin_elevation_paused" in markup, "page does not branch on the detail"
        assert "/me/profile" in markup, "no route to the control that fixes it"

    def test_the_action_is_gated_on_that_detail(self, markup):
        """A generic 403 must not advertise an admin-only remedy."""
        assert re.search(r"\{%\s*if .*admin_elevation_paused", markup), (
            "the re-enable affordance is not conditional on the detail"
        )


#: `/me/profile` renders a single template (`app/web/router.py`) since the
#: frozen pre-redesign `profile_legacy.html` was retired (Wave 0 legacy
#: retirement, 2026-08). Kept as a tuple + the guard below so a future
#: chrome variant can't silently reintroduce an unchecked template.
PROFILE_TEMPLATES = ("profile.html",)


def _template(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / name).read_text(encoding="utf-8")


class TestTheLinkTargetExists:
    @pytest.mark.parametrize("template_name", PROFILE_TEMPLATES)
    def test_every_profile_chrome_carries_the_anchor(self, template_name):
        """A link to `#admin-mode` is only useful if something has that id.

        Both panels previously carried `aria-label="Admin mode"` and no id,
        so the anchor would have scrolled nowhere — the same silent dead end
        this change set is fixing elsewhere.
        """
        error_page = TEMPLATE.read_text(encoding="utf-8")
        anchors = re.findall(r"/me/profile#([\w-]+)", error_page)
        assert anchors, "error page links to /me/profile without an anchor"

        profile = _template(template_name)
        for anchor in anchors:
            assert f'id="{anchor}"' in profile, (
                f"#{anchor} has no target in {template_name} — the 403 link is dead "
                f"on the {'rail' if template_name == 'profile.html' else 'topnav (default)'} chrome"
            )

    def test_the_guard_covers_every_template_the_route_can_render(self):
        """Pin the template set against the route, so a third chrome cannot
        be added with the anchor guard silently still checking only two."""
        router = (Path(__file__).resolve().parents[1] / "app" / "web" / "router.py").read_text(encoding="utf-8")
        rendered = set(re.findall(r'"(profile(?:_\w+)?\.html)"', router))
        assert rendered, "could not find the profile template choice in the router"
        assert rendered <= set(PROFILE_TEMPLATES), (
            f"route can render {sorted(rendered - set(PROFILE_TEMPLATES))}, which this guard does not check"
        )


class TestAnchorSurvivesRendering:
    """The static guard reads source; `ds.panel` could still drop `attrs`.

    Rendering the page is what proves the anchor actually reaches the browser —
    a source-only assertion would stay green on a macro that silently ignored
    the attribute.
    """

    def test_profile_page_renders_the_anchor(self, seeded_app):
        """Was parametrized over ``["topnav", "rail"]``, with a companion test
        asserting the two really rendered differently so the pair could not be
        checking one chrome twice. Wave 0 (2026-08) retired the topnav:
        `get_ui_layout()` returns "rail" whatever is configured, so the
        parametrization rendered the same page twice under two labels and the
        companion could only ever fail. One chrome, one case."""
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        r = c.get("/me/profile", headers={"Accept": "text/html"})

        assert r.status_code == 200, r.text
        assert 'id="admin-mode"' in r.text, "anchor missing from the rendered profile page"
        assert 'data-ui-layout="rail"' in r.text

    def test_a_configured_layout_cannot_change_the_chrome(self, seeded_app, monkeypatch):
        """The replacement for the retired parametrization guard.

        What used to need pinning was that `get_ui_layout()` re-read the
        environment per request — a memoization would have left the pair above
        silently testing one chrome twice. What needs pinning now is the
        opposite: a leftover `AGNES_UI_LAYOUT=topnav` in a real deployment's
        .env must NOT resurrect a second chrome. It is ignored, and the page
        still renders the rail with its anchor intact.
        """
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])

        monkeypatch.setenv("AGNES_UI_LAYOUT", "topnav")
        r = c.get("/me/profile", headers={"Accept": "text/html"})

        assert r.status_code == 200, r.text
        assert 'data-ui-layout="rail"' in r.text
        assert 'id="admin-mode"' in r.text


class TestRenderedPage:
    def test_paused_admin_sees_the_remedy(self, seeded_app, monkeypatch):
        """End to end: pause elevation, hit an admin page, read the response."""
        # `*_` because the real signature takes an optional SUBJECT (the pause
        # applies to the person who paused, not to whoever is being asked
        # about) — a zero-arg stub broke the moment a second caller passed one.
        monkeypatch.setattr("app.auth.elevation.elevation_paused", lambda *_: True)
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["admin_token"])
        r = c.get("/admin/server-config", headers={"Accept": "text/html"})

        assert r.status_code == 403
        body = r.text
        assert "/me/profile" in body, "403 page does not point at the fix"
        assert "Data Analyst Portal" not in body

    def test_an_ordinary_403_does_not_offer_it(self, seeded_app):
        """A non-admin hitting an admin page is not told to un-pause anything."""
        c = seeded_app["client"]
        c.cookies.set("access_token", seeded_app["analyst_token"])
        r = c.get("/admin/server-config", headers={"Accept": "text/html"})

        if r.status_code == 403:
            assert "Re-enable admin mode" not in r.text
