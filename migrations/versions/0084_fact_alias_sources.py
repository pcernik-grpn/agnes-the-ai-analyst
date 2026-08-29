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

**Backfill (adversarial review finding, 2026-08-29).** The table is created
EMPTY by ``op.create_table`` alone — on any instance with pre-existing fact
data, every alias minted before this deploy would then have ZERO provenance
rows, and the read-path filter (``src/repositories/facts_pg.py``'s
``_alias_readable_sql``) treats zero rows as "unreadable to every
non-admin" exactly like a genuinely-restricted alias. Left unfixed this
would silently blank every subject's display name for every non-admin
caller across search/neighbors/collection-summary/review-labels, AND make
the free-text ``q`` lookup (``candidates`` CTE requires a readable alias
match whenever ``q`` is supplied) return ZERO results for every
pre-existing subject — a full functional regression an operator could only
fix by re-ingesting everything.

So ``upgrade()`` backfills: for every existing ``fact_aliases`` row, one
``fact_alias_sources`` row per DISTINCT ``corpus_id`` among ``claims`` on
that alias's ``fact_id``. This is DELIBERATELY BROADER than the live
per-node rule ``add_alias_source`` enforces going forward (which ties a
specific alias to only the corpus of the evidence that minted THAT alias,
not every corpus the fact happens to carry a claim from) — the exact
distinction §5's alias-visibility rule exists to draw. But history doesn't
record which claim minted which alias before this migration; a NARROWER
backfill (e.g., "nothing") would blank legitimate, real names rather than
merely being imprecise. The trade-off is bounded and one-time: only facts
that already had BOTH an alias AND a same-fact claim from a DIFFERENT,
unreadable collection at the moment of this upgrade can show a
name to a caller who could not have independently derived it from THAT
alias's true minting evidence — a narrower echo of the original bug,
confined to data that predates this fix. Every alias minted through
``ingest_batch`` AFTER this migration gets the precise, narrow guarantee
via ``add_alias_source`` (see ``_write_evidence`` in
``src/repositories/facts_pg.py``). An operator who wants the precise
guarantee retroactively too must re-ingest the affected corpora; nothing
here should be read as a promise that a legacy alias's provenance is
minting-accurate.

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
    # Backfill from pre-existing data (see the module docstring's
    # "Backfill" section above) — a no-op INSERT ... SELECT on a fresh
    # instance with no facts yet.
    op.execute(
        sa.text(
            "INSERT INTO fact_alias_sources (type, natural_key, corpus_id) "
            "SELECT DISTINCT fa.type, fa.natural_key, c.corpus_id "
            "FROM fact_aliases fa JOIN claims c ON c.fact_id = fa.fact_id "
            "ON CONFLICT DO NOTHING"
        )
    )


def downgrade() -> None:
    op.drop_table("fact_alias_sources")
