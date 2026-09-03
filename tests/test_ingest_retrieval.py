"""Tests for src.ingest.retrieval.search — hybrid + fail-closed RBAC scoping."""

from __future__ import annotations


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
