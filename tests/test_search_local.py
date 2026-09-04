"""agnes search --local — hybrid ranking over pulled knowledge artifacts (K3)."""

from unittest.mock import patch

import pytest

CHUNKS = [
    {
        "id": "ck1",
        "corpus_id": "col_a",
        "file_id": "f1",
        "ordinal": 0,
        "text": "invoices are monthly",
        "embedding": None,
        "section_path": None,
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
    {
        "id": "ck2",
        "corpus_id": "col_a",
        "file_id": "f1",
        "ordinal": 1,
        "text": "vacation policy is generous",
        "embedding": None,
        "section_path": None,
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Workspace with one artifact built by the REAL packaging builder —
    schema drift between builder and reader fails here first."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "srv"))
    from src.knowledge_packaging import artifacts_dir, build_artifact

    with (
        patch(
            "src.knowledge_packaging._list_chunk_batch",
            lambda cid, *, after_id, limit: list(CHUNKS) if after_id is None else [],
        ),
        patch("src.knowledge_packaging._list_files", lambda cid: [{"id": "f1", "filename": "handbook.md"}]),
        patch("src.knowledge_packaging._list_corpora", lambda: [{"id": "col_a", "name": "Handbook"}]),
    ):
        build_artifact("col_a")
    ws = tmp_path / "ws"
    kdir = ws / "user" / "knowledge"
    kdir.mkdir(parents=True)
    (artifacts_dir() / "col_a.duckdb").rename(kdir / "col_a.duckdb")
    return ws


def test_local_search_ranks_and_cites(workspace):
    from src.search.local import local_search

    hits = local_search("monthly invoices", workspace=workspace, k=5)
    assert hits and hits[0]["chunk_id"] == "ck1"
    assert hits[0]["type"] == "chunk"
    assert hits[0]["filename"] == "handbook.md"
    assert hits[0]["confidence"] in {"high", "medium", "low"}


def test_no_artifacts_returns_empty(tmp_path):
    from src.search.local import local_search

    assert local_search("anything", workspace=tmp_path) == []


def test_blank_query_returns_empty(workspace):
    from src.search.local import local_search

    assert local_search("   ", workspace=workspace) == []


def test_rank_parity_with_server_engine(workspace):
    """Local ranking == server ranking over the identical candidate set."""
    from src.ingest.retrieval import rank_chunks
    from src.search.local import local_search

    server_top, _conf = rank_chunks(list(CHUNKS), "monthly invoices", k=5)
    local_hits = local_search("monthly invoices", workspace=workspace, k=5)
    assert [c["id"] for _s, c in server_top] == [h["chunk_id"] for h in local_hits]


def test_a_filename_query_works_offline_too(workspace):
    """Devin Review on #1267: offline must not answer "nothing" to a query the
    server answers.

    `handbook` appears in the FILE NAME and in no chunk body. The server's
    `search()` falls back to names; this module promises "the exact same
    ranking behavior", and it backs `agnes search --local` plus the stdio MCP
    fallback — the offline half of the same question.
    """
    from src.search.local import local_search

    hits = local_search("handbook", workspace=workspace, k=5)

    assert hits, "a name-only query found nothing offline"
    assert hits[0]["filename"] == "handbook.md"
    assert hits[0]["matched_on"] == "filename"
    assert hits[0]["confidence"] == "low"


def test_a_body_hit_is_still_labelled_a_body_hit(workspace):
    """The label must distinguish the two, or the combined search's cap on
    name-only hits fires on real matches."""
    from src.search.local import local_search

    hits = local_search("monthly invoices", workspace=workspace, k=5)

    assert hits and hits[0]["matched_on"] == "body"
