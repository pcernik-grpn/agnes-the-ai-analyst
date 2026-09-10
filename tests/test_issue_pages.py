"""``/me/issues`` and ``/admin/issues`` — the reporter and admin-queue web
pages for issue reports (issue reporting, step 3 of the design, pulled
forward on its own — see docs/superpowers/specs/2026-09-09-issue-reporting-
step1-design.md). Both are static shells: list/detail/comment/resolve are
fetched client-side from the EXISTING JSON API (app/api/issues.py) by
js/issue_pages.js, so these tests only cover the two new page ROUTES —
gating, chrome, and that the shell wires the right JS/markup for its mode.

Issue reports are a Postgres-only table pair (A3 PG-first ratchet), so both
routes are gated on ``_issue_reporting_available()`` — same flag, same
reasoning, as the rail's "Report a problem" button
(tests/test_issue_dialog_template.py). Patches
``app.web.router._issue_reporting_available`` directly rather than
``src.repositories.use_pg`` for the same reason that file gives: flipping
``use_pg()`` globally would exercise untested PG code paths other parts of
the page render touch.
"""

from __future__ import annotations


def _auth_client(seeded_app, token: str):
    client = seeded_app["client"]
    client.cookies.set("access_token", token)
    return client


class TestMeIssuesPage:
    def test_renders_for_entitled_user(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: True)
        client = _auth_client(seeded_app, seeded_app["analyst_token"])
        try:
            resp = client.get("/me/issues")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200, resp.text
        html = resp.text

        assert 'id="iss-page"' in html
        assert 'data-mode="mine"' in html
        assert 'id="iss-status-filter"' in html
        assert 'id="iss-detail"' in html
        assert "issue_pages.js" in html
        assert "My reports" in html
        # The reporter's own filings, not the admin queue's Reporter column.
        assert "<th>Reporter</th>" not in html

    def test_redirects_home_when_reporting_unavailable(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: False)
        client = _auth_client(seeded_app, seeded_app["analyst_token"])
        try:
            resp = client.get("/me/issues", follow_redirects=False)
        finally:
            client.cookies.clear()
        assert resp.status_code == 302, resp.text
        assert resp.headers["location"] == "/"

    def test_requires_sign_in(self, seeded_app):
        client = seeded_app["client"]
        client.cookies.clear()
        resp = client.get("/me/issues", follow_redirects=False)
        assert resp.status_code in (302, 303, 401)


class TestAdminIssuesPage:
    def test_renders_for_admin(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: True)
        client = _auth_client(seeded_app, seeded_app["admin_token"])
        try:
            resp = client.get("/admin/issues")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200, resp.text
        html = resp.text

        assert 'id="iss-page"' in html
        assert 'data-mode="admin"' in html
        assert "<th>Reporter</th>" in html
        assert 'id="iss-resolve-btn"' in html
        assert "issue_pages.js" in html
        assert "Issue reports" in html

    def test_redirects_home_when_reporting_unavailable(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: False)
        client = _auth_client(seeded_app, seeded_app["admin_token"])
        try:
            resp = client.get("/admin/issues", follow_redirects=False)
        finally:
            client.cookies.clear()
        assert resp.status_code == 302, resp.text
        assert resp.headers["location"] == "/"

    def test_refuses_non_admin(self, seeded_app, monkeypatch):
        """require_admin is a FastAPI dependency, so it is resolved (and
        raises) before the handler body's own _issue_reporting_available
        check ever runs — true regardless of the PG-backend flag."""
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: True)
        client = _auth_client(seeded_app, seeded_app["analyst_token"])
        try:
            resp = client.get("/admin/issues", follow_redirects=False)
        finally:
            client.cookies.clear()
        assert resp.status_code == 403, resp.text


class TestAdminNavEntry:
    def test_issue_reports_row_registered(self):
        from app.web.admin_nav import ADMIN_NAV_SECTIONS, _section_entries

        rows = [entry for section in ADMIN_NAV_SECTIONS for entry in _section_entries(section)]
        matches = [r for r in rows if r["href"] == "/admin/issues"]
        assert len(matches) == 1, "expected exactly one /admin/issues entry in ADMIN_NAV_SECTIONS"
        assert matches[0]["when"] == "can_report_issue"

    def test_row_renders_only_when_flag_is_true(self, seeded_app, monkeypatch):
        client = _auth_client(seeded_app, seeded_app["admin_token"])
        try:
            monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: True)
            resp_on = client.get("/admin/users")
            assert resp_on.status_code == 200, resp_on.text
            assert 'href="/admin/issues"' in resp_on.text

            monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: False)
            resp_off = client.get("/admin/users")
            assert resp_off.status_code == 200, resp_off.text
            assert 'href="/admin/issues"' not in resp_off.text
        finally:
            client.cookies.clear()


class TestTheDetailDrawerSurvivesFastClicking:
    """Two rows opened in quick succession must not cross their responses.

    `openDetail` starts a fetch per click and the first to ARRIVE is not
    necessarily the one asked for last. Without a guard the drawer could show
    issue A's text while the comment box and Resolve button still addressed
    issue B, because those read `currentDetailId` (#2402).
    """

    def _source(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "issue_pages.js").read_text(
            encoding="utf-8"
        )

    def test_a_stale_response_is_dropped(self):
        src = self._source()
        assert "if (generation !== detailGeneration) return;" in src, (
            "openDetail must ignore a response that does not belong to the current opening"
        )

    def test_the_guard_is_keyed_on_the_opening_not_the_issue_id(self):
        """Keyed on the id alone, closing and reopening the SAME report let an
        abandoned request back in: its id matches again, so it could land its
        older snapshot over what the second opening had already fetched, or
        over a comment posted since (#2402)."""
        src = self._source()
        assert "var generation = ++detailGeneration;" in src
        # close() must move the counter too, or a response outstanding when
        # the drawer closed is still "current" for the next opening.
        assert "detailGeneration++;" in src
        assert "currentDetailId !== id" not in src, "the id-keyed guard is the one this replaced"

    def test_the_guard_precedes_any_rendering(self):
        """The bail-out has to come BEFORE the handler touches the DOM —
        after `loading.hidden = false` it would already have flickered the
        wrong state onto the active drawer."""
        src = self._source()
        handler = src[
            src.index("getJson('/api/issues/")
            if "getJson('/api/issues/" in src
            else src.index('getJson("/api/issues/') :
        ]
        body = handler[: handler.index("});")]
        guard = body.index("generation !== detailGeneration")
        assert guard < body.index("loading.hidden = true")


class TestTheDrawerIsReachableByAssistiveTech:
    """The panel ships `aria-hidden="true"` so it is out of the accessibility
    tree while closed. Clearing `hidden` and adding `is-open` does not undo
    that, so the state has to be flipped explicitly both ways — otherwise a
    screen-reader user gets a drawer they can see nothing of (review on
    #2402)."""

    def _source(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "app" / "web" / "static" / "js" / "issue_pages.js").read_text(
            encoding="utf-8"
        )

    def test_opening_exposes_it_and_closing_hides_it_again(self):
        src = self._source()
        assert 'setAttribute("aria-hidden", "false")' in src
        assert 'setAttribute("aria-hidden", "true")' in src

    def test_the_closed_markup_starts_hidden(self):
        from pathlib import Path

        drawer = (
            Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "_issue_detail_drawer.html"
        ).read_text(encoding="utf-8")
        assert 'aria-hidden="true"' in drawer
