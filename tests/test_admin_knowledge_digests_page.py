"""Tests for the /admin/knowledge-digests page (K4, #799).

Mirrors tests/test_admin_data_sources_page.py: server renders a thin shell
(data + CRUD fetched client-side against /api/admin/knowledge-digests), so
the assertions here are auth gating + page-shell markers, not full CRUD
behavior (that's covered by tests/test_api_knowledge_digests.py).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def digests_page_on(monkeypatch):
    """The PAGE is hidden by default since the admin cleanup (the API, the CLI
    and the scheduler job are untouched — see get_knowledge_digests_ui_enabled).
    These tests are the page's own auth gating and shell markers, so they
    expose it."""
    monkeypatch.setenv("AGNES_KNOWLEDGE_DIGESTS_ENABLED", "1")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_digest(
    slug: str = "architecture-overview",
    title: str = "Architecture overview",
    status: str = "fresh",
):
    from src.db import get_system_db
    from src.repositories.knowledge_digests import KnowledgeDigestsRepository

    conn = get_system_db()
    repo = KnowledgeDigestsRepository(conn)
    digest_id = repo.create(
        slug=slug,
        title=title,
        instructions="Maintain an overview of our architecture.",
        source_corpus_ids=[],
        created_by="admin@test.com",
    )
    if status == "stale":
        repo.set_generated(digest_id, output_md="# " + title, source_fingerprint="fp1", model="test-model")
        repo.mark_stale(digest_id, reason="LLM timeout")
    elif status == "fresh":
        repo.set_generated(digest_id, output_md="# " + title, source_fingerprint="fp1", model="test-model")
    conn.close()
    return digest_id


class TestKnowledgeDigestsPageAuth:
    def test_admin_can_load_page(self, seeded_app):
        _seed_digest()
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/knowledge-digests", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.text

        # Heading.
        assert "Maintained digests" in body

        # Seeded digest surfaces server-rendered only if pre-rendered; page
        # is a client-fetch shell, so assert the endpoint constant + the
        # resource-access pointer instead of the digest title (fetched
        # client-side via JS, not present in the initial HTML).
        assert "/api/admin/knowledge-digests" in body

        # Pointer to where grants are managed — the Access workspace.
        assert "/admin/access" in body
        assert "grants" in body.lower()

    def test_non_admin_cannot_load_page(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        resp = c.get("/admin/knowledge-digests", headers=_auth(token))
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, seeded_app):
        c = seeded_app["client"]
        resp = c.get("/admin/knowledge-digests", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)


class TestKnowledgeDigestsPageStatusBadges:
    def test_status_badge_classes_present_for_fresh_stale_pending(self, seeded_app):
        """The client-side JS renders fresh/stale/pending badges; assert the
        rendering logic + collections endpoint constant ship in the page so
        a future refactor can't silently drop a status branch."""
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/knowledge-digests", headers=_auth(token))
        assert resp.status_code == 200
        body = resp.text
        assert "fresh" in body.lower()
        assert "stale" in body.lower()
        assert "pending" in body.lower()
        # source-collections multi-select is fed from GET /api/collections.
        assert "/api/collections" in body


class TestKnowledgeDigestsNav:
    def test_nav_link_present_for_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["admin_token"]
        resp = c.get("/admin/knowledge-digests", headers=_auth(token))
        assert resp.status_code == 200
        assert "/admin/knowledge-digests" in resp.text
        assert "Maintained digests" in resp.text

    def test_nav_link_absent_for_non_admin(self, seeded_app):
        c = seeded_app["client"]
        token = seeded_app["analyst_token"]
        # Analysts land on a page that does render the (non-admin) nav —
        # use a page they CAN access to confirm the admin-only nav item
        # is suppressed for non-admins.
        resp = c.get("/me/profile", headers=_auth(token))
        assert resp.status_code == 200
        assert "/admin/knowledge-digests" not in resp.text
