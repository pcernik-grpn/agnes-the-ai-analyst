"""knowledge packaging (K3, #798) — per-corpus knowledge.duckdb artifacts.

TCRD-296 synthesis C.15: bounded reads (never a whole corpus's chunks in
memory at once) and a checkpointed, deadline-aware pass (the
``knowledge-packaging`` worker job kind's time budget — see
``app/worker/kinds.py`` and ``tests/test_worker_kinds.py``).
"""

from unittest.mock import patch

import duckdb
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
        "text": "in EUR only",
        "embedding": [0.1] * 384,
        "section_path": "Billing",
        "page": None,
        "bbox": None,
        "metadata": None,
        "created_at": None,
    },
]
FILES = [{"id": "f1", "filename": "billing.md"}]
CORPORA = [{"id": "col_a", "name": "Handbook"}]


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def _list_chunk_batch_fake(chunks):
    """Build a `_list_chunk_batch(corpus_id, *, after_id, limit)` fake that
    pages through a fixed, id-sorted list — same keyset-pagination contract
    the real repo method honors (see src/repositories/corpus_chunks.py)."""
    ordered = sorted(chunks, key=lambda c: str(c.get("id") or ""))

    def _fake(corpus_id, *, after_id, limit):
        page = [c for c in ordered if after_id is None or str(c["id"]) > str(after_id)]
        return page[:limit]

    return _fake


def _patched(chunks=CHUNKS):
    return (
        patch("src.knowledge_packaging._list_chunk_batch", _list_chunk_batch_fake(chunks)),
        patch("src.knowledge_packaging._list_files", lambda cid: list(FILES)),
        patch("src.knowledge_packaging._list_corpora", lambda: list(CORPORA)),
    )


def test_fingerprint_flips_on_content_change():
    from src.knowledge_packaging import corpus_fingerprint

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        a = corpus_fingerprint("col_a")
        b = corpus_fingerprint("col_a")
    changed = [dict(CHUNKS[0], text="invoices are yearly"), CHUNKS[1]]
    p1, p2, p3 = _patched(changed)
    with p1, p2, p3:
        c = corpus_fingerprint("col_a")
    assert a == b
    assert a != c


def test_fingerprint_is_stable_across_batch_sizes():
    """The fingerprint must not depend on how many pages the walk takes —
    a batch_size of 1 (worst case: one chunk per round trip) must hash to
    the same value as a batch_size covering everything in one page."""
    from src.knowledge_packaging import corpus_fingerprint

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        one_page = corpus_fingerprint("col_a", batch_size=500)
        many_pages = corpus_fingerprint("col_a", batch_size=1)
    assert one_page == many_pages


def test_build_artifact_writes_chunks_filename_and_meta(tmp_path):
    from src.knowledge_packaging import artifacts_dir, build_artifact

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        info = build_artifact("col_a")
    path = artifacts_dir() / "col_a.duckdb"
    assert path.exists()
    assert info["chunks"] == 2 and info["md5"] and info["size_bytes"] > 0
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute("SELECT id, filename, text, embedding FROM chunks ORDER BY ordinal").fetchall()
        meta = dict(con.execute("SELECT key, value FROM artifact_meta").fetchall())
    finally:
        con.close()
    assert rows[0][1] == "billing.md"  # filename denormalized
    assert rows[0][3] is None  # NULL embedding survives
    assert len(rows[1][3]) == 384  # vector survives round-trip
    assert meta["kind"] == "chunks" and meta["corpus_id"] == "col_a"
    assert meta["format_version"] == "1"
    assert not list(artifacts_dir().glob("*.tmp"))  # atomic promotion, no debris


def test_build_artifact_pages_through_chunks_in_bounded_batches(tmp_path):
    """A batch_size smaller than the corpus's chunk count must still
    produce a complete, correct artifact — proves the paging loop, not
    just a single-page happy path."""
    from src.knowledge_packaging import artifacts_dir, build_artifact

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        info = build_artifact("col_a", batch_size=1)
    assert info["chunks"] == 2
    con = duckdb.connect(str(artifacts_dir() / "col_a.duckdb"), read_only=True)
    try:
        n = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    finally:
        con.close()
    assert n == 2


def test_pass_builds_then_skips_unchanged_then_rebuilds():
    from src.knowledge_packaging import run_packaging_pass

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        first = run_packaging_pass()
        second = run_packaging_pass()
    assert first["built"] == ["col_a"] and second["built"] == []
    assert second["skipped"] == ["col_a"]
    assert first["interrupted_reason"] is None
    changed = [dict(CHUNKS[0], text="edited"), CHUNKS[1]]
    p1, p2, p3 = _patched(changed)
    with p1, p2, p3:
        third = run_packaging_pass()
    assert third["built"] == ["col_a"]


