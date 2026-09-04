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

Re-parented onto ``0096_users_kind`` (and before that onto
``0095_memory_detection_runs``): each time a sibling landed on main first and
claimed the same parent, and two revisions sharing one ``down_revision`` is a
branched script directory — ``ensure_pg_at_head`` then refuses to start the
app with "multiple heads". Chaining behind the sibling keeps the ladder
linear, which is what that check is protecting. The shared ``0096`` prefix is
cosmetic and has precedent (``0094_usage_turns`` -> ``0094_extraction_runs``);
the revision id is what the chain is keyed on.
"""

from typing import Sequence, Union

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision: str = "0096_resource_grants_source"
down_revision: Union[str, None] = "0096_users_kind"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existence-checked, not a bare ``add_column``. Where a grant was written (#1956 item 13).
    #
    # A bare ``op.add_column`` raises ``DuplicateColumn`` on a database that
    # already has the column, and this revision reaches such databases: any
    # instance that ever ran an image built from the branch this column was
    # developed on has it without the revision being stamped. That is not a
    # hypothetical — it happened the day the access stack shipped, and because
    # boot is strict (``app``/``scheduler``/``worker`` wait for a successful
    # ``migrate``), the failure is not a warning in a log but a 502 for
    # however long it takes someone to notice and drop the column by hand.
    #
    # The end state is identical either way, so skipping is the safe arm: a
    # column that is already there needs no adding, and refusing to boot over
    # it buys nothing. Deliberately NOT ``IF NOT EXISTS`` in raw SQL — the
    # inspector keeps this revision engine-agnostic and readable, and says in
    # the log which arm it took.
    if "source" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("resource_grants")}:
        logger.info(
            "source already exists on resource_grants — leaving it alone. An image built "
            "from the development branch added it before this revision was stamped."
        )
        return
    op.add_column(
        "resource_grants",
        sa.Column("source", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resource_grants", "source")
