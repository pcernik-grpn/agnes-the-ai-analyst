"""Tests for src.ingest.embeddings — query-time ``encode()`` must never raise
uncaught into a search request (#2151)."""

from __future__ import annotations


class _BoomModel:
    """A loaded model whose ``encode`` fails at inference time — the class
    of failure ``model.encode`` was previously unguarded against (bad input,
    or a genuine ``MemoryError`` from the C++/torch runtime under load)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def encode(self, *_args, **_kwargs):
        raise self._exc


def test_embed_texts_returns_none_when_encode_raises(monkeypatch):
    """A ``.encode()`` failure must degrade to lexical-only — never propagate
    and take the whole search request down with it (#2151), mirroring the
    ingest side's best-effort embedding contract
    (``src.ingest.runner._chunk_embed_store``)."""
    import src.ingest.embeddings as embeddings

    monkeypatch.setattr(embeddings, "_model", _BoomModel(RuntimeError("boom")))
    assert embeddings.embed_texts(["hello"]) is None


def test_embed_query_returns_none_when_encode_raises(monkeypatch):
    import src.ingest.embeddings as embeddings

    monkeypatch.setattr(embeddings, "_model", _BoomModel(RuntimeError("boom")))
    assert embeddings.embed_query("hello") is None


def test_embed_texts_memory_error_is_also_guarded(monkeypatch):
    """``MemoryError`` is a real, expected failure mode under load (#2151)
    and must degrade the same way as any other encode exception."""
    import src.ingest.embeddings as embeddings

    monkeypatch.setattr(embeddings, "_model", _BoomModel(MemoryError("out of memory")))
    assert embeddings.embed_texts(["hello"]) is None


def test_embed_texts_encode_failure_self_corrects_capability(monkeypatch):
    """After an encode failure, ``embedding_capability()`` must report
    False — the same self-correcting contract its docstring already
    documents for a LOAD-time failure, extended to an ENCODE-time one so a
    response's ``retrieval`` label (computed right after ranking runs)
    matches what actually happened for THIS query."""
    import src.ingest.embeddings as embeddings

    monkeypatch.setattr(embeddings, "_model", _BoomModel(RuntimeError("boom")))
    embeddings.embed_texts(["hello"])
    assert embeddings.embedding_capability() is False


def test_embed_texts_success_path_unaffected(monkeypatch):
    """The guard must not change anything about a normal, successful encode."""
    import src.ingest.embeddings as embeddings

    class _OkModel:
        def encode(self, texts, normalize_embeddings=True):
            return [[float(i)] * embeddings.EMBED_DIM for i, _ in enumerate(texts)]

    monkeypatch.setattr(embeddings, "_model", _OkModel())
    out = embeddings.embed_texts(["a", "b"])
    assert out == [[0.0] * embeddings.EMBED_DIM, [1.0] * embeddings.EMBED_DIM]
    assert embeddings.embedding_capability() is True
