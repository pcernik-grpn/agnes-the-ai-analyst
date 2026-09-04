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
    """Cosine breaks a lexical tie.

    P0 OOM fix (2026-09): candidate SELECTION now happens in SQL
    (``corpus_chunks_repo().search_candidates``), which is lexical-first —
    a query with literally no shared vocabulary in ANY candidate's body
    text no longer reaches the candidate set at all (see the trade-off
    documented on ``search()``). So both chunks here share a query term
    (tying their lexical score exactly, per ``_minmax_normalize``'s
    all-equal case), and only the EMBEDDING distinguishes the winner —
    still the same assertion as before the bounded rewrite: hybrid ranking
    picks the cosine-aligned chunk over its lexical twin.
    """
    import src.ingest.retrieval as retrieval
    from src.ingest.retrieval import search

    # Query vector aligned with the first chunk's embedding.
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    cid = _seed(
        "rs-vec",
        [
            {"ordinal": 0, "text": "alpha beta gamma network signal", "embedding": [1.0] + [0.0] * 383},
            {"ordinal": 1, "text": "delta epsilon zeta network signal", "embedding": [0.0] * 384},
        ],
    )
    res = search([cid], "network signal")
    assert res
    assert res[0]["ordinal"] == 0  # lexical tie; cosine picked the aligned vector


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
# P0 OOM fix (2026-09): bounded candidate selection
# ---------------------------------------------------------------------------


def test_search_reports_capped_when_candidates_exceed_the_configured_limit(e2e_env, monkeypatch):
    """search() must never load more candidate chunks than the configured
    cap, and must say so via `.capped` when the cap was actually hit — the
    signal `app.api.knowledge_search` / `app.api.collections` surface as
    the additive `candidates_capped` response field."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 3)
    cid = _seed_files(
        "rs-cap",
        [(f"f{i}.txt", [{"ordinal": 0, "text": "shared keyword apple"}]) for i in range(6)],
    )
    res = retrieval.search([cid], "apple")
    assert res
    assert res.capped is True


def test_search_not_capped_when_candidates_are_under_the_limit(e2e_env, monkeypatch):
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 100)
    cid = _seed("rs-nocap", [{"ordinal": 0, "text": "shared keyword apple"}])
    res = retrieval.search([cid], "apple")
    assert res
    assert res.capped is False


def test_search_results_equal_plain_lists(e2e_env):
    """`SearchResults` is additive: every pre-existing caller compares/
    iterates/indexes the return value as a plain `list[dict]`."""
    from src.ingest.retrieval import search

    cid = _seed("rs-plain", [{"ordinal": 0, "text": "hello world"}])
    res = search([cid], "hello")
    assert isinstance(res, list)
    assert res == list(res)
    assert res[0]["chunk_id"]


def test_search_candidates_and_search_by_filename_are_sql_bounded():
    """Static guard: both bounded-candidate repo methods, on both backends,
    must carry a SQL ``LIMIT`` — an in-Python slice AFTER an unbounded
    fetch is exactly the OOM shape this fixes."""
    import inspect

    from src.repositories.corpus_chunks import CorpusChunksRepository
    from src.repositories.corpus_chunks_pg import CorpusChunksPgRepository

    for repo_cls in (CorpusChunksRepository, CorpusChunksPgRepository):
        for name in ("search_candidates", "search_by_filename"):
            source = inspect.getsource(getattr(repo_cls, name))
            assert "LIMIT" in source, f"{repo_cls.__name__}.{name} must bound its query with LIMIT"


def test_max_candidate_chunks_default(e2e_env):
    from src.ingest.retrieval import _DEFAULT_MAX_CANDIDATE_CHUNKS, _max_candidate_chunks

    assert _max_candidate_chunks() == _DEFAULT_MAX_CANDIDATE_CHUNKS == 5000


def test_max_candidate_chunks_reads_instance_config(monkeypatch):
    import app.instance_config as instance_config
    from src.ingest.retrieval import _max_candidate_chunks

    def _fake_get_value(*keys, default=None):
        if keys == ("knowledge", "retrieval", "max_candidate_chunks"):
            return 42
        return default

    monkeypatch.setattr(instance_config, "get_value", _fake_get_value)
    assert _max_candidate_chunks() == 42


def test_search_finds_a_filename_match_with_zero_body_overlap_even_when_capped(e2e_env, monkeypatch):
    """The filename fallback's bounded candidate path (`search_by_filename`)
    is independent of the body cap — a tiny body cap must not disable it."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 1)
    cid = _seed_files("rs-fn-cap", [("quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])])
    res = retrieval.search([cid], "quarterly-report")
    assert res, "the filename fallback must survive a tiny body candidate cap"
    assert res[0]["matched_on"] == "filename"


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
    ranking must have come through the embeddings-by-id phase: the bounded
    candidate fetch (``search_candidates``) is column-pruned and never
    returns a vector, so the ONLY way the cosine-aligned chunk can win a
    lexical tie is a real ``list_embeddings_for_ids`` round-trip for the
    shortlist. (Both chunks share the query's words on purpose — candidate
    SELECTION is lexical-first since the P0 OOM fix, so a chunk with zero
    body overlap is not a candidate at all; see ``search_with_meta``.)"""
    import src.ingest.retrieval as retrieval
    from src.repositories.corpus_chunks import CorpusChunksRepository

    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    fetched: list[list[str]] = []
    real = CorpusChunksRepository.list_embeddings_for_ids

    def _spy(self, ids):
        fetched.append(sorted(ids))
        return real(self, ids)

    monkeypatch.setattr(CorpusChunksRepository, "list_embeddings_for_ids", _spy)
    cid = _seed(
        "rs-shortlist",
        [
            {"ordinal": 0, "text": "alpha beta gamma network signal", "embedding": [1.0] + [0.0] * 383},
            {"ordinal": 1, "text": "delta epsilon zeta network signal", "embedding": [0.0] * 384},
        ],
    )
    meta = retrieval.search_with_meta([cid], "network signal")
    assert meta["results"]
    assert meta["results"][0]["ordinal"] == 0
    assert meta["truncated"] is False
    assert len(fetched) == 1 and len(fetched[0]) == 2, "one phase-2 fetch, for exactly the shortlist"


def test_search_with_meta_shortlist_excludes_low_lexical_rank_from_vector_rerank(e2e_env, monkeypatch):
    """Documents the accepted approximation (#2151): only the top
    ``_VECTOR_SHORTLIST_SIZE`` lexical candidates ever get a vector fetched
    and considered, so a candidate with weak lexical overlap that would
    have won on pure cosine similarity is invisible once the shortlist is
    smaller than the candidate set — proven with a shortlist forced down
    to 1. (The weaker chunk still shares ONE query word so it reaches the
    lexical-first candidate set at all; it loses the shortlist slot, not
    the SQL prefilter.)"""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_VECTOR_SHORTLIST_SIZE", 1)
    monkeypatch.setattr(retrieval, "embed_query", lambda q: [1.0] + [0.0] * 383)
    cid = _seed(
        "rs-shortlist-miss",
        [
            # Lexically strongest (matches every query term) but orthogonal
            # vector — wins the shortlist slot, then loses on cosine.
            {"ordinal": 0, "text": "shared query terms shared query terms", "embedding": [0.0] * 384},
            # Perfectly aligned vector but only ONE overlapping word —
            # excluded from the size-1 shortlist before it ever gets a
            # vector fetch.
            {"ordinal": 1, "text": "the terms of something else entirely", "embedding": [1.0] + [0.0] * 383},
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
    """Three chunks match the query's term; the cap admits two → the bounded
    candidate fetch filled its LIMIT and says so (``truncated`` + the cap
    that bound). A chunk that shares no word with the query is not a
    candidate in the first place, so it does not count toward the cap."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 2)
    cid = _seed(
        "rs-cap-over",
        [
            {"ordinal": 0, "text": "kubernetes cluster guide"},
            {"ordinal": 1, "text": "kubernetes weather report"},
            {"ordinal": 2, "text": "another kubernetes row about nothing"},
        ],
    )
    meta = retrieval.search_with_meta([cid], "kubernetes")
    assert meta["truncated"] is True
    assert meta["cap"] == 2
    assert meta["results"]
    assert all("kubernetes" in r["text"] for r in meta["results"])


def test_search_with_meta_cap_is_min_of_both_config_keys(e2e_env, monkeypatch):
    """The P0 SQL-side candidate cap (``knowledge.retrieval.
    max_candidate_chunks``) is applied first; #2151's
    ``collections.search_max_chunks`` is a second ceiling on the same
    candidate set — the effective LIMIT is the smaller of the two, and the
    reported ``cap`` is that number."""
    import src.ingest.retrieval as retrieval

    cid = _seed("rs-cap-min", [{"ordinal": i, "text": "kubernetes cluster guide"} for i in range(3)])

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 1)
    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 100)
    meta = retrieval.search_with_meta([cid], "kubernetes")
    assert (meta["truncated"], meta["cap"]) == (True, 1)

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 100)
    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 2)
    meta = retrieval.search_with_meta([cid], "kubernetes")
    assert (meta["truncated"], meta["cap"]) == (True, 2)

    monkeypatch.setattr(retrieval, "_max_candidate_chunks", lambda: 100)
    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 100)
    meta = retrieval.search_with_meta([cid], "kubernetes")
    assert (meta["truncated"], meta["cap"]) == (False, None)
    # And search()'s list-shaped view of the same signal agrees.
    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 2)
    assert retrieval.search([cid], "kubernetes").capped is True


