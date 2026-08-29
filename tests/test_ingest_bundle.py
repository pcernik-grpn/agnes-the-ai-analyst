"""Bundle (zip) unpack + child-row lifecycle (K1)."""

from __future__ import annotations

import io
import zipfile


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _zip_bytes_allow_dup_names(entries: list[tuple[str, bytes]]) -> bytes:
    """Like ``_zip_bytes`` but takes a list, not a dict, so two entries can
    share the exact same member name — a plain ``dict`` literal cannot
    express that, and a real-world zip tool CAN legally produce it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _new_corpus(slug: str) -> str:
    from src.repositories import file_corpora_repo

    return file_corpora_repo().create(name=slug, slug=slug, description=None, created_by="u1")


def _make_archive_row(corpus_id: str, data: bytes, filename: str = "dump.zip") -> tuple[str, str]:
    """Store zip bytes + create the archive corpus_files row; returns (file_id, path)."""
    from src.file_storage import store_corpus_bytes
    from src.repositories import corpus_files_repo

    stored = store_corpus_bytes(corpus_id, filename, data)
    fid = corpus_files_repo().add(
        corpus_id=corpus_id,
        filename=filename,
        sha256=stored.sha256,
        file_type="zip",
        size_bytes=stored.size_bytes,
        storage_path=stored.storage_path,
    )
    return fid, stored.storage_path


def _fake_indexing_ingest(child_id: str) -> str:
    from src.repositories import corpus_files_repo

    corpus_files_repo().set_status(child_id, status="indexed", detail={})
    return "indexed"


def test_bundle_happy_path(e2e_env):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-happy")
    data = _zip_bytes(
        {
            "docs/page.html": b"<html><body><p>hello world</p></body></html>",
            "data/t.csv": b"a,b\n1,2\n",
            "__MACOSX/junk": b"x",
            ".DS_Store": b"x",
        }
    )
    fid, path = _make_archive_row(corpus_id, data)
    seen: list[str] = []

    def fake_ingest(child_id: str) -> str:
        seen.append(child_id)
        return _fake_indexing_ingest(child_id)

    status = ingest_bundle(corpus_id, fid, path, ingest_child=fake_ingest)
    assert status == "indexed"
    kids = corpus_files_repo().list_children(fid)
    assert sorted(k["filename"] for k in kids) == ["data/t.csv", "docs/page.html"]  # junk skipped
    assert len(seen) == 2
    parent = corpus_files_repo().get(fid)
    assert parent["processing_status"] == "indexed"
    assert parent["processing_detail"]["children"] == 2
    assert parent["processing_detail"]["indexed"] == 2


def test_bundle_unsafe_and_nested_members_rejected(e2e_env):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-unsafe")
    data = _zip_bytes(
        {
            "../evil.txt": b"escape",
            "inner.zip": b"PK\x03\x04fakezip",
            "ok.md": b"# fine\ncontent here",
        }
    )
    fid, path = _make_archive_row(corpus_id, data)

    status = ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
    assert status == "indexed"  # ok.md indexed
    kids = {k["filename"]: k for k in corpus_files_repo().list_children(fid)}
    assert kids["../evil.txt"]["processing_status"] == "rejected"
    assert kids["../evil.txt"]["processing_detail"]["reason"] == "unsafe_path"
    assert kids["inner.zip"]["processing_status"] == "rejected"
    assert kids["inner.zip"]["processing_detail"]["reason"] == "nested_archive_unsupported"
    assert kids["ok.md"]["processing_status"] == "indexed"


def test_bundle_unsupported_member_rejected_supported_ingested(e2e_env):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-mixed")
    fid, path = _make_archive_row(corpus_id, _zip_bytes({"cad.dwg": b"binary", "ok.txt": b"text content"}))

    ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
    kids = {k["filename"]: k for k in corpus_files_repo().list_children(fid)}
    assert kids["cad.dwg"]["processing_status"] == "rejected"
    assert kids["cad.dwg"]["processing_detail"]["reason"] == "unsupported_type"
    assert kids["ok.txt"]["processing_status"] == "indexed"


def test_bundle_empty_or_all_rejected_needs_review(e2e_env):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-empty")
    fid, path = _make_archive_row(corpus_id, _zip_bytes({"junk.dwg": b"x"}))
    status = ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
    assert status == "needs_review"
    parent = corpus_files_repo().get(fid)
    assert parent["processing_status"] == "needs_review"
    assert parent["processing_detail"]["reason"] == "no_member_indexed"


def test_bundle_corrupt_zip_rejected(e2e_env):
    from src.file_storage import store_corpus_bytes
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-corrupt")
    stored = store_corpus_bytes(corpus_id, "bad.zip", b"this is not a zip")
    fid = corpus_files_repo().add(
        corpus_id=corpus_id,
        filename="bad.zip",
        sha256=stored.sha256,
        file_type="zip",
        size_bytes=stored.size_bytes,
        storage_path=stored.storage_path,
    )
    assert ingest_bundle(corpus_id, fid, stored.storage_path) == "rejected"
    assert corpus_files_repo().get(fid)["processing_detail"]["reason"] == "invalid_archive"


def test_bundle_member_limits(e2e_env, monkeypatch):
    import src.ingest.bundle as bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-limits")
    monkeypatch.setattr(bundle, "MAX_BUNDLE_MEMBERS", 1)
    fid, path = _make_archive_row(corpus_id, _zip_bytes({"a.txt": b"a", "b.txt": b"b"}))
    assert bundle.ingest_bundle(corpus_id, fid, path) == "rejected"
    assert corpus_files_repo().get(fid)["processing_detail"]["reason"] == "too_many_members"


def test_bundle_total_size_limit(e2e_env, monkeypatch):
    import src.ingest.bundle as bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-size")
    monkeypatch.setattr(bundle, "MAX_BUNDLE_TOTAL_BYTES", 3)
    fid, path = _make_archive_row(corpus_id, _zip_bytes({"a.txt": b"abcdef"}))
    assert bundle.ingest_bundle(corpus_id, fid, path) == "rejected"
    assert corpus_files_repo().get(fid)["processing_detail"]["reason"] == "bundle_too_large"


def test_bundle_reingest_reuses_children(e2e_env):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-reingest")
    fid, path = _make_archive_row(corpus_id, _zip_bytes({"a.md": b"# a\nbody"}))

    ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
    first = corpus_files_repo().list_children(fid)
    ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
    second = corpus_files_repo().list_children(fid)
    assert [k["id"] for k in first] == [k["id"] for k in second]  # rows reused, not duplicated


def test_bundle_confluence_member_normalized(e2e_env):
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-confl")
    page = (
        b"<html><head><title>S : P</title></head><body>"
        b'<div id="breadcrumb-section"><ol><li>Home</li></ol></div>'
        b'<h1 id="title-heading">P</h1><p>real content</p></body></html>'
    )
    fid, path = _make_archive_row(corpus_id, _zip_bytes({"p.html": page}))
    ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
    kid = corpus_files_repo().list_children(fid)[0]
    with open(kid["storage_path"], "rb") as fh:
        stored = fh.read()
    assert b"breadcrumb-section" not in stored
    assert b"real content" in stored


def test_bundle_duplicate_member_name_does_not_orphan_across_resyncs(e2e_env):
    """Review finding 4: two entries in ONE zip sharing the exact same
    member name (legal at the zip format level) both get rejected (same
    unsupported extension), so both are stored with ``sha256=""`` — a plain
    ``dict``-keyed ``prior`` lookup collapses ``(name, "")`` to a single
    entry, so re-ingesting the identical archive used to leave one of the
    two permanently un-reused AND un-pruned: an orphan accumulating once per
    re-sync round. The multi-map ``prior_by_key`` fix pairs same-key rows up
    1:1 in file order, so re-ingesting the SAME bytes must keep exactly the
    same two rows, not three or four."""
    from src.ingest.bundle import ingest_bundle
    from src.repositories import corpus_files_repo

    corpus_id = _new_corpus("bun-dupname")
    data = _zip_bytes_allow_dup_names([("dup.dwg", b"one"), ("dup.dwg", b"two")])
    fid, path = _make_archive_row(corpus_id, data)

    assert ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest) == "needs_review"
    first = corpus_files_repo().list_children(fid)
    assert len(first) == 2, "both duplicate-named rejected entries must get their own row"
    assert {k["processing_detail"]["reason"] for k in first} == {"unsupported_type"}

    # Re-ingest the IDENTICAL bytes three more times — a real re-sync of an
    # unchanged archive. Neither prior round's rows may be dropped nor
    # multiplied: still exactly 2 rows, and (since nothing changed) the
    # exact same two ids, every round.
    first_ids = sorted(k["id"] for k in first)
    for _ in range(3):
        ingest_bundle(corpus_id, fid, path, ingest_child=_fake_indexing_ingest)
        again = corpus_files_repo().list_children(fid)
        assert sorted(k["id"] for k in again) == first_ids, (
            "re-syncing an unchanged archive must neither orphan nor duplicate duplicate-named rejected members"
        )
