"""PG-backed tests for the Collections upsert prerequisite (fact-graph-over-
Collections design §6): source-stable-id-first matching, id preservation,
and the unchanged-content short-circuit.

Two layers:

* Direct calls into ``app.api.collections._upsert_corpus_file`` — precise,
  fast, and sidesteps FastAPI's ``BackgroundTasks`` running real ingestion
  synchronously under ``TestClient`` (which would otherwise overwrite the
  ``processing_status`` this module asserts on).
* One HTTP round-trip via ``build_seeded_client("pg", ...)`` proving the
  endpoint wiring itself (form fields -> repo calls) end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ID = "col_test"


def _setup_pg(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": CORPUS_ID, "slug": "test", "name": "Test", "by": "u"},
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    import src.repositories as factory

    return factory


@pytest.fixture
def pg_repos(pg_engine, monkeypatch):
    return _setup_pg(pg_engine, monkeypatch)


def test_new_file_with_stable_id_creates_row_and_mapping(pg_repos):
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()
    file_id, needs_processing = _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:abc123",
        source_doc_id="doc1",
        source_sha256_meta="crawlersha",
        filename="a.md",
        sha256="content-sha-1",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/content-sha-1.md",
        sources_repo=sources_repo,
    )
    assert needs_processing is True
    row = pg_repos.corpus_files_repo().get(file_id)
    assert row["sha256"] == "content-sha-1"
    assert sources_repo.resolve(CORPUS_ID, "graph:abc123") == file_id
    mapping = sources_repo.get(file_id)
    assert mapping["source_doc_id"] == "doc1"
    assert mapping["source_sha256"] == "crawlersha"


def test_resync_same_stable_id_unchanged_content_short_circuits(pg_repos):
    """Lifecycle row C3: re-sync, content unchanged -> row kept, zero
    re-processing, claims untouched (no chunk purge, no status reset)."""
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()

    file_id, _ = _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:abc123",
        source_doc_id="doc1",
        source_sha256_meta=None,
        filename="a.md",
        sha256="same-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/same-sha.md",
        sources_repo=sources_repo,
    )
    cf_repo.set_status(file_id, status="indexed", detail={"chunk_count": 5})

    file_id2, needs_processing = _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:abc123",
        source_doc_id="doc1",
        source_sha256_meta=None,
        filename="a.md",
        sha256="same-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/same-sha.md",
        sources_repo=sources_repo,
    )
    assert file_id2 == file_id
    assert needs_processing is False
    row = cf_repo.get(file_id)
    assert row["processing_status"] == "indexed"
    assert row["processing_detail"]["chunk_count"] == 5


def test_resync_same_stable_id_unchanged_content_retries_a_failed_row(pg_repos):
    """The crawler-sync counterpart of the short-circuit above: a row the
    previous ingest left `rejected` (or `needs_review`, or parked in
    `pending`) is NOT considered done, so the next re-sync of the identical
    bytes resets it to `pending` and reports `needs_processing`. Without
    this, a doc-sync source could never recover a file whose ingest failed
    for an environmental reason — the crawler re-sends the same bytes
    forever and nothing re-runs (Devin Review on #1655)."""
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()

    def _sync():
        return _upsert_corpus_file(
            CORPUS_ID,
            path=None,
            stable_id="graph:retry1",
            source_doc_id="doc-retry",
            source_sha256_meta=None,
            filename="a.md",
            sha256="same-sha",
            file_type="md",
            size_bytes=10,
            storage_path="/blobs/same-sha.md",
            sources_repo=sources_repo,
        )

    file_id, _ = _sync()
    for failed_status in ("rejected", "needs_review", "pending"):
        cf_repo.set_status(file_id, status=failed_status, detail={"reason": "ingest_error: boom"})
        file_id2, needs_processing = _sync()
        assert file_id2 == file_id, failed_status
        assert needs_processing is True, f"{failed_status} row must be retried by an unchanged re-sync"
        assert cf_repo.get(file_id)["processing_status"] == "pending", failed_status

    # ...and the mapping still points at the same row (no duplicate anchor).
    assert sources_repo.resolve(CORPUS_ID, "graph:retry1") == file_id


def test_resync_same_stable_id_rename_only_updates_path(pg_repos):
    """Lifecycle row C4: rename/move -> same source_stable_id -> same row,
    path updated; claims untouched (still no re-processing)."""
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()

    file_id, _ = _upsert_corpus_file(
        CORPUS_ID,
        path="old/name.md",
        stable_id="graph:abc123",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="old-name.md",
        sha256="same-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/same-sha.md",
        sources_repo=sources_repo,
    )
    cf_repo.set_status(file_id, status="indexed")

    file_id2, needs_processing = _upsert_corpus_file(
        CORPUS_ID,
        path="new/name.md",
        stable_id="graph:abc123",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="new-name.md",
        sha256="same-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/same-sha.md",
        sources_repo=sources_repo,
    )
    assert file_id2 == file_id
    assert needs_processing is False
    row = cf_repo.get(file_id)
    assert row["path"] == "new/name.md"
    assert row["filename"] == "new-name.md"
    assert row["processing_status"] == "indexed"


def test_extension_only_rename_cleans_up_the_old_blob(pg_repos, tmp_path):
    """Storage paths are content-addressed as ``{sha256}{ext}`` with the
    extension taken from the FILENAME — so a rename that changes only the
    extension keeps the sha yet allocates a NEW blob path. The old blob must
    be unlinked even though ``content_changed`` is False; nesting the
    cleanup under the content-changed branch leaked it on disk (found in
    review — the replaced delete+insert path cleaned unconditionally)."""
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()

    old_blob = tmp_path / "same-sha.htm"
    new_blob = tmp_path / "same-sha.html"
    old_blob.write_text("same content")
    new_blob.write_text("same content")

    file_id, _ = _upsert_corpus_file(
        CORPUS_ID,
        path="site/page.htm",
        stable_id="graph:ext-rename",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="page.htm",
        sha256="same-sha",
        file_type="htm",
        size_bytes=12,
        storage_path=str(old_blob),
        sources_repo=sources_repo,
    )
    # Stage the row as fully ingested so the second call really takes the
    # unchanged-content short-circuit — a row still `pending` from its first
    # upsert is deliberately re-scheduled (see `_ingest_incomplete`), which
    # would make `needs_processing is False` below assert nothing.
    cf_repo.set_status(file_id, status="indexed", detail={"chunk_count": 1})

    file_id2, needs_processing = _upsert_corpus_file(
        CORPUS_ID,
        path="site/page.html",
        stable_id="graph:ext-rename",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="page.html",
        sha256="same-sha",
        file_type="html",
        size_bytes=12,
        storage_path=str(new_blob),
        sources_repo=sources_repo,
    )

    assert file_id2 == file_id
    assert needs_processing is False
    assert cf_repo.get(file_id)["storage_path"] == str(new_blob)
    assert not old_blob.exists(), "old blob leaked after extension-only rename"
    assert new_blob.exists()


def test_resync_same_stable_id_content_changed_resets_and_purges(pg_repos):
    """Content changed -> same row, new sha; status reset to 'pending' so a
    fresh extraction pass runs (old chunks purged)."""
    from app.api.collections import _upsert_corpus_file
    from src.repositories import corpus_chunks_repo

    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()
    chunks_repo = corpus_chunks_repo()

    file_id, _ = _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:abc123",
        source_doc_id="provisional-doc",
        source_sha256_meta="old-crawler-sha",
        filename="a.md",
        sha256="v1-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/v1-sha.md",
        sources_repo=sources_repo,
    )
    cf_repo.set_status(file_id, status="indexed")
    chunks_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": file_id, "ordinal": 0, "text": "old chunk"}])

    file_id2, needs_processing = _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:abc123",
        source_doc_id="real-doc-id",
        source_sha256_meta="new-crawler-sha",
        filename="a.md",
        sha256="v2-sha",
        file_type="md",
        size_bytes=20,
        storage_path="/blobs/v2-sha.md",
        sources_repo=sources_repo,
    )
    assert file_id2 == file_id  # id preserved
    assert needs_processing is True

    row = cf_repo.get(file_id)
    assert row["sha256"] == "v2-sha"
    assert row["storage_path"] == "/blobs/v2-sha.md"
    assert row["processing_status"] == "pending"
    assert chunks_repo.list_for_file(file_id) == []

    mapping = sources_repo.get(file_id)
    assert mapping["source_doc_id"] == "real-doc-id"
    assert mapping["source_sha256"] == "new-crawler-sha"


