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
# Qualified variant for the ``search_by_filename`` JOIN, where ``corpus_files``
# also has ``id``/``corpus_id``/``created_at`` columns.
_SELECT_CC_NO_EMBED = ", ".join(f"cc.{c}" for c in _SELECT_NO_EMBED.split(", "))

# Same 5s budget as src/repositories/facts_pg.py's ``_STATEMENT_TIMEOUT_MS``
# (SET LOCAL statement_timeout idiom) — duplicated per-module like
# ``_EMBED_DIM`` above rather than cross-imported from a sibling repo's
# private constant.
_STATEMENT_TIMEOUT_MS = 5_000

# Ranking cap for ``search_candidates`` (P1 perf fix, 2026-09 — see that
# method's docstring for the live finding). A plain multiplier + floor, not
# a config knob (project rule: defaults live in code, not a speculative new
# setting) — the candidate set fed to ``ts_rank_cd`` is capped at
# ``max(limit * _RANK_CANDIDATE_MULTIPLIER, _RANK_CANDIDATE_FLOOR)`` rows
# regardless of how many rows match the ``tsquery``.
_RANK_CANDIDATE_MULTIPLIER = 4
_RANK_CANDIDATE_FLOOR = 20_000


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

    def list_for_corpus_batch(
        self, corpus_id: str, *, after_id: Optional[str] = None, limit: int
    ) -> List[Dict[str, Any]]:
        """One bounded, keyset-paginated page of a corpus's chunks, ordered
        by ``id`` ascending — the PG twin of
        ``CorpusChunksRepository.list_for_corpus_batch``. See that
        docstring for the full contract (keyset pagination via
        ``after_id``, the memory-bound rationale, TCRD-296 synthesis C.15).
        """
        with self._engine.connect() as conn:
            if after_id is None:
                rows = (
                    conn.execute(
                        sa.text(
                            "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                            "       section_path, page, bbox, metadata, created_at "
                            "FROM corpus_chunks WHERE corpus_id = :corpus_id "
                            "ORDER BY id LIMIT :limit"
                        ),
                        {"corpus_id": corpus_id, "limit": limit},
                    )
                    .mappings()
                    .all()
                )
            else:
                rows = (
                    conn.execute(
                        sa.text(
                            "SELECT id, corpus_id, file_id, ordinal, text, embedding, "
                            "       section_path, page, bbox, metadata, created_at "
                            "FROM corpus_chunks WHERE corpus_id = :corpus_id AND id > :after_id "
                            "ORDER BY id LIMIT :limit"
                        ),
                        {"corpus_id": corpus_id, "after_id": after_id, "limit": limit},
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
        """Cheap ``COUNT(*)`` across several corpora (#2151). On the search
        path this is consulted only when a stopword-only query filled the
        candidate cap (the ``SearchQueryTooBroad`` message carries it) —
        never on an ordinary search. Empty ``corpus_ids`` → 0."""
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
        in memory regardless of corpus size — the caller passes
        ``min(knowledge.retrieval.max_candidate_chunks,
        collections.search_max_chunks)`` (defaults 5000 / 25000; see
        ``src.ingest.retrieval.search_with_meta`` for how the two compose).
        Ranking WITHIN the candidate set (IDF-lexical + cosine fusion) is
        unchanged — it still runs in Python
        (``src.ingest.retrieval.rank_chunks``), just over this bounded set
        instead of the whole corpus.

        Column-pruned like ``list_for_corpora`` (#2151): ``embedding`` is
        always ``None`` on the returned dicts — the retrieval layer fetches
        vectors for its lexical shortlist via ``list_embeddings_for_ids``.

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
        (see that migration's docstring for the operator follow-up). That
        is also why this query deliberately does NOT carry the
        ``SET LOCAL statement_timeout`` the ILIKE-driven methods above do:
        on exactly the instance this fix exists for (index not yet built),
        a 5s budget would turn every search into a typed 503 rather than a
        slow-but-correct answer.

        The bound here is NOT the row ``LIMIT`` — a common single term (a
        production instance measured ~1.07M matches for ``contract`` out of
        14.5M chunks) still matches far more rows than any sane ``limit``,
        and ``ORDER BY ts_rank_cd(...)`` forces Postgres to heap-fetch AND
        re-tokenize (``to_tsvector``) every matching row before ``LIMIT``
        can drop any of them — the GIN index bounds the WHERE clause, not
        the ranking. Measured on that instance: 395s for a 20-row result.
        The actual bound is ``rank_cap`` (``max(limit *
        _RANK_CANDIDATE_MULTIPLIER, _RANK_CANDIDATE_FLOOR)``): an inner
        subquery selects at most ``rank_cap`` matching rows (a real
        optimization fence in Postgres — a subquery with a ``LIMIT`` cannot
        be flattened into the outer query), and only THAT bounded set is
        ranked. Expected on the same instance: well under 2s.

        Trade-off, by construction: when a term matches more than
        ``rank_cap`` chunks, this ranks an arbitrary ``rank_cap``-sized
        subset of the matches, not the globally top-ranked ones — the
        subquery has no ``ORDER BY``, so which rows land in the subset is
        whatever order the planner's scan happens to produce. Accepted
        because (a) ``plainto_tsquery`` is AND-semantics, so a multi-term
        query is already selective enough to stay well under the cap in
        practice, and (b) a single term common enough to blow through
        ``rank_cap`` alone (like "contract" above) carries almost no
        ranking signal to begin with — it appears in a large, roughly
        uniform slice of the corpus, so which slice gets ranked barely
        changes the top results.

        Empty ``corpus_ids`` → ``[]`` without querying, matching
        ``list_for_corpora``.
        """
        if not corpus_ids:
            return []
        rank_cap = max(limit * _RANK_CANDIDATE_MULTIPLIER, _RANK_CANDIDATE_FLOOR)
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        f"SELECT {_SELECT_NO_EMBED} FROM ("
                        f"  SELECT {_SELECT_NO_EMBED} FROM corpus_chunks "
                        "   WHERE corpus_id = ANY(:corpus_ids) "
                        "     AND to_tsvector('simple', text) @@ plainto_tsquery('simple', :query) "
                        "   LIMIT :rank_cap"
                        ") c "
                        "ORDER BY ts_rank_cd(to_tsvector('simple', c.text), plainto_tsquery('simple', :query)) DESC "
                        "LIMIT :limit"
                    ),
                    {"corpus_ids": list(corpus_ids), "query": query, "limit": limit, "rank_cap": rank_cap},
                )
                .mappings()
                .all()
            )
        return [dict(r, embedding=None) for r in rows]

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
        Column-pruned (``embedding`` always ``None``) like every candidate
        fetch here. Empty ``corpus_ids``/``terms`` → ``[]``.
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
            f"SELECT {_SELECT_CC_NO_EMBED} "
            "FROM corpus_chunks cc JOIN corpus_files cf ON cf.id = cc.file_id "
            "WHERE cc.corpus_id = ANY(:corpus_ids) AND (" + " OR ".join(clauses) + ") "
            "ORDER BY cc.file_id, cc.ordinal LIMIT :limit"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r, embedding=None) for r in rows]
