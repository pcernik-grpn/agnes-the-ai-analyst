"""Postgres-backed repository for ``corpus_chunks`` (v82).

Mirrors ``src/repositories/corpus_chunks.py`` (the DuckDB impl) on the
``CorpusChunksRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_corpus_chunks_contract.py``.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

import sqlalchemy as sa
from sqlalchemy.engine import Engine

_EMBED_DIM = 384

# Column-pruned SELECT list for the retrieval candidate-set fetch (#2151) —
# mirrors src/repositories/corpus_chunks.py's ``_SELECT_NO_EMBED``. Never
# selects ``embedding``; ``list_embeddings_for_ids`` is the only path that
# fetches vectors, and only for a caller-bounded id set.
_SELECT_NO_EMBED = "id, corpus_id, file_id, ordinal, text, section_path, page, bbox, metadata, created_at"

# Same 5s budget as src/repositories/facts_pg.py's ``_STATEMENT_TIMEOUT_MS``
# (SET LOCAL statement_timeout idiom) — duplicated per-module like
# ``_EMBED_DIM`` above rather than cross-imported from a sibling repo's
# private constant.
_STATEMENT_TIMEOUT_MS = 5_000


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

    def list_for_corpora(
        self,
        corpus_ids: List[str],
        *,
        query_terms: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Candidate chunks across several corpora, for retrieval (#2151).

        Mirrors ``src/repositories/corpus_chunks.py``'s DuckDB sibling —
        see its docstring for the column-pruning / prefilter / limit
        contract. ``SET LOCAL statement_timeout`` guards the query the same
        way ``src/repositories/facts_pg.py``'s ILIKE-driven candidate scans
        are guarded (an unbounded prefilter scan must not stall a pooled
        connection). DuckDB has no equivalent per-statement wall-clock
        timeout primitive, so that guard is PG-only; the row cap (``limit``)
        is the shared, cross-backend bound both repos apply.
        """
        if not corpus_ids:
            return []
        params: Dict[str, Any] = {"corpus_ids": list(corpus_ids)}
        where_extra = ""
        if query_terms:
            term_clauses = []
            for i, term in enumerate(query_terms):
                key = f"term_{i}"
                term_clauses.append(f"text ILIKE :{key}")
                params[key] = f"%{term}%"
            where_extra = " AND (" + " OR ".join(term_clauses) + ")"
        limit_sql = ""
        if limit is not None:
            params["limit_n"] = int(limit)
            limit_sql = " LIMIT :limit_n"
        sql = sa.text(
            f"SELECT {_SELECT_NO_EMBED} FROM corpus_chunks "
            f"WHERE corpus_id IN :corpus_ids{where_extra} "
            f"ORDER BY file_id, ordinal{limit_sql}"
        ).bindparams(sa.bindparam("corpus_ids", expanding=True))
        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            rows = conn.execute(sql, params).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["embedding"] = None
            out.append(d)
        return out

    def count_for_corpora(self, corpus_ids: List[str]) -> int:
        """Cheap ``COUNT(*)`` across several corpora — the precheck that
        decides whether ``list_for_corpora`` needs the cap/prefilter path
        (#2151). Empty ``corpus_ids`` → 0."""
        if not corpus_ids:
            return 0
        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            row = conn.execute(
                sa.text("SELECT COUNT(*) FROM corpus_chunks WHERE corpus_id IN :corpus_ids").bindparams(
                    sa.bindparam("corpus_ids", expanding=True)
                ),
                {"corpus_ids": list(corpus_ids)},
            ).first()
        return int(row[0]) if row else 0

    def list_embeddings_for_ids(self, ids: List[str]) -> Dict[str, List[float]]:
        """``{chunk_id: embedding}`` for the given ids that HAVE a stored
        vector (#2151) — phase 2 of the retrieval layer's two-phase hybrid
        fetch. An id with no stored embedding (or that does not exist) is
        simply absent from the returned mapping. Empty ``ids`` → ``{}``."""
        if not ids:
            return {}
        with self._engine.begin() as conn:
            conn.execute(sa.text(f"SET LOCAL statement_timeout = {_STATEMENT_TIMEOUT_MS}"))
            rows = conn.execute(
                sa.text("SELECT id, embedding FROM corpus_chunks WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)
                ),
                {"ids": list(ids)},
            ).all()
        return {r[0]: list(r[1]) for r in rows if r[1] is not None}
