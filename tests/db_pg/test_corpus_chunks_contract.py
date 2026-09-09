"""Cross-engine contract tests for the ``corpus_chunks`` repository.

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
    from src.repositories.corpus_chunks import CorpusChunksRepository

    conn = duckdb.connect(str(tmp_path / "duck.duckdb"))
    _ensure_schema(conn)
    # seed parent corpus + file rows (not FK-constrained in DuckDB, but use
    # real ids for realism)
    conn.execute("INSERT INTO file_corpora (id, slug, name, created_by) VALUES ('col_cc', 'cc', 'CC', 'u')")
    conn.execute(
        "INSERT INTO corpus_files "
        "(id, corpus_id, filename, sha256, file_type) "
        "VALUES ('cf_cc1', 'col_cc', 'doc.txt', 'abc', 'txt')"
    )
    return CorpusChunksRepository(conn), conn


def _make_pg_repo(pg_engine, monkeypatch):
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["sqlalchemy.url"] = str(pg_engine.url)
    command.upgrade(cfg, "head")

    with pg_engine.begin() as conn:
        conn.execute(
            sa.text("INSERT INTO file_corpora (id, slug, name, created_by) VALUES (:id, :slug, :name, :by)"),
            {"id": "col_cc", "slug": "cc", "name": "CC", "by": "u"},
        )
        conn.execute(
            sa.text(
                "INSERT INTO corpus_files "
                "(id, corpus_id, filename, sha256, file_type) "
                "VALUES (:id, :corpus_id, :filename, :sha256, :ft)"
            ),
            {
                "id": "cf_cc1",
                "corpus_id": "col_cc",
                "filename": "doc.txt",
                "sha256": "abc",
                "ft": "txt",
            },
        )

    monkeypatch.setenv("AGNES_DB_URL", str(pg_engine.url))
    import src.db_pg as db_pg

    db_pg.dispose()
    db_pg.get_engine()

    from src.repositories.corpus_chunks_pg import CorpusChunksPgRepository

    return CorpusChunksPgRepository(db_pg.get_engine()), None


@pytest.fixture(params=["duckdb", "pg"])
def repo(request, tmp_path, pg_engine, monkeypatch):
    """Yields a corpus_chunks repo bound to either DuckDB or PG."""
    backend = request.param
    if backend == "duckdb":
        repo, conn = _make_duckdb_repo(tmp_path)
        yield repo
        if conn is not None:
            conn.close()
    else:
        repo, _ = _make_pg_repo(pg_engine, monkeypatch)
        yield repo


@pytest.fixture
def pg_repo(pg_engine, monkeypatch):
    """PG-only variant of ``repo`` — for ``search_candidates`` behavior that
    is Postgres-specific (``ts_rank_cd`` ranking; the DuckDB sibling doesn't
    rank at all, see its docstring), like the ranking-cap tests below."""
    repo, _ = _make_pg_repo(pg_engine, monkeypatch)
    return repo


# ---------------------------------------------------------------------------
# contract tests
# ---------------------------------------------------------------------------

CORPUS_ID = "col_cc"
FILE_ID = "cf_cc1"


def test_add_many_then_list_for_file_round_trips(repo):
    chunks = [
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "Hello world"},
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "Second chunk"},
    ]
    n = repo.add_many(chunks)
    assert n == 2

    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 2
    texts = [r["text"] for r in rows]
    assert "Hello world" in texts
    assert "Second chunk" in texts


def test_ordinal_ordering_preserved(repo):
    chunks = [
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 2, "text": "Third"},
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "First"},
        {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "Second"},
    ]
    repo.add_many(chunks)
    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 3
    assert rows[0]["ordinal"] == 0
    assert rows[1]["ordinal"] == 1
    assert rows[2]["ordinal"] == 2


def test_embedding_column_is_none_on_read(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "no embed"}])
    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 1
    assert rows[0]["embedding"] is None


def test_optional_fields_round_trip(repo):
    repo.add_many(
        [
            {
                "corpus_id": CORPUS_ID,
                "file_id": FILE_ID,
                "ordinal": 0,
                "text": "section text",
                "section_path": "Chapter 1 > Intro",
                "page": 3,
                "bbox": "0,0,100,200",
                "metadata": '{"source": "test"}',
            }
        ]
    )
    rows = repo.list_for_file(FILE_ID)
    assert len(rows) == 1
    r = rows[0]
    assert r["section_path"] == "Chapter 1 > Intro"
    assert r["page"] == 3
    assert r["bbox"] == "0,0,100,200"
    assert r["metadata"] == '{"source": "test"}'


def test_list_for_corpus_returns_all_file_chunks(repo):
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "chunk A"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "chunk B"},
        ]
    )
    rows = repo.list_for_corpus(CORPUS_ID)
    assert len(rows) >= 2
    texts = [r["text"] for r in rows]
    assert "chunk A" in texts
    assert "chunk B" in texts


def test_list_for_file_empty_when_no_chunks(repo):
    assert repo.list_for_file("cf_nonexistent") == []


def test_list_for_corpus_empty_when_no_chunks(repo):
    assert repo.list_for_corpus("col_nonexistent") == []


# ---------------------------------------------------------------------------
# list_for_corpus_batch (TCRD-296 synthesis C.15 — bounded reads)
# ---------------------------------------------------------------------------


def test_list_for_corpus_batch_pages_through_in_id_order(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": f"row {i}"} for i in range(5)])
    all_ids = {r["id"] for r in repo.list_for_corpus(CORPUS_ID)}

    seen: list[str] = []
    after_id = None
    while True:
        page = repo.list_for_corpus_batch(CORPUS_ID, after_id=after_id, limit=2)
        if not page:
            break
        seen.extend(r["id"] for r in page)
        after_id = page[-1]["id"]
        if len(page) < 2:
            break

    assert set(seen) == all_ids
    assert len(seen) == len(all_ids)  # no duplicate/missed row across pages
    assert seen == sorted(seen)  # ascending id order, the pagination cursor


def test_list_for_corpus_batch_respects_limit(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": f"row {i}"} for i in range(5)])
    page = repo.list_for_corpus_batch(CORPUS_ID, limit=2)
    assert len(page) == 2


def test_list_for_corpus_batch_empty_when_no_chunks(repo):
    assert repo.list_for_corpus_batch("col_nonexistent", limit=10) == []


def test_list_for_corpus_batch_scoped_to_given_corpus(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "in"}])
    repo.add_many([{"corpus_id": "col_other", "file_id": "cf_other", "ordinal": 0, "text": "out"}])
    page = repo.list_for_corpus_batch(CORPUS_ID, limit=10)
    assert len(page) == 1
    assert page[0]["text"] == "in"


def test_list_for_corpus_batch_carries_embedding(repo):
    """Unlike the column-pruned candidate fetches, a full-corpus batch read
    (used by knowledge packaging to build the artifact's own chunks table)
    carries the embedding, matching ``list_for_corpus``."""
    vec = [0.02 * i for i in range(384)]
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "v", "embedding": vec}])
    page = repo.list_for_corpus_batch(CORPUS_ID, limit=10)
    assert len(page) == 1
    assert len(page[0]["embedding"]) == 384


def test_delete_for_file_removes_chunks(repo):
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "to remove"},
        ]
    )
    assert len(repo.list_for_file(FILE_ID)) == 1
    repo.delete_for_file(FILE_ID)
    assert repo.list_for_file(FILE_ID) == []


def test_delete_for_file_missing_is_noop(repo):
    # Should not raise
    repo.delete_for_file("cf_nonexistent")


def test_add_many_empty_list_returns_zero(repo):
    n = repo.add_many([])
    assert n == 0


def test_embedding_round_trips(repo):
    vec = [0.01 * i for i in range(384)]
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "v", "embedding": vec}])
    row = repo.list_for_file(FILE_ID)[0]
    stored = row["embedding"]
    assert stored is not None
    assert len(stored) == 384
    assert abs(stored[1] - 0.01) < 1e-6


def test_wrong_dim_embedding_rejected(repo):
    import pytest

    with pytest.raises(Exception):
        repo.add_many(
            [{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "v", "embedding": [0.1, 0.2]}]
        )


def test_list_for_corpora_spans_multiple(repo):
    repo.add_many([{"corpus_id": "col_a", "file_id": "cf_a", "ordinal": 0, "text": "aa"}])
    repo.add_many([{"corpus_id": "col_b", "file_id": "cf_b", "ordinal": 0, "text": "bb"}])
    rows = repo.list_for_corpora(["col_a", "col_b"])
    corpora = {r["corpus_id"] for r in rows}
    assert {"col_a", "col_b"} <= corpora
    assert repo.list_for_corpora([]) == []


# ---------------------------------------------------------------------------
# search_candidates / search_by_filename (P0 OOM fix, 2026-09)
# ---------------------------------------------------------------------------


def _files_repo_for(repo):
    """A ``corpus_files`` repo bound to the SAME connection/engine the
    ``corpus_chunks`` ``repo`` fixture already holds — needed to seed real
    ``corpus_files`` rows for ``search_by_filename``'s JOIN."""
    if hasattr(repo, "conn"):
        from src.repositories.corpus_files import CorpusFilesRepository

        return CorpusFilesRepository(repo.conn)
    from src.repositories.corpus_files_pg import CorpusFilesPgRepository

    return CorpusFilesPgRepository(repo._engine)


def test_search_candidates_matches_lexically_and_is_bounded_by_limit(repo):
    for i in range(5):
        repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": f"shared keyword apple {i}"}])
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 99, "text": "no overlap at all"}])

    all_matches = repo.search_candidates([CORPUS_ID], "apple", limit=100)
    assert len(all_matches) == 5
    assert all("apple" in r["text"] for r in all_matches)

    capped = repo.search_candidates([CORPUS_ID], "apple", limit=2)
    assert len(capped) == 2


