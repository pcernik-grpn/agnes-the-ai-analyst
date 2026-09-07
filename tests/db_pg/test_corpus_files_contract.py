"""Cross-engine contract tests for the corpus_files repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both backends; the same return shapes must come back.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# repo construction helpers
# ---------------------------------------------------------------------------


def _make_duckdb_repo(tmp_path):
    from src.db import _ensure_schema
    from src.repositories.corpus_files import CorpusFilesRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    # seed a parent corpus row (corpus_files.corpus_id is not FK-constrained
    # in DuckDB but we use a real id for realism)
    conn.execute("INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_test', 'test', 'Test', 'u')")
    return CorpusFilesRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    # seed parent corpus
    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": "col_test", "slug": "test", "name": "Test", "by": "u"},
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    from src import db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.corpus_files_pg import CorpusFilesPgRepository

    return CorpusFilesPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a corpus_files repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------

CORPUS_ID = "col_test"


def test_add_then_get_returns_same_shape(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="report.pdf",
        sha256="abc123",
        file_type="application/pdf",
        size_bytes=1024,
        storage_path="/uploads/report.pdf",
    )
    row = repo.get(file_id)
    assert row is not None
    assert row["id"] == file_id
    assert row["corpus_id"] == CORPUS_ID
    assert row["filename"] == "report.pdf"
    assert row["sha256"] == "abc123"
    assert row["file_type"] == "application/pdf"
    assert row["size_bytes"] == 1024
    assert row["storage_path"] == "/uploads/report.pdf"
    assert row["processing_status"] == "pending"
    assert row["processing_detail"] is None


def test_add_id_has_cf_prefix(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="x.txt",
        sha256="d",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    assert file_id.startswith("cf_")


def test_add_default_status_is_pending(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="doc.txt",
        sha256="deadbeef",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    row = repo.get(file_id)
    assert row["processing_status"] == "pending"


def test_add_returns_unique_ids(repo):
    id1 = repo.add(
        corpus_id=CORPUS_ID,
        filename="a.txt",
        sha256="s1",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    id2 = repo.add(
        corpus_id=CORPUS_ID,
        filename="b.txt",
        sha256="s2",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    assert id1 != id2


def test_get_returns_none_when_missing(repo):
    assert repo.get("cf_nonexistent") is None


def test_list_for_corpus_returns_files(repo):
    id1 = repo.add(
        corpus_id=CORPUS_ID,
        filename="x.pdf",
        sha256="h1",
        file_type="application/pdf",
        size_bytes=100,
        storage_path=None,
    )
    id2 = repo.add(
        corpus_id=CORPUS_ID,
        filename="y.pdf",
        sha256="h2",
        file_type="application/pdf",
        size_bytes=200,
        storage_path=None,
    )
    rows = repo.list_for_corpus(CORPUS_ID)
    ids = {r["id"] for r in rows}
    assert {id1, id2} <= ids


def test_list_for_corpus_empty_when_no_files(repo):
    assert repo.list_for_corpus("col_nonexistent") == []


def test_set_status_updates_processing_status(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="doc.pdf",
        sha256="h3",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    repo.set_status(file_id, status="indexed")
    row = repo.get(file_id)
    assert row["processing_status"] == "indexed"


def test_set_status_with_detail_round_trips_json(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="big.pdf",
        sha256="h4",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    detail = {"tier": 1, "vision_used": False, "chunk_count": 12}
    repo.set_status(file_id, status="indexed", detail=detail)
    row = repo.get(file_id)
    assert row["processing_status"] == "indexed"
    # detail is stored as JSON text and decoded back to dict on read
    stored = row["processing_detail"]
    assert isinstance(stored, dict), f"Expected dict, got {type(stored)}: {stored!r}"
    assert stored["chunk_count"] == 12
    assert stored["tier"] == 1


def test_set_status_rejected_with_error_detail(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="broken.pdf",
        sha256="h5",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    repo.set_status(file_id, status="rejected", detail={"error": "parse failed"})
    row = repo.get(file_id)
    assert row["processing_status"] == "rejected"
    assert row["processing_detail"]["error"] == "parse failed"


def test_delete_removes_file(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="gone.pdf",
        sha256="h6",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    assert repo.get(file_id) is not None
    repo.delete(file_id)
    assert repo.get(file_id) is None


def test_parent_file_id_roundtrip_and_children(repo):
    """`parent_file_id` (v87, K1 bundle ingest) links archive children to their zip."""
    parent = repo.add(
        corpus_id=CORPUS_ID,
        filename="dump.zip",
        sha256="p" * 64,
        file_type="zip",
        size_bytes=10,
        storage_path="/tmp/p.zip",
    )
    child = repo.add(
        corpus_id=CORPUS_ID,
        filename="page.html",
        sha256="c" * 64,
        file_type="html",
        size_bytes=5,
        storage_path="/tmp/c.html",
        parent_file_id=parent,
    )
    assert repo.get(parent)["parent_file_id"] is None
    assert repo.get(child)["parent_file_id"] == parent
    kids = repo.list_children(parent)
    assert [k["id"] for k in kids] == [child]
    assert repo.list_children(child) == []


def test_set_status_needs_review_roundtrip(repo):
    """`needs_review` (status-honesty, spec 2026-07-08) persists with its reason."""
    fid = repo.add(
        corpus_id=CORPUS_ID,
        filename="empty.xlsx",
        sha256="s1",
        file_type="xlsx",
        size_bytes=1,
        storage_path="/tmp/empty.xlsx",
    )
    repo.set_status(fid, status="needs_review", detail={"reason": "extraction produced empty table"})
    row = repo.get(fid)
    assert row["processing_status"] == "needs_review"
    assert row["processing_detail"]["reason"] == "extraction produced empty table"


def test_path_roundtrips_and_defaults_none(repo):
    """`path` (v96 upsert identity) persists, and defaults to None when omitted."""
    with_path = repo.add(
        corpus_id=CORPUS_ID,
        filename="storage-api.md",
        sha256="p1",
        file_type="md",
        size_bytes=10,
        storage_path="/tmp/storage-api.md",
        path="apis/storage-api.md",
    )
    without_path = repo.add(
        corpus_id=CORPUS_ID,
        filename="loose.md",
        sha256="p2",
        file_type="md",
        size_bytes=10,
        storage_path="/tmp/loose.md",
    )
    assert repo.get(with_path)["path"] == "apis/storage-api.md"
    assert repo.get(without_path)["path"] is None


def test_get_by_path_finds_row(repo):
    fid = repo.add(
        corpus_id=CORPUS_ID,
        filename="concepts.md",
        sha256="g1",
        file_type="md",
        size_bytes=10,
        storage_path="/tmp/concepts.md",
        path="concepts/overview.md",
    )
    hit = repo.get_by_path(CORPUS_ID, "concepts/overview.md")
    assert hit is not None
    assert hit["id"] == fid
    # Wrong corpus / missing path / None path all return None.
    assert repo.get_by_path("col_other", "concepts/overview.md") is None
    assert repo.get_by_path(CORPUS_ID, "nope.md") is None
    assert repo.get_by_path(CORPUS_ID, None) is None


def test_count_by_storage_path(repo):
    """Refcount helper: how many rows in a corpus share a storage_path."""
    repo.add(
        corpus_id=CORPUS_ID,
        filename="one.md",
        sha256="shared",
        file_type="md",
        size_bytes=4,
        storage_path="/blobs/shared.md",
    )
    repo.add(
        corpus_id=CORPUS_ID,
        filename="two.md",
        sha256="shared",
        file_type="md",
        size_bytes=4,
        storage_path="/blobs/shared.md",
    )
    assert repo.count_by_storage_path(CORPUS_ID, "/blobs/shared.md") == 2
    assert repo.count_by_storage_path(CORPUS_ID, "/blobs/absent.md") == 0
    assert repo.count_by_storage_path("col_other", "/blobs/shared.md") == 0
    assert repo.count_by_storage_path(CORPUS_ID, None) == 0


def test_duplicate_path_rejected_by_unique_index(repo):
    """The (corpus_id, path) unique index forbids a second row on the same path."""
    repo.add(
        corpus_id=CORPUS_ID,
        filename="a.md",
        sha256="x",
        file_type="md",
        size_bytes=1,
        storage_path="/p/a.md",
        path="docs/a.md",
    )
    with pytest.raises(Exception):
        repo.add(
            corpus_id=CORPUS_ID,
            filename="a-again.md",
            sha256="y",
            file_type="md",
            size_bytes=1,
            storage_path="/p/a-again.md",
            path="docs/a.md",
        )


def test_multiple_null_paths_allowed(repo):
    """path=NULL rows are exempt from the unique index (NULLs distinct)."""
    for i in range(3):
        repo.add(
            corpus_id=CORPUS_ID,
            filename=f"loose{i}.md",
            sha256=f"s{i}",
            file_type="md",
            size_bytes=1,
            storage_path=f"/p/loose{i}.md",
        )
    assert len(repo.list_for_corpus(CORPUS_ID)) == 3


def test_move_to_corpus_reparents_and_clears_path(repo):
    """Drag-and-drop in the Library moves a file between collections. The
    file keeps its identity; `path` is cleared because it described a location
    inside the OLD collection and is unique per (corpus_id, path)."""
    a = repo.add(
        corpus_id="col_src",
        filename="f.md",
        sha256="s1",
        file_type="md",
        size_bytes=10,
        storage_path="blobs/s1",
        path="sub/f.md",
    )
    assert repo.move_to_corpus(a, "col_dst") is True
    row = repo.get(a)
    assert row["corpus_id"] == "col_dst"
    assert row["path"] is None
    # It left the source and joined the target.
    assert [r["id"] for r in repo.list_for_corpus("col_dst")] == [a]
    assert repo.list_for_corpus("col_src") == []


def test_move_to_corpus_returns_false_when_missing(repo):
    assert repo.move_to_corpus("cf_nonexistent", "col_dst") is False


def test_update_in_place_preserves_id_and_refreshes_fields(repo):
    """Upsert-in-place (fact-graph-over-Collections §6 prerequisite): a
    matched re-upload refreshes content fields on the SAME row instead of
    delete+insert, so its id (and anything that references it) survives."""
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="old.md",
        sha256="old-sha",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/old-sha.md",
        path="docs/a.md",
    )
    repo.update_in_place(
        file_id,
        filename="new.md",
        sha256="new-sha",
        file_type="md",
        size_bytes=9,
        storage_path="/blobs/new-sha.md",
        path="docs/a.md",
    )
    row = repo.get(file_id)
    assert row["id"] == file_id
    assert row["filename"] == "new.md"
    assert row["sha256"] == "new-sha"
    assert row["size_bytes"] == 9
    assert row["storage_path"] == "/blobs/new-sha.md"
    assert row["path"] == "docs/a.md"


def test_update_in_place_can_change_path_rename(repo):
    """Rename/move: same row, new path (spec §6 lifecycle table)."""
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="a.md",
        sha256="s1",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/s1.md",
        path="old/location.md",
    )
    repo.update_in_place(
        file_id,
        filename="a.md",
        sha256="s1",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/s1.md",
        path="new/location.md",
    )
    assert repo.get(file_id)["path"] == "new/location.md"
    assert repo.get_by_path(CORPUS_ID, "old/location.md") is None
    assert repo.get_by_path(CORPUS_ID, "new/location.md")["id"] == file_id


def test_update_in_place_does_not_touch_processing_status(repo):
    """The caller (collections upload endpoint) decides whether content
    changed and resets status itself via ``set_status`` — this method never
    resets status on its own, so an unchanged-content match can leave an
    'indexed' row exactly as it was (skip-re-chunking short-circuit)."""
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="a.md",
        sha256="s1",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/s1.md",
    )
    repo.set_status(file_id, status="indexed")
    repo.update_in_place(
        file_id,
        filename="a-renamed.md",
        sha256="s1",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/s1.md",
        path=None,
    )
    assert repo.get(file_id)["processing_status"] == "indexed"


def test_update_path_changes_path_and_filename_only(repo):
    """Rename/move with UNCHANGED content (SharePoint crawl rename gate,
    ``connectors.sharepoint.crawler._Ingestor.rename``) — no sha256/
    storage_path/size write, unlike ``update_in_place``."""
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="a.md",
        sha256="s1",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/s1.md",
        path="old/a.md",
    )
    repo.update_path(file_id, path="new/b.md", filename="b.md")
    row = repo.get(file_id)
    assert row["path"] == "new/b.md"
    assert row["filename"] == "b.md"
    assert row["sha256"] == "s1"
    assert row["storage_path"] == "/blobs/s1.md"
    assert row["size_bytes"] == 5


def test_update_path_does_not_touch_processing_status(repo):
    file_id = repo.add(
        corpus_id=CORPUS_ID,
        filename="a.md",
        sha256="s1",
        file_type="md",
        size_bytes=5,
        storage_path="/blobs/s1.md",
    )
    repo.set_status(file_id, status="indexed")
    repo.update_path(file_id, path=None, filename="a-renamed.md")
    assert repo.get(file_id)["processing_status"] == "indexed"
    assert repo.get(file_id)["filename"] == "a-renamed.md"


def test_count_by_corpus_groups_every_corpus_in_one_read(repo):
    """The admin /access projection needs a count per collection; doing that
    with `list_for_corpus` per collection made the page's query count grow with
    the number of collections."""
    for i in range(2):
        repo.add(
            corpus_id="col_a",
            filename=f"a{i}.pdf",
            sha256=f"sha_a{i}",
            file_type="pdf",
            size_bytes=10,
            storage_path=f"/tmp/a{i}.pdf",
        )
    repo.add(
        corpus_id="col_b",
        filename="b.pdf",
        sha256="sha_b",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/b.pdf",
    )
    counts = repo.count_by_corpus()
    assert counts["col_a"] == 2
    assert counts["col_b"] == 1
    # A corpus with no files is ABSENT rather than 0 — the caller renders the
    # zero, so this method needs no knowledge of which corpora exist.
    assert "col_empty" not in counts


def test_count_by_corpus_is_empty_when_there_are_no_files(repo):
    assert repo.count_by_corpus() == {}


def test_search_across_corpora_matches_filename_across_all_corpora(repo):
    """The admin per-file grant picker's bounded, on-demand search — the
    counterpart to the (now capped) `/admin/access` overview projection
    in `app.resource_types._corpus_file_blocks`."""
    repo.add(
        corpus_id="col_a",
        filename="quarterly-report.pdf",
        sha256="s1",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/1",
    )
    repo.add(
        corpus_id="col_b",
        filename="Report-2026.docx",
        sha256="s2",
        file_type="docx",
        size_bytes=10,
        storage_path="/tmp/2",
    )
    repo.add(
        corpus_id="col_b",
        filename="unrelated.csv",
        sha256="s3",
        file_type="csv",
        size_bytes=10,
        storage_path="/tmp/3",
    )
    results = repo.search_across_corpora("report", limit=50)
    names = {r["filename"] for r in results}
    assert names == {"quarterly-report.pdf", "Report-2026.docx"}


def test_search_across_corpora_respects_limit(repo):
    for i in range(5):
        repo.add(
            corpus_id="col_a",
            filename=f"doc-{i}.pdf",
            sha256=f"s{i}",
            file_type="pdf",
            size_bytes=10,
            storage_path=f"/tmp/{i}",
        )
    results = repo.search_across_corpora("doc", limit=2)
    assert len(results) == 2


def test_search_across_corpora_blank_query_matches_nothing(repo):
    repo.add(
        corpus_id="col_a",
        filename="a.pdf",
        sha256="s1",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/1",
    )
    assert repo.search_across_corpora("", limit=50) == []
    assert repo.search_across_corpora("   ", limit=50) == []


# ---------------------------------------------------------------------------
# match_filenames — the RBAC-scoped counterpart to search_across_corpora,
# used by app/chat/document_links.py to resolve a `document:` citation.
# ---------------------------------------------------------------------------


def test_match_filenames_is_scoped_to_the_given_corpus_ids(repo):
    repo.add(
        corpus_id="col_a", filename="Report.pdf", sha256="s1", file_type="pdf", size_bytes=10, storage_path="/tmp/1"
    )
    repo.add(
        corpus_id="col_b", filename="Report.pdf", sha256="s2", file_type="pdf", size_bytes=10, storage_path="/tmp/2"
    )
    # Only col_a in scope: col_b's identically-named file must never surface.
    results = repo.match_filenames(["col_a"], ["report"], limit=20)
    assert {r["corpus_id"] for r in results} == {"col_a"}


def test_match_filenames_none_scope_means_unrestricted(repo):
    """``corpus_ids=None`` mirrors ``accessible_collection_ids``'s admin
    convention: no restriction, not "match nothing"."""
    repo.add(
        corpus_id="col_a", filename="Report.pdf", sha256="s1", file_type="pdf", size_bytes=10, storage_path="/tmp/1"
    )
    repo.add(
        corpus_id="col_b", filename="Report.pdf", sha256="s2", file_type="pdf", size_bytes=10, storage_path="/tmp/2"
    )
    results = repo.match_filenames(None, ["report"], limit=20)
    assert {r["corpus_id"] for r in results} == {"col_a", "col_b"}


def test_match_filenames_empty_corpus_list_matches_nothing(repo):
    """An empty (never ``None``) scope must not vacuously match everything."""
    repo.add(
        corpus_id="col_a", filename="Report.pdf", sha256="s1", file_type="pdf", size_bytes=10, storage_path="/tmp/1"
    )
    assert repo.match_filenames([], ["report"], limit=20) == []


def test_match_filenames_no_needles_matches_nothing(repo):
    repo.add(
        corpus_id="col_a", filename="Report.pdf", sha256="s1", file_type="pdf", size_bytes=10, storage_path="/tmp/1"
    )
    assert repo.match_filenames(["col_a"], [], limit=20) == []


def test_match_filenames_matches_any_of_several_needles(repo):
    repo.add(
        corpus_id="col_a", filename="Alpha.pdf", sha256="s1", file_type="pdf", size_bytes=10, storage_path="/tmp/1"
    )
    repo.add(corpus_id="col_a", filename="Beta.pdf", sha256="s2", file_type="pdf", size_bytes=10, storage_path="/tmp/2")
    results = repo.match_filenames(["col_a"], ["alpha", "beta"], limit=20)
    assert {r["filename"] for r in results} == {"Alpha.pdf", "Beta.pdf"}


def test_match_filenames_extra_file_ids_reaches_a_file_outside_corpus_scope(repo):
    """A per-file grant (``document_links.resolve_document_url``'s
    ``extra_file_ids``) must surface a file whose own corpus is NOT in
    ``corpus_ids`` — the whole point of the parameter."""
    fid = repo.add(
        corpus_id="col_private",
        filename="Shared.pdf",
        sha256="s1",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/1",
    )
    results = repo.match_filenames(["col_a"], ["shared"], extra_file_ids=[fid], limit=20)
    assert {r["id"] for r in results} == {fid}


def test_match_filenames_extra_file_ids_does_not_widen_beyond_the_named_files(repo):
    """A file in an out-of-scope corpus, NOT named in ``extra_file_ids``,
    must still never surface — the parameter grants specific files, not
    their whole corpus."""
    repo.add(
        corpus_id="col_private",
        filename="NotGranted.pdf",
        sha256="s1",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/1",
    )
    results = repo.match_filenames(["col_a"], ["notgranted"], extra_file_ids=["some-other-file-id"], limit=20)
    assert results == []


def test_match_filenames_empty_corpus_list_with_extra_file_ids_still_matches(repo):
    """``corpus_ids=[]`` alone means "nothing" (see the empty-scope test
    above), but paired with ``extra_file_ids`` it must not short-circuit —
    a caller with zero collection access but one per-file grant is exactly
    the case this exists for."""
    fid = repo.add(
        corpus_id="col_private",
        filename="Shared.pdf",
        sha256="s1",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/1",
    )
    results = repo.match_filenames([], ["shared"], extra_file_ids=[fid], limit=20)
    assert {r["id"] for r in results} == {fid}


def test_match_filenames_respects_limit(repo):
    for i in range(5):
        repo.add(
            corpus_id="col_a",
            filename=f"doc-{i}.pdf",
            sha256=f"s{i}",
            file_type="pdf",
            size_bytes=10,
            storage_path=f"/tmp/{i}",
        )
    results = repo.match_filenames(["col_a"], ["doc"], limit=2)
    assert len(results) == 2


def test_status_counts_for_corpora_groups_by_corpus_and_status_in_one_read(repo):
    """The batched sibling of ``count_by_corpus``: a caller with a LIST of
    corpus ids (e.g. a connection's confirmed scopes) gets every corpus's
    per-status breakdown in one call instead of walking ``list_for_corpus``
    once per scope."""
    for i in range(2):
        fid = repo.add(
            corpus_id="col_a",
            filename=f"a{i}.pdf",
            sha256=f"sha_a{i}",
            file_type="pdf",
            size_bytes=10,
            storage_path=f"/tmp/a{i}.pdf",
        )
        if i == 0:
            repo.set_status(fid, status="indexed")
    repo.add(
        corpus_id="col_b",
        filename="b.pdf",
        sha256="sha_b",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/b.pdf",
    )
    counts = repo.status_counts_for_corpora(["col_a", "col_b", "col_absent"])
    assert counts["col_a"] == {"indexed": 1, "pending": 1}
    assert counts["col_b"] == {"pending": 1}
    # A requested id with no files is simply absent, same contract as
    # ``count_by_corpus``.
    assert "col_absent" not in counts


def test_status_counts_for_corpora_only_counts_requested_ids(repo):
    """A corpus NOT in the requested list is never counted, even if it has
    files — this is a scoped read, not a global one."""
    repo.add(
        corpus_id="col_a",
        filename="a.pdf",
        sha256="sha_a",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/a.pdf",
    )
    repo.add(
        corpus_id="col_unrequested",
        filename="u.pdf",
        sha256="sha_u",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/u.pdf",
    )
    counts = repo.status_counts_for_corpora(["col_a"])
    assert set(counts) == {"col_a"}


def test_status_counts_for_corpora_empty_ids_returns_empty_dict(repo):
    repo.add(
        corpus_id="col_a",
        filename="a.pdf",
        sha256="sha_a",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/a.pdf",
    )
    assert repo.status_counts_for_corpora([]) == {}


def test_top_folder_status_counts_groups_by_first_path_segment(repo):
    """The completeness check's per-folder breakdown: a file under
    ``Reports/2024/q1.pdf`` buckets under ``Reports``, and a file with no
    ``/`` in its path buckets under ``""`` (the corpus-root bucket)."""
    a = repo.add(
        corpus_id="col_a",
        filename="q1.pdf",
        sha256="sha_a",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/a.pdf",
        path="Reports/2024/q1.pdf",
    )
    repo.set_status(a, status="indexed")
    b = repo.add(
        corpus_id="col_a",
        filename="q2.pdf",
        sha256="sha_b",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/b.pdf",
        path="Reports/2024/q2.pdf",
    )
    repo.set_status(b, status="rejected")
    repo.add(
        corpus_id="col_a",
        filename="readme.txt",
        sha256="sha_c",
        file_type="txt",
        size_bytes=10,
        storage_path="/tmp/c.pdf",
        path="readme.txt",
    )
    counts = repo.top_folder_status_counts("col_a")
    assert counts["Reports"] == {"indexed": 1, "rejected": 1}
    assert counts[""] == {"pending": 1}


def test_top_folder_status_counts_null_path_buckets_under_root(repo):
    repo.add(
        corpus_id="col_a",
        filename="no-path.pdf",
        sha256="sha_a",
        file_type="pdf",
        size_bytes=10,
        storage_path="/tmp/a.pdf",
    )
    counts = repo.top_folder_status_counts("col_a")
    assert counts[""] == {"pending": 1}


def test_top_folder_status_counts_unknown_corpus_returns_empty_dict(repo):
    assert repo.top_folder_status_counts("col_absent") == {}


def test_extension_status_counts_groups_by_extension_from_path_not_filename(repo):
    """The trap this method exists to avoid: `filename`/`file_type` name the
    STORED artifact (always markdown for a converted document), never the
    original file type. Two files whose `filename`/`file_type` both say
    "md" but whose real `path` extensions differ must bucket separately."""
    a = repo.add(
        corpus_id="col_a",
        filename="report.md",
        sha256="sha_a",
        file_type="md",
        size_bytes=100,
        storage_path="/tmp/a.md",
        path="Reports/2024/report.pdf",
    )
    repo.set_status(a, status="indexed")
    b = repo.add(
        corpus_id="col_a",
        filename="deck.md",
        sha256="sha_b",
        file_type="md",
        size_bytes=50,
        storage_path="/tmp/b.md",
        path="Slides/deck.pptx",
    )
    repo.set_status(b, status="rejected")
    counts = repo.extension_status_counts(["col_a"])
    assert counts["pdf"] == {"indexed": {"count": 1, "bytes": 100}}
    assert counts["pptx"] == {"rejected": {"count": 1, "bytes": 50}}


def test_extension_status_counts_buckets_extensionless_and_null_path_under_blank(repo):
    repo.add(
        corpus_id="col_a",
        filename="README.md",
        sha256="sha_a",
        file_type="md",
        size_bytes=5,
        storage_path="/tmp/a.md",
        path="README",
    )
    repo.add(
        corpus_id="col_a",
        filename="no-path.md",
        sha256="sha_b",
        file_type="md",
        size_bytes=7,
        storage_path="/tmp/b.md",
    )
    counts = repo.extension_status_counts(["col_a"])
    assert counts[""]["pending"]["count"] == 2
    assert counts[""]["pending"]["bytes"] == 12


def test_extension_status_counts_is_case_insensitive_and_scoped_across_ids(repo):
    a = repo.add(
        corpus_id="col_a",
        filename="a.md",
        sha256="sha_a",
        file_type="md",
        size_bytes=10,
        storage_path="/tmp/a.md",
        path="doc.PDF",
    )
    repo.set_status(a, status="indexed")
    b = repo.add(
        corpus_id="col_b",
        filename="b.md",
        sha256="sha_b",
        file_type="md",
        size_bytes=20,
        storage_path="/tmp/b.md",
        path="other.pdf",
    )
    repo.set_status(b, status="indexed")
    repo.add(
        corpus_id="col_unrequested",
        filename="c.md",
        sha256="sha_c",
        file_type="md",
        size_bytes=30,
        storage_path="/tmp/c.md",
        path="skip.pdf",
    )
    counts = repo.extension_status_counts(["col_a", "col_b"])
    assert counts["pdf"] == {"indexed": {"count": 2, "bytes": 30}}


def test_extension_status_counts_empty_ids_returns_empty_dict(repo):
    assert repo.extension_status_counts([]) == {}


# ---------------------------------------------------------------------------
# list_for_corpus / count_for_corpus — pagination + search (contract §1)
# ---------------------------------------------------------------------------


def _set_created_at(repo, file_id, ts: datetime) -> None:
    """Force a row's ``created_at`` directly, bypassing the DB default, so
    ordering/pagination tests are deterministic instead of racing the clock."""
    if hasattr(repo, "conn"):
        # DuckDB's TIMESTAMP column is naive.
        naive = ts.replace(tzinfo=None) if ts.tzinfo is not None else ts
        repo.conn.execute("UPDATE corpus_files SET created_at = ? WHERE id = ?", [naive, file_id])
    else:
        with repo._engine.begin() as conn:
            conn.execute(
                sa.text("UPDATE corpus_files SET created_at = :ts WHERE id = :id"),
                {"ts": ts, "id": file_id},
            )


def _seed_ordered(repo, n, corpus_id=CORPUS_ID, prefix="seed"):
    """Insert ``n`` files with strictly increasing ``created_at`` and return
    their ids oldest -> newest."""
    ids = []
    base = datetime.now(UTC)
    for i in range(n):
        fid = repo.add(
            corpus_id=corpus_id,
            filename=f"{prefix}{i}.txt",
            sha256=f"{prefix}-sha{i}",
            file_type=None,
            size_bytes=None,
            storage_path=None,
        )
        _set_created_at(repo, fid, base + timedelta(seconds=i))
        ids.append(fid)
    return ids


def test_list_for_corpus_default_call_is_backward_compatible(repo):
    """14 existing callers depend on the bare ``list_for_corpus(corpus_id)``
    call returning every row ordered by ``created_at`` ascending."""
    ids = _seed_ordered(repo, 3)
    rows = repo.list_for_corpus(CORPUS_ID)
    assert [r["id"] for r in rows] == ids


def test_list_for_corpus_limit_offset_paging(repo):
    ids = _seed_ordered(repo, 5)
    page1 = repo.list_for_corpus(CORPUS_ID, limit=2, offset=0)
    page2 = repo.list_for_corpus(CORPUS_ID, limit=2, offset=2)
    page3 = repo.list_for_corpus(CORPUS_ID, limit=2, offset=4)
    assert [r["id"] for r in page1] == ids[0:2]
    assert [r["id"] for r in page2] == ids[2:4]
    assert [r["id"] for r in page3] == ids[4:5]


def test_list_for_corpus_limit_none_means_no_limit(repo):
    ids = _seed_ordered(repo, 4)
    rows = repo.list_for_corpus(CORPUS_ID, limit=None)
    assert [r["id"] for r in rows] == ids


def test_list_for_corpus_stable_tiebreak_pages_through_identical_created_at(repo):
    """The single most important test here: files uploaded in one batch share
    a ``created_at``. Without an ``id`` tie-break, paging at limit=1 repeats
    or skips rows instead of covering the set exactly once."""
    ts = datetime.now(UTC)
    ids = []
    for i in range(4):
        fid = repo.add(
            corpus_id=CORPUS_ID,
            filename=f"batch{i}.txt",
            sha256=f"batch-sha{i}",
            file_type=None,
            size_bytes=None,
            storage_path=None,
        )
        _set_created_at(repo, fid, ts)
        ids.append(fid)

    seen = []
    for offset in range(4):
        page = repo.list_for_corpus(CORPUS_ID, limit=1, offset=offset)
        assert len(page) == 1
        seen.append(page[0]["id"])

    # Every row appears exactly once across the pages — no repeats, no skips.
    assert sorted(seen) == sorted(ids)
    assert len(set(seen)) == 4
    # The tie-break is ``id ASC``, so the page sequence is fully determined.
    assert seen == sorted(ids)
    # One shot with a real LIMIT agrees with the paged walk.
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, limit=4)] == sorted(ids)


def test_list_for_corpus_order_oldest_is_default(repo):
    ids = _seed_ordered(repo, 3)
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, order="oldest")] == ids
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID)] == ids


def test_list_for_corpus_order_newest(repo):
    ids = _seed_ordered(repo, 3)
    rows = repo.list_for_corpus(CORPUS_ID, order="newest")
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_list_for_corpus_order_name(repo):
    id_b = repo.add(
        corpus_id=CORPUS_ID, filename="Banana.txt", sha256="s1", file_type=None, size_bytes=None, storage_path=None
    )
    id_a = repo.add(
        corpus_id=CORPUS_ID, filename="apple.txt", sha256="s2", file_type=None, size_bytes=None, storage_path=None
    )
    id_c = repo.add(
        corpus_id=CORPUS_ID, filename="Cherry.txt", sha256="s3", file_type=None, size_bytes=None, storage_path=None
    )
    rows = repo.list_for_corpus(CORPUS_ID, order="name")
    # Case-insensitive: apple < Banana < Cherry.
    assert [r["id"] for r in rows] == [id_a, id_b, id_c]


def test_list_for_corpus_order_size_nulls_last(repo):
    id_big = repo.add(
        corpus_id=CORPUS_ID, filename="big.bin", sha256="s1", file_type=None, size_bytes=300, storage_path=None
    )
    id_small = repo.add(
        corpus_id=CORPUS_ID, filename="small.bin", sha256="s2", file_type=None, size_bytes=100, storage_path=None
    )
    id_null = repo.add(
        corpus_id=CORPUS_ID, filename="unknown.bin", sha256="s3", file_type=None, size_bytes=None, storage_path=None
    )
    rows = repo.list_for_corpus(CORPUS_ID, order="size")
    assert [r["id"] for r in rows] == [id_big, id_small, id_null]


def test_list_for_corpus_unknown_order_falls_back_to_oldest(repo):
    """An unknown ``order`` value never raises and never reaches SQL as text —
    it must be mapped through a literal dict, so even a string shaped like an
    injection attempt just falls back to the default ordering."""
    ids = _seed_ordered(repo, 3)
    rows = repo.list_for_corpus(CORPUS_ID, order="not-a-real-order; DROP TABLE corpus_files;--")
    assert [r["id"] for r in rows] == ids


def test_list_for_corpus_q_matches_filename_substring(repo):
    hit = repo.add(
        corpus_id=CORPUS_ID,
        filename="quarterly_report_final.pdf",
        sha256="s1",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    miss = repo.add(
        corpus_id=CORPUS_ID, filename="notes.txt", sha256="s2", file_type=None, size_bytes=None, storage_path=None
    )
    rows = repo.list_for_corpus(CORPUS_ID, q="report")
    ids = {r["id"] for r in rows}
    assert hit in ids
    assert miss not in ids
    # Case-insensitive.
    rows_upper = repo.list_for_corpus(CORPUS_ID, q="REPORT")
    assert hit in {r["id"] for r in rows_upper}


def test_list_for_corpus_q_matches_path_substring(repo):
    hit = repo.add(
        corpus_id=CORPUS_ID,
        filename="x.md",
        sha256="s1",
        file_type=None,
        size_bytes=None,
        storage_path=None,
        path="apis/storage-api.md",
    )
    miss = repo.add(
        corpus_id=CORPUS_ID,
        filename="y.md",
        sha256="s2",
        file_type=None,
        size_bytes=None,
        storage_path=None,
        path="notes/misc.md",
    )
    rows = repo.list_for_corpus(CORPUS_ID, q="storage-api")
    ids = {r["id"] for r in rows}
    assert hit in ids
    assert miss not in ids


def test_list_for_corpus_q_escapes_like_metacharacters(repo):
    """``q`` is untrusted: a literal ``%``/``_`` in the search term must match
    literally rather than acting as a SQL wildcard."""
    literal = repo.add(
        corpus_id=CORPUS_ID,
        filename="report_v2.pdf",
        sha256="s1",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    decoy = repo.add(
        corpus_id=CORPUS_ID,
        filename="reportXv2.pdf",
        sha256="s2",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    rows = repo.list_for_corpus(CORPUS_ID, q="report_v2")
    ids = {r["id"] for r in rows}
    assert literal in ids
    assert decoy not in ids

    percent_literal = repo.add(
        corpus_id=CORPUS_ID,
        filename="100%done.txt",
        sha256="s3",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    percent_decoy = repo.add(
        corpus_id=CORPUS_ID,
        filename="100Xdone.txt",
        sha256="s4",
        file_type=None,
        size_bytes=None,
        storage_path=None,
    )
    rows_pct = repo.list_for_corpus(CORPUS_ID, q="100%done")
    ids_pct = {r["id"] for r in rows_pct}
    assert percent_literal in ids_pct
    assert percent_decoy not in ids_pct


def test_list_for_corpus_blank_q_and_status_mean_no_filter(repo):
    """A prior bug of exactly this shape narrowed a search to nothing on a
    blank input — blank must behave identically to ``None``, never "match
    nothing"."""
    ids = _seed_ordered(repo, 3)
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, q=None)] == ids
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, q="")] == ids
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, q="   ")] == ids
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, status=None)] == ids
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, status="")] == ids
    assert [r["id"] for r in repo.list_for_corpus(CORPUS_ID, status="   ")] == ids


