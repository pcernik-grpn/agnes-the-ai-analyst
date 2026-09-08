"""Unit test for the ``search_candidates`` ranking-cap fix (TCRD-296 gap #69).

Live finding: on a 14.5M-chunk production table, a common single term
(``contract``, ~1.07M matches) made ``ORDER BY ts_rank_cd(...)`` heap-fetch
and re-tokenize every matching row before the outer ``LIMIT`` could drop
any of them — 395s for a 20-row result. The fix wraps the FTS predicate in
an inner subquery capped at ``rank_cap`` rows (a real optimization fence in
Postgres — a subquery ``LIMIT`` cannot be flattened into the outer query),
so only a bounded candidate set is ever ranked.

This test inspects the generated SQL text/params via a fake SQLAlchemy
engine — no real Postgres needed (that's what
``tests/db_pg/test_corpus_chunks_contract.py`` and the dedicated PG tests
in this file's sibling module are for). See
``CorpusChunksPgRepository.search_candidates``'s docstring for the full
design and trade-off.
"""

from __future__ import annotations

from src.repositories.corpus_chunks_pg import (
    _RANK_CANDIDATE_FLOOR,
    _RANK_CANDIDATE_MULTIPLIER,
    _SELECT_CC_NO_EMBED,
    CorpusChunksPgRepository,
)


class _FakeResult:
    def mappings(self):
        return self

    def all(self):
        return []


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def execute(self, stmt, params=None):
        self._calls.append((str(stmt), dict(params or {})))
        return _FakeResult()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeEngine:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def connect(self):
        return _FakeConn(self.calls)


def _run(limit: int):
    engine = _FakeEngine()
    repo = CorpusChunksPgRepository(engine)
    repo.search_candidates(["col_1"], "contract", limit=limit)
    assert len(engine.calls) == 1
    return engine.calls[0]


def test_search_candidates_wraps_fts_predicate_in_a_capped_inner_subquery():
    sql, params = _run(limit=20)

    # The inner subquery carries the FTS predicate AND a LIMIT of its own —
    # the fence that bounds how many rows ever reach ts_rank_cd.
    inner_limit_pos = sql.find(":rank_cap")
    where_pos = sql.find("plainto_tsquery")
    rank_pos = sql.find("ts_rank_cd")
    assert -1 not in (inner_limit_pos, where_pos, rank_pos)
    # Order in the text: WHERE predicate, then the inner LIMIT :rank_cap,
    # then the ranking ORDER BY — i.e. ranking sees an already-capped set.
    assert where_pos < inner_limit_pos < rank_pos

    assert "rank_cap" in params
    assert params["limit"] == 20
    assert params["query"] == "contract"
    assert params["corpus_ids"] == ["col_1"]


def test_rank_cap_is_always_at_least_the_result_limit():
    for limit in (1, 20, 5000, 25000):
        _, params = _run(limit=limit)
        assert params["rank_cap"] >= limit


def test_rank_cap_uses_the_documented_multiplier_and_floor():
    # Small limit: the floor dominates.
    _, params = _run(limit=20)
    assert params["rank_cap"] == _RANK_CANDIDATE_FLOOR
    assert params["rank_cap"] == max(20 * _RANK_CANDIDATE_MULTIPLIER, _RANK_CANDIDATE_FLOOR)

    # Large limit: the multiplier dominates.
    _, params = _run(limit=25000)
    assert params["rank_cap"] == 25000 * _RANK_CANDIDATE_MULTIPLIER
    assert params["rank_cap"] == max(25000 * _RANK_CANDIDATE_MULTIPLIER, _RANK_CANDIDATE_FLOOR)


# ---------------------------------------------------------------------------
# Stored tsvector (migration 0113_corpus_chunks_tsv): ranking reads the
# column, with a per-row fallback; the WHERE keeps the index expression.
# ---------------------------------------------------------------------------


def test_search_candidates_ranks_on_the_stored_tsvector_with_a_per_row_fallback():
    sql, _ = _run(limit=20)
    where = sql[sql.find("WHERE") : sql.find(":rank_cap")]
    ranked = sql[sql.find("ranked AS (") : sql.find("LIMIT :limit")]

    # The rank expression reads the stored column and falls back per row —
    # never re-tokenizes unconditionally.
    assert "ts_rank_cd(COALESCE(tsv, to_tsvector('simple', text))" in ranked
    # The WHERE stays on the EXPRESSION the 0101 GIN index is over (which a
    # NULL-tsv row still satisfies) — never on the column.
    assert "to_tsvector('simple', text) @@ plainto_tsquery('simple', :query)" in where
    assert "tsv @@" not in sql


def test_search_candidates_sorts_narrow_rows_and_fetches_columns_for_the_top_limit_only():
    """The candidate rows are matched (bounded by :rank_cap), ranked as
    ``(id, rank)`` pairs (bounded by :limit), and only THEN joined back to
    the table for their columns — sorting full text-bearing rows spilled to
    disk at the default work_mem. The returned SELECT list is the same
    column-pruned set as every other candidate fetch: ``tsv`` and ``rank``
    never reach the returned dicts."""
    sql, _ = _run(limit=20)
    matched = sql[sql.find("WITH matched AS (") : sql.find("ranked AS (")]
    ranked = sql[sql.find("ranked AS (") : sql.find(f"SELECT {_SELECT_CC_NO_EMBED}")]
    final = sql[sql.find(f"SELECT {_SELECT_CC_NO_EMBED}") :]

    assert "SELECT id, tsv, text FROM corpus_chunks" in matched and "LIMIT :rank_cap" in matched
    assert (
        ranked.startswith("ranked AS (  SELECT id, ts_rank_cd(")
        and "FROM matched ORDER BY rank DESC LIMIT :limit" in ranked
    )
    assert "JOIN corpus_chunks cc ON cc.id = ranked.id" in final
    assert final.rstrip().endswith("ORDER BY ranked.rank DESC")
    assert "tsv" not in final and "text" not in final.replace("cc.text", "")


def _run_filename(limit: int):
    engine = _FakeEngine()
    repo = CorpusChunksPgRepository(engine)
    repo.search_by_filename(["col_1"], ["quarterly", "report"], limit=limit)
    assert len(engine.calls) == 1
    return engine.calls[0]


def test_search_by_filename_scopes_both_sides_of_the_join_by_corpus():
    """The file table is filtered by the caller's collections BEFORE the
    ILIKE (so `idx_corpus_files_corpus_path`'s leading column can bound the
    pattern scan), and the chunk side keeps its own scope — fail-closed for
    a file whose row moved while its chunk rows did not."""
    sql, params = _run_filename(limit=20)
    assert "cf.corpus_id = ANY(:corpus_ids)" in sql
    assert "cc.corpus_id = ANY(:corpus_ids)" in sql
    assert "cf.filename ILIKE :t0" in sql and "cf.filename ILIKE :t1" in sql
    assert sql.rstrip().endswith("LIMIT :limit")
    assert params == {"corpus_ids": ["col_1"], "limit": 20, "t0": "%quarterly%", "t1": "%report%"}
