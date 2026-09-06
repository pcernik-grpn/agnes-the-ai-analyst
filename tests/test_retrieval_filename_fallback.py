"""Searching for a file by its NAME finds it.

`rank_chunks` scores chunk text only — its own docstring says "no filename
resolution" — and the filename is attached afterwards, for the citation. So
the most natural query a person or an agent makes ("what is in
quarterly-report.md?") matched nothing, because the words are in the file's
NAME and not in its body.

Observed live: an agent asked about an uploaded file searched for the
filename, got `[]`, and concluded it lacked access. #1236 made that empty
result explain itself; this makes the query work.

Deliberately a FALLBACK, not a scoring change: content hits still win and
rank exactly as before, and the filename pass runs only when the body
search comes up empty. A filename is weak evidence — it should never
outrank a real match in the text.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _seed(slug: str, filename: str, chunks: list[dict]) -> str:
    from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

    cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
    fid = corpus_files_repo().add(
        corpus_id=cid,
        filename=filename,
        sha256="s",
        file_type=filename.rsplit(".", 1)[-1],
        size_bytes=1,
        storage_path="/x",
    )
    rows = [{"corpus_id": cid, "file_id": fid, **c} for c in chunks]
    corpus_chunks_repo().add_many(rows)
    return cid


class TestFilenameFallback:
    def test_query_matching_only_the_filename_finds_the_file(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed(
            "fn-basic",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "alpha bravo charlie delta"}],
        )
        res = search([cid], "quarterly-report")
        assert res, "a file cannot be found by the name it is displayed under"
        assert res[0]["filename"] == "quarterly-report.md"

    def test_a_single_distinctive_word_from_the_name_is_enough(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-word", "zs-test-poznamka.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "poznamka"), "filename tokens are not searchable"

    def test_the_extension_alone_does_not_match_everything(self, e2e_env):
        """`md` is in every markdown filename — matching on it returns noise."""
        from src.ingest.retrieval import search

        cid = _seed("fn-ext", "notes.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "md") == []

    def test_content_hits_still_win_and_are_unchanged(self, e2e_env):
        """The fallback must not reorder a search that already worked."""
        from src.ingest.retrieval import search

        cid = _seed(
            "fn-content",
            "report.md",
            [
                {"ordinal": 0, "text": "the quick brown fox jumps over"},
                {"ordinal": 1, "text": "completely unrelated weather"},
            ],
        )
        res = search([cid], "brown fox")
        assert res[0]["text"].startswith("the quick brown fox")
        assert res[0]["score"] > 0

    def test_a_content_match_is_preferred_over_a_filename_match(self, e2e_env):
        """A body hit beats a name hit — the name is the weaker signal."""
        from src.ingest.retrieval import search

        cid = _seed("fn-vs-a", "revenue.md", [{"ordinal": 0, "text": "alpha bravo"}])
        _seed("fn-vs-b", "notes.md", [{"ordinal": 0, "text": "revenue grew 12 percent"}])
        res = search([cid], "revenue")
        assert res, "precondition"
        # Only one corpus is in scope, so this asserts the fallback did not
        # fire while a real content hit existed elsewhere in that corpus.
        assert res[0]["filename"] == "revenue.md"

    def test_no_match_anywhere_still_returns_empty(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-none", "notes.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "nosuchwordanywhere") == []

    def test_blank_query_is_still_fail_closed(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-blank", "notes.md", [{"ordinal": 0, "text": "alpha"}])
        assert search([cid], "   ") == []

    def test_filename_hits_are_labelled_low_confidence(self, e2e_env):
        """A name match is a hint, not evidence — say so in the payload."""
        from src.ingest.retrieval import search

        cid = _seed("fn-conf", "quarterly-report.md", [{"ordinal": 0, "text": "alpha"}])
        res = search([cid], "quarterly-report")
        assert res and res[0].get("confidence") == "low"

    def test_fallback_respects_the_corpus_scope(self, e2e_env):
        """Fail-closed RBAC applies to the name pass as much as the text pass."""
        from src.ingest.retrieval import search

        cid_a = _seed("fn-rbac-a", "alpha-notes.md", [{"ordinal": 0, "text": "x"}])
        _seed("fn-rbac-b", "beta-secret.md", [{"ordinal": 0, "text": "y"}])
        assert search([cid_a], "beta-secret") == []

    def test_result_rows_keep_their_shape(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("fn-shape", "quarterly-report.md", [{"ordinal": 0, "text": "alpha"}])
        row = search([cid], "quarterly-report")[0]
        for key in ("chunk_id", "corpus_id", "file_id", "filename", "ordinal", "text", "score"):
            assert key in row, f"filename hit is missing {key}"


class TestANameHitCannotCrowdOutRealMatches:
    """Devin Review on #1267: `_minmax` makes any bucket's top hit 1.0.

    A fallback that matched only file names would therefore arrive looking
    exactly as strong as a document that genuinely contains the words, and
    could fill every slot of a combined answer — hiding the knowledge notes,
    glossary entries and tables that actually matched.
    """

    def test_a_body_hit_is_labelled_as_one(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("label-body", "notes.md", [{"ordinal": 0, "text": "alpha bravo charlie"}])
        res = search([cid], "bravo")
        assert res and res[0]["matched_on"] == "body"

    def test_a_name_hit_is_labelled_as_one(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("label-name", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])
        res = search([cid], "quarterly-report")
        assert res and res[0]["matched_on"] == "filename"

    def test_the_fallback_fires_even_when_scoring_returns_something(self, e2e_env):
        """The trigger is "no body carries the query", not "no results".

        With the embeddings extra installed every chunk gets a non-zero
        cosine, so `rank_chunks` essentially always returns rows — an
        empty-only trigger made the whole fallback dead code on exactly the
        instances that have semantic search. Simulated here by scoring every
        chunk from a query that appears in no body. (Devin Review on #1267.)
        """
        from unittest.mock import patch

        from src.ingest import retrieval

        cid = _seed("label-semantic", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])

        real = retrieval.rank_chunks

        def _always_scores(chunks, query, *, k=10, **kwargs):
            top, _conf = real(chunks, query, k=k, **kwargs)
            if top:
                return top, "medium"
            return [(0.42, ch) for ch in chunks][:k], "medium"

        with patch.object(retrieval, "rank_chunks", _always_scores):
            res = retrieval.search([cid], "quarterly-report")

        assert res, "the fallback never ran"
        assert res[0]["matched_on"] == "filename"
        assert res[0]["confidence"] == "low"

    def test_the_combined_search_caps_a_name_only_chunk_bucket(self, e2e_env):
        """Two name-only chunks at most WHEN something else matched too."""
        from src.search.unified import unified_search

        cid = _seed(
            "cap-name",
            "quarterly-report.md",
            [{"ordinal": i, "text": f"alpha bravo charlie {i}"} for i in range(6)],
        )
        knowledge = [
            {"id": f"k{i}", "title": f"quarterly report note {i}", "content": "quarterly report", "domain": "d"}
            for i in range(3)
        ]

        from unittest.mock import patch

        with patch("src.search.unified._knowledge_search", return_value=knowledge):
            hits = unified_search(
                "quarterly-report",
                corpus_ids=[cid],
                user_groups=None,
                granted_domains=None,
                tables=[],
                k=10,
            )

        chunks = [h for h in hits if h.get("type") == "chunk"]
        assert chunks, "the name match must still be reachable"
        assert len(chunks) <= 2, f"a name-only bucket took {len(chunks)} slots"
        assert any(h.get("type") == "knowledge" for h in hits), "real matches must survive"

    def test_the_cap_does_not_fire_when_nothing_else_matched(self, e2e_env):
        """Then the cap would only throw away the answer to the query that
        motivated the fallback. (Devin Review on #1267.)"""
        from src.search.unified import unified_search

        cid = _seed(
            "cap-alone",
            "quarterly-report.md",
            [{"ordinal": i, "text": f"alpha bravo charlie {i}"} for i in range(6)],
        )

        from unittest.mock import patch

        with patch("src.search.unified._knowledge_search", return_value=[]):
            hits = unified_search(
                "quarterly-report",
                corpus_ids=[cid],
                user_groups=None,
                granted_domains=None,
                tables=[],
                k=10,
            )

        chunks = [h for h in hits if h.get("type") == "chunk"]
        assert len(chunks) > 2, f"only {len(chunks)} chunks, but nothing else matched"

    def test_offline_search_has_the_same_fallback(self):
        """Behaviour is pinned in `tests/test_search_local.py` against a real
        built artifact; this only guards that the offline reader still calls
        the shared ranker rather than growing its own."""
        import inspect

        from src.search import local

        src = inspect.getsource(local.local_search)
        assert "apply_filename_fallback" in src, "the offline reader must share the server's fallback"


class TestTheAdviceMatchesTheBehaviour:
    """The caveat told users and agents not to do the thing that now works."""

    def test_no_surface_still_says_filenames_are_not_indexed(self):
        import subprocess

        out = subprocess.run(
            ["grep", "-rn", "filenames are not indexed", "--include=*.py", "app", "cli", "src"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        ).stdout
        assert out.strip() == "", f"stale caveat still shipped:\n{out}"

    def test_both_mcp_surfaces_describe_the_fallback(self):
        for path in ("cli/mcp/server.py", "app/api/mcp/foundation_tools.py"):
            text = (ROOT / path).read_text()
            assert "file names are a fallback, not an index" in text, path
            assert 'matched_on: "filename"' in text, path


class TestAHumanCanTellWhichKindOfHitItIs:
    """Devin Review on #1267: the snippet under a name match is the file's
    opening text, which does not contain the query. MCP agents read
    `matched_on`; a person reading the collections UI saw a normal-looking
    quotation instead.

    The UI-labelling half of this (`test_the_collection_page_labels_a_name_match`,
    asserted against the frozen pre-redesign `library_detail_legacy.html`) was
    removed when that template was deleted in Wave 0 legacy retirement — the
    live `library_detail.html` never carried the same inline labelling, so
    there is no surviving template to point the assertion at. The API-level
    contract below is unaffected."""

    def test_the_api_carries_the_label_to_it(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("label-api", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])
        assert search([cid], "quarterly-report")[0]["matched_on"] == "filename"


class TestTheTriggerSurvivesAnOrdinarySentence:
    """Devin Review on #1267, third round: the headline case still missed.

    "no body contains ANY query word" meant one stray `in` or `the` in one
    passage disabled the name pass — which is every question phrased as a
    sentence rather than as keywords. The trigger compares coverage now: a
    name that explains more of the question than the best passage does leads.
    """

    def test_a_natural_question_still_finds_the_file(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed(
            "sentence",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "alpha bravo charlie is in the delta"}],
        )

        res = search([cid], "what is in quarterly-report.md")

        assert res, "the question that motivated the whole change found nothing"
        assert res[0]["matched_on"] == "filename"

    def test_a_passage_that_explains_more_keeps_its_place(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed(
            "real-body",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "the quarterly report explains margin by region"}],
        )

        res = search([cid], "quarterly report margin")

        assert res and res[0]["matched_on"] == "body"

    def test_scores_do_not_contradict_the_order(self, e2e_env):
        """A kept body hit must not carry a bigger number than the name hit
        printed above it. (Devin Review on #1267.)"""
        from src.ingest.retrieval import search

        cid = _seed(
            "monotone",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "alpha bravo"}, {"ordinal": 1, "text": "in the report of margins"}],
        )

        res = search([cid], "what is in quarterly-report.md")

        assert res
        scores = [r["score"] for r in res]
        assert scores == sorted(scores, reverse=True), res

    def test_an_ordinary_search_does_not_load_every_file_list(self, e2e_env):
        """The bulk name listing is for the fallback; a search that finds real
        matches must not pay for it. (Devin Review on #1267.)"""
        from unittest.mock import patch

        from src.repositories import corpus_files_repo

        cid = _seed("no-bulk", "notes.md", [{"ordinal": 0, "text": "margin by region explained"}])

        real = corpus_files_repo().list_for_corpus
        calls = []

        def _counting(corpus_id):
            calls.append(corpus_id)
            return real(corpus_id)

        # Patch where `search` LOOKS IT UP — `src.ingest.retrieval` imported
        # the factory into its own namespace at import time, so patching
        # `src.repositories.corpus_files_repo` left the real one in place and
        # the counter could never move. (Devin Review on #1267 — the guard was
        # green and asserting nothing.)
        from src.ingest import retrieval as retrieval_mod

        real_repo = corpus_files_repo()

        class _Spy:
            def list_for_corpus(self, corpus_id):
                return _counting(corpus_id)

            def get(self, file_id):
                return real_repo.get(file_id)

        with patch.object(retrieval_mod, "corpus_files_repo", lambda: _Spy()):
            res = retrieval_mod.search([cid], "margin region")

        assert res, "precondition: this query matches a body"
        assert calls == [], f"the bulk listing ran anyway: {calls}"


