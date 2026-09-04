"""Two hand-built hot-path indexes, shipped as code (TCRD-296 gaps #43, #44).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately NO matching ``_vN_to_v(N+1)`` step in ``src/db.py``;
the DuckDB app-state backend is frozen.

Live finding (2026-09-03, the whole-site SharePoint backfill): an operator
built both of these indexes BY HAND, out-of-band, because no migration ever
shipped them. Codifying them here is so the NEXT instance gets them without
an operator paged mid-crawl.

1. ``idx_corpus_chunks_corpus_id`` on ``corpus_chunks (corpus_id)``. Every
   per-collection chunk read (``list_for_corpus``, ``list_for_corpora``,
   ``count_for_corpora``) and the retrieval candidate query
   (``CorpusChunksPgRepository.search_candidates``,
   ``WHERE corpus_id = ANY(:corpus_ids)``) filters on this column, and the
   table carried no index over it — only the primary key and the ``file_id``/
   full-text indexes ``0098``/``0101`` added. On the affected instance —
   already north of 10M rows, see ``0101_corpus_chunks_fts_index`` and the
   CHANGELOG's "14.5M-chunk table" follow-up — that was a sequential scan
   per collection read, 100+ seconds.

   ``corpus_chunks`` is the SAME table ``0101`` already treats as too large
   to index unconditionally at startup: a plain (non-``CONCURRENTLY``)
   ``CREATE INDEX`` takes a SHARE lock for the whole build, and at this row
   count that build is no longer the "short lock, costs nothing" case
   ``0098`` was written for at 4.3M rows — it is minutes, on a table taking
   writes from a live crawl. So this index reuses ``0101``'s row-count gate
   verbatim: build in place at or under the threshold, otherwise skip and
   log the exact ``CREATE INDEX CONCURRENTLY`` statement for an operator to
   run out-of-band, off-peak.

2. ``idx_fact_aliases_natural_key`` on ``fact_aliases (natural_key)``. Ingest
   resolution (``FactsPgRepository``'s ``SELECT fact_id, type FROM
   fact_aliases WHERE natural_key = :nk``) looks up by ``natural_key`` alone,
   but the only index covering that column was the composite primary key
   ``(type, natural_key)`` — ``natural_key`` is not its leading column, so it
   cannot serve a lookup that omits ``type``. Sequential scan over 550k rows
   per lookup on the affected instance. ``fact_aliases`` is two orders of
   magnitude smaller than ``corpus_chunks``; a plain B-tree build here is the
   same "short lock, costs nothing" case ``0098`` already relied on, so this
   one is unconditional, same as ``0098``.

Both use ``IF NOT EXISTS`` so an instance where the operator already built
the index by hand during the incident is left alone.

Revision ID: 0106_hot_path_indexes
Revises: 0105_fact_collection_stats
Create Date: 2026-09-04
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0106_hot_path_indexes"
down_revision: Union[str, None] = "0105_fact_collection_stats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)

CORPUS_CHUNKS_INDEX_NAME = "idx_corpus_chunks_corpus_id"
FACT_ALIASES_INDEX_NAME = "idx_fact_aliases_natural_key"

#: Same threshold ``0101_corpus_chunks_fts_index`` uses for the same table —
#: comfortably above anything a fresh or lightly-loaded instance has,
#: comfortably below the incident's 10M+ rows.
_LARGE_TABLE_ROW_THRESHOLD = 1_000_000

_CONCURRENT_BUILD_SQL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {CORPUS_CHUNKS_INDEX_NAME} ON corpus_chunks (corpus_id);"
)


def upgrade() -> None:
    bind = op.get_bind()
    row_count = bind.execute(sa.text("SELECT COUNT(*) FROM corpus_chunks")).scalar() or 0
    if row_count <= _LARGE_TABLE_ROW_THRESHOLD:
        op.create_index(CORPUS_CHUNKS_INDEX_NAME, "corpus_chunks", ["corpus_id"], if_not_exists=True)
    else:
        logger.warning(
            "corpus_chunks has %d rows (over the %d in-place-build threshold) — "
            "skipping the corpus_id index build during startup migration to avoid "
            "holding a share lock on a table under active write load. Per-collection "
            "chunk reads and the retrieval candidate query still work without it, "
            "just via a slower sequential scan. Run this out-of-band, once, off-peak: %s",
            row_count,
            _LARGE_TABLE_ROW_THRESHOLD,
            _CONCURRENT_BUILD_SQL,
        )

    # fact_aliases is two orders of magnitude smaller than corpus_chunks — a
    # plain B-tree build here is cheap even unconditionally, same as 0098.
    op.create_index(FACT_ALIASES_INDEX_NAME, "fact_aliases", ["natural_key"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index(FACT_ALIASES_INDEX_NAME, table_name="fact_aliases", if_exists=True)
    op.drop_index(CORPUS_CHUNKS_INDEX_NAME, table_name="corpus_chunks", if_exists=True)
