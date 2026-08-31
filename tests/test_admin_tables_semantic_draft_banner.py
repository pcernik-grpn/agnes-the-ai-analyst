"""/admin/tables surfaces the semantic auto-draft sweep's output.

`POST /api/admin/semantic-auto-draft-sweep` runs every 55 minutes and files
its drafts in the `authoring_suggestions` moderation queue as the non-admin
`semantic-drafter` identity. Nothing on /admin/tables said so, so the feature
worked and was invisible: an admin standing in front of the very tables it
drafted for had to already know the queue existed to find out.

The strip this file guards closes that. Scoped to the DRAFTER's own pending
rows — a person's proposal in the same shared queue is not "a table awaiting
approval of a suggested model", and counting it would make the sentence lie.
"""

import re

import pytest

from app.auth.system_users import SEMANTIC_DRAFTER_USER_EMAIL

STRIP_ID = "adminTablesSemanticDrafts"
SUGGESTIONS_QUEUE_HREF = "/admin/studio/suggestions"


@pytest.fixture(autouse=True)
def _studio_on(monkeypatch):
    """Studio is OFF by default, and the suggestions queue redirects home when
    it is — so the linking half of this strip only exists on an instance that
    exposes it. Tests that want the other branch turn it back off themselves.
    """
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "1")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def file_suggestion():
    """File pending `authoring_suggestions` rows, resolved again on teardown
    so a leftover row cannot leak a strip into another test."""
    from src.repositories import authoring_suggestions_repo

    created: list[str] = []

    def _file(*, created_by: str = SEMANTIC_DRAFTER_USER_EMAIL, domain: str = "semantic-layer") -> str:
        sid = authoring_suggestions_repo().create(
            domain=domain,
            payload={"slug": "orders", "document": "spec_version: 1.0\n"},
            created_by=created_by,
        )
        created.append(sid)
        return sid

    yield _file

    for sid in created:
        authoring_suggestions_repo().resolve(sid, status="rejected", resolved_by="test")


def _tables_html(seeded_app) -> str:
    resp = seeded_app["client"].get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    return resp.text


def _strip(html: str) -> str:
    """The auto-draft strip's own markup, or "" when the page renders none.

    Scoped on purpose: `/admin/studio/suggestions` also appears in the admin
    nav and the command palette, so a page-wide substring assertion would
    pass with no strip at all.
    """
    marker = f'id="{STRIP_ID}"'
    if marker not in html:
        return ""
    start = html.rindex("<div", 0, html.index(marker))
    return html[start : html.index("</div>", start) + len("</div>")]


def _sentence(strip_html: str) -> str:
    """The strip as a reader sees it — tags dropped, whitespace collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", strip_html).split())


def test_pending_auto_draft_is_announced_and_links_to_the_queue(seeded_app, file_suggestion):
    file_suggestion()
    strip = _strip(_tables_html(seeded_app))

    assert strip, "no auto-draft strip rendered for a pending drafter suggestion"
    assert "1 table awaits approval of a suggested model" in _sentence(strip)
    assert f'href="{SUGGESTIONS_QUEUE_HREF}"' in strip


def test_count_is_plural_and_accurate_for_several_drafts(seeded_app, file_suggestion):
    file_suggestion()
    file_suggestion()
    file_suggestion()

    assert "3 tables await approval of a suggested model" in _sentence(_strip(_tables_html(seeded_app)))


def test_nothing_extra_when_the_queue_is_empty(seeded_app):
    assert _strip(_tables_html(seeded_app)) == ""


def test_a_persons_proposal_does_not_claim_a_table_was_auto_drafted(seeded_app, file_suggestion):
    """The queue is shared with the authoring studio. Someone's own submission
    is not the sweep's output, and this strip only speaks for the sweep."""
    file_suggestion(created_by="analyst@example.com")

    assert _strip(_tables_html(seeded_app)) == ""


def test_a_drafter_row_in_another_domain_is_not_counted(seeded_app, file_suggestion):
    """`authoring_suggestions` is domain-keyed and the drafter identity could
    grow a second job later; this strip is about semantic models."""
    file_suggestion(domain="metrics")

    assert _strip(_tables_html(seeded_app)) == ""


def test_studio_off_states_the_count_without_a_link_that_redirects_home(seeded_app, file_suggestion, monkeypatch):
    """Rows filed while Studio was on survive it being turned off (the queue
    page then 302s home). Announce them, but do not send the admin through a
    door that bounces — name the toggle instead."""
    file_suggestion()
    monkeypatch.setenv("AGNES_STUDIO_ENABLED", "0")
    strip = _strip(_tables_html(seeded_app))

    assert "1 table awaits approval of a suggested model" in _sentence(strip)
    assert SUGGESTIONS_QUEUE_HREF not in strip
    assert "studio.enabled" in _sentence(strip)


def test_the_count_is_capped_rather_than_loading_the_whole_queue(seeded_app, monkeypatch):
    """A badge is not worth loading an unbounded result set. Past the cap the
    strip says "99+" and the repository is never asked for more than one row
    beyond it."""
    import app.web.router as router_mod

    asked_for: list[int] = []

    class _Fake:
        def list(self, *, status, domain, created_by, limit):
            asked_for.append(limit)
            return [{"id": f"asug_{i}"} for i in range(limit)]

    monkeypatch.setattr("src.repositories.authoring_suggestions_repo", lambda: _Fake())
    strip = _strip(_tables_html(seeded_app))

    assert asked_for == [router_mod._DRAFT_BADGE_CAP + 1], "the badge must cap its own query"
    assert f"{router_mod._DRAFT_BADGE_CAP}+ tables await approval of a suggested model" in _sentence(strip)


def test_an_unreadable_queue_never_breaks_the_page(seeded_app, monkeypatch):
    """A strip is not worth a 500. If the suggestions store cannot be read,
    /admin/tables still renders — without it."""

    def _boom():
        raise RuntimeError("suggestions store unavailable")

    monkeypatch.setattr("src.repositories.authoring_suggestions_repo", _boom)

    resp = seeded_app["client"].get("/admin/tables", headers=_auth(seeded_app["admin_token"]))
    assert resp.status_code == 200
    assert _strip(resp.text) == ""
