"""DuckDB-backed repository for ``corpus_chunks`` (v82).

One row per text chunk extracted from a ``corpus_files`` document.
``embedding`` is left NULL by this repo (populated in Retrieval slice 4).

Template: src/repositories/corpus_files.py.
"""

from __future__ import annotations

import re
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
# Qualified variant for the JOIN queries below, where `corpus_files` also has
# `id`/`corpus_id`/`created_at` columns and an unqualified SELECT would be
# ambiguous.
_EMBED_DIM = 384

# The retrieval CANDIDATE-SET fetches (``search_candidates`` /
# ``search_by_filename``, and the older ``list_for_corpora``) are
# column-pruned (#2151): every accessible chunk's ``embedding FLOAT[384]``
# used to be materialized into Python on every search, whether or not
# anything downstream read it (measured at ~371 MB RSS for 25k chunks by
# scripts/bench_retrieval.py). None of them selects the embedding column —
# a caller that actually wants vectors for a bounded id set (the retrieval
# layer's shortlist re-rank phase) uses ``list_embeddings_for_ids`` instead.
_COLS_NO_EMBED = [c for c in _COLS if c != "embedding"]
_SELECT_NO_EMBED = ", ".join(_COLS_NO_EMBED)
# Qualified variant for the JOIN query below, where `corpus_files` also has
# `id`/`corpus_id`/`created_at` columns and an unqualified SELECT would be
# ambiguous.
_SELECT_CC_NO_EMBED = ", ".join(f"cc.{c}" for c in _COLS_NO_EMBED)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Bounds the OR'd ILIKE clause below — a pathologically long query must not
# turn into a pathologically long WHERE clause.
_MAX_ILIKE_TERMS = 16


def _ilike_terms(text: str) -> List[str]:
    """Lowercased, de-duplicated, order-preserving tokens, capped at
    ``_MAX_ILIKE_TERMS``. Tokenizer for the bounded body-candidate query
    below (the filename path receives pre-tokenized terms from the
    caller)."""
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

    def list_text_for_file(self, file_id: str) -> list[str]:
        """Chunk texts for one file in ordinal order — nothing else.

        The whole-file text a preview pages over needs only this column;
        ``list_for_file`` also hauls every chunk's embedding out of the
        database, which a preview never reads.
        """
        rows = self.conn.execute(
            "SELECT text FROM corpus_chunks WHERE file_id = ? ORDER BY ordinal",
            [file_id],
        ).fetchall()
        return [r[0] for r in rows]

    def list_for_corpus(self, corpus_id: str) -> List[Dict[str, Any]]:
        """All chunks for an entire corpus, ordered by file_id then ordinal."""
        rows = self.conn.execute(
            f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id = ? ORDER BY file_id, ordinal",
            [corpus_id],
        ).fetchall()
        return [dict(zip(_COLS, r)) for r in rows]

    def list_for_corpus_batch(
        self, corpus_id: str, *, after_id: Optional[str] = None, limit: int
    ) -> List[Dict[str, Any]]:
        """One bounded, keyset-paginated page of a corpus's chunks, ordered
        by ``id`` ascending (TCRD-296 synthesis C.15 — knowledge-packaging
        memory bound).

        ``list_for_corpus`` materializes an ENTIRE corpus's chunk rows
        (including every ``embedding FLOAT[384]``) in one call — the same
        unbounded-fetch shape ``search_candidates``'s docstring documents an
        OOM incident from on the PG side. This is the bounded alternative a
        caller that must walk a whole corpus (e.g.
        ``src.knowledge_packaging.build_artifact``) uses instead: call
        repeatedly with ``after_id`` set to the previous page's last row's
        ``id`` until a page comes back shorter than ``limit`` (or empty) —
        the caller never holds more than ``limit`` chunk rows in memory at
        once, regardless of corpus size.

        Ordered by ``id`` (not ``file_id, ordinal`` like ``list_for_corpus``)
        because keyset pagination needs a monotonic, unique cursor column —
        ``id`` is the primary key here. Callers that need file/ordinal order
        get it implicitly for free where it matters
        (``src.knowledge_packaging.corpus_fingerprint`` hashes by id already;
        an artifact's insertion order is cosmetic).
        """
        if after_id is None:
            rows = self.conn.execute(
                f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id = ? ORDER BY id LIMIT ?",
                [corpus_id, limit],
            ).fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT {_SELECT} FROM corpus_chunks WHERE corpus_id = ? AND id > ? ORDER BY id LIMIT ?",
                [corpus_id, after_id, limit],
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
        fixes is Postgres-only). Column-pruned like ``list_for_corpora``
        (#2151): ``embedding`` is always ``None`` on the returned dicts —
        the retrieval layer fetches vectors for its shortlist via
        ``list_embeddings_for_ids``. Empty ``corpus_ids`` or a query with
        no indexable tokens → ``[]``.
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
            f"SELECT {_SELECT_NO_EMBED} FROM corpus_chunks "
            f"WHERE corpus_id IN ({placeholders}) AND ({term_clause}) "
            f"ORDER BY file_id, ordinal LIMIT ?",
            params,
        ).fetchall()
        return [dict(zip(_COLS_NO_EMBED, r), embedding=None) for r in rows]

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
        does no NLP of its own, just an OR'd ``ILIKE`` per term.
        Column-pruned (``embedding`` always ``None``) like every candidate
        fetch here. Empty ``corpus_ids``/``terms`` → ``[]``.
        """
        if not corpus_ids or not terms:
            return []
        placeholders = ", ".join("?" for _ in corpus_ids)
        term_clause = " OR ".join("cf.filename ILIKE ?" for _ in terms)
        params: List[Any] = list(corpus_ids) + [f"%{t}%" for t in terms] + [limit]
        rows = self.conn.execute(
            f"SELECT {_SELECT_CC_NO_EMBED} FROM corpus_chunks cc "
            f"JOIN corpus_files cf ON cf.id = cc.file_id "
            f"WHERE cc.corpus_id IN ({placeholders}) AND ({term_clause}) "
            f"ORDER BY cc.file_id, cc.ordinal LIMIT ?",
            params,
        ).fetchall()
        return [dict(zip(_COLS_NO_EMBED, r), embedding=None) for r in rows]