def test_list_for_corpus_status_exact_match(repo):
    a = repo.add(corpus_id=CORPUS_ID, filename="a.pdf", sha256="s1", file_type=None, size_bytes=None, storage_path=None)
    b = repo.add(corpus_id=CORPUS_ID, filename="b.pdf", sha256="s2", file_type=None, size_bytes=None, storage_path=None)
    repo.set_status(a, status="indexed")
    repo.set_status(b, status="needs_review")
    rows = repo.list_for_corpus(CORPUS_ID, status="indexed")
    assert [r["id"] for r in rows] == [a]


def test_count_for_corpus_matches_list_length_under_filters(repo):
    ids = _seed_ordered(repo, 5, prefix="batch")
    repo.set_status(ids[0], status="indexed")
    repo.set_status(ids[1], status="indexed")

    assert repo.count_for_corpus(CORPUS_ID) == 5
    assert repo.count_for_corpus(CORPUS_ID) == len(repo.list_for_corpus(CORPUS_ID))

    assert repo.count_for_corpus(CORPUS_ID, status="indexed") == 2
    assert repo.count_for_corpus(CORPUS_ID, status="indexed") == len(repo.list_for_corpus(CORPUS_ID, status="indexed"))

    assert repo.count_for_corpus(CORPUS_ID, q="batch") == 5
    assert repo.count_for_corpus(CORPUS_ID, q="batch") == len(repo.list_for_corpus(CORPUS_ID, q="batch"))

    assert repo.count_for_corpus(CORPUS_ID, q="batch", status="indexed") == 2
    assert repo.count_for_corpus(CORPUS_ID, q="batch", status="indexed") == len(
        repo.list_for_corpus(CORPUS_ID, q="batch", status="indexed")
    )