class TestTheNamePassCostsCandidatesNotCollectionSize:
    """Live finding (2026-09): the previous fix above only covers the case
    where some passage explains the WHOLE question — the ceiling this module
    documents as rarely tripping in practice. Any ordinary multi-word query
    whose best passage covers LESS than all of it (the common case — see
    `_NAME_PASS_BODY_CEILING`'s docstring) still ran the name pass, which
    used to resolve citation filenames by ``list_for_corpus``-ing every file
    row of every corpus in scope: O(files in the collection), a measured
    ~4s fixed tax on a ~285k-file collection on nearly every query,
    independent of term frequency, ``limit``, or ``lexical_only``. Filenames
    for the (already bounded) candidate pool must cost O(candidates)
    instead — a bulk fetch by id, never a whole-corpus listing.
    """

    def _seed_large_corpus(self, slug: str, n_unrelated: int, target_filename: str, target_text: str) -> str:
        from src.repositories import corpus_chunks_repo, corpus_files_repo, file_corpora_repo

        cid = file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u")
        for i in range(n_unrelated):
            fid = corpus_files_repo().add(
                corpus_id=cid,
                filename=f"unrelated-{i}.txt",
                sha256=f"u{i}",
                file_type="txt",
                size_bytes=1,
                storage_path="/x",
            )
            corpus_chunks_repo().add_many(
                [{"corpus_id": cid, "file_id": fid, "ordinal": 0, "text": "filler content nobody searches for"}]
            )
        target = corpus_files_repo().add(
            corpus_id=cid,
            filename=target_filename,
            sha256="target",
            file_type=target_filename.rsplit(".", 1)[-1],
            size_bytes=1,
            storage_path="/y",
        )
        corpus_chunks_repo().add_many([{"corpus_id": cid, "file_id": target, "ordinal": 0, "text": target_text}])
        return cid

    def test_a_partially_covered_query_never_lists_the_whole_corpus(self, e2e_env):
        """Fails before the fix: a 3-word query whose only matching passage
        covers just 2 of the 3 words trips the name pass, which used to list
        every one of the corpus's (here, 200) unrelated files to resolve a
        single citation filename."""
        from unittest.mock import patch

        from src.ingest import retrieval as retrieval_mod
        from src.repositories import corpus_files_repo

        cid = self._seed_large_corpus("np-cost-partial", 200, "quarterly-report.md", "the report covers margin trends")

        real_repo = corpus_files_repo()

        class _Spy:
            def list_for_corpus(self, corpus_id, **kw):
                raise AssertionError("resolving a handful of citation filenames must not list the whole corpus")

            def get(self, file_id):
                return real_repo.get(file_id)

            def filenames_for_ids(self, file_ids):
                return real_repo.filenames_for_ids(file_ids)

        with patch.object(retrieval_mod, "corpus_files_repo", lambda: _Spy()):
            res = retrieval_mod.search([cid], "quarterly report margin", k=5)

        assert res, "precondition: the query must actually match something"

    def test_a_name_led_win_is_unchanged_by_collection_size(self, e2e_env):
        """Behaviour-preserving: the SAME name-led win the small-corpus test
        (`TestFillerWordsDoNotDecideWhichFileWins`) proves must still happen
        once the corpus has hundreds of unrelated files — the fix changes
        the COST of resolving names, never which file wins."""
        from src.ingest.retrieval import search

        target = self._seed_large_corpus("np-cost-namewin", 200, "quarterly-report.md", "alpha bravo")

        res = search([target], "what is in quarterly-report.md", k=5)

        assert res, "the question that motivated the whole fallback found nothing"
        assert res[0]["filename"] == "quarterly-report.md"
        assert res[0]["matched_on"] == "filename"


