"""facts, fact_aliases, edges, claims, corrections — fact graph over
Collections (PG-only, A3 ratchet).

Schema per docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md
§3 — build order step 2. Postgres app-state, per the design's §2
consequences: per-caller filtering over a "readable claim set" is impossible
on distributed parquet, so this is app-state, not analytics.

PG-first ratchet (A3): brand-new app-state surface added after the freeze —
Alembic-only, no matching DuckDB `_vN_to_v(N+1)` step, `SCHEMA_VERSION` does
not move. `src/repositories/facts_pg.py` is the only repository; there is no
DuckDB sibling.

A claim is the unit (§2): a claim references EXACTLY ONE of a fact or an
edge (the CHECK constraint below), always carries the corpus_file_id and
denormalized corpus_id it was extracted from (the visibility predicate, an
indexed column rather than a join), the sha256 of the file content it was
extracted against (staleness check, not a resurrection mechanism — see the
design's "load-bearing details"), and a verbatim quote (§8). The functional
unique index prevents a duplicate (subject, document, quote) triple from
accumulating on re-ingest (§7.2's union-mode idempotency).

Revision ID: 0076_facts_tables
Revises: 0075_corpus_file_sources
Create Date: 2026-08-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0076_facts_tables"
down_revision: Union[str, None] = "0075_corpus_file_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "facts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_facts_type", "facts", ["type"])

    op.create_table(
        "fact_aliases",
        sa.Column("fact_id", sa.String(), nullable=False),
        # Denormalized from facts.type (like corpus_id on claims) so the
        # UNIQUE(type, natural_key) constraint below never needs a join.
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("natural_key", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["facts.id"], ondelete="CASCADE"),
        # No surrogate id column in the spec — (type, natural_key) IS the
        # row's identity (a producer node id `<type>:<slug>` resolves
        # directly through it), so it is the primary key.
        sa.PrimaryKeyConstraint("type", "natural_key"),
    )
    op.create_index("idx_fact_aliases_fact_id", "fact_aliases", ["fact_id"])

    op.create_table(
        "edges",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("src", sa.String(), nullable=False),
        sa.Column("dst", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["src"], ["facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dst"], ["facts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("src", "type", "dst", name="uq_edges_src_type_dst"),
    )
    op.create_index("idx_edges_src", "edges", ["src"])
    op.create_index("idx_edges_dst", "edges", ["dst"])

    op.create_table(
        "claims",
        sa.Column("id", sa.String(), nullable=False),
        # Exactly one of fact_id/edge_id is set — the CHECK below.
        sa.Column("fact_id", sa.String(), nullable=True),
        sa.Column("edge_id", sa.String(), nullable=True),
        sa.Column("corpus_file_id", sa.String(), nullable=False),
        # Denormalized: THE visibility predicate column (§3) — an indexed
        # equality filter, never a join through corpus_files on a
        # traversal hop.
        sa.Column("corpus_id", sa.String(), nullable=False),
        # Staleness check (not a resurrection mechanism, see module docstring).
        sa.Column("file_sha256", sa.String(), nullable=False),
        sa.Column("attrs", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("quote_hash", sa.String(), nullable=False),
        sa.Column("document_date", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["edge_id"], ["edges.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["corpus_file_id"], ["corpus_files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "(fact_id IS NULL) <> (edge_id IS NULL)",
            name="ck_claims_fact_xor_edge",
        ),
    )
    op.create_index("idx_claims_corpus_id", "claims", ["corpus_id"])
    op.create_index("idx_claims_fact_id", "claims", ["fact_id"])
    op.create_index("idx_claims_edge_id", "claims", ["edge_id"])
    op.create_index("idx_claims_corpus_file_id", "claims", ["corpus_file_id"])
    # Functional unique index — Postgres cannot express COALESCE in a plain
    # UNIQUE table constraint (spec §3). Backs §7.2's union-mode ingest
    # idempotency: re-asserting the same (subject, document, quote) triple
    # is a no-op rather than a duplicate row.
    op.create_index(
        "uq_claims_subject_file_quote",
        "claims",
        [sa.text("COALESCE(fact_id, edge_id)"), sa.text("corpus_file_id"), sa.text("quote_hash")],
        unique=True,
    )

    op.create_table(
        "corrections",
        sa.Column("subject_kind", sa.String(), nullable=False),
        sa.Column("subject_id", sa.String(), nullable=False),
        # Snapshot of the subject's natural keys so a subject deleted (all
        # claims cascaded away) and later re-created under a new surrogate
        # id re-attaches its correction at write time (§3) — this table
        # deliberately carries NO foreign key to facts/edges and never
        # cascades with its subject.
        sa.Column("natural_keys", JSONB(), nullable=False),
        sa.Column("verdict", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("decided_by", sa.String(), nullable=False),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("subject_kind", "subject_id"),
    )
    op.create_index(
        "idx_corrections_natural_keys_gin",
        "corrections",
        ["natural_keys"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_corrections_natural_keys_gin", "corrections")
    op.drop_table("corrections")

    op.drop_index("uq_claims_subject_file_quote", "claims")
    op.drop_index("idx_claims_corpus_file_id", "claims")
    op.drop_index("idx_claims_edge_id", "claims")
    op.drop_index("idx_claims_fact_id", "claims")
    op.drop_index("idx_claims_corpus_id", "claims")
    op.drop_table("claims")

    op.drop_index("idx_edges_dst", "edges")
    op.drop_index("idx_edges_src", "edges")
    op.drop_table("edges")

    op.drop_index("idx_fact_aliases_fact_id", "fact_aliases")
    op.drop_table("fact_aliases")

    op.drop_index("idx_facts_type", "facts")
    op.drop_table("facts")