def test_pass_prunes_artifact_for_deleted_corpus():
    from src.knowledge_packaging import artifacts_dir, load_state, run_packaging_pass

    p1, p2, p3 = _patched()
    with p1, p2, p3:
        run_packaging_pass()
    with (
        patch("src.knowledge_packaging._list_chunk_batch", _list_chunk_batch_fake([])),
        patch("src.knowledge_packaging._list_files", lambda cid: []),
        patch("src.knowledge_packaging._list_corpora", lambda: []),
    ):
        summary = run_packaging_pass()
    assert summary["pruned"] == ["col_a"]
    assert not (artifacts_dir() / "col_a.duckdb").exists()
    assert "col_a" not in load_state()


def test_empty_corpus_builds_empty_artifact_not_error():
    from src.knowledge_packaging import build_artifact

    with (
        patch("src.knowledge_packaging._list_chunk_batch", _list_chunk_batch_fake([])),
        patch("src.knowledge_packaging._list_files", lambda cid: []),
        patch("src.knowledge_packaging._list_corpora", lambda: list(CORPORA)),
    ):
        info = build_artifact("col_a")
    assert info["chunks"] == 0


class TestDeadline:
    """The time budget the ``knowledge-packaging`` worker job kind enforces
    (app/worker/kinds.py) — a pass that hits its deadline mid-sweep stops
    starting new collections, records why, and checkpoints what it already
    finished rather than losing it."""

    _TWO_CORPORA = [{"id": "col_a", "name": "A"}, {"id": "col_b", "name": "B"}]
    _CHUNKS_A = [dict(CHUNKS[0], id="a1", corpus_id="col_a")]
    _CHUNKS_B = [dict(CHUNKS[0], id="b1", corpus_id="col_b")]

    def _patched_two_corpora(self):
        all_chunks = self._CHUNKS_A + self._CHUNKS_B
        return (
            patch("src.knowledge_packaging._list_chunk_batch", _list_chunk_batch_fake(all_chunks)),
            patch("src.knowledge_packaging._list_files", lambda cid: []),
            patch("src.knowledge_packaging._list_corpora", lambda: list(self._TWO_CORPORA)),
        )

    def test_deadline_already_passed_interrupts_before_any_collection(self):
        from src.knowledge_packaging import run_packaging_pass

        p1, p2, p3 = self._patched_two_corpora()
        with p1, p2, p3:
            summary = run_packaging_pass(deadline=0.0)  # time.monotonic() is always >= 0.0

        assert summary["interrupted_reason"] == "timeout"
        assert summary["built"] == []
        assert summary["collections_processed"] == 0
        # Pruning is skipped on an interrupted pass — an untouched corpus
        # must never be treated as "gone".
        assert summary["pruned"] == []

    def test_generous_deadline_completes_normally(self):
        from src.knowledge_packaging import run_packaging_pass

        p1, p2, p3 = self._patched_two_corpora()
        with p1, p2, p3:
            import time

            summary = run_packaging_pass(deadline=time.monotonic() + 60)

        assert summary["interrupted_reason"] is None
        assert set(summary["built"]) == {"col_a", "col_b"}
        assert summary["collections_processed"] == 2

    def test_interrupted_pass_checkpoints_already_built_collections(self):
        """A run that times out after the FIRST collection must persist
        that collection's state — the next run should skip it, not rebuild
        it (state.json is the checkpoint). Uses a deterministic fake clock
        (not real wall-clock timing) so the interruption point is exact:
        the deadline (2.0) is still ahead on the pre-col_a check (0.0) but
        has passed by the pre-col_b check (10.0)."""
        from src.knowledge_packaging import artifacts_dir, load_state, run_packaging_pass

        p1, p2, p3 = self._patched_two_corpora()
        with p1, p2, p3, patch("src.knowledge_packaging.time.monotonic", side_effect=[0.0, 0.0, 10.0, 10.0]):
            summary = run_packaging_pass(deadline=2.0)

        assert summary["interrupted_reason"] == "timeout"
        assert summary["built"] == ["col_a"]
        assert "col_b" not in summary["built"]
        state = load_state()
        assert "col_a" in state  # checkpointed despite the interruption
        assert (artifacts_dir() / "col_a.duckdb").exists()
