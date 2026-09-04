"""Re-converge the access stack's lineage with main's.

Empty by design — a merge revision carries NO schema change. It exists
because two branches extended the chain in parallel from a parent both
already carried (``0107_merge_access_policy_revs``), so merging them left
the repository with two heads:

- ``0098_everyone_becomes_a_scope`` — the access stack (#2111 -> #2112 ->
  #2191 -> #2127): ``0107_merge_access_policy_revs`` +
  ``0096_resource_grants_source`` -> ``0108_merge_grants_source`` ->
  ``0097_resource_grants_scope`` -> here
- ``0108_sp_acl_snapshot_kind`` — main's newest, widening
  ``sharepoint_connection_state.kind`` for ``'acl_snapshot:<scope_id>'``
  rows (``0107_merge_access_policy_revs`` -> here)

With two heads, ``alembic upgrade head`` refuses ("Multiple head revisions
are present for given argument 'head'"), which fails every Postgres-backed
test at fixture setup and a real deployment's migrate step the same way.
Naming both as ``down_revision`` re-converges the graph: each branch's
revisions keep their own identity and order, and everything after this point
chains onto a single head again. A database already stamped at EITHER head
upgrades cleanly — Alembic applies only the other lineage's revisions and
then this one.

The two lineages touch disjoint tables — ``resource_grants`` /
``marketplace_plugins`` / ``user_groups`` on the stack's side,
``sharepoint_connection_state`` on main's — so the order they converge in
changes nothing. Same precedent as ``0102_merge_users_kind_fts``,
``0107_merge_access_policy_revs`` and ``0108_merge_grants_source``; this is
the second reconciliation the same stack has needed, because main moved
again between the first one and the merge.

Revision ID: 0109_merge_access_stack
Revises: 0098_everyone_becomes_a_scope, 0108_sp_acl_snapshot_kind
Create Date: 2026-09-04
"""

from typing import Sequence, Union

revision: str = "0109_merge_access_stack"
down_revision: Union[str, Sequence[str], None] = (
    "0098_everyone_becomes_a_scope",
    "0108_sp_acl_snapshot_kind",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge point only — no schema change."""


def downgrade() -> None:
    """Merge point only — no schema change."""