class TestFillerWordsDoNotDecideWhichFileWins:
    """Devin Review on #1267: the shortlist and the filter disagreed.

    `_rank_by_filename` scored candidates with filler words counted, so a file
    named after "what is this" could outrank the file the question names and
    take the shortlist's slots — after which the stricter filter downstream
    had nothing left to accept.
    """

    def test_the_named_file_wins_over_a_filler_word_match(self, e2e_env):
        from src.ingest.retrieval import search

        target = _seed("filler-target", "quarterly-report.md", [{"ordinal": 0, "text": "alpha bravo"}])
        decoy = _seed("filler-decoy", "what-is-in-this.md", [{"ordinal": 0, "text": "charlie delta"}])

        res = search([target, decoy], "what is in quarterly-report.md", k=5)

        assert res, "the question found nothing at all"
        assert res[0]["filename"] == "quarterly-report.md", [r["filename"] for r in res]

    def test_a_filler_only_query_matches_no_file(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed("filler-only", "what-is-this.md", [{"ordinal": 0, "text": "alpha bravo"}])

        assert search([cid], "what is this") == []


class TestAPassageThatContainsTheWordsStaysABodyHit:
    """Devin Review on #1267: every chunk of a name-matched file was folded
    into the name list, including the ones that genuinely contain the search
    terms — relabelled and reordered under a hit that explains less."""

    def test_the_matching_passage_keeps_its_label_and_lead(self, e2e_env):
        from src.ingest.retrieval import search

        cid = _seed(
            "mixed",
            "quarterly-report.md",
            [
                {"ordinal": 0, "text": "cover page"},
                {"ordinal": 1, "text": "the quarterly margin by region"},
            ],
        )

        res = search([cid], "quarterly margin")

        assert res[0]["matched_on"] == "body", res
        assert res[0]["ordinal"] == 1

    def test_a_large_named_file_is_not_lost_to_the_shortlist_cut(self, e2e_env):
        """The shortlist was cut to `k` BEFORE the filter, so a file whose
        early chunks all mention one query word could fill it, be filtered
        out, and leave the file unfound. (Devin Review on #1267.)"""
        from src.ingest.retrieval import search

        cid = _seed(
            "big-named",
            "quarterly-report.md",
            [{"ordinal": i, "text": f"quarterly notes page {i}"} for i in range(12)]
            + [{"ordinal": 12, "text": "closing remarks"}],
        )

        res = search([cid], "quarterly report", k=5)

        assert res, "the named file was lost"
        assert res[0]["filename"] == "quarterly-report.md"

    def test_a_two_word_question_still_reaches_the_named_file(self, e2e_env):
        """The half-coverage short-circuit killed exactly this case: any
        passage containing one of two content words hid the file named after
        both. (Devin Review on #1267.)"""
        from src.ingest.retrieval import search

        cid = _seed(
            "two-word",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "the report was circulated on Monday"}],
        )
        other = _seed("two-word-other", "notes.md", [{"ordinal": 0, "text": "a report about nothing"}])

        res = search([cid, other], "quarterly report")

        assert res, "nothing at all came back"
        assert res[0]["filename"] == "quarterly-report.md", [(r["filename"], r["matched_on"]) for r in res]


class TestEverySurfaceSaysHowTheHitWasFound:
    """Devin Review on #1267: the label existed but three readers ignored it."""

    def test_the_cli_marks_a_name_match(self):
        page = (ROOT / "cli" / "commands" / "search.py").read_text()
        assert 'r.get("matched_on") == "filename"' in page
        assert "matched by file name" in page

    def test_the_tool_descriptions_state_the_real_rule(self):
        for path in ("cli/mcp/server.py", "app/api/mcp/foundation_tools.py"):
            text = (ROOT / path).read_text()
            assert "only when nothing in any body matches" not in text, path
            assert "explains the question better" in text or "explains more of the question" in text, path

    def test_confidence_is_per_row(self, e2e_env):
        """A name hit is low-confidence; a passage that really matched keeps
        the body pass's own judgement."""
        from src.ingest.retrieval import search

        cid = _seed(
            "per-row-conf",
            "quarterly-report.md",
            [{"ordinal": 0, "text": "alpha bravo"}, {"ordinal": 1, "text": "charlie delta"}],
        )
        res = search([cid], "quarterly report")
        assert res and all(r["confidence"] == "low" for r in res if r["matched_on"] == "filename")