def test_count_for_corpus_zero_when_no_match(repo):
    _seed_ordered(repo, 2)
    assert repo.count_for_corpus(CORPUS_ID, q="no-such-substring-anywhere") == 0
    assert repo.count_for_corpus("col_nonexistent") == 0


# ---------------------------------------------------------------------------
# filenames_for_ids — bulk-by-ids citation lookup (retrieval cost fix, 2026-09)
# ---------------------------------------------------------------------------


def test_filenames_for_ids_returns_only_the_requested_ids(repo):
    a = repo.add(corpus_id=CORPUS_ID, filename="alpha.md", sha256="a", file_type="md", size_bytes=1, storage_path="/a")
    b = repo.add(corpus_id=CORPUS_ID, filename="beta.md", sha256="b", file_type="md", size_bytes=1, storage_path="/b")
    repo.add(corpus_id=CORPUS_ID, filename="gamma.md", sha256="c", file_type="md", size_bytes=1, storage_path="/c")

    result = repo.filenames_for_ids([a, b])

    assert result == {a: "alpha.md", b: "beta.md"}


def test_filenames_for_ids_tolerates_unknown_ids(repo):
    a = repo.add(corpus_id=CORPUS_ID, filename="known.md", sha256="a", file_type="md", size_bytes=1, storage_path="/a")

    result = repo.filenames_for_ids([a, "cf_does_not_exist"])

    # A requested id with no matching row is simply absent — never an error,
    # never a placeholder entry.
    assert result == {a: "known.md"}


def test_filenames_for_ids_empty_input_returns_empty_dict(repo):
    repo.add(corpus_id=CORPUS_ID, filename="a.md", sha256="a", file_type="md", size_bytes=1, storage_path="/a")
    assert repo.filenames_for_ids([]) == {}


def test_filenames_for_ids_all_unknown_returns_empty_dict(repo):
    assert repo.filenames_for_ids(["cf_nope1", "cf_nope2"]) == {}
