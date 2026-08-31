"""column_metadata.source_ref

Nullable per-connection provenance for a dataset field written by the
semantic-layer projector (``src.semantic.projection.project_document``) —
the same addition ``0054_semantic_source_ref_v107`` made to
``metric_definitions`` / ``glossary_terms``. Postgres-only: the A3 PG-first
ratchet (``docs/migrations.md`` -> "Adding a PG-only feature") freezes the
DuckDB app-state schema, so this column has no DuckDB counterpart —
``src/repositories/column_metadata.py``'s ``save()`` accepts ``source_ref``
for signature parity with the Postgres repo but does not persist it.

Revision ID: 0085_column_meta_source_ref
Revises: 0084_fact_alias_sources
Create Date: 2026-08-26

This file is the ROOT of the semantic-layer chain: everything the
``mf/semantic-layer-v0`` branch adds hangs off it, in the order
``0085`` -> ``0086`` -> ``0087`` -> ``0088`` -> ``0089`` -> ``0090``.
It has been re-parented three times, always for the same reason — the
branch it sits on merged its base, and the base had grown its own chain
off the parent this file pointed at, forking Alembic into two heads
(``upgrade head`` then refuses to pick between them).

  1. Written against ``0074_llm_usage_caller_user_id`` (``main``'s tip at
     authoring time), under the id ``0073_column_metadata_source_ref_v125``.
  2. F3 merge: renumbered to ``0075_column_meta_source_ref`` and re-parented
     onto ``0074_llm_usage_caller_user_id``.
  3. Base merge: re-parented onto ``0078_facts_ingest_runs``, the tip
     ``main`` had reached via its own independent chain off that same
     branchpoint (``0075_corpus_file_sources`` -> ``0076_facts_tables`` ->
     ``0077_ontology_drafts`` -> ``0078_facts_ingest_runs``).
  4. ``integration`` merge (landing-plan step 0): renumbered to
     ``0085_column_meta_source_ref`` and re-parented onto
     ``0084_fact_alias_sources``. ``integration`` had added six revisions
     (``0079_sso_login`` .. ``0084_fact_alias_sources``) off
     ``0078_facts_ingest_runs`` — the exact parent this file claimed — so
     the merge produced two heads. The whole semantic chain was renumbered
     to ``0085``..``0090`` and appended AFTER ``integration``'s, which puts
     every filename's numeric prefix back in real chain order (it had drifted
     out of order across the earlier re-parents) and keeps the two branches'
     revisions in the sequence they were actually authored in.

The revisions in this chain are mutually independent of ``integration``'s
(new tables and additive nullable columns on either side, no shared object),
so appending rather than interleaving is safe.

**Renumbering changes revision IDs.** A database already stamped at one of
the OLD semantic ids (``0073_resource_source_tags``, ``0074_semantic_
feedback``, ``0075_column_meta_source_ref``, ``0075_semantic_health_mutes``,
``0076_semantic_draft_pending``, ``0077_semantic_models_detach``) will fail
loudly with "Can't locate revision" rather than silently believing itself at
head — re-stamp it at the matching new id (``alembic stamp <new id>``) before
upgrading. Only pre-merge development instances of this feature branch can be
in that state; nothing released ever carried these ids.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# Kept to 32 chars — alembic_version.version_num is VARCHAR(32); a longer id
# truncates and breaks every later revision's WHERE clause (verified live:
# StringDataRightTruncation on this exact migration during development).
revision: str = "0085_column_meta_source_ref"
down_revision: Union[str, None] = "0084_fact_alias_sources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("column_metadata", sa.Column("source_ref", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("column_metadata", "source_ref")
