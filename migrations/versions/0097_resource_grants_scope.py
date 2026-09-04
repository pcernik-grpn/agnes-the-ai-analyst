"""resource_grants.scope — WHO a grant reaches: this group, or every account.

``Everyone`` was a group that normally held every account but was mirrored
from a Workspace group when ``AGNES_GROUP_EVERYONE_EMAIL`` was set. So the
word named two different things, and on a mirrored instance two different
*sets of people*. A grant now says which it means:

- ``NULL``       — the members of ``group_id`` (every grant written so far)
- ``'everyone'`` — every account on the instance, unconditionally

An ``'everyone'`` row keeps its ``group_id``: the column is ignored on read
rather than nulled. That is cheaper and safer than making a NOT NULL FK
nullable, it keeps the UNIQUE (group_id, resource_type, resource_id) index
doing useful work (one everyone-grant per resource, since the carrier group
is always the same), and it leaves the pre-migration row recoverable.

POSTGRES-ONLY, deliberately — same shape as ``resource_grants.source``
(0096). The DuckDB app-state ladder is frozen at
``src/db.py::FROZEN_DUCKDB_SCHEMA_VERSION`` (A3), so its sibling repository
accepts ``scope`` and drops it, and the scope-specific reads raise
``RequiresPostgresBackend`` -> 501. See ``docs/migrations.md``.

Additive and behaviour-neutral on its own: nothing writes or reads the
column until ``0098_everyone_becomes_a_scope``, which is why this is a
separate revision. A worker still on the old code and a web process on the
new one cannot disagree about who gets what while only this has run.
"""

from typing import Sequence, Union

import logging

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger("alembic.runtime.migration")

revision: str = "0097_resource_grants_scope"
down_revision: Union[str, None] = "0108_merge_grants_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existence-checked, not a bare ``add_column``. WHO a grant reaches.
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
    if "scope" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("resource_grants")}:
        logger.info(
            "scope already exists on resource_grants — leaving it alone. An image built "
            "from the development branch added it before this revision was stamped."
        )
        return
    op.add_column(
        "resource_grants",
        sa.Column("scope", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("resource_grants", "scope")
