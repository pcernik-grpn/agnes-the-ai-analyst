"""Tests for /api/collections — Collections Slice 2 (Upload).

Covers:
- Admin creates a collection (201); non-admin gets 403.
- Unauthenticated request gets 401.
- Admin GET list returns the collection; non-member analyst gets empty list.
- RBAC-granted member can GET collection detail; non-member gets 403.
- Member uploads a tier1 file → 200, processing_status='pending'.
- Member uploads a .dwg file → 422, processing_status='rejected'.
- Non-member file upload → 403.
- GET /files for collection lists the uploaded file with correct status.
- Admin soft-deletes collection → 204; then 404 on GET.
"""

from __future__ import annotations

import io


from src.db import get_system_db


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _seed_everyone_membership(user_id: str) -> None:
    """Put ``user_id`` in Everyone so an Everyone-scoped grant reaches them.

    Membership is never implicit (see ``_seed_collection_grant``), and the
    fixture only seeds the admin's — so a test that shares *to* Everyone and
    then reads *as* the analyst has to add the row itself.
    """
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    grp = UserGroupsRepository(conn).get_by_name("Everyone")
    assert grp, "Everyone group must be seeded"
    members = UserGroupMembersRepository(conn)
    if grp["id"] not in set(members.list_groups_for_user(user_id)):
        members.add_member(user_id, grp["id"], source="system_seed")
    conn.close()


def _seed_collection_grant(corpus_id: str, user_id: str) -> None:
    """Give ``user_id`` access to the collection.

    Group membership is no longer implicit — ``_user_group_ids``
    (app/auth/access.py) returns only concrete ``user_group_members`` rows, so
    a user is in Everyone only if a real membership row exists (in production
    that row comes from google_sync/system_seed). The seeded_app fixture only
    seeds the admin's membership, so we must add ``user_id`` to Everyone here
    before the Everyone→collection grant has any effect.
    """
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_group_members import UserGroupMembersRepository
    from src.repositories.user_groups import UserGroupsRepository

    conn = get_system_db()
    groups = UserGroupsRepository(conn)
    grp = groups.get_by_name("Everyone")
    assert grp, "Everyone group must be seeded"
    members = UserGroupMembersRepository(conn)
    if grp["id"] not in set(members.list_groups_for_user(user_id)):
        members.add_member(user_id, grp["id"], source="system_seed")
    grants = ResourceGrantsRepository(conn)
    if not grants.has_grant([grp["id"]], "collection", corpus_id):
        grants.create(
            group_id=grp["id"],
            resource_type="collection",
            resource_id=corpus_id,
            assigned_by="test",
        )
    conn.close()


