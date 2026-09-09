"""Postgres-backed repository for ``corpus_chunks`` (v82).

Mirrors ``src/repositories/corpus_chunks.py`` (the DuckDB impl) on the
``CorpusChunksRepository`` public surface. Cross-engine parity is covered
by ``tests/db_pg/test_corpus_chunks_contract.py``.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

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

# The tokenizer config shared by everything full-text on this table: the
# ``0101`` GIN index expression, the WHERE predicate in ``search_candidates``,
# the stored ``tsv`` column ``add_many`` writes (migration
# ``0114_corpus_chunks_tsv``) and the per-row fallback that stands in for it.
# A vector built with any other config would rank differently from the
# expression it replaces, so this is one literal, used everywhere.
_FTS_CONFIG = "simple"

# How many DISTINCT query terms the any-term top-up pass (see
# ``search_candidates``) will fan out over — one bounded index scan each, so
# a pathologically long question must not turn into a pathologically wide
# UNION. Deliberately well below the sibling term bounds (16, in this
# module's DuckDB twin and in ``retrieval._MAX_FILENAME_TERMS``): the cost
# here is per-term SCANS rather than per-term WHERE clauses, and the GIN
# index that makes each one cheap may be absent on a given instance (see
# ``search_candidates``).
#
# Trade-off, stated rather than hidden: terms past the 8th get no window of
# their own, in first-seen order — so on a question long enough to reach
# this bound, a discriminating term late in the sentence depends on the
# ALL-terms pass to surface its chunks. Not reordered by any rarity
# heuristic on purpose: the only stopword vocabulary in this codebase lives
# in ``src.ingest.retrieval``, which imports THIS module, and a hand-copied
# second list is the kind of duplication that drifts silently.
_MAX_FTS_TERMS = 8


def _fts_terms(query: str) -> list[str]:
    """Distinct word tokens of ``query``, order-preserving, bounded.

    Mirrors ``src.ingest.retrieval._tokenize``'s ``[a-z0-9]+`` alphabet
    deliberately: these tokens are handed to ``plainto_tsquery`` one at a
    time, so a token that the Python ranker would never score is a term this
    pass should not spend an index scan on either.
    """
    seen: set[str] = set()
    out: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", (query or "").lower()):
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
        if len(out) >= _MAX_FTS_TERMS:
            break
    return out


class CorpusChunksPgRepository:
    """Postgres twin of ``CorpusChunksRepository``."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def add_many(self, chunks: list[dict[str, Any]]) -> int:
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

        Also writes ``tsv`` — the tokenized body,
        ``to_tsvector('simple', text)`` — from the insert itself (migration
        ``0114_corpus_chunks_tsv``), so a new row is rankable from its
        stored vector without any backfill; see ``search_candidates`` for
        why ranking reads that column and how a row without one (written
        before the column existed, not yet backfilled) still ranks. The
        DuckDB sibling has no such column (frozen backend, no ranking).

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
                        "(id, corpus_id, file_id, ordinal, text, tsv, embedding, "
                        " section_path, page, bbox, metadata) "
                        "VALUES (:id, :corpus_id, :file_id, :ordinal, :text, "
                        f"        to_tsvector('{_FTS_CONFIG}', :tsv_text), :embedding, "
                        "        :section_path, :page, :bbox, :metadata)"
                    ),
                    {
                        "id": chunk_id,
                        "corpus_id": chunk["corpus_id"],
                        "file_id": chunk["file_id"],
                        "ordinal": chunk.get("ordinal"),
                        "text": chunk.get("text"),
                        # The same value bound twice under two names on purpose:
                        # psycopg deduces one parameter's type from every place
                        # it appears, and the varchar column vs. to_tsvector's
                        # text argument disagree ("inconsistent types deduced").
                        "tsv_text": chunk.get("text"),
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

    def reassign_file_corpus(
        self, file_id: str, target_corpus_id: str, *, expected_corpus_id: str | None = None
    ) -> int:
        """Repoint one file's chunks at the collection it now lives in; return
        the count. See the DuckDB twin for why the column must follow the file,
        and what ``expected_corpus_id`` (a compare-and-set) is for.
        """
        sql = "UPDATE corpus_chunks SET corpus_id = :target WHERE file_id = :file_id"
        params: dict[str, Any] = {"target": target_corpus_id, "file_id": file_id}
        if expected_corpus_id is not None:
            sql += " AND corpus_id = :expected"
            params["expected"] = expected_corpus_id
        with self._engine.begin() as conn:
            res = conn.execute(sa.text(sql), params)
            return int(res.rowcount or 0)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def list_for_file(self, file_id: str) -> list[dict[str, Any]]:
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

    def list_text_for_file(self, file_id: str) -> list[str]:
        """Chunk texts for one file in ordinal order — nothing else.

        The whole-file text a preview pages over needs only this column;
        ``list_for_file`` also hauls every chunk's embedding out of the
        database, which a preview never reads.
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.text("SELECT text FROM corpus_chunks WHERE file_id = :file_id ORDER BY ordinal"),
                {"file_id": file_id},
            ).all()
        return [r[0] for r in rows]

    def list_for_corpus(self, corpus_id: str) -> list[dict[str, Any]]:
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

    def list_for_corpus_batch(self, corpus_id: str, *, after_id: str | None = None, limit: int) -> list[dict[str, Any]]:
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
        corpus_ids: list[str],
        *,
        query_terms: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
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
        params: dict[str, Any] = {"corpus_ids": list(corpus_ids)}
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

    def count_for_corpora(self, corpus_ids: list[str]) -> int:
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

    def list_embeddings_for_ids(self, ids: list[str]) -> dict[str, list[float]]:
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

    def search_candidates(
        self, corpus_ids: list[str], query: str, *, limit: int, path_prefix: str | None = None
    ) -> list[dict[str, Any]]:
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

        Two passes, ALL-terms then ANY-term (2026-09)
        ---------------------------------------------
        ``plainto_tsquery`` is AND-semantics: every term must occur in the
        SAME chunk. As the only candidate pass that made a multi-word
        natural-language question — the ordinary shape of a question an
        agent asks — return nothing at all on a corpus that plainly
        contained the answer. Observed live: four of six searches in one
        session came back ``results: []``, among them "Riveron AI Execution
        workstreams scope pods sprint"; no single chunk carried all seven
        words, so candidate selection was empty and the document the agent
        was asked to write from was never read. It also destroyed the
        RANKING signal in the pass that did return rows: AND-semantics
        guarantees every candidate contains every query term, so
        ``rank_chunks``'s IDF-weighted overlap scored every one of them
        identically and min-max normalization flattened the lot to exactly
        1.0 — order by chunk id, not by relevance.

        So the AND pass runs FIRST and unchanged (it is the most precise
        candidate set there is, and when it fills ``limit`` nothing else
        runs — the hot path is untouched), and only when it under-fills is
        it TOPPED UP by an any-term pass: one bounded subquery PER term,
        ``UNION ALL``-ed, de-duplicated, then ranked by ``ts_rank_cd``
        against the OR'd tsquery.

        Per-term windows rather than one OR'd query, deliberately: a single
        ``LIMIT`` over an OR'd match lets the corpus's most COMMON term fill
        the whole window (``ai`` matched ~everything in the session above)
        and crowd out the rare term that actually identifies the document
        (``riveron``). A window each guarantees every term contributes
        candidates regardless of how common its neighbours are; which term
        is the discriminating one is then decided by ``rank_chunks``'s IDF
        over the returned set, which is where that judgment belongs.

        Ranking reads the STORED tokenized body (``tsv``, migration
        ``0114_corpus_chunks_tsv``) rather than re-tokenizing ``text``
        (perf follow-up, 2026-09). With the GIN index bounding the WHERE
        clause, the remaining cost of this query was
        ``to_tsvector('simple', c.text)`` evaluated once per matched row
        inside the ORDER BY — measured on a 15M-row instance at 2 s for a
        two-term query with ~4 500 matches, all from shared buffers,
        against 40 ms for a three-term query with a handful (the cost is
        per matched row, not per query). ``tsv`` is written by
        ``add_many`` for every new row and backfilled for older ones (in
        place by the migration on a small table, by
        ``scripts/backfill_corpus_chunks_tsv.py`` on a large one), and the
        ORDER BY wraps it in ``COALESCE(tsv, to_tsvector('simple', text))``
        so a row the backfill has not reached yet ranks exactly as before
        — slower, never wrong, never missing; a partially backfilled table
        is a normal state. The WHERE clause stays on the
        ``to_tsvector('simple', text)`` EXPRESSION on purpose: that is what
        the ``0101`` GIN index is over (an index on the column would not
        serve it), and it is what a NULL-``tsv`` row still satisfies —
        ``tsv @@ query`` would silently drop every row not yet backfilled.

        The ranking sorts NARROW rows, then fetches columns for the top
        ``limit`` only (the ``matched`` → ``ranked`` → join-back shape).
        Sorting the full candidate rows — ``text`` included — was the other
        half of the cost: a few thousand chunk-sized rows exceed the default
        4 MB ``work_mem``, so the top-``limit`` sort ran as an on-disk
        external merge on every query (measured on a 400k-row bench: 8.6 MB
        of temp files for 8 000 candidates, vs a 755 kB in-memory quicksort
        over ``(id, rank)``). The join-back is ``limit`` primary-key
        lookups, milliseconds. Each CTE is referenced once, so Postgres
        inlines both; the ``LIMIT`` inside each is still the optimization
        fence that bounds what gets ranked and what gets fetched. The
        returned dicts carry the same column-pruned set as every other
        candidate fetch here — ``tsv`` and ``rank`` never leave the query.

        Empty ``corpus_ids`` → ``[]`` without querying, matching
        ``list_for_corpora``.
        """
        if not corpus_ids:
            return []
        rank_cap = max(limit * _RANK_CANDIDATE_MULTIPLIER, _RANK_CANDIDATE_FLOOR)
        scope_sql, scope_params = self._path_prefix_clause(path_prefix, alias="")
        params: dict[str, Any] = {
            "corpus_ids": list(corpus_ids),
            "query": query,
            "limit": limit,
            "rank_cap": rank_cap,
            **scope_params,
        }
        with self._engine.connect() as conn:
            rows = (
                conn.execute(
                    sa.text(
                        "WITH matched AS ("
                        "  SELECT id, tsv, text FROM corpus_chunks "
                        "  WHERE corpus_id = ANY(:corpus_ids) "
                        f"    AND to_tsvector('{_FTS_CONFIG}', text) @@ plainto_tsquery('{_FTS_CONFIG}', :query) "
                        f"    {scope_sql} "
                        "  LIMIT :rank_cap"
                        "), ranked AS ("
                        f"  SELECT id, ts_rank_cd(COALESCE(tsv, to_tsvector('{_FTS_CONFIG}', text)), "
                        f"                        plainto_tsquery('{_FTS_CONFIG}', :query)) AS rank "
                        "  FROM matched ORDER BY rank DESC LIMIT :limit"
                        ") "
                        f"SELECT {_SELECT_CC_NO_EMBED} FROM ranked "
                        "JOIN corpus_chunks cc ON cc.id = ranked.id "
                        "ORDER BY ranked.rank DESC"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            out = [dict(r, embedding=None) for r in rows]
            if len(out) >= limit:
                return out

            terms = _fts_terms(query)
            if len(terms) < 2:
                # A single-term query's AND pass IS its any-term pass —
                # re-running it would return the identical rows.
                return out
            out.extend(
                self._any_term_candidates(
                    conn,
                    corpus_ids,
                    terms,
                    limit=limit - len(out),
                    rank_cap=rank_cap,
                    exclude_ids=[str(r["id"]) for r in out],
                    path_prefix=path_prefix,
                )
            )
        return out

    @staticmethod
    def _path_prefix_clause(path_prefix: str | None, *, alias: str) -> tuple[str, dict[str, Any]]:
        """``(extra WHERE fragment, params)`` narrowing a chunk query to files
        under ``path_prefix`` — ``("", {})`` when no prefix is given.

        Corpus scoping (2026-09). One collection routinely holds every
        client's files at once — a crawled ``00_Customers`` bucket is one
        collection with every engagement's invoices, deal files and
        contracts in it — so "search my accessible collections" was the only
        available scope even when the caller knew the answer lived under one
        folder, and one client's document had to out-rank thousands of
        chunks of everyone else's invoices to be seen. A prefix match on
        ``corpus_files.path`` narrows candidate SELECTION instead of hoping
        ranking sorts it out.

        A prefix is matched literally: LIKE metacharacters in it are escaped
        (``_`` is common in real folder names — ``00_Customers/…`` — and an
        unescaped one is a single-character wildcard).

        ``alias`` is the ``corpus_chunks`` alias to correlate on (``""`` for
        an unaliased table, ``"cc"`` inside the filename JOIN).
        """
        prefix = (path_prefix or "").strip()
        if not prefix:
            return "", {}
        col = f"{alias}.file_id" if alias else "file_id"
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clause = (
            f"AND {col} IN (SELECT id FROM corpus_files "
            "WHERE corpus_id = ANY(:corpus_ids) AND path LIKE :path_prefix ESCAPE '\\')"
        )
        return clause, {"path_prefix": f"{escaped}%"}

    def _any_term_candidates(
        self,
        conn: Any,
        corpus_ids: list[str],
        terms: list[str],
        *,
        limit: int,
        rank_cap: int,
        exclude_ids: list[str],
        path_prefix: str | None = None,
    ) -> list[dict[str, Any]]:
        """The any-term top-up pass — see ``search_candidates``'s docstring.

        One ``LIMIT``-ed subquery per term (each its own index scan, so a
        rare term is never crowded out by a common one), ``UNION ALL``-ed,
        de-duplicated by chunk id, then ranked against the OR'd tsquery.
        Every term goes through ``plainto_tsquery`` — never a hand-built
        ``to_tsquery`` string — so a token carrying tsquery syntax
        (``&``, ``!``, ``:*``) is data, not an operator.

        Ranks exactly the way the all-terms pass does, and for the same
        reasons (see ``search_candidates``): off the STORED ``tsv`` column
        with ``COALESCE(tsv, to_tsvector(...))`` so a row the backfill has
        not reached still ranks rather than dropping out, while the WHERE
        clause stays on the ``to_tsvector(text)`` EXPRESSION the ``0101``
        GIN index is built over. Narrow rows are sorted and only the top
        ``limit`` fetched, so the two passes cannot rank the same corpus by
        different rules or pay different costs.

        De-duplication is ``GROUP BY id`` with ``MAX(rank)`` rather than
        ``DISTINCT ON``: a chunk's rank is a function of its own row, so
        every duplicate of an id carries the identical value and ``MAX`` is
        just the dedupe. ``DISTINCT ON (id)`` would force ``ORDER BY id``
        first and cost another nesting level to get back to rank order.

        A FINAL any-term leg joins the per-term ones, and it is not
        redundant: the per-term windows are ``rank_cap / len(terms)`` rows
        each, so a query whose matches all sit under ONE term could only
        ever return that term's share — measured at 1 row of a requested 6
        with 30 chunks matching. The caller infers "was the scan capped"
        from ``len(rows) >= limit``, so that short result was then reported
        as complete: the cap silently ate matching documents and said
        nothing, which is the exact failure this whole change set exists to
        remove. The extra leg carries the OR'd query under the caller's own
        ``limit``, so the window always fills from whichever term actually
        has the matches while the per-term legs keep the rare term
        represented. Bounded by ``rank_cap + limit`` rows, one query.
        (Devin Review on #2420; the DuckDB sibling had this top-up from the
        start, so this also closes a parity gap between the two.)
        """
        if limit <= 0 or not terms:
            return []
        per_term = max(1, rank_cap // len(terms))
        scope_sql, scope_params = self._path_prefix_clause(path_prefix, alias="")
        params: dict[str, Any] = {
            "corpus_ids": list(corpus_ids),
            "limit": limit,
            "per_term": per_term,
            **scope_params,
        }
        legs = []
        for i, term in enumerate(terms):
            params[f"t{i}"] = term
            legs.append(
                "(SELECT id, tsv, text FROM corpus_chunks "
                " WHERE corpus_id = ANY(:corpus_ids) "
                f"  AND to_tsvector('{_FTS_CONFIG}', text) @@ plainto_tsquery('{_FTS_CONFIG}', :t{i}) "
                f"  {scope_sql} "
                " LIMIT :per_term)"
            )
        or_query = " || ".join(f"plainto_tsquery('{_FTS_CONFIG}', :t{i})" for i in range(len(terms)))
        # The fill leg — see the docstring. Under the caller's `limit`, not
        # `per_term`, because its whole job is to make the window fillable
        # when the per-term shares cannot fill it.
        legs.append(
            "(SELECT id, tsv, text FROM corpus_chunks "
            " WHERE corpus_id = ANY(:corpus_ids) "
            f"  AND to_tsvector('{_FTS_CONFIG}', text) @@ ({or_query}) "
            f"  {scope_sql} "
            " LIMIT :limit)"
        )
        exclude_sql = ""
        if exclude_ids:
            exclude_sql = "WHERE u.id <> ALL(:exclude_ids) "
            params["exclude_ids"] = exclude_ids
        rows = (
            conn.execute(
                sa.text(
                    "WITH matched AS (" + " UNION ALL ".join(legs) + "), ranked AS ("
                    f"  SELECT u.id, MAX(ts_rank_cd(COALESCE(u.tsv, to_tsvector('{_FTS_CONFIG}', u.text)), "
                    f"                              ({or_query}))) AS rank "
                    "  FROM matched u "
                    f"  {exclude_sql}"
                    "   GROUP BY u.id ORDER BY rank DESC LIMIT :limit"
                    ") "
                    f"SELECT {_SELECT_CC_NO_EMBED} FROM ranked "
                    "JOIN corpus_chunks cc ON cc.id = ranked.id "
                    "ORDER BY ranked.rank DESC"
                ),
                params,
            )
            .mappings()
            .all()
        )
        return [dict(r, embedding=None) for r in rows]

    def search_by_filename(
        self, corpus_ids: list[str], terms: list[str], *, limit: int, path_prefix: str | None = None
    ) -> list[dict[str, Any]]:
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

        Scoped on BOTH sides of the join (perf follow-up, 2026-09): the
        file row's ``corpus_id`` as well as the chunk's. Filtering
        ``corpus_files`` by collection first lets the planner narrow the
        ``ILIKE`` scan to the caller's collections through
        ``idx_corpus_files_corpus_path`` (leading column ``corpus_id``)
        instead of pattern-matching every filename on the instance — on a
        large instance that was a sequential scan over a few hundred
        thousand file rows per query. A ``%term%`` pattern has no index
        path of its own without the ``pg_trgm`` extension, which this
        schema does not use, so a caller whose grant spans (nearly) every
        collection still pays one pass over ``corpus_files`` — bounded by
        that table, never by ``corpus_chunks``. Semantically the extra
        predicate is fail-closed: a chunk row is a name hit only when its
        file's CURRENT collection is in scope too, so a file moved to a
        collection outside the caller's scope stops answering by name
        under the one it left even while stale chunk rows still carry the
        old ``corpus_id``. Mirrored in the DuckDB sibling so both backends
        agree.

        ``path_prefix`` narrows to files under one folder — see
        ``_path_prefix_clause``. It applies here as well as to the body pass
        because a scoped search that still let a name from OUTSIDE the scope
        answer would not be scoped at all.
        """
        if not corpus_ids or not terms:
            return []
        params: dict[str, Any] = {"corpus_ids": list(corpus_ids), "limit": limit}
        clauses = []
        for i, term in enumerate(terms):
            key = f"t{i}"
            clauses.append(f"cf.filename ILIKE :{key}")
            params[key] = f"%{term}%"
        scope_sql, scope_params = self._path_prefix_clause(path_prefix, alias="cc")
        params.update(scope_params)
        sql = (
            f"SELECT {_SELECT_CC_NO_EMBED} "
            "FROM corpus_files cf JOIN corpus_chunks cc ON cc.file_id = cf.id "
            "WHERE cf.corpus_id = ANY(:corpus_ids) AND cc.corpus_id = ANY(:corpus_ids) "
            "AND (" + " OR ".join(clauses) + ") "
            f"{scope_sql} "
            "ORDER BY cc.file_id, cc.ordinal LIMIT :limit"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(sa.text(sql), params).mappings().all()
        return [dict(r, embedding=None) for r in rows]
