"""DuckDB-backed repository for ``corpus_chunks`` (v82).

One row per text chunk extracted from a ``corpus_files`` document.
``embedding`` is left NULL by this repo (populated in Retrieval slice 4).

Template: src/repositories/corpus_files.py.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, List, Optional

import duckdb

_COLS = [
    "id",
    "corpus_id",
    "file_id",
    "ordinal",
    "text",
    "embedding",
    "section_path",
    "page",
    "bbox",
    "metadata",
    "created_at",
]
_SELECT = ", ".join(_COLS)
_EMBED_DIM = 384

# ``list_for_corpora`` is the retrieval CANDIDATE-SET fetch (#2151): every
# accessible chunk's ``embedding FLOAT[384]`` was materialized into Python on
# every search, whether or not anything downstream reads it (measured at
# ~371 MB RSS for 25k chunks by scripts/bench_retrieval.py). It never
# selects the embedding column — a caller that actually wants vectors for a
# bounded id set (the retrieval layer's shortlist re-rank phase) uses
# ``list_embeddings_for_ids`` instead.
_COLS_NO_EMBED = [c for c in _COLS if c != "embedding"]
_SELECT_NO_EMBED = ", ".join(_COLS_NO_EMBED)


class CorpusChunksRepository:
    """DuckDB twin for the ``corpus_chunks`` table."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_many(self, chunks: List[Dict[str, Any]]) -> int:
        """Bulk-insert chunk rows.

        Each dict must contain ``corpus_id``, ``file_id``, ``ordinal``,
        ``text``.  Optional keys: ``embedding`` (list of 384 floats, else NULL),
        ``section_path``, ``page``, ``bbox``, ``metadata``.

        Embeddings are validated to ``_EMBED_DIM`` up front (matching the PG
        sibling) so both backends raise the same ``ValueError`` on a
        wrong-dimension vector before any insert. Inserts run in DuckDB's default
        autocommit mode — like every other repo here. We deliberately do NOT
        wrap them in an explicit transaction: the system DB connection is a
        shared singleton, so a long-held ``BEGIN`` would serialize concurrent
        background-ingest writers and could wrongly fail one. Re-ingest is
        idempotent (the runner clears a file's chunks before re-adding), so a
        partial batch after a mid-loop failure is cleaned up on retry. (The PG
        sibling's ``engine.begin()`` is safe there because it runs on a fresh
        per-call connection, not a shared one.)

        Returns the number of rows inserted.
        """
        if not chunks:
            return 0
        for chunk in chunks:
            emb = chunk.get("embedding")
            if emb is not None and len(emb) != _EMBED_DIM:
                raise ValueError(f"embedding must be {_EMBED_DIM}-dim, got {len(emb)}")
        for chunk in chunks:
            chunk_id = "ck_" + secrets.token_hex(8)
            self.conn.execute(
                "INSERT INTO corpus_chunks "
                "(id, corpus_id, file_id, ordinal, text, embedding, "
                " section_path, page, bbox, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    chunk_id,
                    chunk["corpus_id"],
                    chunk["file_id"],
                    chunk.get("ordinal"),
                    chunk.get("text"),
                    chunk.get("embedding"),
                    chunk.get("section_path"),
                    chunk.get("page"),
                    chunk.get("bbox"),
                    chunk.get("metadata"),
                ],
            )
        return len(chunks)

    def delete_for_file(self, file_id: str) -> None:
        """Remove all chunks for the given file (idempotent)."""
        self.conn.execute("DELETE FROM corpus_chunks WHERE file_id = ?", [file_id])

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_file(self, file_id: str) -> List[Dict[str, Any]]:
        """All chunks for one file, ordered by ordinal."""
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE file_id = ? ORDER BY ordinal",
            [file_id],
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All chunks for an entire corpus, ordered by file_id then ordinal."""
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id = ? ORDER BY file_id, ordinal",
            [corpus_id],
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def list_for_corpora(
        self,
        corpus_ids: List[str],
        *,
        query_terms: Optional[List[str]] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Candidate chunks across several corpora, for retrieval (#2151).

        Column-pruned: never selects ``embedding`` (always ``None`` on the
        returned dicts) — the retrieval layer fetches vectors separately,
        only for a bounded shortlist, via ``list_embeddings_for_ids``.

        ``query_terms`` (optional) applies a SQL-side lexical prefilter —
        keep a chunk whose ``text`` contains ANY listed term (case-
        insensitive substring, ``ILIKE``) — used once a corpus is over the
        server's chunk cap so the DB does the narrowing instead of shipping
        every row to Python. Deliberately over-inclusive (a substring
        match, not the whole-word match the Python ranker applies): a
        prefilter must never exclude a chunk the real ranker would have
        scored, only shrink the set it has to look at. Terms are always
        bound as parameters, never interpolated into the SQL text.

        ``limit`` (optional) caps the row count — paired with
        ``query_terms`` when over cap, otherwise omitted so an under-cap
        corpus is fetched in full (unchanged behavior).

        Empty ``corpus_ids`` → ``[]``.
        """
        if not corpus_ids:
            return []
        placeholders = ", ".join("?" for _ in corpus_ids)
        params: List[Any] = list(corpus_ids)
        where_extra = ""
        if query_terms:
            term_clause = " OR ".join("text ILIKE ?" for _ in query_terms)
            where_extra = f" AND ({term_clause})"
            params.extend(f"%{term}%" for term in query_terms)
        sql = (
            f"SELECT {_SELECT_NO_EMBED} FROM corpus_chunks "
            f"WHERE corpus_id IN ({placeholders}){where_extra} "
            "ORDER BY file_id, ordinal"
        )
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(zip(_COLS_NO_EMBED, r))
            d["embedding"] = None
            out.append(d)
        return out

    def count_for_corpora(self, corpus_ids: List[str]) -> int:
        """Cheap ``COUNT(*)`` across several corpora — the precheck that
        decides whether ``list_for_corpora`` needs the cap/prefilter path
        (#2151). Empty ``corpus_ids`` → 0."""
        if not corpus_ids:
            return 0
        placeholders = ", ".join("?" for _ in corpus_ids)
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM corpus_chunks WHERE corpus_id IN ({placeholders})",
            list(corpus_ids),
        ).fetchone()
        return int(row[0]) if row else 0

    def list_embeddings_for_ids(self, ids: List[str]) -> Dict[str, List[float]]:
        """``{chunk_id: embedding}`` for the given ids that HAVE a stored
        vector (#2151) — phase 2 of the retrieval layer's two-phase hybrid
        fetch: rank lexically over ``list_for_corpora``'s (embedding-less)
        candidates first, then fetch vectors only for that shortlist. An id
        with no stored embedding (or that does not exist) is simply absent
        from the returned mapping. Empty ``ids`` → ``{}``."""
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT id, embedding FROM corpus_chunks WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
        return {r[0]: list(r[1]) for r in rows if r[1] is not None}