class TestCreateCollection:
    def test_admin_creates_collection(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Test Corp", "description": "test corpus"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "id" in body
        assert body["name"] == "Test Corp"
        assert body["id"].startswith("col_")

    def test_any_user_creates_private_upload(self, seeded_app):
        # Uploads are private per-user resources: any authenticated user can
        # create their own (owned by them), not just admins.
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "My Private Upload"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["name"] == "My Private Upload"
        assert body["id"].startswith("col_")

    def test_owner_accesses_own_upload_without_grant(self, seeded_app):
        # The creator can read/manage their own upload with no resource_grant —
        # ownership is access. A different non-member still gets 404/403.
        c = seeded_app["client"]
        created = c.post(
            "/api/collections",
            json={"name": "Owner Only"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        # Owner reads it back without any grant.
        own = c.get(f"/api/collections/{cid}", headers=_auth(seeded_app["analyst_token"]))
        assert own.status_code == 200, own.text
        # It does NOT leak into another user's list (privacy).
        # (admin still sees everything via god-mode; that's covered elsewhere.)

    def test_unauthenticated_create_returns_401(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post("/api/collections", json={"name": "Anon"})
        assert resp.status_code == 401

    def test_slug_collision_returns_409(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/collections",
            json={"name": "Dupe", "slug": "dupe-slug"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.post(
            "/api/collections",
            json={"name": "Dupe Again", "slug": "dupe-slug"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 409

    def test_auto_slug_generated_from_name(self, seeded_app):
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "My Auto Slug Collection"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert "slug" in body
        assert body["slug"]  # non-empty

    def test_whitespace_only_slug_falls_back_to_auto_slug(self, seeded_app):
        # A whitespace-only explicit slug is truthy; it must not survive as an
        # empty slug (unreachable via /library/{slug} + bogus 409 collisions).
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Whitespace Slug", "slug": "   "},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        slug = resp.json()["slug"]
        assert slug.strip()  # non-empty, non-whitespace
        assert slug == "whitespace-slug"

    def test_explicit_slug_normalised_to_url_safe(self, seeded_app):
        # An admin-provided slug with URL-unsafe chars must be normalised so it
        # resolves via /library/{slug} (path params don't consume "/").
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Has Slashes", "slug": "my/collection path"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["slug"] == "my-collection-path"

    def test_auto_slug_no_trailing_hyphen_after_truncation(self):
        # The [:100] cap runs after strip("-"); a name whose 100th char lands on
        # a word boundary would otherwise leave a trailing hyphen.
        from app.api.collections import _auto_slug

        slug = _auto_slug("a" * 99 + " " + "b" * 50)
        assert len(slug) <= 100
        assert not slug.endswith("-")
        assert slug == "a" * 99


class TestListCollections:
    def test_list_enumerates_corpora_once(self, seeded_app, monkeypatch):
        """Regression: N+1 collapse — the handler must enumerate the corpora
        exactly once per request (previously once inside
        ``_accessible_corpus_ids`` and once more in the handler).

        Counts *both* enumerating methods rather than one by name: the handler
        reads ``list_all()`` (``list()`` defaults to ``limit=200``, which
        truncated the listing), and a future second enumeration through either
        method is the regression this guards.
        """
        import app.api.collections as collections_mod
        from src.repositories import file_corpora_repo as real_file_corpora_repo

        calls = {"n": 0}
        real_repo = real_file_corpora_repo()
        real_list, real_list_all = real_repo.list, real_repo.list_all

        def counting_list(**kwargs):
            calls["n"] += 1
            return real_list(**kwargs)

        def counting_list_all():
            calls["n"] += 1
            return real_list_all()

        monkeypatch.setattr(real_repo, "list", counting_list)
        monkeypatch.setattr(real_repo, "list_all", counting_list_all)
        monkeypatch.setattr(collections_mod, "file_corpora_repo", lambda: real_repo)

        c = seeded_app["client"]
        resp = c.get("/api/collections", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        assert calls["n"] == 1, f"expected the corpora enumerated once, got {calls['n']}"

    def test_admin_sees_all_collections(self, seeded_app):
        c = seeded_app["client"]
        c.post(
            "/api/collections",
            json={"name": "Visible Col"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.get("/api/collections", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200
        ids = [col["id"] for col in resp.json()["items"]]
        assert len(ids) >= 1

    def test_non_member_analyst_sees_empty_list(self, seeded_app):
        """Analyst with no grants sees zero collections (fail-closed)."""
        c = seeded_app["client"]
        c.post(
            "/api/collections",
            json={"name": "Hidden"},
            headers=_auth(seeded_app["admin_token"]),
        )
        resp = c.get("/api/collections", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        # analyst1 has no grant — list must be empty (RBAC-filtered)
        assert resp.json()["items"] == []

    def test_granted_member_sees_collection(self, seeded_app):
        c = seeded_app["client"]
        create_resp = c.post(
            "/api/collections",
            json={"name": "Granted Col"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = create_resp.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        resp = c.get("/api/collections", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200
        ids = [col["id"] for col in resp.json()["items"]]
        assert corpus_id in ids

    def test_unauthenticated_list_returns_401(self, seeded_app):
        resp = seeded_app["client"].get("/api/collections")
        assert resp.status_code == 401


class TestGetCollection:
    def test_admin_gets_collection_detail(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Detail Test"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == corpus_id
        assert "files" in body

    def test_non_member_gets_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Members Only"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_granted_member_gets_detail(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Member Detail"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200

    def test_missing_collection_returns_404(self, seeded_app):
        resp = seeded_app["client"].get(
            "/api/collections/col_doesnotexist",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 404


class TestDeleteCollection:
    def test_admin_soft_deletes(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "To Delete"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        del_resp = c.delete(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert del_resp.status_code == 204
        # Subsequent GET returns 404
        get_resp = c.get(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["admin_token"]),
        )
        assert get_resp.status_code == 404

    def test_non_admin_delete_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Protected"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.delete(
            f"/api/collections/{corpus_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestFileUpload:
    def _create_and_grant(self, seeded_app, name: str = "Upload Target"):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")
        return corpus_id

    def test_member_uploads_tier1_file(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Tier1 Upload")

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("notes.txt", io.BytesIO(b"hello world"), "text/plain")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        results = resp.json()
        assert len(results) == 1
        assert results[0]["processing_status"] == "pending"
        assert results[0]["filename"] == "notes.txt"
        assert "file_id" in results[0]

    def test_upload_triggers_background_ingestion(self, seeded_app):
        """A tabular upload kicks off ingestion; a follow-up GET shows it
        indexed (TestClient runs BackgroundTasks before the POST returns)."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Ingest Trigger")
        up = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("metrics.csv", io.BytesIO(b"a,b\n1,2\n3,4\n"), "text/csv")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert up.status_code == 201, up.text
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        files = listing.json()["files"]
        assert files[0]["processing_status"] == "indexed"
        assert files[0]["processing_detail"]["kind"] == "tabular"

    def test_member_uploads_unsupported_type_returns_422_rejected(self, seeded_app):
        """DWG file → 422 response but file row persisted with status='rejected'."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Reject Upload")

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("blueprint.dwg", io.BytesIO(b"binary data"), "application/octet-stream")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 422, resp.text
        results = resp.json()
        assert len(results) == 1
        assert results[0]["processing_status"] == "rejected"
        assert results[0]["filename"] == "blueprint.dwg"

    def test_non_member_upload_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "No Access"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        # analyst1 has NO grant on this collection
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("doc.pdf", io.BytesIO(b"pdf bytes"), "application/pdf")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403

    def test_unauthenticated_upload_returns_401(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "Anon Upload"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("doc.txt", io.BytesIO(b"data"), "text/plain")},
        )
        assert resp.status_code == 401

    def test_mixed_upload_returns_422_with_all_results(self, seeded_app):
        """One valid + one rejected file in a single multipart request."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Mixed Upload")

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("valid.pdf", io.BytesIO(b"pdf content"), "application/pdf")),
                ("files", ("bad.exe", io.BytesIO(b"exe bytes"), "application/octet-stream")),
            ],
            headers=_auth(seeded_app["analyst_token"]),
        )
        # Any rejected file → 422 for the whole request
        assert resp.status_code == 422
        results = resp.json()
        assert len(results) == 2
        statuses = {r["filename"]: r["processing_status"] for r in results}
        assert statuses["valid.pdf"] == "pending"
        assert statuses["bad.exe"] == "rejected"


class TestListFiles:
    def test_member_lists_uploaded_files(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "List Files"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        # Upload a file first
        c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("data.csv", io.BytesIO(b"a,b\n1,2"), "text/csv")},
            headers=_auth(seeded_app["analyst_token"]),
        )

        resp = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200
        files = resp.json()["files"]
        assert len(files) >= 1
        assert any(f["filename"] == "data.csv" for f in files)

    def test_non_member_list_files_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "File List Guard"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


class TestDeleteFile:
    def test_member_deletes_file(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "File Del"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")

        upload_resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("to_del.txt", io.BytesIO(b"bye"), "text/plain")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        file_id = upload_resp.json()[0]["file_id"]

        del_resp = c.delete(
            f"/api/collections/{corpus_id}/files/{file_id}",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert del_resp.status_code == 204

    def test_non_member_file_delete_returns_403(self, seeded_app):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": "File Del Guard"},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        resp = c.delete(
            f"/api/collections/{corpus_id}/files/cf_fakeid",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 403


def test_list_collections_session_principal_filters_without_crash(seeded_app):
    """Regression: a co-session ``SessionPrincipal`` caller must not crash on
    ``user['id']`` (it is not subscriptable) and must be RBAC-filtered to its
    intersection — not see every collection.
    """
    import asyncio

    from app.api.collections import list_collections
    from app.auth.session_principal import SessionPrincipal
    from src.repositories import file_corpora_repo

    repo = file_corpora_repo()
    granted = repo.create(name="SP Granted", slug="sp-granted", description=None, created_by="admin1")
    other = repo.create(name="SP Other", slug="sp-other", description=None, created_by="admin1")

    principal = SessionPrincipal(
        "chat_sp",
        ["analyst1"],
        ["analyst@test.com"],
        {"collection": frozenset({granted})},
    )
    result = asyncio.run(list_collections(user=principal))
    ids = {c["id"] for c in result["items"]}
    assert granted in ids
    assert other not in ids


class TestSearch:
    def _seed_corpus_with_chunk(self, seeded_app, name, text, *, grant):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        if grant:
            _seed_collection_grant(corpus_id, "analyst1")
        from src.repositories import corpus_chunks_repo, corpus_files_repo

        fid = corpus_files_repo().add(
            corpus_id=corpus_id,
            filename="d.txt",
            sha256="s",
            file_type="txt",
            size_bytes=1,
            storage_path="/x",
        )
        corpus_chunks_repo().add_many([{"corpus_id": corpus_id, "file_id": fid, "ordinal": 0, "text": text}])
        return corpus_id

    def test_member_searches_accessible_collection(self, seeded_app):
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "Searchable", "the magic keyword appears here", grant=True)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert any("magic" in (r.get("text") or "") for r in results)
        assert results[0]["filename"] == "d.txt"

    def test_search_results_carry_confidence(self, seeded_app):
        """#756: the calibrated confidence label from retrieval.search()
        must pass through the API response unchanged."""
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "Confident", "the magic keyword appears here", grant=True)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        results = resp.json()["results"]
        assert results
        assert results[0]["confidence"] in ("high", "medium", "low")

    def test_search_response_labels_lexical_only_retrieval(self, seeded_app, monkeypatch):
        """#898: without the embeddings extra the ranking silently degrades to
        lexical-only — the response must say so instead of leaving clients to
        read server logs."""
        import src.ingest.retrieval as retrieval

        monkeypatch.setattr(retrieval, "embedding_capability", lambda: False)
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "Degraded", "the magic keyword appears here", grant=True)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["retrieval"] == "lexical_only"

    def test_search_response_labels_hybrid_retrieval(self, seeded_app, monkeypatch):
        """#898: with an embedding model available the response labels the
        ranking as hybrid."""
        import src.ingest.retrieval as retrieval

        monkeypatch.setattr(retrieval, "embedding_capability", lambda: True)
        # Keep ranking deterministic without a real model — the label reflects
        # capability; the blend handles a None query vector as lexical scores.
        monkeypatch.setattr(retrieval, "embed_query", lambda _q: None)
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "Hybrid", "the magic keyword appears here", grant=True)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["retrieval"] == "hybrid"

    def test_search_fail_closed_excludes_ungranted(self, seeded_app):
        c = seeded_app["client"]
        # Collection is NOT granted to analyst1.
        self._seed_corpus_with_chunk(seeded_app, "Private", "the magic keyword appears here", grant=False)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 200, resp.text
        # Fail-closed: an analyst with no grant sees nothing from it.
        assert resp.json()["results"] == []

    def test_admin_search_sees_all(self, seeded_app):
        c = seeded_app["client"]
        self._seed_corpus_with_chunk(seeded_app, "AdminSee", "the magic keyword appears here", grant=False)
        resp = c.get(
            "/api/collections/search",
            params={"q": "magic keyword"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 200
        assert any("magic" in (r.get("text") or "") for r in resp.json()["results"])


def test_delete_file_removes_its_chunks(seeded_app):
    """Regression: deleting a file must also remove its corpus_chunks, so they
    don't linger in search results with a null filename."""
    from src.repositories import corpus_chunks_repo, corpus_files_repo

    c = seeded_app["client"]
    cid = c.post("/api/collections", json={"name": "Del Chunks"}, headers=_auth(seeded_app["admin_token"])).json()["id"]
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename="d.txt",
        sha256="s",
        file_type="txt",
        size_bytes=1,
        storage_path=None,
    )
    corpus_chunks_repo().add_many([{"corpus_id": cid, "file_id": fid, "ordinal": 0, "text": "hello world"}])
    assert len(corpus_chunks_repo().list_for_file(fid)) == 1

    r = c.delete(f"/api/collections/{cid}/files/{fid}", headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 204, r.text
    assert corpus_chunks_repo().list_for_file(fid) == []


def test_create_collection_non_alphanumeric_name_gets_fallback_slug(seeded_app):
    """A name with no alphanumerics must not yield an empty slug."""
    c = seeded_app["client"]
    r = c.post("/api/collections", json={"name": "!!!"}, headers=_auth(seeded_app["admin_token"]))
    assert r.status_code == 201, r.text
    assert r.json()["slug"]  # non-empty (falls back to "collection")


def test_delete_tabular_file_purges_table_registry_row(seeded_app):
    """Deleting a tabular file must remove its derived table_registry row so
    it no longer appears in agnes catalog."""
    import io

    from src.repositories import table_registry_repo

    c = seeded_app["client"]
    cr = c.post(
        "/api/collections",
        json={"name": "Tabular Purge"},
        headers=_auth(seeded_app["admin_token"]),
    )
    corpus_id = cr.json()["id"]
    _seed_collection_grant(corpus_id, "analyst1")

    up = c.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("data.csv", io.BytesIO(b"x,y\n1,2\n3,4\n"), "text/csv")},
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert up.status_code == 201, up.text
    file_id = up.json()[0]["file_id"]

    # After ingestion the table_registry must contain a derived row for this corpus.
    rows_before = table_registry_repo().list_by_source("collection")
    corpus_rows_before = [r for r in rows_before if r.get("bucket") == corpus_id]
    assert len(corpus_rows_before) == 1, "Expected one derived table_registry row after tabular ingest"

    # Delete the file — must cascade to the derived table_registry row.
    del_resp = c.delete(
        f"/api/collections/{corpus_id}/files/{file_id}",
        headers=_auth(seeded_app["analyst_token"]),
    )
    assert del_resp.status_code == 204, del_resp.text

    rows_after = table_registry_repo().list_by_source("collection")
    corpus_rows_after = [r for r in rows_after if r.get("bucket") == corpus_id]
    assert corpus_rows_after == [], "Derived table_registry row must be purged on file delete"


def test_delete_collection_purges_all_derived_table_registry_rows(seeded_app):
    """Soft-deleting a collection must also purge all derived table_registry
    rows so the tables no longer appear in agnes catalog."""
    import io

    from src.repositories import table_registry_repo

    c = seeded_app["client"]
    cr = c.post(
        "/api/collections",
        json={"name": "Collection Cascade Purge"},
        headers=_auth(seeded_app["admin_token"]),
    )
    corpus_id = cr.json()["id"]
    _seed_collection_grant(corpus_id, "analyst1")

    # Upload two tabular files so we get two derived registry rows.
    for name, content in [("a.csv", b"a,b\n1,2"), ("b.csv", b"c,d\n3,4")]:
        up = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": (name, io.BytesIO(content), "text/csv")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert up.status_code == 201, up.text

    rows_before = [r for r in table_registry_repo().list_by_source("collection") if r.get("bucket") == corpus_id]
    assert len(rows_before) == 2, f"Expected 2 derived rows, got {len(rows_before)}"

    # Delete the collection — cascade must purge all derived rows.
    del_resp = c.delete(
        f"/api/collections/{corpus_id}",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert del_resp.status_code == 204, del_resp.text

    rows_after = [r for r in table_registry_repo().list_by_source("collection") if r.get("bucket") == corpus_id]
    assert rows_after == [], "All derived table_registry rows must be purged on collection delete"


def test_reingest_resets_status_and_reruns(seeded_app, tmp_path):
    """needs_review file + fixed content -> reingest -> indexed."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ri", slug="ri", description=None, created_by="u1")
    csv = tmp_path / "d.csv"
    csv.write_text("a,b\n", encoding="utf-8")  # header-only -> needs_review
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="d.csv",
        sha256="s",
        file_type="csv",
        size_bytes=csv.stat().st_size,
        storage_path=str(csv),
    )
    from src.ingest.runner import ingest_file

    assert ingest_file(fid) == "needs_review"

    csv.write_text("a,b\n1,2\n", encoding="utf-8")  # operator fixes the file
    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 202, r.text
    assert r.json()["processing_status"] == "pending"

    # TestClient runs BackgroundTasks synchronously after the response — by now ingest re-ran.
    assert corpus_files_repo().get(fid)["processing_status"] == "indexed"


def test_reingest_404_on_missing_file(seeded_app):
    from src.repositories import file_corpora_repo

    col_a = file_corpora_repo().create(name="ria", slug="ria", description=None, created_by="u1")
    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_a}/files/cf_nonexistent/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 404


def test_reingest_404_when_file_belongs_to_other_collection(seeded_app):
    """A file that exists but belongs to a different collection must 404,
    not be re-ingested through the wrong collection's endpoint."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_a = file_corpora_repo().create(name="ria2", slug="ria2", description=None, created_by="u1")
    col_b = file_corpora_repo().create(name="rib2", slug="rib2", description=None, created_by="u1")
    fid = corpus_files_repo().add(
        corpus_id=col_a,
        filename="x.csv",
        sha256="s",
        file_type="csv",
        size_bytes=1,
        storage_path=None,
    )
    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_b}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 404


def test_reingest_409_while_run_in_flight(seeded_app):
    """A file already in 'processing' must reject reingest with 409 and keep
    its status untouched (no purge/reset) — guards against duplicate racing
    ingest runs from a second admin tab or a direct API caller."""
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ric", slug="ric", description=None, created_by="u1")
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="busy.csv",
        sha256="s",
        file_type="csv",
        size_bytes=1,
        storage_path=None,
    )
    corpus_files_repo().set_status(fid, status="processing", detail={"reason": "ingest running"})

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "reingest_in_progress"

    row = corpus_files_repo().get(fid)
    assert row["processing_status"] == "processing"  # no reset happened
    assert row["processing_detail"] == {"reason": "ingest running"}  # detail untouched


def test_reingest_stale_processing_is_recoverable(seeded_app, tmp_path):
    """A 'processing' row whose updated_at predates the staleness threshold
    must be treated as crash-abandoned, not in-flight, so reingest proceeds
    (202) instead of 409 — otherwise a crash mid-ingest would permanently
    block the only recovery path for the stuck row."""
    from datetime import datetime, timedelta, timezone

    from app.api.collections import REINGEST_STALE_PROCESSING_MINUTES
    from src.repositories import corpus_files_repo, file_corpora_repo

    col_id = file_corpora_repo().create(name="ris", slug="ris", description=None, created_by="u1")
    csv = tmp_path / "s.csv"
    csv.write_text("a,b\n1,2\n", encoding="utf-8")
    fid = corpus_files_repo().add(
        corpus_id=col_id,
        filename="s.csv",
        sha256="s",
        file_type="csv",
        size_bytes=csv.stat().st_size,
        storage_path=str(csv),
    )
    corpus_files_repo().set_status(fid, status="processing", detail={"reason": "ingest running"})

    # Backdate updated_at past the threshold — simulates a crash mid-ingest,
    # where the row never got a chance to move past 'processing'.
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=REINGEST_STALE_PROCESSING_MINUTES + 5)
    conn = get_system_db()
    conn.execute(
        "UPDATE corpus_files SET updated_at = ? WHERE id = ?",
        [stale_at.replace(tzinfo=None), fid],
    )
    conn.close()

    c = seeded_app["client"]
    r = c.post(
        f"/api/collections/{col_id}/files/{fid}/reingest",
        headers=_auth(seeded_app["admin_token"]),
    )
    assert r.status_code == 202, r.text
    assert r.json()["processing_status"] == "pending"

    # TestClient runs BackgroundTasks synchronously after the response — by now ingest re-ran.
    assert corpus_files_repo().get(fid)["processing_status"] == "indexed"


class TestBundleUpload:
    """K1 — zip upload unpacks into ingested child rows."""

    def _create_and_grant(self, seeded_app, name: str = "Bundle Target"):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")
        return corpus_id

    def test_upload_zip_bundle_end_to_end(self, seeded_app):
        import zipfile

        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("notes.md", "# Notes\n\nBundle ingestion works end to end.")
            zf.writestr("junk.dwg", "binary")
        buf.seek(0)

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("dump.zip", buf, "application/zip")},
            headers=_auth(seeded_app["analyst_token"]),
        )
        # zip itself accepted; member-level rejection ≠ upload rejection.
        assert resp.status_code == 201, resp.text

        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        by_name = {f["filename"]: f for f in listing.json()["files"]}
        archive = by_name["dump.zip"]
        assert archive["processing_status"] == "indexed"
        assert archive["parent_file_id"] is None
        assert archive["processing_detail"]["kind"] == "bundle"
        assert archive["processing_detail"]["children"] == 2
        assert by_name["notes.md"]["processing_status"] == "indexed"
        assert by_name["notes.md"]["parent_file_id"] == archive["file_id"]
        assert by_name["junk.dwg"]["processing_status"] == "rejected"

        # Bundle content is searchable like any directly-uploaded document.
        hits = c.get(
            "/api/collections/search",
            params={"q": "bundle ingestion works", "corpus_id": corpus_id},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert hits.status_code == 200
        assert any("notes.md" in str(h) for h in hits.json()["results"])


class TestFileUpsert:
    """Upsert-on-upload: `paths` form field gives a file a logical identity so
    re-uploading the same (corpus_id, path) replaces instead of duplicating."""

    def _create_and_grant(self, seeded_app, name: str = "Upsert Target"):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")
        return corpus_id

    def test_reupload_same_path_preserves_row_id(self, seeded_app):
        """fact-graph-over-Collections §6: ANY match through this code path
        preserves the existing row id — content is updated in place, not
        delete+insert — so anything anchored to that id (a future claim)
        survives a re-upload."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Upsert Replace")

        first = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"alpha"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert first.status_code == 201, first.text
        fid1 = first.json()[0]["file_id"]
        assert first.json()[0]["path"] == "docs/a.md"

        second = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"bravo beta gamma"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert second.status_code == 201, second.text
        fid2 = second.json()[0]["file_id"]
        assert fid2 == fid1  # updated in place, id preserved

        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        files = listing.json()["files"]
        # Exactly one row survives for that path — the same row, refreshed.
        assert len(files) == 1
        assert files[0]["file_id"] == fid1
        assert files[0]["path"] == "docs/a.md"
        assert files[0]["size_bytes"] == len(b"bravo beta gamma")

    def test_reupload_same_path_unchanged_content_skips_reprocessing(self, seeded_app):
        """Unchanged sha256 short-circuits: no chunk purge, no status reset —
        a byte-identical re-upload (or a pure rename) leaves the row's
        processing state exactly as it was."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Upsert Short Circuit")

        first = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"same bytes"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert first.status_code == 201, first.text
        fid1 = first.json()[0]["file_id"]

        from src.repositories import corpus_files_repo

        corpus_files_repo().set_status(fid1, status="indexed", detail={"chunk_count": 3})

        second = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"same bytes"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert second.status_code == 201, second.text
        assert second.json()[0]["file_id"] == fid1
        # processing_status untouched — the short-circuit never reset it.
        assert second.json()[0]["processing_status"] == "indexed"
        assert second.json()[0]["processing_detail"]["chunk_count"] == 3

    def test_reupload_same_path_unchanged_content_retries_a_failed_row(self, seeded_app):
        """The unchanged-sha256 short-circuit must not strand a row whose
        ingest never completed. Re-uploading the identical bytes is the
        obvious way a user retries a `rejected` file (an extractor was
        missing, a dependency was installed since), so an unchanged match on
        a NON-`indexed` row still resets to `pending` and re-schedules
        ingestion — only `indexed` means "derived data is present and
        current" (Devin Review on #1655)."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Upsert Failed Retry")

        first = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"retry me"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert first.status_code == 201, first.text
        fid1 = first.json()[0]["file_id"]

        from src.repositories import corpus_files_repo

        # Simulate a prior ingest that failed and left the row unusable.
        corpus_files_repo().set_status(fid1, status="rejected", detail={"reason": "ingest_error: boom"})

        second = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"retry me"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert second.status_code == 201, second.text
        assert second.json()[0]["file_id"] == fid1
        # The response tells the caller a retry was accepted, not that the
        # row is still rejected.
        assert second.json()[0]["processing_status"] == "pending"
        # ...and the ingest really re-ran (TestClient drains BackgroundTasks).
        row = corpus_files_repo().get(fid1)
        assert row["processing_status"] != "rejected"

    def test_reupload_same_path_unchanged_content_retries_a_pending_row(self, seeded_app):
        """Same rule for a row the runner deliberately parked in `pending`
        (a tier-2 image left "awaiting vision (no model/key)"): once the
        model is configured, re-uploading the same bytes must pick it up."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Upsert Pending Retry")

        first = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"park me"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert first.status_code == 201, first.text
        fid1 = first.json()[0]["file_id"]

        from src.repositories import corpus_files_repo

        corpus_files_repo().set_status(fid1, status="pending", detail={"note": "awaiting vision (no model/key)"})

        called: list[str] = []
        import src.ingest.runner as _runner

        real_ingest = _runner.ingest_file

        def _spy(file_id):
            called.append(file_id)
            return real_ingest(file_id)

        _runner.ingest_file = _spy
        try:
            second = c.post(
                f"/api/collections/{corpus_id}/files",
                files={"files": ("a.md", io.BytesIO(b"park me"), "text/markdown")},
                data={"paths": "docs/a.md"},
                headers=_auth(seeded_app["analyst_token"]),
            )
        finally:
            _runner.ingest_file = real_ingest
        assert second.status_code == 201, second.text
        assert second.json()[0]["file_id"] == fid1
        assert called == [fid1], "unchanged re-upload of a non-indexed row must re-schedule ingest"

    def test_uploads_without_path_do_not_upsert(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "No Path No Upsert")
        for _ in range(2):
            r = c.post(
                f"/api/collections/{corpus_id}/files",
                files={"files": ("same.md", io.BytesIO(b"x"), "text/markdown")},
                headers=_auth(seeded_app["analyst_token"]),
            )
            assert r.status_code == 201, r.text
            assert r.json()[0]["path"] is None
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # Legacy behavior: two rows, no replacement.
        assert len(listing.json()["files"]) == 2

    def test_distinct_paths_coexist(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Distinct Paths")
        for p in ("apis/x.md", "concepts/x.md"):
            r = c.post(
                f"/api/collections/{corpus_id}/files",
                files={"files": ("x.md", io.BytesIO(b"data"), "text/markdown")},
                data={"paths": p},
                headers=_auth(seeded_app["analyst_token"]),
            )
            assert r.status_code == 201, r.text
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        # Same basename, different logical path → both kept (no collision).
        paths = {f["path"] for f in listing.json()["files"]}
        assert paths == {"apis/x.md", "concepts/x.md"}

    def test_reupload_bundle_same_path_purges_old_children(self, seeded_app):
        """Re-uploading a zip at the same path must not orphan the previous
        archive's extracted member rows."""
        import zipfile

        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Bundle Upsert")

        def _zip(members: dict[str, str]) -> io.BytesIO:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for name, body in members.items():
                    zf.writestr(name, body)
            buf.seek(0)
            return buf

        first = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("dump.zip", _zip({"a.md": "# A", "b.md": "# B"}), "application/zip")},
            data={"paths": "bundles/dump.zip"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert first.status_code == 201, first.text
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()["files"]
        # archive + 2 members
        assert len(listing) == 3

        # Re-upload a different archive (one member) at the same logical path.
        second = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("dump.zip", _zip({"c.md": "# C"}), "application/zip")},
            data={"paths": "bundles/dump.zip"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert second.status_code == 201, second.text

        files = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        ).json()["files"]
        # Old archive + its 2 members purged; new archive + 1 member remain.
        assert len(files) == 2, [f["filename"] for f in files]
        by_name = {f["filename"]: f for f in files}
        assert set(by_name) == {"dump.zip", "c.md"}
        # No orphaned member points at a vanished archive.
        ids = {f["file_id"] for f in files}
        for f in files:
            if f["parent_file_id"] is not None:
                assert f["parent_file_id"] in ids

    def test_replace_keeps_blob_shared_with_another_file(self, seeded_app):
        """A content-addressed blob shared by two files (different paths, same
        bytes) survives when one of them is replaced."""
        import os

        from src.repositories import corpus_files_repo

        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Shared Blob")
        shared = b"identical shared bytes"

        for p in ("a.md", "b.md"):
            r = c.post(
                f"/api/collections/{corpus_id}/files",
                files={"files": (p, io.BytesIO(shared), "text/markdown")},
                data={"paths": p},
                headers=_auth(seeded_app["analyst_token"]),
            )
            assert r.status_code == 201, r.text

        row_b = corpus_files_repo().get_by_path(corpus_id, "b.md")
        blob_b = row_b["storage_path"]
        assert blob_b and os.path.exists(blob_b)

        # Replace a.md with different content — must not wipe the shared blob.
        r = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"now different"), "text/markdown")},
            data={"paths": "a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert r.status_code == 201, r.text

        # b.md still points at the (still-present) shared blob.
        assert os.path.exists(blob_b)
        assert corpus_files_repo().get_by_path(corpus_id, "b.md")["storage_path"] == blob_b

    def test_paths_length_mismatch_rejected(self, seeded_app):
        """`paths` must pair 1:1 with `files`; a misaligned list is a 400."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Paths Mismatch")
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("a.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("b.md", io.BytesIO(b"b"), "text/markdown")),
            ],
            data={"paths": "docs/only-one.md"},  # 1 path for 2 files
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "paths_length_mismatch" in resp.text
        # Nothing was created.
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert listing.json()["files"] == []

    def test_duplicate_path_in_same_batch_rejected(self, seeded_app):
        """Two files in one request sharing a path would have the second
        purge the first's already-queued row mid-request — reject up front
        instead of silently dropping a file (Devin Review on #1004)."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Duplicate Path Batch")
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("a.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("b.md", io.BytesIO(b"b"), "text/markdown")),
            ],
            data={"paths": ["docs/same.md", "docs/same.md"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "duplicate_path_in_batch" in resp.text
        # Nothing was created.
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert listing.json()["files"] == []


class TestSourceAnchoredUpsert:
    """`source_stable_ids` (+ optional `source_doc_ids`, `source_sha256s`,
    `document_dates`) — the crawler-anchor upsert prerequisite for the
    fact-graph-over-Collections design (§6). The mapping table is
    Postgres-only; these tests exercise the DuckDB-backed default app, where
    every one of these fields is either omitted (byte-identical to today) or
    triggers the typed 501 before any file is touched. End-to-end PG-backed
    behavior (stable-id matching, mapping upserts) lives in
    tests/db_pg/test_collections_upsert_pg.py."""

    def _create_and_grant(self, seeded_app, name: str = "Source Upsert Target"):
        c = seeded_app["client"]
        cr = c.post(
            "/api/collections",
            json={"name": name},
            headers=_auth(seeded_app["admin_token"]),
        )
        corpus_id = cr.json()["id"]
        _seed_collection_grant(corpus_id, "analyst1")
        return corpus_id

    def test_source_stable_ids_length_mismatch_rejected(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("a.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("b.md", io.BytesIO(b"b"), "text/markdown")),
            ],
            data={"source_stable_ids": "graph:only-one"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "source_stable_ids_length_mismatch" in resp.text

    def test_source_doc_ids_length_mismatch_rejected(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("a.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("b.md", io.BytesIO(b"b"), "text/markdown")),
            ],
            data={
                "source_stable_ids": ["graph:a", "graph:b"],
                "source_doc_ids": ["doc-a"],
            },
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "source_doc_ids_length_mismatch" in resp.text

    def test_source_sha256s_length_mismatch_rejected(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[("files", ("a.md", io.BytesIO(b"a"), "text/markdown"))],
            data={"source_sha256s": ["sha-a", "sha-b"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "source_sha256s_length_mismatch" in resp.text

    def test_document_dates_length_mismatch_rejected(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[("files", ("a.md", io.BytesIO(b"a"), "text/markdown"))],
            data={"document_dates": ["2026-01-01", "2026-01-02"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "document_dates_length_mismatch" in resp.text

    def test_source_stable_ids_on_duckdb_backend_yields_typed_501(self, seeded_app):
        """The mapping table is PG-only — supplying `source_stable_ids` on a
        DuckDB-backed instance must fail clean (typed 501), before any file
        is written, never a raw 500 or a silent partial upload."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"content"), "text/markdown")},
            data={"source_stable_ids": "graph:abc123"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 501, resp.text
        body = resp.json()
        assert body["error"] == "requires_postgres_backend"

        # Nothing was created — the 501 fired before any file was touched.
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert listing.json()["files"] == []

    def test_duplicate_source_stable_id_in_same_batch_rejected(self, seeded_app):
        """Two files in one request sharing a `source_stable_id` would have
        the second resolve to — and update in place — the first's row: the
        first file's bytes are lost and both response entries carry the same
        `file_id`. Same failure mode as `duplicate_path_in_batch`, so the
        same up-front 400 (Devin Review on #1655).

        Runs on the DuckDB-backed app on purpose: the guard is request
        validation and fires BEFORE the PG-only mapping repo is resolved, so
        a malformed batch is rejected identically on either backend.
        """
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Duplicate Stable Id Batch")
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("a.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("b.md", io.BytesIO(b"b"), "text/markdown")),
            ],
            data={"source_stable_ids": ["graph:same", "graph:same"]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 400, resp.text
        assert "duplicate_source_stable_id_in_batch" in resp.text
        # Nothing was created — the guard fires before any file is stored.
        listing = c.get(
            f"/api/collections/{corpus_id}/files",
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert listing.json()["files"] == []

    def test_distinct_and_blank_source_stable_ids_are_not_duplicates(self, seeded_app):
        """Only NON-BLANK stable ids collide — several files may legitimately
        carry no stable id at all in the same batch (mirrors the `paths`
        guard, which ignores blanks). Reaching the PG-only 501 proves the
        duplicate guard did not fire."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app, "Blank Stable Ids Batch")
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files=[
                ("files", ("a.md", io.BytesIO(b"a"), "text/markdown")),
                ("files", ("b.md", io.BytesIO(b"b"), "text/markdown")),
                ("files", ("c.md", io.BytesIO(b"c"), "text/markdown")),
            ],
            data={"source_stable_ids": ["graph:a", "", "  "]},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 501, resp.text
        assert resp.json()["error"] == "requires_postgres_backend"

    def test_omitting_source_stable_ids_is_byte_identical_to_today(self, seeded_app):
        """Omitting the field entirely never resolves the PG-only repo — the
        plain `paths` upsert flow keeps working on a DuckDB-backed instance."""
        c = seeded_app["client"]
        corpus_id = self._create_and_grant(seeded_app)
        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("a.md", io.BytesIO(b"content"), "text/markdown")},
            data={"paths": "docs/a.md"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()[0]["path"] == "docs/a.md"


# ---------------------------------------------------------------------------
# Preview — GET …/files/{file_id}/preview  +  …/raw
# ---------------------------------------------------------------------------


class TestFilePreview:
    """The Library's file-preview contract.

    One JSON endpoint tells the client what to show (`kind`), and a separate
    raw endpoint streams only the formats a browser can safely draw itself.
    The split is the security boundary: uploads accept `.html`, so what is
    streamed inline can never be decided by the uploader.
    """

    def _collection(self, seeded_app, name: str) -> str:
        r = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def _upload(self, seeded_app, cid: str, filename: str, body: bytes, ctype: str) -> str:
        r = seeded_app["client"].post(
            f"/api/collections/{cid}/files",
            files={"files": (filename, io.BytesIO(body), ctype)},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code in (200, 201, 422), r.text
        return r.json()[0]["file_id"]

    def test_textual_file_previews_its_own_bytes(self, seeded_app):
        cid = self._collection(seeded_app, "Preview Text")
        fid = self._upload(seeded_app, cid, "notes.md", b"# Title\n\nbody text", "text/markdown")

        r = seeded_app["client"].get(
            f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["kind"] == "text"
        assert body["source"] == "file"
        assert "# Title" in body["text"]
        assert body["truncated"] is False
        assert body["filename"] == "notes.md"

    def test_textual_row_with_no_blob_explains_itself_instead_of_erroring(self, seeded_app):
        """A textual row can legitimately have no bytes on disk, and the modal
        must still say why rather than fail.

        An oversize or empty upload is recorded `rejected` with
        `storage_path=None` but keeps the extension derived from its filename —
        so the row looks textual while having nothing to read. Resolving the
        blob fatally here returned 404 `file_blob_missing`, which the modal
        renders as its generic "The preview could not be loaded.", burying the
        one useful answer: that ingestion rejected the file. Non-textual
        formats already degraded correctly; this makes the textual branch
        behave the same.
        """
        from pathlib import Path

        from src.repositories import corpus_files_repo

        cid = self._collection(seeded_app, "Preview No Blob")
        fid = self._upload(seeded_app, cid, "notes.md", b"real bytes", "text/markdown")
        # A stored path can outlive its bytes; the row keeps its textual
        # extension either way, which is the state under test.
        stored = corpus_files_repo().get(fid).get("storage_path")
        assert stored, "upload should have stored a blob for this fixture to remove"
        Path(stored).unlink()

        r = seeded_app["client"].get(
            f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"])
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["filename"] == "notes.md"
        # Whichever graceful shape applies — never the 404 that the modal turns
        # into "The preview could not be loaded."
        assert body["kind"] in {"text", "none"}, body
        # Crucially not `source: "file"`: there is no file left to have read.
        assert body["source"] != "file", body
        if body["kind"] == "text":
            # Already ingested, so the extracted text is the better answer than
            # a status sentence — that is the point of falling through.
            assert body["source"] == "extracted"
            assert body["text"]
        else:
            assert body["reason"], "a kind='none' preview must say why"

    def test_long_text_is_truncated_not_streamed_whole(self, seeded_app):
        """A preview is a glance: a big file comes back capped, and says so."""
        from app.api.collections import _PREVIEW_MAX_CHARS

        cid = self._collection(seeded_app, "Preview Long")
        fid = self._upload(seeded_app, cid, "big.txt", b"x" * (_PREVIEW_MAX_CHARS + 5000), "text/plain")

        body = (
            seeded_app["client"]
            .get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"]))
            .json()
        )
        assert body["kind"] == "text"
        assert body["truncated"] is True
        assert len(body["text"]) == _PREVIEW_MAX_CHARS

    def test_image_previews_through_the_raw_endpoint(self, seeded_app):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        cid = self._collection(seeded_app, "Preview Image")
        fid = self._upload(seeded_app, cid, "shot.png", png, "image/png")

        c = seeded_app["client"]
        body = c.get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"])).json()
        assert body["kind"] == "image"
        assert body["raw_url"] == f"/api/collections/{cid}/files/{fid}/raw"

        raw = c.get(body["raw_url"], headers=_auth(seeded_app["admin_token"]))
        assert raw.status_code == 200, raw.text
        assert raw.headers["content-type"] == "image/png"
        assert raw.headers["content-disposition"].startswith("inline")
        assert raw.headers["x-content-type-options"] == "nosniff"
        assert raw.content == png

    def test_pdf_previews_inline(self, seeded_app):
        cid = self._collection(seeded_app, "Preview Pdf")
        fid = self._upload(seeded_app, cid, "deck.pdf", b"%PDF-1.4 body", "application/pdf")

        c = seeded_app["client"]
        body = c.get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"])).json()
        assert body["kind"] == "pdf"
        raw = c.get(body["raw_url"], headers=_auth(seeded_app["admin_token"]))
        assert raw.headers["content-type"] == "application/pdf"
        # The modal draws a PDF in a same-origin iframe, so this one response
        # must narrow the app-wide DENY / frame-ancestors 'none' defaults to
        # SELF — otherwise the viewer is blocked by our own security headers.
        assert raw.headers["x-frame-options"] == "SAMEORIGIN"
        assert "frame-ancestors 'self'" in raw.headers["content-security-policy"]
        assert raw.headers["x-content-type-options"] == "nosniff"

    def test_uploaded_html_is_never_streamed_inline(self, seeded_app):
        """The XSS boundary: an uploaded .html previews as SOURCE TEXT, and the
        raw endpoint refuses it outright — serving it inline from our origin
        would run the uploader's script against every viewer."""
        cid = self._collection(seeded_app, "Preview Html")
        fid = self._upload(seeded_app, cid, "evil.html", b"<script>alert(document.cookie)</script>", "text/html")

        c = seeded_app["client"]
        body = c.get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"])).json()
        assert body["kind"] == "text"  # shown as source in a <pre>, never rendered
        assert body["raw_url"] is None
        assert "<script>" in body["text"]

        raw = c.get(f"/api/collections/{cid}/files/{fid}/raw", headers=_auth(seeded_app["admin_token"]))
        assert raw.status_code == 415, raw.text
        assert "/preview" in raw.text  # points at what to call instead

    def test_binary_format_previews_its_extracted_text(self, seeded_app):
        """A .docx has no readable bytes — its preview is the text ingestion
        already extracted, labelled as such."""
        from src.repositories import corpus_chunks_repo, corpus_files_repo

        cid = self._collection(seeded_app, "Preview Extracted")
        fid = corpus_files_repo().add(
            corpus_id=cid,
            filename="report.docx",
            sha256="s",
            file_type="docx",
            size_bytes=10,
            storage_path=None,
        )
        corpus_chunks_repo().add_many(
            [{"corpus_id": cid, "file_id": fid, "ordinal": 0, "text": "quarterly revenue grew"}]
        )

        body = (
            seeded_app["client"]
            .get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"]))
            .json()
        )
        assert body["kind"] == "text"
        assert body["source"] == "extracted"
        assert "quarterly revenue grew" in body["text"]

    def test_unpreviewable_file_says_why(self, seeded_app):
        """No bytes we can draw and no extracted text yet → an explicit reason,
        in the words the modal shows, not an empty box."""
        from src.repositories import corpus_files_repo

        cid = self._collection(seeded_app, "Preview None")
        fid = corpus_files_repo().add(
            corpus_id=cid,
            filename="archive.zip",
            sha256="s",
            file_type="zip",
            size_bytes=10,
            storage_path=None,
        )

        body = (
            seeded_app["client"]
            .get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["admin_token"]))
            .json()
        )
        assert body["kind"] == "none"
        assert body["reason"]
        assert "indexed" in body["reason"]

    def test_preview_404s_for_wrong_collection_and_unknown_file(self, seeded_app):
        cid = self._collection(seeded_app, "Preview Guard A")
        other = self._collection(seeded_app, "Preview Guard B")
        fid = self._upload(seeded_app, cid, "n.txt", b"hi", "text/plain")

        c = seeded_app["client"]
        tok = _auth(seeded_app["admin_token"])
        assert c.get(f"/api/collections/{other}/files/{fid}/preview", headers=tok).status_code == 404
        assert c.get(f"/api/collections/{cid}/files/cf_nope/preview", headers=tok).status_code == 404
        assert c.get(f"/api/collections/{other}/files/{fid}/raw", headers=tok).status_code == 404

    def test_preview_404s_without_access(self, seeded_app):
        """404, not 403 — an outsider can't tell the file exists."""
        cid = self._collection(seeded_app, "Preview Private")
        fid = self._upload(seeded_app, cid, "secret.txt", b"classified", "text/plain")

        c = seeded_app["client"]
        other = _auth(seeded_app["analyst_token"])
        assert c.get(f"/api/collections/{cid}/files/{fid}/preview", headers=other).status_code == 404
        assert c.get(f"/api/collections/{cid}/files/{fid}/raw", headers=other).status_code == 404

    def test_a_file_shared_out_of_its_folder_is_previewable(self, seeded_app):
        """Per-file sharing has to carry the preview with it: the recipient holds
        no grant on the parent collection, so a collection-only rule would share
        a file nobody but its owner can open."""
        cid = self._collection(seeded_app, "Preview Shared File")
        fid = self._upload(seeded_app, cid, "shared.txt", b"for you", "text/plain")

        c = seeded_app["client"]
        gs = c.get("/api/sharing/groups", headers=_auth(seeded_app["admin_token"])).json()
        everyone = next(g for g in gs if g["is_everyone"])
        r = c.put(
            f"/api/sharing/corpus_file/{fid}",
            json={"group_ids": [everyone["id"]]},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code == 200, r.text

        _seed_everyone_membership("analyst1")
        body = c.get(f"/api/collections/{cid}/files/{fid}/preview", headers=_auth(seeded_app["analyst_token"]))
        assert body.status_code == 200, body.text
        assert body.json()["text"] == "for you"

    def test_restricted_principal_is_held_to_its_intersection(self, seeded_app):
        """A co-session caller is not a user dict — ``user['id']`` would crash it
        (the /api/collections list regression above) — and its authority is its
        intersection, so a collection outside that set previews as 404 even
        though the underlying owner is an admin."""
        import asyncio

        import pytest
        from fastapi import HTTPException as _HTTPException

        from app.api.collections import preview_file
        from app.auth.session_principal import SessionPrincipal
        from src.repositories import corpus_files_repo, file_corpora_repo

        repo = file_corpora_repo()
        inside = repo.create(name="SP Preview In", slug="sp-preview-in", description=None, created_by="admin1")
        outside = repo.create(name="SP Preview Out", slug="sp-preview-out", description=None, created_by="admin1")
        fid_in = corpus_files_repo().add(
            corpus_id=inside, filename="in.txt", sha256="s", file_type="txt", size_bytes=1, storage_path=None
        )
        fid_out = corpus_files_repo().add(
            corpus_id=outside, filename="out.txt", sha256="s", file_type="txt", size_bytes=1, storage_path=None
        )
        principal = SessionPrincipal(
            "chat_sp_preview",
            ["analyst1"],
            ["analyst@test.com"],
            {"collection": frozenset({inside})},
        )

        # Inside the intersection: reached. These rows carry no blob, and a
        # textual row without one degrades to the explanatory shape rather than
        # erroring, so "the access gate let it through" now shows up as a real
        # response instead of as the next check's 404.
        inside_body = asyncio.run(preview_file(collection_id=inside, file_id=fid_in, user=principal))
        assert inside_body["kind"] == "none"
        assert inside_body["reason"]

        # Outside it: indistinguishable from "no such file".
        with pytest.raises(_HTTPException) as outside_err:
            asyncio.run(preview_file(collection_id=outside, file_id=fid_out, user=principal))
        assert outside_err.value.detail == "file_not_found"


class TestTieredAudienceDocumentText:
    """SharePoint ACL-mirroring Slice 4c (2026-08-30 plan, Task 11): a
    TIERED collection's document TEXT — raw bytes, text preview, and
    chunk-backed search snippets — is narrowed to the caller holding the
    scope's top audience class (or an admin); collection *reachability* is
    unaffected. See ``app.api.collections._document_text_visible``.
    """

    def _tiered_collection(self, seeded_app, name: str) -> tuple[str, str]:
        """A collection reachable by both ``analyst1`` and ``km_admin1``
        (Everyone grant) whose SharePoint scope carries two audience classes
        — ``full`` (most-privileged, held by ``analyst1`` alone via a
        dedicated group) and ``redacted`` (nobody). Same ``config.scopes``
        shape ``app/api/admin_sharepoint.py::confirm_scope`` persists (Task
        8), matching ``tests/test_audience_classes.py``'s seeding idiom.
        """
        import uuid

        from src.repositories import source_connections_repo, user_group_members_repo, user_groups_repo

        c = seeded_app["client"]
        r = c.post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        _seed_collection_grant(cid, "analyst1")
        _seed_collection_grant(cid, "km_admin1")

        top_group = user_groups_repo().create(name=f"top-{uuid.uuid4().hex[:8]}")["id"]
        user_group_members_repo().add_member("analyst1", top_group, source="admin", added_by="test")

        source_connections_repo().create(
            id=uuid.uuid4().hex,
            name=f"sp-{cid}",
            source_type="sharepoint",
            config={
                "scopes": [
                    {
                        "source_scope_id": f"drive:{cid}",
                        "display_path": name,
                        "collection_id": cid,
                        "audience_classes": [
                            {"name": "full", "group_ids": [top_group]},
                            {"name": "redacted", "group_ids": []},
                        ],
                    }
                ]
            },
        )
        return cid, top_group

    def _upload(self, seeded_app, cid: str, filename: str, body: bytes, ctype: str) -> str:
        r = seeded_app["client"].post(
            f"/api/collections/{cid}/files",
            files={"files": (filename, io.BytesIO(body), ctype)},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert r.status_code in (200, 201, 422), r.text
        return r.json()[0]["file_id"]

    def test_top_class_caller_gets_raw_and_preview(self, seeded_app):
        cid, _top_group = self._tiered_collection(seeded_app, "Tiered Top")
        text_fid = self._upload(seeded_app, cid, "notes.md", b"# Title\n\ntop secret body", "text/markdown")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        img_fid = self._upload(seeded_app, cid, "shot.png", png, "image/png")

        c = seeded_app["client"]
        tok = _auth(seeded_app["analyst_token"])

        preview = c.get(f"/api/collections/{cid}/files/{text_fid}/preview", headers=tok)
        assert preview.status_code == 200, preview.text
        assert preview.json()["kind"] == "text"
        assert "top secret body" in preview.json()["text"]

        raw = c.get(f"/api/collections/{cid}/files/{img_fid}/raw", headers=tok)
        assert raw.status_code == 200, raw.text
        assert raw.content == png

    def test_lower_class_caller_gets_404_raw_and_no_text_preview(self, seeded_app):
        cid, _top_group = self._tiered_collection(seeded_app, "Tiered Lower")
        text_fid = self._upload(seeded_app, cid, "notes.md", b"# Title\n\ntop secret body", "text/markdown")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        img_fid = self._upload(seeded_app, cid, "shot.png", png, "image/png")

        c = seeded_app["client"]
        # km_admin1 holds the collection grant (reachable) but no audience class.
        tok = _auth(seeded_app["km_admin_token"])

        preview = c.get(f"/api/collections/{cid}/files/{text_fid}/preview", headers=tok)
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["kind"] == "none"
        assert body["reason"]
        assert body.get("text") is None

        raw = c.get(f"/api/collections/{cid}/files/{img_fid}/raw", headers=tok)
        assert raw.status_code == 404, raw.text
        assert raw.json()["detail"] == "file_not_found"

    def test_admin_sees_regardless_of_audience_class(self, seeded_app):
        cid, _top_group = self._tiered_collection(seeded_app, "Tiered Admin")
        text_fid = self._upload(seeded_app, cid, "notes.md", b"# Title\n\ntop secret body", "text/markdown")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
        img_fid = self._upload(seeded_app, cid, "shot.png", png, "image/png")

        c = seeded_app["client"]
        tok = _auth(seeded_app["admin_token"])

        preview = c.get(f"/api/collections/{cid}/files/{text_fid}/preview", headers=tok)
        assert preview.status_code == 200, preview.text
        assert preview.json()["kind"] == "text"
        assert "top secret body" in preview.json()["text"]

        raw = c.get(f"/api/collections/{cid}/files/{img_fid}/raw", headers=tok)
        assert raw.status_code == 200, raw.text
        assert raw.content == png

    def test_search_excludes_tiered_chunks_below_top_class_admin_sees_all(self, seeded_app):
        cid, _top_group = self._tiered_collection(seeded_app, "Tiered Search")
        from src.repositories import corpus_chunks_repo, corpus_files_repo

        fid = corpus_files_repo().add(
            corpus_id=cid, filename="d.txt", sha256="s", file_type="txt", size_bytes=1, storage_path="/x"
        )
        corpus_chunks_repo().add_many(
            [{"corpus_id": cid, "file_id": fid, "ordinal": 0, "text": "the confidential keyword appears here"}]
        )

        c = seeded_app["client"]

        top = c.get(
            "/api/collections/search",
            params={"q": "confidential keyword"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert top.status_code == 200, top.text
        assert any("confidential" in (r.get("text") or "") for r in top.json()["results"])

        lower = c.get(
            "/api/collections/search",
            params={"q": "confidential keyword"},
            headers=_auth(seeded_app["km_admin_token"]),
        )
        assert lower.status_code == 200, lower.text
        # The collection is still reachable (Everyone grant) — the chunk is
        # silently excluded, not turned into an access-denied hint.
        assert lower.json()["results"] == []

        admin = c.get(
            "/api/collections/search",
            params={"q": "confidential keyword"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert admin.status_code == 200, admin.text
        assert any("confidential" in (r.get("text") or "") for r in admin.json()["results"])

    def test_plain_collection_regression_unchanged(self, seeded_app):
        """Non-tiered collection: raw/preview/search behave exactly as before
        Task 11 — ``top_class_for`` has no entry, so ``_document_text_visible``
        is always True and this is byte-for-byte the pre-existing behavior."""
        c = seeded_app["client"]
        r = c.post("/api/collections", json={"name": "Plain Regression"}, headers=_auth(seeded_app["admin_token"]))
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
        _seed_collection_grant(cid, "analyst1")
        fid = self._upload(seeded_app, cid, "notes.md", b"plain text body", "text/markdown")

        tok = _auth(seeded_app["analyst_token"])
        preview = c.get(f"/api/collections/{cid}/files/{fid}/preview", headers=tok)
        assert preview.status_code == 200, preview.text
        assert preview.json()["kind"] == "text"
        assert "plain text body" in preview.json()["text"]


class TestListingPastTheRepoCap:
    """A 201st collection is still listed and still authorized.

    ``file_corpora_repo().list()`` defaults to ``limit=200``. Two surfaces read
    it for "every live corpus": the listing itself, and
    ``accessible_collection_ids``, which builds the *owned* half of an
    authorization decision from it. Truncation there fails closed — the owner
    of the 201st collection is told they have no access — which reads as a
    broken grant rather than as a cut-off list.

    Filler names sort before the target so the target is exactly the row an
    ``ORDER BY name LIMIT 200`` drops.
    """

    _FILLER = 200
    _TARGET_NAME = "zzz-owned-by-analyst"

    def _seed(self, owner: str) -> str:
        from src.repositories import file_corpora_repo

        repo = file_corpora_repo()
        for i in range(self._FILLER):
            repo.create(name=f"coll-{i:03d}", slug=f"coll-{i:03d}", description=None, created_by="somebody-else")
        return repo.create(
            name=self._TARGET_NAME,
            slug="zzz-owned-by-analyst",
            description=None,
            created_by=owner,
        )

    def test_owner_of_201st_collection_is_granted_access(self, seeded_app):
        """The authorization input must see past the cap."""
        from app.auth.access import accessible_collection_ids

        target = self._seed(owner="analyst1")
        allowed = accessible_collection_ids({"id": "analyst1", "email": "analyst@test.com"})
        assert allowed is not None, "analyst is not admin — expected a concrete set"
        assert target in allowed

    def test_owner_sees_201st_collection_in_listing(self, seeded_app):
        c = seeded_app["client"]
        target = self._seed(owner="analyst1")
        resp = c.get("/api/collections", headers=_auth(seeded_app["analyst_token"]))
        assert resp.status_code == 200, resp.text
        assert target in {item["id"] for item in resp.json()["items"]}

    def test_admin_listing_is_not_truncated(self, seeded_app):
        c = seeded_app["client"]
        target = self._seed(owner="somebody-else")
        resp = c.get("/api/collections", headers=_auth(seeded_app["admin_token"]))
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == self._FILLER + 1
        assert target in {item["id"] for item in items}


class TestAutoShareAdminUploads:
    """`library.auto_share_admin_uploads` — admin Library uploads land already
    shared to Everyone (opt-in per instance, default off).

    Scope is deliberately narrow: only the `POST /api/collections` path and
    only admin creators. Analyst uploads stay private-by-default, and an
    admin's casual chat drop (`create_single_file_artefact`) must never
    auto-publish.
    """

    def _everyone_grant_exists(self, corpus_id: str) -> bool:
        from src.repositories.resource_grants import ResourceGrantsRepository
        from src.repositories.user_groups import UserGroupsRepository

        conn = get_system_db()
        try:
            grp = UserGroupsRepository(conn).get_by_name("Everyone")
            assert grp, "Everyone group must be seeded"
            return ResourceGrantsRepository(conn).has_grant([grp["id"]], "collection", corpus_id)
        finally:
            conn.close()

    def test_flag_on_admin_upload_is_shared_to_everyone(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS", "true")
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Company Docs"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["visibility"] == "workspace"
        assert self._everyone_grant_exists(body["id"])
        # The point of the flag: an analyst in Everyone sees the upload with
        # no manual share step.
        _seed_everyone_membership("analyst1")
        listed = c.get("/api/collections", headers=_auth(seeded_app["analyst_token"]))
        assert listed.status_code == 200, listed.text
        assert body["id"] in {r["id"] for r in listed.json()["items"]}

    def test_flag_on_non_admin_upload_stays_private(self, seeded_app, monkeypatch):
        monkeypatch.setenv("AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS", "true")
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Analyst Notes"},
            headers=_auth(seeded_app["analyst_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["visibility"] == "private"
        assert not self._everyone_grant_exists(body["id"])

    def test_flag_off_admin_upload_stays_private(self, seeded_app):
        # Default off: a routine upgrade must not change what admin uploads
        # are visible to anyone.
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Still Private"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["visibility"] == "private"
        assert not self._everyone_grant_exists(body["id"])

    def test_flag_on_chat_drop_by_admin_stays_private(self, seeded_app, monkeypatch):
        # Guard: the chat-upload path creates artefacts through
        # create_single_file_artefact, which the auto-share hook must not
        # touch — an admin's ad-hoc chat file is not a publication.
        monkeypatch.setenv("AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS", "true")
        from app.corpus_ingest import create_single_file_artefact

        created = create_single_file_artefact(owner_id="admin1", filename="note.txt", data=b"hello")
        assert created is not None
        assert not self._everyone_grant_exists(created["collection"]["id"])

    def test_flag_on_restricted_principal_stays_private(self, seeded_app, monkeypatch):
        # A restricted principal (co-session / agent-session) is never an
        # admin — the helper must refuse via the explicit PRINCIPAL_TYPES
        # seam (app/auth/session_principal.py), not via an accidental
        # TypeError on `user["id"]`.
        monkeypatch.setenv("AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS", "true")
        from app.api.collections import _maybe_auto_share_admin_upload
        from app.auth.session_principal import AgentPrincipal, SessionPrincipal

        principals = [
            SessionPrincipal(
                session_id="s1",
                participant_user_ids=["admin1"],
                participant_emails=["admin@test.com"],
                intersection={},
            ),
            AgentPrincipal(
                session_id="s2",
                agent_id="agent1",
                owner_user_id="admin1",
                owner_email="admin@test.com",
                intersection={},
            ),
        ]
        for principal in principals:
            assert _maybe_auto_share_admin_upload("col_whatever", principal) == "private"

    def test_flag_on_grant_write_failure_leaves_create_ok_and_private(self, seeded_app, monkeypatch):
        # Fail-closed branch must actually execute: a broken grant write may
        # not fail the create — the collection lands private and the
        # response says so.
        monkeypatch.setenv("AGNES_LIBRARY_AUTO_SHARE_ADMIN_UPLOADS", "true")

        def _boom():
            raise RuntimeError("grants backend down")

        import src.repositories as repos

        monkeypatch.setattr(repos, "resource_grants_repo", _boom)
        c = seeded_app["client"]
        resp = c.post(
            "/api/collections",
            json={"name": "Grant Write Down"},
            headers=_auth(seeded_app["admin_token"]),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["visibility"] == "private"
        # _everyone_grant_exists reads through the repository classes
        # directly, so the factory monkeypatch does not blind this assert.
        assert not self._everyone_grant_exists(body["id"])


class TestProducerUploadAccess:
    """A corpus-extraction producer's own scoped callback credential
    (`ProducerPrincipal`, `app.auth.producer_token`) may upload into a
    collection listed in its own `collection_ids` claim — every OTHER
    collection route (list/delete/reingest/preview/raw) still 403s it,
    since only `upload_files` uses `require_collection_write_or_producer_
    access` instead of the plain `require_collection_access`."""

    def _create_collection(self, seeded_app, name: str) -> str:
        c = seeded_app["client"]
        cr = c.post("/api/collections", json={"name": name}, headers=_auth(seeded_app["admin_token"]))
        assert cr.status_code == 201, cr.text
        return cr.json()["id"]

    def _producer_token(self, collection_ids) -> str:
        from app.auth.producer_token import mint_producer_token

        return mint_producer_token(connection_id="conn1", collection_ids=list(collection_ids), ttl_seconds=3600)

    def test_producer_uploads_into_its_own_scoped_collection(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_collection(seeded_app, "Producer Scoped Upload")
        token = self._producer_token([corpus_id])

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("notes.txt", io.BytesIO(b"hello world"), "text/plain")},
            headers=_auth(token),
        )
        assert resp.status_code == 201, resp.text

    def test_producer_upload_to_undeclared_collection_is_403(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_collection(seeded_app, "Producer Undeclared")
        # Token scoped to a DIFFERENT collection only.
        token = self._producer_token(["some-other-collection"])

        resp = c.post(
            f"/api/collections/{corpus_id}/files",
            files={"files": ("notes.txt", io.BytesIO(b"hello world"), "text/plain")},
            headers=_auth(token),
        )
        assert resp.status_code == 403

    def test_producer_cannot_list_files_of_its_own_scoped_collection(self, seeded_app):
        """Scope is upload-only — GET .../files stays on the plain
        `require_collection_access`, which never accepts a producer."""
        c = seeded_app["client"]
        corpus_id = self._create_collection(seeded_app, "Producer Read Denied")
        token = self._producer_token([corpus_id])

        resp = c.get(f"/api/collections/{corpus_id}/files", headers=_auth(token))
        assert resp.status_code == 403

    def test_producer_cannot_read_the_collection_itself(self, seeded_app):
        c = seeded_app["client"]
        corpus_id = self._create_collection(seeded_app, "Producer Detail Denied")
        token = self._producer_token([corpus_id])

        resp = c.get(f"/api/collections/{corpus_id}", headers=_auth(token))
        assert resp.status_code == 403
