"""Tests for src.ingest.retrieval.search — hybrid + fail-closed RBAC scoping."""

from __future__ import annotations

import pytest


def _seed(slug: str, chunks: list[dict]) -> str:
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename=f"{slug}.txt",
        sha256="s",
        file_type="txt",
        size_bytes=1,
        storage_path="/x",
    )
    rows = [{"corpus_id": cid, "file_id": fid, **c} for c in chunks]
    corpus_chunks_repo().add_many(rows)
    return cid


def test_search_fail_closed_on_empty_inputs(e2e_env):
    from src.ingest.retrieval import search

    assert search([], "anything") == []
    cid = _seed("rs-empty", [{"ordinal": 0, "text": "hello world"}])
    assert search([cid], "   ") == []


def test_search_lexical_ranks_matches(e2e_env):
    from src.ingest.retrieval import search

    cid = _seed(
        "rs-lex",
        [
            {"ordinal": 0, "text": "the quick brown fox jumps over"},
            {"ordinal": 1, "text": "completely unrelated weather report"},
        ],
    )
    res = search([cid], "brown fox")
    assert res
    assert res[0]["text"].startswith("the quick brown fox")
    assert res[0]["filename"] == "rs-lex.txt"
    assert res[0]["score"] > 0
    assert res[0]["chunk_id"]


def test_search_is_rbac_scoped_to_listed_corpora(e2e_env):
    from src.ingest.retrieval import search

    cid_a = _seed("rs-a", [{"ordinal": 0, "text": "shared keyword apple"}])
    _seed("rs-b", [{"ordinal": 0, "text": "shared keyword apple"}])
    # Only corpus A is granted → corpus B must never appear (fail-closed).
    res = search([cid_a], "apple")
    assert res
    assert all(r["corpus_id"] == cid_a for r in res)


def test_search_hybrid_uses_embeddings(e2e_env, monkeypatch):
    import src.ingest.retrieval as retrieval
    from src.ingest.retrieval import search

    # Query vector aligned with the first chunk's embedding; no lexical overlap.
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    cid = _seed(
        "rs-vec",
        [
            {"ordinal": 0, "text": "alpha beta gamma", "embedding": [1.0] + [0.0] * 383},
            {"ordinal": 1, "text": "delta epsilon zeta", "embedding": [0.0] * 384},
        ],
    )
    res = search([cid], "no-lexical-overlap-query")
    assert res
    assert res[0]["ordinal"] == 0  # cosine picked the aligned vector


def _seed_files(slug: str, files: list[tuple[str, list[dict]]]) -> str:
    """Seed several *files* (not just chunks) under one corpus, so tests can
    control the corpus's distinct-file count."""
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    for filename, chunks in files:
        fid = corpus_files_repo().add(
            corpus_id=cid,
            filename=filename,
            sha256="s",
            file_type="txt",
            size_bytes=1,
            storage_path="/x",
        )
        rows = [{"corpus_id": cid, "file_id": fid, **c} for c in chunks]
        corpus_chunks_repo().add_many(rows)
    return cid


def test_search_idf_ranks_distinctive_term_over_common_term(e2e_env):
    """#756: a chunk matching only a term that's rare across the candidate
    set must outrank a chunk matching only a term common to most candidates
    — even though both match exactly one of the two query terms (a tie
    under the old "fraction of distinct terms present" score)."""
    from src.ingest.retrieval import search

    cid = _seed_files(
        "rs-idf",
        [
            ("kube.txt", [{"ordinal": 0, "text": "kubernetes cluster autoscaling guide"}]),
            ("data1.txt", [{"ordinal": 0, "text": "data warehouse pipeline overview"}]),
            ("data2.txt", [{"ordinal": 0, "text": "data quality checks nightly"}]),
            ("data3.txt", [{"ordinal": 0, "text": "data retention policy"}]),
            ("data4.txt", [{"ordinal": 0, "text": "data export formats"}]),
        ],
    )
    res = search([cid], "data kubernetes")
    assert res
    # "kubernetes" is unique to kube.txt (high IDF); "data" is common to the
    # other four files (low IDF) — the distinctive-term match must win.
    assert res[0]["filename"] == "kube.txt"
    assert res[0]["score"] > res[1]["score"]