def test_search_candidates_empty_when_no_lexical_match(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "alpha bravo"}])
    assert repo.search_candidates([CORPUS_ID], "nosuchwordanywhere", limit=10) == []


def test_search_candidates_empty_corpus_ids_or_query(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "alpha bravo"}])
    assert repo.search_candidates([], "alpha", limit=10) == []
    assert repo.search_candidates([CORPUS_ID], "   ", limit=10) == []


def test_search_candidates_scoped_to_given_corpora(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "shared apple"}])
    repo.add_many([{"corpus_id": "col_other", "file_id": "cf_other", "ordinal": 0, "text": "shared apple"}])
    rows = repo.search_candidates([CORPUS_ID], "apple", limit=10)
    assert rows
    assert all(r["corpus_id"] == CORPUS_ID for r in rows)


def test_search_by_filename_matches_files_and_is_bounded_by_limit(repo):
    files_repo = _files_repo_for(repo)
    for i in range(5):
        fid = files_repo.add(
            corpus_id=CORPUS_ID,
            filename=f"quarterly-report-{i}.md",
            sha256="s",
            file_type="md",
            size_bytes=1,
            storage_path="/x",
        )
        repo.add_many([{"corpus_id": CORPUS_ID, "file_id": fid, "ordinal": 0, "text": "unrelated body text"}])

    all_matches = repo.search_by_filename([CORPUS_ID], ["quarterly", "report"], limit=100)
    assert len(all_matches) == 5

    capped = repo.search_by_filename([CORPUS_ID], ["quarterly", "report"], limit=2)
    assert len(capped) == 2


