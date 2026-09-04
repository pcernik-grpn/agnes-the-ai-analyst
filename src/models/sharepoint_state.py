"""SQLAlchemy model behind ``sharepoint_connection_state`` — the per-
connection crawl and facts-extraction bookkeeping the SharePoint pipeline
used to keep as a JSON file on the worker's local disk.

PG-ONLY (A3 PG-first ratchet — ``CLAUDE.md`` -> "Dual-backend discipline"):
this table landed after the DuckDB app-state backend was frozen, so there is
no ``src/db.py`` ladder step and no DuckDB repository sibling. See
``docs/migrations.md`` -> "Adding a PG-only feature (post-A3)".

Why this table exists — the same problem ``0094_extraction_runs.py`` names
for run *observability*, here for run *resume state*: jobs are claimed from
a shared Postgres queue (``src/repositories/jobs_pg.py``, ``claim_next``
with ``SKIP LOCKED``), so any extraction worker on any host can pick up any
connection's job. A per-connection state file under ``DATA_DIR`` only works
when every worker shares that disk — a deployment coincidence the Compose
topology happens to provide today, not a contract, and it breaks the moment
a worker runs on a second VM. Moving the state into the same database the
job queue already uses removes that coincidence.

One row per ``(connection_id, kind)`` — ``kind`` is ``"crawl"`` (the
delta-link + cTag + failed-item bookkeeping ``connectors/sharepoint/
crawler.py`` used to keep in ``sharepoint_crawl/<connection_id>.json``),
``"facts"`` (the per-document extraction bookkeeping ``connectors/
sharepoint/facts_extraction.py`` used to keep in ``sharepoint_facts/
<connection_id>.json``), ``"crawl:<state_key>"`` (2026-09-03 auto-
parallel-crawl design §4.2 — one row per DELTA UNIT, owned by exactly one
shard child; ``state_key`` is a ``DriveTarget.state_key``, e.g. a drive id
or ``"<drive_id>:<item_id>"``), or ``"acl_snapshot:<source_scope_id>"``
(TCRD-296 gap #79 — one row per SCOPE holding that scope's informational
Graph-permissions read, ``connectors/sharepoint/acl_sync.py::
snapshot_principals``, captured for every scope regardless of
``access_mode`` — unlike the grant-mirroring half of the sync, which only
ever touches ``access_mode='mirrored'`` scopes) — deliberately separate
ROWS, never one JSON blob keyed by all of them: a corrupt facts pass must
never cost the crawl its deltaLinks, a resync must never touch the facts
corpus's own bookkeeping, two shard children must never be able to clobber
each other's cursor, and one scope's permissions snapshot must never block
on (or be lost with) another's — exactly the isolation the original two
separate legacy files gave for free, extended to N.

The connection-level ``"crawl"`` row is kept even once a connection shards:
it still carries the persisted ``shard_plan`` and, read-only, the legacy
per-drive ``ctags`` a shard's own row seeds from until its first fully-done
sharded run (see ``connectors/sharepoint/crawler.py``'s ``legacy_ctags``
docstring) — never written to by a shard child.

``payload`` keeps the exact same JSON shape either module already wrote to
its file — nothing about ``crawler.py``'s or ``facts_extraction.py``'s own
state contract changes, only where the bytes live. See
``connectors/sharepoint/state_store.py`` for the backend-selection seam
(Postgres here when the active app-state backend is Postgres, the
pre-existing JSON file otherwise) and its one-time legacy-file import.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SharepointConnectionState(Base):
    __tablename__ = "sharepoint_connection_state"
    __table_args__ = (
        sa.CheckConstraint(
            "kind IN ('crawl', 'facts') OR kind LIKE 'crawl:%' OR kind LIKE 'acl_snapshot:%'",
            name="ck_sharepoint_connection_state_kind",
        ),
    )

    connection_id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    kind: Mapped[str] = mapped_column(sa.String, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
