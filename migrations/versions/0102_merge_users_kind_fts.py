"""Re-converge the service-account and corpus-extraction lineages into one head.

Empty by design — a merge revision carries NO schema change. It exists
because two branches each extended the Alembic chain in parallel from a
parent both already carried (``0095_memory_detection_runs``), so merging
them left the repository with two heads:

- ``0096_users_kind``              — service-account identities (#2163),
  ``0095_memory_detection_runs`` -> here
- ``0101_corpus_chunks_fts_index`` — the corpus-extraction chain
  (``0095_memory_detection_runs`` -> ``0096_sharepoint_connection_state``
  -> ``0097_facts_llm_cache`` -> ``0098_corpus_chunks_file_id_index``
  -> ``0099_ingest_runs_edges_skipped`` -> ``0100_facts_created_at`` -> here)

With two heads, ``alembic upgrade head`` refuses ("Multiple head revisions
are present for given argument 'head'"), which fails every Postgres-backed
test at fixture setup and would fail a real deployment's migrate step the
same way. Neither branch can see this on its own: each has exactly one head
in isolation, and the revision files never conflict textually.

Naming both as ``down_revision`` re-converges the graph: each branch's
revisions keep their own identity and order, and everything after this point
chains onto a single head again. A database already stamped at EITHER head
upgrades cleanly — Alembic applies only the other lineage's revisions and
then this one.

Chosen over re-chaining one lineage under the other: both lineages have
already been applied to live databases (``0096_users_kind`` on instances
tracking the release channel, the corpus chain on the instance the
extraction work targets), and rewriting a shipped revision's parent makes
Alembic believe the OTHER lineage's DDL was already applied wherever the
rewritten one was — its tables would silently never be created. A merge
revision is additive and safe on top of either lineage. Same precedent as
``0091_merge_semantic_facts`` and ``0093_merge_train23_semantic``.

Revision ID: 0102_merge_users_kind_fts
Revises: 0096_users_kind, 0101_corpus_chunks_fts_index
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0102_merge_users_kind_fts"
down_revision: str | Sequence[str] | None = (
    "0096_users_kind",
    "0101_corpus_chunks_fts_index",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: this revision only re-converges the graph."""


def downgrade() -> None:
    """No-op: splitting the head back into two branches is the inverse."""
