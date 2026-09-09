"""The "Report a problem" rail entry + dialog partial (issue reporting, step 1).

Issue reports are a Postgres-only table pair (A3 PG-first ratchet) — the rail
button, the user-menu item, and the dialog partial (`_issue_dialog.html`) must
all be gated on `can_report_issue` so a DuckDB-backed instance renders no
button rather than one that answers a typed 501. See
`app/web/router.py::_issue_reporting_available` and
`docs/superpowers/specs/2026-09-09-issue-reporting-step1-design.md`.

Patches `app.web.router._issue_reporting_available` directly rather than
`src.repositories.use_pg` — `/library`'s own body touches a lot of
DuckDB-backed repositories on every render, and flipping `use_pg()` globally
for the whole page (rather than just the one gate under test) would exercise
an untested PG code path this test isn't about. Behaviour of
`_issue_reporting_available()` itself (the `use_pg()` read) is a one-line
pass-through, exercised implicitly by every `tests/db_pg/` run of the
Postgres-backed test suite.
"""

from __future__ import annotations


def _auth_client(seeded_app, token: str):
    client = seeded_app["client"]
    client.cookies.set("access_token", token)
    return client


class TestRailReportButton:
    def test_rail_has_report_button_on_postgres(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: True)
        client = _auth_client(seeded_app, seeded_app["analyst_token"])
        try:
            resp = client.get("/library")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200, resp.text
        html = resp.text

        assert 'id="rail-report-issue"' in html
        assert "Report a problem" in html
        assert 'id="rail-report-issue-menu"' in html
        assert 'id="issue-dialog"' in html
        assert "Include a screenshot of this page" in html
        assert "client_diag.js" in html
        assert "issue_report.js" in html

    def test_rail_hides_report_button_on_duckdb(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: False)
        client = _auth_client(seeded_app, seeded_app["analyst_token"])
        try:
            resp = client.get("/library")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200, resp.text
        html = resp.text

        assert 'id="rail-report-issue"' not in html
        assert 'id="rail-report-issue-menu"' not in html
        assert 'id="issue-dialog"' not in html
        # The vendored script tags stay off the gate too — no reason to load
        # css/drawer.css or issue_dialog.css for a dialog that never renders.
        assert "css/issue_dialog.css" not in html


class TestClientDiagAlwaysLoads:
    """client_diag.js is app-wide (installed before "Report a problem" even
    exists as a concept, so it can capture errors from every other script),
    unlike the rail entry and dialog partial above."""

    def test_client_diag_loads_regardless_of_the_gate(self, seeded_app, monkeypatch):
        monkeypatch.setattr("app.web.router._issue_reporting_available", lambda: False)
        client = _auth_client(seeded_app, seeded_app["analyst_token"])
        try:
            resp = client.get("/library")
        finally:
            client.cookies.clear()
        assert resp.status_code == 200, resp.text
        assert "client_diag.js" in resp.text


class TestScriptLoadOrder:
    def test_client_diag_is_the_first_script_and_not_deferred(self):
        text = _app_scripts_source()
        first_script = text.index("<script")
        # The exact static_url() call, not the bare "js/client_diag.js"
        # substring — that also appears in this script tag's own prose
        # comment ("See app/web/static/js/client_diag.js."), which precedes
        # the tag itself and would otherwise match first.
        diag_idx = text.index("static_url('js/client_diag.js')")
        tag_start = text.rfind("<script", 0, diag_idx)
        assert tag_start == first_script, "client_diag.js must be the first <script> tag in _app_scripts.html"
        tag_end = text.index(">", diag_idx)
        tag = text[tag_start : tag_end + 1]
        assert "defer" not in tag, "client_diag.js must be non-deferred"

    def test_issue_report_js_is_deferred(self):
        text = _app_scripts_source()
        idx = text.index("static_url('js/issue_report.js')")
        tag_start = text.rfind("<script", 0, idx)
        tag_end = text.index(">", idx)
        tag = text[tag_start : tag_end + 1]
        assert "defer" in tag, "issue_report.js must be deferred"

    def test_html2canvas_url_is_stamped_next_to_mermaid(self):
        text = _app_scripts_source()
        assert "_agHtml2CanvasUrl" in text
        assert "vendor/html2canvas.min.js" in text
        assert "_agMermaidUrl" in text


def _app_scripts_source() -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "app" / "web" / "templates" / "_app_scripts.html"
    return path.read_text(encoding="utf-8")
