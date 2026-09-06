"""sharepoint_crawl_items — per-file crawl bookkeeping split OUT of
``sharepoint_connection_state.payload`` (measured incident: a connection
with a few hundred thousand documents grew a single ``ctags`` map to ~26 MB,
``empty_items`` to ~11 MB and ``failed_items`` to ~2 MB inside that ONE
row's ``payload``; every crawl checkpoint rewrote the whole thing, and since
Postgres UPDATE always produces a brand-new toasted value for a changed
``jsonb`` column — even via ``jsonb_set`` targeting one key — every
checkpoint orphaned the PREVIOUS ~21 MB of TOAST chunks. Measured on the
running instance: ~21,900 dead TOAST tuples/minute, ~60 GB/day of table
growth while a crawl runs, autovacuum reclaiming for reuse but never
returning it to the OS).

One row per ``(connection_id, kind, stable_id)`` — ``kind`` matches
``connectors/sharepoint/crawler.py``'s ``_crawl_state_kind()`` (``"crawl"``
for the connection-level row, ``"crawl:<shard_key>"`` for a shard's own),
so the shard seam that already isolates ``sharepoint_connection_state`` rows
isolates these too. ``ctag``/``failed_entry``/``empty_entry`` are
independently nullable: a file's row only ever gets the columns its own
lifecycle has touched (a ctag once ingested; failed/empty on their own
outcomes; success clears whichever of the last two was set). Updating ONE
file's row is a single-row UPDATE touching only that row's own (small)
tuple — the fix's whole point: a checkpoint that changes N files now costs
O(N), not O(every file this connection has ever seen).

``sharepoint_connection_state.payload`` keeps only the genuinely
whole-state fields (``delta_links``, ``last_run``, ``shard_plan``, the
resync markers, ...) after this split — see
``connectors/sharepoint/state_store.py``'s ``crawl_items_get``/
``crawl_items_apply`` for the read/write seam, and
``connectors/sharepoint/crawler.py``'s ``load_state``/``save_state`` for
the one-time import of a connection's pre-split embedded copies (a live
crawl mid-flight when this change deploys must not lose its already-seen
ctags).

PG-first ratchet (A3): brand-new app-state table, Alembic-only — no
matching DuckDB ``_vN_to_v(N+1)`` step, ``SCHEMA_VERSION`` does not move.
The DuckDB fallback (frozen backend) is UNTOUCHED by this migration: it
keeps ``ctags``/``failed_items``/``empty_items`` embedded in its existing
per-connection JSON file exactly as before — a local file rewrite has none
of the TOAST/dead-tuple pathology this table exists to avoid.

Revision ID: 0110_sharepoint_crawl_items
Revises: 0109_merge_access_stack
Create Date: 2026-09-05
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0110_sharepoint_crawl_items"
down_revision: Union[str, None] = "0109_merge_access_stack"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sharepoint_crawl_items",
        sa.Column("connection_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("stable_id", sa.String(), nullable=False),
        sa.Column("ctag", sa.String(), nullable=True),
        sa.Column("failed_entry", JSONB(), nullable=True),
        sa.Column("empty_entry", JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("connection_id", "kind", "stable_id"),
    )


def downgrade() -> None:
    op.drop_table("sharepoint_crawl_items")