@pytest.fixture
def pg_repos_facts(pg_engine, monkeypatch, tmp_path):
    """``pg_repos`` with the (default-off) facts flag enabled, so the claim
    purge + orphan sweep hooks in ``_purge_children_and_content`` actually
    run."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AGNES_FACTS_ENABLED", "1")
    return _setup_pg(pg_engine, monkeypatch)


def _seed_claim_on(repo, *, file_id: str, natural_key: str, sha: str, quote: str) -> str:
    """One fact + alias + a single claim anchored to ``file_id``. Returns the
    fact id."""
    fact_id = repo.create_fact(type="engagement")
    repo.add_alias(fact_id=fact_id, type="engagement", natural_key=natural_key)
    repo.add_claim(
        fact_id=fact_id,
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        file_sha256=sha,
        quote=quote,
    )
    return fact_id


def _claim_count(engine, file_id: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM claims WHERE corpus_file_id = :f"), {"f": file_id}
        ).scalar_one()


def _fact_exists(engine, fact_id: str) -> bool:
    with engine.begin() as conn:
        return conn.execute(sa.text("SELECT 1 FROM facts WHERE id = :i"), {"i": fact_id}).first() is not None


def test_content_changed_replace_purges_that_documents_claims(pg_repos_facts, pg_engine):
    """Spec §6 "content changed": the row keeps its id, but a claim quotes a
    verbatim span of the OLD bytes — bytes that no longer exist once the sha
    moves (the old blob is refcount-deleted). Nothing in the read path
    filters that: `claims.file_sha256` is compared by no query, and
    `facts_pg.claims()` joins `corpus_files` for the document's CURRENT
    name/path, so a stale quote would be served under the new document's
    identity and keep its subject alive in search/neighbors. So the claims
    are dropped at replace time, and the subject left with no evidence is
    swept — instead of waiting for a producer re-extraction that, for a
    hand-replaced file, never comes (Devin Review on #1652).

    Before #1655's in-place upsert this happened for free: the row was
    deleted and the claims cascaded."""
    from app.api.collections import _upsert_corpus_file
    from src.repositories.facts_pg import FactsPgRepository

    sources_repo = pg_repos_facts.corpus_file_sources_repo()
    repo = FactsPgRepository(pg_engine)

    def _upsert(sha: str) -> str:
        fid, _ = _upsert_corpus_file(
            CORPUS_ID,
            path="docs/a.md",
            stable_id="graph:claims1",
            source_doc_id=None,
            source_sha256_meta=None,
            filename="a.md",
            sha256=sha,
            file_type="md",
            size_bytes=10,
            storage_path=f"/blobs/{sha}.md",
            sources_repo=sources_repo,
        )
        return fid

    file_id = _upsert("v1-sha")
    doomed = _seed_claim_on(
        repo, file_id=file_id, natural_key="engagement:old", sha="v1-sha", quote="The old sentence."
    )

    # A second document's fact must be untouched by both the purge and the
    # sweep — neither may reach past the replaced row.
    other_file = _upsert_corpus_file(
        CORPUS_ID,
        path="docs/b.md",
        stable_id="graph:claims2",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="b.md",
        sha256="other-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/other-sha.md",
        sources_repo=sources_repo,
    )[0]
    survivor = _seed_claim_on(
        repo, file_id=other_file, natural_key="engagement:kept", sha="other-sha", quote="Still true."
    )

    assert _claim_count(pg_engine, file_id) == 1

    file_id2 = _upsert("v2-sha")
    assert file_id2 == file_id, "the row id is still preserved — that is the point of §6"

    assert _claim_count(pg_engine, file_id) == 0, "claims quoting replaced bytes must not survive the replace"
    assert not _fact_exists(pg_engine, doomed), "a subject left with zero claims must be swept"
    assert _claim_count(pg_engine, other_file) == 1
    assert _fact_exists(pg_engine, survivor)


def test_unchanged_content_resync_keeps_claims(pg_repos_facts, pg_engine):
    """The other half of the same rule, and lifecycle row C3: an unchanged
    re-sync (or a pure rename) must NOT touch claims — that is exactly the
    destruction §6 preserved the row id to prevent. Guards the purge above
    from being hoisted out of the content-changed branch."""
    from app.api.collections import _upsert_corpus_file
    from src.repositories.facts_pg import FactsPgRepository

    sources_repo = pg_repos_facts.corpus_file_sources_repo()
    repo = FactsPgRepository(pg_engine)

    def _upsert(filename: str, path: str) -> str:
        fid, _ = _upsert_corpus_file(
            CORPUS_ID,
            path=path,
            stable_id="graph:keepclaims",
            source_doc_id=None,
            source_sha256_meta=None,
            filename=filename,
            sha256="stable-sha",
            file_type="md",
            size_bytes=10,
            storage_path="/blobs/stable-sha.md",
            sources_repo=sources_repo,
        )
        return fid

    file_id = _upsert("a.md", "docs/a.md")
    fact_id = _seed_claim_on(repo, file_id=file_id, natural_key="engagement:kept", sha="stable-sha", quote="Unchanged.")

    assert _upsert("renamed.md", "docs/renamed.md") == file_id
    assert _claim_count(pg_engine, file_id) == 1, "a rename must not destroy claims"
    assert _fact_exists(pg_engine, fact_id)


def test_manual_path_reupload_of_crawler_anchored_file_preserves_id(pg_repos):
    """A manual (no source_stable_ids) path re-upload of a file the crawler
    previously anchored must still match via path and preserve the id — the
    exact scenario spec §6 calls out ("a hand upload can no longer cascade a
    document's claims away")."""
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()
    cf_repo = pg_repos.corpus_files_repo()

    file_id, _ = _upsert_corpus_file(
        CORPUS_ID,
        path="shared/doc.md",
        stable_id="graph:abc123",
        source_doc_id="doc1",
        source_sha256_meta=None,
        filename="doc.md",
        sha256="v1-sha",
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/v1-sha.md",
        sources_repo=sources_repo,
    )

    # Manual re-upload: no stable_id this time, but the SAME path.
    file_id2, needs_processing = _upsert_corpus_file(
        CORPUS_ID,
        path="shared/doc.md",
        stable_id=None,
        source_doc_id=None,
        source_sha256_meta=None,
        filename="doc.md",
        sha256="v2-sha",
        file_type="md",
        size_bytes=12,
        storage_path="/blobs/v2-sha.md",
        sources_repo=None,  # request never supplied source_stable_ids
    )
    assert file_id2 == file_id
    assert needs_processing is True
    row = cf_repo.get(file_id)
    assert row["sha256"] == "v2-sha"
    # The crawler's mapping survives untouched — this upload never touched it.
    assert sources_repo.resolve(CORPUS_ID, "graph:abc123") == file_id


def test_unreferenced_old_blob_is_cleaned_up_after_content_change(pg_repos, tmp_path):
    from app.api.collections import _upsert_corpus_file

    sources_repo = pg_repos.corpus_file_sources_repo()

    old_blob = tmp_path / "old.md"
    old_blob.write_text("old content")
    new_blob = tmp_path / "new.md"
    new_blob.write_text("new content")

    file_id, _ = _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:blob-test",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="a.md",
        sha256="v1-sha",
        file_type="md",
        size_bytes=10,
        storage_path=str(old_blob),
        sources_repo=sources_repo,
    )
    assert old_blob.exists()

    _upsert_corpus_file(
        CORPUS_ID,
        path=None,
        stable_id="graph:blob-test",
        source_doc_id=None,
        source_sha256_meta=None,
        filename="a.md",
        sha256="v2-sha",
        file_type="md",
        size_bytes=11,
        storage_path=str(new_blob),
        sources_repo=sources_repo,
    )
    assert not old_blob.exists()  # unreferenced -> cleaned up
    assert new_blob.exists()
    del file_id


def test_upload_files_endpoint_with_source_stable_id_end_to_end(pg_engine, monkeypatch, tmp_path):
    """HTTP round trip proving the form-field wiring itself, on the real PG
    backend."""
    import io

    from ._parity_sweep_util import build_seeded_client

    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    auth = {"Authorization": f"Bearer {admin_token}"}

    cr = client.post("/api/collections", json={"name": "PG Upsert E2E"}, headers=auth)
    assert cr.status_code == 201, cr.text
    corpus_id = cr.json()["id"]

    resp = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.md", io.BytesIO(b"hello world"), "text/markdown")},
        data={"source_stable_ids": "graph:e2e-1"},
        headers=auth,
    )
    assert resp.status_code == 201, resp.text
    file_id = resp.json()[0]["file_id"]

    import src.repositories as factory

    assert factory.corpus_file_sources_repo().resolve(corpus_id, "graph:e2e-1") == file_id


