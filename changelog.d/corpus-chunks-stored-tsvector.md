### Changed
- **Knowledge / collections search ranks faster on a large Postgres corpus.**
  The bounded candidate query (`CorpusChunksPgRepository.search_candidates`)
  re-tokenized every matched chunk's text inside its `ORDER BY ts_rank_cd(...)`
  — measured at 2 s for a two-term query with ~4 500 matches on a 15M-row
  table, all from cache. Migration `0114_corpus_chunks_tsv` adds a stored
  `corpus_chunks.tsv` tsvector, written on every insert; ranking reads it
  through a per-row `COALESCE(tsv, to_tsvector('simple', text))` fallback, so a
  row the backfill has not reached ranks exactly as before. The query now also
  sorts narrow `(id, rank)` rows and fetches columns for the top `limit` only —
  sorting full text-bearing rows spilled to disk at the default 4 MB
  `work_mem` on every over-limit query. On a table over
  200,000 rows the migration adds the column only and logs the operator
  follow-up — `python scripts/backfill_corpus_chunks_tsv.py`, batched,
  idempotent, run off-peak (`docs/migrations.md` → *Adding a column that needs
  a backfill on an already-huge table*). Results, the candidate cap and the
  `truncated` / `candidates_capped` disclosure are unchanged.
- **The filename-fallback candidate query scopes `corpus_files` by collection
  first.** `search_by_filename` filters the file table by the caller's
  collections (the existing `(corpus_id, path)` index) before its `ILIKE`
  pattern match instead of pattern-matching every filename on the instance; a
  grant spanning nearly every collection still pays one pass over
  `corpus_files`, never over `corpus_chunks`. Fail-closed: a file moved to a
  collection outside the caller's scope no longer answers by name under the
  collection it left. Mirrored on the DuckDB backend.
