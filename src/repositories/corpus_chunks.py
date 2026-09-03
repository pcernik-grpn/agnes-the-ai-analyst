"""DuckDB-backed repository for ``corpus_chunks`` (v82).

One row per text chunk extracted from a ``corpus_files`` document.
``embedding`` is left NULL by this repo (populated in Retrieval slice 4).

Template: src/repositories/corpus_files.py.
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Dict, List

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
# Qualified variant for the JOIN queries below, where `corpus_files` also has
# `id`/`corpus_id`/`created_at` columns and an unqualified SELECT would be
# ambiguous.
_SELECT_CC = ", ".join(f"cc.{c}" for c in _COLS)
_EMBED_DIM = 384

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Bounds the OR'd ILIKE clause below — a pathologically long query must not
# turn into a pathologically long WHERE clause.
_MAX_ILIKE_TERMS = 16


def _ilike_terms(text: str) -> List[str]:
    """Lowercased, de-duplicated, order-preserving tokens, capped at
    ``_MAX_ILIKE_TERMS``. Shared tokenizer for both bounded-candidate
    queries below."""
    seen: List[str] = []
    for t in _TOKEN_RE.findall((text or "").lower()):
        if t not in seen:
            seen.append(t)
        if len(seen) >= _MAX_ILIKE_TERMS:
            break
    return seen


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

    def list_for_corpora(self, corpus_ids: List[str]) -> List[Dict[str, Any]]:
        """All chunks across several corpora (for retrieval). Empty list → []."""
        if not corpus_ids:
            return []
        placeholders = ", ".join("?" for _ in corpus_ids)
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id IN ({placeholders}) ORDER BY file_id, ordinal",
            list(corpus_ids),
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def search_candidates(self, corpus_ids: List[str], query: str, *, limit: int) -> List[Dict[str, Any]]:
        """Bounded, lexically-filtered candidate set for retrieval (P0 OOM
        fix, 2026-09 — see ``CorpusChunksPgRepository.search_candidates``
        for the production incident and the full design).

        Replaces ``list_for_corpora`` on the ``src.ingest.retrieval.search``
        path: DuckDB has no full-text index wired for this table, so this is
        a plain any-term ``ILIKE`` prefilter (each query token OR'd) plus
        ``LIMIT`` rather than ranked FTS — acceptable at the scale a
        DuckDB-backed app-state install reaches (the backend is frozen and
        never grows past what it already is; the 10M-row incident this
        fixes is Postgres-only). Empty ``corpus_ids`` or a query with no
        indexable tokens → ``[]``.
        """
        if not corpus_ids:
            return []
        terms = _ilike_terms(query)
        if not terms:
            return []
        placeholders = ", ".join("?" for _ in corpus_ids)
        term_clause = " OR ".join("text ILIKE ?" for _ in terms)
        params: List[Any] = list(corpus_ids) + [f"%{t}%" for t in terms] + [limit]
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks "
            f"WHERE corpus_id IN ({placeholders}) AND ({term_clause}) "
            f"ORDER BY file_id, ordinal LIMIT ?",
            params,
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def search_by_filename(self, corpus_ids: List[str], terms: List[str], *, limit: int) -> List[Dict[str, Any]]:
        """Bounded candidate set of chunks whose FILE's name matches any of
        ``terms`` (P0 OOM fix, 2026-09).

        Backs the filename fallback (``src.ingest.retrieval.
        apply_filename_fallback``): body-text candidate selection
        (``search_candidates``) is lexical-first over CHUNK TEXT, so a file
        findable only by its NAME — the whole reason the fallback exists,
        see the module docstring in ``src.ingest.retrieval`` — would never
        reach the candidate set on a corpus large enough to hit the cap.
        ``terms`` are pre-tokenized by the caller (stopwords/extensions
        already stripped — see ``retrieval._content_terms``); this method
        does no NLP of its own, just an OR'd ``ILIKE`` per term. Empty
        ``corpus_ids``/``terms`` → ``[]``.
        """
        if not corpus_ids or not terms:
            return []
        placeholders = ", ".join("?" for _ in corpus_ids)
        term_clause = " OR ".join("cf.filename ILIKE ?" for _ in terms)
        params: List[Any] = list(corpus_ids) + [f"%{t}%" for t in terms] + [limit]
        rows = self.conn.execute(
            f"SELECT {_SELECT_CC} FROM corpus_chunks cc "
            f"JOIN corpus_files cf ON cf.id = cc.file_id "
            f"WHERE cc.corpus_id IN ({placeholders}) AND ({term_clause}) "
            f"ORDER BY cc.file_id, cc.ordinal LIMIT ?",
            params,
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]