def _e2e_client(pg_engine, monkeypatch, tmp_path):
    from ._parity_sweep_util import build_seeded_client

    client, admin_token = build_seeded_client("pg", tmp_path, monkeypatch, pg_engine)
    auth = {"Authorization": f"Bearer {admin_token}"}
    cr = client.post("/api/collections", json={"name": "PG Preflight"}, headers=auth)
    assert cr.status_code == 201, cr.text
    return client, auth, cr.json()["id"]


def test_cross_anchor_collision_in_one_batch_rejected(pg_engine, monkeypatch, tmp_path):
    """The per-key guards compare `paths` against itself and
    `source_stable_ids` against itself, but the match is stable_id FIRST then
    path — so a batch where file 1's stable id and file 2's path resolve to
    the SAME existing row slips through both, and the second overwrites the
    first in place: file 1's bytes lost, one `file_id` returned twice. The
    read-only pre-flight resolves each file's target row up front and refuses
    the batch (Devin Review on #1655)."""
    import io

    client, auth, corpus_id = _e2e_client(pg_engine, monkeypatch, tmp_path)

    first = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.md", io.BytesIO(b"original"), "text/markdown")},
        data={"source_stable_ids": "graph:cross", "paths": "docs/a.md"},
        headers=auth,
    )
    assert first.status_code == 201, first.text
    anchored_id = first.json()[0]["file_id"]

    # File 1 targets the row by stable id; file 2 targets the SAME row by its
    # path. Neither per-key guard fires: the stable ids differ (one is blank)
    # and the paths differ (one is blank).
    resp = client.post(
        f"/api/collections/{corpus_id}/files",
        files=[
            ("files", ("a.md", io.BytesIO(b"via stable id"), "text/markdown")),
            ("files", ("a.md", io.BytesIO(b"via path"), "text/markdown")),
        ],
        data={"source_stable_ids": ["graph:cross", ""], "paths": ["", "docs/a.md"]},
        headers=auth,
    )
    assert resp.status_code == 400, resp.text
    assert "duplicate_target_row_in_batch" in resp.text

    # Nothing was written: the anchored row still holds its original bytes.
    listing = client.get(f"/api/collections/{corpus_id}/files", headers=auth)
    rows = listing.json()["files"]
    assert [r["file_id"] for r in rows] == [anchored_id]
    assert rows[0]["size_bytes"] == len(b"original")


