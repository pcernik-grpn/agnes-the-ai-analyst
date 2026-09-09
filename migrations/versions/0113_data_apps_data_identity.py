"""data_apps.data_identity — whose grants a hosted app reads data with.

``'owner'`` (the default, today's behaviour) or ``'viewer'`` (the ingress
proxy hands the container a short-lived viewer token per request and Agnes
authorizes data reads as ``owner ∩ viewer``, binding row-level policies to
the viewer — see ``app/auth/data_app_viewer.py`` and
``docs/superpowers/specs/2026-09-09-data-app-viewer-identity-and-sharing-design.md``).

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline").
``data_apps``/``data_apps_pg`` is an existing pre-A3 pair; a schema change to
its Postgres side alone is what ``docs/migrations.md`` prescribes ("A genuine
schema change on an existing pair's table … follows 'Adding a PG-only
feature'"). There is deliberately no matching ``_vN_to_v(N+1)`` step in
``src/db.py``: a DuckDB-backed row lacks the column, every reader goes
through ``src.data_apps.identity.data_identity_of`` (absent -> ``'owner'``),
and the one write path (``DataAppsRepository.set_data_identity``) raises
``RequiresPostgresBackend`` -> 501 there.

``NOT NULL`` with a ``server_default`` so existing rows read ``'owner'`` and
``tests/db_pg/test_schema_parity.py``'s PG-only-required-column check stays
satisfied (it tolerates a PG-only NOT NULL column iff it carries a default).

Revision ID: 0113_data_apps_data_identity
Revises: 0112_jobs_idem_pattern_index
Create Date: 2026-09-09
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0113_data_apps_data_identity"
down_revision: Union[str, None] = "0112_jobs_idem_pattern_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_apps" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("data_apps")}
    if "data_identity" in existing_cols:
        return
    op.add_column(
        "data_apps",
        sa.Column("data_identity", sa.String(), nullable=False, server_default="owner"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_apps" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("data_apps")}
    if "data_identity" in existing_cols:
        op.drop_column("data_apps", "data_identity")
