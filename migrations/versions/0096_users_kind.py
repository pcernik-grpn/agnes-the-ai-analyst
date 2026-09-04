"""users.kind — service-account identities (issue #1534).

PG-first ratchet (A3): a genuine schema change on an existing DuckDB<->PG
pair follows "Adding a PG-only feature"
(``.claude/skills/agnes-conventions/references/migration.md``) — the column
lands here only, same precedent as ``0082_session_revoked_before``. There is
no matching DuckDB ``_vN_to_v(N+1)`` step and ``SCHEMA_VERSION`` does not
move; ``src/repositories/users.py`` (DuckDB) gains no capability that depends
on this column — ``create_service_account`` / ``list_service_accounts`` are
documented ``RequiresPostgresBackend`` raises there.

Context: every non-human identity Agnes has shipped so far (the scheduler's
shared-secret user, the semantic-drafter, the memory-curator) is a bare
``users`` row nobody flags as anything special — indistinguishable from a
human account except by its synthetic ``@system.local`` address and the
absence of a password. Issue #1534 asks for a real, queryable identity kind
so an admin can provision a headless caller (CI, an integration, a bot) with
its OWN group grants and its OWN independently-revocable PATs, without that
caller ever being able to hold an interactive browser session or land in the
Admin group by accident.

``kind`` is a plain enum-shaped TEXT column, not a foreign key or a separate
table: every consumer (the session-mint guard in ``app/auth/jwt.py``, the
Admin-group-membership guard in ``user_group_members(_pg)``, the people-picker
exclusion in ``search_recent``) already loads the full ``users`` row it needs
for other reasons, so a column read costs nothing extra. ``'human'`` is the
default for every existing and future plain row — only
``UsersPgRepository.create_service_account`` ever writes ``'service'``.

No backfill needed: every row that exists at deploy time is a human (or an
internal system identity that behaves like one) by construction, and the
column default handles them for free.

Revision ID: 0096_users_kind
Revises: 0095_memory_detection_runs
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0096_users_kind"
down_revision: Union[str, None] = "0095_memory_detection_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "users" not in set(insp.get_table_names()):
        return

    cols = {c["name"] for c in insp.get_columns("users")}
    if "kind" not in cols:
        op.add_column(
            "users",
            # String (VARCHAR), matching the other short text columns on this
            # model (email, name, password_hash) — must match
            # src/models/rbac.py::User.kind exactly or
            # test_no_model_migration_drift flags a type-change diff.
            sa.Column("kind", sa.String(), nullable=False, server_default=sa.text("'human'")),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "users" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "kind" in cols:
        op.drop_column("users", "kind")
