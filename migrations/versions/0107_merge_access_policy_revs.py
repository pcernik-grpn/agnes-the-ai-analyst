"""Re-converge the access-policy-revisions and corpus-extraction lineages.

Empty by design — a merge revision carries NO schema change. It exists
because two branches each extended the Alembic chain in parallel from a
parent both already carried (``0096_users_kind``), so merging them left the
repository with two heads:

- ``0097_access_policy_revisions`` — table access-policy revision history
  (#1979), ``0096_users_kind`` -> here
- ``0106_hot_path_indexes``        — the corpus-extraction chain
  (``0096_users_kind`` -> ``0102_merge_users_kind_fts`` -> ``0103_crawl_shards``
  -> ``0104_extraction_conditions`` -> ``0105_fact_collection_stats`` -> here)

With two heads, ``alembic upgrade head`` refuses ("Multiple head revisions
are present for given argument 'head'"), which fails every Postgres-backed
test at fixture setup and would fail a real deployment's migrate step the
same way. Naming both as ``down_revision`` re-converges the graph: each
branch's revisions keep their own identity and order, and everything after
this point chains onto a single head again. A database already stamped at
EITHER head upgrades cleanly — Alembic applies only the other lineage's
revisions and then this one. Same precedent as ``0102_merge_users_kind_fts``.

Revision ID: 0107_merge_access_policy_revs
Revises: 0106_hot_path_indexes, 0097_access_policy_revisions
Create Date: 2026-09-04
"""

from typing import Sequence, Union

revision: str = "0107_merge_access_policy_revs"
down_revision: Union[str, Sequence[str], None] = ("0106_hot_path_indexes", "0097_access_policy_revisions")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge point only — no schema change."""


def downgrade() -> None:
    """Merge point only — no schema change."""
