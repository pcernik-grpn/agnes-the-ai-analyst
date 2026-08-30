"""SQLAlchemy models for the fact graph over Collections (PG-only, A3
ratchet — no DuckDB sibling).

Mirrors migrations/versions/0076_facts_tables.py. See
docs/superpowers/specs/2026-08-27-fact-graph-over-collections-design.md §3
for the schema rationale and §5 for why every read goes through
``src/repositories/facts_pg.py``'s single shared visibility helper rather
than through these models directly.

``IngestRun`` (mirrors ``migrations/versions/0077_facts_ingest_runs.py``) is
a separate concern — one row per ``POST /api/facts/ingest`` batch, the
persisted run report the source card (spec §13.2) reads its pipeline counts
and error badges from. Written by ``src/repositories/facts_ingest_runs_pg.py``,
a distinct PG-only repository, AFTER ``facts_repo().ingest_batch()``
commits — see ``app/api/facts.py::facts_ingest`` for why that write is
deliberately outside the ingest transaction.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (sa.Index("idx_facts_type", "type"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    type: Mapped[str] = mapped_column(String, nullable=False)


class FactAlias(Base):
    """Producer node id `<type>:<slug>` resolution table.

    No surrogate id column (spec §3) — `(type, natural_key)` IS the row's
    identity, hence the composite primary key. `type` is denormalized from
    `facts.type` (like `corpus_id` on claims) so the alias lookup never
    joins.
    """

    __tablename__ = "fact_aliases"
    __table_args__ = (sa.Index("idx_fact_aliases_fact_id", "fact_id"),)

    type: Mapped[str] = mapped_column(String, primary_key=True)
    natural_key: Mapped[str] = mapped_column(String, primary_key=True)
    fact_id: Mapped[str] = mapped_column(String, ForeignKey("facts.id", ondelete="CASCADE"), nullable=False)


class FactAliasSource(Base):
    """Per-corpus provenance for a ``fact_aliases`` row (security hardening
    — see ``migrations/versions/0084_fact_alias_sources.py``): the set of
    corpora whose evidence actually contributed to minting this EXACT
    ``(type, natural_key)`` string, distinct from "any corpus with a claim
    on the same fact". ``src/repositories/facts_pg.py``'s alias-visibility
    filter joins through this table instead of ever showing
    ``fact_aliases.natural_key`` unconditionally.
    """

    __tablename__ = "fact_alias_sources"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["type", "natural_key"], ["fact_aliases.type", "fact_aliases.natural_key"], ondelete="CASCADE"
        ),
    )

    type: Mapped[str] = mapped_column(String, primary_key=True)
    natural_key: Mapped[str] = mapped_column(String, primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String, primary_key=True)


class Edge(Base):
    __tablename__ = "edges"
    __table_args__ = (
        sa.UniqueConstraint("src", "type", "dst", name="uq_edges_src_type_dst"),
        sa.Index("idx_edges_src", "src"),
        sa.Index("idx_edges_dst", "dst"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    src: Mapped[str] = mapped_column(String, ForeignKey("facts.id", ondelete="CASCADE"), nullable=False)
    dst: Mapped[str] = mapped_column(String, ForeignKey("facts.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)


class Claim(Base):
    """One document saying one thing about one subject (§2 — the unit).

    Exactly one of ``fact_id``/``edge_id`` is set (``ck_claims_fact_xor_edge``).
    ``corpus_id`` is denormalized from the claim's ``corpus_file_id`` so the
    visibility predicate (§5) is a single indexed equality filter, never a
    join, at every traversal hop.
    """

    __tablename__ = "claims"
    __table_args__ = (
        sa.CheckConstraint("(fact_id IS NULL) <> (edge_id IS NULL)", name="ck_claims_fact_xor_edge"),
        sa.Index("idx_claims_corpus_id", "corpus_id"),
        sa.Index("idx_claims_fact_id", "fact_id"),
        sa.Index("idx_claims_edge_id", "edge_id"),
        sa.Index("idx_claims_corpus_file_id", "corpus_file_id"),
        sa.Index("idx_claims_corpus_audience", "corpus_id", "audience"),
        # Functional unique index — Postgres cannot express COALESCE in a
        # plain UNIQUE table constraint (spec §3); backs §7.2's union-mode
        # ingest idempotency.
        sa.Index(
            "uq_claims_subject_file_quote",
            sa.text("COALESCE(fact_id, edge_id)"),
            sa.text("corpus_file_id"),
            sa.text("quote_hash"),
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    fact_id: Mapped[str | None] = mapped_column(String, ForeignKey("facts.id", ondelete="CASCADE"), nullable=True)
    edge_id: Mapped[str | None] = mapped_column(String, ForeignKey("edges.id", ondelete="CASCADE"), nullable=True)
    corpus_file_id: Mapped[str] = mapped_column(
        String, ForeignKey("corpus_files.id", ondelete="CASCADE"), nullable=False
    )
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String, nullable=False)
    attrs: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    quote_hash: Mapped[str] = mapped_column(String, nullable=False)
    document_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # NULL = untagged (unrestricted within its collection; under
    # acl_sync.guarantee_mode=must_not, admin-only in a tiered corpus) —
    # see facts_pg's visibility contract and migration 0086.
    audience: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
    )


class Correction(Base):
    """Admin verdict on a subject (`wrong` / `restricted` / `revealed`),
    enforced at read time in every repository read method (§4), never
    cascaded with its subject — see ``natural_keys`` docstring below.
    """

    __tablename__ = "corrections"
    __table_args__ = (sa.Index("idx_corrections_natural_keys_gin", "natural_keys", postgresql_using="gin"),)

    subject_kind: Mapped[str] = mapped_column(String, primary_key=True)  # 'fact' | 'edge'
    subject_id: Mapped[str] = mapped_column(String, primary_key=True)
    # Snapshot of the subject's natural keys (a fact's alias keys; an edge's
    # [src_key, type, dst_key]) so a subject deleted and later re-created
    # under a new surrogate id re-attaches this correction at write time.
    natural_keys: Mapped[dict] = mapped_column(JSONB, nullable=False)
    verdict: Mapped[str] = mapped_column(String, nullable=False)  # 'wrong' | 'restricted' | 'revealed'
    reason: Mapped[str] = mapped_column(String, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
    )


class IngestRun(Base):
    """One persisted run report per ``POST /api/facts/ingest`` batch (spec
    §7.2's response shape, §13.2's source-card pipeline strip + error
    badges).

    ``claims_rejected_count`` is a plain integer alongside the
    ``claims_rejected`` JSONB detail list so the card's pipeline-strip cost
    placeholder and badge counts never have to decode JSONB just to sum —
    every OTHER list field (``deferred``, ``review_items``) has no separate
    count column because nothing on the card needs to aggregate across many
    runs' worth of them, only itemize the single latest one.
    """

    __tablename__ = "facts_ingest_runs"
    __table_args__ = (sa.Index("idx_facts_ingest_runs_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=sa.text("now()"), nullable=True
    )
    corpus_ids: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False)
    caller: Mapped[str] = mapped_column(String, nullable=False)
    documents_seen: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    claims_written: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    claims_rejected_count: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    claims_rejected: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False)
    #: Same shape as `claims_rejected_count`/`claims_rejected`, one column
    #: pair over — a document's `source_url` Agnes dropped as invalid
    #: (O7, `_validate_source_url`). The claim itself still writes; only the
    #: citation link is missing, and this is the operator-visible record of
    #: why (see migrations/versions/0083_ingest_runs_source_urls.py).
    source_urls_rejected_count: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    source_urls_rejected: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False)
    deferred: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False)
    subjects_created: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    subjects_deleted: Mapped[int] = mapped_column(sa.Integer, server_default="0", nullable=False)
    review_items: Mapped[list] = mapped_column(JSONB, server_default=sa.text("'[]'::jsonb"), nullable=False)
    #: The producer's OPTIONAL anonymization declaration for this batch
    #: (spec §9.2): ``{declared: bool, scopes: {corpus_id: {docs_anonymized,
    #: docs_skipped}}}``. Empty ``{}`` (never null) when the producer never
    #: anonymizes — see ``migrations/versions/0081_ingest_runs_anonymize.py``.
    anonymization: Mapped[dict] = mapped_column(JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False)
