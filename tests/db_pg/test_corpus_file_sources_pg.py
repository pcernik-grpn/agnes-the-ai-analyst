"""Postgres-only tests for the ``corpus_file_sources`` repository.

There is no DuckDB half to parametrize against (PG-first ratchet, A3) — see
``docs/migrations.md`` -> "Adding a PG-only feature". Pattern follows
``tests/db_pg/test_corpus_files_contract.py``'s PG construction helper.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ID = "col_test"


def _make_repo(pg_engine, monkeypatch):
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

    from src.repositories.corpus_files_pg import CorpusFilesPgRepository
    from src.repositories.corpus_file_sources_pg import CorpusFileSourcesPgRepository

    return (
        CorpusFileSourcesPgRepository(db_pg.get_engine()),
        CorpusFilesPgRepository(db_pg.get_engine()),
    )


def _add_file(cf_repo, *, sha256="s1", path=None):
    return cf_repo.add(
        corpus_id=CORPUS_ID,
        filename="a.md",
        sha256=sha256,
        file_type="md",
        size_bytes=10,
        storage_path="/blobs/a.md",
        path=path,
    )


def test_resolve_returns_none_when_unmapped(pg_engine, monkeypatch):
    repo, _cf = _make_repo(pg_engine, monkeypatch)
    assert repo.resolve(CORPUS_ID, "graph:missing") is None


def test_upsert_then_resolve_round_trips(pg_engine, monkeypatch):
    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    file_id = _add_file(cf_repo)
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
        source_doc_id="doc1",
        source_sha256="sha1",
    )
    assert repo.resolve(CORPUS_ID, "graph:abc123") == file_id


def test_upsert_is_scoped_per_corpus(pg_engine, monkeypatch):
    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    file_id = _add_file(cf_repo)
    repo.upsert(corpus_file_id=file_id, corpus_id=CORPUS_ID, source_stable_id="graph:abc123")
    assert repo.resolve("col_other", "graph:abc123") is None


def test_get_returns_full_row(pg_engine, monkeypatch):
    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    file_id = _add_file(cf_repo)
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
        source_doc_id="doc1",
        source_sha256="sha1",
        source_url="https://example.com/doc",
    )
    row = repo.get(file_id)
    assert row is not None
    assert row["corpus_id"] == CORPUS_ID
    assert row["source_stable_id"] == "graph:abc123"
    assert row["source_doc_id"] == "doc1"
    assert row["source_sha256"] == "sha1"
    assert row["source_url"] == "https://example.com/doc"


def test_get_returns_none_when_missing(pg_engine, monkeypatch):
    repo, _cf = _make_repo(pg_engine, monkeypatch)
    assert repo.get("cf_nonexistent") is None


def test_upsert_refreshes_existing_mapping_same_corpus_file_id(pg_engine, monkeypatch):
    """Second upsert on the same corpus_file_id updates in place (doc_id
    rewritten when a provisional id is replaced — spec §6)."""
    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    file_id = _add_file(cf_repo)
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
        source_doc_id="provisional",
    )
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
        source_doc_id="real-doc-id",
        source_sha256="sha-final",
    )
    row = repo.get(file_id)
    assert row["source_doc_id"] == "real-doc-id"
    assert row["source_sha256"] == "sha-final"
    # Still only one mapping row for this file.
    assert repo.resolve(CORPUS_ID, "graph:abc123") == file_id


def test_upsert_omitting_optional_fields_preserves_them(pg_engine, monkeypatch):
    """`source_doc_ids`/`source_sha256s` are independently optional on the
    upload endpoint, so a rename-only re-sync carries `source_stable_ids` and
    nothing else. Writing EXCLUDED unconditionally reset a real
    `source_doc_id` back to NULL — breaking the very lookup its index exists
    for (`facts` ingest resolves a doc_id through this column). Omitted now
    means "leave alone"; the wire format has no way to say "clear it"
    (Devin Review on #1655)."""
    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    file_id = _add_file(cf_repo)
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
        source_doc_id="real-doc-id",
        source_sha256="crawler-sha",
    )

    # A rename-only delta: stable id only.
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
    )

    row = repo.get(file_id)
    assert row["source_doc_id"] == "real-doc-id", "an omitted doc_id must not null a stored one"
    assert row["source_sha256"] == "crawler-sha"

    # ...while a supplied value still overwrites (the provisional -> real
    # rewrite of spec §6 must keep working).
    repo.upsert(
        corpus_file_id=file_id,
        corpus_id=CORPUS_ID,
        source_stable_id="graph:abc123",
        source_doc_id="newer-doc-id",
    )
    assert repo.get(file_id)["source_doc_id"] == "newer-doc-id"


def test_unique_constraint_rejects_second_file_same_stable_id(pg_engine, monkeypatch):
    """(corpus_id, source_stable_id) is unique — a distinct corpus_file_id
    cannot claim a stable id another row already anchors."""
    import pytest

    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    first = _add_file(cf_repo, sha256="s1")
    second = _add_file(cf_repo, sha256="s2")
    repo.upsert(corpus_file_id=first, corpus_id=CORPUS_ID, source_stable_id="graph:dup")
    with pytest.raises(Exception):
        repo.upsert(corpus_file_id=second, corpus_id=CORPUS_ID, source_stable_id="graph:dup")


def test_cascade_delete_removes_mapping(pg_engine, monkeypatch):
    """corpus_file_id FK ON DELETE CASCADE: deleting the corpus_files row
    removes its source mapping too."""
    repo, cf_repo = _make_repo(pg_engine, monkeypatch)
    file_id = _add_file(cf_repo)
    repo.upsert(corpus_file_id=file_id, corpus_id=CORPUS_ID, source_stable_id="graph:abc123")
    cf_repo.delete(file_id)
    assert repo.get(file_id) is None
    assert repo.resolve(CORPUS_ID, "graph:abc123") is None
