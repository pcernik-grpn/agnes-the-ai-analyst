"""An empty collections search must say why, not leave the caller guessing.

Observed on a live instance: a user uploaded one file, the UI showed it as
`indexed` / `Searchable 1 of 1`, and then asked the chat agent what was in
it. The agent ran `collections_search` six times — the question's own words,
the filename, `*`, `md`, `seznam` — got `{"results": [], "retrieval":
"lexical_only"}` every time, tried a `agnes collections cat` that does not
exist, and told the user **"I don't have access to your files or
collections"**. It did have access. That sentence then became the
conversation's permanent title; three more like it were already in the
sidebar, so this is a repeating pattern, not a one-off.

Nothing in an empty result distinguishes "you cannot see any collection"
from "your words are not in the text", so the model picks the scarier
reading. Three properties of the engine make the wrong guesses easy:

  - filenames are NOT indexed (`src/ingest/retrieval.py` ranks chunk text
    only — the filename is attached afterwards, for citations);
  - matching is whole-word, so `test` does not find `Testovaci`;
  - there is no wildcard: `*` and `""` return nothing rather than everything.

The response now carries a `hint` on the empty case naming all three, and
— decisively — how many collections were actually searched, which is what
separates "no access" from "no match".
"""

from __future__ import annotations


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _search(seeded_app, token: str, q: str, **params) -> dict:
    r = seeded_app["client"].get("/api/collections/search", params={"q": q, **params}, headers=_auth(token))
    assert r.status_code == 200, r.text
    return r.json()


def _make_collection_with_file(seeded_app, token: str, name: str, body: str) -> dict:
    c = seeded_app["client"].post("/api/collections", json={"name": name}, headers=_auth(token))
    assert c.status_code == 201, c.text
    col = c.json()
    up = seeded_app["client"].post(
        f"/api/collections/{col['id']}/files",
        files={"files": ("note.md", body.encode(), "text/markdown")},
        headers=_auth(token),
    )
    assert up.status_code == 201, up.text
    return col


class TestEmptyResultCarriesAHint:
    def test_no_match_says_it_is_not_an_access_problem(self, seeded_app):
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        body = _search(seeded_app, tok, "nosuchwordanywhere")
        assert body["results"] == []
        hint = body.get("hint", "")
        assert hint, "empty result carries no hint"
        # The single most important thing to rule out.
        assert "access" in hint.lower()

    def test_hint_reports_how_many_collections_were_searched(self, seeded_app):
        """The number is what makes 'not an access problem' checkable."""
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        body = _search(seeded_app, tok, "nosuchwordanywhere")
        assert body.get("searched_collections", 0) >= 1
        assert str(body["searched_collections"]) in body["hint"]

    def test_hint_names_the_engine_surprises(self, seeded_app):
        """The caveats have to match the engine, not the other way round.

        File names ARE searched now, as a fallback when no body matches, so
        the hint says that instead of the old "not indexed" — a reader steered
        away from a query that works is worse off than one told nothing.
        """
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        hint = _search(seeded_app, tok, "nosuchwordanywhere")["hint"].lower()
        assert "file names are searched" in hint, "must say file names are searched as a fallback"
        assert "not indexed" not in hint, "the stale caveat must not come back"
        assert "whole word" in hint or "whole-word" in hint, "must say matching is whole-word"
        assert "wildcard" in hint or "*" in hint, "must say there is no wildcard"

    def test_a_hit_carries_no_hint(self, seeded_app):
        """The hint is for the dead end only — it must not pad every response."""
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Notes", "alpha bravo charlie")

        body = _search(seeded_app, tok, "bravo")
        assert body["results"], "precondition: this query matches"
        assert "hint" not in body


class TestNoAccessIsDistinguishable:
    def test_a_caller_with_no_collections_is_told_that_instead(self, seeded_app):
        """Zero accessible collections IS the access case — say so plainly.

        Same empty `results`, different diagnosis: the agent must not tell
        this user to rephrase their query, and must not tell the user above
        that they lack access.
        """
        tok = seeded_app["analyst_token"]
        body = _search(seeded_app, tok, "anything")
        assert body["results"] == []
        if body.get("searched_collections", 0) == 0:
            hint = body["hint"].lower()
            assert "no collection" in hint or "not shared" in hint
            # Must NOT send them chasing better search terms.
            assert "whole word" not in hint and "whole-word" not in hint