def test_search_by_filename_ignores_non_matching_names(repo):
    files_repo = _files_repo_for(repo)
    fid = files_repo.add(
        corpus_id=CORPUS_ID, filename="notes.md", sha256="s", file_type="md", size_bytes=1, storage_path="/x"
    )
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": fid, "ordinal": 0, "text": "alpha bravo"}])
    assert repo.search_by_filename([CORPUS_ID], ["quarterly", "report"], limit=10) == []


def test_search_by_filename_requires_the_file_row_itself_to_be_in_scope(repo):
    """A name hit needs the file's CURRENT collection in scope, not only the
    chunk rows': ``move_to_corpus`` re-homes the file row while stale chunk
    rows may still carry the old ``corpus_id``, and the moved file must stop
    answering by name under the collection it left (fail-closed, both
    backends — see ``search_by_filename``'s docstring)."""
    files_repo = _files_repo_for(repo)
    fid = files_repo.add(
        corpus_id=CORPUS_ID,
        filename="quarterly-report.md",
        sha256="s",
        file_type="md",
        size_bytes=1,
        storage_path="/x",
    )
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": fid, "ordinal": 0, "text": "alpha"}])
    assert repo.search_by_filename([CORPUS_ID], ["quarterly"], limit=10)

    assert files_repo.move_to_corpus(fid, "col_elsewhere")

    assert repo.search_by_filename([CORPUS_ID], ["quarterly"], limit=10) == []
    assert repo.search_by_filename(["col_elsewhere"], ["quarterly"], limit=10) == []
    # Back in scope once BOTH the file's collection and the chunks' are granted.
    hits = repo.search_by_filename([CORPUS_ID, "col_elsewhere"], ["quarterly"], limit=10)
    assert [h["file_id"] for h in hits] == [fid]