def test_stable_id_match_onto_a_path_owned_by_another_row_is_refused(pg_engine, monkeypatch, tmp_path):
    """A stable-id match resolves row R while the upload's `path` is already
    held by a DIFFERENT row: `update_in_place` issues an unconditional UPDATE
    and violates the `(corpus_id, path)` unique index — an unhandled
    IntegrityError, i.e. a 500 with earlier files in the batch already
    written. The pre-flight answers a typed 409 naming both rows instead
    (Devin Review on #1655)."""
    import io

    client, auth, corpus_id = _e2e_client(pg_engine, monkeypatch, tmp_path)

    anchored = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.md", io.BytesIO(b"anchored"), "text/markdown")},
        data={"source_stable_ids": "graph:mover", "paths": "docs/a.md"},
        headers=auth,
    )
    assert anchored.status_code == 201, anchored.text

    occupier = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("b.md", io.BytesIO(b"occupier"), "text/markdown")},
        data={"paths": "docs/b.md"},
        headers=auth,
    )
    assert occupier.status_code == 201, occupier.text
    occupier_id = occupier.json()[0]["file_id"]

    # The crawler now reports graph:mover living at docs/b.md — a path
    # another row already owns.
    resp = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.md", io.BytesIO(b"moved"), "text/markdown")},
        data={"source_stable_ids": "graph:mover", "paths": "docs/b.md"},
        headers=auth,
    )
    assert resp.status_code == 409, resp.text
    assert "path_owned_by_another_file" in resp.text
    assert occupier_id in resp.text

    # Both rows survive untouched — no partial write, no 500.
    listing = client.get(f"/api/collections/{corpus_id}/files", headers=auth)
    by_path = {r["path"]: r for r in listing.json()["files"]}
    assert by_path["docs/a.md"]["size_bytes"] == len(b"anchored")
    assert by_path["docs/b.md"]["size_bytes"] == len(b"occupier")