def test_search_with_meta_over_cap_stopword_only_query_raises(e2e_env, monkeypatch):
    """A stopword-only query whose matches still fill the cap is an
    arbitrary slice of the corpus, not a search — refused, and the
    message carries the corpus-wide count (paid only on this path)."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 1)
    cid = _seed(
        "rs-cap-broad",
        [
            {"ordinal": 0, "text": "the kubernetes cluster guide is here and there"},
            {"ordinal": 1, "text": "the weather report is unrelated and long"},
        ],
    )
    with pytest.raises(retrieval.SearchQueryTooBroad) as excinfo:
        retrieval.search_with_meta([cid], "the and is")
    assert excinfo.value.cap == 1
    assert excinfo.value.chunk_count == 2


def test_search_with_meta_stopword_only_query_under_cap_is_not_refused(e2e_env, monkeypatch):
    """The refusal is about the cap, not the query: a stopword-only query
    over a corpus small enough not to fill the cap is answered normally."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 100)
    cid = _seed("rs-cap-broad-small", [{"ordinal": 0, "text": "the kubernetes cluster guide is here"}])
    meta = retrieval.search_with_meta([cid], "the is")
    assert meta["truncated"] is False


def test_search_with_meta_over_cap_with_real_terms_never_raises(e2e_env, monkeypatch):
    """A query with at least one non-stopword term is never refused, even
    when it fills the cap — only an all-stopword query is."""
    import src.ingest.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_search_max_chunks", lambda: 1)
    cid = _seed(
        "rs-cap-ok",
        [
            {"ordinal": 0, "text": "kubernetes cluster guide"},
            {"ordinal": 1, "text": "kubernetes weather report"},
        ],
    )
    meta = retrieval.search_with_meta([cid], "what is kubernetes")
    assert meta["truncated"] is True
