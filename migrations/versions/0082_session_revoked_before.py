"""users.session_revoked_before — server-side session revocation (issue #1676).

PG-first ratchet (A3): a genuine schema change on an existing DuckDB<->PG
pair follows "Adding a PG-only feature"
(``.claude/skills/agnes-conventions/references/migration.md``) — the column
lands here only, same precedent as ``0074_llm_usage_caller_user_id``. There
is no matching DuckDB ``_vN_to_v(N+1)`` step and ``SCHEMA_VERSION`` does not
move; ``src/repositories/users.py`` (DuckDB) gains no capability that depends
on this column — its ``revoke_sessions()`` is a documented no-op there.

Context: a `typ="session"` JWT was trusted purely off signature + `exp` — no
DB-backed revocation existed, so the only server-side kill switch for a
leaked/stolen session was deactivating the whole account. This column is a
per-user timestamp floor: any session token whose `iat` predates it is
refused by ``app.auth.pat_resolver.resolve_token_to_user``, even though its
signature and `exp` are still fine. ``POST /auth/logout`` (issue #1675) bumps
it to "now" on logout, so a captured cookie stops working the moment the
owner logs out instead of staying valid for the rest of its 30-day `exp`.

Deliberately a per-USER floor, not a per-session one: `resolve_token_to_user`
already loads the user row on every authenticated request (to check
``active``), so comparing its `iat` against a column on that SAME row costs
no additional query — sessions are the hot path (every page view), and a
second lookup per request was rejected on cost grounds (see the PR
description). The cost is that logging out one browser also revokes every
OTHER live session for that account; a narrower, per-``jti`` revocation list
could layer on top later if single-session precision is ever needed.

No backfill: NULL means "no floor set", so every session that already exists
at deploy time keeps working until its own `exp` — this migration does not
itself log anyone out.

Revision ID: 0082_session_revoked_before
Revises: 0081_ingest_runs_anonymize
Create Date: 2026-08-28
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0082_session_revoked_before"
down_revision: Union[str, None] = "0081_ingest_runs_anonymize"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "users" not in set(insp.get_table_names()):
        return

    cols = {c["name"] for c in insp.get_columns("users")}
    if "session_revoked_before" not in cols:
        op.add_column("users", sa.Column("session_revoked_before", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "users" not in set(insp.get_table_names()):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "session_revoked_before" in cols:
        op.drop_column("users", "session_revoked_before")
