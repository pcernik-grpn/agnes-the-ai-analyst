"""llm_usage.caller_user_id — WHO incurred this usage row (remediation
Track C, task C2.4).

PG-first ratchet (A3): a genuine schema change on an existing DuckDB<->PG
pair follows "Adding a PG-only feature"
(``.claude/skills/agnes-conventions/references/migration.md``) — the column
lands here only, mirroring ``0073_agent_scope_granted_by``'s precedent.
There is no matching DuckDB ``_vN_to_v(N+1)`` step and ``SCHEMA_VERSION``
does not move; ``src/repositories/llm_usage.py`` (DuckDB) gains no
capability that depends on this column (its ``insert_batch`` accepts the
same ``caller_user_id`` row key for call-site symmetry across both repos,
but has nothing to persist it into).

Context (``docs/superpowers/plans/2026-08-26-one-agent-model.md`` §C2.4): a
shared agent (C2.3) can be run by many callers, but usage rows were
attributed to the agent only. ``caller_user_id`` records WHICH caller
incurred each row, alongside the existing ``user_id`` column (unchanged —
still the agent's owner, written by the broker's usage-recording path). No
backfill: an existing row predates per-caller attribution entirely (every
pre-C2.3 session ran under its agent's owner, so a NULL here is honestly
"unknown/unattributed", not silently wrong) — unlike ``granted_by``, where
every pre-existing row's writer WAS knowable (the agent's owner, since
every write path was ownership-gated), there is no equivalent fact to
backfill from for who was actually driving a past turn.

Token-budget enforcement is UNCHANGED by this column — it still sums
across the whole agent (``llm_usage.agent_id``), regardless of caller.

Revision ID: 0074_llm_usage_caller_user_id
Revises: 0073_agent_scope_granted_by
Create Date: 2026-08-26
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0074_llm_usage_caller_user_id"
down_revision: Union[str, None] = "0073_agent_scope_granted_by"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "llm_usage" not in set(insp.get_table_names()):
        return

    cols = {c["name"] for c in insp.get_columns("llm_usage")}
    if "caller_user_id" not in cols:
        op.add_column("llm_usage", sa.Column("caller_user_id", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "llm_usage" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("llm_usage")}
    if "caller_user_id" in cols:
        op.drop_column("llm_usage", "caller_user_id")