def test_search_candidates_row_shape_is_the_column_pruned_set_on_both_backends(repo):
    """The candidate row shape is pinned across backends: the PG side's
    stored ``tsv`` column (migration ``0114_corpus_chunks_tsv``) is a
    ranking input, never part of the returned dict."""
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "apple pie"}])
    (row,) = repo.search_candidates([CORPUS_ID], "apple", limit=10)
    assert set(row) == {
        "id",
        "corpus_id",
        "file_id",
        "ordinal",
        "text",
        "section_path",
        "page",
        "bbox",
        "metadata",
        "created_at",
        "embedding",
    }


def test_search_by_filename_empty_terms_or_corpus_ids(repo):
    files_repo = _files_repo_for(repo)
    fid = files_repo.add(
        corpus_id=CORPUS_ID,
        filename="quarterly-report.md",
        sha256="s",
        file_type="md",
        size_bytes=1,
        storage_path="/x",
    )
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": fid, "ordinal": 0, "text": "alpha"}])
    assert repo.search_by_filename([], ["quarterly"], limit=10) == []
    assert repo.search_by_filename([CORPUS_ID], [], limit=10) == []


# #2151 hardening: column-pruned fetch, cheap COUNT, shortlist embeddings,
# SQL-side lexical prefilter + LIMIT cap.
# ---------------------------------------------------------------------------


def test_list_for_corpora_never_returns_a_stored_embedding(repo):
    """``list_for_corpora`` is the retrieval candidate-set fetch — it must
    never bring the (potentially large) ``embedding`` column back, even for
    a chunk that has one stored. Phase 2 (``list_embeddings_for_ids``) is the
    only path that fetches vectors, and only for a caller-chosen id set."""
    vec = [0.01 * i for i in range(384)]
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "v", "embedding": vec}])
    rows = repo.list_for_corpora([CORPUS_ID])
    assert len(rows) == 1
    assert rows[0]["embedding"] is None
    # The other columns are unaffected by column pruning.
    assert rows[0]["text"] == "v"
    assert rows[0]["id"]


def test_count_for_corpora_matches_row_count(repo):
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "a"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "b"},
        ]
    )
    assert repo.count_for_corpora([CORPUS_ID]) == 2


def test_count_for_corpora_spans_multiple_and_empty_is_zero(repo):
    repo.add_many([{"corpus_id": "col_a", "file_id": "cf_a", "ordinal": 0, "text": "aa"}])
    repo.add_many([{"corpus_id": "col_b", "file_id": "cf_b", "ordinal": 0, "text": "bb"}])
    assert repo.count_for_corpora(["col_a", "col_b"]) == 2
    assert repo.count_for_corpora([]) == 0
    assert repo.count_for_corpora(["col_nonexistent"]) == 0


def test_list_embeddings_for_ids_returns_only_requested_ids_with_vectors(repo):
    vec = [0.02 * i for i in range(384)]
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "has vector", "embedding": vec},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "no vector"},
        ]
    )
    rows = repo.list_for_corpora([CORPUS_ID])
    by_text = {r["text"]: r["id"] for r in rows}
    embedded_id, bare_id = by_text["has vector"], by_text["no vector"]

    out = repo.list_embeddings_for_ids([embedded_id, bare_id, "ck_doesnotexist"])
    assert set(out.keys()) == {embedded_id}
    assert len(out[embedded_id]) == 384
    assert abs(out[embedded_id][1] - 0.02) < 1e-6


def test_list_embeddings_for_ids_empty_input_returns_empty_dict(repo):
    assert repo.list_embeddings_for_ids([]) == {}


def test_list_for_corpora_query_terms_prefilter_is_any_term_ilike(repo):
    """The SQL-side lexical prefilter (used once a corpus is over the
    server's chunk cap) keeps a chunk whose text contains ANY listed term —
    a conservative, over-inclusive filter safe for a prefilter (the exact,
    whole-word ranking still runs in Python over whatever this returns)."""
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "kubernetes cluster"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "unrelated weather report"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 2, "text": "another unrelated row"},
        ]
    )
    rows = repo.list_for_corpora([CORPUS_ID], query_terms=["kubernetes"])
    assert len(rows) == 1
    assert rows[0]["text"] == "kubernetes cluster"

    # Case-insensitive, and ANY (not ALL) listed term matches.
    rows_ci = repo.list_for_corpora([CORPUS_ID], query_terms=["KUBERNETES", "weather"])
    assert {r["text"] for r in rows_ci} == {"kubernetes cluster", "unrelated weather report"}


