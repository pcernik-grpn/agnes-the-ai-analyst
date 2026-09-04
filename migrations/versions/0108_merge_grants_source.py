"""Re-converge the grant-provenance lineage with main's.

Empty by design — a merge revision carries NO schema change. It exists
because two branches each extended the chain in parallel from a parent both
already carried (``0096_users_kind``), so merging them left the repository
with two heads:

- ``0096_resource_grants_source`` — grant provenance (#1956 item 13),
  ``0096_users_kind`` -> here
- ``0107_merge_access_policy_revs`` — everything main has shipped since
  (``0096_users_kind`` -> ``0102_merge_users_kind_fts`` -> ... ->
  ``0106_hot_path_indexes`` + ``0097_access_policy_revisions`` -> here)

With two heads, ``alembic upgrade head`` refuses ("Multiple head revisions
are present for given argument 'head'"), which fails every Postgres-backed
test at fixture setup and a real deployment's migrate step the same way.
Naming both as ``down_revision`` re-converges the graph: each branch's
revisions keep their own identity and order, and everything after this point
chains onto a single head again. A database already stamped at EITHER head
upgrades cleanly — Alembic applies only the other lineage's revisions and
then this one.

The two lineages touch disjoint tables — ``resource_grants`` /
``marketplace_plugins`` on this side, the extraction and corpus tables on
main's — so the order they converge in changes nothing. Same precedent as
``0102_merge_users_kind_fts`` and ``0107_merge_access_policy_revs``.

Revision ID: 0108_merge_grants_source
Revises: 0096_resource_grants_source, 0107_merge_access_policy_revs
Create Date: 2026-09-04
"""

from typing import Sequence, Union

revision: str = "0108_merge_grants_source"
down_revision: Union[str, Sequence[str], None] = (
    "0096_resource_grants_source",
    "0107_merge_access_policy_revs",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge point only — no schema change."""


def downgrade() -> None:
    """Merge point only — no schema change."""
