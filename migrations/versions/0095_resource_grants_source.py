"""resource_grants.source — which surface wrote a grant (#1956 item 13).

A grant already records WHO (``assigned_by``). It did not record WHAT, and the
two are different questions: marking a plugin Required on /admin/marketplaces
fans a grant out to every group and stamps each with the admin who clicked, so
N machine-written rows read exactly like N an admin typed on /admin/access.
The page then offers a Revoke that either fails or is silently undone by the
next sync.

POSTGRES-ONLY, deliberately. The DuckDB app-state schema is frozen at
``src/db.py::FROZEN_DUCKDB_SCHEMA_VERSION`` (A3), so this column exists on PG
alone — the same shape as ``table_registry.semantic_draft_pending_at``
(0086). A DuckDB instance simply does not gain provenance: its repository
accepts the argument and drops it, and the API reports ``None``, which the
Access page already renders as an ordinary grant.

Nullable with no backfill and no default. Every row that predates this is
genuinely of unknown origin, and ``NULL`` says so; inventing ``access_page``
for them would assert something nobody checked. ``src.grant_sources.describe``
maps both NULL and an unknown key to "no badge".
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0095_resource_grants_source"
down_revision: Union[str, None] = "0094_extraction_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "resource_grants",
        sa.Column("source", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resource_grants", "source")