def test_list_for_corpora_limit_caps_row_count(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": f"row {i}"} for i in range(5)])
    rows = repo.list_for_corpora([CORPUS_ID], limit=2)
    assert len(rows) == 2


def test_list_for_corpora_no_query_terms_or_limit_is_unfiltered(repo):
    """No prefilter, no cap — behavior identical to the pre-#2151
    unfiltered fetch, minus embeddings."""
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": f"row {i}"} for i in range(5)])
    rows = repo.list_for_corpora([CORPUS_ID])
    assert len(rows) == 5


def test_search_candidates_and_search_by_filename_never_return_a_stored_embedding(repo):
    """The bounded candidate fetches on the search path are column-pruned
    exactly like ``list_for_corpora`` (#2151 × P0 OOM fix): ``embedding`` is
    always ``None`` on their rows even for a chunk that has one stored —
    ``list_embeddings_for_ids`` is the only path that brings vectors back,
    for the retrieval layer's bounded shortlist."""
    files_repo = _files_repo_for(repo)
    fid = files_repo.add(
        corpus_id=CORPUS_ID, filename="quarterly-report.md", sha256="s", file_type="md", size_bytes=1, storage_path="/x"
    )
    vec = [0.03 * i for i in range(384)]
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": fid, "ordinal": 0, "text": "apple pie", "embedding": vec}])

    body = repo.search_candidates([CORPUS_ID], "apple", limit=10)
    assert len(body) == 1 and body[0]["embedding"] is None and body[0]["text"] == "apple pie"

    by_name = repo.search_by_filename([CORPUS_ID], ["quarterly"], limit=10)
    assert len(by_name) == 1 and by_name[0]["embedding"] is None and by_name[0]["file_id"] == fid

    # The pruned row's id still resolves to its stored vector in phase 2.
    assert len(repo.list_embeddings_for_ids([body[0]["id"]])[body[0]["id"]]) == 384


# ---------------------------------------------------------------------------
# search_candidates ranking-cap fix (TCRD-296 gap #69, PG-only — the
# DuckDB sibling has no ``ts_rank_cd`` ranking to bound, see its docstring)
# ---------------------------------------------------------------------------


def test_search_candidates_ranks_by_relevance_when_matches_are_under_the_cap(pg_repo):
    """Below ``rank_cap`` the inner subquery's own LIMIT never trims
    anything, so ranking is unaffected by the fix: a chunk repeating the
    query term more often still ranks first."""
    pg_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "contract"}])
    pg_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "contract contract contract"}])
    pg_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 2, "text": "a contract mentioned once"}])

    rows = pg_repo.search_candidates([CORPUS_ID], "contract", limit=10)
    assert len(rows) == 3
    assert rows[0]["text"] == "contract contract contract"


def test_search_candidates_returns_exactly_limit_rows_when_matches_exceed_the_rank_cap(pg_repo, monkeypatch):
    """Correctness under cap: with more matching rows than ``rank_cap``,
    the query still returns exactly ``limit`` rows — never a crash, never
    a short result. The module constants are monkeypatched down so the
    test doesn't need to insert 20 000+ rows to exercise the branch."""
    import src.repositories.corpus_chunks_pg as corpus_chunks_pg_module

    monkeypatch.setattr(corpus_chunks_pg_module, "_RANK_CANDIDATE_FLOOR", 5)
    monkeypatch.setattr(corpus_chunks_pg_module, "_RANK_CANDIDATE_MULTIPLIER", 1)

    for i in range(20):
        pg_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": f"contract text {i}"}])

    rows = pg_repo.search_candidates([CORPUS_ID], "contract", limit=3)
    assert len(rows) == 3
    assert all("contract" in r["text"] for r in rows)


# ---------------------------------------------------------------------------
# reassign_file_corpus — the single-file move path's chunk re-homing
# ---------------------------------------------------------------------------


