"""Tests for the ``knowledge_artifacts`` manifest ``kind:"digest"`` entries and
``GET /api/knowledge/digests/{digest_id}/content`` (K4, #799).

Covers:
- GET /api/sync/manifest: ``knowledge_artifacts`` gains ``kind == "digest"``
  entries even when no K3 chunk artifact exists (the old early-return on
  empty packaging state must not suppress digests); K3 ``kind == "chunks"``
  entries keep co-existing; RBAC fail-closed via ``resource_grants`` on
  ``ResourceType.KNOWLEDGE_DIGEST``; ``pending`` (never-generated) digests
  are never listed; ``stale`` digests are listed with their reason and a
  different ``md5`` than the fresh state (the md5 is a change-token
  covering content AND staleness).
- GET /api/knowledge/digests/{digest_id}/content: 401 unauthenticated.

  **House-style decision (deviates from the plan's original 404-only
  guess):** the sibling K3 endpoint (``GET
  /api/knowledge/artifacts/{corpus_id}/download``) was refactored in this
  branch's history (commit 9053edd2) to gate via
  ``Depends(require_resource_access(ResourceType.COLLECTION, ...))``, which
  raises **403** for an ungranted-but-known resource (see
  ``tests/test_api_knowledge_artifacts.py::test_download_ungranted_analyst_403``).
  The digest content endpoint mirrors that house style exactly:
  ``require_resource_access(ResourceType.KNOWLEDGE_DIGEST, "{digest_id}")``
  → **403** for an ungranted analyst on a real digest id. Unknown id or a
  digest that has never generated (``pending``, empty ``output_md``) stays
  **404** for an admin (who always passes the resource-access gate) — same
  posture as ``test_download_granted_corpus_no_artifact_built_404``.
"""

from __future__ import annotations

from src.db import get_system_db


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_chunk_artifact(seeded_app, corpus_id: str, name: str = "Handbook") -> None:
    """Build a K3 chunks artifact so it co-exists with digest entries."""
    from unittest.mock import patch

    from src.knowledge_packaging import run_packaging_pass

    chunks = [
        {
            "id": "ck1",
            "corpus_id": corpus_id,
            "file_id": "f1",
            "ordinal": 0,
            "text": "invoices are monthly",
            "embedding": None,
            "section_path": None,
            "page": None,
            "bbox": None,
            "metadata": None,
            "created_at": None,
        },
    ]
    with (
        patch("src.knowledge_packaging._list_chunks", lambda cid: list(chunks)),
        patch("src.knowledge_packaging._list_files", lambda cid: [{"id": "f1", "filename": "billing.md"}]),
        patch("src.knowledge_packaging._list_corpora", lambda: [{"id": corpus_id, "name": name}]),
    ):
        run_packaging_pass()


def _create_digest(slug: str = "arch-overview", title: str = "Architecture overview") -> str:
    from src.repositories import knowledge_digests_repo

    return knowledge_digests_repo().create(
        slug=slug,
        title=title,
        instructions="Maintain an overview of our architecture.",
        source_corpus_ids=[],
        created_by="admin1",
    )


def _generate(digest_id: str, output_md: str = "# Architecture\n\nOverview.", model: str = "test-model") -> None:
    from src.repositories import knowledge_digests_repo

    knowledge_digests_repo().set_generated(digest_id, output_md=output_md, source_fingerprint="fp1", model=model)


def _mark_stale(digest_id: str, reason: str = "LLM timeout") -> None:
    from src.repositories import knowledge_digests_repo

    knowledge_digests_repo().mark_stale(digest_id, reason=reason)


def _grant_analyst(digest_id: str, group_name: str = "Digest Readers") -> None:
    """Create a group, add the seeded analyst to it, grant the group access
    to ``digest_id``. ``seeded_app`` users are NOT auto-added to Everyone
    (PR #131 removed the implicit membership — see
    ``src/repositories/users.py``), so the K3 artifact test's "grant
    Everyone" idiom does not actually reach the analyst; this mirrors the
    real group-membership pattern used by ``tests/test_api_stack.py``.
    """
    from src.repositories import resource_grants_repo, user_groups_repo
    from src.repositories.user_group_members import UserGroupMembersRepository

    conn = get_system_db()
    group = user_groups_repo().create(name=group_name, description="", created_by="test")
    gid = group["id"] if isinstance(group, dict) else group
    UserGroupMembersRepository(conn).add_member("analyst1", gid, source="test")
    conn.close()
    resource_grants_repo().create(
        group_id=gid,
        resource_type="knowledge_digest",
        resource_id=digest_id,
        assigned_by="test",
    )


def _digest_entries(body: dict) -> dict:
    return {e["id"]: e for e in body["knowledge_artifacts"] if e.get("kind") == "digest"}


