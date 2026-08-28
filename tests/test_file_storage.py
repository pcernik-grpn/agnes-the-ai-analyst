"""Tests for src/file_storage.py — content-addressed corpus file storage.

TDD-first: written before the implementation.
Async helpers use asyncio.run() — no pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest
from fastapi import UploadFile

from src.corpus_allowlist import MAX_UPLOAD_BYTES


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_upload(data: bytes, filename: str = "test.txt") -> UploadFile:
    """Construct a minimal UploadFile from raw bytes."""
    return UploadFile(filename=filename, file=io.BytesIO(data))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# store_corpus_file tests
# ---------------------------------------------------------------------------


def test_store_returns_storedfile(tmp_path, monkeypatch):
    """Storing bytes returns a StoredFile with correct fields."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import StoredFile, store_corpus_file

    data = b"hello corpus"
    upload = _make_upload(data, "hello.txt")
    result = asyncio.run(store_corpus_file("col_abc123", "hello.txt", upload))

    assert isinstance(result, StoredFile)
    assert result.sha256 == _sha256(data)
    assert result.size_bytes == len(data)
    assert result.ext == ".txt"
    assert result.storage_path.endswith(f"{result.sha256}.txt")


def test_store_writes_file_under_corpus_dir(tmp_path, monkeypatch):
    """File lands at DATA_DIR/file_corpora/<corpus_id>/<sha256>.<ext>."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_file

    data = b"corpus content"
    upload = _make_upload(data, "doc.pdf")
    result = asyncio.run(store_corpus_file("col_myid", "doc.pdf", upload))

    expected_path = tmp_path / "file_corpora" / "col_myid" / f"{result.sha256}.pdf"
    assert expected_path.exists()
    assert expected_path.read_bytes() == data


def test_store_idempotent_same_content(tmp_path, monkeypatch):
    """Uploading the same content twice returns the same path (idempotent)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_file

    data = b"idempotent test bytes"
    result1 = asyncio.run(store_corpus_file("col_1", "file.txt", _make_upload(data, "file.txt")))
    result2 = asyncio.run(store_corpus_file("col_1", "file.txt", _make_upload(data, "file.txt")))

    assert result1.sha256 == result2.sha256
    assert result1.storage_path == result2.storage_path


def test_identical_bytes_in_two_collections_never_share_a_storage_path(tmp_path, monkeypatch):
    """Blobs are content-addressed *within* a corpus dir, never globally —
    the same bytes uploaded to two collections produce two files at two
    distinct paths.

    This is the invariant `corpus_files.count_by_storage_path(corpus_id,
    storage_path)` rests on: because a `storage_path` string is reachable
    only from rows in the corpus whose directory it lives under, a
    corpus-scoped reference count is exact, and the unlink in
    `_upsert_corpus_file` / `_purge_file_row` can never remove a blob another
    collection still points at. Flattening the layout would silently make
    that count wrong and start deleting shared blobs, so pin it here (raised
    as a question in review on #1652)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from pathlib import Path

    from src.file_storage import store_corpus_file

    data = b"the very same bytes"
    a = asyncio.run(store_corpus_file("col_a", "doc.txt", _make_upload(data, "doc.txt")))
    b = asyncio.run(store_corpus_file("col_b", "doc.txt", _make_upload(data, "doc.txt")))

    assert a.sha256 == b.sha256, "content addressing still keys on the digest"
    assert a.storage_path != b.storage_path, "a blob path must be reachable from exactly one corpus"
    assert Path(a.storage_path).parent != Path(b.storage_path).parent
    assert Path(a.storage_path).exists() and Path(b.storage_path).exists()


def test_store_raises_on_oversize(tmp_path, monkeypatch):
    """Uploading a file exceeding MAX_UPLOAD_BYTES raises HTTPException 413."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from fastapi import HTTPException

    from src.file_storage import store_corpus_file

    # Construct an UploadFile that is exactly cap+1 bytes.
    cap = MAX_UPLOAD_BYTES
    oversized = b"x" * (cap + 1)
    upload = _make_upload(oversized, "big.pdf")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(store_corpus_file("col_x", "big.pdf", upload))

    assert exc_info.value.status_code == 413


def test_store_raises_on_empty_file(tmp_path, monkeypatch):
    """Uploading an empty file raises HTTPException 400."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from fastapi import HTTPException

    from src.file_storage import store_corpus_file

    upload = _make_upload(b"", "empty.txt")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(store_corpus_file("col_x", "empty.txt", upload))

    assert exc_info.value.status_code == 400


def test_store_path_traversal_safe(tmp_path, monkeypatch):
    """Filename with path-traversal sequences does not escape the corpus dir."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import store_corpus_file

    data = b"traversal test"
    # A malicious filename with path components — the stored file must still
    # land inside the corpus directory regardless.
    upload = _make_upload(data, "../../etc/passwd.txt")
    result = asyncio.run(store_corpus_file("col_x", "../../etc/passwd.txt", upload))

    stored = tmp_path / "file_corpora" / "col_x" / f"{result.sha256}.txt"
    assert stored.exists(), "file must be stored under the corpus directory"
    # The storage_path must be inside DATA_DIR/file_corpora/
    assert "file_corpora" in result.storage_path
    assert ".." not in result.storage_path


# ---------------------------------------------------------------------------
# delete_corpus_file tests
# ---------------------------------------------------------------------------


def test_delete_removes_existing_file(tmp_path, monkeypatch):
    """delete_corpus_file removes a file that exists."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import delete_corpus_file

    target = tmp_path / "file_corpora" / "col_x"
    target.mkdir(parents=True)
    f = target / "abc123.txt"
    f.write_bytes(b"content")

    delete_corpus_file(str(f))
    assert not f.exists()


def test_delete_noop_on_missing_file(tmp_path, monkeypatch):
    """delete_corpus_file does not raise when the file is already gone."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from src.file_storage import delete_corpus_file

    delete_corpus_file(str(tmp_path / "nonexistent.txt"))  # must not raise
