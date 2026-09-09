"""corpus_chunks stored tsvector — the RANKING half of the bounded retrieval
candidate scan (perf follow-up to the P0 OOM fix, 2026-09).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately NO matching ``_vN_to_v(N+1)`` step in ``src/db.py``;
the DuckDB app-state backend is frozen.

Live finding: with ``0101``'s GIN index in place, the WHERE half of
``CorpusChunksPgRepository.search_candidates`` is cheap — the index bounds
the match set. The ORDER BY half is not: ``ts_rank_cd(to_tsvector('simple',
text), …)`` re-tokenizes every matched row's body text at rank time, and on
a 15M-row instance a two-term query with ~4 500 matches spent 2 s doing
exactly that, all from shared buffers (a three-term query with a handful of
matches took 40 ms — the cost is per matched row, not per query).

This migration adds ``corpus_chunks.tsv tsvector`` — the tokenized body,
stored once — so ranking reads a column instead of re-parsing text. Three
things make it safe on a table that may already be huge:

1. **The column is a plain nullable column, not a ``GENERATED … STORED``
   one.** ``ALTER TABLE … ADD COLUMN … GENERATED ALWAYS AS (…) STORED``
   rewrites the whole table (and every index on it) under an ``ACCESS
   EXCLUSIVE`` lock, with no ``CONCURRENTLY`` form — on a 20 GB table that
   is hours of downtime for every crawler and every search, the exact shape
   ``0101``'s row-count gate exists to avoid. A nullable column add is a
   metadata-only change: instant, no rewrite, safe at any size.
2. **New rows are covered from the moment the migration lands** —
   ``CorpusChunksPgRepository.add_many`` writes ``tsv`` on every insert.
3. **Every reader falls back per row**: ranking uses ``COALESCE(tsv,
   to_tsvector('simple', text))``, so a row not yet backfilled ranks
   exactly as it did before this migration (slower, never wrong), and the
   WHERE clause keeps matching on the ``to_tsvector('simple', text)``
   expression — which is what the ``0101`` GIN index is over, and what a
   NULL-``tsv`` row still satisfies. The index does NOT move onto the
   column for the same reason: ``tsv @@ query`` would silently drop every
   row not yet backfilled.

The backfill of pre-existing rows is the expensive part, and it follows
``0101``'s pattern: count the rows first. At or under
``_IN_PLACE_BACKFILL_ROW_THRESHOLD`` run one ``UPDATE … WHERE tsv IS NULL``
in place, inside the startup migration like any other. Above it, SKIP the
backfill and log a WARNING naming the operator follow-up — run once,
off-peak, from inside the app container (where the database URL is set)::

    python scripts/backfill_corpus_chunks_tsv.py

The threshold sits well UNDER ``0101``'s 1,000,000 because an ``UPDATE``
that rewrites every row is heavier than an index build over the same rows:
each row gets a new heap version (the table roughly doubles until
autovacuum reclaims the old ones), and because the new tuple rarely fits on
its old page the update is not HOT, so every index on the table — the GIN
one included — is maintained for every row. At 200,000 rows that is on the
order of a minute; comfortably above what a fresh or lightly-loaded instance
has (a dozens-of-files deployment is thousands of chunks), comfortably below
the scale that made this migration necessary. The script batches by primary
key with a commit per batch, so it never holds a long lock or a long
transaction, and it is idempotent (only ``tsv IS NULL`` rows are touched) —
safe to interrupt and re-run. Search keeps working throughout.

Revision ID: 0114_corpus_chunks_tsv
Revises: 0113_data_apps_data_identity
Create Date: 2026-09-08
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0114_corpus_chunks_tsv"
down_revision: str | None = "0113_data_apps_data_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

COLUMN_NAME = "tsv"
_IN_PLACE_BACKFILL_ROW_THRESHOLD = 200_000

_BACKFILL_COMMAND = "python scripts/backfill_corpus_chunks_tsv.py"


def upgrade() -> None:
    # Metadata-only: instant on any table size, no rewrite (see docstring).
    op.execute(sa.text(f"ALTER TABLE corpus_chunks ADD COLUMN IF NOT EXISTS {COLUMN_NAME} tsvector"))

    bind = op.get_bind()
    row_count = bind.execute(sa.text("SELECT COUNT(*) FROM corpus_chunks")).scalar() or 0
    if row_count <= _IN_PLACE_BACKFILL_ROW_THRESHOLD:
        op.execute(
            sa.text(
                f"UPDATE corpus_chunks SET {COLUMN_NAME} = to_tsvector('simple', text) "
                f"WHERE {COLUMN_NAME} IS NULL AND text IS NOT NULL"
            )
        )
        return
    logger.warning(
        "corpus_chunks has %d rows (over the %d in-place-backfill threshold) — "
        "added the stored tsvector column but skipped populating existing rows "
        "during startup migration to avoid rewriting a large table under active "
        "write load. Search keeps working: a row without a stored vector ranks "
        "via a per-row fallback (its text is tokenized at rank time, exactly the "
        "pre-migration cost); new rows are stored on insert. Run this out-of-band, "
        "once, off-peak (batched, idempotent, safe to interrupt and re-run): %s",
        row_count,
        _IN_PLACE_BACKFILL_ROW_THRESHOLD,
        _BACKFILL_COMMAND,
    )


def downgrade() -> None:
    op.execute(sa.text(f"ALTER TABLE corpus_chunks DROP COLUMN IF EXISTS {COLUMN_NAME}"))