class TestManifestDigestEntries:
    def test_admin_sees_generated_digest_entry(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        did = _create_digest()
        _generate(did)

        resp = c.get("/api/sync/manifest", headers=_auth(admin))
        assert resp.status_code == 200, resp.text
        entries = _digest_entries(resp.json())
        assert did in entries
        entry = entries[did]
        assert entry["kind"] == "digest"
        assert entry["slug"] == "arch-overview"
        assert entry["title"] == "Architecture overview"
        assert entry["status"] == "fresh"
        assert entry["status_reason"] is None
        assert entry["generated_at"]
        assert entry["md5"]
        assert entry["url"] == f"/api/knowledge/digests/{did}/content"

    def test_digest_entries_coexist_with_chunk_artifact_entries(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        col = c.post("/api/collections", json={"name": "KA Col"}, headers=_auth(admin)).json()
        _seed_chunk_artifact(seeded_app, col["id"])
        did = _create_digest("mixed-slug", "Mixed")
        _generate(did)

        resp = c.get("/api/sync/manifest", headers=_auth(admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        kinds = {e["kind"] for e in body["knowledge_artifacts"]}
        assert kinds == {"chunks", "digest"}
        chunk_entries = {e["corpus_id"]: e for e in body["knowledge_artifacts"] if e["kind"] == "chunks"}
        assert col["id"] in chunk_entries
        assert did in _digest_entries(body)

    def test_digest_entries_listed_even_without_any_chunk_artifact(self, seeded_app):
        """Regression: the old early-return on empty packaging state must not
        suppress digests when no K3 artifact exists at all."""
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        did = _create_digest("solo-digest", "Solo")
        _generate(did)

        resp = c.get("/api/sync/manifest", headers=_auth(admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert did in _digest_entries(body)

    def test_ungranted_analyst_digest_absent_then_granted_present(self, seeded_app):
        c = seeded_app["client"]
        analyst = seeded_app["analyst_token"]
        did = _create_digest("private-digest", "Private")
        _generate(did)

        resp = c.get("/api/sync/manifest", headers=_auth(analyst))
        assert resp.status_code == 200, resp.text
        assert did not in _digest_entries(resp.json())

        _grant_analyst(did)

        resp2 = c.get("/api/sync/manifest", headers=_auth(analyst))
        assert resp2.status_code == 200, resp2.text
        assert did in _digest_entries(resp2.json())

    def test_pending_digest_never_listed(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        did = _create_digest("never-generated", "Never Generated")
        # No set_generated call — status stays 'pending', output_md is NULL.

        resp = c.get("/api/sync/manifest", headers=_auth(admin))
        assert resp.status_code == 200, resp.text
        assert did not in _digest_entries(resp.json())

    def test_stale_digest_listed_with_reason_and_different_md5(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        did = _create_digest("goes-stale", "Goes Stale")
        _generate(did)

        fresh_resp = c.get("/api/sync/manifest", headers=_auth(admin))
        fresh_md5 = _digest_entries(fresh_resp.json())[did]["md5"]

        _mark_stale(did, reason="LLM timeout")

        stale_resp = c.get("/api/sync/manifest", headers=_auth(admin))
        assert stale_resp.status_code == 200, stale_resp.text
        stale_entry = _digest_entries(stale_resp.json())[did]
        assert stale_entry["status"] == "stale"
        assert stale_entry["status_reason"] == "LLM timeout"
        # md5 is a change-token covering content AND staleness — it must
        # flip when the digest goes stale so `agnes pull` re-fetches and
        # the staleness banner reaches the laptop.
        assert stale_entry["md5"] != fresh_md5

    def test_manifest_key_present_with_no_digests_at_all(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        resp = c.get("/api/sync/manifest", headers=_auth(admin))
        assert resp.status_code == 200, resp.text
        assert resp.json()["knowledge_artifacts"] == []


class TestDigestContentEndpoint:
    def test_unauthenticated_401(self, seeded_app):
        resp = seeded_app["client"].get("/api/knowledge/digests/kd_bogus/content")
        assert resp.status_code == 401

    def test_ungranted_analyst_403(self, seeded_app):
        c = seeded_app["client"]
        analyst = seeded_app["analyst_token"]
        did = _create_digest("ungranted-digest", "Ungranted")
        _generate(did)

        resp = c.get(f"/api/knowledge/digests/{did}/content", headers=_auth(analyst))
        # House-style match with the sibling artifact-download endpoint's
        # require_resource_access gate — 403, not the plan's original
        # 404-only guess.
        assert resp.status_code == 403

    def test_unknown_id_404_for_admin(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        resp = c.get("/api/knowledge/digests/kd_does_not_exist/content", headers=_auth(admin))
        assert resp.status_code == 404

    def test_never_generated_digest_404_for_admin(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        did = _create_digest("never-gen-content", "Never Generated Content")

        resp = c.get(f"/api/knowledge/digests/{did}/content", headers=_auth(admin))
        assert resp.status_code == 404

    def test_never_generated_digest_404_for_granted_analyst(self, seeded_app):
        c = seeded_app["client"]
        analyst = seeded_app["analyst_token"]
        did = _create_digest("never-gen-granted", "Never Generated Granted")
        _grant_analyst(did)

        resp = c.get(f"/api/knowledge/digests/{did}/content", headers=_auth(analyst))
        assert resp.status_code == 404

    def test_admin_200_shape(self, seeded_app):
        c = seeded_app["client"]
        admin = seeded_app["admin_token"]
        did = _create_digest("content-shape", "Content Shape")
        _generate(did, output_md="# Content Shape\n\nBody text.")

        resp = c.get(f"/api/knowledge/digests/{did}/content", headers=_auth(admin))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == did
        assert body["slug"] == "content-shape"
        assert body["title"] == "Content Shape"
        assert body["output_md"] == "# Content Shape\n\nBody text."
        assert body["status"] == "fresh"
        assert body["status_reason"] is None
        assert body["generated_at"]

    def test_granted_analyst_200(self, seeded_app):
        c = seeded_app["client"]
        analyst = seeded_app["analyst_token"]
        did = _create_digest("granted-content", "Granted Content")
        _generate(did)
        _grant_analyst(did)

        resp = c.get(f"/api/knowledge/digests/{did}/content", headers=_auth(analyst))
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == did


class TestAnalystDigestList:
    """``GET /api/knowledge/digests`` — the enumeration a web surface needs
    (TCRD-250).

    The content endpoint and the pulled `.claude/rules/ka_<slug>.md` files
    have existed since K4; what was missing was any way to LIST what a caller
    can read, so a page could not show which digests exist without already
    knowing an id.

    The contract that matters: this list and the sync manifest share one
    predicate, so a caller can never be offered a digest in the browser that
    `agnes pull` would refuse them (or the reverse).
    """

    def test_unauthenticated_is_401(self, seeded_app):
        assert seeded_app["client"].get("/api/knowledge/digests").status_code == 401

    def test_admin_sees_a_generated_digest(self, seeded_app):
        c, admin = seeded_app["client"], seeded_app["admin_token"]
        did = _create_digest()
        _generate(did)
        r = c.get("/api/knowledge/digests", headers=_auth(admin))
        assert r.status_code == 200, r.text
        assert [d["slug"] for d in r.json()["digests"]] == ["arch-overview"]

    def test_a_never_generated_digest_is_never_listed(self, seeded_app):
        """Listing it would promise a page that 404s — the content endpoint
        returns 404 for an empty `output_md`. Same rule as the manifest."""
        c, admin = seeded_app["client"], seeded_app["admin_token"]
        _create_digest(slug="pending-one", title="Not yet run")
        r = c.get("/api/knowledge/digests", headers=_auth(admin))
        assert r.json()["digests"] == []

    def test_an_ungranted_analyst_sees_nothing(self, seeded_app):
        c = seeded_app["client"]
        did = _create_digest()
        _generate(did)
        r = c.get("/api/knowledge/digests", headers=_auth(seeded_app["analyst_token"]))
        assert r.status_code == 200, r.text
        assert r.json()["digests"] == []

    def test_a_granted_analyst_sees_it(self, seeded_app):
        c = seeded_app["client"]
        did = _create_digest()
        _generate(did)
        _grant_analyst(did)
        r = c.get("/api/knowledge/digests", headers=_auth(seeded_app["analyst_token"]))
        assert [d["slug"] for d in r.json()["digests"]] == ["arch-overview"]

    def test_the_list_agrees_with_the_manifest_for_the_same_caller(self, seeded_app):
        """The contract this endpoint exists to keep. Both sides filter with
        `_caller_can_read_digest`; if they ever diverge, a reader is offered
        something `agnes pull` will not give them."""
        c = seeded_app["client"]
        granted = _create_digest(slug="granted-one", title="Granted")
        _generate(granted)
        _grant_analyst(granted)
        withheld = _create_digest(slug="withheld-one", title="Withheld")
        _generate(withheld)

        headers = _auth(seeded_app["analyst_token"])
        listed = {d["slug"] for d in c.get("/api/knowledge/digests", headers=headers).json()["digests"]}
        manifest = c.get("/api/sync/manifest", headers=headers).json()
        in_manifest = {
            e["slug"] for e in manifest.get("knowledge_artifacts", []) if e.get("kind") == "digest"
        }
        assert listed == in_manifest == {"granted-one"}

    def test_staleness_travels_so_a_stale_digest_is_visibly_stale(self, seeded_app):
        c, admin = seeded_app["client"], seeded_app["admin_token"]
        did = _create_digest()
        _generate(did)
        _mark_stale(did, reason="LLM timeout")
        (row,) = c.get("/api/knowledge/digests", headers=_auth(admin)).json()["digests"]
        assert row["status"] == "stale"
        assert row["status_reason"] == "LLM timeout"

    def test_the_markdown_itself_is_not_in_the_list(self, seeded_app):
        """A list is a list. The body stays behind the per-digest content
        endpoint, which audits each read."""
        c, admin = seeded_app["client"], seeded_app["admin_token"]
        did = _create_digest()
        _generate(did, output_md="# Secret heading\n\nBody.")
        body = c.get("/api/knowledge/digests", headers=_auth(admin)).text
        assert "output_md" not in body
        assert "Secret heading" not in body
