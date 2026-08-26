"""agent_scope.granted_by — who wrote this grant (remediation Track C, C2.1)

PG-first ratchet (A3): a genuine schema change on an existing DuckDB<->PG
pair follows "Adding a PG-only feature"
(``.claude/skills/agnes-conventions/references/migration.md``) — the column
lands here only. There is no matching DuckDB ``_vN_to_v(N+1)`` step and
``SCHEMA_VERSION`` does not move; ``src/repositories/agents.py`` (DuckDB)
gains no capability that depends on this column (its ``set_scope`` accepts
the same ``granted_by`` keyword for call-site symmetry across both repos,
but has nothing to persist it into).

``granted_by`` records the identity of the caller who wrote a given
``agent_scope`` row — the write-gate half of the staged D-C2 decision
(``docs/superpowers/plans/2026-08-26-one-agent-model.md``): an admin-granted
row will (task C2.2) resolve unconditioned, a self-granted row resolves
narrowed to the granter's own live access, exactly like today's
owner-intersection. Backfilling every pre-existing row to its agent's
``owner_user_id`` is what makes that swap a no-op at cutover — every write
path that exists today is ownership-gated (``_load_agent(require_owner=
True)``, ``app/api/agents_admin.py``), so the writer of any row that
already exists was always its agent's owner.

Revision ID: 0073_agent_scope_granted_by
Revises: 0072_sync_state_id_v124
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0073_agent_scope_granted_by"
down_revision: Union[str, None] = "0072_sync_state_id_v124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "agent_scope" not in tables:
        return

    cols = {c["name"] for c in insp.get_columns("agent_scope")}
    if "granted_by" not in cols:
        op.add_column("agent_scope", sa.Column("granted_by", sa.Text(), nullable=True))

    if "agents" not in tables:
        return

    # Backfill: every existing row's writer was its agent's owner — see the
    # module docstring. Only touches rows the column-add just created as
    # NULL, so a second run of this step (a replayed upgrade) is a no-op.
    bind.execute(
        sa.text(
            """
            UPDATE agent_scope
               SET granted_by = agents.owner_user_id
              FROM agents
             WHERE agent_scope.agent_id = agents.id
               AND agent_scope.granted_by IS NULL
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agent_scope" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("agent_scope")}
    if "granted_by" in cols:
        op.drop_column("agent_scope", "granted_by")
