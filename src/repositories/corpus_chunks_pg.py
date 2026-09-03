"""Postgres-backed repository for ``corpus_chunks`` (v82).

Mirrors ``src/repositories/corpus_chunks.py`` (the DuckDB impl) on the
``CorpusChunksRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_corpus_chunks_contract.py``.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_EMBED_DIM = 384


class CorpusChunksPgRepository:
    """Postgres twin of ``CorpusChunksRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_many(self, chunks: List[Dict[str, Any]]) -> int:
        """Bulk-insert chunk rows.

        Each dict must contain ``corpus_id``, ``file_id``, ``ordinal``,
        ``text``.  Optional keys: ``embedding`` (list of 384 floats, else NULL),
        ``section_path``, ``page``, ``bbox``, ``metadata``.

        The PG ``embedding`` column is an unbounded ``real[]`` (float4, matching
        the DuckDB ``FLOAT[384]`` storage precision; pgvector is a later option),
        so the dimension is not enforced by the column type on either backend.
        Both repos therefore validate explicitly, up front in a pre-loop pass —
        so a wrong-dimension vector raises the same ``ValueError`` before any
        insert round-trip, mirroring the DuckDB sibling.

        Returns the number of rows inserted.
        """
        if not chunks:
            return 0
        for chunk in chunks:
            embedding = chunk.get("embedding")
            if embedding is not None and len(embedding) != _EMBED_DIM:
                raise ValueError(f"embedding must be {_EMBED_DIM}-dim, got {len(embedding)}")
        with self._engine.begin() as conn:
            for chunk in chunks:
                chunk_id = "ck_" + secrets.token_hex(8)
                embedding = chunk.get("embedding")
                conn.execute(
                    sa.text(
                        "INSERT INTO corpus_chunks "
                        "(id, corpus_id, file_id, ordinal, text, embedding, "
                        " section_path, page, bbox, metadata) "
                        "VALUES (:id, :corpus_id, :file_id, :ordinal, :text, :embedding, "
                        "        :section_path, :page, :bbox, :metadata)"
                    ),
                    {
                        "id": chunk_id,
                        "corpus_id": chunk["corpus_id"],
                        "file_id": chunk["file_id"],
                        "ordinal": chunk.get("ordinal"),
                        "text": chunk.get("text"),
                        "embedding": embedding,
                        "section_path": chunk.get("section_path"),
                        "page": chunk.get("page"),
                        "bbox": chunk.get("bbox"),
                        "metadata": chunk.get("metadata"),
                    },
                )
        return len(chunks)

    def delete_for_file(self, file_id: str) -> None:
        """Remove all chunks for the given file (idempotent)."""
        with self._engine.begin() as conn:
            conn.execute(
                sa.text("DELETE FROM corpus_chunks WHERE file_id = :file_id"),
                {"file_id": file_id},
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_file(self, file_id: str) -> List[Dict[str, Any]]:
        """All chunks for one file, ordered by ordinal."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks WHERE file_id = :file_id ORDER BY ordinal"
                    ),
                    {"file_id": file_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All chunks for an entire corpus, ordered by file_id then ordinal."""
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks WHERE corpus_id = :corpus_id "
                        "ORDER BY file_id, ordinal"
                    ),
                    {"corpus_id": corpus_id},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def list_for_corpora(self, corpus_ids: List[str]) -> List[Dict[str, Any]]:
        """All chunks across several corpora (for retrieval). Empty list → []."""
        if not corpus_ids:
            return []
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks WHERE corpus_id IN :corpus_ids "
                        "ORDER BY file_id, ordinal"
                    ).bindparams(sa.bindparam("corpus_ids", expanding=True)),
                    {"corpus_ids": list(corpus_ids)},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def search_candidates(self, corpus_ids: List[str], query: str, *, limit: int) -> List[Dict[str, Any]]:
        """Bounded, server-ranked candidate set for retrieval (P0 OOM fix,
        2026-09).

        Live finding: an admin caller's ``GET /api/knowledge/search`` (also
        ``/api/collections/search`` and the MCP tools over the same
        function) resolved every collection it may access — on a
        production instance, ~390 SharePoint-derived collections — and
        ``list_for_corpora`` loaded EVERY chunk row across all of them
        before a single one was scored: a ~10M-row / 12GB table, a
        100+ second sequential scan, and ~10M materialized Python dicts
        that pushed uvicorn to 16.7GB RSS and got it OOM-killed (13 restarts
        in 75 minutes while users retried).

        This method pushes candidate SELECTION into SQL instead: Postgres
        full-text search (``to_tsvector('simple', text) @@
        plainto_tsquery('simple', :query)``), ranked by ``ts_rank_cd``,
        ``LIMIT :limit``. The process never holds more than ``limit`` rows
        in memory regardless of corpus size — bounded by
        ``knowledge.retrieval.max_candidate_chunks`` (default 5000; see
        ``src.ingest.retrieval._max_candidate_chunks``). Ranking WITHIN the
        candidate set (IDF-lexical + cosine fusion) is unchanged — it still
        runs in Python (``src.ingest.retrieval.rank_chunks``), just over
        this bounded set instead of the whole corpus.

        Trade-off, by construction: a query with literally no term overlap
        with any candidate's body text returns no rows here even if some
        chunk's EMBEDDING would have matched by pure cosine similarity —
        there is no vector index behind this table (``embedding`` is a
        plain ``real[]``; ``pgvector`` is a documented future option, see
        ``src.models.collections``), so a corpus-wide unindexed vector scan
        is exactly the unbounded-memory shape this method exists to avoid.
        The filename fallback is unaffected: it has its own bounded
        candidate path, ``search_by_filename``.

        A GIN expression index (``idx_corpus_chunks_text_fts``, migration
        ``0101_corpus_chunks_fts_index``) speeds this query up but is
        not required for correctness — it may be absent on an instance
        whose table was too large to build it in-place at migration time
        (see that migration's docstring for the operator follow-up).

        Empty ``corpus_ids`` → ``[]`` without querying, matching
        ``list_for_corpora``.
        """
        if not corpus_ids:
            return []
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                        "       section_path, page, bbox, metadata, created_at "
                        "FROM corpus_chunks "
                        "WHERE corpus_id = ANY(:corpus_ids) "
                        "  AND to_tsvector('simple', text) @@ plainto_tsquery('simple', :query) "
                        "ORDER BY ts_rank_cd(to_tsvector('simple', text), plainto_tsquery('simple', :query)) DESC "
                        "LIMIT :limit"
                    ),
                    {"corpus_ids": list(corpus_ids), "query": query, "limit": limit},
                )
                .mappings()
                .all()
            )
        return [dict(r) for r in rows]

    def search_by_filename(self, corpus_ids: List[str], terms: List[str], *, limit: int) -> List[Dict[str, Any]]:
        """Bounded candidate set of chunks whose FILE's name matches any of
        ``terms`` (P0 OOM fix, 2026-09).

        Backs the filename fallback (``src.ingest.retrieval.
        apply_filename_fallback``) — see the DuckDB sibling's docstring for
        why this is a SEPARATE bounded path from ``search_candidates``
        rather than reusing its (body-text-filtered) result. ``terms`` are
        pre-tokenized by the caller (stopwords/bare extensions already
        stripped — ``retrieval._content_terms``); a plain-``ILIKE``-per-term
        match against ``corpus_files.filename`` rather than Postgres FTS —
        deliberately, so filename matching behaves identically on both
        backends instead of depending on how ``plainto_tsquery`` happens to
        tokenize punctuation-heavy filenames (``quarterly-report.md``).
        Empty ``corpus_ids``/``terms`` → ``[]``.
        """
        if not corpus_ids or not terms:
            return []
        params: Dict[str, Any] = {"corpus_ids": list(corpus_ids), "limit": limit}
        clauses = []
        for i, term in enumerate(terms):
            key = f"t{i}"
            clauses.append(f"cf.filename ILIKE :{key}")
            params[key] = f"%{term}%"
        sql = (
            "SELECT cc.id, cc.corpus_id, cc.file_id, cc.ordinal, cc.text, cc.embedding, "
            "       cc.section_path, cc.page, cc.bbox, cc.metadata, cc.created_at "
            "FROM corpus_chunks cc JOIN corpus_files cf ON cf.id = cc.file_id "
            "WHERE cc.corpus_id = ANY(:corpus_ids) AND (" + " OR ".join(clauses) + ") "
            "ORDER BY cc.file_id, cc.ordinal LIMIT :limit"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r) for r in rows]
