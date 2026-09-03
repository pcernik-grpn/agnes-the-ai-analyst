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

A plain (transactional) ``CREATE INDEX IF NOT EXISTS``, on purpose: the
startup revision repair (``src/db_pg.py``) runs ``alembic upgrade head``
inside a connection whose transaction it owns, and ``CREATE INDEX
CONCURRENTLY`` — which needs Alembic's ``autocommit_block()`` to commit
that transaction first — fails there with ``PendingRollbackError``
(caught by ``tests/db_pg/test_startup_revision_check.py``). Migrations run
at process start, before any worker writes, so the short share lock a
plain build takes on ``corpus_chunks`` costs nothing; an operator who
already built the index by hand during the incident (the live fix was
``CREATE INDEX CONCURRENTLY``) is left alone by ``IF NOT EXISTS``.

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
    op.create_index(INDEX_NAME, "corpus_chunks", ["file_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="corpus_chunks", if_exists=True)
