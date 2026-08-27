"""Cross-engine contract tests for the corpus_files repository.

Parametrises over [DuckDB impl, Postgres impl]. The same calls go to
both backends; the same return shapes must come back.
"""

from __future__ import annotations

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
    import src.db_pg as db_pg

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
