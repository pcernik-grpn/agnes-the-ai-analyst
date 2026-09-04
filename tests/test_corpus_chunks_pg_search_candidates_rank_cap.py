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
