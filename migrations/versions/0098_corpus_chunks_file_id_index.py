"""corpus_chunks(file_id) index — the per-file lookup every ingest performs.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
There is deliberately NO matching ``_vN_to_v(N+1)`` step in ``src/db.py``;
the DuckDB app-state backend is frozen.

Live finding (2026-09, a whole-site SharePoint backfill on a 64-vCPU
instance): ``corpus_chunks`` had only its primary key. Every ingested file
first runs ``DELETE FROM corpus_chunks WHERE file_id = :file_id`` (the
re-ingest safety delete in ``CorpusChunksPgRepository``) and later reads
``... WHERE file_id = :file_id ORDER BY ordinal`` — both sequential scans
over what was by then 4.3 million rows / 5 GB, 2–3 s each, with three
PostgreSQL backends pinned at ~90 % CPU. The database, not the crawler,
bounded the whole fleet at ~240 documents/min.

Created ``CONCURRENTLY`` so an instance mid-crawl is never blocked: Alembic
runs each revision inside a transaction by default, and ``CREATE INDEX
CONCURRENTLY`` refuses to run inside one, so this revision commits the
migration transaction first (``autocommit_block``). ``IF NOT EXISTS`` makes
it safe on an instance where an operator already built the index by hand
during the incident.

Revision ID: 0098_corpus_chunks_file_id_index
Revises: 0097_facts_llm_cache
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0098_corpus_chunks_file_id_index"
down_revision: Union[str, None] = "0097_facts_llm_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "idx_corpus_chunks_file_id"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "corpus_chunks",
            ["file_id"],
            if_not_exists=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="corpus_chunks",
            if_exists=True,
            postgresql_concurrently=True,
        )
