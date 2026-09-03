"""Web surfaces for the skill linter (v89, Task 7):

* ``/admin/store/lint`` admin curator page (server-rendered findings + audit)
* owner-visible findings on ``/marketplace/flea/{id}/edit``
* the skill-author chat profile advertising the dry-run/lint step

Reuses the ``web_client`` + ``_make_admin`` fixtures from
``test_store_lint_api`` (fresh app per test, no LLM key → degraded lint).
"""

from __future__ import annotations

from tests.test_store_api import _create_user
from tests.test_store_lint_api import _make_admin, _publish, web_client  # noqa: F401


def _seed_finding(entity_id: str, *, message: str, rule_id: str = "SL002", content_hash: str = "h1") -> str:
    from src.repositories import store_lint_repo

    repo = store_lint_repo()
    run_id = repo.start_run("admin")
    repo.replace_findings(
        entity_id,
        run_id,
        [
            {
                "rule_id": rule_id,
                "severity": "warn",
                "message": message,
                "evidence": {},
                "doc_url": f"/docs/skill-guidelines#{rule_id.lower()}",
            }
        ],
        content_hash,
    )
    repo.finish_run(run_id, linted=1, skipped=0, findings=1)
    return content_hash


class TestLintIsReachable:
    """The lint page has no sidebar row — it is in ``ADMIN_NAV_OFFNAV``, whose
    contract is that an off-nav page states the door it IS reached from. This
    is that door, so it is a guard and not a detail: drop the link and the page
    becomes URL-only, which is the state the off-nav list exists to prevent.

    On Submissions rather than the moderation hub (/admin/store) on purpose —
    that page is off by default (``features.store_moderation_enabled``) and
    redirects home when it is, so lint's only door cannot hang there.
    """

    def test_the_submissions_queue_links_the_lint_page(self, web_client):  # noqa: F811
        _, admin_cookies = _make_admin(web_client, "admin@x.com")
        resp = web_client.get(
            "/admin/store/submissions", cookies=admin_cookies, headers={"Accept": "text/html"}
        )
        assert resp.status_code == 200, resp.text
        assert '/admin/store/lint' in resp.text, (
            "the Submissions queue no longer links /admin/store/lint — that link is the "
            "lint page's only door (see ADMIN_NAV_OFFNAV in app/web/admin_nav.py)"
        )

    def test_lint_has_no_sidebar_row_of_its_own(self):
        """The other half of the same decision: if a row comes back, this door
        is redundant rather than load-bearing, and the two should be reconciled
        deliberately instead of drifting into both."""
        from app.web.admin_nav import ADMIN_NAV_OFFNAV, ADMIN_NAV_SECTIONS, _section_entries

        rows = {e["href"] for s in ADMIN_NAV_SECTIONS for e in _section_entries(s)}
        assert "/admin/store/lint" not in rows
        assert any(e["href"] == "/admin/store/lint" and e.get("reached_from") for e in ADMIN_NAV_OFFNAV)


class TestAdminLintPage:
    def test_admin_sees_findings_and_audit_button(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "alice@x.com")
        entity_id = _publish(web_client, cookies).json()["id"]
        _seed_finding(entity_id, message="This skill body is unusually large.")

        _, admin_cookies = _make_admin(web_client, "admin@x.com")
        resp = web_client.get("/admin/store/lint", cookies=admin_cookies, headers={"Accept": "text/html"})
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Audit now" in body
        assert "This skill body is unusually large." in body
        # base_ds chrome actually rendered (the _chrome_ctx regression guard):
        # the shared nav and a real stylesheet href must be present. The
        # markers are the rail's since Wave 0 (2026-08) retired the topnav.
        assert 'class="rail' in body
        assert "rail-i" in body
        assert ".css" in body

    def test_dismissed_finding_hidden_by_default(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "bob@x.com")
        entity_id = _publish(web_client, cookies).json()["id"]
        h = _seed_finding(entity_id, message="Dismiss-me finding.", rule_id="SL011")

        from src.repositories import store_lint_repo

        store_lint_repo().dismiss(entity_id, "SL011", "admin@x.com", h)

        _, admin_cookies = _make_admin(web_client, "admin@x.com")
        default_view = web_client.get("/admin/store/lint", cookies=admin_cookies)
        assert "Dismiss-me finding." not in default_view.text
        shown = web_client.get("/admin/store/lint?include_dismissed=true", cookies=admin_cookies)
        assert "Dismiss-me finding." in shown.text

    def test_non_admin_blocked(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "carol@x.com")
        r = web_client.get("/admin/store/lint", cookies=cookies)
        assert r.status_code == 403


class TestOwnerFindings:
    def test_owner_sees_findings_on_edit_page(self, web_client):  # noqa: F811
        _, cookies = _create_user(web_client, "dave@x.com")
        entity_id = _publish(web_client, cookies).json()["id"]
        _seed_finding(entity_id, message="Owner-visible advisory message.", rule_id="SL010")

        resp = web_client.get(
            f"/marketplace/flea/{entity_id}/edit",
            cookies=cookies,
            headers={"Accept": "text/html"},
        )
        assert resp.status_code == 200, resp.text
        assert "Owner-visible advisory message." in resp.text
        assert "/docs/skill-guidelines#sl010" in resp.text


class TestProfileMentionsLint:
    def test_skill_author_profile_advises_dry_run(self):
        from app.chat.profiles import get_profile

        prof = get_profile("skill-author")
        assert prof is not None
        assert "dry-run" in prof.claude_md.lower()
        assert "/docs/skill-guidelines" in prof.claude_md