def test_preflight_allows_a_stable_id_move_onto_a_free_path(pg_engine, monkeypatch, tmp_path):
    """Negative control: the ordinary crawler move — same stable id, new path
    nobody holds — must still succeed and keep the row id."""
    import io

    client, auth, corpus_id = _e2e_client(pg_engine, monkeypatch, tmp_path)

    first = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.md", io.BytesIO(b"content"), "text/markdown")},
        data={"source_stable_ids": "graph:free", "paths": "docs/a.md"},
        headers=auth,
    )
    assert first.status_code == 201, first.text
    file_id = first.json()[0]["file_id"]

    moved = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.md", io.BytesIO(b"content"), "text/markdown")},
        data={"source_stable_ids": "graph:free", "paths": "docs/moved/a.md"},
        headers=auth,
    )
    assert moved.status_code == 201, moved.text
    assert moved.json()[0]["file_id"] == file_id
    assert moved.json()[0]["path"] == "docs/moved/a.md"


def test_content_changed_reupload_on_api_plane_uses_one_ordered_job(pg_engine, monkeypatch, tmp_path):
    """A content-changed re-upload keeps the row id, and the derived
    `table_id` is derived from that id — so on a process WITHOUT the worker
    role, enqueueing a bare derived purge while scheduling `ingest_file`
    in-process lets the purge land AFTER the rebuild and delete the table it
    just built. (The replaced delete+insert path was immune: a fresh row id
    meant a different `table_id`.) The upload path must instead do what
    `reingest_file` does — ONE `collections-purge` job carrying
    `reingest_after_purge=True` — and must not run the ingest in-process
    (Devin Review on #1655)."""
    import io

    import sqlalchemy as _sa

    client, auth, corpus_id = _e2e_client(pg_engine, monkeypatch, tmp_path)

    first = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        data={"paths": "docs/a.csv"},
        headers=auth,
    )
    assert first.status_code == 201, first.text
    file_id = first.json()[0]["file_id"]

    # Now pretend this replica has no worker role.
    import app.roles as roles

    real_role_enabled = roles.role_enabled
    monkeypatch.setattr(roles, "role_enabled", lambda r: False if r is roles.Role.WORKER else real_role_enabled(r))

    with pg_engine.begin() as conn:
        conn.execute(_sa.text("DELETE FROM jobs WHERE kind = 'collections-purge'"))

    second = client.post(
        f"/api/collections/{corpus_id}/files",
        files={"files": ("a.csv", io.BytesIO(b"a,b\n3,4\n5,6\n"), "text/csv")},
        data={"paths": "docs/a.csv"},
        headers=auth,
    )
    assert second.status_code == 201, second.text
    assert second.json()[0]["file_id"] == file_id  # id preserved, hence same table_id

    import json

    with pg_engine.begin() as conn:
        jobs = (
            conn.execute(_sa.text("SELECT payload_json FROM jobs WHERE kind = 'collections-purge' ORDER BY created_at"))
            .scalars()
            .all()
        )
    payloads = [j if isinstance(j, dict) else json.loads(j) for j in jobs]
    mine = [p for p in payloads if p.get("file_id") == file_id]
    assert len(mine) == 1, f"expected exactly one ordered job for this file, got {payloads}"
    assert mine[0].get("reingest_after_purge") is True, (
        f"the purge must carry its re-ingest, not race an in-process one: {mine[0]}"
    )

    # ...and nothing ran in-process: TestClient drains BackgroundTasks, so an
    # in-process ingest would have moved the row off 'pending'.
    assert second.json()[0]["processing_status"] == "pending"
    listing = client.get(f"/api/collections/{corpus_id}/files", headers=auth)
    row = next(r for r in listing.json()["files"] if r["file_id"] == file_id)
    assert row["processing_status"] == "pending", "ingest must be left to the worker plane's ordered job"
