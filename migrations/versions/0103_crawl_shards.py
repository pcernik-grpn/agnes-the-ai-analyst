"""crawl_shards — parent/child columns on extraction_runs + per-delta-unit
crawl state rows (2026-09-03 auto-parallel-crawl design §4.2).

Two independent additions, one revision:

* ``extraction_runs`` gains ``parent_run_id`` (a shard child's own run row
  points at the PLANNER run it was enqueued from — ``NULL`` for every run
  that is not a shard, i.e. every run before this feature and the inline
  path forever), ``shard_key``/``shard_label`` (this shard's own identity —
  ``shard_key`` is the delta-unit key it owns, e.g. a drive id or
  ``"<drive_id>:<item_id>"``; ``shard_label`` is the human-facing folder
  path), and ``shards_total``/``shards_done`` (the PARENT's own rollup — a
  child never sets these on its own row). ``idx_extraction_runs_parent``
  is what lets the finalizer and the read side fetch every child of one
  parent in one query.
* ``sharepoint_connection_state``'s ``kind`` CHECK constraint is relaxed
  from the fixed pair ``'crawl'``/``'facts'`` to also allow any
  ``'crawl:<state_key>'`` — one row per delta unit, owned by exactly one
  shard child, so two children never write the same row. The connection-
  level ``'crawl'`` row is kept (it still carries ``shard_plan`` and,
  read-only, the legacy per-drive ``ctags`` a shard's own row seeds from
  until its first fully-done sharded run — see
  ``connectors/sharepoint/crawler.py``'s ``legacy_ctags`` docstring).

PG-first ratchet (A3): both are schema changes on brand-new / already-PG-only
app-state surfaces, Alembic-only — no matching DuckDB ``_vN_to_v(N+1)`` step,
``SCHEMA_VERSION`` does not move (the SharePoint crawl feature is PG-only by
construction; a DuckDB-backed instance never shards — see the crawler
module's own docstring).

Revision ID: 0103_crawl_shards
Revises: 0102_merge_users_kind_fts
Create Date: 2026-09-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0103_crawl_shards"
down_revision: Union[str, None] = "0102_merge_users_kind_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_KIND_CHECK = "kind IN ('crawl', 'facts')"
_NEW_KIND_CHECK = "kind IN ('crawl', 'facts') OR kind LIKE 'crawl:%'"


def upgrade() -> None:
    op.add_column("extraction_runs", sa.Column("parent_run_id", sa.String(), nullable=True))
    op.add_column("extraction_runs", sa.Column("shard_key", sa.String(), nullable=True))
    op.add_column("extraction_runs", sa.Column("shard_label", sa.String(), nullable=True))
    op.add_column("extraction_runs", sa.Column("shards_total", sa.Integer(), nullable=True))
    op.add_column("extraction_runs", sa.Column("shards_done", sa.Integer(), server_default="0", nullable=False))
    op.create_index("idx_extraction_runs_parent", "extraction_runs", ["parent_run_id"])

    op.drop_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", type_="check")
    op.create_check_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", _NEW_KIND_CHECK)


def downgrade() -> None:
    op.drop_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", type_="check")
    op.create_check_constraint("ck_sharepoint_connection_state_kind", "sharepoint_connection_state", _OLD_KIND_CHECK)

    op.drop_index("idx_extraction_runs_parent", "extraction_runs")
    op.drop_column("extraction_runs", "shards_done")
    op.drop_column("extraction_runs", "shards_total")
    op.drop_column("extraction_runs", "shard_label")
    op.drop_column("extraction_runs", "shard_key")
    op.drop_column("extraction_runs", "parent_run_id")
