"""corpus_file_sources — crawler-anchor mapping (PG-only, A3 ratchet).

Prerequisite change to Collections for the fact-graph-over-Collections
design (docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
§6): maps a producer's ``(corpus_id, source_stable_id)`` delta key to the
``corpus_files`` row it currently resolves to, so a re-sync or a manual path
re-upload of the same document preserves that row's id instead of cascading
its (future) claims away.

PG-first ratchet (A3): brand-new app-state table, Alembic-only — there is no
matching DuckDB ``_vN_to_v(N+1)`` step and ``SCHEMA_VERSION`` does not move;
``src/repositories/corpus_files.py`` (DuckDB) gains no capability that
depends on this table.

Revision ID: 0075_corpus_file_sources
Revises: 0074_llm_usage_caller_user_id
Create Date: 2026-08-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0075_corpus_file_sources"
down_revision: Union[str, None] = "0074_llm_usage_caller_user_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "corpus_file_sources",
        sa.Column("corpus_file_id", sa.String(), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("source_stable_id", sa.String(), nullable=False),
        sa.Column("source_doc_id", sa.String(), nullable=True),
        sa.Column("source_sha256", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["corpus_file_id"], ["corpus_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("corpus_file_id"),
        sa.UniqueConstraint("corpus_id", "source_stable_id", name="uq_corpus_file_sources_corpus_stable_id"),
    )
    op.create_index(
        "idx_corpus_file_sources_corpus_doc",
        "corpus_file_sources",
        ["corpus_id", "source_doc_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_corpus_file_sources_corpus_doc", "corpus_file_sources")
    op.drop_table("corpus_file_sources")
