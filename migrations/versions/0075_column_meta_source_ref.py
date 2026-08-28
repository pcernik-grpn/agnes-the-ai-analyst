"""column_metadata.source_ref

Nullable per-connection provenance for a dataset field written by the
semantic-layer projector (``src.semantic.projection.project_document``) —
the same addition ``0054_semantic_source_ref_v107`` made to
``metric_definitions`` / ``glossary_terms``. Postgres-only: the A3 PG-first
ratchet (``docs/migrations.md`` -> "Adding a PG-only feature") freezes the
DuckDB app-state schema, so this column has no DuckDB counterpart —
``src/repositories/column_metadata.py``'s ``save()`` accepts ``source_ref``
for signature parity with the Postgres repo but does not persist it.

Revision ID: 0075_column_meta_source_ref
Revises: 0078_facts_ingest_runs
Create Date: 2026-08-26

Re-chained onto ``0078_facts_ingest_runs`` — the tip of ``main`` at the time
this branch (``mf/semantic-layer-v0``) merged it in. Originally written
against the shared branchpoint ``0074_llm_usage_caller_user_id``, which was
``main``'s tip when this migration (and the rest of the semantic
coverage/health/mute/feedback chain built on top of it, ending at
``0077_semantic_models_detach``) was authored; ``main`` has since grown its
own independent chain off that same branchpoint
(``0075_corpus_file_sources`` -> ``0076_facts_tables`` ->
``0077_ontology_drafts`` -> ``0078_facts_ingest_runs``), so leaving this
pointed at the old branchpoint would fork the chain into two Alembic heads
and make ``upgrade head`` refuse. Revision ID left unchanged (an instance
that already applied it under this id keeps working), only ``down_revision``
moves — same pattern as ``0073_resource_source_tags``'s own re-chain.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Kept to 32 chars — alembic_version.version_num is VARCHAR(32); a longer id
# truncates and breaks every later revision's WHERE clause (verified live:
# StringDataRightTruncation on this exact migration during development).
revision: str = "0075_column_meta_source_ref"
down_revision: Union[str, None] = "0078_facts_ingest_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("column_metadata", sa.Column("source_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("column_metadata", "source_ref")
