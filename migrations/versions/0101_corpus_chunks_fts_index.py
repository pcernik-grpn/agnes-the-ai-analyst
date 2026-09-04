"""corpus_chunks GIN full-text index — the body-text half of the bounded
retrieval candidate scan (P0 OOM fix, 2026-09).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately NO matching ``_vN_to_v(N+1)`` step in ``src/db.py``;
the DuckDB app-state backend is frozen.

Live incident: an admin caller's ``GET /api/knowledge/search`` (also
``/api/collections/search`` and the MCP tools over the same function)
resolved every collection it may access — ~390 collections on the affected
instance — and ``src.ingest.retrieval.search`` loaded EVERY chunk row
across all of them before scoring a single one: a ~10M-row / 12GB table, a
100+ second sequential scan, and ~10M materialized Python dicts. uvicorn
reached 16.7GB RSS and was OOM-killed, 13 times in 75 minutes while users
retried. The fix (this train) bounds candidate SELECTION itself in SQL —
``CorpusChunksPgRepository.search_candidates`` — via Postgres full-text
search: ``to_tsvector('simple', text) @@ plainto_tsquery('simple', :query)``,
ranked by ``ts_rank_cd``, ``LIMIT``. This migration is the index that query
needs to stay fast at scale; the query already WORKS without it (a sequential
scan, just a slower one, still bounded by the same LIMIT).

Unlike ``0098_corpus_chunks_file_id_index`` (a plain B-tree, cheap even
unconditionally), a GIN index over ``to_tsvector('simple', text)`` on a
10M-row table takes several MINUTES to build and holds a SHARE lock for the
whole build — inside the startup migration (``alembic upgrade head`` run in
a transaction the startup repair owns, see that migration's docstring for
why ``CREATE INDEX CONCURRENTLY`` cannot run there either) that would stall
every crawler insert for the duration, on exactly the instance-under-load
this whole fix is about. So: count ``corpus_chunks`` rows first. At or under
``_LARGE_TABLE_ROW_THRESHOLD`` (1,000,000 — comfortably above anything a
fresh or lightly-loaded instance has, comfortably below the incident's 10M),
build the index in place, same as any other startup migration. Above it,
SKIP the build and log a WARNING naming the exact statement an operator
must run out-of-band, once, off-peak:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_corpus_chunks_text_fts
    ON corpus_chunks USING gin (to_tsvector('simple', text));

Revision ID: 0101_corpus_chunks_fts_index
Revises: 0100_facts_created_at
Create Date: 2026-09-03
"""

from __future__ import annotations

import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0101_corpus_chunks_fts_index"
down_revision: Union[str, None] = "0100_facts_created_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger(__name__)

INDEX_NAME = "idx_corpus_chunks_text_fts"
_LARGE_TABLE_ROW_THRESHOLD = 1_000_000

_CONCURRENT_BUILD_SQL = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON corpus_chunks USING gin (to_tsvector('simple', text));"
)


def upgrade() -> None:
    bind = op.get_bind()
    row_count = bind.execute(sa.text("SELECT COUNT(*) FROM corpus_chunks")).scalar() or 0
    if row_count <= _LARGE_TABLE_ROW_THRESHOLD:
        op.execute(
            sa.text(f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} ON corpus_chunks USING gin (to_tsvector('simple', text))")
        )
        return
    logger.warning(
        "corpus_chunks has %d rows (over the %d in-place-build threshold) — "
        "skipping the full-text GIN index build during startup migration to "
        "avoid holding a long lock on a table under active write load. "
        "Bounded full-text search (src.ingest.retrieval.search) still works "
        "without it, just via a slower sequential scan. Run this out-of-band, "
        "once, off-peak: %s",
        row_count,
        _LARGE_TABLE_ROW_THRESHOLD,
        _CONCURRENT_BUILD_SQL,
    )


def downgrade() -> None:
    op.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
