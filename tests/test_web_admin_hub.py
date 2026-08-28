"""Admin dashboard page (GET /admin).

`/admin` used to be a card grid indexing every admin surface — a second copy
of the sidebar (`app/web/admin_nav.py`) rendered beside the first. The grid is
gone; the page now answers "what needs my attention?" from the signal registry
in `app/web/admin_signals.py`, and the sidebar is the only admin navigation.

This suite covers the three things that consolidation put at risk:

  * the gate still holds (unchanged);
  * the dashboard renders its zones, and an instance with empty queues gets an
    explicit all-clear rather than a wall of zeros;
  * NOTHING became unreachable when the grid was deleted — every destination
    it carried, including the three that were not sidebar entries before, is
    still linked from this page.
"""

from __future__ import annotations

from pathlib import Path

_HUB_SRC = Path("app/web/templates/admin_hub.html").read_text(encoding="utf-8")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestAdminDashboard:
    def test_admin_sees_both_zones(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        body = resp.text
        assert "Needs you" in body
        assert "Needs fixing" in body

    def test_empty_queues_render_an_explicit_all_clear(self, seeded_app):
        """A seeded instance has nothing pending. Rule 1 of the registry: a
        clear signal renders NOTHING, and the zone collapses to one line — a
        dashboard of zeros is one nobody reads."""
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert "Nothing needs your attention." in body

    def test_needs_fixing_is_not_resolved_in_the_render_path(self, seeded_app):
        """Zone 2 reads the unbounded audit/history tables, so it must be
        fetched after first paint — not inlined. The skeleton + the script tag
        are what prove the split is still in place."""
        c = seeded_app["client"]
        body = c.get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert "data-adash-fixing" in body
        assert "js/admin/admin_dashboard.js" in body

    def test_non_admin_gets_403(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 403


class TestOverviewWearsTheAdminPagePattern:
    """Overview was the last admin surface still carrying a bordered hero card
    above its content, and the only one whose setup chain was a bespoke panel.

    It is not in `test_web_admin_page_pattern.py`'s `SECTION_PAGES` — that guard
    also asserts the h1 names the SECTION, and Overview is the hub rather than a
    lens of one — so the two head rules it does share are pinned here.
    """

    def test_head_is_the_shared_plain_variant_with_no_eyebrow(self) -> None:
        assert "page_hero_plain = True" in _HUB_SRC, (
            "Overview must open on the plain page head every other admin page uses — "
            "a card above a page whose body is panels puts a card around the cards."
        )
        assert "page_hero_eyebrow" not in _HUB_SRC, (
            "the rail's own header already says Admin; an eyebrow restates the row the reader used to get here."
        )

    def test_the_setup_chain_is_the_shared_collapsible_section_card(self, seeded_app) -> None:
        """`.dsec` (section_card.css) rather than a page-local panel: it is what
        makes the chain collapsible on ANY instance, keyboard- and
        screen-reader-correct with no JS, and answers its own question while
        closed (the fraction + bar ride the component's summary slot)."""
        assert "css/section_card.css" in _HUB_SRC
        body = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert 'class="dsec aov-setup' in body
        # A disclosure, so "put it away" costs no script and survives keyboard use.
        assert "<details" in body and "<summary>" in body
        # ...and the progress read-out is in the head, where a closed panel can
        # still be read.
        assert 'role="progressbar"' in body

    def test_a_step_is_one_row_not_a_block(self, seeded_app) -> None:
        """The compactness is the redesign. A four-column grid puts each step's
        name, its count sentence and its link in their own column; the old block
        stacked seven elements per step and needed its own border to look
        organised."""
        assert "grid-template-columns: 18px 200px minmax(0, 1fr) auto" in _HUB_SRC
        body = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert 'class="aov-step__title"' in body
        # The retired per-step block anatomy must not come back.
        for retired in ("aj-step__facts", "aj-step__health", "aj-step__why", "aj__meter"):
            assert retired not in body, f".{retired} is the pre-redesign step block"

    def test_a_healthy_step_does_not_render_a_green_all_is_well_line(self, seeded_app) -> None:
        """`ok` health lines are resolved (the service still owns them) and
        deliberately NOT drawn: green reassurance under five finished steps
        teaches the eye to skip the slot where the amber one appears. The warn
        line still renders — that is the half worth the row."""
        from app.services.admin_dashboard import resolve_journey

        body = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text
        for s in resolve_journey()["setup"]["steps"]:
            health = s.get("health")
            if not health:
                continue
            if health["level"] == "ok":
                assert health["text"] not in body, f"{s['key']} draws its ok-health line"

    def test_a_signal_row_carries_no_card_of_its_own(self, seeded_app) -> None:
        """The LIST is the panel; a row inside it is whitespace and a hairline.
        Nine bordered cards with severity stripes read as nine alarms of equal
        rank, and the stripe was the only thing ranking them — severity is the
        count's ink now."""
        assert ".adash-row.is-error .adash-row__count" in _HUB_SRC
        # The declaration, not the word: the sheet's comments still explain why
        # the stripe and the bordered "why" quote were dropped.
        assert "border-left:" not in _HUB_SRC, "the per-row severity stripe is retired"

    def test_a_screen_reader_still_gets_each_step_state(self, seeded_app) -> None:
        """The state words came off the screen (a tick says "done", a numeral
        says "later"), so the glyph is aria-hidden and the word has to survive
        for anyone who cannot see it."""
        body = seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text
        assert 'class="aov-sr"' in body
        assert "— done" in body or "— step" in body or "— not checked" in body


class TestGridDeletionLeftNothingStranded:
    """The card grid was the ONLY link to several surfaces. Deleting it
    without moving them would have made them reachable by typed URL only —
    the exact regression these assertions exist to catch."""

    def _body(self, seeded_app) -> str:
        return seeded_app["client"].get("/admin", headers=_auth(seeded_app["admin_token"])).text

    def test_ordinary_admin_destinations_still_reachable(self, seeded_app):
        body = self._body(seeded_app)
        for href in ("/admin/users", "/admin/server-config", "/admin/tables"):
            assert f'href="{href}"' in body, f"{href} is no longer linked from /admin"

    def test_sync_is_reached_from_the_surface_that_owns_it(self, seeded_app):
        """/admin/sync is deliberately OFF the nav — a sync run is per-source,
        so a nav row would imply a cross-source page that does not exist. It is
        reached from a source card's SYNC cell instead, which `ADMIN_NAV_OFFNAV`
        records. It was in the list above while the /admin card grid linked
        everything; the grid is gone and the entry is the door now."""
        from app.web.admin_nav import ADMIN_NAV_OFFNAV

        entry = next((e for e in ADMIN_NAV_OFFNAV if e["href"] == "/admin/sync"), None)
        assert entry is not None, "/admin/sync left ADMIN_NAV_OFFNAV without gaining a nav row"
        assert entry.get("reached_from"), "an off-nav page must record the door it IS reached from"

    def test_chat_sessions_moved_into_the_sidebar(self, seeded_app):
        """Registered in app/api/admin_chat.py, not the web router — it was
        invisible to the sidebar's inventory until the grid was retired."""
        assert 'href="/admin/chat"' in self._body(seeded_app)

    def test_api_docs_moved_into_the_sidebar_footer(self, seeded_app):
        """Not /admin routes at all, so they ride a footer DISCLOSURE rather than
        an eighth section (see ADMIN_NAV_DOCS).

        All three are in the markup and all three are reachable — the group is
        rendered collapsed, not conditional, so `hidden` on the body is a default
        state a click undoes, never a link that isn't there. These URLs lost their
        home once when /admin's card grid was retired; this is the guard that
        noticed."""
        body = self._body(seeded_app)
        for href in ("/documentation/api", "/docs", "/redoc"):
            assert f'href="{href}"' in body, f"{href} lost its home in the sidebar"
        # …inside the disclosure, so the foot is one row at rest.
        assert 'data-admin-nav-group="docs"' in body
        assert 'id="admin-nav-body-docs" hidden' in body

    def test_studio_row_follows_its_instance_flag(self, seeded_app, monkeypatch):
        """A conditional nav item — no longer the only one. Studio is OFF by
        default since the admin cleanup, so the assertion flipped: what this
        guards is that the row still FOLLOWS the flag in both directions rather
        than becoming unconditional (which is how it would come back on a
        default-off instance and link to a redirect).

        The other four conditional rows are covered in
        tests/test_retired_admin_surfaces.py."""
        assert 'href="/admin/studio"' not in self._body(seeded_app)

        monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")
        assert 'href="/admin/studio"' in self._body(seeded_app)
