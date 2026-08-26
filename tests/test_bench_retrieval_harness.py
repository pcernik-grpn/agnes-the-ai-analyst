"""The benchmark harness must measure the real code path, not a copy of it.

`scripts/bench_retrieval.py` exists to answer a capacity question, so its
numbers are only worth anything if the thing it times is what production
runs. Two ways that quietly stops being true, both pinned here:

* the harness's candidate fetch drifts from `CorpusChunksRepository.
  list_for_corpora` (different columns → different materialization cost,
  which is most of what is being measured)
* the harness stops exercising `rank_chunks` itself

Deliberately tiny scales: this is a correctness test for the measuring
instrument, not a performance test. A perf assertion here would either be
too loose to catch anything or too tight to survive a shared CI runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bench_retrieval as bench  # noqa: E402


def test_harness_fetches_the_same_columns_as_the_repository():
    """The `embedding FLOAT[384]` column is the expensive one, and the point
    of the measurement is that production pays for it on every query even
    when nothing reads it. A harness that omitted it would report a cost
    nobody has."""
    from src.repositories.corpus_chunks import _COLS as repo_cols

    assert bench._COLS == repo_cols, (
        "the benchmark's column list drifted from CorpusChunksRepository's — "
        "the measured materialization cost is no longer production's"
    )
    assert "embedding" in bench._COLS


def test_bench_scale_runs_end_to_end_and_reports_every_field():
    row = bench.bench_scale(200, 2, with_embeddings=False)
    for field in (
        "chunks",
        "fetch_p50_ms",
        "fetch_p95_ms",
        "rank_p50_ms",
        "rank_p95_ms",
        "total_p50_ms",
        "total_p95_ms",
        "peak_rss_mb",
    ):
        assert field in row, f"missing {field}"
    assert row["chunks"] == 200
    assert row["total_p50_ms"] >= row["rank_p50_ms"], "total must include the rank phase"


def test_the_embedded_path_actually_scores_vectors():
    """With --embed the harness injects a query vector so the cosine+fusion
    branch runs. If that injection broke, the run would silently measure the
    lexical path twice and report the hybrid cost as far cheaper than it is.
    """
    import src.ingest.retrieval as retrieval_mod

    seen = {"called": False}
    original = retrieval_mod._cosine

    def _spy(a, b):
        seen["called"] = True
        return original(a, b)

    retrieval_mod._cosine = _spy
    try:
        bench.bench_scale(120, 1, with_embeddings=True)
    finally:
        retrieval_mod._cosine = original

    assert seen["called"], "the embedded run never reached the cosine path"


def test_query_mix_covers_common_rare_and_multi_term():
    """A benchmark that only asked rare-term queries would understate the
    cost: a common term matches nearly every chunk, which is the expensive
    shape."""
    import random

    qs = bench._queries(random.Random(1), 9)
    assert any(q in bench._VOCAB for q in qs), "no common-term query"
    assert any(q in bench._RARE for q in qs), "no rare-term query"
    assert any(" " in q for q in qs), "no multi-term query"