class TestRetrievalLabelUnchanged:
    def test_retrieval_mode_still_reported(self, seeded_app):
        tok = seeded_app["admin_token"]
        body = _search(seeded_app, tok, "anything")
        assert body["retrieval"] in ("hybrid", "lexical_only")


class TestCombinedKnowledgeSearchCarriesTheSameHint:
    """`/api/knowledge/search` is the surface the in-chat agent actually calls.

    Devin Review on this PR: the stdio `knowledge_search` docstring tells the
    model to "check the ``hint``" before concluding it has no access, but the
    combined endpoint never returned one — only the collections-only sibling
    did. So the tool promised guidance that was not in the response, and an
    empty combined search could still produce the exact "I don't have access
    to your files" answer this change set exists to prevent.

    The counts differ from the collections case on purpose: this leg also
    searches the table catalog and metrics, so zero *collections* is not by
    itself an access problem.
    """

    def _knowledge(self, seeded_app, token: str, q: str) -> dict:
        r = seeded_app["client"].get("/api/knowledge/search", params={"q": q}, headers=_auth(token))
        assert r.status_code == 200, r.text
        return r.json()

    def test_empty_combined_search_says_it_is_not_an_access_problem(self, seeded_app):
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Combined notes", "alpha bravo charlie")

        body = self._knowledge(seeded_app, tok, "nosuchwordanywhere")
        assert body["results"] == []
        assert "searched_collections" in body, "no count to check the access reading against"
        assert "searched_tables" in body
        hint = body.get("hint", "")
        assert hint, "the docstring promises a hint; the response must carry one"
        assert "NOT evidence that access is missing" in hint
        assert "DO have access" not in hint  # must not over-claim the other way
        for caveat in ("file names are searched", "whole word", "wildcard"):
            assert caveat in hint.lower(), f"hint does not name: {caveat}"

    def test_a_non_empty_result_carries_no_hint(self, seeded_app):
        """The hint is for the ambiguous case only — not noise on every call."""
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Findable", "distinctivetoken here")

        body = self._knowledge(seeded_app, tok, "distinctivetoken")
        assert body["results"], "fixture did not produce a hit; the test proves nothing"
        assert "hint" not in body
        assert "searched_collections" not in body


class TestABlankCollectionFilterIsNotAFilter:
    """Devin Review on this PR: `?corpus_id=` produced the wrong sentence.

    `search_collections` narrowed on `corpus_id is not None`, and an empty
    string passes that — which is what an HTML form and most clients send for
    an unset optional. The allowed list narrowed to nothing (no collection has
    the empty id), and the hint's `searched == 0` branch then told a caller
    with plenty of access that no collections were shared with them: the exact
    wrong conclusion this change set exists to prevent, produced by its own
    fix.
    """

    def test_blank_corpus_id_searches_everything_the_caller_can_see(self, seeded_app):
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Blank filter", "alpha bravo charlie")

        body = _search(seeded_app, tok, "nosuchwordanywhere", corpus_id="")

        assert body["searched_collections"] >= 1, "a blank filter narrowed the search to nothing"
        assert "no collections are shared with you" not in body.get("hint", "").lower()
        assert "NOT evidence that access is missing" in body["hint"]
        assert "DO have access" not in body["hint"]

    def test_a_real_corpus_id_still_narrows(self, seeded_app):
        """The filter must keep working — this is not "ignore corpus_id"."""
        tok = seeded_app["admin_token"]
        col = _make_collection_with_file(seeded_app, tok, "Narrowed", "alpha bravo charlie")
        _make_collection_with_file(seeded_app, tok, "Other", "delta echo")

        body = _search(seeded_app, tok, "nosuchwordanywhere", corpus_id=col["id"])
        assert body["searched_collections"] == 1