def test_search_tiny_corpus_small_margin_is_low_confidence(e2e_env):
    """#756: on a tiny corpus (2 files) with a tied/near-tied top score, the
    surfaced confidence must be "low" — never presented as a trustworthy
    top pick."""
    from src.ingest.retrieval import search

    cid = _seed_files(
        "rs-tiny",
        [
            ("a.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
            ("b.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
        ],
    )
    res = search([cid], "apple")
    assert res
    assert all(r["confidence"] == "low" for r in res)


def test_search_embeddings_absent_tie_is_deterministic(e2e_env, monkeypatch):
    """#756: with embeddings absent (the default deployment), a lexical-score
    tie must resolve deterministically (stable chunk-id tie-break) instead
    of by arbitrary DB fetch order."""
    import src.ingest.retrieval as retrieval
    from src.repositories import corpus_chunks_repo

    monkeypatch.setattr(retrieval, "embed_query", lambda q: None)
    cid = _seed_files(
        "rs-det",
        [
            ("z.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
            ("a.txt", [{"ordinal": 0, "text": "shared keyword apple"}]),
        ],
    )
    chunk_ids = sorted(c["id"] for c in corpus_chunks_repo().list_for_corpus(cid))

    res1 = retrieval.search([cid], "apple")
    res2 = retrieval.search([cid], "apple")

    assert [r["chunk_id"] for r in res1] == chunk_ids
    assert [r["chunk_id"] for r in res2] == chunk_ids


def test_search_normalization_handles_single_candidate(e2e_env):
    """#756: min-max normalization over a single-candidate set (or a set
    with no lexical/vector signal) must not divide by zero."""
    from src.ingest.retrieval import search

    cid = _seed("rs-single", [{"ordinal": 0, "text": "hello world"}])
    res = search([cid], "hello")
    assert res
    assert res[0]["score"] > 0
    assert res[0]["confidence"] == "low"  # a single-file corpus can't discriminate


def test_retrieval_mode_never_loads_the_model(monkeypatch):
    """#898 review follow-up: labeling a response must not pay the model
    instantiation/download cost — `retrieval_mode` goes through the
    `embedding_capability` probe, never `_load_model`."""
    import src.ingest.embeddings as embeddings
    from src.ingest.retrieval import retrieval_mode

    def _boom():
        raise AssertionError("retrieval_mode must not trigger a model load")

    monkeypatch.setattr(embeddings, "_load_model", _boom)

    monkeypatch.setattr(embeddings, "_model", None)  # unresolved → import probe
    assert retrieval_mode() in ("hybrid", "lexical_only")

    monkeypatch.setattr(embeddings, "_model", False)  # resolved: known-absent
    assert retrieval_mode() == "lexical_only"

    monkeypatch.setattr(embeddings, "_model", object())  # resolved: loaded model
    assert retrieval_mode() == "hybrid"


# ---------------------------------------------------------------------------
# #2151: rank_chunks accepts a pre-computed q_vec (search_with_meta's
# two-phase shortlist calls embed_query exactly once, up front, instead of
# letting rank_chunks call it again internally).
# ---------------------------------------------------------------------------


def test_rank_chunks_uses_precomputed_q_vec_over_calling_embed_query(monkeypatch):
    import src.ingest.retrieval as retrieval

    def _boom(_q):
        raise AssertionError("rank_chunks must not call embed_query when q_vec is given")

    monkeypatch.setattr(retrieval, "embed_query", _boom)
    chunks = [
        {"id": "a", "file_id": "f1", "text": "no lexical overlap", "embedding": [1.0] + [0.0] * 383},
        {"id": "b", "file_id": "f2", "text": "no lexical overlap either", "embedding": [0.0] * 384},
    ]
    top, _confidence = retrieval.rank_chunks(chunks, "zzz-query-with-no-overlap", q_vec=[1.0] + [0.0] * 383)
    assert top
    assert top[0][1]["id"] == "a"


def test_rank_chunks_default_still_calls_embed_query(monkeypatch):
    """Backward compatibility: every existing caller (search_with_meta with
    no embeddings, src.search.local, scripts/bench_retrieval.py) omits
    q_vec and relies on rank_chunks calling embed_query itself."""
    import src.ingest.retrieval as retrieval

    calls = []
    monkeypatch.setattr(retrieval, "embed_query", lambda q: calls.append(q) or None)
    retrieval.rank_chunks([{"id": "a", "file_id": "f1", "text": "hello"}], "hello")
    assert calls == ["hello"]


# ---------------------------------------------------------------------------
# #2151: search_with_meta's two-phase shortlist — fetch text-only, rank
# lexically to a shortlist, fetch embeddings ONLY for the shortlist.
# ---------------------------------------------------------------------------


def test_search_with_meta_shortlists_before_fetching_embeddings(e2e_env, monkeypatch):
    """With a query vector available, the candidate set handed to the final
    ranking must have come through the embeddings-by-id phase — proven here
    by a corpus whose ONLY embedded chunk is lexically irrelevant (so it
    would never be re-ranked to the top without a real vector attached)."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    cid = _seed(
        "rs-shortlist",
        [
            {"ordinal": 0, "text": "alpha beta gamma", "embedding": [1.0] + [0.0] * 383},
            {"ordinal": 1, "text": "delta epsilon zeta", "embedding": [0.0] * 384},
        ],
    )
    meta = retrieval.search_with_meta([cid], "no-lexical-overlap-query")
    assert meta["results"]
    assert meta["results"][0]["ordinal"] == 0
    assert meta["truncated"] is False


def test_search_with_meta_shortlist_excludes_low_lexical_rank_from_vector_rerank(e2e_env, monkeypatch):
    """Documents the accepted approximation (#2151): only the top
    ``_VECTOR_SHORTLIST_SIZE`` lexical candidates ever get a vector fetched
    and considered, so a chunk with zero lexical overlap that would have
    won on pure cosine similarity is invisible once the shortlist is
    smaller than the corpus — proven with a shortlist forced down to 1."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_VECTOR_SHORTLIST_SIZE", 1)
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    cid = _seed(
        "rs-shortlist-miss",
        [
            # Lexically strongest (matches the query terms) but orthogonal
            # vector — wins the shortlist slot, then loses on cosine.
            {"ordinal": 0, "text": "shared query terms shared query terms", "embedding": [0.0] * 384},
            # Perfectly aligned vector but ZERO lexical overlap — excluded
            # from the size-1 shortlist before it ever gets a vector fetch.
            {"ordinal": 1, "text": "nothing matches here at all", "embedding": [1.0] + [0.0] * 383},
        ],
    )
    meta = retrieval.search_with_meta([cid], "shared query terms")
    ordinals = {r["ordinal"] for r in meta["results"]}
    assert 1 not in ordinals  # the vector-best candidate never got a chance


def test_search_with_meta_small_corpus_matches_search(e2e_env):
    """Regression pin: for a corpus under both the shortlist size and the
    chunk cap, search_with_meta's results must be identical to plain
    search() (and, transitively, to the pre-#2151 behavior)."""
    from src.ingest.retrieval import search, search_with_meta

    cid = _seed(
        "rs-parity",
        [
            {"ordinal": 0, "text": "the quick brown fox jumps over"},
            {"ordinal": 1, "text": "completely unrelated weather report"},
        ],
    )
    assert search_with_meta([cid], "brown fox")["results"] == search([cid], "brown fox")


# ---------------------------------------------------------------------------
# #2151: collections.search_max_chunks cap — over-cap corpora get a SQL-side
# lexical prefilter + truncated:true instead of an unbounded fetch; a query
# with no usable term over the cap is refused (SearchQueryTooBroad).
# ---------------------------------------------------------------------------


def test_search_with_meta_under_cap_is_not_truncated(e2e_env, monkeypatch):
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 10)
    cid = _seed("rs-cap-under", [{"ordinal": i, "text": "kubernetes cluster guide"} for i in range(3)])
    meta = retrieval.search_with_meta([cid], "kubernetes")
    assert meta["truncated"] is False
    assert meta["cap"] is None


def test_search_with_meta_over_cap_is_truncated_and_prefiltered(e2e_env, monkeypatch):
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 2)
    cid = _seed(
        "rs-cap-over",
        [
            {"ordinal": 0, "text": "kubernetes cluster guide"},
            {"ordinal": 1, "text": "totally unrelated weather report"},
            {"ordinal": 2, "text": "another unrelated row about nothing"},
        ],
    )
    meta = retrieval.search_with_meta([cid], "kubernetes")
    assert meta["truncated"] is True
    assert meta["cap"] == 2
    assert meta["results"]
    assert meta["results"][0]["text"] == "kubernetes cluster guide"


def test_search_with_meta_over_cap_stopword_only_query_raises(e2e_env, monkeypatch):
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 1)
    cid = _seed(
        "rs-cap-broad",
        [
            {"ordinal": 0, "text": "kubernetes cluster guide"},
            {"ordinal": 1, "text": "totally unrelated weather report"},
        ],
    )
    with pytest.raises(retrieval.SearchQueryTooBroad) as excinfo:
        retrieval.search_with_meta([cid], "the and is")
    assert excinfo.value.cap == 1
    assert excinfo.value.chunk_count == 2


def test_search_with_meta_over_cap_with_real_terms_never_raises(e2e_env, monkeypatch):
    """A query with at least one non-stopword term always builds a usable
    prefilter, even over the cap — only an all-stopword query is refused."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 1)
    cid = _seed(
        "rs-cap-ok",
        [
            {"ordinal": 0, "text": "kubernetes cluster guide"},
            {"ordinal": 1, "text": "totally unrelated weather report"},
        ],
    )
    meta = retrieval.search_with_meta([cid], "what is kubernetes")
    assert meta["truncated"] is True
