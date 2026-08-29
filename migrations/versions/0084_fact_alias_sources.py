"""fact_alias_sources — per-corpus alias provenance (PG-only, A3 ratchet).

Security hardening: the fact-graph read path already projects a claim's
``attrs``/quotes only from claims the caller can read (spec §5, the S2
attribute-oracle rule), but ``fact_aliases.natural_key`` — used as the
subject's DISPLAY NAME everywhere (search, neighbors, the collection-detail
page, review items) — carried no such filter: a caller could see a name
minted from a document they cannot read, as long as the SUBJECT was
otherwise visible through some other, unrelated readable claim.

This table records, for each alias, every corpus whose evidence
contributed to establishing that EXACT ``(type, natural_key)`` string — not
merely "a corpus with some claim on the same fact" (that weaker signal is
exactly what let the bug through: a fact can carry claims from several
corpora while only one of them actually names it). A many-to-many join
table rather than a single denormalized column on ``fact_aliases`` because
a producer can independently re-derive the SAME literal alias string from
more than one corpus over separate ingest batches (spec §7.2's
deterministic node-id contract) — each such derivation grows this table,
never replaces a row.

No surrogate id — ``(type, natural_key, corpus_id)`` is the row's identity.
FK to ``fact_aliases(type, natural_key)`` ON DELETE CASCADE: an alias
repointed by ``merge_facts``/``split_fact`` only ever UPDATEs
``fact_aliases.fact_id`` (its ``(type, natural_key)`` key is stable across
both operations), so this table needs no matching UPDATE — it stays
correctly associated for free. A fully orphaned fact (``sweep_orphans``)
cascades away its aliases and, through this FK, their provenance too.

Revision ID: 0084_fact_alias_sources
Revises: 0083_ingest_runs_source_urls
Create Date: 2026-08-29
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0084_fact_alias_sources"
down_revision: Union[str, None] = "0083_ingest_runs_source_urls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fact_alias_sources",
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("natural_key", sa.String(), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["type", "natural_key"],
            ["fact_aliases.type", "fact_aliases.natural_key"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("type", "natural_key", "corpus_id"),
    )


def downgrade() -> None:
    op.drop_table("fact_alias_sources")
