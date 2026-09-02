"""A failed file count must not read as "this collection is empty".

`knowledge_sources_for` shows the agent builder every collection the caller
reaches, each labelled with how many files it holds. The count is metadata:
losing it should cost the label, never the row. Swallowing the repository
error into an empty mapping labelled every reachable collection "0 files",
so one transient failure made a populated Library look empty — and an empty
Library reads as an access problem, which is the expensive thing to debug
(Devin Review on #2062).
"""

from __future__ import annotations

import pytest

from app.services.agent_ingredients import knowledge_sources_for


class _CountBlowsUp:
    def count_by_corpus(self):
        raise RuntimeError("count backend unavailable")


class _CountWorks:
    def count_by_corpus(self):
        return {"col-1": 3}


@pytest.fixture
def one_collection(monkeypatch):
    """Two empty stacks and a single reachable collection, so the assertions
    below are about the artefact section and nothing else."""
    import src.repositories as repos
    from app.services import stack_resolver

    monkeypatch.setattr(stack_resolver.StackResolver, "stack", lambda self, *a, **k: [])
    monkeypatch.setattr("app.auth.access.accessible_collection_ids", lambda user: None)
    monkeypatch.setattr(
        repos,
        "file_corpora_repo",
        lambda: type(
            "R", (), {"list_all": staticmethod(lambda: [{"id": "col-1", "name": "Handbook"}])}
        )(),
    )
    return monkeypatch


def test_collection_survives_a_failed_count(one_collection):
    one_collection.setattr("src.repositories.corpus_files_repo", lambda: _CountBlowsUp())

    rows = [r for r in knowledge_sources_for({"id": "u1"}) if r["kind"] == "file"]

    assert [r["id"] for r in rows] == ["col-1"], "a lost count must not drop the collection"
    assert "0 file" not in rows[0]["meta"], (
        "a count that could not be read is not a count of zero — labelling the "
        "collection empty is the bug this test exists for"
    )
    assert rows[0]["meta"] == "file count unavailable"


def test_working_count_is_still_reported(one_collection):
    one_collection.setattr("src.repositories.corpus_files_repo", lambda: _CountWorks())

    rows = [r for r in knowledge_sources_for({"id": "u1"}) if r["kind"] == "file"]

    assert rows[0]["meta"] == "3 files"