def test_reassign_file_corpus_rehomes_only_that_files_chunks(repo):
    """Moving a file between collections must carry its chunks along:
    ``corpus_chunks.corpus_id`` is the column body search scopes on, so a
    chunk left behind keeps answering under the collection the file just
    left. Only the moved file's rows move; a sibling file's stay put."""
    repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "relocated body zebra"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": "relocated body zebra two"},
        ]
    )
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": "cf_stays", "ordinal": 0, "text": "staying body zebra"}])

    moved = repo.reassign_file_corpus(FILE_ID, "col_target")

    assert moved == 2
    assert [r["corpus_id"] for r in repo.list_for_file(FILE_ID)] == ["col_target", "col_target"]
    assert [r["file_id"] for r in repo.list_for_corpus(CORPUS_ID)] == ["cf_stays"]
    # Body search follows the move: nothing of the file under the source,
    # all of it under the target.
    assert {r["file_id"] for r in repo.search_candidates([CORPUS_ID], "relocated", limit=10)} == set()
    assert {r["file_id"] for r in repo.search_candidates(["col_target"], "relocated", limit=10)} == {FILE_ID}


def test_reassign_file_corpus_unknown_file_is_zero(repo):
    repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "untouched"}])
    assert repo.reassign_file_corpus("cf_nope", "col_target") == 0
    assert [r["corpus_id"] for r in repo.list_for_file(FILE_ID)] == [CORPUS_ID]


# ---------------------------------------------------------------------------
# Stored tsvector (migration 0114_corpus_chunks_tsv, PG-only — the DuckDB
# sibling neither stores nor ranks, see its docstring)
# ---------------------------------------------------------------------------


def _null_out_tsv(pg_repo, where: str = "TRUE") -> None:
    with pg_repo._engine.begin() as conn:
        conn.execute(sa.text(f"UPDATE corpus_chunks SET tsv = NULL WHERE {where}"))


def test_add_many_stores_the_tokenized_body_alongside_the_text(pg_repo):
    """Every new row carries ``tsv`` = ``to_tsvector('simple', text)`` from
    the insert itself — nothing written after the column landed ever needs
    the backfill. A NULL text stays NULL on both columns."""
    pg_repo.add_many(
        [
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "Contract renewal terms"},
            {"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 1, "text": None},
        ]
    )
    with pg_repo._engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT tsv::text AS stored, to_tsvector('simple', text)::text AS fresh "
                "FROM corpus_chunks ORDER BY ordinal"
            )
        ).all()
    assert rows[0].stored == rows[0].fresh
    assert "'contract':1" in rows[0].stored
    assert rows[1].stored is None and rows[1].fresh is None


def test_search_candidates_ranking_is_identical_with_and_without_the_stored_tsvector(pg_repo):
    """The stored column is a cache of the ranking input, never a different
    input: the ranked result is identical whether every row, no row, or only
    some rows carry ``tsv`` (the per-row ``COALESCE`` fallback — what a
    table looks like part-way through ``scripts/backfill_corpus_chunks_tsv.py``,
    and what every pre-migration row looks like until then)."""
    texts = [
        "contract",
        "contract contract contract",
        "a contract mentioned once",
        "renewal of the contract and its terms",
        "nothing relevant here",
    ]
    pg_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": i, "text": t} for i, t in enumerate(texts)])

    stored = pg_repo.search_candidates([CORPUS_ID], "contract", limit=10)
    assert len(stored) == 4
    assert stored[0]["text"] == "contract contract contract"
    assert "tsv" not in stored[0]

    _null_out_tsv(pg_repo, where="ordinal % 2 = 0")  # partially backfilled
    partial = pg_repo.search_candidates([CORPUS_ID], "contract", limit=10)
    _null_out_tsv(pg_repo)  # nothing backfilled
    fallback = pg_repo.search_candidates([CORPUS_ID], "contract", limit=10)

    assert stored == partial == fallback


def test_search_candidates_still_matches_rows_without_a_stored_tsvector(pg_repo):
    """The WHERE clause stays on the ``to_tsvector('simple', text)``
    expression — a row the backfill has not reached is still a candidate
    (``tsv @@ query`` would have silently dropped it)."""
    pg_repo.add_many([{"corpus_id": CORPUS_ID, "file_id": FILE_ID, "ordinal": 0, "text": "shared keyword apple"}])
    _null_out_tsv(pg_repo)
    rows = pg_repo.search_candidates([CORPUS_ID], "apple", limit=10)
    assert len(rows) == 1 and rows[0]["text"] == "shared keyword apple"