class TestTheCombinedHintCountsEverySearchedLeg:
    """Devin Review on this PR (second round), on the hint added in the first.

    `_empty_combined_hint` judged "nothing was searched" from documents,
    tables and metrics only. Two things were wrong with that: a caller can
    hold memory-domain grants and none of those three, and the **glossary**
    has no RBAC at all — `unified_search` fetches it for every authenticated
    caller — so something always ran and the claim was never literally true.
    """

    def test_a_caller_who_can_reach_something_gets_the_wording_branch(self, seeded_app):
        tok = seeded_app["admin_token"]
        _make_collection_with_file(seeded_app, tok, "Reachable", "alpha bravo")
        r = seeded_app["client"].get(
            "/api/knowledge/search", params={"q": "nosuchwordanywhere"}, headers=_auth(tok)
        )
        assert r.status_code == 200, r.text
        hint = r.json().get("hint", "")
        assert "NOT evidence that access is missing" in hint
        assert "DO have access" not in hint

    def test_the_empty_branch_reads_correctly_for_an_admin_too(self):
        """An admin's counts ARE the whole instance, so the empty branch can
        reach them — and telling an admin to ask an admin is nonsense. It must
        describe what is loaded, not blame the caller's wording."""
        from app.api.knowledge_search import _empty_combined_hint

        hint = _empty_combined_hint(0, 0, 0)
        assert "shared with you" not in hint
        assert "reachable from this account" in hint
        assert "not about your wording" in hint

    def test_the_no_access_branch_admits_the_other_legs_ran(self):
        """Otherwise the sentence claims a search that did happen did not."""
        from app.api.knowledge_search import _empty_combined_hint

        hint = _empty_combined_hint(0, 0, 0).lower()
        assert "glossary" in hint
        assert "knowledge notes" in hint
        assert "nothing was searched" not in hint

    def test_group_membership_is_not_treated_as_proof_of_access(self):
        """Devin Review, second pass on this same hint.

        Folding the knowledge leg in as a fourth term looked like a fix and
        was worse: the only signal available is group membership, and nearly
        every account is auto-added to the built-in "Everyone" group — so the
        term was true for essentially everyone and the no-access branch became
        unreachable. A brand-new user with nothing shared at all was told to
        try different words.
        """
        import inspect

        from app.api import knowledge_search as mod

        assert "knowledge" not in inspect.signature(mod._empty_combined_hint).parameters, (
            "the hint judges access from a term that is true for nearly every account"
        )
        assert "NOT evidence that access is missing" not in mod._empty_combined_hint(0, 0, 0)

    def test_a_caller_with_any_countable_source_is_a_wording_miss(self):
        from app.api.knowledge_search import _empty_combined_hint

        assert "NOT evidence that access is missing" in _empty_combined_hint(0, 3, 0)


class TestTheHintDoesNotOverClaimAccess:
    """The mirror of this file's founding incident, found in a later audit.

    The original bug: an agent read an empty result as "I don't have access to
    your files" when it plainly did, so the hint began asserting the caller DID
    have access. That fixed the observed case and introduced its opposite —
    the sentence was emitted unconditionally, so a member searching for a term
    that names a real resource nobody had shared with them was told, in the one
    place the product volunteers an opinion about access, that access was not
    the problem. The chat agent repeats it.

    Both readings have to be wrong-proof. The endpoint cannot see the
    un-granted set, so it must not describe it in either direction: it states
    the scope it searched (the count, which is what separates "no access" from
    "no match" — see this file's module docstring) and says an empty result is
    not evidence of missing access, without claiming the caller can reach the
    thing they asked for.
    """

    def test_it_never_asserts_the_caller_has_access_to_the_query(self):
        from app.api.knowledge_search import _empty_combined_hint

        for args in [(0, 0, 0), (1, 0, 0), (0, 3, 0), (2, 5, 1)]:
            hint = _empty_combined_hint(*args)
            assert "DO have access" not in hint, args
            assert "not an access problem" not in hint, args

    def test_it_still_denies_the_no_access_inference_when_something_is_reachable(self):
        """The founding incident: the agent must not conclude it is locked out."""
        from app.api.knowledge_search import _empty_combined_hint

        hint = _empty_combined_hint(1, 0, 0)
        assert "NOT evidence that access is missing" in hint

    def test_it_still_names_what_was_searched(self):
        """The count is the fact that separates the two readings."""
        from app.api.knowledge_search import _empty_combined_hint

        assert "1 collection(s)" in _empty_combined_hint(1, 2, 0)
        assert "2 table(s)" in _empty_combined_hint(1, 2, 0)

    def test_the_nothing_reachable_branch_is_unchanged(self):
        """A caller with nothing shared is a genuine access story, and that
        branch was always correct — it must not inherit the wording language."""
        from app.api.knowledge_search import _empty_combined_hint

        hint = _empty_combined_hint(0, 0, 0)
        assert "reachable from this account" in hint
        assert "not about your wording" in hint
